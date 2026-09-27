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
   waiting, paused, evaluating; oldest first), by a plain read of ``tasks``
   (through ``ix_tasks_project_id_state``).
3. Stops each task in **ONE transaction** (see "One transaction per task"): the
   PAW-032 **Cancel** command through ``TaskService.execute`` with
   ``Actor.policy()`` and a fixed reason, the task's active **queue entry**
   (``TaskQueue.cancel_in``: the entry can no longer be claimed and a worker that
   holds it loses its lease) and a check that the project is still Pending deletion,
   made under ``SELECT ... FOR SHARE`` on the project row. Task state is never
   written directly: the state machine, the ``task_events`` history and the
   listeners of the task service all apply.
4. **Sweeps the queue by project.** Step 3 reaches a queue entry through a task
   that is still active. An entry can also sit behind a task that is TERMINAL: the
   task was cancelled or failed by someone else (a user, a worker) while the entry
   was still active, or another caller restarted a task and enqueued the new
   attempt and step 3 then ended it. Step 2 never lists such a task. So, after the
   loop, the stopper lists the active queue entries of the tasks of the project that
   are terminal (``queue_entries`` joined to ``tasks``; at most ``batch_size``) and
   cancels each one in a transaction of its own that holds the project row
   ``FOR SHARE`` and cancels the entry only IF THE TASK IS STILL TERMINAL
   (``TaskQueue.cancel_in(..., only_if_task_terminal=True)``, judged with the task
   row share-locked). The same sweep finds an entry that appeared after the request
   was processed (a rerun). An entry of a task that is still ACTIVE is left alone (a
   task that is not terminal keeps the entry it would run again with if its project
   were restored; step 3 of the next run cancels the task and the entry together).
   Decision 0008, section 8, items 7 and 8.
5. In one short transaction that holds ``SELECT ... FOR SHARE`` on the project
   row (so the project cannot be restored or changed in between), checks that no
   active task AND no active queue entry of the project's tasks is left and only
   then sets ``processed_at``. If any is left (the batch was full, a concurrent
   writer beat the command, a task became active again, or an entry was
   enqueued after the sweep) the request stays open and ``done`` is ``False``.

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
Decision 0008 records this choice (approved 2026-09-25).

One transaction per task (Issue #83)
------------------------------------
The Cancel and the cancel of the queue entry used to be two commits of two separate
parts, and every failure that could fall between them had to be reasoned about
(Decision 0008, section 8, items 5, 7 and 8). Now they are one: ``execute`` is
given ``in_transaction=<step>`` (``TaskService``), and the step runs in the Cancel's
own transaction, after the Cancel's writes and before the commit, in this order:

1. ``SELECT status FROM projects ... FOR SHARE`` (through
   ``transaction.share_lock_status``, a bounded wait): a project that is not Pending
   deletion / Deleted any more ends the step with a private error, the whole
   command is rolled back and the stopper stops the batch. Lock order is the task row
   (taken by ``execute``), then the project row, like the Project state gate of
   Retry and Restart.
2. ``TaskQueue.cancel_in(session, task_id)``: the task's active entry is cancelled in
   the same transaction.

What that closes:

* **A Restore racing the stopper.** A Restore holds the project row ``FOR UPDATE``.
  It either committed before the step (the step sees Archived and the Cancel does
  not happen) or waits for the stop's transaction (the task is cancelled while the
  project is still Pending deletion, and the Restore commits afterwards). There is
  no state in which "the read said Pending deletion" and "the Cancel committed in an
  Archived project" are both true, so no task of a live project is cancelled.
* **Cancel, Restart, entry cancel.** No caller can Restart the task between the
  Cancel and the cancel of its entry: a Restart needs the task row, which the stop's
  transaction holds until it commits. The entry that a Restart would inherit is
  cancelled together with the Cancel; the restarted task is queued without an entry
  and is enqueued afresh (the Project state gate refuses that in a project that is
  not Active). For a task that is already terminal (step 4) the entry cancel is
  conditional and share-locks the task row, so a Restart that committed first keeps
  its entry.
* **A crash between the two commits, and a Restore before the next run.** There is
  no second commit: the task is either cancelled with its entry or untouched with
  it, so a Restore that follows can never find a cancelled task with an active entry
  from this module, and a task that stays active keeps the entry it needs to run
  again. The processor needs no reconciliation step any more.

Not closed here: an entry that is left behind a terminal task by somebody else (a
user's Cancel, a worker's Fail) stays until the sweep of step 4, the orchestrator's
own cleanup (PAW-034) or a Restart finds it. The queue does not skip an entry
because its TASK is terminal (Decision 0020, C filters by the project's state: an
entry of a project that is not Active is never claimed, whatever its task's state).

Idempotent and re-runnable
--------------------------
A second call finds no active task and no active entry, changes nothing and returns
``TaskStopResult(project_id, (), 0, done=True)``; the first ``processed_at``
stays. Every step is either a state change that is itself idempotent or a read.
A task that leaves the active states between the list and the command (the
worker failed or finished it, a user cancelled it) raises ``IllegalTransitionError``
from the task service and is simply not counted (its queue entry, if it still has
one, is cancelled by the conditional cancel of step 4: the task is terminal); a
``TaskConflictError`` leaves the task AND its entry alone for the next run. Any
other error propagates, with the request still open and the task (if the error came
from the transaction) untouched.

What the caller must do (limits)
--------------------------------
* Keep calling until ``done``; a project with more than ``batch_size`` active
  tasks needs several calls.
* Build the ``TaskService`` and ``TaskQueue`` of the whole backend with the Project
  state gate (``ProjectStateGate``; both constructors REQUIRE one, Decision 0020,
  Issue #83): ``create_task`` / Retry / Restart / Start and ``enqueue`` then refuse a
  project that is not Active, in the transaction of their own write, serialised with
  Delete by the project row lock, and the queue does not claim the entries of such a
  project. The Authorizer already refuses ``project.task.run`` in Archived and
  Pending deletion (PAW-025), but only before the command; the gate is what holds
  when a Delete overlaps it. So nothing new can appear in a Pending deletion
  project after the request was processed, and the call is cheap and idempotent:
  the orchestrator can still call ``stop_project_tasks`` for the projects that
  are Pending deletion on its regular cycle (a safety net, no longer needed for
  correctness; it stops a task and cancels an entry that slipped in, also that of
  a task that is already terminal; tests:
  ``test_a_task_created_after_the_deletion_began_is_stopped_on_a_rerun``,
  ``test_an_entry_of_a_finished_task_is_found_by_project_on_a_rerun``).
* **A Restore during a batch.** The ids of a batch are listed in one read and a
  Restore can commit at any time in between. Each task is checked under the project
  row lock in its own transaction (step 3), so the loop stops at the first task whose
  transaction finds the project live (a Restore, or a restore followed by a NEW
  deletion: the state counts, not the request, so that project is stopped again).
  The tasks and entries stopped before the Restore stay cancelled (a task can be
  restarted once the project is Active again); none after it is touched, and none is
  stopped in a live project. A project that was restored is not stopped further:
  ``_finish`` finds it neither Pending deletion nor Deleted, marks the (moot)
  request processed and ``done`` is true. The bounded wait for the project row is
  ``DEFAULT_LOCK_TIMEOUT_MS`` (``ProjectBusyError``, the request stays open).

This class is Backend-internal: like ``ProjectService.purge_expired`` it takes
no actor and asks no Authorizer, and it writes no Audit event of its own (each
cancelled task has its ``task_events`` row). Errors carry no caller content.
"""

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto

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
from paw_backend.projects.transaction import share_lock_status, transaction
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


class _Outcome(Enum):
    """What the stop of one task did (see ``ProjectTaskStopper._stop_task``)."""

    CANCELLED = auto()  # this call ended the task (and cancelled its entry with it)
    TERMINAL = auto()  # the task was not active any more (or is gone)
    ACTIVE = auto()  # a concurrent writer won; the task may still be active
    LIVE = auto()  # the project is not (or no longer) Pending deletion / Deleted


class _ProjectIsLive(Exception):
    """Raised inside a stop's transaction: the project is not being deleted (any more).

    Private to this module. It rolls the transaction back (nothing of the stop is
    written) and is caught by the caller of that transaction.
    """


# The tasks of a project in these states are stopped. A Deleted project is a
# tombstone, but tasks it had when it was purged may still be active.
_STOPPING = (ProjectStatus.PENDING_DELETION, ProjectStatus.DELETED)


@dataclass(frozen=True, slots=True)
class TaskStopResult:
    """What one ``stop_project_tasks`` call did.

    ``stopped`` lists the tasks whose Cancel this call issued (oldest first).
    ``cancelled_entries`` counts the queue entries it cancelled (each in the
    transaction of the Cancel of its task, or, for an entry left behind a task that
    was terminal already, in a transaction of its own: see the module docstring,
    step 4). ``done`` is true when no task of the project was left active, no queue
    entry of its tasks is active and the request is marked processed (or the
    project needs no stop: it is Active or Archived); false means "call again".
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
            # The ids were listed a moment ago; a Restore may have committed since
            # (also while an earlier task was being stopped). Each task is checked
            # under the project's row lock in ITS OWN transaction, so the rest of
            # the batch is left alone as soon as the project is live.
            outcome, entry = await self._stop_task(project_id, task_id)
            if outcome is _Outcome.LIVE:
                break
            if outcome is _Outcome.CANCELLED:
                stopped.append(task_id)
            if entry:
                entries += 1
        entries += await self._cancel_stray_entries(project_id)
        done = await self._finish(project_id)
        return TaskStopResult(project_id, tuple(stopped), entries, done)

    async def _stop_task(
        self, project_id: uuid.UUID, task_id: uuid.UUID
    ) -> tuple[_Outcome, bool]:
        """Cancel the task and its entry in ONE transaction; ``(outcome, entry)``.

        ``TaskService.execute`` runs the Cancel and, in the same transaction after
        the Cancel's writes, the step below: it locks the project row ``FOR SHARE``
        (``_Outcome.LIVE`` if the project is not being deleted, which rolls the
        whole command back) and cancels the task's active entry. So either the task
        is cancelled together with its entry, or neither is touched: a crash, an
        error, a cancelled stopper or a Restore can never leave a cancelled task with
        an active entry, or an active task without its entry.

        A task that is not active any more (``IllegalTransitionError``) has its entry
        cancelled by ``_cancel_entry_of_finished_task`` (under the project lock, only
        if the task is still terminal); a ``TaskConflictError`` (a concurrent writer
        changed the task first) leaves the task and its entry alone.
        """
        entry_cancelled = False

        async def cancel_with_the_entry(
            session: AsyncSession, task: uuid.UUID, project: uuid.UUID
        ) -> None:
            nonlocal entry_cancelled
            await self._require_stopping(session, project)
            entry_cancelled = await self._queue.cancel_in(session, task)

        try:
            await self._tasks.execute(
                task_id,
                TaskCommand.CANCEL,
                actor=Actor.policy(),
                reason=STOP_REASON,
                in_transaction=cancel_with_the_entry,
            )
        except _ProjectIsLive:
            return _Outcome.LIVE, False
        except (IllegalTransitionError, TaskNotFoundError):
            # It left the active states by itself since it was listed (failed,
            # completed, cancelled by a user): nothing left to stop.
            return await self._cancel_entry_of_finished_task(project_id, task_id)
        except TaskConflictError:
            # A concurrent writer changed it first. It stays active if that did
            # not end it, and ``_finish`` keeps the request open.
            return _Outcome.ACTIVE, False
        return _Outcome.CANCELLED, entry_cancelled

    async def _cancel_entry_of_finished_task(
        self, project_id: uuid.UUID, task_id: uuid.UUID
    ) -> tuple[_Outcome, bool]:
        """Cancel the active entry of a task that is terminal, if it still is.

        One transaction that holds the project row ``FOR SHARE`` (so a Restore cannot
        commit between the check and the cancel: ``_Outcome.LIVE`` if the project is
        not being deleted) and cancels the entry only if the TASK is still Completed,
        Failed or Cancelled, judged with the task row share-locked
        (``cancel_in(..., only_if_task_terminal=True)``): a Restart that committed
        meanwhile keeps its entry.
        """
        try:
            async with self._transaction() as session:
                await self._require_stopping(session, project_id)
                cancelled = await self._queue.cancel_in(
                    session, task_id, only_if_task_terminal=True
                )
        except _ProjectIsLive:
            return _Outcome.LIVE, False
        return _Outcome.TERMINAL, cancelled

    async def _require_stopping(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """Lock the project row ``FOR SHARE``; ``_ProjectIsLive`` unless it is stopping.

        Pending deletion (or Deleted) is the only state whose tasks this module
        stops, and the share lock keeps it so until the caller's transaction ends:
        Restore, Archive and Delete hold the row ``FOR UPDATE``. The wait for a
        writer is bounded (``ProjectBusyError``).
        """
        status = await share_lock_status(session, project_id, DEFAULT_LOCK_TIMEOUT_MS)
        if status is None:
            raise ProjectNotFoundError()
        if status not in _STOPPING:
            raise _ProjectIsLive

    async def _cancel_stray_entries(self, project_id: uuid.UUID) -> int:
        """Cancel the active entries behind terminal tasks of the project; count them.

        Found through the project, not through the state of the task: a raced
        Restart or a task ended by somebody else can leave an active entry behind a
        terminal task (module docstring, step 4). The entry of a task that is still
        active is not touched (``terminal_tasks_only``). The project is read first;
        each entry is then cancelled by ``_cancel_entry_of_finished_task`` under the
        project row lock and the task's row lock, so a project that was restored
        since (or during the sweep) keeps the entries not cancelled yet and a
        restarted task keeps its entry. At most ``batch_size`` entries; ``_finish``
        keeps the request open if more remain.
        """
        async with self._transaction() as session:
            project = await store.get_project(session, project_id)
            if project is None:
                raise ProjectNotFoundError()
            task_ids = (
                await store.select_active_entry_task_ids(
                    session, project_id, self._batch_size, terminal_tasks_only=True
                )
                if project.status in _STOPPING
                else []
            )
        cancelled = 0
        for task_id in task_ids:
            outcome, entry = await self._cancel_entry_of_finished_task(
                project_id, task_id
            )
            if outcome is _Outcome.LIVE:
                break
            if entry:
                cancelled += 1
        return cancelled

    async def _finish(self, project_id: uuid.UUID) -> bool:
        """Mark the request processed if nothing is active; ``True`` when it is.

        Nothing active: no task of the project in an active state and no active
        queue entry of any of its tasks (also of a terminal one: the entry that a
        raced Restart enqueued after the sweep is caught here, and the next run
        cancels it). One short transaction under ``FOR SHARE`` on the project
        row: the project cannot be restored or begun again between the check and
        the mark.
        """
        now = validate_instant("clock", self._clock())
        async with self._transaction() as session:
            project = await store.get_project_for_share(session, project_id)
            if project is None:
                raise ProjectNotFoundError()
            if project.status in _STOPPING and (
                await store.has_active_task(session, project_id)
                or await store.has_active_queue_entry(session, project_id)
            ):
                return False
            await store.mark_task_stop_processed(session, project_id, now=now)
            return True

    def _transaction(self) -> AbstractAsyncContextManager[AsyncSession]:
        return transaction(self._database, DEFAULT_LOCK_TIMEOUT_MS)


__all__ = ["STOP_REASON", "ProjectTaskStopper", "TaskStopResult"]
