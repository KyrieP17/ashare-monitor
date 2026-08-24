from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from thesis.existing_json_adapter import ExistingJsonAdapter
from thesis.models import DataStatus
from thesis.symbols import normalize_symbol


DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DAY_1 = date(2026, 8, 20)
DAY_2 = date(2026, 8, 21)


def _membership(snapshot):
    return next(
        metric
        for metric in snapshot.stock_observations[0].membership_metrics
        if metric.metric_key == "in_limit_up_pool"
    )


def _write_pool(tmp_path: Path, trade_date: date, payload: dict) -> Path:
    pool_dir = tmp_path / "pool_cache"
    pool_dir.mkdir(exist_ok=True)
    target = pool_dir / f"{trade_date.strftime('%Y%m%d')}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def _real_pool(trade_date: date) -> dict:
    path = DATA_DIR / "pool_cache" / f"{trade_date.strftime('%Y%m%d')}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("trade_date", "symbol", "expected"),
    [
        (DAY_1, "002437", True),
        (DAY_2, "002437", False),
    ],
)
def test_real_primary_pools_satisfy_full_collection_contract(trade_date, symbol, expected):
    snapshot = ExistingJsonAdapter(DATA_DIR).get_market_snapshot(
        trade_date,
        [normalize_symbol(symbol)],
    )
    metric = _membership(snapshot)
    assert metric.status is DataStatus.AVAILABLE
    assert metric.value is expected


@pytest.mark.parametrize(
    "mutation",
    [
        "truncated_info",
        "multiple_pages",
        "total_mismatch",
        "limit_too_small",
        "today_count_mismatch",
        "missing_page",
        "wrong_page_type",
        "wrong_status_type",
        "wrong_info_type",
    ],
)
def test_unverifiable_or_partial_collection_emits_missing_not_false(tmp_path, mutation):
    payload = deepcopy(_real_pool(DAY_2))
    if mutation == "truncated_info":
        payload["data"]["info"].pop()
    elif mutation == "multiple_pages":
        payload["data"]["page"]["count"] = 2
    elif mutation == "total_mismatch":
        payload["data"]["page"]["total"] += 1
    elif mutation == "limit_too_small":
        payload["data"]["page"]["limit"] = payload["data"]["page"]["total"] - 1
    elif mutation == "today_count_mismatch":
        payload["data"]["limit_up_count"]["today"]["num"] += 1
    elif mutation == "missing_page":
        payload["data"].pop("page")
    elif mutation == "wrong_page_type":
        payload["data"]["page"]["total"] = "54"
    elif mutation == "wrong_status_type":
        payload["status_code"] = "0"
    elif mutation == "wrong_info_type":
        payload["data"]["info"] = {"not": "a list"}
    _write_pool(tmp_path, DAY_2, payload)

    snapshot = ExistingJsonAdapter(tmp_path).get_market_snapshot(
        DAY_2,
        [normalize_symbol("002437")],
    )
    metric = _membership(snapshot)
    assert metric.status is DataStatus.MISSING
    assert metric.value is None
    assert any("completeness could not be confirmed" in item for item in snapshot.known_limitations)


def test_empty_collection_is_complete_only_when_all_counts_agree(tmp_path):
    payload = deepcopy(_real_pool(DAY_2))
    payload["data"]["info"] = []
    payload["data"]["page"].update({"page": 1, "count": 1, "total": 0, "limit": 200})
    payload["data"]["limit_up_count"]["today"]["num"] = 0
    _write_pool(tmp_path, DAY_2, payload)

    metric = _membership(
        ExistingJsonAdapter(tmp_path).get_market_snapshot(
            DAY_2,
            [normalize_symbol("002437")],
        )
    )
    assert metric.status is DataStatus.AVAILABLE
    assert metric.value is False


def test_empty_collection_with_inconsistent_count_is_missing(tmp_path):
    payload = deepcopy(_real_pool(DAY_2))
    payload["data"]["info"] = []
    payload["data"]["page"].update({"page": 1, "count": 1, "total": 0, "limit": 200})
    payload["data"]["limit_up_count"]["today"]["num"] = 1
    _write_pool(tmp_path, DAY_2, payload)

    metric = _membership(
        ExistingJsonAdapter(tmp_path).get_market_snapshot(
            DAY_2,
            [normalize_symbol("002437")],
        )
    )
    assert metric.status is DataStatus.MISSING
    assert metric.value is None


@pytest.mark.parametrize(
    ("container", "invalid_value", "expected_limitation"),
    [
        ("limit_up_count", [], "data.limit_up_count is not an object"),
        ("limit_up_count", "invalid", "data.limit_up_count is not an object"),
        ("limit_up_count", None, "data.limit_up_count is not an object"),
        ("today", [], "data.limit_up_count.today is not an object"),
        ("today", "invalid", "data.limit_up_count.today is not an object"),
        ("today", None, "data.limit_up_count.today is not an object"),
    ],
)
def test_invalid_limit_up_count_containers_emit_missing_without_crashing(
    tmp_path,
    container,
    invalid_value,
    expected_limitation,
):
    payload = deepcopy(_real_pool(DAY_2))
    if container == "limit_up_count":
        payload["data"]["limit_up_count"] = invalid_value
    else:
        payload["data"]["limit_up_count"]["today"] = invalid_value
    _write_pool(tmp_path, DAY_2, payload)

    snapshot = ExistingJsonAdapter(tmp_path).get_market_snapshot(
        DAY_2,
        [normalize_symbol("002437")],
    )

    membership = _membership(snapshot)
    assert membership.status is DataStatus.MISSING
    assert membership.value is None
    count_metrics = {
        metric.metric_key: metric
        for metric in snapshot.market_metrics
        if metric.metric_key in {"total_limit_up", "opened_limit_attempts"}
    }
    assert set(count_metrics) == {"total_limit_up", "opened_limit_attempts"}
    assert all(metric.status is DataStatus.MISSING for metric in count_metrics.values())
    assert all(metric.value is None for metric in count_metrics.values())
    assert any(expected_limitation in item for item in snapshot.known_limitations)


@pytest.mark.parametrize("missing_container", ["limit_up_count", "today"])
def test_absent_optional_count_containers_are_not_treated_as_type_errors(tmp_path, missing_container):
    payload = deepcopy(_real_pool(DAY_2))
    if missing_container == "limit_up_count":
        payload["data"].pop("limit_up_count")
    else:
        payload["data"]["limit_up_count"].pop("today")
    _write_pool(tmp_path, DAY_2, payload)

    snapshot = ExistingJsonAdapter(tmp_path).get_market_snapshot(
        DAY_2,
        [normalize_symbol("002437")],
    )

    membership = _membership(snapshot)
    assert membership.status is DataStatus.AVAILABLE
    assert membership.value is False
    count_metrics = {
        metric.metric_key: metric
        for metric in snapshot.market_metrics
        if metric.metric_key in {"total_limit_up", "opened_limit_attempts"}
    }
    assert all(metric.status is DataStatus.MISSING for metric in count_metrics.values())
    assert not any("is not an object" in item for item in snapshot.known_limitations)
