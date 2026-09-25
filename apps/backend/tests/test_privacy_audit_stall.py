"""A database that does not answer must never let a query out (issue #87).

The audit write is a required one: when it does not finish inside the gate's
deadline the send is refused (``audit_failed``), on time, without waiting for a
driver-level cancellation (about ten seconds against a server that accepts the
connection and never answers), and without leaking a connection or a slot. Two
kinds of server: ``StalledPostgres`` (``HangingPostgres``, a wire-protocol stub that
authenticates and then says nothing) and, when ``PAW_TEST_DATABASE_URL`` is set, a
real PostgreSQL behind a proxy that goes silent in the middle of a session.
"""

import asyncio
import time
import unittest
import uuid

from sqlalchemy.engine import make_url

from paw_backend.db import Database
from paw_backend.research.privacy import (
    DEFAULT_AUDIT_TIMEOUT_SECONDS,
    PostgresExternalSendAudit,
    PrivacyInput,
    PrivacyRefusal,
    RefusalReason,
    build_research_broker,
)
from paw_backend.research.providers import ResearchRequest

from .fake_postgres import FreezableProxy, HangingPostgres
from .privacy_audit_support import (
    PostgresAuditTestCase,
    TrackingDatabase,
    make_record,
)
from .privacy_support import guarded
from .research_support import fixed_clock, hit, registry_of, web
from .support import make_settings, wait_until
from .task_support import TEST_DATABASE_URL

# A generous limit: only a send that waited for a driver-level cancellation (about
# ten seconds) or never stopped can miss it, never a slow machine.
LATE = 4.0
PROJECT = uuid.UUID("11111111-2222-3333-4444-555555555555")


def settings_for(port: int, **overrides):
    return make_settings(
        database_url=f"postgresql://paw:pw@127.0.0.1:{port}/paw", **overrides
    )


class StalledPostgres(HangingPostgres):
    """``HangingPostgres`` that also closes a connection nobody has handled yet.

    A client that connects and is then aborted (before it sends its startup message)
    can leave its socket open until it is garbage collected, and the stub's handler
    for it may not have started when the stub is closed: ``HangingPostgres`` then
    waits forever for a connection it never closed. ``close_clients`` closes every
    accepted connection whether or not its handler has run.
    """

    async def __aexit__(self, *exc_info) -> None:
        self._server.close()
        self._server.close_clients()
        await super().__aexit__(*exc_info)


class StubbornDatabase(Database):
    """A driver whose statement does not end when it is aborted or cancelled.

    The situation behind the slot accounting: the abort was requested and the query
    is still running (a driver cleanup that keeps waiting). ``release`` lets it end.
    """

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.release = asyncio.Event()
        self.started = 0
        self.running = 0

    async def _query(self, sql, params=None):
        self.started += 1
        self.running += 1
        # A safety net so that broken code fails a test instead of hanging it.
        asyncio.get_running_loop().call_later(6, self.release.set)
        try:
            while True:
                try:
                    await self.release.wait()
                    return []
                except asyncio.CancelledError:
                    pass  # swallowed, like a cleanup that keeps waiting
        finally:
            self.running -= 1


async def send(broker, query: str = "python asyncio", project=PROJECT):
    return await broker.gather(
        ResearchRequest(query), preflight_input=PrivacyInput([], project)
    )


class RefusedSendMixin:
    async def assert_refused(self, broker, provider, limit: float = LATE):
        started = time.monotonic()
        with self.assertLogs("paw_backend.research.privacy", level="WARNING") as logs:
            with self.assertRaises(PrivacyRefusal) as caught:
                await guarded(send(broker))
        elapsed = time.monotonic() - started
        self.assertIs(caught.exception.reason, RefusalReason.AUDIT_FAILED)
        self.assertLess(elapsed, limit)
        self.assertEqual(provider.search_calls, [])  # nothing was sent
        (line,) = logs.output
        self.assertNotIn("python", line)
        return elapsed


class StalledServerTest(RefusedSendMixin, unittest.IsolatedAsyncioTestCase):
    async def broker_for(self, server, **options):
        database = TrackingDatabase(
            settings_for(server.port, **options.pop("settings", {}))
        )
        self.addAsyncCleanup(database.dispose)
        provider = web(hits=[hit()])
        broker = build_research_broker(
            registry_of(provider), database, clock=fixed_clock(), **options
        )
        return broker, provider, database

    async def assert_wound_down(self, database):
        self.assertTrue(await wait_until(lambda: not database._probes, limit=5))
        self.assertEqual(database._probe_connections, {})
        self.assertFalse(database._abortable_slots.locked())

    async def test_a_server_that_never_answers_the_insert_refuses_on_time(self):
        async with StalledPostgres() as server:
            broker, provider, database = await self.broker_for(
                server, audit_timeout_seconds=0.5
            )
            elapsed = await self.assert_refused(broker, provider)
            self.assertGreaterEqual(elapsed, 0.4)  # it did wait for the deadline
            self.assertEqual(database.peak, 1)  # one write, one connection
            await self.assert_wound_down(database)

    async def test_a_server_that_never_answers_the_login_refuses_on_time(self):
        async with StalledPostgres(login=False) as server:
            broker, provider, database = await self.broker_for(
                server, audit_timeout_seconds=0.5
            )
            await self.assert_refused(broker, provider)
            await self.assert_wound_down(database)

    async def test_the_default_deadline_is_five_seconds(self):
        self.assertEqual(DEFAULT_AUDIT_TIMEOUT_SECONDS, 5.0)
        async with StalledPostgres() as server:
            broker, provider, database = await self.broker_for(server)
            elapsed = await self.assert_refused(broker, provider, limit=9.0)
            # It waited the whole 5 seconds ... and not the ten of a driver cancel.
            self.assertGreaterEqual(elapsed, 4.5)
            await self.assert_wound_down(database)

    async def test_the_sink_alone_gives_up_at_its_own_deadline(self):
        async with StalledPostgres() as server:
            database = Database(settings_for(server.port))
            self.addAsyncCleanup(database.dispose)
            sink = PostgresExternalSendAudit(database, timeout_seconds=0.3)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                await sink.record(make_record(PROJECT))
            self.assertLess(time.monotonic() - started, LATE)
            await self.assert_wound_down(database)

    async def test_waiting_for_a_slot_and_writing_share_the_one_deadline(self):
        async with StalledPostgres() as server:
            broker, provider, database = await self.broker_for(
                server, audit_timeout_seconds=0.6, settings={"database_pool_size": 1}
            )
            started = time.monotonic()
            with self.assertLogs("paw_backend.research.privacy", level="WARNING"):
                results = await guarded(
                    asyncio.gather(send(broker), send(broker), return_exceptions=True)
                )
            elapsed = time.monotonic() - started
            for result in results:
                self.assertIsInstance(result, PrivacyRefusal)
                self.assertIs(result.reason, RefusalReason.AUDIT_FAILED)
            # The second send waited for the only slot inside its own deadline: both
            # ended together (not one after the other), and the two never had a
            # connection at the same time.
            self.assertLess(elapsed, LATE)
            self.assertEqual(database.peak, 1)
            self.assertEqual(provider.search_calls, [])
            await self.assert_wound_down(database)

    async def test_a_burst_never_runs_more_writes_than_the_cap(self):
        async with StalledPostgres() as server:
            broker, provider, database = await self.broker_for(
                server, audit_timeout_seconds=0.5, settings={"database_pool_size": 2}
            )
            with self.assertLogs(
                "paw_backend.research.privacy", level="WARNING"
            ) as logs:
                results = await guarded(
                    asyncio.gather(
                        *(send(broker) for _ in range(10)), return_exceptions=True
                    )
                )
            self.assertEqual(len(logs.output), 10)  # one line per refused send
            self.assertTrue(all(isinstance(r, PrivacyRefusal) for r in results))
            # However many attempts a deadline hands a freed slot to, no more than 2
            # statements ever ran at once (the slot is kept until the query ends).
            self.assertLessEqual(database.peak, 2)
            self.assertGreaterEqual(database.peak, 1)
            self.assertEqual(provider.search_calls, [])
            await self.assert_wound_down(database)

    async def test_cancelling_a_stalled_send_aborts_the_connection(self):
        async with StalledPostgres() as server:
            broker, provider, database = await self.broker_for(server)
            task = asyncio.ensure_future(send(broker))
            self.assertTrue(await wait_until(lambda: server.logins == 1, limit=10))
            await asyncio.sleep(0.2)  # inside the write now
            started = time.monotonic()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):  # not a refusal
                await asyncio.wait_for(task, LATE)
            self.assertLess(time.monotonic() - started, LATE)
            self.assertEqual(provider.search_calls, [])
            await self.assert_wound_down(database)

    async def test_disposing_the_database_ends_a_stalled_send_with_a_refusal(self):
        async with StalledPostgres() as server:
            broker, provider, database = await self.broker_for(server)
            task = asyncio.ensure_future(send(broker))
            self.assertTrue(await wait_until(lambda: server.logins == 1, limit=10))
            await asyncio.sleep(0.2)
            with self.assertLogs("paw_backend.research.privacy", level="WARNING"):
                await database.dispose()
                with self.assertRaises(PrivacyRefusal) as caught:
                    await asyncio.wait_for(task, LATE)
            self.assertIs(caught.exception.reason, RefusalReason.AUDIT_FAILED)
            self.assertEqual(provider.search_calls, [])

    async def test_an_aborted_query_that_is_still_running_keeps_its_slot(self):
        database = StubbornDatabase(
            make_settings(
                database_url="postgresql://paw:pw@127.0.0.1:1/paw",
                database_pool_size=1,
            )
        )
        self.addAsyncCleanup(database.dispose)
        provider = web(hits=[hit()])
        broker = build_research_broker(
            registry_of(provider),
            database,
            clock=fixed_clock(),
            audit_timeout_seconds=0.3,
        )
        await self.assert_refused(broker, provider)
        self.assertEqual((database.started, database.running), (1, 1))
        # The query is still running: the slot is taken, the next send does not
        # open a second connection and is refused within its own deadline.
        await self.assert_refused(broker, provider)
        self.assertEqual((database.started, database.running), (1, 1))
        self.assertTrue(database._abortable_slots.locked())
        database.release.set()
        self.assertTrue(
            await wait_until(lambda: not database._abortable_slots.locked(), limit=5)
        )
        self.assertEqual(database.running, 0)


class FrozenRealServerTest(PostgresAuditTestCase):
    """A real server that answers, and then stops answering in mid-session."""

    async def test_a_send_after_the_server_went_silent_is_refused_on_time(self):
        url = make_url(TEST_DATABASE_URL)
        async with FreezableProxy(url.host, url.port) as proxy:
            proxied = url.set(host="127.0.0.1", port=proxy.port)
            database = Database(
                make_settings(
                    database_url=proxied.render_as_string(hide_password=False)
                )
            )
            self.addAsyncCleanup(database.dispose)
            provider = web(hits=[hit()])
            registry = registry_of(provider)
            # Healthy: a generous deadline (a slow machine must not fail this half),
            # the send is recorded and reaches the provider.
            healthy = build_research_broker(
                registry, database, clock=fixed_clock(), audit_timeout_seconds=30
            )
            await guarded(send(healthy, "python asyncio", self.project_id))
            self.assertEqual(len(provider.search_calls), 1)
            self.assertEqual(len(await self.rows()), 1)
            # The server stops answering (existing and new connections alike); the
            # same database, a short deadline.
            proxy.freeze()
            broker = build_research_broker(
                registry, database, clock=fixed_clock(), audit_timeout_seconds=0.7
            )
            started = time.monotonic()
            with self.assertLogs("paw_backend.research.privacy", level="WARNING"):
                with self.assertRaises(PrivacyRefusal) as caught:
                    await guarded(send(broker, "python asyncio 2", self.project_id))
            self.assertIs(caught.exception.reason, RefusalReason.AUDIT_FAILED)
            self.assertLess(time.monotonic() - started, LATE)
            # Nothing more was sent, and nothing more is claimed in the audit trail
            # (a write that was aborted may or may not have committed: here the
            # bytes never reached the server).
            self.assertEqual(len(provider.search_calls), 1)
            self.assertEqual(len(await self.rows()), 1)
            self.assertTrue(await wait_until(lambda: not database._probes, limit=5))
            self.assertFalse(database._abortable_slots.locked())


if __name__ == "__main__":
    unittest.main()
