"""Create and run disposable benchmark worktrees.

This module deliberately has no knowledge of benchmark checks or model runtimes.
It gives each candidate a detached checkout of the same commit and records only
safe execution metadata outside that checkout.

Trust boundary: a candidate runs as the evaluator's own OS user, so nothing in
this module can *prevent* it from writing evaluator files.  The runner keeps the
lifecycle log out of the candidate's directory tree, never follows links when
writing it, and detects (but cannot stop) tampering.  See ``benchmarks/README.md``.
"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_CANDIDATE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
# Conservative revision syntax: no option-like or whitespace/control characters.
_REVISION = re.compile(r"[A-Za-z0-9_@][A-Za-z0-9._/@~^{}-]{0,254}\Z")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
# The only inherited candidate variables.  Everything else, notably credentials
# and every ``GIT_*`` selector, is dropped; HOME is replaced per run.
_CANDIDATE_ENVIRONMENT_ALLOWLIST = (
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
)


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


@dataclass
class _RunState:
    """Evaluator-side facts about one run; the caller's ``WorktreeRun`` is not
    trusted for anything that selects a path to write or delete."""

    run_directory: Path
    worktree: Path
    home: Path
    log_path: Path
    admin_directory: Path | None = None
    log_identity: tuple[int, int] | None = None
    log_size: int = 0


class _PipeDrain:
    """Read candidate output without keeping it: only byte counts survive."""

    def __init__(self, process: subprocess.Popen[bytes]):
        self._selector = selectors.DefaultSelector()
        self._streams = {}
        self.counts = {"stdout": 0, "stderr": 0}
        for name in ("stdout", "stderr"):
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            self._selector.register(stream, selectors.EVENT_READ, name)
            self._streams[name] = stream

    @property
    def open(self) -> bool:
        return bool(self._selector.get_map())

    def pump(self, timeout: float) -> None:
        for key, _ in self._selector.select(timeout):
            try:
                chunk = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            if chunk:
                self.counts[key.data] += len(chunk)
            else:
                self._selector.unregister(key.fileobj)

    def close(self) -> None:
        """Stop reading even if an escaped process still holds the write end."""
        self._selector.close()
        for stream in self._streams.values():
            stream.close()


class WorktreeRunner:
    """Own disposable, detached worktrees below one evaluator-owned directory.

    A runner is meant to be long-lived: all per-run state is released when
    ``cleanup`` finishes, so the registries stay bounded by the active runs.
    Lifecycle logs live in ``logs_directory`` (default: a sibling of
    ``runs_directory``), never below the candidate's directory chain.
    """

    term_grace_seconds = 2.0
    drain_seconds = 1.0
    poll_seconds = 0.05

    def __init__(
        self,
        repository: Path,
        runs_directory: Path,
        logs_directory: Path | None = None,
    ):
        self.repository = Path(repository).resolve()
        self.runs_directory = Path(runs_directory).resolve()
        if logs_directory is None:
            logs_directory = self.runs_directory.with_name(
                self.runs_directory.name + "-logs"
            )
        self.logs_directory = Path(logs_directory).resolve()
        if self.logs_directory.is_relative_to(
            self.runs_directory
        ) or self.runs_directory.is_relative_to(self.logs_directory):
            raise ValueError("logs_directory and runs_directory must not nest")
        self._lock = threading.Lock()
        # Serializes log appends: the size check below assumes a single writer.
        self._log_lock = threading.Lock()
        self._cancelled: set[str] = set()
        self._runs: dict[str, _RunState] = {}
        self._assert_repository()
        self._common_git_directory = self._find_common_git_directory()
        self._ensure_private_directory(self.runs_directory)
        self._ensure_private_directory(self.logs_directory)

    def create(self, candidate_id: str, starting_commit: str) -> WorktreeRun:
        """Create a detached worktree at ``starting_commit`` for one candidate."""
        if not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(
            candidate_id
        ):
            raise ValueError(
                "candidate_id must use only letters, digits, '.', '_' or '-'"
            )
        commit = self._resolve_commit(starting_commit)
        run_id = uuid.uuid4().hex
        run_directory = self.runs_directory / run_id
        state = _RunState(
            run_directory=run_directory,
            worktree=run_directory / "worktree",
            home=run_directory / "home",
            log_path=self.logs_directory / f"{run_id}.jsonl",
        )
        run = WorktreeRun(run_id, candidate_id, commit, state.worktree, state.log_path)
        os.mkdir(run_directory, 0o700)
        with self._lock:
            self._runs[run_id] = state
        try:
            self._write_event(run, "created")
            os.mkdir(state.home, 0o700)
            self._git("worktree", "add", "--detach", "--", str(state.worktree), commit)
            state.admin_directory = Path(
                self._git("-C", str(state.worktree), "rev-parse", "--absolute-git-dir")
                .stdout.decode()
                .strip()
            )
            self._write_event(run, "ready")
        except BaseException:
            # The log lives outside the run directory, so the partial checkout
            # and any Git registration can go while the evidence stays.
            self._try_event(run, "creation_failed")
            try:
                self._remove_checkout(state)
            finally:
                with self._lock:
                    self._runs.pop(run_id, None)
            raise
        return run

    def execute(
        self, run: WorktreeRun, command: Sequence[str], timeout_seconds: float
    ) -> ExecutionResult:
        """Run a candidate command, then always remove its worktree.

        Argument errors are rejected before anything runs and leave the worktree
        in place for the caller to reuse or ``cleanup``.  Once the command has
        been requested the worktree is removed on every path.

        Command text and output are intentionally not persisted: both can contain
        credentials.  Output is drained as it is produced and discarded, so only
        byte counts are kept and memory use does not depend on candidate output.
        The returned byte counts allow a later evaluator to record diagnostics
        without turning the runner log into a secret store.
        """
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("command must contain non-empty strings")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a finite number above zero")
        state = self._owned_state(run)
        if state is None or not run.path.is_dir():
            raise WorktreeRunnerError("worktree is not available")

        started = time.monotonic()
        status = "launch_failed"
        exit_code: int | None = None
        try:
            self._write_event(run, "execution_started")
            process = subprocess.Popen(
                list(command),
                cwd=run.path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=self._candidate_environment(state),
            )
            status, drain = self._supervise(run.run_id, process, timeout_seconds)
            exit_code = process.returncode
            return ExecutionResult(
                status=status,
                exit_code=exit_code,
                duration_ms=round((time.monotonic() - started) * 1000),
                stdout_bytes=drain.counts["stdout"],
                stderr_bytes=drain.counts["stderr"],
            )
        finally:
            self._try_event(run, status, exit_code=exit_code)
            self.cleanup(run, reason=status)

    def cancel(self, run: WorktreeRun) -> None:
        """Request cancellation of an active run; ``execute`` performs cleanup.

        Only ``execute`` signals the candidate, so a finished process is never
        signalled from here.  ``execute`` notices the request within
        ``poll_seconds``.
        """
        with self._lock:
            if self._owned_state(run) is None:
                raise WorktreeRunnerError(
                    "refusing to cancel a worktree not owned by this runner"
                )
            self._cancelled.add(run.run_id)
        self._write_event(run, "cancellation_requested")

    def cleanup(self, run: WorktreeRun, *, reason: str = "completed") -> None:
        """Remove only the checkout owned by ``run``; keep its log."""
        state = self._owned_state(run)
        if state is None:
            raise WorktreeRunnerError(
                "refusing to clean a worktree not owned by this runner"
            )
        log_error = self._try_event(run, "cleanup_started", reason=reason)
        try:
            self._remove_checkout(state)
            log_error = log_error or self._try_event(
                run, "cleanup_finished", reason=reason
            )
        finally:
            # Cleanup is terminal: remove in-memory ownership and cancellation
            # state so a long-lived runner does not retain every completed run.
            with self._lock:
                self._runs.pop(run.run_id, None)
                self._cancelled.discard(run.run_id)
        if log_error is not None:
            raise log_error

    def _supervise(
        self, run_id: str, process: subprocess.Popen[bytes], timeout_seconds: float
    ) -> tuple[str, _PipeDrain]:
        """Drain output until exit, deadline or cancellation, then contain it."""
        drain = _PipeDrain(process)
        deadline = time.monotonic() + timeout_seconds
        status = "completed"
        exited_at: float | None = None
        try:
            while True:
                now = time.monotonic()
                with self._lock:
                    cancelled = run_id in self._cancelled
                if cancelled:
                    status = "cancelled"
                    break
                if now >= deadline:
                    status = "timed_out"
                    break
                if exited_at is None and process.poll() is not None:
                    exited_at = now
                if exited_at is None:
                    if not drain.open:
                        time.sleep(self.poll_seconds)
                        continue
                elif not drain.open or now - exited_at >= self.drain_seconds:
                    # Done, or a leftover background process keeps the pipes open.
                    break
                if drain.open:
                    drain.pump(min(self.poll_seconds, deadline - now))
            if status != "completed":
                self._terminate(process)
                until = time.monotonic() + self.drain_seconds
                while drain.open and time.monotonic() < until:
                    drain.pump(self.poll_seconds)
            return status, drain
        finally:
            # Whatever happened, no member of the candidate's session may outlive
            # the run, and no pipe may keep this evaluator waiting.
            self._kill_group(process.pid)
            drain.close()
            try:
                process.wait(timeout=self.term_grace_seconds)
            except subprocess.TimeoutExpired:
                pass

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        """TERM, wait for a grace period, then KILL whatever is left."""
        # Snapshot first: children that started their own session are not in the
        # process group, and they are re-parented once their parent dies.
        escaped = self._descendant_pids(process.pid)
        self._signal_group(process.pid, signal.SIGTERM)
        for pid in escaped:
            self._signal_process(pid, signal.SIGTERM)
        try:
            process.wait(timeout=self.term_grace_seconds)
        except subprocess.TimeoutExpired:
            pass
        # Even after the leader exited, group members may have ignored TERM.
        self._kill_group(process.pid)
        for pid in escaped:
            self._signal_process(pid, signal.SIGKILL)
        try:
            process.wait(timeout=self.term_grace_seconds)
        except subprocess.TimeoutExpired:
            pass

    @staticmethod
    def _signal_group(pgid: int, number: int) -> None:
        try:
            os.killpg(pgid, number)
        except (ProcessLookupError, PermissionError):
            pass

    @staticmethod
    def _signal_process(pid: int, number: int) -> None:
        try:
            os.kill(pid, number)
        except (ProcessLookupError, PermissionError):
            pass

    @classmethod
    def _kill_group(cls, pgid: int) -> None:
        cls._signal_group(pgid, signal.SIGKILL)

    @staticmethod
    def _descendant_pids(root: int) -> list[int]:
        """Best-effort process tree below ``root`` (Linux ``/proc``)."""
        children: dict[int, list[int]] = {}
        try:
            entries = os.listdir("/proc")
        except OSError:
            return []
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as handle:
                    fields = handle.read().rsplit(b")", 1)[1].split()
                children.setdefault(int(fields[1]), []).append(int(entry))
            except (OSError, IndexError, ValueError):
                continue
        found: list[int] = []
        pending = [root]
        while pending:
            for child in children.get(pending.pop(), []):
                found.append(child)
                pending.append(child)
        return found

    def _remove_checkout(self, state: _RunState) -> None:
        """Remove the run directory and Git's record of its worktree."""
        # A candidate may have locked, moved, emptied or deleted its checkout.
        self._git(
            "worktree", "remove", "--force", "--force", str(state.worktree), check=False
        )
        if state.run_directory.is_symlink():
            state.run_directory.unlink()
        else:
            shutil.rmtree(state.run_directory, ignore_errors=True)
        if os.path.lexists(state.run_directory):
            raise WorktreeRunnerError("could not remove the isolated worktree")
        self._remove_admin_entry(state)

    def _remove_admin_entry(self, state: _RunState) -> None:
        """Drop this run's ``.git/worktrees/<id>`` if Git could not.

        Only the entry recorded at creation is touched; ``git worktree prune``
        would also discard records of unrelated missing worktrees.
        """
        admin = state.admin_directory
        if admin is None or not os.path.lexists(admin):
            return
        expected_parent = self._common_git_directory / "worktrees"
        if admin.is_symlink() or admin.resolve().parent != expected_parent:
            raise WorktreeRunnerError("unexpected Git worktree metadata location")
        shutil.rmtree(admin)
        try:
            os.rmdir(expected_parent)  # as Git does once the last entry is gone
        except OSError:
            pass

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

    def _find_common_git_directory(self) -> Path:
        output = self._git("rev-parse", "--git-common-dir").stdout.decode().strip()
        return (self.repository / output).resolve()

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        """Create ``path`` owner-only from the start; refuse a shared one."""
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(path)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise WorktreeRunnerError(f"{path} must be a private directory")

    def _resolve_commit(self, starting_commit: str) -> str:
        if not isinstance(starting_commit, str) or not _REVISION.fullmatch(
            starting_commit
        ):
            raise ValueError("starting_commit must be a plain revision")
        result = self._git(
            "rev-parse", "--verify", "--end-of-options", f"{starting_commit}^{{commit}}"
        )
        commit = result.stdout.decode("ascii").strip()
        if not _OBJECT_ID.fullmatch(commit):
            raise WorktreeRunnerError("git returned an unexpected commit id")
        return commit

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

    def _owned_state(self, run: WorktreeRun) -> _RunState | None:
        """Return the state for ``run`` only if every path it names is ours."""
        state = self._runs.get(run.run_id)
        if (
            state is None
            or run.path != state.worktree
            or run.log_path != state.log_path
        ):
            return None
        return state

    def _try_event(
        self, run: WorktreeRun, event: str, **details: object
    ) -> WorktreeRunnerError | None:
        """Log without letting a failing log skip cleanup; return the failure."""
        try:
            self._write_event(run, event, **details)
        except WorktreeRunnerError as error:
            return error
        return None

    def _write_event(self, run: WorktreeRun, event: str, **details: object) -> None:
        state = self._owned_state(run)
        if state is None:
            raise WorktreeRunnerError("lifecycle log is not owned by this runner")
        record = {
            "event": event,
            "run_id": run.run_id,
            "candidate_id": run.candidate_id,
            "commit": run.commit,
            "timestamp": time.time(),
            **details,
        }
        line = (
            json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
        with self._log_lock:
            self._append_to_log(state, line)

    @staticmethod
    def _append_to_log(state: _RunState, line: bytes) -> None:
        first = state.log_identity is None
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
        # 0600 is applied at creation, so no wider mode ever exists, and a
        # candidate-planted link is never followed (O_NOFOLLOW / O_EXCL).
        flags |= os.O_CREAT | os.O_EXCL if first else 0
        try:
            descriptor = os.open(state.log_path, flags, 0o600)
        except OSError:
            raise WorktreeRunnerError("lifecycle log cannot be opened safely") from None
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or (
                    not first
                    and (
                        (info.st_dev, info.st_ino) != state.log_identity
                        or info.st_size != state.log_size
                    )
                )
            ):
                raise WorktreeRunnerError("lifecycle log failed its integrity check")
            written = 0
            while written < len(line):
                written += os.write(descriptor, line[written:])
            state.log_identity = (info.st_dev, info.st_ino)
            state.log_size = info.st_size + len(line)
        finally:
            os.close(descriptor)

    @staticmethod
    def _git_environment() -> dict[str, str]:
        """Avoid inheriting a caller's Git worktree or index selection."""
        return {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }

    @staticmethod
    def _candidate_environment(state: _RunState) -> dict[str, str]:
        """An explicit allowlist: no credentials, no ``GIT_*``, private HOME."""
        environment = {
            key: os.environ[key]
            for key in _CANDIDATE_ENVIRONMENT_ALLOWLIST
            if key in os.environ
        }
        environment["HOME"] = str(state.home)
        return environment
