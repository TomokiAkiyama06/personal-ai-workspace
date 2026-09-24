"""FastAPI dependencies that expose application-wide objects.

They take ``HTTPConnection`` (the base of both ``Request`` and ``WebSocket``)
so the same dependency works for REST and WebSocket endpoints.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status
from starlette.exceptions import WebSocketException
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from paw_backend.config import Settings
from paw_backend.db import Database, DatabaseNotConfiguredError
from paw_backend.errors import ApiError
from paw_backend.events import EventBus
from paw_backend.security import origin_allowed


def get_database(connection: HTTPConnection) -> Database:
    return connection.app.state.database


def get_event_bus(connection: HTTPConnection) -> EventBus:
    return connection.app.state.event_bus


def get_settings(connection: HTTPConnection) -> Settings:
    return connection.app.state.settings


def require_event_capacity(bus: Annotated[EventBus, Depends(get_event_bus)]) -> None:
    """Answer 503 before an SSE response starts when the bus is at its cap."""
    if bus.is_full:
        raise ApiError(
            503, "event_capacity_reached", "Too many event subscribers; retry later"
        )


def require_allowed_origin(
    websocket: WebSocket, settings: Annotated[Settings, Depends(get_settings)]
) -> None:
    """Refuse a cross-origin browser handshake before the socket is accepted.

    Browsers do not apply CORS to WebSockets, so the server must check
    ``Origin`` itself. Clients that send no ``Origin`` header (CLI, scripts)
    are not browsers and pass; they need authentication (PAW-022) instead.
    """
    origin = websocket.headers.get("origin")
    if origin is not None and not origin_allowed(
        origin, websocket.headers.get("host"), settings.allowed_origins
    ):
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)


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
