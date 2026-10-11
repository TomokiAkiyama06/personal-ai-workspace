"""An update's maintenance: stop starting tasks, drain the running ones to a
checkpoint, resume them afterwards (Issue #54, Decision 0079 3).

REQUIREMENTS.md ("Deployment / Update / Rollback", FIXED) asks a normal update
to stop accepting new tasks, drain the running ones to a safe checkpoint, keep
their state / branch / worktree and resume the queue after the update. This uses
what Full GPU Mode already does for the GPU (Decision 0055, Approved), with a
reason of its own:

* **begin** writes the one row of ``deploy_maintenance``. While it exists
  ``TaskQueue.claim_next`` hands out nothing: no queued task starts (they stay
  queued and keep their order). Creating and queueing tasks still works.
* **hold** puts every ``running`` task in ``waiting`` for a resource, by the
  policy actor, with :data:`HOLD_REASON` (``PostgresTaskHolds``). Waiting stops
  a task like a pause: no new node starts, and the running nodes finish (the
  checkpoint is the end of a node: its result, branch and worktree are kept).
* **drained** when no queue entry is claimed with a live lease (no worker is
  running anything) and no task is ``running``. The drain repeats the hold, so
  a task a worker started just before the row was written is held too.
* **end** resumes the tasks this maintenance held (``unblock`` and a new queue
  entry with their priority; Full GPU Mode's held tasks are not touched) and
  then deletes the row. A crash in between leaves the maintenance on (visible,
  safe); ending it again finishes the job.

Every step is idempotent. The row lives in the database, so it holds every
backend process and survives their restart; a database restored from a restore
point taken during the maintenance has the row too (the restored system stays
in maintenance until it is ended explicitly).
"""

import asyncio
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from paw_backend.compute.full_gpu import ResumeReport
from paw_backend.compute.holds import PostgresTaskHolds
from paw_backend.db import Database
from paw_backend.tasks import TaskService, TaskState
from paw_backend.tasks.models import TaskRow
from paw_backend.tasks.queueing import TaskQueue
from paw_backend.tasks.queueing.domain import QueueStatus
from paw_backend.tasks.queueing.models import (
    RELEASE_NAME_PATTERN,
    DeployMaintenanceRow,
    QueueEntryRow,
)

HOLD_REASON = "Deploy / Update maintenance"
RESUME_REASON = "Deploy / Update maintenance ended"

# The task states the status reports (the ones that are not over).
OPEN_STATES = (
    TaskState.QUEUED,
    TaskState.RUNNING,
    TaskState.WAITING,
    TaskState.PAUSED,
    TaskState.EVALUATING,
)


class InvalidReleaseNameError(ValueError):
    """A release name that is not ``RELEASE_NAME_PATTERN``."""


def check_release_name(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(RELEASE_NAME_PATTERN, value):
        raise InvalidReleaseNameError("release name")
    return value


@dataclass(frozen=True, slots=True)
class MaintenanceState:
    started_at: datetime
    from_release: str | None
    to_release: str | None


@dataclass(frozen=True, slots=True)
class DrainStatus:
    """``active_claims``: queue entries a worker holds with a live lease;
    ``running``: tasks in ``running``; ``held``: tasks this maintenance holds."""

    active_claims: int
    running: int
    held: int

    @property
    def drained(self) -> bool:
        return self.active_claims == 0 and self.running == 0


class DeployMaintenance:
    """See the module."""

    def __init__(self, database: Database, tasks: TaskService, queue: TaskQueue):
        self._database = database
        self._holds = PostgresTaskHolds(
            tasks, queue, hold_reason=HOLD_REASON, resume_reason=RESUME_REASON
        )

    async def state(self) -> MaintenanceState | None:
        async with self._database.session() as session:
            row = (await session.execute(select(DeployMaintenanceRow))).scalar()
        if row is None:
            return None
        return MaintenanceState(row.started_at, row.from_release, row.to_release)

    async def begin(
        self, *, from_release: str | None = None, to_release: str | None = None
    ) -> bool:
        """Write the row; ``False`` when a maintenance was on already (kept as
        it was)."""
        check_release_name(from_release)
        check_release_name(to_release)
        statement = (
            insert(DeployMaintenanceRow)
            .values(id=1, from_release=from_release, to_release=to_release)
            .on_conflict_do_nothing(index_elements=[DeployMaintenanceRow.id])
            .returning(DeployMaintenanceRow.id)
        )
        async with self._database.session() as session, session.begin():
            created = (await session.execute(statement)).scalar()
        return created is not None

    async def hold_running(self) -> int:
        """Hold every running task; how many were held now."""
        held = 0
        after: uuid.UUID | None = None
        while True:
            query = (
                select(TaskRow.id)
                .where(TaskRow.state == TaskState.RUNNING)
                .order_by(TaskRow.id)
                .limit(100)
            )
            if after is not None:
                query = query.where(TaskRow.id > after)
            async with self._database.session() as session:
                batch = list((await session.execute(query)).scalars())
            for task_id in batch:
                if await self._holds.hold(task_id):
                    held += 1
            if len(batch) < 100:
                return held
            after = batch[-1]

    async def drain_status(self) -> DrainStatus:
        claims = (
            select(func.count())
            .select_from(QueueEntryRow)
            .where(
                QueueEntryRow.status == QueueStatus.CLAIMED,
                QueueEntryRow.lease_expires_at > func.now(),
            )
            .scalar_subquery()
        )
        running = (
            select(func.count())
            .select_from(TaskRow)
            .where(TaskRow.state == TaskState.RUNNING)
            .scalar_subquery()
        )
        async with self._database.session() as session:
            row = (await session.execute(select(claims, running))).one()
        held = await self._held_count()
        return DrainStatus(active_claims=row[0], running=row[1], held=held)

    async def _held_count(self) -> int:
        count = 0
        after: uuid.UUID | None = None
        while True:
            batch = await self._holds.held(after=after)
            count += len(batch)
            if len(batch) < 100:
                return count
            after = batch[-1][0]

    async def drain(
        self,
        timeout_seconds: float,
        *,
        poll_seconds: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> DrainStatus:
        """Hold and wait until drained or ``timeout_seconds`` passed; the last
        status (``drained`` says which)."""
        deadline = monotonic() + timeout_seconds
        while True:
            await self.hold_running()
            status = await self.drain_status()
            if status.drained or monotonic() >= deadline:
                return status
            await sleep(min(poll_seconds, max(deadline - monotonic(), 0.0)))

    async def end(self) -> ResumeReport:
        """Resume the held tasks, then delete the row (see the module)."""
        report = await self._holds.resume_held()
        async with self._database.session() as session, session.begin():
            await session.execute(delete(DeployMaintenanceRow))
        return report


async def task_counts(database: Database) -> dict[str, int]:
    """The number of tasks in each state that is not over."""
    query = (
        select(TaskRow.state, func.count())
        .where(TaskRow.state.in_(OPEN_STATES))
        .group_by(TaskRow.state)
    )
    async with database.session() as session:
        rows = (await session.execute(query)).all()
    counts = {state.value: 0 for state in OPEN_STATES}
    for state, count in rows:
        counts[TaskState(state).value] = count
    return counts
