from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any, TypeVar
from .adapters import MarketDataAdapter
from .catalyst import CatalystContext, catalyst_context_from_snapshot
from .gate3_models import ToolInvocation, ToolInvocationStatus, ToolName
from .models import (
    InstrumentRef,
    MarketSnapshot,
    MetricObservation,
    SectorObservation,
    StockObservation,
)
from .repository import SQLiteThesisRepository
from .symbols import normalize_symbol


class ToolInputError(ValueError):
    pass


class ToolCoverageError(LookupError):
    pass


T = TypeVar("T")


def _sector_refs(sectors: Sequence[SectorObservation]) -> list[str]:
    refs = [
        sector.observation_ref_id
        for sector in sectors
        if sector.observation_ref_id is not None
    ]
    refs.extend(metric.observation_ref_id for sector in sectors for metric in sector.metrics)
    return refs


def _stock_refs(stock: StockObservation) -> list[str]:
    refs = [
        metric.observation_ref_id
        for metric in stock.membership_metrics + stock.price_metrics + stock.fund_flow_metrics
    ]
    return [*refs, *_sector_refs(stock.sectors)]


def _snapshot_refs(snapshot: MarketSnapshot) -> list[str]:
    refs = [metric.observation_ref_id for metric in snapshot.market_metrics]
    refs.extend(_sector_refs(snapshot.sector_observations))
    for stock in snapshot.stock_observations:
        refs.extend(_stock_refs(stock))
    return list(dict.fromkeys(refs))


class ReadOnlyMarketTools:
    """Audited typed tools over a structured adapter, never over raw JSON.

    ``default_instruments`` is an explicit coverage boundary. It is required for
    market-wide or sector-only requests because the current adapter must never
    guess which instruments constitute a complete universe.
    """

    def __init__(
        self,
        adapter: MarketDataAdapter,
        repository: SQLiteThesisRepository,
        *,
        default_instruments: Sequence[str | InstrumentRef] = (),
    ) -> None:
        self.adapter = adapter
        self.repository = repository
        self.default_instruments = tuple(self._instrument(item) for item in default_instruments)

    @staticmethod
    def _instrument(value: str | InstrumentRef) -> InstrumentRef:
        return normalize_symbol(value) if isinstance(value, str) else value

    def _execute(
        self,
        *,
        tool_name: ToolName,
        arguments: dict[str, Any],
        operation: Callable[[], tuple[T, MarketSnapshot, list[str], list[str]]],
        llm_tool_call_id: str | None,
    ) -> T:
        started_at = datetime.now(UTC)
        try:
            result, snapshot, refs, instrument_coverage = operation()
        except Exception as exc:
            invocation = ToolInvocation(
                llm_tool_call_id=llm_tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                started_at=started_at,
                completed_at=datetime.now(UTC),
                status=ToolInvocationStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
            self.repository.save_tool_invocation(invocation)
            raise
        invocation = ToolInvocation(
            llm_tool_call_id=llm_tool_call_id,
            tool_name=tool_name,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            status=ToolInvocationStatus.SUCCEEDED,
            snapshot_id=snapshot.snapshot_id,
            instrument_coverage=instrument_coverage,
            arguments={
                **arguments,
                "resolved_instrument_coverage": instrument_coverage,
            },
            returned_observation_ref_ids=list(dict.fromkeys(refs)),
        )
        self.repository.save_snapshot_tool_invocation(snapshot, invocation)
        return result

    def get_market_snapshot(
        self,
        trade_date: date,
        symbols: Sequence[str | InstrumentRef] | None = None,
        *,
        llm_tool_call_id: str | None = None,
    ) -> MarketSnapshot:
        raw_symbols = list(symbols) if symbols is not None else None

        def operation() -> tuple[MarketSnapshot, MarketSnapshot, list[str], list[str]]:
            instruments = (
                tuple(self._instrument(item) for item in raw_symbols)
                if raw_symbols is not None
                else self.default_instruments
            )
            if not instruments:
                raise ToolCoverageError(
                    "get_market_snapshot requires symbols or an explicit default instrument coverage"
                )
            snapshot = self.adapter.get_market_snapshot(trade_date, instruments)
            coverage = [item.instrument_id for item in instruments]
            return snapshot, snapshot, _snapshot_refs(snapshot), coverage

        return self._execute(
            tool_name=ToolName.GET_MARKET_SNAPSHOT,
            arguments={
                "trade_date": trade_date.isoformat(),
                "symbols": [
                    item.instrument_id if isinstance(item, InstrumentRef) else item
                    for item in raw_symbols
                ] if raw_symbols is not None else None,
            },
            operation=operation,
            llm_tool_call_id=llm_tool_call_id,
        )

    def get_stock_observation(
        self,
        instrument_id: str,
        trade_date: date,
        lookback_days: int = 1,
        *,
        llm_tool_call_id: str | None = None,
    ) -> StockObservation:
        def operation() -> tuple[StockObservation, MarketSnapshot, list[str], list[str]]:
            if lookback_days != 1:
                raise ToolCoverageError(
                    "current adapter supports a single trading day only; lookback_days must be 1"
                )
            instrument = self._instrument(instrument_id)
            snapshot = self.adapter.get_market_snapshot(trade_date, [instrument])
            stock = next(
                (item for item in snapshot.stock_observations if item.instrument.instrument_id == instrument.instrument_id),
                None,
            )
            if stock is None:
                raise ToolCoverageError(f"snapshot did not return {instrument.instrument_id}")
            return stock, snapshot, _stock_refs(stock), [instrument.instrument_id]

        return self._execute(
            tool_name=ToolName.GET_STOCK_OBSERVATION,
            arguments={
                "instrument_id": instrument_id,
                "trade_date": trade_date.isoformat(),
                "lookback_days": lookback_days,
            },
            operation=operation,
            llm_tool_call_id=llm_tool_call_id,
        )

    def get_sector_observations(
        self,
        instrument_id: str,
        trade_date: date,
        *,
        llm_tool_call_id: str | None = None,
    ) -> list[SectorObservation]:
        def operation() -> tuple[list[SectorObservation], MarketSnapshot, list[str], list[str]]:
            instrument = self._instrument(instrument_id)
            snapshot = self.adapter.get_market_snapshot(trade_date, [instrument])
            stock = next(
                (item for item in snapshot.stock_observations if item.instrument.instrument_id == instrument.instrument_id),
                None,
            )
            if stock is None:
                raise ToolCoverageError(f"snapshot did not return {instrument.instrument_id}")
            return stock.sectors, snapshot, _sector_refs(stock.sectors), [instrument.instrument_id]

        return self._execute(
            tool_name=ToolName.GET_SECTOR_OBSERVATIONS,
            arguments={"instrument_id": instrument_id, "trade_date": trade_date.isoformat()},
            operation=operation,
            llm_tool_call_id=llm_tool_call_id,
        )

    def get_fund_flow_observations(
        self,
        trade_date: date,
        instrument_id: str | None = None,
        sector_name: str | None = None,
        *,
        llm_tool_call_id: str | None = None,
    ) -> list[MetricObservation]:
        def operation() -> tuple[list[MetricObservation], MarketSnapshot, list[str], list[str]]:
            if instrument_id is None and sector_name is None:
                raise ToolInputError("instrument_id or sector_name is required")
            instruments = (
                (self._instrument(instrument_id),)
                if instrument_id is not None
                else self.default_instruments
            )
            if not instruments:
                raise ToolCoverageError(
                    "sector-only fund-flow query requires explicit default instrument coverage"
                )
            snapshot = self.adapter.get_market_snapshot(trade_date, instruments)
            observations: list[MetricObservation] = []
            supporting_sector_refs: list[str] = []
            for stock in snapshot.stock_observations:
                if sector_name is not None:
                    matching_sectors = [
                        sector for sector in stock.sectors if sector.sector_name == sector_name
                    ]
                    if not matching_sectors:
                        continue
                    supporting_sector_refs.extend(_sector_refs(matching_sectors))
                observations.extend(stock.fund_flow_metrics)
            refs = [item.observation_ref_id for item in observations]
            refs.extend(supporting_sector_refs)
            coverage = [item.instrument_id for item in instruments]
            return observations, snapshot, refs, coverage

        return self._execute(
            tool_name=ToolName.GET_FUND_FLOW_OBSERVATIONS,
            arguments={
                "trade_date": trade_date.isoformat(),
                "instrument_id": instrument_id,
                "sector_name": sector_name,
            },
            operation=operation,
            llm_tool_call_id=llm_tool_call_id,
        )

    def get_catalyst_context(
        self,
        instrument_id: str,
        trade_date: date,
        *,
        llm_tool_call_id: str | None = None,
    ) -> CatalystContext:
        def operation() -> tuple[CatalystContext, MarketSnapshot, list[str], list[str]]:
            instrument = self._instrument(instrument_id)
            snapshot = self.adapter.get_market_snapshot(trade_date, [instrument])
            context = catalyst_context_from_snapshot(
                snapshot,
                instrument.instrument_id,
                tool_call_id=llm_tool_call_id,
            )
            return (
                context,
                snapshot,
                context.observation_ref_ids,
                [instrument.instrument_id],
            )

        return self._execute(
            tool_name=ToolName.GET_CATALYST_CONTEXT,
            arguments={
                "instrument_id": instrument_id,
                "trade_date": trade_date.isoformat(),
            },
            operation=operation,
            llm_tool_call_id=llm_tool_call_id,
        )
