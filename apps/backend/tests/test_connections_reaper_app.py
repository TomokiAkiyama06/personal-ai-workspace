"""The application starts the reaper of abandoned connection calls and stops it
(PAW-034, Decision 0016).

``PAW_CONNECTION_REAP_INTERVAL_SECONDS`` (0 off, otherwise 60 to 86400, default
600) and the lifespan: the reaper starts with a configured database next to the
project task-stop loop, is asked to stop and cancelled at shutdown, and does not
start without a database or when it is switched off.
"""

import asyncio
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from paw_backend.app import create_app
from paw_backend.config import Settings
from paw_backend.orchestrator import limits

from .support import FakeDatabase, make_settings, paw_environment, wait_until
from .test_orchestrator_project_sweep_app import RecordingLoop
from .test_scratch_janitor_lifespan import configured


class IntervalSettingTest(unittest.TestCase):
    def test_the_default_is_ten_minutes(self):
        self.assertEqual(make_settings().connection_reap_interval_seconds, 600)
        self.assertEqual(limits.DEFAULT_REAP_INTERVAL_SECONDS, 600)

    def test_the_value_comes_from_the_environment(self):
        with paw_environment(PAW_CONNECTION_REAP_INTERVAL_SECONDS="900"):
            self.assertEqual(Settings().connection_reap_interval_seconds, 900)

    def test_the_bounds_are_the_ones_the_reaper_accepts(self):
        self.assertEqual(
            make_settings(
                connection_reap_interval_seconds=0
            ).connection_reap_interval_seconds,
            0,
        )
        for value in (60, 61, 86_400):
            with self.subTest(value):
                self.assertEqual(
                    make_settings(
                        connection_reap_interval_seconds=value
                    ).connection_reap_interval_seconds,
                    value,
                )
        for value in (-1, 1, 59, 86_401):
            with self.subTest(value), self.assertRaises(ValidationError):
                make_settings(connection_reap_interval_seconds=value)
        self.assertEqual(
            (limits.MIN_REAP_INTERVAL_SECONDS, limits.MAX_REAP_INTERVAL_SECONDS),
            (60, 86_400),
        )


class RecordingReaper(RecordingLoop):
    instances: list["RecordingReaper"] = []


class LifespanTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        RecordingLoop.instances = []
        RecordingReaper.instances = []
        for target, fake in (
            ("paw_backend.app.build_project_stop_loop", RecordingLoop),
            ("paw_backend.app.build_connection_reaper", RecordingReaper),
        ):
            patcher = patch(target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_it_starts_and_stops_with_a_configured_database(self):
        settings, database = configured()
        app = create_app(settings, database=database)

        async with app.router.lifespan_context(app):
            (reaper,) = RecordingReaper.instances
            self.assertTrue(await wait_until(reaper.started.is_set))
            self.assertIs(reaper.database, database)
            self.assertEqual(reaper.options, {"interval_seconds": 600})

        self.assertTrue(reaper.stopped)
        self.assertTrue(reaper.cancelled)

    async def test_it_does_not_start_when_switched_off_or_without_a_database(self):
        settings, database = configured(connection_reap_interval_seconds=0)
        app = create_app(settings, database=database)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)
        app = create_app(make_settings(), database=FakeDatabase())
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)
        self.assertEqual(RecordingReaper.instances, [])


if __name__ == "__main__":
    unittest.main()
