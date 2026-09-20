"""Create and run disposable benchmark worktrees.

This module deliberately has no knowledge of benchmark checks or model runtimes.
It gives each candidate a detached checkout of the same commit and records only
safe execution metadata outside that checkout.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_CANDIDATE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class WorktreeRunnerError(RuntimeError):
    """Raised when the runner cannot prepare or execute an isolated run."""


@dataclass(frozen=True)
class WorktreeRun:
    """A detached candidate checkout and its durable, evaluator-owned log."""

    run_id: str
    candidate_id: str
    commit: str
    path: Path
    log_path: Path


@dataclass(frozen=True)
class ExecutionResult:
    """Safe metadata about one candidate process invocation."""

    status: str
    exit_code: int | None
    duration_ms: int
    stdout_bytes: int
    stderr_bytes: int


class WorktreeRunner:
    """Own disposable, detached worktrees below one evaluator-owned directory."""

    def __init__(self, repository: Path, runs_directory: Path):
        self.repository = Path(repository).resolve()
        self.runs_directory = Path(runs_directory).resolve()
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._cancelled: set[str] = set()
        self._runs: dict[str, Path] = {}
        self._assert_repository()
        self.runs_directory.mkdir(parents=True, exist_ok=True)

    def create(self, candidate_id: str, starting_commit: str) -> WorktreeRun:
        """Create a detached worktree at ``starting_commit`` for one candidate."""
        if not _CANDIDATE_ID.fullmatch(candidate_id):
            raise ValueError(
                "candidate_id must use only letters, digits, '.', '_' or '-'"
            )
        commit = self._resolve_commit(starting_commit)
        run_id = uuid.uuid4().hex
        run_directory = self.runs_directory / run_id
        worktree = run_directory / "worktree"
        log_path = run_directory / "execution.jsonl"
        run_directory.mkdir(mode=0o700)
        run = WorktreeRun(run_id, candidate_id, commit, worktree, log_path)
        self._write_event(run, "created")
        try:
            self._git("worktree", "add", "--detach", str(worktree), commit)
        except Exception:
            self._write_event(run, "creation_failed")
            # Retain the evaluator-owned log, but remove a checkout or Git
            # registration left behind by a partially failed `worktree add`.
            self._git("worktree", "remove", "--force", str(worktree), check=False)
            shutil.rmtree(worktree, ignore_errors=True)
            raise
        with self._lock:
            self._runs[run_id] = worktree
        self._write_event(run, "ready")
        return run

    def execute(
        self, run: WorktreeRun, command: Sequence[str], timeout_seconds: float
    ) -> ExecutionResult:
        """Run a candidate command, then always remove its worktree.

        Command text and output are intentionally not persisted: both can contain
        credentials.  The returned byte counts allow a later evaluator to record
        diagnostics without turning the runner log into a secret store.
        """
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("command must contain non-empty strings")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if not self._is_owned_run(run) or not run.path.is_dir():
            raise WorktreeRunnerError("worktree is not available")

        started = time.monotonic()
        self._write_event(run, "execution_started")
        process: subprocess.Popen[bytes] | None = None
        status = "launch_failed"
        exit_code: int | None = None
        stdout = b""
        stderr = b""
        try:
            process = subprocess.Popen(
                list(command),
                cwd=run.path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=self._candidate_environment(),
            )
            with self._lock:
                self._processes[run.run_id] = process
                cancelled = run.run_id in self._cancelled
            if cancelled:
                self._terminate(process)
            status = "completed"
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                status = "timed_out"
                self._terminate(process)
                stdout, stderr = self._drain_after_termination(process)
            with self._lock:
                if run.run_id in self._cancelled:
                    status = "cancelled"
            exit_code = process.returncode
            return ExecutionResult(
                status=status,
                exit_code=exit_code,
                duration_ms=round((time.monotonic() - started) * 1000),
                stdout_bytes=len(stdout),
                stderr_bytes=len(stderr),
            )
        finally:
            with self._lock:
                self._processes.pop(run.run_id, None)
            self._write_event(run, status, exit_code=exit_code)
            self.cleanup(run, reason=status)

    def cancel(self, run: WorktreeRun) -> None:
        """Request cancellation of an active run; ``execute`` performs cleanup."""
        with self._lock:
            if not self._is_owned_run(run):
                raise WorktreeRunnerError(
                    "refusing to cancel a worktree not owned by this runner"
                )
            self._cancelled.add(run.run_id)
            process = self._processes.get(run.run_id)
        self._write_event(run, "cancellation_requested")
        if process is not None:
            self._terminate(process)

    def cleanup(self, run: WorktreeRun, *, reason: str = "completed") -> None:
        """Remove only the detached worktree owned by ``run``; keep its log."""
        if not self._is_owned_run(run):
            raise WorktreeRunnerError(
                "refusing to clean a worktree not owned by this runner"
            )
        self._write_event(run, "cleanup_started", reason=reason)
        try:
            # `git worktree remove` also drops the administrative entry when a
            # candidate has already deleted its checkout.
            result = self._git(
                "worktree", "remove", "--force", str(run.path), check=False
            )
            if result.returncode != 0 and run.path.exists():
                raise WorktreeRunnerError("git could not remove the isolated worktree")
            self._write_event(run, "cleanup_finished", reason=reason)
        finally:
            # Cleanup is terminal: remove in-memory ownership and cancellation
            # state so a long-lived runner does not retain every completed run.
            with self._lock:
                self._runs.pop(run.run_id, None)
                self._cancelled.discard(run.run_id)

    def _assert_repository(self) -> None:
        result = subprocess.run(
            ["git", "-C", str(self.repository), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
            env=self._git_environment(),
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise WorktreeRunnerError("repository must be a Git working tree")

    def _resolve_commit(self, starting_commit: str) -> str:
        if (
            not isinstance(starting_commit, str)
            or not starting_commit
            or starting_commit.startswith("-")
        ):
            raise ValueError("starting_commit is required")
        result = self._git("rev-parse", "--verify", f"{starting_commit}^{{commit}}")
        return result.stdout.decode("ascii").strip()

    def _git(
        self, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            capture_output=True,
            check=False,
            env=self._git_environment(),
        )
        if check and result.returncode != 0:
            raise WorktreeRunnerError("git worktree operation failed")
        return result

    def _is_owned_run(self, run: WorktreeRun) -> bool:
        try:
            expected_path = self._runs.get(run.run_id)
            return (
                expected_path is not None
                and run.path.resolve() == expected_path.resolve()
            )
        except OSError:
            return False

    def _write_event(self, run: WorktreeRun, event: str, **details: object) -> None:
        record = {
            "event": event,
            "run_id": run.run_id,
            "candidate_id": run.candidate_id,
            "commit": run.commit,
            "timestamp": time.time(),
            **details,
        }
        run.log_path.parent.mkdir(parents=True, exist_ok=True)
        with run.log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
        os.chmod(run.log_path, 0o600)

    @staticmethod
    def _git_environment() -> dict[str, str]:
        """Avoid inheriting a caller's Git worktree or index selection."""
        return {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }

    @staticmethod
    def _candidate_environment() -> dict[str, str]:
        """Prevent a caller's Git selectors from escaping the worktree."""
        return WorktreeRunner._git_environment()

    @staticmethod
    def _drain_after_termination(
        process: subprocess.Popen[bytes], timeout_seconds: float = 2
    ) -> tuple[bytes, bytes]:
        """Drain pipes briefly without letting escaped children block cleanup."""
        try:
            return process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            stdout = error.output or b""
            stderr = error.stderr or b""
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            process.wait()
            return stdout, stderr

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
