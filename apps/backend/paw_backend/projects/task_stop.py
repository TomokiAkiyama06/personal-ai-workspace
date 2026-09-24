"""Stopping the tasks of a project whose deletion began (PAW-026, Decision 0008 s. 8).

``REQUIREMENTS.md`` ("Pending deletion"): when the deletion starts, running tasks
are safe-stopped and no new Agent task runs. ``ProjectService.begin_deletion``
cannot do that by itself (the task service and the queue are separate, and an
external worker cannot be reached from inside a database transaction), so it
records a **durable request** in ``project_task_stops`` in the SAME transaction
as the lifecycle change: either both are committed or neither is. This module is
the **processor** of that outbox. The orchestrator (PAW-034) calls it: for every
id of :meth:`ProjectTaskStopper.pending_project_ids`, again and again until
``TaskStopResult.done`` (see "What the caller must do").

What one call of :meth:`~ProjectTaskStopper.stop_project_tasks` does
--------------------------------------------------------------------
1. Reads the project. Only a project that is **Pending deletion** (or Deleted:
   purged, but tasks of it may still be active) has its tasks stopped. For an
   Active or Archived project (the deletion was restored, or it never began) it
   touches nothing: a stale or wrong call can never stop the tasks of a live
   project. It works from the project's state, not from the outbox row, so it is
   also the "stop" for a project that has no row.
2. Lists at most ``batch_size`` active tasks of the project (queued, running,
   waiting, paused, evaluating; oldest first), by a plain read of ``tasks``.
3. For each: cancels its active **queue entry** (``TaskQueue.cancel``: the entry
   can no longer be claimed and a worker that holds it loses its lease), then
   issues the PAW-032 **Cancel** command through ``TaskService.execute`` with
   ``Actor.policy()`` and a fixed reason. Task state is never written directly:
   the state machine, the ``task_events`` history and the listeners of the task
   service all apply. The order (entry first) makes a crash between the two
   harmless: the task is still active, so the next run finds it and repeats the
   idempotent queue cancel.
4. In one short transaction that holds ``SELECT ... FOR SHARE`` on the project
   row (so the project cannot be restored or changed in between), checks that no
   active task is left and only then sets ``processed_at``. If any is left (the
   batch was full, a concurrent writer beat the command, or a task became
   active again) the request stays open and ``done`` is ``False``.

Why Cancel
----------
"Safe stop" is the graceful stop of PAW-032: Cancel ends the task, keeps branch,
worktree and partial results, and lets the worker finish its current step at a
safe boundary (``finish_step``); it cannot start another (``begin_step`` is
refused in a terminal state) and its queue lease is gone. It is valid in every
active state (Pause and Stop Now are not: Stop Now does not apply to queued or
paused tasks). Stop Now (immediate, aborts the running step) is the emergency
stop and is not used: nothing here is an emergency, and Restart can re-run a
cancelled task. The alternative, Pause, would leave tasks that nobody resumes.
Decision 0008 records this choice.

Idempotent and re-runnable
--------------------------
A second call finds no active task, changes nothing and returns
``TaskStopResult(project_id, (), 0, done=True)``; the first ``processed_at``
stays. Every step is either a state change that is itself idempotent or a read.
A task that leaves the active states between the list and the command (the
worker failed or finished it, a user cancelled it) raises ``IllegalTransitionError``
from the task service and is simply not counted; a ``TaskConflictError`` leaves
the task active for the next run. Any other error propagates, with the request
still open.

What the caller must do (limits)
--------------------------------
* Keep calling until ``done``; a project with more than ``batch_size`` active
  tasks needs several calls.
* ``TaskService.create_task`` (and Retry / Restart of an earlier task) do not
  look at the project, so a task whose creation was authorized just before the
  deletion began can appear **after** the request was processed. The Authorizer
  already refuses ``project.task.run`` in Archived and Pending deletion (PAW-025);
  only that race remains. Until the task service closes it (Decision 0008
  proposes a gate in the same transaction as the insert), the orchestrator should
  also call ``stop_project_tasks`` for the projects that are Pending deletion on
  its regular cycle: the call stops such a task (test:
  ``test_a_task_created_after_the_deletion_began_is_stopped_on_a_rerun``).
* A restore that commits between the read of step 1 and a command lets that
  command cancel a task of a project that has just been restored (a task can be
  restarted). The window is one task command; closing it would need the task
  service to share a transaction with the project row.
* ``tasks.project_id`` has no index (PAW-032): the list reads ``tasks`` by a
  sequential scan until the task lane adds one on ``(project_id, state)``.

This class is Backend-internal: like ``ProjectService.purge_expired`` it takes
no actor and asks no Authorizer, and it writes no Audit event of its own (each
cancelled task has its ``task_events`` row). Errors carry no caller content.
"""

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.db import Database
from paw_backend.projects import store
from paw_backend.projects.errors import ProjectNotFoundError
from paw_backend.projects.limits import (
    DEFAULT_LOCK_TIMEOUT_MS,
    DEFAULT_TASK_STOP_BATCH_SIZE,
    utc_now,
)
from paw_backend.projects.records import ProjectStatus
from paw_backend.projects.transaction import transaction
from paw_backend.projects.validation import (
    validate_batch_size,
    validate_instant,
    validate_uuid,
)
from paw_backend.tasks import (
    Actor,
    IllegalTransitionError,
    TaskCommand,
    TaskConflictError,
    TaskNotFoundError,
    TaskService,
)
from paw_backend.tasks.queueing import TaskQueue

# Recorded as the reason of the Cancel event of every task this module stops.
STOP_REASON = "Project deletion started"

Clock = Callable[[], datetime]

# The tasks of a project in these states are stopped. A Deleted project is a
# tombstone, but tasks it had when it was purged may still be active.
_STOPPING = (ProjectStatus.PENDING_DELETION, ProjectStatus.DELETED)


@dataclass(frozen=True, slots=True)
class TaskStopResult:
    """What one ``stop_project_tasks`` call did.

    ``stopped`` lists the tasks whose Cancel this call issued (oldest first).
    ``cancelled_entries`` counts the queue entries it cancelled. ``done`` is
    true when no task of the project was left active and the request is marked
    processed (or the project needs no stop: it is Active or Archived); false
    means "call again".
    """

    project_id: uuid.UUID
    stopped: tuple[uuid.UUID, ...]
    cancelled_entries: int
    done: bool


class ProjectTaskStopper:
    """Carries out the task-stop requests of ``begin_deletion`` (module docstring)."""

    def __init__(
        self,
        database: Database,
        task_service: TaskService,
        task_queue: TaskQueue,
        *,
        clock: Clock = utc_now,
        batch_size: int = DEFAULT_TASK_STOP_BATCH_SIZE,
    ) -> None:
        """``TypeError`` for a wrong type, ``InvalidProjectInputError`` for a size."""
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if not isinstance(task_service, TaskService):
            raise TypeError("task_service must be a TaskService")
        if not isinstance(task_queue, TaskQueue):
            raise TypeError("task_queue must be a TaskQueue")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._database = database
        self._tasks = task_service
        self._queue = task_queue
        self._clock = clock
        self._batch_size = validate_batch_size(batch_size)

    async def pending_project_ids(
        self, limit: int = DEFAULT_TASK_STOP_BATCH_SIZE
    ) -> tuple[uuid.UUID, ...]:
        """Projects whose stop request is open, oldest first (at most ``limit``).

        ``limit`` is an ``int`` from 1 to 500 (``InvalidProjectInputError``
        otherwise). A request stays listed until ``stop_project_tasks`` found
        nothing active left.
        """
        limit = validate_batch_size(limit, "limit")
        async with self._transaction() as session:
            found = await store.list_open_task_stops(session, limit)
        return tuple(found)

    async def stop_project_tasks(self, project_id: uuid.UUID) -> TaskStopResult:
        """Stop the active tasks of a project whose deletion began; see the module.

        ``ProjectNotFoundError`` for an unknown id (a Deleted project is known
        here: this is not a user-facing call). Idempotent: call it again until
        ``TaskStopResult.done``.
        """
        project_id = validate_uuid("project_id", project_id)
        async with self._transaction() as session:
            project = await store.get_project(session, project_id)
            if project is None:
                raise ProjectNotFoundError()
            task_ids = (
                await store.select_active_task_ids(
                    session, project_id, self._batch_size
                )
                if project.status in _STOPPING
                else []
            )
        stopped: list[uuid.UUID] = []
        entries = 0
        for task_id in task_ids:
            # The entry first: if this run stops here, the task is still active
            # and the next run repeats it (a cancelled task would not be listed).
            if await self._queue.cancel(task_id):
                entries += 1
            if await self._cancel(task_id):
                stopped.append(task_id)
        done = await self._finish(project_id)
        return TaskStopResult(project_id, tuple(stopped), entries, done)

    async def _cancel(self, task_id: uuid.UUID) -> bool:
        """Issue Cancel; ``False`` if the task was not (or not yet) stopped by it."""
        try:
            await self._tasks.execute(
                task_id,
                TaskCommand.CANCEL,
                actor=Actor.policy(),
                reason=STOP_REASON,
            )
        except (IllegalTransitionError, TaskNotFoundError):
            # It left the active states by itself since it was listed (failed,
            # completed, cancelled by a user): nothing left to stop.
            return False
        except TaskConflictError:
            # A concurrent writer changed it first. It stays active if that did
            # not end it, and ``_finish`` keeps the request open.
            return False
        return True

    async def _finish(self, project_id: uuid.UUID) -> bool:
        """Mark the request processed if no task is active; ``True`` when it is.

        One short transaction under ``FOR SHARE`` on the project row: the project
        cannot be restored or begun again between the check and the mark.
        """
        now = validate_instant("clock", self._clock())
        async with self._transaction() as session:
            project = await store.get_project_for_share(session, project_id)
            if project is None:
                raise ProjectNotFoundError()
            if project.status in _STOPPING and await store.has_active_task(
                session, project_id
            ):
                return False
            await store.mark_task_stop_processed(session, project_id, now=now)
            return True

    def _transaction(self) -> AbstractAsyncContextManager[AsyncSession]:
        return transaction(self._database, DEFAULT_LOCK_TIMEOUT_MS)


__all__ = ["STOP_REASON", "ProjectTaskStopper", "TaskStopResult"]
