"""The maintenance loop (issue #125): the freshness jobs and the task-end sweep.

The cycle and the loop with fakes and a manual clock (the real jobs run in
``test_orchestrator_task_end``); the ``PAW_FRESHNESS_JOB_INTERVAL_SECONDS``
setting; and the application's lifespan, which starts the loop over its own task
execution with a configured database and stops it at shutdown.
"""

import asyncio
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from paw_backend.app import create_app
from paw_backend.config import Settings
from paw_backend.orchestrator import limits
from paw_backend.orchestrator.errors import InvalidOrchestratorArgumentError
from paw_backend.orchestrator.freshness_loop import (
    FIRST_CYCLE_DELAY_SECONDS,
    FreshnessJobLoop,
    MaintenanceReport,
    build_freshness_loop,
)

from .orchestrator_support import ManualClock
from .support import FakeDatabase, make_settings, paw_environment, wait_until
from .test_scratch_janitor_lifespan import configured


class FakeFreshness:
    def __init__(self, due=(), expired=(), batch=3, fail=()) -> None:
        self.due = list(due)
        self.expired = list(expired)
        self.batch = batch
        self.fail = set(fail)
        self.calls: list[str] = []

    async def _next(self, name, queue):
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError("db down: secret detail")
        return queue.pop(0) if queue else 0

    async def mark_revalidation_due(self):
        return await self._next("due", self.due)

    async def expire_due(self):
        return await self._next("expire", self.expired)


class FakeTaskEnd:
    def __init__(self, finished=0, fail=False, batches=None) -> None:
        """Each sweep finishes ``finished`` tasks, or the next of ``batches``
        (then none)."""
        self.finished = finished
        self.fail = fail
        self.batches = None if batches is None else list(batches)
        self.calls: list[str] = []

    async def sweep(self):
        self.calls.append("sweep")
        if self.fail:
            raise RuntimeError("db down: secret detail")
        if self.batches is not None:
            return tuple(range(self.batches.pop(0) if self.batches else 0))
        return tuple(range(self.finished))


class CycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_cycle_sweeps_then_marks_then_expires(self):
        calls = []
        freshness = FakeFreshness(due=[2], expired=[1])
        task_end = FakeTaskEnd(finished=4)
        freshness.calls = task_end.calls = calls

        report = await FreshnessJobLoop(freshness, task_end).run_cycle()

        self.assertEqual(report, MaintenanceReport(4, 2, 1))
        self.assertEqual(calls, ["sweep", "due", "expire"])

    async def test_a_job_is_repeated_while_it_changes_a_full_batch(self):
        freshness = FakeFreshness(due=[3, 3, 1, 3], expired=[3, 0], batch=3)

        report = await FreshnessJobLoop(freshness, FakeTaskEnd()).run_cycle()

        self.assertEqual((report.marked_stale, report.expired), (7, 3))
        self.assertEqual(freshness.calls.count("due"), 3)
        self.assertEqual(freshness.calls.count("expire"), 2)

    async def test_the_task_end_sweep_is_repeated_while_it_takes_a_full_batch(self):
        full = limits.MAX_TASK_END_SWEEP
        task_end = FakeTaskEnd(batches=[full, full, 5, full])

        report = await FreshnessJobLoop(FakeFreshness(), task_end).run_cycle()

        self.assertEqual(report.finished_tasks, 2 * full + 5)
        self.assertEqual(task_end.calls.count("sweep"), 3)

    async def test_the_task_end_repetition_is_bounded(self):
        full = limits.MAX_TASK_END_SWEEP
        task_end = FakeTaskEnd(finished=full)

        report = await FreshnessJobLoop(FakeFreshness(), task_end).run_cycle()

        self.assertEqual(report.finished_tasks, full * limits.MAX_FRESHNESS_ROUNDS)

    async def test_the_repetition_is_bounded(self):
        freshness = FakeFreshness(due=[1] * 100, batch=1)

        report = await FreshnessJobLoop(freshness, FakeTaskEnd()).run_cycle()

        self.assertEqual(report.marked_stale, limits.MAX_FRESHNESS_ROUNDS)

    async def test_a_failing_step_does_not_stop_the_others(self):
        freshness = FakeFreshness(expired=[2], fail={"due"})
        task_end = FakeTaskEnd(fail=True)
        loop = FreshnessJobLoop(freshness, task_end)

        with self.assertLogs(
            "paw_backend.orchestrator.freshness_loop", "WARNING"
        ) as logs:
            report = await loop.run_cycle()

        self.assertEqual(
            report, MaintenanceReport(0, 0, 2, ("task_end", "revalidation_due"))
        )
        self.assertNotIn("secret detail", "\n".join(logs.output))


class LoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_loop_waits_cycles_and_stops(self):
        clock = ManualClock()
        freshness = FakeFreshness()
        loop = FreshnessJobLoop(
            freshness, FakeTaskEnd(), interval_seconds=600, clock=clock
        )
        running = asyncio.create_task(loop.run())
        self.assertTrue(await wait_until(lambda: clock.waiting_for(60) == 1))
        self.assertEqual(freshness.calls, [])

        await clock.advance(FIRST_CYCLE_DELAY_SECONDS)
        self.assertTrue(await wait_until(lambda: clock.waiting_for(600) == 1))
        self.assertEqual(freshness.calls, ["due", "expire"])

        await clock.advance(600)
        self.assertTrue(await wait_until(lambda: freshness.calls.count("due") == 2))
        loop.stop()
        await asyncio.wait_for(running, 10)

    async def test_a_short_interval_is_also_the_first_wait(self):
        clock = ManualClock()
        loop = FreshnessJobLoop(
            FakeFreshness(), FakeTaskEnd(), interval_seconds=60, clock=clock
        )
        running = asyncio.create_task(loop.run())
        self.assertTrue(await wait_until(lambda: clock.waiting_for(60) == 1))
        loop.stop()
        await asyncio.wait_for(running, 10)

    async def test_a_cancellation_ends_it(self):
        loop = FreshnessJobLoop(FakeFreshness(), FakeTaskEnd(), clock=ManualClock())
        running = asyncio.create_task(loop.run())
        await asyncio.sleep(0)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running


class ArgumentTest(unittest.TestCase):
    def test_the_loop_refuses_what_it_cannot_use(self):
        with self.assertRaises(TypeError):
            FreshnessJobLoop(object(), FakeTaskEnd())
        with self.assertRaises(TypeError):
            FreshnessJobLoop(FakeFreshness(), object())
        for bad in (0, True, "a"):
            with self.subTest(batch=bad), self.assertRaises(TypeError):
                FreshnessJobLoop(FakeFreshness(batch=bad), FakeTaskEnd())
        for bad in (0, 59, 86_401, True, "600", float("nan")):
            with (
                self.subTest(interval=bad),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                FreshnessJobLoop(FakeFreshness(), FakeTaskEnd(), interval_seconds=bad)


class IntervalSettingTest(unittest.TestCase):
    def test_the_default_is_an_hour(self):
        self.assertEqual(make_settings().freshness_job_interval_seconds, 3_600)
        self.assertEqual(limits.DEFAULT_FRESHNESS_INTERVAL_SECONDS, 3_600)

    def test_the_value_comes_from_the_environment(self):
        with paw_environment(PAW_FRESHNESS_JOB_INTERVAL_SECONDS="900"):
            self.assertEqual(Settings().freshness_job_interval_seconds, 900)

    def test_the_bounds_are_the_ones_the_loop_accepts(self):
        for value in (0, 60, 61, 86_400):
            with self.subTest(value):
                self.assertEqual(
                    make_settings(
                        freshness_job_interval_seconds=value
                    ).freshness_job_interval_seconds,
                    value,
                )
        for value in (-1, 1, 59, 86_401):
            with self.subTest(value), self.assertRaises(ValidationError):
                make_settings(freshness_job_interval_seconds=value)
        self.assertEqual(
            (
                limits.MIN_FRESHNESS_INTERVAL_SECONDS,
                limits.MAX_FRESHNESS_INTERVAL_SECONDS,
            ),
            (60, 86_400),
        )


class RecordingMaintenance:
    """Stands in for the loop ``create_app`` builds."""

    instances: list["RecordingMaintenance"] = []

    def __init__(self, execution, **options) -> None:
        self.execution = execution
        self.options = options
        self.started = asyncio.Event()
        self.stopped = False
        self.cancelled = False
        self.instances.append(self)

    def stop(self) -> None:
        self.stopped = True

    async def run(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class LifespanTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        RecordingMaintenance.instances = []
        patcher = patch("paw_backend.app.build_freshness_loop", RecordingMaintenance)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_it_runs_over_the_apps_task_execution(self):
        settings, database = configured(freshness_job_interval_seconds=120)
        app = create_app(settings, database=database)

        async with app.router.lifespan_context(app):
            (loop,) = RecordingMaintenance.instances
            self.assertTrue(await wait_until(loop.started.is_set))
            self.assertIs(loop.execution, app.state.task_execution)
            self.assertEqual(loop.options, {"interval_seconds": 120})

        self.assertTrue(loop.stopped)
        self.assertTrue(loop.cancelled)
        self.assertTrue(database.disposed)

    async def test_not_when_switched_off_or_without_a_database(self):
        settings, database = configured(freshness_job_interval_seconds=0)
        app = create_app(settings, database=database)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)
        app = create_app(make_settings(), database=FakeDatabase())
        self.assertIsNone(app.state.task_execution)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)
        self.assertEqual(RecordingMaintenance.instances, [])

    def test_the_real_builder_uses_the_executions_jobs(self):
        settings, database = configured()
        execution = create_app(settings, database=database).state.task_execution
        loop = build_freshness_loop(execution, interval_seconds=300)
        self.assertIs(loop._freshness, execution.freshness)
        self.assertIs(loop._task_end, execution.task_end)
        self.assertEqual(loop._interval, 300)


if __name__ == "__main__":
    unittest.main()
