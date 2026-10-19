"""The one session slot of a connection and the write-back of a refreshed
credential (Decision 0083, 6 and its approval's condition (2); issue #239):
real PostgreSQL, a real ``FileSecretStore``, fake session adapters.

* the store: a lease is taken when there is none or it expired (a takeover),
  never while somebody else holds it; a heartbeat moves the expiry while it
  holds; a release deletes the holder's own row only;
* an ``exclusive_sessions`` adapter: a second session waits for the slot
  BEFORE the admission and, when it stays taken, fails ``SessionSlotBusyError``
  (``connection_busy``) with no ``connection.use`` audit and no usage row; it
  starts once the slot is free within the wait; the slot is given back after
  every end (success, failure, refusal, cancellation);
* a holder that cannot renew stops its session before the lease could
  expire: the adapter is cancelled (and runs its own cleanup), the session
  fails ``unavailable``;
* ``write_back``: the refreshed credential becomes the next version of the
  stored one (``connection.rotate``), but only while the version the session
  was given is still the current one (compare-and-swap);
* an adapter that is not exclusive takes no slot and gets no write-back.
"""

import asyncio
import dataclasses
import os
import shutil
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from paw_backend.connections import (
    AgentSessionResult,
    ConnectionBusyError,
    ConnectionKind,
    ConnectionUnavailableError,
    FailureCode,
    FileSecretStore,
    SessionRequest,
    SessionSlotBusyError,
    UsagePurpose,
    WriteBack,
)
from paw_backend.connections.store import ConnectionStore
from paw_backend.db import Database
from paw_backend.tasks.queueing import BudgetPreset, BudgetTracker
from paw_backend.tasks.queueing.domain import QueueLease

from .connections_support import PostgresConnectionTestCase, requires_postgres
from .support import make_settings
from .task_support import TEST_DATABASE_URL

CODEX = ConnectionKind.CODEX
KEY = bytes(range(32))
LOGIN = '{"tokens": {"refresh_token": "first"}}'
REFRESHED = '{"tokens": {"refresh_token": "second"}}'
# Short slot timing for the tests (production: 60 s wait, 60 s lease,
# 10 s heartbeat, 20 s margin).
FAST = {
    "session_slot_wait_seconds": 1.0,
    "session_slot_ttl_seconds": 1.2,
    "session_slot_heartbeat_seconds": 0.1,
    "session_slot_margin_seconds": 0.6,
}


class ExclusiveSession:
    """A session adapter that holds the connection's slot (Codex's form)."""

    kind = CODEX
    exclusive_sessions = True

    def __init__(self, *, refresh=None, hold=None, result=None):
        self.refresh = refresh
        self.hold = hold  # an Event the session waits for
        self.result = result or AgentSessionResult(submission={"summary": "done"})
        self.started = asyncio.Event()
        self.secrets = []
        self.write_backs = []
        self.cancelled = False
        self.cleaned_up = False

    async def run_session(self, secret, request):
        self.secrets.append(secret.reveal())
        self.write_back = request.write_back
        self.started.set()
        try:
            if self.hold is not None:
                await self.hold.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            # The adapter's own end: the CLI stopped, the login written back.
            if self.refresh is not None and request.write_back is not None:
                self.write_backs.append(await request.write_back(self.refresh))
            self.cleaned_up = True
        return self.result


class SharedSession(ExclusiveSession):
    exclusive_sessions = False


@requires_postgres
class SlotCase(PostgresConnectionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.clean_leases()
        self.addCleanup(self.clean_leases)
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.chmod(self.root, 0o700)
        self.store = FileSecretStore(self.root, KEY)
        self.resolver = self.store  # production's resolver is the store too
        self.handle = await self.store.put(CODEX, LOGIN)
        self.seed_connection(CODEX, secret_handle=self.handle)
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        self.budget = BudgetTracker(database)
        self.task = self.seed_task(self.user)
        await self.budget.set_preset(self.task, BudgetPreset.STANDARD)
        self.entry = self.seed_entry(self.task)

    def clean_leases(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM connection_session_leases"))

    def seed_entry(self, task_id):
        with self.engine.begin() as connection:
            return connection.execute(
                text(
                    "INSERT INTO queue_entries (task_id, priority, priority_rank,"
                    " status, enqueued_at, claimed_by, claimed_at, claim_count,"
                    " lease_expires_at) VALUES (:task, 'normal', 1, 'claimed',"
                    " now(), 'w1', now(), 1, now() + interval '10 minutes')"
                    " RETURNING id"
                ),
                {"task": task_id},
            ).scalar_one()

    def slot_service(self, adapter, **options):
        return self.new_service(
            budget=self.budget,
            session_adapters={CODEX: adapter},
            credential_store=self.store,
            **{**FAST, **options},
        )

    def start(self, service):
        context = self.context(self.task, self.user, self.project_of(self.task))
        context = dataclasses.replace(context, lease=QueueLease(self.entry, "w1", 1))
        return service.execute_session(
            self.principal(self.user),
            context,
            CODEX,
            SessionRequest("gpt-5-codex", UsagePurpose.CODING, "the task", 600),
        )

    def leases(self):
        return self.rows("SELECT kind, holder FROM connection_session_leases")

    def usage_count(self):
        return self.scalar("SELECT count(*) FROM connection_usage")

    def uses(self):
        return [e for e in self.sink.events if e.action == "connection.use"]


class StoreTest(SlotCase):
    """The SQL of the lease (``ConnectionStore``), with a test clock."""

    def store_at(self, now):
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        return ConnectionStore(
            database, clock=lambda: now[0], allow_explicit_clock=True
        )

    async def test_one_holder_at_a_time_and_a_takeover_after_expiry(self):
        now = [datetime(2026, 10, 20, 12, 0, tzinfo=UTC)]
        store = self.store_at(now)
        first, second = uuid.uuid4(), uuid.uuid4()
        self.assertTrue(await store.acquire_session_slot(CODEX, first, 60))
        self.assertFalse(await store.acquire_session_slot(CODEX, second, 60))
        # Another kind is another connection.
        self.assertTrue(
            await store.acquire_session_slot(ConnectionKind.CLAUDE, second, 60)
        )
        now[0] += timedelta(seconds=59)
        self.assertFalse(await store.acquire_session_slot(CODEX, second, 60))
        now[0] += timedelta(seconds=1)  # expired: taken over
        self.assertTrue(await store.acquire_session_slot(CODEX, second, 60))
        (row,) = self.rows(
            "SELECT holder, acquired_at FROM connection_session_leases"
            " WHERE kind = 'codex'"
        )
        self.assertEqual((row["holder"], row["acquired_at"]), (second, now[0]))
        # The old holder can neither renew nor release it any more.
        self.assertFalse(await store.renew_session_slot(CODEX, first, 60))
        await store.release_session_slot(CODEX, first)
        self.assertEqual(len(self.leases()), 2)

    async def test_a_heartbeat_moves_the_expiry_while_it_holds(self):
        now = [datetime(2026, 10, 20, 12, 0, tzinfo=UTC)]
        store = self.store_at(now)
        holder, other = uuid.uuid4(), uuid.uuid4()
        self.assertTrue(await store.acquire_session_slot(CODEX, holder, 60))
        now[0] += timedelta(seconds=50)
        self.assertTrue(await store.renew_session_slot(CODEX, holder, 60))
        now[0] += timedelta(seconds=50)  # 100 s after the start: still held
        self.assertFalse(await store.acquire_session_slot(CODEX, other, 60))
        now[0] += timedelta(seconds=10)  # the renewed lease has expired
        self.assertFalse(await store.renew_session_slot(CODEX, holder, 60))
        self.assertTrue(await store.acquire_session_slot(CODEX, other, 60))

    async def test_a_release_frees_the_slot(self):
        now = [datetime(2026, 10, 20, 12, 0, tzinfo=UTC)]
        store = self.store_at(now)
        holder, other = uuid.uuid4(), uuid.uuid4()
        self.assertTrue(await store.acquire_session_slot(CODEX, holder, 60))
        await store.release_session_slot(CODEX, holder)
        self.assertEqual(self.leases(), [])
        self.assertTrue(await store.acquire_session_slot(CODEX, other, 60))


class WaitTest(SlotCase):
    async def test_a_second_session_fails_busy_without_audit_or_usage(self):
        hold = asyncio.Event()
        adapter = ExclusiveSession(hold=hold)
        first = asyncio.ensure_future(self.start(self.slot_service(adapter)))
        await asyncio.wait_for(adapter.started.wait(), 10)
        uses, usage = len(self.uses()), self.usage_count()
        second = ExclusiveSession()
        loop = asyncio.get_running_loop()
        began = loop.time()
        with self.assertRaises(SessionSlotBusyError) as caught:
            await self.start(self.slot_service(second))
        self.assertGreaterEqual(loop.time() - began, FAST["session_slot_wait_seconds"])
        self.assertIsInstance(caught.exception, ConnectionBusyError)
        self.assertEqual(caught.exception.code, "connection_busy")
        self.assertEqual(second.secrets, [])
        self.assertEqual(len(self.uses()), uses)  # no connection.use audit
        self.assertEqual(self.usage_count(), usage)  # no usage row
        hold.set()
        result = await first
        self.assertIsNone(result.failure)
        self.assertEqual(self.leases(), [])

    async def test_a_waiting_session_starts_once_the_slot_is_free(self):
        hold = asyncio.Event()
        adapter = ExclusiveSession(hold=hold)
        first = asyncio.ensure_future(self.start(self.slot_service(adapter)))
        await asyncio.wait_for(adapter.started.wait(), 10)
        second = ExclusiveSession()
        waiting = asyncio.ensure_future(
            self.start(self.slot_service(second, session_slot_wait_seconds=10.0))
        )
        await asyncio.sleep(0.5)
        self.assertFalse(second.started.is_set())
        self.assertEqual(self.usage_count(), 1)  # admitted only after the wait
        hold.set()
        await first
        result = await asyncio.wait_for(waiting, 10)
        self.assertIsNone(result.failure)
        self.assertEqual(second.secrets, [LOGIN])
        self.assertEqual(self.usage_count(), 2)
        self.assertEqual(self.leases(), [])

    async def test_the_slot_is_given_back_after_a_refusal(self):
        # The admission refuses (the connection is disabled): the slot taken
        # before it is released all the same.
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE shared_connections SET enabled = false"))
        with self.assertRaises(ConnectionUnavailableError):
            await self.start(self.slot_service(ExclusiveSession()))
        self.assertEqual(self.leases(), [])

    async def test_the_slot_is_given_back_after_a_cancellation(self):
        adapter = ExclusiveSession(hold=asyncio.Event())
        running = asyncio.ensure_future(self.start(self.slot_service(adapter)))
        await asyncio.wait_for(adapter.started.wait(), 10)
        self.assertEqual(len(self.leases()), 1)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertTrue(adapter.cancelled)
        self.assertEqual(self.leases(), [])

    async def test_a_shared_adapter_takes_no_slot_and_gets_no_write_back(self):
        hold = asyncio.Event()
        first = SharedSession(hold=hold)
        running = asyncio.ensure_future(self.start(self.slot_service(first)))
        await asyncio.wait_for(first.started.wait(), 10)
        self.assertEqual(self.leases(), [])
        second = SharedSession()
        result = await self.start(self.slot_service(second))
        self.assertIsNone(result.failure)
        self.assertIsNone(second.write_back)
        hold.set()
        await running

    def test_the_slot_timing_is_checked(self):
        for options in (
            {"session_slot_wait_seconds": 0},
            {"session_slot_heartbeat_seconds": 0.7},  # not before ttl - margin
            {"session_slot_margin_seconds": -1},
        ):
            with self.subTest(options), self.assertRaises(ValueError):
                self.slot_service(ExclusiveSession(), **options)


class LostSlotTest(SlotCase):
    async def test_a_holder_that_cannot_renew_stops_its_session_in_time(self):
        adapter = ExclusiveSession(hold=asyncio.Event(), refresh=REFRESHED)
        running = asyncio.ensure_future(self.start(self.slot_service(adapter)))
        await asyncio.wait_for(adapter.started.wait(), 10)
        # Somebody took the slot over (as after an expiry): the renewal fails.
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE connection_session_leases SET holder = :other"),
                {"other": uuid.uuid4()},
            )
        result = await asyncio.wait_for(running, 10)
        self.assertTrue(adapter.cancelled)
        self.assertTrue(adapter.cleaned_up)  # its stop and write-back ran
        self.assertEqual(result.failure, FailureCode.UNAVAILABLE)
        (row,) = self.rows("SELECT status, failure_code FROM connection_usage")
        self.assertEqual(
            (row["status"], row["failure_code"]), ("failed", "unavailable")
        )
        # The other holder's row stays.
        self.assertEqual(len(self.leases()), 1)


class WriteBackTest(SlotCase):
    async def test_a_refreshed_login_becomes_the_next_version(self):
        adapter = ExclusiveSession(refresh=REFRESHED)
        result = await self.start(self.slot_service(adapter))
        self.assertIsNone(result.failure)
        self.assertEqual(adapter.secrets, [LOGIN])
        self.assertEqual(adapter.write_backs, [WriteBack.WRITTEN])
        stored = await self.store.read(self.handle)
        self.assertEqual((stored.version, stored.secret.reveal()), (2, REFRESHED))
        rotations = [e for e in self.sink.events if e.action == "connection.rotate"]
        self.assertEqual(len(rotations), 1)
        self.assertNotIn("second", repr(self.sink.events))
        # The next session is given the refreshed login.
        after = ExclusiveSession()
        await self.start(self.slot_service(after))
        self.assertEqual(after.secrets, [REFRESHED])

    async def test_a_login_changed_meanwhile_is_not_overwritten(self):
        hold = asyncio.Event()
        adapter = ExclusiveSession(hold=hold, refresh=REFRESHED)
        running = asyncio.ensure_future(self.start(self.slot_service(adapter)))
        await asyncio.wait_for(adapter.started.wait(), 10)
        other = '{"tokens": {"refresh_token": "admin"}}'
        await self.store.rotate(self.handle, CODEX, 1, other)
        hold.set()
        await running
        self.assertEqual(adapter.write_backs, [WriteBack.STALE])
        stored = await self.store.read(self.handle)
        self.assertEqual((stored.version, stored.secret.reveal()), (2, other))

    async def test_a_replaced_connection_is_not_written(self):
        hold = asyncio.Event()
        adapter = ExclusiveSession(hold=hold, refresh=REFRESHED)
        running = asyncio.ensure_future(self.start(self.slot_service(adapter)))
        await asyncio.wait_for(adapter.started.wait(), 10)
        replacement = await self.store.put(CODEX, LOGIN)
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE shared_connections SET secret_handle = :h"),
                {"h": replacement},
            )
        hold.set()
        await running
        self.assertEqual(adapter.write_backs, [WriteBack.STALE])
        self.assertEqual((await self.store.read(self.handle)).version, 1)

    async def test_the_write_back_comes_also_after_a_cancellation(self):
        adapter = ExclusiveSession(hold=asyncio.Event(), refresh=REFRESHED)
        running = asyncio.ensure_future(self.start(self.slot_service(adapter)))
        await asyncio.wait_for(adapter.started.wait(), 10)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(adapter.write_backs, [WriteBack.WRITTEN])
        self.assertEqual((await self.store.read(self.handle)).version, 2)
        self.assertEqual(self.leases(), [])


if __name__ == "__main__":
    unittest.main()
