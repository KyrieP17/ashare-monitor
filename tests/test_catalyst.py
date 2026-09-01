from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from thesis.candidate_research_adapter import CandidateResearchAdapter
from thesis.candidates import CandidateCard, CandidateObservation, candidate_id_for
from thesis.catalyst import CATALYST_METRIC_KEY
from thesis.existing_json_adapter import ExistingJsonAdapter
from thesis.gate3_models import ToolInvocationStatus, ToolName
from thesis.gate3_tools import ReadOnlyMarketTools
from thesis.models import DataStatus
from thesis.repository import SQLiteThesisRepository


ROOT = Path(__file__).resolve().parents[1]
REAL_DAY = date(2026, 8, 21)
REAL_SYMBOL = "CN.SZ.002716"


def test_catalyst_tool_returns_real_limit_up_reason_and_stable_reference(tmp_path):
    repository = SQLiteThesisRepository(tmp_path / "catalyst.sqlite")
    tools = ReadOnlyMarketTools(
        ExistingJsonAdapter(ROOT / "data"),
        repository,
        default_instruments=[REAL_SYMBOL],
    )

    context = tools.get_catalyst_context(
        REAL_SYMBOL,
        REAL_DAY,
        llm_tool_call_id="provider-catalyst-call",
    )

    assert context.status is DataStatus.AVAILABLE
    assert context.raw_text == "半年报增长+白银+湖南国资"
    assert context.raw_texts == [context.raw_text]
    assert context.source_field == "reason_type"
    assert context.observation_ref_id in context.observation_ref_ids
    assert context.observation_ref_id.startswith("obs_")
    assert context.tool_call_id == "provider-catalyst-call"
    assert "公告" in " ".join(context.limitations)

    invocation = repository.list_tool_invocations()[0]
    assert invocation.tool_name is ToolName.GET_CATALYST_CONTEXT
    assert invocation.status is ToolInvocationStatus.SUCCEEDED
    assert invocation.llm_tool_call_id == "provider-catalyst-call"
    assert invocation.returned_observation_ref_ids == context.observation_ref_ids
    repository.close()


def test_catalyst_tool_returns_missing_without_fabricating_text(tmp_path):
    now = datetime(2026, 8, 27, 7, 0, tzinfo=UTC)
    instrument_id = "CN.SH.600000"
    observation = CandidateObservation(
        instrument_id=instrument_id,
        instrument_name="缺失催化剂测试",
        source="public.sina.board_flow",
        data_as_of=now,
        retrieved_at=now,
        status=DataStatus.AVAILABLE,
        coverage="board_flow:1",
        observation_ref_id="candidate:sector:600000",
        raw_reference="board_flow[银行]",
        source_snapshot_id="snapshot:missing-catalyst",
        reason="板块共振不是涨停池催化剂字段",
        metrics={"board_name": "银行", "board_chg_pct": 1.2},
    )
    card = CandidateCard(
        candidate_id=candidate_id_for(now.date(), instrument_id),
        trade_date=now.date(),
        instrument_id=instrument_id,
        instrument_name=observation.instrument_name,
        first_seen_at=now,
        last_seen_at=now,
        hit_count=1,
        trigger_rules=["SECTOR_RESONANCE"],
        reason_text="板块共振",
        source_snapshot_ids=[observation.source_snapshot_id],
        source_names=[observation.source],
        data_as_of=now,
        freshness_status=DataStatus.AVAILABLE,
        observations=[observation],
    )
    repository = SQLiteThesisRepository(tmp_path / "missing.sqlite")
    context = ReadOnlyMarketTools(
        CandidateResearchAdapter(card),
        repository,
        default_instruments=[instrument_id],
    ).get_catalyst_context(instrument_id, now.date())

    assert context.status is DataStatus.MISSING
    assert context.raw_text is None
    assert context.raw_texts == []
    assert observation.reason not in context.model_dump_json()
    assert context.observation_ref_id.startswith("obs_")
    assert "catalyst_lookup" in repository.get_snapshot(
        repository.list_tool_invocations()[0].snapshot_id
    ).stock_observations[0].membership_metrics[-1].raw_reference
    repository.close()
