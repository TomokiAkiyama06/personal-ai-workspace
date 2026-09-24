"""Tests for the memory worker benchmark runner."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.memory_worker_metrics import MemoryRecord, schema_adherence
from benchmarks.memory_worker_runner import (
    MemoryWorkerCase,
    _output_schema,
    load_cases,
    parse_worker_output,
    run_benchmark,
    validate_worker,
)
from benchmarks.metrics_collector import MetricsCollector


class FakeClock:
    def __init__(self, value: float = 100.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeGpuSampler:
    def __init__(self, samples):
        self.samples = list(samples)
        self.calls = 0

    def sample(self):
        self.calls += 1
        return self.samples.pop(0) if self.samples else ()


class MockWorker:
    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0

    def extract(self, input_text):
        if self.call_count < len(self.responses):
            response = self.responses[self.call_count]
            self.call_count += 1
            return response
        return ""


class MemoryWorkerRunnerTest(unittest.TestCase):
    def test_load_cases_happy_path(self):
        cases = load_cases("benchmarks/tests/fixtures/memory-worker/valid-cases.json")
        self.assertEqual(len(cases), 2)

        case1 = cases[0]
        self.assertEqual(case1.id, "test-1")
        self.assertEqual(
            case1.input_text, "The user mentioned their favorite color is blue."
        )
        self.assertEqual(len(case1.gold), 1)
        self.assertEqual(case1.gold[0].key, "favorite_color")
        self.assertEqual(case1.gold[0].scope, "user_preferences")
        self.assertEqual(case1.gold[0].state, "confirmed")
        self.assertIsNone(case1.gold[0].supersedes)

        case2 = cases[1]
        self.assertEqual(case2.id, "test-2")
        self.assertEqual(
            case2.input_text,
            "The meeting is scheduled for tomorrow at 3 PM in room 205.",
        )
        self.assertEqual(len(case2.gold), 2)
        self.assertEqual(case2.gold[0].key, "meeting_time")
        self.assertEqual(case2.gold[1].key, "meeting_location")

    def test_load_cases_empty_cases(self):
        with self.assertRaises(ValueError) as cm:
            load_cases(
                "benchmarks/tests/fixtures/memory-worker/invalid-cases-empty-cases.json"
            )
        self.assertIn("Cases list cannot be empty", str(cm.exception))

    def test_load_cases_duplicate_ids(self):
        with self.assertRaises(ValueError) as cm:
            load_cases(
                "benchmarks/tests/fixtures/memory-worker/invalid-cases-duplicate-id.json"
            )
        self.assertIn("Duplicate case ID", str(cm.exception))

    def test_load_cases_missing_fields(self):
        with self.assertRaises(ValueError) as cm:
            load_cases(
                "benchmarks/tests/fixtures/memory-worker/invalid-cases-missing-fields.json"
            )
        self.assertIn("must have a 'gold' key", str(cm.exception))

    def test_load_cases_invalid_gold_record(self):
        with self.assertRaises(ValueError) as cm:
            load_cases(
                "benchmarks/tests/fixtures/memory-worker/invalid-cases-invalid-gold-record.json"
            )
        self.assertIn("key must be a non-empty string", str(cm.exception))

    def test_parse_worker_output_valid(self):
        valid_output = """
        {
            "memories": [
                {
                    "key": "test_key",
                    "scope": "test_scope",
                    "state": "confirmed",
                    "supersedes": null
                }
            ]
        }
        """
        result = parse_worker_output(valid_output)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].key, "test_key")
        self.assertEqual(result[0].scope, "test_scope")
        self.assertEqual(result[0].state, "confirmed")
        self.assertIsNone(result[0].supersedes)

    def test_parse_worker_output_invalid_json(self):
        invalid_json = '{"memories": [}'
        result = parse_worker_output(invalid_json)
        self.assertIsNone(result)

    def test_parse_worker_output_wrong_shape(self):
        wrong_shape = '{"wrong_field": "value"}'
        result = parse_worker_output(wrong_shape)
        self.assertIsNone(result)

    def test_parse_worker_output_extra_property(self):
        extra_property = """
        {
            "memories": [
                {
                    "key": "test_key",
                    "scope": "test_scope",
                    "state": "confirmed",
                    "supersedes": null,
                    "extra_field": "should_not_be_here"
                }
            ]
        }
        """
        result = parse_worker_output(extra_property)
        self.assertIsNone(result)

    def test_run_benchmark_perfect_worker(self):
        cases = load_cases("benchmarks/tests/fixtures/memory-worker/valid-cases.json")

        # Create a worker that returns perfect output for both cases
        perfect_worker = MockWorker(
            [
                """{
                "memories": [
                    {
                        "key": "favorite_color",
                        "scope": "user_preferences",
                        "state": "confirmed",
                        "supersedes": null
                    }
                ]
            }""",
                """{
                "memories": [
                    {
                        "key": "meeting_time",
                        "scope": "schedule",
                        "state": "confirmed",
                        "supersedes": null
                    },
                    {
                        "key": "meeting_location",
                        "scope": "schedule",
                        "state": "confirmed",
                        "supersedes": null
                    }
                ]
            }""",
            ]
        )

        report = run_benchmark(perfect_worker, cases)

        # Check that we got 2 cases
        self.assertEqual(len(report.cases), 2)

        # Both cases should succeed
        self.assertTrue(report.cases[0].schema_valid)
        self.assertTrue(report.cases[1].schema_valid)

        # First case should have perfect match
        self.assertEqual(report.cases[0].comparison.gold_count, 1)
        self.assertEqual(report.cases[0].comparison.predicted_count, 1)
        self.assertEqual(report.cases[0].comparison.matched, 1)

        # Second case should have perfect match
        self.assertEqual(report.cases[1].comparison.gold_count, 2)
        self.assertEqual(report.cases[1].comparison.predicted_count, 2)
        self.assertEqual(report.cases[1].comparison.matched, 2)

    def test_run_benchmark_partial_match(self):
        cases = load_cases("benchmarks/tests/fixtures/memory-worker/valid-cases.json")

        # Create a worker that returns partial matches
        partial_worker = MockWorker(
            [
                """{
                "memories": [
                    {
                        "key": "favorite_color",
                        "scope": "user_preferences",
                        "state": "confirmed",
                        "supersedes": null
                    }
                ]
            }""",
                """{
                "memories": [
                    {
                        "key": "meeting_time",
                        "scope": "schedule",
                        "state": "inferred",
                        "supersedes": null
                    }
                ]
            }""",
            ]
        )

        report = run_benchmark(partial_worker, cases)

        # Check that we got 2 cases
        self.assertEqual(len(report.cases), 2)

        # Both cases should succeed
        self.assertTrue(report.cases[0].schema_valid)
        self.assertTrue(report.cases[1].schema_valid)

        # First case should have perfect match
        self.assertEqual(report.cases[0].comparison.gold_count, 1)
        self.assertEqual(report.cases[0].comparison.predicted_count, 1)
        self.assertEqual(report.cases[0].comparison.matched, 1)

        # Second case should have partial match (wrong state)
        self.assertEqual(report.cases[1].comparison.gold_count, 2)
        self.assertEqual(report.cases[1].comparison.predicted_count, 1)
        self.assertEqual(report.cases[1].comparison.matched, 1)  # Only matching key
        self.assertEqual(report.cases[1].comparison.state_correct, 0)  # Wrong state

    def test_run_benchmark_unparsable_output(self):
        cases = load_cases("benchmarks/tests/fixtures/memory-worker/valid-cases.json")

        # Create a worker that returns unparsable output for the second case
        unparsable_worker = MockWorker(
            [
                """{
                "memories": [
                    {
                        "key": "favorite_color",
                        "scope": "user_preferences",
                        "state": "confirmed",
                        "supersedes": null
                    }
                ]
            }""",
                "This is not valid JSON",
            ]
        )

        report = run_benchmark(unparsable_worker, cases)

        # Check that we got 2 cases
        self.assertEqual(len(report.cases), 2)

        # First case should succeed
        self.assertTrue(report.cases[0].schema_valid)

        # Second case should fail to parse
        self.assertFalse(report.cases[1].schema_valid)
        self.assertEqual(
            report.cases[1].comparison.gold_count, 2
        )  # Should still count gold
        self.assertEqual(
            report.cases[1].comparison.predicted_count, 0
        )  # No predictions
        self.assertEqual(report.cases[1].comparison.matched, 0)

    def test_load_cases_rejects_non_json_without_echoing_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text('{"cases": [SECRET-CONTENT', encoding="utf-8")
            with self.assertRaises(ValueError) as context:
                load_cases(str(path))
        self.assertNotIn("SECRET-CONTENT", str(context.exception))

    @staticmethod
    def _load_from_text(text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(text, encoding="utf-8")
            return load_cases(str(path))

    def test_malformed_cases_raise_value_error_naming_the_problem(self):
        gold = {"key": "k", "scope": "user", "state": "confirmed", "supersedes": None}
        good = {"id": "c1", "input": "text", "gold": [gold]}
        cases = (
            (
                {"cases": [{"input": "x", "gold": []}]},
                "Each case must have an 'id' field",
            ),
            ({"cases": [{"id": "c", "gold": []}]}, "'input' field"),
            ({"cases": ["not an object"]}, "case at index 0 must be an object"),
            ({"cases": [dict(good, id=5)]}, "invalid 'id'"),
            ({"cases": [dict(good, gold={})]}, "'gold' must be a list in case 'c1'"),
            ({"cases": [dict(good, gold=[{"scope": "user"}])]}, "missing 'key'"),
            ({"cases": [dict(good, gold=["x"])]}, "must be an object"),
            (
                {"cases": [dict(good, gold=[dict(gold, conflicts_with="b")])]},
                "'conflicts_with' must be a list",
            ),
            ([], "JSON document must be an object"),
            ({"cases": {}}, "'cases' must be a list"),
        )
        for document, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(ValueError) as context:
                    self._load_from_text(json.dumps(document))
                self.assertIn(message, str(context.exception))

    def test_misspelled_or_unknown_fields_are_rejected_not_ignored(self):
        gold = {"key": "k", "scope": "user", "state": "confirmed"}
        case = {"id": "c1", "input": "text", "gold": [gold]}
        documents = (
            (
                {"cases": [dict(case, gold=[dict(gold, supercedes="other")])]},
                "supercedes",
            ),
            (
                {"cases": [dict(case, gold=[dict(gold, conflict_with=["a"])])]},
                "conflict_with",
            ),
            ({"cases": [dict(case, gold=[dict(gold, contents="x")])]}, "contents"),
            ({"cases": [dict(case, goal=[])]}, "goal"),
            ({"cases": [case], "case": []}, "case"),
        )
        for document, name in documents:
            with self.subTest(field=name):
                with self.assertRaises(ValueError) as context:
                    self._load_from_text(json.dumps(document))
                self.assertIn("unknown field(s)", str(context.exception))
                self.assertIn(name, str(context.exception))

    def test_gold_record_may_omit_supersedes_and_carry_content_and_conflicts(self):
        document = {
            "cases": [
                {
                    "id": "c1",
                    "input": "text",
                    "gold": [
                        {
                            "key": "k",
                            "scope": "user",
                            "state": "inferred",
                            "content": "the fact",
                            "conflicts_with": ["other"],
                        }
                    ],
                }
            ]
        }

        (case,) = self._load_from_text(json.dumps(document))

        self.assertEqual(
            case.gold,
            (MemoryRecord("k", "user", "inferred", None, "the fact", ("other",)),),
        )

    def test_worker_output_content_and_conflicts_are_parsed(self):
        raw = json.dumps(
            {
                "memories": [
                    {
                        "key": "k",
                        "scope": "user",
                        "state": "confirmed",
                        "supersedes": None,
                        "content": "the fact",
                        "conflicts_with": ["other"],
                    }
                ]
            }
        )

        self.assertEqual(
            parse_worker_output(raw),
            [MemoryRecord("k", "user", "confirmed", None, "the fact", ("other",))],
        )

    def test_worker_output_with_bad_content_or_conflicts_is_schema_invalid(self):
        base = {"key": "k", "scope": "user", "state": "confirmed", "supersedes": None}
        for extra in (
            {"content": ""},
            {"content": 5},
            {"conflicts_with": "b"},
            {"conflicts_with": ["b", "b"]},
        ):
            with self.subTest(extra=extra):
                raw = json.dumps({"memories": [dict(base, **extra)]})
                self.assertIsNone(parse_worker_output(raw))

    def test_report_serializes_the_worker_error_type_per_case(self):
        class Raising:
            def extract(self, input_text):
                raise KeyError("SECRET-DETAIL")

        cases = [
            MemoryWorkerCase("c0", "x", (MemoryRecord("k", "user", "confirmed", None),))
        ]

        data = run_benchmark(Raising(), cases).to_dict()

        self.assertEqual(data["cases"][0]["error_type"], "KeyError")
        self.assertNotIn("SECRET-DETAIL", json.dumps(data))
        self.assertEqual(data["cases"][0]["comparison"]["matched"], 0)

    def test_report_error_type_is_none_for_a_normal_case(self):
        data = run_benchmark(MockWorker([VALID_OUTPUT]), self._cases(1)).to_dict()
        self.assertIsNone(data["cases"][0]["error_type"])

    def test_workers_without_the_required_interface_are_rejected(self):
        class NoMethod:
            pass

        class WrongSignature:
            def extract(self):
                return "{}"

        class NotCallable:
            extract = 5

        for worker in (None, NoMethod(), WrongSignature(), NotCallable()):
            with self.subTest(worker=type(worker).__name__):
                with self.assertRaises(TypeError):
                    validate_worker(worker)
                with self.assertRaises(TypeError):
                    run_benchmark(worker, self._cases(1))

    def test_extract_with_an_uninspectable_signature_is_rejected(self):
        class Worker:
            def extract(self, input_text):
                return "{}"

        with (
            patch(
                "benchmarks.memory_worker_runner.inspect.signature",
                side_effect=ValueError,
            ),
            self.assertRaises(TypeError) as context,
        ):
            validate_worker(Worker())

        self.assertIn("no inspectable signature", str(context.exception))

    def test_whitespace_only_content_is_invalid_in_the_schema_and_the_parser(self):
        raw = json.dumps(
            {
                "memories": [
                    {
                        "key": "k",
                        "scope": "user",
                        "state": "confirmed",
                        "supersedes": None,
                        "content": " ",
                    }
                ]
            }
        )

        self.assertIsNone(parse_worker_output(raw))
        self.assertEqual(schema_adherence([raw], _output_schema()).valid, 0)

    @staticmethod
    def _cases(count):
        gold = (MemoryRecord("k", "user", "confirmed", None),)
        return [MemoryWorkerCase(f"c{index}", "input", gold) for index in range(count)]

    def test_run_benchmark_exception(self):
        cases = load_cases("benchmarks/tests/fixtures/memory-worker/valid-cases.json")

        class FlakyWorker:
            def __init__(self):
                self.calls = 0

            def extract(self, input_text):
                self.calls += 1
                if self.calls == 2:
                    raise ValueError("Worker failed: SECRET-DETAIL")
                return VALID_OUTPUT

        report = run_benchmark(FlakyWorker(), cases)

        self.assertEqual(len(report.cases), 2)
        self.assertTrue(report.cases[0].schema_valid)
        self.assertIsNone(report.cases[0].error_type)
        self.assertFalse(report.cases[1].schema_valid)
        self.assertEqual(report.cases[1].error_type, "ValueError")
        self.assertEqual(report.cases[1].comparison.matched, 0)
        self.assertEqual(report.cases[1].comparison.predicted_count, 0)
        self.assertEqual(report.cases[1].comparison.gold_count, len(cases[1].gold))
        self.assertAlmostEqual(report.metrics["schema_adherence_rate"], 0.5)
        self.assertNotIn("SECRET-DETAIL", json.dumps(report.to_dict()))

    def test_latency_statistics_use_nearest_rank_percentiles(self):
        class KeyWorker:
            def extract(self, input_text):
                return VALID_OUTPUT.replace("favorite_color", "k").replace(
                    "user_preferences", "user"
                )

        # The runner reads the clock twice per case: before and after extract().
        readings = iter(
            value
            for milliseconds in range(1, 11)
            for value in (0.0, milliseconds / 1000)
        )
        report = run_benchmark(
            KeyWorker(), self._cases(10), clock=lambda: next(readings)
        )

        self.assertAlmostEqual(report.cases[2].latency_ms, 3.0)
        self.assertAlmostEqual(report.metrics["latency_ms_mean"], 5.5)
        self.assertAlmostEqual(report.metrics["latency_ms_p50"], 5.0)
        self.assertAlmostEqual(report.metrics["latency_ms_p95"], 10.0)
        self.assertEqual(report.metrics["extraction_recall"], 1.0)
        self.assertEqual(report.metrics["schema_adherence_rate"], 1.0)

    def test_latency_excludes_parsing_and_comparison(self):
        readings = iter([0.0, 0.004])
        report = run_benchmark(
            MockWorker([VALID_OUTPUT]), self._cases(1), clock=lambda: next(readings)
        )
        self.assertAlmostEqual(report.cases[0].latency_ms, 4.0)

    def test_to_dict_is_json_serializable_without_text_or_raw_output(self):
        cases = load_cases("benchmarks/tests/fixtures/memory-worker/valid-cases.json")
        report = run_benchmark(MockWorker([VALID_OUTPUT]), cases)

        data = report.to_dict()
        serialized = json.dumps(data)

        self.assertNotIn("The user mentioned", serialized)
        self.assertNotIn("favorite_color", serialized)
        self.assertEqual(len(data["cases"]), 2)
        self.assertEqual(data["metrics"], report.metrics)
        self.assertNotIn("resources", data)

    def test_metrics_collector_resources_are_reported(self):
        collector = MetricsCollector(gpu_poll_interval_s=None)

        class CountingWorker:
            def extract(self, input_text):
                collector.record_step()
                return VALID_OUTPUT

        report = run_benchmark(
            CountingWorker(),
            load_cases("benchmarks/tests/fixtures/memory-worker/valid-cases.json"),
            metrics_collector=collector,
        )

        self.assertEqual(report.resources["agent_steps"], 2)
        self.assertEqual(report.to_dict()["resources"]["agent_steps"], 2)

    def test_parsing_does_not_depend_on_the_working_directory(self):
        import os
        import tempfile

        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                records = parse_worker_output(VALID_OUTPUT)
            finally:
                os.chdir(previous)
        self.assertEqual(len(records), 1)


VALID_OUTPUT = json.dumps(
    {
        "memories": [
            {
                "key": "favorite_color",
                "scope": "user_preferences",
                "state": "confirmed",
                "supersedes": None,
            }
        ]
    }
)


if __name__ == "__main__":
    unittest.main()
