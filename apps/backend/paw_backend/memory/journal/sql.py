"""Shared SQL helpers of the journal services: one transaction shape, the clock.

Every write runs in a transaction that begins with ``SET LOCAL lock_timeout``: a
wait for a row or advisory lock longer than ``lock_timeout_ms`` is
:class:`JournalBusyError` (nothing changed, try again), never an unbounded wait.

Time. The DATABASE clock is the only clock the queue trusts (Decision 0007,
section 6, for the task queue; the same reasoning here): every instant it stores
or compares (``enqueued_at``, ``available_at``, ``claimed_at``,
``lease_expires_at``, ``finished_at`` and "has this lease expired?") is
PostgreSQL's ``clock_timestamp()``, the wall clock at the moment it is evaluated.
It is never ``now()`` (fixed at the start of the transaction, so a wait for a row
lock would be judged by a time that has already passed) and never a worker's own
clock. There is no way to pass a time in: a test moves the database-side rows
(with the schema owner's rights) instead of injecting a clock.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg.errors
from sqlalchemy import Text, bindparam, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import BindParameter

from paw_backend.db import Database
from paw_backend.memory.journal.errors import JournalBusyError


@asynccontextmanager
async def transaction(
    database: Database, lock_timeout_ms: int
) -> AsyncIterator[AsyncSession]:
    """One transaction with the lock timeout; commits on return, rolls back on error."""
    try:
        async with database.session() as session, session.begin():
            await session.execute(
                select(func.set_config("lock_timeout", str(lock_timeout_ms), True))
            )
            yield session
    except DBAPIError as error:
        # Only the type of the driver's error is read, never its text.
        if isinstance(error.orig, psycopg.errors.LockNotAvailable):
            raise JournalBusyError from None
        raise


def inlined(name: str, value: str) -> BindParameter[str]:
    """``value`` written into the SQL text, not sent as a parameter.

    The partial indexes of the queue are over ``status IN ('queued', 'claimed')``.
    PostgreSQL uses such an index only if the statement's own predicate implies
    that condition, and it cannot prove it for a value sent as a bind parameter in
    the generic plan it may cache for a prepared statement (the driver prepares a
    statement it runs often): the claim would then scan and sort every job ever
    queued (finished and dead jobs are kept). The statuses are constants, so
    writing them into the statement costs no plan reuse.
    """
    return bindparam(name, value, type_=Text(), literal_execute=True)
