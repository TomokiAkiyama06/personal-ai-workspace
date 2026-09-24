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

import contextlib
import ctypes
import json
import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
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
_PR_GET_DUMPABLE = 3
_PR_SET_DUMPABLE = 4
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
# The environment of the runner's own Git commands.  Git may start programs from
# the shared Git directory (hooks, filters) that a candidate has written to, so
# these commands get no credentials either.  HOME stays so that the operator's
# Git configuration (for example ``safe.directory``) still applies.
_GIT_ENVIRONMENT_ALLOWLIST = ("HOME", *_CANDIDATE_ENVIRONMENT_ALLOWLIST)
# The runner never wants a hook: ``git worktree add`` would run ``post-checkout``.
_GIT_COMMAND = ("git", "-c", "core.hooksPath=/dev/null")


class WorktreeRunnerError(RuntimeError):
    """Raised when the runner cannot prepare or execute an isolated run."""


def _make_process_non_dumpable() -> bool:
    """Best effort (Linux): hide this process's ``/proc/<pid>`` files from others.

    A same-UID process can normally read ``/proc/<pid>/environ`` (the environment
    this process started with) and, where ``ptrace_scope`` allows, ``mem``.  A
    non-dumpable process's ``/proc`` entries belong to root instead.  Children
    become dumpable again on ``exec`` and this process stays non-dumpable.  Side
    effects: no core dumps, no debugger attach, and this process's own
    ``/proc/self/environ`` becomes unreadable to itself.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        zero = ctypes.c_ulong(0)
        if libc.prctl(_PR_SET_DUMPABLE, zero, zero, zero, zero) != 0:
            return False
        return libc.prctl(_PR_GET_DUMPABLE, zero, zero, zero, zero) == 0
    except (OSError, AttributeError):
        return False


def _valid_command(command: object) -> bool:
    """``argv[0]`` must name a program; later arguments may be any string."""
    return (
        isinstance(command, Sequence)
        and not isinstance(command, (str, bytes))
        and len(command) > 0
        and all(isinstance(item, str) and "\0" not in item for item in command)
        and bool(command[0])
    )


class _Leader:
    """A candidate's leader process, and the safe use of its process group id.

    While the leader is unreaped (running, or a zombie) its pid stays reserved as
    the group id, so signalling the group is safe.  Once it has been reaped
    elsewhere (``SIGCHLD`` ignored, another reaper, or no ``WNOWAIT``) that number
    may already belong to an unrelated group.  Then the group is signalled only
    while a process recorded earlier as its member (same pid and start time) is
    still in it; otherwise nothing is sent.

    Members are recorded while the leader runs, and once more at the moment it is
    seen to have vanished: the group id stays reserved as long as any member
    exists, so whatever is in the group then is ours unless the id was reused
    within one polling interval.  Whether the leader is still ours is re-checked
    at every signal, because a reaper can act at any time after it was observed.
    """

    refresh_seconds = 0.2

    def __init__(self, process: subprocess.Popen[bytes]):
        self.process = process
        self.pgid = process.pid
        self.start = WorktreeRunner._start_time(process.pid)  # None without /proc
        self.released = False  # no longer guaranteed to reserve ``pgid``
        self.status_lost = False  # reaped by someone else: exit status unknown
        self.members: dict[int, int] = {}  # pid -> start time, seen in the group
        self._refreshed = 0.0
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        """Record the group's current members (only meaningful while unreaped)."""
        now = time.monotonic()
        if self.released or (
            not force and now - self._refreshed < self.refresh_seconds
        ):
            return
        self._refreshed = now
        for pid, (_, _, pgrp, started) in WorktreeRunner._process_table().items():
            if pgrp == self.pgid:
                self.members.setdefault(pid, started)

    def _release(self, *, status_lost: bool) -> None:
        """Stop assuming the leader reserves the group id (after a last look)."""
        self.refresh(force=True)  # catches a member forked just before the exit
        self.released = True
        self.status_lost = self.status_lost or status_lost

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
                return True  # a zombie: still reserves the group id
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
        try:
            os.waitid(os.P_PID, self.pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return False
        except AttributeError:
            pass
        return self.start is None or WorktreeRunner._start_time(self.pgid) == self.start

    def owns(self, pid: int, pgrp: int, started: int) -> bool:
        """Is this process, seen in the table, a member of our group?"""
        if pgrp != self.pgid:
            return False
        return not self.released or self.members.get(pid) == started

    def signal_group(self, number: int) -> None:
        if not self.released and (
            self.process.returncode is not None or not self._still_ours()
        ):
            # Reaped (by us, or by someone else since it was last observed): the
            # group id is no longer reserved.  Too late for a last look.
            self.released = True
            self.status_lost = self.status_lost or self.process.returncode is None
        if self.released and not any(
            state not in "ZX" and self.owns(pid, pgrp, started)
            for pid, (
                state,
                _,
                pgrp,
                started,
            ) in WorktreeRunner._process_table().items()
        ):
            return  # the group id may belong to a stranger: never signal it
        WorktreeRunner._signal_group(self.pgid, number)

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
    # A handle on the run directory and its identity, taken at creation: they
    # follow the directory if a candidate renames or moves it.
    directory_fd: int | None = None
    directory_identity: tuple[int, int] | None = None

    def close_directory(self) -> None:
        if self.directory_fd is not None:
            try:
                os.close(self.directory_fd)
            finally:
                self.directory_fd = None


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
    ``runs_directory``), never below the candidate's directory chain.  Both
    directories must be outside the source repository.

    By default the constructor marks the evaluator process non-dumpable (see
    ``_make_process_non_dumpable``); pass ``harden_process=False`` to opt out.
    ``process_hardened`` reports whether the kernel accepted it.  This narrows,
    but does not close, what a same-UID candidate can read; see the README.
    """

    term_grace_seconds = 2.0
    drain_seconds = 1.0
    poll_seconds = 0.05

    def __init__(
        self,
        repository: Path,
        runs_directory: Path,
        logs_directory: Path | None = None,
        harden_process: bool = True,
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
        self._reject_state_inside_repository()
        self._ensure_private_directory(self.runs_directory)
        self._ensure_private_directory(self.logs_directory)
        self.process_hardened = harden_process and _make_process_non_dumpable()

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
        state.directory_fd = os.open(
            run_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        )
        info = os.fstat(state.directory_fd)
        state.directory_identity = (info.st_dev, info.st_ino)
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
                state.close_directory()
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
        if not _valid_command(command):
            raise ValueError("command needs a non-empty program and string arguments")
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
        result: ExecutionResult | None = None
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
            try:
                leader = _Leader(process)
            except BaseException:
                # The child is running and nothing supervises it yet: never leave
                # it behind a removed worktree. It is our own unreaped child, so
                # its pid is still its process group id.
                self._stop_unsupervised(process)
                raise
            status, drain = self._supervise(run.run_id, leader, timeout_seconds)
            # If something else reaped the leader (SIGCHLD ignored, another
            # reaper) its exit status is lost; ``returncode`` would read 0.
            exit_code = None if leader.status_lost else process.returncode
            result = ExecutionResult(
                status=status,
                exit_code=exit_code,
                duration_ms=round((time.monotonic() - started) * 1000),
                stdout_bytes=drain.counts["stdout"],
                stderr_bytes=drain.counts["stderr"],
            )
            return result
        finally:
            # The durable record carries the same numbers as the returned
            # result (counts and time only, never output).
            terminal_error = self._try_event(
                run,
                status,
                exit_code=exit_code,
                duration_ms=(
                    result.duration_ms
                    if result
                    else round((time.monotonic() - started) * 1000)
                ),
                stdout_bytes=result.stdout_bytes if result else 0,
                stderr_bytes=result.stderr_bytes if result else 0,
            )
            self.cleanup(run, reason=status)
            if terminal_error is not None and sys.exc_info()[0] is None:
                # Cleanup went on regardless. Nothing else is failing, so do not
                # return a result for a run whose outcome never reached the
                # durable log (as ``cleanup`` does for its own log failures).
                raise terminal_error

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
            try:
                self._remove_checkout(state)
            except (WorktreeRunnerError, OSError) as error:
                # Never claim success for a checkout that is still on disk, and
                # never let a raw OSError skip the record of that.
                self._try_event(run, "cleanup_incomplete", reason=reason)
                if isinstance(error, WorktreeRunnerError):
                    raise
                raise WorktreeRunnerError(
                    "could not remove the isolated worktree"
                ) from None
            log_error = log_error or self._try_event(
                run, "cleanup_finished", reason=reason
            )
        finally:
            # Cleanup is terminal: remove in-memory ownership and cancellation
            # state so a long-lived runner does not retain every completed run.
            with self._lock:
                self._runs.pop(run.run_id, None)
                self._cancelled.discard(run.run_id)
            state.close_directory()
        if log_error is not None:
            raise log_error

    def _supervise(
        self, run_id: str, leader: _Leader, timeout_seconds: float
    ) -> tuple[str, _PipeDrain]:
        """Drain output until exit, deadline or cancellation, then contain it."""
        drain: _PipeDrain | None = None
        deadline = time.monotonic() + timeout_seconds
        status = "completed"
        exited_at: float | None = None
        try:
            # Inside the ``try``: if the drain cannot be built (descriptor
            # exhaustion, say) the ``finally`` below still stops the child.
            drain = _PipeDrain(leader.process)
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
                if exited_at is None and leader.has_exited():
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
                self._terminate(leader, drain)
                until = time.monotonic() + self.drain_seconds
                while drain.open and time.monotonic() < until:
                    drain.pump(self.poll_seconds)
            return status, drain
        finally:
            # Whatever happened, no member of the candidate's session may outlive
            # the run, and no pipe may keep this evaluator waiting.
            leader.signal_group(signal.SIGKILL)
            if drain is not None:
                drain.close()
            else:
                for stream in (leader.process.stdout, leader.process.stderr):
                    if stream is not None:
                        with contextlib.suppress(OSError):
                            stream.close()
            leader.reap(self.term_grace_seconds)

    @staticmethod
    def _stop_unsupervised(process: subprocess.Popen[bytes]) -> None:
        """Kill and reap a just-started child that no supervisor took over."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()

    def _terminate(self, leader: _Leader, drain: _PipeDrain) -> None:
        """TERM, wait for a grace period, then KILL whatever is left.

        The candidate's output keeps being drained during the grace period: a TERM
        handler that writes more than a pipe holds would otherwise block on the
        full pipe, never finish, and be killed.
        """
        # Snapshot first: children that started their own session are not in the
        # process group, and they are re-parented once their parent dies.  Each is
        # recorded with its start time, so a recycled pid is never mistaken for it.
        leader.refresh(force=True)
        tracked = self._descendant_pids(leader.pgid)
        leader.signal_group(signal.SIGTERM)
        for pid, started in tracked.items():
            self._signal_identified(pid, started, signal.SIGTERM)
        # The grace period belongs to everything the candidate started, not just
        # its leader: a leader that exits promptly must not cut short a
        # descendant that is still running its TERM handler.
        deadline = time.monotonic() + self.term_grace_seconds
        while time.monotonic() < deadline:
            if leader.has_exited() and not self._anything_alive(leader, tracked):
                break
            if drain.open:
                drain.pump(0.02)
            else:
                time.sleep(0.02)
        leader.signal_group(signal.SIGKILL)
        for pid, started in tracked.items():
            self._signal_identified(pid, started, signal.SIGKILL)
        # The leader is reaped by the caller after its last group signal.

    @staticmethod
    def _signal_group(pgid: int, number: int) -> None:
        try:
            os.killpg(pgid, number)
        except (ProcessLookupError, PermissionError):
            pass

    @classmethod
    def _signal_identified(cls, pid: int, started: int, number: int) -> None:
        """Signal ``pid`` only if it is still the process first seen at ``started``.

        A process that exited may have its pid reused by an unrelated one, so a
        pid alone is not an identity.  With ``pidfd_open`` (Linux 5.3+) the
        descriptor is opened first and the identity checked afterwards: it then
        refers to exactly the process that was checked, or to none, and the
        signal cannot reach a newcomer.  Without it the check is followed by
        ``kill`` and a window of microseconds remains in which the process could
        exit and its pid be reused.  A pid whose identity changed is dropped.
        """
        descriptor: int | None = None
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        except (AttributeError, OSError):  # unsupported: use the plain fallback
            descriptor = None
        try:
            if cls._start_time(pid) != started:
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

    @classmethod
    def _kill_group(cls, pgid: int) -> None:
        cls._signal_group(pgid, signal.SIGKILL)

    @staticmethod
    def _parse_stat(raw: bytes) -> tuple[str, int, int, int]:
        """(state, ppid, pgrp, start time in clock ticks) from ``/proc/<pid>/stat``."""
        fields = raw.rsplit(b")", 1)[1].split()
        return fields[0].decode(), int(fields[1]), int(fields[2]), int(fields[19])

    @classmethod
    def _start_time(cls, pid: int) -> int | None:
        """The start time that, with the pid, identifies a process; None if gone."""
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                return cls._parse_stat(handle.read())[3]
        except (OSError, IndexError, ValueError):
            return None

    @classmethod
    def _process_table(cls) -> dict[int, tuple[str, int, int, int]]:
        """pid -> (state, ppid, pgrp, start time) from Linux ``/proc``."""
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
                    table[int(entry)] = cls._parse_stat(handle.read())
            except (OSError, IndexError, ValueError):
                continue
        return table

    @classmethod
    def _descendant_pids(cls, root: int) -> dict[int, int]:
        """Best-effort process tree below ``root``: pid -> start time (``/proc``)."""
        table = cls._process_table()
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

    @classmethod
    def _anything_alive(cls, leader: _Leader, tracked: dict[int, int]) -> bool:
        """Is a tracked process, or a member of the leader's group, still running?

        A tracked pid now held by a process with a different start time is a
        stranger and does not count.
        """
        table = cls._process_table()
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

    def _remove_checkout(self, state: _RunState) -> None:
        """Remove the run directory, wherever it now is, and Git's record of it.

        Removes everything it can, then raises ``WorktreeRunnerError`` if any part
        of the run directory could not be found or removed.
        """
        info = self._lstat(state.run_directory)
        still_in_place = (
            info is not None and (info.st_dev, info.st_ino) == state.directory_identity
        )
        where, moved = self._locate_run_directory(state)
        # A candidate may have locked, moved, emptied or deleted its checkout.
        self._git(
            "worktree", "remove", "--force", "--force", str(state.worktree), check=False
        )
        complete = self._remove_tree(state.run_directory)
        if where == "unknown":
            # A directory that may have moved cannot be followed: only a
            # directory still in place is known to be gone.
            complete = complete and still_in_place
        elif where == "moved":
            complete = self._remove_moved_directory(state, moved) and complete
        try:
            self._remove_admin_entry(state)
        finally:
            if not complete:
                raise WorktreeRunnerError("could not remove the isolated worktree")

    @classmethod
    def _locate_run_directory(cls, state: _RunState) -> tuple[str, Path | None]:
        """Where the run directory is: ``("moved", path)``, ``("in place", None)``,
        ``("deleted", None)`` or ``("unknown", None)``.

        The descriptor opened at creation follows the directory if a candidate
        renames or moves it, and Linux reports its current path in ``/proc``.  The
        text is not trusted: a live directory whose name ends in `` (deleted)``
        reads the same as a deleted one, so the path is checked by identity.
        """
        try:
            target = os.readlink(f"/proc/self/fd/{state.directory_fd}")
        except (OSError, TypeError):
            return "unknown", None
        info = cls._lstat(Path(target))
        if info is not None and (info.st_dev, info.st_ino) == state.directory_identity:
            if Path(target) == state.run_directory:
                return "in place", None
            return "moved", Path(target)
        if target.endswith(" (deleted)"):
            return "deleted", None  # nothing at that name is our directory
        return "unknown", None  # the text and the disk disagree

    @staticmethod
    def _still_present(path: Path) -> bool:
        """False only when ``path`` is known to be gone (not merely unreadable)."""
        try:
            os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            return True
        return True

    @staticmethod
    def _lstat(path: Path) -> os.stat_result | None:
        try:
            return os.lstat(path)
        except OSError:
            return None

    @staticmethod
    def _remove_tree(path: Path) -> bool:
        """Remove ``path`` without following a symlink; True if nothing remains."""
        try:
            if path.is_symlink():
                path.unlink()
            else:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            return False
        return not WorktreeRunner._still_present(path)

    def _remove_moved_directory(self, state: _RunState, moved: Path) -> bool:
        """Remove the run directory a candidate moved to ``moved`` (identity checked)."""
        info = self._lstat(moved)
        if (
            info is None
            or not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != state.directory_identity
        ):
            return info is None  # gone already, or no longer the directory we opened
        shutil.rmtree(moved, ignore_errors=True)
        return not self._still_present(moved)

    def _remove_admin_entry(self, state: _RunState) -> None:
        """Drop this run's ``.git/worktrees/<id>`` if Git could not.

        Only the entry recorded at creation is touched; ``git worktree prune``
        would also discard records of unrelated missing worktrees.
        """
        admin = state.admin_directory
        if admin is None:
            return
        try:
            os.lstat(admin)
        except (FileNotFoundError, NotADirectoryError):
            return  # really gone
        except OSError:
            # ``os.path.lexists`` would say "absent" for a permission error: an
            # entry that cannot be looked at is not an entry that is gone.
            raise WorktreeRunnerError(
                "could not inspect the Git worktree metadata"
            ) from None
        expected_parent = self._common_git_directory / "worktrees"
        try:
            if admin.is_symlink() or admin.resolve().parent != expected_parent:
                raise WorktreeRunnerError("unexpected Git worktree metadata location")
            if admin.is_dir():
                shutil.rmtree(admin)
            else:
                admin.unlink()  # a candidate replaced the entry by a plain file
        except OSError:
            raise WorktreeRunnerError(
                "could not remove the Git worktree metadata"
            ) from None
        try:
            os.rmdir(expected_parent)  # as Git does once the last entry is gone
        except OSError:
            pass

    def _assert_repository(self) -> None:
        result = subprocess.run(
            [
                *_GIT_COMMAND,
                "-C",
                str(self.repository),
                "rev-parse",
                "--is-inside-work-tree",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=self._git_environment(),
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise WorktreeRunnerError("repository must be a Git working tree")

    def _reject_state_inside_repository(self) -> None:
        """Runs and logs must not dirty the source repository they check out."""
        toplevel = os.fsdecode(self._git("rev-parse", "--show-toplevel").stdout)
        roots = {Path(toplevel.strip()).resolve(), self._common_git_directory}
        if self._common_git_directory.name == ".git":
            roots.add(self._common_git_directory.parent)  # main worktree
        for name, directory in (
            ("runs_directory", self.runs_directory),
            ("logs_directory", self.logs_directory),
        ):
            if any(directory.is_relative_to(root) for root in roots):
                raise ValueError(f"{name} must be outside the source repository")

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
            [*_GIT_COMMAND, "-C", str(self.repository), *arguments],
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
            try:
                info = os.fstat(descriptor)
            except OSError:
                raise WorktreeRunnerError("lifecycle log cannot be written") from None
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
            try:
                while written < len(line):
                    written += os.write(descriptor, line[written:])
            except OSError:
                # A full disk or an I/O error must not escape as a raw OSError:
                # callers (cleanup above all) only expect WorktreeRunnerError.
                raise WorktreeRunnerError("lifecycle log cannot be written") from None
            state.log_identity = (info.st_dev, info.st_ino)
            state.log_size = info.st_size + len(line)
        except BaseException:
            # Keep the error that is already on its way out: a close failure on
            # top of it would only hide it.
            with contextlib.suppress(OSError):
                os.close(descriptor)
            raise
        try:
            os.close(descriptor)
        except OSError:
            # close(2) can report a delayed write error (EIO, ENOSPC); the
            # descriptor is released either way.
            raise WorktreeRunnerError("lifecycle log cannot be written") from None

    @staticmethod
    def _git_environment() -> dict[str, str]:
        """An explicit allowlist: no credentials, no ``GIT_*`` selection."""
        return {
            key: os.environ[key]
            for key in _GIT_ENVIRONMENT_ALLOWLIST
            if key in os.environ
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
