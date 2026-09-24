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

That check is only the early answer. It can be overtaken by the end of the task
(a terminal transition committing between the check and the use), so the store
checks again **in the transaction that consumes the approval**, reading the task
row locked (:func:`lock_task_activity`, ``ApprovalStore.consume(...,
require_active_task=True)``): the use and the end are then ordered, never crossed.

The default provider knows no task, so nothing that needs an approval may run:
fail closed until a real provider (:class:`PostgresTaskActivity`) is installed,
as with ``BudgetProvider``.
"""

import uuid
from enum import StrEnum
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.db import Database
from paw_backend.tasks.domain import TERMINAL_STATES, TaskState


class TaskActivity(StrEnum):
    ACTIVE = "active"  # the task may still work: an approval may be asked for / used
    ENDED = "ended"  # completed / failed / cancelled: nothing runs for it any more
    UNKNOWN = "unknown"  # no such task, or a state this code does not know


class TaskActivityProvider(Protocol):
    async def check(self, task_id: uuid.UUID) -> TaskActivity:
        """Can ``task_id`` still act? Must read the *current* state."""
        ...


def activity_of(state: object) -> TaskActivity:
    """What a stored task state means: ``UNKNOWN`` for a state this code does not
    know (never read as alive)."""
    try:
        task_state = TaskState(state)
    except ValueError:
        return TaskActivity.UNKNOWN
    return TaskActivity.ENDED if task_state in TERMINAL_STATES else TaskActivity.ACTIVE


async def lock_task_activity(session: AsyncSession, task_id: uuid.UUID) -> TaskActivity:
    """The task's current state, read with its row **locked** (``FOR SHARE``).

    Inside a transaction this serialises the caller with a terminal transition:
    a transition that is in flight makes this wait for its commit and then read
    the new state; one that starts later waits for the caller's transaction to
    end. So what the caller does next in that transaction is ordered before, or
    after, the end of the task, never across it. A row lock needs the UPDATE
    privilege on ``tasks``, which the application role has (PAW-032).
    """
    rows = await session.execute(
        text("SELECT state FROM tasks WHERE id = :id FOR SHARE"), {"id": task_id}
    )
    state = rows.scalar_one_or_none()
    return TaskActivity.UNKNOWN if state is None else activity_of(state)


class FailClosedTaskActivity:
    """The default: no task is known, so no approval may be opened or used."""

    async def check(self, task_id: uuid.UUID) -> TaskActivity:
        return TaskActivity.UNKNOWN


class PostgresTaskActivity:
    """Reads the task's current state from ``tasks`` (PAW-032), on its own
    abortable connection so that a stalled database cannot hold a call up."""

    def __init__(self, database: Database, *, timeout_seconds: float = 3.0) -> None:
        self._database = database
        self._timeout_seconds = timeout_seconds

    async def check(self, task_id: uuid.UUID) -> TaskActivity:
        rows = await self._database.fetch_abortable(
            "SELECT state FROM tasks WHERE id = %(id)s",
            {"id": task_id},
            timeout_seconds=self._timeout_seconds,
        )
        return TaskActivity.UNKNOWN if not rows else activity_of(rows[0][0])
