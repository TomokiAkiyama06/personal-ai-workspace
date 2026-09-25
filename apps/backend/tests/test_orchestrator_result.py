"""The structured result a node passes on, and the bounded JSON it is made of."""

import unittest

from paw_backend.orchestrator.errors import InvalidNodeResultError, ResultReason
from paw_backend.orchestrator.jsonvalue import JsonProblem, check_json_object
from paw_backend.orchestrator.limits import (
    MAX_CHANGED_FILES,
    MAX_COMMIT_CHARS,
    MAX_ITEM_CHARS,
    MAX_LIST_ITEMS,
    MAX_PATH_CHARS,
    MAX_RESULT_BYTES,
    MAX_SUMMARY_CHARS,
    MAX_TEST_RESULT_BYTES,
)
from paw_backend.orchestrator.result import NodeResult, upstream_size

R = ResultReason


class RefusedResultsTest(unittest.TestCase):
    def assertRefused(self, data, reason: ResultReason):
        with self.assertRaises(InvalidNodeResultError) as caught:
            NodeResult.from_json(data)
        self.assertEqual(caught.exception.reason, reason)

    def test_every_bad_result_is_refused_for_its_reason(self):
        many = ["f"] * (MAX_LIST_ITEMS + 1)
        cases = [
            ("a list", [], R.NOT_A_RESULT),
            ("None", None, R.NOT_A_RESULT),
            ("no summary", {}, R.MISSING_FIELD),
            ("a misspelled field", {"summary": "s", "sumary": "x"}, R.UNKNOWN_FIELD),
            ("a number as a field name", {"summary": "s", 1: "x"}, R.UNKNOWN_FIELD),
            ("a summary that is a number", {"summary": 5}, R.BAD_TYPE),
            ("a blank summary", {"summary": " \n"}, R.BAD_TEXT),
            ("a long summary", {"summary": "s" * (MAX_SUMMARY_CHARS + 1)}, R.BAD_TEXT),
            ("a summary with a NUL", {"summary": "a\x00"}, R.BAD_TEXT),
            ("a summary with a surrogate", {"summary": "a\ud800"}, R.BAD_TEXT),
            ("files as text", {"summary": "s", "changed_files": "a.py"}, R.BAD_TYPE),
            (
                "files as a mapping",
                {"summary": "s", "changed_files": {"a": 1}},
                R.BAD_TYPE,
            ),
            (
                "a file that is a number",
                {"summary": "s", "changed_files": [1]},
                R.BAD_TYPE,
            ),
            (
                "a file with a newline",
                {"summary": "s", "changed_files": ["a\nb"]},
                R.BAD_TEXT,
            ),
            (
                "a long path",
                {"summary": "s", "changed_files": ["p" * (MAX_PATH_CHARS + 1)]},
                R.BAD_TEXT,
            ),
            (
                "too many files",
                {"summary": "s", "changed_files": ["f"] * (MAX_CHANGED_FILES + 1)},
                R.TOO_MANY_ITEMS,
            ),
            (
                "too many facts",
                {"summary": "s", "discovered_facts": many},
                R.TOO_MANY_ITEMS,
            ),
            (
                "too many notes",
                {"summary": "s", "dependency_notes": many},
                R.TOO_MANY_ITEMS,
            ),
            (
                "too many questions",
                {"summary": "s", "unresolved_questions": many},
                R.TOO_MANY_ITEMS,
            ),
            (
                "too many artifacts",
                {"summary": "s", "artifacts": many},
                R.TOO_MANY_ITEMS,
            ),
            (
                "a long fact",
                {"summary": "s", "discovered_facts": ["x" * (MAX_ITEM_CHARS + 1)]},
                R.BAD_TEXT,
            ),
            ("a blank fact", {"summary": "s", "discovered_facts": [" "]}, R.BAD_TEXT),
            (
                "a long commit",
                {"summary": "s", "commit": "c" * (MAX_COMMIT_CHARS + 1)},
                R.BAD_TEXT,
            ),
            ("a commit that is a number", {"summary": "s", "commit": 5}, R.BAD_TYPE),
            (
                "a test result that is a list",
                {"summary": "s", "test_result": [1]},
                R.BAD_TYPE,
            ),
            (
                "a test result with NaN",
                {"summary": "s", "test_result": {"ok": float("nan")}},
                R.BAD_TYPE,
            ),
            (
                "a test result that is too large",
                {
                    "summary": "s",
                    "test_result": {"log": "x" * (MAX_TEST_RESULT_BYTES + 1)},
                },
                R.TOO_LARGE,
            ),
            (
                "a confidence above 1",
                {"summary": "s", "confidence": 1.01},
                R.BAD_NUMBER,
            ),
            (
                "a confidence below 0",
                {"summary": "s", "confidence": -0.1},
                R.BAD_NUMBER,
            ),
            (
                "a confidence of NaN",
                {"summary": "s", "confidence": float("nan")},
                R.BAD_NUMBER,
            ),
            (
                "a confidence of infinity",
                {"summary": "s", "confidence": float("inf")},
                R.BAD_NUMBER,
            ),
            (
                "a confidence that is a bool",
                {"summary": "s", "confidence": True},
                R.BAD_NUMBER,
            ),
            (
                "a confidence that is text",
                {"summary": "s", "confidence": "0.5"},
                R.BAD_NUMBER,
            ),
            (
                "a result whose whole JSON is too large",
                {
                    "summary": "s",
                    "discovered_facts": ["x" * MAX_ITEM_CHARS] * MAX_LIST_ITEMS,
                    "dependency_notes": ["x" * MAX_ITEM_CHARS] * MAX_LIST_ITEMS,
                },
                R.TOO_LARGE,
            ),
        ]
        for label, data, reason in cases:
            with self.subTest(label):
                self.assertRefused(data, reason)

    def test_the_error_never_quotes_the_result(self):
        with self.assertRaises(InvalidNodeResultError) as caught:
            NodeResult.from_json({"summary": "s", "secret_field": "hunter2"})
        self.assertNotIn("hunter2", str(caught.exception))
        self.assertNotIn("secret_field", str(caught.exception))


class AcceptedResultsTest(unittest.TestCase):
    def test_a_result_with_only_a_summary_is_accepted(self):
        result = NodeResult("  done  ")
        self.assertEqual(result.summary, "done")
        self.assertEqual(result.changed_files, ())
        self.assertIsNone(result.commit)
        self.assertIsNone(result.confidence)

    def test_a_full_result_round_trips_through_its_storage_form(self):
        result = NodeResult(
            summary="Implemented the parser",
            changed_files=["src/parser.py", "tests/test_parser.py"],
            commit="a" * 40,
            test_result={
                "passed": 12,
                "failed": 0,
                "cases": [{"name": "t", "ok": True}],
            },
            discovered_facts=["The grammar is LL(1)"],
            dependency_notes=["Needs the lexer of node lex"],
            unresolved_questions=["Should tabs be allowed?"],
            confidence=1,
            artifacts=["artifact://build/parser.whl"],
        )

        stored = result.to_json()

        self.assertEqual(set(stored), set(NodeResult.__dataclass_fields__))
        self.assertEqual(NodeResult.from_json(stored), result)
        self.assertEqual(result.confidence, 1.0)
        self.assertIsInstance(result.confidence, float)
        self.assertEqual(result.changed_files[0], "src/parser.py")

    def test_results_at_the_limits_are_accepted(self):
        # Each limit alone; together they would exceed the size of a whole result,
        # which is the binding limit (the next test).
        NodeResult("s" * MAX_SUMMARY_CHARS)
        NodeResult("s", changed_files=["p" * 10] * MAX_CHANGED_FILES)
        NodeResult("s", changed_files=["p" * MAX_PATH_CHARS] * 20)
        NodeResult("s", commit="c" * MAX_COMMIT_CHARS, confidence=0)
        NodeResult("s", discovered_facts=["f"] * MAX_LIST_ITEMS)
        NodeResult("s", discovered_facts=["f" * MAX_ITEM_CHARS] * 20)
        NodeResult("s", test_result={"t": "x" * (MAX_TEST_RESULT_BYTES - 8)})

    def test_the_size_of_the_whole_result_is_the_binding_limit(self):
        NodeResult("s", dependency_notes=["n" * MAX_ITEM_CHARS] * 30)
        with self.assertRaises(InvalidNodeResultError) as caught:
            NodeResult("s", dependency_notes=["n" * MAX_ITEM_CHARS] * 33)
        self.assertEqual(caught.exception.reason, R.TOO_LARGE)

    def test_the_result_is_detached_from_the_callers_containers(self):
        inner = {"passed": [1]}
        files = ["a.py"]
        result = NodeResult("s", changed_files=files, test_result=inner)

        files.append("b.py")
        inner["passed"].append(2)

        self.assertEqual(result.changed_files, ("a.py",))
        self.assertEqual(result.test_result, {"passed": [1]})

    def test_the_size_of_what_a_node_receives_is_the_sum_of_its_dependencies(self):
        one = NodeResult("x" * 100)
        two = NodeResult("y" * 300)
        sizes = [upstream_size({"a": one}), upstream_size({"b": two})]
        self.assertEqual(upstream_size({"a": one, "b": two}), sum(sizes))
        self.assertEqual(upstream_size({}), 0)
        self.assertLess(sizes[0], sizes[1])
        self.assertLessEqual(sizes[1], MAX_RESULT_BYTES)


class JsonValueTest(unittest.TestCase):
    def check(self, value, **overrides):
        limits = {"max_bytes": 1000, "max_depth": 4}
        limits.update(overrides)
        return check_json_object(value, **limits)

    def kind(self, value, **overrides) -> str:
        with self.assertRaises(JsonProblem) as caught:
            self.check(value, **overrides)
        return caught.exception.kind

    def test_plain_json_is_copied(self):
        source = {"a": [1, 2.5, "x", True, None, {"b": (3, 4)}]}
        clean = self.check(source)
        self.assertEqual(clean, {"a": [1, 2.5, "x", True, None, {"b": [3, 4]}]})
        source["a"].append(9)
        self.assertNotIn(9, clean["a"])

    def test_every_bad_value_is_refused_with_its_kind(self):
        class Str(str):
            pass

        class Int(int):
            pass

        cases = [
            ("a list at the top", [1], "type"),
            ("a dict subclass at the top", type("D", (dict,), {})(), "type"),
            ("a set", {"a": {1}}, "type"),
            ("bytes", {"a": b"x"}, "type"),
            ("an object", {"a": object()}, "type"),
            ("a number key", {1: "a"}, "type"),
            ("a str subclass", {"a": Str("x")}, "type"),
            ("an int subclass", {"a": Int(1)}, "number"),
            ("NaN", {"a": float("nan")}, "number"),
            ("infinity", {"a": float("inf")}, "number"),
            ("a huge integer", {"a": 2**64}, "number"),
            ("a NUL", {"a": "x\x00"}, "text"),
            ("a NUL in a key", {"a\x00": 1}, "text"),
            ("a surrogate", {"a": "\ud800"}, "text"),
            ("a very long key", {"k" * 201: 1}, "text"),
            ("too deep", {"a": {"b": {"c": {"d": {"e": 1}}}}}, "depth"),
            ("too large", {"a": "x" * 1000}, "size"),
            ("a list of many small items", {"a": [1] * 600}, "size"),
        ]
        for label, value, kind in cases:
            with self.subTest(label):
                self.assertEqual(self.kind(value), kind)

    def test_a_self_referencing_value_is_refused_not_followed(self):
        loop: dict = {}
        loop["me"] = loop
        self.assertEqual(self.kind(loop), "depth")
        cycle: list = []
        cycle.append(cycle)
        self.assertEqual(self.kind({"a": cycle}), "depth")

    def test_shared_substructure_is_charged_each_time_it_is_visited(self):
        shared = ["x" * 50]
        value = {"a": [shared] * 30}  # small to write down, large once expanded
        self.assertEqual(self.kind(value), "size")

    def test_the_walk_stops_as_soon_as_the_size_is_exceeded(self):
        # Not only the final encoding is checked: the walk itself is bounded, so a
        # value that repeats one list a hundred thousand times costs about as much
        # as the size limit allows, not a hundred thousand steps.
        from unittest.mock import patch

        from paw_backend.orchestrator import jsonvalue

        visits = []
        original = jsonvalue._Walker.walk

        def counting(self, value, depth):
            visits.append(1)
            return original(self, value, depth)

        with patch.object(jsonvalue._Walker, "walk", counting):
            self.assertEqual(self.kind({"a": [1] * 100_000}), "size")
        self.assertLess(len(visits), 1000)

    def test_the_limits_are_inclusive(self):
        exact = {"a": "x" * (1000 - 8)}
        self.assertEqual(len(str(self.check(exact))), len(str(exact)))
        self.assertEqual(self.kind({"a": "x" * (1000 - 7)}), "size")
        self.check({"a": {"b": {"c": {}}}})  # four levels
        self.assertEqual(self.kind({"a": {"b": {"c": {"d": {}}}}}), "depth")

    def test_wide_characters_count_by_their_encoded_size(self):
        # 3 bytes each in UTF-8: 400 characters are 1200 bytes.
        self.assertEqual(self.kind({"a": "あ" * 400}), "size")


if __name__ == "__main__":
    unittest.main()
