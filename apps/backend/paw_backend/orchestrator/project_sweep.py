"""Stopping the tasks of projects whose deletion began, on a schedule (PAW-034).

Decision 0008, section 8: ``ProjectService.begin_deletion`` records a durable
request, and ``ProjectTaskStopper.stop_project_tasks`` carries it out. Two things
need somebody to call it regularly, and the orchestrator's lane is that somebody
(the acceptance condition of issue #30):

* the open requests (``pending_project_ids``), until each reports ``done``;
* **every project that is Pending deletion, also those whose request was already
  processed**: a task or a queue entry can appear after the request was processed
  (a ``create_task`` or ``enqueue`` that raced the deletion; the gate that closes
  the race is issue #83), and ``stop_project_tasks`` works from the project's
  state, so a later call stops it. That is why the sweep lists the Pending
  deletion projects themselves.

The schedule is bounded (Decision 0021, section 10)
--------------------------------------------------
* a **cycle** looks at most ``projects_per_cycle`` projects: the projects with an
  open request first, then Pending deletion projects in id order from where the
  previous cycle stopped (a cursor, so that no project starves when there are more
  than the bound); each project gets at most ``rounds_per_project`` calls in a
  cycle (a project with more active tasks than one call stops is finished by the
  next cycle, which comes soon after);
* between cycles the loop waits ``interval_seconds``, or less when a project was
  not finished (:data:`CATCH_UP_DELAY_SECONDS`), or more (a back-off that doubles
  from :data:`RETRY_BASE_SECONDS` up to the interval) after a cycle that failed
  as a whole; one project that raises does not stop the others or the loop;
* the first cycle starts :data:`FIRST_CYCLE_DELAY_SECONDS` after the loop does.

Shutdown is clean: :meth:`ProjectTaskStopLoop.stop` (or a cancellation) ends the
loop at its next suspension point; a call to ``stop_project_tasks`` that is cut
short is safe (it is idempotent and re-run by the next cycle). Errors are logged
by type only (a database message can hold ids). The loop has no clock of its own:
its waits go through the injected ``Clock``.

Several Backend processes may each run one: the stopper's commands are idempotent
and conflicts are left for the next run (``TaskConflictError``).
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import bindparam, select
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz import PostgresAuditSink
from paw_backend.db import Database
from paw_backend.orchestrator.config import Clock, SystemClock
from paw_backend.orchestrator.errors import error_class_of
from paw_backend.orchestrator.limits import (
    DEFAULT_PROJECTS_PER_CYCLE,
    DEFAULT_ROUNDS_PER_PROJECT,
    DEFAULT_STOP_INTERVAL_SECONDS,
    MAX_PROJECTS_PER_CYCLE,
    MAX_ROUNDS_PER_PROJECT,
    MAX_STOP_INTERVAL_SECONDS,
    MIN_STOP_INTERVAL_SECONDS,
)
from paw_backend.orchestrator.validation import check_int, check_seconds, check_uuid
from paw_backend.projects.models import ProjectRow
from paw_backend.projects.task_stop import ProjectTaskStopper, TaskStopResult
from paw_backend.tasks import TaskService
from paw_backend.tasks.queueing import TaskQueue
from paw_backend.tools import ApprovalService, PostgresApprovalStore
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger(__name__)

# The wait after a cycle that left a project unfinished.
CATCH_UP_DELAY_SECONDS = 5.0
# The first wait after a cycle that failed as a whole (it doubles up to the interval).
RETRY_BASE_SECONDS = 10.0
# The wait between the start of the loop and its first cycle (at most the interval).
FIRST_CYCLE_DELAY_SECONDS = 5.0
# Doubling stops here, so that the failure counter stays a small integer.
_MAX_BACKOFF_STEPS = 20


class TaskStopping(Protocol):
    """What the loop needs of ``ProjectTaskStopper``."""

    async def pending_project_ids(self, limit: int = ...) -> tuple[uuid.UUID, ...]: ...

    async def stop_project_tasks(self, project_id: uuid.UUID) -> TaskStopResult: ...


def pending_deletion_statement(after: uuid.UUID | None, limit: int):
    """The statement of :meth:`PendingDeletionLister.list_after`. The state is
    written into it (``literal_execute``), not bound: a prepared statement's
    generic plan could not otherwise prove that it may use the partial index
    ``ix_projects_pending_deletion``."""
    statement = (
        select(ProjectRow.id)
        .where(
            ProjectRow.status
            == bindparam("state", "pending_deletion", literal_execute=True)
        )
        .order_by(ProjectRow.id)
        .limit(limit)
    )
    if after is not None:
        statement = statement.where(ProjectRow.id > after)
    return statement


class PendingDeletionLister:
    """The ids of the projects that are Pending deletion, in id order."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database

    async def list_after(
        self, after: uuid.UUID | None, limit: int
    ) -> tuple[uuid.UUID, ...]:
        """At most ``limit`` (1 to ``MAX_PROJECTS_PER_CYCLE``) ids greater than
        ``after`` (``None``: from the start). The state is written into the
        statement (not a bind parameter) so that the partial index
        ``ix_projects_pending_deletion`` is usable by a generic plan too."""
        if after is not None:
            check_uuid("after", after)
        check_int("limit", limit, minimum=1, maximum=MAX_PROJECTS_PER_CYCLE)
        statement = pending_deletion_statement(after, limit)
        async with self._database.session() as session, session.begin():
            return await self._ids(session, statement)

    @staticmethod
    async def _ids(session: AsyncSession, statement) -> tuple[uuid.UUID, ...]:
        return tuple((await session.execute(statement)).scalars())


@dataclass(frozen=True, slots=True)
class SweepReport:
    """What one cycle did. ``failed`` counts the projects whose call raised."""

    projects: int
    stopped_tasks: int
    cancelled_entries: int
    unfinished: int
    failed: int


class ProjectTaskStopLoop:
    def __init__(
        self,
        stopper: TaskStopping,
        lister: PendingDeletionLister,
        *,
        interval_seconds: float = DEFAULT_STOP_INTERVAL_SECONDS,
        projects_per_cycle: int = DEFAULT_PROJECTS_PER_CYCLE,
        rounds_per_project: int = DEFAULT_ROUNDS_PER_PROJECT,
        clock: Clock | None = None,
    ) -> None:
        """Every argument is checked here: a wrong one fails loudly at construction
        (``TypeError`` for a wrong object, ``InvalidOrchestratorArgumentError`` for
        a value out of range)."""
        require_async_method(stopper, "pending_project_ids", 1)
        require_async_method(stopper, "stop_project_tasks", 1)
        if not isinstance(lister, PendingDeletionLister):
            raise TypeError("lister must be a PendingDeletionLister")
        self._interval = check_seconds(
            "interval_seconds",
            interval_seconds,
            minimum=MIN_STOP_INTERVAL_SECONDS,
            maximum=MAX_STOP_INTERVAL_SECONDS,
        )
        self._per_cycle = check_int(
            "projects_per_cycle",
            projects_per_cycle,
            minimum=1,
            maximum=MAX_PROJECTS_PER_CYCLE,
        )
        self._rounds = check_int(
            "rounds_per_project",
            rounds_per_project,
            minimum=1,
            maximum=MAX_ROUNDS_PER_PROJECT,
        )
        clock = clock or SystemClock()
        require_async_method(clock, "sleep", 1)
        self._stopper = stopper
        self._lister = lister
        self._clock = clock
        self._cursor: uuid.UUID | None = None
        self._stopping = asyncio.Event()

    @property
    def cursor(self) -> uuid.UUID | None:
        """Where the next cycle continues the Pending deletion projects."""
        return self._cursor

    def stop(self) -> None:
        """Ask the loop to end: ``run`` returns at its next suspension point, and a
        cycle in progress ends after the project it is working on."""
        self._stopping.set()

    async def run_cycle(self) -> SweepReport:
        """One cycle (see the module docstring). Errors of one project are logged
        and counted; the others are still swept. An error of the listing itself
        propagates (``run`` handles it)."""
        wanted = self._per_cycle
        open_requests = await self._stopper.pending_project_ids(wanted)
        ids: list[uuid.UUID] = list(dict.fromkeys(open_requests))[:wanted]
        room = wanted - len(ids)
        if room > 0:
            listed = await self._lister.list_after(self._cursor, room)
            self._cursor = listed[-1] if len(listed) == room else None
            ids.extend(i for i in listed if i not in ids)
        stopped = entries = unfinished = failed = 0
        for project_id in ids:
            if self._stopping.is_set():
                break
            try:
                result = await self._sweep(project_id)
            except Exception as error:  # one project must not stop the others
                failed += 1
                logger.error(
                    "Stopping a project's tasks failed (%s)", error_class_of(error)
                )
                continue
            stopped += len(result.stopped)
            entries += result.cancelled_entries
            unfinished += not result.done
        return SweepReport(len(ids), stopped, entries, unfinished, failed)

    async def _sweep(self, project_id: uuid.UUID) -> TaskStopResult:
        stopped: list[uuid.UUID] = []
        entries = 0
        result = None
        for _ in range(self._rounds):
            result = await self._stopper.stop_project_tasks(project_id)
            stopped.extend(result.stopped)
            entries += result.cancelled_entries
            if result.done:
                break
        assert result is not None
        return TaskStopResult(project_id, tuple(stopped), entries, result.done)

    async def run(self) -> None:
        """Cycle, wait, cycle, ... until :meth:`stop` or a cancellation."""
        failures = 0
        await self._sleep(min(self._interval, FIRST_CYCLE_DELAY_SECONDS))
        while not self._stopping.is_set():
            try:
                report = await self.run_cycle()
            except Exception as error:  # a supervisor: one bad cycle must not end it
                failures = min(failures + 1, _MAX_BACKOFF_STEPS)
                delay = min(self._interval, RETRY_BASE_SECONDS * 2 ** (failures - 1))
                logger.warning(
                    "Project task-stop cycle failed (%s); retrying in %.0f s",
                    error_class_of(error),
                    delay,
                )
            else:
                failures = 0
                delay = (
                    min(self._interval, CATCH_UP_DELAY_SECONDS)
                    if report.unfinished
                    else self._interval
                )
                if report.stopped_tasks or report.cancelled_entries:
                    logger.info(
                        "Project task-stop stopped %d task(s) and %d queue entry(ies)",
                        report.stopped_tasks,
                        report.cancelled_entries,
                    )
            await self._sleep(delay)

    async def _sleep(self, seconds: float) -> None:
        """Wait ``seconds`` on the clock, or until :meth:`stop`."""
        timer = asyncio.create_task(self._clock.sleep(seconds))
        waiter = asyncio.create_task(self._stopping.wait())
        try:
            await asyncio.wait({timer, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (timer, waiter):
                task.cancel()
            await asyncio.gather(timer, waiter, return_exceptions=True)


def build_project_stop_loop(
    database: Database,
    *,
    interval_seconds: float = DEFAULT_STOP_INTERVAL_SECONDS,
    clock: Clock | None = None,
) -> ProjectTaskStopLoop:
    """The loop the application runs, wired the way production wires it.

    The task service carries the approval revocation listener (Decision 0006,
    section 9: a task that ends, here by Cancel, keeps no usable approval), so a
    task the stopper cancels loses its open approvals at once.
    """
    approvals = ApprovalService(
        PostgresApprovalStore(database), PostgresAuditSink(database)
    )
    tasks = TaskService(database, listeners=[approvals.revoke_on_task_end])
    stopper = ProjectTaskStopper(database, tasks, TaskQueue(database))
    return ProjectTaskStopLoop(
        stopper,
        PendingDeletionLister(database),
        interval_seconds=interval_seconds,
        clock=clock,
    )
