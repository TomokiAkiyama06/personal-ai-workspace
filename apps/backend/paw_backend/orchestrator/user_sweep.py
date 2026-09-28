"""Stopping the tasks of users whose deletion began, on a schedule (Issue #127).

``REQUIREMENTS.md`` ("User Lifecycle", "User Deletion Retention"): when a user is
deleted, running Agents are safe-stopped and no new Agent runs for them. Decision
0033 (section 3) left the stop to a later issue; Decision 0043 (Proposed) records
how it is done here.

``UserLifecycleService.delete_user`` cannot stop tasks itself (the task service and
the queue are separate, and a worker cannot be reached from inside the deletion's
transaction), and what it does in that transaction already stops everything NEW:
the user's sessions end (``account_closed``), ``SessionPrincipalProvider`` treats a
user who is not ``active`` as anonymous, and ``DatabasePrincipalDirectory`` refuses
to resolve such a user for an Agent's delegation. What is left is what was already
there: the user's own tasks that are queued or running. This module stops them.

It works from the STATE, not from a request row: every cycle lists the users who are
``pending_deletion`` (or ``deleted``) and still have an active task they created, or
an active queue entry of one of their tasks (:meth:`UserTaskStopper.stopping_user_ids`),
and stops them (:meth:`UserTaskStopper.stop_user_tasks`). So no migration and no
outbox are needed, a task that appears later (a race with the deletion) is found by
the next cycle, and a user who was restored is simply not listed any more.

What one ``stop_user_tasks`` call does (the shape of ``ProjectTaskStopper``, PAW-026):

1. Reads the user. Only a user who is Pending deletion (or Deleted) has tasks
   stopped; for an ``active`` user (restored meanwhile) nothing is touched.
2. Lists at most ``batch_size`` active tasks the user created (queued, running,
   waiting, paused, evaluating; oldest first).
3. Stops each in ONE transaction: the PAW-032 **Cancel** through
   ``TaskService.execute`` (``Actor.policy()``, :data:`STOP_REASON`) and, in the same
   transaction (``in_transaction``), ``SELECT ... FOR SHARE`` on the user's row (a
   Restore holds it ``FOR NO KEY UPDATE``, so the two are serialised: a task is never
   cancelled after a Restore committed) and the cancel of the task's active queue
   entry (``TaskQueue.cancel_in``). Task state is never written directly.
4. Cancels the active queue entries left behind the user's tasks that are already
   terminal (a raced Restart), each under the same user lock and only if the task is
   still terminal.
5. Reports ``done`` when no active task and no active entry of the user is left.

Cancel, not Stop Now: Cancel is the graceful stop (the worker finishes its current
step at a safe boundary and cannot start another; branch, worktree and partial
results are kept), valid in every active state; Decision 0008 chose it for projects
for the same reasons. A cancelled task stays cancelled if the user is restored (the
restored user, or a project Manager, can Restart it).

Tasks of the user in a SHARED project are stopped as well: "実行中Agentの安全停止" is
about the deleted person's Agents, wherever they run.

Every cancelled task is audited (``auth.user.task_stop``, resource the task, its
project; no actor: the scheduled system job) after its transaction committed, best
effort (the ``task_events`` row of the Cancel is the durable record), the way the
connection reaper audits.

The loop (:class:`UserTaskStopLoop`) is the project sweep's: a first cycle shortly
after the start, then every ``interval_seconds`` (less while a user is unfinished;
a back-off after a failed cycle); one user that raises does not stop the others;
``stop`` ends it. Several Backend processes may each run one (the commands are
idempotent; a conflict is left for the next cycle).
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg.errors
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.audit import AuthAction, AuthReason
from paw_backend.authz import AuditEvent, AuditSink, PostgresAuditSink
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
from paw_backend.tasks import (
    TERMINAL_STATES,
    Actor,
    IllegalTransitionError,
    ProjectGate,
    TaskCommand,
    TaskConflictError,
    TaskNotFoundError,
    TaskService,
    TaskState,
)
from paw_backend.tasks.queueing import ACTIVE_QUEUE_STATUSES, TaskQueue
from paw_backend.tools import ApprovalService, PostgresApprovalStore
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger(__name__)

# Recorded as the reason of the Cancel event of every task this module stops.
STOP_REASON = "User deletion started"
ACTION_TASK_STOP = AuthAction.USER_TASK_STOP.value
REASON_USER_DELETION = AuthReason.USER_DELETION.value
AUDIT_TIMEOUT_SECONDS = 3.0
# How long the user's row lock may be waited for inside a stop's transaction.
USER_LOCK_TIMEOUT_MS = 3000
DEFAULT_BATCH_SIZE = 50

# The waits of the loop (the project sweep's values).
CATCH_UP_DELAY_SECONDS = 5.0
RETRY_BASE_SECONDS = 10.0
FIRST_CYCLE_DELAY_SECONDS = 5.0
_MAX_BACKOFF_STEPS = 20

# A user in one of these states has their tasks stopped. ``deleted``: the erasure
# refuses to finish while a task is active, but a race can still leave one.
STOPPING_STATUSES = ("pending_deletion", "deleted")


def _literals(values) -> str:
    """``'a', 'b'`` of a closed set of enum constants (never caller input)."""
    return ", ".join(f"'{value}'" for value in sorted(values))


_ACTIVE_TASK_STATES = _literals(
    state.value for state in set(TaskState) - TERMINAL_STATES
)
_TERMINAL_TASK_STATES = _literals(state.value for state in TERMINAL_STATES)
_ACTIVE_ENTRIES = _literals(status.value for status in ACTIVE_QUEUE_STATUSES)
_STOPPING = _literals(STOPPING_STATUSES)

_STOPPING_USERS = text(
    f"""
    SELECT u.id FROM users u
     WHERE u.status IN ({_STOPPING})
       AND (EXISTS (SELECT 1 FROM tasks t
                     WHERE t.created_by = u.id
                       AND t.state IN ({_ACTIVE_TASK_STATES}))
            OR EXISTS (SELECT 1 FROM tasks t
                         JOIN queue_entries q ON q.task_id = t.id
                        WHERE t.created_by = u.id
                          AND q.status IN ({_ACTIVE_ENTRIES})))
     ORDER BY u.id
     LIMIT :limit
    """
)
_ACTIVE_TASKS = text(
    f"SELECT id, project_id FROM tasks WHERE created_by = :user "
    f"AND state IN ({_ACTIVE_TASK_STATES}) ORDER BY created_at, id LIMIT :limit"
)
_STRAY_ENTRY_TASKS = text(
    f"""
    SELECT t.id, t.project_id FROM tasks t
      JOIN queue_entries q ON q.task_id = t.id
     WHERE t.created_by = :user AND t.state IN ({_TERMINAL_TASK_STATES})
       AND q.status IN ({_ACTIVE_ENTRIES})
     ORDER BY q.id
     LIMIT :limit
    """
)
_ANYTHING_ACTIVE = text(
    f"""
    SELECT EXISTS (SELECT 1 FROM tasks t WHERE t.created_by = :user
                      AND t.state IN ({_ACTIVE_TASK_STATES}))
        OR EXISTS (SELECT 1 FROM tasks t JOIN queue_entries q ON q.task_id = t.id
                    WHERE t.created_by = :user AND q.status IN ({_ACTIVE_ENTRIES}))
    """
)
_STATUS = text("SELECT status FROM users WHERE id = :id")
_STATUS_FOR_SHARE = text("SELECT status FROM users WHERE id = :id FOR SHARE")


class UserBusyError(Exception):
    """The user's row stayed locked longer than ``USER_LOCK_TIMEOUT_MS``.

    The stop's transaction was rolled back; the next cycle tries again.
    """


class _UserIsLive(Exception):
    """Raised inside a stop's transaction: the user is not being deleted (any more)."""


@dataclass(frozen=True, slots=True)
class UserTaskStopResult:
    """What one ``stop_user_tasks`` call did (``ProjectTaskStopper``'s shape).

    ``stopped``: the tasks this call cancelled, oldest first. ``cancelled_entries``:
    the queue entries it cancelled. ``done``: nothing of the user is left active (or
    the user is not being deleted); ``False`` means "call again".
    """

    user_id: uuid.UUID
    stopped: tuple[uuid.UUID, ...]
    cancelled_entries: int
    done: bool


async def _lock_user_for_share(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    """``FOR SHARE`` on the user's row in the caller's transaction; its status.

    The wait is bounded for this one statement (``UserBusyError``) and the caller's
    own ``lock_timeout`` is put back afterwards.
    """
    previous = (
        await session.execute(select(func.current_setting("lock_timeout")))
    ).scalar_one()
    await session.execute(
        select(func.set_config("lock_timeout", str(USER_LOCK_TIMEOUT_MS), True))
    )
    try:
        status = (
            await session.execute(_STATUS_FOR_SHARE, {"id": user_id})
        ).scalar_one_or_none()
    except DBAPIError as error:
        # Only the type of the driver's error is read, never its text.
        if isinstance(error.orig, psycopg.errors.LockNotAvailable):
            raise UserBusyError() from None
        raise
    await session.execute(select(func.set_config("lock_timeout", previous, True)))
    return status


class UserTaskStopper:
    """Cancels the active tasks of users whose deletion began (module docstring)."""

    def __init__(
        self,
        database: Database,
        task_service: TaskService,
        task_queue: TaskQueue,
        audit_sink: AuditSink,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if not isinstance(task_service, TaskService):
            raise TypeError("task_service must be a TaskService")
        if not isinstance(task_queue, TaskQueue):
            raise TypeError("task_queue must be a TaskQueue")
        require_async_method(audit_sink, "record", 1)
        self._database = database
        self._tasks = task_service
        self._queue = task_queue
        self._sink = audit_sink
        self._batch_size = check_int("batch_size", batch_size, minimum=1, maximum=500)

    async def stopping_user_ids(
        self, limit: int = DEFAULT_PROJECTS_PER_CYCLE
    ) -> tuple[uuid.UUID, ...]:
        """Users being deleted who still have something active, in id order."""
        check_int("limit", limit, minimum=1, maximum=MAX_PROJECTS_PER_CYCLE)
        async with self._database.session() as session, session.begin():
            rows = await session.execute(_STOPPING_USERS, {"limit": limit})
            return tuple(rows.scalars())

    async def stop_user_tasks(self, user_id: uuid.UUID) -> UserTaskStopResult:
        """Stop the user's active tasks (see the module). Idempotent: call it again
        until ``done``. An unknown user is ``done`` at once (nothing to stop)."""
        check_uuid("user_id", user_id)
        async with self._database.session() as session, session.begin():
            status = (await session.execute(_STATUS, {"id": user_id})).scalar()
            tasks = (
                (
                    await session.execute(
                        _ACTIVE_TASKS, {"user": user_id, "limit": self._batch_size}
                    )
                ).all()
                if status in STOPPING_STATUSES
                else []
            )
        if status not in STOPPING_STATUSES:
            return UserTaskStopResult(user_id, (), 0, True)
        stopped: list[uuid.UUID] = []
        entries = 0
        correlation_id = uuid.uuid4()
        for task_id, project_id in tasks:
            outcome, entry = await self._stop_task(user_id, task_id)
            if outcome == "live":
                break
            if outcome == "cancelled":
                stopped.append(task_id)
                await self._audit(correlation_id, task_id, project_id)
            entries += entry
        entries += await self._cancel_stray_entries(user_id)
        done = await self._finish(user_id)
        return UserTaskStopResult(user_id, tuple(stopped), entries, done)

    async def _stop_task(
        self, user_id: uuid.UUID, task_id: uuid.UUID
    ) -> tuple[str, bool]:
        """Cancel the task and its entry in ONE transaction; ``(outcome, entry)``."""
        entry_cancelled = False

        async def cancel_with_the_entry(
            session: AsyncSession, task: uuid.UUID, _project: uuid.UUID
        ) -> None:
            nonlocal entry_cancelled
            await self._require_stopping(session, user_id)
            entry_cancelled = await self._queue.cancel_in(session, task)

        try:
            await self._tasks.execute(
                task_id,
                TaskCommand.CANCEL,
                actor=Actor.policy(),
                reason=STOP_REASON,
                in_transaction=cancel_with_the_entry,
            )
        except _UserIsLive:
            return "live", False
        except (IllegalTransitionError, TaskNotFoundError):
            # It left the active states by itself since it was listed.
            return await self._cancel_entry_of_finished_task(user_id, task_id)
        except TaskConflictError:
            return "active", False
        return "cancelled", entry_cancelled

    async def _cancel_entry_of_finished_task(
        self, user_id: uuid.UUID, task_id: uuid.UUID
    ) -> tuple[str, bool]:
        try:
            async with self._database.session() as session, session.begin():
                await self._require_stopping(session, user_id)
                cancelled = await self._queue.cancel_in(
                    session, task_id, only_if_task_terminal=True
                )
        except _UserIsLive:
            return "live", False
        return "terminal", cancelled

    async def _require_stopping(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        if await _lock_user_for_share(session, user_id) not in STOPPING_STATUSES:
            raise _UserIsLive

    async def _cancel_stray_entries(self, user_id: uuid.UUID) -> int:
        async with self._database.session() as session, session.begin():
            rows = (
                await session.execute(
                    _STRAY_ENTRY_TASKS, {"user": user_id, "limit": self._batch_size}
                )
            ).all()
        cancelled = 0
        for task_id, _project_id in rows:
            outcome, entry = await self._cancel_entry_of_finished_task(user_id, task_id)
            if outcome == "live":
                break
            cancelled += entry
        return cancelled

    async def _finish(self, user_id: uuid.UUID) -> bool:
        async with self._database.session() as session, session.begin():
            if await _lock_user_for_share(session, user_id) not in STOPPING_STATUSES:
                return True
            left = (
                await session.execute(_ANYTHING_ACTIVE, {"user": user_id})
            ).scalar_one()
            return not left

    async def _audit(
        self,
        correlation_id: uuid.UUID,
        task_id: uuid.UUID,
        project_id: uuid.UUID | None,
    ) -> None:
        try:
            event = AuditEvent(
                event_id=uuid.uuid4(),
                correlation_id=correlation_id,
                occurred_at=datetime.now(UTC),
                actor_id=None,
                actor_role=None,
                action=ACTION_TASK_STOP,
                resource_kind="task",
                resource_id=task_id,
                project_id=project_id,
                decision="allow",
                reason=REASON_USER_DELETION,
            )
            async with asyncio.timeout(AUDIT_TIMEOUT_SECONDS):
                await self._sink.record(event)
        except Exception as error:
            logger.error(
                "Auditing a task stopped for a user deletion failed (%s)",
                error_class_of(error),
            )


@dataclass(frozen=True, slots=True)
class UserSweepReport:
    """What one cycle did. ``failed`` counts the users whose stop raised."""

    users: int
    stopped_tasks: int
    cancelled_entries: int
    unfinished: int
    failed: int


class UserTaskStopLoop:
    """Runs :class:`UserTaskStopper` on a schedule (the project sweep's shape)."""

    def __init__(
        self,
        stopper: UserTaskStopper,
        *,
        interval_seconds: float = DEFAULT_STOP_INTERVAL_SECONDS,
        users_per_cycle: int = DEFAULT_PROJECTS_PER_CYCLE,
        rounds_per_user: int = DEFAULT_ROUNDS_PER_PROJECT,
        clock: Clock | None = None,
    ) -> None:
        require_async_method(stopper, "stopping_user_ids", 1)
        require_async_method(stopper, "stop_user_tasks", 1)
        self._interval = check_seconds(
            "interval_seconds",
            interval_seconds,
            minimum=MIN_STOP_INTERVAL_SECONDS,
            maximum=MAX_STOP_INTERVAL_SECONDS,
        )
        self._per_cycle = check_int(
            "users_per_cycle",
            users_per_cycle,
            minimum=1,
            maximum=MAX_PROJECTS_PER_CYCLE,
        )
        self._rounds = check_int(
            "rounds_per_user",
            rounds_per_user,
            minimum=1,
            maximum=MAX_ROUNDS_PER_PROJECT,
        )
        clock = clock or SystemClock()
        require_async_method(clock, "sleep", 1)
        self._stopper = stopper
        self._clock = clock
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        """Ask the loop to end at its next suspension point."""
        self._stopping.set()

    async def run_cycle(self) -> UserSweepReport:
        """One cycle: every listed user gets up to ``rounds_per_user`` calls."""
        ids = await self._stopper.stopping_user_ids(self._per_cycle)
        stopped = entries = unfinished = failed = 0
        for user_id in ids:
            if self._stopping.is_set():
                break
            try:
                for _ in range(self._rounds):
                    result = await self._stopper.stop_user_tasks(user_id)
                    stopped += len(result.stopped)
                    entries += result.cancelled_entries
                    if result.done:
                        break
            except Exception as error:  # one user must not stop the others
                failed += 1
                logger.error(
                    "Stopping a deleted user's tasks failed (%s)", error_class_of(error)
                )
                continue
            unfinished += not result.done
        return UserSweepReport(len(ids), stopped, entries, unfinished, failed)

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
                    "User task-stop cycle failed (%s); retrying in %.0f s",
                    error_class_of(error),
                    delay,
                )
            else:
                failures = 0
                delay = (
                    min(self._interval, CATCH_UP_DELAY_SECONDS)
                    if report.unfinished or report.failed
                    else self._interval
                )
                if report.stopped_tasks or report.cancelled_entries:
                    logger.info(
                        "User task-stop stopped %d task(s) and %d queue entry(ies)",
                        report.stopped_tasks,
                        report.cancelled_entries,
                    )
            await self._sleep(delay)

    async def _sleep(self, seconds: float) -> None:
        timer = asyncio.create_task(self._clock.sleep(seconds))
        waiter = asyncio.create_task(self._stopping.wait())
        try:
            await asyncio.wait({timer, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (timer, waiter):
                task.cancel()
            await asyncio.gather(timer, waiter, return_exceptions=True)


def build_user_stop_loop(
    database: Database,
    *,
    project_gate: ProjectGate,
    interval_seconds: float = DEFAULT_STOP_INTERVAL_SECONDS,
    clock: Clock | None = None,
) -> UserTaskStopLoop:
    """The loop the application runs, wired like ``build_project_stop_loop``.

    The task service carries the approval revocation listener (Decision 0006,
    section 9), so a task this cancels loses its open approvals at once.
    ``project_gate`` is REQUIRED (the task lane refuses to be built without one).
    """
    audit = PostgresAuditSink(database)
    approvals = ApprovalService(PostgresApprovalStore(database), audit)
    tasks = TaskService(
        database,
        listeners=[approvals.revoke_on_task_end],
        project_gate=project_gate,
    )
    queue = TaskQueue(database, project_gate=project_gate)
    return UserTaskStopLoop(
        UserTaskStopper(database, tasks, queue, audit),
        interval_seconds=interval_seconds,
        clock=clock,
    )


__all__ = [
    "ACTION_TASK_STOP",
    "STOP_REASON",
    "UserBusyError",
    "UserSweepReport",
    "UserTaskStopLoop",
    "UserTaskStopResult",
    "UserTaskStopper",
    "build_user_stop_loop",
]
