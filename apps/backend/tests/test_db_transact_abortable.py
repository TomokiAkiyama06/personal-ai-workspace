"""``Database.transact_abortable``: a multi-statement transaction that fails closed
on time.

``fetch_abortable`` bounds one statement on a connection outside the pool. The
approval store also needs a lock, a read and a change in ONE transaction
(``open_request`` / ``consume``), and a pooled transaction is not bounded by
``asyncio.timeout`` on a server that stops answering (the cancel of a pooled
query waits about ten seconds for the server to confirm it). These tests check
the same guarantees for a transaction, without a PostgreSQL (a server that
answers nothing) and with one (transactions and locks).
"""

import asyncio
import time
import unittest
import uuid

from sqlalchemy import text

from paw_backend.db import _SERVER_GRACE_SECONDS, Database, DatabaseNotConfiguredError

from .fake_postgres import HangingPostgres
from .support import make_settings, wait_until
from .task_support import new_database, requires_postgres


def settings_for(server: HangingPostgres, **overrides):
    return make_settings(
        database_url=f"postgresql://paw:pw@127.0.0.1:{server.port}/paw", **overrides
    )


class TransactAbortableStallTest(unittest.IsolatedAsyncioTestCase):
    """A PostgreSQL that accepts the connection and then answers nothing."""

    async def test_a_stalled_transaction_times_out_on_time_and_runs_no_work(self):
        calls = []

        async def work(connection):
            calls.append(connection)

        async with HangingPostgres() as server:
            database = Database(settings_for(server))
            self.addAsyncCleanup(database.dispose)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                await database.transact_abortable(work, timeout_seconds=0.3)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(calls, [])
            # nothing went through the pool, and everything is given back
            self.assertIsNone(database._engine)
            self.assertTrue(
                await wait_until(
                    lambda: (
                        not database._probes
                        and not database._probe_connections
                        and database._abortable_slots._value
                        == database._settings.database_pool_size
                    ),
                    limit=3,
                )
            )

    async def test_cancelling_the_caller_aborts_the_connection_promptly(self):
        async def work(connection):
            return None

        async with HangingPostgres() as server:
            database = Database(settings_for(server))
            self.addAsyncCleanup(database.dispose)
            caller = asyncio.create_task(
                database.transact_abortable(work, timeout_seconds=30)
            )
            self.assertTrue(await wait_until(lambda: server.logins == 1))
            await asyncio.sleep(0.2)  # inside the first statement now
            started = time.monotonic()
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 3)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertTrue(await wait_until(lambda: not database._probes, limit=3))
            self.assertEqual(database._probe_connections, {})

    async def test_it_shares_the_slots_and_the_deadline_with_the_statements(self):
        async def work(connection):
            return None

        async with HangingPostgres() as server:
            database = Database(settings_for(server, database_pool_size=1))
            self.addAsyncCleanup(database.dispose)
            first = asyncio.create_task(
                database.transact_abortable(work, timeout_seconds=30)
            )
            self.assertTrue(await wait_until(lambda: server.logins == 1))
            started = time.monotonic()
            with self.assertRaises(TimeoutError):  # no free slot within its limit
                await database.fetch_abortable("SELECT 1", timeout_seconds=0.3)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(server.logins, 1)  # it never opened a connection
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            self.assertTrue(await wait_until(lambda: not database._probes, limit=3))

    async def test_it_needs_a_configured_database(self):
        async def work(connection):
            return None

        with self.assertRaises(DatabaseNotConfiguredError):
            await Database(make_settings()).transact_abortable(work)


@requires_postgres
class TransactAbortableTest(unittest.IsolatedAsyncioTestCase):
    """Transactions and locks on a real PostgreSQL."""

    LIMIT = 0.5
    GUARD = 10  # far above the limit: only reached by a call that ignores it

    async def asyncSetUp(self):
        self.database = new_database()
        self.addAsyncCleanup(self.database.dispose)
        self.table = f"abortable_probe_{uuid.uuid4().hex}"
        async with self.database.engine.begin() as connection:
            await connection.execute(text(f"CREATE TABLE {self.table} (n integer)"))
        self.addAsyncCleanup(self.drop_table)

    async def drop_table(self):
        async with self.database.engine.begin() as connection:
            await connection.execute(text(f"DROP TABLE IF EXISTS {self.table}"))

    async def rows(self) -> list[int]:
        async with self.database.engine.begin() as connection:
            result = await connection.execute(text(f"SELECT n FROM {self.table}"))
            return sorted(row[0] for row in result)

    async def backends(self, condition: str) -> int:
        """The other backends of this database that satisfy ``condition``."""
        async with self.database.engine.begin() as connection:
            return (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname ="
                        " current_database() AND pid <> pg_backend_pid() AND "
                        + condition
                    )
                )
            ).scalar_one()

    async def until_no_backend(self, condition: str) -> None:
        async with asyncio.timeout(self.GUARD):
            while True:
                if await self.backends(condition) == 0:
                    return
                await asyncio.sleep(0.02)

    async def test_it_returns_the_result_and_commits_when_the_work_returns(self):
        async def work(connection):
            await connection.execute(f"INSERT INTO {self.table} VALUES (1)")
            await connection.execute(f"INSERT INTO {self.table} VALUES (2)")
            return "done"

        self.assertEqual(await self.database.transact_abortable(work), "done")
        self.assertEqual(await self.rows(), [1, 2])

    async def test_its_statements_are_one_transaction_on_one_connection(self):
        async def work(connection):
            seen = []
            for _ in range(2):
                cursor = await connection.execute(
                    "SELECT txid_current(), pg_backend_pid()"
                )
                seen.append(await cursor.fetchone())
            return seen

        first, second = await self.database.transact_abortable(work)
        self.assertEqual(first, second)

    async def test_it_rolls_back_when_the_work_raises(self):
        class Failure(Exception):
            pass

        async def work(connection):
            await connection.execute(f"INSERT INTO {self.table} VALUES (1)")
            raise Failure

        with self.assertRaises(Failure):  # the caller's own exception, unchanged
            await self.database.transact_abortable(work)
        self.assertEqual(await self.rows(), [])

    async def test_a_transaction_that_is_aborted_is_rolled_back(self):
        started = asyncio.Event()
        tag = uuid.uuid4().hex  # to tell this statement from any other backend's

        async def work(connection):
            await connection.execute(f"INSERT INTO {self.table} VALUES (1)")
            started.set()
            await connection.execute(f"SELECT pg_sleep(60) /* {tag} */")

        began = time.monotonic()
        with self.assertRaises(TimeoutError):
            await self.database.transact_abortable(work, timeout_seconds=self.LIMIT)
        self.assertLess(time.monotonic() - began, self.GUARD / 2)
        self.assertTrue(started.is_set())
        # the server gives the statement up (its own limit), and with the
        # connection closed it never sees a COMMIT
        await self.until_no_backend(f"query LIKE '%{tag}%'")
        self.assertEqual(await self.rows(), [])

    async def test_the_server_is_told_to_stop_waiting_shortly_after_the_caller(self):
        async def work(connection):
            cursor = await connection.execute(
                "SELECT name, setting::integer FROM pg_settings"
                " WHERE name IN ('lock_timeout', 'statement_timeout')"
            )
            return dict(await cursor.fetchall())

        limit = 2.0
        milliseconds = await self.database.transact_abortable(
            work, timeout_seconds=limit
        )
        self.assertEqual(set(milliseconds), {"lock_timeout", "statement_timeout"})
        for name, value in milliseconds.items():
            with self.subTest(setting=name):
                # what is left of the limit (a little less than all of it) plus the
                # grace, so that the caller's own deadline is always reached first
                self.assertGreater(value, (limit - 0.5 + _SERVER_GRACE_SECONDS) * 1000)
                self.assertLessEqual(value, (limit + _SERVER_GRACE_SECONDS) * 1000)

    async def test_a_statement_that_waits_on_a_lock_leaves_the_server_by_itself(self):
        """The closed socket does not wake a backend that waits on a lock: only
        the limit that was set on the server does (the holder still holds)."""

        async def work(connection):
            await connection.execute("SELECT pg_advisory_xact_lock(4711)")

        async with self.database.engine.connect() as holder:
            await holder.execute(text("SELECT pg_advisory_xact_lock(4711)"))
            try:
                began = time.monotonic()
                with self.assertRaises(TimeoutError):
                    await self.database.transact_abortable(
                        work, timeout_seconds=self.LIMIT
                    )
                self.assertLess(time.monotonic() - began, self.GUARD / 2)
                await self.until_no_backend("wait_event_type = 'Lock'")
                left = time.monotonic() - began
                # within the limit and its grace (plus the polling and a margin)
                self.assertLess(left, self.LIMIT + _SERVER_GRACE_SECONDS + 3)
            finally:
                await holder.rollback()


@requires_postgres
class DisposeAbortsATransactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_dispose_aborts_a_transaction_in_flight(self):
        database = new_database()
        started = asyncio.Event()
        tag = uuid.uuid4().hex

        async def work(connection):
            started.set()
            await connection.execute(f"SELECT pg_sleep(60) /* {tag} */")

        caller = asyncio.create_task(
            database.transact_abortable(work, timeout_seconds=2)
        )
        async with asyncio.timeout(10):
            await started.wait()
        await asyncio.sleep(0.2)
        began = time.monotonic()
        await database.dispose()
        self.assertLess(time.monotonic() - began, 1.5)
        with self.assertRaises(Exception) as caught:
            await asyncio.wait_for(caller, 3)
        self.assertNotIsInstance(caught.exception, TimeoutError)
        # do not leave the abandoned statement to the tests that follow: it ends
        # by its server-side limit (2 s + the grace)
        observer = new_database()
        self.addAsyncCleanup(observer.dispose)
        async with asyncio.timeout(10):
            while True:
                async with observer.engine.begin() as connection:
                    left = (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity WHERE"
                                " datname = current_database() AND"
                                " pid <> pg_backend_pid() AND query LIKE :tag"
                            ),
                            {"tag": f"%{tag}%"},
                        )
                    ).scalar_one()
                if left == 0:
                    break
                await asyncio.sleep(0.05)
