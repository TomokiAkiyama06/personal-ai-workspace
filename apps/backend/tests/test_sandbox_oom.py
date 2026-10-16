"""An OOM kill in a tool sandbox's container (Decision 0092, 5; issue #251).

The server's wrapper compares the ``oom_kill`` of the container's host-side
``memory.events`` before and after an ``exec`` and, for ``paw-sandbox/v2``
only, says ``oom=<0|1|->`` on its status line; the backend sends ``v2`` only
with ``PAW_SANDBOX_OOM_STATUS``; an ``oom=1`` ends the node's attempt as
``AgentOutOfMemory``. Here: the wrapper's functions with a fake ``capture`` and a
temporary directory as the cgroup root (no Podman, no real cgroup), the wire,
the runner, the pool, the repository tools and the toolkit on fakes.
"""

import os
import shlex
import shutil
import tempfile
import unittest
import uuid
from dataclasses import replace

from paw_backend.config import Settings
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.sandbox.pool import SandboxKey, SandboxPool
from paw_backend.sandbox.runner import ProcessResult, _WrapperRunner
from paw_backend.sandbox.spec import (
    CommandKind,
    SandboxCommand,
    SandboxExecResult,
    SandboxMount,
    SandboxSpec,
)
from paw_backend.sandbox.tools import REPO_FILE_WRITE, REPO_SHELL_RUN
from paw_backend.sandbox.wire import (
    OOM_PROTOCOL_TAG,
    PROTOCOL_TAG,
    exec_command,
    split_status,
    start_command,
    stop_command,
)
from paw_backend.tasks import TaskRun
from paw_backend.tools.runner import ExecutionStatus

from .repositories_support import requires_git
from .sandbox_support import HostSandboxRunner
from .test_sandbox_tools import _Fixture
from .test_ssh_git_wrapper import wrapper

SANDBOX = uuid.UUID("12345678-1234-5678-1234-567812345678")
NONCE = "0123456789abcdef0123456789abcdef"
IMAGE = "registry.example/paw-sandbox@sha256:" + "a" * 64
ACCOUNT = LinuxAccount(uuid.uuid4(), "alice", os.geteuid(), "/home/alice")
SHELL = SandboxCommand(CommandKind.SHELL, "/workspace", "make test")
CGROUP = "/user.slice/user-1000.slice/libpod-abc.scope/container"


def events(oom_kill: int) -> str:
    return f"low 0\nhigh 0\nmax 3\noom 2\noom_kill {oom_kill}\noom_group_kill 0\n"


class WrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="paw-oom-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = f"{self.tmp}/cgroup"
        self.cgroup = f"{self.root}{CGROUP}"
        os.makedirs(self.cgroup)
        home = f"{self.tmp}/home"
        os.makedirs(f"{home}/workspaces")
        self.config = wrapper.parse_config(
            [f"--root={home}/workspaces", f"--home={home}", f"--sandbox-image={IMAGE}"]
        )

    def write_events(self, oom_kill):
        with open(f"{self.cgroup}/memory.events", "w") as handle:
            handle.write(events(oom_kill))

    def call(self, *, oom=True):
        return wrapper.sandbox_plan(
            exec_command(SANDBOX, NONCE, SHELL, oom_status=oom), self.config
        )

    def run_exec(self, call, *, inspect=None, kill=False):
        """The wrapper's ``exec`` with Podman faked: ``inspect`` is what
        ``podman container inspect`` prints; ``kill`` makes the command bump
        the cgroup's ``oom_kill``."""
        if inspect is None:
            inspect = f"true {CGROUP}\n" if call.oom else "true\n"

        def capture(argv, env, stdin, timeout):
            if argv == call.inspect:
                return 0, inspect.encode(), b"", False
            if kill:
                self.write_events(5)
            return 3, b"out", b"err", False

        out, err = wrapper.run_sandbox_exec(
            call, self.config, b"make test", capture=capture, cgroup_root=self.root
        )
        self.assertEqual(out, b"out")
        return err.decode().rsplit("\n", 2)[-2]

    def test_v2_inspect_asks_for_the_cgroup_path(self):
        call = self.call()
        self.assertTrue(call.oom)
        self.assertEqual(
            call.inspect[-3:],
            ["--format", "{{.State.Running}} {{.State.CgroupPath}}", call.name],
        )
        old = self.call(oom=False)
        self.assertFalse(old.oom)
        self.assertEqual(old.inspect[-2], "{{.State.Running}}")
        self.assertEqual(old.argv, call.argv)  # the same command runs

    def test_only_exec_has_a_v2(self):
        for original in (
            start_command(SANDBOX, SPEC).replace(PROTOCOL_TAG, OOM_PROTOCOL_TAG),
            stop_command(SANDBOX).replace(PROTOCOL_TAG, OOM_PROTOCOL_TAG),
            exec_command(SANDBOX, NONCE, SHELL).replace(PROTOCOL_TAG, "paw-sandbox/v3"),
        ):
            with (
                self.subTest(original=original),
                self.assertRaises(wrapper.Rejected) as caught,
            ):
                wrapper.sandbox_plan(original, self.config)
            self.assertEqual(caught.exception.reason, "bad_protocol")

    def test_an_oom_kill_while_the_command_ran(self):
        self.write_events(4)
        self.assertEqual(
            self.run_exec(self.call(), kill=True),
            f"paw-sandbox-status {NONCE} exit=3 truncated=0 oom=1",
        )

    def test_no_oom_kill(self):
        self.write_events(4)  # earlier kills do not count
        self.assertEqual(
            self.run_exec(self.call()),
            f"paw-sandbox-status {NONCE} exit=3 truncated=0 oom=0",
        )

    def test_what_cannot_be_read_is_a_dash(self):
        line = f"paw-sandbox-status {NONCE} exit=3 truncated=0 oom=-"
        # No memory.events (the file is not there).
        self.assertEqual(self.run_exec(self.call()), line)
        self.write_events(4)
        for inspect in (
            "true\n",  # no path
            "true \n",
            "true relative/path\n",
            f"true {CGROUP}/../../escape\n",
            "true /\n",
            "true /./x\n",
        ):
            with self.subTest(inspect=inspect):
                self.assertEqual(self.run_exec(self.call(), inspect=inspect), line)
        with open(f"{self.cgroup}/memory.events", "w") as handle:
            handle.write("oom_kill many\n")
        self.assertEqual(self.run_exec(self.call()), line)
        with open(f"{self.cgroup}/memory.events", "w") as handle:
            handle.write("low 0\n")
        self.assertEqual(self.run_exec(self.call()), line)

    def test_paths_stay_under_the_root(self):
        self.assertEqual(
            wrapper.memory_events_path(CGROUP, "/sys/fs/cgroup"),
            f"/sys/fs/cgroup{CGROUP}/memory.events",
        )
        for bad in ("", "x", "/..", "/a/../b", "/a/./b", "/", "/a\x00b"):
            with self.subTest(bad=bad):
                self.assertIsNone(wrapper.memory_events_path(bad, "/sys/fs/cgroup"))

    def test_v1_keeps_its_line(self):
        self.write_events(4)
        self.assertEqual(
            self.run_exec(self.call(oom=False), kill=True),
            f"paw-sandbox-status {NONCE} exit=3 truncated=0",
        )

    def test_a_container_that_is_not_running_is_missing(self):
        for call in (self.call(), self.call(oom=False)):
            for inspect in ("false\n", f"false {CGROUP}\n", ""):
                with self.subTest(oom=call.oom, inspect=inspect):

                    def capture(argv, env, stdin, timeout, inspect=inspect):
                        return 0, inspect.encode(), b"", False

                    _, err = wrapper.run_sandbox_exec(
                        call, self.config, b"x", capture=capture, cgroup_root=self.root
                    )
                    self.assertEqual(
                        err.decode(), f"\npaw-sandbox-status {NONCE} missing\n"
                    )

    def test_main_accepts_a_v2_exec(self):
        # The forced command dispatches v2 like v1 (to the sandbox, not git).
        original = exec_command(SANDBOX, NONCE, SHELL, oom_status=True)
        self.assertTrue(original.startswith(f"{OOM_PROTOCOL_TAG} exec "))
        self.assertEqual(wrapper.SANDBOX_TAG_V2, OOM_PROTOCOL_TAG)
        self.assertEqual(wrapper.SANDBOX_TAG, PROTOCOL_TAG)


class WireTest(unittest.TestCase):
    def test_the_v2_tag_only_when_asked(self):
        self.assertEqual(
            shlex.split(exec_command(SANDBOX, NONCE, SHELL))[0], PROTOCOL_TAG
        )
        self.assertEqual(
            shlex.split(exec_command(SANDBOX, NONCE, SHELL, oom_status=True))[0],
            OOM_PROTOCOL_TAG,
        )
        # The rest of the words are the same.
        self.assertEqual(
            shlex.split(exec_command(SANDBOX, NONCE, SHELL))[1:],
            shlex.split(exec_command(SANDBOX, NONCE, SHELL, oom_status=True))[1:],
        )
        for command in (start_command(SANDBOX, SPEC), stop_command(SANDBOX)):
            self.assertTrue(command.startswith(PROTOCOL_TAG + " "))

    def test_the_v2_line(self):
        for oom, expected in (("1", True), ("0", False), ("-", None)):
            line = f"\npaw-sandbox-status {NONCE} exit=2 truncated=1 oom={oom}\n"
            own, status = split_status(b"err" + line.encode(), NONCE, oom_status=True)
            self.assertEqual(own, b"err")
            self.assertEqual(
                (status.exit_code, status.truncated, status.out_of_memory),
                (2, True, expected),
            )
        missing = f"\npaw-sandbox-status {NONCE} missing\n".encode()
        self.assertIsNone(split_status(missing, NONCE, oom_status=True)[1].exit_code)

    def test_a_line_of_the_other_form_is_not_accepted(self):
        old = f"\npaw-sandbox-status {NONCE} exit=0 truncated=0\n".encode()
        new = f"\npaw-sandbox-status {NONCE} exit=0 truncated=0 oom=0\n".encode()
        self.assertIsNone(split_status(old, NONCE, oom_status=True)[1])
        self.assertIsNone(split_status(new, NONCE)[1])
        bad = f"\npaw-sandbox-status {NONCE} exit=0 truncated=0 oom=2\n".encode()
        self.assertIsNone(split_status(bad, NONCE, oom_status=True)[1])
        self.assertIsNone(split_status(old, NONCE)[1].out_of_memory)


class _Answering(_WrapperRunner):
    def __init__(self, oom):
        self.oom = oom
        self.remotes = []

    async def _call(self, account, remote, *, stdin, timeout_s):
        self.remotes.append(remote)
        words = shlex.split(remote)
        line = f"exit=0 truncated=0{'' if self.oom is None else ' oom=' + self.oom}"
        return ProcessResult(
            0, b"", f"\npaw-sandbox-status {words[3]} {line}\n".encode(), False
        )


class RunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_result_says_out_of_memory(self):
        for oom, expected in (("1", True), ("0", False), ("-", False)):
            runner = _Answering(oom)
            result = await runner.exec(
                ACCOUNT, SANDBOX, SHELL, timeout_s=5, oom_status=True
            )
            self.assertIs(result.out_of_memory, expected)
            self.assertTrue(runner.remotes[0].startswith(OOM_PROTOCOL_TAG + " "))

    async def test_v1_by_default(self):
        runner = _Answering(None)
        result = await runner.exec(ACCOUNT, SANDBOX, SHELL, timeout_s=5)
        self.assertFalse(result.out_of_memory)
        self.assertTrue(runner.remotes[0].startswith(PROTOCOL_TAG + " "))


class FakeRunner:
    """A runner without ``oom_status`` (as the ones before Decision 0092)."""

    def __init__(self):
        self.options = []

    async def start(self, account, sandbox_id, spec):
        pass

    async def stop(self, account, sandbox_id):
        pass

    async def exec(self, account, sandbox_id, command, *, timeout_s, **options):
        self.options.append(options)
        return SandboxExecResult(0, "", "", out_of_memory=bool(options))


KEY = SandboxKey(uuid.uuid4(), TaskRun(1, 0), uuid.uuid4(), uuid.uuid4(), True)
SPEC = SandboxSpec((SandboxMount("/w/repo", "/workspace", writable=True),))


class PoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_pool_asks_only_when_it_is_on(self):
        runner = FakeRunner()
        pool = SandboxPool(runner)
        result = await pool.run(KEY, ACCOUNT, SPEC, SHELL)
        self.assertEqual(runner.options, [{}])
        self.assertFalse(result.out_of_memory)
        pool = SandboxPool(runner, oom_status=True)
        result = await pool.run(KEY, ACCOUNT, SPEC, SHELL)
        self.assertEqual(runner.options[-1], {"oom_status": True})
        self.assertTrue(result.out_of_memory)
        await pool.close()

    def test_the_setting_is_off_by_default(self):
        self.assertIs(Settings().sandbox_oom_status, False)
        self.assertIs(
            Settings(_env_file=None, sandbox_oom_status=True).sandbox_oom_status, True
        )


class _OomHost(HostSandboxRunner):
    """The host runner whose next commands' containers count an OOM kill."""

    oom = False

    async def exec(self, account, sandbox_id, command, *, timeout_s, oom_status=False):
        result = await super().exec(account, sandbox_id, command, timeout_s=timeout_s)
        return replace(result, out_of_memory=oom_status and self.oom)


@requires_git
class ToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = _Fixture(self)
        host = _OomHost(self.f.shims)
        self.addCleanup(host.close)
        self.f.sandboxes = host
        self.f.pool = SandboxPool(host, shim_base=self.f.shims, oom_status=True)
        self.f.executor._pool = self.f.pool
        self.worktree = self.f.world.worktree

    async def asyncTearDown(self):
        await self.f.pool.close()

    async def test_an_oom_kill_is_a_failed_call_of_its_own_type(self):
        shell = {"workdir": self.worktree, "command": "echo hi"}
        result = await self.f.result(REPO_SHELL_RUN, shell)
        self.assertEqual(result["exit_code"], 0)
        self.f.sandboxes.oom = True
        for tool, arguments in (
            (REPO_SHELL_RUN, shell),
            (REPO_FILE_WRITE, {"path": f"{self.worktree}/n.txt", "content": "x"}),
        ):
            with self.subTest(tool=tool):
                outcome = await self.f.call(tool, arguments)
                self.assertIs(outcome.status, ExecutionStatus.FAILED)
                self.assertEqual(outcome.error_type, "SandboxOutOfMemoryError")
                self.assertIsNone(outcome.result)  # nothing the command printed


if __name__ == "__main__":
    unittest.main()
