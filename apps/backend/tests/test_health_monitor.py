"""The System Health monitor (PAW-066): caching, isolation and the sampling cycle.

A source's answer is reused for its ``max_age_seconds``; concurrent callers share
one refresh; a source that raises or hangs is ``check_failed`` (a warning) and
does not hold the report back; the sampling cycle stores the numeric metrics and
the changes, and rolls up every ``ROLLUP_EVERY_CYCLES`` cycles; the loop keeps
going after a failing cycle and ends on ``stop``.
"""

import asyncio
import unittest

from paw_backend.health import limits
from paw_backend.health.domain import Component, ComponentHealth, Severity, Status
from paw_backend.health.monitor import HealthMonitor

from .support import wait_until


class CountingSource:
    def __init__(
        self,
        component: Component,
        *,
        severity: Severity = Severity.INFO,
        max_age: float = 60.0,
        error: Exception | None = None,
        hang: bool = False,
    ) -> None:
        self.component = component
        self.max_age_seconds = max_age
        self.severity = severity
        self.error = error
        self.hang = hang
        self.calls = 0

    async def check(self) -> ComponentHealth:
        self.calls += 1
        await asyncio.sleep(0)
        if self.hang:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return ComponentHealth(
            self.component, self.severity, Status.OK, metrics={"value": self.calls}
        )


class RecordingStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.samples: list[dict] = []
        self.changes: list[tuple] = []
        self.rollups = 0
        self.fail = fail
        self.changes_fail = False

    async def add_samples(self, values, *, interval_seconds):
        if self.fail:
            raise ConnectionError("secret-looking message")
        self.samples.append(dict(values))

    async def record_changes(self, components, *, occurred_at=None):
        if self.changes_fail:
            raise ConnectionError()
        self.changes.append(
            (tuple((h.component, h.severity) for h in components), occurred_at)
        )
        return 0

    async def roll_up(self, *, retention_days):
        self.rollups += 1


class ReportTest(unittest.IsolatedAsyncioTestCase):
    async def test_answers_are_reused_for_their_max_age(self):
        fast = CountingSource(Component.DATABASE, max_age=0.0)
        slow = CountingSource(Component.RECOVERY_BACKUP, max_age=300.0)
        monitor = HealthMonitor([fast, slow])
        first = await monitor.report(max_age_seconds=0)
        second = await monitor.report(max_age_seconds=0)
        self.assertEqual((fast.calls, slow.calls), (2, 1))
        self.assertEqual(
            [h.component for h in second.components],
            [Component.DATABASE, Component.RECOVERY_BACKUP],
        )
        self.assertIsNot(first, second)
        # Within the report's own max age nothing is read again.
        await monitor.report(max_age_seconds=60)
        self.assertEqual(fast.calls, 2)

    async def test_concurrent_callers_share_one_refresh(self):
        source = CountingSource(Component.DATABASE)
        monitor = HealthMonitor([source])
        reports = await asyncio.gather(*(monitor.report() for _ in range(5)))
        self.assertEqual(source.calls, 1)
        self.assertTrue(all(r is reports[0] for r in reports))

    async def test_a_failing_or_hanging_source_is_check_failed(self):
        broken = CountingSource(Component.TASK_QUEUE, error=RuntimeError("password=x"))
        hung = CountingSource(Component.MEMORY_WORKER, hang=True)
        fine = CountingSource(Component.DATABASE)
        monitor = HealthMonitor([fine, broken, hung], check_timeout_seconds=0.05)
        with self.assertLogs("paw_backend.health.monitor", "WARNING") as logs:
            report = await monitor.report()
        by = {h.component: h for h in report.components}
        self.assertEqual(
            (by[Component.TASK_QUEUE].status, by[Component.TASK_QUEUE].reasons),
            (Status.CHECK_FAILED, ("check_error",)),
        )
        self.assertEqual(by[Component.MEMORY_WORKER].reasons, ("check_timeout",))
        self.assertIs(by[Component.TASK_QUEUE].severity, Severity.WARNING)
        self.assertIs(by[Component.DATABASE].status, Status.OK)
        self.assertNotIn("password", "\n".join(logs.output))

    def test_one_source_per_component(self):
        with self.assertRaises(ValueError):
            HealthMonitor(
                [CountingSource(Component.DATABASE), CountingSource(Component.DATABASE)]
            )


class SamplingTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_cycle_stores_numbers_and_changes(self):
        store = RecordingStore()
        source = CountingSource(
            Component.DATABASE, severity=Severity.WARNING, max_age=0
        )
        monitor = HealthMonitor([source], store=store, sample_interval_seconds=30)
        await monitor.run_cycle(0)
        self.assertEqual(
            store.samples, [{"database.severity": 1.0, "database.value": 1.0}]
        )
        self.assertEqual(len(store.changes), 1)
        self.assertEqual(store.rollups, 1)
        await monitor.run_cycle(1)
        self.assertEqual(store.rollups, 1)
        await monitor.run_cycle(limits.ROLLUP_EVERY_CYCLES)
        self.assertEqual(store.rollups, 2)

    async def test_changes_seen_while_postgresql_was_down_are_recorded_later(self):
        # Codex P1 on PR #170: an outage must reach health_events once it is over.
        store = RecordingStore()
        source = CountingSource(Component.DATABASE, max_age=0)
        monitor = HealthMonitor([source], store=store)
        await monitor.run_cycle(1)
        store.changes_fail = True
        source.severity = Severity.CRITICAL
        for cycle in (2, 3):  # the same outage, twice: kept once
            with self.assertRaises(ConnectionError):
                await monitor.run_cycle(cycle)
        store.changes_fail = False
        source.severity = Severity.INFO
        await monitor.run_cycle(4)
        severities = [changes[0][0][1] for changes in store.changes]
        self.assertEqual(severities, [Severity.INFO, Severity.CRITICAL, Severity.INFO])
        # Each with the time its report was taken, in order.
        times = [changes[1] for changes in store.changes]
        self.assertEqual(times, sorted(times))
        self.assertIsNotNone(times[1])

    async def test_the_loop_survives_a_failing_cycle_and_stops(self):
        store = RecordingStore(fail=True)
        source = CountingSource(Component.DATABASE, max_age=0)
        # A short interval (the setting's bounds are checked in ``Settings``).
        monitor = HealthMonitor([source], store=store, sample_interval_seconds=0.01)
        with self.assertLogs("paw_backend.health.monitor", "WARNING") as logs:
            task = asyncio.create_task(monitor.run())
            self.assertTrue(await wait_until(lambda: source.calls >= 1))
            monitor.stop()
            await asyncio.wait_for(task, 2)
        self.assertIn("ConnectionError", "\n".join(logs.output))
        self.assertNotIn("secret-looking", "\n".join(logs.output))

    async def test_no_store_no_cycle(self):
        with self.assertRaises(RuntimeError):
            await HealthMonitor([CountingSource(Component.DATABASE)]).run_cycle(0)
