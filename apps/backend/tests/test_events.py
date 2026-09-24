import asyncio
import json
import unittest

from fastapi.testclient import TestClient
from pydantic import ValidationError

from paw_backend.app import create_app
from paw_backend.events import Event, EventBus, EventType, publish_heartbeats

from .support import make_settings, read_sse, wait_until


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
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/events/ws") as websocket:
                connected = websocket.receive_json()
                beat = websocket.receive_json()
        self.assertEqual(connected["type"], "system.connected")
        self.assertEqual(beat["type"], "system.heartbeat")
        self.assertEqual(set(beat), {"id", "type", "occurred_at", "data"})

    def test_client_messages_are_ignored(self):
        app = create_app(make_settings(event_heartbeat_seconds=0.02))
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/events/ws") as websocket:
                websocket.receive_json()
                websocket.send_text("ignored")
                websocket.send_bytes(b"ignored")
                self.assertEqual(websocket.receive_json()["type"], "system.heartbeat")

    def test_disconnect_removes_the_subscription(self):
        app = create_app(make_settings(event_heartbeat_seconds=0.02))
        bus = app.state.event_bus
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/events/ws") as websocket:
                websocket.receive_json()
                self.assertEqual(bus.subscriber_count, 1)
            self.assertTrue(
                asyncio.run(wait_until(lambda: bus.subscriber_count == 0)),
                "subscription was not released after the client disconnected",
            )


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
