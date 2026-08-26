from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

from thesis.candidate_repository import SQLiteCandidateRepository
from thesis.candidates import (
    CandidateObservation,
    ScanMode,
    ScanRun,
    ScanRunStatus,
    ScanSourceStatus,
)
from thesis.models import DataStatus
from thesis.realtime_scan import RealtimeScanner
from thesis.scan_health import derive_loop_health, derive_run_display, format_beijing


DAY = date(2026, 8, 26)
START = datetime(2026, 8, 26, 2, 0, tzinfo=UTC)
BEIJING = timezone(timedelta(hours=8))


def _observation(ref: str = "obs:one") -> CandidateObservation:
    return CandidateObservation(
        instrument_id="CN.SH.600001",
        instrument_name="测试股票",
        source="public.ths.limit_up_pool",
        data_as_of=START,
        retrieved_at=START,
        status=DataStatus.AVAILABLE,
        coverage="test",
        observation_ref_id=ref,
        raw_reference="test[600001]",
        source_snapshot_id="snapshot:test",
        reason="测试原因",
        metrics={"boards": 2},
    )


class FakeAdapter:
    source_name = "fake"

    def __init__(self, observations, statuses, error: Exception | None = None):
        self.observations = observations
        self.source_statuses = statuses
        self.error = error

    def collect(self):
        if self.error:
            raise self.error
        return self.observations


def _status(source: str, status: DataStatus, count: int, error: str | None = None):
    return ScanSourceStatus(
        source=source,
        status=status,
        observation_count=count,
        error_message=error,
    )


def test_succeeded_scan_run_is_persisted(tmp_path):
    adapter = FakeAdapter([_observation()], [_status("source.ok", DataStatus.AVAILABLE, 1)])
    database = tmp_path / "runs.sqlite"
    with SQLiteCandidateRepository(database) as repository:
        result = RealtimeScanner(adapter, repository, now=lambda: START).run_once()
        stored = repository.latest_once_scan_run()

    assert result.scan_run.status is ScanRunStatus.SUCCEEDED
    assert result.scan_run.observation_count == 1
    assert result.scan_run.candidate_count == 1
    assert stored == result.scan_run


def test_partial_scan_uses_successful_source_and_records_error(tmp_path):
    adapter = FakeAdapter(
        [_observation()],
        [
            _status("source.ok", DataStatus.AVAILABLE, 1),
            _status("source.bad", DataStatus.ERROR, 0, "timeout"),
        ],
    )
    with SQLiteCandidateRepository(tmp_path / "partial.sqlite") as repository:
        result = RealtimeScanner(adapter, repository, now=lambda: START).run_once()

    assert result.scan_run.status is ScanRunStatus.PARTIAL
    assert result.scan_run.candidate_count == 1
    assert result.cards
    assert result.scan_run.error_messages == ["source.bad:timeout"]


def test_all_sources_failed_creates_no_candidate(tmp_path):
    adapter = FakeAdapter(
        [],
        [_status("source.bad", DataStatus.ERROR, 0, "offline")],
        error=RuntimeError("offline"),
    )
    with SQLiteCandidateRepository(tmp_path / "failed.sqlite") as repository:
        result = RealtimeScanner(adapter, repository, now=lambda: START).run_once()
        cards = repository.list()

    assert result.scan_run.status is ScanRunStatus.FAILED
    assert result.scan_run.candidate_count == 0
    assert result.cards == []
    assert cards == []


def test_candidate_write_failure_cannot_be_succeeded(tmp_path):
    class FailingRepository(SQLiteCandidateRepository):
        def upsert(self, cards):
            raise RuntimeError("candidate_write_failed")

    adapter = FakeAdapter([_observation()], [_status("source.ok", DataStatus.AVAILABLE, 1)])
    with FailingRepository(tmp_path / "write.sqlite") as repository:
        result = RealtimeScanner(adapter, repository, now=lambda: START).run_once()
        stored = repository.latest_scan_run()

    assert result.scan_run.status is ScanRunStatus.FAILED
    assert stored is not None and stored.status is ScanRunStatus.FAILED
    assert any("candidate_write_failed" in item for item in stored.error_messages)


def test_loop_interval_and_expected_next_persist_after_reopen(tmp_path):
    database = tmp_path / "loop.sqlite"
    adapter = FakeAdapter([_observation()], [_status("source.ok", DataStatus.AVAILABLE, 1)])
    with SQLiteCandidateRepository(database) as repository:
        result = RealtimeScanner(adapter, repository, now=lambda: START).run_once(
            mode=ScanMode.LOOP,
            interval_seconds=180,
        )

    with SQLiteCandidateRepository(database) as reopened:
        stored = reopened.latest_loop_scan_run()

    assert stored is not None
    assert stored.interval_seconds == 180
    assert stored.expected_next_run_at == START + timedelta(seconds=180)
    assert stored.scan_run_id == result.scan_run.scan_run_id


def _running(mode: ScanMode, started: datetime, interval: int | None = None) -> ScanRun:
    return ScanRun(
        scan_run_id=f"run-{mode.value}-{started.timestamp()}",
        started_at=started,
        status=ScanRunStatus.RUNNING,
        mode=mode,
        interval_seconds=interval,
    )


def test_running_scan_display_distinguishes_active_and_stalled():
    run = _running(ScanMode.LOOP, START, 180)
    assert derive_run_display(run, now=START + timedelta(seconds=300)).label == "扫描中"
    assert derive_run_display(run, now=START + timedelta(seconds=361)).label == "疑似停滞"


def test_old_once_does_not_trigger_loop_stopped_warning():
    once = _running(ScanMode.ONCE, START)
    assert derive_run_display(once, now=START + timedelta(hours=1)).label == "疑似停滞"
    during = datetime(2026, 8, 26, 10, 0, tzinfo=BEIJING)
    assert derive_loop_health(None, now=during).label == "尚无 LOOP 运行记录"
    outside = datetime(2026, 8, 26, 8, 0, tzinfo=BEIJING)
    assert derive_loop_health(None, now=outside).label == "当前非盘中扫描时段"


def test_loop_alarm_uses_actual_interval_and_not_outside_session():
    loop = ScanRun(
        scan_run_id="loop-complete",
        started_at=START,
        completed_at=START + timedelta(seconds=10),
        trade_date=DAY,
        status=ScanRunStatus.SUCCEEDED,
        mode=ScanMode.LOOP,
        interval_seconds=300,
        expected_next_run_at=START + timedelta(seconds=310),
    )
    during_before_limit = datetime(2026, 8, 26, 10, 9, tzinfo=BEIJING)
    during_after_limit = datetime(2026, 8, 26, 10, 11, tzinfo=BEIJING)
    outside = datetime(2026, 8, 26, 16, 0, tzinfo=BEIJING)

    assert derive_loop_health(loop, now=during_before_limit).label == "LOOP 扫描健康"
    assert "可能已停止" in derive_loop_health(loop, now=during_after_limit).label
    assert derive_loop_health(loop, now=outside).label == "当前非盘中扫描时段"


def test_later_once_does_not_replace_latest_loop_query(tmp_path):
    database = tmp_path / "independent.sqlite"
    loop = ScanRun(
        scan_run_id="loop",
        started_at=START,
        completed_at=START + timedelta(seconds=1),
        trade_date=DAY,
        status=ScanRunStatus.SUCCEEDED,
        mode=ScanMode.LOOP,
        interval_seconds=180,
        expected_next_run_at=START + timedelta(seconds=181),
    )
    once = ScanRun(
        scan_run_id="once",
        started_at=START + timedelta(hours=1),
        completed_at=START + timedelta(hours=1, seconds=1),
        trade_date=DAY,
        status=ScanRunStatus.SUCCEEDED,
        mode=ScanMode.ONCE,
    )
    with SQLiteCandidateRepository(database) as repository:
        repository.create_scan_run(
            loop.model_copy(
                update={
                    "completed_at": None,
                    "trade_date": None,
                    "status": ScanRunStatus.RUNNING,
                    "expected_next_run_at": None,
                }
            )
        )
        repository.update_scan_run(loop)
        repository.create_scan_run(
            once.model_copy(
                update={
                    "completed_at": None,
                    "trade_date": None,
                    "status": ScanRunStatus.RUNNING,
                }
            )
        )
        repository.update_scan_run(once)
        assert repository.latest_scan_run() == once
        assert repository.latest_once_scan_run() == once
        assert repository.latest_loop_scan_run() == loop
        assert repository.latest_usable_scan_run() == once


def test_beijing_time_conversion():
    assert format_beijing(datetime(2026, 8, 26, 2, 3, 4, tzinfo=UTC)) == "2026-08-26 10:03:04"


def test_home_uses_new_scan_entry_and_not_legacy_scripts():
    home = (Path(__file__).resolve().parents[1] / "home.py").read_text(encoding="utf-8")
    assert "run_realtime_scan.py" in home
    assert '"--once"' in home
    assert "fetch_data.py" not in home
    assert "limit_up_scan.py" not in home
    assert "旧版规则看板" in home
