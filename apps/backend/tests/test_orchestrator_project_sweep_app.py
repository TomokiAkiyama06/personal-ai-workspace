"""The application starts the project task-stop loop and stops it (PAW-034).

``PAW_PROJECT_TASK_STOP_INTERVAL_SECONDS`` (0 off, otherwise 10 to 3600, default
60) and the lifespan: the loop starts with a configured database, is asked to stop
and cancelled before the database is disposed, and does not start without a
database or when it is switched off.
"""

import asyncio
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from paw_backend.app import create_app
from paw_backend.config import Settings
from paw_backend.orchestrator import limits

from .support import FakeDatabase, make_settings, paw_environment, wait_until
from .test_scratch_janitor_lifespan import configured


class IntervalSettingTest(unittest.TestCase):
    def test_the_default_is_a_minute(self):
        self.assertEqual(make_settings().project_task_stop_interval_seconds, 60)
        self.assertEqual(limits.DEFAULT_STOP_INTERVAL_SECONDS, 60)

    def test_the_value_comes_from_the_environment(self):
        with paw_environment(PAW_PROJECT_TASK_STOP_INTERVAL_SECONDS="120"):
            self.assertEqual(Settings().project_task_stop_interval_seconds, 120)

    def test_zero_turns_the_loop_off(self):
        self.assertEqual(
            make_settings(
                project_task_stop_interval_seconds=0
            ).project_task_stop_interval_seconds,
            0,
        )

    def test_the_bounds_are_ten_seconds_and_an_hour(self):
        for value in (10, 11, 60, 3599, 3600):
            with self.subTest(value):
                settings = make_settings(project_task_stop_interval_seconds=value)
                self.assertEqual(settings.project_task_stop_interval_seconds, value)
        for value in (-1, 1, 5, 9, 3601, 10**9):
            with self.subTest(value), self.assertRaises(ValidationError):
                make_settings(project_task_stop_interval_seconds=value)

    def test_the_bounds_are_the_ones_the_loop_accepts(self):
        self.assertEqual(limits.MIN_STOP_INTERVAL_SECONDS, 10)
        self.assertEqual(limits.MAX_STOP_INTERVAL_SECONDS, 3600)

    def test_a_non_integer_is_rejected(self):
        for value in ("often", "1e3", "60.5", "", " "):
            with (
                self.subTest(value),
                paw_environment(PAW_PROJECT_TASK_STOP_INTERVAL_SECONDS=value),
                self.assertRaises(ValidationError),
            ):
                Settings()


class RecordingLoop:
    """Stands in for the loop ``create_app`` builds."""

    instances: list["RecordingLoop"] = []

    def __init__(self, database, **options) -> None:
        self.database = database
        self.options = options
        self.started = asyncio.Event()
        self.stopped = False
        self.cancelled = False
        self.__class__.instances.append(self)

    def stop(self) -> None:
        self.stopped = True

    async def run(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class LifespanTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        RecordingLoop.instances = []
        patcher = patch("paw_backend.app.build_project_stop_loop", RecordingLoop)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def run_lifespan(self, app) -> None:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)


class StartTest(LifespanTestCase):
    async def test_it_starts_with_a_configured_database(self):
        settings, database = configured()
        app = create_app(settings, database=database)

        async with app.router.lifespan_context(app):
            (loop,) = RecordingLoop.instances
            self.assertTrue(await wait_until(loop.started.is_set))
            self.assertIs(loop.database, database)
            self.assertEqual(loop.options, {"interval_seconds": 60})
            self.assertFalse(loop.cancelled)

        self.assertTrue(loop.stopped)  # asked to stop ...
        self.assertTrue(loop.cancelled)  # ... and cancelled
        self.assertTrue(database.disposed)

    async def test_the_interval_comes_from_the_settings(self):
        settings, database = configured(project_task_stop_interval_seconds=90)
        app = create_app(settings, database=database)

        await self.run_lifespan(app)

        (loop,) = RecordingLoop.instances
        self.assertEqual(loop.options, {"interval_seconds": 90})

    async def test_it_is_stopped_before_the_database_is_disposed(self):
        settings, database = configured()
        seen = []
        database.on_dispose = lambda: seen.append(
            [(loop.stopped, loop.cancelled) for loop in RecordingLoop.instances]
        )
        app = create_app(settings, database=database)

        await self.run_lifespan(app)

        self.assertEqual(seen, [[(True, True)]])


class DoesNotStartTest(LifespanTestCase):
    async def test_not_without_a_database(self):
        database = FakeDatabase()  # PAW_DATABASE_URL is not set
        app = create_app(make_settings(), database=database)

        await self.run_lifespan(app)

        self.assertEqual(RecordingLoop.instances, [])
        self.assertTrue(database.disposed)

    async def test_not_when_the_interval_is_zero(self):
        settings, database = configured(project_task_stop_interval_seconds=0)
        app = create_app(settings, database=database)

        await self.run_lifespan(app)

        self.assertEqual(RecordingLoop.instances, [])


if __name__ == "__main__":
    unittest.main()
