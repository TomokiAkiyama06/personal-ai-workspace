"""Kaggle / Full GPU Mode (PAW-037): the GPU for one exclusive job, and back.

``REQUIREMENTS.md`` ("Kaggle / Full GPU Mode", FIXED, and "GPU運用": Kaggle /
research computing comes first; the local AI is a service of the spare GPU) lists
the steps; Decision 0037 (Approved) section 7 gave the scheduler the Exclusive
class that empties the GPU; ``docs/decisions/0055-kaggle-full-gpu-mode.md``
(Proposed) records what the requirements leave open. :class:`FullGpuMode` runs
the steps around that Exclusive lease, one per backend process (the scheduler is
one per process, Decision 0037's 1):

Start (:meth:`FullGpuMode.start`, Owner / Admin only)
    1. New local GPU work stops: the scheduler's Exclusive request refuses every
       new local GPU admission from the moment it is made (``exclusive_mode``;
       work that may use the cloud goes there, CPU copies keep serving).
    2. / 3. The Agent Tasks whose work holds or waits for a local GPU lease are
       **held**: put in ``waiting`` for a resource by the policy actor, with a
       fixed reason (:data:`HOLD_REASON`). Waiting quiesces a task like a pause:
       no new node starts and the running ones finish (the orchestrator's graceful
       stop), so the running work drains and nothing is cut short. A queued task
       stays queued (the lifecycle has no queued -> waiting): it is held when its
       first local GPU request meets Full GPU Mode. The sweep repeats while the
       mode lasts.
    4. - 6. The scheduler waits for the local GPU leases to end (``drain``
       seconds). With ``preempt`` the work still running then is asked to stop
       (its lease is ``revoked``: the local call is cancelled, nothing is
       killed). Then it unloads the Memory Worker, moves Embedding / Reranker to
       the CPU (or unloads them) and unloads the main LLM.
    7. It confirms from the read-only probe that no process of the workspace
       holds GPU memory and that the requested VRAM is free.
    8. The Exclusive lease is held here: the GPU is the job's (``ON``).
    Any failure puts the scheduler back to normal and the held tasks resume.

End (:meth:`FullGpuMode.end`, Owner / Admin only)
    The lease is released; the scheduler's ``refresh()`` loads the main LLM
    again, then the support models. Once the main LLM is back on the GPU,
    :meth:`tick` resumes the held tasks (``unblock`` and a new queue entry): the
    task keeps its state, branch, worktree, DAG and results throughout.

Raw conversations and Pending Observations are written without the GPU
(PostgreSQL); a Memory Worker job finds no lease and is deferred (Decision 0018),
until the Memory Worker is back.

The held tasks are found again from the task history (the ``wait`` event of the
policy actor with :data:`HOLD_REASON`), not from memory: tasks held by a process
that stopped are resumed by the next one (:meth:`tick`), once its main LLM is
on the GPU. Full GPU Mode itself does not survive a restart (the scheduler's
state is in the process).

GPU safety: nothing here reads or touches the GPU but through the scheduler (its
read-only probe and its injected ``ModelControl``); no process is signalled.
"""

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from paw_backend.authz import Authorizer, Capability, Principal, Resource
from paw_backend.compute.domain import (
    DeploymentState,
    ExclusiveFailure,
    ModelRole,
    ResourceClass,
)
from paw_backend.compute.errors import (
    ExclusiveUnavailableError,
    FullGpuModeStateError,
    FullGpuPermissionDeniedError,
    InvalidComputeArgumentError,
)
from paw_backend.compute.limits import (
    DEFAULT_FULL_GPU_DRAIN_SECONDS,
    DEFAULT_FULL_GPU_PREEMPT_SECONDS,
    DEFAULT_FULL_GPU_RELOAD_SECONDS,
    DEFAULT_REFRESH_SECONDS,
)
from paw_backend.compute.scheduler import (
    ComputeLease,
    ComputeRequest,
    ComputeScheduler,
    check_seconds,
)
from paw_backend.orchestrator.config import Clock, SystemClock
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger("paw_backend.compute")

CAPABILITY = Capability.ADMIN_COMPUTE_FULL_GPU
# The reason of the ``wait`` event that holds a task, and of the ``unblock`` that
# resumes it: how a held task is told apart from one waiting for anything else.
HOLD_REASON = "Kaggle / Full GPU Mode"
RESUME_REASON = "Kaggle / Full GPU Mode ended"


class FullGpuState(StrEnum):
    OFF = "off"
    STARTING = "starting"  # holding tasks, draining, unloading, confirming
    ON = "on"  # the Exclusive lease is held: the GPU is the job's
    # The lease is released: the held tasks resume once the main LLM is back.
    RESUMING = "resuming"


@dataclass(frozen=True, slots=True)
class ResumeReport:
    """What one :meth:`TaskHolds.resume_held` did: ``resumed`` tasks were
    unblocked, ``remaining`` held tasks could not be now (tried again later)."""

    resumed: int
    remaining: int


class TaskHolds(Protocol):
    """Holding and resuming Agent Tasks for Full GPU Mode (``holds.py`` is the
    PostgreSQL one)."""

    async def hold(self, task_id: uuid.UUID) -> bool:
        """Put a running task in ``waiting`` (resource) with :data:`HOLD_REASON`.
        ``False``: the task is not running (it waits, is paused, evaluates or has
        ended) and is left alone. Raises when it could not be decided now."""
        ...

    async def resume_held(self) -> ResumeReport:
        """Unblock every task that is still held and put it back in the queue."""
        ...


@dataclass(frozen=True, slots=True)
class FullGpuStatus:
    """A snapshot for monitoring (no task id, no pid)."""

    state: FullGpuState
    # The tasks this process held since the mode was last started.
    held_tasks: int
    # The local GPU work was asked to stop after the drain time.
    preempted: bool
    # How long the GPU has been the job's (``ON`` only).
    on_seconds: float | None
    # Why the last start failed (``None``: it did not).
    last_failure: ExclusiveFailure | None
    # The main LLM has not come back within the reload time after the end:
    # the held tasks keep waiting, a human should look.
    needs_human: bool


class FullGpuMode:
    """See the module. One per backend process, with the process's scheduler."""

    def __init__(
        self,
        scheduler: ComputeScheduler,
        holds: object,
        authorizer: Authorizer,
        *,
        drain_seconds: float = DEFAULT_FULL_GPU_DRAIN_SECONDS,
        preempt_seconds: float = DEFAULT_FULL_GPU_PREEMPT_SECONDS,
        reload_seconds: float = DEFAULT_FULL_GPU_RELOAD_SECONDS,
        sweep_seconds: float = DEFAULT_REFRESH_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(scheduler, ComputeScheduler):
            raise TypeError("scheduler must be a ComputeScheduler")
        require_async_method(holds, "hold", 1)
        require_async_method(holds, "resume_held", 0)
        if not isinstance(authorizer, Authorizer):
            raise TypeError("authorizer must be an Authorizer")
        clock = clock or SystemClock()
        if not callable(getattr(clock, "monotonic", None)):
            raise TypeError("clock must have monotonic()")
        require_async_method(clock, "sleep", 1)
        self._scheduler = scheduler
        self._holds = holds
        self._authorizer = authorizer
        self._drain = check_seconds("drain_seconds", drain_seconds)
        self._preempt_wait = check_seconds("preempt_seconds", preempt_seconds)
        self._reload = check_seconds("reload_seconds", reload_seconds)
        self._sweep = check_seconds("sweep_seconds", sweep_seconds)
        if self._sweep == 0:
            raise InvalidComputeArgumentError("sweep_seconds")
        self._clock = clock
        # Tasks held by an earlier process are looked for at the first tick.
        self._state = FullGpuState.RESUMING
        self._lease: ComputeLease | None = None
        self._held: set[uuid.UUID] = set()
        self._preempted = False
        self._failure: ExclusiveFailure | None = None
        self._ended_at: float | None = None
        self._needs_human = False
        self._lock = asyncio.Lock()  # one start / end / tick at a time

    # -- public ---------------------------------------------------------------

    def status(self) -> FullGpuStatus:
        on = None
        if self._state is FullGpuState.ON and self._lease is not None:
            on = self._clock.monotonic() - self._lease.granted_at
        return FullGpuStatus(
            state=self._state,
            held_tasks=len(self._held),
            preempted=self._preempted,
            on_seconds=on,
            last_failure=self._failure,
            needs_human=self._needs_human,
        )

    async def start(
        self,
        principal: Principal | None,
        *,
        vram_bytes: int | None = None,
        drain_seconds: float | None = None,
        preempt: bool = False,
    ) -> FullGpuStatus:
        """Give the GPU to an exclusive job (see the module); returns once the
        GPU is the job's. ``vram_bytes``: what the job needs free (default:
        everything but the safety headroom and the other workloads' memory).
        ``drain_seconds``: how long running local GPU work may take to end
        (default: the configured one). ``preempt``: ask the work still running
        then to stop, instead of giving up.

        Raises :class:`FullGpuPermissionDeniedError` (not Owner / Admin; the
        decision is audited), :class:`FullGpuModeStateError` (already started)
        and :class:`ExclusiveUnavailableError` (the GPU could not be emptied;
        everything is back to normal and the held tasks resume)."""
        if vram_bytes is not None and (
            isinstance(vram_bytes, bool) or not isinstance(vram_bytes, int)
        ):
            raise InvalidComputeArgumentError("vram_bytes")
        drain = (
            self._drain
            if drain_seconds is None
            else check_seconds("drain_seconds", drain_seconds)
        )
        if not isinstance(preempt, bool):
            raise InvalidComputeArgumentError("preempt")
        await self._authorize(principal)
        # Refused at once, not after the start in progress (it takes minutes).
        if self._state in (FullGpuState.STARTING, FullGpuState.ON):
            raise FullGpuModeStateError(self._state.value)
        async with self._lock:
            if self._state in (FullGpuState.STARTING, FullGpuState.ON):
                raise FullGpuModeStateError(self._state.value)
            if vram_bytes is None:
                vram_bytes = self._whole_gpu()
            if vram_bytes is None:
                self._failure = ExclusiveFailure.PROBE_UNAVAILABLE
                raise ExclusiveUnavailableError(ExclusiveFailure.PROBE_UNAVAILABLE)
            request = ComputeRequest(ResourceClass.EXCLUSIVE, vram_bytes=vram_bytes)
            if self._state is FullGpuState.OFF:
                self._held.clear()
            self._state = FullGpuState.STARTING
            self._preempted = False
            self._failure = None
            self._needs_human = False
            logger.warning("Full GPU Mode requested: local GPU work is held")
            try:
                lease = await self._acquire(request, drain, preempt)
            except BaseException as error:
                if isinstance(error, ExclusiveUnavailableError):
                    self._failure = error.failure
                    logger.warning(
                        "Full GPU Mode could not start (%s): the held tasks resume",
                        error.failure.value,
                    )
                self._state = FullGpuState.RESUMING
                self._ended_at = self._clock.monotonic()
                raise
            self._lease = lease
            self._state = FullGpuState.ON
            logger.warning("Full GPU Mode started: the GPU is the exclusive job's")
            return self.status()

    async def end(self, principal: Principal | None) -> FullGpuStatus:
        """Give the GPU back: the models are loaded again and the held tasks
        resume (:meth:`tick`). Raises :class:`FullGpuPermissionDeniedError` and
        :class:`FullGpuModeStateError` (Full GPU Mode is not on)."""
        await self._authorize(principal)
        if self._state is not FullGpuState.ON:
            raise FullGpuModeStateError(self._state.value)
        async with self._lock:
            if self._state is not FullGpuState.ON or self._lease is None:
                raise FullGpuModeStateError(self._state.value)
            # A last sweep: a task refused since the last tick is held before
            # the scheduler forgets it with the lease (Codex review #161).
            await self._hold_gpu_tasks()
            lease, self._lease = self._lease, None
            await lease.release()
            self._state = FullGpuState.RESUMING
            self._ended_at = self._clock.monotonic()
            logger.warning("Full GPU Mode ended: the models are loaded again")
            return self.status()

    async def tick(self) -> FullGpuStatus:
        """What is done regularly: while the mode is on, hold the tasks whose
        work newly waits for the GPU; after it, resume the held tasks once the
        main LLM is back. Errors are logged; the next tick tries again."""
        async with self._lock:
            try:
                if self._state is FullGpuState.ON:
                    await self._hold_gpu_tasks()
                elif self._state is FullGpuState.RESUMING:
                    await self._resume()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("Full GPU Mode tick failed (%s)", type(error).__name__)
            return self.status()

    async def serve(
        self, stop: asyncio.Event, *, interval: float | None = None
    ) -> None:
        """:meth:`tick` every ``interval`` seconds (default: the sweep) until
        ``stop`` is set. The scheduler's own ``serve`` must run too (it loads
        the models again)."""
        if not isinstance(stop, asyncio.Event):
            raise InvalidComputeArgumentError("stop")
        interval = (
            self._sweep if interval is None else check_seconds("interval", interval)
        )
        if interval == 0:
            raise InvalidComputeArgumentError("interval")
        while not stop.is_set():
            await self.tick()
            waiter = asyncio.ensure_future(stop.wait())
            try:
                await self._wait_for(waiter, interval)
            finally:
                waiter.cancel()

    # -- internals ------------------------------------------------------------

    async def _authorize(self, principal: Principal | None) -> None:
        decision = await self._authorizer.authorize(
            principal, CAPABILITY, Resource.system()
        )
        if not decision:
            raise FullGpuPermissionDeniedError(decision.reason)

    def _whole_gpu(self) -> int | None:
        """What the job may have: the GPU but the safety headroom and what other
        workloads use (the workspace frees its own). ``None``: no fresh reading."""
        vram = self._scheduler.status().vram
        if vram is None:
            return None
        free = vram.total - vram.headroom - vram.external
        return free if free > 0 else None

    async def _acquire(
        self, request: ComputeRequest, drain: float, preempt: bool
    ) -> ComputeLease:
        """The Exclusive lease, holding the tasks of the GPU work meanwhile."""
        wait = drain + (self._preempt_wait if preempt else 0.0)
        acquire = asyncio.ensure_future(
            self._scheduler.acquire(request, wait_seconds=wait)
        )
        deadline = self._clock.monotonic() + drain
        try:
            while not acquire.done():
                await self._hold_gpu_tasks()
                if acquire.done():
                    break
                now = self._clock.monotonic()
                if preempt and not self._preempted and now >= deadline:
                    # The drain time is over: the work still running is asked to
                    # stop (its tasks are held already, so nothing new starts).
                    self._preempted = True
                    self._scheduler.revoke_local_gpu()
                pause = self._sweep
                if preempt and not self._preempted:
                    pause = min(pause, max(0.0, deadline - now))
                await self._wait_for(acquire, pause)
        except BaseException:
            acquire.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await acquire
            # The lease may have been granted just before the cancellation
            # reached us (Codex review #161, P1): give it back, or the scheduler
            # would stay exclusive with a lease nobody holds.
            if not acquire.cancelled() and acquire.exception() is None:
                await acquire.result().release()
            raise
        return acquire.result()

    async def _hold_gpu_tasks(self) -> None:
        for task_id in self._scheduler.gpu_task_ids():
            # Asked again at every sweep, also when it was held or not running
            # before: another rule or a person may have let it run since
            # (Codex review #161). A task that is not running is not written.
            try:
                held = await self._holds.hold(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # tried again at the next sweep
                logger.warning(
                    "A task could not be held for Full GPU Mode (%s)",
                    type(error).__name__,
                )
                continue
            if held is True:
                self._held.add(task_id)

    def _main_resident(self) -> bool:
        # Also seen on the GPU (or placed there by the scheduler), not only so
        # in the scheduler's state: a process that starts after one that ended
        # with the main LLM off the GPU has it ``gpu`` from the configuration
        # (``initial``) until a reading shows otherwise (Codex review #168).
        # ``None``: no model control, the state is all there is.
        return all(
            deployment.state is DeploymentState.GPU
            and not deployment.draining
            and deployment.observed_on_gpu is not False
            for deployment in self._scheduler.status().deployments
            if deployment.role is ModelRole.MAIN
        )

    async def _resume(self) -> None:
        if not self._main_resident():
            if (
                self._ended_at is not None
                and not self._needs_human
                and self._clock.monotonic() - self._ended_at > self._reload
            ):
                self._needs_human = True
                logger.warning(
                    "The main LLM is not back on the GPU after Full GPU Mode: the "
                    "held tasks keep waiting"
                )
            return
        report = await self._holds.resume_held()
        if report.resumed:
            logger.info("%d task(s) held by Full GPU Mode resumed", report.resumed)
        if report.remaining == 0:
            self._state = FullGpuState.OFF
            self._needs_human = False
            self._ended_at = None

    async def _wait_for(self, awaitable: asyncio.Future, seconds: float) -> bool:
        if awaitable.done():
            return True
        timer = asyncio.ensure_future(self._clock.sleep(seconds))
        try:
            await asyncio.wait({awaitable, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await timer
        return awaitable.done()


__all__ = [
    "CAPABILITY",
    "HOLD_REASON",
    "RESUME_REASON",
    "FullGpuMode",
    "FullGpuState",
    "FullGpuStatus",
    "ResumeReport",
    "TaskHolds",
]
