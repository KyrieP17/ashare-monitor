from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from thesis.candidate_repository import SQLiteCandidateRepository
from thesis.models import DataStatus
from thesis.price_volume import PublicPriceVolumeTool


DATES = [
    "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14", "2026-08-17",
    "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21", "2026-08-24",
    "2026-08-25",
]
BEIJING = timezone(timedelta(hours=8))
AFTER_CLOSE = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def rows(
    closes=(100, 105, 110, 100, 90, 95, 100, 105, 110, 108, 120),
    volumes=(100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 200),
):
    result = []
    for day, close, volume in zip(DATES, closes, volumes, strict=True):
        result.append([day, str(close), str(close), str(close + 5), str(close - 5), str(volume)])
    return result


class FakeHistoryClient:
    def __init__(self, payload_rows=None, adjustment="qfq"):
        self.payload_rows = payload_rows or rows()
        self.adjustment = adjustment
        self.calls = []

    def daily_bars(self, instrument_id, *, end_trade_date, count):
        self.calls.append((instrument_id, end_trade_date, count))
        return {
            "source": "public.tencent.qfqkline",
            "adjustment_method": self.adjustment,
            "rows": self.payload_rows,
            "raw_reference": f"fake[{instrument_id}]",
        }


def run_context(tmp_path, *, client=None, symbol="CN.SH.600519", lookback=10, now=AFTER_CLOSE, end=date(2026, 8, 25)):
    repository = SQLiteCandidateRepository(tmp_path / "context.sqlite")
    context = PublicPriceVolumeTool(client or FakeHistoryClient(), repository, now=lambda: now).get_price_volume_context(
        symbol, end, lookback
    )
    return repository, context


@pytest.mark.parametrize(
    ("symbol", "normalized"),
    [
        ("sh600519", "CN.SH.600519"),
        ("000001", "CN.SZ.000001"),
        ("sz300750", "CN.SZ.300750"),
        ("sh688981", "CN.SH.688981"),
    ],
)
def test_supported_a_share_symbol_normalization(tmp_path, symbol, normalized):
    repository, context = run_context(tmp_path, symbol=symbol)
    repository.close()
    assert context.instrument_id == normalized


def test_only_5_or_10_day_windows_are_supported(tmp_path):
    with SQLiteCandidateRepository(tmp_path / "invalid.sqlite") as repository:
        tool = PublicPriceVolumeTool(FakeHistoryClient(), repository, now=lambda: AFTER_CLOSE)
        with pytest.raises(ValueError, match="5 or 10"):
            tool.get_price_volume_context("sh600519", date(2026, 8, 25), 6)


def test_5_day_window_returns_5d_and_marks_10d_metrics_missing(tmp_path):
    repository, context = run_context(tmp_path, lookback=5)
    repository.close()
    assert context.trading_days == 6
    assert context.return_5d_pct == pytest.approx(26.32)
    assert context.return_10d_pct is None
    assert context.distance_to_10d_high_pct is None
    assert context.max_close_drawdown_10d_pct is None
    assert context.status is DataStatus.MISSING


def test_10_day_formulas_and_missing_turnover(tmp_path):
    repository, context = run_context(tmp_path)
    repository.close()
    assert context.return_5d_pct == pytest.approx(26.32)
    assert context.return_10d_pct == pytest.approx(20.0)
    assert context.latest_completed_volume_vs_prev5_avg == pytest.approx(2.0)
    assert context.distance_to_10d_high_pct == pytest.approx(4.0)
    assert context.max_close_drawdown_10d_pct == pytest.approx(18.18)
    assert context.latest_turnover_pct is None
    assert context.adjustment_method == "qfq"
    assert all(bar.amount is None and bar.turnover_pct is None for bar in context.daily_bars)


def test_non_trading_end_date_uses_latest_prior_bar(tmp_path):
    repository, context = run_context(tmp_path, end=date(2026, 8, 30))
    repository.close()
    assert context.daily_bars[-1].trade_date == date(2026, 8, 25)
    assert context.end_trade_date == date(2026, 8, 30)
    assert any("数据源最新 Bar 为 2026-08-25" in item for item in context.limitations)


def test_insufficient_history_never_fills_missing_with_zero(tmp_path):
    client = FakeHistoryClient(rows()[:4])
    repository, context = run_context(tmp_path, client=client)
    repository.close()
    assert context.return_5d_pct is None
    assert context.return_10d_pct is None
    assert context.latest_completed_volume_vs_prev5_avg is None
    assert context.max_close_drawdown_10d_pct is None
    assert any("有效交易日不足" in item for item in context.limitations)


def test_unfinished_bar_is_excluded_from_completed_volume_ratio(tmp_path):
    current_dates = DATES[:-1] + ["2026-08-26"]
    unfinished_rows = rows(volumes=(100, 100, 100, 100, 100, 100, 100, 100, 100, 200, 999))
    for item, day in zip(unfinished_rows, current_dates, strict=True):
        item[0] = day
    now = datetime(2026, 8, 26, 11, 0, tzinfo=BEIJING)
    repository, context = run_context(
        tmp_path,
        client=FakeHistoryClient(unfinished_rows),
        now=now,
        end=date(2026, 8, 26),
    )
    repository.close()
    assert context.current_bar_complete is False
    assert context.latest_completed_volume_vs_prev5_avg == pytest.approx(2.0)
    assert any("尚未收盘" in item for item in context.limitations)
    assert any(flag.metric == "current_bar_complete" for flag in context.deterministic_price_in_flags)


def test_max_close_drawdown_ignores_intraday_high_low(tmp_path):
    monotonic = rows(closes=tuple(range(100, 111)))
    for item in monotonic:
        item[3] = "999"
        item[4] = "1"
    repository, context = run_context(tmp_path, client=FakeHistoryClient(monotonic))
    repository.close()
    assert context.max_close_drawdown_10d_pct == 0


def test_repeated_same_payload_reuses_stable_reference(tmp_path):
    database = tmp_path / "stable.sqlite"
    client = FakeHistoryClient()
    with SQLiteCandidateRepository(database) as repository:
        first = PublicPriceVolumeTool(client, repository, now=lambda: AFTER_CLOSE).get_price_volume_context(
            "sh600519", date(2026, 8, 25), 10
        )
        second = PublicPriceVolumeTool(client, repository, now=lambda: AFTER_CLOSE + timedelta(minutes=5)).get_price_volume_context(
            "CN.SH.600519", date(2026, 8, 25), 10
        )
        stored = repository.list_price_volume_contexts(instrument_id="CN.SH.600519")
    assert first == second
    assert first.observation_ref_id == second.observation_ref_id
    assert len(stored) == 1


def test_conflicting_payload_is_preserved_and_marked(tmp_path):
    database = tmp_path / "conflict.sqlite"
    with SQLiteCandidateRepository(database) as repository:
        first = PublicPriceVolumeTool(FakeHistoryClient(), repository, now=lambda: AFTER_CLOSE).get_price_volume_context(
            "sh600519", date(2026, 8, 25), 10
        )
        changed = rows()
        changed[-1][2] = "121"
        changed[-1][3] = "126"
        second = PublicPriceVolumeTool(FakeHistoryClient(changed), repository, now=lambda: AFTER_CLOSE + timedelta(minutes=1)).get_price_volume_context(
            "sh600519", date(2026, 8, 25), 10
        )
        stored = repository.list_price_volume_contexts(instrument_id="CN.SH.600519")
    assert first.observation_ref_id != second.observation_ref_id
    assert second.status is DataStatus.CONFLICTED
    assert len(stored) == 2
    assert all(item.status is DataStatus.CONFLICTED for item in stored)


def test_sqlite_reopen_reads_context(tmp_path):
    database = tmp_path / "reopen.sqlite"
    with SQLiteCandidateRepository(database) as repository:
        context = PublicPriceVolumeTool(FakeHistoryClient(), repository, now=lambda: AFTER_CLOSE).get_price_volume_context(
            "sh600519", date(2026, 8, 25), 10
        )
    with SQLiteCandidateRepository(database) as reopened:
        loaded = reopened.get_price_volume_context(context.observation_ref_id)
    assert loaded == context


def test_candidate_page_has_on_demand_unambiguous_labels():
    source = (Path(__file__).resolve().parents[1] / "pages" / "6_candidates.py").read_text(encoding="utf-8")
    assert "查看价格行为" in source
    assert "完整交易日成交量 / 前5日均量" in source
    assert "旧数据源盘中量比/供应商口径" in source
    assert "最大收盘回撤基于每日收盘价序列" in source
    assert "PublicPriceVolumeTool" in source
    assert '"RESEARCH_FOCUS": "用户研究池"' in source
