import asyncio
import logging
import time
import unittest
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from paw_backend.api.deps import get_session
from paw_backend.app import create_app
from paw_backend.db import Database, DatabaseNotConfiguredError, DatabaseStatus

from .fake_postgres import HangingPostgres
from .support import FakeDatabase, make_client, make_settings

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
        # Real psycopg against a server that logs in and then ignores queries:
        # the cancel request is never confirmed and psycopg waits ~10 s for it.
        # Its background warnings about the abandoned cancel are not under test.
        psycopg_logger = logging.getLogger("psycopg")
        self.addCleanup(psycopg_logger.setLevel, psycopg_logger.level)
        psycopg_logger.setLevel(logging.CRITICAL)
        async with HangingPostgres() as server:
            database = self.database(f"postgresql://paw:pw@127.0.0.1:{server.port}/paw")
            started = time.monotonic()
            with self.assertLogs("paw_backend.db", level=logging.WARNING):
                status = await database.check()
            elapsed = time.monotonic() - started

        self.assertEqual(status, DatabaseStatus.UNAVAILABLE)
        self.assertLess(elapsed, self.TIMEOUT + 1.0)

    async def test_a_probe_cancelled_after_the_timeout_leaves_no_task_behind(self):
        database = self.database()
        database._ping = lambda: asyncio.sleep(60)
        with self.assertLogs("paw_backend.db", level=logging.WARNING):
            await database.check()
        await asyncio.sleep(0.05)  # let the cancellation finish
        self.assertEqual(database._cancelling, set())


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
