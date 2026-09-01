from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .candidate_rules import build_candidate_cards
from .candidates import CandidateCard, CandidateObservation
from .models import DataStatus
from .symbols import InvalidSymbolError, normalize_symbol


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_POOL_KEYS = ("pool_a_leaders", "pool_b_starters", "pool_c_repair")


def legacy_pool_rows(payload: dict[str, object] | None) -> list[dict[str, Any]]:
    """Return one normalized legacy row per stock without double-counting pools."""

    if not isinstance(payload, dict):
        return []
    rows_by_code: dict[str, dict[str, Any]] = {}
    for pool_key in _POOL_KEYS:
        raw_rows = payload.get(pool_key)
        if not isinstance(raw_rows, list):
            continue
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                continue
            code = str(raw_row.get("code") or "").strip()
            if len(code) != 6 or not code.isdigit():
                continue
            if code not in rows_by_code:
                rows_by_code[code] = {**raw_row, "legacy_pool": pool_key}
                continue
            existing = rows_by_code[code]
            if bool(raw_row.get("theme_hot")):
                existing["theme_hot"] = True
            existing["theme_cnt"] = max(
                _number(existing.get("theme_cnt")),
                _number(raw_row.get("theme_cnt")),
            )
    return list(rows_by_code.values())


def build_legacy_candidate_cards(
    payload: dict[str, object] | None,
    *,
    limit: int = 10,
) -> list[CandidateCard]:
    """Build read-only CandidateCards from the committed daily legacy artifact."""

    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        return []
    meta = payload["meta"]
    trade_date = _trade_date(meta.get("trade_date"))
    if trade_date is None:
        return []
    observed_at = _observed_at(meta.get("generated_at"), trade_date)
    total_limit_up = _optional_int(meta.get("total_limit_up"))
    coverage = (
        f"full_limit_up_pool:{total_limit_up}"
        if total_limit_up is not None
        else "normalized_legacy_candidate_subset"
    )
    snapshot_id = f"legacy-limit-up:{trade_date.isoformat()}"
    observations: list[CandidateObservation] = []

    for row in legacy_pool_rows(payload):
        code = str(row["code"])
        try:
            instrument_id = normalize_symbol(code).instrument_id
        except InvalidSymbolError:
            continue
        name = str(row.get("name") or code).strip()
        reason = str(row.get("reason") or "旧版涨停池候选").strip()
        metrics = {
            "boards": _optional_int(row.get("boards")),
            "chg_pct": _optional_float(row.get("chg_pct")),
            "turnover_pct": _optional_float(row.get("turnover_pct")),
            "open_num": _optional_float(row.get("open_num")),
            "price": _optional_float(row.get("close")),
            "theme_cnt": _optional_int(row.get("theme_cnt")),
            "theme_hot": bool(row.get("theme_hot")),
            "legacy_score": _optional_float(row.get("score")),
            "legacy_pool": str(row.get("legacy_pool") or ""),
        }
        observations.append(
            CandidateObservation(
                instrument_id=instrument_id,
                instrument_name=name,
                source="public.ths.limit_up_pool",
                data_as_of=observed_at,
                retrieved_at=observed_at,
                status=DataStatus.AVAILABLE,
                coverage=coverage,
                observation_ref_id=f"legacy:{trade_date.isoformat()}:{code}:limit-up",
                raw_reference=f"limit_up.json[{code}]",
                source_snapshot_id=snapshot_id,
                reason=reason,
                metrics=metrics,
            )
        )
        if bool(row.get("theme_hot")):
            observations.append(
                CandidateObservation(
                    instrument_id=instrument_id,
                    instrument_name=name,
                    source="legacy.market.theme_counts",
                    data_as_of=observed_at,
                    retrieved_at=observed_at,
                    status=DataStatus.AVAILABLE,
                    coverage="legacy_theme_hot_flag",
                    observation_ref_id=f"legacy:{trade_date.isoformat()}:{code}:theme-hot",
                    raw_reference=f"limit_up.json[{code}].theme_hot",
                    source_snapshot_id=snapshot_id,
                    reason=f"题材热度计数命中（{_number(row.get('theme_cnt'))} 个标签）",
                    metrics={
                        "sector_resonance": True,
                        "theme_cnt": _optional_int(row.get("theme_cnt")),
                    },
                )
            )

    return build_candidate_cards(
        observations,
        trade_date=trade_date,
        seen_at=observed_at,
        limit=limit,
    )


def _trade_date(value: object) -> date | None:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _observed_at(value: object, trade_date: date) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = datetime.combine(trade_date, datetime.min.time())
    return parsed.replace(tzinfo=parsed.tzinfo or _SHANGHAI)


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _number(value: object) -> int:
    return _optional_int(value) or 0
