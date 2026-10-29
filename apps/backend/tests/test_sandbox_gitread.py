"""The host side of the sandbox's ``git`` (Decision 0083, 2): ``gitread.py``.

Real git on temporary repositories of the test process's own user
(``SubprocessGitRunner``; no SSH, no other user). A writing node's worktree is
made by the real ``GitWorktreeCoordinator``; its base is
``refs/paw/base/<branch>``. What is checked:

* where: a worktree is read only pinned to its git directory in a checkout the
  node's scope protects, on a ``paw/`` branch; a checkout only when its git
  directory is its own ``.git``;
* which history: the base and its descendants up to ``HEAD``; a commit of the
  shared history, a tree, a blob or a tag id is refused; ``log`` stops at the
  base; without a base only the current state;
* which files: every read excludes the hidden paths (an excluded file's change
  never shows, not even in ``show`` or ``log -p``), and a path out of scope is
  refused.
"""

import asyncio
import os
import unittest
from dataclasses import replace

from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.scope import derive_child_scope
from paw_backend.repositories import SubprocessGitRunner
from paw_backend.sandbox.gitargs import GitReadRefused, GitRefusal, parse_git_read
from paw_backend.sandbox.gitread import (
    GitReader,
    branch_changes,
    commit_paths,
    pathspecs,
)
from paw_backend.sandbox.plan import plan_sandbox, repository_of
from paw_backend.tools.scope import RealpathResolver

from .repositories_support import requires_git
from .sandbox_support import World
from .worktrees_support import commit_file, git


def run(coroutine):
    return asyncio.run(coroutine)


@requires_git
class GitReadTestCase(unittest.TestCase):
    writer = True
    base = True

    def setUp(self):
        self.world = World(writer=self.writer, base=self.base)
        self.addCleanup(self.world.close)
        self.reader = GitReader(SubprocessGitRunner())

    def locate(self):
        return run(
            self.reader.locate(
                self.world.root,
                account=self.world.account,
                checkouts=self.world.scope.excluded_paths,
            )
        )

    def read(self, *argv, workdir=None):
        """``git <argv>`` as the shim would ask it: the output, or the refusal."""
        world = self.world
        repository = repository_of(world.scope, world.root)
        assert repository is not None
        located = self.locate()
        plan = plan_sandbox(
            world.scope,
            repository,
            located,
            writable=located.pinned is not None,
            shim=False,
        )

        async def go():
            args = parse_git_read(list(argv))
            specs = await pathspecs(
                args.paths,
                workdir=workdir or world.root,
                root=world.root,
                scope=world.scope,
                resolver=RealpathResolver(),
                hidden=plan.hidden,
            )
            return await self.reader.read(
                located, args, account=world.account, pathspecs=specs
            )

        try:
            answer = run(go())
        except GitReadRefused as refused:
            return refused.code
        return answer.output

    def refused(self, *argv, code):
        self.assertIs(self.read(*argv), code, argv)


class WorktreeTest(GitReadTestCase):
    def setUp(self):
        super().setUp()
        worktree = self.world.worktree
        assert worktree is not None
        self.first = commit_file(worktree, "src/a.py", "one\n", "node work 1")
        self.second = commit_file(worktree, "src/a.py", "two\n", "node work 2")
        # The node also changes the excluded file (in its worktree).
        commit_file(worktree, "secret/key.txt", "changed-secret\n", "node secret")
        with open(os.path.join(worktree, "src", "a.py"), "a") as handle:
            handle.write("three\n")

    def test_the_worktree_is_pinned_and_has_its_base(self):
        located = self.locate()
        self.assertIsNotNone(located.pinned)
        self.assertEqual(located.branch, self.world.branch)
        self.assertEqual(located.base, self.world.base_commit)
        self.assertTrue(located.has_history)

    def test_status_and_diff_show_the_work_but_never_the_secret(self):
        self.assertIn("src/a.py", self.read("status", "--short"))
        diff = self.read("diff")
        self.assertIn("+three", diff)
        self.assertNotIn("secret", self.read("diff", "HEAD~3"))
        self.assertIn("src/a.py", self.read("diff", "--stat", "HEAD~3"))

    def test_log_stops_at_the_base(self):
        log = self.read("log", "--format=oneline")
        self.assertIn(self.first, log)
        self.assertIn(self.second, log)
        self.assertNotIn(self.world.old_commit, log)
        self.assertNotIn(self.world.base_commit, log)
        self.assertNotIn("secret", self.read("log", "-p"))
        self.assertNotIn("node secret", self.read("log", "--oneline"))

    def test_show_of_a_commit_in_range_hides_the_excluded_path(self):
        shown = self.read("show", "HEAD")
        self.assertNotIn("changed-secret", shown)
        self.assertIn(self.first, self.read("show", "--format=oneline", self.first))
        # The base itself may be read (it is the commit that changed only the
        # excluded file, so nothing of it is shown).
        base = "refs/paw/base/" + self.world.branch
        shown = self.read("show", base)
        self.assertIsInstance(shown, str)
        self.assertNotIn("s3cret", shown)
        self.assertNotIn("s3cret", self.read("show", self.world.base_commit))

    def test_commits_outside_the_range_are_refused(self):
        self.refused("show", self.world.old_commit, code=GitRefusal.OUT_OF_RANGE)
        self.refused("log", self.world.old_commit, code=GitRefusal.OUT_OF_RANGE)
        self.refused("diff", self.world.old_commit, code=GitRefusal.OUT_OF_RANGE)
        self.refused("show", "HEAD~9", code=GitRefusal.OUT_OF_RANGE)
        self.refused("show", "refs/paw/base/paw/other", code=GitRefusal.OUT_OF_RANGE)
        # Another branch's commit in the same object store.
        other = git(
            "commit-tree",
            "-m",
            "x",
            f"{self.world.old_commit}^{{tree}}",
            cwd=self.world.checkout,
        )
        self.refused("show", other, code=GitRefusal.OUT_OF_RANGE)
        self.refused("show", "0" * 40, code=GitRefusal.OUT_OF_RANGE)

    def test_tree_blob_and_tag_ids_are_refused(self):
        tree = git("rev-parse", "HEAD^{tree}", cwd=self.world.worktree)
        blob = git("rev-parse", "HEAD:src/a.py", cwd=self.world.worktree)
        git("tag", "-a", "-m", "t", "v1", "HEAD", cwd=self.world.worktree)
        tag = git("rev-parse", "v1", cwd=self.world.worktree)
        for object_id in (tree, blob, tag):
            self.refused("show", object_id, code=GitRefusal.REVISION)

    def test_paths_must_be_in_scope(self):
        self.assertIn("+three", self.read("diff", "--", "src/a.py"))
        self.assertIn(
            "+three", self.read("diff", "a.py", workdir=f"{self.world.worktree}/src")
        )
        self.refused("diff", "--", "secret/key.txt", code=GitRefusal.PATH)
        self.refused("log", "-p", "--", "secret", code=GitRefusal.PATH)
        self.refused("diff", "--", "/etc/passwd", code=GitRefusal.PATH)
        self.refused(
            "diff", "--", f"{self.world.checkout}/code.txt", code=GitRefusal.PATH
        )
        self.refused("diff", "--", ".git/config", code=GitRefusal.PATH)
        self.refused("diff", "--", "../x", code=GitRefusal.PATH)


class NoBaseTest(GitReadTestCase):
    """A worktree whose base is not recorded reads the current state only."""

    base = False

    def test_only_the_current_state(self):
        self.assertIsNone(self.locate().base)
        self.assertEqual(self.read("status", "--short"), "")
        self.assertEqual(self.read("diff"), "")
        self.assertIn("first", self.read("log", "--oneline"))
        self.refused("show", "HEAD", code=GitRefusal.HISTORY)
        self.refused("log", "-p", code=GitRefusal.HISTORY)
        self.refused("diff", "HEAD~1", code=GitRefusal.HISTORY)
        self.refused("log", "--format=full", code=GitRefusal.HISTORY)


class CheckoutTest(GitReadTestCase):
    """A node without a worktree reads the user's checkout, current state only."""

    writer = False

    def test_the_checkout_is_read_without_its_history(self):
        located = self.locate()
        self.assertIsNone(located.pinned)
        self.assertEqual(located.git_dir, f"{self.world.checkout}/.git")
        with open(os.path.join(self.world.checkout, "code.txt"), "a") as handle:
            handle.write("more\n")
        self.assertIn("+more", self.read("diff"))
        self.assertIn("old history", self.read("log", "--oneline"))
        self.refused("show", "HEAD", code=GitRefusal.HISTORY)
        self.refused("log", "-p", code=GitRefusal.HISTORY)
        self.refused("diff", "--", "secret/key.txt", code=GitRefusal.PATH)

    def test_a_checkout_whose_git_directory_is_elsewhere_is_refused(self):
        # A ``.git`` file pointing at another git directory is not a checkout.
        elsewhere = f"{self.world.ws.home}/workspaces/elsewhere.git"
        os.rename(f"{self.world.checkout}/.git", elsewhere)
        with open(f"{self.world.checkout}/.git", "w") as handle:
            handle.write(f"gitdir: {elsewhere}\n")
        with self.assertRaises(GitReadRefused) as caught:
            self.locate()
        self.assertIs(caught.exception.code, GitRefusal.UNAVAILABLE)


class ReviewWorktreeTest(GitReadTestCase):
    """A reviewer's read-only worktree (Decision 0099, 5): its range starts at
    its review base, before the upstream Worker's branch was merged in."""

    def setUp(self):
        super().setUp()
        world = self.world
        assert world.worktree is not None
        self.work = commit_file(world.worktree, "src/a.py", "one\n", "node work")
        commit_file(world.worktree, "secret/key.txt", "changed-secret\n", "secret")
        prepared = run(
            world.ws.coordinator().prepare_review(
                world.ws.node_request("paw-review-codex", "a", scope=world.parent)
            )
        )
        review = prepared[world.repo_id]
        self.review = review
        scope = derive_child_scope(
            world.parent,
            role=NodeRole.REVIEWER,
            repositories=None,
            worktrees=prepared,
        )
        # The worktree's copy of the excluded directory is excluded too.
        world.scope = replace(
            scope, excluded_paths=(*scope.excluded_paths, f"{review.path}/secret")
        )
        world.worktree = review.path

    def test_the_review_worktree_has_its_review_base(self):
        located = self.locate()
        self.assertTrue(located.review)
        self.assertEqual(located.branch, self.review.branch)
        self.assertEqual(located.base, self.review.base_commit)
        self.assertEqual(located.base_ref, f"refs/paw/review-base/{located.branch}")
        # Before the Worker's merge: HEAD is not the review base.
        head = git("rev-parse", "HEAD", cwd=self.review.path)
        self.assertNotEqual(located.base, head)

    def test_the_diff_from_the_review_base_is_the_workers_change(self):
        base = f"refs/paw/review-base/{self.review.branch}"
        diff = self.read("diff", f"{base}..HEAD")
        self.assertIn("+one", diff)
        self.assertNotIn("secret", diff)
        log = self.read("log", "-p", f"{base}..HEAD")
        self.assertIn(self.work, log)
        self.assertNotIn("changed-secret", log)
        self.assertIn("+one", self.read("show", self.work))
        self.assertNotIn("changed-secret", self.read("log", "-p"))
        self.assertNotIn(self.world.old_commit, self.read("log", "--format=oneline"))

    def test_other_bases_and_the_shared_history_are_out_of_range(self):
        self.refused(
            "show", f"refs/paw/base/{self.world.branch}", code=GitRefusal.OUT_OF_RANGE
        )
        self.refused(
            "show",
            f"refs/paw/review-base/{self.world.branch}",
            code=GitRefusal.OUT_OF_RANGE,
        )
        self.refused("show", self.world.old_commit, code=GitRefusal.OUT_OF_RANGE)
        self.refused("diff", "--", "secret/key.txt", code=GitRefusal.PATH)

    def test_nothing_is_ever_committed_in_it(self):
        located = self.locate()
        account = self.world.account
        with self.assertRaises(GitReadRefused) as caught:
            run(commit_paths(self.reader, located, ["x"], account=account, marker="m"))
        self.assertIs(caught.exception.code, GitRefusal.UNAVAILABLE)
        with self.assertRaises(GitReadRefused):
            run(branch_changes(self.reader, located, account=account, pathspecs=["."]))

    def test_a_workers_read_never_asks_for_a_review_base(self):
        # A Worker's branch has its base: the review base is not even asked
        # (a wrapper that does not know the ref yet still serves Workers).
        asked = []
        commit_of = self.reader._worktrees.commit_of

        async def recording(where, revision, account):
            asked.append(revision)
            return await commit_of(where, revision, account)

        self.reader._worktrees.commit_of = recording
        located = run(
            self.reader.locate(
                f"{self.world.ws.worktree(self.world.repo_id, 'a')}",
                account=self.world.account,
                checkouts=self.world.scope.excluded_paths,
            )
        )
        self.assertFalse(located.review)
        self.assertFalse(any("review-base" in r for r in asked))


class UnprotectedWorktreeTest(GitReadTestCase):
    def test_a_worktree_of_a_checkout_the_scope_does_not_protect_is_refused(self):
        with self.assertRaises(GitReadRefused) as caught:
            run(
                self.reader.locate(
                    self.world.root, account=self.world.account, checkouts=()
                )
            )
        self.assertIs(caught.exception.code, GitRefusal.UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
