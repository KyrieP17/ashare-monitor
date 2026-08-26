from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path

from thesis.existing_json_adapter import ExistingJsonAdapter
from thesis.gate3_tools import ReadOnlyMarketTools
from thesis.models import DiscoverySource
from thesis.openai_provider import (
    DEFAULT_OPENAI_MODEL,
    OpenAIProviderAdapter,
    RequestsOpenAITransport,
)
from thesis.openai_workflow import OpenAIInitialResearchWorkflow
from thesis.repository import SQLiteThesisRepository


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYMBOL = "CN.SZ.002437"
TRADE_DATE = date(2026, 8, 20)


def _estimated_cost(model: str, usage: dict[str, int]) -> dict[str, object]:
    if model != DEFAULT_OPENAI_MODEL:
        return {
            "estimated_cost_usd": None,
            "cost_note": "No estimate: OPENAI_MODEL overrides the priced default.",
        }
    input_cost = usage.get("input_tokens", 0) * 0.75 / 1_000_000
    output_cost = usage.get("output_tokens", 0) * 4.50 / 1_000_000
    return {
        "estimated_cost_usd": round(input_cost + output_cost, 6),
        "cost_note": (
            "Upper-bound estimate using the 2026-08-24 gpt-5.4-mini standard token rates; "
            "cached-input discounts are not subtracted."
        ),
    }


def main() -> None:
    model = os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    timeout_seconds = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "45"))
    with tempfile.TemporaryDirectory(prefix="ashare-openai-smoke-") as temp_dir:
        repository = SQLiteThesisRepository(Path(temp_dir) / "smoke.sqlite")
        try:
            tools = ReadOnlyMarketTools(
                ExistingJsonAdapter(PROJECT_ROOT / "data"),
                repository,
                default_instruments=[SYMBOL],
            )
            provider = OpenAIProviderAdapter(
                RequestsOpenAITransport(),
                tools,
                model=model,
                timeout_seconds=timeout_seconds,
            )
            result = OpenAIInitialResearchWorkflow(repository, provider).start_initial_thesis(
                SYMBOL,
                TRADE_DATE,
                DiscoverySource.MANUAL_SEARCH,
                "OpenAI M1 live smoke",
            )
            card = repository.get_card(result.thesis_id)
            payload = {
                "provider": "OpenAI",
                "model": model,
                "status": result.status.value,
                "symbol": SYMBOL,
                "trade_date": TRADE_DATE.isoformat(),
                "latency_seconds": round(result.latency_seconds, 3),
                "tool_rounds": result.tool_rounds,
                "tool_invocation_count": len(result.tool_invocations),
                "tool_trace": [
                    {
                        "tool_name": item.tool_name.value,
                        "llm_tool_call_id": item.llm_tool_call_id,
                        "status": item.status.value,
                        "snapshot_id": str(item.snapshot_id) if item.snapshot_id else None,
                        "observation_ref_count": len(item.returned_observation_ref_ids),
                        "observation_ref_sample": item.returned_observation_ref_ids[:2],
                    }
                    for item in result.tool_invocations
                ],
                "usage": result.usage,
                "proposal_revision_id": str(result.proposal_revision_id),
                "accepted_pointer": (
                    str(card.current_accepted_revision_id)
                    if card.current_accepted_revision_id is not None
                    else None
                ),
                "lifecycle_status": card.lifecycle_status.value,
                **_estimated_cost(model, result.usage),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        finally:
            repository.close()


if __name__ == "__main__":
    main()
