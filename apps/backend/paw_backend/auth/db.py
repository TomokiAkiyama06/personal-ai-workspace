"""Running a unit of work on an abortable connection, with one error contract."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import psycopg
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.errors import AuthUnavailableError
from paw_backend.db import Database, DatabaseDisposedError, DatabaseNotConfiguredError

logger = logging.getLogger(__name__)


async def run[T](
    database: Database,
    work: Callable[[AsyncSession], Awaitable[T]],
    timeout_seconds: float,
) -> T:
    """``work(session)`` in ONE transaction that never outlives ``timeout_seconds``.

    ``Database.run_abortable`` on its own connection outside the pool; when the
    deadline passes, or the caller is cancelled, the connection's socket is shut
    down (a stalled server is not asked to cancel and waited for). Whatever the
    database, the driver or the deadline does is one ``AuthUnavailableError``
    (fail closed); only its type is logged. An exception ``work`` raises itself
    (the domain errors) passes through unchanged, after the rollback.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            return await database.run_abortable(work)
    except TimeoutError:
        logger.error("Authentication database work timed out")
        raise AuthUnavailableError from None
    except (DatabaseNotConfiguredError, DatabaseDisposedError):
        raise AuthUnavailableError from None
    except (SQLAlchemyError, psycopg.Error, OSError) as error:
        # Only the type: a driver message can name the host or the user.
        logger.error("Authentication database work failed (%s)", type(error).__name__)
        raise AuthUnavailableError from None


def sqlstate_of(error: BaseException) -> str | None:
    """The SQLSTATE of a database error, or ``None``."""
    original = error.orig if isinstance(error, DBAPIError) else error
    return getattr(original, "sqlstate", None)
