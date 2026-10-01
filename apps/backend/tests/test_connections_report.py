"""The usage report of the Usage screen (issue #187, Decision 0069, Proposed).

The periods are pure (``report_window``); the sums and the authorization run on a
real PostgreSQL with the clock seam of the store (Asia/Tokyo calendar days).
"""

import unittest
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from paw_backend.authz import InMemoryAuditSink
from paw_backend.connections import (
    ConnectionKind,
    ConnectionPermissionDeniedError,
    InvalidConnectionInputError,
    UsagePurpose,
)
from paw_backend.connections.report import (
    DailyTasks,
    KindTotal,
    PurposeTotal,
    UsageRange,
    report_window,
)

from .connections_support import (
    FakeClock,
    PostgresConnectionTestCase,
    requires_postgres,
)

TOKYO = ZoneInfo("Asia/Tokyo")
# 12:00 on 17 September 2026 in Tokyo.
NOW = datetime(2026, 9, 17, 3, 0, tzinfo=UTC)


def tokyo(*args: int) -> datetime:
    """A wall-clock time in Tokyo as a UTC instant."""
    return datetime(*args, tzinfo=TOKYO).astimezone(UTC)


class ReportWindowTest(unittest.TestCase):
    def test_last_14_days_end_today_in_the_zone(self):
        window = report_window(UsageRange.LAST_14, NOW, TOKYO)

        self.assertEqual(len(window.days), 14)
        self.assertEqual(window.days[0], date(2026, 9, 4))
        self.assertEqual(window.days[-1], date(2026, 9, 17))
        self.assertEqual(window.start, tokyo(2026, 9, 4))
        self.assertEqual(window.end, tokyo(2026, 9, 18))
        self.assertEqual(len(window.starts), 15)
        # The 14 days just before.
        self.assertEqual(window.previous_start, tokyo(2026, 8, 21))
        self.assertEqual(window.previous_end, tokyo(2026, 9, 4))

    def test_the_day_is_the_zones_not_utcs(self):
        # 23:30 UTC on the 16th is already the 17th in Tokyo.
        late = datetime(2026, 9, 16, 23, 30, tzinfo=UTC)

        window = report_window(UsageRange.LAST_14, late, TOKYO)

        self.assertEqual(window.days[-1], date(2026, 9, 17))

    def test_last_30_days(self):
        window = report_window(UsageRange.LAST_30, NOW, TOKYO)

        self.assertEqual(len(window.days), 30)
        self.assertEqual(window.days[0], date(2026, 8, 19))
        self.assertEqual(window.previous_start, tokyo(2026, 7, 20))
        self.assertEqual(window.previous_end, tokyo(2026, 8, 19))

    def test_month_is_the_calendar_month_up_to_today(self):
        window = report_window(UsageRange.MONTH, NOW, TOKYO)

        self.assertEqual(window.days[0], date(2026, 9, 1))
        self.assertEqual(window.days[-1], date(2026, 9, 17))
        self.assertEqual(window.start, tokyo(2026, 9, 1))
        self.assertEqual(window.end, tokyo(2026, 9, 18))
        # The previous month up to the same day.
        self.assertEqual(window.previous_start, tokyo(2026, 8, 1))
        self.assertEqual(window.previous_end, tokyo(2026, 8, 18))

    def test_the_previous_month_is_cut_at_its_end(self):
        window = report_window(UsageRange.MONTH, tokyo(2026, 3, 31, 9), TOKYO)

        self.assertEqual(window.previous_start, tokyo(2026, 2, 1))
        self.assertEqual(window.previous_end, tokyo(2026, 3, 1))

    def test_january_compares_with_december(self):
        window = report_window(UsageRange.MONTH, tokyo(2027, 1, 5, 9), TOKYO)

        self.assertEqual(window.previous_start, tokyo(2026, 12, 1))
        self.assertEqual(window.previous_end, tokyo(2026, 12, 6))

    def test_a_day_with_a_clock_change_starts_at_its_midnight(self):
        new_york = ZoneInfo("America/New_York")
        # 2026-11-01 is 25 hours long in New York.
        now = datetime(2026, 11, 2, 12, tzinfo=new_york)

        window = report_window(UsageRange.LAST_14, now, new_york)

        self.assertEqual(window.starts[-2] - window.starts[-3], timedelta(hours=25))
        self.assertEqual(
            window.starts[-1], datetime(2026, 11, 3, tzinfo=new_york).astimezone(UTC)
        )

    def test_a_naive_instant_is_refused(self):
        with self.assertRaises(ValueError):
            report_window(UsageRange.LAST_14, datetime(2026, 9, 17), TOKYO)


@requires_postgres
class UsageReportTest(PostgresConnectionTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.clock = FakeClock(NOW)
        self.service = self.new_service(clock=self.clock, allow_explicit_clock=True)
        self.other = self.seed_user()
        user = self.user
        self.t1 = self.seed_task(user)
        self.t2 = self.seed_task(user)
        self.t3 = self.seed_task(user)
        self.t4 = self.seed_task(user)
        self.t5 = self.seed_task(self.other)
        # The 17th in Tokyo (twice: the second call at 01:30 is the 16th in UTC).
        self.seed_usage(
            user, self.t1, started_at=tokyo(2026, 9, 17, 10), tokens=100,
            output_tokens=50,
        )  # fmt: skip
        self.seed_usage(
            user, self.t1, started_at=tokyo(2026, 9, 17, 1, 30), tokens=None,
            status="failed",
        )  # fmt: skip
        # The 16th, Claude, a review.
        self.seed_usage(
            user, self.t2, kind=ConnectionKind.CLAUDE,
            started_at=tokyo(2026, 9, 16, 23), tokens=7, purpose="review",
        )  # fmt: skip
        # The first day of the period, and the last minute before it.
        self.seed_usage(user, self.t3, started_at=tokyo(2026, 9, 4, 0, 30), tokens=3)
        self.seed_usage(
            user, self.t4, started_at=tokyo(2026, 9, 3, 23, 59), tokens=1000
        )
        # Still running (no tokens yet): a task all the same.
        self.seed_usage(
            user, self.t3, started_at=tokyo(2026, 9, 17, 11), status="in_flight"
        )
        # Somebody else's.
        self.seed_usage(
            self.other, self.t5, started_at=tokyo(2026, 9, 10, 12), tokens=20
        )

    async def test_ones_own_report(self):
        report = await self.service.usage_report(
            self.principal(self.user), UsageRange.LAST_14, self.user
        )

        self.assertEqual(report.range, UsageRange.LAST_14)
        self.assertEqual(len(report.days), 14)
        self.assertEqual((report.window_start, report.window_end), (
            tokyo(2026, 9, 4), tokyo(2026, 9, 18),
        ))  # fmt: skip
        self.assertEqual(report.tasks, 3)
        self.assertEqual(report.previous_tasks, 1)
        self.assertEqual(report.tokens, 160)
        self.assertEqual(
            report.daily,
            (
                DailyTasks(date(2026, 9, 4), ConnectionKind.CODEX, 1),
                DailyTasks(date(2026, 9, 16), ConnectionKind.CLAUDE, 1),
                DailyTasks(date(2026, 9, 17), ConnectionKind.CODEX, 2),
            ),
        )
        self.assertEqual(
            report.kinds,
            (
                KindTotal(ConnectionKind.CODEX, 2, 153),
                KindTotal(ConnectionKind.CLAUDE, 1, 7),
            ),
        )
        self.assertEqual(
            report.purposes,
            (
                PurposeTotal(UsagePurpose.CODING, 2, 153),
                PurposeTotal(UsagePurpose.REVIEW, 1, 7),
            ),
        )
        self.assertEqual(report.quotas, ())
        self.assertIsNone(report.users)

    async def test_the_month(self):
        report = await self.service.usage_report(
            self.principal(self.user), UsageRange.MONTH, self.user
        )

        self.assertEqual(report.days[0], date(2026, 9, 1))
        self.assertEqual(report.tasks, 4)  # t4 on the 3rd too
        self.assertEqual(report.previous_tasks, 0)
        self.assertEqual(report.tokens, 1160)

    async def test_the_workspace_report_has_every_user(self):
        self.seed_quota(self.other, 10, metric="tasks", period="month")
        self.seed_quota(self.admin, None, metric="tokens", period="week")
        deleted = self.seed_user(status="deleted")

        report = await self.service.workspace_usage_report(
            self.principal(self.admin), UsageRange.LAST_14
        )

        self.assertEqual(report.tasks, 4)
        self.assertEqual(report.tokens, 180)
        users = {user.user_id: user for user in report.users}
        self.assertNotIn(deleted, users)
        self.assertEqual(
            [user.system_role for user in report.users][:2], ["owner", "admin"]
        )
        self.assertEqual((users[self.user].tasks, users[self.user].tokens), (3, 160))
        self.assertEqual((users[self.other].tasks, users[self.other].tokens), (1, 20))
        self.assertEqual((users[self.owner].tasks, users[self.owner].tokens), (0, 0))
        (quota,) = users[self.other].quotas
        self.assertEqual((quota.limit, quota.used), (10, 1))
        # The viewer's own quotas.
        self.assertEqual([quota.metric.value for quota in report.quotas], ["tokens"])

    async def test_a_deleted_user_with_usage_is_listed(self):
        gone = self.seed_user(status="deleted")
        task = self.seed_task(gone)
        self.seed_usage(gone, task, started_at=tokyo(2026, 9, 12), tokens=5)

        report = await self.service.workspace_usage_report(
            self.principal(self.admin), UsageRange.LAST_14
        )

        self.assertIn(gone, {user.user_id for user in report.users})

    async def test_the_quotas_of_the_report(self):
        self.seed_quota(self.user, 5, metric="tasks", period="day")

        report = await self.service.usage_report(
            self.principal(self.user), UsageRange.LAST_14, self.user
        )

        (quota,) = report.quotas
        # The 17th in Tokyo: t1 and t3 (the in-flight call).
        self.assertEqual((quota.limit, quota.used), (5, 2))
        self.assertEqual(quota.window_start, tokyo(2026, 9, 17))

    async def test_a_user_may_not_read_the_workspace_or_another_user(self):
        user = self.principal(self.user)

        with self.assertRaises(ConnectionPermissionDeniedError):
            await self.service.workspace_usage_report(user, UsageRange.LAST_14)
        with self.assertRaises(ConnectionPermissionDeniedError):
            await self.service.usage_report(user, UsageRange.LAST_14, self.other)

    async def test_an_admin_may_read_another_users_report(self):
        report = await self.service.usage_report(
            self.principal(self.admin), UsageRange.LAST_14, self.other
        )

        self.assertEqual((report.tasks, report.tokens), (1, 20))

    async def test_the_decisions_are_audited(self):
        sink = InMemoryAuditSink()
        service = self.new_service(
            clock=self.clock, allow_explicit_clock=True, audit_sink=sink,
            authorizer_sink=sink,
        )  # fmt: skip

        await service.workspace_usage_report(
            self.principal(self.admin), UsageRange.MONTH
        )
        with self.assertRaises(ConnectionPermissionDeniedError):
            await service.workspace_usage_report(
                self.principal(self.user), UsageRange.MONTH
            )

        self.assertEqual(
            [(event.action, event.decision) for event in sink.events],
            [("admin.usage.view", "allow"), ("admin.usage.view", "deny")],
        )

    async def test_the_arguments_are_checked(self):
        admin = self.principal(self.admin)

        with self.assertRaises(InvalidConnectionInputError):
            await self.service.workspace_usage_report(admin, "last7")
        with self.assertRaises(InvalidConnectionInputError):
            await self.service.usage_report(admin, UsageRange.MONTH, "not-a-uuid")
        with self.assertRaises(ConnectionPermissionDeniedError):
            await self.service.workspace_usage_report(object(), UsageRange.MONTH)

    async def test_a_report_holds_no_text(self):
        report = await self.service.workspace_usage_report(
            self.principal(self.admin), UsageRange.LAST_30
        )

        self.assertNotIn("seeded", repr(report))  # the model name
        self.assertIsInstance(report.users[0].user_id, uuid.UUID)


@requires_postgres
class UsageReportSnapshotTest(PostgresConnectionTestCase):
    """The report's statements read one snapshot (Codex review of PR #193): a
    call that commits while the report is being read is in none of its figures."""

    async def test_a_call_committed_meanwhile_is_not_half_counted(self):
        clock = FakeClock(NOW)
        service = self.new_service(clock=clock, allow_explicit_clock=True)
        self.seed_quota(self.admin, None, metric="requests", period="month")
        task = self.seed_task(self.admin)
        self.seed_usage(self.admin, task, started_at=tokyo(2026, 9, 17, 9))
        store = service._store
        original = store._quota_status_in
        late: list[uuid.UUID] = []

        async def quota_status_in(connection, now, user_id, kind):
            if not late:  # the first user's quotas: the totals are read already
                other = self.seed_task(self.admin)
                late.append(
                    self.seed_usage(
                        self.admin, other, started_at=tokyo(2026, 9, 17, 10)
                    )
                )
            return await original(connection, now, user_id, kind)

        store._quota_status_in = quota_status_in

        report = await service.workspace_usage_report(
            self.principal(self.admin), UsageRange.LAST_14
        )

        self.assertTrue(late)
        self.assertEqual(report.tasks, 1)
        (quota,) = report.quotas
        self.assertEqual(quota.used, 1)  # the same snapshot as the totals
        admin = next(user for user in report.users if user.user_id == self.admin)
        self.assertEqual((admin.tasks, admin.quotas[0].used), (1, 1))

    async def test_the_quotas_alone_read_one_snapshot_too(self):
        # ``quota_status`` (GET /quotas/me, /users/{id}/quotas): a call that
        # commits between the sums of two quotas is in neither.
        clock = FakeClock(NOW)
        service = self.new_service(clock=clock, allow_explicit_clock=True)
        self.seed_quota(self.user, None, kind=ConnectionKind.CODEX, period="month")
        self.seed_quota(self.user, None, kind=ConnectionKind.CLAUDE, period="month")
        task = self.seed_task(self.user)
        store = service._store
        original = store._sums
        late: list[uuid.UUID] = []

        async def sums(connection, user_id, kind, since):
            result = await original(connection, user_id, kind, since)
            if not late:  # after the first quota's sums
                late.append(
                    self.seed_usage(
                        self.user, task, kind=ConnectionKind.CLAUDE,
                        started_at=tokyo(2026, 9, 17, 9),
                    )
                )  # fmt: skip
            return result

        store._sums = sums

        quotas = await service.quota_status(self.principal(self.user), self.user)

        self.assertTrue(late)
        self.assertEqual([quota.used for quota in quotas], [0, 0])
