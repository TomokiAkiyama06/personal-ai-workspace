"""One transaction with a lock timeout, shared by the project services (PAW-026).

Every write of the project module starts with ``SELECT ... FOR UPDATE`` on the
project row (and the task-stop processor takes ``FOR SHARE`` on it): another
transaction that holds the row for long must not stall a caller for ever, so the
transaction opens with ``SET LOCAL lock_timeout`` and a lock wait that is longer
than the timeout becomes :class:`ProjectBusyError`.

:func:`share_lock_status` does the same for the transactions that are NOT the
project module's (Issue #83): the task lane's transaction takes ``FOR SHARE`` on
the project row through the Project state gate, and the stop processor does it inside
the transaction of a task command.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg.errors
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.db import Database
from paw_backend.projects import store
from paw_backend.projects.errors import ProjectBusyError
from paw_backend.projects.records import ProjectStatus


@asynccontextmanager
async def transaction(
    database: Database, lock_timeout_ms: int
) -> AsyncIterator[AsyncSession]:
    """Begin a transaction (committed on exit) whose lock waits are bounded."""
    try:
        async with database.session() as session, session.begin():
            await session.execute(
                select(func.set_config("lock_timeout", str(lock_timeout_ms), True))
            )
            yield session
    except DBAPIError as error:
        # Only the type of the driver's error is read, never its text.
        if isinstance(error.orig, psycopg.errors.LockNotAvailable):
            raise ProjectBusyError() from None
        raise


async def share_lock_status(
    session: AsyncSession, project_id: uuid.UUID, lock_timeout_ms: int
) -> ProjectStatus | None:
    """Lock the project row ``FOR SHARE`` in the caller's transaction; its status.

    ``None`` for an unknown project. The lock lasts until the caller's transaction
    ends. The wait for a writer is bounded: ``lock_timeout`` is set to
    ``lock_timeout_ms`` for this one statement and put back afterwards, because the
    caller's own statements (the task lane's row locks) have their own waits and must
    keep them. A wait that exceeds it is :class:`ProjectBusyError` (the caller's
    transaction is aborted by the error and must end). Only the type of the driver's
    error is read, never its text.
    """
    previous = (
        await session.execute(select(func.current_setting("lock_timeout")))
    ).scalar_one()
    await session.execute(
        select(func.set_config("lock_timeout", str(lock_timeout_ms), True))
    )
    try:
        status = await store.get_project_status_for_share(session, project_id)
    except DBAPIError as error:
        if isinstance(error.orig, psycopg.errors.LockNotAvailable):
            raise ProjectBusyError() from None
        raise
    await session.execute(select(func.set_config("lock_timeout", previous, True)))
    return status
