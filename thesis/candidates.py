from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, model_validator

from .models import DataStatus, DomainModel


ScalarValue = str | int | float | bool | None


class CandidateDecision(StrEnum):
    PENDING = "pending"
    KEEP = "keep"
    IGNORE = "ignore"
    PROMOTE = "promote"


class ScanRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class ScanMode(StrEnum):
    ONCE = "once"
    LOOP = "loop"


class ScanSourceStatus(DomainModel):
    source: str = Field(min_length=1)
    status: DataStatus
    observation_count: int = Field(ge=0)
    error_message: str | None = None


class ScanRun(DomainModel):
    scan_run_id: str = Field(min_length=1)
    started_at: datetime
    completed_at: datetime | None = None
    trade_date: date | None = None
    status: ScanRunStatus
    mode: ScanMode
    interval_seconds: int | None = Field(default=None, ge=1)
    expected_next_run_at: datetime | None = None
    source_statuses: list[ScanSourceStatus] = Field(default_factory=list)
    observation_count: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    error_messages: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_scan_run(self) -> ScanRun:
        for field_name in ("started_at", "completed_at", "expected_next_run_at"):
            value = getattr(self, field_name)
            if value is not None and value.utcoffset() is None:
                raise ValueError(f"{field_name} must include timezone")
        if self.mode is ScanMode.LOOP and self.interval_seconds is None:
            raise ValueError("LOOP scan requires interval_seconds")
        if self.mode is ScanMode.ONCE and self.interval_seconds is not None:
            raise ValueError("ONCE scan must not set interval_seconds")
        if self.mode is ScanMode.ONCE and self.expected_next_run_at is not None:
            raise ValueError("ONCE scan must not set expected_next_run_at")
        if self.status is ScanRunStatus.RUNNING and self.completed_at is not None:
            raise ValueError("RUNNING scan must not have completed_at")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        return self


class CandidateObservation(DomainModel):
    instrument_id: str = Field(pattern=r"^CN\.(SH|SZ|BJ)\.\d{6}$")
    instrument_name: str = Field(min_length=1)
    source: str = Field(min_length=1)
    data_as_of: datetime
    retrieved_at: datetime
    status: DataStatus
    coverage: str = Field(min_length=1)
    observation_ref_id: str = Field(min_length=1)
    raw_reference: str = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    reason: str = ""
    metrics: dict[str, ScalarValue] = Field(default_factory=dict)


class CandidateCard(DomainModel):
    candidate_id: str = Field(min_length=1)
    trade_date: date
    instrument_id: str = Field(pattern=r"^CN\.(SH|SZ|BJ)\.\d{6}$")
    instrument_name: str = Field(min_length=1)
    first_seen_at: datetime
    last_seen_at: datetime
    hit_count: int = Field(ge=1)
    trigger_rules: list[str] = Field(default_factory=list)
    reason_text: str = Field(min_length=1)
    source_snapshot_ids: list[str] = Field(default_factory=list)
    source_names: list[str] = Field(default_factory=list)
    data_as_of: datetime
    freshness_status: DataStatus
    user_decision: CandidateDecision = CandidateDecision.PENDING
    observations: list[CandidateObservation] = Field(default_factory=list)


class CandidateSourceAdapter(Protocol):
    def collect(self) -> list[CandidateObservation]: ...


class TonghuasunLocalAdapter:
    """Boundary only. Business access is deliberately deferred beyond M4a."""

    source_name = "tonghuasun.local"

    def collect(self) -> list[CandidateObservation]:
        raise NotImplementedError("TonghuasunLocalAdapter business integration is deferred")


def candidate_id_for(trade_date: date, instrument_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"candidate:{trade_date.isoformat()}:{instrument_id}"))
