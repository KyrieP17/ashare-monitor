from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from thesis.adapters import MockDemoAdapter
from thesis.proposal_builders import MockProposalBuilder
from thesis.repository import SQLiteThesisRepository
from thesis.workflow import ThesisWorkflow


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repository():
    repo = SQLiteThesisRepository()
    yield repo
    repo.close()


@pytest.fixture
def workflow(repository):
    return ThesisWorkflow(repository, MockDemoAdapter(), MockProposalBuilder())


@pytest.fixture
def historical_data_dir(tmp_path):
    """Stable 2026-08-21 adapter fixture, isolated from rolling market JSON."""

    fixture_dir = tmp_path / "historical_market_data"
    pool_dir = fixture_dir / "pool_cache"
    pool_dir.mkdir(parents=True)
    for day in ("20260820", "20260821"):
        shutil.copy(ROOT / "data" / "pool_cache" / f"{day}.json", pool_dir / f"{day}.json")

    latest = {
        "watchlist": [
            {
                "code": "sh600519",
                "name": "贵州茅台",
                "price": 1272.83,
                "prev_close": 1291.5,
                "open": 1291.5,
                "volume_hand": 33472.0,
                "amount_wan": 427831.0,
                "chg": -18.67,
                "chg_pct": -1.45,
                "high": 1291.5,
                "low": 1272.01,
                "turnover_pct": 0.27,
                "time": "20260821161449",
                "avg5_vol_hand": 42701.0,
                "vol_ratio": 0.78,
                "fund_flow": {
                    "date": "2026-08-21",
                    "main_net_wan": -70995.1,
                    "main_ratio_pct": -16.8,
                },
                "alerts": [],
            }
        ]
    }
    normalized = {
        "meta": {
            "trade_date": "20260821",
            "prev_date": "20260820",
            "generated_at": "2026-08-23 19:51:26",
            "total_limit_up": 54,
            "prev_total": 78,
            "open_ratio_pct": 59.3,
            "promo_rate_pct": 13.3,
            "max_board": 4,
            "prev_max_board": 8,
            "sentiment": "退潮期",
        },
        "pool_a_leaders": [
            {
                "code": "603958",
                "name": "哈森股份",
                "boards": 4,
                "reason": "苹果产业链+机器人概念+重组+中报预计扭亏",
                "seal_yi": 0.57,
                "float_cap_yi": 45.1,
                "role": "龙头",
                "score": 54,
                "grade": "后排跟踪",
            }
        ],
        "pool_b_starters": [],
        "pool_c_repair": [],
    }
    (fixture_dir / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False),
        encoding="utf-8",
    )
    (fixture_dir / "limit_up.json").write_text(
        json.dumps(normalized, ensure_ascii=False),
        encoding="utf-8",
    )
    return fixture_dir
