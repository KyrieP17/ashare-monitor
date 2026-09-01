from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from thesis.existing_json_adapter import ExistingJsonAdapter
from thesis.gate3_models import Gate3RunStatus, ToolInvocationStatus, ToolName
from thesis.gate3_tools import ReadOnlyMarketTools
from thesis.gate3_validation import HardProposalValidator
from thesis.models import (
    ClaimType,
    DiscoverySource,
    EvidenceDirection,
    EvidenceItem,
    EvidenceQuality,
    MarketSnapshot,
    ThesisLifecycleStatus,
)
from thesis.openai_provider import (
    OpenAIAuthenticationError,
    OpenAIProviderAdapter,
    OpenAIProviderError,
    OpenAIProviderTimeoutError,
    OpenAIQuotaError,
    OpenAIRateLimitError,
    OpenAIRequiredEvidenceError,
    OpenAIResearchRequest,
    OpenAIResponseFormatError,
    OpenAIToolLimitError,
    RequestsOpenAITransport,
    _function_calls,
    openai_tool_schemas,
)
from thesis.openai_workflow import OpenAIInitialResearchWorkflow
from thesis.proposal_builders import DeterministicReplayProposalBuilder
from thesis.repository import SQLiteThesisRepository
from thesis.symbols import normalize_symbol


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DAY = date(2026, 8, 20)
SYMBOL = "CN.SZ.002437"


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
    }


class SuccessfulFakeOpenAITransport:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def create_response(self, payload, *, timeout_seconds):
        assert timeout_seconds > 0
        self.payloads.append(payload)
        if len(self.payloads) == 1:
            return {
                "id": "resp_fake_tools",
                "output": [
                    _call(
                        "fc_market_real_id",
                        ToolName.GET_MARKET_SNAPSHOT.value,
                        {"trade_date": DAY.isoformat(), "symbols": [SYMBOL]},
                    ),
                    _call(
                        "fc_stock_real_id",
                        ToolName.GET_STOCK_OBSERVATION.value,
                        {
                            "instrument_id": SYMBOL,
                            "trade_date": DAY.isoformat(),
                            "lookback_days": 1,
                        },
                    ),
                    _call(
                        "fc_catalyst_real_id",
                        ToolName.GET_CATALYST_CONTEXT.value,
                        {
                            "instrument_id": SYMBOL,
                            "trade_date": DAY.isoformat(),
                        },
                    ),
                ],
                "usage": {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140},
            }

        assert payload["previous_response_id"] == "resp_fake_tools"
        outputs = [json.loads(item["output"]) for item in payload["input"]]
        assert all(item["ok"] for item in outputs)
        snapshot = MarketSnapshot.model_validate(outputs[0]["result"])
        marker = "Exact request identity: "
        identity = json.loads(self.payloads[0]["instructions"].split(marker, 1)[1])
        instrument = normalize_symbol(identity["instrument_id"])
        proposal = DeterministicReplayProposalBuilder().build_proposal(
            thesis_id=identity["thesis_id"],
            snapshot=snapshot,
            instrument=instrument,
            version=identity["version"],
            derived_from_revision_id=identity["derived_from_revision_id"],
            previous_revision=None,
        )
        return {
            "id": "resp_fake_proposal",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": proposal.model_dump_json()}
                    ],
                }
            ],
            "usage": {"input_tokens": 300, "output_tokens": 200, "total_tokens": 500},
        }


def _provider(repository, transport, **kwargs):
    tools = ReadOnlyMarketTools(
        ExistingJsonAdapter(DATA_DIR),
        repository,
        default_instruments=[SYMBOL],
    )
    return OpenAIProviderAdapter(transport, tools, model="fake-openai-model", **kwargs)


def test_openai_tool_schema_mapping_is_strict_and_complete():
    def contains_default(value):
        if isinstance(value, list):
            return any(contains_default(item) for item in value)
        if isinstance(value, dict):
            return "default" in value or any(contains_default(item) for item in value.values())
        return False

    schemas = openai_tool_schemas()
    assert {item["name"] for item in schemas} == {item.value for item in ToolName}
    assert all(item["type"] == "function" and item["strict"] for item in schemas)
    for item in schemas:
        parameters = item["parameters"]
        assert parameters["additionalProperties"] is False
        assert set(parameters["required"]) == set(parameters["properties"])
        assert not contains_default(parameters)
    stock = next(item for item in schemas if item["name"] == "get_stock_observation")
    assert "lookback_days" in stock["parameters"]["properties"]


def test_function_call_parser_rejects_missing_provider_call_identity():
    with pytest.raises(OpenAIResponseFormatError, match="call_id"):
        _function_calls(
            {
                "output": [
                    {"type": "function_call", "name": "get_market_snapshot", "arguments": "{}"}
                ]
            }
        )


def test_provider_enforces_six_invocation_limit_before_execution():
    class TooManyCalls:
        def create_response(self, payload, *, timeout_seconds):
            return {
                "id": "resp_too_many",
                "output": [
                    _call(
                        f"fc_{index}",
                        ToolName.GET_MARKET_SNAPSHOT.value,
                        {"trade_date": DAY.isoformat(), "symbols": [SYMBOL]},
                    )
                    for index in range(7)
                ],
            }

    repository = SQLiteThesisRepository()
    provider = _provider(repository, TooManyCalls())
    with pytest.raises(OpenAIToolLimitError, match="6 tool invocations"):
        provider.generate(OpenAIResearchRequest(uuid4(), normalize_symbol(SYMBOL), DAY))
    assert repository.list_tool_invocations() == []
    repository.close()


@pytest.mark.parametrize("failure", [TimeoutError("slow"), RuntimeError("broken")])
def test_provider_timeout_and_transport_errors_are_explicit(failure):
    class FailingTransport:
        def create_response(self, payload, *, timeout_seconds):
            raise failure

    repository = SQLiteThesisRepository()
    provider = _provider(repository, FailingTransport(), timeout_seconds=0.5)
    expected = OpenAIProviderTimeoutError if isinstance(failure, TimeoutError) else OpenAIProviderError
    with pytest.raises(expected):
        provider.generate(OpenAIResearchRequest(uuid4(), normalize_symbol(SYMBOL), DAY))
    repository.close()


def test_http_429_is_explicit_and_exposes_only_safe_diagnostics():
    class Response:
        status_code = 429
        headers = {"Retry-After": "17", "x-request-id": "req_safe_123"}

        def json(self):
            return {"error": {"code": "rate_limit_exceeded", "message": "sensitive body"}}

    class Session:
        def post(self, *args, **kwargs):
            return Response()

    transport = RequestsOpenAITransport(api_key="secret-key", session=Session())
    with pytest.raises(OpenAIRateLimitError) as captured:
        transport.create_response({"input": "fake"}, timeout_seconds=1)
    error = captured.value
    assert error.status_category == "rate_limit"
    assert error.retryable is True
    assert error.retry_after == "17"
    assert error.request_id == "req_safe_123"
    assert "HTTP 429" in str(error)
    assert "secret-key" not in str(error)
    assert "sensitive body" not in str(error)


def test_failed_typed_tool_is_audited_with_provider_call_id():
    class InvalidLookbackTransport:
        def __init__(self):
            self.calls = 0

        def create_response(self, payload, *, timeout_seconds):
            self.calls += 1
            if self.calls == 1:
                return {
                    "id": "resp_bad_tool",
                    "output": [
                        _call(
                            "fc_bad_lookback",
                            ToolName.GET_STOCK_OBSERVATION.value,
                            {
                                "instrument_id": SYMBOL,
                                "trade_date": DAY.isoformat(),
                                "lookback_days": 2,
                            },
                        )
                    ],
                }
            return {"id": "resp_no_evidence", "output_text": "{}", "output": []}

    repository = SQLiteThesisRepository()
    provider = _provider(repository, InvalidLookbackTransport())
    with pytest.raises(OpenAIRequiredEvidenceError):
        provider.generate(OpenAIResearchRequest(uuid4(), normalize_symbol(SYMBOL), DAY))
    invocation = repository.list_tool_invocations()[0]
    assert invocation.status is ToolInvocationStatus.FAILED
    assert invocation.llm_tool_call_id == "fc_bad_lookback"
    assert invocation.returned_observation_ref_ids == []
    repository.close()


def test_hard_validator_rejects_unreferenced_number_in_model_output():
    snapshot = ExistingJsonAdapter(DATA_DIR).get_market_snapshot(
        DAY, [normalize_symbol(SYMBOL)]
    )
    thesis_id = uuid4()
    proposal = DeterministicReplayProposalBuilder().build_proposal(
        thesis_id=thesis_id,
        snapshot=snapshot,
        instrument=normalize_symbol(SYMBOL),
        version=1,
        derived_from_revision_id=None,
        previous_revision=None,
    )
    unsupported = EvidenceItem(
        evidence_id=uuid4(),
        claim="Assume the setup lasts 9 trading days.",
        claim_type=ClaimType.ASSUMPTION,
        direction=EvidenceDirection.SUPPORT,
        evidence_quality=EvidenceQuality.UNKNOWN,
        quality_reason="Model assumption without tool support.",
        observed_at=snapshot.created_at,
    )
    raw = proposal.model_copy(update={"support_evidence": [unsupported]}).model_dump(mode="json")
    _, result = HardProposalValidator().validate(
        raw,
        snapshot=snapshot,
        thesis_id=thesis_id,
        version=1,
        derived_from_revision_id=None,
    )
    assert not result.is_valid
    assert any(item.issue_code == "provenance.unsourced_number" for item in result.issues)


def test_offline_fake_openai_e2e_persists_audited_pending_proposal(tmp_path):
    repository = SQLiteThesisRepository(tmp_path / "openai-fake.sqlite")
    transport = SuccessfulFakeOpenAITransport()
    provider = _provider(repository, transport)
    result = OpenAIInitialResearchWorkflow(repository, provider).start_initial_thesis(
        SYMBOL,
        DAY,
        DiscoverySource.MANUAL_SEARCH,
    )

    assert result.status is Gate3RunStatus.READY_FOR_HUMAN_REVIEW
    assert result.graph_trace[-1] == "ready_for_human_review"
    assert result.hard_validation.is_valid
    assert result.tool_rounds == 1
    assert result.provider_response_ids == ["resp_fake_tools", "resp_fake_proposal"]
    assert result.usage == {"input_tokens": 400, "output_tokens": 240, "total_tokens": 640}
    assert [item.llm_tool_call_id for item in result.tool_invocations] == [
        "fc_market_real_id",
        "fc_stock_real_id",
        "fc_catalyst_real_id",
    ]
    assert all(item.returned_observation_ref_ids for item in result.tool_invocations)
    assert not any(
        item.llm_tool_call_id in item.returned_observation_ref_ids
        for item in result.tool_invocations
    )

    card = repository.get_card(result.thesis_id)
    proposal = repository.get_revision(result.proposal_revision_id)
    assert card.lifecycle_status is ThesisLifecycleStatus.DRAFT
    assert card.current_accepted_revision_id is None
    assert proposal.accepted is False
    assert repository.list_pending_proposals(result.thesis_id) == [proposal]
    repository.close()
