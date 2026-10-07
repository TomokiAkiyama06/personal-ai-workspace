"""The local usage, its GPU time and the escalations in the usage report (issue
#187 item 5, Decision 0077), on a real PostgreSQL.

* ``PostgresLocalUsage`` writes one row of ``local_usage`` for a local call,
  attributed to the task's creator, and nothing for a task that does not exist;
* the report counts a task with a shared connection's call and a local call once,
  puts the local calls on their Asia/Tokyo calendar days, counts as GPU time the
  seconds of the calls placed on the GPU only, and the escalated tasks of the user
  (or of the workspace, with the rows written before revision 0189 that have no
  task).

The HTTP answer is ``test_usage_api.py``; ``HybridRuntime``'s side is
``test_compute_runtimes.py`` (``HybridUsageTest``).
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from paw_backend.compute import Placement, PostgresLocalUsage
from paw_backend.compute.errors import InvalidComputeArgumentError
from paw_backend.connections import ConnectionPermissionDeniedError
from paw_backend.connections.report import (
    EscalationTotal,
    LocalDailyTasks,
    UsageRange,
)
from paw_backend.db import Database

from .connections_support import (
    FakeClock,
    PostgresConnectionTestCase,
    requires_postgres,
)
from .support import make_settings
from .task_support import TEST_DATABASE_URL

TOKYO = ZoneInfo("Asia/Tokyo")
# 12:00 on 17 September 2026 in Tokyo.
NOW = datetime(2026, 9, 17, 3, 0, tzinfo=UTC)


def tokyo(*args: int) -> datetime:
    return datetime(*args, tzinfo=TOKYO).astimezone(UTC)


class LocalUsageCase(PostgresConnectionTestCase):
    @classmethod
    def clean_tables(cls) -> None:
        super().clean_tables()  # tasks CASCADE: local_usage and agent_incidents too
        with cls.engine.begin() as connection:
            connection.execute(text("TRUNCATE agent_incidents"))

    def local(
        self,
        user_id: uuid.UUID,
        task_id: uuid.UUID,
        started_at: datetime,
        *,
        tokens: int = 0,
        seconds: int = 0,
        placement: str = "local_gpu",
        calls: int = 1,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO local_usage (user_id, task_id, placement, calls,"
                    " tokens, seconds, started_at) VALUES (:u, :t, :p, :c, :tokens,"
                    " :s, :at)"
                ),
                {
                    "u": user_id,
                    "t": task_id,
                    "p": placement,
                    "c": calls,
                    "tokens": tokens,
                    "s": seconds,
                    "at": started_at,
                },
            )

    def escalation(self, task_id: uuid.UUID | None, at: datetime) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO agent_incidents (kind, task_id, occurred_at)"
                    " VALUES ('escalation', :t, :at)"
                ),
                {"t": task_id, "at": at},
            )


@requires_postgres
class LocalUsageReportTest(LocalUsageCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.service = self.new_service(clock=FakeClock(NOW), allow_explicit_clock=True)
        self.other = self.seed_user()
        user = self.user
        self.shared = self.seed_task(user)  # Codex and local
        self.local_only = self.seed_task(user)
        self.before = self.seed_task(user)  # the previous period only
        self.theirs = self.seed_task(self.other)
        self.seed_usage(user, self.shared, started_at=tokyo(2026, 9, 17, 9), tokens=5)
        self.local(user, self.shared, tokyo(2026, 9, 17, 10), tokens=100, seconds=60)
        # 01:30 on the 17th in Tokyo is the 16th in UTC: the 17th.
        self.local(user, self.shared, tokyo(2026, 9, 17, 1, 30), seconds=4, calls=0)
        # On the CPU: tokens and a task, no GPU time.
        self.local(
            user, self.local_only, tokyo(2026, 9, 10), tokens=7, seconds=30,
            placement="local_cpu",
        )  # fmt: skip
        # The last minute before the period: the previous period.
        self.local(user, self.before, tokyo(2026, 9, 3, 23, 59), tokens=1, seconds=1)
        self.local(self.other, self.theirs, tokyo(2026, 9, 12), tokens=50, seconds=9)
        # Escalations: the same task twice (one task), another user's, one
        # without a task (before revision 0189), one before the period.
        self.escalation(self.shared, tokyo(2026, 9, 16, 8))
        self.escalation(self.shared, tokyo(2026, 9, 17, 8))
        self.escalation(self.theirs, tokyo(2026, 9, 12))
        self.escalation(None, tokyo(2026, 9, 5))
        self.escalation(self.before, tokyo(2026, 9, 3, 12))

    async def test_ones_own_report(self):
        report = await self.service.usage_report(
            self.principal(self.user), UsageRange.LAST_14, self.user
        )

        # The task with Codex and local calls is one task.
        self.assertEqual(report.tasks, 2)
        self.assertEqual(report.previous_tasks, 1)
        self.assertEqual(report.tokens, 5)  # the shared connections'
        self.assertEqual(
            (report.local.tasks, report.local.tokens, report.local.gpu_seconds),
            (2, 107, 64),
        )
        self.assertEqual(
            report.local.daily,
            (
                LocalDailyTasks(date(2026, 9, 10), 1),
                LocalDailyTasks(date(2026, 9, 17), 1),
            ),
        )
        self.assertEqual(report.escalations, EscalationTotal(0, 1))

    async def test_the_workspace_report(self):
        report = await self.service.workspace_usage_report(
            self.principal(self.admin), UsageRange.LAST_14
        )

        self.assertEqual(report.tasks, 3)
        self.assertEqual(
            (report.local.tasks, report.local.tokens, report.local.gpu_seconds),
            (3, 157, 73),
        )
        # shared, theirs, and the row without a task.
        self.assertEqual(report.escalations, EscalationTotal(0, 3))
        users = {user.user_id: user for user in report.users}
        self.assertEqual((users[self.user].tasks, users[self.user].tokens), (2, 112))
        self.assertEqual((users[self.other].tasks, users[self.other].tokens), (1, 50))

    async def test_the_month_and_the_previous_one(self):
        report = await self.service.usage_report(
            self.principal(self.user), UsageRange.MONTH, self.user
        )

        self.assertEqual(report.tasks, 3)  # ``before`` on the 3rd too
        self.assertEqual(report.previous_tasks, 0)
        self.assertEqual(report.local.gpu_seconds, 65)
        self.assertEqual(report.escalations, EscalationTotal(0, 2))

    async def test_a_deleted_user_with_local_calls_only_is_listed(self):
        gone = self.seed_user(status="deleted")
        task = self.seed_task(gone)
        self.local(gone, task, tokyo(2026, 9, 15), tokens=3)

        report = await self.service.workspace_usage_report(
            self.principal(self.admin), UsageRange.LAST_14
        )

        users = {user.user_id: user for user in report.users}
        self.assertEqual((users[gone].tasks, users[gone].tokens), (1, 3))

    async def test_another_user_may_not_read_it(self):
        with self.assertRaises(ConnectionPermissionDeniedError):
            await self.service.usage_report(
                self.principal(self.other), UsageRange.LAST_14, self.user
            )


@requires_postgres
class PostgresLocalUsageTest(LocalUsageCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        self.usage = PostgresLocalUsage(database)

    def rows(self) -> list[tuple]:
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    text(
                        "SELECT user_id, task_id, placement, calls, tokens, seconds,"
                        " now() - started_at FROM local_usage ORDER BY id"
                    )
                )
            )

    async def test_a_call_is_recorded_for_the_tasks_creator(self):
        task = self.seed_task(self.user)

        await self.usage.record(
            task, placement=Placement.LOCAL_GPU, calls=1, tokens=1234, seconds=91
        )
        await self.usage.record(
            task, placement=Placement.LOCAL_CPU, calls=0, tokens=0, seconds=0
        )

        first, second = self.rows()
        self.assertEqual(first[:6], (self.user, task, "local_gpu", 1, 1234, 91))
        # It started the seconds it held its lease before it was recorded.
        self.assertGreaterEqual(first[6], timedelta(seconds=91))
        self.assertLess(first[6], timedelta(seconds=91 + 600))
        self.assertEqual(second[:6], (self.user, task, "local_cpu", 0, 0, 0))

    async def test_an_unknown_task_records_nothing(self):
        await self.usage.record(
            uuid.uuid4(), placement=Placement.LOCAL_GPU, calls=1, tokens=1, seconds=1
        )

        self.assertEqual(self.rows(), [])

    async def test_the_arguments_are_checked_before_anything_is_written(self):
        task = self.seed_task(self.user)
        valid = dict(placement=Placement.LOCAL_GPU, calls=1, tokens=1, seconds=1)
        wrong = {
            "placement": [Placement.CLOUD, "local_gpu", None],
            "calls": [2, -1, True, 1.0],
            "tokens": [-1, 10**15 + 1, False, "1"],
            "seconds": [-1, 10**12 + 1, None],
        }
        for name, values in wrong.items():
            for value in values:
                with (
                    self.subTest(name=name, value=value),
                    self.assertRaises(InvalidComputeArgumentError),
                ):
                    await self.usage.record(task, **{**valid, name: value})
        with self.assertRaises(InvalidComputeArgumentError):
            await self.usage.record(str(task), **valid)

        self.assertEqual(self.rows(), [])

    async def test_the_database_refuses_what_the_model_refuses(self):
        task = self.seed_task(self.user)
        for column, value in (
            ("placement", "'cloud'"),
            ("calls", "2"),
            ("tokens", "-1"),
            ("seconds", "-1"),
        ):
            values = {
                "placement": "'local_gpu'",
                "calls": "1",
                "tokens": "0",
                "seconds": "0",
                column: value,
            }
            with self.subTest(column=column), self.assertRaises(IntegrityError):
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO local_usage (user_id, task_id, placement,"
                            f" calls, tokens, seconds, started_at) VALUES (:u, :t,"
                            f" {values['placement']}, {values['calls']},"
                            f" {values['tokens']}, {values['seconds']}, now())"
                        ),
                        {"u": self.user, "t": task},
                    )
        self.assertEqual(self.rows(), [])

    def test_it_needs_a_database(self):
        with self.assertRaises(TypeError):
            PostgresLocalUsage(object())
