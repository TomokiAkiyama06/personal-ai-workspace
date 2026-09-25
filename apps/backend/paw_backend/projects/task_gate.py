"""The Project state gate for the task lane (Issue #83, Decisions 0008 and 0020).

Implements ``paw_backend.tasks.ProjectGate``: ``TaskService.create_task`` / Retry /
Restart / Start and ``TaskQueue.enqueue`` call :meth:`ProjectStateGate.require_active`
inside the transaction of their own write, and ``TaskQueue.claim_next`` puts
:meth:`ProjectStateGate.active_condition` in its WHERE clause so that the entries of
a project that is not Active are not claimed. It locks the project row ``SELECT ...
FOR SHARE`` and raises ``ProjectNotActiveError`` unless the project is Active
(Archived, Pending deletion, Deleted and unknown projects are refused). The row
stays locked until the caller's transaction ends, so Delete, Archive and Restore
(``FOR UPDATE``) wait for the write and a write that comes after them is refused;
see ``paw_backend.tasks.project_gate`` for the whole argument and for why the gate
is injected (``tasks`` must not import ``projects``).

Privileges (the application role, migration 0026): ``SELECT`` on ``projects`` and
``UPDATE`` on some column of it (``FOR SHARE`` needs both; the role has ``UPDATE``
on ``status`` and six other columns, and nothing more is granted for this gate).
The gate reads the ``status`` column only and writes nothing. Its lock wait is
bounded by ``lock_timeout_ms`` (:class:`ProjectBusyError`, retryable), and the
transaction's own ``lock_timeout`` is put back afterwards.

The gate writes no audit event: neither the task lane nor the queue audits (they
authorize nothing); the caller that authorized the request (the API layer of
PAW-022) records a refusal.
"""

import uuid

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.projects import store
from paw_backend.projects.limits import (
    DEFAULT_LOCK_TIMEOUT_MS,
    MAX_LOCK_TIMEOUT_MS,
    MIN_LOCK_TIMEOUT_MS,
)
from paw_backend.projects.records import ProjectStatus
from paw_backend.projects.transaction import share_lock_status
from paw_backend.projects.validation import validate_uuid
from paw_backend.tasks import ProjectNotActiveError


class ProjectStateGate:
    """Requires the project to be Active, under ``FOR SHARE`` (module docstring)."""

    def __init__(self, *, lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS) -> None:
        """``lock_timeout_ms``: an ``int`` (not a ``bool``) from 1 to 60000.

        ``TypeError`` for a wrong type and ``ValueError`` for a value out of range,
        like ``ProjectService``.
        """
        if isinstance(lock_timeout_ms, bool) or not isinstance(lock_timeout_ms, int):
            raise TypeError("lock_timeout_ms must be an int")
        if not MIN_LOCK_TIMEOUT_MS <= lock_timeout_ms <= MAX_LOCK_TIMEOUT_MS:
            raise ValueError("lock_timeout_ms is out of range")
        self._lock_timeout_ms = lock_timeout_ms

    async def require_active(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """Lock the project ``FOR SHARE`` and require ``Active``.

        ``InvalidProjectInputError`` for a ``project_id`` that is not a ``uuid.UUID``,
        ``ProjectNotActiveError`` (a ``TaskError``) for a project that is not Active or
        does not exist, ``ProjectBusyError`` when the lock is not granted in time.
        Nothing is written.
        """
        project_id = validate_uuid("project_id", project_id)
        status = await share_lock_status(session, project_id, self._lock_timeout_ms)
        if status is not ProjectStatus.ACTIVE:
            raise ProjectNotActiveError()

    def active_condition(self, project_id: ColumnElement) -> ColumnElement[bool]:
        """A SQL condition that is true for a task whose project is Active.

        ``project_id`` is the column that names the project in the caller's statement
        (``tasks.project_id``). The queue puts it in the WHERE clause of its claim so
        that an entry of a project that is not Active (or unknown) is skipped and
        stays queued. It locks nothing: the claim only decides what to hand out; the
        Start command asks ``require_active`` under a lock right after.
        """
        return store.active_project_condition(project_id)


__all__ = ["ProjectStateGate"]
