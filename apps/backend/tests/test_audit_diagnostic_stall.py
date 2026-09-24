"""The startup audit diagnostic must not be able to hold up shutdown.

A PostgreSQL that accepts the connection but never answers the catalog query
used to keep the diagnostic task inside psycopg's server-side query
cancellation (up to about ten seconds, or a blocked thread with a libpq older
than 17) when the application shut down. The diagnostic now runs on a dedicated
connection whose socket is shut down instead (``Database.fetch_abortable``).
"""

import asyncio
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from paw_backend.app import create_app
from paw_backend.authz import (
    Authorizer,
    Capability,
    PostgresAuditSink,
    ProjectRole,
    SystemRole,
)
from paw_backend.authz.diagnostics import (
    read_audit_table_access,
    warn_if_audit_table_is_mutable,
)
from paw_backend.db import Database, DatabaseNotConfiguredError
from paw_backend.identity.diagnostics import (
    read_token_table_access,
    warn_if_tokens_can_be_minted,
)

from .authz_support import P1, U1, principal, repo_resource
from .fake_postgres import HangingPostgres
from .support import FakeDatabase, make_settings, wait_until


def settings_for(server: HangingPostgres, **overrides):
    return make_settings(
        database_url=f"postgresql://paw:pw@127.0.0.1:{server.port}/paw", **overrides
    )


class FetchAbortableTest(unittest.IsolatedAsyncioTestCase):
    async def test_it_returns_the_rows_of_a_healthy_server(self):
        async with HangingPostgres(answer_queries=True) as server:
            database = Database(settings_for(server))
            self.addAsyncCleanup(database.dispose)
            self.assertEqual(await database.fetch_abortable("SELECT 1"), [(1,)])
            # The dedicated connection is gone and the pool was never involved.
            self.assertEqual(database._probe_connections, {})
            self.assertEqual(database._probes, set())
            self.assertIsNone(database._engine)

    async def test_it_needs_a_configured_database(self):
        with self.assertRaises(DatabaseNotConfiguredError):
            await Database(make_settings()).fetch_abortable("SELECT 1")

    async def test_a_stalled_query_times_out_on_time_without_a_server_cancel(self):
        async with HangingPostgres() as server:
            database = Database(settings_for(server))
            self.addAsyncCleanup(database.dispose)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                await database.fetch_abortable("SELECT 1", timeout_seconds=0.3)
            self.assertLess(time.monotonic() - started, 1.5)
            # The task and its connection are wound down, not left hanging.
            self.assertTrue(await wait_until(lambda: not database._probes, limit=3))
            self.assertEqual(database._probe_connections, {})

    async def test_cancelling_the_caller_aborts_the_connection_promptly(self):
        async with HangingPostgres() as server:
            database = Database(settings_for(server))
            self.addAsyncCleanup(database.dispose)
            caller = asyncio.create_task(database.fetch_abortable("SELECT 1"))
            self.assertTrue(await wait_until(lambda: server.logins == 1))
            await asyncio.sleep(0.2)  # inside the query now
            started = time.monotonic()
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 3)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertTrue(await wait_until(lambda: not database._probes, limit=3))

    async def test_abortable_statements_are_limited_to_the_pool_size(self):
        async with HangingPostgres() as server:
            database = Database(settings_for(server, database_pool_size=1))
            self.addAsyncCleanup(database.dispose)
            first = asyncio.create_task(
                database.execute_abortable("SELECT 1", timeout_seconds=30)
            )
            self.assertTrue(await wait_until(lambda: server.logins == 1))
            started = time.monotonic()
            with self.assertRaises(TimeoutError):  # no free slot within its limit
                await database.execute_abortable("SELECT 1", timeout_seconds=0.3)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(server.logins, 1)  # it never opened a connection
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            self.assertTrue(await wait_until(lambda: not database._probes, limit=3))

    async def test_dispose_aborts_a_query_in_flight(self):
        async with HangingPostgres() as server:
            database = Database(settings_for(server, shutdown_timeout_seconds=2))
            caller = asyncio.create_task(
                database.fetch_abortable("SELECT 1", timeout_seconds=30)
            )
            self.assertTrue(await wait_until(lambda: server.logins == 1))
            await asyncio.sleep(0.2)
            started = time.monotonic()
            await database.dispose()
            self.assertLess(time.monotonic() - started, 1.5)
            with self.assertRaises(Exception) as caught:
                await asyncio.wait_for(caller, 3)
            self.assertNotIsInstance(caught.exception, TimeoutError)


class AuditWriteStallTest(unittest.IsolatedAsyncioTestCase):
    """A required audit write must fail closed on time, not after a server cancel."""

    async def test_a_stalled_audit_insert_is_given_up_on_time(self):
        async with HangingPostgres() as server:
            database = Database(settings_for(server))
            self.addAsyncCleanup(database.dispose)
            authorizer = Authorizer(PostgresAuditSink(database), timeout_seconds=0.3)
            contributor = principal(
                SystemRole.USER, user_id=U1, projects={P1: ProjectRole.CONTRIBUTOR}
            )
            started = time.monotonic()
            with self.assertLogs("paw_backend.authz.authorizer", level="WARNING"):
                decision = await authorizer.authorize(
                    contributor,
                    Capability.PROJECT_REPO_WRITE,
                    repo_resource(set()),  # an override that forbids: audited denial
                )
            elapsed = time.monotonic() - started
            self.assertFalse(decision.allowed)
            # psycopg's own server-side cancellation would take about ten seconds.
            self.assertLess(elapsed, 1.5)
            # The connection is wound down, not left hanging.
            self.assertTrue(await wait_until(lambda: not database._probes, limit=3))
            self.assertEqual(database._probe_connections, {})


class DiagnosticStallTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_stalled_catalog_query_is_given_up_on_time(self):
        async with HangingPostgres() as server:
            database = Database(settings_for(server))
            self.addAsyncCleanup(database.dispose)
            started = time.monotonic()
            with self.assertLogs("paw_backend.authz.diagnostics", level="INFO") as logs:
                await warn_if_audit_table_is_mutable(database, 0.3)
            self.assertLess(time.monotonic() - started, 1.5)
            (line,) = logs.output
            self.assertTrue(line.startswith("INFO:"))
            self.assertIn("skipped (TimeoutError)", line)

    async def test_the_read_helper_uses_the_same_bounded_path(self):
        async with HangingPostgres() as server:
            database = Database(settings_for(server))
            self.addAsyncCleanup(database.dispose)
            with self.assertRaises(TimeoutError):
                await read_audit_table_access(database, 0.3)

    async def test_a_stalled_token_privilege_query_is_given_up_on_time(self):
        async with HangingPostgres() as server:
            database = Database(settings_for(server))
            self.addAsyncCleanup(database.dispose)
            started = time.monotonic()
            with self.assertLogs(
                "paw_backend.identity.diagnostics", level="INFO"
            ) as logs:
                await warn_if_tokens_can_be_minted(database, 0.3)
            self.assertLess(time.monotonic() - started, 1.5)
            (line,) = logs.output
            self.assertIn("skipped (TimeoutError)", line)
            with self.assertRaises(TimeoutError):
                await read_token_table_access(database, 0.3)

    async def test_the_application_leaves_its_lifespan_on_time_with_a_stalled_query(
        self,
    ):
        async with HangingPostgres() as server:
            app = create_app(
                settings_for(
                    server, database_timeout_seconds=30, shutdown_timeout_seconds=2
                )
            )
            async with asyncio.timeout(8):  # without the fix: ~10 s (or a hang)
                started = None
                async with app.router.lifespan_context(app):
                    # The audit and the Owner-token diagnostics, one connection each.
                    self.assertTrue(await wait_until(lambda: server.logins == 2))
                    await asyncio.sleep(0.3)  # both are inside their query
                    started = time.monotonic()
                self.assertLess(time.monotonic() - started, 1.5)


class ShutdownWaitIsBoundedTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_diagnostic_that_ignores_cancellation_cannot_block_shutdown(self):
        async def stubborn(database, timeout_seconds):
            for _ in range(20):  # about two seconds; swallows every cancel
                try:
                    await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    pass

        database = FakeDatabase()
        app = create_app(make_settings(shutdown_timeout_seconds=1), database=database)
        with patch("paw_backend.app.warn_if_audit_table_is_mutable", stubborn):
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0.1)
                started = time.monotonic()
        elapsed = time.monotonic() - started
        # It waits for the shutdown budget (1 s), not for the task (2 s) ...
        self.assertGreater(elapsed, 0.8)
        self.assertLess(elapsed, 1.7)
        # ... and still goes on to close the database.
        self.assertTrue(database.disposed)


class ProcessShutdownTest(unittest.TestCase):
    """The whole process exits promptly, not only the lifespan.

    A child process, because a blocked thread or task shows up in
    ``asyncio.run``'s shutdown, which cannot be observed from inside the loop
    (same pattern as ``test_database.ProcessTeardownTest``).
    """

    def run_child(self, mode: str) -> tuple[float, float]:
        environment = {k: v for k, v in os.environ.items() if not k.startswith("PAW_")}
        started = time.monotonic()
        try:
            result = subprocess.run(
                [sys.executable, "-m", "tests.teardown_child", mode],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            self.fail(f"the process ({mode}) did not exit after shutdown")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("done", result.stdout)
        shutdown = next(
            float(line.split()[1])
            for line in result.stdout.splitlines()
            if line.startswith("shutdown ")
        )
        return shutdown, time.monotonic() - started

    def test_shutdown_is_prompt_with_query_cancellation_via_libpq_17(self):
        shutdown, total = self.run_child("audit_cancel_safe")
        self.assertLess(shutdown, 1.5)  # without the fix: about 9 seconds
        self.assertLess(total, 8)

    def test_shutdown_is_prompt_when_libpq_cancels_from_a_thread(self):
        shutdown, total = self.run_child("audit_libpq_fallback")
        self.assertLess(shutdown, 1.5)  # without the fix: never returns
        self.assertLess(total, 8)


if __name__ == "__main__":
    unittest.main()
