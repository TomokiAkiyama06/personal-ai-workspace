"""Behavioral tests for isolated benchmark candidate worktrees."""

import contextlib
import ctypes
import errno
import inspect
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

from benchmarks import worktree_runner
from benchmarks.worktree_runner import WorktreeRun, WorktreeRunner, WorktreeRunnerError

# Deadline for waiting on something that must happen; generous on purpose so a
# loaded machine slows the test down instead of failing it.
PATIENCE = 20

# Candidate that starts a child in its own session (so it is not in the process
# group), publishes the child's pid, then blocks.
ESCAPED_CHILD = """
import os, sys, time
pid_file = sys.argv[1]
if os.fork() == 0:
    os.setsid()
    with open(pid_file + '.tmp', 'w') as handle:
        handle.write(str(os.getpid()))
    os.replace(pid_file + '.tmp', pid_file)
    time.sleep(60)
    os._exit(0)
time.sleep(60)
"""

# Candidate that leaves an orphaned grandchild in its own process group.  The
# grandchild ignores SIGTERM and, having been re-parented, is not a descendant
# of the candidate, so only a group-wide SIGKILL can stop it.  With ``exit`` the
# candidate returns once the grandchild runs; otherwise it blocks.
ORPHANED_TERM_IGNORING_CHILD = """
import os, signal, sys, time
pid_file, exit_early = sys.argv[1], sys.argv[2] == 'exit'
child = os.fork()
if child == 0:
    if os.fork() == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with open(pid_file + '.tmp', 'w') as handle:
            handle.write(str(os.getpid()))
        os.replace(pid_file + '.tmp', pid_file)
        time.sleep(60)
    os._exit(0)
os.waitpid(child, 0)
while not os.path.exists(pid_file):
    time.sleep(0.01)
if not exit_early:
    time.sleep(60)
"""

# Candidate that ignores SIGTERM itself, so only SIGKILL after the grace period
# ends it.  It signals readiness once the handler is installed.
TERM_IGNORING_LEADER = """
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1], 'w').close()
time.sleep(60)
"""

# Candidate that leaves a descendant whose SIGTERM handler needs ~0.3 s to finish
# its cleanup.  ``orphan``: an orphaned member of the candidate's process group.
# ``session``: a child in its own session.  The candidate itself dies on SIGTERM.
SLOW_TERM_CLEANUP = """
import os, signal, sys, time
mode, pid_file, done = sys.argv[1:4]

def install_handler_and_wait():
    def cleanup(signum, frame):
        time.sleep(0.3)
        with open(done, 'w') as handle:
            handle.write('cleaned')
        os._exit(0)
    signal.signal(signal.SIGTERM, cleanup)
    with open(pid_file + '.tmp', 'w') as handle:
        handle.write(str(os.getpid()))
    os.replace(pid_file + '.tmp', pid_file)
    time.sleep(60)

child = os.fork()
if child == 0:
    if mode == 'session':
        os.setsid()
        install_handler_and_wait()
    elif os.fork() == 0:
        install_handler_and_wait()
    os._exit(0)
if mode == 'orphan':
    os.waitpid(child, 0)
while not os.path.exists(pid_file):
    time.sleep(0.01)
time.sleep(60)
"""


def probe_proc_file(pid, name, address=None, expected=b""):
    """Can this process read ``/proc/<pid>/<name>``?  Any ``OSError`` means no.

    ``mem`` is read at ``address`` and must return ``expected``: reading at offset 0
    raises ``EIO`` even where the file can be opened, so only a read of memory that
    exists proves the file is readable.  The source of this function is also sent
    to the candidate, so the candidate and the tests probe identically.
    """
    try:
        with open(f"/proc/{pid}/{name}", "rb", buffering=0) as handle:
            if address is None:
                handle.read(16)
                return {"readable": True, "errno": None}
            data = os.pread(handle.fileno(), len(expected), address)
            return {"readable": data == expected, "errno": None}
    except OSError as error:
        return {"readable": False, "errno": error.errno}


# Candidate that reports whether it can read the evaluator's /proc entries.
PROBE_EVALUATOR_PROC = (
    "import ctypes, json, os, sys\n"
    + inspect.getsource(probe_proc_file)
    + """
pid, report, address, expected = sys.argv[1:5]
result = {
    'environ': probe_proc_file(pid, 'environ'),
    'mem': probe_proc_file(pid, 'mem', int(address), bytes.fromhex(expected)),
    'own_dumpable': ctypes.CDLL(None).prctl(3, 0, 0, 0, 0),
}
json.dump(result, open(report, 'w'))
"""
)


def dumpable():
    return ctypes.CDLL(None).prctl(3, 0, 0, 0, 0)


def set_dumpable(value):
    zero = ctypes.c_ulong(0)
    ctypes.CDLL(None).prctl(4, ctypes.c_ulong(value), zero, zero, zero)


def wait_until(condition, seconds=PATIENCE):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def is_running(pid):
    """True unless the process is gone or only an unreaped zombie."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return handle.read().rsplit(b")", 1)[1].split()[0] not in (b"Z", b"X")
    except OSError:
        return False


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
        # Hardening the evaluator is process-wide; leave the test process as found.
        self.addCleanup(set_dumpable, dumpable())
        self.runner = WorktreeRunner(self.repository, self.root / "benchmark-runs")
        self.pid_files = []
        # Cleanups run last-in first-out: stop stray processes, then delete files.
        self.addCleanup(self.temporary_directory.cleanup)
        self.addCleanup(self.kill_recorded_processes)

    def kill_recorded_processes(self):
        for pid_file in self.pid_files:
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def pid_file(self, name="child.pid"):
        path = self.root / name
        self.pid_files.append(path)
        return path

    def read_pid(self, path):
        self.assertTrue(wait_until(path.exists), "candidate never published its pid")
        return int(path.read_text())

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

    def events(self, run):
        return [
            json.loads(line)
            for line in run.log_path.read_text(encoding="utf-8").splitlines()
        ]

    def execute_in_thread(self, run, command, timeout_seconds=PATIENCE * 3):
        box = {}

        def target():
            box["result"] = self.runner.execute(run, command, timeout_seconds)

        worker = threading.Thread(target=target)
        worker.start()
        self.addCleanup(worker.join, PATIENCE)
        return worker, box

    def assert_only_the_main_worktree_remains(self, run=None):
        listing = self.git("worktree", "list", "--porcelain").stdout
        self.assertEqual(listing.count("worktree "), 1, listing)
        self.assertNotIn("prunable", listing)
        records = self.repository / ".git" / "worktrees"
        self.assertEqual(list(records.iterdir()) if records.exists() else [], [])
        if run is not None:
            self.assertFalse(run.path.parent.exists())

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

    def test_candidate_commits_do_not_move_the_main_branch(self):
        run = self.runner.create("candidate-a", self.commit)
        result = self.runner.execute(
            run,
            [
                "git",
                "-c",
                "user.name=Candidate",
                "-c",
                "user.email=candidate@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                "candidate work",
            ],
            PATIENCE,
        )
        self.assertEqual((result.status, result.exit_code), ("completed", 0))
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.commit)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_a_normal_run_logs_its_whole_lifecycle_with_exact_byte_counts(self):
        run = self.runner.create("candidate-a", self.commit)
        result = self.runner.execute(
            run,
            [
                sys.executable,
                "-c",
                "import sys; print('hello'); print('oops', file=sys.stderr)",
            ],
            PATIENCE,
        )
        self.assertEqual(
            (
                result.status,
                result.exit_code,
                result.stdout_bytes,
                result.stderr_bytes,
            ),
            ("completed", 0, len("hello\n"), len("oops\n")),
        )
        events = self.events(run)
        self.assertEqual(
            [event["event"] for event in events],
            [
                "created",
                "ready",
                "execution_started",
                "completed",
                "cleanup_started",
                "cleanup_finished",
            ],
        )
        self.assertEqual({event["candidate_id"] for event in events}, {"candidate-a"})
        self.assertEqual({event["commit"] for event in events}, {self.commit})
        self.assertEqual(events[3]["exit_code"], 0)
        self.assert_only_the_main_worktree_remains(run)

    def test_output_boundaries_are_counted_exactly_while_streaming(self):
        run = self.runner.create("candidate-a", self.commit)
        script = (
            "import sys; "
            "sys.stdout.buffer.write(b'o' * 3_000_000); "
            "sys.stderr.buffer.write(b'e' * 65_536)"
        )
        result = self.runner.execute(run, [sys.executable, "-c", script], PATIENCE)
        self.assertEqual(result.status, "completed")
        self.assertEqual(
            (result.stdout_bytes, result.stderr_bytes), (3_000_000, 65_536)
        )

    def test_output_is_not_buffered_in_memory_while_the_candidate_runs(self):
        run = self.runner.create("candidate-a", self.commit)
        size = 64 * 1024 * 1024
        tracemalloc.start()
        try:
            result = self.runner.execute(
                run,
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'x' * int(sys.argv[1]))",
                    str(size),
                ],
                PATIENCE,
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result.stdout_bytes, size)
        # Buffering the whole stream would need at least ``size`` bytes.
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_endless_output_is_drained_until_the_timeout(self):
        run = self.runner.create("noisy", self.commit)
        script = (
            "import sys\n"
            "while True:\n"
            "    sys.stdout.buffer.write(b'x' * 65536)\n"
            "    sys.stdout.buffer.flush()\n"
        )
        result = self.runner.execute(run, [sys.executable, "-c", script], 1.0)
        self.assertEqual(result.status, "timed_out")
        self.assertGreater(result.stdout_bytes, 0)
        self.assertFalse(run.path.exists())

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
        events = [event["event"] for event in self.events(run)]
        self.assertIn("timed_out", events)
        self.assertIn("cleanup_finished", events)
        self.assertNotIn(
            secret,
            run.log_path.read_text(encoding="utf-8"),
        )
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_cancel_terminates_active_process_and_removes_worktree(self):
        run = self.runner.create("cancelled-candidate", self.commit)
        worker, box = self.execute_in_thread(
            run, [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        time.sleep(0.1)
        self.runner.cancel(run)
        worker.join(timeout=PATIENCE)

        self.assertFalse(worker.is_alive())
        self.assertEqual(box["result"].status, "cancelled")
        self.assertFalse(run.path.exists())
        self.assertIn("cancellation_requested", run.log_path.read_text())

    def test_timeout_kills_a_child_in_its_own_session_and_does_not_block(self):
        run = self.runner.create("escaped-child", self.commit)
        pid_file = self.pid_file()
        started = time.monotonic()
        result = self.runner.execute(
            run, [sys.executable, "-c", ESCAPED_CHILD, str(pid_file)], 3.0
        )
        self.assertEqual(result.status, "timed_out")
        # The candidate and its child sleep for 60 seconds.
        self.assertLess(time.monotonic() - started, 30)
        self.assertFalse(run.path.exists())
        child = self.read_pid(pid_file)
        self.assertTrue(wait_until(lambda: not is_running(child)))

    def test_cancel_kills_a_group_member_that_ignores_sigterm(self):
        self.runner.term_grace_seconds = 0.3
        run = self.runner.create("stubborn-child", self.commit)
        pid_file = self.pid_file()
        worker, box = self.execute_in_thread(
            run,
            [sys.executable, "-c", ORPHANED_TERM_IGNORING_CHILD, str(pid_file), "wait"],
        )
        child = self.read_pid(pid_file)
        self.runner.cancel(run)
        worker.join(timeout=PATIENCE)

        self.assertFalse(worker.is_alive())
        self.assertEqual(box["result"].status, "cancelled")
        self.assertTrue(wait_until(lambda: not is_running(child)))
        self.assertFalse(run.path.exists())

    def test_cancel_kills_a_leader_that_ignores_sigterm_after_the_grace_period(self):
        self.runner.term_grace_seconds = 0.3
        run = self.runner.create("stubborn-leader", self.commit)
        ready = self.root / "handler-installed"
        worker, box = self.execute_in_thread(
            run, [sys.executable, "-c", TERM_IGNORING_LEADER, str(ready)]
        )
        self.assertTrue(wait_until(ready.exists), "the handler was never installed")
        self.runner.cancel(run)
        worker.join(timeout=PATIENCE)

        self.assertFalse(worker.is_alive())
        self.assertEqual(box["result"].status, "cancelled")
        self.assertEqual(box["result"].exit_code, -signal.SIGKILL)

    def test_a_process_left_behind_by_a_finished_candidate_is_killed(self):
        self.runner.drain_seconds = 0.3
        run = self.runner.create("background-child", self.commit)
        pid_file = self.pid_file()
        result = self.runner.execute(
            run,
            [sys.executable, "-c", ORPHANED_TERM_IGNORING_CHILD, str(pid_file), "exit"],
            PATIENCE,
        )
        self.assertEqual((result.status, result.exit_code), ("completed", 0))
        self.assertTrue(wait_until(lambda: not is_running(self.read_pid(pid_file))))

    def test_rejects_invalid_candidate_ids(self):
        for candidate_id in (
            "candidate/unsafe",
            "",
            "a b",
            "../escape",
            ".hidden",
            "-leading-dash",
            "new\nline",
            "x" * 129,
            None,
        ):
            with self.subTest(candidate_id=candidate_id), self.assertRaises(ValueError):
                self.runner.create(candidate_id, self.commit)
        self.assertEqual(list((self.root / "benchmark-runs").iterdir()), [])
        self.assertEqual(self.runner._runs, {})

    def test_rejects_option_like_and_malformed_revisions_before_running_git(self):
        for revision in (
            "--show-toplevel",
            "--upload-pack=touch injected",
            "-h",
            "",
            " HEAD",
            "HEAD\n--all",
            "HEAD;true",
            "HEAD:state.txt",
            None,
        ):
            with self.subTest(revision=revision):
                with (
                    mock.patch.object(WorktreeRunner, "_git") as git,
                    self.assertRaises(ValueError),
                ):
                    self.runner.create("candidate-a", revision)
                git.assert_not_called()

    def test_revision_is_passed_to_git_after_end_of_options(self):
        with mock.patch.object(
            worktree_runner.subprocess, "run", wraps=subprocess.run
        ) as run_git:
            run = self.runner.create("candidate-a", "HEAD")
        self.assertEqual(run.commit, self.commit)
        verifications = [
            call.args[0]
            for call in run_git.call_args_list
            if "--verify" in call.args[0]
        ]
        self.assertEqual(len(verifications), 1)
        self.assertEqual(
            verifications[0][-3:], ["--verify", "--end-of-options", "HEAD^{commit}"]
        )
        self.runner.cleanup(run)

    def test_rejects_nonexistent_commit_without_creating_a_run(self):
        with self.assertRaises(WorktreeRunnerError):
            self.runner.create("candidate-a", "0" * 40)
        self.assertEqual(list((self.root / "benchmark-runs").iterdir()), [])
        self.assertEqual(list((self.root / "benchmark-runs-logs").iterdir()), [])
        self.assertEqual(self.runner._runs, {})

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
        with self.assertRaises(WorktreeRunnerError):
            self.runner.cancel(foreign)
        with self.assertRaises(WorktreeRunnerError):
            self.runner.execute(foreign, [sys.executable, "-c", "pass"], PATIENCE)
        self.assertTrue(self.repository.exists())
        self.assertEqual(self.runner._cancelled, set())

    def test_a_run_naming_someone_elses_paths_is_not_owned(self):
        run = self.runner.create("candidate-a", self.commit)
        victim = self.root / "victim.txt"
        victim.write_text("precious\n", encoding="utf-8")
        forged = WorktreeRun(run.run_id, run.candidate_id, run.commit, run.path, victim)
        with self.assertRaises(WorktreeRunnerError):
            self.runner.cleanup(forged)
        self.assertEqual(victim.read_text(encoding="utf-8"), "precious\n")
        self.runner.cleanup(run)

    def test_rejects_a_non_git_repository(self):
        non_repository = self.root / "not-a-repository"
        non_repository.mkdir()
        with self.assertRaises(WorktreeRunnerError):
            WorktreeRunner(non_repository, self.root / "other-runs")

    def test_execute_rejects_bad_arguments_and_keeps_the_worktree(self):
        run = self.runner.create("candidate-a", self.commit)
        for command, timeout in (
            ([], 1),
            ([""], 1),
            ([sys.executable, 1], 1),
            ([sys.executable], 0),
            ([sys.executable], -1),
            ([sys.executable], float("nan")),
            ([sys.executable], float("inf")),
            ([sys.executable], True),
            ([sys.executable], "1"),
        ):
            with (
                self.subTest(command=command, timeout=timeout),
                self.assertRaises(ValueError),
            ):
                self.runner.execute(run, command, timeout)
        self.assertTrue(run.path.is_dir())
        self.runner.cleanup(run)

    def test_a_command_that_cannot_start_is_logged_and_cleaned_up(self):
        run = self.runner.create("candidate-a", self.commit)
        with self.assertRaises(FileNotFoundError):
            self.runner.execute(run, ["/nonexistent/candidate-binary"], PATIENCE)
        self.assertIn("launch_failed", [event["event"] for event in self.events(run)])
        self.assert_only_the_main_worktree_remains(run)

    def test_log_correlates_each_event_with_candidate_and_commit(self):
        run = self.runner.create("candidate-a", self.commit)
        event = self.events(run)[0]
        self.assertEqual(event["candidate_id"], "candidate-a")
        self.assertEqual(event["commit"], self.commit)
        self.runner.cleanup(run)

    def test_cleanup_releases_terminal_run_state(self):
        run = self.runner.create("candidate-a", self.commit)
        self.runner.cancel(run)
        self.runner.cleanup(run, reason="cancelled")
        self.assertNotIn(run.run_id, self.runner._runs)
        self.assertNotIn(run.run_id, self.runner._cancelled)
        with self.assertRaises(WorktreeRunnerError):
            self.runner.cancel(run)
        self.assertEqual(self.runner._cancelled, set())

    def test_a_long_lived_runner_retains_no_state_for_finished_runs(self):
        for index in range(3):
            run = self.runner.create(f"candidate-{index}", self.commit)
            self.runner.execute(run, [sys.executable, "-c", "pass"], PATIENCE)
        self.assertEqual((self.runner._runs, self.runner._cancelled), ({}, set()))

    def test_candidate_git_environment_cannot_select_the_main_index(self):
        run = self.runner.create("candidate-a", self.commit)
        inherited_index = self.repository / ".git" / "index"
        script = (
            "import subprocess; "
            "open('state.txt', 'w').write('staged by candidate\\n'); "
            "subprocess.run(['git', 'add', 'state.txt'], check=True)"
        )
        with mock.patch.dict(os.environ, {"GIT_INDEX_FILE": str(inherited_index)}):
            result = self.runner.execute(
                run, [sys.executable, "-c", script], timeout_seconds=PATIENCE
            )
        self.assertEqual((result.status, result.exit_code), ("completed", 0))
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_candidate_environment_is_an_allowlist_without_credentials(self):
        run = self.runner.create("candidate-a", self.commit)
        report = self.root / "environment.json"
        secrets = {
            "SECRET_TOKEN": "hunter2",
            "GITHUB_TOKEN": "ghp_not_for_candidates",
            "ANTHROPIC_API_KEY": "sk-not-for-candidates",
            "GIT_DIR": str(self.repository / ".git"),
            "GIT_WORK_TREE": str(self.repository),
        }
        with mock.patch.dict(os.environ, secrets):
            result = self.runner.execute(
                run,
                [
                    sys.executable,
                    "-c",
                    "import json, os, sys; json.dump(dict(os.environ), open(sys.argv[1], 'w'))",
                    str(report),
                ],
                PATIENCE,
            )
        self.assertEqual(result.status, "completed")
        candidate = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(set(candidate) & set(secrets), set())
        self.assertEqual(
            {key for key in candidate if key.startswith("GIT_")}, set(), candidate
        )
        # Python may add LC_CTYPE itself when coercing a C locale.
        self.assertLessEqual(
            set(candidate),
            set(worktree_runner._CANDIDATE_ENVIRONMENT_ALLOWLIST)
            | {"HOME", "LC_CTYPE"},
        )
        self.assertEqual(candidate["PATH"], os.environ["PATH"])
        self.assertEqual(
            candidate["HOME"], str(self.runner.runs_directory / run.run_id / "home")
        )

    def test_state_directories_are_private_from_the_moment_they_exist(self):
        previous = os.umask(0)
        try:
            runner = WorktreeRunner(self.repository, self.root / "umask-runs")
            run = runner.create("candidate-a", self.commit)
        finally:
            os.umask(previous)
        modes = {
            path.name: stat.S_IMODE(path.stat().st_mode)
            for path in (
                runner.runs_directory,
                runner.logs_directory,
                run.path.parent,
                run.log_path,
            )
        }
        self.assertEqual(
            modes,
            {
                "umask-runs": 0o700,
                "umask-runs-logs": 0o700,
                run.run_id: 0o700,
                run.log_path.name: 0o600,
            },
        )
        runner.cleanup(run)

    def test_shared_or_nested_state_directories_are_refused(self):
        shared = self.root / "shared"
        shared.mkdir()
        shared.chmod(0o777)
        with self.assertRaises(WorktreeRunnerError):
            WorktreeRunner(self.repository, shared)
        with self.assertRaises(WorktreeRunnerError):
            WorktreeRunner(self.repository, self.root / "runs", shared)
        with self.assertRaises(ValueError):
            WorktreeRunner(
                self.repository,
                self.root / "runs",
                self.root / "runs" / "logs",
            )

    def test_lifecycle_log_is_outside_the_candidates_directory_chain(self):
        run = self.runner.create("candidate-a", self.commit)
        self.assertNotIn(run.log_path.parent, run.path.parents)
        self.assertFalse(run.log_path.is_relative_to(self.runner.runs_directory))
        self.assertFalse((run.path.parent / "execution.jsonl").exists())
        self.runner.cleanup(run)
        self.assertTrue(run.log_path.is_file())

    def test_a_symlinked_log_is_not_followed_or_chmodded(self):
        run = self.runner.create("candidate-a", self.commit)
        victim = self.root / "victim.txt"
        victim.write_text("precious\n", encoding="utf-8")
        victim.chmod(0o644)
        run.log_path.unlink()
        run.log_path.symlink_to(victim)

        with self.assertRaises(WorktreeRunnerError):
            self.runner.execute(run, [sys.executable, "-c", "pass"], PATIENCE)

        self.assertEqual(victim.read_text(encoding="utf-8"), "precious\n")
        self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o644)
        # A tampered log never prevents removal of the checkout.
        self.assert_only_the_main_worktree_remains(run)
        self.assertEqual(self.runner._runs, {})

    def test_forged_or_truncated_logs_are_detected(self):
        def forge(path):
            with path.open("a") as log:
                log.write('{"event":"forged"}\n')

        def truncate(path):
            with path.open("w"):
                pass

        def replace_with_a_copy(path):
            copy = path.with_suffix(".copy")
            copy.write_bytes(path.read_bytes())
            os.replace(copy, path)

        def add_a_hard_link(path):
            os.link(path, path.with_suffix(".alias"))

        for tamper in (forge, truncate, replace_with_a_copy, add_a_hard_link):
            with self.subTest(tamper=tamper.__name__):
                run = self.runner.create("candidate-a", self.commit)
                tamper(run.log_path)
                with self.assertRaises(WorktreeRunnerError):
                    self.runner.cleanup(run)
                self.assertFalse(run.path.exists())

    def test_creation_failure_keeps_the_log_and_removes_the_partial_checkout(self):
        (self.repository / ".gitattributes").write_text(
            "state.txt filter=boom\n", encoding="utf-8"
        )
        self.git("add", ".gitattributes")
        self.git("commit", "-m", "add a filter that cannot run")
        self.git("config", "filter.boom.smudge", "false")
        self.git("config", "filter.boom.required", "true")
        failing_commit = self.git("rev-parse", "HEAD").stdout.strip()

        with self.assertRaises(WorktreeRunnerError):
            self.runner.create("candidate-a", failing_commit)

        (log,) = list(self.runner.logs_directory.iterdir())
        events = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(
            [event["event"] for event in events], ["created", "creation_failed"]
        )
        self.assertEqual({event["candidate_id"] for event in events}, {"candidate-a"})
        self.assertEqual(list(self.runner.runs_directory.iterdir()), [])
        self.assertEqual(self.runner._runs, {})
        self.assert_only_the_main_worktree_remains()

    def test_cleanup_prunes_git_metadata_when_the_candidate_deleted_its_checkout(self):
        run = self.runner.create("candidate-a", self.commit)
        result = self.runner.execute(
            run,
            [
                sys.executable,
                "-c",
                "import os, shutil; d = os.getcwd(); os.chdir('/'); shutil.rmtree(d)",
            ],
            PATIENCE,
        )
        self.assertEqual(result.status, "completed")
        self.assert_only_the_main_worktree_remains(run)

    def test_cleanup_removes_a_checkout_the_candidate_locked(self):
        run = self.runner.create("candidate-a", self.commit)
        result = self.runner.execute(
            run, ["git", "worktree", "lock", "--reason", "mine", "."], PATIENCE
        )
        self.assertEqual((result.status, result.exit_code), ("completed", 0))
        self.assert_only_the_main_worktree_remains(run)

    def test_cleanup_removes_a_checkout_whose_git_link_was_deleted(self):
        run = self.runner.create("candidate-a", self.commit)
        result = self.runner.execute(
            run, [sys.executable, "-c", "import os; os.remove('.git')"], PATIENCE
        )
        self.assertEqual(result.status, "completed")
        self.assert_only_the_main_worktree_remains(run)

    def test_cleanup_removes_a_checkout_the_candidate_renamed(self):
        run = self.runner.create("candidate-a", self.commit)
        script = "import os; d = os.getcwd(); os.chdir('/'); os.rename(d, d + '-moved')"
        self.runner.execute(run, [sys.executable, "-c", script], PATIENCE)
        self.assert_only_the_main_worktree_remains(run)

    # Evaluator /proc exposure ----------------------------------------------

    def probe_evaluator_proc(self, runner):
        # A buffer in this (the evaluator's) memory that a reader can look for.
        marker = os.urandom(16)
        buffer = ctypes.create_string_buffer(marker, len(marker))
        report = self.root / "proc-report.json"
        run = runner.create("candidate-a", self.commit)
        result = runner.execute(
            run,
            [
                sys.executable,
                "-c",
                PROBE_EVALUATOR_PROC,
                str(os.getpid()),
                str(report),
                str(ctypes.addressof(buffer)),
                marker.hex(),
            ],
            PATIENCE,
        )
        self.assertEqual((result.status, result.exit_code), ("completed", 0))
        return json.loads(report.read_text(encoding="utf-8"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc")
    def test_probe_reads_real_memory_and_reports_unreadable_memory_without_raising(
        self,
    ):
        set_dumpable(1)  # a process may always read its own memory when dumpable
        marker = os.urandom(16)
        buffer = ctypes.create_string_buffer(marker, len(marker))
        address = ctypes.addressof(buffer)
        pid = os.getpid()
        self.assertEqual(
            probe_proc_file(pid, "mem", address, marker),
            {"readable": True, "errno": None},
        )
        # Offset 0 is unmapped: reading it fails with EIO, which must not escape.
        self.assertEqual(
            probe_proc_file(pid, "mem", 0, marker),
            {"readable": False, "errno": errno.EIO},
        )
        self.assertEqual(
            probe_proc_file(pid, "no-such-file"),
            {"readable": False, "errno": errno.ENOENT},
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc")
    def test_a_same_uid_candidate_cannot_read_the_evaluator_environment(self):
        self.assertTrue(self.runner.process_hardened)
        observed = self.probe_evaluator_proc(self.runner)
        # Whatever the ``ptrace_scope`` setting, neither file may be readable.
        self.assertFalse(observed["environ"]["readable"], observed)
        self.assertFalse(observed["mem"]["readable"], observed)
        # The candidate is dumpable again after exec; the evaluator stays hardened.
        self.assertEqual(observed["own_dumpable"], 1)
        self.assertEqual(dumpable(), 0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc")
    def test_process_hardening_can_be_disabled(self):
        set_dumpable(1)
        runner = WorktreeRunner(
            self.repository, self.root / "open-runs", harden_process=False
        )
        self.assertFalse(runner.process_hardened)
        self.assertEqual(dumpable(), 1)
        # Without the mitigation the environment is readable, which is why it
        # exists.  ``mem`` depends on ``ptrace_scope`` and is not asserted here.
        observed = self.probe_evaluator_proc(runner)
        self.assertTrue(observed["environ"]["readable"], observed)

    # Process identity ----------------------------------------------------------

    def spawn_bystander(self):
        """An unrelated process standing in for one that inherited a recycled pid."""
        bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        self.assertTrue(
            wait_until(lambda: WorktreeRunner._start_time(bystander.pid) is not None)
        )
        return bystander

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc")
    def test_a_signal_reaches_only_the_process_that_was_recorded(self):
        # Both with pidfd_open and with the plain ``kill`` fallback.
        for label, context in (
            ("pidfd", contextlib.nullcontext()),
            (
                "fallback",
                mock.patch.object(os, "pidfd_open", side_effect=OSError(errno.ENOSYS)),
            ),
        ):
            with self.subTest(path=label), context:
                bystander = self.spawn_bystander()
                started = WorktreeRunner._start_time(bystander.pid)
                # The recorded process was replaced: same pid, other start time.
                WorktreeRunner._signal_identified(
                    bystander.pid, started + 1, signal.SIGKILL
                )
                time.sleep(0.3)
                self.assertIsNone(bystander.poll(), "an unrelated process was killed")
                # Same identity: the signal is delivered.
                WorktreeRunner._signal_identified(
                    bystander.pid, started, signal.SIGKILL
                )
                self.assertEqual(bystander.wait(timeout=PATIENCE), -signal.SIGKILL)

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc")
    def test_a_recycled_pid_is_neither_signalled_nor_waited_for_on_termination(self):
        self.runner.term_grace_seconds = 30
        bystander = self.spawn_bystander()
        stale = {bystander.pid: WorktreeRunner._start_time(bystander.pid) - 1}
        run = self.runner.create("candidate-a", self.commit)
        started = time.monotonic()
        with mock.patch.object(WorktreeRunner, "_descendant_pids", return_value=stale):
            result = self.runner.execute(
                run, [sys.executable, "-c", "import time; time.sleep(60)"], 0.3
            )

        self.assertEqual(result.status, "timed_out")
        # It was not treated as a live descendant to wait out for the grace period.
        self.assertLess(time.monotonic() - started, 20)
        time.sleep(0.3)
        self.assertIsNone(bystander.poll(), "an unrelated process was signalled")

    @unittest.skipUnless(hasattr(os, "WNOWAIT"), "needs waitid(WNOWAIT)")
    def test_the_leader_is_not_reaped_before_the_final_group_kill(self):
        # An unreaped leader keeps its pid (the process group id) from being reused.
        for label, script, timeout in (
            ("exits", "pass", PATIENCE),
            ("timed out", "import time; time.sleep(60)", 0.3),
        ):
            with self.subTest(candidate=label):
                seen = []
                real = WorktreeRunner._signal_group

                def spy(pgid, number, real=real, seen=seen):
                    try:
                        os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                        reaped = False
                    except ChildProcessError:
                        reaped = True
                    seen.append((number, reaped))
                    real(pgid, number)

                run = self.runner.create("candidate-a", self.commit)
                with mock.patch.object(
                    WorktreeRunner, "_signal_group", staticmethod(spy)
                ):
                    self.runner.execute(run, [sys.executable, "-c", script], timeout)

                kills = [reaped for number, reaped in seen if number == signal.SIGKILL]
                self.assertTrue(kills)
                self.assertEqual(kills, [False] * len(kills), seen)

    # State outside the source repository -------------------------------------

    def test_state_directories_inside_the_repository_are_rejected(self):
        (self.repository / "sub").mkdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "inside").symlink_to(self.repository / "sub")
        for runs, logs in (
            (self.repository / "benchmark-runs", None),
            (self.repository / "sub" / "runs", None),
            (self.repository, None),
            (self.repository / ".git" / "runs", None),
            (self.root / "runs", self.repository / "logs"),
            (elsewhere / "inside" / "runs", self.root / "logs"),
        ):
            with (
                self.subTest(runs=runs, logs=logs),
                self.assertRaisesRegex(ValueError, "outside the source repository"),
            ):
                WorktreeRunner(self.repository, runs, logs)
        self.assertEqual(self.git("status", "--porcelain", "-uall").stdout, "")
        self.assertFalse(list(self.repository.rglob("*-logs")))

    def test_a_subdirectory_or_linked_worktree_does_not_hide_the_repository_root(self):
        linked = self.root / "linked"
        self.git("worktree", "add", "--detach", str(linked))
        for repository in (self.repository / "sub", linked):
            repository.mkdir(exist_ok=True)
            with (
                self.subTest(repository=repository),
                self.assertRaisesRegex(ValueError, "outside the source repository"),
            ):
                WorktreeRunner(repository, self.repository / "benchmark-runs")

    # Durable execution metrics ------------------------------------------------

    def final_event(self, run, status):
        (event,) = [event for event in self.events(run) if event["event"] == status]
        return event

    def assert_event_matches_result(self, run, result):
        event = self.final_event(run, result.status)
        self.assertEqual(
            {key: event[key] for key in ("exit_code", "duration_ms", "stdout_bytes")},
            {
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "stdout_bytes": result.stdout_bytes,
            },
        )
        self.assertEqual(event["stderr_bytes"], result.stderr_bytes)
        for key in ("duration_ms", "stdout_bytes", "stderr_bytes"):
            self.assertIsInstance(event[key], int)

    def test_completed_run_persists_its_metrics(self):
        run = self.runner.create("candidate-a", self.commit)
        script = "import sys; print('out-secret'); print('err', file=sys.stderr)"
        result = self.runner.execute(run, [sys.executable, "-c", script], PATIENCE)
        self.assertEqual(result.status, "completed")
        self.assert_event_matches_result(run, result)
        self.assertEqual(self.final_event(run, "completed")["stdout_bytes"], 11)
        self.assertEqual(self.final_event(run, "completed")["stderr_bytes"], 4)
        self.assertNotIn("out-secret", run.log_path.read_text(encoding="utf-8"))

    def test_timed_out_run_persists_its_metrics(self):
        run = self.runner.create("candidate-a", self.commit)
        script = "import sys, time; print('out-secret', flush=True); time.sleep(60)"
        result = self.runner.execute(run, [sys.executable, "-c", script], 1.5)
        self.assertEqual(result.status, "timed_out")
        self.assert_event_matches_result(run, result)
        self.assertGreaterEqual(self.final_event(run, "timed_out")["duration_ms"], 1500)
        self.assertNotIn("out-secret", run.log_path.read_text(encoding="utf-8"))

    def test_cancelled_run_persists_its_metrics(self):
        run = self.runner.create("candidate-a", self.commit)
        ready = self.root / "printed"
        script = (
            "import sys, time; print('out-secret', flush=True); "
            "open(sys.argv[1], 'w').close(); time.sleep(60)"
        )
        worker, box = self.execute_in_thread(
            run, [sys.executable, "-c", script, str(ready)]
        )
        self.assertTrue(wait_until(ready.exists), "the candidate never printed")
        self.runner.cancel(run)
        worker.join(timeout=PATIENCE)
        self.assertEqual(box["result"].status, "cancelled")
        self.assert_event_matches_result(run, box["result"])
        self.assertEqual(self.final_event(run, "cancelled")["stdout_bytes"], 11)
        self.assertNotIn("out-secret", run.log_path.read_text(encoding="utf-8"))

    def test_a_command_that_cannot_start_persists_zero_output(self):
        run = self.runner.create("candidate-a", self.commit)
        with self.assertRaises(FileNotFoundError):
            self.runner.execute(run, ["/nonexistent/candidate-binary"], PATIENCE)
        event = self.final_event(run, "launch_failed")
        self.assertEqual((event["stdout_bytes"], event["stderr_bytes"]), (0, 0))
        self.assertIsNone(event["exit_code"])

    # TERM grace period ----------------------------------------------------------

    def test_descendants_may_finish_their_term_handlers_within_the_grace_period(self):
        self.runner.term_grace_seconds = 10
        for mode in ("orphan", "session"):
            with self.subTest(mode=mode):
                run = self.runner.create(f"slow-{mode}", self.commit)
                pid_file = self.pid_file(f"{mode}.pid")
                done = self.root / f"{mode}.done"
                worker, box = self.execute_in_thread(
                    run,
                    [
                        sys.executable,
                        "-c",
                        SLOW_TERM_CLEANUP,
                        mode,
                        str(pid_file),
                        str(done),
                    ],
                )
                self.read_pid(pid_file)
                self.runner.cancel(run)
                worker.join(timeout=PATIENCE)

                self.assertEqual(box["result"].status, "cancelled")
                self.assertTrue(done.exists(), "the TERM handler was cut short")
                self.assertEqual(done.read_text(encoding="utf-8"), "cleaned")

    def test_a_descendant_that_ignores_sigterm_is_killed_only_after_the_grace_period(
        self,
    ):
        self.runner.term_grace_seconds = 1.0
        run = self.runner.create("stubborn-child", self.commit)
        pid_file = self.pid_file()
        worker, _ = self.execute_in_thread(
            run,
            [sys.executable, "-c", ORPHANED_TERM_IGNORING_CHILD, str(pid_file), "wait"],
        )
        grandchild = self.read_pid(pid_file)
        cancelled_at = time.monotonic()
        self.runner.cancel(run)
        worker.join(timeout=PATIENCE)

        # The leader dies on TERM at once; the grace period still has to run out.
        self.assertGreaterEqual(time.monotonic() - cancelled_at, 1.0)
        self.assertTrue(wait_until(lambda: not is_running(grandchild)))

    def test_program_must_be_named_but_arguments_may_be_empty(self):
        run = self.runner.create("candidate-a", self.commit)
        result = self.runner.execute(run, [sys.executable, "-c", ""], PATIENCE)
        self.assertEqual((result.status, result.exit_code), ("completed", 0))


if __name__ == "__main__":
    unittest.main()
