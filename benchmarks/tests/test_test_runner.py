"""Acceptance tests for visible and hidden benchmark check execution."""

import contextlib
import errno
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import tracemalloc
import unittest
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
# ``session``: a child in its own session.  The check itself dies on SIGTERM.
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
        for mode in ("orphan", "session"):
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
        # Python may add LC_CTYPE itself when coercing a C locale.
        self.assertLessEqual(
            set(environment),
            set(test_runner._CHECK_ENVIRONMENT_ALLOWLIST) | {"HOME", "LC_CTYPE"},
        )
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
