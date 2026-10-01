"""Server-to-client event paths: Server-Sent Events and WebSocket.

Both need a session (``notification.read``, every human role; issue #188,
Decision 0070 Approved): an anonymous request is refused (401, or close code
1008 for a WebSocket) before it takes a subscriber slot. They carry the system
events (``system.connected``, ``system.heartbeat``) and ``notification.changed``,
a hint without content that the client's notifications changed: it reaches the
streams of the users of its audience only (``paw_backend.events``), and the
client then reads its notifications through the authorized
``GET /api/v1/notifications``.

An open stream checks its session again every ``PAW_EVENT_SESSION_CHECK_SECONDS``
(default 60), without moving its idle expiry: it ends when the session was
signed out, revoked or expired, the user is no longer active, or the database
does not answer (the client reconnects, and is authenticated again). A role
change applies to the notification events from that check on.

Also enforced here: the WebSocket refuses cross-origin browser handshakes
(``require_allowed_origin``; browsers do not apply CORS to WebSockets), the
``Host`` header is validated for every request (``HostValidationMiddleware``),
and the number of concurrent subscribers is capped (``PAW_EVENT_MAX_SUBSCRIBERS``).
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterable
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette import status
from starlette.requests import HTTPConnection
from starlette.websockets import (
    WebSocketDisconnect,
    WebSocketDisconnected,
    WebSocketState,
)

from paw_backend.api.deps import (
    get_event_bus,
    get_settings,
    require_allowed_origin,
    reserve_event_slot,
)
from paw_backend.authz import Capability, Principal, require_capability
from paw_backend.config import Settings
from paw_backend.events import (
    Event,
    EventBus,
    EventBusFull,
    EventType,
    Reservation,
    Subscription,
    Viewer,
)
from paw_backend.notifications import audience_capabilities

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/events", tags=["events"])

_Reader = Annotated[
    Principal, Depends(require_capability(Capability.NOTIFICATION_READ))
]


def viewer_of(connection: HTTPConnection, principal: Principal) -> Viewer:
    policy = connection.app.state.authorizer.policy
    return Viewer(
        principal.user_id, frozenset(audience_capabilities(principal, policy))
    )


class SessionCheck:
    """Checks, at most every ``interval`` seconds, that the stream's session is
    still the same user's and valid; refreshes the subscription's viewer."""

    def __init__(
        self, connection: HTTPConnection, principal: Principal, interval: float
    ) -> None:
        self._connection = connection
        self._user_id = principal.user_id
        self._interval = interval
        self._due = time.monotonic() + interval

    def seconds_left(self) -> float:
        return max(0.0, self._due - time.monotonic())

    async def still_valid(self, subscription: Subscription) -> bool:
        if time.monotonic() < self._due:
            return True
        provider = self._connection.app.state.principal_provider
        check = getattr(provider, "revalidate", None) or provider.get_principal
        try:
            principal = await check(self._connection)
        except Exception as error:  # the database, a restricted session: fail closed
            logger.info("Event stream session check failed (%s)", type(error).__name__)
            return False
        if principal is None or principal.user_id != self._user_id:
            return False
        subscription.viewer = viewer_of(self._connection, principal)
        self._due = time.monotonic() + self._interval
        return True


async def _next_event(subscription: Subscription, check: SessionCheck) -> Event | None:
    """The next event, or ``None`` when the session check is due first."""
    try:
        async with asyncio.timeout(check.seconds_left()):
            return await subscription.get()
    except TimeoutError:
        return None


def _sse(event: Event) -> ServerSentEvent:
    return ServerSentEvent(
        data=event.model_dump(mode="json"), event=event.type, id=event.id
    )


@router.get(
    "/stream",
    response_class=EventSourceResponse,
    summary="Server-Sent Events stream (system and notification events)",
)
async def stream_events(
    request: Request,
    # Authenticated first: an anonymous request never takes a slot.
    principal: _Reader,
    # Reserved before the response starts, so an over-cap client gets a regular
    # 503 error body; released by the dependency however the request ends.
    reservation: Annotated[Reservation, Depends(reserve_event_slot)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterable[ServerSentEvent]:
    # No replay: `Last-Event-ID` is ignored because events are not persisted.
    subscription = reservation.attach(viewer_of(request, principal))
    check = SessionCheck(request, principal, settings.event_session_check_seconds)
    yield _sse(Event(type=EventType.SYSTEM_CONNECTED))
    while True:
        event = await _next_event(subscription, check)
        if not await check.still_valid(subscription):
            return
        if event is not None:
            yield _sse(event)


async def _forward_events(
    websocket: WebSocket, subscription: Subscription, check: SessionCheck
) -> None:
    """Send the events until the session check fails (then return)."""
    await websocket.send_json(
        Event(type=EventType.SYSTEM_CONNECTED).model_dump(mode="json")
    )
    while True:
        event = await _next_event(subscription, check)
        if not await check.still_valid(subscription):
            return
        if event is not None:
            await websocket.send_json(event.model_dump(mode="json"))


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


async def _serve_websocket(
    websocket: WebSocket, subscription: Subscription, check: SessionCheck
) -> bool:
    """Serve until the client leaves (``False``) or the session ends (``True``)."""
    session_ended = False
    async with anyio.create_task_group() as task_group:

        async def stop_when_client_leaves() -> None:
            await _wait_for_disconnect(websocket)
            task_group.cancel_scope.cancel()

        async def forward() -> None:
            nonlocal session_ended
            try:
                await _forward_events(websocket, subscription, check)
            except (WebSocketDisconnect, WebSocketDisconnected):
                return
            session_ended = True
            task_group.cancel_scope.cancel()

        task_group.start_soon(stop_when_client_leaves)
        task_group.start_soon(forward)
    return session_ended


@router.websocket("/ws", dependencies=[Depends(require_allowed_origin)])
async def events_websocket(
    websocket: WebSocket,
    # The Origin check ran first; then the session, before the socket is accepted.
    principal: _Reader,
    bus: Annotated[EventBus, Depends(get_event_bus)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    await websocket.accept()
    check = SessionCheck(websocket, principal, settings.event_session_check_seconds)
    try:
        # Reserving and attaching is one synchronous step, so unlike SSE there
        # is no window between the capacity check and the registration.
        with bus.subscribe(viewer_of(websocket, principal)) as subscription:
            session_ended = await _serve_websocket(websocket, subscription, check)
    except EventBusFull:
        # Accepted first so that the client can read the close code.
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
        return
    if session_ended and websocket.application_state is WebSocketState.CONNECTED:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
