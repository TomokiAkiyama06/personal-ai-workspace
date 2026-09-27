"""The retention / partitioning rules of ``audit_events`` (Issue #86, Decision 0027).

No database, no clock. Every expected value is worked out by hand from the
docstrings of the functions in ``paw_backend.authz.retention.rules``.
"""

import unittest
from datetime import UTC, datetime, timedelta, timezone

from paw_backend.authz.retention.records import (
    PartitionStatus,
    PartitionWindow,
    RetentionPolicy,
    default_policy,
)
from paw_backend.authz.retention.rules import (
    month_start,
    next_month_start,
    partition_name,
    partitions_due_for_archive,
    partitions_due_for_purge,
    plan_missing_partitions,
)

LIVE = PartitionStatus.LIVE
ARCHIVED = PartitionStatus.ARCHIVED
PURGED = PartitionStatus.PURGED


def at(year, month, day=1, hour=0, minute=0, second=0, *, tz=UTC) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=tz)


class MonthStartTest(unittest.TestCase):
    def test_the_start_of_the_month_containing_a_mid_month_moment(self):
        self.assertEqual(month_start(at(2026, 9, 26, 22, 10)), at(2026, 9, 1))

    def test_the_first_of_the_month_at_midnight_is_its_own_start(self):
        self.assertEqual(month_start(at(2026, 9, 1)), at(2026, 9, 1))

    def test_the_last_moment_of_the_month_is_still_that_month(self):
        self.assertEqual(month_start(at(2026, 9, 30, 23, 59, 59)), at(2026, 9, 1))

    def test_a_non_utc_timezone_is_normalised_to_utc_first(self):
        # 2026-01-01 00:30 in UTC+1 is 2025-12-31 23:30 UTC: December, not January.
        tz = timezone(timedelta(hours=1))
        self.assertEqual(month_start(at(2026, 1, 1, 0, 30, tz=tz)), at(2025, 12, 1))

    def test_a_naive_datetime_is_rejected(self):
        with self.assertRaises(ValueError):
            month_start(datetime(2026, 9, 26))

    def test_not_a_datetime_at_all_raises_type_error(self):
        with self.assertRaises(TypeError):
            month_start("2026-09-26")


class NextMonthStartTest(unittest.TestCase):
    def test_the_ordinary_case(self):
        self.assertEqual(next_month_start(at(2026, 9, 26)), at(2026, 10, 1))

    def test_year_rollover(self):
        self.assertEqual(next_month_start(at(2026, 12, 15)), at(2027, 1, 1))

    def test_starting_from_a_month_start_gives_the_next_one(self):
        self.assertEqual(next_month_start(at(2026, 9, 1)), at(2026, 10, 1))


class PartitionNameTest(unittest.TestCase):
    def test_the_name_of_a_month(self):
        self.assertEqual(partition_name(at(2026, 9, 1)), "audit_events_p2026_09")

    def test_a_single_digit_month_is_zero_padded(self):
        self.assertEqual(partition_name(at(2026, 1, 1)), "audit_events_p2026_01")

    def test_not_a_month_start_is_rejected(self):
        with self.assertRaises(ValueError):
            partition_name(at(2026, 9, 26))

    def test_midnight_but_not_the_first_is_rejected(self):
        with self.assertRaises(ValueError):
            partition_name(at(2026, 9, 2))


class RetentionPolicyTest(unittest.TestCase):
    def test_the_default_policy(self):
        policy = default_policy()
        self.assertEqual(policy.archive_after_days, 180)
        self.assertIsNone(policy.purge_after_days)
        self.assertEqual(policy.horizon_months, 3)

    def test_negative_archive_after_days_is_rejected(self):
        with self.assertRaises(ValueError):
            RetentionPolicy(
                archive_after_days=-1, purge_after_days=None, horizon_months=1
            )

    def test_zero_archive_after_days_is_allowed(self):
        RetentionPolicy(archive_after_days=0, purge_after_days=None, horizon_months=1)

    def test_purge_before_archive_is_rejected(self):
        with self.assertRaises(ValueError):
            RetentionPolicy(archive_after_days=10, purge_after_days=9, horizon_months=1)

    def test_purge_equal_to_archive_is_allowed(self):
        RetentionPolicy(archive_after_days=10, purge_after_days=10, horizon_months=1)

    def test_horizon_months_must_be_at_least_one(self):
        with self.assertRaises(ValueError):
            RetentionPolicy(
                archive_after_days=0, purge_after_days=None, horizon_months=0
            )

    def test_a_bool_is_not_an_int_here(self):
        with self.assertRaises(TypeError):
            RetentionPolicy(
                archive_after_days=True, purge_after_days=None, horizon_months=1
            )

    def test_a_float_is_rejected(self):
        with self.assertRaises(TypeError):
            RetentionPolicy(
                archive_after_days=1.5, purge_after_days=None, horizon_months=1
            )

    def test_the_policy_is_frozen(self):
        policy = default_policy()
        with self.assertRaises(AttributeError):
            policy.archive_after_days = 1  # type: ignore[misc]


class PlanMissingPartitionsTest(unittest.TestCase):
    def test_nothing_existing_plans_this_month_and_the_horizon(self):
        policy = RetentionPolicy(
            archive_after_days=180, purge_after_days=None, horizon_months=3
        )
        planned = plan_missing_partitions(at(2026, 9, 26), [], policy)
        self.assertEqual(
            [window.name for window in planned],
            [
                "audit_events_p2026_09",
                "audit_events_p2026_10",
                "audit_events_p2026_11",
                "audit_events_p2026_12",
            ],
        )
        self.assertEqual(planned[0].lower, at(2026, 9, 1))
        self.assertEqual(planned[0].upper, at(2026, 10, 1))
        self.assertTrue(all(window.status == LIVE for window in planned))

    def test_year_rollover_is_planned_correctly(self):
        policy = RetentionPolicy(
            archive_after_days=0, purge_after_days=None, horizon_months=2
        )
        planned = plan_missing_partitions(at(2026, 11, 20), [], policy)
        self.assertEqual(
            [window.name for window in planned],
            ["audit_events_p2026_11", "audit_events_p2026_12", "audit_events_p2027_01"],
        )

    def test_an_existing_name_is_never_planned_again(self):
        policy = RetentionPolicy(
            archive_after_days=0, purge_after_days=None, horizon_months=1
        )
        existing = [
            PartitionWindow(
                "audit_events_p2026_09", at(2026, 9, 1), at(2026, 10, 1), LIVE
            )
        ]
        planned = plan_missing_partitions(at(2026, 9, 26), existing, policy)
        self.assertEqual([window.name for window in planned], ["audit_events_p2026_10"])

    def test_an_existing_name_of_any_status_still_counts_as_present(self):
        # Even archived or purged: this planner only ever fills a genuine gap,
        # never recreates a partition that already existed under this name
        # (October is still planned regardless: horizon_months=1 always wants
        # it, whatever September's own status is).
        policy = RetentionPolicy(
            archive_after_days=0, purge_after_days=None, horizon_months=1
        )
        for status in (LIVE, ARCHIVED, PURGED):
            with self.subTest(status=status.value):
                existing = [
                    PartitionWindow(
                        "audit_events_p2026_09", at(2026, 9, 1), at(2026, 10, 1), status
                    )
                ]
                planned = plan_missing_partitions(at(2026, 9, 26), existing, policy)
                self.assertEqual(
                    [window.name for window in planned], ["audit_events_p2026_10"]
                )

    def test_calling_it_again_with_the_previous_result_plans_nothing_more(self):
        policy = RetentionPolicy(
            archive_after_days=0, purge_after_days=None, horizon_months=2
        )
        first = plan_missing_partitions(at(2026, 9, 26), [], policy)
        second = plan_missing_partitions(at(2026, 9, 26), first, policy)
        self.assertEqual(second, [])

    def test_a_naive_now_is_rejected(self):
        policy = default_policy()
        with self.assertRaises(ValueError):
            plan_missing_partitions(datetime(2026, 9, 26), [], policy)


class PartitionsDueForArchiveTest(unittest.TestCase):
    def window(self, upper, status=LIVE, name="audit_events_p2026_01"):
        return PartitionWindow(name, at(2026, 1, 1), upper, status)

    def test_a_window_that_ended_long_enough_ago_is_due(self):
        policy = RetentionPolicy(
            archive_after_days=30, purge_after_days=None, horizon_months=1
        )
        now = at(2026, 6, 1)
        due = partitions_due_for_archive(now, [self.window(at(2026, 2, 1))], policy)
        self.assertEqual([window.name for window in due], ["audit_events_p2026_01"])

    def test_exactly_at_the_threshold_is_due(self):
        policy = RetentionPolicy(
            archive_after_days=30, purge_after_days=None, horizon_months=1
        )
        upper = at(2026, 2, 1)
        now = upper + timedelta(days=30)
        due = partitions_due_for_archive(now, [self.window(upper)], policy)
        self.assertEqual(len(due), 1)

    def test_one_day_before_the_threshold_is_not_due(self):
        policy = RetentionPolicy(
            archive_after_days=30, purge_after_days=None, horizon_months=1
        )
        upper = at(2026, 2, 1)
        now = upper + timedelta(days=29)
        due = partitions_due_for_archive(now, [self.window(upper)], policy)
        self.assertEqual(due, [])

    def test_the_partition_still_receiving_inserts_is_never_due(self):
        policy = RetentionPolicy(
            archive_after_days=0, purge_after_days=None, horizon_months=1
        )
        now = at(2026, 9, 26)
        current = PartitionWindow(
            "audit_events_p2026_09", at(2026, 9, 1), at(2026, 10, 1), LIVE
        )
        self.assertEqual(partitions_due_for_archive(now, [current], policy), [])

    def test_only_live_partitions_are_considered(self):
        policy = RetentionPolicy(
            archive_after_days=0, purge_after_days=None, horizon_months=1
        )
        now = at(2026, 6, 1)
        for status in (ARCHIVED, PURGED):
            with self.subTest(status=status.value):
                due = partitions_due_for_archive(
                    now, [self.window(at(2026, 2, 1), status)], policy
                )
                self.assertEqual(due, [])

    def test_results_are_sorted_oldest_first_regardless_of_input_order(self):
        policy = RetentionPolicy(
            archive_after_days=0, purge_after_days=None, horizon_months=1
        )
        now = at(2026, 6, 1)
        newer = self.window(at(2026, 3, 1), name="audit_events_p2026_02")
        older = self.window(at(2026, 2, 1), name="audit_events_p2026_01")
        due = partitions_due_for_archive(now, [newer, older], policy)
        self.assertEqual(
            [w.name for w in due], ["audit_events_p2026_01", "audit_events_p2026_02"]
        )


class PartitionsDueForPurgeTest(unittest.TestCase):
    def window(self, upper, status=ARCHIVED, name="audit_events_p2026_01"):
        return PartitionWindow(name, at(2026, 1, 1), upper, status)

    def test_disabled_by_default_purge_after_days_none(self):
        policy = default_policy()
        now = at(2030, 1, 1)
        due = partitions_due_for_purge(now, [self.window(at(2026, 2, 1))], policy)
        self.assertEqual(due, [])

    def test_an_archived_window_old_enough_is_due_when_enabled(self):
        policy = RetentionPolicy(
            archive_after_days=0, purge_after_days=365, horizon_months=1
        )
        now = at(2027, 6, 1)
        due = partitions_due_for_purge(now, [self.window(at(2026, 2, 1))], policy)
        self.assertEqual([w.name for w in due], ["audit_events_p2026_01"])

    def test_only_archived_partitions_are_considered(self):
        policy = RetentionPolicy(
            archive_after_days=0, purge_after_days=0, horizon_months=1
        )
        now = at(2026, 6, 1)
        for status in (LIVE, PURGED):
            with self.subTest(status=status.value):
                due = partitions_due_for_purge(
                    now, [self.window(at(2026, 2, 1), status)], policy
                )
                self.assertEqual(due, [])

    def test_not_old_enough_yet_is_not_due(self):
        policy = RetentionPolicy(
            archive_after_days=0, purge_after_days=365, horizon_months=1
        )
        now = at(2026, 6, 1)
        due = partitions_due_for_purge(now, [self.window(at(2026, 2, 1))], policy)
        self.assertEqual(due, [])
