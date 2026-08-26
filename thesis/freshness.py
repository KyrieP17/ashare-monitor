from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any


BEIJING = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ArtifactFreshness:
    trade_date: date | None
    generated_at: datetime | None
    expected_trade_date: date
    stale: bool


def artifact_freshness(payload: dict[str, Any] | None, *, now: datetime | None = None) -> ArtifactFreshness:
    current = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    expected = latest_expected_trade_date(current)
    trade_date = _payload_trade_date(payload)
    generated_at = _payload_generated_at(payload)
    return ArtifactFreshness(
        trade_date=trade_date,
        generated_at=generated_at,
        expected_trade_date=expected,
        stale=trade_date is None or trade_date < expected,
    )


def latest_expected_trade_date(now: datetime) -> date:
    candidate = now.date()
    if now.weekday() >= 5 or now.time() < time(9, 30):
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _payload_trade_date(payload: dict[str, Any] | None) -> date | None:
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    for value in (
        meta.get("trade_date"),
        meta.get("latest_trade_day"),
        payload.get("trade_date"),
        payload.get("date"),
    ):
        parsed = _parse_date(value)
        if parsed:
            return parsed
    watchlist = payload.get("watchlist")
    if isinstance(watchlist, list):
        dates = [_parse_date(item.get("time")) for item in watchlist if isinstance(item, dict)]
        valid = [item for item in dates if item]
        if valid:
            return max(valid)
    generated = _payload_generated_at(payload)
    return generated.date() if generated else None


def _payload_generated_at(payload: dict[str, Any] | None) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    value = meta.get("generated_at") or payload.get("generated_at")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=BEIJING)
    except ValueError:
        return None


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()
    if len(text) >= 8 and text[:8].isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None
