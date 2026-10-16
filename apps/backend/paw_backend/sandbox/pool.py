"""The running sandboxes of this process: one per node and repository (0083, 6).

A sandbox is started the first time a node's call needs it and kept for the
node's later calls (the benchmark kept one container per task too), keyed by
:class:`SandboxKey`: the task, its run (a Retry / Restart gets new sandboxes),
the node attempt (its agent identity: two nodes, parallel or not, never share a
sandbox, its ``/tmp`` or its processes), the repository and whether it is the
writable one. A call whose :class:`~.spec.SandboxSpec` differs from the running
one's (the scope changed: a Working Set change, a new excluded path) stops it
and starts a new one: a sandbox never outlives the scope it was built for.

It is stopped:

* when a command does not end in time (``timeout_s``) or the call is cancelled
  (Decision 0083, 8: the wrapper's ``stop`` is SIGTERM, five seconds, SIGKILL;
  the next call starts a new sandbox). The stop runs shielded and bounded
  (:data:`STOP_TIMEOUT_SECONDS`, below ``HybridRuntime``'s ten seconds of
  grace), and the cancellation is raised once it ended;
* by :meth:`SandboxPool.release` (the runtime's node ended), by
  :meth:`reap_idle` (unused for ``idle_seconds``), by :meth:`close`;
* and in any case by the container's own lifetime (the wrapper's
  ``--sandbox-lifetime``), should this process end without stopping it.
"""

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from paw_backend.repositories.paths import LinuxAccount
from paw_backend.sandbox.runner import SandboxRunner
from paw_backend.sandbox.shim import ShimHandler, ShimServer
from paw_backend.sandbox.spec import (
    COMMAND_TIMEOUT_SECONDS,
    SandboxCommand,
    SandboxExecResult,
    SandboxGoneError,
    SandboxSpec,
    SandboxUnavailableError,
)
from paw_backend.tasks import TaskRun

logger = logging.getLogger(__name__)

#: How long a stop may take before the pool gives up waiting (the container's
#: lifetime still ends it).
STOP_TIMEOUT_SECONDS = 8.0
#: How long a command's call may take beyond the command's own limit (starting
#: the process over SSH, the wrapper's checks) before the pool stops it.
EXEC_MARGIN_SECONDS = 30.0
DEFAULT_IDLE_SECONDS = 900.0


@dataclass(frozen=True, slots=True)
class SandboxKey:
    """Whose sandbox: the task, its run, the node attempt (its agent identity,
    ``orchestrator.scope.agent_id_of``: one per node and attempt), the
    repository and whether it is the writable one."""

    task_id: uuid.UUID
    run: TaskRun
    agent_id: uuid.UUID
    repo_id: uuid.UUID
    writable: bool


@dataclass(slots=True)
class _Sandbox:
    sandbox_id: uuid.UUID
    spec: SandboxSpec
    account: LinuxAccount
    shim: ShimServer | None
    used_at: float


class SandboxPool:
    """Starts, reuses and stops the sandboxes (see the module).

    ``shim_base`` is the directory the git shims' sockets are made in
    (``None``: no sandbox gets a shim, and its ``git`` answers that it is not
    available); ``shim_group`` the group that may reach them (see ``shim.py``).
    """

    def __init__(
        self,
        runner: SandboxRunner,
        *,
        shim_base: str | None = None,
        shim_group: int | None = None,
        command_timeout_s: float = COMMAND_TIMEOUT_SECONDS,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        oom_status: bool = False,
    ) -> None:
        for name in ("start", "exec", "stop"):
            if not callable(getattr(runner, name, None)):
                raise TypeError("runner must be a SandboxRunner")
        if shim_base is not None and not os.path.isabs(shim_base):
            raise ValueError("shim_base must be an absolute path")
        if not 0 < command_timeout_s <= 3600 or not 0 < idle_seconds:
            raise ValueError("a timeout is out of range")
        self._runner = runner
        self._shim_base = shim_base
        self._shim_group = shim_group
        self._command_timeout = float(command_timeout_s)
        self._idle = float(idle_seconds)
        self._clock = clock
        # Ask the wrapper whether a command's container counted an OOM kill
        # (``paw-sandbox/v2``, ``PAW_SANDBOX_OOM_STATUS``; Decision 0092, 5).
        self._oom_status = bool(oom_status)
        self._sandboxes: dict[SandboxKey, _Sandbox] = {}
        # The lock of each key and how many hold or wait for it.
        self._locks: dict[SandboxKey, list] = {}

    @property
    def shims(self) -> bool:
        return self._shim_base is not None

    @contextlib.asynccontextmanager
    async def _holding(self, key: SandboxKey) -> AsyncIterator[None]:
        """Hold the lock of ``key``. A lock is forgotten once nobody holds or
        waits for it and its sandbox is gone (each node attempt has keys of its
        own: kept, they would pile up for as long as the process runs)."""
        entry = self._locks.get(key)
        if entry is None:
            entry = self._locks[key] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0 and key not in self._sandboxes:
                self._locks.pop(key, None)

    async def run(
        self,
        key: SandboxKey,
        account: LinuxAccount,
        spec: SandboxSpec,
        command: SandboxCommand,
        *,
        shim: ShimHandler | None = None,
    ) -> SandboxExecResult:
        """Run ``command`` in the sandbox of ``key`` (started with ``spec`` when
        none runs, or when the running one has another spec or account).
        ``shim`` answers the sandbox's ``git`` while the command runs."""
        async with self._holding(key):
            for attempt in (1, 2):
                sandbox = await self._ensure(key, account, spec)
                if sandbox.shim is not None and shim is not None:
                    sandbox.shim.activate(shim)
                try:
                    return await self._exec(key, sandbox, command)
                except SandboxGoneError:
                    # Not running (its lifetime ended, the host restarted): the
                    # command did not run. Start a new one and try once more.
                    await self._discard(key)
                    if attempt == 2:
                        raise SandboxUnavailableError("not_running") from None
                finally:
                    if sandbox.shim is not None:
                        sandbox.shim.deactivate()
                    sandbox.used_at = self._clock()
            raise AssertionError("unreachable")  # pragma: no cover

    async def _exec(
        self, key: SandboxKey, sandbox: _Sandbox, command: SandboxCommand
    ) -> SandboxExecResult:
        try:
            # One command at a time per sandbox (the key's lock in ``run``):
            # an OOM kill the wrapper sees while it runs is this command's.
            options = {"oom_status": True} if self._oom_status else {}
            return await self._runner.exec(
                sandbox.account,
                sandbox.sandbox_id,
                command,
                timeout_s=self._command_timeout + EXEC_MARGIN_SECONDS,
                **options,
            )
        except SandboxGoneError:
            raise
        except BaseException:
            # A timeout, a cancellation, a call that did not complete: whatever
            # the command left running is stopped with the whole sandbox.
            await self._stop_shielded(key)
            raise

    async def _ensure(
        self, key: SandboxKey, account: LinuxAccount, spec: SandboxSpec
    ) -> _Sandbox:
        sandbox = self._sandboxes.get(key)
        if sandbox is not None and (sandbox.spec != spec or sandbox.account != account):
            await self._discard(key)
            sandbox = None
        if sandbox is not None:
            return sandbox
        sandbox_id = uuid.uuid4()
        shim = None
        if spec.shim:
            if self._shim_base is None:
                raise SandboxUnavailableError("no_shim")
            shim = ShimServer(
                os.path.join(self._shim_base, sandbox_id.hex), group=self._shim_group
            )
            await shim.open()
        sandbox = _Sandbox(sandbox_id, spec, account, shim, self._clock())
        try:
            await self._runner.start(account, sandbox_id, spec)
        except BaseException:
            await self._close_quietly(sandbox, stop=True)
            raise
        self._sandboxes[key] = sandbox
        return sandbox

    async def _discard(self, key: SandboxKey) -> None:
        sandbox = self._sandboxes.pop(key, None)
        if sandbox is not None:
            await self._close_quietly(sandbox, stop=True)

    async def _stop_shielded(self, key: SandboxKey) -> None:
        """Stop the sandbox of ``key`` even while being cancelled; a further
        cancellation is held back until the stop ended (and then raised)."""
        stopping = asyncio.ensure_future(self._discard(key))
        cancelled: asyncio.CancelledError | None = None
        while not stopping.done():
            try:
                await asyncio.wait({stopping})
            except asyncio.CancelledError as error:
                cancelled = error
        if cancelled is not None:
            raise cancelled

    async def _close_quietly(self, sandbox: _Sandbox, *, stop: bool) -> None:
        if stop:
            try:
                async with asyncio.timeout(STOP_TIMEOUT_SECONDS):
                    await self._runner.stop(sandbox.account, sandbox.sandbox_id)
            except Exception as error:
                # Its lifetime ends it; the next call starts another one.
                logger.warning(
                    "A sandbox could not be stopped (%s)", type(error).__name__
                )
        if sandbox.shim is not None:
            try:
                await sandbox.shim.close()
            except OSError as error:
                logger.warning("A shim could not be removed (%s)", type(error).__name__)

    async def release(
        self,
        task_id: uuid.UUID,
        run: TaskRun | None = None,
        *,
        agent_id: uuid.UUID | None = None,
    ) -> None:
        """Stop every sandbox of ``task_id`` (of ``run`` only, and of the node
        attempt ``agent_id`` only, when given: a node that ended)."""
        for key in [
            k
            for k in self._sandboxes
            if k.task_id == task_id
            and (run is None or k.run == run)
            and (agent_id is None or k.agent_id == agent_id)
        ]:
            async with self._holding(key):
                await self._discard(key)

    async def reap_idle(self) -> int:
        """Stop the sandboxes unused for ``idle_seconds``; how many."""
        now = self._clock()
        stopped = 0
        for key in [
            k for k, s in self._sandboxes.items() if now - s.used_at >= self._idle
        ]:
            entry = self._locks.get(key)
            if entry is not None and entry[1]:
                continue  # in use right now
            async with self._holding(key):
                sandbox = self._sandboxes.get(key)
                if sandbox is not None and now - sandbox.used_at >= self._idle:
                    await self._discard(key)
                    stopped += 1
        return stopped

    async def close(self) -> None:
        for key in list(self._sandboxes):
            async with self._holding(key):
                await self._discard(key)

    def running(self) -> int:
        return len(self._sandboxes)

    def tracked_keys(self) -> int:
        """How many keys have a lock (for the tests)."""
        return len(self._locks)
