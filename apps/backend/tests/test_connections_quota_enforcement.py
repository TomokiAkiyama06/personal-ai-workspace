"""Per-user quotas at the admission: windows, refusals, running tasks, clocks."""

import unittest
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from paw_backend.connections import (
    UNLIMITED,
    AdapterResult,
    ConnectionKind,
    ConnectionUnavailableError,
    QuotaExceededError,
    QuotaMetric,
    QuotaPeriod,
)

from .connections_fakes import T0, FakeClock, handle
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
MIDNIGHT = T0.replace(hour=0)  # 2026-09-24T00:00Z, a Thursday


class QuotaCase(PostgresConnectionTestCase):
    """A connected Codex connection and a service whose clock the test moves."""

    zone: str | None = "UTC"  # None: the default of the service (Asia/Tokyo)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.seed_connection(CODEX)
        self.clock = FakeClock(T0)
        zone = {} if self.zone is None else {"period_timezone": self.zone}
        self.service = self.new_service(
            clock=self.clock, allow_explicit_clock=True, **zone
        )

    async def new_task_call(self, user=None, kind=CODEX, **request):
        """A call of a task that has never used the connection."""
        user = user or self.user
        task = self.seed_task(user)
        return await self.service.execute(
            self.principal(user),
            self.context(task, user, self.project_of(task)),
            kind,
            self.request(**request),
        )

    async def call_of(self, task, user=None, kind=CODEX):
        user = user or self.user
        return await self.service.execute(
            self.principal(user),
            self.context(task, user, self.project_of(task)),
            kind,
            self.request(),
        )

    async def assertAdmits(self, **options):
        result = await self.new_task_call(**options)
        self.assertEqual(result.text, "an answer")

    async def assertRefuses(self, metric, period, resets_at, **options):
        with self.assertRaises(QuotaExceededError) as caught:
            await self.new_task_call(**options)
        error = caught.exception
        self.assertEqual((error.metric, error.period), (metric, period))
        self.assertEqual(error.resets_at, resets_at)
        return error

    def old_task(self):
        """A finished task of the user: a place to hang seeded usage rows on."""
        return self.seed_task(self.user, state="completed")


@requires_postgres
class RequestsQuotaTest(QuotaCase):
    async def test_the_limit_admits_that_many_new_tasks_and_then_refuses(self):
        self.seed_quota(self.user, 2)
        await self.assertAdmits()
        await self.assertAdmits()
        error = await self.assertRefuses(
            REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC)
        )

        self.assertEqual(str(error), "Quota reached: requests per day")
        self.assertEqual(len(self.usage_rows()), 2)
        self.assertEqual(len(self.codex.requests), 2)  # the third never reached it

    async def test_a_refusal_is_audited_with_the_user_and_the_project(self):
        self.seed_quota(self.user, 0)
        task = self.seed_task(self.user)
        with self.assertRaises(QuotaExceededError):
            await self.call_of(task)
        (event,) = [e for e in self.sink.events if e.action == "connection.use"]
        self.assertEqual((event.decision, event.reason), ("deny", "quota_exceeded"))
        self.assertEqual((event.actor_id, event.actor_role), (self.user, "user"))
        self.assertEqual(event.project_id, self.project_of(task))
        self.assertEqual((event.resource_kind, event.resource_id), ("connection", None))
        # The Authorizer allowed the use; the quota refused it: two rows, one story.
        self.assertEqual(
            self.audit_actions(),
            [
                ("agent.use", "allow", "granted_to_resource_owner"),
                ("connection.use", "deny", "quota_exceeded"),
            ],
        )
        self.assertEqual(self.sink.events[0].correlation_id, event.correlation_id)

    async def test_zero_blocks_every_new_task(self):
        self.seed_quota(self.user, 0)
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))
        self.assertEqual(self.usage_rows(), [])

    async def test_the_last_call_below_the_limit_is_admitted_and_the_next_is_not(self):
        self.seed_quota(self.user, 3)
        old = self.old_task()
        for _ in range(2):
            self.seed_usage(self.user, old, started_at=MIDNIGHT)
        await self.assertAdmits()  # 2 used, limit 3
        await self.assertRefuses(
            REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC)
        )  # 3 used

    async def test_unlimited_admits_without_end(self):
        self.seed_quota(self.user, None)
        for _ in range(12):
            await self.assertAdmits()
        self.assertEqual(len(self.usage_rows()), 12)

    async def test_the_next_day_starts_a_new_count(self):
        self.seed_quota(self.user, 1)
        await self.assertAdmits()
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))
        self.clock.now = datetime(2026, 9, 25, tzinfo=UTC)  # midnight itself
        await self.assertAdmits()

    async def test_the_last_instant_of_the_day_still_counts_for_that_day(self):
        self.seed_quota(self.user, 1)
        await self.assertAdmits()
        self.clock.now = datetime(2026, 9, 25, tzinfo=UTC) - timedelta(microseconds=1)
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))

    async def test_only_this_user_and_this_kind_are_counted(self):
        self.seed_quota(self.user, 1)
        stranger = self.seed_user()
        theirs = self.seed_task(stranger)
        self.seed_usage(stranger, theirs, started_at=MIDNIGHT)
        self.seed_usage(self.user, self.old_task(), kind=CLAUDE, started_at=MIDNIGHT)
        await self.assertAdmits()  # not blocked by another user's or another kind's use
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))

    async def test_rows_before_the_window_are_not_counted(self):
        self.seed_quota(self.user, 1)
        self.seed_usage(
            self.user, self.old_task(), started_at=MIDNIGHT - timedelta(seconds=1)
        )
        await self.assertAdmits()

    async def test_failed_cancelled_and_in_flight_calls_all_count(self):
        self.seed_quota(self.user, 3)
        old = self.old_task()
        self.seed_usage(self.user, old, started_at=MIDNIGHT, status="failed")
        self.seed_usage(self.user, old, started_at=MIDNIGHT, status="cancelled")
        self.seed_usage(self.user, old, started_at=MIDNIGHT, status="in_flight")
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))

    async def test_the_limit_of_another_kind_does_not_admit_this_kind(self):
        self.seed_quota(self.user, 0, kind=CODEX)
        self.seed_quota(self.user, None, kind=CLAUDE)
        self.seed_connection(CLAUDE, secret_handle=handle(2))
        await self.assertAdmits(kind=CLAUDE)
        await self.assertRefuses(
            REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC), kind=CODEX
        )

    async def test_a_changed_limit_takes_effect_at_the_next_admission(self):
        self.seed_quota(self.user, 1)
        await self.assertAdmits()
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))
        await self.service.set_quota(
            self.principal(self.admin), self.user, CODEX, REQUESTS, DAY, 2
        )
        await self.assertAdmits()
        await self.service.set_quota(
            self.principal(self.admin), self.user, CODEX, REQUESTS, DAY, 0
        )
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))


@requires_postgres
class WindowsTest(QuotaCase):
    async def test_a_rolling_window_is_the_last_five_hours_and_has_no_reset_time(self):
        self.seed_quota(self.user, 1, period="rolling_5h")
        old = self.old_task()
        self.seed_usage(self.user, old, started_at=T0 - timedelta(hours=5, seconds=1))
        await self.assertAdmits()  # the older call left the window
        await self.assertRefuses(REQUESTS, ROLLING, None)

    async def test_a_call_just_inside_the_rolling_window_counts(self):
        self.seed_quota(self.user, 1, period="rolling_5h")
        self.seed_usage(
            self.user, self.old_task(), started_at=T0 - timedelta(hours=4, minutes=59)
        )
        await self.assertRefuses(REQUESTS, ROLLING, None)

    async def test_the_rolling_window_slides_with_the_clock(self):
        self.seed_quota(self.user, 1, period="rolling_5h")
        await self.assertAdmits()
        self.clock.advance(hours=4, minutes=59)
        await self.assertRefuses(REQUESTS, ROLLING, None)
        self.clock.advance(minutes=2)
        await self.assertAdmits()

    async def test_a_week_runs_from_monday_and_reports_the_next_monday(self):
        self.seed_quota(self.user, 1, period="week")
        # Monday 2026-09-21 is this week's first day; Sunday the 20th is last week's.
        self.seed_usage(
            self.user,
            self.old_task(),
            started_at=datetime(2026, 9, 20, 23, 59, tzinfo=UTC),
        )
        await self.assertAdmits()
        await self.assertRefuses(REQUESTS, WEEK, datetime(2026, 9, 28, tzinfo=UTC))

    async def test_a_call_on_monday_morning_counts_for_the_whole_week(self):
        self.seed_quota(self.user, 1, period="week")
        self.seed_usage(
            self.user, self.old_task(), started_at=datetime(2026, 9, 21, tzinfo=UTC)
        )
        await self.assertRefuses(REQUESTS, WEEK, datetime(2026, 9, 28, tzinfo=UTC))

    async def test_a_month_runs_from_the_first_and_reports_the_next_first(self):
        self.seed_quota(self.user, 1, period="month")
        self.seed_usage(
            self.user,
            self.old_task(),
            started_at=datetime(2026, 8, 31, 23, 59, tzinfo=UTC),
        )
        await self.assertAdmits()
        await self.assertRefuses(REQUESTS, MONTH, datetime(2026, 10, 1, tzinfo=UTC))
        self.clock.now = datetime(2026, 10, 1, tzinfo=UTC)
        await self.assertAdmits()

    async def test_december_resets_in_january(self):
        self.seed_quota(self.user, 0, period="month")
        self.clock.now = datetime(2026, 12, 31, 23, 0, tzinfo=UTC)
        await self.assertRefuses(REQUESTS, MONTH, datetime(2027, 1, 1, tzinfo=UTC))

    async def test_every_period_is_checked_and_the_first_reached_is_reported(self):
        # 3 calls today, 5 this week, 8 this month: limits 5 / 5 / 10 / 10.
        for period, limit in (
            ("rolling_5h", 10),
            ("day", 3),
            ("week", 5),
            ("month", 10),
        ):
            self.seed_quota(self.user, limit, period=period)
        old = self.old_task()
        for _ in range(3):
            self.seed_usage(self.user, old, started_at=MIDNIGHT + timedelta(hours=1))
        for _ in range(2):
            self.seed_usage(
                self.user, old, started_at=datetime(2026, 9, 22, 8, tzinfo=UTC)
            )
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))

    async def test_a_quota_that_is_not_reached_lets_the_next_one_decide(self):
        self.seed_quota(self.user, 100, period="day")
        self.seed_quota(self.user, 2, period="week")
        old = self.old_task()
        for _ in range(2):
            self.seed_usage(
                self.user, old, started_at=datetime(2026, 9, 22, tzinfo=UTC)
            )
        await self.assertRefuses(REQUESTS, WEEK, datetime(2026, 9, 28, tzinfo=UTC))

    async def test_metrics_are_checked_in_order_before_periods(self):
        self.seed_quota(self.user, 0, metric="tokens", period="rolling_5h")
        self.seed_quota(self.user, 0, metric="requests", period="month")
        await self.assertRefuses(REQUESTS, MONTH, datetime(2026, 10, 1, tzinfo=UTC))

    async def test_an_unlimited_period_does_not_hide_a_limited_one(self):
        self.seed_quota(self.user, None, period="day")
        self.seed_quota(self.user, 0, period="week")
        await self.assertRefuses(REQUESTS, WEEK, datetime(2026, 9, 28, tzinfo=UTC))


@requires_postgres
class TasksTokensRuntimeTest(QuotaCase):
    async def test_the_tasks_metric_counts_distinct_tasks(self):
        self.seed_quota(self.user, 2, metric="tasks")
        old = self.old_task()
        for _ in range(4):  # many calls of ONE task: one task
            self.seed_usage(self.user, old, started_at=MIDNIGHT)
        await self.assertAdmits()  # the second task
        await self.assertRefuses(TASKS, DAY, datetime(2026, 9, 25, tzinfo=UTC))

    async def test_tokens_are_counted_when_a_call_has_settled(self):
        self.seed_quota(self.user, 100, metric="tokens")
        old = self.old_task()
        self.seed_usage(
            self.user, old, started_at=MIDNIGHT, tokens=60, output_tokens=39
        )
        await self.assertAdmits()  # 99 used
        # That call reported 15 tokens: 114 now.
        await self.assertRefuses(TOKENS, DAY, datetime(2026, 9, 25, tzinfo=UTC))

    async def test_tokens_exactly_at_the_limit_are_reached(self):
        self.seed_quota(self.user, 100, metric="tokens")
        self.seed_usage(self.user, self.old_task(), started_at=MIDNIGHT, tokens=100)
        await self.assertRefuses(TOKENS, DAY, datetime(2026, 9, 25, tzinfo=UTC))

    async def test_unknown_token_counts_add_nothing(self):
        self.seed_quota(self.user, 1, metric="tokens")
        self.seed_usage(self.user, self.old_task(), started_at=MIDNIGHT, tokens=None)
        await self.assertAdmits()

    async def test_runtime_is_counted_in_whole_seconds_rounded_down(self):
        self.seed_quota(self.user, 2, metric="runtime_seconds")
        old = self.old_task()
        self.seed_usage(self.user, old, started_at=MIDNIGHT, duration_ms=1999)
        await self.assertAdmits()  # 1.999 s used: below 2
        self.seed_usage(self.user, old, started_at=MIDNIGHT, duration_ms=1)  # 2.000 s
        await self.assertRefuses(RUNTIME, DAY, datetime(2026, 9, 25, tzinfo=UTC))

    async def test_a_call_that_has_not_ended_has_no_runtime_yet(self):
        self.seed_quota(self.user, 1, metric="runtime_seconds")
        self.seed_usage(
            self.user, self.old_task(), started_at=MIDNIGHT, status="in_flight"
        )
        await self.assertAdmits()

    async def test_the_duration_of_a_call_is_measured_by_the_clock_of_the_store(self):
        self.seed_quota(self.user, 10, metric="runtime_seconds")

        async def slow(secret, request):
            self.clock.advance(seconds=4, milliseconds=500)
            return AdapterResult("done", 1, 1)

        self.codex.run = slow
        result = await self.new_task_call()
        self.assertEqual(result.duration_ms, 4500)
        status = await self.service.quota_status(self.principal(self.user), self.user)
        (runtime,) = status
        self.assertEqual(runtime.used, 4)


@requires_postgres
class RunningTaskTest(QuotaCase):
    """A quota that is reached stops NEW tasks; a running task is not cut off."""

    async def test_a_running_task_keeps_calling_after_the_quota_is_reached(self):
        self.seed_quota(self.user, 1)
        task = self.seed_task(self.user)
        await self.call_of(task)  # the one request the quota allows
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))

        await self.call_of(task)  # the task that started is finished, not stopped
        await self.call_of(task)

        self.assertEqual(len(self.usage_rows()), 3)  # ...and every call is recorded
        (status,) = await self.service.quota_status(
            self.principal(self.user), self.user
        )
        self.assertEqual((status.used, status.limit, status.reached), (3, 1, True))

    async def test_the_new_task_is_refused_while_the_running_one_goes_on(self):
        self.seed_quota(self.user, 1)
        running = self.seed_task(self.user)
        await self.call_of(running)
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))
        await self.call_of(running)
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))

    async def test_a_running_task_is_not_stopped_by_a_quota_set_to_zero(self):
        self.seed_quota(self.user, 5)
        task = self.seed_task(self.user)
        await self.call_of(task)
        await self.service.set_quota(
            self.principal(self.admin), self.user, CODEX, REQUESTS, DAY, 0
        )
        await self.call_of(task)
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))

    async def test_a_running_task_is_not_stopped_when_its_quotas_are_removed(self):
        self.seed_quota(self.user, 5)
        task = self.seed_task(self.user)
        await self.call_of(task)
        await self.service.remove_quota(
            self.principal(self.admin), self.user, CODEX, REQUESTS, DAY
        )
        await self.call_of(task)
        await self.assertAdmits()  # a user without quota rows is unlimited

    async def test_a_task_that_started_yesterday_continues_today(self):
        self.seed_quota(self.user, 0)
        task = self.seed_task(self.user)
        self.seed_usage(self.user, task, started_at=MIDNIGHT - timedelta(hours=20))
        await self.call_of(task)

    async def test_a_task_that_used_only_the_other_kind_is_new_for_this_one(self):
        self.seed_quota(self.user, 0, kind=CODEX)
        task = self.seed_task(self.user)
        self.seed_usage(self.user, task, kind=CLAUDE, started_at=MIDNIGHT)
        with self.assertRaises(QuotaExceededError):
            await self.call_of(task)

    async def test_a_task_with_a_call_in_flight_is_a_running_task(self):
        self.seed_quota(self.user, 0)
        task = self.seed_task(self.user)
        self.seed_usage(self.user, task, started_at=MIDNIGHT, status="in_flight")
        await self.call_of(task)

    async def test_the_exemption_is_for_quotas_only_a_disabled_connection_stops_all(
        self,
    ):
        task = self.seed_task(self.user)
        self.seed_usage(self.user, task, started_at=MIDNIGHT)
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE shared_connections SET enabled = false"))
        with self.assertRaises(ConnectionUnavailableError):
            await self.call_of(task)

    async def test_the_exemption_is_for_the_users_own_running_tasks_only(self):
        stranger = self.seed_user()
        self.seed_quota(stranger, 0)
        task = self.seed_task(stranger)
        self.seed_usage(stranger, task, started_at=MIDNIGHT)
        # The stranger's running task continues; the quota of self.user is not theirs.
        await self.call_of(task, user=stranger)


@requires_postgres
class UnsetQuotaIsUnlimitedTest(QuotaCase):
    """A quota that is not set is not enforced (Decision 0016, section 2, approved).

    The contract used to be the opposite (a user without any quota could not start a
    task, ``QuotaNotConfiguredError``); the human chose "unlimited" on 2026-09-26.
    An explicit limit is still enforced, and ``UNLIMITED`` is still a valid value.
    """

    async def test_a_user_without_quota_rows_may_start_tasks_without_end(self):
        for _ in range(12):
            await self.assertAdmits()
        self.assertEqual(len(self.usage_rows()), 12)

    async def test_the_use_of_an_unset_user_is_attributed_and_audited(self):
        result = await self.new_task_call()
        (row,) = self.usage_rows()
        self.assertEqual((row["id"], row["user_id"]), (result.usage_id, self.user))
        self.assertEqual(
            (row["kind"], row["status"], row["input_tokens"]),
            ("codex", "succeeded", 10),
        )
        self.assertEqual(
            self.audit_actions(), [("agent.use", "allow", "granted_to_resource_owner")]
        )
        self.assertEqual([e for e in self.sink.events if e.decision == "deny"], [])

    async def test_a_quota_of_the_other_kind_does_not_apply(self):
        self.seed_connection(CLAUDE, secret_handle=handle(2))
        self.seed_quota(self.user, 0, kind=CLAUDE)
        await self.assertAdmits(kind=CODEX)  # codex is unset: unlimited
        await self.assertRefuses(
            REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC), kind=CLAUDE
        )

    async def test_removing_the_last_quota_makes_the_user_unlimited_again(self):
        self.seed_quota(self.user, 1)
        await self.assertAdmits()
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))
        await self.service.remove_quota(
            self.principal(self.admin), self.user, CODEX, REQUESTS, DAY
        )
        await self.assertAdmits()

    async def test_explicit_unlimited_is_still_valid_and_is_shown(self):
        await self.service.set_quota(
            self.principal(self.admin), self.user, CODEX, REQUESTS, DAY, UNLIMITED
        )
        await self.assertAdmits()
        (status,) = await self.service.quota_status(
            self.principal(self.user), self.user
        )
        self.assertEqual(
            (status.limit, status.used, status.reached), (UNLIMITED, 1, False)
        )

    async def test_only_the_configured_metrics_and_periods_are_enforced(self):
        # A requests limit says nothing about tokens, and none about the other periods.
        self.seed_quota(self.user, 5, metric="requests", period="day")
        self.seed_usage(self.user, self.old_task(), started_at=MIDNIGHT, tokens=10**9)
        await self.assertAdmits()

    async def test_an_explicit_limit_of_one_user_does_not_limit_another(self):
        stranger = self.seed_user()
        self.seed_quota(self.user, 0)
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))
        await self.assertAdmits(user=stranger)

    async def test_a_limit_that_is_set_later_applies_to_the_next_task_only(self):
        for _ in range(3):
            await self.assertAdmits()
        await self.service.set_quota(
            self.principal(self.admin), self.user, CODEX, REQUESTS, DAY, 3
        )
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))

    async def test_the_refusal_of_an_unset_quota_no_longer_exists(self):
        import paw_backend.connections as connections
        from paw_backend.connections import RefusalReason

        self.assertFalse(hasattr(connections, "QuotaNotConfiguredError"))
        self.assertNotIn("quota_not_configured", [r.value for r in RefusalReason])


@requires_postgres
class TimeZoneTest(QuotaCase):
    zone = "Asia/Tokyo"

    async def test_a_day_ends_at_midnight_in_the_configured_zone(self):
        self.seed_quota(self.user, 1)
        self.clock.now = datetime(2026, 9, 24, 14, 30, tzinfo=UTC)  # 23:30 in Tokyo
        await self.assertAdmits()
        self.clock.now = datetime(2026, 9, 24, 14, 59, tzinfo=UTC)
        await self.assertRefuses(
            REQUESTS, DAY, datetime(2026, 9, 24, 15, 0, tzinfo=UTC)
        )  # midnight in Tokyo
        self.clock.now = datetime(2026, 9, 24, 15, 1, tzinfo=UTC)  # 00:01 in Tokyo
        await self.assertAdmits()

    async def test_the_same_instants_are_one_day_in_utc(self):
        # The control: the UTC service counts 14:30 and 15:01 as the same day.
        utc_service = self.new_service(
            clock=self.clock, allow_explicit_clock=True, period_timezone="UTC"
        )
        self.seed_quota(self.user, 1)
        self.clock.now = datetime(2026, 9, 24, 14, 30, tzinfo=UTC)
        await self.assertAdmits()
        self.clock.now = datetime(2026, 9, 24, 15, 1, tzinfo=UTC)
        self.service = utc_service
        await self.assertRefuses(REQUESTS, DAY, datetime(2026, 9, 25, tzinfo=UTC))

    async def test_a_week_follows_the_local_calendar(self):
        self.seed_quota(self.user, 0, period="week")
        # Sunday 2026-09-27 20:00 UTC is Monday 05:00 in Tokyo.
        self.clock.now = datetime(2026, 9, 27, 20, tzinfo=UTC)
        await self.assertRefuses(
            REQUESTS,
            WEEK,
            datetime(2026, 10, 4, 15, tzinfo=UTC),  # next Monday 00:00 JST
        )

    async def test_a_month_follows_the_local_calendar(self):
        self.seed_quota(self.user, 0, period="month")
        # 2026-09-30 16:00 UTC is October 1st, 01:00 in Tokyo.
        self.clock.now = datetime(2026, 9, 30, 16, tzinfo=UTC)
        await self.assertRefuses(
            REQUESTS,
            MONTH,
            datetime(2026, 10, 31, 15, tzinfo=UTC),  # November 1st, 00:00 JST
        )


@requires_postgres
class DefaultTimeZoneTest(QuotaCase):
    """The calendar periods follow Asia/Tokyo unless another zone is set (Decision
    0016, section 4, approved). T0 is Thursday 2026-09-24 21:00 in Tokyo."""

    zone = None

    async def test_a_day_ends_at_midnight_in_tokyo(self):
        self.seed_quota(self.user, 1)
        await self.assertAdmits()
        await self.assertRefuses(
            REQUESTS,
            DAY,
            datetime(2026, 9, 24, 15, 0, tzinfo=UTC),  # 00:00 JST
        )
        self.clock.now = datetime(2026, 9, 24, 14, 59, tzinfo=UTC)  # 23:59 JST
        await self.assertRefuses(
            REQUESTS, DAY, datetime(2026, 9, 24, 15, 0, tzinfo=UTC)
        )
        self.clock.now = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)  # 00:00 JST
        await self.assertAdmits()

    async def test_a_week_is_the_calendar_week_starting_on_monday_in_tokyo(self):
        self.seed_quota(self.user, 0, period="week")
        # Monday 2026-09-28 00:00 JST is Sunday 2026-09-27 15:00 UTC.
        await self.assertRefuses(
            REQUESTS, WEEK, datetime(2026, 9, 27, 15, 0, tzinfo=UTC)
        )

    async def test_the_calls_of_sunday_night_in_tokyo_belong_to_the_old_week(self):
        self.seed_quota(self.user, 1, period="week")
        # Sunday 2026-09-27 23:30 JST = 14:30 UTC: the week that began on the 21st.
        self.clock.now = datetime(2026, 9, 27, 14, 30, tzinfo=UTC)
        await self.assertAdmits()
        self.clock.now = datetime(2026, 9, 27, 14, 59, tzinfo=UTC)
        await self.assertRefuses(
            REQUESTS, WEEK, datetime(2026, 9, 27, 15, 0, tzinfo=UTC)
        )
        self.clock.now = datetime(2026, 9, 27, 15, 0, tzinfo=UTC)  # Monday 00:00 JST
        await self.assertAdmits()

    async def test_a_month_ends_at_midnight_of_the_last_day_in_tokyo(self):
        self.seed_quota(self.user, 0, period="month")
        await self.assertRefuses(
            REQUESTS,
            MONTH,
            datetime(2026, 9, 30, 15, 0, tzinfo=UTC),  # Oct 1st JST
        )

    async def test_rolling_5h_does_not_depend_on_the_zone(self):
        self.seed_quota(self.user, 0, period="rolling_5h")
        await self.assertRefuses(REQUESTS, ROLLING, None)


if __name__ == "__main__":
    unittest.main()
