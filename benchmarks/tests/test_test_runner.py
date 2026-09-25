"""Acceptance tests for visible and hidden benchmark check execution."""

import contextlib
import errno
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import tracemalloc
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from benchmarks import test_runner
from benchmarks.test_runner import CheckDefinition, HiddenCheckRegistry, TestRunner

# Deadline for an ordinary check that is expected to finish.  Generous on
# purpose: a loaded machine must slow a test down, never fail it.  Small
# deadlines are used only where the timeout itself is under test.
PATIENCE = 30
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# Check that starts a child in its own session (so it is not in the check's
# process group), publishes the child's pid, then blocks.
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

# Check that leaves an orphaned grandchild in its own process group.  The
# grandchild ignores SIGTERM and, having been re-parented, is not a descendant
# of the check, so only a group-wide SIGKILL can stop it.  With ``exit`` the
# check returns once the grandchild runs; otherwise it blocks.
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

# Check that leaves a descendant whose SIGTERM handler needs ~0.3 s to finish its
# cleanup.  ``orphan``: an orphaned member of the check's process group.
# ``session``: a child in its own session.  ``daemon``: a double fork plus setsid, so
# it is neither in the group nor below the check.  The check itself dies on SIGTERM.
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
    elif mode == 'daemon':
        os.setsid()
        if os.fork() == 0:
            install_handler_and_wait()
    elif os.fork() == 0:
        install_handler_and_wait()
    os._exit(0)
if mode in ('orphan', 'daemon'):
    os.waitpid(child, 0)
while not os.path.exists(pid_file):
    time.sleep(0.01)
time.sleep(60)
"""

# Check whose SIGTERM handler starts a helper in a session of its own, so the helper
# is neither in the check's process group nor known before the timeout.  The helper
# installs a SIGTERM handler whose cleanup takes ~0.3 s and publishes its pid; the
# check's handler waits for that, so the helper can only be stopped cleanly if it is
# sent SIGTERM itself.  ``exit``: the check then exits at once.  ``wait``: it stays
# until the helper's cleanup has finished.  ``scrubbed-wait``: as ``wait`` with a
# helper that carries no marker of the check, so only the record of what runs below
# the check can find it.  The helper is started with SIGTERM blocked and unblocks it
# once its handler is installed and its pid published, so a SIGTERM that reaches it
# early is held back instead of killing it: the test does not depend on how soon after
# its start the runner first sees it.  Optional arguments: how long the check's handler
# waits before it starts the helper (default 0) and how long the helper's cleanup takes
# (default 0.3), both in seconds.
TERM_HANDLER_STARTS_HELPER = """
import os, signal, subprocess, sys, time
mode, pid_file, done, ready = sys.argv[1:5]
delay = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0
cleanup_seconds = sys.argv[6] if len(sys.argv) > 6 else '0.3'
HELPER = (
    'import os, signal, sys, time\\n'
    'pid_file, done = sys.argv[1:3]\\n'
    'def cleanup(signum, frame):\\n'
    '    time.sleep(' + cleanup_seconds + ')\\n'
    '    open(done, "w").write("cleaned")\\n'
    '    os._exit(0)\\n'
    'signal.signal(signal.SIGTERM, cleanup)\\n'
    'open(pid_file + ".tmp", "w").write(str(os.getpid()))\\n'
    'os.replace(pid_file + ".tmp", pid_file)\\n'
    'signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGTERM])\\n'
    'time.sleep(60)\\n'
)
started = False

def wait_for(path):
    deadline = time.monotonic() + 20
    while not os.path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.005)

def on_term(signum, frame):
    global started
    if started:
        return
    started = True
    time.sleep(delay)
    null = subprocess.DEVNULL
    # The child inherits the blocked signal mask across fork and exec.
    old = signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGTERM])
    subprocess.Popen(
        [sys.executable, '-c', HELPER, pid_file, done],
        start_new_session=True, stdin=null, stdout=null, stderr=null,
        env={} if mode.startswith('scrubbed') else None,
    )
    signal.pthread_sigmask(signal.SIG_SETMASK, old)
    wait_for(pid_file)
    if mode.endswith('wait'):
        wait_for(done)
    os._exit(0)

signal.signal(signal.SIGTERM, on_term)
open(ready, 'w').close()
time.sleep(60)
"""

# Check whose SIGTERM handler never stops starting processes that ignore SIGTERM, each
# in a session of its own, one every 0.2 s for up to 15 s, and records their pids.
# Every one is a newcomer for the runner; its wait must end anyway.
TERM_HANDLER_KEEPS_STARTING_PROCESSES = """
import signal, subprocess, sys, time
pids, ready = sys.argv[1:3]
SLEEPER = (
    'import signal, time\\n'
    'signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n'
    'time.sleep(60)\\n'
)

def on_term(signum, frame):
    null = subprocess.DEVNULL
    for _ in range(75):
        child = subprocess.Popen(
            [sys.executable, '-c', SLEEPER],
            start_new_session=True, stdin=null, stdout=null, stderr=null,
        )
        with open(pids, 'a') as handle:
            handle.write(str(child.pid) + '\\n')
        time.sleep(0.2)

signal.signal(signal.SIGTERM, on_term)
open(ready, 'w').close()
time.sleep(60)
"""

# Check that leaves a background process in its process group and stays alive long
# enough for the runner to record that group's members before it exits.
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

# Check that forks a same-group child and exits at once, well inside the runner's
# 0.2 s sampling interval for group members.
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

# Check that forks a same-group child only once ``go`` exists, then lingers for
# ``linger`` seconds (default: exits at once).  The child is a group member that no
# earlier look at the group could have seen.
FORK_WHEN_TOLD_THEN_EXIT = """
import os, sys, time
pid_file, go = sys.argv[1:3]
linger = float(sys.argv[3]) if len(sys.argv) > 3 else 0
while not os.path.exists(go):
    time.sleep(0.005)
if os.fork() == 0:
    with open(pid_file + '.tmp', 'w') as handle:
        handle.write(str(os.getpid()))
    os.replace(pid_file + '.tmp', pid_file)
    time.sleep(60)
    os._exit(0)
time.sleep(linger)
"""

# Runs one check in a process whose SIGCHLD is ignored, so the kernel reaps every
# child itself and the check leader's exit status can never be collected.  It
# records each ``killpg`` the runner makes.
SIGCHLD_IGNORED_HARNESS = """
import json, os, signal, sys
from pathlib import Path
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
killpg_calls = []
real_killpg = os.killpg
def spy(pgid, number):
    killpg_calls.append(int(number))
    return real_killpg(pgid, number)
os.killpg = spy
from benchmarks.test_runner import CheckDefinition, TestRunner
worktree, log, mode, pid_file, background = sys.argv[1:6]
runner = TestRunner(Path(worktree), Path(log))
runner.drain_seconds = 0.3
if mode == 'fails':
    command = (sys.executable, '-c', 'raise SystemExit(3)')
else:
    command = (sys.executable, '-c', background, pid_file)
result = runner.run_visible((CheckDefinition('check', 'unit', command),), 30)[0]
print(json.dumps({'status': result.status, 'exit_code': result.exit_code,
                  'killpg': killpg_calls}))
"""

# Check that starts a process outside its own process group and, by default, exits at
# once: the process is re-parented before any periodic look at the process tree could
# have recorded it.  ``session``: a new program in a session of its own with its output
# discarded.  ``daemon``: a double fork plus setsid, the classic daemon.  With a third
# argument the check blocks instead of exiting.
ESCAPES_THEN_EXITS = """
import os, subprocess, sys, time
mode, pid_file = sys.argv[1:3]
block = len(sys.argv) > 3

def publish(pid):
    with open(pid_file + '.tmp', 'w') as handle:
        handle.write(str(pid))
    os.replace(pid_file + '.tmp', pid_file)

if mode == 'session':
    null = subprocess.DEVNULL
    child = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(60)'],
        start_new_session=True, stdin=null, stdout=null, stderr=null,
    )
    publish(child.pid)
else:
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            publish(os.getpid())
            time.sleep(60)
        os._exit(0)
    while not os.path.exists(pid_file):
        time.sleep(0.005)
if block:
    time.sleep(60)
"""

# Check that starts a program with an EMPTY environment (so it carries no marker of
# the check) in a session of its own, and lingers until ``go`` exists (at most 20 s).
SCRUBBED_ESCAPED_CHILD = """
import os, sys, time
pid_file, go = sys.argv[1:3]
program = (
    'import os, sys, time\\n'
    'open(sys.argv[1] + ".tmp", "w").write(str(os.getpid()))\\n'
    'os.replace(sys.argv[1] + ".tmp", sys.argv[1])\\n'
    'time.sleep(60)\\n'
)
if os.fork() == 0:
    os.setsid()
    os.execve(sys.executable, [sys.executable, '-c', program, pid_file], {})
deadline = time.monotonic() + 20
while not os.path.exists(go) and time.monotonic() < deadline:
    time.sleep(0.005)
"""

# Check that ignores SIGTERM itself, so only SIGKILL after the grace period ends it.
TERM_IGNORING_LEADER = """
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1], 'w').close()
time.sleep(60)
"""


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


class TestRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.worktree = self.root / "candidate-worktree"
        self.worktree.mkdir()
        self.log_path = self.root / "evaluator-private" / "check-results.jsonl"
        self.runner = TestRunner(self.worktree, self.log_path)
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
        self.assertTrue(wait_until(path.exists), "the check never published its pid")
        return int(path.read_text())

    def records(self):
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]

    def python_check(self, name, script, *arguments):
        return CheckDefinition(
            name, "unit", (sys.executable, "-c", script, *map(str, arguments))
        )

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

        visible_result = self.runner.run_visible((visible,), PATIENCE)
        hidden_result = self.runner.run_hidden(("private:case-1",), registry, PATIENCE)

        self.assertEqual(visible_result[0].visibility, "visible")
        self.assertEqual(hidden_result[0].visibility, "hidden")
        self.assertEqual(hidden_result[0].stdout.decode().strip(), hidden_secret)
        self.assertFalse(any(self.worktree.rglob("*")))
        log_content = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn(hidden_secret, log_content)
        self.assertNotIn("private:case-1", log_content)
        self.assertNotIn("-c", log_content)
        self.assertEqual(stat.S_IMODE(self.log_path.stat().st_mode), 0o600)

    def test_exit_code_stdout_and_stderr_are_captured_with_safe_log_records(self):
        secret = "do-not-persist-output"
        script = (
            f"import sys; print('{secret}'); print('{secret}', file=sys.stderr); "
            "raise SystemExit(7)"
        )
        check = self.python_check("fails", script)

        (failed,) = self.runner.run_visible((check,), PATIENCE)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.exit_code, 7)
        self.assertFalse(failed.timed_out)
        self.assertEqual(failed.stdout, f"{secret}\n".encode())
        self.assertEqual(failed.stderr, f"{secret}\n".encode())
        (record,) = self.records()
        self.assertEqual(record["exit_code"], 7)
        self.assertEqual(
            record["stdout"],
            {
                "captured_bytes": len(secret) + 1,
                "sha256": hashlib.sha256(f"{secret}\n".encode()).hexdigest(),
                "truncated": False,
            },
        )
        self.assertEqual(record["stderr"]["captured_bytes"], len(secret) + 1)
        self.assertNotIn(secret, self.log_path.read_text(encoding="utf-8"))

    def test_timeout_is_reported_and_recorded(self):
        # The deadline is the point of this test, so it is short; nothing else
        # here depends on how fast the interpreter starts.
        check = self.python_check("times-out", "import time; time.sleep(60)")

        started = time.monotonic()
        (timed_out,) = self.runner.run_visible((check,), 0.2)

        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(timed_out.status, "timed_out")
        self.assertTrue(timed_out.timed_out)
        (record,) = self.records()
        self.assertTrue(record["timed_out"])
        self.assertEqual(record["status"], "timed_out")

    def test_known_good_passes_and_buggy_revision_fails(self):
        program = self.worktree / "program.py"
        check = CheckDefinition(
            "acceptance", "acceptance", (sys.executable, "program.py")
        )
        program.write_text("raise SystemExit(1)\n", encoding="utf-8")
        buggy = self.runner.run_visible((check,), PATIENCE)[0]
        program.write_text("raise SystemExit(0)\n", encoding="utf-8")
        known_good = self.runner.run_visible((check,), PATIENCE)[0]

        self.assertEqual(buggy.status, "failed")
        self.assertEqual(known_good.status, "passed")

    def test_a_command_that_cannot_start_is_an_error_result(self):
        check = CheckDefinition("missing", "unit", ("/nonexistent/check-binary",))
        (result,) = self.runner.run_visible((check,), PATIENCE)
        self.assertEqual((result.status, result.exit_code), ("error", None))
        self.assertEqual(self.records()[0]["status"], "error")

    def test_rejects_log_inside_candidate_worktree(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            TestRunner(self.worktree, self.worktree / "execution.jsonl")

    def test_rejects_invalid_timeouts_before_running_anything(self):
        marker = self.root / "ran"
        check = self.python_check(
            "marker", "open(__import__('sys').argv[1], 'w')", marker
        )
        for timeout in (0, -1, float("nan"), float("inf"), True, "1", None):
            with (
                self.subTest(timeout=timeout),
                self.assertRaises(ValueError),
            ):
                self.runner.run_visible((check,), timeout)
        self.assertFalse(marker.exists())
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), "")

    def test_unavailable_hidden_reference_does_not_echo_the_reference(self):
        registry = HiddenCheckRegistry({})
        secret_reference = "private:do-not-echo"
        with self.assertRaisesRegex(KeyError, "unavailable") as error:
            self.runner.run_hidden((secret_reference,), registry, 1)
        self.assertNotIn(secret_reference, str(error.exception))

    def test_unavailable_hidden_reference_leaves_no_exception_chain(self):
        registry = HiddenCheckRegistry({})
        secret_reference = "private:do-not-echo"
        with self.assertRaises(KeyError) as error:
            self.runner.run_hidden((secret_reference,), registry, 1)
        caught = error.exception
        self.assertIsNone(caught.__cause__)
        self.assertIsNone(caught.__context__)
        rendered = "".join(traceback.format_exception(caught))
        self.assertNotIn(secret_reference, rendered)
        self.assertNotIn(secret_reference, repr(caught.args))

    # Output bounds ----------------------------------------------------------

    def emit(self, size):
        """A check that writes ``size`` known bytes to stdout and to stderr."""
        script = (
            "import sys; n = int(sys.argv[1]); "
            "data = (bytes(range(256)) * (n // 256 + 1))[:n]; "
            "sys.stdout.buffer.write(data); sys.stderr.buffer.write(data)"
        )
        expected = (bytes(range(256)) * (size // 256 + 1))[:size]
        return self.python_check(f"emit-{size}", script, size), expected

    def test_capture_limit_boundary_is_exact_and_flags_truncation(self):
        limit = test_runner._MAX_CAPTURE_BYTES
        self.assertEqual(limit, 65536)
        for size, truncated in (
            (0, False),
            (limit - 1, False),
            (limit, False),
            (limit + 1, True),
            (3 * limit + 5, True),
        ):
            with self.subTest(size=size):
                check, expected = self.emit(size)
                (result,) = self.runner.run_visible((check,), PATIENCE)
                kept = expected[:limit]
                self.assertEqual(result.status, "passed")
                for stream in ("stdout", "stderr"):
                    self.assertEqual(getattr(result, stream), kept)
                    self.assertEqual(getattr(result, f"{stream}_bytes"), size)
                    self.assertIs(getattr(result, f"{stream}_truncated"), truncated)
                    self.assertEqual(
                        self.records()[-1][stream],
                        {
                            "captured_bytes": size,
                            "sha256": hashlib.sha256(kept).hexdigest(),
                            "truncated": truncated,
                        },
                    )

    def test_output_is_bounded_in_memory_while_the_check_runs(self):
        size = 64 * 1024 * 1024
        check = self.python_check(
            "large-output",
            "import sys; sys.stdout.buffer.write(b'x' * int(sys.argv[1]))",
            size,
        )
        tracemalloc.start()
        try:
            (result,) = self.runner.run_visible((check,), PATIENCE)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.stdout_bytes, size)
        self.assertEqual(len(result.stdout), test_runner._MAX_CAPTURE_BYTES)
        self.assertTrue(result.stdout_truncated)
        # Buffering the whole stream would need at least ``size`` bytes.
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_endless_output_is_drained_and_bounded_until_the_timeout(self):
        check = self.python_check(
            "noisy",
            "import sys\n"
            "while True:\n"
            "    sys.stdout.buffer.write(b'x' * 65536)\n"
            "    sys.stdout.buffer.flush()\n",
        )
        (result,) = self.runner.run_visible((check,), 1.0)
        self.assertEqual(result.status, "timed_out")
        self.assertEqual(len(result.stdout), test_runner._MAX_CAPTURE_BYTES)
        self.assertGreater(result.stdout_bytes, test_runner._MAX_CAPTURE_BYTES)
        self.assertTrue(result.stdout_truncated)

    # Process containment ----------------------------------------------------

    def test_a_term_handler_that_writes_a_lot_can_finish_within_the_grace_period(self):
        # The handler writes 1 MiB (far more than a pipe holds) before recording
        # that it finished, so it only finishes if its output is being drained.
        self.runner.term_grace_seconds = 20
        done = self.root / "noisy-done"
        script = (
            "import os, signal, sys, time\n"
            "def cleanup(signum, frame):\n"
            "    sys.stdout.buffer.write(b'x' * (1 << 20))\n"
            "    sys.stdout.buffer.flush()\n"
            "    open(sys.argv[1], 'w').write('cleaned')\n"
            "    os._exit(0)\n"
            "signal.signal(signal.SIGTERM, cleanup)\n"
            "time.sleep(60)\n"
        )
        check = self.python_check("noisy-handler", script, done)
        (result,) = self.runner.run_visible((check,), 5)

        self.assertEqual(result.status, "timed_out")
        self.assertEqual(done.read_text(), "cleaned")
        self.assertGreaterEqual(result.stdout_bytes, 1 << 20)

    def test_the_child_is_stopped_when_the_capture_cannot_be_set_up(self):
        # After the launch, building the output capture can still fail (for
        # example EMFILE under descriptor exhaustion). The check must not be left
        # running with nobody to supervise it.
        pid_file = self.pid_file()
        script = (
            "import os, sys, time\n"
            "open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
            "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
            "time.sleep(60)\n"
        )

        def no_descriptors(process, limit):
            raise OSError(errno.EMFILE, "Too many open files")

        check = self.python_check("no-capture", script, pid_file)
        with mock.patch.object(test_runner, "_OutputCapture", no_descriptors):
            (result,) = self.runner.run_visible((check,), PATIENCE)

        self.assertEqual(result.status, "error")
        # A live child publishes its pid within moments; a child that was stopped
        # in time never does. Either way it must not be left running.
        wait_until(pid_file.exists, 3)
        if pid_file.exists():
            child = self.read_pid(pid_file)
            self.assertTrue(wait_until(lambda: not is_running(child)))

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

    def fail_after(self, action, seen, target):
        """Stand in for ``_Leader`` / ``_OutputCapture``: run ``action`` on the
        just-launched child, then fail as descriptor exhaustion would."""

        def fail(process, *_):
            seen.append(process.pid)
            action(process)
            raise OSError(errno.EMFILE, "Too many open files")

        return mock.patch.object(test_runner, target, fail)

    def test_a_child_reaped_elsewhere_is_not_signalled_when_the_setup_fails(self):
        # The child was collected by somebody else (SIGCHLD ignored, a concurrent
        # reaper) before the setup failed, so its pid and process group id may
        # already belong to an unrelated process: nothing may be sent to them.
        for target in ("_Leader", "_OutputCapture"):
            with self.subTest(fails=target):
                seen = []
                check = self.python_check("short", "pass")

                def reap(process):
                    os.waitpid(process.pid, 0)  # waits for the exit, then collects it

                with (
                    self.spy_on_signals(passthrough=False) as calls,
                    self.fail_after(reap, seen, target),
                ):
                    (result,) = self.runner.run_visible((check,), PATIENCE)

                self.assertEqual(result.status, "error")
                self.assertEqual(len(seen), 1)
                self.assertEqual(calls, [], "a reaped child's number was signalled")

    def test_a_live_child_and_its_group_are_killed_when_the_setup_fails(self):
        script = (
            "import os, sys, time\n"
            "if os.fork() == 0:\n"
            "    open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
            "    os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
            "    time.sleep(60)\n"
            "    os._exit(0)\n"
            "time.sleep(60)\n"
        )
        for target in ("_Leader", "_OutputCapture"):
            with self.subTest(fails=target):
                pid_file = self.pid_file(f"member-{target}.pid")
                seen = []
                check = self.python_check("live", script, pid_file)

                def publish(process, pid_file=pid_file):
                    # The member is running in the check's group by now.
                    self.assertTrue(wait_until(pid_file.exists))

                with (
                    self.spy_on_signals(passthrough=True) as calls,
                    self.fail_after(publish, seen, target),
                ):
                    (result,) = self.runner.run_visible((check,), PATIENCE)

                self.assertEqual(result.status, "error")
                (leader,) = seen
                member = self.read_pid(pid_file)
                for pid in (leader, member):
                    self.assertTrue(wait_until(lambda pid=pid: not is_running(pid)))
                # Only the child's own group was signalled, and it was killed.
                self.assertIn(("killpg", leader, int(signal.SIGKILL)), calls)
                self.assertEqual({pid for _, pid, _ in calls}, {leader}, calls)

    def test_a_member_forked_after_the_leader_was_built_dies_when_the_setup_fails(
        self,
    ):
        # The leader (built while the check was still starting) recorded no member.
        # The check then forks a same-group child and exits, and something else
        # reaps it before the capture fails: only a look taken at the failure can
        # still find the child, and it must not be left running.
        pid_file = self.pid_file()
        go = self.root / "go"

        def fork_reap_fail(process, limit):
            go.write_text("")
            self.assertTrue(wait_until(pid_file.exists))
            os.waitpid(process.pid, 0)  # a concurrent reaper collects the leader
            raise OSError(errno.EMFILE, "Too many open files")

        check = self.python_check("late-fork", FORK_WHEN_TOLD_THEN_EXIT, pid_file, go)
        with mock.patch.object(test_runner, "_OutputCapture", fork_reap_fail):
            (result,) = self.runner.run_visible((check,), PATIENCE)

        self.assertEqual(result.status, "error")
        child = self.read_pid(pid_file)
        self.assertTrue(
            wait_until(lambda: not is_running(child)),
            "a group member forked before the setup failed was left running",
        )

    def test_a_late_member_is_found_while_the_leader_is_an_unreaped_zombie(self):
        # No reaper involved: the leader is a zombie (it still reserves the group
        # id) when the setup fails, and the member it forked is still recorded.
        pid_file = self.pid_file()
        go = self.root / "go"

        def fork_and_fail(process, limit):
            go.write_text("")
            self.assertTrue(wait_until(pid_file.exists))
            self.assertTrue(wait_until(lambda: not is_running(process.pid)))
            raise OSError(errno.EMFILE, "Too many open files")

        check = self.python_check("late-fork", FORK_WHEN_TOLD_THEN_EXIT, pid_file, go)
        with mock.patch.object(test_runner, "_OutputCapture", fork_and_fail):
            (result,) = self.runner.run_visible((check,), PATIENCE)

        self.assertEqual(result.status, "error")
        child = self.read_pid(pid_file)
        self.assertTrue(wait_until(lambda: not is_running(child)))

    def test_a_last_look_records_a_member_forked_since_the_leader_was_built(self):
        pid_file = self.pid_file()
        go = self.root / "go"
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                FORK_WHEN_TOLD_THEN_EXIT,
                str(pid_file),
                str(go),
                "60",
            ],
            start_new_session=True,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        leader = test_runner._Leader(process)
        leader.refresh_seconds = 1e9  # only an unthrottled look can record anything
        go.write_text("")
        child = self.read_pid(pid_file)

        self.assertNotIn(child, leader.members)
        leader.look()

        self.assertEqual(leader.members[child], test_runner._start_time(child))
        self.assertFalse(leader.released)

    def test_the_last_look_at_a_vanished_leader_ignores_a_reused_number(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        leader = test_runner._Leader(process)
        own = dict(leader.members)
        self.assertEqual(set(own), {process.pid})
        # The leader's number is held by another process, which leads a group of
        # its own with a member: neither belongs to us.
        stranger = {
            process.pid: ("S", 1, process.pid, leader.start + 7),
            process.pid + 1: ("S", process.pid, process.pid, leader.start + 8),
        }
        with (
            mock.patch.object(os, "waitid", side_effect=ChildProcessError),
            mock.patch.object(test_runner, "_process_table", return_value=stranger),
        ):
            self.assertTrue(leader.has_exited())
        self.assertEqual(leader.members, own)
        self.assertTrue(leader.released and leader.status_lost)

    def test_a_listing_taken_while_the_leader_was_reaped_is_not_adopted(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        leader = test_runner._Leader(process)
        own = dict(leader.members)
        listings = []

        def reaped_during_the_listing():
            if not listings:
                os.kill(process.pid, signal.SIGKILL)
                os.waitpid(process.pid, 0)  # a concurrent reaper
                # By now the id could be anyone's: this looks like a member, and
                # like a child of the leader.
                listings.append("racy")
                return {
                    process.pid + 1: ("S", 1, process.pid, 5),
                    process.pid + 2: ("S", process.pid, process.pid + 2, 6),
                }
            # The snapshot taken once the loss was noticed: the reaped leader's
            # number is held by a stranger, so the group is not ours.
            listings.append("fresh")
            return {process.pid: ("S", 1, process.pid, leader.start + 7)}

        with mock.patch.object(
            test_runner, "_process_table", reaped_during_the_listing
        ):
            leader.refresh(force=True)
        self.assertEqual(listings, ["racy", "fresh"])
        self.assertEqual(leader.members, own)
        self.assertEqual(leader.descendants, {})
        self.assertTrue(leader.released and leader.status_lost)

    def leader_with_a_late_member(self):
        """A leader (still unreaped, as a zombie) that has not recorded a member
        its check forked afterwards."""
        pid_file = self.pid_file()
        go = self.root / "go"
        process = subprocess.Popen(
            [sys.executable, "-c", FORK_WHEN_TOLD_THEN_EXIT, str(pid_file), str(go)],
            start_new_session=True,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        leader = test_runner._Leader(process)
        leader.refresh_seconds = 1e9  # nothing is recorded unless it is forced
        go.write_text("")
        child = self.read_pid(pid_file)
        # The zombie still reserves the group id.
        self.assertTrue(wait_until(lambda: not is_running(process.pid)))
        self.assertNotIn(child, leader.members)
        return process, leader, child

    def reaped_after_the_first_listing(self, process, reused_number_after=None):
        """A ``_process_table`` whose first listing is followed at once by a
        concurrent reaper collecting ``process``: the leader is lost after the table
        was read and before the ownership check that trusts it.  Later listings are
        real, or show the leader's number held by a stranger."""
        real_table = test_runner._process_table
        listings = []

        def table():
            snapshot = real_table()
            listings.append(snapshot)
            if len(listings) == 1:
                os.waitpid(process.pid, 0)
            elif reused_number_after is not None:
                snapshot = {**snapshot, process.pid: reused_number_after}
            return snapshot

        return mock.patch.object(test_runner, "_process_table", table)

    def test_a_leader_reaped_after_the_listing_still_yields_a_group_snapshot(self):
        process, leader, child = self.leader_with_a_late_member()

        with self.reaped_after_the_first_listing(process):
            leader.refresh(force=True)

        # The listing itself is not trusted, but the loss is noticed and the
        # snapshot of the vanished leader's group is taken at once.
        self.assertEqual(leader.members.get(child), test_runner._start_time(child))
        self.assertTrue(leader.released and leader.status_lost)

    def test_the_snapshot_after_such_a_loss_ignores_a_reused_number(self):
        process, leader, child = self.leader_with_a_late_member()
        own = dict(leader.members)
        stranger = ("S", 1, process.pid, leader.start + 7)

        with self.reaped_after_the_first_listing(process, stranger):
            leader.refresh(force=True)

        self.assertEqual(leader.members, own)
        self.assertNotIn(child, leader.members)
        self.assertTrue(leader.released and leader.status_lost)

    def test_a_leader_found_reaped_at_a_signal_still_gets_its_group_snapshot(self):
        process, leader, child = self.leader_with_a_late_member()
        os.waitpid(process.pid, 0)  # a concurrent reaper; nobody saw it happen
        self.assertFalse(leader.released)
        started = test_runner._start_time(child)

        leader.signal_group(signal.SIGKILL)

        self.assertTrue(
            wait_until(lambda: not is_running(child)),
            "a group member forked before the loss was noticed survived",
        )
        self.assertEqual(leader.members.get(child), started)
        self.assertTrue(leader.released and leader.status_lost)

    def test_a_member_is_not_lost_when_the_leader_is_reaped_during_the_last_look(
        self,
    ):
        # The setup failure path: the last look at the group reads the table, and
        # a concurrent reaper collects the leader before the look can check that
        # it still owns the id.  The child forked since must still be stopped.
        process, leader, child = self.leader_with_a_late_member()

        with self.reaped_after_the_first_listing(process):
            test_runner._stop_unsupervised(process, leader.start, leader)

        self.assertTrue(
            wait_until(lambda: not is_running(child)),
            "a group member forked before the setup failed was left running",
        )

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
                process.wait()
            for stream in (process.stdout, process.stderr):
                stream.close()

        self.addCleanup(clean_up)
        self.assertTrue(wait_until(lambda: test_runner._start_time(process.pid)))
        return process

    def test_stopping_an_unsupervised_child_needs_its_recorded_identity(self):
        process = self.spawn_unsupervised()
        started = test_runner._start_time(process.pid)

        # Another process now has this pid (same number, other start time).
        with self.spy_on_signals(passthrough=True) as calls:
            test_runner._stop_unsupervised(process, started + 1)
        self.assertEqual(calls, [])
        time.sleep(0.3)
        self.assertTrue(
            is_running(process.pid), "a process with another identity was killed"
        )

    def test_a_reaped_child_is_not_signalled_without_a_recorded_start_time(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.waitpid(process.pid, 0)  # somebody else collected it

        with self.spy_on_signals(passthrough=False) as calls:
            test_runner._stop_unsupervised(process, None)

        self.assertEqual(calls, [])
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def reused_pid_of_another_child(self):
        """A reaped ``Popen`` whose pid now belongs to another direct child.

        Models a reaper that collected the launched child and a later launch that
        got the same pid, which ``waitid`` cannot tell apart from the original.
        The stranger leads its own process group, as a new child would.
        """
        stranger = self.spawn_unsupervised()
        original = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.waitpid(original.pid, 0)  # somebody else collected it
        original.pid = stranger.pid  # ...and its number went to the stranger
        return original, stranger

    def test_a_reused_pid_is_not_signalled_when_no_start_time_was_recorded(self):
        # The stranger is our own child, so ``waitid`` does not fail for its pid: it
        # is no proof that the number is still the child that was launched.
        # Once with ``/proc`` (the read at launch failed) and once without it, where
        # a start time can be read for nobody and "None == None" must not pass.
        for label, no_proc in (("start unreadable", False), ("no /proc", True)):
            with self.subTest(case=label):
                process, stranger = self.reused_pid_of_another_child()
                no_start_times = (
                    mock.patch.object(test_runner, "_start_time", return_value=None)
                    if no_proc
                    else contextlib.nullcontext()
                )
                with no_start_times, self.spy_on_signals(passthrough=True) as calls:
                    test_runner._stop_unsupervised(process, None)

                self.assertEqual(calls, [])
                time.sleep(0.3)
                self.assertIsNone(stranger.poll(), "another child was killed")
                self.assertEqual(process.returncode, 0)  # not waited for
                self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_a_leader_that_is_not_the_launched_child_is_not_signalled(self):
        # The leader was built after the number changed hands, so it recorded the
        # stranger's start time.  The launch-time identity is what counts.
        process, stranger = self.reused_pid_of_another_child()
        leader = test_runner._Leader(process)
        self.assertEqual(leader.start, test_runner._start_time(stranger.pid))

        for label, recorded in (
            ("none recorded", None),
            ("another start time", leader.start - 1),
        ):
            with self.subTest(launch=label):
                with self.spy_on_signals(passthrough=True) as calls:
                    test_runner._stop_unsupervised(process, recorded, leader)
                self.assertEqual(calls, [])
                self.assertIsNone(stranger.poll(), "another child was killed")

    def test_no_recorded_start_time_means_the_child_is_not_provably_ours(self):
        process = self.spawn_unsupervised()
        started = test_runner._start_time(process.pid)
        # Both with ``waitid(WNOWAIT)`` and where only the start time can tell.
        for label, context in (
            ("waitid", contextlib.nullcontext()),
            ("no waitid", mock.patch.object(os, "waitid", side_effect=AttributeError)),
        ):
            with self.subTest(path=label), context:
                self.assertFalse(test_runner._is_unreaped_child(process.pid, None))
                self.assertFalse(
                    test_runner._is_unreaped_child(process.pid, started + 1)
                )
                self.assertTrue(test_runner._is_unreaped_child(process.pid, started))
                # No ``/proc`` at all: no start time can be read now either.
                with mock.patch.object(test_runner, "_start_time", return_value=None):
                    self.assertFalse(test_runner._is_unreaped_child(process.pid, None))

    def test_a_leader_without_a_recorded_start_time_never_signals_its_group(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        with mock.patch.object(test_runner, "_start_time", return_value=None):
            leader = test_runner._Leader(process)
            with self.spy_on_signals(passthrough=True) as calls:
                leader.signal_group(signal.SIGKILL)

        self.assertEqual(calls, [])
        time.sleep(0.3)
        self.assertIsNone(process.poll(), "an unidentified group was signalled")
        # Nothing was learnt about the leader: it is neither released nor lost.
        self.assertFalse(leader.released or leader.status_lost)

    def test_without_any_start_time_a_run_still_reports_its_real_status(self):
        # No /proc, say.  Nothing can be signalled, but a check that finishes must
        # still be reported as what it was, not as a lost status.
        with mock.patch.object(test_runner, "_start_time", return_value=None):
            passed, failed = (
                self.runner.run_visible(
                    (self.python_check(name, f"raise SystemExit({code})"),), PATIENCE
                )[0]
                for name, code in (("ok", 0), ("bad", 3))
            )
        self.assertEqual((passed.status, passed.exit_code), ("passed", 0))
        self.assertEqual((failed.status, failed.exit_code), ("failed", 3))

    def test_a_timed_out_check_is_left_alone_when_it_has_no_identity(self):
        self.runner.term_grace_seconds = 0.3
        self.runner.drain_seconds = 0.3
        pid_file = self.pid_file()
        script = (
            "import os, sys, time\n"
            "open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
            "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
            "time.sleep(60)\n"
        )
        check = self.python_check("no-identity", script, pid_file)
        with (
            mock.patch.object(test_runner, "_start_time", return_value=None),
            self.spy_on_signals(passthrough=True) as calls,
            # The check is deliberately left running, so its ``Popen`` is dropped
            # while the child is alive.
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("ignore", ResourceWarning)
            (result,) = self.runner.run_visible((check,), 1)

        self.assertEqual(result.status, "timed_out")
        self.assertEqual(
            [call for call in calls if call[2] != 0],
            [],
            "a group without a recorded identity was signalled",
        )
        self.assertTrue(is_running(self.read_pid(pid_file)))  # cleanup kills it

    def test_an_unsupervised_child_with_its_recorded_identity_is_killed_and_reaped(
        self,
    ):
        process = self.spawn_unsupervised()
        started = test_runner._start_time(process.pid)

        with self.spy_on_signals(passthrough=True) as calls:
            test_runner._stop_unsupervised(process, started)
        self.assertIn(("killpg", process.pid, int(signal.SIGKILL)), calls)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_timeout_kills_a_group_member_that_ignores_sigterm(self):
        self.runner.term_grace_seconds = 0.3
        pid_file = self.pid_file()
        check = self.python_check(
            "orphan", ORPHANED_TERM_IGNORING_CHILD, pid_file, "wait"
        )

        started = time.monotonic()
        (result,) = self.runner.run_visible((check,), 2.0)

        self.assertEqual(result.status, "timed_out")
        # The check and its orphaned grandchild sleep for 60 seconds.
        self.assertLess(time.monotonic() - started, 30)
        grandchild = self.read_pid(pid_file)
        self.assertTrue(wait_until(lambda: not is_running(grandchild)))

    def test_timeout_kills_a_child_in_its_own_session(self):
        pid_file = self.pid_file()
        check = self.python_check("escaped", ESCAPED_CHILD, pid_file)

        started = time.monotonic()
        (result,) = self.runner.run_visible((check,), 2.0)

        self.assertEqual(result.status, "timed_out")
        self.assertLess(time.monotonic() - started, 30)
        child = self.read_pid(pid_file)
        self.assertTrue(wait_until(lambda: not is_running(child)))

    def test_a_leader_that_ignores_sigterm_is_killed_after_the_grace_period(self):
        self.runner.term_grace_seconds = 0.3
        ready = self.root / "handler-installed"
        check = self.python_check("stubborn", TERM_IGNORING_LEADER, ready)

        (result,) = self.runner.run_visible((check,), 2.0)

        self.assertTrue(ready.exists(), "the check never installed its handler")
        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.exit_code, -signal.SIGKILL)

    def test_program_must_be_named_but_arguments_may_be_empty(self):
        empty_argument = CheckDefinition(
            "empty-arg", "unit", (sys.executable, "-c", "")
        )
        (result,) = self.runner.run_visible((empty_argument,), PATIENCE)
        self.assertEqual((result.status, result.exit_code), ("passed", 0))
        registry = HiddenCheckRegistry({"private:empty": empty_argument})
        (hidden,) = self.runner.run_hidden(("private:empty",), registry, PATIENCE)
        self.assertEqual(hidden.status, "passed")

        for command in ((), ("",), ("", "-c", "pass"), (sys.executable, 1), ("a\0b",)):
            with (
                self.subTest(command=command),
                self.assertRaises(ValueError),
            ):
                self.runner.run_visible((CheckDefinition("bad", "unit", command),), 1)

    def test_descendants_may_finish_their_term_handlers_within_the_grace_period(self):
        self.runner.term_grace_seconds = 10
        for mode in ("orphan", "session", "daemon"):
            with self.subTest(mode=mode):
                pid_file = self.pid_file(f"{mode}.pid")
                done = self.root / f"{mode}.done"
                check = self.python_check(
                    f"slow-{mode}", SLOW_TERM_CLEANUP, mode, pid_file, done
                )

                (result,) = self.runner.run_visible((check,), 2.0)

                self.assertEqual(result.status, "timed_out")
                self.assertTrue(done.exists(), "the TERM handler was cut short")
                self.assertEqual(done.read_text(encoding="utf-8"), "cleaned")

    def test_a_process_started_by_a_term_handler_gets_its_own_term_and_grace_period(
        self,
    ):
        # The set of processes to signal is not fixed when the first SIGTERM goes
        # out: a handler may start one.  Killing it outright would skip its own
        # cleanup, so it is found during the grace period and sent SIGTERM too.
        self.runner.term_grace_seconds = 10
        calls = []
        real_signal_identified = test_runner._signal_identified

        def spy(pid, started, number):
            calls.append((pid, int(number)))
            real_signal_identified(pid, started, number)

        for mode in ("exit", "wait", "scrubbed-wait"):
            with self.subTest(mode=mode):
                pid_file = self.pid_file(f"helper-{mode}.pid")
                done = self.root / f"helper-{mode}.done"
                ready = self.root / f"helper-{mode}.ready"
                check = self.python_check(
                    f"starts-helper-{mode}",
                    TERM_HANDLER_STARTS_HELPER,
                    mode,
                    pid_file,
                    done,
                    ready,
                )

                with mock.patch.object(test_runner, "_signal_identified", spy):
                    (result,) = self.runner.run_visible((check,), 2.0)

                self.assertTrue(ready.exists(), "the check never installed its handler")
                self.assertEqual(result.status, "timed_out")
                helper = self.read_pid(pid_file)
                self.assertTrue(done.exists(), "the helper's TERM handler never ran")
                self.assertEqual(done.read_text(encoding="utf-8"), "cleaned")
                self.assertTrue(
                    wait_until(lambda helper=helper: not is_running(helper))
                )
                # One SIGTERM, not one per look at the process table.
                self.assertEqual(
                    [n for pid, n in calls if pid == helper and n == signal.SIGTERM],
                    [signal.SIGTERM],
                )

    def test_a_process_started_late_in_the_grace_period_gets_a_full_grace_period(
        self,
    ):
        # The grace period is not one fixed deadline for everything: a process that a
        # handler starts near its end (here after 1.8 s of 2 s) is sent SIGTERM when it
        # is found and gets a whole grace period from then on, so its 0.5 s of cleanup
        # is not cut short by the SIGKILL that the original deadline would have sent.
        self.runner.term_grace_seconds = 2.0
        for mode in ("exit", "wait"):
            with self.subTest(mode=mode):
                pid_file = self.pid_file(f"late-{mode}.pid")
                done = self.root / f"late-{mode}.done"
                ready = self.root / f"late-{mode}.ready"
                check = self.python_check(
                    f"starts-late-helper-{mode}",
                    TERM_HANDLER_STARTS_HELPER,
                    mode,
                    pid_file,
                    done,
                    ready,
                    1.8,
                    0.5,
                )

                (result,) = self.runner.run_visible((check,), 2.0)

                self.assertTrue(ready.exists(), "the check never installed its handler")
                self.assertEqual(result.status, "timed_out")
                self.assertTrue(
                    done.exists(),
                    "the late helper was killed in the middle of its cleanup",
                )
                self.assertEqual(done.read_text(encoding="utf-8"), "cleaned")
                helper = self.read_pid(pid_file)
                self.assertTrue(
                    wait_until(lambda helper=helper: not is_running(helper))
                )

    def test_the_wait_for_newcomers_ends_however_many_a_handler_keeps_starting(self):
        # Every newcomer extends the wait, but only up to ``max_grace_periods`` grace
        # periods after the first SIGTERM.  The handler here would go on starting
        # processes that ignore SIGTERM for 15 s; the runner stops after about
        # 2 s (timeout) + 2 x 1 s (grace periods) and kills all of them.
        self.runner.term_grace_seconds = 1.0
        pids = self.root / "sleepers.pids"
        ready = self.root / "sleepers.ready"
        check = self.python_check(
            "keeps-starting", TERM_HANDLER_KEEPS_STARTING_PROCESSES, pids, ready
        )

        started = time.monotonic()
        try:
            (result,) = self.runner.run_visible((check,), 2.0)
            elapsed = time.monotonic() - started
            sleepers = [int(line) for line in pids.read_text().split()]
        finally:
            if pids.exists():
                for line in pids.read_text().split():
                    with contextlib.suppress(OSError):
                        os.kill(int(line), signal.SIGKILL)

        self.assertTrue(ready.exists(), "the check never installed its handler")
        self.assertEqual(result.status, "timed_out")
        self.assertGreaterEqual(len(sleepers), 3)
        self.assertLess(elapsed, 12)
        for sleeper in sleepers:
            self.assertTrue(wait_until(lambda sleeper=sleeper: not is_running(sleeper)))

    def test_a_newcomer_is_signalled_once_and_only_while_it_is_the_recorded_process(
        self,
    ):
        # Direct check of the identity rule: a process found during the grace period
        # is sent SIGTERM once, if it is alive and still the process that was seen; a
        # zombie, a process that was replaced under the same pid, and one that has
        # gone are left alone.
        calls = []
        table = {
            10: ("S", 1, 1, 100),  # alive: signalled
            11: ("Z", 1, 1, 101),  # a zombie: nothing to stop
            12: ("S", 1, 1, 999),  # the pid was reused (recorded start time 102)
            14: ("S", 1, 1, 104),  # already had its signal
        }
        found = {10: 100, 11: 101, 12: 102, 13: 103, 14: 104}
        leader = SimpleNamespace(spawned=lambda table: found)
        tracked = {14: 104}

        with mock.patch.object(
            test_runner,
            "_signal_identified",
            lambda pid, started, number: calls.append((pid, started, int(number))),
        ):
            test_runner._term_newcomers(leader, tracked, table)
            test_runner._term_newcomers(leader, tracked, table)  # the next look

        self.assertEqual(calls, [(10, 100, int(signal.SIGTERM))])
        self.assertEqual(tracked, {10: 100, 14: 104})

    def test_a_descendant_that_ignores_sigterm_is_killed_only_after_the_grace_period(
        self,
    ):
        self.runner.term_grace_seconds = 1.0
        pid_file = self.pid_file()
        check = self.python_check(
            "stubborn-child", ORPHANED_TERM_IGNORING_CHILD, pid_file, "wait"
        )

        started = time.monotonic()
        (result,) = self.runner.run_visible((check,), 2.0)

        # The check dies on TERM at once; its descendant ignores TERM, so the
        # whole grace period has to run out before SIGKILL.
        self.assertGreaterEqual(time.monotonic() - started, 2.0 + 1.0)
        self.assertEqual(result.status, "timed_out")
        self.assertTrue(wait_until(lambda: not is_running(self.read_pid(pid_file))))

    def test_a_process_left_behind_by_a_finished_check_is_killed(self):
        self.runner.drain_seconds = 0.3
        pid_file = self.pid_file()
        check = self.python_check(
            "background", ORPHANED_TERM_IGNORING_CHILD, pid_file, "exit"
        )

        (result,) = self.runner.run_visible((check,), PATIENCE)

        self.assertEqual((result.status, result.exit_code), ("passed", 0))
        grandchild = self.read_pid(pid_file)
        self.assertTrue(wait_until(lambda: not is_running(grandchild)))

    # Processes that left the check's group ----------------------------------------

    def test_a_process_that_left_the_group_is_killed_when_the_check_exits(self):
        # The check starts it in a session of its own (or double-forks a daemon) and
        # returns at once, so it is re-parented before the runner could have seen
        # it below the check.  Only what it inherited (its environment) shows it.
        self.runner.drain_seconds = 0.3
        for mode in ("session", "daemon"):
            with self.subTest(mode=mode):
                pid_file = self.pid_file(f"{mode}.pid")
                check = self.python_check(
                    f"escapes-{mode}", ESCAPES_THEN_EXITS, mode, pid_file
                )

                (result,) = self.runner.run_visible((check,), PATIENCE)

                self.assertEqual((result.status, result.exit_code), ("passed", 0))
                child = self.read_pid(pid_file)
                self.assertTrue(
                    wait_until(lambda child=child: not is_running(child)),
                    "a process outside the check's group outlived a passing check",
                )

    @unittest.skipUnless(shutil.which("setsid"), "needs the setsid program")
    def test_a_child_started_with_the_setsid_program_is_killed_when_the_check_exits_0(
        self,
    ):
        # A shell check that starts ``setsid`` in the background, waits until that
        # child is running, and exits 0: the child is in a session of its own and is
        # re-parented, and must not outlive the passing check.
        self.runner.drain_seconds = 0.3
        pid_file = self.pid_file()
        script = (
            'setsid sh -c \'echo $$ > "$0.tmp"; mv "$0.tmp" "$0"; exec sleep 60\''
            ' "$1" >/dev/null 2>&1 </dev/null &\n'
            'while [ ! -e "$1" ]; do sleep 0.01; done\n'
            "exit 0\n"
        )
        check = CheckDefinition(
            "setsid-child", "unit", ("sh", "-c", script, "check", str(pid_file))
        )

        (result,) = self.runner.run_visible((check,), PATIENCE)

        self.assertEqual((result.status, result.exit_code), ("passed", 0))
        child = self.read_pid(pid_file)
        self.assertTrue(
            wait_until(lambda: not is_running(child)),
            "a setsid child outlived a passing check",
        )

    def test_timeout_kills_a_daemonized_process(self):
        self.runner.term_grace_seconds = 0.3
        self.runner.drain_seconds = 0.3
        pid_file = self.pid_file()
        check = self.python_check(
            "daemon", ESCAPES_THEN_EXITS, "daemon", pid_file, "block"
        )

        (result,) = self.runner.run_visible((check,), 2.0)

        self.assertEqual(result.status, "timed_out")
        daemon = self.read_pid(pid_file)
        self.assertTrue(wait_until(lambda: not is_running(daemon)))

    def test_an_escaped_process_is_killed_when_the_setup_fails(self):
        pid_file = self.pid_file()

        def fail_once_it_escaped(process, limit):
            self.assertTrue(wait_until(pid_file.exists))
            raise OSError(errno.EMFILE, "Too many open files")

        check = self.python_check("escapes", ESCAPES_THEN_EXITS, "session", pid_file)
        with mock.patch.object(test_runner, "_OutputCapture", fail_once_it_escaped):
            (result,) = self.runner.run_visible((check,), PATIENCE)

        self.assertEqual(result.status, "error")
        child = self.read_pid(pid_file)
        self.assertTrue(wait_until(lambda: not is_running(child)))

    def test_a_process_the_group_signal_reaches_is_not_signalled_a_second_time(self):
        # The leader and its group members get their signals through the group.  A
        # second, individual signal could interrupt a TERM handler that is already
        # running, so only what is outside the group is signalled by itself.
        self.runner.term_grace_seconds = 0.3
        pid_file = self.pid_file()
        check = self.python_check(
            "orphan", ORPHANED_TERM_IGNORING_CHILD, pid_file, "wait"
        )
        calls = []
        real_signal_identified = test_runner._signal_identified

        def spy(pid, started, number):
            calls.append((pid, int(number)))
            real_signal_identified(pid, started, number)

        with mock.patch.object(test_runner, "_signal_identified", spy):
            (result,) = self.runner.run_visible((check,), 2.0)

        self.assertEqual(result.status, "timed_out")
        member = self.read_pid(pid_file)
        self.assertTrue(wait_until(lambda: not is_running(member)))
        self.assertEqual(calls, [])

    def spawn_scrubbed_escapee(self):
        """A check leader that started a marker-less process in its own session."""
        pid_file = self.pid_file()
        go = self.root / "go"
        process = subprocess.Popen(
            [sys.executable, "-c", SCRUBBED_ESCAPED_CHILD, str(pid_file), str(go)],
            start_new_session=True,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process, pid_file, go

    def test_a_descendant_outside_the_group_is_recorded_while_the_leader_runs(self):
        process, pid_file, go = self.spawn_scrubbed_escapee()
        leader = test_runner._Leader(process, "unrelated-token")
        escapee = self.read_pid(pid_file)

        leader.refresh(force=True)

        started = test_runner._start_time(escapee)
        self.assertEqual(leader.descendants.get(escapee), started)
        self.assertNotIn(escapee, leader.members)  # a session of its own
        self.assertEqual(test_runner._marked_processes("unrelated-token"), {})
        # The check exits; the process is re-parented and only the record finds it.
        go.write_text("")
        self.assertTrue(wait_until(leader.has_exited))
        self.assertNotEqual(
            test_runner._parse_stat(Path(f"/proc/{escapee}/stat").read_bytes())[1],
            process.pid,
        )

        leader.kill_spawned()

        self.assertTrue(wait_until(lambda: not is_running(escapee)))

    def test_a_descendant_seen_while_the_check_ran_is_killed_when_it_exits(self):
        # The escapee carries no marker, so only the record made while the check
        # was running can find it.  The check is held back until the runner has
        # recorded it, so nothing depends on timing.
        pid_file = self.pid_file()
        go = self.root / "go"
        real_refresh = test_runner._Leader.refresh

        def refresh_then_release_the_check(leader, *args, **kwargs):
            real_refresh(leader, *args, **kwargs)
            if pid_file.exists() and int(pid_file.read_text()) in getattr(
                leader, "descendants", {}
            ):
                go.write_text("")

        check = self.python_check("scrubbed", SCRUBBED_ESCAPED_CHILD, pid_file, go)
        with (
            mock.patch.object(test_runner._Leader, "refresh_seconds", 0),
            mock.patch.object(
                test_runner._Leader, "refresh", refresh_then_release_the_check
            ),
        ):
            (result,) = self.runner.run_visible((check,), PATIENCE)

        self.assertEqual((result.status, result.exit_code), ("passed", 0))
        escapee = self.read_pid(pid_file)
        self.assertTrue(
            wait_until(lambda: not is_running(escapee)),
            "a descendant recorded while the check ran outlived a passing check",
        )

    def test_descendants_that_have_gone_are_forgotten(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        leader = test_runner._Leader(process)
        pid = process.pid
        own = ("S", 1, pid, leader.start)

        def listing(*others):
            return mock.patch.object(
                test_runner,
                "_process_table",
                return_value={pid: own, **dict(others)},
            )

        with listing(
            (pid + 1, ("S", pid, pid + 1, 7)), (pid + 2, ("S", 1, pid + 2, 8))
        ):
            leader.refresh(force=True)
        # A child, in a session of its own; not a stranger's process.
        self.assertEqual(leader.descendants, {pid + 1: 7})

        with listing((pid + 1, ("S", 1, pid + 1, 7))):  # re-parented: still ours
            leader.refresh(force=True)
        self.assertEqual(leader.descendants, {pid + 1: 7})

        with listing((pid + 1, ("S", 1, pid + 1, 99))):  # the pid was reused
            leader.refresh(force=True)
        self.assertEqual(leader.descendants, {})

        with listing():
            leader.refresh(force=True)
        self.assertEqual(leader.descendants, {})

    def test_processes_are_marked_by_the_exact_token_only(self):
        token = "0123456789abcdef"
        name = test_runner._TOKEN_VARIABLE

        def spawn(environment):
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"], env=environment
            )
            self.addCleanup(process.wait)
            self.addCleanup(process.kill)
            self.assertTrue(wait_until(lambda: test_runner._start_time(process.pid)))
            return process

        marked = spawn({"A": "1", name: token, "B": "2"})
        spawn({name: token + "0"})  # another check whose token starts the same
        spawn({name: token[:-1]})  # ... and one that is a prefix of ours
        spawn({"X" + name: token})  # another variable that ends the same
        spawn({name: "other", "NOTE": f"{name}={token}"})  # inside another value
        spawn({})
        zombie = spawn({name: token})
        zombie.kill()
        self.assertTrue(wait_until(lambda: not is_running(zombie.pid)))

        found = test_runner._marked_processes(token)

        self.assertEqual(found, {marked.pid: test_runner._start_time(marked.pid)})
        self.assertEqual(test_runner._marked_processes(None), {})

    # Process identity ----------------------------------------------------------

    def spawn_bystander(self):
        """An unrelated process standing in for one that inherited a recycled pid."""
        bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        self.assertTrue(wait_until(lambda: test_runner._start_time(bystander.pid)))
        return bystander

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
                started = test_runner._start_time(bystander.pid)
                # The recorded process was replaced: same pid, other start time.
                test_runner._signal_identified(
                    bystander.pid, started + 1, signal.SIGKILL
                )
                time.sleep(0.3)
                self.assertIsNone(bystander.poll(), "an unrelated process was killed")
                # Same identity: the signal is delivered.
                test_runner._signal_identified(bystander.pid, started, signal.SIGKILL)
                self.assertEqual(bystander.wait(timeout=PATIENCE), -signal.SIGKILL)

    def test_a_recycled_pid_is_neither_signalled_nor_waited_for_on_termination(self):
        self.runner.term_grace_seconds = 30
        bystander = self.spawn_bystander()
        stale = {bystander.pid: test_runner._start_time(bystander.pid) - 1}
        check = self.python_check("sleeper", "import time; time.sleep(60)")

        started = time.monotonic()
        with mock.patch.object(test_runner, "_descendant_pids", return_value=stale):
            (result,) = self.runner.run_visible((check,), 0.3)

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
            with self.subTest(check=label):
                seen = []
                real = test_runner._signal_group

                def spy(pgid, number, real=real, seen=seen):
                    try:
                        os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                        reaped = False
                    except ChildProcessError:
                        reaped = True
                    seen.append((number, reaped))
                    real(pgid, number)

                check = self.python_check(label, script)
                with mock.patch.object(test_runner, "_signal_group", spy):
                    self.runner.run_visible((check,), timeout)

                kills = [reaped for number, reaped in seen if number == signal.SIGKILL]
                self.assertTrue(kills)
                self.assertEqual(kills, [False] * len(kills), seen)

    # A leader that something else reaped ----------------------------------------

    def run_with_sigchld_ignored(
        self, mode, pid_file="unused", script=BACKGROUND_THEN_EXIT
    ):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                SIGCHLD_IGNORED_HARNESS,
                str(self.worktree),
                str(self.root / "sigchld-log" / "results.jsonl"),
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

    def test_an_unknowable_exit_status_is_an_error_not_a_pass(self):
        # A failing check (exit 3) whose exit status is lost must never read "passed".
        observed = self.run_with_sigchld_ignored("fails")
        self.assertEqual(observed["status"], "error")
        self.assertIsNone(observed["exit_code"])

    def test_no_group_signal_is_sent_for_a_group_id_that_may_be_reused(self):
        observed = self.run_with_sigchld_ignored("fails")
        # The leader vanished and no member of its group was ever seen: the
        # group id could belong to a stranger by now, so nothing may be sent.
        self.assertEqual(observed["killpg"], [])

    def test_a_recorded_member_of_a_reaped_leaders_group_is_still_killed(self):
        pid_file = self.pid_file()
        observed = self.run_with_sigchld_ignored("background", pid_file)
        self.assertEqual(observed["status"], "error")
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
        leader = test_runner._Leader(leader_process)
        self.assertTrue(wait_until(leader.has_exited))  # an unreaped zombie
        self.assertFalse(leader.released)
        os.waitpid(leader_process.pid, 0)  # a concurrent reaper collects it
        sent = []
        with mock.patch.object(
            test_runner, "_signal_group", lambda *args: sent.append(args)
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
        real_signal_group = test_runner._Leader.signal_group

        def reaper_first(leader, number):
            if leader.process.returncode is None:
                os.waitpid(leader.pgid, 0)
            real_signal_group(leader, number)

        check = self.python_check("quick", "raise SystemExit(4)")
        with (
            mock.patch.object(test_runner._Leader, "signal_group", reaper_first),
            mock.patch.object(
                test_runner, "_signal_group", lambda *args: sent.append(args)
            ),
        ):
            (result,) = self.runner.run_visible((check,), PATIENCE)
        self.assertEqual(sent, [])
        # A status nobody collected is unknown, not "passed" and not "failed".
        self.assertEqual((result.status, result.exit_code), ("error", None))

    @unittest.skipUnless(hasattr(os, "WNOWAIT"), "needs waitid(WNOWAIT)")
    def test_a_member_forked_before_a_zombie_leader_is_seen_is_still_killed(self):
        # The leader forks a same-group child and exits inside the sampling
        # interval, so the child is first seen when the zombie is first observed.
        # A concurrent reaper then collects the leader before the final group kill:
        # only a member recorded at that first observation can still be signalled.
        pid_file = self.pid_file()
        real_signal_group = test_runner._Leader.signal_group

        def reaper_first(leader, number):
            if leader.process.returncode is None:
                os.waitpid(leader.pgid, 0)
            real_signal_group(leader, number)

        def kill_child():
            try:
                os.kill(self.read_pid(pid_file), signal.SIGKILL)
            except ProcessLookupError:
                pass

        self.addCleanup(kill_child)
        check = self.python_check("fork", FORK_AND_EXIT, pid_file)
        with mock.patch.object(test_runner._Leader, "signal_group", reaper_first):
            (result,) = self.runner.run_visible((check,), PATIENCE)
        child = self.read_pid(pid_file)
        self.assertTrue(wait_until(lambda: not is_running(child)), result)

    @unittest.skipUnless(hasattr(os, "WNOWAIT"), "needs waitid(WNOWAIT)")
    def test_reap_decodes_the_exit_status_and_notices_a_reaper_that_was_first(self):
        exited = subprocess.Popen([sys.executable, "-c", "raise SystemExit(5)"])
        killed = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        stolen = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(killed.kill)
        killed.kill()
        for process in (exited, killed, stolen):
            self.assertTrue(wait_until(test_runner._Leader(process).has_exited))
        os.waitpid(stolen.pid, 0)
        results = {}
        for name, process in (
            ("exited", exited),
            ("killed", killed),
            ("stolen", stolen),
        ):
            leader = test_runner._Leader(process)
            leader.reap(PATIENCE)
            results[name] = (
                leader.status_lost,
                None if leader.status_lost else process.returncode,
            )
        self.assertEqual(
            results,
            {
                "exited": (False, 5),
                "killed": (False, -signal.SIGKILL),
                "stolen": (True, None),
            },
        )

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
        self.assertTrue(wait_until(lambda: test_runner._start_time(stranger.pid)))
        leader = test_runner._Leader(SimpleNamespace(pid=stranger.pid, returncode=None))
        with mock.patch.object(os, "waitid", side_effect=ChildProcessError):
            self.assertTrue(leader.has_exited())
        self.assertTrue(leader.released and leader.status_lost)

        # Nothing recorded ever belonged to this group id: never signal it.
        leader.members = {stranger.pid + 100000: 1}
        leader.signal_group(signal.SIGKILL)
        leader.members = {stranger.pid: test_runner._start_time(stranger.pid) + 1}
        leader.signal_group(signal.SIGKILL)  # same pid, different start time
        time.sleep(0.3)
        self.assertIsNone(stranger.poll(), "a stranger's group was signalled")

        # A member recorded earlier (same pid and start time) is still in the group.
        leader.members = {stranger.pid: test_runner._start_time(stranger.pid)}
        leader.signal_group(signal.SIGKILL)
        self.assertEqual(stranger.wait(timeout=PATIENCE), -signal.SIGKILL)

    def test_check_environment_is_an_allowlist_without_credentials(self):
        report = self.root / "environment.json"
        secrets = {
            "SECRET_TOKEN": "hunter2",
            "GITHUB_TOKEN": "ghp_not_for_checks",
            "ANTHROPIC_API_KEY": "sk-not-for-checks",
            "GIT_DIR": str(self.root),
            "GIT_INDEX_FILE": str(self.root / "index"),
        }
        check = self.python_check(
            "environment",
            "import json, os, sys; json.dump(dict(os.environ), open(sys.argv[1], 'w'))",
            report,
        )
        with mock.patch.dict(os.environ, secrets):
            (result,) = self.runner.run_visible((check,), PATIENCE)

        self.assertEqual(result.status, "passed")
        environment = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(set(environment) & set(secrets), set())
        # Python may add LC_CTYPE itself when coercing a C locale.  The marker is
        # the one addition: a random token that lets the runner recognise what a
        # check started, wherever it went.
        marker = test_runner._TOKEN_VARIABLE
        self.assertLessEqual(
            set(environment),
            set(test_runner._CHECK_ENVIRONMENT_ALLOWLIST)
            | {"HOME", "LC_CTYPE", marker},
        )
        self.assertRegex(environment[marker], r"^[0-9a-f]{32}$")
        self.assertEqual(environment["PATH"], os.environ["PATH"])
        home = Path(environment["HOME"])
        self.assertNotEqual(home, Path.home())
        self.assertFalse(home.exists(), "the per-check HOME must be removed")

    # Evaluator-owned log ----------------------------------------------------

    def test_log_directory_and_file_are_private_from_the_moment_they_exist(self):
        previous = os.umask(0)
        try:
            log_path = self.root / "fresh" / "private" / "results.jsonl"
            runner = TestRunner(self.worktree, log_path)
            runner.run_visible((self.python_check("noop", "pass"),), PATIENCE)
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(log_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)

    def test_a_wider_existing_log_is_tightened_before_anything_is_written(self):
        log_path = self.root / "older.jsonl"
        log_path.write_text("earlier\n", encoding="utf-8")
        log_path.chmod(0o644)

        TestRunner(self.worktree, log_path)

        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
        self.assertEqual(log_path.read_text(encoding="utf-8"), "earlier\n")

    def test_a_log_swapped_for_a_symlink_is_neither_followed_nor_chmodded(self):
        victim = self.root / "victim.txt"
        victim.write_text("precious\n", encoding="utf-8")
        victim.chmod(0o644)
        self.log_path.unlink()
        self.log_path.symlink_to(victim)

        with self.assertRaises(test_runner.TestRunnerError):
            self.runner.run_visible((self.python_check("noop", "pass"),), PATIENCE)

        self.assertEqual(victim.read_text(encoding="utf-8"), "precious\n")
        self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o644)

    def test_a_symlink_given_as_the_log_is_refused(self):
        victim = self.root / "victim.txt"
        victim.write_text("precious\n", encoding="utf-8")
        link = self.root / "link.jsonl"
        link.symlink_to(victim)
        with self.assertRaises(test_runner.TestRunnerError):
            TestRunner(self.worktree, link)
        self.assertEqual(victim.read_text(encoding="utf-8"), "precious\n")

    def test_a_hard_linked_log_is_refused(self):
        os.link(self.log_path, self.root / "alias.jsonl")
        with self.assertRaises(test_runner.TestRunnerError):
            TestRunner(self.worktree, self.log_path)

    def test_a_group_writable_log_directory_is_refused(self):
        shared = self.root / "shared"
        shared.mkdir()
        shared.chmod(0o777)
        with self.assertRaises(test_runner.TestRunnerError):
            TestRunner(self.worktree, shared / "results.jsonl")


if __name__ == "__main__":
    unittest.main()
