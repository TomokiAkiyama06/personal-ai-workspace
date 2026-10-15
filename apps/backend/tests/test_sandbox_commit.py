"""A Worker node's base, changes and commit (Decision 0083, 10).

Real git (``GitWorktreeCoordinator``, the test process's own user) and the real
Tool Broker (``tools_support.Harness``):

* the worktree's preparation records ``refs/paw/base/<branch>`` once and every
  later preparation reads it back; a lost ref is found again from the branch's
  first-parent history (skipping the node's own ``PAW-Node`` commits);
* ``repo.git.changes`` lists what changed since the base (committed,
  uncommitted, untracked), never the hidden paths;
* ``repo.git.commit`` commits exactly the declared uncommitted paths, as the
  fixed author with the node's trailer, and refuses anything else.
"""

import asyncio
import json
import os
import shutil
import unittest
from pathlib import Path

from paw_backend.orchestrator.workspaces import WorktreeUnavailableError
from paw_backend.sandbox import REPO_GIT_CHANGES, REPO_GIT_COMMIT

from .repositories_support import requires_git
from .test_sandbox_tools import _Fixture
from .worktrees_support import commit_file, git


@requires_git
class BaseRefTest(unittest.TestCase):
    def setUp(self):
        from .sandbox_support import World

        self.world = World()
        self.addCleanup(self.world.close)

    def prepare(self):
        world = self.world
        prepared = asyncio.run(
            world.ws.coordinator().prepare_node(
                world.ws.node_request("a", scope=world.parent)
            )
        )
        return prepared[world.repo_id]

    def test_the_base_is_recorded_once_and_read_back(self):
        world = self.world
        ref = f"refs/paw/base/{world.branch}"
        self.assertEqual(git("rev-parse", ref, cwd=world.checkout), world.base_commit)
        commit_file(world.worktree, "a.py", "x\n", "node work")
        self.assertEqual(self.prepare().base_commit, world.base_commit)

    def test_a_lost_base_is_found_again_from_the_history(self):
        world = self.world
        marker = f"{world.ws.task.id}/a"
        for index in range(2):
            Path(world.worktree, f"f{index}.py").write_text("x\n")
            git("add", "-A", cwd=world.worktree)
            git(
                "commit",
                "--quiet",
                "-m",
                f"paw: node work\n\nPAW-Node: {marker}\n",
                cwd=world.worktree,
            )
        git("update-ref", "-d", f"refs/paw/base/{world.branch}", cwd=world.checkout)
        self.assertEqual(self.prepare().base_commit, world.base_commit)
        self.assertEqual(
            git("rev-parse", f"refs/paw/base/{world.branch}", cwd=world.checkout),
            world.base_commit,
        )

    def test_a_base_that_cannot_be_told_is_refused(self):
        world = self.world
        git("update-ref", "-d", f"refs/paw/base/{world.branch}", cwd=world.checkout)
        coordinator = world.ws.coordinator()

        async def history(*args, **kwargs):
            return [("f" * 40, (f"{world.ws.task.id}/a",))]

        coordinator._git.first_parent_trailers = history
        with self.assertRaises(WorktreeUnavailableError):
            asyncio.run(
                coordinator.prepare_node(world.ws.node_request("a", scope=world.parent))
            )


@requires_git
class CommitToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = _Fixture(self)
        self.worktree = self.f.world.worktree

    async def asyncTearDown(self):
        await self.f.pool.close()

    async def changes(self):
        return await self.f.result(REPO_GIT_CHANGES, {"workdir": self.worktree})

    async def test_changes_and_commit(self):
        world = self.f.world
        commit_file(self.worktree, "src/done.py", "1\n", "node work")
        await asyncio.to_thread(Path(self.worktree, "code.txt").write_text, "changed\n")
        await asyncio.to_thread(Path(self.worktree, "new.py").write_text, "new\n")
        await asyncio.to_thread(Path(self.worktree, "secret", "x").write_text, "s\n")
        found = await self.changes()
        self.assertEqual(found["base"], world.base_commit)
        self.assertEqual(found["changed"], ["code.txt", "new.py", "src/done.py"])
        self.assertEqual(found["uncommitted"], ["code.txt", "new.py"])

        result = await self.f.result(
            REPO_GIT_COMMIT,
            {"workdir": self.worktree, "paths": json.dumps(["code.txt", "new.py"])},
        )
        head = git("rev-parse", "HEAD", cwd=self.worktree)
        self.assertEqual(result, {"commit": head})
        message = git("log", "-1", "--format=%an <%ae>%n%B", cwd=self.worktree)
        self.assertIn("Personal AI Workspace Agent <agent@paw.invalid>", message)
        self.assertIn(f"PAW-Node: {world.ws.task.id}/a", message)
        found = await self.changes()
        self.assertEqual(found["uncommitted"], [])
        self.assertEqual(found["head"], head)
        # The excluded directory was neither listed nor committed.
        self.assertNotIn(
            "secret", git("show", "--name-only", "--format=", "HEAD", cwd=self.worktree)
        )

    async def test_only_declared_uncommitted_paths_are_committed(self):
        await asyncio.to_thread(Path(self.worktree, "a.py").write_text, "a\n")
        for paths in (
            ["code.txt"],  # unchanged
            ["a.py", "a.py"],
            ["secret/x"],
            ["../x"],
            [],
            "a.py",
        ):
            result = await self.f.result(
                REPO_GIT_COMMIT, {"workdir": self.worktree, "paths": json.dumps(paths)}
            )
            self.assertIn("error", result, paths)
        before = git("rev-parse", "HEAD", cwd=self.worktree)
        self.assertEqual(before, self.f.world.base_commit)
        self.assertTrue(
            await asyncio.to_thread(os.path.exists, f"{self.worktree}/a.py")
        )

    async def test_a_nested_repository_is_listed_and_never_committed(self):
        # Decision 0090 (risks): with the sandbox's real git the model can make
        # a repository inside its worktree (``git init sub``).
        await asyncio.to_thread(Path(self.worktree, "a.py").write_text, "a\n")
        git("init", "--quiet", "sub", cwd=self.worktree)
        await asyncio.to_thread(Path(self.worktree, "sub", "b.py").write_text, "b\n")
        found = await self.changes()
        self.assertEqual(found["changed"], ["a.py", "sub/"])
        self.assertEqual(found["nested"], ["sub/"])
        for paths in (["sub/"], ["a.py", "sub/"], ["sub"]):
            result = await self.f.result(
                REPO_GIT_COMMIT, {"workdir": self.worktree, "paths": json.dumps(paths)}
            )
            self.assertEqual(result["error"], "path_not_allowed", paths)
        self.assertEqual(
            git("rev-parse", "HEAD", cwd=self.worktree), self.f.world.base_commit
        )
        await asyncio.to_thread(shutil.rmtree, Path(self.worktree, "sub", ".git"))
        found = await self.changes()
        self.assertEqual(found["changed"], ["a.py", "sub/b.py"])
        self.assertEqual(found["nested"], [])

    async def test_only_the_worktree_root_is_asked(self):
        result = await self.f.result(
            REPO_GIT_CHANGES, {"workdir": f"{self.worktree}/src"}
        )
        self.assertEqual(result["error"], "path_not_allowed")


if __name__ == "__main__":
    unittest.main()
