"""Acceptance and regression tests for the evaluator result schema."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from benchmarks.validate_result import load_schema, main, validate_document

FIXTURES = Path(__file__).parent / "fixtures" / "result-schema"


class EvaluatorResultSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_schema()

    def load_fixture(self, path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_schema_is_valid_draft_2020_12(self):
        Draft202012Validator.check_schema(self.schema)

    def test_valid_fixture_stores_required_evaluation_data(self):
        result = self.load_fixture(FIXTURES / "valid" / "complete.json")
        self.assertEqual(validate_document(result, self.schema), [])
        self.assertEqual(
            {check["type"] for check in result["check_results"]},
            {
                "build",
                "syntax",
                "unit",
                "integration",
                "lint",
                "type",
                "regression",
                "acceptance",
                "security",
                "forbidden_changes",
            },
        )
        self.assertEqual(
            set(result["metrics"]),
            {
                "wall_clock_ms",
                "token_count",
                "context_tokens",
                "agent_steps",
                "retries",
                "tool_calls",
                "diff_size",
                "tool_failures",
                "peak_vram_bytes",
                "peak_gpu_utilization_percent",
                "human_correction_ms",
            },
        )

    def test_metric_fields_can_be_absent_when_not_collected(self):
        result = self.load_fixture(FIXTURES / "valid" / "unavailable-metrics.json")
        self.assertEqual(validate_document(result, self.schema), [])

    def test_duplicate_check_ids_are_rejected(self):
        result = self.load_fixture(FIXTURES / "valid" / "complete.json")
        duplicate = deepcopy(result["check_results"][0])
        duplicate["type"] = "unit"
        result["check_results"].append(duplicate)
        self.assertIn(
            "$.check_results[10].id: duplicate check id",
            validate_document(result, self.schema),
        )

    def test_invalid_fixtures_are_rejected(self):
        paths = sorted((FIXTURES / "invalid").glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name):
                self.assertTrue(validate_document(self.load_fixture(path), self.schema))

    def test_minimum_error_uses_the_schema_constraint(self):
        result = self.load_fixture(FIXTURES / "invalid" / "negative-metric.json")
        self.assertIn(
            "$.metrics.peak_vram_bytes: number must be at least 0",
            validate_document(result, self.schema),
        )

    def test_errors_do_not_repeat_rejected_values(self):
        result = self.load_fixture(FIXTURES / "valid" / "complete.json")
        secret_value = "do-not-repeat-this-value"
        result["candidate"]["model"] = secret_value
        result["candidate"]["unexpected"] = True
        errors = validate_document(result, self.schema)
        self.assertTrue(errors)
        self.assertNotIn(secret_value, "\n".join(errors))

    def test_cli_reports_valid_and_invalid_files(self):
        valid_path = FIXTURES / "valid" / "complete.json"
        invalid_path = FIXTURES / "invalid" / "unknown-check-type.json"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main([str(valid_path), str(invalid_path)])
        self.assertEqual(result, 1)
        self.assertIn(f"{valid_path}: valid", stdout.getvalue())
        self.assertIn(
            "$.check_results[0].type: value is not an allowed enum member",
            stderr.getvalue(),
        )

    def test_cli_rejects_duplicate_nested_keys_without_echoing_content(self):
        secret_value = "do-not-echo-this-json"
        document = (
            '{"schema_version": "1.0", "task_id": "example", "candidate": '
            '{"model": "' + secret_value + '", "model": "replacement"}}'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate-key.json"
            path.write_text(document, encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = main([str(path)])
        self.assertEqual(result, 1)
        self.assertIn("duplicate object key", stderr.getvalue())
        self.assertNotIn(secret_value, stderr.getvalue())

    def test_cli_rejects_float_overflow_without_echoing_content(self):
        secret_value = "do-not-echo-this-json"
        document = '{"issue_text": "' + secret_value + '", "wall_clock_ms": 1e999}'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overflow.json"
            path.write_text(document, encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = main([str(path)])
        self.assertEqual(result, 1)
        self.assertIn("non-standard numeric constant", stderr.getvalue())
        self.assertNotIn(secret_value, stderr.getvalue())

    def test_unexpected_properties_are_rejected_without_mutating_input(self):
        result = self.load_fixture(FIXTURES / "valid" / "complete.json")
        changed = deepcopy(result)
        changed["metrics"]["unrequested"] = 1
        self.assertIn(
            "$.metrics: unexpected property is not allowed",
            validate_document(changed, self.schema),
        )
        self.assertEqual(validate_document(result, self.schema), [])


if __name__ == "__main__":
    unittest.main()
