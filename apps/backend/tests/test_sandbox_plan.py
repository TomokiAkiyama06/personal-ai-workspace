"""Which sandbox a call gets (Decision 0083, 6): ``plan.py`` and ``spec.py``.

One repository is mounted: read-write only when it is the node's own worktree
(its ``.git`` file read-only again), read-only otherwise with the checkout's
``.git`` masked; the scope's excluded paths and the repositories nested inside
are masked in both.
"""

import asyncio
import os
import unittest
import uuid
from dataclasses import replace

from paw_backend.authz import RepoAcl
from paw_backend.repositories import SubprocessGitRunner
from paw_backend.sandbox.gitread import GitReader, RepositoryGit
from paw_backend.sandbox.plan import mount_target, plan_sandbox, repository_of
from paw_backend.sandbox.spec import (
    CommandKind,
    SandboxCommand,
    SandboxMount,
    SandboxSpec,
    SandboxUnavailableError,
    checked_mount_path,
    checked_sandbox_path,
)
from paw_backend.tools import ScopedRepository

from .repositories_support import requires_git
from .sandbox_support import World


def locate(world: World) -> RepositoryGit:
    return asyncio.run(
        GitReader(SubprocessGitRunner()).locate(
            world.root, account=world.account, checkouts=world.scope.excluded_paths
        )
    )


@requires_git
class WriterPlanTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.repository = repository_of(self.world.scope, self.world.root)

    def test_the_own_worktree_is_writable_and_its_git_file_read_only(self):
        world = self.world
        plan = plan_sandbox(
            world.scope, self.repository, locate(world), writable=True, shim=True
        )
        self.assertEqual(
            plan.spec.mounts, (SandboxMount(world.worktree, "/workspace", True),)
        )
        self.assertEqual(plan.spec.masks, (f"{world.worktree}/secret",))
        self.assertEqual(plan.spec.readonly, (f"{world.worktree}/.git",))
        self.assertTrue(plan.spec.shim)
        # Nothing outside the worktree is mounted, so the checkout, the
        # integration worktree and the worktree area need no mask.
        self.assertNotIn(world.checkout, plan.spec.masks)

    def test_a_read_only_call_in_the_worktree_masks_its_git_file(self):
        world = self.world
        plan = plan_sandbox(
            world.scope, self.repository, locate(world), writable=False, shim=False
        )
        self.assertFalse(plan.spec.mounts[0].writable)
        self.assertIn(f"{world.worktree}/.git", plan.spec.masks)
        self.assertEqual(plan.spec.readonly, ())

    def test_nested_repositories_are_masked(self):
        world = self.world
        nested_id = uuid.uuid4()
        nested = ScopedRepository(
            nested_id,
            world.ws.project_id,
            f"{world.worktree}/vendor/lib",
            RepoAcl.inherit(nested_id, world.ws.project_id),
        )
        os.makedirs(nested.root)
        scope = replace(world.scope, repositories=(*world.scope.repositories, nested))
        plan = plan_sandbox(
            scope, self.repository, locate(world), writable=True, shim=False
        )
        self.assertIn(nested.root, plan.spec.masks)
        # With two repositories each is mounted under its id.
        self.assertEqual(
            plan.spec.mounts[0].target, f"/workspace/{self.repository.repo_id}"
        )
        self.assertEqual(repository_of(scope, f"{nested.root}/x.py"), nested)

    def test_a_mask_inside_another_is_not_placed_twice(self):
        world = self.world
        scope = replace(
            world.scope,
            excluded_paths=(
                *world.scope.excluded_paths,
                f"{world.worktree}/secret/deeper",
            ),
        )
        plan = plan_sandbox(
            scope, self.repository, locate(world), writable=True, shim=False
        )
        self.assertEqual(plan.spec.masks, (f"{world.worktree}/secret",))
        self.assertIn(f"{world.worktree}/secret/deeper", plan.hidden)

    def test_an_excluded_root_is_never_mounted(self):
        world = self.world
        scope = replace(
            world.scope,
            excluded_paths=(*world.scope.excluded_paths, world.worktree),
        )
        with self.assertRaises(SandboxUnavailableError):
            plan_sandbox(
                scope, self.repository, locate(world), writable=True, shim=False
            )


@requires_git
class ReaderPlanTest(unittest.TestCase):
    def setUp(self):
        self.world = World(writer=False)
        self.addCleanup(self.world.close)
        self.repository = repository_of(self.world.scope, self.world.root)

    def test_the_checkout_is_read_only_and_its_git_masked(self):
        world = self.world
        plan = plan_sandbox(
            world.scope, self.repository, locate(world), writable=False, shim=True
        )
        self.assertEqual(
            plan.spec.mounts, (SandboxMount(world.checkout, "/workspace", False),)
        )
        self.assertEqual(
            plan.spec.masks,
            (f"{world.checkout}/.git", f"{world.checkout}/secret"),
        )

    def test_a_checkout_is_never_writable(self):
        world = self.world
        with self.assertRaises(SandboxUnavailableError) as caught:
            plan_sandbox(
                world.scope, self.repository, locate(world), writable=True, shim=False
            )
        self.assertEqual(caught.exception.reason, "not_a_worktree")
        self.assertIsInstance(caught.exception, PermissionError)
        self.assertEqual(mount_target(world.scope, self.repository), "/workspace")


class SpecTest(unittest.TestCase):
    def test_mount_paths_must_be_mountable(self):
        for path in ("/a,b", '/a"b', "/a\nb", "relative", "/a/../b", "/"):
            with self.assertRaises(SandboxUnavailableError, msg=path):
                checked_mount_path(path)
        self.assertEqual(checked_mount_path("/a b/c"), "/a b/c")

    def test_sandbox_paths_lie_under_the_workspace(self):
        self.assertEqual(checked_sandbox_path("/workspace/a b"), "/workspace/a b")
        for path in ("/etc/passwd", "/workspace/../etc", "/workspacex", "x"):
            with self.assertRaises(ValueError, msg=path):
                checked_sandbox_path(path)

    def test_masks_lie_inside_the_mount_and_read_only_paths_in_a_writable_one(self):
        mount = SandboxMount("/w/repo", "/workspace", writable=False)
        with self.assertRaises(ValueError):
            SandboxSpec((mount,), masks=("/w/other",))
        with self.assertRaises(ValueError):
            SandboxSpec((mount,), masks=("/w/repo",))
        with self.assertRaises(ValueError):
            SandboxSpec((mount,), readonly=("/w/repo/.git",))
        with self.assertRaises(ValueError):
            SandboxSpec(())
        with self.assertRaises(ValueError):
            SandboxMount("/w/repo", "/elsewhere")
        spec = SandboxSpec((mount,), masks=("/w/repo/b", "/w/repo/a", "/w/repo/a"))
        self.assertEqual(spec.masks, ("/w/repo/a", "/w/repo/b"))
        self.assertEqual(spec.to_container("/w/repo/a/x"), "/workspace/a/x")
        self.assertIsNone(spec.to_container("/w/repository"))
        self.assertEqual(spec.to_host("/workspace/src"), "/w/repo/src")
        self.assertIsNone(spec.to_host("/workspacex"))

    def test_commands(self):
        command = SandboxCommand(CommandKind.SHELL, "/workspace", "ls -la")
        self.assertNotIn("ls -la", repr(command))
        with self.assertRaises(ValueError):
            SandboxCommand(CommandKind.SHELL, "/workspace", "")
        with self.assertRaises(ValueError):
            SandboxCommand(CommandKind.SHELL, "/workspace", "a\x00b")
        with self.assertRaises(ValueError):
            SandboxCommand(CommandKind.SHELL, "/workspace", "\ud800")
        with self.assertRaises(ValueError):
            SandboxCommand(CommandKind.SHELL, "/workspace", "x" * 70_000)
        with self.assertRaises(ValueError):
            SandboxCommand(CommandKind.SHELL, "/workspace", "ls", stdin=b"x")
        with self.assertRaises(ValueError):
            SandboxCommand(CommandKind.FILE_WRITE, "/workspace", "/etc/passwd")
        with self.assertRaises(ValueError):
            SandboxCommand(CommandKind.SHELL, "/tmp", "ls")


if __name__ == "__main__":
    unittest.main()
