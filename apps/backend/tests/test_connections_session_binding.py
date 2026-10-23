"""A Codex session recorded on its connection's session slot (issue #280,
Decision 0096, 2 B, 4 and 5): real PostgreSQL, a real ``FileSecretStore``,
fake session adapters.

* the store: a lease bound to a session (its UUID, the credential's handle
  and version) is never taken over by an ordinary acquisition, even once
  expired; the collection loop takes it over (compare-and-swap on the
  holder, once it expired: of two, one wins); its expected version only
  moves up; ``kept`` needs a bound lease; giving it back after a collection
  that could not read the login marks the connection (while it still points
  at that handle), and a ``connected`` health check or a replacement clears
  the mark;
* the service: a session binds its slot before its login is placed (the
  handle and the version it was given); a CLI that could not be stopped
  after the immediate write-back leaves its row ``kept`` with the written
  version, audited ``connection.session_kept`` and seen by System Health;
  the next session is ``connection_busy``; a ``kept`` mark the database
  failed to take is tried again by the holding loop before any renewal
  (Codex review of #291), audited all the same;
* fencing: a session whose slot was taken over cannot confirm it any more,
  and its row is left as it is (not deleted) for the collection loop.
"""

import asyncio
import dataclasses
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import text

from paw_backend.connections import (
    ConnectionKind,
    ConnectionStatus,
    CredentialSuspicion,
    SessionCollection,
    SessionRequest,
    SessionSlotBusyError,
    UsagePurpose,
    WriteBack,
)
from paw_backend.connections.session_slot import SessionSlot
from paw_backend.connections.store import ConnectionStore
from paw_backend.db import Database
from paw_backend.health.domain import Severity
from paw_backend.health.sources import ConnectionSource
from paw_backend.tasks.queueing.domain import QueueLease

from .connections_support import requires_postgres
from .support import make_settings
from .task_support import TEST_DATABASE_URL
from .test_connections_session_slot import (
    LOGIN,
    REFRESHED,
    ExclusiveSession,
    SlotCase,
)

CODEX = ConnectionKind.CODEX
LATER = '{"tokens": {"refresh_token": "third"}}'


class BindingSession(ExclusiveSession):
    """A Codex-like adapter: binds its slot, refreshes the login, and (when
    ``stuck``) cannot stop its CLI."""

    def __init__(self, *, stuck=False, take_over=None, **options):
        super().__init__(**options)
        self.stuck = stuck
        self.take_over = take_over
        self.session = uuid.uuid4()
        self.bound = None
        self.confirmed = []

    async def run_session(self, secret, request):
        self.bound = await request.bind_session(self.session)
        if self.take_over is not None:
            await self.take_over()
        self.confirmed.append(await request.still_held())
        if self.stuck:
            request.cli_running()
        if self.confirmed[-1] and self.refresh is not None:
            self.write_backs.append(await request.write_back(self.refresh))
        self.secrets.append(secret.reveal())
        return self.result


@requires_postgres
class StoreTest(SlotCase):
    """The SQL of a bound lease (``ConnectionStore``), with a test clock."""

    def store_at(self, now):
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        return ConnectionStore(
            database, clock=lambda: now[0], allow_explicit_clock=True
        )

    async def bound(self, store, holder):
        session = uuid.uuid4()
        self.assertTrue(await store.acquire_session_slot(CODEX, holder, 60))
        self.assertTrue(
            await store.bind_session_slot(CODEX, holder, session, self.handle, 1, 60)
        )
        return session

    async def test_a_bound_lease_is_never_taken_over_by_an_acquisition(self):
        now = [datetime(2026, 10, 22, 12, 0, tzinfo=UTC)]
        store = self.store_at(now)
        holder, other = uuid.uuid4(), uuid.uuid4()
        session = await self.bound(store, holder)
        (row,) = self.rows(
            "SELECT session, secret_handle, expected_version, kept"
            " FROM connection_session_leases"
        )
        self.assertEqual(
            (row["session"], row["secret_handle"], row["expected_version"]),
            (session, self.handle, 1),
        )
        self.assertFalse(row["kept"])
        now[0] += timedelta(hours=5)  # long expired
        self.assertFalse(await store.acquire_session_slot(CODEX, other, 60))
        (lease,) = await store.session_slots()
        self.assertEqual((lease.holder, lease.session), (holder, session))
        self.assertTrue(lease.expired)
        self.assertNotIn(self.handle, repr(lease))

    async def test_only_an_unbound_held_lease_is_bound(self):
        now = [datetime(2026, 10, 22, 12, 0, tzinfo=UTC)]
        store = self.store_at(now)
        holder, other = uuid.uuid4(), uuid.uuid4()
        await self.bound(store, holder)
        # Bound already; another holder's; expired.
        self.assertFalse(
            await store.bind_session_slot(
                CODEX, holder, uuid.uuid4(), self.handle, 1, 60
            )
        )
        self.assertFalse(
            await store.bind_session_slot(
                CODEX, other, uuid.uuid4(), self.handle, 1, 60
            )
        )
        self.clean_leases()
        self.assertTrue(await store.acquire_session_slot(CODEX, other, 60))
        now[0] += timedelta(seconds=60)
        self.assertFalse(
            await store.bind_session_slot(
                CODEX, other, uuid.uuid4(), self.handle, 1, 60
            )
        )
        self.assertEqual(await store.session_slots(), ())

    async def test_the_collection_takes_an_expired_bound_lease_over_once(self):
        now = [datetime(2026, 10, 22, 12, 0, tzinfo=UTC)]
        store = self.store_at(now)
        holder, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await self.bound(store, holder)
        # Not before it expired.
        self.assertFalse(await store.take_over_session_slot(CODEX, holder, first, 60))
        now[0] += timedelta(seconds=60)
        taken = await asyncio.gather(
            store.take_over_session_slot(CODEX, holder, first, 60),
            store.take_over_session_slot(CODEX, holder, second, 60),
        )
        self.assertEqual(sorted(taken), [False, True])
        (lease,) = await store.session_slots()
        self.assertIn(lease.holder, (first, second))
        self.assertTrue(lease.kept)
        self.assertFalse(lease.expired)
        # The previous holder can neither renew nor give it back.
        self.assertFalse(await store.renew_session_slot(CODEX, holder, 60))
        self.assertFalse(await store.finish_session_slot(CODEX, holder))
        await store.release_session_slot(CODEX, holder)
        self.assertEqual(len(self.leases()), 1)
        # An unbound expired lease is not the collection's.
        self.clean_leases()
        self.assertTrue(await store.acquire_session_slot(CODEX, holder, 60))
        now[0] += timedelta(seconds=60)
        self.assertFalse(await store.take_over_session_slot(CODEX, holder, first, 60))

    async def test_a_definite_result_is_kept_on_the_bound_lease(self):
        # Codex review of #292 (P2): kept before the login is deleted.
        now = [datetime(2026, 10, 22, 12, 0, tzinfo=UTC)]
        store = self.store_at(now)
        holder, other = uuid.uuid4(), uuid.uuid4()
        self.assertTrue(await store.acquire_session_slot(CODEX, holder, 60))
        written = SessionCollection.WRITTEN
        self.assertFalse(await store.record_session_result(CODEX, holder, written))
        self.clean_leases()
        await self.bound(store, holder)
        (lease,) = await store.session_slots()
        self.assertIsNone(lease.collected)
        self.assertFalse(await store.record_session_result(CODEX, other, written))
        self.assertTrue(await store.record_session_result(CODEX, holder, written))
        (lease,) = await store.session_slots()
        self.assertIs(lease.collected, written)

    async def test_the_version_only_moves_up_and_kept_needs_a_bound_lease(self):
        now = [datetime(2026, 10, 22, 12, 0, tzinfo=UTC)]
        store = self.store_at(now)
        holder, other = uuid.uuid4(), uuid.uuid4()
        self.assertTrue(await store.acquire_session_slot(CODEX, holder, 60))
        self.assertIsNone(await store.keep_session_slot(CODEX, holder))
        self.assertFalse(await store.record_session_slot_version(CODEX, holder, 2))
        self.clean_leases()
        session = await self.bound(store, holder)
        self.assertTrue(await store.record_session_slot_version(CODEX, holder, 3))
        self.assertTrue(await store.record_session_slot_version(CODEX, holder, 2))
        self.assertFalse(await store.record_session_slot_version(CODEX, other, 9))
        self.assertIsNone(await store.keep_session_slot(CODEX, other))
        self.assertEqual(await store.keep_session_slot(CODEX, holder), session)
        (lease,) = await store.session_slots()
        self.assertEqual((lease.expected_version, lease.kept), (3, True))

    def mark(self):
        (row,) = self.rows(
            "SELECT suspect_at, suspect_reason FROM shared_connections"
            " WHERE kind = 'codex'"
        )
        return row["suspect_reason"], row["suspect_at"]

    async def test_a_collection_that_found_no_login_marks_the_connection(self):
        now = [datetime(2026, 10, 22, 12, 0, tzinfo=UTC)]
        store = self.store_at(now)
        holder = uuid.uuid4()
        await self.bound(store, holder)
        self.assertTrue(
            await store.finish_session_slot(CODEX, holder, CredentialSuspicion.MISSING)
        )
        self.assertEqual(self.leases(), [])
        self.assertEqual(self.mark(), ("missing", now[0]))
        # A check that finds the login working clears it; another verdict not.
        await store.record_health(CODEX, self.handle, ConnectionStatus.EXPIRED)
        self.assertEqual(self.mark()[0], "missing")
        await store.record_health(CODEX, self.handle, ConnectionStatus.CONNECTED)
        self.assertEqual(self.mark(), (None, None))

    async def test_a_replaced_credential_is_not_marked_and_a_replacement_clears(self):
        now = [datetime(2026, 10, 22, 12, 0, tzinfo=UTC)]
        store = self.store_at(now)
        holder = uuid.uuid4()
        await self.bound(store, holder)
        replacement = await self.store.put(CODEX, LOGIN)
        await store.replace_handle(CODEX, replacement)
        self.assertTrue(
            await store.finish_session_slot(CODEX, holder, CredentialSuspicion.INVALID)
        )
        self.assertEqual(self.mark(), (None, None))
        # A mark on the current credential goes with its replacement.
        await self.bound(store, holder)
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE connection_session_leases SET secret_handle = :h"),
                {"h": replacement},
            )
        await store.finish_session_slot(CODEX, holder, CredentialSuspicion.INVALID)
        self.assertEqual(self.mark()[0], "invalid")
        await store.replace_handle(CODEX, self.handle)
        self.assertEqual(self.mark(), (None, None))

    async def test_a_collection_without_suspicion_marks_nothing(self):
        now = [datetime(2026, 10, 22, 12, 0, tzinfo=UTC)]
        store = self.store_at(now)
        holder = uuid.uuid4()
        await self.bound(store, holder)
        self.assertTrue(await store.finish_session_slot(CODEX, holder))
        self.assertEqual(self.leases(), [])
        self.assertEqual(self.mark(), (None, None))


class SessionTest(SlotCase):
    """The service binds the slot and keeps it (Decision 0096, 2 B and 4)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.addAsyncCleanup(self.let_holds_go)

    async def let_holds_go(self):
        # A kept slot's holder stops once its row is gone: before this test's
        # loop closes, so no renewal is left half-way for the next test.
        await asyncio.to_thread(self.clean_leases)
        await asyncio.sleep(0.3)

    def slot_service(self, adapter, **options):
        # A longer lease than the slot tests' (a slow database must not let a
        # confirmation run past it); still a short heartbeat.
        return super().slot_service(
            adapter,
            **{
                "session_slot_ttl_seconds": 6.0,
                "session_slot_margin_seconds": 3.0,
                **options,
            },
        )

    def set_holder(self, holder):
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE connection_session_leases SET holder = :h"),
                {"h": holder},
            )

    async def test_a_session_binds_its_slot_with_the_version_it_was_given(self):
        await self.store.rotate(self.handle, CODEX, 1, REFRESHED)  # version 2
        seen = []

        class Watching(BindingSession):
            async def run_session(inner, secret, request):
                inner.bound = await request.bind_session(inner.session)
                seen.extend(
                    self.rows(
                        "SELECT session, secret_handle, expected_version, kept"
                        " FROM connection_session_leases"
                    )
                )
                return inner.result

        adapter = Watching()
        result = await self.start(self.slot_service(adapter))
        self.assertIsNone(result.failure)
        self.assertTrue(adapter.bound)
        (row,) = seen
        self.assertEqual(
            (row["session"], row["secret_handle"], row["expected_version"]),
            (adapter.session, self.handle, 2),
        )
        self.assertEqual(self.leases(), [])  # an ordinary end gives it back

    async def test_a_cli_that_could_not_be_stopped_leaves_its_row_kept(self):
        adapter = BindingSession(stuck=True, refresh=REFRESHED)
        with self.assertLogs("paw_backend.connections", "ERROR") as logs:
            await self.start(self.slot_service(adapter))
        self.assertEqual(adapter.write_backs, [WriteBack.WRITTEN])
        (row,) = self.rows(
            "SELECT session, expected_version, kept FROM connection_session_leases"
        )
        # Kept, with the version the immediate write-back stored.
        self.assertEqual(
            (row["session"], row["expected_version"], row["kept"]),
            (adapter.session, 2, True),
        )
        line = "\n".join(logs.output)
        self.assertIn(f"systemctl stop paw-cli-session@{adapter.session}.service", line)
        self.assertNotIn("second", line)
        (kept,) = [e for e in self.sink.events if e.action == "connection.session_kept"]
        self.assertEqual((kept.actor_role, kept.reason), ("system", "unit_running"))
        self.assertIsNotNone(kept.resource_id)
        self.assertNotIn(str(adapter.session), repr(self.sink.events))
        # The next session waits for the collection.
        second = ExclusiveSession()
        with self.assertRaises(SessionSlotBusyError):
            await self.start(self.slot_service(second))
        self.assertEqual(second.secrets, [])
        # System Health: a warning, with the count only.
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        health = await ConnectionSource(database).check()
        self.assertIs(health.severity, Severity.WARNING)
        self.assertIn("unit_running:codex", health.reasons)
        self.assertEqual(health.metrics["codex_kept_sessions"], 1)
        self.assertNotIn(str(adapter.session), repr(health))

    async def test_a_session_whose_process_ended_blocks_the_slot_once_expired(self):
        # Decision 0096, 2 B: a bound row whose holder is gone (no ``kept``)
        # is not taken by the next session even after it expired.
        # The row as a process that crashed after binding leaves it.
        session = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO connection_session_leases (kind, holder,"
                    " acquired_at, heartbeat_at, expires_at, session,"
                    " secret_handle, expected_version) VALUES ('codex', :holder,"
                    " now() - interval '1 hour', now() - interval '1 hour',"
                    " now() - interval '1 minute', :session, :handle, 1)"
                ),
                {
                    "holder": uuid.uuid4(),
                    "session": session,
                    "handle": self.handle,
                },
            )
        second = ExclusiveSession()
        with self.assertRaises(SessionSlotBusyError):
            await self.start(self.slot_service(second))
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        health = await ConnectionSource(database).check()
        self.assertIn("unit_running:codex", health.reasons)

    async def test_a_session_whose_slot_was_taken_over_leaves_the_row(self):
        other = uuid.uuid4()

        async def take_over():
            await asyncio.to_thread(self.set_holder, other)

        adapter = BindingSession(take_over=take_over, refresh=REFRESHED)
        await self.start(self.slot_service(adapter))
        self.assertEqual(adapter.confirmed, [False])
        self.assertEqual(adapter.write_backs, [])  # not written back
        self.assertEqual((await self.store.read(self.handle)).version, 1)
        # The row is the new holder's, as it was: not deleted.
        (row,) = self.rows("SELECT holder, session FROM connection_session_leases")
        self.assertEqual((row["holder"], row["session"]), (other, adapter.session))

    def start_seeing_release(self, service, seen):
        context = self.context(self.task, self.user, self.project_of(self.task))
        context = dataclasses.replace(context, lease=QueueLease(self.entry, "w1", 1))
        return service.execute_session(
            self.principal(self.user),
            context,
            CODEX,
            SessionRequest(
                "gpt-5-codex",
                UsagePurpose.CODING,
                "the task",
                600,
                on_slot_released=seen.append,
            ),
        )

    async def test_the_release_says_whether_this_session_gave_its_row_back(self):
        # Codex review of #291: a session whose row was taken over after its
        # last check (a pause past the lease) is told so, and the row stays.
        seen = []
        await self.start_seeing_release(self.slot_service(BindingSession()), seen)
        self.assertEqual((seen, self.leases()), ([True], []))
        other = uuid.uuid4()

        class TakenAtTheEnd(BindingSession):
            async def run_session(inner, secret, request):
                result = await super().run_session(secret, request)
                await asyncio.to_thread(self.set_holder, other)
                return result

        seen = []
        with self.assertLogs("paw_backend.connections", "ERROR"):
            await self.start_seeing_release(self.slot_service(TakenAtTheEnd()), seen)
        self.assertEqual(seen, [False])
        (row,) = self.rows("SELECT holder FROM connection_session_leases")
        self.assertEqual(row["holder"], other)
        # A kept slot is not given back either.
        await asyncio.to_thread(self.clean_leases)
        seen = []
        await self.start_seeing_release(
            self.slot_service(BindingSession(stuck=True)), seen
        )
        self.assertEqual(seen, [False])

    async def test_the_release_is_told_also_through_a_cancellation(self):
        # Codex review of #291: a cancellation that comes while the slot is
        # being released still tells the runtime what the release came to.
        releasing = asyncio.Event()
        real = SessionSlot.release

        async def slow_release(slot):
            releasing.set()
            await asyncio.sleep(0.3)
            await real(slot)

        for taken_over in (False, True):
            with self.subTest(taken_over=taken_over):
                await asyncio.to_thread(self.clean_leases)
                releasing.clear()
                other = uuid.uuid4()

                class Session(BindingSession):
                    take, new_holder = taken_over, other

                    async def run_session(inner, secret, request):
                        result = await super().run_session(secret, request)
                        if inner.take:
                            await asyncio.to_thread(self.set_holder, inner.new_holder)
                        return result

                seen = []
                with patch.object(SessionSlot, "release", slow_release):
                    running = asyncio.ensure_future(
                        self.start_seeing_release(self.slot_service(Session()), seen)
                    )
                    await asyncio.wait_for(releasing.wait(), 10)
                    running.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await running
                self.assertEqual(seen, [not taken_over])

    async def test_a_kept_slot_taken_over_is_let_go(self):
        # The holding process renews a kept bound slot only; once the
        # collection took it over it stops (no fresh, unbound row).
        adapter = BindingSession(stuck=True)
        await self.start(self.slot_service(adapter))
        other = uuid.uuid4()
        # (In a thread: the holder's renewal must not wait on a blocked loop.)
        await asyncio.to_thread(self.set_holder, other)
        await asyncio.sleep(0.5)
        (row,) = self.rows("SELECT holder FROM connection_session_leases")
        self.assertEqual(row["holder"], other)
        await asyncio.to_thread(self.clean_leases)  # (the collection gave it back)
        await asyncio.sleep(0.5)
        self.assertEqual(self.leases(), [])
        # A further session can run.
        after = ExclusiveSession()
        await self.start(self.slot_service(after))
        self.assertEqual(after.secrets, [LOGIN])

    async def test_a_kept_mark_the_database_failed_is_tried_again(self):
        # Codex review of #291: a transient failure of the ``kept`` mark is
        # tried again while the slot is held; the audit and the log come at
        # once, System Health sees the row once it is marked.
        real = ConnectionStore.keep_session_slot
        calls = []

        async def flaky(store, kind, holder):
            calls.append(holder)
            if len(calls) == 1:
                raise ConnectionError("database restarting")
            return await real(store, kind, holder)

        adapter = BindingSession(stuck=True)
        with patch.object(ConnectionStore, "keep_session_slot", flaky):
            with self.assertLogs("paw_backend.connections", "ERROR") as logs:
                await self.start(self.slot_service(adapter))
            self.assertIn(
                f"systemctl stop paw-cli-session@{adapter.session}.service",
                "\n".join(logs.output),
            )
            for _ in range(100):
                if len(calls) > 1:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)
        (row,) = self.rows("SELECT session, kept FROM connection_session_leases")
        self.assertEqual((row["session"], row["kept"]), (adapter.session, True))
        (kept,) = [e for e in self.sink.events if e.action == "connection.session_kept"]
        self.assertEqual(kept.reason, "unit_running")
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        health = await ConnectionSource(database).check()
        self.assertIn("unit_running:codex", health.reasons)
        self.assertEqual(health.metrics["codex_kept_sessions"], 1)


class FlakyKeepStore:
    """A store whose ``kept`` mark fails ``failures`` times (no database)."""

    def __init__(self, failures):
        self.failures = failures
        self.calls = []

    async def keep_session_slot(self, kind, holder):
        self.calls.append("keep")
        if self.failures:
            self.failures -= 1
            raise ConnectionError("database restarting")
        return self.session

    async def renew_session_slot(self, kind, holder, ttl):
        self.calls.append("renew")
        return True


class KeptMarkRetryTest(unittest.IsolatedAsyncioTestCase):
    """Codex review of #291: the holding loop of a bound, kept slot whose
    ``kept`` mark failed marks it before renewing it; it never renews an
    unmarked row (left alone, it expires and the collection sees it)."""

    async def kept_slot(self, store):
        slot = SessionSlot(
            store,
            CODEX,
            ttl_seconds=1.2,
            heartbeat_seconds=0.02,
            margin_seconds=0.6,
        )
        slot.session = store.session = uuid.uuid4()
        slot.keep()
        with self.assertLogs("paw_backend.connections", "WARNING"):
            await slot.release()
        self.addAsyncCleanup(slot._stop_heartbeat)
        return slot

    async def test_the_mark_is_tried_again_before_any_renewal(self):
        store = FlakyKeepStore(failures=3)
        slot = await self.kept_slot(store)
        # Audited and logged at once: the slot outlives its session.
        self.assertEqual(slot.kept_session, slot.session)
        for _ in range(100):
            if "renew" in store.calls:
                break
            await asyncio.sleep(0.02)
        first = store.calls.index("renew")
        self.assertEqual(store.calls[:first], ["keep"] * 4)
        self.assertNotIn("keep", store.calls[first:])

    async def test_a_mark_that_finds_the_row_gone_lets_it_go(self):
        store = FlakyKeepStore(failures=1)

        async def gone(kind, holder):
            store.calls.append("keep")
            return None  # taken over meanwhile

        slot = await self.kept_slot(store)
        store.keep_session_slot = gone
        for _ in range(100):
            if slot._task.done():
                break
            await asyncio.sleep(0.02)
        self.assertTrue(slot._task.done())
        self.assertNotIn("renew", store.calls)


if __name__ == "__main__":
    unittest.main()
