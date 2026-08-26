from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from .candidates import ScanMode, ScanRun, ScanRunStatus
from .realtime_scan import is_a_share_session


BEIJING = timezone(timedelta(hours=8))
ONCE_STALL_SECONDS = 120


@dataclass(frozen=True)
class HealthDisplay:
    label: str
    level: str


def derive_run_display(
    run: ScanRun,
    *,
    now: datetime | None = None,
    once_stall_seconds: int = ONCE_STALL_SECONDS,
) -> HealthDisplay:
    current = _aware_now(now)
    if run.status is not ScanRunStatus.RUNNING:
        return HealthDisplay(run.status.value.upper(), _status_level(run.status))
    threshold = (
        2 * run.interval_seconds
        if run.mode is ScanMode.LOOP and run.interval_seconds is not None
        else once_stall_seconds
    )
    if (current - run.started_at).total_seconds() > threshold:
        return HealthDisplay("疑似停滞", "error")
    return HealthDisplay("扫描中", "info")


def derive_loop_health(latest_loop: ScanRun | None, *, now: datetime | None = None) -> HealthDisplay:
    current = _aware_now(now)
    if not is_a_share_session(current):
        return HealthDisplay("当前非盘中扫描时段", "info")
    if latest_loop is None:
        return HealthDisplay("尚无 LOOP 运行记录", "warning")
    interval = latest_loop.interval_seconds
    if interval is None:
        return HealthDisplay("LOOP 记录缺少扫描间隔", "error")
    reference = latest_loop.completed_at or latest_loop.started_at
    if (current - reference).total_seconds() > 2 * interval:
        if latest_loop.status is ScanRunStatus.RUNNING:
            return HealthDisplay("LOOP 扫描疑似停滞", "error")
        return HealthDisplay("未检测到持续扫描，LOOP 进程可能已停止。", "error")
    if latest_loop.status is ScanRunStatus.RUNNING:
        return HealthDisplay("LOOP 正在扫描", "info")
    if latest_loop.status is ScanRunStatus.FAILED:
        return HealthDisplay("最近一轮 LOOP 失败，等待下一轮重试", "warning")
    if latest_loop.status is ScanRunStatus.PARTIAL:
        return HealthDisplay("LOOP 最近一轮部分成功", "warning")
    return HealthDisplay("LOOP 扫描健康", "success")


def format_beijing(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.utcoffset() is None:
        raise ValueError("display time must include timezone")
    return value.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def _status_level(status: ScanRunStatus) -> str:
    return {
        ScanRunStatus.SUCCEEDED: "success",
        ScanRunStatus.PARTIAL: "warning",
        ScanRunStatus.FAILED: "error",
        ScanRunStatus.RUNNING: "info",
    }[status]


def _aware_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("health clock must include timezone")
    return current.astimezone(UTC)

