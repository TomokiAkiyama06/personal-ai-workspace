"""Pure argument validation of the Research Scratch Store (no database)."""

import unittest
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from uuid import uuid4

from paw_backend.research.scratch import (
    InputProblem,
    InvalidScratchInputError,
    PromotionOutcome,
    PromotionState,
)
from paw_backend.research.scratch.validation import (
    NewItem,
    validate_bool,
    validate_bounded_int,
    validate_datetime,
    validate_new_item,
    validate_optional_text,
    validate_optional_uuid,
    validate_outcome,
    validate_source_metadata,
    validate_uuid,
)

SECRET = "sk-live-SECRET-0123456789"
P = InputProblem


class ValidationTestCase(unittest.TestCase):
    def assertInvalid(self, field, problem, function, *args, **kwargs):
        """``function`` raises InvalidScratchInputError(field, problem), no echo."""
        with self.assertRaises(InvalidScratchInputError) as raised:
            function(*args, **kwargs)
        error = raised.exception
        self.assertEqual((error.field, error.problem), (field, problem))
        self.assertEqual(str(error), f"Invalid {field}: {problem.value}")
        self.assertIsNone(error.__cause__)


class UuidTest(ValidationTestCase):
    def test_a_uuid_is_returned_as_it_is(self):
        value = uuid4()

        self.assertIs(validate_uuid("item_id", value), value)

    def test_none_is_required(self):
        self.assertInvalid("holder_id", P.REQUIRED, validate_uuid, "holder_id", None)

    def test_nothing_is_coerced_into_a_uuid(self):
        value = uuid4()
        for bad in (str(value), value.hex, value.bytes, value.int, [value], b"x", True):
            with self.subTest(type(bad).__name__):
                self.assertInvalid(
                    "item_id", P.WRONG_TYPE, validate_uuid, "item_id", bad
                )

    def test_the_message_never_contains_the_rejected_value(self):
        with self.assertRaises(InvalidScratchInputError) as raised:
            validate_uuid("item_id", SECRET)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertNotIn(SECRET, repr(raised.exception))

    def test_optional_uuid_lets_none_through_and_validates_the_rest(self):
        value = uuid4()

        self.assertIsNone(validate_optional_uuid("task_id", None))
        self.assertIs(validate_optional_uuid("task_id", value), value)
        self.assertInvalid(
            "task_id", P.WRONG_TYPE, validate_optional_uuid, "task_id", str(value)
        )


class OptionalTextTest(ValidationTestCase):
    def validate(self, value, max_chars=10):
        return validate_optional_text("title", value, max_chars=max_chars)

    def test_none_is_absent(self):
        self.assertIsNone(self.validate(None))

    def test_the_text_is_returned_unchanged_and_not_stripped(self):
        self.assertEqual(self.validate("  hello  "), "  hello  ")
        self.assertEqual(self.validate("日本語のタイトル"), "日本語のタイトル")

    def test_the_length_is_counted_in_characters_and_the_limit_is_inclusive(self):
        self.assertEqual(self.validate("あ" * 10), "あ" * 10)
        self.assertInvalid("title", P.TOO_LONG, self.validate, "あ" * 11)
        self.assertEqual(validate_optional_text("q", "a", max_chars=1), "a")
        self.assertInvalid(
            "q", P.TOO_LONG, validate_optional_text, "q", "ab", max_chars=1
        )

    def test_an_emoji_counts_as_one_character(self):
        self.assertEqual(self.validate("😀" * 10), "😀" * 10)
        self.assertInvalid("title", P.TOO_LONG, self.validate, "😀" * 11)

    def test_blank_text_is_rejected_in_every_form(self):
        for blank in ("", " ", "\t\n", "　", "   "):
            with self.subTest(repr(blank)):
                self.assertInvalid("title", P.BLANK, self.validate, blank)

    def test_only_str_is_accepted(self):
        for bad in (b"abc", 5, 1.5, True, ["a"], {"a": 1}, object()):
            with self.subTest(type(bad).__name__):
                self.assertInvalid("title", P.WRONG_TYPE, self.validate, bad)

    def test_nul_and_unencodable_text_is_rejected(self):
        self.assertInvalid("title", P.INVALID_CHARACTERS, self.validate, "a\x00b")
        self.assertInvalid("title", P.INVALID_CHARACTERS, self.validate, "\x00")
        self.assertInvalid("title", P.INVALID_CHARACTERS, self.validate, "a\ud800b")

    def test_the_first_failing_check_wins(self):
        # NUL beats blank/length; blank beats length (a blank string can be long).
        self.assertInvalid("title", P.INVALID_CHARACTERS, self.validate, "\x00" * 50)
        self.assertInvalid("title", P.BLANK, self.validate, " " * 50)

    def test_the_error_does_not_echo_the_text(self):
        with self.assertRaises(InvalidScratchInputError) as raised:
            self.validate(SECRET * 3)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertNotIn(SECRET, repr(raised.exception.args))


def nested(depth: int) -> dict:
    """A dict whose deepest container is at ``depth`` (the top level is 1)."""
    value: dict = {}
    for _ in range(depth - 1):
        value = {"a": value}
    return value


class SourceMetadataTest(ValidationTestCase):
    def validate(self, value):
        return validate_source_metadata(value)

    def test_none_is_an_empty_object(self):
        first, second = self.validate(None), self.validate(None)

        self.assertEqual(first, {})
        self.assertIsNot(first, second)

    def test_a_realistic_object_is_returned_equal(self):
        metadata = {
            "url": "https://docs.example.org/page",
            "source_type": "official_docs",
            "fetched_at": "2026-09-24T12:00:00Z",
            "published_at": None,
            "confidence": 0.85,
            "claims": [{"text": "日本語の主張", "sources": [1, 2]}],
            "flags": {"stale": False, "count": 3},
        }

        result = self.validate(metadata)

        self.assertEqual(result, metadata)
        self.assertIs(result["flags"]["stale"], False)
        self.assertIs(type(result["flags"]["count"]), int)

    def test_the_result_shares_nothing_with_the_argument(self):
        metadata = {"list": [1, {"k": "v"}], "map": {"x": [1]}}

        result = self.validate(metadata)
        metadata["list"].append(2)
        metadata["list"][1]["k"] = "changed"
        metadata["map"]["x"].append(2)
        metadata["new"] = 1

        self.assertEqual(result, {"list": [1, {"k": "v"}], "map": {"x": [1]}})

    def test_only_a_dict_is_accepted(self):
        for bad in ([], [{"a": 1}], "{}", 1, True, ("a",), {"a"}, b"{}"):
            with self.subTest(repr(bad)):
                self.assertInvalid("source_metadata", P.WRONG_TYPE, self.validate, bad)

    def test_values_must_be_json_types(self):
        for bad in (
            (1, 2),
            {1, 2},
            b"x",
            Decimal("1.5"),
            datetime(2026, 9, 24, tzinfo=UTC),
            uuid4(),
            object(),
            1 + 2j,
        ):
            with self.subTest(type(bad).__name__):
                self.assertInvalid(
                    "source_metadata", P.WRONG_TYPE, self.validate, {"a": bad}
                )
                self.assertInvalid(
                    "source_metadata", P.WRONG_TYPE, self.validate, {"a": [bad]}
                )

    def test_keys_must_be_non_blank_short_strings(self):
        self.assertInvalid("source_metadata", P.WRONG_TYPE, self.validate, {1: "a"})
        self.assertInvalid("source_metadata", P.WRONG_TYPE, self.validate, {None: "a"})
        self.assertInvalid("source_metadata", P.BLANK, self.validate, {"": "a"})
        self.assertInvalid("source_metadata", P.BLANK, self.validate, {"  ": "a"})
        self.assertInvalid(
            "source_metadata", P.INVALID_CHARACTERS, self.validate, {"a\x00": 1}
        )
        self.assertEqual(self.validate({"k" * 128: 1}), {"k" * 128: 1})
        self.assertEqual(self.validate({"あ" * 128: 1}), {"あ" * 128: 1})
        self.assertInvalid("source_metadata", P.TOO_LONG, self.validate, {"k" * 129: 1})

    def test_nested_keys_are_checked_too(self):
        self.assertInvalid("source_metadata", P.BLANK, self.validate, {"a": [{"": 1}]})
        self.assertInvalid(
            "source_metadata", P.WRONG_TYPE, self.validate, {"a": {"b": {2: 1}}}
        )

    def test_string_values_reject_nul_and_unencodable_text_but_may_be_empty(self):
        self.assertEqual(self.validate({"a": ""}), {"a": ""})
        self.assertInvalid(
            "source_metadata", P.INVALID_CHARACTERS, self.validate, {"a": "x\x00"}
        )
        self.assertInvalid(
            "source_metadata", P.INVALID_CHARACTERS, self.validate, {"a": ["\ud800"]}
        )

    def test_integers_are_limited_to_the_exactly_representable_range(self):
        limit = 2**53 - 1
        self.assertEqual(
            self.validate({"a": limit, "b": -limit}), {"a": limit, "b": -limit}
        )
        for bad in (limit + 1, -limit - 1, 10**30):
            with self.subTest(bad):
                self.assertInvalid(
                    "source_metadata", P.OUT_OF_RANGE, self.validate, {"a": bad}
                )

    def test_booleans_are_kept_as_booleans(self):
        result = self.validate({"t": True, "f": False, "one": 1, "zero": 0})

        self.assertIs(result["t"], True)
        self.assertIs(result["f"], False)
        self.assertIs(type(result["one"]), int)
        self.assertIs(type(result["zero"]), int)

    def test_floats_must_be_finite_and_of_a_sane_magnitude(self):
        for good in (0.0, -0.0, 1e-6, 0.5, -2.5, 123456.789, 999999999999999.0, -1e14):
            with self.subTest(good):
                self.assertEqual(self.validate({"a": good}), {"a": good})
        for bad in (
            float("nan"),
            float("inf"),
            float("-inf"),
            9.99e-7,
            1e-300,
            -1e-7,
            1e15,
            -1e15,
            1e300,
        ):
            with self.subTest(bad):
                self.assertInvalid(
                    "source_metadata", P.OUT_OF_RANGE, self.validate, {"a": bad}
                )

    def test_the_depth_limit_counts_dicts_and_lists_and_starts_at_one(self):
        self.assertEqual(self.validate(nested(1)), {})
        self.assertEqual(self.validate(nested(6)), nested(6))
        self.assertInvalid("source_metadata", P.TOO_DEEP, self.validate, nested(7))
        five_lists = {"a": [[[[[1]]]]]}  # the innermost list is at depth 6
        six_lists = {"a": [[[[[[1]]]]]]}
        self.assertEqual(self.validate(five_lists), five_lists)
        self.assertInvalid("source_metadata", P.TOO_DEEP, self.validate, six_lists)

    def test_a_self_referencing_structure_is_too_deep_not_a_recursion_error(self):
        cyclic_dict: dict = {}
        cyclic_dict["self"] = cyclic_dict
        cyclic_list: list = []
        cyclic_list.append(cyclic_list)

        self.assertInvalid("source_metadata", P.TOO_DEEP, self.validate, cyclic_dict)
        self.assertInvalid(
            "source_metadata", P.TOO_DEEP, self.validate, {"a": cyclic_list}
        )

    def test_the_size_limit_is_16384_bytes_of_compact_utf8_json(self):
        # {"k":"<n x a>"} is n + 8 bytes.
        self.assertEqual(
            self.validate({"k": "a" * (16384 - 8)}), {"k": "a" * (16384 - 8)}
        )
        self.assertInvalid(
            "source_metadata", P.TOO_LARGE, self.validate, {"k": "a" * (16384 - 7)}
        )

    def test_the_size_counts_utf8_bytes_not_characters_and_does_not_escape(self):
        # 5458 x 3 bytes + 2 x 1 byte + 8 bytes of structure = 16384.
        fits = {"k": "あ" * 5458 + "aa"}
        too_big = {"k": "あ" * 5458 + "aaa"}

        self.assertEqual(self.validate(fits), fits)
        self.assertInvalid("source_metadata", P.TOO_LARGE, self.validate, too_big)
        # Fewer than 16384 characters, far more than 16384 bytes.
        self.assertInvalid(
            "source_metadata", P.TOO_LARGE, self.validate, {"k": "あ" * 10000}
        )

    def test_many_small_values_add_up(self):
        # 3000 entries of "kNNNN":1 are about 3000 * 10 bytes.
        many = {f"k{i:04d}": 1 for i in range(3000)}

        self.assertInvalid("source_metadata", P.TOO_LARGE, self.validate, many)

    def test_the_first_problem_in_document_order_is_reported(self):
        self.assertInvalid(
            "source_metadata",
            P.WRONG_TYPE,
            self.validate,
            {"a": (1,), "b": float("nan")},
        )
        self.assertInvalid(
            "source_metadata",
            P.OUT_OF_RANGE,
            self.validate,
            {"a": float("nan"), "b": (1,)},
        )

    def test_depth_is_reported_before_size(self):
        big_and_deep = {"k": "a" * 20000, "d": nested(7)}

        self.assertInvalid("source_metadata", P.TOO_DEEP, self.validate, big_and_deep)

    def test_the_error_does_not_echo_keys_or_values(self):
        for bad in ({SECRET: (1,)}, {"a": SECRET.encode()}, {SECRET * 10: 1}):
            with self.subTest(repr(bad)[:30]):
                with self.assertRaises(InvalidScratchInputError) as raised:
                    self.validate(bad)
                self.assertNotIn(SECRET, str(raised.exception))
                self.assertNotIn(SECRET, repr(raised.exception.args))


class NewItemTest(ValidationTestCase):
    def arguments(self, **overrides):
        values = {
            "project_id": uuid4(),
            "created_by": uuid4(),
            "task_id": None,
            "query": None,
            "title": None,
            "summary": "A summary",
            "content": None,
            "source_metadata": None,
        }
        values.update(overrides)
        return values

    def test_a_full_item_is_returned_as_a_new_item(self):
        arguments = self.arguments(
            task_id=uuid4(),
            query="how to configure X",
            title="Docs page",
            summary="The summary",
            content="The body",
            source_metadata={"url": "https://example.org"},
        )

        item = validate_new_item(**arguments)

        self.assertEqual(item, NewItem(**arguments))

    def test_optional_fields_default_to_none_and_metadata_to_an_empty_object(self):
        arguments = self.arguments()

        item = validate_new_item(**arguments)

        self.assertEqual(
            item,
            NewItem(
                project_id=arguments["project_id"],
                created_by=arguments["created_by"],
                task_id=None,
                query=None,
                title=None,
                summary="A summary",
                content=None,
                source_metadata={},
            ),
        )

    def test_a_content_without_a_summary_is_enough(self):
        item = validate_new_item(**self.arguments(summary=None, content="body"))

        self.assertIsNone(item.summary)
        self.assertEqual(item.content, "body")

    def test_an_item_without_summary_and_content_is_rejected(self):
        self.assertInvalid(
            "summary",
            P.REQUIRED,
            validate_new_item,
            **self.arguments(summary=None, content=None),
        )

    def test_each_field_is_checked_under_its_own_name(self):
        cases = [
            ("project_id", {"project_id": None}, P.REQUIRED),
            ("project_id", {"project_id": "not-a-uuid"}, P.WRONG_TYPE),
            ("created_by", {"created_by": None}, P.REQUIRED),
            ("created_by", {"created_by": str(uuid4())}, P.WRONG_TYPE),
            ("task_id", {"task_id": str(uuid4())}, P.WRONG_TYPE),
            ("query", {"query": ""}, P.BLANK),
            ("query", {"query": "q" * 1001}, P.TOO_LONG),
            ("title", {"title": "   "}, P.BLANK),
            ("title", {"title": "t" * 501}, P.TOO_LONG),
            ("summary", {"summary": "s" * 8001}, P.TOO_LONG),
            ("summary", {"summary": b"bytes"}, P.WRONG_TYPE),
            ("content", {"content": "c" * 100_001}, P.TOO_LONG),
            ("content", {"content": "a\x00"}, P.INVALID_CHARACTERS),
            ("source_metadata", {"source_metadata": []}, P.WRONG_TYPE),
            ("source_metadata", {"source_metadata": {"a": (1,)}}, P.WRONG_TYPE),
        ]
        for field, overrides, problem in cases:
            with self.subTest(field=field, problem=problem.value):
                self.assertInvalid(
                    field, problem, validate_new_item, **self.arguments(**overrides)
                )

    def test_the_documented_limits_are_accepted_exactly(self):
        item = validate_new_item(
            **self.arguments(
                query="q" * 1000,
                title="t" * 500,
                summary="s" * 8000,
                content="c" * 100_000,
            )
        )

        self.assertEqual(
            (len(item.query), len(item.title), len(item.summary), len(item.content)),
            (1000, 500, 8000, 100_000),
        )

    def test_fields_are_checked_in_the_documented_order(self):
        broken = {
            "project_id": None,
            "created_by": None,
            "task_id": "x",
            "query": "",
            "title": "",
            "summary": "",
            "content": "",
            "source_metadata": [],
        }
        order = list(broken)
        for position, field in enumerate(order):
            # Everything from ``field`` on is broken; ``field`` must be reported.
            overrides = {name: broken[name] for name in order[position:]}
            with self.subTest(first_broken=field):
                with self.assertRaises(InvalidScratchInputError) as raised:
                    validate_new_item(**self.arguments(**overrides))
                self.assertEqual(raised.exception.field, field)

    def test_a_missing_body_is_reported_after_every_other_field(self):
        self.assertInvalid(
            "source_metadata",
            P.WRONG_TYPE,
            validate_new_item,
            **self.arguments(summary=None, content=None, source_metadata=[]),
        )

    def test_an_invalid_item_does_not_echo_its_content(self):
        with self.assertRaises(InvalidScratchInputError) as raised:
            validate_new_item(**self.arguments(summary=SECRET * 1000))
        self.assertNotIn(SECRET, str(raised.exception))


class DatetimeTest(ValidationTestCase):
    def test_an_aware_datetime_is_converted_to_utc_keeping_the_instant(self):
        jst = timezone(timedelta(hours=9))
        value = datetime(2026, 9, 24, 21, 0, tzinfo=jst)

        result = validate_datetime("now", value)

        self.assertEqual(result, datetime(2026, 9, 24, 12, 0, tzinfo=UTC))
        self.assertEqual(result.utcoffset(), timedelta(0))

    def test_none_is_required_and_other_types_are_wrong(self):
        self.assertInvalid("now", P.REQUIRED, validate_datetime, "now", None)
        for bad in (
            "2026-09-24T12:00:00Z",
            1_700_000_000,
            1.5,
            datetime(2026, 9, 24).date(),
        ):
            with self.subTest(repr(bad)):
                self.assertInvalid("now", P.WRONG_TYPE, validate_datetime, "now", bad)

    def test_a_naive_datetime_is_rejected_under_the_given_field_name(self):
        self.assertInvalid(
            "clock",
            P.NAIVE_DATETIME,
            validate_datetime,
            "clock",
            datetime(2026, 9, 24, 12, 0),
        )

    def test_a_timezone_without_an_offset_counts_as_naive(self):
        class NoOffset(tzinfo):
            def utcoffset(self, dt):
                return None

            def dst(self, dt):
                return None

            def tzname(self, dt):
                return "none"

        self.assertInvalid(
            "now",
            P.NAIVE_DATETIME,
            validate_datetime,
            "now",
            datetime(2026, 9, 24, 12, 0, tzinfo=NoOffset()),
        )


class BoundedIntTest(ValidationTestCase):
    def validate(self, value):
        return validate_bounded_int("lease_seconds", value, minimum=1, maximum=3600)

    def test_both_bounds_are_inclusive(self):
        self.assertEqual(self.validate(1), 1)
        self.assertEqual(self.validate(3600), 3600)
        self.assertEqual(self.validate(300), 300)
        self.assertInvalid("lease_seconds", P.OUT_OF_RANGE, self.validate, 0)
        self.assertInvalid("lease_seconds", P.OUT_OF_RANGE, self.validate, -1)
        self.assertInvalid("lease_seconds", P.OUT_OF_RANGE, self.validate, 3601)

    def test_none_is_required(self):
        self.assertInvalid("lease_seconds", P.REQUIRED, self.validate, None)

    def test_only_an_int_is_accepted(self):
        for bad in (True, False, 5.0, 1.5, "5", b"5", [5], Decimal(5)):
            with self.subTest(repr(bad)):
                self.assertInvalid("lease_seconds", P.WRONG_TYPE, self.validate, bad)

    def test_a_huge_int_is_out_of_range_not_an_overflow(self):
        self.assertInvalid("lease_seconds", P.OUT_OF_RANGE, self.validate, 10**40)


class BoolAndOutcomeTest(ValidationTestCase):
    def test_a_bool_is_returned_and_nothing_else_is_accepted(self):
        self.assertIs(validate_bool("include_content", True), True)
        self.assertIs(validate_bool("include_content", False), False)
        self.assertInvalid(
            "include_content", P.REQUIRED, validate_bool, "include_content", None
        )
        for bad in (0, 1, "true", "false", [], object()):
            with self.subTest(repr(bad)):
                self.assertInvalid(
                    "include_content",
                    P.WRONG_TYPE,
                    validate_bool,
                    "include_content",
                    bad,
                )

    def test_an_outcome_must_be_a_promotion_outcome_member(self):
        for member in PromotionOutcome:
            self.assertIs(validate_outcome(member), member)
        self.assertInvalid("outcome", P.REQUIRED, validate_outcome, None)
        for bad in ("promoted", "rejected", PromotionState.PROMOTED, 1, True):
            with self.subTest(repr(bad)):
                self.assertInvalid("outcome", P.WRONG_TYPE, validate_outcome, bad)


if __name__ == "__main__":
    unittest.main()
