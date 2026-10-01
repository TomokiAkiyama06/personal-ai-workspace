import asyncio
import json
import unittest
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from paw_backend.api.deps import reserve_event_slot
from paw_backend.app import create_app
from paw_backend.authz import Capability, SystemRole
from paw_backend.errors import ApiError
from paw_backend.events import (
    Event,
    EventBus,
    EventBusFull,
    EventType,
    Reservation,
    Viewer,
    notification_changed,
    publish_heartbeats,
)

from .authz_support import U1, U2, principal
from .support import (
    WEBSOCKET_URL,
    AsgiWebSocket,
    http_scope,
    make_client,
    make_settings,
    read_sse,
    signed_in,
    wait_until,
)


def heartbeat() -> Event:
    return Event(type=EventType.SYSTEM_HEARTBEAT)


class EventModelTest(unittest.TestCase):
    def test_the_known_event_types(self):
        self.assertEqual(
            {member.value for member in EventType},
            {"system.connected", "system.heartbeat", "notification.changed"},
        )
        with self.assertRaises(ValidationError):
            Event(type="task.created")

    def test_wire_format(self):
        payload = heartbeat().model_dump(mode="json")
        self.assertEqual(set(payload), {"id", "type", "occurred_at", "data"})
        self.assertEqual(payload["type"], "system.heartbeat")
        self.assertEqual(payload["data"], {})
        self.assertTrue(payload["occurred_at"].endswith("Z"))


class ReservationTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_reservation_holds_its_slot_until_released(self):
        bus = EventBus(max_subscribers=1)
        reservation = bus.reserve()
        self.assertTrue(bus.is_full)
        with self.assertRaises(EventBusFull):
            bus.reserve()

        reservation.release()
        self.assertFalse(bus.is_full)
        bus.reserve()  # the slot is available again

    async def test_releasing_twice_does_not_free_another_reservations_slot(self):
        bus = EventBus(max_subscribers=1)
        first = bus.reserve()
        first.release()
        first.release()
        second = bus.reserve()

        first.release()  # already released: must not take the second's slot
        self.assertEqual(bus.slots_in_use, 1)
        with self.assertRaises(EventBusFull):
            bus.reserve()
        second.release()
        self.assertEqual(bus.slots_in_use, 0)

    async def test_an_attached_reservation_receives_events_until_released(self):
        bus = EventBus()
        reservation = bus.reserve()
        self.assertEqual(bus.subscriber_count, 0, "a reservation is not a subscriber")
        subscription = reservation.attach()
        self.assertEqual(bus.subscriber_count, 1)

        event = heartbeat()
        bus.publish(event)
        self.assertEqual(await subscription.get(), event)

        reservation.release()
        self.assertEqual((bus.subscriber_count, bus.slots_in_use), (0, 0))
        with self.assertRaises(RuntimeError):
            reservation.attach()


class EventBusTest(unittest.IsolatedAsyncioTestCase):
    async def test_publish_reaches_every_subscriber(self):
        bus = EventBus()
        with bus.subscribe() as first, bus.subscribe() as second:
            event = heartbeat()
            bus.publish(event)
            self.assertEqual(await first.get(), event)
            self.assertEqual(await second.get(), event)

    async def test_leaving_the_context_unsubscribes(self):
        bus = EventBus()
        with bus.subscribe():
            self.assertEqual(bus.subscriber_count, 1)
        self.assertEqual(bus.subscriber_count, 0)
        bus.publish(heartbeat())  # no subscriber: must not raise

    async def test_unsubscribes_when_the_consumer_raises(self):
        bus = EventBus()
        with self.assertRaises(RuntimeError):
            with bus.subscribe():
                raise RuntimeError
        self.assertEqual(bus.subscriber_count, 0)

    async def test_a_slow_subscriber_loses_its_oldest_events_only(self):
        bus = EventBus(queue_size=2)
        events = [heartbeat() for _ in range(3)]
        with bus.subscribe() as slow, bus.subscribe() as fast:
            bus.publish(events[0])
            self.assertEqual(await fast.get(), events[0])
            bus.publish(events[1])
            bus.publish(events[2])
            self.assertEqual(await fast.get(), events[1])
            self.assertEqual(await slow.get(), events[1])
            self.assertEqual(await slow.get(), events[2])

    async def test_subscriber_cap_is_enforced_and_released(self):
        bus = EventBus(max_subscribers=2)
        with bus.subscribe(), bus.subscribe():
            self.assertTrue(bus.is_full)
            with self.assertRaises(EventBusFull):
                with bus.subscribe():
                    self.fail("a third subscriber must be refused")
            self.assertEqual(bus.subscriber_count, 2)
        self.assertFalse(bus.is_full)
        with bus.subscribe():
            self.assertEqual(bus.subscriber_count, 1)

    async def test_heartbeat_publisher_emits_system_heartbeats(self):
        bus = EventBus()
        with bus.subscribe() as subscription:
            task = asyncio.create_task(publish_heartbeats(bus, 0.01))
            try:
                async with asyncio.timeout(2):
                    first = await subscription.get()
                    second = await subscription.get()
            finally:
                task.cancel()
        self.assertEqual(first.type, EventType.SYSTEM_HEARTBEAT)
        self.assertNotEqual(first.id, second.id)


class WebSocketEventsTest(unittest.TestCase):
    def test_websocket_receives_connected_then_heartbeat(self):
        app = signed_in(create_app(make_settings(event_heartbeat_seconds=0.02)))
        with make_client(app) as client:
            with client.websocket_connect(WEBSOCKET_URL) as websocket:
                connected = websocket.receive_json()
                beat = websocket.receive_json()
        self.assertEqual(connected["type"], "system.connected")
        self.assertEqual(beat["type"], "system.heartbeat")
        self.assertEqual(set(beat), {"id", "type", "occurred_at", "data"})

    def test_client_messages_are_ignored(self):
        app = signed_in(create_app(make_settings(event_heartbeat_seconds=0.02)))
        with make_client(app) as client:
            with client.websocket_connect(WEBSOCKET_URL) as websocket:
                websocket.receive_json()
                websocket.send_text("ignored")
                websocket.send_bytes(b"ignored")
                self.assertEqual(websocket.receive_json()["type"], "system.heartbeat")

    def test_disconnect_removes_the_subscription(self):
        app = signed_in(create_app(make_settings(event_heartbeat_seconds=0.02)))
        bus = app.state.event_bus
        with make_client(app) as client:
            with client.websocket_connect(WEBSOCKET_URL) as websocket:
                websocket.receive_json()
                self.assertEqual(bus.subscriber_count, 1)
            self.assertTrue(
                asyncio.run(wait_until(lambda: bus.subscriber_count == 0)),
                "subscription was not released after the client disconnected",
            )


class WebSocketOriginTest(unittest.TestCase):
    def connect(self, origin: str | None = None, **settings):
        client = make_client(signed_in(create_app(make_settings(**settings))))
        headers = {} if origin is None else {"Origin": origin}
        with client.websocket_connect(WEBSOCKET_URL, headers=headers) as websocket:
            return websocket.receive_json()["type"]

    def test_clients_without_an_origin_header_are_accepted(self):
        self.assertEqual(self.connect(), "system.connected")

    def test_the_requests_own_origin_is_accepted(self):
        self.assertEqual(self.connect("http://localhost"), "system.connected")
        self.assertEqual(self.connect("https://localhost"), "system.connected")

    def test_listed_origins_are_accepted(self):
        allowed = {"allowed_origins": "https://app.example.org:8443"}
        self.assertEqual(
            self.connect("https://app.example.org:8443", **allowed), "system.connected"
        )

    def test_cross_origin_browser_handshakes_are_refused(self):
        allowed = {"allowed_origins": "https://app.example.org"}
        for origin in (
            "https://evil.example",
            "http://localhost.evil.example",
            "http://localhost:9999",
            "https://app.example.org:8443",
            "null",
            "not an origin",
        ):
            with self.subTest(origin=origin):
                with self.assertRaises(WebSocketDisconnect) as caught:
                    self.connect(origin, **allowed)
                self.assertEqual(caught.exception.code, 1008)

    def test_a_refused_handshake_does_not_subscribe(self):
        app = signed_in(create_app(make_settings()))
        with self.assertRaises(WebSocketDisconnect):
            with make_client(app).websocket_connect(
                WEBSOCKET_URL, headers={"Origin": "https://evil.example"}
            ):
                pass
        self.assertEqual(app.state.event_bus.subscriber_count, 0)


class SubscriberCapTest(unittest.TestCase):
    def test_sse_over_the_cap_gets_a_503_error_body(self):
        app = signed_in(create_app(make_settings(event_max_subscribers=1)))
        with app.state.event_bus.subscribe():
            response = make_client(app).get("/api/v1/events/stream")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "event_capacity_reached")
        self.assertEqual(
            response.json()["error"]["request_id"], response.headers["x-request-id"]
        )

    def test_websocket_over_the_cap_is_closed_with_1013(self):
        app = signed_in(create_app(make_settings(event_max_subscribers=1)))
        with app.state.event_bus.subscribe():
            with make_client(app).websocket_connect(WEBSOCKET_URL) as websocket:
                with self.assertRaises(WebSocketDisconnect) as caught:
                    websocket.receive_json()
            # The refused client did not take a slot; only the held one remains.
            self.assertEqual(app.state.event_bus.subscriber_count, 1)
        self.assertEqual(caught.exception.code, 1013)

    def test_a_slot_is_available_again_after_a_client_leaves(self):
        app = signed_in(create_app(make_settings(event_max_subscribers=1)))
        client = make_client(app)
        with client.websocket_connect(WEBSOCKET_URL) as websocket:
            websocket.receive_json()
        self.assertTrue(
            asyncio.run(wait_until(lambda: app.state.event_bus.subscriber_count == 0))
        )
        with client.websocket_connect(WEBSOCKET_URL) as websocket:
            self.assertEqual(websocket.receive_json()["type"], "system.connected")


class SlotReservationTest(unittest.IsolatedAsyncioTestCase):
    """The last slot is claimed atomically, before any response header."""

    STREAM = "/api/v1/events/stream"

    def build(self, **settings):
        app = create_app(
            make_settings(
                event_max_subscribers=1, event_heartbeat_seconds=0.05, **settings
            )
        )
        router = APIRouter()

        @router.get("/test/slow")
        async def slow(
            reservation: Annotated[Reservation, Depends(reserve_event_slot)],
        ):
            await asyncio.sleep(60)  # a request that never gets to stream

        @router.get("/test/fails")
        async def fails(
            reservation: Annotated[Reservation, Depends(reserve_event_slot)],
        ):
            raise ApiError(500, "test_failure", "fails after reserving")

        app.include_router(router)
        return signed_in(app)

    async def test_concurrent_streams_for_the_last_slot_get_one_200_and_the_rest_503(
        self,
    ):
        app = self.build()
        bus = app.state.event_bus
        async with app.router.lifespan_context(app):
            results = await asyncio.gather(
                *(read_sse(app, self.STREAM, chunks=2) for _ in range(6)),
                return_exceptions=True,
            )

            # No client saw a 200 followed by a broken stream.
            self.assertEqual([r for r in results if isinstance(r, BaseException)], [])
            self.assertEqual(
                sorted(start["status"] for start, _ in results), [200] + [503] * 5
            )
            for start, bodies in results:
                if start["status"] == 503:
                    error = json.loads(b"".join(bodies))["error"]
                    self.assertEqual(error["code"], "event_capacity_reached")
            self.assertTrue(
                await wait_until(lambda: bus.slots_in_use == 0),
                "a slot leaked after every client had gone",
            )

    async def test_concurrent_websockets_for_the_last_slot_get_one_socket_and_1013(
        self,
    ):
        app = self.build()
        bus = app.state.event_bus
        clients = [AsgiWebSocket(app) for _ in range(6)]

        self.assertTrue(
            await wait_until(lambda: sum(c.task.done() for c in clients) == 5)
        )
        open_clients = [c for c in clients if not c.task.done()]
        self.assertEqual(len(open_clients), 1)
        self.assertEqual([c.close_code for c in clients if c.task.done()], [1013] * 5)
        self.assertEqual(bus.slots_in_use, 1)

        open_clients[0].disconnect()
        await asyncio.gather(*(c.task for c in clients))
        self.assertTrue(await wait_until(lambda: bus.slots_in_use == 0))

    async def test_the_slot_is_reserved_before_the_request_body_runs(self):
        app = self.build()
        bus = app.state.event_bus
        request = asyncio.create_task(read_sse(app, "/test/slow", chunks=1, limit=30))
        self.assertTrue(await wait_until(lambda: bus.slots_in_use == 1))
        self.assertEqual(bus.subscriber_count, 0, "reserved, not yet streaming")

        # The last slot is taken, so a stream is refused with the standard 503.
        start, _ = await read_sse(app, self.STREAM, chunks=1)
        self.assertEqual(start["status"], 503)

        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertTrue(
            await wait_until(lambda: bus.slots_in_use == 0),
            "cancelling the request leaked its slot",
        )

    async def test_the_slot_is_released_when_the_request_fails_before_streaming(self):
        app = self.build()
        start, _ = await read_sse(app, "/test/fails", chunks=1)
        self.assertEqual(start["status"], 500)
        self.assertEqual(app.state.event_bus.slots_in_use, 0)

    async def test_the_slot_is_released_when_the_client_disconnects_at_once(self):
        app = self.build()

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        async with app.router.lifespan_context(app):
            await app(http_scope(self.STREAM), receive, send)
        self.assertEqual(app.state.event_bus.slots_in_use, 0)


class ServerSentEventsTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream_yields_connected_then_heartbeat(self):
        app = signed_in(create_app(make_settings(event_heartbeat_seconds=0.02)))
        async with app.router.lifespan_context(app):
            start, chunks = await read_sse(app, "/api/v1/events/stream", chunks=2)

        self.assertEqual(start["status"], 200)
        headers = {k.decode(): v.decode() for k, v in start["headers"]}
        self.assertTrue(headers["content-type"].startswith("text/event-stream"))
        # The endpoint's own value wins over the security-header default.
        self.assertEqual(headers["cache-control"], "no-cache")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertIn("x-request-id", headers)

        connected, beat = (chunk.decode() for chunk in chunks)
        self.assertIn("event: system.connected\n", connected)
        self.assertIn("event: system.heartbeat\n", beat)
        payload = json.loads(
            next(line for line in beat.splitlines() if line.startswith("data: "))[6:]
        )
        self.assertEqual(payload["type"], "system.heartbeat")
        self.assertEqual(payload["data"], {})

    async def test_stream_forwards_events_published_on_the_bus(self):
        app = signed_in(create_app(make_settings(event_heartbeat_seconds=60)))
        bus = app.state.event_bus
        reader = asyncio.create_task(read_sse(app, "/api/v1/events/stream", chunks=2))
        self.assertTrue(await wait_until(lambda: bus.subscriber_count == 1))
        event = heartbeat()
        bus.publish(event)
        _, chunks = await reader
        self.assertIn(f"id: {event.id}\n", chunks[1].decode())

    async def test_disconnect_removes_the_subscription(self):
        app = signed_in(create_app(make_settings(event_heartbeat_seconds=0.02)))
        async with app.router.lifespan_context(app):
            await read_sse(app, "/api/v1/events/stream", chunks=1)
            self.assertTrue(
                await wait_until(lambda: app.state.event_bus.subscriber_count == 0)
            )


# -- issue #188: the stream needs a session and routes the notification events --

ADMIN_AUDIENCE = Capability.ADMIN_SYSTEM_HEALTH_VIEW.value


class ChangingProvider:
    """Authenticates as ``who``; a later session check answers ``later`` (a
    principal, ``None``, or an exception to raise)."""

    def __init__(self, who, later) -> None:
        self.who = who
        self.later = later
        self.get_calls = 0
        self.revalidations = 0

    async def get_principal(self, connection):
        self.get_calls += 1
        return self.who

    async def revalidate(self, connection):
        self.revalidations += 1
        if isinstance(self.later, Exception):
            raise self.later
        return self.later


def stream_app(provider=None, **settings):
    app = create_app(make_settings(event_heartbeat_seconds=60, **settings))
    if provider is not None:
        app.state.principal_provider = provider
    return app


def event_types(chunks) -> list[str]:
    return [
        line[len("event: ") :]
        for chunk in chunks
        for line in chunk.decode().splitlines()
        if line.startswith("event: ")
    ]


class AnonymousStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_anonymous_stream_is_refused_without_taking_a_slot(self):
        app = stream_app(event_max_subscribers=1)
        bus = app.state.event_bus
        with bus.subscribe():  # the bus is full: still 401, never 503
            start, bodies = await read_sse(app, "/api/v1/events/stream", chunks=1)
        self.assertEqual(start["status"], 401)
        self.assertEqual(json.loads(b"".join(bodies))["error"]["code"], "unauthorized")
        self.assertEqual(bus.slots_in_use, 0)

    def test_an_anonymous_websocket_is_refused_with_1008(self):
        app = stream_app()
        with self.assertRaises(WebSocketDisconnect) as caught:
            with make_client(app).websocket_connect(WEBSOCKET_URL):
                pass
        self.assertEqual(caught.exception.code, 1008)
        self.assertEqual(app.state.event_bus.subscriber_count, 0)

    async def test_the_system_role_gets_no_stream(self):
        app = signed_in(stream_app(), principal(SystemRole.SYSTEM))
        start, _ = await read_sse(app, "/api/v1/events/stream", chunks=1)
        self.assertEqual(start["status"], 403)


class AudienceRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_notification_event_reaches_its_audience_only(self):
        bus = EventBus()
        user = Viewer(U1, frozenset())
        admin = Viewer(U2, frozenset({ADMIN_AUDIENCE}))
        with (
            bus.subscribe(user) as for_user,
            bus.subscribe(admin) as for_admin,
            bus.subscribe() as nobody,
        ):
            mine = notification_changed(user_ids=frozenset({U1}))
            admins = notification_changed(capability=ADMIN_AUDIENCE)
            bus.publish(mine)
            bus.publish(admins)
            beat = heartbeat()
            bus.publish(beat)
            self.assertEqual(await for_user.get(), mine)
            self.assertEqual(await for_user.get(), beat)
            self.assertEqual(await for_admin.get(), admins)
            self.assertEqual(await for_admin.get(), beat)
            self.assertEqual(await nobody.get(), beat)

    def test_the_audience_is_never_sent_and_is_required(self):
        event = notification_changed(user_ids=frozenset({U1}))
        payload = event.model_dump(mode="json")
        self.assertEqual(set(payload), {"id", "type", "occurred_at", "data"})
        self.assertEqual(payload["data"], {})
        self.assertNotIn(str(U1), event.model_dump_json())
        with self.assertRaises(ValueError):
            notification_changed()

    async def test_a_stream_forwards_its_users_notification_events_only(self):
        app = signed_in(stream_app(), principal(SystemRole.USER, U1))
        bus = app.state.event_bus
        reader = asyncio.create_task(read_sse(app, "/api/v1/events/stream", chunks=3))
        self.assertTrue(await wait_until(lambda: bus.subscriber_count == 1))
        bus.publish(notification_changed(user_ids=frozenset({U2})))
        bus.publish(notification_changed(capability=ADMIN_AUDIENCE))
        bus.publish(notification_changed(user_ids=frozenset({U1})))
        bus.publish(heartbeat())
        _, chunks = await reader
        self.assertEqual(
            event_types(chunks),
            ["system.connected", "notification.changed", "system.heartbeat"],
        )

    async def test_an_admin_stream_receives_the_admin_audience(self):
        app = signed_in(stream_app(), principal(SystemRole.ADMIN, U2))
        bus = app.state.event_bus
        reader = asyncio.create_task(read_sse(app, "/api/v1/events/stream", chunks=2))
        self.assertTrue(await wait_until(lambda: bus.subscriber_count == 1))
        bus.publish(notification_changed(capability=ADMIN_AUDIENCE))
        _, chunks = await reader
        self.assertEqual(
            event_types(chunks), ["system.connected", "notification.changed"]
        )


class SessionCheckTest(unittest.IsolatedAsyncioTestCase):
    STREAM = "/api/v1/events/stream"

    async def ends(self, later) -> tuple[list[bytes], ChangingProvider, EventBus]:
        provider = ChangingProvider(principal(SystemRole.USER, U1), later)
        app = stream_app(provider, event_session_check_seconds=1)
        start, chunks = await read_sse(app, self.STREAM, chunks=2, limit=5)
        self.assertEqual(start["status"], 200)
        return chunks, provider, app.state.event_bus

    async def test_the_stream_ends_when_the_session_is_gone(self):
        chunks, provider, bus = await self.ends(None)
        self.assertEqual(event_types(chunks), ["system.connected"])
        self.assertEqual(provider.revalidations, 1)
        # Checked without the request's cache and without touching the session.
        self.assertEqual(provider.get_calls, 1)
        self.assertTrue(await wait_until(lambda: bus.slots_in_use == 0))

    async def test_the_stream_ends_when_another_user_answers(self):
        chunks, _, _ = await self.ends(principal(SystemRole.USER, U2))
        self.assertEqual(event_types(chunks), ["system.connected"])

    async def test_the_stream_ends_when_the_check_fails(self):
        chunks, _, _ = await self.ends(RuntimeError("database down"))
        self.assertEqual(event_types(chunks), ["system.connected"])

    async def test_a_role_change_applies_from_the_check_on(self):
        provider = ChangingProvider(
            principal(SystemRole.ADMIN, U1), principal(SystemRole.USER, U1)
        )
        app = stream_app(provider, event_session_check_seconds=1)
        bus = app.state.event_bus
        reader = asyncio.create_task(read_sse(app, self.STREAM, chunks=2, limit=5))
        self.assertTrue(await wait_until(lambda: bus.subscriber_count == 1))
        self.assertTrue(await wait_until(lambda: provider.revalidations >= 1, 3))
        # Demoted: the admin audience no longer reaches this stream.
        bus.publish(notification_changed(capability=ADMIN_AUDIENCE))
        bus.publish(heartbeat())
        _, chunks = await reader
        self.assertEqual(event_types(chunks), ["system.connected", "system.heartbeat"])

    def test_a_websocket_is_closed_with_1008_when_the_session_is_gone(self):
        provider = ChangingProvider(principal(SystemRole.USER, U1), None)
        app = stream_app(provider, event_session_check_seconds=1)
        with make_client(app).websocket_connect(WEBSOCKET_URL) as websocket:
            self.assertEqual(websocket.receive_json()["type"], "system.connected")
            with self.assertRaises(WebSocketDisconnect) as caught:
                websocket.receive_json()
        self.assertEqual(caught.exception.code, 1008)
        self.assertEqual(provider.revalidations, 1)


if __name__ == "__main__":
    unittest.main()
