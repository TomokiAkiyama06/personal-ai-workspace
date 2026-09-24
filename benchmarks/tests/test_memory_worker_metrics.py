"""Tests for memory worker comparison and aggregation metrics."""

import unittest

from jsonschema import SchemaError

from benchmarks.memory_worker_metrics import (
    MEMORY_SCOPES,
    MemoryComparison,
    MemoryRecord,
    SchemaAdherence,
    aggregate,
    compare_memories,
    schema_adherence,
)


class MemoryRecordTest(unittest.TestCase):
    def test_memory_record_validation(self):
        # Valid records
        MemoryRecord("key1", "user", "confirmed", None)
        MemoryRecord("key2", "project", "inferred", "key1")

        # Test key validation
        with self.assertRaises(TypeError):
            MemoryRecord("", "user", "confirmed", None)
        with self.assertRaises(TypeError):
            MemoryRecord(123, "user", "confirmed", None)

        # Test scope validation
        with self.assertRaises(TypeError):
            MemoryRecord("key", "", "confirmed", None)
        with self.assertRaises(TypeError):
            MemoryRecord("key", 123, "confirmed", None)

        # Test state validation
        with self.assertRaises(TypeError):
            MemoryRecord("key", "user", "", None)
        with self.assertRaises(TypeError):
            MemoryRecord("key", "user", 123, None)
        with self.assertRaises(ValueError):
            MemoryRecord("key", "user", "invalid", None)

        # Test supersedes validation
        with self.assertRaises(TypeError):
            MemoryRecord("key", "user", "confirmed", 123)

    def test_scope_must_be_a_defined_visibility_class(self):
        # REQUIREMENTS.md: scope is User / Project / Repo / Shared. A topic such
        # as "schedule" or a different spelling must not be scorable as a scope.
        self.assertEqual(MEMORY_SCOPES, {"user", "project", "repo", "shared"})
        for scope in sorted(MEMORY_SCOPES):
            self.assertEqual(MemoryRecord("k", scope, "confirmed", None).scope, scope)
        for scope in ("schedule", "user_preferences", "User", "user "):
            with self.subTest(scope=scope), self.assertRaises(ValueError) as caught:
                MemoryRecord("k", scope, "confirmed", None)
            self.assertNotIn(scope, str(caught.exception))

    def test_memory_record_equality(self):
        record1 = MemoryRecord("key1", "user", "confirmed", None)
        record2 = MemoryRecord("key1", "user", "confirmed", None)
        record3 = MemoryRecord("key2", "user", "confirmed", None)

        self.assertEqual(record1, record2)
        self.assertNotEqual(record1, record3)


class CompareMemoriesTest(unittest.TestCase):
    def test_all_matched_and_correct(self):
        gold = [
            MemoryRecord("key1", "user", "confirmed", None),
            MemoryRecord("key2", "project", "inferred", None),
        ]
        predicted = [
            MemoryRecord("key1", "user", "confirmed", None),
            MemoryRecord("key2", "project", "inferred", None),
        ]

        result = compare_memories(gold, predicted)
        expected = MemoryComparison(
            gold_count=2,
            predicted_count=2,
            matched=2,
            unneeded=0,
            scope_correct=2,
            state_correct=2,
            supersedes_correct=2,
        )
        self.assertEqual(result, expected)

    def test_missed_gold_memory(self):
        gold = [
            MemoryRecord("key1", "user", "confirmed", None),
            MemoryRecord("key2", "project", "inferred", None),
        ]
        predicted = [
            MemoryRecord("key1", "user", "confirmed", None),
            # Missing key2
        ]

        result = compare_memories(gold, predicted)
        expected = MemoryComparison(
            gold_count=2,
            predicted_count=1,
            matched=1,
            unneeded=0,
            scope_correct=1,
            state_correct=1,
            supersedes_correct=1,
        )
        self.assertEqual(result, expected)

    def test_extra_predicted_memory(self):
        gold = [
            MemoryRecord("key1", "user", "confirmed", None),
        ]
        predicted = [
            MemoryRecord("key1", "user", "confirmed", None),
            MemoryRecord("key2", "project", "inferred", None),
        ]

        result = compare_memories(gold, predicted)
        expected = MemoryComparison(
            gold_count=1,
            predicted_count=2,
            matched=1,
            unneeded=1,
            scope_correct=1,
            state_correct=1,
            supersedes_correct=1,
        )
        self.assertEqual(result, expected)

    def test_repeated_predicted_key(self):
        gold = [
            MemoryRecord("key1", "user", "confirmed", None),
        ]
        predicted = [
            MemoryRecord("key1", "user", "confirmed", None),
            MemoryRecord("key1", "user", "confirmed", None),  # repeated
        ]

        result = compare_memories(gold, predicted)
        expected = MemoryComparison(
            gold_count=1,
            predicted_count=2,
            matched=1,
            unneeded=1,  # repeated key counts as unneeded
            scope_correct=1,
            state_correct=1,
            supersedes_correct=1,
        )
        self.assertEqual(result, expected)

    def test_repeated_unknown_key_is_not_counted_twice(self):
        gold = [MemoryRecord("key1", "user", "confirmed", None)]
        predicted = [
            MemoryRecord("key1", "user", "confirmed", None),
            MemoryRecord("extra", "user", "inferred", None),
            MemoryRecord("extra", "user", "inferred", None),
            MemoryRecord("extra", "user", "inferred", None),
        ]

        result = compare_memories(gold, predicted)
        self.assertEqual(result.unneeded, 3)
        self.assertLessEqual(result.unneeded, result.predicted_count)
        self.assertAlmostEqual(aggregate([result])["unneeded_rate"], 3 / 4)

    def test_wrong_scope(self):
        gold = [
            MemoryRecord("key1", "user", "confirmed", None),
        ]
        predicted = [
            MemoryRecord("key1", "project", "confirmed", None),
        ]

        result = compare_memories(gold, predicted)
        expected = MemoryComparison(
            gold_count=1,
            predicted_count=1,
            matched=1,
            unneeded=0,
            scope_correct=0,  # Wrong scope
            state_correct=1,
            supersedes_correct=1,
        )
        self.assertEqual(result, expected)

    def test_wrong_state(self):
        gold = [
            MemoryRecord("key1", "user", "confirmed", None),
        ]
        predicted = [
            MemoryRecord("key1", "user", "inferred", None),
        ]

        result = compare_memories(gold, predicted)
        expected = MemoryComparison(
            gold_count=1,
            predicted_count=1,
            matched=1,
            unneeded=0,
            scope_correct=1,
            state_correct=0,  # Wrong state
            supersedes_correct=1,
        )
        self.assertEqual(result, expected)

    def test_wrong_supersedes(self):
        gold = [
            MemoryRecord("key1", "user", "confirmed", None),
        ]
        predicted = [
            MemoryRecord("key1", "user", "confirmed", "other_key"),
        ]

        result = compare_memories(gold, predicted)
        expected = MemoryComparison(
            gold_count=1,
            predicted_count=1,
            matched=1,
            unneeded=0,
            scope_correct=1,
            state_correct=1,
            supersedes_correct=0,  # Wrong supersedes
        )
        self.assertEqual(result, expected)

    def test_none_vs_value_supersedes(self):
        gold = [
            MemoryRecord("key1", "user", "confirmed", None),
        ]
        predicted = [
            MemoryRecord("key1", "user", "confirmed", "some_key"),
        ]

        result = compare_memories(gold, predicted)
        expected = MemoryComparison(
            gold_count=1,
            predicted_count=1,
            matched=1,
            unneeded=0,
            scope_correct=1,
            state_correct=1,
            supersedes_correct=0,  # None vs value
        )
        self.assertEqual(result, expected)

    def test_empty_gold_and_predicted(self):
        result = compare_memories([], [])
        expected = MemoryComparison(
            gold_count=0,
            predicted_count=0,
            matched=0,
            unneeded=0,
            scope_correct=0,
            state_correct=0,
            supersedes_correct=0,
        )
        self.assertEqual(result, expected)

    def test_empty_gold_but_predicted(self):
        predicted = [
            MemoryRecord("key1", "user", "confirmed", None),
        ]
        result = compare_memories([], predicted)
        expected = MemoryComparison(
            gold_count=0,
            predicted_count=1,
            matched=0,
            unneeded=1,  # Unneeded because no gold records
            scope_correct=0,
            state_correct=0,
            supersedes_correct=0,
        )
        self.assertEqual(result, expected)

    def test_empty_predicted_but_gold(self):
        gold = [
            MemoryRecord("key1", "user", "confirmed", None),
        ]
        result = compare_memories(gold, [])
        expected = MemoryComparison(
            gold_count=1,
            predicted_count=0,
            matched=0,
            unneeded=0,
            scope_correct=0,
            state_correct=0,
            supersedes_correct=0,
        )
        self.assertEqual(result, expected)

    def test_duplicate_gold_keys(self):
        gold = [
            MemoryRecord("key1", "user", "confirmed", None),
            MemoryRecord("key1", "project", "inferred", None),  # Duplicate key
        ]
        with self.assertRaises(ValueError):
            compare_memories(gold, [])

    def test_multiple_matches_same_key(self):
        # Test that we correctly handle multiple predictions for the same key
        gold = [
            MemoryRecord("key1", "user", "confirmed", None),
        ]
        predicted = [
            MemoryRecord("key1", "user", "confirmed", None),  # First match
            MemoryRecord(
                "key1", "project", "inferred", None
            ),  # Second match (should be ignored for correctness)
        ]

        result = compare_memories(gold, predicted)
        expected = MemoryComparison(
            gold_count=1,
            predicted_count=2,
            matched=1,
            unneeded=1,  # Second prediction counts as unneeded
            scope_correct=1,  # Uses first prediction for correctness
            state_correct=1,
            supersedes_correct=1,
        )
        self.assertEqual(result, expected)


class AggregateTest(unittest.TestCase):
    def test_aggregate_across_two_cases(self):
        comparison1 = MemoryComparison(
            gold_count=2,
            predicted_count=3,
            matched=2,
            unneeded=1,
            scope_correct=2,
            state_correct=1,
            supersedes_correct=0,
        )
        comparison2 = MemoryComparison(
            gold_count=1,
            predicted_count=2,
            matched=1,
            unneeded=0,
            scope_correct=1,
            state_correct=1,
            supersedes_correct=1,
        )

        result = aggregate([comparison1, comparison2])
        expected = {
            "extraction_recall": 3 / 3,  # 3 matched / 3 gold
            "unneeded_rate": 1 / 5,  # 1 unneeded / 5 predicted
            "scope_accuracy": 3 / 3,  # 3 scope_correct / 3 matched
            "state_accuracy": 2 / 3,  # 2 state_correct / 3 matched
            "supersedes_accuracy": 1 / 3,  # 1 supersedes_correct / 3 matched
            "exact_recall": 3 / 3,  # no content labels: key match is enough
            "content_accuracy": None,
            "conflict_accuracy": None,
        }
        self.assertEqual(result, expected)

    def test_zero_denominators(self):
        comparison = MemoryComparison(
            gold_count=0,
            predicted_count=0,
            matched=0,
            unneeded=0,
            scope_correct=0,
            state_correct=0,
            supersedes_correct=0,
        )

        result = aggregate([comparison])
        expected = {
            "extraction_recall": None,
            "unneeded_rate": None,
            "scope_accuracy": None,
            "state_accuracy": None,
            "supersedes_accuracy": None,
            "exact_recall": None,
            "content_accuracy": None,
            "conflict_accuracy": None,
        }
        self.assertEqual(result, expected)

    def test_empty_comparisons(self):
        with self.assertRaises(ValueError):
            aggregate([])


class ContentAndConflictTest(unittest.TestCase):
    @staticmethod
    def record(key, content=None, conflicts=None, scope="user", state="confirmed"):
        # ``conflicts=None`` is "no conflict label"; ``[]`` is "labelled: no conflict".
        return MemoryRecord(
            key,
            scope,
            state,
            None,
            content,
            None if conflicts is None else tuple(conflicts),
        )

    def test_right_key_with_wrong_content_is_not_an_exact_recall(self):
        gold = [self.record("meeting_time", "Tomorrow at 3 PM")]
        predicted = [self.record("meeting_time", "Tomorrow at 5 PM")]

        comparison = compare_memories(gold, predicted)
        metrics = aggregate([comparison])

        self.assertEqual(comparison.matched, 1)
        self.assertEqual(comparison.content_evaluated, 1)
        self.assertEqual(comparison.content_correct, 0)
        self.assertEqual(metrics["extraction_recall"], 1.0)
        self.assertEqual(metrics["exact_recall"], 0.0)
        self.assertEqual(metrics["content_accuracy"], 0.0)

    def test_content_comparison_ignores_case_width_and_whitespace(self):
        gold = [self.record("k", "Tomorrow at 3 PM")]
        predicted = [self.record("k", "  tomorrow   at\t３ pm ")]

        comparison = compare_memories(gold, predicted)

        self.assertEqual(comparison.content_correct, 1)
        self.assertEqual(aggregate([comparison])["exact_recall"], 1.0)

    def test_missing_predicted_content_is_wrong_when_gold_has_content(self):
        comparison = compare_memories(
            [self.record("k", "fact")], [self.record("k", None)]
        )
        self.assertEqual(
            (comparison.content_evaluated, comparison.content_correct), (1, 0)
        )

    def test_gold_without_content_is_not_scored_on_content(self):
        comparison = compare_memories(
            [self.record("k")], [self.record("k", "anything")]
        )
        self.assertEqual(
            (comparison.content_evaluated, comparison.content_correct), (0, 0)
        )
        self.assertEqual(aggregate([comparison])["exact_recall"], 1.0)

    def test_conflict_relations_are_compared_as_sets(self):
        gold = [self.record("a", conflicts=["b", "c"]), self.record("d")]
        predicted = [self.record("a", conflicts=["c", "b"]), self.record("d")]

        comparison = compare_memories(gold, predicted)

        self.assertEqual(
            (comparison.conflicts_evaluated, comparison.conflicts_correct), (1, 1)
        )
        self.assertEqual(aggregate([comparison])["conflict_accuracy"], 1.0)

    def test_a_missed_conflict_is_wrong(self):
        comparison = compare_memories(
            [self.record("a", conflicts=["b"])], [self.record("a")]
        )

        self.assertEqual(
            (comparison.conflicts_evaluated, comparison.conflicts_correct), (1, 0)
        )
        self.assertEqual(aggregate([comparison])["conflict_accuracy"], 0.0)

    def test_gold_labelled_as_no_conflict_is_scored_both_ways(self):
        gold = [self.record("a", conflicts=[]), self.record("d", conflicts=[])]
        predicted = [self.record("a"), self.record("d", conflicts=["a"])]

        comparison = compare_memories(gold, predicted)

        # "a": no relation emitted, right. "d": an invented relation, wrong.
        self.assertEqual(
            (comparison.conflicts_evaluated, comparison.conflicts_correct), (2, 1)
        )
        self.assertEqual(aggregate([comparison])["conflict_accuracy"], 0.5)

    def test_unlabelled_gold_is_not_scored_whatever_the_worker_emits(self):
        gold = [self.record("a"), self.record("d")]
        for label, predicted in (
            ("no relation", [self.record("a"), self.record("d")]),
            ("empty list", [self.record("a", conflicts=[]), self.record("d")]),
            ("a relation", [self.record("a", conflicts=["d"]), self.record("d")]),
        ):
            with self.subTest(prediction=label):
                comparison = compare_memories(gold, predicted)

                self.assertEqual(
                    (comparison.conflicts_evaluated, comparison.conflicts_correct),
                    (0, 0),
                )
                self.assertIsNone(aggregate([comparison])["conflict_accuracy"])

    def test_absent_and_empty_conflict_labels_are_different_records(self):
        self.assertNotEqual(self.record("a"), self.record("a", conflicts=[]))
        self.assertIsNone(self.record("a").conflicts_with)
        self.assertEqual(self.record("a", conflicts=[]).conflicts_with, ())

    def test_record_validation_for_content_and_conflicts(self):
        with self.assertRaises(TypeError):
            MemoryRecord("k", "user", "confirmed", None, "   ")
        with self.assertRaises(TypeError):
            MemoryRecord("k", "user", "confirmed", None, 5)
        with self.assertRaises(TypeError):
            MemoryRecord("k", "user", "confirmed", None, None, ["b"])
        with self.assertRaises(TypeError):
            MemoryRecord("k", "user", "confirmed", None, None, ("",))
        self.assertIsNone(MemoryRecord("k", "user", "confirmed", None).conflicts_with)


class SchemaAdherenceTest(unittest.TestCase):
    def test_schema_adherence_with_valid_and_invalid_outputs(self):
        # Define a simple schema
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "number"}},
            "required": ["name", "age"],
        }

        # Test outputs
        outputs = [
            '{"name": "Alice", "age": 30}',  # valid
            '{"name": "Bob"}',  # invalid - missing age
            '{"name": "Charlie", "age": "thirty"}',  # invalid - wrong type
        ]

        result = schema_adherence(outputs, schema)
        expected = SchemaAdherence(valid=1, total=3)
        self.assertEqual(result, expected)
        self.assertAlmostEqual(result.rate, 1 / 3)

    def test_schema_adherence_all_valid(self):
        schema = {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        }

        outputs = [
            '{"message": "hello"}',
            '{"message": "world"}',
        ]

        result = schema_adherence(outputs, schema)
        expected = SchemaAdherence(valid=2, total=2)
        self.assertEqual(result, expected)
        self.assertAlmostEqual(result.rate, 1.0)

    def test_schema_adherence_all_invalid(self):
        schema = {
            "type": "object",
            "properties": {"count": {"type": "number"}},
            "required": ["count"],
        }

        outputs = [
            '{"message": "hello"}',  # invalid - missing count
            '{"count": "not_a_number"}',  # invalid - wrong type
        ]

        result = schema_adherence(outputs, schema)
        expected = SchemaAdherence(valid=0, total=2)
        self.assertEqual(result, expected)
        self.assertAlmostEqual(result.rate, 0.0)

    def test_schema_adherence_empty_outputs(self):
        schema = {"type": "object", "properties": {"test": {"type": "string"}}}

        result = schema_adherence([], schema)
        expected = SchemaAdherence(valid=0, total=0)
        self.assertEqual(result, expected)
        self.assertIsNone(result.rate)

    def test_schema_adherence_invalid_json(self):
        schema = {"type": "object", "properties": {"test": {"type": "string"}}}

        outputs = [
            '{"valid": "json"}',
            '{"invalid": json}',  # invalid JSON
            '{"another": "valid"}',
        ]

        result = schema_adherence(outputs, schema)
        expected = SchemaAdherence(valid=2, total=3)
        self.assertEqual(result, expected)
        self.assertAlmostEqual(result.rate, 2 / 3)

    def test_schema_adherence_rejects_malformed_schema(self):
        with self.assertRaises(SchemaError):
            schema_adherence(["{}"], {"type": "not-a-real-type"})

    def test_schema_adherence_counts_wrong_type_as_invalid(self):
        schema = {"type": "object", "required": ["name"]}
        result = schema_adherence(['{"name": "a"}', "[1, 2]", "not json"], schema)
        self.assertEqual(result, SchemaAdherence(valid=1, total=3))


if __name__ == "__main__":
    unittest.main()
