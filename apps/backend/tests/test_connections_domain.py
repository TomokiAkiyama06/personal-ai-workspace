"""The closed vocabularies and the window rules of the connection module."""

import unittest
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from paw_backend.connections.domain import (
    UNLIMITED,
    ConnectionKind,
    ConnectionStatus,
    FailureCode,
    QuotaMetric,
    QuotaPeriod,
    RefusalReason,
    Unlimited,
    UsagePurpose,
    UsageStatus,
    window_end,
    window_start,
)

TOKYO = ZoneInfo("Asia/Tokyo")
NEW_YORK = ZoneInfo("America/New_York")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class VocabularyTest(unittest.TestCase):
    """The strings below are written to the database and to the audit log: a rename
    is a migration and a Decision, so the values are pinned here one by one."""

    def values(self, enum) -> list[str]:
        return [member.value for member in enum]

    def test_the_kinds_are_codex_then_claude(self):
        self.assertEqual(self.values(ConnectionKind), ["codex", "claude"])

    def test_the_statuses_of_a_connection(self):
        self.assertEqual(
            self.values(ConnectionStatus), ["connected", "unavailable", "expired"]
        )

    def test_the_usage_purposes_are_a_closed_category_set(self):
        self.assertEqual(
            self.values(UsagePurpose),
            ["chat", "coding", "review", "research", "evaluation", "other"],
        )

    def test_the_states_of_a_usage_record(self):
        self.assertEqual(
            self.values(UsageStatus),
            ["in_flight", "succeeded", "failed", "cancelled"],
        )

    def test_the_failure_codes(self):
        self.assertEqual(
            self.values(FailureCode),
            [
                "rate_limited",
                "unavailable",
                "expired",
                "timeout",
                "invalid_response",
                "internal_error",
            ],
        )

    def test_the_quota_metrics_in_check_order(self):
        self.assertEqual(
            self.values(QuotaMetric), ["requests", "tasks", "tokens", "runtime_seconds"]
        )

    def test_the_quota_periods_in_check_order(self):
        self.assertEqual(
            self.values(QuotaPeriod), ["rolling_5h", "day", "week", "month"]
        )

    def test_the_refusal_reasons_are_the_audit_reasons(self):
        self.assertEqual(
            self.values(RefusalReason),
            [
                "task_not_found",
                "task_ended",
                "task_superseded",
                "connection_unavailable",
                "quota_not_configured",
                "quota_exceeded",
                "task_budget_exceeded",
                "task_budget_not_configured",
            ],
        )

    def test_unlimited_is_its_own_type_with_one_member(self):
        self.assertEqual([m.value for m in Unlimited], ["unlimited"])
        self.assertIs(UNLIMITED, Unlimited.UNLIMITED)
        self.assertNotEqual(UNLIMITED, 0)
        self.assertNotEqual(UNLIMITED, None)

    def test_every_reason_fits_the_audit_column(self):
        # AuditEvent.reason is limited to 64 characters.
        for reason in RefusalReason:
            self.assertLessEqual(len(reason.value), 64)


class RollingWindowTest(unittest.TestCase):
    def test_a_rolling_window_is_the_last_five_hours(self):
        now = utc(2026, 9, 24, 12, 30, 15)
        self.assertEqual(
            window_start(QuotaPeriod.ROLLING_5H, now), utc(2026, 9, 24, 7, 30, 15)
        )

    def test_a_rolling_window_has_no_end(self):
        self.assertIsNone(window_end(QuotaPeriod.ROLLING_5H, utc(2026, 9, 24, 12)))

    def test_the_rolling_window_ignores_the_zone(self):
        now = utc(2026, 9, 24, 12)
        self.assertEqual(
            window_start(QuotaPeriod.ROLLING_5H, now, TOKYO),
            window_start(QuotaPeriod.ROLLING_5H, now, UTC),
        )


class CalendarWindowTest(unittest.TestCase):
    def test_a_day_starts_at_midnight_and_ends_at_the_next(self):
        now = utc(2026, 9, 24, 12, 30)
        self.assertEqual(window_start(QuotaPeriod.DAY, now), utc(2026, 9, 24))
        self.assertEqual(window_end(QuotaPeriod.DAY, now), utc(2026, 9, 25))

    def test_midnight_itself_belongs_to_the_new_day(self):
        now = utc(2026, 9, 25)
        self.assertEqual(window_start(QuotaPeriod.DAY, now), utc(2026, 9, 25))
        self.assertEqual(window_end(QuotaPeriod.DAY, now), utc(2026, 9, 26))

    def test_the_last_instant_of_a_day_still_belongs_to_it(self):
        now = utc(2026, 9, 24, 23, 59, 59) + timedelta(microseconds=999999)
        self.assertEqual(window_start(QuotaPeriod.DAY, now), utc(2026, 9, 24))
        self.assertEqual(window_end(QuotaPeriod.DAY, now), utc(2026, 9, 25))

    def test_a_week_starts_on_monday(self):
        # 2026-09-24 is a Thursday.
        now = utc(2026, 9, 24, 12)
        self.assertEqual(now.weekday(), 3)
        self.assertEqual(window_start(QuotaPeriod.WEEK, now), utc(2026, 9, 21))
        self.assertEqual(window_end(QuotaPeriod.WEEK, now), utc(2026, 9, 28))

    def test_monday_midnight_starts_a_new_week_and_sunday_belongs_to_the_old_one(self):
        monday = utc(2026, 9, 21)
        self.assertEqual(window_start(QuotaPeriod.WEEK, monday), monday)
        sunday = utc(2026, 9, 27, 23, 59)
        self.assertEqual(window_start(QuotaPeriod.WEEK, sunday), monday)
        self.assertEqual(window_end(QuotaPeriod.WEEK, sunday), utc(2026, 9, 28))

    def test_a_month_starts_on_the_first(self):
        now = utc(2026, 9, 24, 12)
        self.assertEqual(window_start(QuotaPeriod.MONTH, now), utc(2026, 9, 1))
        self.assertEqual(window_end(QuotaPeriod.MONTH, now), utc(2026, 10, 1))

    def test_december_ends_in_january_of_the_next_year(self):
        now = utc(2026, 12, 31, 23, 59)
        self.assertEqual(window_start(QuotaPeriod.MONTH, now), utc(2026, 12, 1))
        self.assertEqual(window_end(QuotaPeriod.MONTH, now), utc(2027, 1, 1))

    def test_february_of_a_leap_year_has_29_days(self):
        now = utc(2028, 2, 10)
        self.assertEqual(
            window_end(QuotaPeriod.MONTH, now) - window_start(QuotaPeriod.MONTH, now),
            timedelta(days=29),
        )

    def test_the_first_instant_of_a_month_belongs_to_it(self):
        now = utc(2026, 10, 1)
        self.assertEqual(window_start(QuotaPeriod.MONTH, now), utc(2026, 10, 1))

    def test_the_window_always_contains_now(self):
        for period in (QuotaPeriod.DAY, QuotaPeriod.WEEK, QuotaPeriod.MONTH):
            for zone in (UTC, TOKYO, NEW_YORK):
                for now in (
                    utc(2026, 1, 1),
                    utc(2026, 3, 8, 7, 30),
                    utc(2026, 9, 24, 15, 0),
                    utc(2026, 11, 1, 6, 30),
                    utc(2026, 12, 31, 23, 59, 59),
                ):
                    with self.subTest(period=period.value, zone=str(zone), now=now):
                        start = window_start(period, now, zone)
                        end = window_end(period, now, zone)
                        self.assertLessEqual(start, now)
                        self.assertLess(now, end)


class TimeZoneTest(unittest.TestCase):
    def test_a_day_in_tokyo_starts_at_15_utc_of_the_day_before(self):
        # 2026-09-24 09:00 in Tokyo is 00:00 UTC: it belongs to the Tokyo day
        # that started at 2026-09-23 15:00 UTC.
        now = utc(2026, 9, 24, 0, 0)
        self.assertEqual(
            window_start(QuotaPeriod.DAY, now, TOKYO), utc(2026, 9, 23, 15)
        )
        self.assertEqual(window_end(QuotaPeriod.DAY, now, TOKYO), utc(2026, 9, 24, 15))

    def test_the_same_instant_is_a_different_day_in_another_zone(self):
        now = utc(2026, 9, 24, 16, 0)  # 01:00 on the 25th in Tokyo
        self.assertEqual(window_start(QuotaPeriod.DAY, now, UTC), utc(2026, 9, 24))
        self.assertEqual(
            window_start(QuotaPeriod.DAY, now, TOKYO), utc(2026, 9, 24, 15)
        )

    def test_a_week_and_a_month_follow_the_local_calendar(self):
        # Sunday 2026-09-27 20:00 UTC is Monday 05:00 in Tokyo: a new week there.
        now = utc(2026, 9, 27, 20)
        self.assertEqual(window_start(QuotaPeriod.WEEK, now, UTC), utc(2026, 9, 21))
        self.assertEqual(
            window_start(QuotaPeriod.WEEK, now, TOKYO), utc(2026, 9, 27, 15)
        )
        # 2026-09-30 16:00 UTC is October 1st, 01:00 in Tokyo.
        now = utc(2026, 9, 30, 16)
        self.assertEqual(window_start(QuotaPeriod.MONTH, now, UTC), utc(2026, 9, 1))
        self.assertEqual(
            window_start(QuotaPeriod.MONTH, now, TOKYO), utc(2026, 9, 30, 15)
        )

    def test_a_day_is_23_hours_when_the_clocks_go_forward(self):
        now = utc(2026, 3, 8, 12)  # New York, spring forward on 2026-03-08
        length = window_end(QuotaPeriod.DAY, now, NEW_YORK) - window_start(
            QuotaPeriod.DAY, now, NEW_YORK
        )
        self.assertEqual(length, timedelta(hours=23))

    def test_a_day_is_25_hours_when_the_clocks_go_back(self):
        now = utc(2026, 11, 1, 12)  # New York, fall back on 2026-11-01
        length = window_end(QuotaPeriod.DAY, now, NEW_YORK) - window_start(
            QuotaPeriod.DAY, now, NEW_YORK
        )
        self.assertEqual(length, timedelta(hours=25))

    def test_a_naive_instant_is_refused(self):
        for period in QuotaPeriod:
            with self.subTest(period=period.value):
                with self.assertRaises(ValueError):
                    window_start(period, datetime(2026, 9, 24, 12))
                with self.assertRaises(ValueError):
                    window_end(period, datetime(2026, 9, 24, 12))

    def test_a_non_datetime_is_refused(self):
        with self.assertRaises(ValueError):
            window_start(QuotaPeriod.DAY, "2026-09-24")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
