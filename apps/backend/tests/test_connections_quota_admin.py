"""Quota administration and the views of quotas and usage."""

import dataclasses
import unittest
import uuid
from datetime import timedelta

from paw_backend.authz.policy import Reason
from paw_backend.connections import (
    UNLIMITED,
    ConnectionKind,
    ConnectionPermissionDeniedError,
    InvalidConnectionInputError,
    QuotaMetric,
    QuotaPeriod,
    TargetUserNotFoundError,
    UsagePurpose,
    UsageStatus,
)

from .connections_fakes import T0, FakeClock
from .connections_support import PostgresConnectionTestCase, requires_postgres

CODEX, CLAUDE = ConnectionKind.CODEX, ConnectionKind.CLAUDE
REQUESTS, TASKS, TOKENS, RUNTIME = (
    QuotaMetric.REQUESTS,
    QuotaMetric.TASKS,
    QuotaMetric.TOKENS,
    QuotaMetric.RUNTIME_SECONDS,
)
DAY, WEEK, MONTH, ROLLING = (
    QuotaPeriod.DAY,
    QuotaPeriod.WEEK,
    QuotaPeriod.MONTH,
    QuotaPeriod.ROLLING_5H,
)


def quota_rows(test: PostgresConnectionTestCase, user=None):
    return test.rows(
        "SELECT user_id, kind, metric, period, limit_value, created_at, updated_at"
        " FROM connection_quotas"
        + (" WHERE user_id = :u" if user else "")
        + " ORDER BY kind, metric, period",
        **({"u": user} if user else {}),
    )


@requires_postgres
class SetQuotaTest(PostgresConnectionTestCase):
    async def test_a_limit_is_stored_and_returned(self):
        quota = await self.service.set_quota(
            self.principal(self.admin), self.user, CODEX, REQUESTS, DAY, 100
        )

        self.assertEqual(
            (quota.user_id, quota.kind, quota.metric, quota.period, quota.limit),
            (self.user, CODEX, REQUESTS, DAY, 100),
        )
        (row,) = quota_rows(self)
        self.assertEqual(row["limit_value"], 100)
        self.assertEqual(row["created_at"], row["updated_at"])

    async def test_unlimited_is_stored_as_null_and_read_back_as_unlimited(self):
        quota = await self.service.set_quota(
            self.principal(self.owner), self.owner, CODEX, REQUESTS, DAY, UNLIMITED
        )
        self.assertIs(quota.limit, UNLIMITED)
        self.assertIsNone(quota_rows(self)[0]["limit_value"])

    async def test_zero_is_a_limit_and_not_unlimited(self):
        quota = await self.service.set_quota(
            self.principal(self.admin), self.user, CODEX, TOKENS, WEEK, 0
        )
        self.assertEqual(quota.limit, 0)
        self.assertIsNot(quota.limit, UNLIMITED)
        self.assertEqual(quota_rows(self)[0]["limit_value"], 0)

    async def test_the_string_and_member_forms_are_the_same(self):
        await self.service.set_quota(
            self.principal(self.admin),
            str(self.user),
            "claude",
            "tokens",
            "month",
            "unlimited",
        )
        (row,) = quota_rows(self)
        self.assertEqual(
            (row["kind"], row["metric"], row["period"], row["limit_value"]),
            ("claude", "tokens", "month", None),
        )

    async def test_setting_again_changes_the_limit_and_keeps_the_creation_time(self):
        admin = self.principal(self.admin)
        await self.service.set_quota(admin, self.user, CODEX, REQUESTS, DAY, 5)
        first = quota_rows(self)[0]
        again = await self.service.set_quota(admin, self.user, CODEX, REQUESTS, DAY, 9)
        (row,) = quota_rows(self)
        self.assertEqual(again.limit, 9)
        self.assertEqual(row["limit_value"], 9)
        self.assertEqual(row["created_at"], first["created_at"])
        self.assertGreater(row["updated_at"], first["updated_at"])
        self.assertEqual(again.updated_at, row["updated_at"])

    async def test_a_number_can_become_unlimited_and_back(self):
        admin = self.principal(self.admin)
        await self.service.set_quota(admin, self.user, CODEX, REQUESTS, DAY, 5)
        await self.service.set_quota(admin, self.user, CODEX, REQUESTS, DAY, UNLIMITED)
        self.assertIsNone(quota_rows(self)[0]["limit_value"])
        await self.service.set_quota(admin, self.user, CODEX, REQUESTS, DAY, 3)
        self.assertEqual(quota_rows(self)[0]["limit_value"], 3)

    async def test_every_metric_and_period_and_kind_can_have_its_own_limit(self):
        admin = self.principal(self.admin)
        for kind in ConnectionKind:
            for metric in QuotaMetric:
                for period in QuotaPeriod:
                    await self.service.set_quota(
                        admin, self.user, kind, metric, period, 1
                    )
        self.assertEqual(len(quota_rows(self)), 2 * 4 * 4)

    async def test_the_largest_limit(self):
        quota = await self.service.set_quota(
            self.principal(self.admin), self.user, CODEX, TOKENS, MONTH, 10**12
        )
        self.assertEqual(quota.limit, 10**12)

    async def test_a_user_who_does_not_exist_is_refused_and_nothing_is_stored(self):
        with self.assertRaises(TargetUserNotFoundError):
            await self.service.set_quota(
                self.principal(self.admin), uuid.uuid4(), CODEX, REQUESTS, DAY, 5
            )
        self.assertEqual(quota_rows(self), [])
        self.assertEqual(self.own_audit(), [])

    async def test_a_deleted_or_pending_user_is_refused_and_an_invited_one_is_not(self):
        for status, accepted in (
            ("deleted", False),
            ("pending_deletion", False),
            ("invited", True),
            ("active", True),
        ):
            with self.subTest(status=status):
                target = self.seed_user(status=status)
                if accepted:
                    await self.service.set_quota(
                        self.principal(self.admin), target, CODEX, REQUESTS, DAY, 5
                    )
                    self.assertEqual(len(quota_rows(self, target)), 1)
                else:
                    with self.assertRaises(TargetUserNotFoundError):
                        await self.service.set_quota(
                            self.principal(self.admin), target, CODEX, REQUESTS, DAY, 5
                        )
                    self.assertEqual(quota_rows(self, target), [])

    async def test_the_decision_and_the_outcome_are_audited(self):
        await self.service.set_quota(
            self.principal(self.admin), self.user, CODEX, REQUESTS, DAY, 5
        )
        self.assertEqual(
            self.audit_actions(),
            [
                ("admin.quota.manage", "allow", "granted_by_system_role"),
                ("connection.quota.set", "allow", "succeeded"),
            ],
        )
        outcome = self.sink.events[1]
        self.assertEqual(
            (outcome.actor_id, outcome.resource_kind), (self.admin, "connection_quota")
        )
        self.assertEqual(outcome.resource_id, self.user)

    async def test_a_user_cannot_set_their_own_quota(self):
        with self.assertRaises(ConnectionPermissionDeniedError) as caught:
            await self.service.set_quota(
                self.principal(self.user), self.user, CODEX, REQUESTS, DAY, 10**6
            )
        self.assertEqual(caught.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
        self.assertEqual(quota_rows(self), [])
        self.assertEqual(
            self.audit_actions(),
            [("admin.quota.manage", "deny", "capability_not_granted")],
        )

    async def test_a_bad_limit_is_refused_before_anything_is_written(self):
        for bad in (-1, 10**12 + 1, None, True, 1.5, "10"):
            with self.subTest(limit=repr(bad)):
                with self.assertRaises(InvalidConnectionInputError):
                    await self.service.set_quota(
                        self.principal(self.admin), self.user, CODEX, REQUESTS, DAY, bad
                    )
        self.assertEqual(quota_rows(self), [])


@requires_postgres
class RemoveQuotaTest(PostgresConnectionTestCase):
    async def test_a_limit_is_removed_and_the_others_stay(self):
        self.seed_quota(self.user, 5, period="day")
        self.seed_quota(self.user, 9, period="week")
        removed = await self.service.remove_quota(
            self.principal(self.admin), self.user, CODEX, REQUESTS, DAY
        )
        self.assertTrue(removed)
        (row,) = quota_rows(self)
        self.assertEqual((row["period"], row["limit_value"]), ("week", 9))
        self.assertEqual(
            self.own_audit(), [("connection.quota.remove", "allow", "succeeded")]
        )

    async def test_removing_what_is_not_there_is_false_and_writes_no_outcome(self):
        removed = await self.service.remove_quota(
            self.principal(self.admin), self.user, CODEX, REQUESTS, DAY
        )
        self.assertFalse(removed)
        self.assertEqual(self.own_audit(), [])

    async def test_removing_is_scoped_to_user_kind_metric_and_period(self):
        other = self.seed_user()
        self.seed_quota(self.user, 5)
        self.seed_quota(other, 5)
        self.seed_quota(self.user, 5, kind=CLAUDE)
        self.seed_quota(self.user, 5, metric="tokens")
        await self.service.remove_quota(
            self.principal(self.admin), self.user, CODEX, REQUESTS, DAY
        )
        self.assertEqual(len(quota_rows(self)), 3)

    async def test_a_user_cannot_remove_a_quota(self):
        self.seed_quota(self.user, 5)
        with self.assertRaises(ConnectionPermissionDeniedError):
            await self.service.remove_quota(
                self.principal(self.user), self.user, CODEX, REQUESTS, DAY
            )
        self.assertEqual(len(quota_rows(self)), 1)


@requires_postgres
class QuotaStatusTest(PostgresConnectionTestCase):
    """The quotas of a user with what the current window of each has used."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.clock = FakeClock(T0)  # Thursday 2026-09-24 12:00 UTC
        self.service = self.new_service(clock=self.clock, allow_explicit_clock=True)
        self.task = self.seed_task(self.user)

    async def status(self, user=None, kind=None):
        return await self.service.quota_status(
            self.principal(user or self.user), user or self.user, kind
        )

    async def test_no_quota_is_an_empty_list(self):
        self.assertEqual(await self.status(), ())

    async def test_each_quota_shows_its_limit_used_and_window(self):
        self.seed_quota(self.user, 5, metric="requests", period="day")
        other_task = self.seed_task(self.user)
        self.seed_usage(
            self.user, self.task, started_at=T0 - timedelta(hours=1), tokens=100
        )
        self.seed_usage(
            self.user, other_task, started_at=T0 - timedelta(hours=2), tokens=50
        )

        (status,) = await self.status()

        self.assertEqual(
            (status.kind, status.metric, status.period), (CODEX, REQUESTS, DAY)
        )
        self.assertEqual((status.limit, status.used), (5, 2))
        self.assertEqual(status.window_start, T0.replace(hour=0))
        self.assertEqual(status.window_end, T0.replace(hour=0) + timedelta(days=1))
        self.assertFalse(status.reached)

    async def test_the_four_metrics_are_counted_as_documented(self):
        for metric in QuotaMetric:
            self.seed_quota(self.user, 1000, metric=metric.value)
        second = self.seed_task(self.user)
        # 3 calls of 2 tasks: 100 input + 20 output tokens on one, an unknown count
        # (NULL) on another, 7 on the third; 1500 + 600 + 0 ms of runtime.
        self.seed_usage(
            self.user, self.task, tokens=100, output_tokens=20, duration_ms=1500
        )
        self.seed_usage(self.user, self.task, tokens=None, duration_ms=600)
        self.seed_usage(self.user, second, tokens=7, duration_ms=0)

        used = {s.metric: s.used for s in await self.status()}

        self.assertEqual(
            used,
            {REQUESTS: 3, TASKS: 2, TOKENS: 100 + 20 + 7, RUNTIME: 2},  # 2100 ms
        )

    async def test_rows_outside_the_window_and_of_others_are_not_counted(self):
        self.seed_quota(self.user, 3, period="day")
        other_user = self.seed_user()
        other_task = self.seed_task(other_user)
        self.seed_usage(self.user, self.task, started_at=T0 - timedelta(hours=1))
        self.seed_usage(
            self.user, self.task, started_at=T0.replace(hour=0) - timedelta(seconds=1)
        )
        self.seed_usage(other_user, other_task, started_at=T0)
        self.seed_usage(self.user, self.task, kind=CLAUDE, started_at=T0)
        (status,) = await self.status(kind=CODEX)
        self.assertEqual(status.used, 1)

    async def test_the_start_of_the_window_belongs_to_it(self):
        self.seed_quota(self.user, 3, period="day")
        self.seed_usage(self.user, self.task, started_at=T0.replace(hour=0))
        (status,) = await self.status()
        self.assertEqual(status.used, 1)

    async def test_a_quota_is_reached_when_used_equals_the_limit(self):
        self.seed_quota(self.user, 2)
        self.seed_usage(self.user, self.task)
        self.assertFalse((await self.status())[0].reached)
        self.seed_usage(self.user, self.task)
        self.assertTrue((await self.status())[0].reached)

    async def test_unlimited_is_never_reached_but_still_counted(self):
        self.seed_quota(self.user, None)
        self.seed_usage(self.user, self.task)
        (status,) = await self.status()
        self.assertIs(status.limit, UNLIMITED)
        self.assertEqual(status.used, 1)
        self.assertFalse(status.reached)

    async def test_every_period_reports_its_own_window(self):
        for period in QuotaPeriod:
            self.seed_quota(self.user, 9, period=period.value)
        by_period = {s.period: s for s in await self.status()}
        self.assertEqual(by_period[ROLLING].window_start, T0 - timedelta(hours=5))
        self.assertIsNone(by_period[ROLLING].window_end)
        self.assertEqual(
            by_period[WEEK].window_start.isoformat(), "2026-09-21T00:00:00+00:00"
        )
        self.assertEqual(
            by_period[WEEK].window_end.isoformat(), "2026-09-28T00:00:00+00:00"
        )
        self.assertEqual(
            by_period[MONTH].window_start.isoformat(), "2026-09-01T00:00:00+00:00"
        )
        self.assertEqual(
            by_period[MONTH].window_end.isoformat(), "2026-10-01T00:00:00+00:00"
        )

    async def test_the_list_is_ordered_by_kind_metric_and_period(self):
        for kind in (CLAUDE, CODEX):
            for metric in (RUNTIME, REQUESTS):
                for period in (MONTH, DAY):
                    self.seed_quota(
                        self.user,
                        1,
                        kind=kind,
                        metric=metric.value,
                        period=period.value,
                    )
        order = [(s.kind, s.metric, s.period) for s in await self.status()]
        self.assertEqual(
            order,
            [
                (kind, metric, period)
                for kind in (CODEX, CLAUDE)
                for metric in (REQUESTS, RUNTIME)
                for period in (DAY, MONTH)
            ],
        )

    async def test_a_kind_filter_narrows_the_list(self):
        self.seed_quota(self.user, 1, kind=CODEX)
        self.seed_quota(self.user, 1, kind=CLAUDE)
        self.assertEqual([s.kind for s in await self.status(kind=CLAUDE)], [CLAUDE])
        self.assertEqual(len(await self.status()), 2)

    async def test_a_user_sees_their_own_and_an_admin_anyones(self):
        self.seed_quota(self.user, 4)
        own = await self.status()
        seen_by_admin = await self.service.quota_status(
            self.principal(self.admin), self.user
        )
        self.assertEqual(own, seen_by_admin)
        events = self.audit_actions()
        self.assertEqual(
            events,
            [
                ("agent.use", "allow", "granted_to_resource_owner"),
                ("admin.usage.view", "allow", "granted_by_system_role"),
            ],
        )

    async def test_a_user_cannot_see_another_users_quota(self):
        other = self.seed_user()
        self.seed_quota(other, 4)
        with self.assertRaises(ConnectionPermissionDeniedError) as caught:
            await self.service.quota_status(self.principal(self.user), other)
        self.assertEqual(caught.exception.reason, Reason.CAPABILITY_NOT_GRANTED)

    async def test_even_the_owner_reads_another_users_through_the_admin_capability_only(
        self,
    ):
        self.seed_quota(self.user, 4)
        await self.service.quota_status(self.principal(self.owner), self.user)
        self.assertEqual(self.audit_actions()[0][0], "admin.usage.view")

    async def test_the_view_holds_no_text(self):
        self.seed_quota(self.user, 4)
        (status,) = await self.status()
        self.assertEqual(
            {f.name for f in dataclasses.fields(status)},
            {"kind", "metric", "period", "limit", "used", "window_start", "window_end"},
        )


@requires_postgres
class ListUsageTest(PostgresConnectionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.task = self.seed_task(self.user)

    async def listing(self, **options):
        return await self.service.list_usage(
            self.principal(self.user), self.user, **options
        )

    async def test_the_records_are_newest_first_with_paging(self):
        ids = [
            self.seed_usage(self.user, self.task, started_at=T0 + timedelta(minutes=n))
            for n in range(5)
        ]
        everything = await self.listing()
        self.assertEqual([r.id for r in everything], ids[::-1])
        self.assertEqual([r.id for r in await self.listing(limit=2)], ids[:-3:-1])
        self.assertEqual(
            [r.id for r in await self.listing(limit=2, offset=2)], [ids[2], ids[1]]
        )
        self.assertEqual(await self.listing(offset=5), ())

    async def test_a_record_carries_the_attribution_and_no_text(self):
        usage_id = self.seed_usage(self.user, self.task, tokens=12, duration_ms=1500)
        (record,) = await self.listing()
        self.assertEqual(record.id, usage_id)
        self.assertEqual(
            (record.user_id, record.task_id, record.project_id, record.kind),
            (self.user, self.task, self.project_of(self.task), CODEX),
        )
        self.assertEqual(
            (record.model, record.purpose, record.status),
            ("seeded", UsagePurpose.CODING, UsageStatus.SUCCEEDED),
        )
        self.assertEqual((record.input_tokens, record.duration_ms), (12, 1500))
        self.assertEqual(
            {f.name for f in dataclasses.fields(record)},
            {
                "id",
                "user_id",
                "task_id",
                "project_id",
                "kind",
                "model",
                "purpose",
                "status",
                "failure_code",
                "input_tokens",
                "output_tokens",
                "started_at",
                "finished_at",
                "duration_ms",
            },
        )

    async def test_the_kind_filter_and_the_owner_of_the_records(self):
        other = self.seed_user()
        other_task = self.seed_task(other)
        self.seed_usage(self.user, self.task, kind=CODEX)
        self.seed_usage(self.user, self.task, kind=CLAUDE)
        self.seed_usage(other, other_task, kind=CODEX)
        self.assertEqual({r.kind for r in await self.listing()}, {CODEX, CLAUDE})
        self.assertEqual({r.kind for r in await self.listing(kind=CLAUDE)}, {CLAUDE})
        self.assertEqual(
            {
                r.user_id
                for r in await self.service.list_usage(
                    self.principal(self.admin), other
                )
            },
            {other},
        )

    async def test_another_users_records_need_the_admin_capability(self):
        other = self.seed_user()
        with self.assertRaises(ConnectionPermissionDeniedError):
            await self.service.list_usage(self.principal(self.user), other)
        self.assertEqual(
            self.audit_actions(),
            [("admin.usage.view", "deny", "capability_not_granted")],
        )

    async def test_bad_paging_is_refused(self):
        for options in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"limit": True}):
            with self.subTest(options=options):
                with self.assertRaises(InvalidConnectionInputError):
                    await self.listing(**options)


if __name__ == "__main__":
    unittest.main()
