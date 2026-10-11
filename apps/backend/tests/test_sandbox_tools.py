"""The file, shell and git tools through the Tool Broker (Decision 0083, 2 and 6).

The broker is the real one (``tools_support.Harness``: in-memory adapters), the
executor the real :class:`RepoToolExecutor` with real git; only the container
is replaced by a host sandbox (``sandbox_support.HostSandboxRunner``) that runs
the command in the directory its mount maps to, the real file helper and the
real ``git`` shim program over the real shim socket. So a ``bash`` that runs
``git status`` goes: broker → executor → sandbox → shim → socket → a nested
``repo.git.read`` call decided by the broker → pinned git on the host.
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from paw_backend.authz import Capability, ProjectRole, SystemRole
from paw_backend.repositories import SubprocessGitRunner
from paw_backend.repositories.errors import LinuxAccountUnavailableError
from paw_backend.sandbox import (
    REPO_FILE_STR_REPLACE,
    REPO_FILE_WRITE,
    REPO_GIT_READ,
    REPO_SHELL_READ,
    REPO_SHELL_RUN,
    REPO_TOOL_SPECS,
    GitReader,
    RepoToolExecutor,
    SandboxPool,
    ToolExecutors,
)
from paw_backend.tasks import RepoRole
from paw_backend.tools import (
    DEFAULT_TOOL_POLICY,
    ApprovalLevel,
    BrokerReason,
    ExecutionStatus,
    ToolCall,
    ToolRegistry,
    Verdict,
)
from paw_backend.tools.capabilities import Environment, ScopeStatus
from paw_backend.tools.working_set import WORKING_SET_TOOL_SPECS

from .authz_support import StaticDirectory, principal
from .repositories_support import requires_git
from .sandbox_support import HostSandboxRunner, World
from .tools_support import Harness
from .worktrees_support import commit_file


class SpecTest(unittest.TestCase):
    def test_the_tools_and_their_levels(self):
        registry = ToolRegistry((*WORKING_SET_TOOL_SPECS, *REPO_TOOL_SPECS))
        expected = {
            REPO_FILE_WRITE: (Capability.PROJECT_REPO_WRITE, ApprovalLevel.SCOPED_AUTO),
            REPO_FILE_STR_REPLACE: (
                Capability.PROJECT_REPO_WRITE,
                ApprovalLevel.SCOPED_AUTO,
            ),
            REPO_SHELL_RUN: (Capability.PROJECT_REPO_WRITE, ApprovalLevel.SCOPED_AUTO),
            REPO_SHELL_READ: (Capability.PROJECT_READ, ApprovalLevel.SCOPED_AUTO),
            REPO_GIT_READ: (Capability.PROJECT_READ, ApprovalLevel.AUTO),
        }
        for name, (capability, level) in expected.items():
            spec = registry.get(name)
            self.assertEqual(spec.authz_capability, capability)
            self.assertIs(spec.environment, Environment.PROJECT_LOCAL)
            self.assertEqual(
                DEFAULT_TOOL_POLICY.level_for(
                    spec.capabilities, spec.environment, ScopeStatus.IN_SCOPE
                ),
                level,
            )
            # Outside the scope every one is denied.
            self.assertIs(
                DEFAULT_TOOL_POLICY.level_for(
                    spec.capabilities, spec.environment, ScopeStatus.OUT_OF_SCOPE
                ),
                ApprovalLevel.DENY,
            )
            # Each names what it touches with a required path.
            self.assertTrue(
                any(
                    a.required and a.kind.value == "path"
                    for a in spec.arguments.values()
                )
            )


async def _read(path: str) -> str:
    return await asyncio.to_thread(Path(path).read_text)


class _Fixture:
    """A world, a broker with the repository tools, and the executor."""

    def __init__(self, test: unittest.TestCase, *, writer: bool = True) -> None:
        self.world = World(writer=writer)
        test.addCleanup(self.world.close)
        self.shims = tempfile.mkdtemp(prefix="paw-shims-", dir="/tmp")
        test.addCleanup(shutil.rmtree, self.shims, True)
        self.sandboxes = HostSandboxRunner(self.shims)
        test.addCleanup(self.sandboxes.close)
        self.pool = SandboxPool(self.sandboxes, shim_base=self.shims)
        self.executor = RepoToolExecutor(
            self.pool, GitReader(SubprocessGitRunner()), self.world.ws.accounts
        )
        world = self.world
        self.harness = Harness(
            registry=ToolRegistry(REPO_TOOL_SPECS),
            executor=self.executor,
            directory=StaticDirectory(
                principal(
                    SystemRole.USER,
                    world.ws.user_id,
                    {world.ws.project_id: ProjectRole.CONTRIBUTOR},
                )
            ),
        )
        self.executor.bind_git_caller(self.harness.runner.run)

    async def call(self, tool, arguments, *, scope=None):
        return await self.harness.runner.run(
            ToolCall(tool, arguments, self.world.context(scope))
        )

    async def result(self, tool, arguments, **options):
        outcome = await self.call(tool, arguments, **options)
        assert outcome.status is ExecutionStatus.COMPLETED, (
            outcome.status,
            outcome.decision.reason,
            outcome.error_type,
        )
        return outcome.result


@requires_git
class WriterToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = _Fixture(self)
        self.worktree = self.f.world.worktree

    async def asyncTearDown(self):
        await self.f.pool.close()

    async def test_bash_runs_in_the_worktree_and_reports_exit_and_output(self):
        result = await self.f.result(
            REPO_SHELL_RUN,
            {
                "workdir": self.worktree,
                "command": "pwd; ls code.txt; echo err >&2; exit 3",
            },
        )
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["output"], f"{self.worktree}\ncode.txt\nerr\n")
        self.assertFalse(result["truncated"])
        spec = next(iter(self.f.sandboxes.started.values()))
        self.assertTrue(spec.mounts[0].writable)
        self.assertEqual(spec.mounts[0].source, self.worktree)
        self.assertEqual(spec.readonly, (f"{self.worktree}/.git",))
        self.assertEqual(spec.masks, (f"{self.worktree}/secret",))

    async def test_the_sandbox_is_kept_for_the_node(self):
        for _ in range(3):
            await self.f.result(
                REPO_SHELL_RUN, {"workdir": self.worktree, "command": "true"}
            )
        await self.f.result(
            REPO_FILE_WRITE, {"path": f"{self.worktree}/n.txt", "content": "x"}
        )
        self.assertEqual(len(self.f.sandboxes.started), 1)
        await self.f.pool.release(self.f.world.ws.task.id)
        self.assertEqual(len(self.f.sandboxes.stopped), 1)

    async def test_write_and_str_replace(self):
        path = f"{self.worktree}/src/new.py"
        result = await self.f.result(
            REPO_FILE_WRITE, {"path": path, "content": "x = 1\ny = 1\n"}
        )
        self.assertEqual(result, {"ok": True, "chars": 12})
        self.assertEqual(await _read(path), "x = 1\ny = 1\n")
        self.assertEqual(
            await self.f.result(
                REPO_FILE_STR_REPLACE, {"path": path, "old_str": "= 1", "new_str": "2"}
            ),
            {"error": "occurrences", "count": 2},
        )
        self.assertEqual(
            await self.f.result(
                REPO_FILE_STR_REPLACE,
                {"path": path, "old_str": "x = 1", "new_str": "x = 2"},
            ),
            {"ok": True, "chars": 5},
        )
        self.assertEqual(await _read(path), "x = 2\ny = 1\n")
        self.assertEqual(
            await self.f.result(
                REPO_FILE_STR_REPLACE,
                {"path": f"{self.worktree}/none.py", "old_str": "a", "new_str": "b"},
            ),
            {"error": "not_found"},
        )

    async def test_git_in_bash_goes_through_the_broker(self):
        commit_file(self.worktree, "src/a.py", "one\n", "node work")
        await asyncio.to_thread(
            Path(self.worktree, "src", "a.py").write_text, "one\ntwo\n"
        )
        before = len(self.f.harness.tool_events())
        result = await self.f.result(
            REPO_SHELL_RUN,
            {
                "workdir": f"{self.worktree}/src",
                "command": (
                    "git status --short && git diff -- a.py && git log --oneline"
                ),
            },
        )
        self.assertEqual(result["exit_code"], 0, result["output"])
        self.assertIn(" M src/a.py", result["output"])
        self.assertIn("+two", result["output"])
        self.assertIn("node work", result["output"])
        self.assertNotIn("old history", result["output"])
        # The three git calls were decided and recorded by the broker (two
        # audit rows each: the decision and the execution).
        actions = [e.action for e in self.f.harness.tool_events()[before:]]
        self.assertEqual(actions.count(f"tool.{REPO_GIT_READ}"), 6)

    async def test_git_refuses_writes_history_and_secrets(self):
        result = await self.f.result(
            REPO_SHELL_RUN,
            {
                "workdir": self.worktree,
                "command": (
                    "git commit -m x; echo $?;"
                    f" git show {self.f.world.old_commit}; echo $?;"
                    " git diff -- secret/key.txt; echo $?;"
                    " git --no-pager log --all; echo $?"
                ),
            },
        )
        output = result["output"]
        self.assertEqual(output.count("128\n"), 4, output)
        self.assertIn("subcommand_not_allowed", output)
        self.assertIn("revision_out_of_range", output)
        self.assertIn("path_not_allowed", output)
        self.assertIn("option_not_allowed", output)
        self.assertNotIn("s3cret", output)

    async def test_paths_the_scope_excludes_are_refused_by_the_broker(self):
        world = self.f.world
        for tool, arguments in (
            (REPO_FILE_WRITE, {"path": f"{self.worktree}/.git", "content": "x"}),
            (REPO_FILE_WRITE, {"path": f"{self.worktree}/secret/k", "content": "x"}),
            (REPO_FILE_WRITE, {"path": f"{world.checkout}/code.txt", "content": "x"}),
            (REPO_SHELL_RUN, {"workdir": world.checkout, "command": "true"}),
            (REPO_SHELL_RUN, {"workdir": "/etc", "command": "true"}),
        ):
            outcome = await self.f.call(tool, arguments)
            self.assertIs(outcome.status, ExecutionStatus.NOT_EXECUTED, arguments)
            self.assertIs(outcome.decision.verdict, Verdict.DENY)
        self.assertEqual(self.f.sandboxes.commands, [])

    async def test_a_link_out_of_the_repository_is_not_followed(self):
        os.symlink(self.f.world.checkout, f"{self.worktree}/link")
        outcome = await self.f.call(
            REPO_FILE_WRITE, {"path": f"{self.worktree}/link/code.txt", "content": "x"}
        )
        # The broker resolves the link with its resolver (lexical in the
        # harness, so it allows): the executor resolves it and refuses.
        self.assertIs(outcome.status, ExecutionStatus.FAILED)
        self.assertEqual(outcome.error_type, "SandboxUnavailableError")
        self.assertEqual(self.f.sandboxes.commands, [])

    async def test_a_referenced_repository_is_not_executed_in(self):
        world = self.f.world
        scope = replace(
            world.scope,
            repositories=tuple(
                replace(r, role=RepoRole.REFERENCED) for r in world.scope.repositories
            ),
        )
        outcome = await self.f.call(
            REPO_SHELL_RUN, {"workdir": self.worktree, "command": "true"}, scope=scope
        )
        self.assertIs(outcome.status, ExecutionStatus.NOT_EXECUTED)
        self.assertIs(
            outcome.decision.reason, BrokerReason.REPOSITORY_ROLE_INSUFFICIENT
        )

    async def test_no_account_no_sandbox(self):
        async def none(user_id):
            raise LinuxAccountUnavailableError()

        self.f.world.ws.accounts.account_of = none
        outcome = await self.f.call(
            REPO_SHELL_RUN, {"workdir": self.worktree, "command": "true"}
        )
        self.assertIs(outcome.status, ExecutionStatus.FAILED)
        self.assertEqual(outcome.error_type, "SandboxUnavailableError")


@requires_git
class ReaderToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = _Fixture(self, writer=False)
        self.checkout = self.f.world.checkout

    async def asyncTearDown(self):
        await self.f.pool.close()

    async def test_read_only_bash_in_the_checkout(self):
        result = await self.f.result(
            REPO_SHELL_READ,
            {"workdir": self.checkout, "command": "cat code.txt; git log --oneline"},
        )
        self.assertIn("code\n", result["output"])
        self.assertIn("old history", result["output"])
        spec = next(iter(self.f.sandboxes.started.values()))
        self.assertFalse(spec.mounts[0].writable)
        self.assertIn(f"{self.checkout}/.git", spec.masks)
        result = await self.f.result(
            REPO_SHELL_READ, {"workdir": self.checkout, "command": "git show HEAD"}
        )
        self.assertIn("history_not_available", result["output"])

    async def test_a_checkout_is_never_written(self):
        outcome = await self.f.call(
            REPO_SHELL_RUN, {"workdir": self.checkout, "command": "touch x"}
        )
        self.assertIs(outcome.status, ExecutionStatus.FAILED)
        self.assertEqual(outcome.error_type, "SandboxUnavailableError")
        self.assertFalse(await asyncio.to_thread(os.path.exists, f"{self.checkout}/x"))

    async def test_the_git_read_tool_directly(self):
        result = await self.f.result(
            REPO_GIT_READ,
            {"workdir": self.checkout, "arguments": '["log", "--oneline", "-n", "1"]'},
        )
        self.assertEqual(result["exit_code"], 0)
        # The last commit changed only the excluded file: log skips it.
        self.assertIn("old history", result["output"])
        self.assertNotIn("secret", result["output"])
        for arguments, code in (
            ('["show", "HEAD"]', "history_not_available"),
            ("not json", "subcommand_not_allowed"),
            ('{"argv": []}', "subcommand_not_allowed"),
            ('["diff", "--", "secret"]', "path_not_allowed"),
        ):
            result = await self.f.result(
                REPO_GIT_READ, {"workdir": self.checkout, "arguments": arguments}
            )
            self.assertEqual(result["error"], code, arguments)
            self.assertTrue(result["message"])

    async def test_the_shim_without_a_bound_caller(self):
        executor = RepoToolExecutor(
            self.f.pool, GitReader(SubprocessGitRunner()), self.f.world.ws.accounts
        )
        harness = self.f.harness
        runner_executor = harness.runner._executor
        harness.runner._executor = executor
        try:
            result = await self.f.result(
                REPO_SHELL_READ, {"workdir": self.checkout, "command": "git status"}
            )
        finally:
            harness.runner._executor = runner_executor
        self.assertIn("repository_unavailable", result["output"])


class DispatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_each_tool_goes_to_its_executor(self):
        seen = []

        class Recording:
            def __init__(self, name):
                self.name = name

            async def execute(self, invocation):
                seen.append((self.name, invocation.tool))
                return self.name

        executors = ToolExecutors({"a.b": Recording("one"), "c.d": Recording("two")})

        class Invocation:
            def __init__(self, tool):
                self.tool = tool

        self.assertEqual(await executors.execute(Invocation("c.d")), "two")
        with self.assertRaises(LookupError):
            await executors.execute(Invocation("e.f"))
        with self.assertRaises(TypeError):
            ToolExecutors({"a.b": object()})
        self.assertEqual(seen, [("two", "c.d")])


if __name__ == "__main__":
    unittest.main()
