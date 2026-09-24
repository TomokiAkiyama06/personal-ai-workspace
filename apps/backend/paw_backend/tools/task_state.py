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

The default provider knows no task, so nothing that needs an approval may run:
fail closed until a real provider (:class:`PostgresTaskActivity`) is installed,
as with ``BudgetProvider``.
"""

import uuid
from enum import StrEnum
from typing import Protocol

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
        if not rows:
            return TaskActivity.UNKNOWN
        try:
            state = TaskState(rows[0][0])
        except ValueError:
            return TaskActivity.UNKNOWN
        return TaskActivity.ENDED if state in TERMINAL_STATES else TaskActivity.ACTIVE
