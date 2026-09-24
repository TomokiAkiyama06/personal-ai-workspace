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


class Reservation:
    """A subscriber slot claimed on the bus, from ``reserve()`` until ``release()``.

    ``attach()`` turns it into a subscription that receives events. ``release()``
    gives the slot back (and removes the subscription); it is safe to call any
    number of times, so it can sit in a ``finally`` on every exit path.
    """

    def __init__(self, bus: "EventBus") -> None:
        self._bus = bus
        self._subscription: Subscription | None = None
        self._released = False

    def attach(self) -> Subscription:
        if self._released:
            raise RuntimeError("the reservation was released")
        if self._subscription is None:
            self._subscription = Subscription(self._bus._queue_size)
            self._bus._subscriptions.add(self._subscription)
        return self._subscription

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._subscription is not None:
            self._bus._subscriptions.discard(self._subscription)
        self._bus._reserved -= 1


class EventBus:
    """Fan-out of published events to every current subscriber.

    The number of subscribers is capped: every connected client holds a queue
    and a task, so an unbounded number would let a client exhaust memory.

    A slot is claimed with ``reserve()``, which checks the cap and takes the
    slot in one synchronous step. Nothing can run in between, so concurrent
    requests for the last slot cannot both succeed, and the loser can be
    turned away before it has started a response.
    """

    def __init__(self, queue_size: int = 100, max_subscribers: int = 100) -> None:
        self._queue_size = queue_size
        self._max_subscribers = max_subscribers
        self._subscriptions: set[Subscription] = set()
        self._reserved = 0  # live reservations, attached or not

    @property
    def subscriber_count(self) -> int:
        """Subscriptions that currently receive events."""
        return len(self._subscriptions)

    @property
    def slots_in_use(self) -> int:
        """Reservations that have not been released (attached or not)."""
        return self._reserved

    @property
    def is_full(self) -> bool:
        return self._reserved >= self._max_subscribers

    def publish(self, event: Event) -> None:
        for subscription in tuple(self._subscriptions):
            subscription.put(event)

    def reserve(self) -> Reservation:
        """Claim a slot; raises ``EventBusFull`` at the cap. Never awaits."""
        if self.is_full:
            raise EventBusFull
        self._reserved += 1
        return Reservation(self)

    @contextmanager
    def subscribe(self) -> Iterator[Subscription]:
        """Reserve a slot and attach to it; raises ``EventBusFull`` at the cap."""
        reservation = self.reserve()
        try:
            yield reservation.attach()
        finally:
            reservation.release()


async def publish_heartbeats(bus: EventBus, interval_seconds: float) -> None:
    """Publish ``system.heartbeat`` forever; cancel the task to stop it."""
    while True:
        await asyncio.sleep(interval_seconds)
        bus.publish(Event(type=EventType.SYSTEM_HEARTBEAT))
