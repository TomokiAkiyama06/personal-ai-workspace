"""The argument validators of the provenance store (pure functions, no I/O)."""

import unittest
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from enum import StrEnum
from uuid import uuid4

from paw_backend.research.provenance.errors import (
    InputProblem,
    InvalidProvenanceInputError,
    ProvenanceError,
)
from paw_backend.research.provenance.records import Stance
from paw_backend.research.provenance.validation import (
    validate_bounded_int,
    validate_content_hash,
    validate_datetime,
    validate_enum,
    validate_locator,
    validate_optional_datetime,
    validate_optional_uuid,
    validate_text,
    validate_uuid,
)

GOOD_HASH = "sha256:" + "0123456789abcdef" * 4


class Problem:
    """``assertRaises``-style helper: which field and problem was reported."""

    def __init__(self, test: unittest.TestCase, function, *args, **kwargs) -> None:
        with test.assertRaises(InvalidProvenanceInputError) as raised:
            function(*args, **kwargs)
        self.error = raised.exception
        self.field = raised.exception.field
        self.problem = raised.exception.problem


class ErrorTypesTest(unittest.TestCase):
    def test_the_message_is_built_from_the_field_and_the_problem_only(self):
        error = InvalidProvenanceInputError("text", InputProblem.TOO_LONG)

        self.assertEqual(str(error), "Invalid text: too_long")
        self.assertEqual(error.code, "invalid_provenance_input")
        self.assertIsInstance(error, ProvenanceError)

    def test_the_problem_vocabulary_is_closed(self):
        self.assertEqual(
            {problem.value for problem in InputProblem},
            {
                "required",
                "wrong_type",
                "blank",
                "too_long",
                "too_many",
                "empty",
                "out_of_range",
                "invalid_characters",
                "naive_datetime",
                "invalid_format",
                "unknown_reference",
                "self_reference",
                "conflict",
            },
        )

    def test_every_error_has_its_own_code_and_a_fixed_message(self):
        from paw_backend.research.provenance import errors

        classes = [
            errors.ClaimNotFoundError,
            errors.SourceNotFoundError,
            errors.ProvenanceConflictError,
            errors.ProvenanceLimitError,
            errors.ProvenanceBusyError,
        ]
        codes = {cls.code for cls in classes}
        self.assertEqual(
            codes,
            {
                "claim_not_found",
                "source_not_found",
                "provenance_conflict",
                "provenance_limit",
                "provenance_busy",
            },
        )
        for cls in classes:
            with self.subTest(cls.__name__):
                self.assertTrue(issubclass(cls, ProvenanceError))
                self.assertEqual(cls().args, (str(cls()),))


class UuidTest(unittest.TestCase):
    def test_a_uuid_is_returned_as_the_same_object(self):
        value = uuid4()
        self.assertIs(validate_uuid("f", value), value)

    def test_none_is_required_and_everything_else_is_the_wrong_type(self):
        self.assertEqual(
            Problem(self, validate_uuid, "f", None).problem, InputProblem.REQUIRED
        )
        for bad in (str(uuid4()), uuid4().bytes, uuid4().int, 5, object(), [uuid4()]):
            with self.subTest(type(bad).__name__):
                result = Problem(self, validate_uuid, "id", bad)
                self.assertEqual(
                    (result.field, result.problem), ("id", InputProblem.WRONG_TYPE)
                )

    def test_optional_uuid(self):
        value = uuid4()
        self.assertIsNone(validate_optional_uuid("f", None))
        self.assertIs(validate_optional_uuid("f", value), value)
        self.assertEqual(
            Problem(self, validate_optional_uuid, "f", str(value)).problem,
            InputProblem.WRONG_TYPE,
        )


class EnumTest(unittest.TestCase):
    def test_a_member_is_returned(self):
        self.assertIs(
            validate_enum("stance", Stance.CONTRADICTS, Stance), Stance.CONTRADICTS
        )

    def test_a_plain_string_is_not_a_member(self):
        result = Problem(self, validate_enum, "stance", "supports", Stance)
        self.assertEqual(
            (result.field, result.problem), ("stance", InputProblem.WRONG_TYPE)
        )

    def test_none_and_members_of_another_enum(self):
        self.assertEqual(
            Problem(self, validate_enum, "stance", None, Stance).problem,
            InputProblem.REQUIRED,
        )

        class Other(StrEnum):
            SUPPORTS = "supports"

        self.assertEqual(
            Problem(self, validate_enum, "stance", Other.SUPPORTS, Stance).problem,
            InputProblem.WRONG_TYPE,
        )


class BoundedIntTest(unittest.TestCase):
    def test_the_bounds_are_inclusive(self):
        self.assertEqual(validate_bounded_int("n", 1, minimum=1, maximum=200), 1)
        self.assertEqual(validate_bounded_int("n", 200, minimum=1, maximum=200), 200)
        for bad in (0, 201, -5):
            with self.subTest(bad):
                self.assertEqual(
                    Problem(
                        self, validate_bounded_int, "n", bad, minimum=1, maximum=200
                    ).problem,
                    InputProblem.OUT_OF_RANGE,
                )

    def test_only_a_real_int_is_accepted(self):
        for bad in (True, False, 5.0, "5", 5.5, b"5", [5]):
            with self.subTest(repr(bad)):
                self.assertEqual(
                    Problem(
                        self, validate_bounded_int, "n", bad, minimum=0, maximum=10
                    ).problem,
                    InputProblem.WRONG_TYPE,
                )
        self.assertEqual(
            Problem(
                self, validate_bounded_int, "n", None, minimum=0, maximum=10
            ).problem,
            InputProblem.REQUIRED,
        )


class TextTest(unittest.TestCase):
    def check(self, value, **options):
        return validate_text("text", value, max_chars=10, **options)

    def test_a_valid_text_is_returned_unchanged(self):
        for value in ("abc", "  padded  ", "あ" * 10, "line\nbreak", "a\tb"):
            with self.subTest(repr(value)):
                self.assertEqual(self.check(value), value)

    def test_the_length_is_counted_in_characters(self):
        self.assertEqual(self.check("😀" * 10), "😀" * 10)
        self.assertEqual(
            Problem(self, self.check, "😀" * 11).problem, InputProblem.TOO_LONG
        )
        self.assertEqual(
            Problem(self, self.check, "a" * 11).problem, InputProblem.TOO_LONG
        )

    def test_blank_texts_are_refused_unless_allowed(self):
        for value in ("", " ", "\t\n", "　", "  "):
            with self.subTest(repr(value)):
                self.assertEqual(
                    Problem(self, self.check, value).problem, InputProblem.BLANK
                )
                self.assertEqual(self.check(value, allow_blank=True), value)

    def test_nul_and_lone_surrogates_are_refused_before_anything_else(self):
        for value in ("a\x00b", "\x00", "\ud800", "ok\udfff"):
            with self.subTest(repr(value)):
                self.assertEqual(
                    Problem(self, self.check, value).problem,
                    InputProblem.INVALID_CHARACTERS,
                )
        # NUL wins over blank and over length.
        self.assertEqual(
            Problem(self, self.check, "\x00" + " " * 20).problem,
            InputProblem.INVALID_CHARACTERS,
        )
        self.assertEqual(
            Problem(self, self.check, "\x00", allow_blank=True).problem,
            InputProblem.INVALID_CHARACTERS,
        )

    def test_blank_is_reported_before_too_long(self):
        self.assertEqual(
            Problem(self, self.check, " " * 50).problem, InputProblem.BLANK
        )

    def test_other_types_and_none(self):
        self.assertEqual(Problem(self, self.check, None).problem, InputProblem.REQUIRED)
        for bad in (b"abc", 5, ["a"], object()):
            with self.subTest(type(bad).__name__):
                self.assertEqual(
                    Problem(self, self.check, bad).problem, InputProblem.WRONG_TYPE
                )

    def test_the_error_never_contains_the_text(self):
        secret = "SECRET-TOKEN-4f9a1c" * 3
        error = Problem(self, validate_text, "text", secret, max_chars=5).error
        self.assertNotIn("SECRET", str(error))
        self.assertNotIn("SECRET", repr(error))
        self.assertIsNone(error.__cause__)


class DatetimeTest(unittest.TestCase):
    def test_an_aware_datetime_becomes_utc_at_the_same_instant(self):
        tokyo = timezone(timedelta(hours=9))
        value = datetime(2026, 9, 24, 21, 30, 15, 123456, tzinfo=tokyo)

        result = validate_datetime("fetched_at", value)

        self.assertEqual(result, value)
        self.assertEqual(result, datetime(2026, 9, 24, 12, 30, 15, 123456, tzinfo=UTC))
        self.assertEqual(result.utcoffset(), timedelta(0))
        self.assertEqual(result.microsecond, 123456)

    def test_a_naive_datetime_is_refused(self):
        class NoOffset(tzinfo):
            def utcoffset(self, value):
                return None

            def dst(self, value):
                return None

            def tzname(self, value):
                return None

        for value in (datetime(2026, 9, 24), datetime(2026, 9, 24, tzinfo=NoOffset())):
            with self.subTest(repr(value)):
                result = Problem(self, validate_datetime, "fetched_at", value)
                self.assertEqual(
                    (result.field, result.problem),
                    ("fetched_at", InputProblem.NAIVE_DATETIME),
                )

    def test_a_value_that_cannot_be_expressed_in_utc_is_out_of_range(self):
        east = timezone(timedelta(hours=5))
        west = timezone(timedelta(hours=-5))
        for value in (
            datetime.min.replace(tzinfo=east),
            datetime.max.replace(tzinfo=west),
        ):
            with self.subTest(repr(value)):
                self.assertEqual(
                    Problem(self, validate_datetime, "f", value).problem,
                    InputProblem.OUT_OF_RANGE,
                )
        self.assertEqual(
            validate_datetime("f", datetime.min.replace(tzinfo=UTC)),
            datetime.min.replace(tzinfo=UTC),
        )

    def test_only_datetimes_are_accepted(self):
        for bad in ("2026-09-24T12:00:00Z", 1758715200, datetime(2026, 9, 24).date()):
            with self.subTest(repr(bad)):
                self.assertEqual(
                    Problem(self, validate_datetime, "f", bad).problem,
                    InputProblem.WRONG_TYPE,
                )
        self.assertEqual(
            Problem(self, validate_datetime, "f", None).problem, InputProblem.REQUIRED
        )

    def test_optional_datetime(self):
        self.assertIsNone(validate_optional_datetime("f", None))
        value = datetime(2026, 1, 1, tzinfo=UTC)
        self.assertEqual(validate_optional_datetime("f", value), value)
        self.assertEqual(
            Problem(
                self, validate_optional_datetime, "f", datetime(2026, 1, 1)
            ).problem,
            InputProblem.NAIVE_DATETIME,
        )


class LocatorTest(unittest.TestCase):
    def test_the_canonical_form_is_returned(self):
        self.assertEqual(
            validate_locator(
                "locator", "HTTPS://Example.COM:443/a?utm_source=x&b=2&a=1#top"
            ),
            "https://example.com/a?a=1&b=2",
        )
        self.assertEqual(
            validate_locator("locator", "http://example.com"), "http://example.com/"
        )

    def test_a_locator_that_is_not_acceptable_is_an_invalid_format(self):
        for bad in (
            "",
            "ftp://example.com/",
            "example.com/a",
            "https://user:pw@example.com/",
            "https://example.com/a b",
            "https://[::1]/",
            "https://example.com/\n",
        ):
            with self.subTest(repr(bad)):
                result = Problem(self, validate_locator, "locator", bad)
                self.assertEqual(
                    (result.field, result.problem),
                    ("locator", InputProblem.INVALID_FORMAT),
                )

    def test_the_canonical_length_limit_is_2048(self):
        base = "https://example.com/"
        self.assertEqual(
            len(validate_locator("l", base + "x" * (2048 - len(base)))), 2048
        )
        self.assertEqual(
            Problem(
                self, validate_locator, "l", base + "x" * (2049 - len(base))
            ).problem,
            InputProblem.INVALID_FORMAT,
        )

    def test_none_and_other_types(self):
        self.assertEqual(
            Problem(self, validate_locator, "l", None).problem, InputProblem.REQUIRED
        )
        for bad in (b"https://example.com/", 5, ["https://example.com/"]):
            with self.subTest(type(bad).__name__):
                self.assertEqual(
                    Problem(self, validate_locator, "l", bad).problem,
                    InputProblem.WRONG_TYPE,
                )

    def test_the_error_never_contains_the_locator_or_its_credentials(self):
        error = Problem(
            self,
            validate_locator,
            "locator",
            "https://user:SECRET-TOKEN-4f9a1c@example.com/",
        ).error
        self.assertNotIn("SECRET", str(error))
        self.assertNotIn("SECRET", repr(error))
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)


class ContentHashTest(unittest.TestCase):
    def test_a_valid_hash_is_returned(self):
        self.assertEqual(validate_content_hash("h", GOOD_HASH), GOOD_HASH)

    def test_malformed_hashes(self):
        for bad in (
            "",
            "sha256:",
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
            "sha256:" + "z" * 64,
            "SHA256:" + "a" * 64,
            "sha1:" + "a" * 64,
            "a" * 64,
            GOOD_HASH + "\n",
            " " + GOOD_HASH,
        ):
            with self.subTest(repr(bad)):
                self.assertEqual(
                    Problem(self, validate_content_hash, "h", bad).problem,
                    InputProblem.INVALID_FORMAT,
                )

    def test_none_and_other_types(self):
        self.assertEqual(
            Problem(self, validate_content_hash, "h", None).problem,
            InputProblem.REQUIRED,
        )
        self.assertEqual(
            Problem(self, validate_content_hash, "h", GOOD_HASH.encode()).problem,
            InputProblem.WRONG_TYPE,
        )


if __name__ == "__main__":
    unittest.main()
