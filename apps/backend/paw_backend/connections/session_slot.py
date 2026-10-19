"""The one session slot of a connection (Decision 0083, 6 and its approval's
condition (2); issue #239).

A CLI whose login is a file it refreshes (Codex's ChatGPT login: a single-use
refresh token) runs one session per connection at a time: two sessions on the
same login would make each other's refresh token invalid, and the one that
wrote last could store an invalid one. The backend may run in several
processes, so the slot is a database row (``connection_session_leases``,
revision ``0239``), not a lock of one process:

* :meth:`SessionSlot.acquire` takes the row (or takes over an expired one) and
  tries again while somebody else holds it, for at most the wait (60 seconds,
  provisional), BEFORE the admission; ``False`` when the wait ran out (the
  service then raises ``SessionSlotBusyError`` without admitting anything);
* while held, a heartbeat moves the expiry every ``heartbeat_seconds``;
* a holder that cannot renew stops its own session before the lease could
  expire (the approval's condition (2)): once ``ttl - margin`` seconds have
  passed since the last renewal that was sent and confirmed, :attr:`lost` is
  set and the service cancels the session (the adapter stops the CLI and
  writes its credential back first); another process can take the slot over
  only once the lease has expired, ``margin`` seconds later at the earliest;
* :meth:`release` stops the heartbeat and deletes the holder's row, unless
  the adapter said its CLI could not be stopped (:meth:`keep`): then the slot
  stays held, its heartbeat running, as long as this process lives (the
  operator stops the unit; a process that ends lets the lease expire).

The margin is the stop's worst case on production's launcher
(``cli_session.SystemdLauncher``, each ``systemctl`` call bounded by 30 s):
reading how the unit ended (at most 10 s of polls, then up to three calls:
about 100 s), ``systemctl stop`` three times with a second between (about
92 s), then the write-back (a few database statements and the store's file):
about 200 s, inside the 240 s margin. A lease lasts 300 s after its last
renewal, so a holder that cannot renew gives its session up 60 s after the
last renewal it sent and has 240 s to finish before another process can take
the slot over.

The instants of the row are the database's; the holder's own deadline is its
monotonic clock, measured from before each renewal was sent (so it is never
later than the database's expiry).
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from paw_backend.connections.domain import ConnectionKind
from paw_backend.connections.store import ConnectionStore
from paw_backend.research.providers.broker import log_type_name

logger = logging.getLogger(__name__)

#: Decision 0083, 6: how long a second session waits for the slot (provisional).
SLOT_WAIT_SECONDS: Final = 60.0
#: How long a lease lasts after its last heartbeat.
SLOT_TTL_SECONDS: Final = 300.0
#: How often the holder renews it.
SLOT_HEARTBEAT_SECONDS: Final = 10.0
#: How long before the lease could expire the holder gives its session up
#: when it could not renew: the worst case of stopping the CLI and writing
#: its credential back (see the module).
SLOT_STOP_MARGIN_SECONDS: Final = 240.0
_FIRST_RETRY_SECONDS: Final = 0.25
_MAX_RETRY_SECONDS: Final = 2.0


def check_slot_timing(
    wait_seconds: float,
    ttl_seconds: float,
    heartbeat_seconds: float,
    margin_seconds: float,
) -> None:
    """``ValueError`` unless a heartbeat comes well before the holder's own
    deadline (``ttl - margin``) and every number is positive."""
    values = (wait_seconds, ttl_seconds, heartbeat_seconds, margin_seconds)
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0
        for v in values
    ):
        raise ValueError("slot timing")
    if not heartbeat_seconds < ttl_seconds - margin_seconds:
        raise ValueError("slot timing")


class SessionSlot:
    """One acquisition of a connection's session slot (see the module)."""

    def __init__(
        self,
        store: ConnectionStore,
        kind: ConnectionKind,
        *,
        call: Callable[[Awaitable], Awaitable] | None = None,
        ttl_seconds: float = SLOT_TTL_SECONDS,
        heartbeat_seconds: float = SLOT_HEARTBEAT_SECONDS,
        margin_seconds: float = SLOT_STOP_MARGIN_SECONDS,
    ) -> None:
        self._store = store
        self._kind = kind
        self._call = call
        self._ttl = float(ttl_seconds)
        self._heartbeat = float(heartbeat_seconds)
        self._margin = float(margin_seconds)
        self.holder = uuid.uuid4()
        #: Set once the holder must stop its session (see the module).
        self.lost = asyncio.Event()
        self._confirmed = 0.0
        self._task: asyncio.Task | None = None
        self._keep = False

    def __repr__(self) -> str:
        return f"SessionSlot(kind={self._kind.value})"

    async def _run(self, awaitable: Awaitable):
        return await (awaitable if self._call is None else self._call(awaitable))

    async def acquire(self, wait_seconds: float) -> bool:
        """Take the slot, trying again while it is held, for at most
        ``wait_seconds``. A database error passes (nothing was taken)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(wait_seconds)
        delay = _FIRST_RETRY_SECONDS
        while True:
            sent = loop.time()
            taken = await self._run(
                self._store.acquire_session_slot(self._kind, self.holder, self._ttl)
            )
            if taken:
                self._confirmed = sent
                self._task = asyncio.ensure_future(self._beat())
                return True
            left = deadline - loop.time()
            if left <= 0:
                return False
            await asyncio.sleep(min(delay, left))
            delay = min(delay * 2, _MAX_RETRY_SECONDS)

    async def _beat(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            deadline = self._confirmed + self._ttl - self._margin
            await asyncio.sleep(max(0.0, min(self._heartbeat, deadline - loop.time())))
            if loop.time() >= deadline:
                break
            sent = loop.time()
            try:
                async with asyncio.timeout(max(0.0, deadline - sent)):
                    renewed = await self._run(
                        self._store.renew_session_slot(
                            self._kind, self.holder, self._ttl
                        )
                    )
            except TimeoutError:
                continue  # the deadline is checked above
            except Exception as error:
                logger.warning(
                    "connection session slot not renewed: kind=%s exception_type=%s",
                    self._kind.value,
                    log_type_name(error),
                )
                continue
            if not renewed:
                break
            self._confirmed = sent
        logger.error(
            "connection session slot lost; the session is stopped: kind=%s",
            self._kind.value,
        )
        self.lost.set()

    async def _hold(self) -> None:
        """Keep the slot of a CLI that could not be stopped for as long as
        this process lives: renew it, or take it again once it is free or
        expired (only a lease that expired can be taken over). Errors are
        logged and tried again."""
        while True:
            try:
                held = await self._run(
                    self._store.renew_session_slot(self._kind, self.holder, self._ttl)
                ) or await self._run(
                    self._store.acquire_session_slot(self._kind, self.holder, self._ttl)
                )
                if not held:
                    logger.error(
                        "connection session slot taken by another holder while"
                        " a CLI that could not be stopped may run: kind=%s",
                        self._kind.value,
                    )
            except Exception as error:
                logger.warning(
                    "connection session slot not renewed: kind=%s exception_type=%s",
                    self._kind.value,
                    log_type_name(error),
                )
            await asyncio.sleep(self._heartbeat)

    def keep(self) -> None:
        """The session's CLI could not be stopped (it may still run with the
        login): :meth:`release` will keep the slot held."""
        self._keep = True

    async def release(self) -> None:
        """Stop the heartbeat and delete the holder's row (best effort: a row
        left behind expires by itself). After :meth:`keep`, neither: the slot
        stays held while this process lives. Never raises."""
        if self._keep:
            logger.error(
                "connection session slot kept: its CLI could not be stopped;"
                " stop the session's unit (the slot is held while this process"
                " runs): kind=%s",
                self._kind.value,
            )
            if self._task is None or self._task.done():
                # The heartbeat gave up (the slot was being lost when the
                # session was cancelled): hold it again from here on.
                self._task = asyncio.ensure_future(self._hold())
            return
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        try:
            await self._run(self._store.release_session_slot(self._kind, self.holder))
        except Exception as error:
            logger.warning(
                "connection session slot not released (it expires):"
                " kind=%s exception_type=%s",
                self._kind.value,
                log_type_name(error),
            )


async def run_within(slot: SessionSlot, work: Awaitable) -> bool:
    """Run ``work`` while ``slot`` is held. ``True``: it ended by itself (its
    exception passes). ``False``: the slot was lost first; ``work`` was
    cancelled and waited for to its end (the adapter's stop and write-back).
    A cancellation of the caller cancels ``work``, waits for it the same way,
    and is raised."""
    task = asyncio.ensure_future(work)
    lost = asyncio.ensure_future(slot.lost.wait())
    try:
        try:
            await asyncio.wait({task, lost}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            task.cancel()
            await _to_its_end(task)
            raise
    finally:
        lost.cancel()
    if task.done():
        task.result()
        return True
    task.cancel()
    if await _to_its_end(task):
        raise asyncio.CancelledError
    if not task.cancelled():
        task.result()  # an exception it raised while stopping passes
    return False


async def _to_its_end(task: asyncio.Future) -> bool:
    """Wait until ``task`` is done, through cancellations of the caller;
    whether one came."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


__all__ = [
    "SLOT_HEARTBEAT_SECONDS",
    "SLOT_STOP_MARGIN_SECONDS",
    "SLOT_TTL_SECONDS",
    "SLOT_WAIT_SECONDS",
    "SessionSlot",
    "check_slot_timing",
    "run_within",
]
