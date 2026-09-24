"""One transaction with a lock timeout, shared by the project services (PAW-026).

Every write of the project module starts with ``SELECT ... FOR UPDATE`` on the
project row (and the task-stop processor takes ``FOR SHARE`` on it): another
transaction that holds the row for long must not stall a caller for ever, so the
transaction opens with ``SET LOCAL lock_timeout`` and a lock wait that is longer
than the timeout becomes :class:`ProjectBusyError`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg.errors
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.db import Database
from paw_backend.projects.errors import ProjectBusyError


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
