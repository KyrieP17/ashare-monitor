from __future__ import annotations

import json
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from thesis.existing_json_adapter import ExistingJsonAdapter
from thesis.models import DataStatus
from thesis.provenance import validate_snapshot_provenance
from thesis.symbols import normalize_symbol


DATA_DIR = Path(__file__).parents[1] / "data"
TRADE_DAY = date(2026, 8, 21)


def metric_by_key(metrics, key):
    return next(metric for metric in metrics if metric.metric_key == key)


def test_real_json_repeated_parse_has_stable_non_colliding_observation_refs():
    adapter = ExistingJsonAdapter(DATA_DIR)
    instrument = normalize_symbol("sh600519")

    first = adapter.get_market_snapshot(TRADE_DAY, [instrument])
    second = adapter.get_market_snapshot(TRADE_DAY, [instrument])

    first_metrics = first.stock_observations[0].price_metrics + first.stock_observations[0].fund_flow_metrics
    second_metrics = second.stock_observations[0].price_metrics + second.stock_observations[0].fund_flow_metrics
    first_refs = [metric.observation_ref_id for metric in first_metrics]
    second_refs = [metric.observation_ref_id for metric in second_metrics]
    assert first_refs == second_refs
    assert len(first_refs) == len(set(first_refs))
    assert first.raw_data_hash == second.raw_data_hash
    assert first.snapshot_id == second.snapshot_id


def test_latest_json_uses_quote_date_and_converts_wan_to_cny():
    snapshot = ExistingJsonAdapter(DATA_DIR).get_market_snapshot(
        TRADE_DAY,
        [normalize_symbol("sh600519")],
    )
    stock = snapshot.stock_observations[0]
    turnover = metric_by_key(stock.price_metrics, "turnover_amount")

    assert snapshot.trade_date == TRADE_DAY
    assert turnover.source.data_as_of == TRADE_DAY
    assert turnover.value == Decimal("4278310000.0")
    assert turnover.unit == "CNY"
    assert turnover.raw_reference == "watchlist[code=sh600519].amount_wan"
    assert "2026-08-23" not in str(turnover.source.data_as_of)
    assert validate_snapshot_provenance(snapshot).is_valid


def test_limit_up_json_converts_yi_to_cny_without_renaming_only():
    snapshot = ExistingJsonAdapter(DATA_DIR).get_market_snapshot(
        TRADE_DAY,
        [normalize_symbol("603958")],
    )
    seal_amount = metric_by_key(
        snapshot.stock_observations[0].price_metrics,
        "normalized_seal_order_amount",
    )

    assert seal_amount.value == Decimal("57000000.00")
    assert seal_amount.unit == "CNY"
    assert seal_amount.raw_reference == "limit_pool[code=603958].seal_yi"


def test_historical_pool_replay_does_not_use_future_latest_json():
    snapshot = ExistingJsonAdapter(DATA_DIR).get_market_snapshot(
        date(2026, 8, 20),
        [normalize_symbol("600613")],
    )

    assert snapshot.stock_observations[0].name == "神奇制药"
    assert all(source.endpoint_or_dataset != "data/latest.json" for source in snapshot.sources)
    assert any("file generation time was not treated as data_as_of" in item for item in snapshot.known_limitations)
    assert validate_snapshot_provenance(snapshot).is_valid


def test_missing_stock_data_is_missing_not_zero():
    snapshot = ExistingJsonAdapter(DATA_DIR).get_market_snapshot(
        date(2026, 8, 20),
        [normalize_symbol("sh600519")],
    )
    metric = snapshot.stock_observations[0].price_metrics[0]

    assert metric.metric_key == "stock_observation"
    assert metric.status is DataStatus.MISSING
    assert metric.value is None


def test_legacy_role_score_and_grade_are_explicitly_derived():
    snapshot = ExistingJsonAdapter(DATA_DIR).get_market_snapshot(
        TRADE_DAY,
        [normalize_symbol("603958")],
    )
    stock = snapshot.stock_observations[0]
    for key in ("legacy_role", "legacy_score", "legacy_grade"):
        metric = metric_by_key(stock.price_metrics, key)
        assert metric.source.provider == "AShare Monitor legacy rules"
        assert any("derived" in item for item in metric.source.known_limitations)


def test_missing_real_json_field_emits_missing_metric(tmp_path):
    payload = json.loads((DATA_DIR / "latest.json").read_text(encoding="utf-8"))
    quote = next(item for item in payload["watchlist"] if item["code"] == "sh600519")
    quote.pop("amount_wan")
    (tmp_path / "latest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    snapshot = ExistingJsonAdapter(tmp_path).get_market_snapshot(
        TRADE_DAY,
        [normalize_symbol("sh600519")],
    )
    turnover = metric_by_key(snapshot.stock_observations[0].price_metrics, "turnover_amount")
    assert turnover.status is DataStatus.MISSING
    assert turnover.value is None


def test_conflicting_reason_taxonomies_are_preserved_side_by_side(tmp_path):
    shutil.copy(DATA_DIR / "limit_up.json", tmp_path / "limit_up.json")
    pool_dir = tmp_path / "pool_cache"
    pool_dir.mkdir()
    shutil.copy(DATA_DIR / "pool_cache" / "20260821.json", pool_dir / "20260821.json")

    normalized = json.loads((tmp_path / "limit_up.json").read_text(encoding="utf-8"))
    stock = next(item for item in normalized["pool_a_leaders"] if item["code"] == "603958")
    stock["reason"] = "与原始来源冲突的标签"
    (tmp_path / "limit_up.json").write_text(json.dumps(normalized, ensure_ascii=False), encoding="utf-8")

    snapshot = ExistingJsonAdapter(tmp_path).get_market_snapshot(
        TRADE_DAY,
        [normalize_symbol("603958")],
    )
    sectors = snapshot.stock_observations[0].sectors
    assert {sector.taxonomy for sector in sectors} == {
        "ths_limit_up_reason",
        "legacy_normalized_reason",
    }
    assert all(sector.status is DataStatus.CONFLICTED for sector in sectors)


def test_embedded_date_mismatch_is_not_accepted_as_requested_trade_date(tmp_path):
    pool_dir = tmp_path / "pool_cache"
    pool_dir.mkdir()
    shutil.copy(DATA_DIR / "pool_cache" / "20260820.json", pool_dir / "20260819.json")

    with pytest.raises(LookupError, match="no JSON data with an explicit data_as_of"):
        ExistingJsonAdapter(tmp_path).get_market_snapshot(
            date(2026, 8, 19),
            [normalize_symbol("600613")],
        )
