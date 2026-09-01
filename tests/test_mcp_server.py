from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator

from thesis.candidate_repository import SQLiteCandidateRepository
from thesis.candidate_research_adapter import CandidateResearchAdapter
from thesis.candidate_rules import RULE_LIMIT_UP, RULE_SECTOR
from thesis.candidates import CandidateCard, CandidateObservation, candidate_id_for
from thesis.gate3_generator import GenerationRequest, RecordedProposalGenerator
from thesis.gate3_tools import ReadOnlyMarketTools
from thesis.mcp_server import MCPResearchService, create_mcp_server
from thesis.models import DataStatus, ReviewDecision, ThesisLifecycleStatus
from thesis.repository import SQLiteThesisRepository


DAY = date(2026, 8, 27)
NOW = datetime(2026, 8, 27, 7, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def candidate() -> CandidateCard:
    instrument_id = "CN.SZ.002300"
    limit_up = CandidateObservation(
        instrument_id=instrument_id,
        instrument_name="真实候选测试",
        source="public.ths.limit_up_pool",
        data_as_of=NOW,
        retrieved_at=NOW,
        status=DataStatus.AVAILABLE,
        coverage="full_limit_up_pool:1",
        observation_ref_id="candidate:limit-up:002300",
        raw_reference="limit_up_pool[002300]",
        source_snapshot_id="snapshot:mcp:limit-up",
        reason="普通首板",
        metrics={"boards": 1, "price": 12.3, "chg_pct": 10.0, "open_num": 0},
    )
    sector = CandidateObservation(
        instrument_id=instrument_id,
        instrument_name="真实候选测试",
        source="public.sina.board_flow",
        data_as_of=NOW,
        retrieved_at=NOW,
        status=DataStatus.AVAILABLE,
        coverage="board_flow:1",
        observation_ref_id="candidate:sector:002300",
        raw_reference="board_flow[有色金属]",
        source_snapshot_id="snapshot:mcp:sector",
        reason="有色金属板块共振",
        metrics={
            "board_name": "有色金属",
            "board_net_yi": 2.5,
            "board_chg_pct": 3.2,
            "lead_chg_pct": 9.9,
        },
    )
    return CandidateCard(
        candidate_id=candidate_id_for(DAY, instrument_id),
        trade_date=DAY,
        instrument_id=instrument_id,
        instrument_name="真实候选测试",
        first_seen_at=NOW,
        last_seen_at=NOW,
        hit_count=1,
        trigger_rules=[RULE_LIMIT_UP, RULE_SECTOR],
        reason_text="确定性规则：普通首板 + 板块共振",
        source_snapshot_ids=[limit_up.source_snapshot_id, sector.source_snapshot_id],
        source_names=[limit_up.source, sector.source],
        data_as_of=NOW,
        freshness_status=DataStatus.AVAILABLE,
        observations=[limit_up, sector],
    )


def seed(database):
    card = candidate()
    with SQLiteCandidateRepository(database) as repository:
        repository.upsert([card])
    return card


def proposal_for(snapshot, thesis_id):
    stock = snapshot.stock_observations[0]
    return RecordedProposalGenerator().generate(
        GenerationRequest(
            thesis_id=thesis_id,
            snapshot=snapshot,
            instrument=stock.instrument,
            version=1,
            derived_from_revision_id=None,
            previous_revision=None,
            attempt=1,
        )
    )


def test_mcp_read_tools_match_direct_typed_tool_calls(tmp_path):
    database = tmp_path / "read-tools.sqlite"
    card = seed(database)
    service = MCPResearchService(database)

    market = service.get_market_snapshot(DAY.isoformat(), [card.instrument_id])
    stock = service.get_stock_observation(card.instrument_id, DAY.isoformat())
    catalyst = service.get_catalyst_context(card.instrument_id, DAY.isoformat())
    sectors = service.get_sector_observations(card.instrument_id, DAY.isoformat())
    flows = service.get_fund_flow_observations(
        DAY.isoformat(),
        card.instrument_id,
        "有色金属",
    )

    with SQLiteThesisRepository(database) as repository:
        direct = ReadOnlyMarketTools(
            CandidateResearchAdapter(card),
            repository,
            default_instruments=[card.instrument_id],
        )
        assert market == direct.get_market_snapshot(DAY, [card.instrument_id]).model_dump(mode="json")
        assert stock == direct.get_stock_observation(card.instrument_id, DAY).model_dump(mode="json")
        direct_catalyst = direct.get_catalyst_context(card.instrument_id, DAY).model_dump(mode="json")
        assert catalyst["tool_call_id"].startswith("claude-mcp:")
        catalyst_without_call_id = {**catalyst, "tool_call_id": None}
        assert catalyst_without_call_id == direct_catalyst
        assert sectors == [
            item.model_dump(mode="json")
            for item in direct.get_sector_observations(card.instrument_id, DAY)
        ]
        assert flows == [
            item.model_dump(mode="json")
            for item in direct.get_fund_flow_observations(
                DAY,
                instrument_id=card.instrument_id,
                sector_name="有色金属",
            )
        ]


def test_submit_rejects_fabricated_evidence_without_writing(tmp_path):
    database = tmp_path / "rejected.sqlite"
    card = seed(database)
    service = MCPResearchService(database)
    snapshot_payload = service.get_market_snapshot(DAY.isoformat(), [card.instrument_id])

    with SQLiteThesisRepository(database) as repository:
        snapshot = repository.get_snapshot(snapshot_payload["snapshot_id"])
    thesis_id = uuid4()
    proposal = proposal_for(snapshot, thesis_id)
    evidence = proposal["support_evidence"] or proposal["counter_evidence"]
    evidence[0]["observation_ref_ids"] = ["fabricated:observation"]

    result = service.submit_thesis_proposal(
        card.instrument_id,
        DAY.isoformat(),
        str(thesis_id),
        proposal,
        "claude-sonnet-test",
    )

    assert result["ok"] is False
    assert result["status"] == "validation_failed"
    assert any(issue["issue_code"].startswith("provenance.") for issue in result["issues"])
    with SQLiteThesisRepository(database) as repository:
        assert repository.list_cards() == []


def test_valid_mcp_submission_creates_unaccepted_claude_thesis_and_reopens(tmp_path):
    database = tmp_path / "accepted.sqlite"
    card = seed(database)
    service = MCPResearchService(database)
    server = create_mcp_server(service)

    snapshot_payload = asyncio.run(
        server._tool_manager.call_tool(
            "get_market_snapshot",
            {"trade_date": DAY.isoformat(), "symbols": [card.instrument_id]},
        )
    )
    with SQLiteThesisRepository(database) as repository:
        snapshot = repository.get_snapshot(snapshot_payload["snapshot_id"])
    thesis_id = uuid4()
    proposal = proposal_for(snapshot, thesis_id)

    result = asyncio.run(
        server._tool_manager.call_tool(
            "submit_thesis_proposal",
            {
                "instrument_id": card.instrument_id,
                "trade_date": DAY.isoformat(),
                "thesis_id": str(thesis_id),
                "proposal": proposal,
                "claude_model": "claude-sonnet-test",
            },
        )
    )

    assert result == {
        "ok": True,
        "status": "ready_for_human_review",
        "thesis_id": str(thesis_id),
        "proposal_revision_id": proposal["revision_id"],
        "accepted": False,
        "generator_kind": "claude-mcp:claude-sonnet-test",
        "next_step": "Open the local 深度研究 page and choose Accept, Modify, or Reject.",
    }
    with SQLiteThesisRepository(database) as repository:
        thesis = repository.get_card(thesis_id)
        pending = repository.list_pending_proposals(thesis_id)
        review = repository.get_proposal_review(pending[0].revision_id)
        assert thesis.lifecycle_status is ThesisLifecycleStatus.DRAFT
        assert thesis.current_accepted_revision_id is None
        assert pending[0].accepted is False
        assert review.generator_kind == "claude-mcp:claude-sonnet-test"
        repository.review_proposal(pending[0].revision_id, ReviewDecision.ACCEPT)

    with SQLiteThesisRepository(database) as reopened:
        thesis = reopened.get_card(thesis_id)
        accepted = reopened.get_current_accepted_revision(thesis_id)
        assert thesis.lifecycle_status is ThesisLifecycleStatus.ACTIVE
        assert accepted is not None and accepted.accepted is True


def test_valid_submission_from_claude_code_has_distinct_generator_kind(tmp_path):
    database = tmp_path / "claude-code.sqlite"
    card = seed(database)
    service = MCPResearchService(database)
    snapshot_payload = service.get_market_snapshot(DAY.isoformat(), [card.instrument_id])
    with SQLiteThesisRepository(database) as repository:
        snapshot = repository.get_snapshot(snapshot_payload["snapshot_id"])
    thesis_id = uuid4()
    proposal = proposal_for(snapshot, thesis_id)

    result = service.submit_thesis_proposal(
        card.instrument_id,
        DAY.isoformat(),
        str(thesis_id),
        proposal,
        "claude-sonnet-test",
        "claude-code",
    )

    assert result["ok"] is True
    assert result["generator_kind"] == "claude-code:claude-sonnet-test"
    with SQLiteThesisRepository(database) as repository:
        pending = repository.list_pending_proposals(thesis_id)
        review = repository.get_proposal_review(pending[0].revision_id)
        assert review.generator_kind == "claude-code:claude-sonnet-test"
        assert review.graph_trace[0] == "claude_code"


def test_mcp_schema_is_native_and_tools_are_distinctly_exposed(tmp_path):
    server = create_mcp_server(MCPResearchService(tmp_path / "schema.sqlite"))
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}

    assert set(tools) == {
        "get_market_snapshot",
        "get_stock_observation",
        "get_catalyst_context",
        "get_sector_observations",
        "get_fund_flow_observations",
        "get_price_volume_context",
        "submit_thesis_proposal",
    }
    submit_schema = tools["submit_thesis_proposal"].parameters
    assert submit_schema["type"] == "object"
    assert "strict" not in submit_schema
    assert "proposal" in submit_schema["properties"]
    proposal_schema = submit_schema["properties"]["proposal"]
    assert {"thesis_id", "based_on_snapshot_id", "support_evidence", "accepted"} <= set(
        proposal_schema["properties"]
    )
    assert "$defs" in submit_schema
    Draft202012Validator.check_schema(submit_schema)


def test_thesis_page_labels_claude_mcp_as_manual_interactive_path():
    source = (ROOT / "pages" / "7_thesis.py").read_text(encoding="utf-8")

    assert 'generator_kind.startswith("claude-mcp:")' in source
    assert "Claude Desktop + MCP · 交互式研究" in source
    assert "不会由 PROMOTE 自动触发" in source
    assert 'generator_kind.startswith("claude-code:")' in source
    assert "AShare Monitor + Claude Code · 外接深研" in source
