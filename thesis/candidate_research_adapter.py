from __future__ import annotations

import hashlib
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from .candidates import CandidateCard, CandidateObservation
from .models import (
    DataStatus,
    InstrumentRef,
    MarketRegime,
    MarketSnapshot,
    MetricObservation,
    SectorObservation,
    SourceRef,
    StockObservation,
)
from .provenance import make_observation_ref_id, make_sector_observation_ref_id
from .symbols import normalize_symbol


_PRICE_KEYS = {"price", "chg_pct", "turnover_pct"}
_FUND_FLOW_KEYS = {"board_net_yi", "board_chg_pct", "lead_chg_pct"}
_UNITS = {
    "price": "CNY",
    "chg_pct": "%",
    "turnover_pct": "%",
    "board_net_yi": "CNY 100m",
    "board_chg_pct": "%",
    "lead_chg_pct": "%",
    "boards": "board",
    "open_num": "count",
}


class CandidateResearchAdapter:
    """Expose one persisted CandidateCard as a typed, read-only research snapshot."""

    def __init__(self, candidate: CandidateCard) -> None:
        self.candidate = candidate
        self.instrument = normalize_symbol(candidate.instrument_id).model_copy(
            update={"name": candidate.instrument_name}
        )
        self._snapshot = self._build_snapshot()

    def get_market_snapshot(
        self,
        trade_date,
        instruments: list[InstrumentRef] | tuple[InstrumentRef, ...],
    ) -> MarketSnapshot:
        if trade_date != self.candidate.trade_date:
            raise LookupError("candidate research adapter only covers the candidate trade date")
        requested = {item.instrument_id for item in instruments}
        if requested != {self.instrument.instrument_id}:
            raise LookupError("candidate research adapter only covers the promoted instrument")
        return self._snapshot

    def _build_snapshot(self) -> MarketSnapshot:
        membership: list[MetricObservation] = []
        price: list[MetricObservation] = []
        fund_flow: list[MetricObservation] = []
        sectors: list[SectorObservation] = []
        sources: dict[str, SourceRef] = {}

        for observation in self.candidate.observations:
            source = self._source(observation)
            sources[source.source_id] = source
            board_name = observation.metrics.get("board_name")
            sector_metrics: list[MetricObservation] = []
            if observation.source == "public.ths.limit_up_pool":
                membership.append(
                    self._metric(
                        observation,
                        source,
                        "in_limit_up_pool",
                        True,
                        unit=None,
                    )
                )
            for key, value in observation.metrics.items():
                metric = self._metric(
                    observation,
                    source,
                    key,
                    value,
                    unit=_UNITS.get(key),
                    scope=(
                        f"{observation.instrument_id}:sector:public_board_flow:{board_name}"
                        if key in _FUND_FLOW_KEYS
                        and isinstance(board_name, str)
                        and board_name.strip()
                        else observation.instrument_id
                    ),
                )
                if key in _PRICE_KEYS:
                    price.append(metric)
                elif key in _FUND_FLOW_KEYS:
                    if isinstance(board_name, str) and board_name.strip():
                        sector_metrics.append(metric)
                    else:
                        fund_flow.append(metric)
                else:
                    membership.append(metric)

            if isinstance(board_name, str) and board_name.strip():
                sectors.append(
                    SectorObservation(
                        sector_name=board_name,
                        taxonomy="public_board_flow",
                        relation="lead_stock",
                        observation_ref_id=make_sector_observation_ref_id(
                            dataset=source.endpoint_or_dataset or source.source_id,
                            provider=source.provider,
                            trade_date=self.candidate.trade_date,
                            instrument_id=self.instrument.instrument_id,
                            taxonomy="public_board_flow",
                            sector_name=board_name,
                            relation="lead_stock",
                            raw_reference=observation.raw_reference,
                        ),
                        raw_reference=observation.raw_reference,
                        metrics=sector_metrics,
                        status=observation.status,
                        source=source,
                    )
                )

        raw = self.candidate.model_dump_json().encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        snapshot_id = uuid5(
            NAMESPACE_URL,
            f"candidate-research:{self.candidate.candidate_id}:{digest}",
        )
        stock = StockObservation(
            instrument=self.instrument,
            trade_date=self.candidate.trade_date,
            name=self.candidate.instrument_name,
            membership_metrics=_unique_metrics(membership),
            price_metrics=_unique_metrics(price),
            fund_flow_metrics=_unique_metrics(fund_flow),
            sectors=sectors,
            known_limitations=[
                "Research input is limited to persisted public CandidateCard observations.",
                "No announcement/catalyst tool or private account data is included in M5.",
            ],
        )
        return MarketSnapshot(
            snapshot_id=snapshot_id,
            trade_date=self.candidate.trade_date,
            created_at=self.candidate.last_seen_at,
            market_regime=MarketRegime.UNKNOWN,
            stock_observations=[stock],
            sources=list(sources.values()),
            known_limitations=list(stock.known_limitations),
            raw_data_hash=digest,
        )

    @staticmethod
    def _source(observation: CandidateObservation) -> SourceRef:
        return SourceRef(
            source_id=f"{observation.source}:{observation.source_snapshot_id}",
            provider=observation.source,
            endpoint_or_dataset=observation.source,
            retrieved_at=observation.retrieved_at,
            data_as_of=observation.data_as_of,
            definition=observation.coverage,
            known_limitations=[
                "Candidate observation is a bounded public-data snapshot, not a complete research dataset."
            ],
        )

    @staticmethod
    def _metric(
        observation: CandidateObservation,
        source: SourceRef,
        key: str,
        value,
        *,
        unit: str | None,
        scope: str | None = None,
    ) -> MetricObservation:
        normalized = Decimal(str(value)) if isinstance(value, float) else value
        return MetricObservation(
            metric_key=key,
            value=normalized,
            unit=unit,
            status=observation.status if value is not None else DataStatus.MISSING,
            source=source,
            observation_ref_id=make_observation_ref_id(
                dataset=source.endpoint_or_dataset or source.source_id,
                trade_date=observation.data_as_of.date(),
                scope=scope or observation.instrument_id,
                metric_key=key,
                raw_reference=observation.raw_reference,
            ),
            observed_at=observation.data_as_of,
            raw_reference=observation.raw_reference,
        )


def _unique_metrics(metrics: list[MetricObservation]) -> list[MetricObservation]:
    return list({item.observation_ref_id: item for item in metrics}.values())
