"""Behavioral tests for isolated benchmark candidate worktrees."""

import contextlib
import ctypes
import errno
import inspect
import json
import os
import shutil
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
from types import SimpleNamespace
from unittest import mock

from benchmarks import worktree_runner
from benchmarks.worktree_runner import WorktreeRun, WorktreeRunner, WorktreeRunnerError

# Deadline for waiting on something that must happen; generous on purpose so a
# loaded machine slows the test down instead of failing it.
PATIENCE = 20
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

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

# Candidate that moves its own run directory (the parent of its working directory)
# to ``sys.argv[1]``; with ``sys.argv[2]`` it then plants a symlink at the old path
# pointing there, and with ``lock`` it makes the destination's parent unwritable.
MOVE_RUN_DIRECTORY = """
import os, sys
from pathlib import Path
run_directory = Path.cwd().parent
os.chdir('/')
destination = sys.argv[1]
os.rename(run_directory, destination)
if len(sys.argv) > 2 and sys.argv[2] == 'lock':
    os.chmod(os.path.dirname(destination), 0o500)
elif len(sys.argv) > 2:
    os.symlink(sys.argv[2], run_directory)
"""

# Candidate that leaves a background process in its process group and stays alive
# long enough for the runner to record that group's members before it exits.
BACKGROUND_THEN_EXIT = """
import os, sys, time
pid_file = sys.argv[1]
if os.fork() == 0:
    with open(pid_file + '.tmp', 'w') as handle:
        handle.write(str(os.getpid()))
    os.replace(pid_file + '.tmp', pid_file)
    time.sleep(60)
    os._exit(0)
time.sleep(1.0)
"""

# Candidate that forks a same-group child and exits at once, well inside the
# runner's 0.2 s sampling interval for group members.
FORK_AND_EXIT = """
import os, sys, time
pid_file = sys.argv[1]
if os.fork() == 0:
    with open(pid_file + '.tmp', 'w') as handle:
        handle.write(str(os.getpid()))
    os.replace(pid_file + '.tmp', pid_file)
    time.sleep(60)
    os._exit(0)
while not os.path.exists(pid_file):
    time.sleep(0.005)
"""

# Candidate that deletes its own run directory (the parent of its working directory).
DELETE_RUN_DIRECTORY = """
import os, shutil
from pathlib import Path
run_directory = Path.cwd().parent
os.chdir('/')
shutil.rmtree(run_directory)
"""

# Runs one candidate in a process whose SIGCHLD is ignored while the candidate runs,
# so the kernel reaps the leader itself and its exit status can never be collected.
# (Git itself cannot work with SIGCHLD ignored, so it is restored around Git calls.)
# It records each ``killpg`` the runner makes.
SIGCHLD_IGNORED_HARNESS = """
import json, os, signal, sys
from pathlib import Path
killpg_calls = []
real_killpg = os.killpg
def spy(pgid, number):
    killpg_calls.append(int(number))
    return real_killpg(pgid, number)
os.killpg = spy
from benchmarks.worktree_runner import WorktreeRunner
real_supervise = WorktreeRunner._supervise
def supervise(self, run_id, leader, timeout):
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        return real_supervise(self, run_id, leader, timeout)
    finally:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
WorktreeRunner._supervise = supervise
repository, runs, commit, mode, pid_file, background = sys.argv[1:7]
runner = WorktreeRunner(Path(repository), Path(runs))
runner.drain_seconds = 0.3
run = runner.create('candidate-a', commit)
if mode == 'fails':
    command = [sys.executable, '-c', 'import time; time.sleep(0.5); raise SystemExit(3)']
else:
    command = [sys.executable, '-c', background, pid_file]
result = runner.execute(run, command, 30)
events = [json.loads(line) for line in run.log_path.read_text().splitlines()]
logged = [event for event in events if event['event'] == result.status][0]
print(json.dumps({'status': result.status, 'exit_code': result.exit_code,
                  'logged_exit_code': logged['exit_code'], 'killpg': killpg_calls}))
"""

# Candidate that ignores SIGTERM itself, so only SIGKILL after the grace period
# ends it.  It signals readiness once the handler is installed.
TERM_IGNORING_LEADER = """
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1], 'w').close()
time.sleep(60)
"""

# Candidate whose SIGTERM handler writes 1 MiB (far more than a pipe holds) before
# it records that it finished: it can only finish if its output is being drained.
NOISY_TERM_HANDLER = """
import os, signal, sys, time
ready, done = sys.argv[1:3]

def cleanup(signum, frame):
    sys.stdout.buffer.write(b'x' * (1 << 20))
    sys.stdout.buffer.flush()
    with open(done, 'w') as handle:
        handle.write('cleaned')
    os._exit(0)

signal.signal(signal.SIGTERM, cleanup)
with open(ready + '.tmp', 'w') as handle:
    handle.write('ready')
os.replace(ready + '.tmp', ready)
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

    def test_a_term_handler_that_writes_a_lot_can_finish_within_the_grace_period(self):
        self.runner.term_grace_seconds = 20
        run = self.runner.create("noisy-handler", self.commit)
        ready = self.root / "noisy-ready"
        done = self.root / "noisy-done"
        worker, box = self.execute_in_thread(
            run, [sys.executable, "-c", NOISY_TERM_HANDLER, str(ready), str(done)]
        )
        self.assertTrue(wait_until(ready.exists), "the handler was never installed")
        self.runner.cancel(run)
        worker.join(timeout=PATIENCE)

        self.assertFalse(worker.is_alive())
        result = box["result"]
        self.assertEqual(result.status, "cancelled")
        # Its cleanup finished (it was not killed while blocked on a full pipe)
        # and everything it wrote was counted.
        self.assertEqual(done.read_text(), "cleaned")
        self.assertGreaterEqual(result.stdout_bytes, 1 << 20)
        self.assertEqual(result.exit_code, 0)

    def assert_cleanup_survives_a_failing_log(self, patch_target, failing):
        run = self.runner.create("full-disk", self.commit)
        info = run.log_path.stat()
        real = getattr(os, patch_target)
        with (
            mock.patch.object(
                worktree_runner.os,
                patch_target,
                lambda descriptor, *rest: failing(
                    real, (info.st_dev, info.st_ino), descriptor, *rest
                ),
            ),
            self.assertRaises(WorktreeRunnerError) as caught,
        ):
            self.runner.cleanup(run)

        # The failure is reported as ours, without the operating system's text.
        self.assertNotIn("space", str(caught.exception))
        self.assertNotIn("I/O", str(caught.exception))
        # ... but the checkout, its Git metadata and the ownership are gone.
        self.assertFalse(run.path.exists())
        self.assertNotIn(str(run.path), self.git("worktree", "list").stdout)
        self.assertNotIn(run.run_id, self.runner._runs)

    def test_a_failing_log_write_does_not_stop_cleanup(self):
        def failing_write(real, log, descriptor, data):
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) == log:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(descriptor, data)

        self.assert_cleanup_survives_a_failing_log("write", failing_write)

    def test_a_lost_terminal_event_is_reported_after_cleanup(self):
        # Only the record of the outcome cannot be written; the later cleanup
        # events can. ``execute`` must not return as if the log were complete.
        run = self.runner.create("lost-outcome", self.commit)
        real_write = os.write

        def failing_write(descriptor, data):
            if b'"event":"completed"' in data:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(descriptor, data)

        with (
            mock.patch.object(worktree_runner.os, "write", failing_write),
            self.assertRaises(WorktreeRunnerError) as caught,
        ):
            self.runner.execute(run, [sys.executable, "-c", "pass"], PATIENCE)

        self.assertNotIn("space", str(caught.exception))
        self.assertFalse(run.path.exists())  # cleanup still ran
        events = [event["event"] for event in self.events(run)]
        self.assertNotIn("completed", events)
        self.assertEqual(events[-2:], ["cleanup_started", "cleanup_finished"])
        self.assertNotIn(run.run_id, self.runner._runs)

    def test_the_child_is_stopped_when_the_supervisor_cannot_be_set_up(self):
        # After the launch, building the output drain can still fail (for example
        # EMFILE under descriptor exhaustion). The candidate must not be left
        # running with its worktree removed from under it.
        run = self.runner.create("no-drain", self.commit)
        pid_file = self.pid_file()
        script = (
            "import os, sys, time\n"
            "open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
            "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
            "time.sleep(60)\n"
        )

        def no_descriptors(process):
            raise OSError(errno.EMFILE, "Too many open files")

        with (
            mock.patch.object(worktree_runner, "_PipeDrain", no_descriptors),
            self.assertRaises(OSError),
        ):
            self.runner.execute(
                run, [sys.executable, "-c", script, str(pid_file)], PATIENCE
            )

        # A live child publishes its pid within moments; a child that was stopped
        # in time never does. Either way it must not be left running.
        wait_until(pid_file.exists, 3)
        if pid_file.exists():
            child = self.read_pid(pid_file)
            self.assertTrue(wait_until(lambda: not is_running(child)))
        self.assertFalse(run.path.exists())
        self.assertNotIn(run.run_id, self.runner._runs)

    # A child that nobody supervises ----------------------------------------------

    @contextlib.contextmanager
    def spy_on_signals(self, passthrough):
        """Record every signal sent by number; deliver them only if ``passthrough``."""
        calls = []
        real_killpg, real_kill, real_popen_kill = (
            os.killpg,
            os.kill,
            subprocess.Popen.kill,
        )

        def killpg(pgid, number):
            calls.append(("killpg", pgid, int(number)))
            if passthrough:
                real_killpg(pgid, number)

        def kill(pid, number):
            calls.append(("kill", pid, int(number)))
            if passthrough:
                real_kill(pid, number)

        def popen_kill(process):
            calls.append(("Popen.kill", process.pid, int(signal.SIGKILL)))
            if passthrough:
                real_popen_kill(process)

        with (
            mock.patch.object(os, "killpg", killpg),
            mock.patch.object(os, "kill", kill),
            mock.patch.object(subprocess.Popen, "kill", popen_kill),
        ):
            yield calls

    def fail_to_build_the_leader(self, action, seen):
        """Stand in for ``_Leader``: run ``action`` on the just-launched child, then
        fail as descriptor exhaustion would."""

        def fail(process):
            seen.append(process.pid)
            action(process)
            raise OSError(errno.EMFILE, "Too many open files")

        return mock.patch.object(worktree_runner, "_Leader", fail)

    def test_a_child_reaped_elsewhere_is_not_signalled_when_no_leader_can_be_built(
        self,
    ):
        # The child was collected by somebody else (SIGCHLD ignored, a concurrent
        # reaper) before the leader could be built, so its pid and process group
        # id may already belong to an unrelated process: nothing may be sent.
        run = self.runner.create("reaped-elsewhere", self.commit)
        seen = []

        def reap(process):
            os.waitpid(process.pid, 0)  # waits for the exit, then collects it

        with (
            self.spy_on_signals(passthrough=False) as calls,
            self.fail_to_build_the_leader(reap, seen),
            self.assertRaises(OSError),
        ):
            self.runner.execute(run, [sys.executable, "-c", "pass"], PATIENCE)

        self.assertEqual(len(seen), 1)
        self.assertEqual(calls, [], "a reaped child's number was signalled")
        self.assertFalse(run.path.exists())
        self.assertNotIn(run.run_id, self.runner._runs)

    def test_a_live_child_and_its_group_are_killed_when_no_leader_can_be_built(self):
        run = self.runner.create("no-leader", self.commit)
        pid_file = self.pid_file()
        script = (
            "import os, sys, time\n"
            "if os.fork() == 0:\n"
            "    open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
            "    os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
            "    time.sleep(60)\n"
            "    os._exit(0)\n"
            "time.sleep(60)\n"
        )
        seen = []

        def publish(process):
            # The member is running in the candidate's group by now.
            self.assertTrue(wait_until(pid_file.exists))

        with (
            self.spy_on_signals(passthrough=True) as calls,
            self.fail_to_build_the_leader(publish, seen),
            self.assertRaises(OSError),
        ):
            self.runner.execute(
                run, [sys.executable, "-c", script, str(pid_file)], PATIENCE
            )

        (leader,) = seen
        member = self.read_pid(pid_file)
        for pid in (leader, member):
            self.assertTrue(wait_until(lambda pid=pid: not is_running(pid)))
        # Only the child's own group was signalled, and it was killed.
        self.assertIn(("killpg", leader, int(signal.SIGKILL)), calls)
        self.assertEqual({pid for _, pid, _ in calls}, {leader}, calls)
        self.assertFalse(run.path.exists())

    def spawn_unsupervised(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def clean_up():
            with contextlib.suppress(OSError):
                os.kill(process.pid, signal.SIGKILL)
                os.waitpid(process.pid, 0)
            for stream in (process.stdout, process.stderr):
                stream.close()

        self.addCleanup(clean_up)
        self.assertTrue(
            wait_until(lambda: WorktreeRunner._start_time(process.pid) is not None)
        )
        return process

    def test_stopping_an_unsupervised_child_needs_its_recorded_identity(self):
        process = self.spawn_unsupervised()
        started = WorktreeRunner._start_time(process.pid)

        # Another process now has this pid (same number, other start time).
        with self.spy_on_signals(passthrough=True) as calls:
            WorktreeRunner._stop_unsupervised(process, started + 1)
        self.assertEqual(calls, [])
        time.sleep(0.3)
        self.assertTrue(
            is_running(process.pid), "a process with another identity was killed"
        )

    def test_a_reaped_child_is_not_signalled_even_without_a_recorded_start_time(self):
        # Without /proc no start time is known, so only "still our unreaped child"
        # can tell that the number was released.
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.waitpid(process.pid, 0)  # somebody else collected it

        with self.spy_on_signals(passthrough=False) as calls:
            WorktreeRunner._stop_unsupervised(process, None)

        self.assertEqual(calls, [])
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_an_unsupervised_child_with_its_recorded_identity_is_killed_and_reaped(
        self,
    ):
        process = self.spawn_unsupervised()
        started = WorktreeRunner._start_time(process.pid)

        with self.spy_on_signals(passthrough=True) as calls:
            WorktreeRunner._stop_unsupervised(process, started)
        self.assertIn(("killpg", process.pid, int(signal.SIGKILL)), calls)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_a_metadata_entry_replaced_by_a_file_is_removed(self):
        run = self.runner.create("admin-file", self.commit)
        admin = self.runner._runs[run.run_id].admin_directory
        shutil.rmtree(admin)
        admin.write_text("not a directory\n")

        self.runner.cleanup(run)  # no raw NotADirectoryError

        self.assertFalse(os.path.lexists(admin))
        self.assertFalse(run.path.exists())
        events = [event["event"] for event in self.events(run)]
        self.assertEqual(events[-2:], ["cleanup_started", "cleanup_finished"])

    def test_inaccessible_git_metadata_is_a_cleanup_failure_not_absence(self):
        # ``lstat`` of the entry reports a permission error (a candidate removed the
        # search permission of ``.git/worktrees``): that is not the same as being
        # gone. The error is injected, so the test does not depend on the user:
        # root would still be allowed to look.
        run = self.runner.create("locked-admin", self.commit)
        admin = self.runner._runs[run.run_id].admin_directory
        real_git = self.runner._git
        real_lstat = os.lstat

        def git_leaves_its_metadata(*arguments, **options):
            result = real_git(*arguments, **options)
            if arguments[:2] == ("worktree", "remove"):
                admin.mkdir(parents=True, exist_ok=True)  # Git could not drop it
            return result

        def refuse_to_look(path, *args, **kwargs):
            if Path(path) == admin:
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_lstat(path, *args, **kwargs)

        try:
            with (
                mock.patch.object(self.runner, "_git", git_leaves_its_metadata),
                mock.patch.object(worktree_runner.os, "lstat", refuse_to_look),
                self.assertRaises(WorktreeRunnerError),
            ):
                self.runner.cleanup(run)
        finally:
            shutil.rmtree(admin, ignore_errors=True)

        events = [event["event"] for event in self.events(run)]
        self.assertEqual(events[-2:], ["cleanup_started", "cleanup_incomplete"])
        self.assertNotIn(run.run_id, self.runner._runs)

    def test_a_removal_that_fails_is_reported_as_incomplete(self):
        run = self.runner.create("stuck-admin", self.commit)
        admin = self.runner._runs[run.run_id].admin_directory
        real_rmtree = shutil.rmtree

        def stuck(path, *args, **kwargs):
            if Path(path) == admin:
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_rmtree(path, *args, **kwargs)

        real_git = self.runner._git

        def git_leaves_its_metadata(*arguments, **options):
            result = real_git(*arguments, **options)
            if arguments[:2] == ("worktree", "remove"):
                admin.mkdir(parents=True, exist_ok=True)  # Git could not drop it
            return result

        with (
            mock.patch.object(self.runner, "_git", git_leaves_its_metadata),
            mock.patch.object(worktree_runner.shutil, "rmtree", stuck),
            self.assertRaises(WorktreeRunnerError) as caught,
        ):
            self.runner.cleanup(run)

        self.assertNotIn("Permission", str(caught.exception))
        events = [event["event"] for event in self.events(run)]
        self.assertEqual(events[-2:], ["cleanup_started", "cleanup_incomplete"])
        self.assertNotIn(run.run_id, self.runner._runs)
        real_rmtree(admin, ignore_errors=True)  # leave nothing behind

    def test_a_failing_log_close_does_not_stop_cleanup(self):
        # close(2) can report a delayed write error; the descriptor is released
        # all the same.
        def failing_close(real, log, descriptor):
            try:
                info = os.fstat(descriptor)
            except OSError:
                info = None
            real(descriptor)
            if info is not None and (info.st_dev, info.st_ino) == log:
                raise OSError(errno.EIO, "I/O error")

        self.assert_cleanup_survives_a_failing_log("close", failing_close)

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

    def test_git_operations_of_the_runner_get_no_credentials(self):
        secrets = {
            "SECRET_TOKEN": "hunter2",
            "GITHUB_TOKEN": "ghp_not_for_git",
            "ANTHROPIC_API_KEY": "sk-not-for-git",
            "GIT_DIR": str(self.repository / ".git"),
            "GIT_INDEX_FILE": str(self.repository / ".git" / "index"),
        }
        with mock.patch.dict(os.environ, secrets):
            environment = WorktreeRunner._git_environment()
        self.assertEqual(set(environment) & set(secrets), set())
        self.assertLessEqual(
            set(environment), set(worktree_runner._GIT_ENVIRONMENT_ALLOWLIST)
        )
        self.assertEqual(environment["PATH"], os.environ["PATH"])

    def test_a_hook_planted_by_a_candidate_never_sees_the_evaluator_environment(self):
        # The candidate shares the repository's Git directory, so it can leave a
        # hook there that the runner's next ``git worktree add`` would start.
        first = self.runner.create("candidate-a", self.commit)
        leaked = self.root / "leaked.txt"
        hook_source = (
            "#!" + sys.executable + "\n"
            "import os\n"
            f"open({str(leaked)!r}, 'w').write(os.environ.get('SECRET_TOKEN', ''))\n"
        )
        plant = (
            "import os, pathlib, sys; "
            "hook = pathlib.Path(sys.argv[1]) / 'hooks' / 'post-checkout'; "
            "hook.parent.mkdir(exist_ok=True); "
            "hook.write_text(sys.argv[2]); "
            "os.chmod(hook, 0o755)"
        )
        result = self.runner.execute(
            first,
            [sys.executable, "-c", plant, str(self.repository / ".git"), hook_source],
            timeout_seconds=PATIENCE,
        )
        self.assertEqual((result.status, result.exit_code), ("completed", 0))
        with mock.patch.dict(os.environ, {"SECRET_TOKEN": "hunter2"}):
            second = self.runner.create("candidate-b", self.commit)
        self.addCleanup(self.runner.cleanup, second)
        self.assertNotIn("hunter2", leaked.read_text() if leaked.exists() else "")

    def test_the_runner_never_starts_a_hook_of_the_shared_git_directory(self):
        started = self.root / "hook-started.txt"
        hooks = self.repository / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        hook = hooks / "post-checkout"
        hook.write_text(
            "#!" + sys.executable + f"\nopen({str(started)!r}, 'w').write('started')\n"
        )
        hook.chmod(0o755)
        run = self.runner.create("candidate-a", self.commit)
        self.addCleanup(self.runner.cleanup, run)
        self.assertFalse(started.exists())

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

    # A run directory the candidate moved ------------------------------------------

    def run_that_moves_its_directory(self, destination, *extra):
        run = self.runner.create("candidate-a", self.commit)
        result = self.runner.execute(
            run,
            [sys.executable, "-c", MOVE_RUN_DIRECTORY, str(destination), *extra],
            PATIENCE,
        )
        return run, result

    def assert_lifecycle_ended_with(self, run, last_event):
        names = [event["event"] for event in self.events(run)]
        self.assertEqual(names[-2:], ["cleanup_started", last_event], names)
        if last_event == "cleanup_incomplete":
            self.assertNotIn("cleanup_finished", names)

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc")
    def test_a_renamed_run_directory_is_found_and_removed(self):
        runs = self.runner.runs_directory
        # Renamed in place, next to where it was.
        run, result = self.run_that_moves_its_directory(runs / "renamed-by-candidate")
        self.assertEqual(result.status, "completed")
        self.assertEqual(list(runs.iterdir()), [])
        self.assert_lifecycle_ended_with(run, "cleanup_finished")
        self.assert_only_the_main_worktree_remains(run)

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc")
    def test_a_run_directory_moved_elsewhere_is_found_and_removed(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        run, result = self.run_that_moves_its_directory(elsewhere / "stolen")
        self.assertEqual(result.status, "completed")
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertEqual(list(self.runner.runs_directory.iterdir()), [])
        self.assert_lifecycle_ended_with(run, "cleanup_finished")
        self.assert_only_the_main_worktree_remains(run)

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc")
    def test_a_symlink_planted_at_the_old_path_is_removed_not_followed(self):
        victim = self.root / "victim"
        victim.mkdir()
        (victim / "precious.txt").write_text("precious\n", encoding="utf-8")
        run, _ = self.run_that_moves_its_directory(self.root / "stolen", str(victim))
        self.assertEqual(
            (victim / "precious.txt").read_text(encoding="utf-8"), "precious\n"
        )
        self.assertFalse((self.root / "stolen").exists())
        self.assertEqual(list(self.runner.runs_directory.iterdir()), [])
        self.assert_lifecycle_ended_with(run, "cleanup_finished")

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc")
    def test_a_live_directory_whose_name_ends_in_deleted_is_still_removed(self):
        # ``/proc`` prints a live directory called "x (deleted)" exactly like a
        # deleted one, so the text alone must not decide.
        destination = self.root / "stolen (deleted)"
        run, result = self.run_that_moves_its_directory(destination)
        self.assertEqual(result.status, "completed")
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.runner.runs_directory.iterdir()), [])
        self.assert_lifecycle_ended_with(run, "cleanup_finished")
        self.assert_only_the_main_worktree_remains(run)

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc")
    def test_a_run_directory_the_candidate_deleted_is_reported_finished(self):
        run = self.runner.create("candidate-a", self.commit)
        result = self.runner.execute(
            run, [sys.executable, "-c", DELETE_RUN_DIRECTORY], PATIENCE
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(list(self.runner.runs_directory.iterdir()), [])
        self.assert_lifecycle_ended_with(run, "cleanup_finished")
        self.assert_only_the_main_worktree_remains(run)

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc")
    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_a_run_directory_that_cannot_be_removed_is_reported_incomplete(self):
        locked = self.root / "locked"
        locked.mkdir()
        self.addCleanup(locked.chmod, 0o700)
        run = self.runner.create("candidate-a", self.commit)
        with self.assertRaises(WorktreeRunnerError):
            self.runner.execute(
                run,
                [
                    sys.executable,
                    "-c",
                    MOVE_RUN_DIRECTORY,
                    str(locked / "stolen"),
                    "lock",
                ],
                PATIENCE,
            )
        # What could be removed was: the contents are gone, only the emptied
        # directory (whose parent is read-only) is left, and Git's record is pruned.
        self.assertEqual(list((locked / "stolen").iterdir()), [])
        self.assert_lifecycle_ended_with(run, "cleanup_incomplete")
        self.assert_only_the_main_worktree_remains()
        self.assertEqual(self.runner._runs, {})

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc")
    def test_a_run_directory_that_cannot_be_located_is_never_reported_finished(self):
        real_readlink = os.readlink

        def readlink(path, *args, **kwargs):
            if str(path).startswith("/proc/self/fd/"):
                raise OSError(errno.ENOENT, "no /proc")
            return real_readlink(path, *args, **kwargs)

        with mock.patch.object(os, "readlink", readlink):
            # In place: the original path is still the directory we made, so it
            # can be removed and reported finished even without /proc.
            in_place = self.runner.create("candidate-a", self.commit)
            self.runner.execute(in_place, [sys.executable, "-c", "pass"], PATIENCE)
            self.assert_lifecycle_ended_with(in_place, "cleanup_finished")

            # Moved: nothing says where it went, so it cannot be claimed removed.
            moved = self.runner.create("candidate-b", self.commit)
            destination = self.root / "unused-name"
            with self.assertRaises(WorktreeRunnerError):
                self.runner.execute(
                    moved,
                    [sys.executable, "-c", MOVE_RUN_DIRECTORY, str(destination)],
                    PATIENCE,
                )
        self.assertTrue(destination.exists())
        self.assert_lifecycle_ended_with(moved, "cleanup_incomplete")
        self.assert_only_the_main_worktree_remains()

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc")
    def test_run_directory_handles_are_closed_after_every_outcome(self):
        def open_descriptors():
            return len(os.listdir("/proc/self/fd"))

        before = open_descriptors()
        self.runner.execute(
            self.runner.create("candidate-a", self.commit),
            [sys.executable, "-c", "pass"],
            PATIENCE,
        )
        self.run_that_moves_its_directory(self.root / "stolen")
        self.runner.cleanup(self.runner.create("candidate-b", self.commit))
        self.assertEqual(open_descriptors(), before)

    # A leader that something else reaped ----------------------------------------

    def run_with_sigchld_ignored(
        self, mode, pid_file="unused", script=BACKGROUND_THEN_EXIT
    ):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                SIGCHLD_IGNORED_HARNESS,
                str(self.repository),
                str(self.root / "sigchld-runs"),
                self.commit,
                mode,
                str(pid_file),
                script,
            ],
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPOSITORY_ROOT)},
            capture_output=True,
            text=True,
            timeout=PATIENCE * 2,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_an_unknowable_exit_status_is_unknown_not_zero(self):
        # ``Popen`` reads 0 when the status was reaped elsewhere; a failing
        # candidate (exit 3) must not be reported, or logged, as exiting cleanly.
        observed = self.run_with_sigchld_ignored("fails")
        self.assertEqual(observed["status"], "completed")
        self.assertIsNone(observed["exit_code"])
        self.assertIsNone(observed["logged_exit_code"])

    def test_no_group_signal_is_sent_for_a_group_id_that_may_be_reused(self):
        observed = self.run_with_sigchld_ignored("fails")
        # The leader vanished and no member of its group was ever seen: the
        # group id could belong to a stranger by now, so nothing may be sent.
        self.assertEqual(observed["killpg"], [])

    def test_a_recorded_member_of_a_reaped_leaders_group_is_still_killed(self):
        pid_file = self.pid_file()
        observed = self.run_with_sigchld_ignored("background", pid_file)
        self.assertIn(signal.SIGKILL, observed["killpg"])
        background = self.read_pid(pid_file)
        self.assertTrue(wait_until(lambda: not is_running(background)))

    def test_a_member_forked_just_before_the_leader_vanished_is_still_killed(self):
        pid_file = self.pid_file()
        observed = self.run_with_sigchld_ignored("script", pid_file, FORK_AND_EXIT)
        self.assertIn(signal.SIGKILL, observed["killpg"])
        child = self.read_pid(pid_file)
        self.assertTrue(wait_until(lambda: not is_running(child)))

    @unittest.skipUnless(hasattr(os, "WNOWAIT"), "needs waitid(WNOWAIT)")
    def test_a_leader_reaped_after_it_was_observed_does_not_reserve_its_group(self):
        leader_process = subprocess.Popen(
            [sys.executable, "-c", "pass"], start_new_session=True
        )
        self.addCleanup(leader_process.wait)
        leader = worktree_runner._Leader(leader_process)
        self.assertTrue(wait_until(leader.has_exited))  # an unreaped zombie
        self.assertFalse(leader.released)
        os.waitpid(leader_process.pid, 0)  # a concurrent reaper collects it
        sent = []
        with mock.patch.object(
            WorktreeRunner,
            "_signal_group",
            staticmethod(lambda *args: sent.append(args)),
        ):
            leader.signal_group(signal.SIGKILL)
        self.assertEqual(sent, [])
        self.assertTrue(leader.released and leader.status_lost)

    @unittest.skipUnless(hasattr(os, "WNOWAIT"), "needs waitid(WNOWAIT)")
    def test_a_concurrent_reaper_during_supervision_loses_the_status_and_the_signal(
        self,
    ):
        # The reaper acts after the leader was seen exiting and before the final
        # group kill.  (The group signal is spied on, not sent: the id is free.)
        sent = []
        real_signal_group = worktree_runner._Leader.signal_group

        def reaper_first(leader, number):
            if leader.process.returncode is None:
                os.waitpid(leader.pgid, 0)
            real_signal_group(leader, number)

        run = self.runner.create("candidate-a", self.commit)
        with (
            mock.patch.object(worktree_runner._Leader, "signal_group", reaper_first),
            mock.patch.object(
                WorktreeRunner,
                "_signal_group",
                staticmethod(lambda *args: sent.append(args)),
            ),
        ):
            result = self.runner.execute(run, [sys.executable, "-c", "pass"], PATIENCE)
        self.assertEqual(sent, [])
        self.assertIsNone(result.exit_code)

    @unittest.skipUnless(hasattr(os, "WNOWAIT"), "needs waitid(WNOWAIT)")
    def test_reap_decodes_the_exit_status_and_notices_a_reaper_that_was_first(self):
        exited = subprocess.Popen([sys.executable, "-c", "raise SystemExit(5)"])
        killed = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        stolen = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(killed.kill)
        killed.kill()
        for process in (exited, killed, stolen):
            self.assertTrue(wait_until(worktree_runner._Leader(process).has_exited))
        os.waitpid(stolen.pid, 0)
        results = {}
        for name, process in (
            ("exited", exited),
            ("killed", killed),
            ("stolen", stolen),
        ):
            leader = worktree_runner._Leader(process)
            leader.reap(PATIENCE)
            results[name] = (
                leader.status_lost,
                process.returncode if not leader.status_lost else None,
            )
        self.assertEqual(
            results,
            {
                "exited": (False, 5),
                "killed": (False, -signal.SIGKILL),
                "stolen": (True, None),
            },
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux /proc")
    def test_a_released_group_id_is_signalled_only_while_a_recorded_member_remains(
        self,
    ):
        # A stranger that is a group leader, standing in for a process that was
        # given the reaped leader's pid as its own group id.
        stranger = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        self.addCleanup(stranger.wait)
        self.addCleanup(stranger.kill)
        self.assertTrue(
            wait_until(lambda: WorktreeRunner._start_time(stranger.pid) is not None)
        )
        leader = worktree_runner._Leader(
            SimpleNamespace(pid=stranger.pid, returncode=None)
        )
        with mock.patch.object(os, "waitid", side_effect=ChildProcessError):
            self.assertTrue(leader.has_exited())
        self.assertTrue(leader.released and leader.status_lost)

        # Nothing recorded ever belonged to this group id: never signal it.
        leader.members = {stranger.pid + 100000: 1}
        leader.signal_group(signal.SIGKILL)
        leader.members = {stranger.pid: WorktreeRunner._start_time(stranger.pid) + 1}
        leader.signal_group(signal.SIGKILL)  # same pid, different start time
        time.sleep(0.3)
        self.assertIsNone(stranger.poll(), "a stranger's group was signalled")

        # A member recorded earlier (same pid and start time) is still in the group.
        leader.members = {stranger.pid: WorktreeRunner._start_time(stranger.pid)}
        leader.signal_group(signal.SIGKILL)
        self.assertEqual(stranger.wait(timeout=PATIENCE), -signal.SIGKILL)

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
