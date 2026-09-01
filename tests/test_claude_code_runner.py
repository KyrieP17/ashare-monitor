from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from thesis.candidate_repository import SQLiteCandidateRepository
from thesis.candidate_rules import RULE_LIMIT_UP
from thesis.candidates import (
    CandidateCard,
    CandidateDecision,
    CandidateObservation,
    ResearchJobStatus,
    candidate_id_for,
)
from thesis.claude_code_runner import (
    ALLOWED_TOOLS,
    ClaudeCodeUnavailableError,
    ClaudeProcessResult,
    ClaudeResearchBusyError,
    ClaudeResearchJobService,
    build_claude_command,
    build_research_prompt,
    preflight_claude_cli,
    reconcile_stale_job,
    resolve_claude_executable,
    run_research_job,
)
import thesis.claude_code_runner as claude_runner
from thesis.gate3_generator import GenerationRequest, RecordedProposalGenerator
from thesis.mcp_server import MCPResearchService
from thesis.models import DataStatus
from thesis.repository import SQLiteThesisRepository


DAY = date(2026, 8, 27)
NOW = datetime(2026, 8, 27, 7, 0, tzinfo=UTC)


def candidate(code: str = "002300") -> CandidateCard:
    instrument_id = f"CN.SZ.{code}"
    observation = CandidateObservation(
        instrument_id=instrument_id,
        instrument_name="Claude深研测试",
        source="public.ths.limit_up_pool",
        data_as_of=NOW,
        retrieved_at=NOW,
        status=DataStatus.AVAILABLE,
        coverage="full_limit_up_pool:1",
        observation_ref_id=f"candidate:limit-up:{code}",
        raw_reference=f"limit_up_pool[{code}]",
        source_snapshot_id=f"snapshot:claude-code:{code}",
        reason="普通首板+测试催化剂",
        metrics={"boards": 1, "price": 12.3, "chg_pct": 10.0, "open_num": 0},
    )
    return CandidateCard(
        candidate_id=candidate_id_for(DAY, instrument_id),
        trade_date=DAY,
        instrument_id=instrument_id,
        instrument_name=observation.instrument_name,
        first_seen_at=NOW,
        last_seen_at=NOW,
        hit_count=1,
        trigger_rules=[RULE_LIMIT_UP],
        reason_text=observation.reason,
        source_snapshot_ids=[observation.source_snapshot_id],
        source_names=[observation.source],
        data_as_of=NOW,
        freshness_status=DataStatus.AVAILABLE,
        observations=[observation],
    )


def seed(database: Path, code: str = "002300") -> CandidateCard:
    card = candidate(code)
    with SQLiteCandidateRepository(database) as repository:
        return repository.upsert([card])[0]


def fake_executable(tmp_path: Path) -> Path:
    executable = tmp_path / "claude.cmd"
    executable.write_text("@echo 2.0.0 (Claude Code)\n", encoding="utf-8")
    return executable


def test_request_is_persisted_and_duplicate_active_job_is_reused(tmp_path):
    database = tmp_path / "jobs.sqlite"
    card = seed(database)
    executable = fake_executable(tmp_path)
    launches = []

    def launcher(db, job_id, cli):
        launches.append((db, job_id, cli))
        return 4321

    service = ClaudeResearchJobService(database, launcher=launcher, executable=executable)
    first = service.request(card.candidate_id)
    second = service.request(card.candidate_id)

    assert first.job is not None
    assert first.job.status is ResearchJobStatus.QUEUED
    assert first.job.worker_pid == 4321
    assert second.reused_job is True
    assert second.job == first.job
    assert len(launches) == 1
    with SQLiteCandidateRepository(database) as reopened:
        assert reopened.latest_research_job(card.candidate_id) == first.job
        assert reopened.get(card.candidate_id).user_decision is CandidateDecision.PENDING


def test_command_uses_strict_mcp_allowlist_without_bare_or_provider_credentials(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "command.sqlite"
    card = seed(database)
    executable = fake_executable(tmp_path)
    service = ClaudeResearchJobService(
        database,
        launcher=lambda *_: 1234,
        executable=executable,
    )
    job = service.request(card.candidate_id).job
    assert job is not None
    prompt = build_research_prompt(job, card.instrument_name)
    command = build_claude_command(executable, prompt, database)

    assert "--bare" not in command
    assert "--strict-mcp-config" in command
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert set(command[command.index("--allowedTools") + 1].split(",")) == set(ALLOWED_TOOLS)
    assert all(item.startswith("mcp__ashare-thesis-workbench__") for item in ALLOWED_TOOLS)
    config = json.loads(command[command.index("--mcp-config") + 1])
    server = config["mcpServers"]["ashare-thesis-workbench"]
    assert server["args"] == ["-m", "thesis.mcp_server"]
    assert set(server["env"]) == {"PYTHONPATH", "PYTHONNOUSERSITE", "THESIS_DB_PATH"}
    assert "client_kind=claude-code" in prompt

    monkeypatch.setenv("CLAUDE_CODE_EXECUTABLE", str(executable))
    assert resolve_claude_executable() == executable.resolve()


def test_worker_e2e_creates_pending_claude_code_thesis_then_promotes_candidate(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "success.sqlite"
    card = seed(database)
    executable = fake_executable(tmp_path)
    service = ClaudeResearchJobService(
        database,
        launcher=lambda *_: 1234,
        executable=executable,
    )
    job = service.request(card.candidate_id).job
    assert job is not None
    monkeypatch.setenv("CLAUDE_CODE_EXECUTABLE", str(executable))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-child")

    def executor(command, working_directory, timeout_seconds, environment):
        assert working_directory != Path.cwd()
        assert timeout_seconds == 300
        assert "ANTHROPIC_API_KEY" not in environment
        mcp = MCPResearchService(database)
        snapshot_payload = mcp.get_market_snapshot(DAY.isoformat(), [card.instrument_id])
        with SQLiteThesisRepository(database) as repository:
            snapshot = repository.get_snapshot(snapshot_payload["snapshot_id"])
        proposal = RecordedProposalGenerator().generate(
            GenerationRequest(
                thesis_id=job.thesis_id,
                snapshot=snapshot,
                instrument=snapshot.stock_observations[0].instrument,
                version=1,
                derived_from_revision_id=None,
                previous_revision=None,
                attempt=1,
            )
        )
        result = mcp.submit_thesis_proposal(
            card.instrument_id,
            DAY.isoformat(),
            str(job.thesis_id),
            proposal,
            "claude-sonnet-test",
            "claude-code",
        )
        assert result["ok"] is True
        return ClaudeProcessResult(returncode=0)

    result = run_research_job(database, job.job_id, executor=executor)

    assert result.status is ResearchJobStatus.SUCCEEDED
    with SQLiteCandidateRepository(database) as candidates:
        assert candidates.get(card.candidate_id).user_decision is CandidateDecision.PROMOTE
    with SQLiteThesisRepository(database) as theses:
        thesis = theses.get_card(job.thesis_id)
        pending = theses.list_pending_proposals(job.thesis_id)
        review = theses.get_proposal_review(pending[0].revision_id)
        assert thesis.current_accepted_revision_id is None
        assert pending[0].accepted is False
        assert review.generator_kind == "claude-code:claude-sonnet-test"
        assert review.graph_trace[0] == "claude_code"


def test_failed_worker_preserves_candidate_and_allows_retry(tmp_path, monkeypatch):
    database = tmp_path / "failure.sqlite"
    card = seed(database)
    executable = fake_executable(tmp_path)
    service = ClaudeResearchJobService(
        database,
        launcher=lambda *_: 1234,
        executable=executable,
    )
    job = service.request(card.candidate_id).job
    assert job is not None
    monkeypatch.setenv("CLAUDE_CODE_EXECUTABLE", str(executable))

    result = run_research_job(
        database,
        job.job_id,
        executor=lambda *_: ClaudeProcessResult(returncode=1),
    )

    assert result.status is ResearchJobStatus.FAILED
    assert result.failure_category == "unknown_cli_failure"
    with SQLiteCandidateRepository(database) as candidates:
        assert candidates.get(card.candidate_id).user_decision is CandidateDecision.PENDING
    retry = service.request(card.candidate_id)
    assert retry.job is not None and retry.job.job_id != job.job_id


def test_stale_running_job_is_marked_timed_out(tmp_path):
    database = tmp_path / "stale.sqlite"
    card = seed(database)
    executable = fake_executable(tmp_path)
    service = ClaudeResearchJobService(
        database,
        launcher=lambda *_: 1234,
        executable=executable,
    )
    job = service.request(card.candidate_id).job
    assert job is not None
    old = job.model_copy(
        update={
            "status": ResearchJobStatus.RUNNING,
            "started_at": job.requested_at,
            "worker_pid": 1234,
        }
    )
    with SQLiteCandidateRepository(database) as repository:
        repository.update_research_job(old)
        reconciled = reconcile_stale_job(
            repository,
            old,
            now=job.requested_at + timedelta(minutes=11),
        )
        assert reconciled is not None
        assert reconciled.status is ResearchJobStatus.TIMED_OUT
        assert repository.get(card.candidate_id).user_decision is CandidateDecision.PENDING


def test_python_and_claude_desktop_are_rejected_as_cli(tmp_path):
    with pytest.raises(ClaudeCodeUnavailableError, match="python.exe"):
        preflight_claude_cli(Path(__import__("sys").executable))
    desktop = tmp_path / "Claude Desktop" / "Claude.exe"
    desktop.parent.mkdir()
    desktop.write_bytes(b"not launched")
    with pytest.raises(ClaudeCodeUnavailableError, match="Desktop GUI"):
        preflight_claude_cli(desktop)


def test_fake_claude_code_cli_passes_version_preflight(tmp_path):
    result = preflight_claude_cli(fake_executable(tmp_path))
    assert result.executable_kind == "claude-code-cli"
    assert "Claude Code" in result.version


def test_unrecognized_claude_executable_is_rejected(tmp_path):
    executable = tmp_path / "claude.cmd"
    executable.write_text("@echo Claude Desktop 1.0\n", encoding="utf-8")
    with pytest.raises(ClaudeCodeUnavailableError, match="unrecognized"):
        preflight_claude_cli(executable)


def test_cli_failure_is_classified_without_persisting_raw_output(tmp_path, monkeypatch):
    database = tmp_path / "classified.sqlite"
    card = seed(database)
    executable = fake_executable(tmp_path)
    job = ClaudeResearchJobService(
        database, launcher=lambda *_: 1234, executable=executable
    ).request(card.candidate_id).job
    assert job is not None
    monkeypatch.setenv("CLAUDE_CODE_EXECUTABLE", str(executable))
    result = run_research_job(
        database,
        job.job_id,
        executor=lambda *_: ClaudeProcessResult(
            returncode=1,
            stderr="Please login; private-account-marker",
        ),
    )
    assert result.failure_category == "cli_not_authenticated"
    assert result.return_code == 1
    assert "private-account-marker" not in result.model_dump_json()


def test_unhandled_worker_exception_is_closed_as_failed(tmp_path, monkeypatch):
    database = tmp_path / "crashed.sqlite"
    card = seed(database)
    executable = fake_executable(tmp_path)
    job = ClaudeResearchJobService(
        database, launcher=lambda *_: 1234, executable=executable
    ).request(card.candidate_id).job
    assert job is not None
    monkeypatch.setenv("CLAUDE_CODE_EXECUTABLE", str(executable))
    monkeypatch.setattr(claude_runner, "build_research_prompt", lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
    result = run_research_job(database, job.job_id)
    assert result.status is ResearchJobStatus.FAILED
    assert result.failure_category == "worker_crashed"
    assert result.worker_finished_at is not None


def test_global_claude_job_limit_blocks_another_candidate(tmp_path):
    database = tmp_path / "global-limit.sqlite"
    first = seed(database, "002301")
    second = seed(database, "002302")
    executable = fake_executable(tmp_path)
    service = ClaudeResearchJobService(
        database, launcher=lambda *_: 1234, executable=executable
    )
    service.request(first.candidate_id)
    with pytest.raises(ClaudeResearchBusyError) as captured:
        service.request(second.candidate_id)
    assert captured.value.active_job.instrument_id == first.instrument_id
    with SQLiteCandidateRepository(database) as repository:
        assert repository.latest_research_job(second.candidate_id) is None
