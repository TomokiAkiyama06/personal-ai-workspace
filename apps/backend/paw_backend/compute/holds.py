"""Holding and resuming Agent Tasks for Full GPU Mode, in PostgreSQL (PAW-037).

:class:`PostgresTaskHolds` is the :class:`~paw_backend.compute.full_gpu.TaskHolds`
of production. It uses only the task lifecycle of PAW-032 (``TaskService``) and
the queue of PAW-033 (``TaskQueue``); there is no table of its own (Decision
0055, Approved):

* **hold**: ``wait`` (reason ``resource``) by the policy actor with
  :data:`~paw_backend.compute.full_gpu.HOLD_REASON`. Only a running task can be
  held (the lifecycle's rule); one that already waits, is paused, evaluates or
  has ended is left alone.
* **which tasks are held**: a task that is ``waiting`` for a resource and whose
  latest ``wait`` event is the policy's with that reason. Only ``wait`` leads to
  ``waiting``, so that event is the one that put it there. A task a human or
  another rule put in waiting is never taken for a held one, and the history
  keeps the answer across a restart of the backend.
* **resume**: ``unblock`` by the policy actor (reason
  :data:`~paw_backend.compute.full_gpu.RESUME_REASON`), at the version the task
  had when it was found held (a task that changed since is looked at again on
  the next call), and, in the same transaction, a new queue entry with the
  priority of its last one: waiting completed its entry, and whoever unblocks a
  task enqueues it again. When its entry is still active (a worker is still
  finishing the node that was running when it was held) no entry is added: that
  worker goes on with the task.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.compute.full_gpu import HOLD_REASON, RESUME_REASON, ResumeReport
from paw_backend.db import Database
from paw_backend.tasks import (
    Actor,
    ActorKind,
    IllegalTransitionError,
    TaskCommand,
    TaskConflictError,
    TaskError,
    TaskNotFoundError,
    TaskService,
    TaskState,
    WaitReason,
)
from paw_backend.tasks.models import TaskEventRow, TaskRow
from paw_backend.tasks.queueing import Priority, TaskQueue
from paw_backend.tasks.queueing.errors import TaskAlreadyQueuedError
from paw_backend.tasks.queueing.models import QueueEntryRow

logger = logging.getLogger("paw_backend.compute")

# How many held tasks one query reads.
_BATCH = 100


class PostgresTaskHolds:
    """See the module."""

    def __init__(
        self,
        tasks: TaskService,
        queue: TaskQueue,
        *,
        hold_reason: str = HOLD_REASON,
        resume_reason: str = RESUME_REASON,
    ) -> None:
        """``hold_reason`` / ``resume_reason``: the fixed reasons of the policy's
        ``wait`` / ``unblock``. Full GPU Mode's by default; an update's
        maintenance (Issue #54, Decision 0079) holds with its own, so neither
        resumes the other's tasks."""
        if not isinstance(tasks, TaskService):
            raise TypeError("tasks must be a TaskService")
        if not isinstance(queue, TaskQueue):
            raise TypeError("queue must be a TaskQueue")
        for reason in (hold_reason, resume_reason):
            if not isinstance(reason, str) or not reason:
                raise TypeError("the reasons must be non-empty strings")
        if hold_reason == resume_reason:
            raise ValueError("the hold and resume reasons must differ")
        self._tasks = tasks
        self._queue = queue
        self._hold_reason = hold_reason
        self._resume_reason = resume_reason

    @property
    def _database(self) -> Database:
        return self._queue.database

    async def hold(self, task_id: uuid.UUID) -> bool:
        try:
            await self._tasks.execute(
                task_id,
                TaskCommand.WAIT,
                actor=Actor.policy(),
                wait_reason=WaitReason.RESOURCE,
                reason=self._hold_reason,
            )
        except (IllegalTransitionError, TaskNotFoundError):
            return False  # not running: nothing of it will start meanwhile
        return True

    async def held(
        self, *, after: uuid.UUID | None = None, limit: int = _BATCH
    ) -> list[tuple[uuid.UUID, int]]:
        """The held tasks (id and version), by id, at most ``limit`` (one batch)
        after ``after``."""
        latest_wait = (
            select(TaskEventRow.actor_kind, TaskEventRow.reason)
            .where(
                TaskEventRow.task_id == TaskRow.id,
                TaskEventRow.command == TaskCommand.WAIT,
            )
            .order_by(TaskEventRow.seq.desc())
            .limit(1)
            .lateral("latest_wait")
        )
        query = (
            select(TaskRow.id, TaskRow.version)
            .join(latest_wait, latest_wait.c.actor_kind == ActorKind.POLICY)
            .where(
                TaskRow.state == TaskState.WAITING,
                TaskRow.wait_reason == WaitReason.RESOURCE,
                latest_wait.c.reason == self._hold_reason,
            )
            .order_by(TaskRow.id)
            .limit(limit)
        )
        if after is not None:
            query = query.where(TaskRow.id > after)
        async with self._database.session() as session:
            rows = (await session.execute(query)).all()
        return [(row.id, row.version) for row in rows]

    async def any_held(self) -> bool:
        return bool(await self.held(limit=1))

    async def resume_held(self) -> ResumeReport:
        resumed = remaining = 0
        after: uuid.UUID | None = None
        while True:
            batch = await self.held(after=after)
            for task_id, version in batch:
                if await self._resume(task_id, version):
                    resumed += 1
                else:
                    remaining += 1
            if len(batch) < _BATCH:
                return ResumeReport(resumed, remaining)
            after = batch[-1][0]

    async def _resume(self, task_id: uuid.UUID, version: int) -> bool:
        async def enqueue(
            session: AsyncSession, task_id: uuid.UUID, project_id: uuid.UUID
        ) -> None:
            priority = (
                await session.execute(
                    select(QueueEntryRow.priority)
                    .where(QueueEntryRow.task_id == task_id)
                    .order_by(QueueEntryRow.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            try:
                async with session.begin_nested():
                    await self._queue.enqueue_in(
                        session, task_id, priority=priority or Priority.NORMAL
                    )
            except TaskAlreadyQueuedError:
                pass  # its worker is still on it and goes on with the task

        try:
            await self._tasks.execute(
                task_id,
                TaskCommand.UNBLOCK,
                actor=Actor.policy(),
                expected_version=version,
                reason=self._resume_reason,
                in_transaction=enqueue,
            )
        except (TaskConflictError, IllegalTransitionError, TaskNotFoundError):
            # Changed since it was read (looked at again next time) or no longer
            # held: not resumed now.
            return False
        except TaskError as error:
            # A task that cannot run now (no target repository, a project that is
            # not Active): it stays held; a human or the project's rules end it.
            logger.warning(
                "A task held by Full GPU Mode could not resume (%s)",
                type(error).__name__,
            )
            return False
        return True
