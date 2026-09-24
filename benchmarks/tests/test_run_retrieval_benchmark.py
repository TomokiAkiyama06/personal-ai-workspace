"""Tests for the retrieval benchmark command line."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.run_retrieval_benchmark import main

FIXTURES = Path(__file__).parent / "fixtures" / "retrieval"
VALID_DATASET = str(FIXTURES / "valid_dataset.json")
RETRIEVER = "benchmarks.tests.fixture_retrievers:make_retriever"


def run_cli(*arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = main(list(arguments))
        except SystemExit as exit_request:
            code = exit_request.code
    return code, stdout.getvalue(), stderr.getvalue()


class RunRetrievalBenchmarkTest(unittest.TestCase):
    def test_report_is_written_to_stdout(self):
        code, stdout, stderr = run_cli(
            "--dataset", VALID_DATASET, "--retriever", RETRIEVER
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        report = json.loads(stdout)
        self.assertEqual(len(report["queries"]), 2)
        self.assertEqual(report["metrics"]["k"], 5)
        # q1 has two relevant ids and finds one; q2 finds none.
        self.assertAlmostEqual(report["metrics"]["recall_at_k"], 0.25)
        self.assertAlmostEqual(report["metrics"]["mrr"], 0.5)

    def test_cutoff_is_passed_to_the_runner(self):
        code, stdout, _ = run_cli(
            "--dataset", VALID_DATASET, "--retriever", RETRIEVER, "-k", "3"
        )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["metrics"]["k"], 3)

    def test_non_positive_cutoff_is_a_usage_error(self):
        code, stdout, stderr = run_cli(
            "--dataset", VALID_DATASET, "--retriever", RETRIEVER, "-k", "0"
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("must be at least 1", stderr)

    def test_report_is_written_to_the_output_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            code, stdout, _ = run_cli(
                "--dataset",
                VALID_DATASET,
                "--retriever",
                RETRIEVER,
                "--output",
                str(output),
            )
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(len(report["queries"]), 2)

    def test_invalid_dataset_exits_with_one(self):
        invalid = str(FIXTURES / "invisible_relevant.json")
        code, stdout, stderr = run_cli("--dataset", invalid, "--retriever", RETRIEVER)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("invisible_relevant.json", stderr)

    def test_unusable_retriever_exits_with_two_and_names_only_the_error_type(self):
        for spec, error_type in (
            ("no-separator", "ValueError"),
            ("no_such_module_for_test:make", "ModuleNotFoundError"),
            ("benchmarks.tests.fixture_retrievers:missing", "AttributeError"),
        ):
            with self.subTest(spec=spec):
                code, stdout, stderr = run_cli(
                    "--dataset", VALID_DATASET, "--retriever", spec
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, f"retriever is unusable ({error_type})\n")

    def test_retrievers_that_cannot_work_exit_with_two_and_name_only_the_type(self):
        for factory, error_type in (
            ("make_object_without_retrieve", "TypeError"),
            ("make_wrong_signature_retriever", "TypeError"),
            ("make_failing_retriever", "RuntimeError"),
        ):
            with self.subTest(factory=factory):
                code, stdout, stderr = run_cli(
                    "--dataset",
                    VALID_DATASET,
                    "--retriever",
                    f"benchmarks.tests.fixture_retrievers:{factory}",
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, f"retriever is unusable ({error_type})\n")

    def test_malformed_retriever_output_exits_with_two(self):
        code, stdout, stderr = run_cli(
            "--dataset",
            VALID_DATASET,
            "--retriever",
            "benchmarks.tests.fixture_retrievers:make_malformed_retriever",
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "retriever returned an invalid result (TypeError)\n")

    def test_unwritable_output_exits_with_two(self):
        with tempfile.TemporaryDirectory() as directory:
            code, _, stderr = run_cli(
                "--dataset",
                VALID_DATASET,
                "--retriever",
                RETRIEVER,
                "--output",
                directory,
            )

        self.assertEqual(code, 2)
        self.assertEqual(stderr, "cannot write the report (IsADirectoryError)\n")


if __name__ == "__main__":
    unittest.main()
