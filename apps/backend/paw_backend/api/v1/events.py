"""Server-to-client event paths: Server-Sent Events and WebSocket.

SECURITY (PAW-022 must close this before any non-public event is added):
these endpoints are UNAUTHENTICATED because sessions do not exist yet. They
therefore emit system events only (``system.connected`` and
``system.heartbeat``), which contain no user, project, task or memory data.
When PAW-022 introduces sessions, both endpoints must require an
authenticated session before they deliver anything else. The WebSocket also
needs an ``Origin`` check: browsers do not apply CORS to WebSocket
handshakes, so a cookie-authenticated socket is open to cross-site hijacking
without one.
"""

from collections.abc import AsyncIterable
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, WebSocket
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.websockets import WebSocketDisconnect, WebSocketDisconnected

from paw_backend.api.deps import get_event_bus
from paw_backend.events import Event, EventBus, EventType, Subscription

router = APIRouter(prefix="/events", tags=["events"])


def _sse(event: Event) -> ServerSentEvent:
    return ServerSentEvent(
        data=event.model_dump(mode="json"), event=event.type, id=event.id
    )


@router.get(
    "/stream",
    response_class=EventSourceResponse,
    summary="Server-Sent Events stream (system events only)",
)
async def stream_events(
    bus: Annotated[EventBus, Depends(get_event_bus)],
) -> AsyncIterable[ServerSentEvent]:
    # TODO(PAW-022): require an authenticated session (see module docstring).
    # No replay: `Last-Event-ID` is ignored because events are not persisted.
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


@router.websocket("/ws")
async def events_websocket(
    websocket: WebSocket, bus: Annotated[EventBus, Depends(get_event_bus)]
) -> None:
    # TODO(PAW-022): require an authenticated session and check `Origin`
    # before accepting (see module docstring).
    await websocket.accept()
    with bus.subscribe() as subscription:
        async with anyio.create_task_group() as task_group:

            async def stop_when_client_leaves() -> None:
                await _wait_for_disconnect(websocket)
                task_group.cancel_scope.cancel()

            task_group.start_soon(stop_when_client_leaves)
            task_group.start_soon(_forward_events, websocket, subscription)
