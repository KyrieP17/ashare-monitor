from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

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


class ResearchJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


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


class ResearchJob(DomainModel):
    job_id: UUID
    candidate_id: str = Field(min_length=1)
    thesis_id: UUID
    instrument_id: str = Field(pattern=r"^CN\.(SH|SZ|BJ)\.\d{6}$")
    trade_date: date
    status: ResearchJobStatus
    requested_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    worker_pid: int | None = Field(default=None, ge=1)
    error_type: str | None = None
    cli_version: str | None = None
    executable_kind: str | None = None
    return_code: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    failure_category: str | None = None
    worker_started_at: datetime | None = None
    worker_finished_at: datetime | None = None
    status_message: str | None = None

    @model_validator(mode="after")
    def validate_research_job(self) -> ResearchJob:
        for field_name in (
            "requested_at",
            "started_at",
            "completed_at",
            "worker_started_at",
            "worker_finished_at",
        ):
            value = getattr(self, field_name)
            if value is not None and value.utcoffset() is None:
                raise ValueError(f"{field_name} must include timezone")
        if self.started_at is not None and self.started_at < self.requested_at:
            raise ValueError("started_at cannot precede requested_at")
        if self.completed_at is not None:
            boundary = self.started_at or self.requested_at
            if self.completed_at < boundary:
                raise ValueError("completed_at cannot precede the job start")
        terminal = {
            ResearchJobStatus.SUCCEEDED,
            ResearchJobStatus.FAILED,
            ResearchJobStatus.TIMED_OUT,
        }
        if self.status in terminal and self.completed_at is None:
            raise ValueError("terminal research jobs require completed_at")
        if self.status not in terminal and self.completed_at is not None:
            raise ValueError("non-terminal research jobs cannot have completed_at")
        if self.worker_finished_at is not None and self.worker_started_at is None:
            raise ValueError("worker_finished_at requires worker_started_at")
        if (
            self.worker_finished_at is not None
            and self.worker_finished_at < self.worker_started_at
        ):
            raise ValueError("worker_finished_at cannot precede worker_started_at")
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
