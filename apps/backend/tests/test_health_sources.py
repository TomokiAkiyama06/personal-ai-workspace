"""The System Health sources (PAW-066, Decision 0059), on fakes.

Each source turns what it reads into a severity, a status, reason codes and
numbers. The Compute Scheduler here is the real one over the fake GPU and
model control of ``compute_support`` (nothing starts ``nvidia-smi`` or touches a
model); the database-backed sources get the rows a fake ``fetch_abortable``
returns (their SQL runs on PostgreSQL in ``test_health_postgres.py``).
"""

import re
import unittest
from dataclasses import replace
from datetime import UTC, datetime

from paw_backend.compute.domain import DeploymentState, ExclusiveFailure, Relief
from paw_backend.compute.full_gpu import FullGpuState, FullGpuStatus
from paw_backend.db import DatabaseStatus
from paw_backend.health import limits
from paw_backend.health.domain import (
    Component,
    ComponentHealth,
    HealthReport,
    Severity,
    Status,
    numeric_metrics,
    worst,
)
from paw_backend.health.sources import (
    ComputeSource,
    ConnectionSource,
    DatabaseSource,
    MemoryWorkerSource,
    ReaperSource,
    ScheduledJob,
    ScheduledJobSource,
    TaskQueueSource,
)
from paw_backend.orchestrator.connection_reaper import ReaperStats

from .compute_support import GIB, FakeProbe, build
from .support import FakeDatabase


class RowsDatabase(FakeDatabase):
    """``fetch_abortable`` answers each call with the next prepared rows."""

    def __init__(self, *answers: list[tuple]) -> None:
        super().__init__()
        self.answers = list(answers)
        self.calls: list[tuple[str, dict | None]] = []

    @property
    def configured(self) -> bool:
        return True

    async def fetch_abortable(self, sql, params=None, *, timeout_seconds=None):
        self.calls.append((sql, params))
        return self.answers.pop(0)


class DomainTest(unittest.TestCase):
    def test_severities_are_the_notification_policy_levels_in_order(self):
        self.assertEqual(
            [s.value for s in Severity], ["info", "warning", "error", "critical"]
        )
        self.assertEqual([s.rank for s in Severity], [0, 1, 2, 3])
        self.assertIs(worst([]), Severity.INFO)
        self.assertIs(worst([Severity.ERROR, Severity.WARNING]), Severity.ERROR)

    def test_only_numbers_become_time_series(self):
        health = ComponentHealth(
            Component.COMPUTE,
            Severity.WARNING,
            Status.DEGRADED,
            metrics={"a": 1, "b": 2.5, "flag": True, "code": "x", "none": None},
        )
        self.assertEqual(
            numeric_metrics(health),
            {"compute.severity": 1.0, "compute.a": 1.0, "compute.b": 2.5},
        )

    def test_the_report_is_as_bad_as_its_worst_component(self):
        report = HealthReport(
            datetime.now(UTC),
            (
                ComponentHealth(Component.DATABASE, Severity.INFO, Status.OK),
                ComponentHealth(Component.COMPUTE, Severity.ERROR, Status.FAILING),
            ),
        )
        self.assertIs(report.severity, Severity.ERROR)
        self.assertIs(report.component(Component.COMPUTE).status, Status.FAILING)
        self.assertIsNone(report.component(Component.TASK_QUEUE))


class DatabaseSourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_not_configured(self):
        health = await DatabaseSource(FakeDatabase()).check()
        self.assertEqual(
            (health.severity, health.status), (Severity.INFO, Status.NOT_CONFIGURED)
        )

    async def test_up_and_down(self):
        database = RowsDatabase()
        health = await DatabaseSource(database).check()
        self.assertEqual((health.severity, health.status), (Severity.INFO, Status.OK))
        self.assertEqual(health.metrics["up"], 1)
        database.status = DatabaseStatus.UNAVAILABLE
        health = await DatabaseSource(database).check()
        # "PostgreSQL異常" is CRITICAL in the Notification Policy.
        self.assertEqual(
            (health.severity, health.status, health.reasons),
            (Severity.CRITICAL, Status.UNAVAILABLE, ("database_unavailable",)),
        )
        self.assertEqual(health.metrics["up"], 0)


class ComputeSourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_without_a_scheduler_or_probe_it_is_not_configured(self):
        health = await ComputeSource().check()
        self.assertEqual(
            (health.severity, health.status), (Severity.INFO, Status.NOT_CONFIGURED)
        )

    async def test_a_healthy_scheduler(self):
        scheduler, probe, _, _ = build()
        await scheduler.refresh()
        health = await ComputeSource(scheduler).check()
        self.assertEqual((health.severity, health.status), (Severity.INFO, Status.OK))
        self.assertEqual(health.metrics["vram_total_bytes"], 96 * GIB)
        self.assertEqual(health.metrics["utilization_percent"], 10)
        self.assertEqual(health.metrics["models_on_gpu"], 3)
        roles = {part["role"]: part["state"] for part in health.parts}
        self.assertEqual(roles["main"], "gpu")
        self.assertEqual(roles["memory_worker"], "gpu")
        # Codes and numbers only: nothing a process or a command said.
        for part in health.parts:
            self.assertNotIn("pid", part)

    async def test_a_probe_that_fails_is_an_error(self):
        scheduler, probe, _, _ = build()
        probe.fail = True
        await scheduler.refresh()
        health = await ComputeSource(scheduler).check()
        self.assertIs(health.severity, Severity.ERROR)
        self.assertIn("probe_unavailable", health.reasons)

    async def test_pressure_and_relief_are_warnings_and_a_stuck_main_model_an_error(
        self,
    ):
        scheduler, probe, _, _ = build()
        await scheduler.refresh()
        status = scheduler.status()
        source = ComputeSource(
            StaticScheduler(
                replace(status, relief=Relief.BACKGROUND_STOPPED, vram_waiting=1)
            )
        )
        health = await source.check()
        self.assertIs(health.severity, Severity.WARNING)
        self.assertEqual(health.reasons, ("relief_active", "waiting_for_vram"))
        stuck = replace(status, relief=Relief.MAIN_CHANGE_NEEDED, needs_human=True)
        health = await ComputeSource(StaticScheduler(stuck)).check()
        self.assertIs(health.severity, Severity.ERROR)
        self.assertIn("main_model_change_needed", health.reasons)

    async def test_a_failed_or_unloaded_main_model(self):
        scheduler, _, _, _ = build()
        await scheduler.refresh()
        status = scheduler.status()
        main = status.deployment("main")
        others = tuple(d for d in status.deployments if d.name != "main")
        for state, reason in (
            (DeploymentState.FAILED, "model_failed:main"),
            (DeploymentState.UNLOADED, "main_model_not_resident"),
        ):
            with self.subTest(state):
                changed = replace(
                    status, deployments=(replace(main, state=state), *others)
                )
                health = await ComputeSource(StaticScheduler(changed)).check()
                self.assertIs(health.severity, Severity.ERROR)
                self.assertIn(reason, health.reasons)

    async def test_full_gpu_mode(self):
        scheduler, _, _, _ = build()
        await scheduler.refresh()
        full = FullGpuStatus(
            state=FullGpuState.ON,
            held_tasks=2,
            preempted=False,
            on_seconds=12.5,
            last_failure=None,
            needs_human=False,
        )
        source = ComputeSource(scheduler, StaticScheduler(full))
        health = await source.check()
        self.assertEqual(health.metrics["full_gpu_state"], "on")
        self.assertEqual(health.metrics["full_gpu_held_tasks"], 2)
        stuck = replace(full, state=FullGpuState.RESUMING, needs_human=True)
        source.attach(scheduler, StaticScheduler(stuck))
        health = await source.check()
        self.assertIs(health.severity, Severity.ERROR)
        self.assertIn("full_gpu_resume_stuck", health.reasons)
        failed = replace(
            full, state=FullGpuState.OFF, last_failure=ExclusiveFailure.DRAIN_TIMEOUT
        )
        source.attach(scheduler, StaticScheduler(failed))
        health = await source.check()
        self.assertIs(health.severity, Severity.WARNING)
        self.assertEqual(health.metrics["full_gpu_last_failure"], "drain_timeout")

    async def test_the_probe_alone(self):
        probe = FakeProbe(total=24 * GIB, external=2 * GIB)
        health = await ComputeSource(probe=probe).check()
        self.assertEqual((health.severity, health.status), (Severity.INFO, Status.OK))
        self.assertEqual(health.metrics["vram_used_bytes"], 2 * GIB)
        self.assertEqual(health.parts[0]["processes"], 1)
        self.assertNotIn("pid", health.parts[0])
        probe.external = 24 * GIB
        health = await ComputeSource(probe=probe).check()
        self.assertEqual(health.reasons, ("vram_pressure",))
        probe.fail = True
        health = await ComputeSource(probe=probe).check()
        self.assertEqual(
            (health.severity, health.status), (Severity.ERROR, Status.UNAVAILABLE)
        )


class StaticScheduler:
    def __init__(self, status) -> None:
        self._status = status

    def status(self):
        return self._status


TASK_ROW = (1, 2, 3, 1, 1, 1, 0, 1, 0, 2, 7, 1)


class TaskQueueSourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_counts_and_failures(self):
        health = await TaskQueueSource(RowsDatabase([TASK_ROW])).check()
        self.assertIs(health.severity, Severity.INFO)
        self.assertEqual(health.metrics["queued"], 1)
        self.assertEqual(health.metrics["waiting_resource"], 1)
        self.assertEqual(health.metrics["failed_last_day"], 2)
        one = TASK_ROW[:8] + (1,) + TASK_ROW[9:]
        health = await TaskQueueSource(RowsDatabase([one])).check()
        self.assertIs(health.severity, Severity.WARNING)
        many = TASK_ROW[:8] + (limits.TASK_FAILURES_ERROR,) + TASK_ROW[9:]
        health = await TaskQueueSource(RowsDatabase([many])).check()
        self.assertIs(health.severity, Severity.ERROR)
        self.assertEqual(health.reasons, ("task_failures",))


class MemoryWorkerSourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_pending_and_dead_letters(self):
        database = RowsDatabase([(4, 1, 30.0)], [(0,)])
        health = await MemoryWorkerSource(database).check()
        self.assertIs(health.severity, Severity.INFO)
        self.assertEqual(health.metrics["pending"], 4)
        self.assertEqual(health.metrics["oldest_pending_seconds"], 30.0)
        database = RowsDatabase([(0, 0, None)], [(2,)])
        health = await MemoryWorkerSource(database).check()
        self.assertIs(health.severity, Severity.WARNING)
        self.assertEqual(health.reasons, ("dead_letters",))


class ConnectionSourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_none_configured_is_quiet(self):
        health = await ConnectionSource(RowsDatabase([], [])).check()
        self.assertIs(health.severity, Severity.INFO)
        self.assertEqual(
            [(p["kind"], p["configured"], p["available"]) for p in health.parts],
            [("codex", False, False), ("claude", False, False)],
        )

    async def test_status_decides(self):
        rows = [("codex", "connected", True, 5.0), ("claude", "expired", True, 9.0)]
        health = await ConnectionSource(RowsDatabase(rows, [("codex", 2, 3.0)])).check()
        self.assertIs(health.severity, Severity.ERROR)
        self.assertEqual(health.reasons, ("expired:claude",))
        codex, claude = health.parts
        self.assertEqual((codex["available"], codex["in_flight"]), (True, 2))
        self.assertFalse(claude["available"])
        self.assertEqual(health.metrics["codex_available"], 1)
        rows = [("codex", "unavailable", True, 5.0), ("claude", "expired", False, 9.0)]
        health = await ConnectionSource(RowsDatabase(rows, [])).check()
        # A disabled connection is not watched.
        self.assertIs(health.severity, Severity.WARNING)
        self.assertEqual(health.reasons, ("unavailable:codex",))

    async def test_no_handle_is_read(self):
        database = RowsDatabase([], [])
        await ConnectionSource(database).check()
        for sql, _ in database.calls:
            self.assertNotIn("secret_handle", sql)


class FakeReaper:
    def __init__(self, stats: ReaperStats) -> None:
        self.stats = stats


class ReaperSourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_states(self):
        source = ReaperSource()
        health = await source.check()
        self.assertEqual(health.status, Status.NOT_CONFIGURED)
        now = datetime.now(UTC)
        source.attach(
            FakeReaper(ReaperStats(cycles=3, last_cycle_at=now, last_success_at=now))
        )
        health = await source.check()
        self.assertIs(health.severity, Severity.INFO)
        self.assertEqual(health.metrics["cycles"], 3)
        source.attach(FakeReaper(ReaperStats(cycles=3, last_reaped=2, total_reaped=2)))
        health = await source.check()
        self.assertEqual(
            (health.severity, health.reasons),
            (Severity.WARNING, ("abandoned_calls_settled",)),
        )
        source.attach(
            FakeReaper(
                ReaperStats(
                    consecutive_failures=limits.REAPER_FAILURES_ERROR,
                    last_error="database_error",
                )
            )
        )
        health = await source.check()
        self.assertEqual(
            (health.severity, health.reasons), (Severity.ERROR, ("reaper_failing",))
        )
        self.assertEqual(health.metrics["last_error"], "database_error")


JOB = ScheduledJob(
    Component.RECOVERY_BACKUP,
    resource_kind="recovery_backup_run",
    completed="recovery.backup.completed",
    failed="recovery.backup.failed",
    stale_after=(7_200, 86_400),
)


class ScheduledJobSourceTest(unittest.IsolatedAsyncioTestCase):
    async def check(self, row) -> ComponentHealth:
        return await ScheduledJobSource(RowsDatabase([row]), JOB).check()

    async def test_never_ran(self):
        health = await self.check((None, None, None, None, 0))
        self.assertEqual(
            (health.severity, health.status), (Severity.INFO, Status.NEVER_RAN)
        )

    async def test_recent_success(self):
        health = await self.check(
            ("recovery.backup.completed", 60.0, "files=3", 60.0, 0)
        )
        self.assertEqual((health.severity, health.status), (Severity.INFO, Status.OK))
        # The counts of a completed run are not shown.
        self.assertIsNone(health.metrics["last_failure"])

    async def test_one_failure_is_a_warning_and_several_an_error(self):
        health = await self.check(
            ("recovery.backup.failed", 60.0, "push:rejected", 1_800.0, 1)
        )
        self.assertEqual(
            (health.severity, health.status), (Severity.WARNING, Status.DEGRADED)
        )
        self.assertEqual(health.metrics["last_failure"], "push:rejected")
        health = await self.check(
            (
                "recovery.backup.failed",
                60.0,
                "push:rejected",
                3_600.0,
                limits.JOB_FAILURES_ERROR,
            )
        )
        self.assertEqual(
            (health.severity, health.status), (Severity.ERROR, Status.FAILING)
        )

    async def test_an_old_success_is_stale(self):
        health = await self.check(("recovery.backup.completed", 60.0, "x", 7_300.0, 0))
        self.assertEqual(
            (health.severity, health.status), (Severity.WARNING, Status.STALE)
        )
        health = await self.check(("recovery.backup.completed", 60.0, "x", 90_000.0, 0))
        self.assertEqual(
            (health.severity, health.status), (Severity.ERROR, Status.STALE)
        )

    async def test_never_succeeded(self):
        health = await self.check(("recovery.backup.failed", 60.0, "commit:x", None, 1))
        self.assertIs(health.severity, Severity.WARNING)

    async def test_the_parameters_are_bound(self):
        database = RowsDatabase([(None, None, None, None, 0)])
        await ScheduledJobSource(database, JOB).check()
        ((sql, params),) = database.calls
        self.assertNotIn("recovery_backup_run", sql)
        self.assertEqual(params["kind"], "recovery_backup_run")
        self.assertEqual(params["cap"], limits.MAX_COUNTED_FAILURES)


class MetricNamesTest(unittest.IsolatedAsyncioTestCase):
    """Every number a source reports is a series name the schema accepts."""

    async def test_every_numeric_metric_has_a_valid_name(self):
        pattern = re.compile(limits.METRIC_NAME_PATTERN)
        scheduler, _, _, _ = build()
        await scheduler.refresh()
        full = FullGpuStatus(FullGpuState.ON, 1, False, 1.0, None, False)
        now = datetime.now(UTC)
        reports = [
            await DatabaseSource(RowsDatabase()).check(),
            await ComputeSource(scheduler, StaticScheduler(full)).check(),
            await ComputeSource(probe=FakeProbe()).check(),
            await TaskQueueSource(RowsDatabase([TASK_ROW])).check(),
            await MemoryWorkerSource(RowsDatabase([(1, 0, 1.0)], [(0,)])).check(),
            await ConnectionSource(
                RowsDatabase([("codex", "connected", True, 1.0)], [])
            ).check(),
            await ReaperSource(
                FakeReaper(ReaperStats(last_cycle_at=now, last_success_at=now))
            ).check(),
            await ScheduledJobSource(
                RowsDatabase([("recovery.backup.completed", 1.0, "x", 1.0, 0)]), JOB
            ).check(),
        ]
        for health in reports:
            for name in numeric_metrics(health):
                with self.subTest(name):
                    self.assertRegex(name, pattern)
