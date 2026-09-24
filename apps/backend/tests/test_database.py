import asyncio
import logging
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Annotated
from unittest.mock import patch

import psycopg
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from paw_backend.api.deps import get_session
from paw_backend.app import create_app
from paw_backend.db import Database, DatabaseNotConfiguredError, DatabaseStatus

from .fake_postgres import HangingPostgres
from .support import FakeDatabase, make_client, make_settings, wait_until

PASSWORD = "s3cr3t-pw"
URL = f"postgresql://paw:{PASSWORD}@db.internal:5432/paw"


class UnconfiguredDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def test_check_reports_not_configured(self):
        database = Database(make_settings())
        self.assertFalse(database.configured)
        self.assertEqual(await database.check(), DatabaseStatus.NOT_CONFIGURED)

    async def test_engine_and_session_need_a_url(self):
        database = Database(make_settings())
        with self.assertRaises(DatabaseNotConfiguredError):
            _ = database.engine
        with self.assertRaises(DatabaseNotConfiguredError):
            database.session()


class ConfiguredDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def test_engine_is_lazy_async_psycopg_and_masks_the_password(self):
        database = Database(make_settings(database_url=URL, database_pool_size=3))
        self.assertTrue(database.configured)
        self.assertIsNone(database._engine)  # nothing is created at construction

        engine = database.engine
        self.assertIsInstance(engine, AsyncEngine)
        self.assertEqual(engine.dialect.driver, "psycopg")
        self.assertEqual(engine.pool.size(), 3)
        self.assertNotIn(PASSWORD, repr(engine.url))
        self.assertIs(database.engine, engine)
        await database.dispose()
        self.assertIsNone(database._engine)

    async def test_session_is_created_without_connecting(self):
        database = Database(make_settings(database_url=URL))
        async with database.session() as session:
            self.assertIsInstance(session, AsyncSession)
            self.assertIs(session.bind, database.engine)
        await database.dispose()

    async def test_dispose_without_an_engine_is_a_no_op(self):
        await Database(make_settings(database_url=URL)).dispose()


class ReadinessTimeoutTest(unittest.IsolatedAsyncioTestCase):
    """``check()`` must return at its timeout, however slowly the driver gives up."""

    TIMEOUT = 0.3

    def database(self, url: str = URL) -> Database:
        return Database(
            make_settings(database_url=url, database_timeout_seconds=self.TIMEOUT)
        )

    async def test_returns_at_the_timeout_even_if_cancellation_is_slow(self):
        class SlowToCancel(Database):
            async def _ping(self):
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    # What psycopg does: wait for the server to confirm the
                    # cancellation before letting the cancelled call end.
                    await asyncio.sleep(1.5)
                    raise

        database = SlowToCancel(
            make_settings(database_url=URL, database_timeout_seconds=self.TIMEOUT)
        )
        started = time.monotonic()
        with self.assertLogs("paw_backend.db", level=logging.WARNING):
            status = await database.check()
        elapsed = time.monotonic() - started

        self.assertEqual(status, DatabaseStatus.UNAVAILABLE)
        self.assertLess(elapsed, self.TIMEOUT + 0.5, "check() waited for the cancel")

    async def test_returns_at_the_timeout_when_the_server_stops_answering(self):
        # Real psycopg against a server that logs in and then ignores queries.
        async with HangingPostgres() as server:
            database = self.database(f"postgresql://paw:pw@127.0.0.1:{server.port}/paw")
            started = time.monotonic()
            with self.assertLogs("paw_backend.db", level=logging.WARNING):
                status = await database.check()
            elapsed = time.monotonic() - started

            self.assertEqual(status, DatabaseStatus.UNAVAILABLE)
            self.assertLess(elapsed, self.TIMEOUT + 1.0)
            # The abandoned probe ends by itself, quickly: its socket was shut
            # down, so psycopg never starts its (up to ten seconds long) wait
            # for the server to confirm a query cancellation.
            self.assertTrue(
                await wait_until(lambda: not database._probes, limit=2),
                "the timed-out probe is still running",
            )

    async def test_a_probe_that_is_still_connecting_is_cancelled(self):
        # The server accepts the TCP connection but never answers the login.
        async with HangingPostgres(login=False) as server:
            database = self.database(f"postgresql://paw:pw@127.0.0.1:{server.port}/paw")
            with self.assertLogs("paw_backend.db", level=logging.WARNING):
                status = await database.check()
            self.assertEqual(status, DatabaseStatus.UNAVAILABLE)
            self.assertTrue(await wait_until(lambda: not database._probes, limit=2))

    async def test_a_probe_aborted_after_the_timeout_leaves_no_task_behind(self):
        database = self.database()
        database._ping = lambda: asyncio.sleep(60)
        with self.assertLogs("paw_backend.db", level=logging.WARNING):
            await database.check()
        await asyncio.sleep(0.05)  # let the cancellation finish
        self.assertEqual(database._probes, set())


class ReadinessConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    """``/health/ready`` is reachable by anyone: probes must not multiply."""

    TIMEOUT = 0.5

    def database(self, port: int, **overrides) -> Database:
        overrides.setdefault("database_timeout_seconds", self.TIMEOUT)
        return Database(
            make_settings(
                database_url=f"postgresql://paw:pw@127.0.0.1:{port}/paw", **overrides
            )
        )

    async def test_concurrent_checks_share_one_probe_connection(self):
        async with HangingPostgres(answer_queries=True) as server:
            database = self.database(server.port)

            results = await asyncio.gather(*(database.check() for _ in range(50)))

            self.assertEqual(set(results), {DatabaseStatus.OK})
            self.assertEqual(server.logins, 1, "every check opened a connection")

    async def test_a_stalled_server_is_probed_once_and_all_calls_return_in_time(self):
        async with HangingPostgres() as server:
            database = self.database(server.port)
            started = time.monotonic()

            with self.assertLogs("paw_backend.db", level=logging.WARNING):
                results = await asyncio.gather(*(database.check() for _ in range(50)))
            elapsed = time.monotonic() - started

            self.assertEqual(set(results), {DatabaseStatus.UNAVAILABLE})
            self.assertEqual(server.logins, 1)
            self.assertLess(elapsed, self.TIMEOUT + 0.5)

    async def test_the_result_is_reused_within_the_interval_and_then_refreshed(self):
        async with HangingPostgres(answer_queries=True) as server:
            database = self.database(server.port, database_readiness_cache_seconds=0.3)

            await database.check()
            await database.check()
            self.assertEqual(server.logins, 1, "the result was not reused")

            await asyncio.sleep(0.35)
            await database.check()
            self.assertEqual(server.logins, 2, "the result was never refreshed")

    async def test_reuse_can_be_turned_off_but_concurrent_calls_still_share(self):
        async with HangingPostgres(answer_queries=True) as server:
            database = self.database(server.port, database_readiness_cache_seconds=0)

            await asyncio.gather(*(database.check() for _ in range(10)))
            self.assertEqual(server.logins, 1)
            await database.check()
            self.assertEqual(server.logins, 2)

    async def test_a_failure_is_not_reused_beyond_the_interval(self):
        pings = []

        class FailsOnce(Database):
            async def _ping(self):
                pings.append(time.monotonic())
                if len(pings) == 1:
                    raise ConnectionError("the database is starting")

        database = FailsOnce(
            make_settings(database_url=URL, database_readiness_cache_seconds=0.2)
        )

        with self.assertLogs("paw_backend.db", level=logging.WARNING):
            self.assertEqual(await database.check(), DatabaseStatus.UNAVAILABLE)
            self.assertEqual(await database.check(), DatabaseStatus.UNAVAILABLE)
        self.assertEqual(len(pings), 1, "the failure was probed again too soon")

        await asyncio.sleep(0.25)
        self.assertEqual(await database.check(), DatabaseStatus.OK)
        self.assertEqual(len(pings), 2)

    async def test_cancelling_a_waiting_request_leaves_the_shared_probe_running(self):
        pings = []

        class Slow(Database):
            async def _ping(self):
                pings.append(1)
                await asyncio.sleep(0.2)

        database = Slow(make_settings(database_url=URL, database_timeout_seconds=5))
        first, second, third = (asyncio.create_task(database.check()) for _ in range(3))
        await asyncio.sleep(0.05)

        first.cancel()  # the request that started the probe goes away
        self.assertEqual(
            await asyncio.gather(second, third),
            [DatabaseStatus.OK, DatabaseStatus.OK],
        )
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(len(pings), 1)

    async def test_dispose_ends_the_shared_probe_for_every_waiting_request(self):
        async with HangingPostgres() as server:
            database = self.database(
                server.port, database_timeout_seconds=30, shutdown_timeout_seconds=2
            )
            checks = [asyncio.create_task(database.check()) for _ in range(5)]
            self.assertTrue(await wait_until(lambda: database._probe_connections))

            started = time.monotonic()
            with self.assertLogs("paw_backend.db", level=logging.WARNING):
                await database.dispose()
                results = await asyncio.wait_for(asyncio.gather(*checks), 1)

            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(set(results), {DatabaseStatus.UNAVAILABLE})
            self.assertEqual(server.logins, 1)
            self.assertEqual((database._probes, database._flight), (set(), None))


class ConnectionUrlOptionsTest(unittest.IsolatedAsyncioTestCase):
    """Options in ``PAW_DATABASE_URL`` must not collide with the probe's own."""

    OPTIONS = "?connect_timeout=10&application_name=secret-app-name"

    async def test_url_options_that_the_probe_also_sets_are_merged(self):
        async with HangingPostgres(answer_queries=True) as server:
            database = Database(
                make_settings(
                    database_url=(
                        f"postgresql://paw:pw@127.0.0.1:{server.port}/paw{self.OPTIONS}"
                    )
                )
            )
            with self.assertNoLogs("paw_backend.db", level=logging.WARNING):
                self.assertEqual(await database.check(), DatabaseStatus.OK)

    async def test_the_probes_values_win_and_other_url_options_are_kept(self):
        captured = {}

        async def connect(**kwargs):
            captured.update(kwargs)
            raise psycopg.OperationalError("refused")

        database = Database(
            make_settings(
                database_url=f"postgresql://paw:pw@db.internal/paw{self.OPTIONS}",
                database_timeout_seconds=2,
            )
        )
        with (
            patch.object(psycopg.AsyncConnection, "connect", connect),
            self.assertLogs("paw_backend.db", level=logging.WARNING),
        ):
            await database.check()

        self.assertEqual(captured["connect_timeout"], 2)  # not the URL's "10"
        self.assertIs(captured["autocommit"], True)
        self.assertEqual(captured["application_name"], "secret-app-name")
        self.assertEqual(captured["host"], "db.internal")

    async def test_options_and_credentials_never_reach_a_log(self):
        async with HangingPostgres(login=False) as server:
            database = Database(
                make_settings(
                    database_url=(
                        f"postgresql://paw:pw@127.0.0.1:{server.port}/paw{self.OPTIONS}"
                    ),
                    database_timeout_seconds=0.3,
                )
            )
            started = time.monotonic()
            with self.assertLogs("paw_backend.db", level=logging.WARNING) as logs:
                status = await database.check()

        self.assertEqual(status, DatabaseStatus.UNAVAILABLE)
        self.assertLess(time.monotonic() - started, 1.5)
        for secret in ("secret-app-name", "connect_timeout", "pw@"):
            self.assertNotIn(secret, "\n".join(logs.output))


class DisposeTest(unittest.IsolatedAsyncioTestCase):
    """``dispose()`` must not leave probes for ``asyncio.run`` to wait for."""

    async def test_dispose_stops_a_probe_that_is_running_a_query(self):
        async with HangingPostgres() as server:
            database = Database(
                make_settings(
                    database_url=f"postgresql://paw:pw@127.0.0.1:{server.port}/paw",
                    database_timeout_seconds=30,
                    shutdown_timeout_seconds=2,
                )
            )
            check = asyncio.create_task(database.check())
            self.assertTrue(
                await wait_until(lambda: database._probe_connections, limit=3),
                "the probe never reached its query",
            )

            started = time.monotonic()
            with self.assertLogs("paw_backend.db", level=logging.WARNING):
                await database.dispose()
                status = await asyncio.wait_for(check, 1)

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(status, DatabaseStatus.UNAVAILABLE)
        self.assertEqual(database._probes, set())

    async def test_aborting_a_probe_twice_never_falls_back_to_cancellation(self):
        # A timed-out check() aborts its probe and dispose() aborts it again
        # before the probe has had a chance to run. The second shutdown of the
        # socket fails with ENOTCONN; cancelling then would start psycopg's
        # query cancellation, which is exactly what abort exists to avoid.
        async with HangingPostgres() as server:
            database = Database(
                make_settings(
                    database_url=f"postgresql://paw:pw@127.0.0.1:{server.port}/paw",
                    database_timeout_seconds=30,
                )
            )
            check = asyncio.create_task(database.check())
            self.assertTrue(await wait_until(lambda: database._probe_connections))
            (probe,) = database._probes

            database._abort(probe)
            database._abort(probe)

            self.assertEqual(probe.cancelling(), 0)
            with self.assertLogs("paw_backend.db", level=logging.WARNING):
                self.assertEqual(await check, DatabaseStatus.UNAVAILABLE)

    async def test_dispose_is_bounded_when_a_probe_ignores_abort_and_cancel(self):
        release = asyncio.Event()
        self.addCleanup(release.set)  # lets the stuck probe end with the test

        class Stubborn(Database):
            async def _ping(self):
                while not release.is_set():
                    try:
                        await asyncio.sleep(0.02)
                    except asyncio.CancelledError:
                        pass  # like a cancel that is blocked in a thread

        database = Stubborn(
            make_settings(
                database_url=URL,
                database_timeout_seconds=30,
                shutdown_timeout_seconds=1,
            )
        )
        check = asyncio.create_task(database.check())
        await asyncio.sleep(0.05)

        started = time.monotonic()
        with self.assertLogs("paw_backend.db", level=logging.WARNING) as logs:
            await database.dispose()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5, "dispose() waited for the stuck probe")
        self.assertIn("did not stop", "\n".join(logs.output))
        release.set()
        await check


class ProcessTeardownTest(unittest.TestCase):
    """The process must exit promptly after a probe timed out on a stalled server.

    Runs in a child process because the delay is in ``asyncio.run``'s shutdown
    (it waits for threads and tasks that are still running), which cannot be
    observed from inside the loop. ``libpq_fallback`` makes psycopg behave as
    with a libpq older than 17, where a query cancellation blocks a thread.
    """

    def run_child(self, mode: str) -> float:
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
            self.fail(f"the process ({mode}) did not exit after dispose()")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("done", result.stdout)
        return time.monotonic() - started

    def test_exit_is_prompt_with_query_cancellation_via_libpq_17(self):
        self.assertLess(self.run_child("cancel_safe"), 8)

    def test_exit_is_prompt_when_libpq_cancels_from_a_thread(self):
        self.assertLess(self.run_child("libpq_fallback"), 8)


class SessionDependencyTest(unittest.TestCase):
    def build_client(self, database: Database) -> TestClient:
        app = create_app(make_settings(), database=database)
        router = APIRouter()

        @router.get("/test/session")
        async def session_route(
            session: Annotated[AsyncSession, Depends(get_session)],
        ):
            return {"session": type(session).__name__}

        app.include_router(router)
        return make_client(app)

    def test_unconfigured_database_answers_503(self):
        response = self.build_client(FakeDatabase()).get("/test/session")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_not_configured")

    def test_configured_database_provides_a_session(self):
        database = Database(make_settings(database_url=URL))
        response = self.build_client(database).get("/test/session")
        self.assertEqual(response.json(), {"session": "AsyncSession"})


if __name__ == "__main__":
    unittest.main()
