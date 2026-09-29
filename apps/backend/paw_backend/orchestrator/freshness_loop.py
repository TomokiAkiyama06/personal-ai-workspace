"""The Memory freshness jobs and the task-end sweep, on a schedule (issue #125).

Decision 0034 (7) left calling the freshness jobs (``FreshnessMaintenance``) to a
later issue; Decision 0047 wires them here. One cycle runs, in this order:

1. the **task-end sweep** (``TaskEndCleanup.sweep``): terminal tasks that still
   hold an open approval or an active ``session_only`` memory are finished (the
   retryable after-step of a task's end, ``task_end.py``);
2. ``FreshnessMaintenance.mark_revalidation_due``: ``revalidate`` memories past
   ``verified_at + revalidate_after`` become stale candidates;
3. ``FreshnessMaintenance.expire_due``: ``expiring`` memories at or past
   ``expires_at`` are ``deprecated``.

Each job is repeated while it changes a full batch (the sweep: while it took
``MAX_TASK_END_SWEEP`` tasks; each sweep resumes after the previous one), at most
``MAX_FRESHNESS_ROUNDS`` times, so that a backlog is worked off without one cycle
running for ever. One step that raises does not stop the others (it is logged by
type only: a database message can quote a memory).

What is **not** here: ``end_session`` (nothing records the end of a session yet,
so there is nothing to find), ``mark_triggered`` and ``mark_repo_head`` (they
answer events: a member change, a model change, a new head).

The loop is the one of ``connection_reaper.py``: the first cycle comes
``FIRST_CYCLE_DELAY_SECONDS`` after the start (at most the interval), then one
every ``interval_seconds``; an error of a whole cycle is logged by type and retried
after the interval; ``stop`` (or a cancellation) ends it at its next suspension
point. Several Backend processes may each run one: every job locks its rows
``SKIP LOCKED`` and changes a row only once, and the cleanup of a task is
idempotent.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from paw_backend.memory.versioning import FreshnessMaintenance
from paw_backend.orchestrator.config import Clock, SystemClock
from paw_backend.orchestrator.errors import error_class_of
from paw_backend.orchestrator.limits import (
    DEFAULT_FRESHNESS_INTERVAL_SECONDS,
    MAX_FRESHNESS_INTERVAL_SECONDS,
    MAX_FRESHNESS_ROUNDS,
    MAX_TASK_END_SWEEP,
    MIN_FRESHNESS_INTERVAL_SECONDS,
)
from paw_backend.orchestrator.task_end import TaskEndCleanup
from paw_backend.orchestrator.validation import check_seconds
from paw_backend.tools.interfaces import require_async_method

if TYPE_CHECKING:  # composition imports this module
    from paw_backend.orchestrator.composition import TaskExecution

logger = logging.getLogger(__name__)

# The wait between the start of the loop and its first cycle (at most the
# interval): what a restart left behind is picked up soon, not an interval later.
FIRST_CYCLE_DELAY_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    """What one cycle did. ``failed`` names the steps that raised."""

    finished_tasks: int
    marked_stale: int
    expired: int
    failed: tuple[str, ...] = ()


class FreshnessJobLoop:
    def __init__(
        self,
        freshness: FreshnessMaintenance,
        task_end: TaskEndCleanup,
        *,
        interval_seconds: float = DEFAULT_FRESHNESS_INTERVAL_SECONDS,
        batch: int | None = None,
        clock: Clock | None = None,
    ) -> None:
        """``batch`` is the batch of ``freshness`` (a job that changed fewer rows
        is finished); ``None`` reads it from ``freshness``."""
        require_async_method(freshness, "mark_revalidation_due", 0)
        require_async_method(freshness, "expire_due", 0)
        require_async_method(task_end, "sweep", 0)
        self._interval = check_seconds(
            "interval_seconds",
            interval_seconds,
            minimum=MIN_FRESHNESS_INTERVAL_SECONDS,
            maximum=MAX_FRESHNESS_INTERVAL_SECONDS,
        )
        if batch is None:
            batch = getattr(freshness, "batch", None)
        if isinstance(batch, bool) or not isinstance(batch, int) or batch < 1:
            raise TypeError("batch must be a positive int")
        clock = clock or SystemClock()
        require_async_method(clock, "sleep", 1)
        self._freshness = freshness
        self._task_end = task_end
        self._batch = batch
        self._clock = clock
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        """Ask the loop to end at its next suspension point."""
        self._stopping.set()

    async def run_cycle(self) -> MaintenanceReport:
        """One cycle (module docstring); a step that raises is logged and named."""
        failed: list[str] = []
        finished = marked = expired = 0
        try:
            finished = await self._repeat(self._sweep, MAX_TASK_END_SWEEP)
        except Exception as error:
            failed.append("task_end")
            logger.warning("The task-end sweep failed (%s)", error_class_of(error))
        try:
            marked = await self._repeat(
                self._freshness.mark_revalidation_due, self._batch
            )
        except Exception as error:
            failed.append("revalidation_due")
            logger.warning(
                "Marking memories due for revalidation failed (%s)",
                error_class_of(error),
            )
        try:
            expired = await self._repeat(self._freshness.expire_due, self._batch)
        except Exception as error:
            failed.append("expire_due")
            logger.warning("Expiring memories failed (%s)", error_class_of(error))
        if finished or marked or expired:
            logger.info(
                "Maintenance finished %d ended task(s), marked %d memory(ies) stale"
                " and expired %d",
                finished,
                marked,
                expired,
            )
        return MaintenanceReport(finished, marked, expired, tuple(failed))

    async def _sweep(self) -> int:
        return len(await self._task_end.sweep())

    async def _repeat(self, job: Callable[[], Awaitable[int]], batch: int) -> int:
        total = 0
        for _ in range(MAX_FRESHNESS_ROUNDS):
            if self._stopping.is_set():
                break
            changed = await job()
            total += changed
            if changed < batch:
                break
        return total

    async def run(self) -> None:
        """Wait, cycle, wait, ... until :meth:`stop` or a cancellation."""
        await self._sleep(min(self._interval, FIRST_CYCLE_DELAY_SECONDS))
        while not self._stopping.is_set():
            try:
                await self.run_cycle()
            except Exception as error:  # a supervisor: one bad cycle must not end it
                logger.warning("A maintenance cycle failed (%s)", error_class_of(error))
            await self._sleep(self._interval)

    async def _sleep(self, seconds: float) -> None:
        timer = asyncio.create_task(self._clock.sleep(seconds))
        waiter = asyncio.create_task(self._stopping.wait())
        try:
            await asyncio.wait({timer, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (timer, waiter):
                task.cancel()
            await asyncio.gather(timer, waiter, return_exceptions=True)


def build_freshness_loop(
    execution: "TaskExecution",
    *,
    interval_seconds: float = DEFAULT_FRESHNESS_INTERVAL_SECONDS,
    clock: Clock | None = None,
) -> FreshnessJobLoop:
    """The loop the application runs, over the jobs of its task execution (the
    same ``TaskEndCleanup`` as the ``TaskService`` listener)."""
    return FreshnessJobLoop(
        execution.freshness,
        execution.task_end,
        interval_seconds=interval_seconds,
        clock=clock,
    )
