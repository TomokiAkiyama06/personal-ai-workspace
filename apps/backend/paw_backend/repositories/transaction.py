"""One transaction with a lock timeout, shared by the repository service (PAW-027).

Every write of the service starts with a lock on the project row (``FOR UPDATE``
to change the registry, ``FOR SHARE`` to make a checkout while the project must
stay Active): another transaction that holds the row for long must not stall a
caller for ever, so the transaction opens with ``SET LOCAL lock_timeout`` and a
lock wait longer than the timeout becomes :class:`RepositoryBusyError`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg.errors
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.db import Database
from paw_backend.repositories.errors import RepositoryBusyError


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
            raise RepositoryBusyError() from None
        raise
