"""Acceptance and regression tests for the benchmark task schema."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from benchmarks.validate_task import load_schema, main, validate_document

FIXTURES = Path(__file__).parent / "fixtures" / "task-schema"


class BenchmarkTaskSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_schema()

    def load_fixture(self, path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_schema_is_valid_draft_2020_12(self):
        Draft202012Validator.check_schema(self.schema)

    def test_valid_fixtures_cover_all_task_kinds(self):
        kinds = set()
        for path in sorted((FIXTURES / "valid").glob("*.json")):
            with self.subTest(path=path.name):
                task = self.load_fixture(path)
                kinds.add(task["kind"])
                self.assertEqual(validate_document(task, self.schema), [])
        self.assertEqual(kinds, {"historical", "spec", "injected_bug"})

    def test_invalid_fixtures_are_rejected(self):
        paths = sorted((FIXTURES / "invalid").glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name):
                self.assertTrue(validate_document(self.load_fixture(path), self.schema))

    def test_hidden_checks_cannot_contain_test_content_or_commands(self):
        task = self.load_fixture(FIXTURES / "valid" / "historical.json")
        for field in ("body", "command", "path"):
            with self.subTest(field=field):
                changed = deepcopy(task)
                changed["hidden_checks"][0][field] = "sensitive fixture content"
                errors = validate_document(changed, self.schema)
                self.assertIn(
                    "$.hidden_checks[0]: unexpected property is not allowed", errors
                )

    def test_errors_do_not_repeat_rejected_values(self):
        task = self.load_fixture(FIXTURES / "valid" / "spec.json")
        secret_value = "do-not-repeat-this-value"
        task["kind"] = secret_value
        errors = validate_document(task, self.schema)
        self.assertTrue(errors)
        self.assertNotIn(secret_value, "\n".join(errors))

    def test_visible_command_allows_empty_arguments_but_not_an_empty_program(self):
        task = self.load_fixture(FIXTURES / "valid" / "spec.json")
        task["visible_checks"][0]["command"] = ["python3", "-c", ""]
        self.assertEqual(validate_document(task, self.schema), [])

        task["visible_checks"][0]["command"] = [""]
        self.assertIn(
            "$.visible_checks[0].command[0]: string must not be empty",
            validate_document(task, self.schema),
        )

        task["visible_checks"][0]["command"] = []
        self.assertIn(
            "$.visible_checks[0].command: array must contain at least one item",
            validate_document(task, self.schema),
        )

    def test_cli_reports_valid_and_invalid_files(self):
        valid_path = FIXTURES / "valid" / "spec.json"
        invalid_path = FIXTURES / "invalid" / "unknown-kind.json"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main([str(valid_path), str(invalid_path)])
        self.assertEqual(result, 1)
        self.assertIn(f"{valid_path}: valid", stdout.getvalue())
        self.assertIn("$.kind: value is not an allowed enum member", stderr.getvalue())

    def test_cli_rejects_malformed_json_without_echoing_content(self):
        secret_value = "do-not-echo-this-json"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.json"
            path.write_text('{"issue_text": "' + secret_value + '"', encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = main([str(path)])
        self.assertEqual(result, 1)
        self.assertIn("JSON syntax error", stderr.getvalue())
        self.assertNotIn(secret_value, stderr.getvalue())

    def test_cli_rejects_nonstandard_json_constants_without_echoing_content(self):
        secret_value = "do-not-echo-this-json"
        for constant in ("NaN", "Infinity", "-Infinity"):
            with (
                self.subTest(constant=constant),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "nonstandard.json"
                path.write_text(
                    '{"issue_text": "' + secret_value + '", "value": ' + constant + "}",
                    encoding="utf-8",
                )
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    result = main([str(path)])
            self.assertEqual(result, 1)
            self.assertIn("non-standard numeric constant", stderr.getvalue())
            self.assertNotIn(secret_value, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
