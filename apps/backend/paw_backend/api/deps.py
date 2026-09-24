"""FastAPI dependencies that expose application-wide objects.

They take ``HTTPConnection`` (the base of both ``Request`` and ``WebSocket``)
so the same dependency works for REST and WebSocket endpoints.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from paw_backend.db import Database, DatabaseNotConfiguredError
from paw_backend.errors import ApiError
from paw_backend.events import EventBus


def get_database(connection: HTTPConnection) -> Database:
    return connection.app.state.database


def get_event_bus(connection: HTTPConnection) -> EventBus:
    return connection.app.state.event_bus


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """Yield a database session; answer 503 when no database is configured."""
    try:
        session = database.session()
    except DatabaseNotConfiguredError:
        raise ApiError(
            503, "database_not_configured", "The database is not configured"
        ) from None
    async with session:
        yield session
