"""Server-to-client event paths: Server-Sent Events and WebSocket.

SECURITY (PAW-022 must close this before any non-public event is added):
these endpoints are UNAUTHENTICATED because sessions do not exist yet. They
therefore emit system events only (``system.connected`` and
``system.heartbeat``), which contain no user, project, task or memory data.
When PAW-022 introduces sessions, both endpoints must require an
authenticated session before they deliver anything else.

Already enforced here: the WebSocket refuses cross-origin browser handshakes
(``require_allowed_origin``; browsers do not apply CORS to WebSockets), the
``Host`` header is validated for every request (``HostValidationMiddleware``),
and the number of concurrent subscribers is capped (``PAW_EVENT_MAX_SUBSCRIBERS``).
"""

from collections.abc import AsyncIterable
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, WebSocket
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette import status
from starlette.websockets import WebSocketDisconnect, WebSocketDisconnected

from paw_backend.api.deps import (
    get_event_bus,
    require_allowed_origin,
    require_event_capacity,
)
from paw_backend.events import Event, EventBus, EventBusFull, EventType, Subscription

router = APIRouter(prefix="/events", tags=["events"])


def _sse(event: Event) -> ServerSentEvent:
    return ServerSentEvent(
        data=event.model_dump(mode="json"), event=event.type, id=event.id
    )


@router.get(
    "/stream",
    response_class=EventSourceResponse,
    summary="Server-Sent Events stream (system events only)",
    # The capacity check runs before the response starts, so an over-cap
    # client gets a regular 503 error body instead of a broken stream.
    dependencies=[Depends(require_event_capacity)],
)
async def stream_events(
    bus: Annotated[EventBus, Depends(get_event_bus)],
) -> AsyncIterable[ServerSentEvent]:
    # TODO(PAW-022): require an authenticated session (see module docstring).
    # No replay: `Last-Event-ID` is ignored because events are not persisted.
    # `subscribe()` still enforces the cap if another client took the last
    # slot after the dependency ran; that client's stream then just ends.
    with bus.subscribe() as subscription:
        yield _sse(Event(type=EventType.SYSTEM_CONNECTED))
        while True:
            yield _sse(await subscription.get())


async def _forward_events(websocket: WebSocket, subscription: Subscription) -> None:
    try:
        await websocket.send_json(
            Event(type=EventType.SYSTEM_CONNECTED).model_dump(mode="json")
        )
        while True:
            event = await subscription.get()
            await websocket.send_json(event.model_dump(mode="json"))
    except (WebSocketDisconnect, WebSocketDisconnected):
        return


async def _wait_for_disconnect(websocket: WebSocket) -> None:
    """Consume and discard client frames; return when the client goes away.

    The event path is server-to-client only. Client messages carry no
    meaning yet and are ignored.
    """
    try:
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass
    except WebSocketDisconnected:
        return


async def _serve_websocket(websocket: WebSocket, subscription: Subscription) -> None:
    async with anyio.create_task_group() as task_group:

        async def stop_when_client_leaves() -> None:
            await _wait_for_disconnect(websocket)
            task_group.cancel_scope.cancel()

        task_group.start_soon(stop_when_client_leaves)
        task_group.start_soon(_forward_events, websocket, subscription)


@router.websocket("/ws", dependencies=[Depends(require_allowed_origin)])
async def events_websocket(
    websocket: WebSocket, bus: Annotated[EventBus, Depends(get_event_bus)]
) -> None:
    # TODO(PAW-022): require an authenticated session before accepting
    # (see module docstring). The Origin check already ran as a dependency.
    await websocket.accept()
    try:
        with bus.subscribe() as subscription:
            await _serve_websocket(websocket, subscription)
    except EventBusFull:
        # Accepted first so that the client can read the close code.
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
