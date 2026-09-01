from __future__ import annotations

from thesis.legacy_candidate_adapter import build_legacy_candidate_cards, legacy_pool_rows
from thesis.market_environment_report import build_market_environment_from_legacy


def _payload() -> dict[str, object]:
    return {
        "meta": {
            "trade_date": "20260831",
            "prev_date": "20260828",
            "generated_at": "2026-09-01 03:31:30",
            "total_limit_up": 86,
            "prev_total": 81,
            "open_ratio_pct": 47.7,
            "promo_rate_pct": 45.5,
            "max_board": 6,
            "prev_max_board": 7,
            "sentiment": "退潮期",
            "themes_top": [["AI应用", 6]],
        },
        "pool_a_leaders": [
            {"code": "002855", "name": "甲", "boards": 5, "chg_pct": 10, "open_num": 6, "reason": "折叠屏", "theme_hot": False},
            {"code": "600000", "name": "乙", "boards": 1, "chg_pct": 10, "open_num": 0, "reason": "AI应用", "theme_hot": True, "theme_cnt": 3},
        ],
        "pool_b_starters": [
            {"code": "600000", "name": "乙", "boards": 1, "chg_pct": 10, "open_num": 0, "reason": "AI应用", "theme_hot": True, "theme_cnt": 3},
        ],
        "pool_c_repair": [],
    }


def test_legacy_candidates_are_deduplicated_and_include_sector_resonance() -> None:
    payload = _payload()
    assert len(legacy_pool_rows(payload)) == 2
    cards = build_legacy_candidate_cards(payload)
    assert len(cards) == 2
    by_name = {card.instrument_name: card for card in cards}
    assert "CONSECUTIVE_LIMIT_UP" in by_name["甲"].trigger_rules
    assert set(by_name["乙"].trigger_rules) == {"LIMIT_UP_POOL", "SECTOR_RESONANCE"}
    assert by_name["乙"].observations[0].coverage == "full_limit_up_pool:86"


def test_legacy_market_report_uses_stock_level_open_and_theme_fields() -> None:
    report = build_market_environment_from_legacy(_payload())
    assert report is not None
    assert report.stats.selected_limit_up_count == 2
    assert report.stats.known_open_count == 2
    assert report.stats.average_open_num == 3
    assert report.stats.high_divergence_count == 1
    assert report.stats.sector_resonance_count == 1
