"""Dedicated worktrees and the integration node with the real git (PAW-035).

No database. Every repository is a temporary repository of the user running
the tests, and git runs through ``SubprocessGitRunner`` as that user: nothing
here touches SSH, another Linux user or another user's home.
"""

import asyncio
import os
import shutil
import unittest
import uuid

from paw_backend.integration import MERGE_EMAIL, MERGE_NAME
from paw_backend.orchestrator.workspaces import (
    IntegrationState,
    NodeWorktree,
    WorktreeConflictError,
    WorktreeProblem,
    WorktreeUnavailableError,
)
from paw_backend.repositories.git import command_name
from paw_backend.tasks import RepoRole, TaskRun

from .repositories_support import fs, requires_git
from .worktrees_support import Workspace, commit_file, git

# git commands that change what a person sees or what others see: the
# coordinator never runs one of them.
FORBIDDEN = {"push", "fetch", "pull", "checkout", "switch", "reset", "rebase", "clone"}


class CoordinatorTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.repo = self.ws.add_checkout("repo")
        self.checkout = self.ws.checkout(self.repo)
        self.main_head = git("rev-parse", "HEAD", cwd=self.checkout)
        self.coordinator = self.ws.coordinator()

    def tearDown(self):
        # Whatever a test did, the user's checkout is where it was: the default
        # branch did not move, the checkout still has it checked out, it is
        # clean, and no forbidden command ran.
        self.assertEqual(git("rev-parse", "main", cwd=self.checkout), self.main_head)
        self.assertEqual(
            git("symbolic-ref", "--short", "HEAD", cwd=self.checkout), "main"
        )
        self.assertEqual(git("status", "--porcelain", cwd=self.checkout), "")
        self.assertFalse(FORBIDDEN & set(self.ws.runner.subcommands()))

    async def prepare(self, key, *upstream, **options) -> NodeWorktree:
        prepared = await self.coordinator.prepare_node(
            self.ws.node_request(key, *upstream, **options)
        )
        return prepared[self.repo]


@requires_git
class DedicatedWorktreeTest(CoordinatorTestCase):
    async def test_each_worker_gets_its_own_worktree_and_branch(self):
        a = await self.prepare("a")
        b = await self.prepare("b")

        self.assertEqual(a.path, self.ws.worktree(self.repo, "a"))
        self.assertEqual(a.branch, self.ws.branch("a"))
        self.assertEqual(b.path, self.ws.worktree(self.repo, "b"))
        self.assertNotEqual(a.path, b.path)
        self.assertNotEqual(a.branch, b.branch)
        for worktree in (a, b):
            self.assertEqual(
                git("symbolic-ref", "--short", "HEAD", cwd=worktree.path),
                worktree.branch,
            )
            # Both start at the integration branch, which starts at main.
            self.assertEqual(
                git("rev-parse", "HEAD", cwd=worktree.path), self.main_head
            )
            # The node must stay out of the user's checkout, the integration,
            # every other worktree (the whole worktree area but its own
            # worktree) and its own worktree's ``.git``.
            self.assertEqual(
                worktree.protected,
                (
                    self.checkout,
                    self.ws.worktree(self.repo, "_integration"),
                    self.ws.base,
                    f"{worktree.path}/.git",
                ),
            )
        self.assertEqual(
            git("rev-parse", self.ws.branch("_integration"), cwd=self.checkout),
            self.main_head,
        )

    async def test_a_later_attempt_gets_the_same_worktree_with_its_work(self):
        first = await self.prepare("a")
        work = commit_file(first.path, "a.txt", "partial\n")
        fs.write(first.path, "draft.txt", "uncommitted\n")

        again = await self.prepare("a")

        self.assertEqual(again, first)
        self.assertEqual(git("rev-parse", "HEAD", cwd=again.path), work)
        self.assertEqual(fs.read(again.path, "draft.txt"), "uncommitted\n")

    async def test_a_worktree_removed_by_hand_is_made_again_on_its_branch(self):
        first = await self.prepare("a")
        work = commit_file(first.path, "a.txt", "kept\n")
        git("worktree", "remove", "--force", first.path, cwd=self.checkout)
        self.assertFalse(fs.exists(first.path))

        again = await self.prepare("a")

        self.assertEqual(again.path, first.path)
        self.assertEqual(git("rev-parse", "HEAD", cwd=again.path), work)

    async def test_a_new_attempt_of_the_task_starts_from_scratch(self):
        first = await self.prepare("a")
        commit_file(first.path, "a.txt", "attempt 1\n")

        second = await self.prepare("a", run=TaskRun(2, 0))

        self.assertNotEqual(second.path, first.path)
        self.assertEqual(second.branch, self.ws.branch("a", TaskRun(2, 0)))
        self.assertEqual(git("rev-parse", "HEAD", cwd=second.path), self.main_head)

    async def test_a_retry_of_the_same_attempt_keeps_the_worktree(self):
        first = await self.prepare("a")
        again = await self.prepare("a", run=TaskRun(1, 3))
        self.assertEqual(again, first)

    async def test_parallel_nodes_create_the_integration_branch_once(self):
        keys = [f"n{i}" for i in range(4)]
        prepared = await asyncio.gather(*(self.prepare(key) for key in keys))
        self.assertEqual(len({worktree.path for worktree in prepared}), 4)
        listed = git("worktree", "list", "--porcelain", cwd=self.checkout)
        integration = [
            line
            for line in listed.splitlines()
            if line.startswith("worktree ") and line.endswith("/_integration")
        ]
        self.assertEqual(len(integration), 1)

    async def test_a_node_takes_in_the_branches_of_its_upstream_workers(self):
        a = await self.prepare("a")
        commit_file(a.path, "a.txt", "from a\n")
        b = await self.prepare("b")
        commit_file(b.path, "b.txt", "from b\n")

        c = await self.prepare("c", "a", "b")

        self.assertEqual(fs.read(c.path, "a.txt"), "from a\n")
        self.assertEqual(fs.read(c.path, "b.txt"), "from b\n")
        # A later attempt merges nothing again.
        head = git("rev-parse", "HEAD", cwd=c.path)
        await self.prepare("c", "a", "b")
        self.assertEqual(git("rev-parse", "HEAD", cwd=c.path), head)

    async def test_an_upstream_that_wrote_nothing_here_is_skipped(self):
        c = await self.prepare("c", "never-ran")
        self.assertEqual(git("rev-parse", "HEAD", cwd=c.path), self.main_head)

    async def test_conflicting_upstream_branches_fail_the_node(self):
        a = await self.prepare("a")
        commit_file(a.path, "code.txt", "a's version\n")
        b = await self.prepare("b")
        commit_file(b.path, "code.txt", "b's version\n")

        with self.assertRaises(WorktreeConflictError):
            await self.prepare("c", "a", "b")

        c = self.ws.worktree(self.repo, "c")
        # The failed merge was aborted: the worktree holds a's work, cleanly.
        self.assertEqual(git("status", "--porcelain", cwd=c), "")
        self.assertEqual(fs.read(c, "code.txt"), "a's version\n")

    async def test_the_integration_base_is_origin_head_when_there_is_one(self):
        # origin/HEAD names the default branch; the local branch is the base.
        bare = self.ws.world.make_bare("org", "other", branch="trunk")
        clone = f"{self.ws.home}/workspaces/project/other"
        git("clone", "--quiet", bare, clone)
        git("switch", "--quiet", "-c", "feature", cwd=clone)
        commit_file(clone, "feature.txt", "not the base\n")
        trunk = git("rev-parse", "trunk", cwd=clone)
        other = self.ws.add_checkout_at(clone)

        prepared = await self.coordinator.prepare_node(self.ws.node_request("a"))

        self.assertEqual(git("rev-parse", "HEAD", cwd=prepared[other].path), trunk)

    async def test_several_repositories_each_get_a_worktree(self):
        other = self.ws.add_checkout("other")
        prepared = await self.coordinator.prepare_node(self.ws.node_request("a"))
        self.assertEqual(set(prepared), {self.repo, other})
        self.assertEqual(prepared[other].path, self.ws.worktree(other, "a"))

    async def test_a_repository_without_a_checkout_gets_no_worktree(self):
        from paw_backend.tools import ScopedRepository

        bare_id = uuid.uuid4()
        self.ws.repositories.append(
            ScopedRepository(bare_id, self.ws.project_id, role=RepoRole.WORKING)
        )
        prepared = await self.coordinator.prepare_node(self.ws.node_request("a"))
        self.assertEqual(set(prepared), {self.repo})

    async def test_only_working_and_target_repositories_get_a_worktree(self):
        # Decision 0036 (2), after #85: a ``referenced`` repository is only read,
        # and one whose role is unresolved gets nothing (fail-closed); neither is
        # integrated.
        target = self.ws.add_checkout("target", role=RepoRole.TARGET)
        referenced = self.ws.add_checkout("referenced", role=RepoRole.REFERENCED)
        unresolved = self.ws.add_checkout("unresolved", role=None)

        prepared = await self.coordinator.prepare_node(self.ws.node_request("a"))

        self.assertEqual(set(prepared), {self.repo, target})
        for repo_id in (referenced, unresolved):
            self.assertFalse(fs.exists(self.ws.worktree(repo_id, "a")))
        report = await self.coordinator.integrate(self.ws.integration_request("a"))
        self.assertEqual({r.repo_id for r in report.repositories}, {self.repo, target})


@requires_git
class RefusalTest(CoordinatorTestCase):
    async def assert_problem(self, problem, awaitable):
        with self.assertRaises(WorktreeUnavailableError) as caught:
            await awaitable
        self.assertEqual(caught.exception.reason, problem)

    async def test_a_default_branch_inside_the_workspace_namespace_is_refused(self):
        other = self.ws.add_checkout("other", branch="paw/main")
        del other
        await self.assert_problem(
            WorktreeProblem.DEFAULT_BRANCH_IN_NAMESPACE,
            self.coordinator.prepare_node(self.ws.node_request("a")),
        )

    async def test_something_else_at_the_worktree_path_is_refused(self):
        await self.prepare("a")
        # The branch exists but another directory took the worktree's place.
        path = self.ws.worktree(self.repo, "a")
        git("worktree", "remove", "--force", path, cwd=self.checkout)
        os.makedirs(path)
        fs.write(path, "squatter.txt", "not a worktree\n")
        await self.assert_problem(
            WorktreeProblem.GIT_FAILED,
            self.coordinator.prepare_node(self.ws.node_request("a")),
        )

    async def test_a_user_without_a_linux_account_is_refused(self):
        self.ws.task.created_by = uuid.uuid4()
        await self.assert_problem(
            WorktreeProblem.ACCOUNT_UNAVAILABLE,
            self.coordinator.prepare_node(self.ws.node_request("a")),
        )

    async def test_a_checkout_inside_the_worktree_area_is_refused(self):
        # A checkout that encloses the worktree area of the account.
        os.makedirs(os.path.dirname(self.ws.base), exist_ok=True)
        self.ws.world.make_repository(self.ws.base)
        self.ws.add_checkout_at(self.ws.base)
        await self.assert_problem(
            WorktreeProblem.OVERLAPS_CHECKOUT,
            self.coordinator.prepare_node(self.ws.node_request("a")),
        )

    async def test_an_empty_repository_has_no_base(self):
        empty = f"{self.ws.home}/workspaces/project/empty"
        os.makedirs(empty)
        git("init", "--quiet", "--initial-branch=main", empty)
        self.ws.add_checkout_at(empty)
        await self.assert_problem(
            WorktreeProblem.BASE_UNKNOWN,
            self.coordinator.prepare_node(self.ws.node_request("a")),
        )


@requires_git
class IntegrationTest(CoordinatorTestCase):
    async def test_parallel_changes_are_aggregated_in_the_integration_worktree(self):
        a = await self.prepare("a")
        b = await self.prepare("b")
        commit_file(a.path, "a.txt", "from a\n")
        commit_file(b.path, "b.txt", "from b\n")

        report = await self.coordinator.integrate(self.ws.integration_request("a", "b"))

        self.assertTrue(report.clean)
        (result,) = report.repositories
        self.assertEqual(result.state, IntegrationState.MERGED)
        self.assertEqual(result.merged, ("a", "b"))
        self.assertEqual(result.branch, self.ws.branch("_integration"))
        self.assertEqual(result.path, self.ws.worktree(self.repo, "_integration"))
        self.assertEqual(result.head, git("rev-parse", "HEAD", cwd=result.path))
        self.assertEqual(fs.read(result.path, "a.txt"), "from a\n")
        self.assertEqual(fs.read(result.path, "b.txt"), "from b\n")
        # Two merge commits (--no-ff), made by the workspace's fixed identity.
        log = git(
            "log", "--merges", "--format=%an <%ae>|%s", "main..HEAD", cwd=result.path
        ).splitlines()
        self.assertEqual(
            log,
            [
                f"{MERGE_NAME} <{MERGE_EMAIL}>|Integrate {self.ws.branch('b')}",
                f"{MERGE_NAME} <{MERGE_EMAIL}>|Integrate {self.ws.branch('a')}",
            ],
        )
        # The default branch and the user's checkout are untouched (tearDown),
        # and neither branch of the workers was changed.
        self.assertEqual(
            git("rev-parse", self.ws.branch("a"), cwd=self.checkout),
            git("rev-parse", "HEAD", cwd=a.path),
        )

    async def test_integrating_again_merges_nothing_twice(self):
        a = await self.prepare("a")
        commit_file(a.path, "a.txt", "from a\n")
        first = await self.coordinator.integrate(self.ws.integration_request("a"))
        again = await self.coordinator.integrate(self.ws.integration_request("a"))
        self.assertEqual(again.repositories[0].head, first.repositories[0].head)
        self.assertEqual(again.repositories[0].merged, ("a",))

    async def test_a_conflict_is_detected_and_reported_not_merged(self):
        a = await self.prepare("a")
        b = await self.prepare("b")
        c = await self.prepare("c")
        commit_file(a.path, "code.txt", "a's version\n")
        commit_file(b.path, "code.txt", "b's version\n")
        commit_file(c.path, "c.txt", "from c\n")

        report = await self.coordinator.integrate(
            self.ws.integration_request("a", "b", "c")
        )

        self.assertFalse(report.clean)
        (result,) = report.repositories
        self.assertEqual(result.state, IntegrationState.CONFLICT)
        self.assertEqual(result.merged, ("a",))
        self.assertEqual(result.blocking_node, "b")
        self.assertEqual(result.conflicted_files, ("code.txt",))
        # Nothing was half merged, and nothing after the conflict was merged.
        self.assertEqual(git("status", "--porcelain", cwd=result.path), "")
        self.assertFalse(fs.exists(f"{result.path}/c.txt"))
        self.assertEqual(fs.read(result.path, "code.txt"), "a's version\n")
        self.assertEqual(result.head, git("rev-parse", "HEAD", cwd=result.path))

    async def test_a_conflict_a_human_resolved_is_passed_next_time(self):
        a = await self.prepare("a")
        b = await self.prepare("b")
        commit_file(a.path, "code.txt", "a's version\n")
        commit_file(b.path, "code.txt", "b's version\n")
        first = await self.coordinator.integrate(self.ws.integration_request("a", "b"))
        integration = first.repositories[0].path

        # The human resolves the conflict in the integration worktree.
        git("merge", "--quiet", self.ws.branch("b"), cwd=integration, check=False)
        fs.write(integration, "code.txt", "resolved\n")
        git("commit", "--quiet", "-am", "Resolve", cwd=integration)

        again = await self.coordinator.integrate(self.ws.integration_request("a", "b"))
        self.assertTrue(again.clean)
        self.assertEqual(again.repositories[0].merged, ("a", "b"))
        self.assertEqual(fs.read(integration, "code.txt"), "resolved\n")

    async def test_a_merge_left_unfinished_is_kept_and_reported(self):
        # A merge in progress in the integration worktree is a human's (the
        # backend's own merges never leave one: merge-tree judges them first and
        # a failed one is aborted at once). It is never aborted for them.
        a = await self.prepare("a")
        b = await self.prepare("b")
        commit_file(a.path, "code.txt", "a's version\n")
        commit_file(b.path, "code.txt", "b's version\n")
        await self.coordinator.integrate(self.ws.integration_request("a"))
        integration = self.ws.worktree(self.repo, "_integration")
        git("merge", "--quiet", self.ws.branch("b"), cwd=integration, check=False)
        merge_head = git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=integration)
        self.assertTrue(merge_head)
        fs.write(integration, "code.txt", "being resolved\n")

        report = await self.coordinator.integrate(self.ws.integration_request("a", "b"))

        (result,) = report.repositories
        self.assertEqual(result.state, IntegrationState.DIRTY)
        self.assertIsNone(result.blocking_node)
        self.assertEqual(
            git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=integration),
            merge_head,
        )
        self.assertEqual(fs.read(integration, "code.txt"), "being resolved\n")
        # The human finishes the merge: the next integration passes it.
        git("commit", "--quiet", "-am", "Resolve", cwd=integration)
        again = await self.coordinator.integrate(self.ws.integration_request("a", "b"))
        self.assertTrue(again.clean)

    async def test_a_merge_that_fails_without_a_conflict_is_a_git_failure(self):
        # merge-tree says the merge is clean, but git cannot make it (here: the
        # index is locked, as by a process of an expired lease). That is not a
        # conflict for a human to resolve: it is a git failure (a Retry).
        a = await self.prepare("a")
        commit_file(a.path, "a.txt", "from a\n")
        integration = self.ws.worktree(self.repo, "_integration")
        lock = os.path.join(
            git("rev-parse", "--path-format=absolute", "--git-dir", cwd=integration),
            "index.lock",
        )
        fs.write(lock, "")
        self.addCleanup(os.remove, lock)

        with self.assertRaises(WorktreeUnavailableError) as caught:
            await self.coordinator.integrate(self.ws.integration_request("a"))

        self.assertEqual(caught.exception.reason, WorktreeProblem.GIT_FAILED)
        self.assertFalse(
            git(
                "rev-parse",
                "-q",
                "--verify",
                "MERGE_HEAD",
                cwd=integration,
                check=False,
            )
        )

    async def test_an_upstream_merge_that_fails_without_a_conflict_is_retryable(self):
        a = await self.prepare("a")
        commit_file(a.path, "a.txt", "from a\n")
        c = await self.prepare("c")
        lock = os.path.join(
            git("rev-parse", "--path-format=absolute", "--git-dir", cwd=c.path),
            "index.lock",
        )
        fs.write(lock, "")
        self.addCleanup(os.remove, lock)

        with self.assertRaises(WorktreeUnavailableError) as caught:
            await self.prepare("c", "a")

        self.assertEqual(caught.exception.reason, WorktreeProblem.GIT_FAILED)

    async def test_uncommitted_work_of_a_worker_stops_the_repository(self):
        a = await self.prepare("a")
        b = await self.prepare("b")
        commit_file(a.path, "a.txt", "from a\n")
        commit_file(b.path, "b.txt", "from b\n")
        fs.write(b.path, "b.txt", "changed, not committed\n")

        report = await self.coordinator.integrate(self.ws.integration_request("a", "b"))

        (result,) = report.repositories
        self.assertEqual(result.state, IntegrationState.DIRTY)
        self.assertEqual(result.blocking_node, "b")
        self.assertEqual(result.merged, ())
        # Nothing was merged, and the work was neither dropped nor committed.
        self.assertFalse(fs.exists(f"{result.path}/a.txt"))
        self.assertEqual(fs.read(b.path, "b.txt"), "changed, not committed\n")

    async def test_uncommitted_changes_in_the_integration_worktree_stop_it(self):
        a = await self.prepare("a")
        commit_file(a.path, "a.txt", "from a\n")
        fs.write(self.ws.worktree(self.repo, "_integration"), "x.txt", "stray\n")

        report = await self.coordinator.integrate(self.ws.integration_request("a"))

        (result,) = report.repositories
        self.assertEqual(result.state, IntegrationState.DIRTY)
        self.assertIsNone(result.blocking_node)

    async def test_a_repository_no_worker_wrote_to_has_nothing_to_integrate(self):
        other = self.ws.add_checkout("other")
        a = await self.coordinator.prepare_node(
            self.ws.node_request(
                "a",
                scope=self.ws.scope(
                    repositories=[
                        r for r in self.ws.repositories if r.repo_id == self.repo
                    ]
                ),
            )
        )
        commit_file(a[self.repo].path, "a.txt", "from a\n")

        report = await self.coordinator.integrate(self.ws.integration_request("a"))

        self.assertTrue(report.clean)
        self.assertEqual(report.repository(self.repo).state, IntegrationState.MERGED)
        self.assertEqual(report.repository(other).state, IntegrationState.NOTHING)
        self.assertFalse(fs.exists(self.ws.worktree(other, "_integration")))

    async def test_each_repository_has_its_own_integration_state(self):
        other = self.ws.add_checkout("other")
        prepared_a = await self.coordinator.prepare_node(self.ws.node_request("a"))
        prepared_b = await self.coordinator.prepare_node(self.ws.node_request("b"))
        # A conflict in "repo", a clean merge in "other".
        commit_file(prepared_a[self.repo].path, "code.txt", "a\n")
        commit_file(prepared_b[self.repo].path, "code.txt", "b\n")
        commit_file(prepared_a[other].path, "a.txt", "a\n")
        commit_file(prepared_b[other].path, "b.txt", "b\n")

        report = await self.coordinator.integrate(self.ws.integration_request("a", "b"))

        self.assertEqual(report.repository(self.repo).state, IntegrationState.CONFLICT)
        self.assertEqual(report.repository(other).state, IntegrationState.MERGED)
        self.assertEqual(report.repository(other).merged, ("a", "b"))

    async def test_targets_name_the_integration_worktrees(self):
        a = await self.prepare("a")
        commit_file(a.path, "a.txt", "from a\n")
        report = await self.coordinator.integrate(self.ws.integration_request("a"))

        (target,) = await self.coordinator.targets(self.ws.integration_request("a"))

        result = report.repositories[0]
        self.assertEqual(
            (target.repo_id, target.path, target.branch, target.head, target.clean),
            (self.repo, result.path, result.branch, result.head, True),
        )

    async def test_targets_say_when_the_integration_worktree_is_not_clean(self):
        a = await self.prepare("a")
        commit_file(a.path, "a.txt", "from a\n")
        report = await self.coordinator.integrate(self.ws.integration_request("a"))
        fs.write(report.repositories[0].path, "stray.txt", "not committed\n")

        (target,) = await self.coordinator.targets(self.ws.integration_request("a"))

        self.assertFalse(target.clean)
        self.assertEqual(target.head, report.repositories[0].head)

    async def test_no_target_before_any_integration_branch_exists(self):
        self.assertEqual(
            await self.coordinator.targets(self.ws.integration_request()), ()
        )


@requires_git
class TamperedWorktreeTest(CoordinatorTestCase):
    """A Worker's worktree is a directory the agent writes to. The backend runs
    git there only with the worktree's own git directory in the checkout
    (``--git-dir``), so a ``.git`` the agent replaced (with a configuration of
    its own: a filter driver is a command) is never used."""

    def plant_repository(self, path: str, branch: str, marker: str) -> None:
        # What an agent with only file-write tools can leave behind: a ``.git``
        # directory of its own, on the node's branch, with a filter driver.
        shutil.rmtree(f"{path}/.git", ignore_errors=True)
        if fs.exists(f"{path}/.git"):
            os.remove(f"{path}/.git")
        git("init", "--quiet", "--initial-branch=main", path)
        git("symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=path)
        fs.write(path, ".gitattributes", "* filter=evil\n")
        fs.write(path, "planted.txt", "x\n")
        git("add", "-A", cwd=path)
        git("commit", "--quiet", "-m", "planted", cwd=path)
        git("config", "filter.evil.clean", f"sh -c 'touch {marker}; cat'", cwd=path)
        # A stale index: the next status must run the clean filter again.
        fs.write(path, "planted.txt", "y\n")

    async def test_a_replaced_git_directory_of_a_worker_runs_nothing(self):
        a = await self.prepare("a")
        commit_file(a.path, "a.txt", "from a\n")
        marker = f"{self.ws.world.root}/pwned"
        self.plant_repository(a.path, a.branch, marker)
        # The checkout still lists the worktree on its branch.
        self.assertEqual(
            await self.coordinator._git.worktree_branch(
                self.checkout, a.path, self.ws.account
            ),
            a.branch,
        )

        with self.assertRaises(WorktreeUnavailableError) as caught:
            await self.coordinator.integrate(self.ws.integration_request("a"))
        self.assertEqual(caught.exception.reason, WorktreeProblem.NOT_THE_WORKTREE)
        with self.assertRaises(WorktreeUnavailableError):
            await self.prepare("a")

        self.assertFalse(fs.exists(marker))

    async def test_a_git_file_pointing_at_another_worktree_is_refused(self):
        a = await self.prepare("a")
        b = await self.prepare("b")
        commit_file(b.path, "b.txt", "from b\n")
        other = git("rev-parse", "--path-format=absolute", "--git-dir", cwd=a.path)
        os.remove(f"{b.path}/.git")
        fs.write(b.path, ".git", f"gitdir: {other}\n")

        with self.assertRaises(WorktreeUnavailableError) as caught:
            await self.coordinator.integrate(self.ws.integration_request("a", "b"))

        self.assertEqual(caught.exception.reason, WorktreeProblem.NOT_THE_WORKTREE)

    async def test_git_runs_in_a_worktree_with_its_own_git_directory(self):
        a = await self.prepare("a")
        commit_file(a.path, "a.txt", "from a\n")
        self.ws.runner.calls.clear()

        await self.coordinator.integrate(self.ws.integration_request("a"))

        common = git(
            "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=self.checkout
        )
        pinned = 0
        for args in self.ws.runner.calls:
            git_dirs = [a for a in args if a.startswith("--git-dir=")]
            if command_name(args) in ("status", "merge"):
                self.assertEqual(len(git_dirs), 1, args)
            for option in git_dirs:
                pinned += 1
                self.assertEqual(
                    os.path.dirname(option.removeprefix("--git-dir=")),
                    f"{common}/worktrees",
                )
                self.assertTrue(any(a.startswith("--work-tree=/") for a in args))
        self.assertGreater(pinned, 0)


class ConstructionTest(unittest.TestCase):
    def test_collaborators_are_checked(self):
        ws = Workspace()
        self.addCleanup(ws.close)
        with self.assertRaises(TypeError):
            ws.coordinator(runner=object())
        with self.assertRaises(TypeError):
            ws.coordinator(accounts=object())
        with self.assertRaises(TypeError):
            ws.coordinator(policy=object())
