"""Behavioral tests for isolated benchmark candidate worktrees."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from benchmarks.worktree_runner import WorktreeRun, WorktreeRunner, WorktreeRunnerError


class WorktreeRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.git("init")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test User")
        (self.repository / "state.txt").write_text("starting state\n", encoding="utf-8")
        self.git("add", "state.txt")
        self.git("commit", "-m", "starting state")
        self.commit = self.git("rev-parse", "HEAD").stdout.strip()
        self.runner = WorktreeRunner(self.repository, self.root / "benchmark-runs")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def git(self, *arguments):
        return subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(self.repository),
                *arguments,
            ],
            check=True,
            text=True,
            capture_output=True,
            env=self.git_environment(),
        )

    @staticmethod
    def git_environment():
        return {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }

    def test_each_candidate_receives_the_same_detached_starting_state(self):
        first = self.runner.create("candidate-a", self.commit)
        (first.path / "state.txt").write_text("changed by first\n", encoding="utf-8")
        second = self.runner.create("candidate-b", self.commit)

        self.assertEqual(
            (second.path / "state.txt").read_text(encoding="utf-8"),
            "starting state\n",
        )
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(second.path), "branch", "--show-current"],
                text=True,
                capture_output=True,
                check=True,
                env=self.git_environment(),
            ).stdout,
            "",
        )
        self.runner.cleanup(first)
        self.runner.cleanup(second)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_timeout_removes_worktree_and_keeps_safe_execution_log(self):
        run = self.runner.create("slow-candidate", self.commit)
        secret = "do-not-persist-this-output"
        result = self.runner.execute(
            run,
            [sys.executable, "-c", f"import time; print('{secret}'); time.sleep(5)"],
            timeout_seconds=0.05,
        )

        self.assertEqual(result.status, "timed_out")
        self.assertFalse(run.path.exists())
        events = [
            json.loads(line)["event"] for line in run.log_path.read_text().splitlines()
        ]
        self.assertIn("timed_out", events)
        self.assertIn("cleanup_finished", events)
        self.assertNotIn(
            secret,
            run.log_path.read_text(encoding="utf-8"),
        )
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_cancel_terminates_active_process_and_removes_worktree(self):
        run = self.runner.create("cancelled-candidate", self.commit)
        result_box = {}

        def execute():
            result_box["result"] = self.runner.execute(
                run, [sys.executable, "-c", "import time; time.sleep(10)"], 10
            )

        worker = threading.Thread(target=execute)
        worker.start()
        time.sleep(0.1)
        self.runner.cancel(run)
        worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result_box["result"].status, "cancelled")
        self.assertFalse(run.path.exists())
        self.assertIn("cancellation_requested", run.log_path.read_text())

    def test_rejects_invalid_candidate_id_and_option_like_commit(self):
        with self.assertRaises(ValueError):
            self.runner.create("candidate/unsafe", self.commit)
        with self.assertRaises(ValueError):
            self.runner.create("candidate-a", "--show-toplevel")

    def test_rejects_nonexistent_commit_without_creating_a_run(self):
        with self.assertRaises(WorktreeRunnerError):
            self.runner.create("candidate-a", "0" * 40)
        self.assertEqual(list((self.root / "benchmark-runs").iterdir()), [])

    def test_cleanup_refuses_a_run_not_owned_by_this_runner(self):
        foreign = WorktreeRun(
            "foreign-run",
            "candidate-a",
            self.commit,
            self.repository,
            self.root / "foreign-execution.jsonl",
        )
        with self.assertRaises(WorktreeRunnerError):
            self.runner.cleanup(foreign)
        self.assertTrue(self.repository.exists())

    def test_rejects_a_non_git_repository(self):
        non_repository = self.root / "not-a-repository"
        non_repository.mkdir()
        with self.assertRaises(WorktreeRunnerError):
            WorktreeRunner(non_repository, self.root / "other-runs")

    def test_log_correlates_each_event_with_candidate_and_commit(self):
        run = self.runner.create("candidate-a", self.commit)
        event = json.loads(run.log_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(event["candidate_id"], "candidate-a")
        self.assertEqual(event["commit"], self.commit)
        self.runner.cleanup(run)

    def test_cleanup_releases_terminal_run_state(self):
        run = self.runner.create("candidate-a", self.commit)
        self.runner.cancel(run)
        self.runner.cleanup(run, reason="cancelled")
        self.assertNotIn(run.run_id, self.runner._runs)
        self.assertNotIn(run.run_id, self.runner._cancelled)

    def test_candidate_git_environment_cannot_select_the_main_index(self):
        run = self.runner.create("candidate-a", self.commit)
        inherited_index = self.repository / ".git" / "index"
        previous_index = os.environ.get("GIT_INDEX_FILE")
        os.environ["GIT_INDEX_FILE"] = str(inherited_index)
        try:
            result = self.runner.execute(
                run, ["git", "add", "state.txt"], timeout_seconds=1
            )
        finally:
            if previous_index is None:
                os.environ.pop("GIT_INDEX_FILE", None)
            else:
                os.environ["GIT_INDEX_FILE"] = previous_index
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_timeout_with_escaped_child_does_not_block_cleanup(self):
        run = self.runner.create("escaped-child", self.commit)
        command = (
            "import os, time; "
            "pid = os.fork(); "
            "os.setsid() if pid == 0 else None; "
            "time.sleep(5)"
        )
        started = time.monotonic()
        result = self.runner.execute(
            run, [sys.executable, "-c", command], timeout_seconds=0.05
        )
        self.assertEqual(result.status, "timed_out")
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(run.path.exists())


if __name__ == "__main__":
    unittest.main()
