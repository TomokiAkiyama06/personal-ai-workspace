"""A Reviewer's read-only worktree and its review base, with the real git
(issue #297, Decision 0099, 5).

No database: temporary repositories of the user running the tests and
``SubprocessGitRunner``, as in ``test_worktrees_coordinator.py``.

* ``prepare_review`` makes a branch of the reviewer's own from the
  integration branch, writes ``refs/paw/review-base/<branch>`` BEFORE merging
  the upstream Workers' branches, then merges them: the diff from the review
  base to ``HEAD`` is the Workers' change (never empty, also when the default
  branch moved on meanwhile), while a Worker's own base would show nothing;
* the worktree is read-only to the node and is never integrated;
* a later attempt gets the same review base back; a lost ref is found again
  from the first-parent history (the commit below the upstream merges), and a
  history the backend did not make is refused (``BASE_UNKNOWN``).
"""

from paw_backend.orchestrator.workspaces import (
    NodeWorktree,
    WorktreeConflictError,
    WorktreeProblem,
    WorktreeUnavailableError,
)

from .repositories_support import fs, requires_git
from .test_worktrees_coordinator import CoordinatorTestCase
from .worktrees_support import commit_file, git

REVIEW = "paw-review-claude"


@requires_git
class ReviewWorktreeTest(CoordinatorTestCase):
    async def review(self, *upstream, key=REVIEW) -> NodeWorktree:
        prepared = await self.coordinator.prepare_review(
            self.ws.node_request(key, *upstream)
        )
        return prepared[self.repo]

    def ref(self, key=REVIEW) -> str:
        return f"refs/paw/review-base/{self.ws.branch(key)}"

    def changed(self, worktree: NodeWorktree) -> list[str]:
        return git(
            "diff", "--name-only", worktree.base_commit, "HEAD", cwd=worktree.path
        ).splitlines()

    async def workers(self):
        a = await self.prepare("a")
        commit_file(a.path, "a.txt", "from a\n")
        b = await self.prepare("b")
        commit_file(b.path, "b.txt", "from b\n")
        return a, b

    async def test_the_review_base_is_before_the_upstream_merges(self):
        await self.workers()

        review = await self.review("a", "b")

        self.assertTrue(review.read_only)
        self.assertEqual(review.path, self.ws.worktree(self.repo, REVIEW))
        self.assertEqual(review.branch, self.ws.branch(REVIEW))
        self.assertEqual(review.base_commit, self.main_head)
        self.assertEqual(
            git("rev-parse", self.ref(), cwd=self.checkout), self.main_head
        )
        self.assertEqual(fs.read(review.path, "a.txt"), "from a\n")
        self.assertEqual(fs.read(review.path, "b.txt"), "from b\n")
        # The Workers' change, and nothing else.
        self.assertEqual(self.changed(review), ["a.txt", "b.txt"])
        log = git(
            "log", "--format=%s", f"{review.base_commit}..HEAD", cwd=review.path
        ).splitlines()
        self.assertEqual(log.count("work"), 2)
        self.assertEqual(
            review.protected,
            (
                self.checkout,
                self.ws.worktree(self.repo, "_integration"),
                self.ws.base,
                f"{review.path}/.git",
            ),
        )
        # A Worker on the same branches starts AFTER their merges: from its
        # base nothing would show (why the review has a base of its own).
        worker = await self.prepare("c", "a", "b")
        self.assertEqual(worker.base_commit, git("rev-parse", "HEAD", cwd=worker.path))
        # No review base for a Worker, no Worker base for the reviewer.
        self.assertEqual(
            git(
                "for-each-ref", "--format=%(refname)", "refs/paw/", cwd=self.checkout
            ).splitlines(),
            sorted(
                [f"refs/paw/base/{self.ws.branch(k)}" for k in ("a", "b", "c")]
                + [self.ref()]
            ),
        )

    async def test_a_default_branch_that_moved_on_is_not_in_the_review(self):
        await self.workers()
        # The user's default branch moves on after the integration branch
        # was made.
        commit_file(self.checkout, "later.txt", "not the workers'\n")
        self.main_head = git("rev-parse", "HEAD", cwd=self.checkout)

        review = await self.review("a", "b")

        self.assertNotEqual(review.base_commit, self.main_head)
        self.assertEqual(self.changed(review), ["a.txt", "b.txt"])
        self.assertFalse(fs.exists(f"{review.path}/later.txt"))

    async def test_a_later_attempt_keeps_its_review_base(self):
        await self.workers()
        first = await self.review("a", "b")
        head = git("rev-parse", "HEAD", cwd=first.path)

        again = await self.review("a", "b")

        self.assertEqual(again, first)
        self.assertEqual(git("rev-parse", "HEAD", cwd=again.path), head)

    async def test_a_lost_review_base_is_found_below_the_merges(self):
        await self.workers()
        first = await self.review("a", "b")
        git("update-ref", "-d", self.ref(), cwd=self.checkout)

        again = await self.review("a", "b")

        self.assertEqual(again.base_commit, first.base_commit)
        self.assertEqual(
            git("rev-parse", self.ref(), cwd=self.checkout), self.main_head
        )

    async def test_a_review_base_lost_before_the_merges_is_its_start(self):
        # The process ended after the branch was made, before its ref.
        await self.review()  # no upstream branch yet: nothing merged
        git("update-ref", "-d", self.ref(), cwd=self.checkout)
        await self.workers()

        review = await self.review("a", "b")

        self.assertEqual(review.base_commit, self.main_head)
        self.assertEqual(self.changed(review), ["a.txt", "b.txt"])

    async def test_a_history_the_backend_did_not_make_is_refused(self):
        await self.workers()
        review = await self.review("a", "b")
        git("update-ref", "-d", self.ref(), cwd=self.checkout)
        commit_file(review.path, "by-hand.txt", "a commit by hand\n")

        with self.assertRaises(WorktreeUnavailableError) as caught:
            await self.review("a", "b")
        self.assertIs(caught.exception.reason, WorktreeProblem.BASE_UNKNOWN)
        self.assertNotIn(
            self.ref(),
            git("for-each-ref", "--format=%(refname)", cwd=self.checkout),
        )

    async def test_an_upstream_that_wrote_nothing_leaves_the_review_empty(self):
        review = await self.review("never-ran")
        self.assertEqual(review.base_commit, self.main_head)
        self.assertEqual(self.changed(review), [])

    async def test_conflicting_upstream_branches_fail_the_review(self):
        a = await self.prepare("a")
        commit_file(a.path, "code.txt", "a's version\n")
        b = await self.prepare("b")
        commit_file(b.path, "code.txt", "b's version\n")

        with self.assertRaises(WorktreeConflictError):
            await self.review("a", "b")
        # The review base was written before the merges, and stays.
        self.assertEqual(
            git("rev-parse", self.ref(), cwd=self.checkout), self.main_head
        )

    async def test_the_review_branch_is_never_integrated(self):
        await self.workers()
        await self.review("a", "b")

        report = await self.coordinator.integrate(self.ws.integration_request("a", "b"))

        (result,) = report.repositories
        # The Workers' branches only (the review branch merged the same ones,
        # so the trees are the same; it is never one of the merged branches).
        self.assertEqual(result.merged, ("a", "b"))
        self.assertNotIn(
            REVIEW,
            git(
                "log", "--format=%s", self.ws.branch("_integration"), cwd=self.checkout
            ),
        )

    async def test_a_repository_without_a_worktree_role_gets_nothing(self):
        prepared = await self.coordinator.prepare_review(
            self.ws.node_request(REVIEW, "a", scope=self.ws.scope(repositories=[]))
        )
        self.assertEqual(prepared, {})
