"""Validation of caller input, and the records (implemented by the spec author)."""

import unittest
import uuid
from datetime import UTC, datetime, timedelta, timezone, tzinfo

from paw_backend.authz.roles import ProjectRole, SystemRole
from paw_backend.projects import (
    InputProblem,
    InvalidProjectInputError,
    Member,
    MemberStatus,
    ProjectStatus,
    limits,
)
from paw_backend.projects import validation as v

from .projects_support import T0, member

SECRET = "hunter2-secret-connection-detail"


class ProblemTestCase(unittest.TestCase):
    def assertProblem(self, problem: InputProblem, function, *args, **kwargs):
        with self.assertRaises(InvalidProjectInputError) as caught:
            function(*args, **kwargs)
        self.assertIs(caught.exception.problem, problem)
        return caught.exception


class ValidateUuidTest(ProblemTestCase):
    def test_a_uuid_and_its_canonical_string_are_accepted(self):
        value = uuid.uuid4()
        self.assertIs(v.validate_uuid("project_id", value), value)
        self.assertEqual(v.validate_uuid("project_id", str(value)), value)

    def test_every_other_spelling_and_type_is_refused(self):
        value = uuid.uuid4()
        for bad in [
            str(value).upper(),
            value.hex,
            "{" + str(value) + "}",
            "urn:uuid:" + str(value),
            " " + str(value),
            str(value) + "\n",
            "",
            "not-a-uuid",
            None,
            5,
            value.bytes,
            [str(value)],
        ]:
            with self.subTest(bad=repr(bad)):
                self.assertProblem(InputProblem.NOT_A_UUID, v.validate_uuid, "x", bad)

    def test_the_error_names_the_field_and_never_the_value(self):
        error = self.assertProblem(
            InputProblem.NOT_A_UUID, v.validate_uuid, "user_id", SECRET
        )
        self.assertEqual(error.field, "user_id")
        self.assertEqual(str(error), "Invalid user_id: not_a_uuid")
        self.assertNotIn(SECRET, str(error) + repr(error))
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)


class ValidateNameTest(ProblemTestCase):
    def test_a_plain_name_is_returned_unchanged(self):
        self.assertEqual(v.validate_name("Alpha"), "Alpha")
        self.assertEqual(
            v.validate_name("Project 1 (v2) - 日本語 é 🚀"),
            "Project 1 (v2) - 日本語 é 🚀",
        )

    def test_outer_whitespace_is_stripped_and_inner_whitespace_kept(self):
        self.assertEqual(v.validate_name("  Alpha  Beta  "), "Alpha  Beta")
        self.assertEqual(v.validate_name("　Alpha　"), "Alpha")

    def test_the_length_limit_is_100_characters_after_stripping(self):
        self.assertEqual(v.validate_name("a" * 100), "a" * 100)
        self.assertProblem(InputProblem.TOO_LONG, v.validate_name, "a" * 101)
        self.assertEqual(v.validate_name("  " + "a" * 100 + "  "), "a" * 100)
        # Characters are counted, not bytes: 100 four-byte characters fit.
        self.assertEqual(v.validate_name("🚀" * 100), "🚀" * 100)
        self.assertProblem(InputProblem.TOO_LONG, v.validate_name, "🚀" * 101)

    def test_a_huge_string_is_refused_before_it_is_scanned(self):
        self.assertProblem(
            InputProblem.TOO_LONG,
            v.validate_name,
            "a" * (limits.MAX_NAME_CHARS * limits.RAW_TEXT_FACTOR + 1),
        )

    def test_blank_names_are_refused(self):
        for bad in ["", " ", "   ", " ", "　　"]:
            with self.subTest(bad=repr(bad)):
                self.assertProblem(InputProblem.EMPTY, v.validate_name, bad)

    def test_non_strings_are_refused_and_nothing_is_coerced(self):
        for bad in [None, 5, 1.5, True, b"Alpha", ["Alpha"], {"a": 1}, uuid.uuid4()]:
            with self.subTest(bad=repr(bad)):
                self.assertProblem(InputProblem.NOT_A_STRING, v.validate_name, bad)

    def test_control_characters_are_refused_even_at_the_edges(self):
        for bad in [
            "Al\x00pha",
            "Alpha\n",
            "\nAlpha",
            "Al\tpha",
            "Al\rpha",
            "Al\x1bpha",
            "Al\x7fpha",
            "Al\x85pha",  # NEXT LINE (Cc)
            "Al pha",  # LINE SEPARATOR
            "Al pha",  # PARAGRAPH SEPARATOR
            "Al\ud800pha",  # a lone surrogate cannot be encoded
        ]:
            with self.subTest(bad=repr(bad)):
                self.assertProblem(
                    InputProblem.INVALID_CHARACTERS, v.validate_name, bad
                )

    def test_bidirectional_controls_are_refused(self):
        for char in "‪‫‬‭‮⁦⁧⁨⁩":
            with self.subTest(char=hex(ord(char))):
                self.assertProblem(
                    InputProblem.INVALID_CHARACTERS, v.validate_name, f"Al{char}pha"
                )

    def test_a_zero_width_joiner_in_an_emoji_sequence_is_allowed(self):
        family = "👨‍👩‍👧"
        self.assertEqual(v.validate_name(family), family)

    def test_the_field_name_can_be_chosen_and_the_value_is_not_echoed(self):
        error = self.assertProblem(
            InputProblem.NOT_A_STRING, v.validate_name, None, "title"
        )
        self.assertEqual(error.field, "title")
        error = self.assertProblem(
            InputProblem.INVALID_CHARACTERS, v.validate_name, SECRET + "\x00"
        )
        self.assertNotIn(SECRET, str(error) + repr(error.args))
        self.assertEqual(error.field, "name")


class ValidateDescriptionTest(ProblemTestCase):
    def test_none_and_blank_mean_no_description(self):
        for empty in [None, "", " ", "\n\n", "\t \n", " "]:
            with self.subTest(empty=repr(empty)):
                self.assertIsNone(v.validate_description(empty))

    def test_text_is_stripped_and_keeps_inner_line_breaks_and_tabs(self):
        self.assertEqual(
            v.validate_description("  line 1\nline 2\tcol  \n"), "line 1\nline 2\tcol"
        )

    def test_the_limit_is_2000_characters_after_stripping(self):
        self.assertEqual(v.validate_description("a" * 2000), "a" * 2000)
        self.assertProblem(InputProblem.TOO_LONG, v.validate_description, "a" * 2001)
        self.assertEqual(v.validate_description("\n" + "a" * 2000 + "\n"), "a" * 2000)

    def test_a_huge_string_is_refused_before_it_is_scanned(self):
        self.assertProblem(
            InputProblem.TOO_LONG,
            v.validate_description,
            "a" * (limits.MAX_DESCRIPTION_CHARS * limits.RAW_TEXT_FACTOR + 1),
        )

    def test_other_control_characters_are_refused(self):
        for bad in ["a\x00b", "a\rb", "a\x1bb", "a b", "a‮b", "a\ud800b"]:
            with self.subTest(bad=repr(bad)):
                self.assertProblem(
                    InputProblem.INVALID_CHARACTERS, v.validate_description, bad
                )

    def test_non_strings_are_refused(self):
        for bad in [5, True, b"x", ["x"], 0, 0.0]:
            with self.subTest(bad=repr(bad)):
                self.assertProblem(
                    InputProblem.NOT_A_STRING, v.validate_description, bad
                )


class ValidateConfirmationTest(ProblemTestCase):
    def test_the_text_is_returned_unchanged_not_stripped(self):
        self.assertEqual(v.validate_confirmation(" Alpha "), " Alpha ")
        self.assertEqual(v.validate_confirmation(""), "")
        self.assertEqual(v.validate_confirmation("a\nb"), "a\nb")

    def test_the_length_limit_is_the_longest_name(self):
        self.assertEqual(v.validate_confirmation("a" * 100), "a" * 100)
        self.assertProblem(InputProblem.TOO_LONG, v.validate_confirmation, "a" * 101)

    def test_non_strings_are_refused(self):
        for bad in [None, 5, b"x"]:
            with self.subTest(bad=repr(bad)):
                self.assertProblem(
                    InputProblem.NOT_A_STRING, v.validate_confirmation, bad
                )


class ValidateRoleAndStatusTest(ProblemTestCase):
    def test_a_project_role_member_is_returned(self):
        for role in ProjectRole:
            self.assertIs(v.validate_project_role(role), role)

    def test_strings_and_other_enums_are_not_roles(self):
        for bad in ["manager", "MANAGER", None, SystemRole.USER, 1, "viewer"]:
            with self.subTest(bad=repr(bad)):
                self.assertProblem(
                    InputProblem.NOT_A_ROLE, v.validate_project_role, bad
                )

    def test_a_listable_status_is_returned(self):
        for status in (
            ProjectStatus.ACTIVE,
            ProjectStatus.ARCHIVED,
            ProjectStatus.PENDING_DELETION,
        ):
            self.assertIs(v.validate_status_filter(status), status)

    def test_deleted_is_not_listable_and_strings_are_not_statuses(self):
        self.assertProblem(
            InputProblem.OUT_OF_RANGE, v.validate_status_filter, ProjectStatus.DELETED
        )
        for bad in ["active", None, 1]:
            with self.subTest(bad=repr(bad)):
                self.assertProblem(
                    InputProblem.NOT_A_STATUS, v.validate_status_filter, bad
                )


class ValidateIntegersTest(ProblemTestCase):
    def test_limit_accepts_1_to_200(self):
        for good in (1, 50, 200):
            self.assertEqual(v.validate_limit(good), good)
        for bad in (0, -1, 201, 10**9):
            with self.subTest(bad=bad):
                self.assertProblem(InputProblem.OUT_OF_RANGE, v.validate_limit, bad)

    def test_offset_accepts_0_to_100000(self):
        for good in (0, 1, 100_000):
            self.assertEqual(v.validate_offset(good), good)
        for bad in (-1, 100_001):
            with self.subTest(bad=bad):
                self.assertProblem(InputProblem.OUT_OF_RANGE, v.validate_offset, bad)

    def test_batch_size_accepts_1_to_500(self):
        for good in (1, 50, 500):
            self.assertEqual(v.validate_batch_size(good), good)
        for bad in (0, 501):
            with self.subTest(bad=bad):
                self.assertProblem(
                    InputProblem.OUT_OF_RANGE, v.validate_batch_size, bad
                )

    def test_a_bool_a_float_a_string_and_none_are_not_integers(self):
        for function in (v.validate_limit, v.validate_offset, v.validate_batch_size):
            for bad in (True, False, 5.0, "5", None, b"5"):
                with self.subTest(function=function.__name__, bad=repr(bad)):
                    self.assertProblem(InputProblem.NOT_AN_INTEGER, function, bad)


class ValidateInstantTest(ProblemTestCase):
    def test_an_aware_datetime_is_converted_to_utc(self):
        jst = timezone(timedelta(hours=9))
        result = v.validate_instant("now", datetime(2026, 9, 24, 21, 0, tzinfo=jst))
        self.assertEqual(result, datetime(2026, 9, 24, 12, 0, tzinfo=UTC))
        self.assertEqual(result.utcoffset(), timedelta(0))

    def test_a_naive_datetime_is_refused(self):
        self.assertProblem(
            InputProblem.NAIVE_DATETIME,
            v.validate_instant,
            "now",
            datetime(2026, 9, 24),
        )

    def test_a_tzinfo_without_an_offset_counts_as_naive(self):
        class NoOffset(tzinfo):
            def utcoffset(self, dt):
                return None

        self.assertProblem(
            InputProblem.NAIVE_DATETIME,
            v.validate_instant,
            "now",
            datetime(2026, 9, 24, tzinfo=NoOffset()),
        )

    def test_non_datetimes_are_refused(self):
        for bad in ["2026-09-24T12:00:00+00:00", 1_700_000_000, None, T0.date()]:
            with self.subTest(bad=repr(bad)):
                self.assertProblem(
                    InputProblem.NOT_A_DATETIME, v.validate_instant, "clock", bad
                )

    def test_the_field_name_is_reported(self):
        error = self.assertProblem(
            InputProblem.NAIVE_DATETIME,
            v.validate_instant,
            "clock",
            datetime(2026, 1, 1),
        )
        self.assertEqual(error.field, "clock")


class MemberRecordTest(unittest.TestCase):
    P, U = uuid.uuid4(), uuid.uuid4()

    def build(self, **overrides):
        values = dict(
            project_id=self.P,
            user_id=self.U,
            role=ProjectRole.VIEWER,
            status=MemberStatus.ACTIVE,
            invited_at=T0,
            invite_expires_at=None,
            joined_at=T0,
        )
        values.update(overrides)
        return Member(**values)

    def test_a_consistent_member_and_invitation_are_accepted(self):
        active = self.build()
        self.assertIs(active.status, MemberStatus.ACTIVE)
        invited = self.build(
            status=MemberStatus.INVITED,
            invite_expires_at=T0 + timedelta(days=14),
            joined_at=None,
        )
        self.assertEqual(invited.invite_expires_at, T0 + timedelta(days=14))

    def test_inconsistent_combinations_are_refused(self):
        cases = {
            "active without a join time": dict(joined_at=None),
            "active with an expiry": dict(invite_expires_at=T0 + timedelta(days=1)),
            "invited without an expiry": dict(
                status=MemberStatus.INVITED, joined_at=None, invite_expires_at=None
            ),
            "invited with a join time": dict(
                status=MemberStatus.INVITED, invite_expires_at=T0 + timedelta(days=1)
            ),
            "invitation expiring at the invitation": dict(
                status=MemberStatus.INVITED, joined_at=None, invite_expires_at=T0
            ),
            "invitation expiring before the invitation": dict(
                status=MemberStatus.INVITED,
                joined_at=None,
                invite_expires_at=T0 - timedelta(seconds=1),
            ),
            "naive invited_at": dict(invited_at=datetime(2026, 9, 24)),
            "naive joined_at": dict(joined_at=datetime(2026, 9, 24)),
            "string role": dict(role="viewer"),
            "string status": dict(status="active"),
            "string user": dict(user_id=str(self.U)),
            "string project": dict(project_id=str(self.P)),
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    self.build(**overrides)

    def test_a_member_is_immutable(self):
        row = member()
        with self.assertRaises(AttributeError):
            row.role = ProjectRole.MANAGER  # type: ignore[misc]


class LimitsTest(unittest.TestCase):
    def test_the_documented_values(self):
        self.assertEqual(limits.MAX_NAME_CHARS, 100)
        self.assertEqual(limits.MAX_DESCRIPTION_CHARS, 2000)
        self.assertEqual(limits.DELETION_RETENTION, timedelta(days=30))
        self.assertEqual(limits.INVITE_TTL, timedelta(days=14))
        self.assertEqual(limits.DELETED_PROJECT_NAME, "Deleted Project")
        self.assertEqual(limits.MAX_MEMBERS_PER_PROJECT, 200)

    def test_the_default_clock_is_aware_utc(self):
        now = limits.utc_now()
        self.assertEqual(now.utcoffset(), timedelta(0))


if __name__ == "__main__":
    unittest.main()
