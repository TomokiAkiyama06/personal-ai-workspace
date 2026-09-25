"""The task seam: is the task an approval belongs to still alive?

An approval is revoked when its task ends (``ApprovalService.revoke_on_task_end``),
but that runs *after* the terminal transition has been committed, in a listener
whose failure the ``TaskService`` can only log: nothing retries it. An approval
that could not be revoked would stay usable until it expires. So the broker also
asks, **at the time an approval is opened or used**, whether the task can still
act, and denies when it cannot (or when that cannot be found out): the check
does not depend on the revocation having worked.

``ACTIVE`` is every state of the task lifecycle from which work can still go on
(queued, running, waiting, paused, evaluating); ``ENDED`` is a terminal state
(completed, failed, cancelled: ``paw_backend.tasks.domain.TERMINAL_STATES``). A
failed or cancelled task can be retried or restarted later; that makes the task
``ACTIVE`` again, **not** the approvals that were revoked when it ended (the new
run asks again).

**The run.** A Retry or a Restart starts a new *run* of the same task
(:class:`TaskRun`: the attempt, which Restart increments, and the retry count,
which Retry increments; both only ever grow). What a worker asks is "can *my*
run still act?" (``check(task_id, run)``): a run that a Retry / Restart has
replaced is ``SUPERSEDED`` (unless the task has ended: ``ENDED`` is said first).
Every approval is
stamped with the run it was requested in and can only be used by that run. The
revocation that a Retry / Restart triggers runs *after* the transition has
committed, in a listener whose failure nothing retries, so it cannot be what
keeps an approval of the earlier run from a worker of the new one: the run is.

That check is only the early answer. It can be overtaken by the end of the task
or by a Retry / Restart (a transition committing between the check and the
insert of the request, or the use of the approval), so the store checks again
**in the transaction that inserts the request or consumes the approval**,
reading the task row locked (:func:`lock_task_activity`,
``ApprovalStore.open_request(..., require_active_task=True)`` and
``consume(..., require_active_task=True)``): the insert or the use and the
transition are then ordered, never crossed.

The default provider knows no task, so nothing that needs an approval may run:
fail closed until a real provider (:class:`PostgresTaskActivity`) is installed,
as with ``BudgetProvider``.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import psycopg

from paw_backend.db import Database
from paw_backend.tasks.domain import TERMINAL_STATES, TaskState

# ``tasks.attempt`` and ``tasks.retry_count`` are 32-bit ``INTEGER`` columns.
_MAX_COUNTER = 2**31 - 1


def _counter(value: object, name: str, minimum: int) -> None:
    if type(value) is not int or not minimum <= value <= _MAX_COUNTER:
        raise ValueError(f"{name} must be an integer from {minimum} to {_MAX_COUNTER}")


@dataclass(frozen=True, slots=True)
class TaskRun:
    """Which run of a task a worker (or an approval) belongs to.

    ``attempt`` is ``tasks.attempt`` (from 1; Restart increments it) and
    ``retry_count`` is ``tasks.retry_count`` (from 0; Retry increments it). Every
    re-opening of a failed or cancelled task changes exactly one of them, and
    neither ever decreases, so two runs of a task are equal only if they are the
    same run. The orchestrator builds it from the task it starts the worker for
    (``TaskSnapshot.attempt.number`` and ``TaskSnapshot.retry_count``).
    """

    attempt: int
    retry_count: int

    def __post_init__(self) -> None:
        _counter(self.attempt, "attempt", 1)
        _counter(self.retry_count, "retry_count", 0)


class TaskActivity(StrEnum):
    ACTIVE = "active"  # the run may still work: an approval may be asked for / used
    ENDED = "ended"  # completed / failed / cancelled: nothing runs for it any more
    # The task is alive, but a Retry / Restart has started another run: the
    # worker that asks belongs to an earlier one.
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"  # no such task, or a state this code does not know


class TaskActivityProvider(Protocol):
    async def check(self, task_id: uuid.UUID, run: TaskRun) -> TaskActivity:
        """Can ``run`` of ``task_id`` still act? Must read the *current* state
        and run (``UNKNOWN`` for a task that does not exist)."""
        ...


def activity_of(
    state: object, attempt: object, retry_count: object, run: TaskRun
) -> TaskActivity:
    """What a stored task (its state and run) means for the worker of ``run``.

    ``UNKNOWN`` for a state this code does not know, or a counter that is not an
    integer (never read as alive); ``ENDED`` before ``SUPERSEDED`` (a task that
    has ended has no run that can act, and that is the more useful thing to say).
    """
    try:
        task_state = TaskState(state)
        current = TaskRun(attempt, retry_count)
    except ValueError:
        return TaskActivity.UNKNOWN
    if task_state in TERMINAL_STATES:
        return TaskActivity.ENDED
    return TaskActivity.ACTIVE if current == run else TaskActivity.SUPERSEDED


async def lock_task_activity(
    connection: psycopg.AsyncConnection, task_id: uuid.UUID, run: TaskRun
) -> TaskActivity:
    """The task's current state and run, read with its row **locked**
    (``FOR SHARE``), and what they mean for ``run``.

    Inside a transaction (the store runs it on ``Database.transact_abortable``)
    this serialises the caller with a terminal transition and with Retry /
    Restart (which change the row): a transition that is in flight makes this
    wait for its commit and then read the new state and run; one that starts
    later waits for the caller's transaction to end. So what the caller does
    next in that transaction is ordered before, or after, the transition, never
    across it. A row lock needs the UPDATE privilege on ``tasks``, which the
    application role has (PAW-032).
    """
    cursor = await connection.execute(
        "SELECT state, attempt, retry_count FROM tasks WHERE id = %(id)s FOR SHARE",
        {"id": task_id},
    )
    row = await cursor.fetchone()
    return TaskActivity.UNKNOWN if row is None else activity_of(*row, run)


class FailClosedTaskActivity:
    """The default: no task is known, so no approval may be opened or used."""

    async def check(self, task_id: uuid.UUID, run: TaskRun) -> TaskActivity:
        return TaskActivity.UNKNOWN


class PostgresTaskActivity:
    """Reads the task's current state from ``tasks`` (PAW-032), on its own
    abortable connection so that a stalled database cannot hold a call up."""

    def __init__(self, database: Database, *, timeout_seconds: float = 3.0) -> None:
        self._database = database
        self._timeout_seconds = timeout_seconds

    async def check(self, task_id: uuid.UUID, run: TaskRun) -> TaskActivity:
        rows = await self._database.fetch_abortable(
            "SELECT state, attempt, retry_count FROM tasks WHERE id = %(id)s",
            {"id": task_id},
            timeout_seconds=self._timeout_seconds,
        )
        return TaskActivity.UNKNOWN if not rows else activity_of(*rows[0], run)
