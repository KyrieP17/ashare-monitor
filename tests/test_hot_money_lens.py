from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from thesis.candidate_research_adapter import CandidateResearchAdapter
from thesis.candidate_rules import RULE_LIMIT_UP
from thesis.candidates import CandidateCard, CandidateObservation, candidate_id_for
from thesis.hot_money_lens import LensConfidence, ScenarioEffect, build_hot_money_lens
from thesis.models import DataStatus, MarketRegime
from thesis.proposal_builders import DeterministicReplayProposalBuilder


DAY = date(2026, 8, 26)
NOW = datetime(2026, 8, 26, 7, 0, tzinfo=UTC)


def _candidate(metrics: dict, *, source: str = "public.ths.limit_up_pool") -> CandidateCard:
    instrument_id = "CN.SZ.002295"
    observation = CandidateObservation(
        instrument_id=instrument_id,
        instrument_name="测试标的",
        source=source,
        data_as_of=NOW,
        retrieved_at=NOW,
        status=DataStatus.AVAILABLE,
        coverage="bounded_test_snapshot",
        observation_ref_id="candidate:test:002295",
        raw_reference="fixture[002295]",
        source_snapshot_id="snapshot:test",
        reason="测试题材，尚未经公告核实",
        metrics=metrics,
    )
    return CandidateCard(
        candidate_id=candidate_id_for(DAY, instrument_id),
        trade_date=DAY,
        instrument_id=instrument_id,
        instrument_name="测试标的",
        first_seen_at=NOW,
        last_seen_at=NOW,
        hit_count=1,
        trigger_rules=[RULE_LIMIT_UP],
        reason_text="测试候选",
        source_snapshot_ids=["snapshot:test"],
        source_names=[source],
        data_as_of=NOW,
        freshness_status=DataStatus.AVAILABLE,
        observations=[observation],
    )


def _proposal(adapter: CandidateResearchAdapter, snapshot):
    return DeterministicReplayProposalBuilder().build_proposal(
        thesis_id=uuid4(),
        snapshot=snapshot,
        instrument=adapter.instrument,
        version=1,
        derived_from_revision_id=None,
        previous_revision=None,
    )


def test_hot_money_lens_classifies_role_without_emitting_trading_instruction():
    adapter = CandidateResearchAdapter(
        _candidate(
            {
                "boards": 3,
                "chg_pct": 10.0,
                "open_num": 2,
                "turnover_pct": 24.5,
            }
        )
    )
    snapshot = adapter.get_market_snapshot(DAY, [adapter.instrument]).model_copy(
        update={"market_regime": MarketRegime.DIVERGENCE}
    )
    report = build_hot_money_lens(
        snapshot,
        _proposal(adapter, snapshot),
        adapter.instrument.instrument_id,
    )

    assert report.role == "3板身位候选"
    assert "分歧环境" in report.market_fit
    assert any("2 次开板" in item for item in report.opponent_view)
    assert any("24.50%" in item for item in report.opponent_view)
    assert any("3 板身位" in item for item in report.price_in_view)
    assert report.direct_trading_allowed is False
    assert {scenario.effect for scenario in report.scenarios} == set(ScenarioEffect)
    assert report.observation_ref_ids


def test_hot_money_lens_treats_missing_structure_as_unknown_not_zero():
    adapter = CandidateResearchAdapter(_candidate({"chg_pct": 2.1}, source="manual.watchlist"))
    snapshot = adapter.get_market_snapshot(DAY, [adapter.instrument])
    report = build_hot_money_lens(
        snapshot,
        _proposal(adapter, snapshot),
        adapter.instrument.instrument_id,
    )

    assert report.role == "角色未证实"
    assert report.confidence is LensConfidence.LOW
    assert any("缺少开板次数" in item for item in report.opponent_view)
    assert any("UNKNOWN" in item for item in report.counter_signals)
    assert not any("开板次数为 0" in item for item in report.opponent_view)


def test_hot_money_lens_uses_sector_context_but_does_not_call_it_actor_intent():
    adapter = CandidateResearchAdapter(
        _candidate(
            {
                "board_name": "有色金属",
                "board_chg_pct": 3.2,
                "board_net_yi": 2.5,
                "lead_chg_pct": 9.9,
                "boards": 1,
                "open_num": 0,
            },
            source="public.sina.board_flow",
        )
    )
    snapshot = adapter.get_market_snapshot(DAY, [adapter.instrument])
    report = build_hot_money_lens(
        snapshot,
        _proposal(adapter, snapshot),
        adapter.instrument.instrument_id,
    )

    assert report.role == "板块共振观察标的"
    assert any("有色金属" in item for item in report.role_basis)
    assert any("不等同于个股资金意图" in item for item in report.opponent_view)
    assert any("不读取账户" in item for item in report.limitations)


def test_deep_research_page_exposes_optional_lens_and_review_boundary():
    page_source = (Path(__file__).resolve().parents[1] / "pages" / "7_thesis.py").read_text(
        encoding="utf-8"
    )

    assert "启用游资情绪与对手盘 Lens" in page_source
    assert "build_hot_money_lens" in page_source
    assert "不读取账户、不执行交易" in page_source
    assert "已接受研究 · 游资情绪与对手盘 Lens" in page_source
