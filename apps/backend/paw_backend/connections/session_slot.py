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
  stays held as long as this process lives, the heartbeat replaced by a
  holder that renews it or takes it again after any failure (the operator
  stops the unit; a process that ends lets the lease expire).

A Codex session binds its slot to itself before its login is placed
(:meth:`SessionSlot.bind`, Decision 0096, 2 B: the session's UUID, the
credential handle and version it was given, on the row): such a row is never
taken over by an ordinary acquisition, even once expired, but only by the
collection loop that collects the login after the unit was confirmed stopped
(Decision 0096, 1 D). Before each side effect (placing the login,
starting the unit, the write-back) the session checks that it still holds the
slot (:meth:`SessionSlot.confirm`); once that fails it gives up for good and
leaves its row and directory to the collection loop (Decision 0096, 2's
fencing).

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
from typing import Final, Protocol

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
#: Decision 0096, 1 D: how often a kept session's unit is looked at and its
#: login collected once it stopped (provisional).
SLOT_COLLECT_SECONDS: Final = 60.0
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
        collector: "SessionCollector | None" = None,
        collect_seconds: float = SLOT_COLLECT_SECONDS,
    ) -> None:
        self._store = store
        # Decision 0096, 1 D: who collects a kept session's login, and how
        # often this holder tries (``None``: renewed only).
        self._collector = collector
        self._collect_seconds = float(collect_seconds)
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
        #: The Codex session this slot was bound to (Decision 0096, 2 B).
        self.session: uuid.UUID | None = None
        #: Set once a check before a side effect failed (Decision 0096, 2's
        #: fencing): the row is left to the collection loop.
        self.fenced = False
        #: The session whose slot :meth:`release` kept: its row is marked
        #: ``kept``, or (the database failed) the holding loop marks it
        #: before it renews it (Codex review of #291).
        self.kept_session: uuid.UUID | None = None
        #: Whether the ``kept`` mark got its answer (marked, or the row was
        #: not this holder's any more).
        self._marked = False
        #: Set by :meth:`release` once it deleted this holder's own row: the
        #: session's files are nobody else's to collect (Codex review of
        #: #291). ``False`` after a takeover, a database error, a kept or a
        #: fenced slot: the files must stay for the collection.
        self.given_back = False

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

    async def bind(self, session: uuid.UUID, handle: str, version: int) -> bool:
        """Record on the held lease the Codex session, the credential handle
        it was given and that credential's version, BEFORE its login is
        placed (Decision 0096, 2 B); a renewal as well. ``False`` (also on a
        database error): the session must not place its login."""
        loop = asyncio.get_running_loop()
        sent = loop.time()
        try:
            bound = await self._run(
                self._store.bind_session_slot(
                    self._kind, self.holder, session, handle, version, self._ttl
                )
            )
        except Exception as error:
            logger.error(
                "connection session slot not bound to its session: kind=%s"
                " exception_type=%s",
                self._kind.value,
                log_type_name(error),
            )
            return False
        if not bound:
            logger.error(
                "connection session slot not bound to its session (not held): kind=%s",
                self._kind.value,
            )
            return False
        self._confirmed = sent
        self.session = session
        return True

    async def confirm(self) -> bool:
        """The check before a side effect of a bound session (placing the
        login, starting the unit, writing back, deleting the session's
        directory: Decision 0096, 2's fencing (a)): the lease's own deadline
        (the last confirmed renewal plus the TTL, by the monotonic clock) has
        not come, and a renewal now succeeds (the row is still this holder's
        and unexpired). ``False``, also on a database error, from then on for
        good: the session must give up without touching its directory, and
        :meth:`release` leaves the row to the collection loop."""
        if self.fenced:
            return False
        loop = asyncio.get_running_loop()
        sent = loop.time()
        if sent >= self._confirmed + self._ttl:
            renewed = False
        else:
            try:
                renewed = await self._run(
                    self._store.renew_session_slot(self._kind, self.holder, self._ttl)
                )
            except Exception as error:
                logger.warning(
                    "connection session slot not confirmed: kind=%s exception_type=%s",
                    self._kind.value,
                    log_type_name(error),
                )
                renewed = False
        if not renewed:
            logger.error(
                "connection session slot no longer held; the session gives up and"
                " leaves its files to the collection: kind=%s session=%s",
                self._kind.value,
                self.session or "-",
            )
            self.fenced = True
            return False
        self._confirmed = max(self._confirmed, sent)
        return True

    async def record_version(self, version: int) -> None:
        """The immediate write-back stored ``version``: the bound row's
        expected version follows (Decision 0096, 2's version check makes a
        failure here harmless; logged by type)."""
        if self.session is None:
            return
        try:
            await self._run(
                self._store.record_session_slot_version(
                    self._kind, self.holder, version
                )
            )
        except Exception as error:
            logger.warning(
                "connection session slot's version not moved on: kind=%s"
                " exception_type=%s",
                self._kind.value,
                log_type_name(error),
            )

    async def _hold(self) -> None:
        """Keep the slot of a CLI that could not be stopped for as long as
        this process lives: renew it, or take it again once it is free or
        expired (only a lease that expired can be taken over). Errors are
        logged and tried again. A slot bound to a session is only renewed:
        once that fails (the collection loop took the row over, collected
        it, or it expired) this process lets it go for good (Decision 0096,
        2's fencing): an acquisition here would make an unbound row the
        collection never looks at. Its ``kept`` mark, when the database
        failed it, is tried again first and the row is not renewed before:
        a renewed row neither kept nor expired would block every session
        unseen by System Health (Codex review of #291); left alone, it
        expires and the collection loop takes it over."""
        while True:
            if self.session is not None and not self._marked:
                try:
                    marked = await self._run(
                        self._store.keep_session_slot(self._kind, self.holder)
                    )
                except Exception as error:
                    logger.warning(
                        "connection session slot not marked kept: kind=%s"
                        " exception_type=%s",
                        self._kind.value,
                        log_type_name(error),
                    )
                    await asyncio.sleep(self._heartbeat)
                    continue
                self._marked = True
                if marked is None:
                    logger.error(
                        "connection session slot of a CLI that could not be"
                        " stopped is no longer this process's; the collection"
                        " loop has it: kind=%s session=%s",
                        self._kind.value,
                        self.session,
                    )
                    return
            if self.session is not None:
                await self._keep_bound()
                return
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

    async def _keep_bound(self) -> None:
        """A kept slot bound to a session: renewed, and its login collected
        every ``collect_seconds`` once its unit stopped (Decision 0096, 1 D),
        until it was collected or is no longer this holder's."""

        async def renew() -> bool:
            held = await self._run(
                self._store.renew_session_slot(self._kind, self.holder, self._ttl)
            )
            if not held:
                logger.error(
                    "connection session slot of a CLI that could not be stopped"
                    " is no longer this process's; the collection loop has it:"
                    " kind=%s session=%s",
                    self._kind.value,
                    self.session,
                )
            return held

        collect = None
        if self._collector is not None:
            collector = self._collector

            async def collect() -> bool:
                return await collector.collect(self._kind, self.holder)

        await keep_until_collected(
            renew,
            collect,
            heartbeat_seconds=self._heartbeat,
            interval_seconds=self._collect_seconds,
            first_at_once=False,
        )

    def keep(self) -> None:
        """The session's CLI could not be stopped (it may still run with the
        login): :meth:`release` will keep the slot held."""
        self._keep = True

    async def _stop_heartbeat(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def release(self) -> None:
        """Stop the heartbeat and delete the holder's row (best effort: a row
        left behind expires by itself). After :meth:`keep`, neither: the slot
        stays held while this process lives; a slot bound to a session is
        marked ``kept`` on its row (Decision 0096, 2 B: it outlives this
        process until its login was collected). After a failed
        :meth:`confirm`, neither: the row is left as it is to the collection
        loop. Never raises."""
        if self.fenced:
            await self._stop_heartbeat()
            logger.error(
                "connection session slot left to the collection loop: kind=%s"
                " session=%s",
                self._kind.value,
                self.session or "-",
            )
            return
        if self._keep:
            if self.session is not None:
                try:
                    self.kept_session = await self._run(
                        self._store.keep_session_slot(self._kind, self.holder)
                    )
                    self._marked = True
                except Exception as error:
                    # The holding loop marks it before renewing it (Codex
                    # review of #291); the slot outlives the session all the
                    # same (audited).
                    self.kept_session = self.session
                    logger.warning(
                        "connection session slot not marked kept (tried again"
                        " while it is held): kind=%s exception_type=%s",
                        self._kind.value,
                        log_type_name(error),
                    )
                logger.error(
                    "connection session slot kept: its CLI could not be stopped;"
                    " stop its unit (systemctl stop paw-cli-session@%s.service)"
                    " and do not delete /run/paw-cli/%s: its login is collected"
                    " once the unit stopped: kind=%s",
                    self.session,
                    self.session,
                    self._kind.value,
                )
            else:
                logger.error(
                    "connection session slot kept: its CLI could not be stopped;"
                    " stop the session's unit (the slot is held while this"
                    " process runs): kind=%s",
                    self._kind.value,
                )
            # The heartbeat gives up once a renewal fails (and may already
            # have, when the slot was being lost as the session was
            # cancelled): hold it from here on, renewing or taking it again
            # after any later failure.
            await self._stop_heartbeat()
            self._task = asyncio.ensure_future(self._hold())
            return
        await self._stop_heartbeat()
        try:
            self.given_back = await self._run(
                self._store.release_session_slot(self._kind, self.holder)
            )
            if not self.given_back and self.session is not None:
                logger.error(
                    "connection session slot was taken over before it was given"
                    " back; its files are left to the collection: kind=%s"
                    " session=%s",
                    self._kind.value,
                    self.session,
                )
        except Exception as error:
            logger.warning(
                "connection session slot not released (it expires):"
                " kind=%s exception_type=%s",
                self._kind.value,
                log_type_name(error),
            )


class SessionCollector(Protocol):
    """Collects the login of a session whose slot ``holder`` holds (issue
    #280, Decision 0096): ``True`` once the slot was given back (or is not
    ``holder``'s any more), ``False`` to be tried again later. Never raises
    (``agents.codex_collect.CodexLoginCollector``)."""

    async def collect(self, kind: ConnectionKind, holder: uuid.UUID) -> bool: ...


async def keep_until_collected(
    renew: Callable[[], Awaitable[bool]],
    collect: Callable[[], Awaitable[bool]] | None,
    *,
    heartbeat_seconds: float,
    interval_seconds: float,
    first_at_once: bool,
) -> None:
    """Hold a slot bound to a session (Decision 0096, 1 D and 2 B): renew it
    every ``heartbeat_seconds`` in a task of its own (a slow round of the
    collection does not let it expire) and, with ``collect``, try a round
    every ``interval_seconds`` (the first one at once with
    ``first_at_once``). Ends once a renewal answers ``False`` (the slot was
    taken over, given back or expired: never taken again here) or a round
    ended the collection. A renewal or a round that fails is logged and
    tried again."""
    lost = asyncio.Event()

    async def beat() -> None:
        while True:
            try:
                held = await renew()
            except Exception as error:
                logger.warning(
                    "kept session slot not renewed: exception_type=%s",
                    log_type_name(error),
                )
                held = True  # not known to be lost: tried again
            if not held:
                lost.set()
                return
            await asyncio.sleep(heartbeat_seconds)

    beating = asyncio.ensure_future(beat())
    try:
        wait = 0.0 if first_at_once else interval_seconds
        while True:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait):
                    await lost.wait()
            if lost.is_set():
                return
            if collect is not None:
                try:
                    done = await collect()
                except Exception as error:
                    logger.warning(
                        "session collection failed for now: exception_type=%s",
                        log_type_name(error),
                    )
                    done = False
                if done is True:
                    return
            wait = interval_seconds
    finally:
        beating.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beating


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
    "SLOT_COLLECT_SECONDS",
    "SLOT_HEARTBEAT_SECONDS",
    "SLOT_STOP_MARGIN_SECONDS",
    "SLOT_TTL_SECONDS",
    "SLOT_WAIT_SECONDS",
    "SessionCollector",
    "SessionSlot",
    "check_slot_timing",
    "keep_until_collected",
    "run_within",
]
