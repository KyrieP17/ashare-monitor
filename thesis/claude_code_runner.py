from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from .candidate_repository import SQLiteCandidateRepository
from .candidates import CandidateDecision, ResearchJob, ResearchJobStatus
from .repository import SQLiteThesisRepository


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "data" / "thesis.db"
COMMAND_TIMEOUT_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = COMMAND_TIMEOUT_SECONDS  # compatibility
STALE_GRACE_SECONDS = 60
STALE_JOB_SECONDS = COMMAND_TIMEOUT_SECONDS + STALE_GRACE_SECONDS
CLI_PREFLIGHT_TIMEOUT_SECONDS = 10
SERVER_NAME = "ashare-thesis-workbench"

READ_TOOLS = (
    "get_market_snapshot",
    "get_stock_observation",
    "get_sector_observations",
    "get_fund_flow_observations",
    "get_catalyst_context",
    "get_price_volume_context",
)
WRITE_TOOL = "submit_thesis_proposal"
ALLOWED_TOOLS = tuple(
    f"mcp__{SERVER_NAME}__{name}" for name in (*READ_TOOLS, WRITE_TOOL)
)
DISALLOWED_TOOLS = (
    "Bash",
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
)
PROVIDER_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "AWS_BEARER_TOKEN_BEDROCK",
)


class ClaudeCodeUnavailableError(RuntimeError):
    pass


class ClaudeResearchBusyError(RuntimeError):
    def __init__(self, active_job: ResearchJob) -> None:
        super().__init__(f"Claude research is already active for {active_job.instrument_id}")
        self.active_job = active_job


@dataclass(frozen=True)
class ClaudeProcessResult:
    returncode: int
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ClaudeCliPreflight:
    executable: Path
    version: str
    executable_kind: str = "claude-code-cli"


@dataclass(frozen=True)
class ClaudeResearchRequestOutcome:
    job: ResearchJob | None
    thesis_id: UUID
    reused_existing: bool
    reused_job: bool


CommandExecutor = Callable[[list[str], Path, int, Mapping[str, str]], ClaudeProcessResult]
WorkerLauncher = Callable[[Path, UUID, Path], int]
CliPreflight = Callable[[Path], ClaudeCliPreflight]


def preflight_claude_cli(executable: Path) -> ClaudeCliPreflight:
    resolved = executable.resolve()
    lowered_name = resolved.name.lower()
    lowered_path = str(resolved).lower()
    if lowered_name in {"python.exe", "python", "python3.exe", "python3"}:
        raise ClaudeCodeUnavailableError("python.exe is not Claude Code CLI")
    if "claude desktop" in lowered_path or "claude.app" in lowered_path:
        raise ClaudeCodeUnavailableError("Claude Desktop GUI is not Claude Code CLI")
    try:
        completed = subprocess.run(
            [str(resolved), "--version"],
            cwd=ROOT,
            env=_sanitized_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLI_PREFLIGHT_TIMEOUT_SECONDS,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeCodeUnavailableError("Claude Code CLI --version preflight failed") from exc
    version_output = " ".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    if completed.returncode != 0:
        raise ClaudeCodeUnavailableError("Claude Code CLI --version returned non-zero")
    if "claude code" not in version_output.lower():
        raise ClaudeCodeUnavailableError("unrecognized claude executable; expected Claude Code CLI")
    # Store only the short version line, never arbitrary command output.
    version = version_output.splitlines()[0][:160]
    return ClaudeCliPreflight(resolved, version)


def resolve_claude_executable(
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    values = environ or os.environ
    explicit = values.get("CLAUDE_CODE_EXECUTABLE")
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate.resolve() if candidate.is_file() else None

    located = shutil.which("claude")
    if located:
        return Path(located).resolve()

    roots: list[Path] = []
    appdata = values.get("APPDATA")
    local_appdata = values.get("LOCALAPPDATA")
    if appdata:
        roots.append(Path(appdata) / "Claude" / "claude-code")
    if local_appdata:
        roots.extend(
            [
                Path(local_appdata) / "Claude" / "claude-code",
                Path(local_appdata) / "Programs" / "Claude Code",
            ]
        )
    matches: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        matches.extend(root.glob("*/claude.exe"))
        matches.extend(root.glob("claude.exe"))
        matches.extend(root.glob("claude.cmd"))
    files = [item.resolve() for item in matches if item.is_file()]
    return max(files, key=lambda item: item.stat().st_mtime) if files else None


def build_mcp_config(database: Path) -> str:
    payload = {
        "mcpServers": {
            SERVER_NAME: {
                "type": "stdio",
                "command": str(ROOT / ".venv" / "Scripts" / "python.exe"),
                "args": ["-m", "thesis.mcp_server"],
                "env": {
                    "PYTHONPATH": str(ROOT),
                    "PYTHONNOUSERSITE": "1",
                    "THESIS_DB_PATH": str(database.resolve()),
                },
            }
        }
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_research_prompt(job: ResearchJob, instrument_name: str) -> str:
    return f"""对本地 Candidate {job.instrument_id}（{instrument_name}）在 {job.trade_date.isoformat()} 做一次盘后深度研究。

这是由 AShare Monitor 用户明确点击触发的单股研究任务。不要给买卖指令，不要调用 Bash、文件、网页或任何未列出的工具。

必须依次调用本地 MCP 工具：
1. get_market_snapshot；
2. get_stock_observation；
3. get_catalyst_context，MISSING 时不得推测；
4. 有板块证据时调用 get_sector_observations 和 get_fund_flow_observations；
5. 按需调用 get_price_volume_context。

只使用工具返回的 observation_ref_id、source_refs 与 snapshot_id 形成完整 ThesisRevision。支持与反对证据并列，明确 Price In 风险、失效条件和数据限制。可以采用有边界的“游资情绪与对手盘”视角，分析可观察的短线角色、市场环境适配、筹码交换和预期差，但不得推断隐藏的主力/席位意图，不得给出仓位、具体买点、买卖或下单指令。不得把 provider reason/theme 当成已经核实的公司公告。

最后必须调用 submit_thesis_proposal：
- instrument_id={job.instrument_id}
- trade_date={job.trade_date.isoformat()}
- thesis_id={job.thesis_id}
- proposal.thesis_id 必须同为 {job.thesis_id}
- proposal.based_on_snapshot_id 使用 get_market_snapshot 返回值
- version=1，revision_type=agent_proposal，accepted=false，derived_from_revision_id=null
- claude_model 填写当前实际 Claude 模型名称
- client_kind=claude-code

Hard Validator 拒绝时不得编造数据绕过；应停止并让本次任务失败。成功提交后只需简短说明 proposal 已进入本地待审核状态。"""


def build_claude_command(executable: Path, prompt: str, database: Path) -> list[str]:
    return [
        str(executable),
        "-p",
        prompt,
        "--output-format",
        "json",
        "--max-turns",
        "16",
        "--permission-mode",
        "dontAsk",
        "--strict-mcp-config",
        "--mcp-config",
        build_mcp_config(database),
        "--allowedTools",
        ",".join(ALLOWED_TOOLS),
        "--disallowedTools",
        ",".join(DISALLOWED_TOOLS),
    ]


def _sanitized_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in PROVIDER_ENV_KEYS:
        environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def execute_claude_command(
    command: list[str],
    working_directory: Path,
    timeout_seconds: int,
    environment: Mapping[str, str],
) -> ClaudeProcessResult:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        command,
        cwd=working_directory,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        # Captured only in this worker's memory for classification. Raw text is
        # never persisted and becomes unreachable when this call returns.
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            process.kill()
        stdout, stderr = process.communicate()
        return ClaudeProcessResult(
            returncode=-1,
            timed_out=True,
            stdout=stdout or "",
            stderr=stderr or "",
        )
    return ClaudeProcessResult(
        returncode=process.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
    )


def launch_worker(database: Path, job_id: UUID, executable: Path) -> int:
    environment = dict(os.environ)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["CLAUDE_CODE_EXECUTABLE"] = str(executable)
    command = [
        sys.executable,
        "-m",
        "thesis.claude_code_runner",
        "--database",
        str(database.resolve()),
        "--job-id",
        str(job_id),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    return process.pid


class ClaudeResearchJobService:
    def __init__(
        self,
        database: str | Path = DEFAULT_DATABASE,
        *,
        launcher: WorkerLauncher = launch_worker,
        executable: Path | None = None,
        preflight: CliPreflight = preflight_claude_cli,
    ) -> None:
        self.database = Path(database)
        self.launcher = launcher
        self.executable = executable
        self.preflight = preflight

    def request(self, candidate_id: str) -> ClaudeResearchRequestOutcome:
        with SQLiteCandidateRepository(self.database) as candidates:
            candidate = candidates.get(candidate_id)
            latest = candidates.latest_research_job(candidate_id)
            if latest is not None and latest.status in {
                ResearchJobStatus.QUEUED,
                ResearchJobStatus.RUNNING,
            }:
                return ClaudeResearchRequestOutcome(
                    job=latest,
                    thesis_id=latest.thesis_id,
                    reused_existing=False,
                    reused_job=True,
                )
            active = candidates.active_research_job()
            if active is not None:
                raise ClaudeResearchBusyError(active)
            with SQLiteThesisRepository(self.database) as theses:
                existing = theses.find_active_card_by_instrument(candidate.instrument_id)
            if existing is not None:
                return ClaudeResearchRequestOutcome(
                    job=None,
                    thesis_id=existing.thesis_id,
                    reused_existing=True,
                    reused_job=False,
                )

            executable = self.executable or resolve_claude_executable()
            if executable is None:
                raise ClaudeCodeUnavailableError(
                    "Claude Code executable was not found; configure CLAUDE_CODE_EXECUTABLE."
                )
            cli = self.preflight(executable)
            now = datetime.now(UTC)
            job = ResearchJob(
                job_id=uuid4(),
                candidate_id=candidate.candidate_id,
                thesis_id=uuid4(),
                instrument_id=candidate.instrument_id,
                trade_date=candidate.trade_date,
                status=ResearchJobStatus.QUEUED,
                requested_at=now,
                cli_version=cli.version,
                executable_kind=cli.executable_kind,
                status_message="等待 Claude Code worker 启动",
            )
            candidates.create_research_job(job)
            try:
                worker_pid = self.launcher(self.database, job.job_id, cli.executable)
            except Exception as exc:
                failed_at = datetime.now(UTC)
                failed = job.model_copy(
                    update={
                        "status": ResearchJobStatus.FAILED,
                        "completed_at": failed_at,
                        "error_type": type(exc).__name__,
                        "failure_category": "worker_crashed",
                        "worker_started_at": failed_at,
                        "worker_finished_at": failed_at,
                        "duration_ms": 0,
                        "status_message": "Claude 深研 worker 启动失败，可重试",
                    }
                )
                candidates.update_research_job(failed)
                raise ClaudeCodeUnavailableError("Claude research worker could not start") from exc
            queued = job.model_copy(update={"worker_pid": worker_pid})
            candidates.update_research_job(queued)
            return ClaudeResearchRequestOutcome(
                job=queued,
                thesis_id=queued.thesis_id,
                reused_existing=False,
                reused_job=False,
            )


def run_research_job(
    database: str | Path,
    job_id: UUID,
    *,
    executor: CommandExecutor = execute_claude_command,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    preflight: CliPreflight = preflight_claude_cli,
) -> ResearchJob:
    database_path = Path(database)
    try:
        return _run_research_job_inner(
            database_path,
            job_id,
            executor=executor,
            timeout_seconds=timeout_seconds,
            preflight=preflight,
        )
    except Exception:
        # Last-resort worker boundary: even repository/candidate/prompt failures
        # must not leave a job permanently RUNNING.
        with SQLiteCandidateRepository(database_path) as candidates:
            current = candidates.get_research_job(str(job_id))
        if current.status in {
            ResearchJobStatus.SUCCEEDED,
            ResearchJobStatus.FAILED,
            ResearchJobStatus.TIMED_OUT,
        }:
            return current
        started_at = current.worker_started_at or datetime.now(UTC)
        fallback = current.model_copy(
            update={
                "status": ResearchJobStatus.RUNNING,
                "started_at": current.started_at or started_at,
                "worker_started_at": started_at,
                "worker_pid": os.getpid(),
            }
        )
        return _finish_job(
            database_path,
            fallback,
            ResearchJobStatus.FAILED,
            "worker_crashed",
            "Claude 深研 worker 未处理异常，已安全收口，可重试",
        )


def _run_research_job_inner(
    database_path: Path,
    job_id: UUID,
    *,
    executor: CommandExecutor,
    timeout_seconds: int,
    preflight: CliPreflight,
) -> ResearchJob:
    with SQLiteCandidateRepository(database_path) as candidates:
        job = candidates.get_research_job(str(job_id))
        if job.status is not ResearchJobStatus.QUEUED:
            return job
        candidate = candidates.get(job.candidate_id)
        worker_started_at = datetime.now(UTC)
        running = job.model_copy(
            update={
                "status": ResearchJobStatus.RUNNING,
                "started_at": worker_started_at,
                "worker_started_at": worker_started_at,
                "worker_pid": os.getpid(),
                "status_message": "Claude 正在调用本地证据工具",
            }
        )
        candidates.update_research_job(running)

    executable = resolve_claude_executable()
    if executable is None:
        return _finish_job(
            database_path,
            running,
            ResearchJobStatus.FAILED,
            "cli_unavailable",
            "未找到 Claude Code，可配置 CLAUDE_CODE_EXECUTABLE 后重试",
        )

    try:
        cli = preflight(executable)
    except ClaudeCodeUnavailableError:
        return _finish_job(
            database_path,
            running,
            ResearchJobStatus.FAILED,
            "cli_unavailable",
            "配置的程序未通过 Claude Code CLI --version 预检；请使用 Claude Desktop + MCP 手动路径",
        )
    running = running.model_copy(
        update={"cli_version": cli.version, "executable_kind": cli.executable_kind}
    )

    prompt = build_research_prompt(running, candidate.instrument_name)
    command = build_claude_command(cli.executable, prompt, database_path)
    try:
        with tempfile.TemporaryDirectory(prefix="ashare-claude-job-") as temporary:
            result = executor(
                command,
                Path(temporary),
                timeout_seconds,
                _sanitized_environment(),
            )
    except Exception as exc:
        return _finish_job(
            database_path,
            running,
            ResearchJobStatus.FAILED,
            "worker_crashed",
            "Claude 深研进程异常退出，候选决定未改变，可重试",
        )

    if result.timed_out:
        return _finish_job(
            database_path,
            running,
            ResearchJobStatus.TIMED_OUT,
            "command_timeout",
            f"Claude 深研命令超过 {timeout_seconds} 秒，已停止，可重试",
            return_code=result.returncode,
        )

    with SQLiteThesisRepository(database_path) as theses:
        thesis = theses.find_active_card_by_instrument(running.instrument_id)
        valid_submission = thesis is not None and thesis.thesis_id == running.thesis_id
        if valid_submission:
            pending = theses.list_pending_proposals(thesis.thesis_id)
            valid_submission = bool(pending)
            if pending:
                review = theses.get_proposal_review(pending[-1].revision_id)
                valid_submission = review.generator_kind.startswith("claude-code:")

    if not valid_submission:
        failure_category = (
            classify_cli_failure(result)
            if result.returncode
            else "proposal_not_submitted"
        )
        return _finish_job(
            database_path,
            running,
            ResearchJobStatus.FAILED,
            failure_category,
            "Claude 未生成可审核 proposal，候选决定未改变，可重试",
            return_code=result.returncode,
        )

    finished = _finish_job(
        database_path,
        running,
        ResearchJobStatus.SUCCEEDED,
        None,
        "Claude proposal 已进入深度研究待审核区",
        return_code=result.returncode,
    )
    # A stale-page reconciliation may have won the race. Only the worker that
    # successfully committed SUCCEEDED may change the Candidate decision.
    if finished.status is ResearchJobStatus.SUCCEEDED:
        with SQLiteCandidateRepository(database_path) as candidates:
            candidates.set_decision(running.candidate_id, CandidateDecision.PROMOTE)
    return finished


def classify_cli_failure(result: ClaudeProcessResult) -> str:
    """Classify transient in-memory output without returning or persisting it."""
    text = f"{result.stdout}\n{result.stderr}".lower()
    if any(marker in text for marker in ("not authenticated", "please login", "authentication required")):
        return "cli_not_authenticated"
    if any(marker in text for marker in ("usage limit", "rate limit", "monthly limit", "quota")):
        return "sdk_usage_limit"
    if any(marker in text for marker in ("unknown argument", "unknown option", "invalid argument")):
        return "invalid_cli_arguments"
    if "mcp" in text and any(marker in text for marker in ("failed", "could not start", "startup")):
        return "mcp_start_failed"
    return "unknown_cli_failure"


def reconcile_stale_job(
    repository: SQLiteCandidateRepository,
    job: ResearchJob | None,
    *,
    now: datetime | None = None,
) -> ResearchJob | None:
    if job is None or job.status not in {
        ResearchJobStatus.QUEUED,
        ResearchJobStatus.RUNNING,
    }:
        return job
    current = now or datetime.now(UTC)
    boundary = job.started_at or job.requested_at
    if (current - boundary).total_seconds() <= STALE_JOB_SECONDS:
        return job
    stale = job.model_copy(
        update={
            "status": ResearchJobStatus.TIMED_OUT,
            "completed_at": current,
            "worker_started_at": job.worker_started_at or boundary,
            "worker_finished_at": current,
            "duration_ms": max(0, int((current - boundary).total_seconds() * 1000)),
            "error_type": "command_timeout",
            "failure_category": "command_timeout",
            "status_message": (
                f"后台 worker 已超过命令超时 {COMMAND_TIMEOUT_SECONDS} 秒加"
                f" {STALE_GRACE_SECONDS} 秒收口宽限，已标记超时，可重试"
            ),
        }
    )
    repository.update_research_job(stale)
    return stale


def _finish_job(
    database: Path,
    job: ResearchJob,
    status: ResearchJobStatus,
    error_type: str | None,
    message: str,
    *,
    return_code: int | None = None,
) -> ResearchJob:
    finished_at = datetime.now(UTC)
    started_at = job.worker_started_at or job.started_at or job.requested_at
    finished = job.model_copy(
        update={
            "status": status,
            "completed_at": finished_at,
            "worker_finished_at": finished_at,
            "duration_ms": max(0, int((finished_at - started_at).total_seconds() * 1000)),
            "error_type": error_type,
            "failure_category": error_type,
            "return_code": return_code,
            "status_message": message,
        }
    )
    with SQLiteCandidateRepository(database) as repository:
        try:
            repository.update_research_job(finished)
        except ValueError:
            current = repository.get_research_job(str(job.job_id))
            if current.status in {
                ResearchJobStatus.SUCCEEDED,
                ResearchJobStatus.FAILED,
                ResearchJobStatus.TIMED_OUT,
            }:
                return current
            raise
    return finished


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one persisted Claude research job.")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--job-id", type=UUID, required=True)
    args = parser.parse_args()
    result = run_research_job(args.database, args.job_id)
    return 0 if result.status is ResearchJobStatus.SUCCEEDED else 1


if __name__ == "__main__":
    raise SystemExit(main())
