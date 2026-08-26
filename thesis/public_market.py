from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from .candidates import CandidateObservation, ScanSourceStatus
from .models import DataStatus
from .symbols import normalize_symbol


THS_LIMIT_UP_URL = (
    "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
    "?page=1&limit=200&field=199112,10,9001,330323,330324,330325,9002,"
    "330329,133971,133970,1968584,3475914,9003,9004"
    "&filter=HS,GEM2STAR&date={date}&order_field=330324&order_type=0"
)
TENCENT_TRADE_DATES_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,15,qfq"
)
TENCENT_QFQ_KLINE_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,{end},{count},qfq"
)
SINA_BOARD_FLOW_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "MoneyFlow.ssl_bkzj_bk?page=1&num={limit}&sort=netamount&asc=0&fenlei={kind}"
)
THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Referer": "https://data.10jqka.com.cn/",
}
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}
BEIJING = timezone(timedelta(hours=8))


class PublicMarketError(RuntimeError):
    pass


class PublicMarketClient:
    """Side-effect-free HTTP client; it never changes process proxy variables."""

    def __init__(self, *, timeout: float = 15.0, session: requests.Session | None = None) -> None:
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": THS_HEADERS["User-Agent"]})

    def _json(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        response = self.session.get(url, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def latest_trade_date(self) -> date:
        payload = self._json(TENCENT_TRADE_DATES_URL)
        node = payload.get("data", {}).get("sh000001", {}) if isinstance(payload, dict) else {}
        rows = node.get("qfqday") or node.get("day") or []
        if not rows:
            raise PublicMarketError("trade_date_unavailable")
        return date.fromisoformat(str(rows[-1][0]))

    def daily_bars(
        self,
        instrument_id: str,
        *,
        end_trade_date: date,
        count: int,
    ) -> dict[str, Any]:
        """Return raw Tencent daily bars for any supported A-share symbol.

        The caller owns completeness and metric calculations. This method only
        normalizes the symbol, requests a bounded history and reports whether
        the response actually supplied qfq or unadjusted rows.
        """

        if count < 1 or count > 120:
            raise ValueError("count must be between 1 and 120")
        instrument = normalize_symbol(instrument_id)
        symbol = f"{instrument.exchange.value.lower()}{instrument.code}"
        url = TENCENT_QFQ_KLINE_URL.format(
            symbol=symbol,
            end=end_trade_date.isoformat(),
            count=count,
        )
        payload = self._json(url)
        node = payload.get("data", {}).get(symbol, {}) if isinstance(payload, dict) else {}
        qfq_rows = node.get("qfqday") if isinstance(node, dict) else None
        raw_rows = node.get("day") if isinstance(node, dict) else None
        if isinstance(qfq_rows, list) and qfq_rows:
            rows = qfq_rows
            adjustment_method = "qfq"
        elif isinstance(raw_rows, list) and raw_rows:
            rows = raw_rows
            adjustment_method = "none"
        else:
            raise PublicMarketError(f"daily_bars_unavailable:{symbol}")
        return {
            "source": "public.tencent.qfqkline",
            "symbol": symbol,
            "adjustment_method": adjustment_method,
            "rows": rows,
            "raw_reference": f"tencent.fqkline[{symbol};end={end_trade_date.isoformat()};count={count}]",
        }

    def limit_up_pool(self, trade_date: date) -> list[dict[str, Any]]:
        payload = self._json(
            THS_LIMIT_UP_URL.format(date=trade_date.strftime("%Y%m%d")),
            headers=THS_HEADERS,
        )
        if not isinstance(payload, dict) or payload.get("status_code") != 0:
            raise PublicMarketError("limit_up_pool_unavailable")
        rows = payload.get("data", {}).get("info", [])
        if not isinstance(rows, list):
            raise PublicMarketError("limit_up_pool_invalid_shape")
        return [item for item in rows if isinstance(item, dict)]

    def quotes(self, codes: list[str]) -> list[dict[str, Any]]:
        if not codes:
            return []
        response = self.session.get(
            "https://qt.gtimg.cn/q=" + ",".join(codes),
            timeout=self.timeout,
        )
        response.raise_for_status()
        response.encoding = "gbk"
        result: list[dict[str, Any]] = []
        for line in response.text.split(";"):
            if "=" not in line:
                continue
            variable, raw = line.split("=", 1)
            fields = raw.strip().strip('"').split("~")
            if len(fields) < 40:
                continue
            code = variable.strip().removeprefix("v_")
            result.append(
                {
                    "code": code,
                    "name": fields[1],
                    "price": _float(fields, 3),
                    "chg_pct": _float(fields, 32),
                    "turnover_pct": _float(fields, 38),
                    "time": fields[30],
                }
            )
        return result

    def board_flows(self, *, kind: int, limit: int = 30) -> list[dict[str, Any]]:
        payload = self._json(
            SINA_BOARD_FLOW_URL.format(limit=limit, kind=kind),
            headers=SINA_HEADERS,
        )
        if not isinstance(payload, list):
            raise PublicMarketError("board_flow_invalid_shape")
        return [item for item in payload if isinstance(item, dict)]


class PublicMarketAdapter:
    source_name = "public.market"

    def __init__(
        self,
        project_root: str | Path,
        *,
        client: PublicMarketClient | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.client = client or PublicMarketClient()
        self._now = now or (lambda: datetime.now(UTC))
        self.last_errors: list[str] = []
        self.source_statuses: list[ScanSourceStatus] = []

    def collect(self) -> list[CandidateObservation]:
        retrieved_at = self._now()
        source_names = (
            "public.ths.limit_up_pool",
            "public.tencent.research_focus",
            "public.tencent.watchlist",
            "public.sina.board_flow",
        )
        self.last_errors = []
        self.source_statuses = []
        try:
            trade_date = self.client.latest_trade_date()
        except Exception as exc:
            message = _safe_error(exc)
            self.last_errors = [f"{source}:{message}" for source in source_names]
            self.source_statuses = [
                ScanSourceStatus(
                    source=source,
                    status=DataStatus.ERROR,
                    observation_count=0,
                    error_message=message,
                )
                for source in source_names
            ]
            raise PublicMarketError("trade_date_unavailable") from exc
        observations: list[CandidateObservation] = []
        errors: list[str] = []

        for source_name, collector in (
            ("public.ths.limit_up_pool", lambda: self._limit_up_observations(trade_date, retrieved_at)),
            ("public.tencent.research_focus", lambda: self._research_focus_observations(trade_date, retrieved_at)),
            ("public.tencent.watchlist", lambda: self._watchlist_observations(trade_date, retrieved_at)),
            ("public.sina.board_flow", lambda: self._board_observations(trade_date, retrieved_at)),
        ):
            try:
                collected = collector()
            except Exception as exc:
                message = _safe_error(exc)
                errors.append(f"{source_name}:{message}")
                self.source_statuses.append(
                    ScanSourceStatus(
                        source=source_name,
                        status=DataStatus.ERROR,
                        observation_count=0,
                        error_message=message,
                    )
                )
            else:
                observations.extend(collected)
                self.source_statuses.append(
                    ScanSourceStatus(
                        source=source_name,
                        status=DataStatus.AVAILABLE,
                        observation_count=len(collected),
                    )
                )

        self.last_errors = errors
        if self.source_statuses and all(item.status is DataStatus.ERROR for item in self.source_statuses):
            raise PublicMarketError("all_public_sources_failed:" + ",".join(errors))
        return observations

    def _limit_up_observations(self, trade_date: date, retrieved_at: datetime) -> list[CandidateObservation]:
        rows = self.client.limit_up_pool(trade_date)
        snapshot_id = _snapshot_id("public.ths.limit_up_pool", trade_date, rows)
        return [
            _observation(
                instrument_id=_instrument_id(str(row.get("code") or "")),
                instrument_name=str(row.get("name") or "未知"),
                source="public.ths.limit_up_pool",
                trade_date=trade_date,
                retrieved_at=retrieved_at,
                coverage=f"full_limit_up_pool:{len(rows)}",
                raw_reference=f"limit_up_pool[{row.get('code', '')}]",
                snapshot_id=snapshot_id,
                reason=str(row.get("reason_type") or ""),
                metrics={
                    "boards": _boards(str(row.get("high_days") or "")),
                    "open_num": row.get("open_num"),
                    "chg_pct": row.get("change_rate"),
                    "price": row.get("latest"),
                    "limit_up_type": row.get("limit_up_type"),
                },
            )
            for row in rows
            if _instrument_id(str(row.get("code") or "")) is not None
        ]

    def _watchlist_observations(self, trade_date: date, retrieved_at: datetime) -> list[CandidateObservation]:
        payload = json.loads((self.project_root / "watchlist.json").read_text(encoding="utf-8"))
        configured = payload.get("stocks", []) if isinstance(payload, dict) else []
        names = {str(item.get("code")): str(item.get("name") or "未知") for item in configured if isinstance(item, dict)}
        rows = self.client.quotes(list(names))
        snapshot_id = _snapshot_id("public.tencent.watchlist", trade_date, rows)
        result: list[CandidateObservation] = []
        for row in rows:
            data_as_of = _quote_time(str(row.get("time") or ""), trade_date, retrieved_at)
            instrument_id = _instrument_id(str(row.get("code") or ""))
            if instrument_id is None:
                continue
            result.append(
                _observation(
                    instrument_id=instrument_id,
                    instrument_name=str(row.get("name") or names.get(str(row.get("code")), "未知")),
                    source="public.tencent.watchlist",
                    trade_date=trade_date,
                    retrieved_at=retrieved_at,
                    data_as_of=data_as_of,
                    coverage=f"configured_watchlist:{len(configured)}",
                    raw_reference=f"watchlist_quote[{row.get('code', '')}]",
                    snapshot_id=snapshot_id,
                    reason="自选股公开行情异动",
                    metrics={
                        "price": row.get("price"),
                        "chg_pct": row.get("chg_pct"),
                        "turnover_pct": row.get("turnover_pct"),
                    },
                )
            )
        return result

    def _research_focus_observations(self, trade_date: date, retrieved_at: datetime) -> list[CandidateObservation]:
        payload = json.loads((self.project_root / "research_pool.json").read_text(encoding="utf-8"))
        configured = payload.get("stocks", []) if isinstance(payload, dict) else []
        names = {str(item.get("code")): str(item.get("name") or "未知") for item in configured if isinstance(item, dict)}
        rows = self.client.quotes(list(names))
        snapshot_id = _snapshot_id("public.tencent.research_focus", trade_date, rows)
        result: list[CandidateObservation] = []
        for row in rows:
            instrument_id = _instrument_id(str(row.get("code") or ""))
            if instrument_id is None:
                continue
            result.append(
                _observation(
                    instrument_id=instrument_id,
                    instrument_name=names.get(str(row.get("code")), str(row.get("name") or "未知")),
                    source="public.tencent.research_focus",
                    trade_date=trade_date,
                    retrieved_at=retrieved_at,
                    data_as_of=_quote_time(str(row.get("time") or ""), trade_date, retrieved_at),
                    coverage=f"user_research_pool:{len(configured)}",
                    raw_reference=f"research_pool_quote[{row.get('code', '')}]",
                    snapshot_id=snapshot_id,
                    reason="用户明确指定的研究池；公开行情确认",
                    metrics={
                        "price": row.get("price"),
                        "chg_pct": row.get("chg_pct"),
                        "turnover_pct": row.get("turnover_pct"),
                        "user_selected": True,
                    },
                )
            )
        return result

    def _board_observations(self, trade_date: date, retrieved_at: datetime) -> list[CandidateObservation]:
        rows = self.client.board_flows(kind=0) + self.client.board_flows(kind=1)
        snapshot_id = _snapshot_id("public.sina.board_flow", trade_date, rows)
        result: list[CandidateObservation] = []
        for row in rows:
            instrument_id = _instrument_id(str(row.get("ts_symbol") or ""))
            if instrument_id is None:
                continue
            net_yi = _to_float(row.get("netamount")) / 100_000_000
            board_chg = _to_float(row.get("avg_changeratio")) * 100
            lead_chg = _to_float(row.get("ts_changeratio")) * 100
            board_name = str(row.get("name") or "未知板块")
            result.append(
                _observation(
                    instrument_id=instrument_id,
                    instrument_name=str(row.get("ts_name") or "未知"),
                    source="public.sina.board_flow",
                    trade_date=trade_date,
                    retrieved_at=retrieved_at,
                    coverage=f"top_board_flows:{len(rows)}",
                    raw_reference=f"board_flow[{row.get('category', board_name)}]",
                    snapshot_id=snapshot_id,
                    reason=f"{board_name}；公开原始资金净额与领涨信息",
                    metrics={"board_name": board_name, "board_net_yi": net_yi, "board_chg_pct": board_chg, "lead_chg_pct": lead_chg},
                )
            )
        return result


def _observation(
    *,
    instrument_id: str | None,
    instrument_name: str,
    source: str,
    trade_date: date,
    retrieved_at: datetime,
    coverage: str,
    raw_reference: str,
    snapshot_id: str,
    reason: str,
    metrics: dict[str, Any],
    data_as_of: datetime | None = None,
) -> CandidateObservation:
    if instrument_id is None:
        raise PublicMarketError("invalid_instrument")
    observed = data_as_of or datetime.combine(trade_date, datetime.min.time(), tzinfo=UTC)
    digest = hashlib.sha256(
        f"{snapshot_id}|{instrument_id}|{raw_reference}".encode("utf-8")
    ).hexdigest()
    return CandidateObservation(
        instrument_id=instrument_id,
        instrument_name=instrument_name,
        source=source,
        data_as_of=observed,
        retrieved_at=retrieved_at,
        status=DataStatus.AVAILABLE,
        coverage=coverage,
        observation_ref_id=f"obs:{digest}",
        raw_reference=raw_reference,
        source_snapshot_id=snapshot_id,
        reason=reason,
        metrics=metrics,
    )


def _snapshot_id(source: str, trade_date: date, payload: object) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"snapshot:{source}:{trade_date.isoformat()}:{digest}"


def _instrument_id(code: str) -> str | None:
    normalized = code.strip().lower()
    digits = re.sub(r"\D", "", normalized)
    if len(digits) != 6:
        return None
    if normalized.startswith("bj") or digits.startswith(("4", "8")):
        exchange = "BJ"
    elif normalized.startswith("sh") or digits.startswith(("5", "6", "9")):
        exchange = "SH"
    else:
        exchange = "SZ"
    return f"CN.{exchange}.{digits}"


def _boards(high_days: str) -> int:
    if high_days == "首板":
        return 1
    match = re.search(r"(\d+)天(\d+)板", high_days)
    return int(match.group(2)) if match else 1


def _float(fields: list[str], index: int) -> float:
    try:
        return float(fields[index])
    except (IndexError, TypeError, ValueError):
        return 0.0


def _to_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _quote_time(value: str, fallback: date, retrieved_at: datetime) -> datetime:
    try:
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=BEIJING)
    except ValueError:
        if value[:8].isdigit():
            return datetime.combine(date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:8]}"), datetime.min.time(), tzinfo=UTC)
        return datetime.combine(fallback, datetime.min.time(), tzinfo=UTC) if fallback else retrieved_at


def _safe_error(error: Exception) -> str:
    message = " ".join(str(error).split())[:240]
    return f"{type(error).__name__}:{message}" if message else type(error).__name__
