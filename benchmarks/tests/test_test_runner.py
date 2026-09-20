"""Acceptance tests for visible and hidden benchmark check execution."""

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from benchmarks.test_runner import CheckDefinition, HiddenCheckRegistry, TestRunner


class TestRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.worktree = self.root / "candidate-worktree"
        self.worktree.mkdir()
        self.log_path = self.root / "evaluator-private" / "check-results.jsonl"
        self.runner = TestRunner(self.worktree, self.log_path)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_visible_and_hidden_checks_are_separate_and_hidden_details_are_not_logged(
        self,
    ):
        hidden_secret = "hidden-test-content-must-not-be-disclosed"
        visible = CheckDefinition(
            "visible-pass", "unit", (sys.executable, "-c", "print('visible')")
        )
        registry = HiddenCheckRegistry(
            {
                "private:case-1": CheckDefinition(
                    "hidden-pass",
                    "acceptance",
                    (sys.executable, "-c", f"print('{hidden_secret}')"),
                )
            }
        )

        visible_result = self.runner.run_visible((visible,), 1)
        hidden_result = self.runner.run_hidden(("private:case-1",), registry, 1)

        self.assertEqual(visible_result[0].visibility, "visible")
        self.assertEqual(hidden_result[0].visibility, "hidden")
        self.assertEqual(hidden_result[0].stdout.decode().strip(), hidden_secret)
        self.assertFalse(any(self.worktree.rglob("*")))
        log_content = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn(hidden_secret, log_content)
        self.assertNotIn("private:case-1", log_content)
        self.assertNotIn("-c", log_content)
        self.assertEqual(stat.S_IMODE(self.log_path.stat().st_mode), 0o600)

    def test_timeout_exit_code_stdout_and_stderr_are_captured_with_safe_log_records(
        self,
    ):
        secret = "do-not-persist-output"
        checks = (
            CheckDefinition(
                "fails",
                "unit",
                (
                    sys.executable,
                    "-c",
                    f"import sys; print('{secret}'); print('{secret}', file=sys.stderr); raise SystemExit(7)",
                ),
            ),
            CheckDefinition(
                "times-out",
                "integration",
                (sys.executable, "-c", "import time; time.sleep(5)"),
            ),
        )

        failed, timed_out = self.runner.run_visible(checks, 0.05)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.exit_code, 7)
        self.assertIn(secret.encode(), failed.stdout)
        self.assertIn(secret.encode(), failed.stderr)
        self.assertEqual(timed_out.status, "timed_out")
        self.assertTrue(timed_out.timed_out)
        records = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        self.assertEqual(records[0]["exit_code"], 7)
        self.assertTrue(records[1]["timed_out"])
        self.assertIn("sha256", records[0]["stdout"])
        self.assertGreater(records[0]["stderr"]["captured_bytes"], 0)
        self.assertNotIn(secret, self.log_path.read_text(encoding="utf-8"))

    def test_known_good_passes_and_buggy_revision_fails(self):
        program = self.worktree / "program.py"
        check = CheckDefinition(
            "acceptance", "acceptance", (sys.executable, "program.py")
        )
        program.write_text("raise SystemExit(1)\n", encoding="utf-8")
        buggy = self.runner.run_visible((check,), 1)[0]
        program.write_text("raise SystemExit(0)\n", encoding="utf-8")
        known_good = self.runner.run_visible((check,), 1)[0]

        self.assertEqual(buggy.status, "failed")
        self.assertEqual(known_good.status, "passed")

    def test_rejects_log_inside_candidate_worktree(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            TestRunner(self.worktree, self.worktree / "execution.jsonl")

    def test_unavailable_hidden_reference_does_not_echo_the_reference(self):
        registry = HiddenCheckRegistry({})
        secret_reference = "private:do-not-echo"
        with self.assertRaisesRegex(KeyError, "unavailable") as error:
            self.runner.run_hidden((secret_reference,), registry, 1)
        self.assertNotIn(secret_reference, str(error.exception))


if __name__ == "__main__":
    unittest.main()
