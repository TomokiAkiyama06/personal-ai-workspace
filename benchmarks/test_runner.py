"""Run visible and evaluator-owned hidden benchmark checks.

Hidden check definitions never enter a candidate worktree or the durable run log.
The registry is constructed by the evaluator from private configuration after the
candidate has finished.  It deliberately maps opaque task-manifest reference IDs
to commands without exposing that mapping through this module's public results.
"""

from __future__ import annotations

import contextlib
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
                # The child's identity (pid plus start time), recorded before
                # anything else can go wrong.
                started_at = _start_time(process.pid)
                leader: _Leader | None = None
                try:
                    leader = _Leader(process)
                    capture = _OutputCapture(process, _MAX_CAPTURE_BYTES)
                except BaseException:
                    # The child is running and nothing supervises it yet: never
                    # leave it behind, but never signal a number that may no
                    # longer be ours either.
                    _stop_unsupervised(process, started_at, leader)
                    raise
                try:
                    timed_out = self._supervise(leader, capture, timeout_seconds)
                    stdout, stderr = (
                        bytes(capture.data["stdout"]),
                        bytes(capture.data["stderr"]),
                    )
                    stdout_bytes = capture.total["stdout"]
                    stderr_bytes = capture.total["stderr"]
                finally:
                    capture.close()
                # If something else reaped the leader (SIGCHLD ignored, another
                # reaper), its exit status is lost: never report that as a pass.
                exit_code = None if leader.status_lost else process.returncode
                if timed_out:
                    status = "timed_out"
                elif leader.status_lost:
                    status = "error"
                else:
                    status = "passed" if exit_code == 0 else "failed"
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
        leader: _Leader,
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
                if exited_at is None and leader.has_exited():
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
                self._terminate(leader, capture)
                until = time.monotonic() + self.drain_seconds
                while capture.open and time.monotonic() < until:
                    capture.pump(self.poll_seconds)
            return timed_out
        finally:
            # No member of the check's session may outlive it, and no pipe may
            # keep this evaluator waiting.
            leader.signal_group(signal.SIGKILL)
            leader.reap(self.term_grace_seconds)

    def _terminate(self, leader: _Leader, capture: _OutputCapture) -> None:
        """TERM, wait for a grace period, then KILL whatever is left.

        The check's output keeps being drained during the grace period: a TERM
        handler that writes more than a pipe holds would otherwise block on the
        full pipe, never finish, and be killed.
        """
        # Snapshot first: children that started their own session are not in the
        # process group, and they are re-parented once their parent dies.  Each is
        # recorded with its start time, so a recycled pid is never mistaken for it.
        leader.refresh(force=True)
        tracked = _descendant_pids(leader.pgid)
        leader.signal_group(signal.SIGTERM)
        for pid, started in tracked.items():
            _signal_identified(pid, started, signal.SIGTERM)
        # The grace period belongs to everything the check started, not just its
        # leader: a leader that exits promptly must not cut short a descendant
        # that is still running its TERM handler.
        deadline = time.monotonic() + self.term_grace_seconds
        while time.monotonic() < deadline:
            if leader.has_exited() and not _anything_alive(leader, tracked):
                break
            if capture.open:
                capture.pump(0.02)
            else:
                time.sleep(0.02)
        leader.signal_group(signal.SIGKILL)
        for pid, started in tracked.items():
            _signal_identified(pid, started, signal.SIGKILL)
        # The leader is reaped by the caller after its last group signal.

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
    command = check.command
    # ``argv[0]`` must name a program; later arguments may be any string, as the
    # task schema allows (for example ``("python3", "-c", "")``).
    if (
        not isinstance(command, Sequence)
        or isinstance(command, (str, bytes))
        or not command
        or not all(isinstance(item, str) and "\0" not in item for item in command)
        or not command[0]
    ):
        raise ValueError("check command needs a non-empty program and string arguments")


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


def _stop_unsupervised(
    process: subprocess.Popen[bytes],
    started_at: int | None,
    leader: _Leader | None = None,
) -> None:
    """Kill and reap a just-started child that no supervisor took over.

    ``started_at`` is the child's start time, recorded right after it was launched.
    The child may already have been reaped by someone else (``SIGCHLD`` ignored, a
    concurrent reaper), and its pid, which is also its process group id, may then
    belong to an unrelated process.  So nothing is signalled or waited for unless
    the child has a recorded start time and is still our own unreaped child with
    it: without a recorded identity nothing can show that the number is still ours,
    so nothing is sent.  A constructed ``leader`` is used when there is one: it
    takes a last look at the group (members forked since it was built), knows the
    members, and re-checks its own identity at every signal.
    """
    # The descriptors go first: under descriptor exhaustion (the usual reason to be
    # here) the identity checks below need a free one to read ``/proc``.
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()
    identified = started_at is not None and (
        leader is None or leader.start == started_at
    )
    if identified and leader is not None:
        leader.look()
        leader.signal_group(signal.SIGKILL)
        leader.reap(5)
    elif identified and _is_unreaped_child(process.pid, started_at):
        # Its own session, so its pid is the group id, and being unreaped reserves it.
        _signal_group(process.pid, signal.SIGKILL)
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=5)
    elif process.returncode is None:
        # Reaped elsewhere, or not provably ours: nothing is sent.  Recording that
        # keeps ``Popen`` from waiting for (or reaping) whatever holds the number
        # now.
        process.returncode = 0


def _is_unreaped_child(pid: int, started_at: int | None) -> bool:
    """Is ``pid`` still our own, unreaped child, and the process first seen at
    ``started_at``?  Looks without reaping it.

    An unreaped child keeps its pid from being reused; once it was reaped (by us or
    by someone else) the number may belong to an unrelated process.  ``waitid``
    alone cannot tell: if the original child was reaped and its pid went to another
    direct child of ours, ``waitid`` describes that one.  So a start time recorded
    at launch is required; without one (no ``/proc``, or the read failed) the child
    is not provably ours and the answer is no.
    """
    if started_at is None:
        return False
    try:
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return False
    except AttributeError:
        pass  # no WNOWAIT: only the start time can tell
    return _start_time(pid) == started_at


class _Leader:
    """A check's leader process, and the safe use of its process group id.

    While the leader is unreaped (running, or a zombie) its pid stays reserved as
    the group id, so signalling the group is safe.  Once it has been reaped
    elsewhere (``SIGCHLD`` ignored, another reaper, or no ``WNOWAIT``) that number
    may already belong to an unrelated group.  Then the group is signalled only
    while a process recorded earlier as its member (same pid and start time) is
    still in it; otherwise nothing is sent.

    Members are recorded while the leader runs, once more when it is first seen as a
    zombie, and once more at the moment it is seen to have vanished: the group id
    stays reserved as long as any member exists, so whatever is in the group then is
    ours unless the id was reused within one polling interval.  Whether the leader is
    still ours is re-checked at every signal, because a reaper can act at any time
    after it was observed.  A listing is only adopted if the leader was still our own
    unreaped child after it, so it was taken while the group id was reserved.  A
    leader without a recorded start time cannot be told from a stranger holding its
    number: its group is never signalled.
    """

    refresh_seconds = 0.2

    def __init__(self, process: subprocess.Popen[bytes]):
        self.process = process
        self.pgid = process.pid
        self.start = _start_time(process.pid)  # None without /proc
        self.released = False  # no longer guaranteed to reserve ``pgid``
        self.status_lost = False  # reaped by someone else: exit status unknown
        self.zombie_seen = False  # its members were recorded while it was a zombie
        self.members: dict[int, int] = {}  # pid -> start time, seen in the group
        self._refreshed = 0.0
        self.refresh(force=True)

    def refresh(self, force: bool = False, *, gone: bool = False) -> None:
        """Record the group's current members.

        Trusted only while the leader is unreaped, so the listing is adopted only if
        the leader still is after it.  ``gone``: the leader is known to be reaped
        (this is the last look).  The group id is then reserved only for as long as
        a member exists, so whatever is in the group is ours, unless a process
        holds the leader's own number: then the number was reused and its group is
        a stranger's.
        """
        now = time.monotonic()
        if self.released or (
            not force and now - self._refreshed < self.refresh_seconds
        ):
            return
        self._refreshed = now
        table = _process_table()
        if gone and self.pgid in table:
            return
        found = {
            pid: started
            for pid, (_, _, pgrp, started) in table.items()
            if pgrp == self.pgid
        }
        if gone or self._still_ours():
            for pid, started in found.items():
                self.members.setdefault(pid, started)

    def _release(self, *, status_lost: bool) -> None:
        """Stop assuming the leader reserves the group id (after a last look)."""
        self.refresh(force=True, gone=True)  # catches a member forked just before
        self.released = True
        self.status_lost = self.status_lost or status_lost

    def look(self) -> None:
        """Take one last, unthrottled look at the group before it is signalled.

        For a caller that never supervised the leader: members forked since the
        leader was built are not recorded yet, and if another reaper has collected
        the leader by now they would be lost.  ``has_exited`` takes the zombie and
        the vanished snapshots; a leader that is still running is listed here.
        """
        self.has_exited()
        self.refresh(force=True)

    def has_exited(self) -> bool:
        """Has the leader exited?  Looks without reaping it."""
        if self.process.returncode is not None:
            self._release(status_lost=False)
            return True
        try:
            if (
                os.waitid(
                    os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
                is not None
            ):
                # A zombie still reserves the group id, so the group can be
                # listed reliably now: a member forked just before the exit is
                # recorded before a concurrent reaper can release the id.
                self.refresh(force=not self.zombie_seen)
                self.zombie_seen = True
                return True
        except ChildProcessError:
            # Someone else reaped it; its exit status is gone with it.
            self._release(status_lost=True)
            return True
        except AttributeError:  # no WNOWAIT: poll() reaps, so nothing reserves it
            if self.process.poll() is not None:
                self._release(status_lost=False)
                return True
        self.refresh()
        return False

    def _still_ours(self) -> bool:
        """Is the process at ``pgid`` still our own, unreaped leader?"""
        return _is_unreaped_child(self.pgid, self.start)

    def owns(self, pid: int, pgrp: int, started: int) -> bool:
        """Is this process, seen in the table, a member of our group?"""
        if pgrp != self.pgid:
            return False
        return not self.released or self.members.get(pid) == started

    def signal_group(self, number: int) -> None:
        if self.start is None:
            return  # no recorded identity: nothing shows the number is still ours
        if not self.released and (
            self.process.returncode is not None or not self._still_ours()
        ):
            # Reaped (by us, or by someone else since it was last observed): the
            # group id is no longer reserved.  Too late for a last look.
            self.released = True
            self.status_lost = self.status_lost or self.process.returncode is None
        if self.released and not any(
            state not in "ZX" and self.owns(pid, pgrp, started)
            for pid, (state, _, pgrp, started) in _process_table().items()
        ):
            return  # the group id may belong to a stranger: never signal it
        _signal_group(self.pgid, number)

    def reap(self, timeout: float) -> None:
        """Collect the leader's exit status, waiting up to ``timeout`` seconds.

        Reaping here rather than through ``Popen.wait`` shows whether something
        else got there first (``ChildProcessError``): ``Popen`` would quietly
        report 0.
        """
        process = self.process
        deadline = time.monotonic() + timeout
        while process.returncode is None:
            try:
                info = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG)
            except ChildProcessError:
                self.released = self.status_lost = True
                process.returncode = 0  # as Popen would; ``status_lost`` overrides it
                return
            except AttributeError:
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
                return
            if info is not None:
                process.returncode = (
                    info.si_status if info.si_code == os.CLD_EXITED else -info.si_status
                )
                self.released = True  # reaped: the id is no longer reserved
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(0.01)


def _signal_group(pgid: int, number: int) -> None:
    try:
        os.killpg(pgid, number)
    except (ProcessLookupError, PermissionError):
        pass


def _signal_identified(pid: int, started: int, number: int) -> None:
    """Signal ``pid`` only if it is still the process first seen at ``started``.

    A process that exited may have its pid reused by an unrelated one, so a pid
    alone is not an identity.  With ``pidfd_open`` (Linux 5.3+) the descriptor is
    opened first and the identity checked afterwards: it then refers to exactly the
    process that was checked, or to none, and the signal cannot reach a newcomer.
    Without it the check is followed by ``kill`` and a window of microseconds
    remains in which the process could exit and its pid be reused.  A pid whose
    identity changed is dropped.
    """
    descriptor: int | None = None
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    except (AttributeError, OSError):  # unsupported: use the plain fallback
        descriptor = None
    try:
        if _start_time(pid) != started:
            return
        if descriptor is not None:
            signal.pidfd_send_signal(descriptor, number)
        else:
            os.kill(pid, number)
    except (ProcessLookupError, PermissionError):
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _parse_stat(raw: bytes) -> tuple[str, int, int, int]:
    """(state, ppid, pgrp, start time in clock ticks) from ``/proc/<pid>/stat``."""
    fields = raw.rsplit(b")", 1)[1].split()
    return fields[0].decode(), int(fields[1]), int(fields[2]), int(fields[19])


def _start_time(pid: int) -> int | None:
    """The start time that, with the pid, identifies a process; None if gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return _parse_stat(handle.read())[3]
    except (OSError, IndexError, ValueError):
        return None


def _process_table() -> dict[int, tuple[str, int, int, int]]:
    """pid -> (state, ppid, pgrp, start time) from Linux ``/proc``; empty elsewhere."""
    table: dict[int, tuple[str, int, int, int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return table
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                table[int(entry)] = _parse_stat(handle.read())
        except (OSError, IndexError, ValueError):
            continue
    return table


def _descendant_pids(root: int) -> dict[int, int]:
    """Best-effort process tree below ``root``: pid -> start time (Linux ``/proc``)."""
    table = _process_table()
    children: dict[int, list[int]] = {}
    for pid, (_, ppid, _, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    found: dict[int, int] = {}
    pending = [root]
    while pending:
        for child in children.get(pending.pop(), []):
            found[child] = table[child][3]
            pending.append(child)
    return found


def _anything_alive(leader: _Leader, tracked: dict[int, int]) -> bool:
    """Is a tracked process, or a member of the leader's group, still running?

    A tracked pid now held by a process with a different start time is a stranger
    and does not count.
    """
    table = _process_table()
    if not table:  # no /proc: probe the process group itself
        try:
            os.killpg(leader.pgid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True
    return any(
        state not in "ZX"
        and (tracked.get(pid) == started or leader.owns(pid, pgrp, started))
        for pid, (state, _, pgrp, started) in table.items()
    )
