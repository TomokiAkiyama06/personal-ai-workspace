"""The scheduler in the application (issue #165, Decision 0058, Proposed).

What ``paw_backend.app`` needs to run the Compute Resource Scheduler (PAW-036)
and Kaggle / Full GPU Mode (PAW-037) in the backend process, only when the
deployment gives it a :class:`ComputeSetup` (there is no environment setting;
Decision 0058, 1):

* :func:`build_compute` builds the process's one :class:`ComputeScheduler` (Decision
  0037, 1) with :class:`RecentVramWarnings` as its ``VramWarningSink`` (Decision
  0042, 6: the scheduler logs the warning itself; the sink keeps the latest ones
  in memory for the administrators' status, until System Health (PAW-066) takes
  them). The lifespan runs ``scheduler.serve`` and, with a database,
  ``FullGpuMode.serve``.
* :class:`LocalRuntime` names an orchestrator runtime that runs on a local model;
  the composition (``orchestrator/composition.py``) wraps it in a
  :class:`HybridRuntime` on the scheduler, so a node waits for a lease before it
  uses the GPU. No ``CloudPolicy`` is injected (Decision 0037, 14): nothing goes
  to the cloud from here.
* :class:`FullGpuController` is what the HTTP routes call
  (``api/v1/compute.py``). Starting Full GPU Mode drains the running local GPU
  work for up to ``drain_seconds`` (600 by default), longer than an HTTP request
  should wait: the start runs in the background and the route answers at once
  (``202``); the state is read with ``GET`` (Decision 0058, 3). Ending it while
  the start is still in progress abandons the start (Decision 0058, 4). So does
  the shutdown, which then, like after any start that did not finish and after
  an end, gives the models back and resumes the held tasks before the
  scheduler's loops stop, within the shutdown time (Codex review #168).

GPU safety: nothing here reads or touches the GPU but through the scheduler (its
read-only probe and the injected ``ModelControl``).
"""

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from paw_backend.authz import Principal
from paw_backend.compute.alerts import VramDeferral
from paw_backend.compute.config import ComputeConfig
from paw_backend.compute.domain import ResourceClass
from paw_backend.compute.errors import FullGpuModeStateError
from paw_backend.compute.full_gpu import FullGpuMode, FullGpuState, FullGpuStatus
from paw_backend.compute.limits import (
    DEFAULT_NODE_WAIT_SECONDS,
    DEFAULT_REFRESH_SECONDS,
)
from paw_backend.compute.scheduler import ComputeScheduler, check_seconds
from paw_backend.orchestrator.config import Clock
from paw_backend.orchestrator.runtime import AgentRuntime, validate_runtime
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger("paw_backend.compute")

# How many VRAM warnings the status keeps (the latest ones). The scheduler emits
# one of a kind per ``vram_warning_interval_seconds`` (300 s), so this covers
# hours of a busy GPU.
DEFAULT_KEPT_VRAM_WARNINGS = 50
MAX_KEPT_VRAM_WARNINGS = 1_000
# At shutdown, after an abandoned start: how long to pause between two attempts
# to load the main LLM again and resume the held tasks (real time: the caller
# bounds the whole with the shutdown timeout).
RECOVERY_PAUSE_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class ComputeSetup:
    """What a deployment gives ``create_app(compute=...)`` to run the scheduler.

    ``probe``: the read-only GPU probe (``NvidiaSmiProbe``). ``control``: the
    ``ModelControl`` that loads and unloads the models (``None``: the scheduler
    only observes and admits, and Full GPU Mode cannot start, Decision 0037, 7).
    ``refresh_seconds``: how often ``refresh()`` runs. ``kept_vram_warnings``: how
    many warnings :class:`RecentVramWarnings` keeps. ``clock``: tests pass one."""

    config: ComputeConfig
    probe: object
    control: object | None = None
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS
    kept_vram_warnings: int = DEFAULT_KEPT_VRAM_WARNINGS
    clock: Clock | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.config, ComputeConfig):
            raise TypeError("config must be a ComputeConfig")
        require_async_method(self.probe, "sample", 0)
        if check_seconds("refresh_seconds", self.refresh_seconds) == 0:
            raise ValueError("refresh_seconds must be positive")
        kept = self.kept_vram_warnings
        if (
            isinstance(kept, bool)
            or not isinstance(kept, int)
            or not 1 <= kept <= MAX_KEPT_VRAM_WARNINGS
        ):
            raise ValueError("kept_vram_warnings")


@dataclass(frozen=True, slots=True)
class LocalRuntime:
    """An orchestrator runtime that runs on the local model ``deployment``.

    The composition wraps it in ``HybridRuntime(scheduler, runtime,
    deployment=..., local_model=..., resource_class=..., wait_seconds=...)``
    with the task budget's late GPU charge (``TrackerLateGpuCharge``). It is
    checked there, against the scheduler's deployments."""

    runtime: AgentRuntime
    deployment: str
    local_model: str | None = None
    resource_class: ResourceClass = ResourceClass.CODING
    wait_seconds: float = DEFAULT_NODE_WAIT_SECONDS

    def __post_init__(self) -> None:
        validate_runtime(self.runtime, "runtime")


@dataclass(frozen=True, slots=True)
class RecordedVramWarning:
    occurred_at: datetime
    event: VramDeferral


class RecentVramWarnings:
    """The ``VramWarningSink`` of the application (Decision 0042, 6; Decision
    0058, 2): keeps the latest warnings in memory, newest last. The scheduler
    has logged each one already; nothing is written to the database. Called on
    the event loop, it never blocks."""

    def __init__(
        self,
        *,
        kept: int = DEFAULT_KEPT_VRAM_WARNINGS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._kept: deque[RecordedVramWarning] = deque(maxlen=kept)
        self._now = now
        self._total = 0

    def vram_deferred(self, event: VramDeferral) -> None:
        self._kept.append(RecordedVramWarning(self._now(), event))
        self._total += 1

    @property
    def total(self) -> int:
        """How many warnings this process received (also the ones dropped)."""
        return self._total

    def recent(self) -> tuple[RecordedVramWarning, ...]:
        return tuple(self._kept)


def build_compute(setup: ComputeSetup) -> tuple[ComputeScheduler, RecentVramWarnings]:
    """The process's scheduler with the application's VRAM warning sink."""
    if not isinstance(setup, ComputeSetup):
        raise TypeError("compute must be a ComputeSetup")
    warnings = RecentVramWarnings(kept=setup.kept_vram_warnings)
    scheduler = ComputeScheduler(
        setup.config,
        setup.probe,
        control=setup.control,
        clock=setup.clock,
        vram_warnings=warnings,
    )
    return scheduler, warnings


class FullGpuController:
    """Full GPU Mode for the HTTP routes (see the module): the start runs in the
    background, one at a time; the end is immediate. The principal is the
    request's: :class:`FullGpuMode` authorizes it again (and audits it) for
    ``admin.compute.full_gpu``."""

    def __init__(self, mode: FullGpuMode, scheduler: ComputeScheduler) -> None:
        if not isinstance(mode, FullGpuMode):
            raise TypeError("mode must be a FullGpuMode")
        if not isinstance(scheduler, ComputeScheduler):
            raise TypeError("scheduler must be a ComputeScheduler")
        self._mode = mode
        self._scheduler = scheduler
        self._start: asyncio.Task[None] | None = None

    @property
    def mode(self) -> FullGpuMode:
        return self._mode

    @property
    def start_pending(self) -> bool:
        """A start was accepted and has not finished (it may not have reached
        the ``starting`` state yet: it is authorized first)."""
        return self._start is not None and not self._start.done()

    def status(self) -> FullGpuStatus:
        return self._mode.status()

    def start(
        self,
        principal: Principal,
        *,
        vram_bytes: int | None = None,
        drain_seconds: float | None = None,
        preempt: bool = False,
    ) -> None:
        """Begin the start in the background. Raises
        :class:`FullGpuModeStateError` when a start is pending or the mode is
        ``starting`` / ``on``. How the start ends is in :meth:`status`
        (``state``, ``last_failure``) and the log."""
        if self.start_pending:
            raise FullGpuModeStateError(FullGpuState.STARTING.value)
        state = self._mode.status().state
        if state in (FullGpuState.STARTING, FullGpuState.ON):
            raise FullGpuModeStateError(state.value)
        self._start = asyncio.create_task(
            self._run_start(
                principal,
                vram_bytes=vram_bytes,
                drain_seconds=drain_seconds,
                preempt=preempt,
            ),
            name="full-gpu-start",
        )

    async def end(self, principal: Principal) -> FullGpuStatus:
        """End Full GPU Mode, or abandon a start that has not finished (the
        scheduler goes back to normal and the held tasks resume). Raises
        :class:`FullGpuModeStateError` when there is nothing to end and
        ``FullGpuPermissionDeniedError`` from :meth:`FullGpuMode.end`."""
        if self.start_pending:
            await self._cancel_start()
            logger.warning("Full GPU Mode start abandoned: the held tasks resume")
            return self._mode.status()
        return await self._mode.end(principal)

    async def close(self) -> None:
        """At shutdown, before the scheduler's loops stop: abandon a start in
        progress, and finish what a Full GPU Mode that is ``resuming`` left
        undone. A start abandoned now or before (``DELETE``, a failure) may
        have unloaded models, an end has: the scheduler is refreshed (it loads
        the main LLM again) and the mode ticked (it resumes the held tasks)
        until the mode is ``off`` (Codex review #168). The caller bounds this
        with the shutdown timeout; what is left then is done by the next
        process (it resumes the tasks held by this one)."""
        if self.start_pending:
            await self._cancel_start()
        while self._mode.status().state is FullGpuState.RESUMING:
            try:
                await self._scheduler.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # tried again after the pause
                logger.warning(
                    "The GPU could not be read after an abandoned Full GPU Mode "
                    "start (%s)",
                    type(error).__name__,
                )
            if (await self._mode.tick()).state is not FullGpuState.RESUMING:
                break
            await asyncio.sleep(RECOVERY_PAUSE_SECONDS)

    async def _cancel_start(self) -> None:
        task = self._start
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run_start(
        self,
        principal: Principal,
        *,
        vram_bytes: int | None,
        drain_seconds: float | None,
        preempt: bool,
    ) -> None:
        try:
            await self._mode.start(
                principal,
                vram_bytes=vram_bytes,
                drain_seconds=drain_seconds,
                preempt=preempt,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # FullGpuMode logged why the GPU could not be emptied; the rest (a
            # denial, a start that raced another one) is logged by its type.
            logger.warning("Full GPU Mode did not start (%s)", type(error).__name__)


@dataclass(slots=True)
class ComputeServices:
    """What ``create_app`` keeps in ``app.state.compute``: the scheduler, the
    warning sink and, once the lifespan runs with a database, the Full GPU
    Mode controller (``None`` without one: holding tasks needs PostgreSQL)."""

    setup: ComputeSetup
    scheduler: ComputeScheduler
    warnings: RecentVramWarnings
    full_gpu: FullGpuController | None = None


__all__ = [
    "DEFAULT_KEPT_VRAM_WARNINGS",
    "ComputeServices",
    "ComputeSetup",
    "FullGpuController",
    "LocalRuntime",
    "RecentVramWarnings",
    "RecordedVramWarning",
    "build_compute",
]
