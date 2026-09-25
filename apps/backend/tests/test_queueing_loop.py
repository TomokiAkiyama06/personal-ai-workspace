"""Failure signatures and loop evaluation: pure functions, no database."""

import hashlib
import json
import re
import unittest
import uuid

from paw_backend.tasks.queueing import (
    DEFAULT_LOOP_POLICY,
    FailureRecord,
    InvalidQueueingArgumentError,
    LoopAssessment,
    LoopDetector,
    LoopPolicy,
    LoopVerdict,
    evaluate_loop,
    failure_signature,
    normalize_failure_message,
)

A, B, C = "a" * 64, "b" * 64, "c" * 64
V = LoopVerdict


def r(signature: str, approach: int = 0) -> FailureRecord:
    return FailureRecord(signature, approach)


class NormalizeTest(unittest.TestCase):
    def test_hand_computed_examples(self):
        cases = {
            "Timeout after 30s  on 0x7FFF": "timeout after <n>s on <hex>",
            "Timeout after 45s on 0xABCDEF": "timeout after <n>s on <hex>",
            "": "",
            "   \n\t ": "",
            "File /tmp/run-3f2a9c1e-1111-4222-8333-444455556666/out.txt not found"
            " (commit a1b2c3d4e5f6)": "file /tmp/run-<uuid>/out.txt not found"
            " (commit <hex>)",
            "ＡＢＣ　ｆａｉｌｅｄ １２３": "abc failed <n>",
            "Error 404: page 12 of 300": "error <n>: page <n> of <n>",
            "decade defaced cafe 1234567 abcdef0": "decade defaced cafe <n> <hex>",
            "STRASSE straße": "strasse strasse",
            "line1\nline2\r\n  line3": "line<n> line<n> line<n>",
            "0x": "<n>x",
            "abc123 deadbeef1 deadbeef": "abc<n> <hex> deadbeef",
            "UUID 3F2A9C1E-1111-4222-8333-444455556666 upper": "uuid <uuid> upper",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(normalize_failure_message(message), expected)

    def test_only_the_first_2000_characters_count(self):
        self.assertEqual(normalize_failure_message("a" * 2000 + "TAIL 99"), "a" * 2000)
        self.assertEqual(
            normalize_failure_message("x" * 1999 + "12"), "x" * 1999 + "<n>"
        )
        self.assertEqual(normalize_failure_message("x" * 2000 + "12"), "x" * 2000)

    def test_a_huge_message_is_cut_before_it_is_processed(self):
        message = "boom " * 400_000  # 2,000,000 characters
        self.assertEqual(
            normalize_failure_message(message),
            normalize_failure_message(message[:2000]),
        )

    def test_the_result_never_contains_a_digit_or_line_break(self):
        result = normalize_failure_message("a1 b22\n\tc333 0x1f 12345678 deadbeef99")
        self.assertIsNone(re.search(r"\d|\s\s|[\n\t]", result))

    def test_a_non_string_is_rejected(self):
        for bad in (None, 5, b"x", ["x"]):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    normalize_failure_message(bad)
                self.assertEqual(caught.exception.parameter, "message")


class SignatureTest(unittest.TestCase):
    def test_golden_values_are_stable(self):
        # sha256("ToolError\x1frun_tests\x1ftimeout after <n>s on <hex>")
        self.assertEqual(
            failure_signature("ToolError", "run_tests", "Timeout after 30s on 0x7FFF"),
            "e8deabc6dfab7d7589e92aa2700d0a163c45ac2e11022ef3e9e7feef902ebcfd",
        )
        # sha256("ValueError\x1fstep\x1fboom")
        self.assertEqual(
            failure_signature("ValueError", "step", "boom"),
            "206c806eaa7c3c6974bc1db1d59e6cb29267ebbfaa8b7103624613577e594e0a",
        )
        # sha256("ValueError\x1fstep\x1f")  (an empty message is allowed)
        self.assertEqual(
            failure_signature("ValueError", "step", ""),
            "2c02bacdd4b42ab240b63201cfad1c08cc918c103a77962cae7167e5df9b5a2d",
        )

    def test_format_and_determinism(self):
        first = failure_signature("E", "s", "m")
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(first, failure_signature("E", "s", "m"))

    def test_messages_that_normalise_alike_share_a_signature(self):
        base = failure_signature(
            "ToolError", "run_tests", "Timeout after 30s on 0x7FFF"
        )
        for message in (
            "timeout   after 45s on 0xABCDEF",
            "TIMEOUT AFTER 7s\non 0x1",
            "Timeout after 30s on 0x7FFF",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    failure_signature("ToolError", "run_tests", message), base
                )

    def test_different_failures_have_different_signatures(self):
        base = failure_signature("ToolError", "run_tests", "Timeout after 30s")
        others = [
            failure_signature("toolerror", "run_tests", "Timeout after 30s"),
            failure_signature("OtherError", "run_tests", "Timeout after 30s"),
            failure_signature("ToolError", "Run_tests", "Timeout after 30s"),
            failure_signature("ToolError", "build", "Timeout after 30s"),
            failure_signature("ToolError", "run_tests", "Timeout after 30s!"),
            failure_signature("ToolError", "run_tests", "Connection refused"),
            failure_signature("ToolError", "run_tests", ""),
        ]
        self.assertNotIn(base, others)
        self.assertEqual(len(set(others)), len(others))

    def test_the_fields_cannot_be_shifted_into_each_other(self):
        self.assertNotEqual(
            failure_signature("ab", "c", "x"), failure_signature("a", "bc", "x")
        )
        self.assertNotEqual(
            failure_signature("a", "b", "c d"), failure_signature("a", "b c", "d")
        )

    def test_error_class_and_step_are_not_normalised(self):
        self.assertNotEqual(
            failure_signature("Error1", "s", "m"), failure_signature("Error2", "s", "m")
        )
        self.assertNotEqual(
            failure_signature("E", "step 1", "m"), failure_signature("E", "step 2", "m")
        )

    def test_only_the_first_2000_characters_of_the_message_count(self):
        prefix = "y" * 2000
        self.assertEqual(
            failure_signature("E", "s", prefix + "one"),
            failure_signature("E", "s", prefix + "another ending"),
        )
        self.assertEqual(
            failure_signature("E", "s", prefix * 500),
            failure_signature("E", "s", prefix),
        )
        self.assertNotEqual(
            failure_signature("E", "s", "y" * 1999 + "q"),
            failure_signature("E", "s", "y" * 1999 + "r"),
        )

    def test_invalid_arguments_are_named_and_never_echoed(self):
        secret = "hunter2-token"
        cases = [
            ("error_class", ("", "s", "m")),
            ("error_class", ("  ", "s", "m")),
            ("error_class", ("E" * 201, "s", "m")),
            ("error_class", ("E\nF", "s", "m")),
            ("error_class", (None, "s", "m")),
            ("step", ("E", "", "m")),
            ("step", ("E", "s" * 101, "m")),
            ("step", ("E", "s\x1fs", "m")),
            ("step", ("E", 5, "m")),
            ("message", ("E", "s", None)),
            ("message", ("E", "s", 5)),
            ("message", ("E", "s", b"m")),
            ("error_class", (secret + "\n", "s", "m")),
        ]
        for parameter, args in cases:
            with self.subTest(parameter=parameter, args=args):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    failure_signature(*args)
                self.assertEqual(caught.exception.parameter, parameter)
                self.assertNotIn(secret, str(caught.exception))

    def test_text_that_utf8_cannot_encode_raises_the_typed_error_not_a_raw_one(self):
        # ``json.loads`` yields a lone surrogate for the JSON string "\\ud800".
        lone = json.loads('"\\ud800"')
        self.assertEqual(lone, "\ud800")
        secret = "hunter2-token"
        cases = [
            ("message", ("E", "s", lone)),
            ("message", ("E", "s", secret + lone)),
            ("message", ("E", "s", "m" * 3000 + lone)),  # beyond the used prefix
            ("error_class", (secret + lone, "s", "m")),
            ("step", ("E", secret + lone, "m")),
        ]
        for parameter, args in cases:
            with self.subTest(parameter=parameter, args=ascii(args)[-40:]):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    failure_signature(*args)
                error = caught.exception
                self.assertEqual(error.parameter, parameter)
                self.assertNotIn(secret, str(error) + repr(error))
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)  # no UnicodeEncodeError inside
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            normalize_failure_message(lone)
        self.assertEqual(caught.exception.parameter, "message")

    def test_a_signature_of_valid_text_is_computed_from_its_utf8_encoding(self):
        text = "caf\u00e9 \U0001f600 timeout"
        expected = hashlib.sha256(
            f"E\x1fs\x1f{normalize_failure_message(text)}".encode()
        ).hexdigest()
        self.assertEqual(failure_signature("E", "s", text), expected)


class RecordFailureValidationTest(unittest.IsolatedAsyncioTestCase):
    """``record_failure`` refuses unencodable text before it touches the database:
    the detector is built on an object that is not a database, so any database use
    would fail with another error than the typed one."""

    async def test_unencodable_text_is_refused_with_the_typed_error(self):
        detector = LoopDetector(object())
        lone = json.loads('"\\ud800"')
        secret = "hunter2-token"
        cases = [
            ("message", dict(message=secret + lone)),
            ("message", dict(message="m" * 3000 + lone)),
            ("error_class", dict(error_class=secret + lone)),
            ("step", dict(step=secret + lone)),
            ("error_class", dict(error_class="E\x00")),
            ("step", dict(step="s\x00")),
        ]
        for parameter, overrides in cases:
            arguments = dict(attempt=1, error_class="E", step="s", message="m")
            arguments.update(overrides)
            with self.subTest(parameter=parameter, overrides=ascii(overrides)[-40:]):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    await detector.record_failure(uuid.uuid4(), **arguments)
                error = caught.exception
                self.assertEqual(error.parameter, parameter)
                self.assertNotIn(secret, str(error) + repr(error))
                self.assertIsNone(error.__context__)


class EvaluateLoopTest(unittest.TestCase):
    def assess(self, history, policy=DEFAULT_LOOP_POLICY):
        return evaluate_loop(history, policy)

    def test_an_empty_history_continues(self):
        self.assertEqual(self.assess([]), LoopAssessment(V.CONTINUE, None, None, 0))
        self.assertEqual(self.assess(()), LoopAssessment(V.CONTINUE, None, None, 0))

    def test_repeats_below_the_threshold_continue(self):
        self.assertEqual(self.assess([r(A)]), LoopAssessment(V.CONTINUE, A, 0, 1))
        self.assertEqual(self.assess([r(A), r(A)]), LoopAssessment(V.CONTINUE, A, 0, 2))

    def test_the_threshold_switches_to_an_alternative_approach(self):
        self.assertEqual(
            self.assess([r(A)] * 3), LoopAssessment(V.TRY_ALTERNATIVE, A, 0, 3)
        )
        self.assertEqual(
            self.assess([r(A)] * 4), LoopAssessment(V.TRY_ALTERNATIVE, A, 0, 4)
        )

    def test_a_different_last_failure_breaks_the_streak(self):
        self.assertEqual(
            self.assess([r(A), r(A), r(B)]), LoopAssessment(V.CONTINUE, B, 0, 1)
        )
        self.assertEqual(
            self.assess([r(A), r(A), r(A), r(B)]),
            LoopAssessment(V.CONTINUE, B, 0, 1),
        )
        self.assertEqual(
            self.assess([r(B), r(A), r(A), r(A)]),
            LoopAssessment(V.TRY_ALTERNATIVE, A, 0, 3),
        )

    def test_repeats_need_not_be_consecutive(self):
        self.assertEqual(
            self.assess([r(A), r(B), r(A), r(B), r(A)]),
            LoopAssessment(V.TRY_ALTERNATIVE, A, 0, 3),
        )
        self.assertEqual(
            self.assess([r(A), r(A), r(B), r(C), r(A)]),
            LoopAssessment(V.TRY_ALTERNATIVE, A, 0, 3),
        )

    def test_an_alternative_that_keeps_failing_escalates(self):
        history = [r(A, 0)] * 3
        self.assertEqual(
            self.assess([*history, r(A, 1)]), LoopAssessment(V.CONTINUE, A, 1, 1)
        )
        self.assertEqual(
            self.assess([*history, r(A, 1), r(A, 1)]),
            LoopAssessment(V.CONTINUE, A, 1, 2),
        )
        self.assertEqual(
            self.assess([*history, *[r(A, 1)] * 3]),
            LoopAssessment(V.ESCALATE, A, 1, 3),
        )
        self.assertEqual(
            self.assess([*history, *[r(A, 1)] * 4]),
            LoopAssessment(V.ESCALATE, A, 1, 4),
        )

    def test_failures_of_an_earlier_approach_do_not_count_toward_the_new_one(self):
        history = [r(A, 0)] * 5 + [r(B, 1), r(A, 1)]
        self.assertEqual(self.assess(history), LoopAssessment(V.CONTINUE, A, 1, 1))

    def test_a_new_kind_of_failure_in_the_alternative_is_not_a_loop(self):
        history = [r(A, 0)] * 3 + [r(A, 1), r(A, 1), r(B, 1)]
        self.assertEqual(self.assess(history), LoopAssessment(V.CONTINUE, B, 1, 1))

    def test_beyond_the_alternatives_the_verdict_stays_escalate(self):
        self.assertEqual(
            self.assess([r(A, 2)] * 3), LoopAssessment(V.ESCALATE, A, 2, 3)
        )
        self.assertEqual(
            self.assess([r(A, 100)] * 3), LoopAssessment(V.ESCALATE, A, 100, 3)
        )

    def test_only_the_most_recent_window_counts(self):
        # 11 records: the first A falls out of the window of 10.
        outside = [r(A), r(A)] + [r(B)] * 8 + [r(A)]
        self.assertEqual(len(outside), 11)
        self.assertEqual(self.assess(outside), LoopAssessment(V.CONTINUE, A, 0, 2))
        # 10 records: all inside the window.
        inside = [r(A), r(A)] + [r(B)] * 7 + [r(A)]
        self.assertEqual(len(inside), 10)
        self.assertEqual(
            self.assess(inside), LoopAssessment(V.TRY_ALTERNATIVE, A, 0, 3)
        )

    def test_repeats_are_capped_by_the_window(self):
        self.assertEqual(
            self.assess([r(A)] * 25), LoopAssessment(V.TRY_ALTERNATIVE, A, 0, 10)
        )

    def test_a_very_long_history_is_handled(self):
        history = [r(B)] * 10_000 + [r(A)] * 3
        self.assertEqual(
            self.assess(history), LoopAssessment(V.TRY_ALTERNATIVE, A, 0, 3)
        )

    def test_the_policy_changes_the_thresholds(self):
        strict = LoopPolicy(repeat_threshold=2, window_size=5, max_alternatives=0)
        self.assertEqual(
            self.assess([r(A), r(A)], strict), LoopAssessment(V.ESCALATE, A, 0, 2)
        )
        patient = LoopPolicy(repeat_threshold=4, window_size=10, max_alternatives=2)
        self.assertEqual(
            self.assess([r(A)] * 3, patient), LoopAssessment(V.CONTINUE, A, 0, 3)
        )
        self.assertEqual(
            self.assess([r(A, 0)] * 4, patient),
            LoopAssessment(V.TRY_ALTERNATIVE, A, 0, 4),
        )
        self.assertEqual(
            self.assess([r(A, 1)] * 4, patient),
            LoopAssessment(V.TRY_ALTERNATIVE, A, 1, 4),
        )
        self.assertEqual(
            self.assess([r(A, 2)] * 4, patient), LoopAssessment(V.ESCALATE, A, 2, 4)
        )
        narrow = LoopPolicy(repeat_threshold=3, window_size=3, max_alternatives=1)
        self.assertEqual(
            self.assess([r(A), r(A), r(B), r(A), r(A)], narrow),
            LoopAssessment(V.CONTINUE, A, 0, 2),
        )

    def test_the_last_record_decides_even_if_its_approach_is_lower(self):
        history = [r(A, 1)] * 3 + [r(A, 0)]
        self.assertEqual(self.assess(history), LoopAssessment(V.CONTINUE, A, 0, 1))

    def test_evaluation_is_idempotent_and_does_not_modify_its_input(self):
        history = [r(A), r(B), r(A), r(A)]
        snapshot = list(history)
        first = self.assess(history)
        second = self.assess(history)
        self.assertEqual(first, second)
        self.assertEqual(first, LoopAssessment(V.TRY_ALTERNATIVE, A, 0, 3))
        self.assertEqual(history, snapshot)
        self.assertEqual(self.assess(tuple(history)), first)

    def test_only_lists_and_tuples_of_failure_records_are_accepted(self):
        good = [r(A)]
        bad_histories = [
            None,
            "abc",
            {r(A)},
            {0: r(A)},
            (x for x in good),
            [(A, 0)],
            [{"signature": A, "approach": 0}],
            [r(A), None],
            [A],
        ]
        for history in bad_histories:
            with self.subTest(history=repr(history)):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    evaluate_loop(history)
                self.assertEqual(caught.exception.parameter, "history")

    def test_every_element_is_validated_even_outside_the_window(self):
        history = ["not a record"] + [r(A)] * 20
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            evaluate_loop(history)
        self.assertEqual(caught.exception.parameter, "history")

    def test_the_policy_must_be_a_loop_policy(self):
        for bad in (None, {"repeat_threshold": 3}, 3):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    evaluate_loop([r(A)], bad)
                self.assertEqual(caught.exception.parameter, "policy")


if __name__ == "__main__":
    unittest.main()
