"""Names, paths and the git commands of the worktrees (PAW-035). No database.

The git tests run the real ``git`` on temporary repositories of the user running
the tests (``SubprocessGitRunner``); nothing uses SSH or another Linux user.
"""

import os
import shutil
import unittest
import uuid

from paw_backend.integration import (
    MERGE_EMAIL,
    MERGE_NAME,
    WorktreeGit,
    branch_name,
    in_namespace,
    worktree_base,
    worktree_path,
)
from paw_backend.orchestrator.workspaces import (
    WorktreeProblem,
    WorktreeUnavailableError,
)
from paw_backend.repositories import (
    GitCommandError,
    InvalidRepositoryInputError,
    LinuxAccount,
)
from paw_backend.repositories.git import command_name

from .repositories_support import fs, requires_git
from .worktrees_support import (
    RecordingRunner,
    RevParseFailingRunner,
    Workspace,
    commit_file,
    git,
)

TASK = uuid.UUID("0f5f6a3e-0000-4000-8000-000000000035")
REPO = uuid.UUID("0f5f6a3e-0000-4000-8000-0000000000aa")


class LayoutTest(unittest.TestCase):
    def test_names_are_derived_from_the_ids_only(self):
        self.assertEqual(branch_name(TASK, 2, "impl"), f"paw/{TASK}/2/impl")
        self.assertEqual(
            branch_name(TASK, 1, "_integration"), f"paw/{TASK}/1/_integration"
        )
        account = LinuxAccount(uuid.uuid4(), "alice", 1000, "/home/alice")
        base = worktree_base(account, "workspaces")
        self.assertEqual(base, "/home/alice/workspaces/.paw-worktrees")
        self.assertEqual(
            worktree_path(base, TASK, 1, REPO, "impl"), f"{base}/{TASK}/1/{REPO}/impl"
        )

    def test_only_node_keys_and_the_integration_are_accepted(self):
        for key in ("", "Impl", "../x", "a/b", "-a", "integration/../x", "_other"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                branch_name(TASK, 1, key)
        # "integration" is an ordinary node key; the integration is "_integration".
        self.assertNotEqual(
            branch_name(TASK, 1, "integration"), branch_name(TASK, 1, "_integration")
        )
        with self.assertRaises(ValueError):
            branch_name(TASK, 0, "impl")
        with self.assertRaises(ValueError):
            branch_name(TASK, True, "impl")
        with self.assertRaises(TypeError):
            branch_name(str(TASK), 1, "impl")

    def test_the_namespace(self):
        self.assertTrue(in_namespace("paw"))
        self.assertTrue(in_namespace("paw/main"))
        self.assertFalse(in_namespace("main"))
        self.assertFalse(in_namespace("pawn"))
        self.assertTrue(in_namespace(branch_name(TASK, 1, "a")))

    def test_a_path_that_would_be_too_long_is_refused(self):
        account = LinuxAccount(uuid.uuid4(), "alice", 1000, "/home/" + "h" * 1000)
        base = worktree_base(account, "workspaces")
        with self.assertRaises(WorktreeUnavailableError) as caught:
            worktree_path(base, TASK, 1, REPO, "impl")
        self.assertEqual(caught.exception.reason, WorktreeProblem.TOO_LONG)

    def test_a_bad_workspace_subdirectory_is_refused(self):
        account = LinuxAccount(uuid.uuid4(), "alice", 1000, "/home/alice")
        with self.assertRaises(ValueError):
            worktree_base(account, "../etc")


class CommandNameTest(unittest.TestCase):
    def test_the_sub_command_is_the_first_word_after_the_configuration(self):
        self.assertEqual(command_name(["merge", "--no-ff"]), "merge")
        self.assertEqual(command_name(["-c", "user.name=x", "merge"]), "merge")
        self.assertEqual(command_name(["-c", "a=b", "-c", "c=d", "status"]), "status")
        self.assertEqual(command_name([]), "git")
        self.assertEqual(command_name(["-c"]), "-c")

    def test_the_git_directory_options_are_skipped_too(self):
        self.assertEqual(
            command_name(
                ["--git-dir=/r/.git/worktrees/a", "--work-tree=/w/a", "status"]
            ),
            "status",
        )
        self.assertEqual(
            command_name(["--git-dir=/g", "--work-tree=/w", "-c", "a=b", "merge"]),
            "merge",
        )
        self.assertEqual(command_name(["--git-dir=/g"]), "git")


@requires_git
class WorktreeGitTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.repo = self.ws.add_checkout("repo")
        self.checkout = self.ws.checkout(self.repo)
        self.runner = RecordingRunner()
        self.git = WorktreeGit(self.runner, timeout_s=30)
        self.account = self.ws.account
        self.head = git("rev-parse", "HEAD", cwd=self.checkout)

    async def add(self, name: str) -> str:
        path = f"{self.ws.base}/{name}"
        await self.git.add_worktree(
            self.checkout, path, f"paw/t/{name}", self.head, self.account
        )
        return path

    async def test_merge_tree_tells_a_conflict_without_touching_anything(self):
        a = await self.add("a")
        b = await self.add("b")
        into = await self.add("into")
        commit_file(a, "code.txt", "a\n")
        commit_file(b, "code.txt", "b\n")
        commit_file(b, "other.txt", "b\n")
        clean = await self.git.check_merge(into, "paw/t/into", "paw/t/a", self.account)
        self.assertTrue(clean.clean)
        git("merge", "--quiet", "paw/t/a", cwd=into)
        before = git("rev-parse", "HEAD", cwd=into)

        conflict = await self.git.check_merge(
            into, "paw/t/into", "paw/t/b", self.account
        )

        self.assertFalse(conflict.clean)
        self.assertEqual(conflict.conflicted_files, ("code.txt",))
        self.assertEqual(git("rev-parse", "HEAD", cwd=into), before)
        self.assertEqual(git("status", "--porcelain", cwd=into), "")

    async def test_a_merge_has_the_fixed_identity_and_no_signature(self):
        a = await self.add("a")
        into = await self.add("into")
        commit_file(a, "a.txt", "a\n")
        # The repository asks for signed commits: the merge must not need a key.
        git("config", "commit.gpgSign", "true", cwd=self.checkout)

        self.assertTrue(await self.git.merge(into, "paw/t/a", self.account))

        self.assertEqual(
            git("log", "-1", "--format=%an <%ae>|%cn <%ce>|%P", cwd=into).split("|")[
                :2
            ],
            [f"{MERGE_NAME} <{MERGE_EMAIL}>"] * 2,
        )
        self.assertEqual(len(git("log", "-1", "--format=%P", cwd=into).split()), 2)
        self.assertEqual(fs.read(into, "a.txt"), "a\n")

    async def test_a_failed_merge_is_aborted(self):
        a = await self.add("a")
        into = await self.add("into")
        commit_file(a, "code.txt", "a\n")
        commit_file(into, "code.txt", "into\n")

        self.assertFalse(await self.git.merge(into, "paw/t/a", self.account))

        self.assertFalse(await self.git.merging(into, self.account))
        self.assertEqual(git("status", "--porcelain", cwd=into), "")

    async def test_the_worktree_list_is_read_from_git(self):
        a = await self.add("a")
        self.assertEqual(
            await self.git.worktree_branch(self.checkout, a, self.account), "paw/t/a"
        )
        self.assertIsNone(
            await self.git.worktree_branch(
                self.checkout, f"{self.ws.base}/nothing", self.account
            )
        )
        git("switch", "--quiet", "--detach", cwd=a)
        self.assertEqual(
            await self.git.worktree_branch(self.checkout, a, self.account), ""
        )
        fs_remove(a)
        self.assertIsNone(
            await self.git.worktree_branch(self.checkout, a, self.account)
        )

    async def test_a_missing_ref_is_none_but_a_git_failure_is_raised(self):
        # Codex review of PAW-035 (P1): exit 1 of ``rev-parse --verify --quiet``
        # is "no such ref"; anything else (exit 128: the checkout is no longer a
        # repository, it is corrupted, it cannot be read) is a git failure, never
        # read as an absent branch.
        self.assertIsNone(
            await self.git.branch_commit(self.checkout, "paw/t/none", self.account)
        )
        runner = RevParseFailingRunner()
        failing = WorktreeGit(runner, timeout_s=30)
        runner.failing = True
        with self.assertRaises(GitCommandError):
            await failing.branch_commit(self.checkout, "main", self.account)
        with self.assertRaises(GitCommandError):
            await failing.commit_of(
                self.checkout, "refs/heads/paw/t/none", self.account
            )

    async def test_an_existing_branch_is_never_recreated(self):
        await self.add("a")
        with self.assertRaises(GitCommandError):
            await self.git.add_worktree(
                self.checkout,
                f"{self.ws.base}/again",
                "paw/t/a",
                self.head,
                self.account,
            )

    async def test_hostile_values_are_refused_before_git_runs(self):
        with self.assertRaises(ValueError):
            await self.git.add_worktree(
                self.checkout, f"{self.ws.base}/x", "paw/t/x", "HEAD", self.account
            )
        with self.assertRaises(InvalidRepositoryInputError):
            await self.git.merge(self.checkout, "--upload-pack=evil", self.account)
        self.assertEqual(self.runner.calls, [])

    async def test_a_worktree_is_pinned_to_its_git_directory_in_the_checkout(self):
        a = await self.add("a")
        common = await self.git.common_dir(self.checkout, self.account)
        self.assertEqual(
            common,
            git(
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
                cwd=self.checkout,
            ),
        )
        pinned = await self.git.pin(self.checkout, a, self.account)
        self.assertEqual(pinned.path, a)
        self.assertEqual(os.path.dirname(pinned.git_dir), f"{common}/worktrees")
        self.assertEqual(await self.git.current_branch(pinned, self.account), "paw/t/a")
        self.assertTrue(await self.git.is_clean(pinned, self.account))

        # The worktree's own ``.git`` is replaced by a repository of its own:
        # the pinned commands still see the real one (and its clean index).
        os.remove(f"{a}/.git")
        git("init", "--quiet", a)
        self.assertEqual(await self.git.current_branch(pinned, self.account), "paw/t/a")
        # git never lists a ``.git`` entry: the pinned status sees the real index.
        self.assertTrue(await self.git.is_clean(pinned, self.account))
        # Asked again, the worktree no longer names a git directory of the checkout.
        self.assertIsNone(await self.git.pin(self.checkout, a, self.account))

    async def test_a_path_that_is_no_worktree_is_not_pinned(self):
        path = f"{self.ws.base}/plain"
        os.makedirs(path)
        self.assertIsNone(await self.git.pin(self.checkout, path, self.account))
        # The checkout itself is not a worktree of its own git directory.
        self.assertIsNone(
            await self.git.pin(self.checkout, self.checkout, self.account)
        )

    def test_the_timeout_is_bounded(self):
        with self.assertRaises(ValueError):
            WorktreeGit(self.runner, timeout_s=0)
        with self.assertRaises(TypeError):
            WorktreeGit(self.runner, timeout_s=True)


def fs_remove(path: str) -> None:
    shutil.rmtree(path)
