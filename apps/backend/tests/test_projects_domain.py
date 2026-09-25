"""The pure rules of the project lifecycle and of membership (PAW-026).

No database, no clock: every instant is written out. Expected values are
computed by hand (see the comments).
"""

import unittest
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

from paw_backend.authz.roles import ProjectRole
from paw_backend.projects import domain
from paw_backend.projects.errors import IllegalTransitionError
from paw_backend.projects.records import (
    InviteState,
    LifecycleAction,
    MemberStatus,
    ProjectStatus,
    TransitionPlan,
)

from .projects_support import member

A, R, P, D = (
    ProjectStatus.ACTIVE,
    ProjectStatus.ARCHIVED,
    ProjectStatus.PENDING_DELETION,
    ProjectStatus.DELETED,
)
ARCHIVE, UNARCHIVE, BEGIN, RESTORE, PURGE = (
    LifecycleAction.ARCHIVE,
    LifecycleAction.UNARCHIVE,
    LifecycleAction.BEGIN_DELETION,
    LifecycleAction.RESTORE,
    LifecycleAction.PURGE,
)
MANAGER, CONTRIBUTOR, VIEWER = (
    ProjectRole.MANAGER,
    ProjectRole.CONTRIBUTOR,
    ProjectRole.VIEWER,
)
JST = timezone(timedelta(hours=9))
US = timedelta(microseconds=1)

# (status, action) -> (new status, changed); None = illegal. The table of the
# docstring of ``plan_transition``, one entry per cell.
TABLE = {
    (A, ARCHIVE): (R, True),
    (A, UNARCHIVE): (A, False),
    (A, BEGIN): (P, True),
    (A, RESTORE): None,
    (A, PURGE): None,
    (R, ARCHIVE): (R, False),
    (R, UNARCHIVE): (A, True),
    (R, BEGIN): (P, True),
    (R, RESTORE): (R, False),
    (R, PURGE): None,
    (P, ARCHIVE): None,
    (P, UNARCHIVE): None,
    (P, BEGIN): (P, False),
    (P, RESTORE): (R, True),
    (P, PURGE): (D, True),
    (D, ARCHIVE): None,
    (D, UNARCHIVE): None,
    (D, BEGIN): None,
    (D, RESTORE): None,
    (D, PURGE): None,
}


class PlanTransitionTest(unittest.TestCase):
    def test_every_cell_of_the_table(self):
        # Every status x action pair, so a cell missing from TABLE is a KeyError.
        for status in ProjectStatus:
            for action in LifecycleAction:
                expected = TABLE[(status, action)]
                with self.subTest(status=status.value, action=action.value):
                    if expected is None:
                        with self.assertRaises(IllegalTransitionError) as caught:
                            domain.plan_transition(status, action)
                        self.assertIs(caught.exception.status, status)
                        self.assertIs(caught.exception.action, action)
                    else:
                        plan = domain.plan_transition(status, action)
                        self.assertEqual(plan, TransitionPlan(*expected))

    def test_starting_the_deletion_again_is_a_repeat_that_changes_nothing(self):
        plan = domain.plan_transition(P, BEGIN)
        self.assertEqual((plan.new_status, plan.changed), (P, False))

    def test_a_repeat_keeps_the_status_and_a_change_moves_it(self):
        legal = [(key, value) for key, value in TABLE.items() if value is not None]
        self.assertEqual(len(legal), 10)  # 20 cells, 10 of them legal
        for (status, action), _ in legal:
            with self.subTest(status=status.value, action=action.value):
                plan = domain.plan_transition(status, action)
                self.assertEqual(plan.changed, plan.new_status is not status)

    def test_restoring_an_active_project_is_illegal_because_it_was_never_deleted(self):
        with self.assertRaises(IllegalTransitionError):
            domain.plan_transition(A, RESTORE)

    def test_a_pending_deletion_project_is_restored_only_to_archived(self):
        self.assertIs(domain.plan_transition(P, RESTORE).new_status, R)
        with self.assertRaises(IllegalTransitionError):
            domain.plan_transition(P, UNARCHIVE)

    def test_a_deleted_project_allows_nothing(self):
        for action in LifecycleAction:
            with self.subTest(action=action.value):
                with self.assertRaises(IllegalTransitionError):
                    domain.plan_transition(D, action)

    def test_only_a_pending_deletion_project_can_be_purged(self):
        for status in (A, R, D):
            with self.subTest(status=status.value):
                with self.assertRaises(IllegalTransitionError):
                    domain.plan_transition(status, PURGE)

    def test_strings_are_not_statuses_or_actions(self):
        for status, action in [
            ("active", ARCHIVE),
            (A, "archive"),
            (None, ARCHIVE),
            (A, None),
        ]:
            with self.subTest(status=status, action=action):
                with self.assertRaises(TypeError):
                    domain.plan_transition(status, action)

    def test_the_error_message_names_only_the_status_and_the_action(self):
        with self.assertRaises(IllegalTransitionError) as caught:
            domain.plan_transition(A, RESTORE)
        self.assertEqual(str(caught.exception), "Cannot restore a active project")


class DeletionScheduleTest(unittest.TestCase):
    def test_it_is_30_times_24_hours_later(self):
        started = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)
        self.assertEqual(
            domain.deletion_schedule(started),
            datetime(2026, 2, 14, 10, 30, tzinfo=UTC),
        )

    def test_it_crosses_a_short_month(self):
        # February 2026 has 28 days: Feb 1 + 27 = Feb 28, + 28 = Mar 1, + 30 = Mar 3.
        self.assertEqual(
            domain.deletion_schedule(datetime(2026, 2, 1, tzinfo=UTC)),
            datetime(2026, 3, 3, tzinfo=UTC),
        )

    def test_it_crosses_a_leap_day_and_keeps_microseconds(self):
        # February 2028 has 29 days: Feb 1 + 28 = Feb 29, + 29 = Mar 1, + 30 = Mar 2.
        self.assertEqual(
            domain.deletion_schedule(datetime(2028, 2, 1, 12, 0, 0, 1, tzinfo=UTC)),
            datetime(2028, 3, 2, 12, 0, 0, 1, tzinfo=UTC),
        )

    def test_it_crosses_a_year_end(self):
        # Dec 20 + 11 = Dec 31, + 12 = Jan 1, + 30 = Jan 19.
        self.assertEqual(
            domain.deletion_schedule(datetime(2026, 12, 20, 23, 59, 59, tzinfo=UTC)),
            datetime(2027, 1, 19, 23, 59, 59, tzinfo=UTC),
        )

    def test_the_result_is_utc_whatever_the_offset_of_the_input(self):
        started = datetime(2026, 1, 15, 19, 30, tzinfo=JST)  # 10:30 UTC
        result = domain.deletion_schedule(started)
        self.assertEqual(result, datetime(2026, 2, 14, 10, 30, tzinfo=UTC))
        self.assertEqual(result.utcoffset(), timedelta(0))
        self.assertEqual(result.hour, 10)

    def test_the_difference_is_exactly_720_hours(self):
        started = datetime(2026, 3, 8, 1, 2, 3, 4, tzinfo=UTC)
        self.assertEqual(
            domain.deletion_schedule(started) - started, timedelta(hours=720)
        )

    def test_naive_and_non_datetimes_are_refused(self):
        with self.assertRaises(ValueError):
            domain.deletion_schedule(datetime(2026, 1, 15, 10, 30))
        for value in ("2026-01-15", None, 5):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    domain.deletion_schedule(value)


class WindowTest(unittest.TestCase):
    SCHEDULED = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

    def test_restore_is_possible_strictly_before_the_deadline(self):
        s = self.SCHEDULED
        cases = [
            (s - timedelta(days=30), True),
            (s - timedelta(days=1), True),
            (s - US, True),
            (s, False),
            (s + US, False),
            (s + timedelta(days=1), False),
        ]
        for now, expected in cases:
            with self.subTest(now=now.isoformat()):
                self.assertIs(domain.restore_window_open(s, now), expected)

    def test_the_purge_is_due_from_the_deadline_on(self):
        s = self.SCHEDULED
        cases = [
            (s - timedelta(days=1), False),
            (s - US, False),
            (s, True),
            (s + US, True),
            (s + timedelta(days=1), True),
        ]
        for now, expected in cases:
            with self.subTest(now=now.isoformat()):
                self.assertIs(domain.purge_due(s, now), expected)

    def test_at_every_instant_exactly_one_of_restore_and_purge_applies(self):
        s = self.SCHEDULED
        for delta in (-timedelta(days=30), -US, timedelta(0), US, timedelta(days=90)):
            with self.subTest(delta=delta):
                self.assertNotEqual(
                    domain.restore_window_open(s, s + delta),
                    domain.purge_due(s, s + delta),
                )

    def test_offsets_are_compared_as_instants(self):
        s = self.SCHEDULED  # 12:00 UTC == 21:00 JST
        same_instant = datetime(2026, 10, 24, 21, 0, tzinfo=JST)
        self.assertIs(domain.restore_window_open(s, same_instant), False)
        self.assertIs(domain.purge_due(s, same_instant), True)
        just_before = same_instant - US
        self.assertIs(domain.restore_window_open(s, just_before), True)
        self.assertIs(domain.purge_due(s, just_before), False)
        self.assertIs(domain.restore_window_open(same_instant, s), False)

    def test_naive_datetimes_are_refused(self):
        naive = datetime(2026, 10, 24, 12, 0)
        for function in (domain.restore_window_open, domain.purge_due):
            with self.subTest(function=function.__name__):
                with self.assertRaises(ValueError):
                    function(naive, self.SCHEDULED)
                with self.assertRaises(ValueError):
                    function(self.SCHEDULED, naive)

    def test_non_datetimes_are_refused(self):
        for function in (domain.restore_window_open, domain.purge_due):
            with self.subTest(function=function.__name__):
                with self.assertRaises(TypeError):
                    function("2026-10-24", self.SCHEDULED)
                with self.assertRaises(TypeError):
                    function(self.SCHEDULED, None)


class InviteExpiryTest(unittest.TestCase):
    def test_it_is_14_times_24_hours_later(self):
        self.assertEqual(
            domain.invite_expiry(datetime(2026, 3, 1, tzinfo=UTC)),
            datetime(2026, 3, 15, tzinfo=UTC),
        )

    def test_it_crosses_a_month_end(self):
        # Jan 25 + 6 = Jan 31, + 7 = Feb 1, + 14 = Feb 8.
        self.assertEqual(
            domain.invite_expiry(datetime(2026, 1, 25, 8, 15, 30, 7, tzinfo=UTC)),
            datetime(2026, 2, 8, 8, 15, 30, 7, tzinfo=UTC),
        )

    def test_the_result_is_utc_whatever_the_offset_of_the_input(self):
        # 01:00 +09:00 on Mar 1 is 16:00 UTC on Feb 28; + 14 days = Mar 14 16:00 UTC.
        result = domain.invite_expiry(datetime(2026, 3, 1, 1, 0, tzinfo=JST))
        self.assertEqual(result, datetime(2026, 3, 14, 16, 0, tzinfo=UTC))
        self.assertEqual(result.utcoffset(), timedelta(0))

    def test_naive_and_non_datetimes_are_refused(self):
        with self.assertRaises(ValueError):
            domain.invite_expiry(datetime(2026, 3, 1))
        with self.assertRaises(TypeError):
            domain.invite_expiry("2026-03-01")


class InviteStateTest(unittest.TestCase):
    NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

    def test_no_row_is_none(self):
        self.assertIs(domain.invite_state(None, self.NOW), InviteState.NONE)

    def test_an_accepted_member_is_a_member_whatever_the_role(self):
        for role in ProjectRole:
            with self.subTest(role=role.value):
                row = member(role, MemberStatus.ACTIVE, invited_at=self.NOW)
                self.assertIs(domain.invite_state(row, self.NOW), InviteState.MEMBER)

    def test_an_invitation_is_open_strictly_before_its_expiry(self):
        expires = self.NOW + timedelta(days=1)
        row = member(
            VIEWER, MemberStatus.INVITED, invited_at=self.NOW, expires_at=expires
        )
        cases = [
            (self.NOW, InviteState.OPEN),
            (expires - timedelta(days=1) + US, InviteState.OPEN),
            (expires - US, InviteState.OPEN),
            (expires, InviteState.EXPIRED),
            (expires + US, InviteState.EXPIRED),
            (expires + timedelta(days=400), InviteState.EXPIRED),
        ]
        for now, expected in cases:
            with self.subTest(now=now.isoformat()):
                self.assertIs(domain.invite_state(row, now), expected)

    def test_an_invitation_of_any_role_can_be_open(self):
        for role in ProjectRole:
            with self.subTest(role=role.value):
                row = member(role, MemberStatus.INVITED, invited_at=self.NOW)
                self.assertIs(domain.invite_state(row, self.NOW), InviteState.OPEN)

    def test_offsets_are_compared_as_instants(self):
        expires = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
        row = member(
            VIEWER, MemberStatus.INVITED, invited_at=self.NOW, expires_at=expires
        )
        same_instant = datetime(2026, 9, 25, 21, 0, tzinfo=JST)
        self.assertIs(domain.invite_state(row, same_instant), InviteState.EXPIRED)
        self.assertIs(domain.invite_state(row, same_instant - US), InviteState.OPEN)

    def test_a_naive_now_is_refused(self):
        with self.assertRaises(ValueError):
            domain.invite_state(None, datetime(2026, 9, 24, 12, 0))

    def test_wrong_types_are_refused(self):
        with self.assertRaises(TypeError):
            domain.invite_state({"status": "active"}, self.NOW)
        with self.assertRaises(TypeError):
            domain.invite_state(None, "2026-09-24")


def people(*roles_and_status):
    """Members with fresh ids: ``people((MANAGER, ACTIVE), (VIEWER, INVITED))``."""
    return [member(role, status) for role, status in roles_and_status]


ACTIVE, INVITED = MemberStatus.ACTIVE, MemberStatus.INVITED


class ManagerWouldRemainTest(unittest.TestCase):
    def test_removing_the_only_manager_leaves_none(self):
        a, b = people((MANAGER, ACTIVE), (CONTRIBUTOR, ACTIVE))
        self.assertIs(domain.manager_would_remain([a, b], a.user_id, None), False)

    def test_removing_someone_else_keeps_the_manager(self):
        a, b = people((MANAGER, ACTIVE), (CONTRIBUTOR, ACTIVE))
        self.assertIs(domain.manager_would_remain([a, b], b.user_id, None), True)

    def test_demoting_the_only_manager_leaves_none(self):
        a, b = people((MANAGER, ACTIVE), (CONTRIBUTOR, ACTIVE))
        for role in (CONTRIBUTOR, VIEWER):
            with self.subTest(role=role.value):
                self.assertIs(
                    domain.manager_would_remain([a, b], a.user_id, role), False
                )

    def test_keeping_the_manager_role_changes_nothing(self):
        a, b = people((MANAGER, ACTIVE), (CONTRIBUTOR, ACTIVE))
        self.assertIs(domain.manager_would_remain([a, b], a.user_id, MANAGER), True)

    def test_promoting_someone_keeps_a_manager(self):
        a, b = people((MANAGER, ACTIVE), (CONTRIBUTOR, ACTIVE))
        self.assertIs(domain.manager_would_remain([a, b], b.user_id, MANAGER), True)

    def test_with_two_managers_one_can_go_or_be_demoted(self):
        a, b = people((MANAGER, ACTIVE), (MANAGER, ACTIVE))
        self.assertIs(domain.manager_would_remain([a, b], a.user_id, None), True)
        self.assertIs(domain.manager_would_remain([a, b], b.user_id, VIEWER), True)

    def test_an_invited_manager_never_counts(self):
        a, b = people((MANAGER, ACTIVE), (MANAGER, INVITED))
        self.assertIs(domain.manager_would_remain([a, b], a.user_id, None), False)
        self.assertIs(domain.manager_would_remain([a, b], a.user_id, VIEWER), False)

    def test_an_invitation_is_never_changed(self):
        # The invitation of ``b`` is not an active member: removing / re-roling
        # it does not touch the active Manager ``a``, and it does not count.
        a, b = people((MANAGER, ACTIVE), (VIEWER, INVITED))
        self.assertIs(domain.manager_would_remain([a, b], b.user_id, None), True)
        self.assertIs(domain.manager_would_remain([a, b], b.user_id, MANAGER), True)
        only_invited = people((MANAGER, INVITED))
        self.assertIs(
            domain.manager_would_remain(only_invited, only_invited[0].user_id, MANAGER),
            False,
        )

    def test_an_unknown_user_changes_nothing(self):
        (a,) = people((MANAGER, ACTIVE))
        self.assertIs(domain.manager_would_remain([a], uuid4(), None), True)
        self.assertIs(domain.manager_would_remain([a], uuid4(), VIEWER), True)

    def test_without_any_active_manager_the_answer_is_false(self):
        self.assertIs(domain.manager_would_remain([], uuid4(), None), False)
        self.assertIs(domain.manager_would_remain([], uuid4(), MANAGER), False)
        c, v = people((CONTRIBUTOR, ACTIVE), (VIEWER, ACTIVE))
        self.assertIs(domain.manager_would_remain([c, v], c.user_id, None), False)

    def test_promoting_an_unknown_user_does_not_create_a_manager(self):
        c, v = people((CONTRIBUTOR, ACTIVE), (VIEWER, ACTIVE))
        self.assertIs(domain.manager_would_remain([c, v], uuid4(), MANAGER), False)

    def test_a_tuple_works_and_the_input_is_not_modified(self):
        members = tuple(people((MANAGER, ACTIVE), (MANAGER, ACTIVE), (VIEWER, ACTIVE)))
        before = list(members)
        first = members[0]
        self.assertIs(domain.manager_would_remain(members, first.user_id, None), True)
        self.assertEqual(list(members), before)
        listed = list(members)
        domain.manager_would_remain(listed, first.user_id, VIEWER)
        self.assertEqual(listed, before)

    def test_wrong_types_are_refused(self):
        (a,) = people((MANAGER, ACTIVE))
        with self.assertRaises(TypeError):
            domain.manager_would_remain([a], str(a.user_id), None)
        with self.assertRaises(TypeError):
            domain.manager_would_remain([a], a.user_id, "viewer")
        with self.assertRaises(TypeError):
            domain.manager_would_remain([a], None, None)

    def test_the_result_is_a_bool(self):
        (a,) = people((MANAGER, ACTIVE))
        self.assertIsInstance(domain.manager_would_remain([a], a.user_id, None), bool)
        self.assertIsInstance(domain.manager_would_remain([a], uuid4(), None), bool)


if __name__ == "__main__":
    unittest.main()
