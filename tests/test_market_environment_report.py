from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from thesis.candidate_rules import RULE_CONSECUTIVE, RULE_SECTOR
from thesis.candidates import CandidateCard, CandidateObservation, candidate_id_for
from thesis.market_environment_report import (
    EnvironmentConfidence,
    MarketEnvironmentReport,
    build_market_environment_from_legacy,
    build_market_environment_report,
)
from thesis.models import DataStatus, MarketRegime


NOW = datetime(2026, 8, 26, 7, 0, tzinfo=UTC)


def _card(
    trade_date: date,
    code: str,
    *,
    total_limit_up: int,
    boards: int,
    open_num: int | None,
    sector: bool = False,
) -> CandidateCard:
    instrument_id = f"CN.SZ.{code}"
    limit_observation = CandidateObservation(
        instrument_id=instrument_id,
        instrument_name=f"测试{code}",
        source="public.ths.limit_up_pool",
        data_as_of=NOW,
        retrieved_at=NOW,
        status=DataStatus.AVAILABLE,
        coverage=f"full_limit_up_pool:{total_limit_up}",
        observation_ref_id=f"obs:limit:{trade_date}:{code}",
        raw_reference=f"limit[{code}]",
        source_snapshot_id=f"snapshot:limit:{trade_date}",
        reason="测试题材",
        metrics={"boards": boards, "open_num": open_num, "chg_pct": 10.0},
    )
    observations = [limit_observation]
    rules = [RULE_CONSECUTIVE]
    if sector:
        observations.append(
            CandidateObservation(
                instrument_id=instrument_id,
                instrument_name=f"测试{code}",
                source="public.sina.board_flow",
                data_as_of=NOW,
                retrieved_at=NOW,
                status=DataStatus.AVAILABLE,
                coverage="top_board_flows:60",
                observation_ref_id=f"obs:sector:{trade_date}:{code}",
                raw_reference=f"sector[{code}]",
                source_snapshot_id=f"snapshot:sector:{trade_date}",
                reason="测试板块",
                metrics={"board_name": "测试板块", "board_chg_pct": 2.0},
            )
        )
        rules.append(RULE_SECTOR)
    return CandidateCard(
        candidate_id=candidate_id_for(trade_date, instrument_id),
        trade_date=trade_date,
        instrument_id=instrument_id,
        instrument_name=f"测试{code}",
        first_seen_at=NOW,
        last_seen_at=NOW,
        hit_count=1,
        trigger_rules=rules,
        reason_text="测试候选",
        source_snapshot_ids=[item.source_snapshot_id for item in observations],
        source_names=[item.source for item in observations],
        data_as_of=NOW,
        freshness_status=DataStatus.AVAILABLE,
        observations=observations,
    )


def test_market_environment_identifies_height_with_contracting_breadth_as_divergence():
    previous = date(2026, 8, 25)
    current = date(2026, 8, 26)
    cards = [
        _card(previous, "000001", total_limit_up=65, boards=5, open_num=1),
        _card(previous, "000002", total_limit_up=65, boards=4, open_num=2),
        _card(previous, "000003", total_limit_up=65, boards=2, open_num=3),
        _card(current, "000004", total_limit_up=52, boards=5, open_num=2),
        _card(current, "000005", total_limit_up=52, boards=4, open_num=5, sector=True),
        _card(current, "000006", total_limit_up=52, boards=2, open_num=7),
    ]

    report = build_market_environment_report(cards)

    assert report is not None
    assert report.regime is MarketRegime.DIVERGENCE
    assert report.confidence is EnvironmentConfidence.MEDIUM
    assert report.stats.total_limit_up == 52
    assert report.stats.previous_total_limit_up == 65
    assert report.stats.total_limit_up_change_pct == pytest.approx(-20.0)
    assert report.stats.selected_max_board == 5
    assert report.stats.average_open_num == pytest.approx(14 / 3)
    assert report.stats.sector_resonance_count == 1
    assert report.direct_trading_allowed is False
    assert any("宽度" in item for item in report.risk_signals)


def test_market_environment_keeps_missing_comparison_low_confidence():
    current = date(2026, 8, 26)
    report = build_market_environment_report(
        [_card(current, "000001", total_limit_up=52, boards=2, open_num=None)]
    )

    assert report is not None
    assert report.regime is MarketRegime.UNKNOWN
    assert report.confidence is EnvironmentConfidence.LOW
    assert report.stats.average_open_num is None
    assert any("缺少开板次数" in item for item in report.risk_signals)


def test_market_overview_and_stock_lens_share_the_environment_report():
    root = Path(__file__).resolve().parents[1]
    home = (root / "home.py").read_text(encoding="utf-8")
    thesis_page = (root / "pages" / "7_thesis.py").read_text(encoding="utf-8")

    assert "柚子视角 · 市场环境报告" in home
    assert "build_market_environment_report" in home
    assert "build_market_environment_from_legacy" in home
    assert "这是一份历史市场环境快照" in home
    assert "candidate_market_environment.regime" in thesis_page


def test_daily_legacy_artifact_builds_a_typed_current_market_report():
    report = build_market_environment_from_legacy(
        {
            "meta": {
                "trade_date": "20260831",
                "prev_date": "20260828",
                "total_limit_up": 86,
                "prev_total": 81,
                "open_ratio_pct": 47.7,
                "promo_rate_pct": 45.5,
                "max_board": 6,
                "prev_max_board": 7,
                "sentiment": "退潮期",
                "themes_top": [["AI应用", 6], ["机器人", 4]],
            }
        }
    )

    assert isinstance(report, MarketEnvironmentReport)
    assert report.trade_date == date(2026, 8, 31)
    assert report.regime is MarketRegime.RETREAT
    assert report.stats.total_limit_up == 86
    assert report.stats.open_ratio_pct == 47.7
    assert report.stats.promotion_rate_pct == 45.5
    assert report.stats.sector_resonance_count is None
    assert report.direct_trading_allowed is False
