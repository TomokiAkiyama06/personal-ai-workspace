import asyncio
import json
import unittest

from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from paw_backend.app import create_app
from paw_backend.events import (
    Event,
    EventBus,
    EventBusFull,
    EventType,
    publish_heartbeats,
)

from .support import (
    WEBSOCKET_URL,
    make_client,
    make_settings,
    read_sse,
    wait_until,
)


def heartbeat() -> Event:
    return Event(type=EventType.SYSTEM_HEARTBEAT)


class EventModelTest(unittest.TestCase):
    def test_only_system_event_types_exist(self):
        self.assertEqual(
            {member.value for member in EventType},
            {"system.connected", "system.heartbeat"},
        )
        with self.assertRaises(ValidationError):
            Event(type="task.created")

    def test_wire_format(self):
        payload = heartbeat().model_dump(mode="json")
        self.assertEqual(set(payload), {"id", "type", "occurred_at", "data"})
        self.assertEqual(payload["type"], "system.heartbeat")
        self.assertEqual(payload["data"], {})
        self.assertTrue(payload["occurred_at"].endswith("Z"))


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
        app = create_app(make_settings(event_heartbeat_seconds=0.02))
        with make_client(app) as client:
            with client.websocket_connect(WEBSOCKET_URL) as websocket:
                connected = websocket.receive_json()
                beat = websocket.receive_json()
        self.assertEqual(connected["type"], "system.connected")
        self.assertEqual(beat["type"], "system.heartbeat")
        self.assertEqual(set(beat), {"id", "type", "occurred_at", "data"})

    def test_client_messages_are_ignored(self):
        app = create_app(make_settings(event_heartbeat_seconds=0.02))
        with make_client(app) as client:
            with client.websocket_connect(WEBSOCKET_URL) as websocket:
                websocket.receive_json()
                websocket.send_text("ignored")
                websocket.send_bytes(b"ignored")
                self.assertEqual(websocket.receive_json()["type"], "system.heartbeat")

    def test_disconnect_removes_the_subscription(self):
        app = create_app(make_settings(event_heartbeat_seconds=0.02))
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
        client = make_client(create_app(make_settings(**settings)))
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
        app = create_app(make_settings())
        with self.assertRaises(WebSocketDisconnect):
            with make_client(app).websocket_connect(
                WEBSOCKET_URL, headers={"Origin": "https://evil.example"}
            ):
                pass
        self.assertEqual(app.state.event_bus.subscriber_count, 0)


class SubscriberCapTest(unittest.TestCase):
    def test_sse_over_the_cap_gets_a_503_error_body(self):
        app = create_app(make_settings(event_max_subscribers=1))
        with app.state.event_bus.subscribe():
            response = make_client(app).get("/api/v1/events/stream")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "event_capacity_reached")
        self.assertEqual(
            response.json()["error"]["request_id"], response.headers["x-request-id"]
        )

    def test_websocket_over_the_cap_is_closed_with_1013(self):
        app = create_app(make_settings(event_max_subscribers=1))
        with app.state.event_bus.subscribe():
            with make_client(app).websocket_connect(WEBSOCKET_URL) as websocket:
                with self.assertRaises(WebSocketDisconnect) as caught:
                    websocket.receive_json()
            # The refused client did not take a slot; only the held one remains.
            self.assertEqual(app.state.event_bus.subscriber_count, 1)
        self.assertEqual(caught.exception.code, 1013)

    def test_a_slot_is_available_again_after_a_client_leaves(self):
        app = create_app(make_settings(event_max_subscribers=1))
        client = make_client(app)
        with client.websocket_connect(WEBSOCKET_URL) as websocket:
            websocket.receive_json()
        self.assertTrue(
            asyncio.run(wait_until(lambda: app.state.event_bus.subscriber_count == 0))
        )
        with client.websocket_connect(WEBSOCKET_URL) as websocket:
            self.assertEqual(websocket.receive_json()["type"], "system.connected")


class ServerSentEventsTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream_yields_connected_then_heartbeat(self):
        app = create_app(make_settings(event_heartbeat_seconds=0.02))
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
        app = create_app(make_settings(event_heartbeat_seconds=60))
        bus = app.state.event_bus
        reader = asyncio.create_task(read_sse(app, "/api/v1/events/stream", chunks=2))
        self.assertTrue(await wait_until(lambda: bus.subscriber_count == 1))
        event = heartbeat()
        bus.publish(event)
        _, chunks = await reader
        self.assertIn(f"id: {event.id}\n", chunks[1].decode())

    async def test_disconnect_removes_the_subscription(self):
        app = create_app(make_settings(event_heartbeat_seconds=0.02))
        async with app.router.lifespan_context(app):
            await read_sse(app, "/api/v1/events/stream", chunks=1)
            self.assertTrue(
                await wait_until(lambda: app.state.event_bus.subscriber_count == 0)
            )


if __name__ == "__main__":
    unittest.main()
