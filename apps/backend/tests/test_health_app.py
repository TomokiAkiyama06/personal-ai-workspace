"""System Health in the application (PAW-066): settings and the lifespan.

``PAW_HEALTH_SAMPLE_INTERVAL_SECONDS`` (0 off, otherwise 10 to 300, default 30),
``PAW_HEALTH_RETENTION_DAYS`` (366 to 3650, default 400) and
``PAW_HEALTH_GPU_PROBE`` (default off). With a database the sampling loop runs in
the lifespan and is stopped and cancelled at shutdown; the connection reaper the
lifespan starts is reported on while it runs. Without a database or with the
sampling off, no loop starts; the GPU probe is used only when asked for and only
without a Compute Scheduler.
"""

import asyncio
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from paw_backend.app import create_app
from paw_backend.compute.probe import NvidiaSmiProbe
from paw_backend.config import Settings
from paw_backend.health.domain import Component, Status
from paw_backend.orchestrator.connection_reaper import ReaperStats

from .support import FakeDatabase, make_settings, paw_environment, wait_until
from .test_orchestrator_project_sweep_app import RecordingLoop
from .test_scratch_janitor_lifespan import configured


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        settings = make_settings()
        self.assertEqual(settings.health_sample_interval_seconds, 30)
        self.assertEqual(settings.health_retention_days, 400)
        self.assertFalse(settings.health_gpu_probe)

    def test_from_the_environment(self):
        with paw_environment(
            PAW_HEALTH_SAMPLE_INTERVAL_SECONDS="10",
            PAW_HEALTH_RETENTION_DAYS="730",
            PAW_HEALTH_GPU_PROBE="true",
        ):
            settings = Settings()
        self.assertEqual(
            (
                settings.health_sample_interval_seconds,
                settings.health_retention_days,
                settings.health_gpu_probe,
            ),
            (10, 730, True),
        )

    def test_bounds(self):
        for value in (0, 10, 300):
            make_settings(health_sample_interval_seconds=value)
        for value in (-1, 1, 9, 301):
            with self.subTest(value), self.assertRaises(ValidationError):
                make_settings(health_sample_interval_seconds=value)
        for value in (365, 3_651):
            with self.subTest(value), self.assertRaises(ValidationError):
                make_settings(health_retention_days=value)


class StaticScheduler:
    def status(self):  # never called here
        raise AssertionError


class ProbeWiringTest(unittest.TestCase):
    def test_the_probe_only_when_asked_and_without_a_scheduler(self):
        app = create_app(make_settings(), database=FakeDatabase())
        self.assertIsNone(app.state.system_health.compute._probe)
        app = create_app(make_settings(health_gpu_probe=True), database=FakeDatabase())
        self.assertIsInstance(app.state.system_health.compute._probe, NvidiaSmiProbe)
        app = create_app(
            make_settings(health_gpu_probe=True),
            database=FakeDatabase(),
            compute=StaticScheduler(),
        )
        self.assertIsNone(app.state.system_health.compute._probe)
        self.assertIsInstance(
            app.state.system_health.compute._scheduler, StaticScheduler
        )


class RecordingReaper(RecordingLoop):
    instances: list["RecordingReaper"] = []

    @property
    def stats(self):
        return ReaperStats(cycles=4)


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

    async def test_the_sampling_loop_runs_with_a_database(self):
        settings, database = configured()
        app = create_app(settings, database=database)
        health = app.state.system_health
        self.assertTrue(health.sampling)
        started = asyncio.Event()
        cancelled = []

        async def run():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        with patch.object(health.monitor, "run", run):
            async with app.router.lifespan_context(app):
                self.assertTrue(await wait_until(started.is_set))
                # The reaper the lifespan started is the one reported on.
                reaper = await health.reaper.check()
                self.assertEqual(reaper.metrics["cycles"], 4)
        self.assertEqual(cancelled, [True])
        self.assertTrue(health.monitor._stopping.is_set())
        self.assertEqual((await health.reaper.check()).status, Status.NOT_CONFIGURED)

    async def test_no_loop_when_off_or_without_a_database(self):
        for settings, database in (
            configured(health_sample_interval_seconds=0),
            (make_settings(), FakeDatabase()),
        ):
            app = create_app(settings, database=database)
            health = app.state.system_health
            self.assertFalse(health.sampling)
            with patch.object(health.monitor, "run", side_effect=AssertionError):
                async with app.router.lifespan_context(app):
                    await asyncio.sleep(0.05)

    def test_every_source_with_a_database(self):
        settings, database = configured()
        app = create_app(settings, database=database)
        self.assertEqual(
            [s.component for s in app.state.system_health.monitor.sources],
            list(Component),
        )
