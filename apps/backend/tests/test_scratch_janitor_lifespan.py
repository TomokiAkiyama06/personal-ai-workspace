"""The application starts the scratch janitor and stops it on shutdown (PAW-050).

The janitor itself is tested in ``test_scratch_janitor`` (fakes) and
``test_scratch_janitor_postgres`` (a real PostgreSQL). Here the ``ScratchJanitor``
that ``create_app`` builds is replaced by a recorder, to see *whether* and *how*
the lifespan starts it, and that shutdown cancels it before the database is
disposed, within the shutdown budget.
"""

import asyncio
import time
import unittest
from unittest.mock import patch

from paw_backend.app import create_app
from paw_backend.db import Database
from paw_backend.research.scratch import ScratchStore

from .support import FakeDatabase, make_settings, wait_until

# Nothing listens on port 1: the startup diagnostics fail at once, quietly.
UNREACHABLE = "postgresql://paw:pw@127.0.0.1:1/paw"


def configured(**overrides):
    """Settings with a database (that nothing listens on) and the database."""
    settings = make_settings(database_url=UNREACHABLE, **overrides)
    return settings, RecordingDatabase(settings)


class RecordingDatabase(Database):
    """A configured database that never gets a working connection."""

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.disposed = False
        self.on_dispose = lambda: None

    async def dispose(self) -> None:
        self.on_dispose()
        self.disposed = True
        await super().dispose()


class RecordingJanitor:
    """Stands in for ``ScratchJanitor``: records how it was built and stopped."""

    instances: list["RecordingJanitor"] = []

    def __init__(self, store, **options) -> None:
        self.store = store
        self.options = options
        self.started = asyncio.Event()
        self.cancelled = False
        self.__class__.instances.append(self)

    async def run(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class LifespanTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        RecordingJanitor.instances = []
        patcher = patch("paw_backend.app.ScratchJanitor", RecordingJanitor)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def run_lifespan(self, app) -> None:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)


class StartTest(LifespanTestCase):
    async def test_it_starts_with_a_configured_database_and_the_default_interval(self):
        settings, database = configured()
        app = create_app(settings, database=database)

        async with app.router.lifespan_context(app):
            (janitor,) = RecordingJanitor.instances
            self.assertTrue(await wait_until(janitor.started.is_set))
            self.assertIsInstance(janitor.store, ScratchStore)
            self.assertIs(janitor.store._database, database)
            self.assertEqual(janitor.options, {"interval_seconds": 3600})
            self.assertFalse(janitor.cancelled)

        self.assertTrue(janitor.cancelled)
        self.assertTrue(database.disposed)

    async def test_the_interval_comes_from_the_settings(self):
        settings, database = configured(scratch_purge_interval_seconds=90)
        app = create_app(settings, database=database)

        await self.run_lifespan(app)

        (janitor,) = RecordingJanitor.instances
        self.assertEqual(janitor.options, {"interval_seconds": 90})

    async def test_it_is_cancelled_before_the_database_is_disposed(self):
        settings, database = configured()
        seen = []
        database.on_dispose = lambda: seen.append(
            [janitor.cancelled for janitor in RecordingJanitor.instances]
        )
        app = create_app(settings, database=database)

        await self.run_lifespan(app)

        self.assertEqual(seen, [[True]])


class DoesNotStartTest(LifespanTestCase):
    async def test_not_without_a_database(self):
        database = FakeDatabase()  # PAW_DATABASE_URL is not set
        app = create_app(make_settings(), database=database)

        await self.run_lifespan(app)

        self.assertEqual(RecordingJanitor.instances, [])
        self.assertTrue(database.disposed)

    async def test_not_when_the_interval_is_zero(self):
        settings, database = configured(scratch_purge_interval_seconds=0)
        app = create_app(settings, database=database)

        await self.run_lifespan(app)

        self.assertEqual(RecordingJanitor.instances, [])

    async def test_not_when_the_interval_is_off_and_there_is_no_database(self):
        app = create_app(
            make_settings(scratch_purge_interval_seconds=0), database=FakeDatabase()
        )

        await self.run_lifespan(app)

        self.assertEqual(RecordingJanitor.instances, [])


class ShutdownTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_janitor_that_ignores_cancellation_cannot_block_shutdown(self):
        class Stubborn:
            def __init__(self, store, **options) -> None:
                pass

            async def run(self) -> None:
                for _ in range(20):  # about two seconds; swallows every cancel
                    try:
                        await asyncio.sleep(0.1)
                    except asyncio.CancelledError:
                        pass

        settings, database = configured(shutdown_timeout_seconds=1)
        app = create_app(settings, database=database)
        with patch("paw_backend.app.ScratchJanitor", Stubborn):
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0.1)
                started = time.monotonic()
        elapsed = time.monotonic() - started

        # It waits for the shutdown budget (1 s), not for the task (2 s) ...
        self.assertGreater(elapsed, 0.8)
        self.assertLess(elapsed, 1.7)
        # ... and still goes on to close the database.
        self.assertTrue(database.disposed)

    async def test_the_janitor_is_cancelled_when_the_application_fails(self):
        # An error inside the ``async with`` body (the application running) must
        # still stop the janitor: it lives in the same ``finally``.
        RecordingJanitor.instances = []
        settings, database = configured()
        app = create_app(settings, database=database)
        with patch("paw_backend.app.ScratchJanitor", RecordingJanitor):
            with self.assertRaises(RuntimeError):
                async with app.router.lifespan_context(app):
                    (janitor,) = RecordingJanitor.instances
                    await asyncio.wait_for(janitor.started.wait(), 30)
                    raise RuntimeError("the server failed")

        self.assertTrue(janitor.cancelled)
        self.assertTrue(database.disposed)


if __name__ == "__main__":
    unittest.main()
