"""Run visible and evaluator-owned hidden benchmark checks.

Hidden check definitions never enter a candidate worktree or the durable run log.
The registry is constructed by the evaluator from private configuration after the
candidate has finished.  It deliberately maps opaque task-manifest reference IDs
to commands without exposing that mapping through this module's public results.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_MAX_CAPTURE_BYTES = 64 * 1024
# The only inherited check variables.  Checks execute candidate code, so
# credentials and every ``GIT_*`` selector are dropped; HOME is a private
# temporary directory per check.
_CHECK_ENVIRONMENT_ALLOWLIST = (
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
)


class TestRunnerError(RuntimeError):
    """Raised when evaluator-owned state cannot be used safely."""


@dataclass(frozen=True)
class CheckDefinition:
    """An evaluator check command; hidden definitions stay outside task manifests."""

    id: str
    type: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class CheckExecution:
    """One check's in-memory output and safe, durable execution metadata."""

    id: str
    type: str
    visibility: str
    status: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout: bytes
    stderr: bytes
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool


class HiddenCheckRegistry:
    """Evaluator-owned mapping from opaque reference IDs to hidden commands."""

    def __init__(self, checks: Mapping[str, CheckDefinition]):
        if not checks:
            self._checks: dict[str, CheckDefinition] = {}
            return
        self._checks = {}
        for reference_id, check in checks.items():
            if not isinstance(reference_id, str) or not reference_id:
                raise ValueError("hidden check reference IDs must be non-empty strings")
            _validate_check(check)
            if check.id in {item.id for item in self._checks.values()}:
                raise ValueError("hidden check IDs must be unique")
            self._checks[reference_id] = check

    def resolve(self, reference_id: str) -> CheckDefinition:
        """Resolve one private reference without including it in any error output.

        The lookup miss is handled outside an ``except`` block so no exception
        chain (``__cause__`` or ``__context__``) can carry the reference.
        """
        check = self._checks.get(reference_id)
        if check is None:
            raise KeyError("hidden check reference is unavailable")
        return check


class _OutputCapture:
    """Read check output as it is produced into bounded buffers.

    Only the first ``limit`` bytes per stream are kept; everything else is
    counted and discarded, so memory does not depend on how much a check prints.
    """

    def __init__(self, process: subprocess.Popen[bytes], limit: int):
        self._limit = limit
        self._selector = selectors.DefaultSelector()
        self._streams = {}
        self.data = {"stdout": bytearray(), "stderr": bytearray()}
        self.total = {"stdout": 0, "stderr": 0}
        for name in self.data:
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
            if not chunk:
                self._selector.unregister(key.fileobj)
                continue
            name = key.data
            self.total[name] += len(chunk)
            room = self._limit - len(self.data[name])
            if room > 0:
                self.data[name] += chunk[:room]

    def close(self) -> None:
        """Stop reading even if an escaped process still holds the write end."""
        self._selector.close()
        for stream in self._streams.values():
            stream.close()


class TestRunner:
    """Execute checks in an existing candidate worktree and retain safe evidence.

    ``execution_log`` must be evaluator-owned and outside ``worktree``.  Raw
    stdout/stderr are returned to the trusted caller for immediate diagnosis, but
    durable records retain only byte counts, SHA-256 digests, and truncation flags.
    This avoids creating a credential store while still preserving evidence that
    output was captured.

    Each check runs in its own session with an allowlisted environment.  Output is
    bounded while the check runs, and on timeout the check's process group and the
    processes below it are terminated, then killed after ``term_grace_seconds``.
    A check that daemonizes (double fork plus ``setsid``) cannot be found from
    here; production needs a container or cgroup for that.
    """

    term_grace_seconds = 2.0
    drain_seconds = 1.0
    poll_seconds = 0.05

    def __init__(self, worktree: Path, execution_log: Path):
        self.worktree = Path(worktree).resolve()
        # Resolve the directory but not the file: a symlink planted as the log
        # itself must be refused by the open below, never adopted.
        log = Path(execution_log)
        self.execution_log = log.parent.resolve() / log.name
        if not self.worktree.is_dir():
            raise ValueError("worktree must be an existing directory")
        if self.execution_log.is_relative_to(self.worktree):
            raise ValueError("execution_log must be outside the candidate worktree")
        self._ensure_private_directory(self.execution_log.parent)
        # Create (0600) and vet the log now, before any check has run.
        self._append_to_log(b"")

    def run_visible(
        self, checks: Sequence[CheckDefinition], timeout_seconds: float
    ) -> tuple[CheckExecution, ...]:
        """Run task-manifest checks that may be disclosed to the candidate."""
        return self._run(checks, "visible", timeout_seconds)

    def run_hidden(
        self,
        reference_ids: Sequence[str],
        registry: HiddenCheckRegistry,
        timeout_seconds: float,
    ) -> tuple[CheckExecution, ...]:
        """Resolve and run private checks after candidate execution has ended."""
        checks = tuple(registry.resolve(reference_id) for reference_id in reference_ids)
        return self._run(checks, "hidden", timeout_seconds)

    def _run(
        self,
        checks: Sequence[CheckDefinition],
        visibility: str,
        timeout_seconds: float,
    ) -> tuple[CheckExecution, ...]:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a finite number above zero")
        results = []
        for check in checks:
            _validate_check(check)
            result = self._execute(check, visibility, timeout_seconds)
            self._write_record(result)
            results.append(result)
        return tuple(results)

    def _execute(
        self, check: CheckDefinition, visibility: str, timeout_seconds: float
    ) -> CheckExecution:
        started = time.monotonic()
        status = "error"
        exit_code: int | None = None
        timed_out = False
        stdout = b""
        stderr = b""
        stdout_bytes = stderr_bytes = 0
        try:
            with tempfile.TemporaryDirectory(
                prefix="paw-check-home-", ignore_cleanup_errors=True
            ) as home:
                process = subprocess.Popen(
                    check.command,
                    cwd=self.worktree,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    env=_check_environment(home),
                )
                capture = _OutputCapture(process, _MAX_CAPTURE_BYTES)
                try:
                    timed_out = self._supervise(process, capture, timeout_seconds)
                    stdout, stderr = (
                        bytes(capture.data["stdout"]),
                        bytes(capture.data["stderr"]),
                    )
                    stdout_bytes = capture.total["stdout"]
                    stderr_bytes = capture.total["stderr"]
                finally:
                    capture.close()
                exit_code = process.returncode
                status = (
                    "timed_out"
                    if timed_out
                    else ("passed" if exit_code == 0 else "failed")
                )
        except OSError:
            status = "error"
        return CheckExecution(
            id=check.id,
            type=check.type,
            visibility=visibility,
            status=status,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=round((time.monotonic() - started) * 1000),
            stdout=stdout,
            stderr=stderr,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_truncated=stdout_bytes > _MAX_CAPTURE_BYTES,
            stderr_truncated=stderr_bytes > _MAX_CAPTURE_BYTES,
        )

    def _supervise(
        self,
        process: subprocess.Popen[bytes],
        capture: _OutputCapture,
        timeout_seconds: float,
    ) -> bool:
        """Capture output until exit or the deadline; True if the deadline hit."""
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        exited_at: float | None = None
        try:
            while True:
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    break
                if exited_at is None and process.poll() is not None:
                    exited_at = now
                if exited_at is None:
                    if not capture.open:
                        time.sleep(self.poll_seconds)
                        continue
                elif not capture.open or now - exited_at >= self.drain_seconds:
                    # Done, or a leftover background process keeps the pipes open.
                    break
                if capture.open:
                    capture.pump(min(self.poll_seconds, deadline - now))
            if timed_out:
                self._terminate(process)
                until = time.monotonic() + self.drain_seconds
                while capture.open and time.monotonic() < until:
                    capture.pump(self.poll_seconds)
            return timed_out
        finally:
            # No member of the check's session may outlive it, and no pipe may
            # keep this evaluator waiting.
            _signal_group(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=self.term_grace_seconds)
            except subprocess.TimeoutExpired:
                pass

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        """TERM, wait for a grace period, then KILL whatever is left."""
        # Snapshot first: children that started their own session are not in the
        # process group, and they are re-parented once their parent dies.
        escaped = _descendant_pids(process.pid)
        _signal_group(process.pid, signal.SIGTERM)
        for pid in escaped:
            _signal_process(pid, signal.SIGTERM)
        try:
            process.wait(timeout=self.term_grace_seconds)
        except subprocess.TimeoutExpired:
            pass
        # Even after the leader exited, group members may have ignored TERM.
        _signal_group(process.pid, signal.SIGKILL)
        for pid in escaped:
            _signal_process(pid, signal.SIGKILL)
        try:
            process.wait(timeout=self.term_grace_seconds)
        except subprocess.TimeoutExpired:
            pass

    def _write_record(self, result: CheckExecution) -> None:
        record = {
            "duration_ms": result.duration_ms,
            "exit_code": result.exit_code,
            "id": result.id,
            "status": result.status,
            "stderr": _safe_stream_record(
                result.stderr, result.stderr_bytes, result.stderr_truncated
            ),
            "stdout": _safe_stream_record(
                result.stdout, result.stdout_bytes, result.stdout_truncated
            ),
            "timed_out": result.timed_out,
            "type": result.type,
            "visibility": result.visibility,
        }
        line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        self._append_to_log(line.encode("utf-8"))

    def _append_to_log(self, line: bytes) -> None:
        # 0600 is applied when the file is created, so no wider mode ever
        # exists, and a planted symlink is never followed.
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(self.execution_log, flags, 0o600)
        except OSError:
            raise TestRunnerError("execution log cannot be opened safely") from None
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
            ):
                raise TestRunnerError("execution log is not a private regular file")
            if info.st_mode & 0o077:
                os.fchmod(descriptor, 0o600)  # by descriptor: nothing to follow
            written = 0
            while written < len(line):
                written += os.write(descriptor, line[written:])
        finally:
            os.close(descriptor)

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
            raise TestRunnerError(f"{path} must be a private directory")


def _validate_check(check: CheckDefinition) -> None:
    if not check.id or not check.type:
        raise ValueError("check ID and type must be non-empty")
    if not check.command or not all(
        isinstance(item, str) and item for item in check.command
    ):
        raise ValueError("check command must contain non-empty strings")


def _check_environment(home: str) -> dict[str, str]:
    """An explicit allowlist: no credentials, no ``GIT_*``, private HOME."""
    environment = {
        key: os.environ[key]
        for key in _CHECK_ENVIRONMENT_ALLOWLIST
        if key in os.environ
    }
    environment["HOME"] = home
    return environment


def _safe_stream_record(
    stream: bytes, total_bytes: int, truncated: bool
) -> dict[str, object]:
    return {
        "captured_bytes": total_bytes,
        "sha256": hashlib.sha256(stream).hexdigest(),
        "truncated": truncated,
    }


def _signal_group(pgid: int, number: int) -> None:
    try:
        os.killpg(pgid, number)
    except (ProcessLookupError, PermissionError):
        pass


def _signal_process(pid: int, number: int) -> None:
    try:
        os.kill(pid, number)
    except (ProcessLookupError, PermissionError):
        pass


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
