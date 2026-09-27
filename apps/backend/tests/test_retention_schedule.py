"""Running ``audit_events`` retention on a schedule (Issue #117, Decision 0031).

No database and no real scheduler: the coverage rule is a pure function, the
runner is driven with a fake ``AuditRetentionService``, the command is run in
this process without a database URL, and the systemd unit files are read as
text. ``tests/test_retention_cli_postgres.py`` runs the same command against a
real PostgreSQL.
"""

import asyncio
import configparser
import contextlib
import io
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path

from paw_backend.authz.retention import (
    MaintenanceAlreadyRunningError,
    MaintenanceStep,
    PartitionStatus,
    PartitionWindow,
    RetentionAction,
    RetentionActor,
    RetentionPolicy,
    default_policy,
    first_uncovered_moment,
    run_scheduled_maintenance,
)
from paw_backend.cli import retention as cli

from .support import paw_environment

BACKEND_DIR = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = BACKEND_DIR / "deploy" / "systemd"
SECRET_URL = "postgresql://paw:hunter2-secret-pw@127.0.0.1:1/paw"

LIVE = PartitionStatus.LIVE
ARCHIVED = PartitionStatus.ARCHIVED


def at(year, month, day=1, hour=0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def window(name, lower, upper, status=LIVE) -> PartitionWindow:
    return PartitionWindow(name, lower, upper, status)


def month(year, month_, status=LIVE) -> PartitionWindow:
    upper = at(year + 1, 1) if month_ == 12 else at(year, month_ + 1)
    return window(
        f"audit_events_p{year:04d}_{month_:02d}", at(year, month_), upper, status
    )


class FirstUncoveredMomentTest(unittest.TestCase):
    NOW = at(2026, 9, 28, 3)

    def test_fully_covered_through_next_month_is_none(self):
        existing = [month(2026, 9), month(2026, 10)]
        self.assertIsNone(first_uncovered_moment(self.NOW, existing, 1))

    def test_a_missing_next_month_is_reported_at_its_start(self):
        existing = [month(2026, 9)]
        self.assertEqual(first_uncovered_moment(self.NOW, existing, 1), at(2026, 10))

    def test_a_missing_current_month_is_reported_at_now(self):
        existing = [month(2026, 10), month(2026, 11)]
        self.assertEqual(first_uncovered_moment(self.NOW, existing, 1), self.NOW)

    def test_zero_months_ahead_only_needs_the_current_month(self):
        existing = [month(2026, 9)]
        self.assertIsNone(first_uncovered_moment(self.NOW, existing, 0))

    def test_the_horizon_is_checked_to_the_end_of_the_last_month(self):
        existing = [month(2026, 9), month(2026, 10), month(2026, 11)]
        self.assertIsNone(first_uncovered_moment(self.NOW, existing, 2))
        self.assertEqual(first_uncovered_moment(self.NOW, existing, 3), at(2026, 12))

    def test_a_gap_in_the_middle_is_found(self):
        existing = [month(2026, 9), month(2026, 11)]
        self.assertEqual(first_uncovered_moment(self.NOW, existing, 2), at(2026, 10))

    def test_an_archived_partition_does_not_cover(self):
        existing = [month(2026, 9), month(2026, 10, ARCHIVED)]
        self.assertEqual(first_uncovered_moment(self.NOW, existing, 1), at(2026, 10))

    def test_the_legacy_partition_and_a_mid_month_first_partition_cover(self):
        # Migration 0086: legacy up to the cutover, then the cutover to month end.
        cutover = at(2026, 9, 27, 12)
        existing = [
            window("audit_events_p_legacy", None, cutover),
            window("audit_events_p2026_09", cutover, at(2026, 10)),
            month(2026, 10),
        ]
        self.assertIsNone(first_uncovered_moment(at(2026, 9, 20), existing, 1))

    def test_the_order_of_existing_does_not_matter(self):
        existing = [month(2026, 10), month(2026, 9)]
        self.assertIsNone(first_uncovered_moment(self.NOW, existing, 1))

    def test_negative_months_ahead_is_rejected(self):
        with self.assertRaises(ValueError):
            first_uncovered_moment(self.NOW, [], -1)

    def test_a_bool_is_not_a_month_count(self):
        with self.assertRaises(TypeError):
            first_uncovered_moment(self.NOW, [], True)

    def test_a_naive_now_is_rejected(self):
        with self.assertRaises(ValueError):
            first_uncovered_moment(datetime(2026, 9, 28), [], 1)


class Boom(RuntimeError):
    pass


class FakeService:
    """Records what the runner asks of ``AuditRetentionService``."""

    def __init__(self, *, now=None, windows=None, fail_at=None):
        self.now = now or at(2026, 9, 28, 3)
        self.windows = list(windows or [month(2026, 9)])
        self.fail_at = fail_at
        self.fail_audit = False
        self.locked = False
        self.busy = False
        self.calls: list[str] = []
        self.outcomes: list[tuple[RetentionAction, str, RetentionActor | None]] = []
        self.policies: list[RetentionPolicy] = []

    def _now(self) -> datetime:
        return self.now

    @contextlib.asynccontextmanager
    async def maintenance_lock(self):
        if self.busy:
            raise MaintenanceAlreadyRunningError()
        self.locked = True
        self.calls.append("lock")
        try:
            yield
        finally:
            self.locked = False
            self.calls.append("unlock")

    async def _step(self, step: MaintenanceStep, policy, result):
        assert self.locked, "every step runs under the maintenance lock"
        self.calls.append(step.value)
        self.policies.append(policy)
        if self.fail_at is step:
            raise Boom("details that must never be recorded")
        return result

    async def ensure_partitions(self, policy=None, *, actor=None):
        created = [month(2026, 10), month(2026, 11), month(2026, 12)]
        result = await self._step(MaintenanceStep.ENSURE_PARTITIONS, policy, created)
        self.windows.extend(created)
        return result

    async def archive_due_partitions(self, policy=None, *, actor=None):
        return await self._step(MaintenanceStep.ARCHIVE, policy, [])

    async def purge_due_partitions(self, policy=None, *, actor=None):
        return await self._step(MaintenanceStep.PURGE, policy, [])

    async def existing_partitions(self, session=None):
        self.calls.append("existing_partitions")
        return list(self.windows)

    async def record_maintenance_outcome(self, action, reason, *, actor=None):
        self.calls.append("record")
        if self.fail_audit:
            raise Boom("the audit trail is unavailable")
        self.outcomes.append((action, reason, actor))


class RunScheduledMaintenanceTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_successful_run_runs_every_step_in_order_under_the_lock(self):
        service = FakeService()
        result = await run_scheduled_maintenance(service)

        self.assertTrue(result.ok)
        self.assertIsNone(result.failed_step)
        self.assertEqual(
            service.calls,
            [
                "lock",
                "ensure_partitions",
                "archive_due_partitions",
                "purge_due_partitions",
                "existing_partitions",
                "record",
                "unlock",
            ],
        )
        self.assertEqual(len(result.report.created), 3)

    async def test_a_successful_run_is_audited_with_its_counts(self):
        service = FakeService()
        await run_scheduled_maintenance(service)
        self.assertEqual(
            service.outcomes,
            [
                (
                    RetentionAction.MAINTENANCE_COMPLETED,
                    "created=3 archived=0 purged=0",
                    None,
                )
            ],
        )

    async def test_the_default_policy_is_decision_0027s(self):
        service = FakeService()
        await run_scheduled_maintenance(service)
        self.assertEqual(set(service.policies), {default_policy()})

    async def test_the_actor_is_passed_to_the_outcome_row(self):
        service = FakeService()
        actor = RetentionActor(user_id=uuid.uuid4(), system_role="admin")
        await run_scheduled_maintenance(service, actor=actor)
        self.assertIs(service.outcomes[0][2], actor)

    async def test_a_failing_step_stops_the_run_and_is_audited_by_type_only(self):
        for step in (
            MaintenanceStep.ENSURE_PARTITIONS,
            MaintenanceStep.ARCHIVE,
            MaintenanceStep.PURGE,
        ):
            with self.subTest(step=step):
                service = FakeService(fail_at=step)
                result = await run_scheduled_maintenance(service)

                self.assertFalse(result.ok)
                self.assertIs(result.failed_step, step)
                self.assertEqual(result.error_type, "Boom")
                self.assertTrue(result.audited)
                self.assertEqual(
                    service.outcomes,
                    [(RetentionAction.MAINTENANCE_FAILED, f"{step.value}:Boom", None)],
                )
                later = service.calls[service.calls.index(step.value) + 1 :]
                self.assertEqual(later, ["record", "unlock"])

    async def test_what_was_done_before_the_failure_is_still_reported(self):
        service = FakeService(fail_at=MaintenanceStep.ARCHIVE)
        result = await run_scheduled_maintenance(service)
        self.assertEqual(len(result.report.created), 3)
        self.assertEqual(result.report.archived, ())

    async def test_a_coverage_gap_after_the_steps_is_a_failure(self):
        service = FakeService()
        result = await run_scheduled_maintenance(service, required_months_ahead=6)

        self.assertFalse(result.ok)
        self.assertIs(result.failed_step, MaintenanceStep.VERIFY_COVERAGE)
        self.assertEqual(result.error_type, "coverage_gap")
        self.assertEqual(result.uncovered_from, at(2027, 1))
        self.assertEqual(
            service.outcomes,
            [
                (
                    RetentionAction.MAINTENANCE_FAILED,
                    "verify_coverage:coverage_gap",
                    None,
                )
            ],
        )

    async def test_a_run_whose_outcome_cannot_be_audited_is_not_ok(self):
        service = FakeService()
        service.fail_audit = True
        result = await run_scheduled_maintenance(service)
        self.assertFalse(result.ok)
        self.assertFalse(result.audited)
        self.assertIsNone(result.failed_step)

    async def test_a_failure_whose_audit_also_fails_says_so(self):
        service = FakeService(fail_at=MaintenanceStep.ENSURE_PARTITIONS)
        service.fail_audit = True
        result = await run_scheduled_maintenance(service)
        self.assertFalse(result.ok)
        self.assertIs(result.failed_step, MaintenanceStep.ENSURE_PARTITIONS)
        self.assertFalse(result.audited)

    async def test_a_run_already_in_progress_is_refused_before_any_step(self):
        service = FakeService()
        service.busy = True
        with self.assertRaises(MaintenanceAlreadyRunningError):
            await run_scheduled_maintenance(service)
        self.assertEqual(service.calls, [])

    async def test_a_cancelled_run_is_audited_as_failed_and_stays_cancelled(self):
        # SIGTERM from systemd's TimeoutStartSec cancels the run (the command
        # turns the signal into a cancellation): the step it was waiting in is
        # recorded as maintenance_failed before the cancellation goes on.
        service = FakeService()
        waiting = asyncio.Event()

        async def hang(policy=None, *, actor=None):
            service.calls.append("archive_due_partitions")
            waiting.set()
            await asyncio.Event().wait()

        service.archive_due_partitions = hang
        task = asyncio.create_task(run_scheduled_maintenance(service))
        await waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(
            service.outcomes,
            [
                (
                    RetentionAction.MAINTENANCE_FAILED,
                    "archive_due_partitions:CancelledError",
                    None,
                )
            ],
        )
        self.assertEqual(service.calls[-2:], ["record", "unlock"])

    async def test_a_cancelled_run_whose_audit_fails_is_still_cancelled(self):
        service = FakeService()
        service.fail_audit = True
        waiting = asyncio.Event()

        async def hang(policy=None, *, actor=None):
            waiting.set()
            await asyncio.Event().wait()

        service.ensure_partitions = hang
        task = asyncio.create_task(run_scheduled_maintenance(service))
        await waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(service.calls[-2:], ["record", "unlock"])

    async def test_a_long_reason_is_cut_to_the_column_width(self):
        class AnErrorWhoseNameIsMuchLongerThanAnyoneWouldEverReasonablyChoose(
            Exception
        ):
            pass

        service = FakeService()

        async def fail(policy=None, *, actor=None):
            raise AnErrorWhoseNameIsMuchLongerThanAnyoneWouldEverReasonablyChoose()

        service.archive_due_partitions = fail
        await run_scheduled_maintenance(service)
        reason = service.outcomes[0][1]
        self.assertLessEqual(len(reason), 64)
        self.assertTrue(reason.startswith("archive_due_partitions:AnError"))


def run(argv: list[str], **environment: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with paw_environment(**environment):
        code = cli.main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class CommandWithoutDatabaseTest(unittest.TestCase):
    def test_the_exit_codes(self):
        self.assertEqual(
            (
                cli.EXIT_OK,
                cli.EXIT_REFUSED,
                cli.EXIT_ENVIRONMENT,
                cli.EXIT_MAINTENANCE_FAILED,
            ),
            (0, 1, 2, 3),
        )

    def test_the_scheduled_ddl_waits_for_locks_only_a_few_seconds(self):
        # A DDL step queued behind a long reader would block every INSERT into
        # audit_events behind it; the command bounds that wait (Decision 0031).
        self.assertGreaterEqual(cli.DDL_LOCK_TIMEOUT_MS, 1000)
        self.assertLessEqual(cli.DDL_LOCK_TIMEOUT_MS, 30000)

    def test_help_lists_both_commands(self):
        code, out, _ = run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("audit-retention-run", out)
        self.assertIn("audit-retention-check", out)

    def test_without_the_migration_url_nothing_runs(self):
        for command in ("audit-retention-run", "audit-retention-check"):
            with self.subTest(command=command):
                code, out, err = run([command])
                self.assertEqual(code, cli.EXIT_ENVIRONMENT)
                self.assertIn("PAW_MIGRATION_DATABASE_URL", err)
                self.assertEqual(out, "")

    def test_the_application_url_alone_is_not_used(self):
        # The application's role cannot run DDL; there is no silent fallback.
        code, _, err = run(["audit-retention-run"], PAW_DATABASE_URL=SECRET_URL)
        self.assertEqual(code, cli.EXIT_ENVIRONMENT)
        self.assertIn("PAW_MIGRATION_DATABASE_URL", err)
        self.assertNotIn("hunter2", err)

    def test_an_unreachable_database_is_an_environment_error_without_secrets(self):
        for command in ("audit-retention-run", "audit-retention-check"):
            with self.subTest(command=command):
                code, out, err = run(
                    [command],
                    PAW_MIGRATION_DATABASE_URL=SECRET_URL,
                    PAW_DATABASE_TIMEOUT_SECONDS="1",
                )
                self.assertEqual(code, cli.EXIT_ENVIRONMENT)
                self.assertNotIn("hunter2", out + err)
                self.assertIn("Database error", err)

    def test_a_purge_shorter_than_the_archive_period_is_refused(self):
        code, _, err = run(
            ["audit-retention-run", "--purge-after-days", "10"],
            PAW_MIGRATION_DATABASE_URL=SECRET_URL,
        )
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("purge", err)

    def test_a_usage_error_does_not_echo_its_arguments(self):
        code, _, err = run(["audit-retention-run", "--database", SECRET_URL])
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertNotIn("hunter2", err)


class DispatchTest(unittest.TestCase):
    def test_the_package_entry_point_routes_retention_commands(self):
        from paw_backend.cli import dispatch

        self.assertIs(dispatch.command_module(["audit-retention-run"]), cli)
        self.assertIs(dispatch.command_module(["audit-retention-check"]), cli)

    def test_every_other_command_still_goes_to_the_owner_commands(self):
        from paw_backend.cli import dispatch, owner

        for argv in (["owner-setup"], ["owner-recover"], ["--help"], []):
            with self.subTest(argv=argv):
                self.assertIs(dispatch.command_module(argv), owner)

    def test_the_owner_help_points_at_the_retention_commands(self):
        from paw_backend.cli import owner

        out, err = io.StringIO(), io.StringIO()
        with paw_environment():
            owner.main(["--help"], stdout=out, stderr=err)
        self.assertIn("audit-retention-run", out.getvalue())


def unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read_string((SYSTEMD_DIR / name).read_text(encoding="utf-8"))
    return parser


class SystemdUnitTest(unittest.TestCase):
    def test_the_service_runs_the_command_once_as_a_oneshot(self):
        service = unit("paw-audit-retention.service")
        self.assertEqual(service["Service"]["Type"], "oneshot")
        self.assertIn(
            "-m paw_backend.cli audit-retention-run", service["Service"]["ExecStart"]
        )

    def test_the_service_reads_the_migration_url_from_a_root_only_file(self):
        service = unit("paw-audit-retention.service")
        self.assertIn("EnvironmentFile", service["Service"])
        self.assertNotIn(
            "PAW_MIGRATION_DATABASE_URL=", service["Service"].get("Environment", "")
        )

    def test_the_service_does_not_run_as_the_backends_user(self):
        # The job holds the table owner's credential: it must not run as the
        # OS user the web backend runs as (a web-side compromise could read
        # /proc/<pid>/environ of a same-uid process or rewrite its code).
        service = unit("paw-audit-retention.service")
        self.assertEqual(service["Service"]["User"], "paw-maint")
        text = (SYSTEMD_DIR / "paw-audit-retention.service").read_text("utf-8")
        self.assertIn("never the user the backend runs as", text)
        self.assertIn("root-owned", text)

    def test_a_terminated_run_has_time_to_record_its_failure(self):
        service = unit("paw-audit-retention.service")
        self.assertEqual(service["Service"]["KillSignal"], "SIGTERM")
        self.assertIn("TimeoutStopSec", service["Service"])

    def test_a_failed_run_triggers_the_failure_unit(self):
        service = unit("paw-audit-retention.service")
        self.assertEqual(
            service["Unit"]["OnFailure"], "paw-audit-retention-failure.service"
        )
        self.assertTrue((SYSTEMD_DIR / "paw-audit-retention-failure.service").exists())

    def test_the_timer_runs_daily_and_catches_up_after_downtime(self):
        timer = unit("paw-audit-retention.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "daily")
        self.assertEqual(timer["Timer"]["Persistent"], "true")
        self.assertEqual(timer["Timer"]["Unit"], "paw-audit-retention.service")
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")


if __name__ == "__main__":
    unittest.main()
