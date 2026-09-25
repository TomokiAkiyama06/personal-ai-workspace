"""``Database.run_abortable``: a transaction that can be aborted at once (PAW-050).

The stalled-server cases use the ``HangingPostgres`` fake (never answers a
query); the semantics of the transaction itself (commit, rollback, the number of
connections) use a real PostgreSQL and are skipped unless
``PAW_TEST_DATABASE_URL`` is set. The janitor's purge on top of it is tested in
``test_scratch_janitor_stall`` and ``test_scratch_janitor_postgres``.
"""

import asyncio
import time
import unittest
from unittest.mock import patch

import psycopg
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from paw_backend.db import Database, DatabaseNotConfiguredError

from .fake_postgres import HangingPostgres
from .memory_support import TEST_DATABASE_URL, requires_postgres
from .support import make_settings, wait_until

DEADLINE = 30  # generous: only a hang can reach it


async def select_one(session) -> int:
    return (await session.execute(text("SELECT 1"))).scalar_one()


class UnconfiguredTest(unittest.IsolatedAsyncioTestCase):
    async def test_it_needs_a_configured_database(self):
        database = Database(make_settings())
        with self.assertRaises(DatabaseNotConfiguredError):
            await database.run_abortable(select_one)
        self.assertEqual(database._probes, set())  # nothing was started


class StalledServerTest(unittest.IsolatedAsyncioTestCase):
    async def start(self, server: HangingPostgres, **overrides):
        database = Database(
            make_settings(
                database_url=f"postgresql://paw:pw@127.0.0.1:{server.port}/paw",
                database_timeout_seconds=30,
                **overrides,
            )
        )
        self.addAsyncCleanup(database.dispose)
        call = asyncio.create_task(database.run_abortable(select_one))
        self.addCleanup(call.cancel)
        self.assertTrue(await wait_until(lambda: server.logins == 1))
        await asyncio.sleep(0.2)  # inside its first statement
        self.assertFalse(call.done())
        return database, call

    async def test_cancelling_the_caller_shuts_the_connection_down_at_once(self):
        async with HangingPostgres() as server:
            database, call = await self.start(server)
            self.assertEqual(len(database._probe_connections), 1)

            started = time.monotonic()
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call

            # psycopg's own server-side cancellation would take about ten seconds.
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertTrue(await wait_until(lambda: not database._probes, limit=5))
            self.assertEqual(database._probe_connections, {})
            # The slot is free again for the next call.
            self.assertEqual(database._abortable_slots.free, 5)

    async def test_dispose_aborts_a_running_transaction_within_the_budget(self):
        async with HangingPostgres() as server:
            database, call = await self.start(server, shutdown_timeout_seconds=4)

            started = time.monotonic()
            await database.dispose()

            self.assertLess(time.monotonic() - started, 1.0)
            done, pending = await asyncio.wait({call}, timeout=DEADLINE)
            self.assertEqual(pending, set())
            # An error of the aborted connection, never a result.
            self.assertIsInstance(call.exception(), DBAPIError)
            self.assertEqual(database._probe_connections, {})
            self.assertIsNone(database._abortable_engine)

    async def test_a_transaction_that_is_still_connecting_is_cancelled(self):
        async with HangingPostgres(login=False) as server:
            database = Database(
                make_settings(
                    database_url=f"postgresql://paw:pw@127.0.0.1:{server.port}/paw",
                    database_timeout_seconds=30,
                )
            )
            self.addAsyncCleanup(database.dispose)
            call = asyncio.create_task(database.run_abortable(select_one))
            self.assertTrue(await wait_until(lambda: server.logins == 1))

            started = time.monotonic()
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call

            self.assertLess(time.monotonic() - started, 1.0)
            self.assertTrue(await wait_until(lambda: not database._probes, limit=5))

    async def test_url_options_are_merged_and_the_connection_is_not_autocommit(self):
        captured = {}

        async def connect(**kwargs):
            captured.update(kwargs)
            raise psycopg.OperationalError("refused")

        database = Database(
            make_settings(
                database_url=(
                    "postgresql://paw:pw@db.internal/paw"
                    "?connect_timeout=10&autocommit=true&application_name=name"
                ),
                database_timeout_seconds=2,
            )
        )
        self.addAsyncCleanup(database.dispose)
        with (
            patch.object(psycopg.AsyncConnection, "connect", connect),
            self.assertRaises(DBAPIError),
        ):
            await database.run_abortable(select_one)

        self.assertEqual(captured["connect_timeout"], 2)  # not the URL's "10"
        # SQLAlchemy owns the transaction: an autocommit URL option must not win.
        self.assertIs(captured["autocommit"], False)
        self.assertEqual(captured["application_name"], "name")
        self.assertEqual(captured["host"], "db.internal")


@requires_postgres
class TransactionTest(unittest.IsolatedAsyncioTestCase):
    TABLE = "paw_run_abortable_probe"

    async def asyncSetUp(self) -> None:
        self.database = Database(
            make_settings(database_url=TEST_DATABASE_URL, database_pool_size=2)
        )
        self.addAsyncCleanup(self.database.dispose)
        async with self.database.session() as session, session.begin():
            await session.execute(text(f"DROP TABLE IF EXISTS {self.TABLE}"))
            await session.execute(text(f"CREATE TABLE {self.TABLE} (n int)"))
        self.addAsyncCleanup(self.drop_table)

    async def drop_table(self) -> None:
        async with self.database.session() as session, session.begin():
            await session.execute(text(f"DROP TABLE IF EXISTS {self.TABLE}"))

    async def count(self) -> int:
        async with self.database.session() as session:
            result = await session.execute(text(f"SELECT count(*) FROM {self.TABLE}"))
            return result.scalar_one()

    async def test_a_normal_return_commits_and_gives_back_the_result(self):
        async def work(session):
            await session.execute(text(f"INSERT INTO {self.TABLE} VALUES (1), (2)"))
            return "done"

        self.assertEqual(await self.database.run_abortable(work), "done")

        self.assertEqual(await self.count(), 2)
        self.assertEqual(self.database._probes, set())
        self.assertEqual(self.database._probe_connections, {})

    async def test_an_exception_rolls_the_transaction_back_and_is_reraised(self):
        class Boom(Exception):
            pass

        async def work(session):
            await session.execute(text(f"INSERT INTO {self.TABLE} VALUES (1)"))
            raise Boom

        with self.assertRaises(Boom):
            await self.database.run_abortable(work)

        self.assertEqual(await self.count(), 0)
        self.assertEqual(self.database._probe_connections, {})

    async def test_all_statements_run_in_one_transaction(self):
        async def work(session):
            first = (await session.execute(text("SELECT txid_current()"))).scalar_one()
            second = (await session.execute(text("SELECT txid_current()"))).scalar_one()
            return first, second

        first, second = await self.database.run_abortable(work)

        # A transaction id is assigned once per transaction: two statements that
        # each committed on their own would have got two different ids.
        self.assertEqual(first, second)

    async def test_at_most_pool_size_transactions_run_at_once(self):
        release = asyncio.Event()
        running = 0
        peak = 0

        async def work(session):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            try:
                await release.wait()
            finally:
                running -= 1

        calls = [
            asyncio.create_task(self.database.run_abortable(work)) for _ in range(4)
        ]
        self.assertTrue(await wait_until(lambda: running == 2))
        await asyncio.sleep(0.2)  # the other two are waiting for a slot
        self.assertEqual((running, peak), (2, 2))

        release.set()
        await asyncio.wait_for(asyncio.gather(*calls), DEADLINE)
        self.assertEqual(peak, 2)

    async def test_a_caller_cancelled_while_waiting_for_a_slot_leaks_nothing(self):
        release = asyncio.Event()

        async def hold(session):
            await release.wait()

        holders = [
            asyncio.create_task(self.database.run_abortable(hold)) for _ in range(2)
        ]
        self.assertTrue(await wait_until(lambda: len(self.database._probes) == 2))
        waiting = asyncio.create_task(self.database.run_abortable(select_one))
        await asyncio.sleep(0.1)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting

        release.set()
        await asyncio.wait_for(asyncio.gather(*holders), DEADLINE)
        # Every slot is back: two calls run at once again.
        again = [self.database.run_abortable(select_one) for _ in range(2)]
        results = await asyncio.wait_for(asyncio.gather(*again), DEADLINE)
        self.assertEqual(results, [1, 1])

    async def backends_in_the_sleep(self) -> int:
        async with self.database.session() as session:
            result = await session.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid <> "
                    "pg_backend_pid() AND datname = current_database() "
                    "AND query LIKE '%pg_sleep(1)%'"
                )
            )
            return result.scalar_one()

    async def test_cancelling_the_caller_rolls_the_transaction_back(self):
        """A statement that is running on a healthy server is not left half done."""
        inside = asyncio.Event()

        async def work(session):
            await session.execute(text(f"INSERT INTO {self.TABLE} VALUES (1)"))
            inside.set()
            await session.execute(text("SELECT pg_sleep(1)"))

        call = asyncio.create_task(self.database.run_abortable(work))
        await asyncio.wait_for(inside.wait(), DEADLINE)

        started = time.monotonic()
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        self.assertLess(time.monotonic() - started, 0.9)  # not the statement's 1 s

        # The server only notices the closed connection when the statement ends:
        # wait until the backend is gone. The insert must not have been committed.
        async with asyncio.timeout(DEADLINE):
            # Polls the server's own state; there is no event to wait on.
            while await self.backends_in_the_sleep():  # noqa: ASYNC110
                await asyncio.sleep(0.05)
        self.assertEqual(await self.count(), 0)


if __name__ == "__main__":
    unittest.main()
