"""Tests for the Memory Worker benchmark command line."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.memory_worker_runner import DEFAULT_TIMEOUT_SECONDS
from benchmarks.run_memory_worker_benchmark import main
from benchmarks.tests.fixture_workers import release_hanging_workers

FIXTURES = Path(__file__).parent / "fixtures" / "memory-worker"
VALID_CASES = str(FIXTURES / "valid-cases.json")
WORKER = "benchmarks.tests.fixture_workers:make_worker"


def run_cli(*arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(list(arguments))
    return code, stdout.getvalue(), stderr.getvalue()


class RunMemoryWorkerBenchmarkTest(unittest.TestCase):
    def test_report_is_written_to_stdout(self):
        code, stdout, stderr = run_cli("--cases", VALID_CASES, "--worker", WORKER)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        report = json.loads(stdout)
        self.assertEqual(len(report["cases"]), 2)
        self.assertEqual(report["metrics"]["schema_adherence_rate"], 1.0)
        self.assertEqual(report["timeout_seconds"], DEFAULT_TIMEOUT_SECONDS)
        self.assertNotIn("The user mentioned", stdout)

    def test_a_stalled_worker_is_cut_off_by_the_deadline(self):
        self.addCleanup(release_hanging_workers)

        code, stdout, stderr = run_cli(
            "--cases",
            VALID_CASES,
            "--worker",
            "benchmarks.tests.fixture_workers:make_hanging_worker",
            "--timeout-seconds",
            "0.2",
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        report = json.loads(stdout)
        self.assertEqual(report["timeout_seconds"], 0.2)
        self.assertEqual(
            [case["error_type"] for case in report["cases"]],
            ["deadline_exceeded", "deadline_exceeded"],
        )
        self.assertEqual(report["metrics"]["schema_adherence_rate"], 0.0)
        self.assertEqual(report["metrics"]["extraction_recall"], 0.0)

    def test_an_invalid_deadline_is_a_usage_error(self):
        for value in ("0", "-1", "nan", "inf", "abc", ""):
            with self.subTest(value=value):
                stderr = io.StringIO()
                with (
                    contextlib.redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as caught,
                ):
                    main(
                        [
                            "--cases",
                            VALID_CASES,
                            "--worker",
                            WORKER,
                            f"--timeout-seconds={value}",
                        ]
                    )
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("--timeout-seconds", stderr.getvalue())

    def test_report_is_written_to_the_output_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            code, stdout, _ = run_cli(
                "--cases", VALID_CASES, "--worker", WORKER, "--output", str(output)
            )
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(len(report["cases"]), 2)

    def test_invalid_cases_file_exits_with_one(self):
        invalid = str(FIXTURES / "invalid-cases-duplicate-id.json")
        code, stdout, stderr = run_cli("--cases", invalid, "--worker", WORKER)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("invalid-cases-duplicate-id.json", stderr)

    def test_missing_cases_file_exits_with_one(self):
        code, _, stderr = run_cli(
            "--cases", "/nonexistent/cases.json", "--worker", WORKER
        )

        self.assertEqual(code, 1)
        self.assertIn("/nonexistent/cases.json", stderr)

    def test_unusable_worker_exits_with_two_and_names_only_the_error_type(self):
        for spec, error_type in (
            ("no-separator", "ValueError"),
            ("no_such_module_for_test:make", "ModuleNotFoundError"),
            ("benchmarks.tests.fixture_workers:missing", "AttributeError"),
        ):
            with self.subTest(spec=spec):
                code, stdout, stderr = run_cli("--cases", VALID_CASES, "--worker", spec)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, f"worker is unusable ({error_type})\n")

    def test_malformed_worker_output_exits_with_two(self):
        code, stdout, stderr = run_cli(
            "--cases",
            VALID_CASES,
            "--worker",
            "benchmarks.tests.fixture_workers:make_malformed_worker",
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "worker returned an invalid result (TypeError)\n")

    def test_any_factory_exception_is_reported_by_type_only(self):
        code, stdout, stderr = run_cli(
            "--cases",
            VALID_CASES,
            "--worker",
            "benchmarks.tests.fixture_workers:make_failing_worker",
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "worker is unusable (RuntimeError)\n")

    def test_case_missing_its_id_exits_with_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(
                json.dumps({"cases": [{"input": "x", "gold": []}]}), encoding="utf-8"
            )
            code, stdout, stderr = run_cli("--cases", str(path), "--worker", WORKER)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Each case must have an 'id' field", stderr)

    def test_factories_that_do_not_return_a_worker_exit_with_two(self):
        for factory in ("make_none_worker", "make_wrong_signature_worker"):
            with self.subTest(factory=factory):
                code, stdout, stderr = run_cli(
                    "--cases",
                    VALID_CASES,
                    "--worker",
                    f"benchmarks.tests.fixture_workers:{factory}",
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, "worker is unusable (TypeError)\n")

    def test_resources_are_recorded_only_when_requested(self):
        _, without, _ = run_cli("--cases", VALID_CASES, "--worker", WORKER)
        code, with_resources, _ = run_cli(
            "--cases", VALID_CASES, "--worker", WORKER, "--collect-resources"
        )

        self.assertNotIn("resources", json.loads(without))
        self.assertEqual(code, 0)
        resources = json.loads(with_resources)["resources"]
        self.assertGreaterEqual(resources["wall_clock_ms"], 0)

    def test_unwritable_output_exits_with_two(self):
        with tempfile.TemporaryDirectory() as directory:
            code, _, stderr = run_cli(
                "--cases", VALID_CASES, "--worker", WORKER, "--output", directory
            )

        self.assertEqual(code, 2)
        self.assertEqual(stderr, "cannot write the report (IsADirectoryError)\n")


if __name__ == "__main__":
    unittest.main()
