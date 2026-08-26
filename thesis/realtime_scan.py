from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from .candidate_repository import SQLiteCandidateRepository
from .candidate_rules import build_candidate_cards
from .candidates import (
    CandidateCard,
    CandidateObservation,
    CandidateSourceAdapter,
    ScanMode,
    ScanRun,
    ScanRunStatus,
    ScanSourceStatus,
)
from .models import DataStatus


@dataclass(frozen=True)
class ScanExecutionResult:
    scan_run: ScanRun
    cards: list[CandidateCard]


class RealtimeScanner:
    def __init__(
        self,
        adapter: CandidateSourceAdapter,
        repository: SQLiteCandidateRepository,
        *,
        now: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.adapter = adapter
        self.repository = repository
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleeper

    def run_once(
        self,
        *,
        mode: ScanMode = ScanMode.ONCE,
        interval_seconds: int | None = None,
    ) -> ScanExecutionResult:
        started_at = _utc(self._now())
        run = ScanRun(
            scan_run_id=str(uuid4()),
            started_at=started_at,
            status=ScanRunStatus.RUNNING,
            mode=mode,
            interval_seconds=interval_seconds,
        )
        self.repository.create_scan_run(run)

        observations: list[CandidateObservation] = []
        cards: list[CandidateCard] = []
        try:
            observations = self.adapter.collect()
            source_statuses = _source_statuses(self.adapter, observations)
            trade_date = max((item.data_as_of.date() for item in observations), default=None)
            all_failed = bool(source_statuses) and all(
                item.status is DataStatus.ERROR for item in source_statuses
            )
            if all_failed:
                raise RuntimeError("all_sources_failed")
            if trade_date is not None:
                cards = build_candidate_cards(
                    observations,
                    trade_date=trade_date,
                    seen_at=_utc(self._now()),
                )
            saved_cards = self.repository.upsert(cards)
        except Exception as exc:
            completed_at = _utc(self._now())
            source_statuses = _source_statuses(self.adapter, observations, fallback_error=exc)
            errors = _source_errors(source_statuses)
            scan_error = _safe_error(exc)
            if scan_error not in errors:
                errors.append(scan_error)
            failed = run.model_copy(
                update={
                    "completed_at": completed_at,
                    "trade_date": max((item.data_as_of.date() for item in observations), default=None),
                    "status": ScanRunStatus.FAILED,
                    "source_statuses": source_statuses,
                    "observation_count": len(observations),
                    "candidate_count": 0,
                    "error_messages": errors,
                }
            )
            self.repository.update_scan_run(failed)
            return ScanExecutionResult(scan_run=failed, cards=[])

        completed_at = _utc(self._now())
        has_source_failure = any(item.status is DataStatus.ERROR for item in source_statuses)
        status = ScanRunStatus.PARTIAL if has_source_failure else ScanRunStatus.SUCCEEDED
        expected_next = (
            completed_at + timedelta(seconds=interval_seconds)
            if mode is ScanMode.LOOP and interval_seconds is not None
            else None
        )
        completed = run.model_copy(
            update={
                "completed_at": completed_at,
                "trade_date": trade_date,
                "status": status,
                "expected_next_run_at": expected_next,
                "source_statuses": source_statuses,
                "observation_count": len(observations),
                "candidate_count": len(saved_cards),
                "error_messages": _source_errors(source_statuses),
            }
        )
        self.repository.update_scan_run(completed)
        return ScanExecutionResult(scan_run=completed, cards=saved_cards)

    def run_loop(
        self,
        *,
        interval_seconds: int = 240,
        on_result: Callable[[ScanExecutionResult], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        max_cycles: int | None = None,
    ) -> None:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            now = self._now()
            if is_a_share_session(now):
                try:
                    result = self.run_once(mode=ScanMode.LOOP, interval_seconds=interval_seconds)
                except Exception as exc:
                    if on_error:
                        on_error(_safe_error(exc))
                else:
                    if on_result:
                        on_result(result)
                    if result.scan_run.status is ScanRunStatus.FAILED and on_error:
                        on_error("; ".join(result.scan_run.error_messages))
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                self._sleep(interval_seconds)


def is_a_share_session(now: datetime) -> bool:
    local = now.astimezone(timezone(timedelta(hours=8)))
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return 9 * 60 + 30 <= minutes <= 11 * 60 + 30 or 13 * 60 <= minutes <= 15 * 60


def _source_statuses(
    adapter: CandidateSourceAdapter,
    observations: list[CandidateObservation],
    *,
    fallback_error: Exception | None = None,
) -> list[ScanSourceStatus]:
    reported = getattr(adapter, "source_statuses", None)
    if isinstance(reported, list) and all(isinstance(item, ScanSourceStatus) for item in reported):
        return list(reported)
    source = str(getattr(adapter, "source_name", type(adapter).__name__))
    if fallback_error is not None:
        return [
            ScanSourceStatus(
                source=source,
                status=DataStatus.ERROR,
                observation_count=0,
                error_message=_safe_error(fallback_error),
            )
        ]
    return [
        ScanSourceStatus(
            source=source,
            status=DataStatus.AVAILABLE,
            observation_count=len(observations),
        )
    ]


def _source_errors(statuses: list[ScanSourceStatus]) -> list[str]:
    return [
        f"{item.source}:{item.error_message or 'source_error'}"
        for item in statuses
        if item.status is DataStatus.ERROR
    ]


def _safe_error(error: Exception) -> str:
    message = " ".join(str(error).split())[:300]
    return f"{type(error).__name__}:{message}" if message else type(error).__name__


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("scanner clock must return timezone-aware datetime")
    return value.astimezone(UTC)
