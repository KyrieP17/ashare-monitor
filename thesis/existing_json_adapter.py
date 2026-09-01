from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from .catalyst import CATALYST_METRIC_KEY
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
from .symbols import InvalidSymbolError, normalize_symbol


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ADAPTER_SCHEMA_VERSION = "existing-json-v2-sector-observation-refs"
_REGIME_MAP = {
    "未知": MarketRegime.UNKNOWN,
    "进攻期": MarketRegime.ATTACK,
    "分歧期": MarketRegime.DIVERGENCE,
    "退潮期": MarketRegime.RETREAT,
}


class ExistingJsonDataError(RuntimeError):
    """A configured legacy JSON artifact could not be safely parsed or trusted."""


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _parse_yyyymmdd(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _parse_quote_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=_SHANGHAI)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _extract_limit_up_today(data: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Separate an absent optional count block from a malformed supplied block."""
    if "limit_up_count" not in data:
        return None, []
    limit_up_count = data["limit_up_count"]
    if not isinstance(limit_up_count, dict):
        return None, ["data.limit_up_count is not an object"]
    if "today" not in limit_up_count:
        return None, []
    today = limit_up_count["today"]
    if not isinstance(today, dict):
        return None, ["data.limit_up_count.today is not an object"]
    return today, []


def _validate_pool_completeness(payload: dict[str, Any], info: list[Any]) -> list[str]:
    """Return concrete reasons why a cached collection cannot prove non-membership."""
    reasons: list[str] = []
    status_code = payload.get("status_code")
    if not _is_int(status_code) or status_code != 0:
        reasons.append("status_code is not 0")
    if payload.get("status_msg") != "success":
        reasons.append("status_msg is not success")
    data = payload.get("data")
    if not isinstance(data, dict):
        return [*reasons, "data is not an object"]
    page = data.get("page")
    if not isinstance(page, dict):
        return [*reasons, "data.page is not an object"]
    page_number = page.get("page")
    page_count = page.get("count")
    total = page.get("total")
    limit = page.get("limit")
    for key, value in (
        ("page", page_number),
        ("count", page_count),
        ("total", total),
        ("limit", limit),
    ):
        if not _is_int(value):
            reasons.append(f"data.page.{key} is not an integer")
    if _is_int(page_number) and page_number != 1:
        reasons.append("data.page.page is not 1")
    if _is_int(page_count) and page_count != 1:
        reasons.append("data.page.count indicates uncached additional pages")
    if _is_int(total) and total < 0:
        reasons.append("data.page.total is negative")
    if _is_int(limit) and limit < 0:
        reasons.append("data.page.limit is negative")
    if _is_int(total) and total != len(info):
        reasons.append("data.page.total does not equal len(data.info)")
    if _is_int(limit) and _is_int(total) and limit < total:
        reasons.append("data.page.limit is smaller than data.page.total")

    today, count_structure_issues = _extract_limit_up_today(data)
    reasons.extend(count_structure_issues)
    if today is not None and "num" in today:
        today_num = today.get("num")
        if not _is_int(today_num):
            reasons.append("data.limit_up_count.today.num is not an integer")
        elif _is_int(total) and today_num != total:
            reasons.append("data.limit_up_count.today.num does not equal data.page.total")
    return reasons


class ExistingJsonAdapter:
    """Read legacy JSON artifacts without importing legacy collection modules."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)

    def _read_json(self, relative_path: str) -> tuple[dict[str, Any], str]:
        path = self.data_dir / relative_path
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExistingJsonDataError(f"cannot read or parse {relative_path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ExistingJsonDataError(f"{relative_path} must contain a JSON object")
        return payload, hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _source(
        *,
        dataset: str,
        provider: str,
        trade_date: date,
        retrieved_at: datetime,
        definition: str,
        limitations: Iterable[str] = (),
    ) -> SourceRef:
        return SourceRef(
            source_id=f"{provider}:{dataset}:{trade_date.isoformat()}",
            provider=provider,
            endpoint_or_dataset=dataset,
            retrieved_at=retrieved_at,
            data_as_of=trade_date,
            definition=definition,
            known_limitations=list(limitations),
        )

    @staticmethod
    def _metric(
        *,
        metric_key: str,
        value: Decimal | int | str | bool | None,
        unit: str | None,
        status: DataStatus,
        source: SourceRef,
        trade_date: date,
        scope: str,
        raw_reference: str,
        observed_at: datetime,
    ) -> MetricObservation:
        return MetricObservation(
            metric_key=metric_key,
            value=value,
            unit=unit,
            status=status,
            source=source,
            observation_ref_id=make_observation_ref_id(
                dataset=source.endpoint_or_dataset or source.source_id,
                trade_date=trade_date,
                scope=scope,
                metric_key=metric_key,
                raw_reference=raw_reference,
            ),
            observed_at=observed_at,
            raw_reference=raw_reference,
        )

    def get_market_snapshot(
        self,
        trade_date: date,
        instruments: Sequence[InstrumentRef],
    ) -> MarketSnapshot:
        if not instruments:
            raise ValueError("at least one instrument is required")
        retrieved_at = datetime.now(UTC)
        observed_at = datetime.combine(trade_date, datetime.min.time(), tzinfo=_SHANGHAI).replace(hour=15)
        selected_digests: dict[str, str] = {}
        known_limitations: list[str] = []

        normalized_limit: dict[str, Any] | None = None
        limit_path = self.data_dir / "limit_up.json"
        if limit_path.exists():
            candidate, digest = self._read_json("limit_up.json")
            raw_trade_date = candidate.get("meta", {}).get("trade_date")
            if raw_trade_date and _parse_yyyymmdd(str(raw_trade_date)) == trade_date:
                normalized_limit = candidate
                selected_digests["data/limit_up.json"] = digest
            else:
                known_limitations.append(
                    "data/limit_up.json was excluded because its embedded trade date did not match "
                    "the requested trade date."
                )

        raw_pool: dict[str, Any] | None = None
        raw_pool_complete = False
        pool_name = f"pool_cache/{trade_date.strftime('%Y%m%d')}.json"
        pool_path = self.data_dir / pool_name
        if pool_path.exists():
            candidate, digest = self._read_json(pool_name)
            status_code = candidate.get("status_code")
            if _is_int(status_code) and status_code != 0:
                raise ExistingJsonDataError(
                    f"{pool_name} reports upstream error status_code={status_code}"
                )
            raw_trade_date = candidate.get("data", {}).get("date")
            if raw_trade_date and _parse_yyyymmdd(str(raw_trade_date)) == trade_date:
                raw_pool = candidate
                selected_digests[f"data/{pool_name}"] = digest
                info = candidate.get("data", {}).get("info")
                if isinstance(info, list):
                    completeness_issues = _validate_pool_completeness(candidate, info)
                    raw_pool_complete = not completeness_issues
                    if not raw_pool_complete:
                        known_limitations.append(
                            f"{pool_name} records were readable but collection completeness could not be confirmed: "
                            + "; ".join(completeness_issues)
                        )
                else:
                    known_limitations.append(
                        f"{pool_name} records were readable but collection completeness could not be confirmed: "
                        "data.info is not a list."
                    )
            else:
                known_limitations.append(f"Ignored {pool_name}: embedded date does not match requested trade date.")
        else:
            known_limitations.append(
                f"Missing {pool_name}; limit-up-pool membership is unavailable, not false."
            )

        latest: dict[str, Any] | None = None
        latest_quotes: dict[str, dict[str, Any]] = {}
        latest_path = self.data_dir / "latest.json"
        if latest_path.exists():
            candidate, digest = self._read_json("latest.json")
            for quote in candidate.get("watchlist", []):
                quote_time = quote.get("time")
                if not quote_time:
                    continue
                try:
                    quote_date = _parse_quote_time(str(quote_time)).date()
                    instrument = normalize_symbol(str(quote.get("code", "")))
                except (ValueError, InvalidSymbolError):
                    continue
                if quote_date == trade_date:
                    latest_quotes[instrument.instrument_id] = quote
            if latest_quotes:
                latest = candidate
                selected_digests["data/latest.json"] = digest
            else:
                known_limitations.append(
                    "latest.json was not used because no watchlist quote had the requested trade date; "
                    "file generation time was not treated as data_as_of."
                )

        if normalized_limit is None and raw_pool is None and latest is None:
            raise LookupError(f"no JSON data with an explicit data_as_of for {trade_date}")

        market_metrics: list[MetricObservation] = []
        regime = MarketRegime.UNKNOWN
        if normalized_limit is not None:
            source = self._source(
                dataset="data/limit_up.json",
                provider="AShare Monitor normalized limit-up output",
                trade_date=trade_date,
                retrieved_at=retrieved_at,
                definition="Normalized output produced from the legacy Tonghuashun limit-up collector.",
                limitations=["Some fields are legacy derived rules rather than exchange facts."],
            )
            meta = normalized_limit["meta"]
            regime = _REGIME_MAP.get(str(meta.get("sentiment", "未知")), MarketRegime.UNKNOWN)
            for key, unit in (
                ("total_limit_up", "COUNT"),
                ("prev_total", "COUNT"),
                ("open_ratio_pct", "PCT"),
                ("promo_rate_pct", "PCT"),
                ("max_board", "COUNT"),
                ("prev_max_board", "COUNT"),
            ):
                value = meta.get(key)
                market_metrics.append(
                    self._metric(
                        metric_key=key,
                        value=_decimal(value),
                        unit=unit,
                        status=DataStatus.AVAILABLE if value is not None else DataStatus.MISSING,
                        source=source,
                        trade_date=trade_date,
                        scope="market:CN",
                        raw_reference=f"meta.{key}",
                        observed_at=observed_at,
                    )
                )
        elif raw_pool is not None:
            source = self._source(
                dataset=f"data/{pool_name}",
                provider="Tonghuashun limit-up pool cache",
                trade_date=trade_date,
                retrieved_at=retrieved_at,
                definition="Raw cached limit-up pool response.",
            )
            raw_data = raw_pool.get("data")
            today, _ = _extract_limit_up_today(raw_data) if isinstance(raw_data, dict) else (None, [])
            for raw_key, metric_key in (("num", "total_limit_up"), ("open_num", "opened_limit_attempts")):
                raw_value = today.get(raw_key) if today is not None else None
                value = raw_value if _is_int(raw_value) else None
                market_metrics.append(
                    self._metric(
                        metric_key=metric_key,
                        value=_decimal(value),
                        unit="COUNT",
                        status=DataStatus.AVAILABLE if value is not None else DataStatus.MISSING,
                        source=source,
                        trade_date=trade_date,
                        scope="market:CN",
                        raw_reference=f"data.limit_up_count.today.{raw_key}",
                        observed_at=observed_at,
                    )
                )

        normalized_stocks = self._normalized_stock_index(normalized_limit)
        raw_stocks = self._raw_stock_index(raw_pool)
        stock_observations: list[StockObservation] = []
        for instrument in instruments:
            normalized_stock = normalized_stocks.get(instrument.code)
            raw_stock = raw_stocks.get(instrument.code)
            quote = latest_quotes.get(instrument.instrument_id)
            stock_observations.append(
                self._stock_observation(
                    instrument=instrument,
                    trade_date=trade_date,
                    observed_at=observed_at,
                    retrieved_at=retrieved_at,
                    normalized_stock=normalized_stock,
                    raw_stock=raw_stock,
                    latest_quote=quote,
                    pool_name=pool_name,
                    pool_complete=raw_pool_complete,
                )
            )

        sources: dict[str, SourceRef] = {}
        for metric in market_metrics:
            sources[metric.source.source_id] = metric.source
        for stock in stock_observations:
            for metric in stock.membership_metrics + stock.price_metrics + stock.fund_flow_metrics:
                sources[metric.source.source_id] = metric.source
            for sector in stock.sectors:
                sources[sector.source.source_id] = sector.source

        raw_hash_input = json.dumps(
            {
                "adapter_schema_version": _ADAPTER_SCHEMA_VERSION,
                "trade_date": trade_date.isoformat(),
                "instruments": sorted(item.instrument_id for item in instruments),
                "files": selected_digests,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        raw_hash = hashlib.sha256(raw_hash_input.encode("utf-8")).hexdigest()
        return MarketSnapshot(
            snapshot_id=uuid5(NAMESPACE_URL, f"existing-json:{raw_hash}"),
            trade_date=trade_date,
            created_at=retrieved_at,
            market_regime=regime,
            market_metrics=market_metrics,
            stock_observations=stock_observations,
            sources=list(sources.values()),
            known_limitations=known_limitations,
            raw_data_hash=raw_hash,
        )

    @staticmethod
    def _normalized_stock_index(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        if payload is None:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for pool_name in ("pool_a_leaders", "pool_b_starters", "pool_c_repair"):
            for item in payload.get(pool_name, []):
                result.setdefault(str(item.get("code")), item)
        return result

    @staticmethod
    def _raw_stock_index(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        if payload is None:
            return {}
        info = payload.get("data", {}).get("info", [])
        if not isinstance(info, list):
            return {}
        return {
            str(item.get("code")): item
            for item in info
            if isinstance(item, dict) and item.get("code")
        }

    def _stock_observation(
        self,
        *,
        instrument: InstrumentRef,
        trade_date: date,
        observed_at: datetime,
        retrieved_at: datetime,
        normalized_stock: dict[str, Any] | None,
        raw_stock: dict[str, Any] | None,
        latest_quote: dict[str, Any] | None,
        pool_name: str,
        pool_complete: bool,
    ) -> StockObservation:
        membership_metrics: list[MetricObservation] = []
        price_metrics: list[MetricObservation] = []
        fund_metrics: list[MetricObservation] = []
        limitations: list[str] = []
        name = instrument.name

        membership_source = self._source(
            dataset=f"data/{pool_name}",
            provider=(
                "Tonghuashun limit-up pool cache"
                if pool_complete
                else "ExistingJsonAdapter collection lookup"
            ),
            trade_date=trade_date,
            retrieved_at=retrieved_at,
            definition=(
                "Deterministic membership query against a successfully loaded complete dated limit-up pool."
                if pool_complete
                else "Limit-up-pool membership could not be established because collection completeness was unavailable."
            ),
            limitations=(
                []
                if pool_complete
                else ["MISSING means the collection was unavailable or unverifiable; it does not mean non-membership."]
            ),
        )
        membership_raw_reference = (
            f"data.info membership_query[trade_date={trade_date.isoformat()};"
            f"query_code={instrument.code};predicate=item.code==query_code]"
            if pool_complete
            else f"collection_membership_query[dataset=data/{pool_name};"
            f"trade_date={trade_date.isoformat()};query_code={instrument.code}]"
        )
        membership_metrics.append(
            self._metric(
                metric_key="in_limit_up_pool",
                value=(raw_stock is not None) if pool_complete else None,
                unit="BOOLEAN",
                status=DataStatus.AVAILABLE if pool_complete else DataStatus.MISSING,
                source=membership_source,
                trade_date=trade_date,
                scope=instrument.instrument_id,
                raw_reference=membership_raw_reference,
                observed_at=observed_at,
            )
        )

        raw_reason = (
            str(raw_stock.get("reason_type") or "").strip()
            if raw_stock is not None
            else ""
        )
        normalized_reason = (
            str(normalized_stock.get("reason") or "").strip()
            if normalized_stock is not None
            else ""
        )
        catalyst_conflicted = bool(
            raw_reason and normalized_reason and raw_reason != normalized_reason
        )
        if raw_reason:
            catalyst_source = self._source(
                dataset=f"data/{pool_name}",
                provider="Tonghuashun limit-up pool cache",
                trade_date=trade_date,
                retrieved_at=retrieved_at,
                definition="Original provider reason_type text from the dated limit-up pool.",
                limitations=["Provider event/theme text is not an announcement or independently verified fact."],
            )
            membership_metrics.append(
                self._metric(
                    metric_key=CATALYST_METRIC_KEY,
                    value=raw_reason,
                    unit=None,
                    status=DataStatus.CONFLICTED if catalyst_conflicted else DataStatus.AVAILABLE,
                    source=catalyst_source,
                    trade_date=trade_date,
                    scope=instrument.instrument_id,
                    raw_reference=f"data.info[code={instrument.code}].reason_type",
                    observed_at=observed_at,
                )
            )
        if normalized_reason and (not raw_reason or catalyst_conflicted):
            catalyst_source = self._source(
                dataset="data/limit_up.json",
                provider="AShare Monitor normalized limit-up output",
                trade_date=trade_date,
                retrieved_at=retrieved_at,
                definition="Original normalized reason text retained from the legacy limit-up artifact.",
                limitations=["Normalized reason text is not an announcement or independently verified fact."],
            )
            membership_metrics.append(
                self._metric(
                    metric_key=CATALYST_METRIC_KEY,
                    value=normalized_reason,
                    unit=None,
                    status=DataStatus.CONFLICTED if catalyst_conflicted else DataStatus.AVAILABLE,
                    source=catalyst_source,
                    trade_date=trade_date,
                    scope=instrument.instrument_id,
                    raw_reference=f"limit_pool[code={instrument.code}].reason",
                    observed_at=observed_at,
                )
            )
        if not raw_reason and not normalized_reason:
            membership_metrics.append(
                self._metric(
                    metric_key=CATALYST_METRIC_KEY,
                    value=None,
                    unit=None,
                    status=DataStatus.MISSING,
                    source=membership_source,
                    trade_date=trade_date,
                    scope=instrument.instrument_id,
                    raw_reference=(
                        f"catalyst_lookup[trade_date={trade_date.isoformat()};"
                        f"query_code={instrument.code}]"
                    ),
                    observed_at=observed_at,
                )
            )

        if latest_quote is not None:
            name = name or latest_quote.get("name")
            quote_source = self._source(
                dataset="data/latest.json",
                provider="Tencent quote via AShare Monitor JSON",
                trade_date=trade_date,
                retrieved_at=retrieved_at,
                definition="Watchlist quote fields parsed from the committed latest.json artifact.",
            )
            quote_observed_at = _parse_quote_time(str(latest_quote["time"]))
            quote_fields = (
                ("price", "close_price", "CNY", Decimal("1")),
                ("prev_close", "previous_close", "CNY", Decimal("1")),
                ("open", "open_price", "CNY", Decimal("1")),
                ("high", "high_price", "CNY", Decimal("1")),
                ("low", "low_price", "CNY", Decimal("1")),
                ("volume_hand", "volume", "HAND", Decimal("1")),
                ("amount_wan", "turnover_amount", "CNY", Decimal("10000")),
                ("chg", "price_change", "CNY", Decimal("1")),
                ("chg_pct", "price_change_pct", "PCT", Decimal("1")),
                ("turnover_pct", "turnover_rate", "PCT", Decimal("1")),
                ("avg5_vol_hand", "average_5d_volume", "HAND", Decimal("1")),
                ("vol_ratio", "volume_ratio", "RATIO", Decimal("1")),
            )
            for raw_key, metric_key, unit, multiplier in quote_fields:
                raw_value = latest_quote.get(raw_key)
                value = _decimal(raw_value)
                if value is not None:
                    value *= multiplier
                price_metrics.append(
                    self._metric(
                        metric_key=metric_key,
                        value=value,
                        unit=unit,
                        status=DataStatus.AVAILABLE if raw_value is not None else DataStatus.MISSING,
                        source=quote_source,
                        trade_date=trade_date,
                        scope=instrument.instrument_id,
                        raw_reference=f"watchlist[code={latest_quote['code']}].{raw_key}",
                        observed_at=quote_observed_at,
                    )
                )

            fund_flow = latest_quote.get("fund_flow") or {}
            if fund_flow.get("date") == trade_date.isoformat():
                fund_source = self._source(
                    dataset="data/latest.json",
                    provider="Sina MoneyFlow via AShare Monitor JSON",
                    trade_date=trade_date,
                    retrieved_at=retrieved_at,
                    definition="Vendor-defined main fund-flow observation; not exchange-confirmed cash flow.",
                    limitations=["Main fund flow is a vendor methodology and may conflict with other providers."],
                )
                for raw_key, metric_key, unit, multiplier in (
                    ("main_net_wan", "main_net_flow", "CNY", Decimal("10000")),
                    ("main_ratio_pct", "main_net_flow_ratio", "PCT", Decimal("1")),
                ):
                    raw_value = fund_flow.get(raw_key)
                    value = _decimal(raw_value)
                    if value is not None:
                        value *= multiplier
                    fund_metrics.append(
                        self._metric(
                            metric_key=metric_key,
                            value=value,
                            unit=unit,
                            status=DataStatus.AVAILABLE if raw_value is not None else DataStatus.MISSING,
                            source=fund_source,
                            trade_date=trade_date,
                            scope=instrument.instrument_id,
                            raw_reference=f"watchlist[code={latest_quote['code']}].fund_flow.{raw_key}",
                            observed_at=quote_observed_at,
                        )
                    )
            elif fund_flow:
                limitations.append("Fund-flow data was ignored because its date did not match the requested trade date.")

        if raw_stock is not None:
            name = name or raw_stock.get("name")
            raw_source = self._source(
                dataset=f"data/{pool_name}",
                provider="Tonghuashun limit-up pool cache",
                trade_date=trade_date,
                retrieved_at=retrieved_at,
                definition="Raw cached limit-up pool fields.",
            )
            raw_fields = (
                ("latest", "limit_up_close", "CNY"),
                ("change_rate", "limit_up_change_pct", "PCT"),
                ("turnover_rate", "limit_up_turnover_rate", "PCT"),
                ("order_amount", "seal_order_amount", "CNY"),
                ("currency_value", "circulating_market_value", "CNY"),
                ("open_num", "limit_open_count", "COUNT"),
            )
            for raw_key, metric_key, unit in raw_fields:
                raw_value = raw_stock.get(raw_key)
                price_metrics.append(
                    self._metric(
                        metric_key=metric_key,
                        value=_decimal(raw_value),
                        unit=unit,
                        status=DataStatus.AVAILABLE if raw_value is not None else DataStatus.MISSING,
                        source=raw_source,
                        trade_date=trade_date,
                        scope=instrument.instrument_id,
                        raw_reference=f"data.info[code={instrument.code}].{raw_key}",
                        observed_at=observed_at,
                    )
                )

        if normalized_stock is not None:
            name = name or normalized_stock.get("name")
            normalized_data_source = self._source(
                dataset="data/limit_up.json",
                provider="AShare Monitor normalized limit-up output",
                trade_date=trade_date,
                retrieved_at=retrieved_at,
                definition="Normalized legacy output; *_yi amounts are rounded 100-million-CNY fields.",
                limitations=["Normalized yi-denominated amounts may be rounded relative to the raw pool response."],
            )
            for raw_key, metric_key in (
                ("seal_yi", "normalized_seal_order_amount"),
                ("float_cap_yi", "normalized_circulating_market_value"),
            ):
                raw_value = normalized_stock.get(raw_key)
                value = _decimal(raw_value)
                if value is not None:
                    value *= Decimal("100000000")
                price_metrics.append(
                    self._metric(
                        metric_key=metric_key,
                        value=value,
                        unit="CNY",
                        status=DataStatus.AVAILABLE if raw_value is not None else DataStatus.MISSING,
                        source=normalized_data_source,
                        trade_date=trade_date,
                        scope=instrument.instrument_id,
                        raw_reference=f"limit_pool[code={instrument.code}].{raw_key}",
                        observed_at=observed_at,
                    )
                )

            normalized_source = self._source(
                dataset="data/limit_up.json",
                provider="AShare Monitor legacy rules",
                trade_date=trade_date,
                retrieved_at=retrieved_at,
                definition="Legacy deterministic rule output, not an exchange-confirmed fact.",
                limitations=["role, score and grade are derived by legacy project heuristics."],
            )
            for raw_key, metric_key, unit in (
                ("role", "legacy_role", "LEGACY_CATEGORY"),
                ("score", "legacy_score", "LEGACY_POINTS"),
                ("grade", "legacy_grade", "LEGACY_CATEGORY"),
            ):
                raw_value = normalized_stock.get(raw_key)
                price_metrics.append(
                    self._metric(
                        metric_key=metric_key,
                        value=raw_value,
                        unit=unit,
                        status=DataStatus.AVAILABLE if raw_value is not None else DataStatus.MISSING,
                        source=normalized_source,
                        trade_date=trade_date,
                        scope=instrument.instrument_id,
                        raw_reference=f"limit_pool[code={instrument.code}].{raw_key}",
                        observed_at=observed_at,
                    )
                )

        sectors = self._sector_observations(
            instrument=instrument,
            trade_date=trade_date,
            retrieved_at=retrieved_at,
            normalized_stock=normalized_stock,
            raw_stock=raw_stock,
            pool_name=pool_name,
        )

        if not price_metrics:
            missing_source = self._source(
                dataset="existing-json-lookup",
                provider="ExistingJsonAdapter",
                trade_date=trade_date,
                retrieved_at=retrieved_at,
                definition="No matching stock observation was present in the eligible dated JSON artifacts.",
            )
            price_metrics.append(
                self._metric(
                    metric_key="stock_observation",
                    value=None,
                    unit=None,
                    status=DataStatus.MISSING,
                    source=missing_source,
                    trade_date=trade_date,
                    scope=instrument.instrument_id,
                    raw_reference=f"lookup[instrument_id={instrument.instrument_id}]",
                    observed_at=observed_at,
                )
            )
            limitations.append("Stock was absent from all eligible JSON artifacts for the requested trade date.")

        return StockObservation(
            instrument=instrument,
            trade_date=trade_date,
            name=name,
            membership_metrics=membership_metrics,
            price_metrics=price_metrics,
            fund_flow_metrics=fund_metrics,
            sectors=sectors,
            known_limitations=limitations,
        )

    def _sector_observations(
        self,
        *,
        instrument: InstrumentRef,
        trade_date: date,
        retrieved_at: datetime,
        normalized_stock: dict[str, Any] | None,
        raw_stock: dict[str, Any] | None,
        pool_name: str,
    ) -> list[SectorObservation]:
        normalized_tags = self._split_tags(normalized_stock.get("reason") if normalized_stock else None)
        raw_tags = self._split_tags(raw_stock.get("reason_type") if raw_stock else None)
        conflicted = bool(normalized_tags and raw_tags and normalized_tags != raw_tags)
        result: list[SectorObservation] = []

        if raw_tags:
            source = self._source(
                dataset=f"data/{pool_name}",
                provider="Tonghuashun limit-up pool cache",
                trade_date=trade_date,
                retrieved_at=retrieved_at,
                definition="Provider reason_type tags; these are event/theme labels, not formal industry membership.",
            )
            raw_reference = f"data.info[code={instrument.code}].reason_type"
            for tag in sorted(raw_tags):
                result.append(
                    SectorObservation(
                        sector_name=tag,
                        taxonomy="ths_limit_up_reason",
                        relation="provider_reason_tag",
                        observation_ref_id=make_sector_observation_ref_id(
                            dataset=source.endpoint_or_dataset or source.source_id,
                            provider=source.provider,
                            trade_date=trade_date,
                            instrument_id=instrument.instrument_id,
                            taxonomy="ths_limit_up_reason",
                            sector_name=tag,
                            relation="provider_reason_tag",
                            raw_reference=raw_reference,
                        ),
                        raw_reference=raw_reference,
                        status=DataStatus.CONFLICTED if conflicted else DataStatus.AVAILABLE,
                        source=source,
                    )
                )
        if normalized_tags and (conflicted or not raw_tags):
            source = self._source(
                dataset="data/limit_up.json",
                provider="AShare Monitor normalized limit-up output",
                trade_date=trade_date,
                retrieved_at=retrieved_at,
                definition="Normalized legacy reason tags; not formal industry membership.",
            )
            raw_reference = f"limit_pool[code={instrument.code}].reason"
            for tag in sorted(normalized_tags):
                result.append(
                    SectorObservation(
                        sector_name=tag,
                        taxonomy="legacy_normalized_reason",
                        relation="legacy_reason_tag",
                        observation_ref_id=make_sector_observation_ref_id(
                            dataset=source.endpoint_or_dataset or source.source_id,
                            provider=source.provider,
                            trade_date=trade_date,
                            instrument_id=instrument.instrument_id,
                            taxonomy="legacy_normalized_reason",
                            sector_name=tag,
                            relation="legacy_reason_tag",
                            raw_reference=raw_reference,
                        ),
                        raw_reference=raw_reference,
                        status=DataStatus.CONFLICTED if conflicted else DataStatus.AVAILABLE,
                        source=source,
                    )
                )
        return result

    @staticmethod
    def _split_tags(value: Any) -> set[str]:
        if not isinstance(value, str):
            return set()
        return {item.strip() for item in value.split("+") if item.strip()}
