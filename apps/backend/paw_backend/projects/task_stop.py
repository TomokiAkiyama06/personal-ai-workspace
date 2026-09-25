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
3. For each (after reading the project again: see "Restore during a batch"):
   issues the PAW-032 **Cancel** command through ``TaskService.execute`` with
   ``Actor.policy()`` and a fixed reason, and only THEN cancels its active
   **queue entry** (``TaskQueue.cancel``: the entry can no longer be claimed and a
   worker that holds it loses its lease). Task state is never written directly:
   the state machine, the ``task_events`` history and the listeners of the task
   service all apply. The order (task first) is what makes the two commits safe to
   interrupt, see "Cancel and entry: order and interruption".
4. **Sweeps the queue by project.** Steps 2-3 reach a queue entry only through a
   task that is still active. A concurrent caller can leave an entry behind a
   task that is TERMINAL: it cancels the task, restarts it (Restart) and
   enqueues the new attempt, and the Cancel of step 3 then ends the restarted
   task; or an interrupted step 3 (see below) cancelled the task but not its
   entry. Step 2 never lists such a task again. So, after the loop, the stopper
   lists the active queue entries of the tasks of the project that are terminal
   (``queue_entries`` joined to ``tasks``; at most ``batch_size``) and cancels
   each one with ``TaskQueue.cancel``. The same sweep also finds an entry that
   appeared after the request was processed (a rerun). An entry of a task that is
   still ACTIVE is left alone (a task that is not terminal keeps the entry it
   would run again with if its project were restored; step 3 of the next run
   cancels the task and then the entry). The sweep re-reads the project first,
   and again before EACH entry, and does nothing for a project that is not (or no
   longer) Pending deletion or Deleted. The queue's state machine is not changed:
   an entry of a task that is already terminal is only cancelled (a worker that
   still held it loses its lease). Decision 0008, section 8, items 7 and 8.
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
Decision 0008 records this choice.

Cancel and entry: order and interruption
-----------------------------------------
The Cancel and the cancel of the queue entry are two commits of two separate parts
(they cannot share a transaction without a change of the task or queue lane), so a
crash, an error or a cancelled stopper can fall between them, and a Restore can
commit there. The order decides what such a gap leaves behind:

* Entry first (the order of the first implementations): the task is still active
  (queued) and its ONLY entry is cancelled. While the project stays Pending
  deletion the next run finds the task and repeats. But a Restore in the gap
  makes the project live: the next run then touches nothing, marks the request
  processed, and the queued task is never claimed again (it has no entry, and a
  queued task without an entry is also the normal state before it is first
  enqueued, so nothing can tell them apart afterwards).
* Task first (this module): a task that is still active always keeps its entry,
  whatever fails or is interrupted (an error of the Cancel, a ``TaskConflictError``,
  a cancelled stopper, a Restore in between). The entry is cancelled only when the
  task is terminal (the Cancel ended it, or it was terminal already). The gap that
  is left is "terminal task, active entry": while the project is Pending deletion
  the sweep of step 4 finds the entry by project on the next run.

A gap after the Cancel is also reconciled in the same call, by STATE, never by
outcome: if the Cancel or the entry cancel raises anything (including a
cancellation, or an interruption of a transition listener, which runs after the
commit), ``_reconcile_entry`` reads the task once and, when it is terminal now,
cancels its entry (the same idempotent queue cancel), then re-raises the original
error. A task that is still active is never touched by it. It runs in the
``except`` of the interruption, so one cancellation does not stop it, and it is
bounded by ``RECONCILE_TIMEOUT_S`` (another cancellation, a timeout or a failure of
the reconciliation itself never replaces the original error).

What remains (Decision 0008, section 8, item 8; recorded, not closed):

* A concurrent ``Restart`` of the task between the Cancel and the entry cancel (the
  entry cancel does not check the task again: ``TaskQueue.cancel`` has no
  condition, and the check and the cancel cannot be one transaction) leaves the
  restarted task active without an entry. While the project is Pending deletion the
  next run finds it (it is active) and stops it; it needs a concurrent Restore as
  well to matter. This is the race of ``TaskService`` / ``TaskQueue`` not looking at
  the project (Decision 0008, section 8, item 4).
* The process dies (or the reconciliation fails) between the two commits AND a
  Restore commits before the next run. The project is live then, the request is
  moot and is marked processed, and a CANCELLED task keeps an active entry: an
  entry of a terminal task that a worker claims and then cannot start (``start``
  is refused), and that the stopper does not touch in a live project (it must not
  touch anything there: a completing worker holds a claimed entry of a completed
  task for a moment). The task itself is cancelled, which is the accepted "one
  task may be cancelled" window of "Restore during a batch" (it can be
  restarted).

Closing both needs the task and queue lanes: one transaction for the two commands
(or a conditional queue cancel), or a queue rule that a claim skips (cancels) the
entry of a terminal task.

Idempotent and re-runnable
--------------------------
A second call finds no active task and no active entry, changes nothing and returns
``TaskStopResult(project_id, (), 0, done=True)``; the first ``processed_at``
stays. Every step is either a state change that is itself idempotent or a read.
A task that leaves the active states between the list and the command (the
worker failed or finished it, a user cancelled it) raises ``IllegalTransitionError``
from the task service and is simply not counted (its queue entry, if it still has
one, is cancelled: the task is terminal); a ``TaskConflictError`` leaves the task
AND its entry alone for the next run. Any other error propagates, with the request
still open (and the interruption reconciled, see above).

What the caller must do (limits)
--------------------------------
* Keep calling until ``done``; a project with more than ``batch_size`` active
  tasks needs several calls.
* ``TaskService.create_task`` (and Retry / Restart of an earlier task) and
  ``TaskQueue.enqueue`` do not look at the project, so a task whose creation was
  authorized just before the deletion began (or a queue entry for a task of the
  project) can appear **after** the request was processed. The Authorizer
  already refuses ``project.task.run`` in Archived and Pending deletion (PAW-025);
  only that race remains. Until the task service closes it (Decision 0008
  proposes a gate in the same transaction as the insert and the enqueue), the
  orchestrator should also call ``stop_project_tasks`` for the projects that are
  Pending deletion on its regular cycle: the call stops such a task and cancels
  such an entry, also that of a task that is already terminal (tests:
  ``test_a_task_created_after_the_deletion_began_is_stopped_on_a_rerun``,
  ``test_an_entry_of_a_finished_task_is_found_by_project_on_a_rerun``).
* **Restore during a batch.** The ids of a batch are listed in one read, and a
  batch is up to ``batch_size`` (default 100) tasks or entries; a Restore can
  commit at any time in between. So the project is read again before EACH task
  (step 3) and before each stray entry (step 4), and the loop stops at the first
  read that shows a project that is not Pending deletion / Deleted any more (a
  Restore, or a restore followed by a NEW deletion: the state counts, not the
  request, so that project is stopped again). The tasks and entries cancelled
  before the Restore stay cancelled (a task can be restarted); none after it is
  touched. A project that was restored is not stopped further: ``_finish`` finds
  it neither Pending deletion nor Deleted, marks the (moot) request processed and
  ``done`` is true. Cost: one extra plain read (no lock) per task and per entry.
  What remains: the read and the commands are not one atomic step, so a Restore
  that commits after the read of a task and before the end of ITS commands (the
  Cancel and the cancel of its entry: the entry is cancelled after the Cancel, so
  a restored task that the Cancel did not end keeps its entry) still lets that ONE
  task (or one entry) be cancelled. Closing it needs the task service and the
  queue to run their commands under ``FOR SHARE`` on the project row (Decision
  0008, section 5). Holding that lock here, in a transaction of its own around
  the two calls, was rejected: it would take a second pooled connection per
  stopper, and every Restore would wait (``ProjectBusyError`` after the lock
  timeout) for as long as the task service and its listeners, which this module
  does not bound, take.
* ``tasks.project_id`` has no index (PAW-032): the list reads ``tasks`` by a
  sequential scan until the task lane adds one on ``(project_id, state)``.

This class is Backend-internal: like ``ProjectService.purge_expired`` it takes
no actor and asks no Authorizer, and it writes no Audit event of its own (each
cancelled task has its ``task_events`` row). Errors carry no caller content.
"""

import asyncio
import contextlib
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
    RECONCILE_TIMEOUT_S,
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


class _Outcome(Enum):
    """What the Cancel command did to a task (see ``ProjectTaskStopper._cancel``)."""

    CANCELLED = auto()  # this call ended the task
    TERMINAL = auto()  # the task was not active any more (or is gone)
    ACTIVE = auto()  # a concurrent writer won; the task may still be active


# The tasks of a project in these states are stopped. A Deleted project is a
# tombstone, but tasks it had when it was purged may still be active.
_STOPPING = (ProjectStatus.PENDING_DELETION, ProjectStatus.DELETED)


@dataclass(frozen=True, slots=True)
class TaskStopResult:
    """What one ``stop_project_tasks`` call did.

    ``stopped`` lists the tasks whose Cancel this call issued (oldest first).
    ``cancelled_entries`` counts the queue entries it cancelled (each after its
    task was terminal; also entries left behind a terminal task, see the module
    docstring, step 4). ``done``
    is true when no task of the project was left active, no queue entry of its
    tasks is active and the request is marked processed (or the project needs no
    stop: it is Active or Archived); false means "call again".
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
            # (also while an earlier task was being cancelled). One read per task:
            # the rest of the batch is left alone as soon as the project is live.
            if not await self._is_stopping(project_id):
                break
            # The Cancel first, then the entry: a task that is still active keeps
            # its entry whatever happens in between (module docstring, "Cancel and
            # entry: order and interruption").
            cancelled, entry = await self._stop_task(task_id)
            if cancelled:
                stopped.append(task_id)
            if entry:
                entries += 1
        entries += await self._cancel_stray_entries(project_id)
        done = await self._finish(project_id)
        return TaskStopResult(project_id, tuple(stopped), entries, done)

    async def _stop_task(self, task_id: uuid.UUID) -> tuple[bool, bool]:
        """Cancel the task, then its entry; ``(task cancelled by us, entry cancelled)``.

        The entry is cancelled only once the task is terminal (the Cancel ended
        it, or it already was): a task that stays active (``TaskConflictError``)
        keeps its entry. If anything is raised, also a cancellation, the task is
        read once (``_reconcile_entry``) and its entry cancelled if the task is
        terminal by now, then the original error propagates.
        """
        try:
            outcome = await self._cancel(task_id)
            if outcome is _Outcome.ACTIVE:
                return False, False
            return outcome is _Outcome.CANCELLED, await self._queue.cancel(task_id)
        except BaseException:
            await self._reconcile_entry(task_id)
            raise

    async def _reconcile_entry(self, task_id: uuid.UUID) -> None:
        """After an interrupted ``_stop_task``: cancel the entry of a terminal task.

        Decided by the STATE of the task, read now (plain read), never by how far
        the interrupted call got: the Cancel may have committed although it raised
        (a transition listener runs after the commit and can be cancelled), and the
        entry cancel is idempotent. A task that is still active keeps its entry.
        Runs inside the ``except`` of the interruption (one cancellation has been
        delivered already), is bounded by ``RECONCILE_TIMEOUT_S``, and never
        replaces the original error: whatever it raises (another database error, a
        timeout) is dropped, and the entry then stays for the sweep of the next run
        while the project is Pending deletion. A second cancellation is not
        suppressed.
        """
        with contextlib.suppress(Exception):
            async with asyncio.timeout(RECONCILE_TIMEOUT_S):
                async with self._transaction() as session:
                    terminal = await store.is_task_terminal(session, task_id)
                if terminal:
                    await self._queue.cancel(task_id)

    async def _cancel(self, task_id: uuid.UUID) -> _Outcome:
        """Issue Cancel and say what it did to the task."""
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
            return _Outcome.TERMINAL
        except TaskConflictError:
            # A concurrent writer changed it first. It stays active if that did
            # not end it, and ``_finish`` keeps the request open.
            return _Outcome.ACTIVE
        return _Outcome.CANCELLED

    async def _cancel_stray_entries(self, project_id: uuid.UUID) -> int:
        """Cancel the active entries behind terminal tasks of the project; count them.

        Found through the project, not through the state of the task: a raced
        Restart or an interrupted stop can leave an active entry behind a terminal
        task (module docstring, step 4). The entry of a task that is still active
        is not touched (``terminal_tasks_only``). The project is read again first
        and before EACH entry (``_is_stopping``), so a project that was restored
        since the first read, or during the sweep, keeps the entries not cancelled
        yet. At most ``batch_size`` entries; ``_finish`` keeps the request open if
        more remain.
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
            # Same as in ``stop_project_tasks``: one read per entry, so a Restore
            # that commits during the sweep keeps the entries not yet cancelled.
            if not await self._is_stopping(project_id):
                break
            if await self._queue.cancel(task_id):
                cancelled += 1
        return cancelled

    async def _is_stopping(self, project_id: uuid.UUID) -> bool:
        """Whether the project is still Pending deletion (or Deleted): read now.

        A plain read in its own short transaction (it waits for no lock), made
        before EACH task and each stray entry: the loops must not go on with ids
        that were listed while the project was Pending deletion once a Restore
        has committed. It does not lock the project (see the module docstring for
        the window that stays and why).
        """
        async with self._transaction() as session:
            project = await store.get_project(session, project_id)
        if project is None:
            raise ProjectNotFoundError()
        return project.status in _STOPPING

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
