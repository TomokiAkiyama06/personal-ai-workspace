"""``TaskService._checked_input`` reads the caller's object exactly once.

The check (bounded depth, bounded work, plain JSON types only) and the copy that
is stored must come from the SAME walk. Otherwise another thread that changes the
caller's object between a separate check pass and a separate encode pass (a small
list replaced by a tuple, an integer-keyed ``dict`` or a huge value) gets
something stored that was never checked, or makes the encoder do the unbounded
work the check exists to prevent (issue #90, finding on #70).

The race is injected deterministically: the walk is wrapped so that the caller's
object is changed right after the walk returns, which is the window a second
thread would need. No database is used.
"""

import json
import unittest
from unittest import mock

from paw_backend.tasks import InvalidCommandArgumentError, TaskService
from paw_backend.tasks import service as service_module

MAX_INPUT_BYTES = service_module.MAX_INPUT_BYTES


def change_after_the_check(change):
    """Patch the walk so that ``change(caller_value)`` runs as soon as it ends."""
    real = service_module._JsonInputCheck.check_object

    def check_then_change(self, value):
        result = real(self, value)
        change(value)
        return result

    return mock.patch.object(
        service_module._JsonInputCheck, "check_object", check_then_change
    )


class CheckedInputSnapshotTest(unittest.TestCase):
    def test_a_change_after_the_check_is_not_stored(self):
        def to_a_tuple(value):
            value["a"] = (1, 2, 3)

        def to_an_integer_keyed_dict(value):
            value["a"] = {5: "never checked"}

        def to_a_huge_text(value):
            value["a"] = "x" * (2 * MAX_INPUT_BYTES)

        def by_adding_a_key(value):
            value["late"] = {"never": "checked"}

        cases = {
            "list replaced by a tuple": ({"a": [1]}, to_a_tuple),
            "object replaced by an integer-keyed dict": (
                {"a": {"k": 1}},
                to_an_integer_keyed_dict,
            ),
            "small value replaced by a huge one": ({"a": [1]}, to_a_huge_text),
            "key added": ({"a": [1]}, by_adding_a_key),
        }
        for name, (payload, change) in cases.items():
            with self.subTest(name):
                checked = json.loads(json.dumps(payload))
                with change_after_the_check(change):
                    stored = TaskService._checked_input(payload)
                self.assertEqual(stored, checked)

    def test_the_encoder_is_given_the_checked_copy_never_the_callers_object(self):
        payload = {"a": [1, {"b": "x"}], "c": {"d": [2.5, None, True]}}
        with mock.patch.object(service_module.json, "dumps", wraps=json.dumps) as dumps:
            stored = TaskService._checked_input(payload)
        dumps.assert_called_once()
        encoded_object = dumps.call_args.args[0]
        self.assertIsNot(encoded_object, payload)
        self.assertEqual(encoded_object, payload)
        self.assertEqual(stored, payload)
        # Nothing of the caller's is shared with what is stored or encoded.
        for original, kept in (
            (payload["a"], stored["a"]),
            (payload["a"][1], stored["a"][1]),
            (payload["c"], stored["c"]),
            (payload["c"]["d"], stored["c"]["d"]),
        ):
            self.assertIsNot(original, kept)

    def test_the_value_is_still_checked_and_the_byte_limit_still_applies(self):
        # The single walk keeps every rule of the separate check.
        cases = {
            "tuple": {"a": (1,)},
            "integer key": {"a": {1: 2}},
            "nan": {"a": float("nan")},
            "nul": {"a": "x\x00"},
            "surrogate": {"a": "\ud800"},
            "not a dict": [],
            "one byte over": {"b": "x" * MAX_INPUT_BYTES},
        }
        for name, value in cases.items():
            with self.subTest(name), self.assertRaises(InvalidCommandArgumentError):
                TaskService._checked_input(value)
        self.assertEqual(TaskService._checked_input(None), {})

    def test_a_dict_changed_while_it_is_walked_is_a_typed_error(self):
        # An exact ``dict`` whose size changes during the iteration raises
        # ``RuntimeError`` in CPython; another thread does exactly that.
        payload = {str(n): n for n in range(50)}
        real = service_module._JsonInputCheck._check_text
        seen = []

        def check_text_and_grow(self, value):
            real(self, value)
            if not seen:
                seen.append(value)
                payload["late"] = 1

        with mock.patch.object(
            service_module._JsonInputCheck, "_check_text", check_text_and_grow
        ):
            with self.assertRaises(InvalidCommandArgumentError):
                TaskService._checked_input(payload)


if __name__ == "__main__":
    unittest.main()
