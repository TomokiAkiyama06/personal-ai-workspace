"""The running sandboxes (Decision 0083, 6 and 8): ``pool.py``.

A sandbox is started once per node and repository and reused; another spec or
account replaces it; a sandbox that is not running is started again once; a
timeout or a cancellation stops it (shielded, before the cancellation goes on);
the node's end, idleness and ``close`` stop it. Its git shim lives and goes
with it.
"""

import asyncio
import os
import tempfile
import unittest
import uuid

from paw_backend.repositories.paths import LinuxAccount
from paw_backend.sandbox.pool import RETRY_MIN_SECONDS, SandboxKey, SandboxPool
from paw_backend.sandbox.spec import (
    CommandKind,
    SandboxCommand,
    SandboxExecResult,
    SandboxGoneError,
    SandboxMount,
    SandboxSpec,
    SandboxUnavailableError,
)
from paw_backend.tasks import TaskRun
from paw_backend.tools.deadline import call_deadline

ACCOUNT = LinuxAccount(uuid.uuid4(), "alice", os.geteuid(), "/home/alice")
TASK = uuid.uuid4()
REPO = uuid.uuid4()
AGENT = uuid.uuid4()
KEY = SandboxKey(TASK, TaskRun(1, 0), AGENT, REPO, True)
SPEC = SandboxSpec((SandboxMount("/w/repo", "/workspace", writable=True),))
COMMAND = SandboxCommand(CommandKind.SHELL, "/workspace", "true")


class FakeRunner:
    def __init__(self):
        self.started: list[uuid.UUID] = []
        self.stopped: list[uuid.UUID] = []
        self.execs: list[uuid.UUID] = []
        self.on_exec = None
        self.start_error: BaseException | None = None

    async def start(self, account, sandbox_id, spec):
        if self.start_error is not None:
            raise self.start_error
        self.started.append(sandbox_id)

    async def stop(self, account, sandbox_id):
        self.stopped.append(sandbox_id)

    async def exec(self, account, sandbox_id, command, *, timeout_s):
        self.execs.append(sandbox_id)
        if self.on_exec is not None:
            return await self.on_exec(sandbox_id)
        return SandboxExecResult(0, "ok", "", False)


class PoolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runner = FakeRunner()
        self.now = 1000.0
        self.pool = SandboxPool(self.runner, clock=lambda: self.now, idle_seconds=60)

    async def test_one_sandbox_per_key_is_reused(self):
        await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
        await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
        self.assertEqual(len(self.runner.started), 1)
        self.assertEqual(self.runner.execs, self.runner.started * 2)
        other = SandboxKey(TASK, TaskRun(2, 0), AGENT, REPO, True)
        await self.pool.run(other, ACCOUNT, SPEC, COMMAND)
        self.assertEqual(len(self.runner.started), 2)
        # Another node of the same task and run gets a sandbox of its own.
        sibling = SandboxKey(TASK, TaskRun(1, 0), uuid.uuid4(), REPO, True)
        await self.pool.run(sibling, ACCOUNT, SPEC, COMMAND)
        await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
        self.assertEqual(len(self.runner.started), 3)
        self.assertEqual(self.runner.stopped, [])
        await self.pool.release(TASK, TaskRun(1, 0), agent_id=sibling.agent_id)
        self.assertEqual(len(self.runner.stopped), 1)
        self.assertEqual(self.pool.running(), 2)

    async def test_another_spec_or_account_replaces_the_sandbox(self):
        await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
        first = self.runner.started[0]
        masked = SandboxSpec(SPEC.mounts, masks=("/w/repo/secret",))
        await self.pool.run(KEY, ACCOUNT, masked, COMMAND)
        self.assertEqual(self.runner.stopped, [first])
        bob = LinuxAccount(uuid.uuid4(), "bob", os.geteuid(), "/home/bob")
        await self.pool.run(KEY, bob, masked, COMMAND)
        self.assertEqual(len(self.runner.stopped), 2)
        self.assertEqual(len(self.runner.started), 3)

    async def test_a_sandbox_that_is_gone_is_started_again_once(self):
        gone = set()

        async def on_exec(sandbox_id):
            if sandbox_id in gone or not gone:
                gone.add(sandbox_id)
                raise SandboxGoneError()
            return SandboxExecResult(0, "again", "", False)

        self.runner.on_exec = on_exec
        result = await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
        self.assertEqual(result.stdout, "again")
        self.assertEqual(len(self.runner.started), 2)

        async def always_gone(sandbox_id):
            raise SandboxGoneError()

        self.runner.on_exec = always_gone
        with self.assertRaises(SandboxUnavailableError) as caught:
            await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
        self.assertEqual(caught.exception.reason, "not_running")

    async def test_a_timeout_or_a_failed_call_stops_the_sandbox(self):
        for error in (TimeoutError(), SandboxUnavailableError("exec_failed")):

            async def failing(sandbox_id, error=error):
                raise error

            self.runner.on_exec = failing
            with self.assertRaises(type(error)):
                await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
            self.assertEqual(self.runner.stopped[-1], self.runner.started[-1])
            self.assertEqual(self.pool.running(), 0)

    async def test_a_cancellation_stops_the_sandbox_first(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow(sandbox_id):
            entered.set()
            await asyncio.sleep(30)

        original_stop = self.runner.stop

        async def slow_stop(account, sandbox_id):
            await release.wait()
            await original_stop(account, sandbox_id)

        self.runner.on_exec = slow
        self.runner.stop = slow_stop
        task = asyncio.create_task(self.pool.run(KEY, ACCOUNT, SPEC, COMMAND))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0.05)
        # The stop is still running: the cancellation waits for it.
        self.assertFalse(task.done())
        task.cancel()  # a second cancellation is held back too
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.runner.stopped, self.runner.started)

    async def test_release_idleness_and_close(self):
        await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
        other = SandboxKey(uuid.uuid4(), TaskRun(1, 0), AGENT, REPO, False)
        await self.pool.run(other, ACCOUNT, SPEC, COMMAND)
        await self.pool.release(TASK)
        self.assertEqual(self.pool.running(), 1)
        self.now += 59
        self.assertEqual(await self.pool.reap_idle(), 0)
        self.now += 2
        self.assertEqual(await self.pool.reap_idle(), 1)
        self.assertEqual(self.pool.running(), 0)
        await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
        await self.pool.close()
        self.assertEqual(self.runner.stopped, self.runner.started)
        # No lock outlives its sandbox.
        self.assertEqual(self.pool.tracked_keys(), 0)

    async def test_a_failed_stop_is_logged_and_forgotten(self):
        async def failing_stop(account, sandbox_id):
            raise SandboxUnavailableError("stop_failed")

        self.runner.stop = failing_stop
        await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
        with self.assertLogs("paw_backend.sandbox.pool", "WARNING"):
            await self.pool.close()
        self.assertEqual(self.pool.running(), 0)


def _not_connected() -> SandboxUnavailableError:
    return SandboxUnavailableError("ssh_unavailable", not_run=True)


class NeverConnectedRetryTest(unittest.IsolatedAsyncioTestCase):
    """Issue #346: a start or a command whose ``ssh`` surely never connected
    (``not_run``) is tried once more; one that may have run never is."""

    def setUp(self):
        self.runner = FakeRunner()
        self.pool = SandboxPool(self.runner)

    def failing_first(self, *errors):
        queue = list(errors)

        async def on_exec(sandbox_id):
            if queue:
                raise queue.pop(0)
            return SandboxExecResult(0, "ran", "", False)

        self.runner.on_exec = on_exec

    async def test_a_command_that_never_connected_runs_once_more(self):
        self.failing_first(_not_connected())
        command = SandboxCommand(CommandKind.SHELL, "/workspace", "echo hidden-cmd")
        with self.assertLogs("paw_backend.sandbox.pool", "WARNING") as logs:
            result = await self.pool.run(KEY, ACCOUNT, SPEC, command)
        self.assertEqual(result.stdout, "ran")
        # The same sandbox, not stopped: the command never reached it.
        self.assertEqual(self.runner.execs, self.runner.started * 2)
        self.assertEqual(self.runner.stopped, [])
        self.assertEqual(self.pool.running(), 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("ssh_unavailable", logs.output[0])
        self.assertIn("trying once more", logs.output[0])
        self.assertNotIn("/w/repo", logs.output[0])
        self.assertNotIn("hidden-cmd", logs.output[0])

    async def test_a_command_that_may_have_run_is_never_run_again(self):
        for error in (
            SandboxUnavailableError("exec_failed"),
            SandboxUnavailableError("ssh_unavailable"),  # 255, may have run
            TimeoutError(),
        ):
            self.runner.execs.clear()
            self.failing_first(error)
            with self.assertRaises(type(error)):
                await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
            self.assertEqual(len(self.runner.execs), 1, error)
            self.assertEqual(self.runner.stopped[-1], self.runner.started[-1])
            self.assertEqual(self.pool.running(), 0)

    async def test_a_second_failure_to_connect_fails_as_before(self):
        self.failing_first(_not_connected(), _not_connected())
        with self.assertLogs("paw_backend.sandbox.pool", "WARNING"):
            with self.assertRaises(SandboxUnavailableError) as caught:
                await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
        self.assertEqual(caught.exception.reason, "ssh_unavailable")
        self.assertIsInstance(caught.exception, PermissionError)
        self.assertEqual(len(self.runner.execs), 2)
        # As any failed call: the sandbox is stopped.
        self.assertEqual(self.runner.stopped, self.runner.started)
        self.assertEqual(self.pool.running(), 0)

    async def test_no_retry_when_the_call_has_no_time_left(self):
        self.failing_first(_not_connected())
        with call_deadline(RETRY_MIN_SECONDS - 1):
            with self.assertLogs("paw_backend.sandbox.pool", "WARNING") as logs:
                with self.assertRaises(SandboxUnavailableError) as caught:
                    await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
        self.assertEqual(caught.exception.reason, "ssh_unavailable")
        self.assertEqual(len(self.runner.execs), 1)
        self.assertIn("no time left", logs.output[0])
        self.assertEqual(self.pool.running(), 0)
        # With time left, the same failure is tried once more.
        self.failing_first(_not_connected())
        with call_deadline(RETRY_MIN_SECONDS + 60):
            with self.assertLogs("paw_backend.sandbox.pool", "WARNING"):
                result = await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
        self.assertEqual(result.stdout, "ran")

    async def test_a_start_that_never_connected_starts_once_more(self):
        starts = [_not_connected()]
        original = self.runner.start

        async def start(account, sandbox_id, spec):
            if starts:
                raise starts.pop(0)
            await original(account, sandbox_id, spec)

        self.runner.start = start
        with self.assertLogs("paw_backend.sandbox.pool", "WARNING") as logs:
            result = await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(len(self.runner.started), 1)
        # Nothing was started by the failed call: nothing to stop.
        self.assertEqual(self.runner.stopped, [])
        self.assertIn("start did not connect (ssh_unavailable)", logs.output[0])

        # Twice in a row, or a start that may have run: no more tries.
        await self.pool.close()
        self.runner.stopped.clear()
        for errors, calls in (
            ([_not_connected(), _not_connected()], 2),
            ([SandboxUnavailableError("start_failed")], 1),
        ):
            starts[:] = errors
            seen = len(starts)
            with self.assertRaises(SandboxUnavailableError):
                await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
            self.assertEqual(seen - len(starts), calls)
            self.assertEqual(self.pool.running(), 0)
            starts.clear()

    async def test_a_gone_sandbox_and_a_failed_connection_share_one_retry(self):
        self.failing_first(SandboxGoneError(), _not_connected())
        with self.assertRaises(SandboxUnavailableError) as caught:
            await self.pool.run(KEY, ACCOUNT, SPEC, COMMAND)
        self.assertEqual(caught.exception.reason, "ssh_unavailable")
        self.assertEqual(len(self.runner.execs), 2)


class ShimLifetimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="paw-shims-", dir="/tmp")
        self.addCleanup(os.rmdir, self.base)
        self.runner = FakeRunner()
        self.pool = SandboxPool(self.runner, shim_base=self.base)

    async def test_the_shim_lives_and_goes_with_its_sandbox(self):
        spec = SandboxSpec(SPEC.mounts, shim=True)
        seen = []

        async def on_exec(sandbox_id):
            directory = os.path.join(self.base, sandbox_id.hex)
            seen.append(sorted(os.listdir(directory)))
            return SandboxExecResult(0, "", "", False)

        self.runner.on_exec = on_exec
        await self.pool.run(KEY, ACCOUNT, spec, COMMAND)
        self.assertEqual(seen, [["git.sock", "token"]])
        mode = os.stat(os.path.join(self.base, self.runner.started[0].hex)).st_mode
        self.assertEqual(mode & 0o777, 0o700)
        await self.pool.close()
        self.assertEqual(os.listdir(self.base), [])

    async def test_a_sandbox_that_does_not_start_leaves_no_shim(self):
        self.runner.start_error = SandboxUnavailableError("refused")
        with self.assertRaises(SandboxUnavailableError):
            await self.pool.run(
                KEY, ACCOUNT, SandboxSpec(SPEC.mounts, shim=True), COMMAND
            )
        self.assertEqual(os.listdir(self.base), [])
        self.assertEqual(self.pool.running(), 0)

    async def test_a_shim_needs_a_base(self):
        pool = SandboxPool(self.runner)
        self.assertFalse(pool.shims)
        with self.assertRaises(SandboxUnavailableError):
            await pool.run(KEY, ACCOUNT, SandboxSpec(SPEC.mounts, shim=True), COMMAND)


if __name__ == "__main__":
    unittest.main()
