from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from thesis.models import (
    ClaimType,
    DataStatus,
    EvidenceDirection,
    EvidenceItem,
    EvidenceQuality,
    MetricObservation,
    MarketSnapshot,
    SourceRef,
    StockObservation,
)
from thesis.repository import SQLiteThesisRepository
from thesis.symbols import normalize_symbol


def source():
    return SourceRef(
        source_id="migration-source",
        provider="migration-test",
        endpoint_or_dataset="fixture-v1",
        retrieved_at=datetime.now(UTC),
        data_as_of=date(2026, 8, 21),
    )


def test_gate1_metric_field_loads_but_serializes_with_new_name():
    metric = MetricObservation.model_validate(
        {
            "metric_key": "close_price",
            "value": "10.5",
            "unit": "CNY",
            "status": DataStatus.AVAILABLE,
            "source": source(),
            "tool_call_id": "legacy-stable-reference",
            "observed_at": datetime.now(UTC),
            "raw_reference": "fixture.price",
        }
    )

    dumped = metric.model_dump(mode="json")
    assert metric.observation_ref_id == "legacy-stable-reference"
    assert dumped["observation_ref_id"] == "legacy-stable-reference"
    assert "tool_call_id" not in dumped
    with pytest.warns(DeprecationWarning, match="deprecated"):
        assert metric.tool_call_id == "legacy-stable-reference"


def test_gate1_evidence_field_loads_but_serializes_with_new_name():
    evidence = EvidenceItem.model_validate(
        {
            "evidence_id": uuid4(),
            "claim": "Legacy observation value was 10.5 CNY.",
            "claim_type": ClaimType.FACT,
            "direction": EvidenceDirection.SUPPORT,
            "evidence_quality": EvidenceQuality.MEDIUM,
            "quality_reason": "Migration compatibility test.",
            "tool_call_ids": ["legacy-stable-reference"],
            "source_refs": [source()],
            "observed_at": datetime.now(UTC),
        }
    )

    dumped = evidence.model_dump(mode="json")
    assert evidence.observation_ref_ids == ["legacy-stable-reference"]
    assert dumped["observation_ref_ids"] == ["legacy-stable-reference"]
    assert "tool_call_ids" not in dumped
    with pytest.warns(DeprecationWarning, match="deprecated"):
        assert evidence.tool_call_ids == ["legacy-stable-reference"]


def test_repository_rewrites_gate1_alias_to_gate2_field(tmp_path):
    metric = MetricObservation.model_validate(
        {
            "metric_key": "close_price",
            "value": "10.5",
            "unit": "CNY",
            "status": DataStatus.AVAILABLE,
            "source": source(),
            "tool_call_id": "legacy-stable-reference",
            "observed_at": datetime.now(UTC),
            "raw_reference": "fixture.price",
        }
    )
    snapshot = MarketSnapshot(
        snapshot_id=uuid4(),
        trade_date=date(2026, 8, 21),
        created_at=datetime.now(UTC),
        stock_observations=[
            StockObservation(
                instrument=normalize_symbol("sh600519"),
                trade_date=date(2026, 8, 21),
                price_metrics=[metric],
            )
        ],
        sources=[metric.source],
        raw_data_hash="migration-hash",
    )
    repository = SQLiteThesisRepository(tmp_path / "migration.db")
    repository.save_snapshot(snapshot)
    reloaded = repository.get_snapshot(snapshot.snapshot_id)
    repository.close()

    dumped = reloaded.model_dump(mode="json")
    stored_metric = dumped["stock_observations"][0]["price_metrics"][0]
    assert stored_metric["observation_ref_id"] == "legacy-stable-reference"
    assert "tool_call_id" not in stored_metric
