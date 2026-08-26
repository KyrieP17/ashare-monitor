from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Callable, Protocol

from pydantic import Field, model_validator

from .models import DataStatus, DomainModel
from .public_market import PublicMarketClient, PublicMarketError
from .symbols import normalize_symbol


BEIJING = timezone(timedelta(hours=8))


class DailyPriceBar(DomainModel):
    trade_date: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    volume_unit: str = "lot_100_shares"
    amount: float | None = None
    turnover_pct: float | None = None
    complete: bool

    @model_validator(mode="after")
    def validate_ohlc(self) -> DailyPriceBar:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must cover open/close/low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must cover open/close/high")
        return self


class DeterministicPriceInFlag(DomainModel):
    metric: str
    value: float | None = None
    message: str


class PriceVolumeContext(DomainModel):
    instrument_id: str = Field(pattern=r"^CN\.(SH|SZ|BJ)\.\d{6}$")
    source: str
    data_as_of: datetime
    retrieved_at: datetime
    adjustment_method: str = Field(pattern=r"^(qfq|none)$")
    coverage: str
    observation_ref_id: str
    raw_reference: str
    source_payload_hash: str
    end_trade_date: date
    lookback_days: int
    trading_days: int = Field(ge=0)
    daily_bars: list[DailyPriceBar]
    return_5d_pct: float | None = None
    return_10d_pct: float | None = None
    latest_completed_volume_vs_prev5_avg: float | None = None
    distance_to_10d_high_pct: float | None = None
    max_close_drawdown_10d_pct: float | None = None
    latest_turnover_pct: float | None = None
    current_bar_complete: bool
    deterministic_price_in_flags: list[DeterministicPriceInFlag] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    status: DataStatus = DataStatus.AVAILABLE

    @model_validator(mode="after")
    def validate_context(self) -> PriceVolumeContext:
        if self.lookback_days not in (5, 10):
            raise ValueError("lookback_days must be 5 or 10")
        if self.data_as_of.utcoffset() is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("timestamps must include timezone")
        if self.trading_days != len(self.daily_bars):
            raise ValueError("trading_days must match daily_bars")
        return self


class PriceVolumeRepository(Protocol):
    def save_price_volume_context(self, context: PriceVolumeContext) -> PriceVolumeContext: ...


class PublicPriceVolumeTool:
    """Typed, deterministic price/volume context over the public market client."""

    def __init__(
        self,
        client: PublicMarketClient,
        repository: PriceVolumeRepository,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.repository = repository
        self._now = now or (lambda: datetime.now(UTC))

    def get_price_volume_context(
        self,
        instrument_id: str,
        end_trade_date: date,
        lookback_days: int = 10,
    ) -> PriceVolumeContext:
        if lookback_days not in (5, 10):
            raise ValueError("lookback_days must be 5 or 10")
        instrument = normalize_symbol(instrument_id)
        normalized_id = instrument.instrument_id
        retrieved_at = self._now()
        if retrieved_at.utcoffset() is None:
            raise ValueError("now must return a timezone-aware datetime")

        # One prior close is needed for N-day return; one extra slot allows a
        # current unfinished bar without losing the completed-day baseline.
        response = self.client.daily_bars(
            normalized_id,
            end_trade_date=end_trade_date,
            count=lookback_days + 2,
        )
        raw_rows = response.get("rows")
        if not isinstance(raw_rows, list):
            raise PublicMarketError("daily_bars_invalid_shape")
        payload_hash = hashlib.sha256(
            json.dumps(raw_rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        adjustment_method = str(response.get("adjustment_method") or "")
        if adjustment_method not in {"qfq", "none"}:
            raise PublicMarketError("daily_bars_unknown_adjustment")

        parsed = _parse_rows(raw_rows, end_trade_date=end_trade_date, retrieved_at=retrieved_at)
        if not parsed:
            raise PublicMarketError("daily_bars_empty")
        bars = parsed[-(lookback_days + 1):]
        context = _build_context(
            instrument_id=normalized_id,
            end_trade_date=end_trade_date,
            lookback_days=lookback_days,
            source=str(response.get("source") or "public.tencent.qfqkline"),
            raw_reference=str(response.get("raw_reference") or "tencent.fqkline"),
            adjustment_method=adjustment_method,
            payload_hash=payload_hash,
            retrieved_at=retrieved_at,
            bars=bars,
        )
        return self.repository.save_price_volume_context(context)


def get_price_volume_context(
    instrument_id: str,
    end_trade_date: date,
    lookback_days: int = 10,
    *,
    client: PublicMarketClient,
    repository: PriceVolumeRepository,
    now: Callable[[], datetime] | None = None,
) -> PriceVolumeContext:
    """Functional entry point matching the public typed-tool contract."""

    return PublicPriceVolumeTool(client, repository, now=now).get_price_volume_context(
        instrument_id,
        end_trade_date,
        lookback_days,
    )


def _parse_rows(
    rows: list[object],
    *,
    end_trade_date: date,
    retrieved_at: datetime,
) -> list[DailyPriceBar]:
    by_date: dict[date, DailyPriceBar] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            trade_date = date.fromisoformat(str(row[0]))
            if trade_date > end_trade_date:
                continue
            bar = DailyPriceBar(
                trade_date=trade_date,
                open=float(row[1]),
                close=float(row[2]),
                high=float(row[3]),
                low=float(row[4]),
                volume=float(row[5]),
                amount=None,
                turnover_pct=None,
                complete=_bar_complete(trade_date, retrieved_at),
            )
        except (TypeError, ValueError):
            continue
        by_date[trade_date] = bar
    return [by_date[item] for item in sorted(by_date)]


def _bar_complete(trade_date: date, retrieved_at: datetime) -> bool:
    local = retrieved_at.astimezone(BEIJING)
    if trade_date < local.date():
        return True
    if trade_date > local.date():
        return False
    return local.time() >= time(15, 0)


def _build_context(
    *,
    instrument_id: str,
    end_trade_date: date,
    lookback_days: int,
    source: str,
    raw_reference: str,
    adjustment_method: str,
    payload_hash: str,
    retrieved_at: datetime,
    bars: list[DailyPriceBar],
) -> PriceVolumeContext:
    latest = bars[-1]
    completed = [bar for bar in bars if bar.complete]
    limitations: list[str] = []
    if adjustment_method == "qfq":
        limitations.append("价格来自数据源前复权（qfq）口径")
    else:
        limitations.append("数据源未返回可靠复权序列，使用不复权口径；除权除息可能造成价格断层")
    limitations.extend(
        [
            "公开端点未提供 amount",
            "公开端点未提供 turnover；未使用流通股本自行推算",
            "volume 单位为手（1 手 = 100 股），由同响应成交额/价格字段交叉核验",
            "distance_to_10d_high_pct 使用最近 10 个交易日的日内 high",
            "max_close_drawdown_10d_pct 仅使用最近 10 个完整交易日的每日收盘价",
        ]
    )
    if not latest.complete:
        limitations.append("当前交易日尚未收盘；最近价格、收益和距高点为盘中值，成交量指标未使用盘中部分数据")
    if latest.trade_date < end_trade_date:
        limitations.append(
            f"请求截至 {end_trade_date.isoformat()}，数据源最新 Bar 为 {latest.trade_date.isoformat()}；"
            "指标不包含两者之间尚未提供的交易日"
        )

    return_5d = _return_pct(bars, 5)
    return_10d = _return_pct(bars, 10)
    volume_ratio = _completed_volume_ratio(completed)
    distance_high = _distance_to_high(bars)
    close_drawdown = _max_close_drawdown(completed)

    missing_metrics: list[str] = []
    if return_5d is None:
        missing_metrics.append("return_5d_pct")
    if return_10d is None:
        missing_metrics.append("return_10d_pct")
    if volume_ratio is None:
        missing_metrics.append("latest_completed_volume_vs_prev5_avg")
    if distance_high is None:
        missing_metrics.append("distance_to_10d_high_pct")
    if close_drawdown is None:
        missing_metrics.append("max_close_drawdown_10d_pct")
    if missing_metrics:
        limitations.append("有效交易日不足：" + ", ".join(missing_metrics))

    flags = _flags(
        return_5d=return_5d,
        volume_ratio=volume_ratio,
        distance_high=distance_high,
        close_drawdown=close_drawdown,
        latest_complete=latest.complete,
        missing=missing_metrics,
    )
    observation_key = (
        f"{instrument_id}|{end_trade_date.isoformat()}|{lookback_days}|"
        f"{adjustment_method}|{payload_hash}"
    )
    observation_ref_id = "price-volume:" + hashlib.sha256(observation_key.encode("utf-8")).hexdigest()
    data_as_of = (
        datetime.combine(latest.trade_date, time(15, 0), tzinfo=BEIJING)
        if latest.complete
        else retrieved_at
    )
    coverage = (
        f"requested={lookback_days};returned={len(bars)};complete={len(completed)};"
        f"from={bars[0].trade_date.isoformat()};to={bars[-1].trade_date.isoformat()}"
    )
    return PriceVolumeContext(
        instrument_id=instrument_id,
        source=source,
        data_as_of=data_as_of,
        retrieved_at=retrieved_at,
        adjustment_method=adjustment_method,
        coverage=coverage,
        observation_ref_id=observation_ref_id,
        raw_reference=f"{raw_reference};sha256={payload_hash}",
        source_payload_hash=payload_hash,
        end_trade_date=end_trade_date,
        lookback_days=lookback_days,
        trading_days=len(bars),
        daily_bars=bars,
        return_5d_pct=return_5d,
        return_10d_pct=return_10d,
        latest_completed_volume_vs_prev5_avg=volume_ratio,
        distance_to_10d_high_pct=distance_high,
        max_close_drawdown_10d_pct=close_drawdown,
        latest_turnover_pct=None,
        current_bar_complete=latest.complete,
        deterministic_price_in_flags=flags,
        limitations=limitations,
        status=DataStatus.MISSING if missing_metrics else DataStatus.AVAILABLE,
    )


def _return_pct(bars: list[DailyPriceBar], periods: int) -> float | None:
    if len(bars) < periods + 1:
        return None
    baseline = bars[-(periods + 1)].close
    return round((bars[-1].close / baseline - 1) * 100, 2) if baseline else None


def _completed_volume_ratio(completed: list[DailyPriceBar]) -> float | None:
    if len(completed) < 6:
        return None
    prior = completed[-6:-1]
    average = sum(item.volume for item in prior) / 5
    return round(completed[-1].volume / average, 2) if average else None


def _distance_to_high(bars: list[DailyPriceBar]) -> float | None:
    if len(bars) < 10:
        return None
    high = max(item.high for item in bars[-10:])
    return round((high - bars[-1].close) / high * 100, 2) if high else None


def _max_close_drawdown(completed: list[DailyPriceBar]) -> float | None:
    if len(completed) < 10:
        return None
    peak = completed[-10].close
    maximum = 0.0
    for bar in completed[-10:]:
        peak = max(peak, bar.close)
        if peak:
            maximum = max(maximum, (peak - bar.close) / peak * 100)
    return round(maximum, 2)


def _flags(
    *,
    return_5d: float | None,
    volume_ratio: float | None,
    distance_high: float | None,
    close_drawdown: float | None,
    latest_complete: bool,
    missing: list[str],
) -> list[DeterministicPriceInFlag]:
    flags: list[DeterministicPriceInFlag] = []
    if return_5d is not None and return_5d >= 10:
        flags.append(DeterministicPriceInFlag(metric="return_5d_pct", value=return_5d, message=f"近 5 日累计上涨 {return_5d:.2f}%，短期涨幅较大"))
    if distance_high is not None and distance_high <= 3:
        flags.append(DeterministicPriceInFlag(metric="distance_to_10d_high_pct", value=distance_high, message=f"当前价格距离 10 日最高价 {distance_high:.2f}%"))
    if volume_ratio is not None and volume_ratio >= 1.5:
        flags.append(DeterministicPriceInFlag(metric="latest_completed_volume_vs_prev5_avg", value=volume_ratio, message=f"最近完整交易日成交量为前 5 日均量的 {volume_ratio:.2f} 倍"))
    if close_drawdown is not None and close_drawdown >= 8:
        flags.append(DeterministicPriceInFlag(metric="max_close_drawdown_10d_pct", value=close_drawdown, message=f"10 日最大收盘回撤为 {close_drawdown:.2f}%"))
    if not latest_complete:
        flags.append(DeterministicPriceInFlag(metric="current_bar_complete", value=None, message="当前交易日尚未收盘，成交量指标未使用盘中部分数据"))
    if missing:
        flags.append(DeterministicPriceInFlag(metric="coverage", value=None, message="有效交易日不足，部分指标无法判断"))
    return flags
