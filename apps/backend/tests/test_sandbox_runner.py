"""The ``paw-sandbox/v1`` commands and how their answers read (Decision 0083, 6).

``wire.py`` builds the remote command (each word quoted on its own, a shell
command only ever on stdin) and finds the wrapper's status line, which a
command cannot forge (its nonce never enters the sandbox). ``runner.py`` turns
an answer into a result or a fixed failure, and runs the process (stdin,
bounded output, its process group killed on a timeout).
"""

import asyncio
import json
import os
import shlex
import sys
import time
import unittest
import uuid

from paw_backend.repositories.paths import LinuxAccount
from paw_backend.sandbox.runner import (
    LocalWrapperSandboxRunner,
    ProcessResult,
    _WrapperRunner,
    run_process,
)
from paw_backend.sandbox.spec import (
    CommandKind,
    SandboxCommand,
    SandboxGoneError,
    SandboxMount,
    SandboxSpec,
    SandboxUnavailableError,
)
from paw_backend.sandbox.wire import (
    PROTOCOL_TAG,
    exec_command,
    exec_input,
    spec_document,
    split_status,
    start_command,
    stop_command,
)

SANDBOX = uuid.UUID("12345678-1234-5678-1234-567812345678")
NONCE = "0123456789abcdef0123456789abcdef"
SPEC = SandboxSpec(
    (SandboxMount("/w/a b", "/workspace", writable=True),),
    masks=("/w/a b/secret",),
    readonly=("/w/a b/.git",),
    shim=True,
)
ACCOUNT = LinuxAccount(uuid.uuid4(), "alice", os.geteuid(), "/home/alice")


class WireTest(unittest.TestCase):
    def test_start_and_stop_are_quoted_words(self):
        words = shlex.split(start_command(SANDBOX, SPEC))
        self.assertEqual(words[:3], [PROTOCOL_TAG, "start", str(SANDBOX)])
        self.assertEqual(
            json.loads(words[3]),
            {
                "mounts": [
                    {"source": "/w/a b", "target": "/workspace", "writable": True}
                ],
                "masks": ["/w/a b/secret"],
                "readonly": ["/w/a b/.git"],
                "shim": True,
            },
        )
        self.assertEqual(words[3], spec_document(SPEC))
        self.assertEqual(
            shlex.split(stop_command(SANDBOX)), [PROTOCOL_TAG, "stop", str(SANDBOX)]
        )

    def test_a_shell_command_never_enters_the_remote_command(self):
        command = SandboxCommand(
            CommandKind.SHELL, "/workspace/x y", "rm -rf '$HOME'; echo $(id)"
        )
        remote = exec_command(SANDBOX, NONCE, command)
        self.assertEqual(
            shlex.split(remote),
            [PROTOCOL_TAG, "exec", str(SANDBOX), NONCE, "/workspace/x y", "shell"],
        )
        self.assertNotIn("rm -rf", remote)
        self.assertEqual(exec_input(command), b"rm -rf '$HOME'; echo $(id)")
        write = SandboxCommand(
            CommandKind.FILE_WRITE, "/workspace", "/workspace/a.py", stdin=b"{}"
        )
        self.assertEqual(
            shlex.split(exec_command(SANDBOX, NONCE, write))[-2:],
            ["file-write", "/workspace/a.py"],
        )
        self.assertEqual(exec_input(write), b"{}")
        with self.assertRaises(ValueError):
            exec_command(SANDBOX, "short", command)

    def test_the_status_line_is_the_last_one_with_the_nonce(self):
        line = f"\npaw-sandbox-status {NONCE} exit=3 truncated=0\n".encode()
        own, status = split_status(b"err\n" + line, NONCE)
        self.assertEqual(
            (own, status.exit_code, status.truncated), (b"err\n", 3, False)
        )
        own, status = split_status(b"no newline" + line, NONCE)
        self.assertEqual(own, b"no newline")
        own, status = split_status(line, NONCE)
        self.assertEqual((own, status.exit_code), (b"", 3))
        missing = f"\npaw-sandbox-status {NONCE} missing\n".encode()
        self.assertIsNone(split_status(missing, NONCE)[1].exit_code)
        truncated = f"\npaw-sandbox-status {NONCE} exit=0 truncated=1\n".encode()
        self.assertTrue(split_status(truncated, NONCE)[1].truncated)

    def test_a_forged_or_misplaced_status_line_does_not_count(self):
        other = "f" * 32
        forged = f"\npaw-sandbox-status {other} exit=0 truncated=0\n".encode()
        self.assertIsNone(split_status(forged, NONCE)[1])
        # A command's own line before the wrapper's is just output.
        line = f"\npaw-sandbox-status {NONCE} exit=0 truncated=0\n".encode()
        self.assertIsNone(split_status(line + b"more\n", NONCE)[1])
        self.assertIsNone(split_status(line.rstrip(b"\n"), NONCE)[1])
        self.assertIsNone(split_status(b"", NONCE)[1])
        self.assertIsNone(split_status(b"\xff\n", NONCE)[1])


class _Answering(_WrapperRunner):
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def _call(self, account, remote, *, stdin, timeout_s):
        self.calls.append((remote, stdin))
        return self.answer(remote)


def _status(remote: str, text: str) -> bytes:
    nonce = shlex.split(remote)[3]
    return f"\npaw-sandbox-status {nonce} {text}\n".encode()


class RunnerAnswerTest(unittest.IsolatedAsyncioTestCase):
    command = SandboxCommand(CommandKind.SHELL, "/workspace", "make test")

    async def test_an_answer_becomes_a_result(self):
        runner = _Answering(
            lambda r: ProcessResult(
                0, b"out\n", b"err\n" + _status(r, "exit=2 truncated=0"), False
            )
        )
        result = await runner.exec(ACCOUNT, SANDBOX, self.command, timeout_s=5)
        self.assertEqual(
            (result.exit_code, result.stdout, result.stderr, result.truncated),
            (2, "out\n", "err\n", False),
        )
        self.assertEqual(runner.calls[0][1], b"make test")

    async def test_failures_are_fixed_reasons(self):
        cases = [
            (
                ProcessResult(
                    126, b"", b"paw-git-wrapper: rejected (bad_path)\n", False
                ),
                "refused",
            ),
            (ProcessResult(1, b"", b"", False), "exec_failed"),
            (ProcessResult(0, b"", b"no status\n", False), "exec_failed"),
        ]
        for answer, reason in cases:
            runner = _Answering(lambda r, a=answer: a)
            with self.assertRaises(SandboxUnavailableError) as caught:
                await runner.exec(ACCOUNT, SANDBOX, self.command, timeout_s=5)
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn("bad_path", str(caught.exception))
        runner = _Answering(
            lambda r: ProcessResult(0, b"", _status(r, "missing"), False)
        )
        with self.assertRaises(SandboxGoneError):
            await runner.exec(ACCOUNT, SANDBOX, self.command, timeout_s=5)

    async def test_start_and_stop(self):
        runner = _Answering(lambda r: ProcessResult(0, b"id\n", b"", False))
        await runner.start(ACCOUNT, SANDBOX, SPEC)
        await runner.stop(ACCOUNT, SANDBOX)
        self.assertEqual(
            [shlex.split(r)[1] for r, _ in runner.calls], ["start", "stop"]
        )
        for code, reason in ((126, "refused"), (125, "start_failed")):
            runner = _Answering(lambda r, c=code: ProcessResult(c, b"", b"", False))
            with self.assertRaises(SandboxUnavailableError) as caught:
                await runner.start(ACCOUNT, SANDBOX, SPEC)
            self.assertEqual(caught.exception.reason, reason)
            with self.assertRaises(SandboxUnavailableError):
                await runner.stop(ACCOUNT, SANDBOX)

    async def test_the_local_runner_runs_only_as_this_user(self):
        runner = LocalWrapperSandboxRunner("/usr/local/lib/paw/paw-git-wrapper")
        other = LinuxAccount(uuid.uuid4(), "bob", os.geteuid() + 1, "/home/bob")
        with self.assertRaises(SandboxUnavailableError) as caught:
            await runner.start(other, SANDBOX, SPEC)
        self.assertEqual(caught.exception.reason, "identity_mismatch")
        with self.assertRaises(ValueError):
            LocalWrapperSandboxRunner("relative/wrapper")


class ProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_stdin_and_both_streams(self):
        result = await run_process(
            [
                sys.executable,
                "-c",
                "import sys; d=sys.stdin.read(); print(d.upper());"
                " print('e', file=sys.stderr); sys.exit(4)",
            ],
            env={},
            stdin=b"hello",
            timeout_s=30,
        )
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr, result.truncated),
            (4, b"HELLO\n", b"e\n", False),
        )

    async def test_output_beyond_the_limit_is_dropped_and_said(self):
        result = await run_process(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000)"],
            env={},
            stdin=b"",
            timeout_s=30,
            limit=1000,
        )
        self.assertEqual(len(result.stdout), 1000)
        self.assertTrue(result.truncated)

    async def test_a_timeout_kills_the_process_group(self):
        marker = f"/tmp/paw-sandbox-test-{uuid.uuid4().hex}"
        # A child that would leave a marker a second later, and a parent that
        # waits: the whole group is killed at the timeout.
        script = (
            "import subprocess, time;"
            f" subprocess.Popen(['sh', '-c', 'sleep 1; touch {marker}']);"
            " time.sleep(30)"
        )
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await run_process(
                [sys.executable, "-c", script],
                env={"PATH": "/usr/bin:/bin"},
                stdin=b"",
                timeout_s=0.3,
            )
        self.assertLess(time.monotonic() - started, 10)
        await asyncio.sleep(1.5)
        self.assertFalse(await asyncio.to_thread(os.path.exists, marker))

    async def test_a_missing_program_is_unavailable(self):
        with self.assertRaises(SandboxUnavailableError):
            await run_process(["/nonexistent/podman"], env={}, stdin=b"", timeout_s=5)


if __name__ == "__main__":
    unittest.main()
