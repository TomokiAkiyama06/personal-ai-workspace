"""``Secret``: a credential that cannot be printed, copied or pickled by accident."""

import copy
import json
import logging
import pickle
import unittest
from dataclasses import dataclass

from paw_backend.connections import InputProblem, InvalidConnectionInputError, Secret
from paw_backend.connections.secret import REDACTED_SECRET

from .connections_fakes import CANARY


class SecretTextTest(unittest.TestCase):
    def setUp(self):
        self.secret = Secret(CANARY)

    def assertNoCanary(self, text: str) -> None:
        self.assertNotIn(CANARY, text)
        self.assertNotIn(CANARY[:12], text)

    def test_the_value_is_available_to_an_adapter(self):
        self.assertEqual(self.secret.reveal(), CANARY)

    def test_every_usual_way_of_printing_it_shows_a_fixed_text(self):
        for name, text in {
            "repr": repr(self.secret),
            "str": str(self.secret),
            "format": format(self.secret),
            "format spec": format(self.secret, ">40"),
            "f-string": f"{self.secret}",
            "f-string !r": f"{self.secret!r}",
            "percent s": "%s" % self.secret,  # noqa: UP031
            "percent r": "%r" % self.secret,  # noqa: UP031
            "str.format": "{}".format(self.secret),  # noqa: UP032
            "bytes": bytes(self.secret).decode(),
            "list": str([self.secret]),
            "dict": str({"key": self.secret}),
            "tuple": repr((self.secret,)),
        }.items():
            with self.subTest(way=name):
                self.assertNoCanary(text)
                self.assertIn("Secret(<redacted>)", text)

    def test_a_dataclass_holding_it_prints_no_value(self):
        @dataclass
        class Holder:
            secret: Secret

        self.assertNoCanary(repr(Holder(self.secret)))

    def test_logging_it_prints_no_value(self):
        with self.assertLogs("secret-test", logging.DEBUG) as logs:
            logger = logging.getLogger("secret-test")
            logger.info("with %s", self.secret)
            logger.info("with %r", self.secret)
            logger.info("with %(s)s", {"s": self.secret})
        self.assertNoCanary("\n".join(logs.output))

    def test_it_cannot_be_serialised_as_json(self):
        with self.assertRaises(TypeError):
            json.dumps({"secret": self.secret})

    def test_it_has_no_attribute_dictionary_to_dump(self):
        with self.assertRaises(TypeError):
            vars(self.secret)
        self.assertFalse(hasattr(self.secret, "__dict__"))

    def test_it_cannot_be_pickled_in_any_protocol(self):
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            with self.subTest(protocol=protocol):
                with self.assertRaises(TypeError):
                    pickle.dumps(self.secret, protocol=protocol)

    def test_it_cannot_be_copied(self):
        with self.assertRaises(TypeError):
            copy.copy(self.secret)
        with self.assertRaises(TypeError):
            copy.deepcopy(self.secret)
        with self.assertRaises(TypeError):
            copy.deepcopy({"holder": [self.secret]})

    def test_it_cannot_be_changed_or_deleted(self):
        with self.assertRaises(AttributeError):
            self.secret._value = "other"
        with self.assertRaises(AttributeError):
            self.secret.other = "x"
        with self.assertRaises(AttributeError):
            del self.secret._value
        self.assertEqual(self.secret.reveal(), CANARY)

    def test_equality_is_identity_never_the_value(self):
        # A comparison that looked at the value would be an oracle for guessing it.
        self.assertEqual(self.secret, self.secret)
        self.assertNotEqual(self.secret, Secret(CANARY))
        self.assertNotEqual(self.secret, CANARY)
        self.assertEqual(len({self.secret, Secret(CANARY)}), 2)

    def test_a_subclass_cannot_bypass_the_text_protection(self):
        self.assertEqual(Secret.__slots__, ("_value", "_needle"))


class SecretScrubTest(unittest.TestCase):
    def test_every_exact_occurrence_is_replaced(self):
        secret = Secret(CANARY)
        text = f"start {CANARY} middle {CANARY}{CANARY} end"
        self.assertEqual(
            secret.scrub(text),
            f"start {REDACTED_SECRET} middle {REDACTED_SECRET}{REDACTED_SECRET} end",
        )

    def test_a_text_without_the_value_is_unchanged(self):
        self.assertEqual(Secret(CANARY).scrub("nothing here"), "nothing here")

    def test_a_partial_value_is_not_the_value(self):
        self.assertEqual(Secret(CANARY).scrub(CANARY[:-1]), CANARY[:-1])


class SecretConstructionTest(unittest.TestCase):
    def test_a_bad_value_is_refused_with_a_closed_problem_and_without_the_value(self):
        cases = [
            (None, InputProblem.NOT_A_STRING),
            (b"bytes", InputProblem.NOT_A_STRING),
            (123, InputProblem.NOT_A_STRING),
            ("", InputProblem.EMPTY),
            ("x" * 16_385, InputProblem.TOO_LONG),
            ("a\x00b", InputProblem.INVALID_CHARACTERS),
            ("a\ud800b", InputProblem.INVALID_CHARACTERS),
        ]
        for value, problem in cases:
            with self.subTest(value=repr(value)[:20]):
                with self.assertRaises(InvalidConnectionInputError) as caught:
                    Secret(value)
                self.assertEqual(caught.exception.field, "secret")
                self.assertEqual(caught.exception.problem, problem)
                self.assertNotIn("x" * 20, str(caught.exception))

    def test_a_str_subclass_is_refused(self):
        class Sneaky(str):
            def replace(self, *args):  # pragma: no cover - must never run
                return "leak"

        with self.assertRaises(InvalidConnectionInputError):
            Secret(Sneaky("value"))

    def test_the_longest_accepted_value(self):
        self.assertEqual(Secret("x" * 16_384).reveal(), "x" * 16_384)

    def test_a_multi_line_or_unicode_value_is_a_value(self):
        self.assertEqual(Secret("line1\nline2 日本").reveal(), "line1\nline2 日本")


if __name__ == "__main__":
    unittest.main()
