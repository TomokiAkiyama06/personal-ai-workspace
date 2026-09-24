"""The Backend's janitor of the Research Scratch Store (PAW-050).

``ScratchStore`` hides an expired item at once (it is "not found" for every
method), but only ``purge_expired`` removes the row and the research content
inside it. Something has to call it regularly, otherwise the 24 hour TTL is not
enforced in the database. That is this loop; the application starts it in its
lifespan (``paw_backend.app``) when a database is configured and
``PAW_SCRATCH_PURGE_INTERVAL_SECONDS`` is greater than 0.

Behaviour
---------
* **A tick** calls ``purge_expired`` batch after batch until it reports that
  nothing more is due (``has_more`` is False), but at most ``max_batches``
  times, so one tick has a bounded cost. If the bound is reached with work left,
  the next tick starts after :data:`CATCH_UP_DELAY_SECONDS` instead of a whole
  interval; the backlog is worked off in slices and never in one long run.
* **The loop** sleeps :data:`FIRST_TICK_DELAY_SECONDS` (or the interval, if that
  is shorter) after it starts, so that startup, the privilege diagnostics and a
  restart loop do not compete with a purge and a Backend that is stopped again
  at once never opens a purge connection; then it runs a tick and sleeps
  ``interval_seconds`` between ticks. A Backend that restarts more often than
  every 30 seconds therefore never purges.
* **Failure isolation.** A tick that raises does not end the loop. It logs only
  the exception TYPE (the text of a database error can hold SQL parameters, that
  is research content), waits with an exponential back-off that starts at
  :data:`RETRY_BASE_SECONDS`, is capped at ``interval_seconds`` and goes back to
  the normal interval after the next success. ``asyncio.CancelledError`` (and
  every other ``BaseException``) is never caught.
* **Stopping** is cancelling the task returned by ``asyncio.create_task(
  janitor.run())``: the loop is at ``await`` the whole time, so it stops at the
  next suspension point. Nothing is left running and nothing is kept between
  ticks except a failure counter. One limit: a purge that is *inside a query*
  when PostgreSQL stalls is cancelled the way every pooled query is (psycopg asks
  the server to cancel and waits, up to about ten seconds; with a libpq older
  than 17 from a thread that the interpreter waits for at exit). Unlike the
  startup diagnostics it is not on a dedicated, abortable connection: a purge is
  several statements in one transaction. The lifespan's wait is bounded anyway
  (see ``paw_backend.app``).
* **Several Backend processes** may each run a janitor: ``purge_expired`` locks
  its rows with ``SKIP LOCKED``, so two of them never wait for each other and
  never delete a row twice.

Time
----
The janitor has no clock of its own. The store decides what is expired with its
own injected clock (``ScratchStore(clock=...)``); the janitor only decides when
to call it, and its ``sleep`` is injectable so that tests never wait.
"""

import asyncio
import inspect
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from paw_backend.research.scratch.limits import (
    DEFAULT_PURGE_BATCH_SIZE,
    MAX_PURGE_BATCH_SIZE,
)
from paw_backend.research.scratch.records import PurgeResult

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


class Purger(Protocol):
    """What the janitor needs of the store: ``ScratchStore`` is one."""

    async def purge_expired(self, *, batch_size: int) -> PurgeResult: ...


# The interval is at most the TTL: a longer one could not enforce the 24 hours.
MAX_INTERVAL_SECONDS = 86_400
# Batches per tick: with the default batch size one tick removes at most 50 000
# rows. The rest is left for the next tick (see CATCH_UP_DELAY_SECONDS).
DEFAULT_MAX_BATCHES = 100
MAX_MAX_BATCHES = 10_000
# Wait after a tick that stopped at its batch bound with work left, and the
# first wait after a failed tick (it doubles up to the interval).
CATCH_UP_DELAY_SECONDS = 5.0
RETRY_BASE_SECONDS = 30.0
# Wait between the start of the loop and its first tick.
FIRST_TICK_DELAY_SECONDS = 30.0
# Doubling stops here, so that the failure counter stays a small integer.
_MAX_BACKOFF_STEPS = 20


@dataclass(frozen=True, slots=True)
class PurgeRun:
    """The outcome of one tick.

    ``purged``: rows deleted by all its batches. ``deferred``: expired rows left
    because they are pinned, in use or awaiting a promotion decision (as counted
    by the last batch). ``batches``: how many times ``purge_expired`` ran.
    ``has_more``: the tick stopped at its batch bound and purgeable rows remain.
    """

    purged: int
    deferred: int
    batches: int
    has_more: bool


class ScratchJanitor:
    """Calls ``ScratchStore.purge_expired`` regularly (see the module docstring)."""

    def __init__(
        self,
        store: Purger,
        *,
        interval_seconds: float,
        batch_size: int = DEFAULT_PURGE_BATCH_SIZE,
        max_batches: int = DEFAULT_MAX_BATCHES,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Validate the arguments up front; a wrong one fails here, loudly.

        ``store`` is a :class:`ScratchStore` (or anything with its
        ``purge_expired(*, batch_size)``; ``TypeError`` otherwise).
        ``interval_seconds`` must be a finite number greater than 0 and at most
        :data:`MAX_INTERVAL_SECONDS` (the application only ever passes 60 to
        86400, see ``Settings``); ``batch_size`` an ``int`` from 1 to
        ``MAX_PURGE_BATCH_SIZE``; ``max_batches`` an ``int`` from 1 to
        :data:`MAX_MAX_BATCHES`; ``sleep`` a callable taking the seconds and
        returning an awaitable (tests pass a fake that never waits). A
        ``TypeError`` or ``ValueError`` names the argument, never its value.
        """
        purge = getattr(store, "purge_expired", None)
        if not callable(purge):
            raise TypeError("store must have a purge_expired method")
        try:
            inspect.signature(purge).bind(batch_size=1)
        except TypeError:
            raise TypeError("purge_expired must accept batch_size") from None
        except ValueError:
            pass  # a callable without an introspectable signature
        if isinstance(interval_seconds, bool) or not isinstance(
            interval_seconds, int | float
        ):
            raise TypeError("interval_seconds must be a number")
        if not (
            math.isfinite(interval_seconds)
            and 0 < interval_seconds <= MAX_INTERVAL_SECONDS
        ):
            raise ValueError("interval_seconds is out of range")
        for name, value, maximum in (
            ("batch_size", batch_size, MAX_PURGE_BATCH_SIZE),
            ("max_batches", max_batches, MAX_MAX_BATCHES),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int")
            if not 1 <= value <= maximum:
                raise ValueError(f"{name} is out of range")
        if not callable(sleep):
            raise TypeError("sleep must be callable")
        try:
            inspect.signature(sleep).bind(1.0)
        except TypeError:
            raise TypeError("sleep must be callable with the seconds") from None
        except ValueError:
            pass  # a callable without an introspectable signature
        self._store = store
        self._interval = float(interval_seconds)
        self._batch_size = batch_size
        self._max_batches = max_batches
        self._sleep = sleep

    async def tick(self) -> PurgeRun:
        """One run: purge batch after batch until nothing is due or the bound.

        Errors of the store (for example a lost database connection or
        ``ScratchBusyError``) propagate; :meth:`run` handles them.
        """
        purged = 0
        deferred = 0
        batches = 0
        has_more = False
        while batches < self._max_batches:
            result = await self._store.purge_expired(batch_size=self._batch_size)
            batches += 1
            purged += result.purged
            deferred = result.deferred
            has_more = result.has_more
            if not has_more:
                break
        return PurgeRun(
            purged=purged, deferred=deferred, batches=batches, has_more=has_more
        )

    async def run(self) -> None:
        """Wait, tick, sleep, tick, ... until the task is cancelled. Never returns."""
        failures = 0
        await self._sleep(min(self._interval, FIRST_TICK_DELAY_SECONDS))
        while True:
            try:
                outcome = await self.tick()
            except Exception as error:  # a supervisor: one bad tick must not end it
                failures = min(failures + 1, _MAX_BACKOFF_STEPS)
                delay = min(self._interval, RETRY_BASE_SECONDS * 2 ** (failures - 1))
                logger.warning(
                    "Scratch purge failed (%s); retrying in %.0f s",
                    type(error).__name__,
                    delay,
                )
            else:
                failures = 0
                delay = (
                    min(self._interval, CATCH_UP_DELAY_SECONDS)
                    if outcome.has_more
                    else self._interval
                )
                if outcome.purged:
                    logger.info(
                        "Scratch purge removed %d expired item(s) in %d batch(es)",
                        outcome.purged,
                        outcome.batches,
                    )
            await self._sleep(delay)
