"""In-process event bus behind the SSE and WebSocket endpoints.

Scope of this skeleton: only system events exist (``system.connected`` and
``system.heartbeat``). They carry no user, project or task data. Later issues
add event types together with the authorization they need.

The bus is process-local. Events are neither persisted nor replayed, so a
client that reconnects only sees events published after it subscribed.
"""

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class EventType(StrEnum):
    SYSTEM_CONNECTED = "system.connected"
    SYSTEM_HEARTBEAT = "system.heartbeat"


class Event(BaseModel):
    """Wire format shared by the SSE and WebSocket endpoints."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    type: EventType
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = Field(default_factory=dict)


class Subscription:
    """A bounded queue of events for one connected client."""

    def __init__(self, queue_size: int) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=queue_size)

    def put(self, event: Event) -> None:
        """Enqueue without blocking; a slow client loses its oldest event."""
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        self._queue.put_nowait(event)

    async def get(self) -> Event:
        return await self._queue.get()


class EventBusFull(Exception):
    """The bus already has its maximum number of subscribers."""


class EventBus:
    """Fan-out of published events to every current subscriber.

    The number of subscribers is capped: every connected client holds a queue
    and a task, so an unbounded number would let a client exhaust memory.
    """

    def __init__(self, queue_size: int = 100, max_subscribers: int = 100) -> None:
        self._queue_size = queue_size
        self._max_subscribers = max_subscribers
        self._subscriptions: set[Subscription] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    @property
    def is_full(self) -> bool:
        return len(self._subscriptions) >= self._max_subscribers

    def publish(self, event: Event) -> None:
        for subscription in tuple(self._subscriptions):
            subscription.put(event)

    @contextmanager
    def subscribe(self) -> Iterator[Subscription]:
        """Register a subscriber; raises ``EventBusFull`` at the cap."""
        if self.is_full:
            raise EventBusFull
        subscription = Subscription(self._queue_size)
        self._subscriptions.add(subscription)
        try:
            yield subscription
        finally:
            self._subscriptions.discard(subscription)


async def publish_heartbeats(bus: EventBus, interval_seconds: float) -> None:
    """Publish ``system.heartbeat`` forever; cancel the task to stop it."""
    while True:
        await asyncio.sleep(interval_seconds)
        bus.publish(Event(type=EventType.SYSTEM_HEARTBEAT))
