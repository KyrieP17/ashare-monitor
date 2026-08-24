from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, Sequence
from uuid import NAMESPACE_URL, uuid5

from .models import (
    DataStatus,
    InstrumentRef,
    MarketRegime,
    MarketSnapshot,
    MetricObservation,
    SourceRef,
    StockObservation,
)
from .provenance import make_observation_ref_id


class MarketDataAdapter(Protocol):
    def get_market_snapshot(
        self, trade_date: date, instruments: Sequence[InstrumentRef]
    ) -> MarketSnapshot: ...


class MockDemoAdapter:
    """Deterministic, offline market data for state-machine and demo tests."""

    _prices: dict[tuple[str, date], Decimal] = {
        ("CN.SH.600519", date(2026, 8, 20)): Decimal("1408.00"),
        ("CN.SH.600519", date(2026, 8, 21)): Decimal("1386.50"),
        ("CN.SZ.300750", date(2026, 8, 20)): Decimal("245.20"),
        ("CN.SZ.300750", date(2026, 8, 21)): Decimal("249.80"),
        ("CN.SZ.002437", date(2026, 8, 20)): Decimal("8.34"),
        ("CN.SZ.002437", date(2026, 8, 21)): Decimal("7.67"),
    }

    def get_market_snapshot(
        self, trade_date: date, instruments: Sequence[InstrumentRef]
    ) -> MarketSnapshot:
        if not instruments:
            raise ValueError("at least one instrument is required")
        now = datetime.combine(trade_date, datetime.min.time(), tzinfo=UTC).replace(hour=15, minute=5)
        source = SourceRef(
            source_id=f"mock-demo-{trade_date.isoformat()}",
            provider="MockDemoAdapter",
            endpoint_or_dataset="deterministic-fixture-v1",
            retrieved_at=now,
            data_as_of=trade_date,
            definition="Synthetic fixture values for workflow testing only.",
            known_limitations=["Not real market data; must never be presented as live data."],
        )
        observations: list[StockObservation] = []
        raw_values: list[dict[str, str]] = []
        for instrument in instruments:
            key = (instrument.instrument_id, trade_date)
            if key not in self._prices:
                raise LookupError(f"no mock fixture for {instrument.instrument_id} on {trade_date}")
            price = self._prices[key]
            raw_reference = f"fixture:{instrument.instrument_id}:{trade_date.isoformat()}:close_price"
            observation_ref_id = make_observation_ref_id(
                dataset=source.endpoint_or_dataset or source.source_id,
                trade_date=trade_date,
                scope=instrument.instrument_id,
                metric_key="close_price",
                raw_reference=raw_reference,
            )
            metric = MetricObservation(
                metric_key="close_price",
                value=price,
                unit="CNY",
                status=DataStatus.AVAILABLE,
                source=source,
                observation_ref_id=observation_ref_id,
                observed_at=now,
                raw_reference=raw_reference,
            )
            observations.append(
                StockObservation(
                    instrument=instrument,
                    trade_date=trade_date,
                    name=instrument.name,
                    price_metrics=[metric],
                    known_limitations=["Synthetic test observation."],
                )
            )
            raw_values.append({"instrument_id": instrument.instrument_id, "close_price": str(price)})

        canonical = json.dumps(
            {"trade_date": trade_date.isoformat(), "values": raw_values},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        raw_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        snapshot_key = f"mock-snapshot:{raw_hash}"
        return MarketSnapshot(
            snapshot_id=uuid5(NAMESPACE_URL, snapshot_key),
            trade_date=trade_date,
            created_at=now,
            market_regime=MarketRegime.UNKNOWN,
            stock_observations=observations,
            sources=[source],
            known_limitations=["Entire snapshot is an offline synthetic fixture."],
            raw_data_hash=raw_hash,
        )
