"""The rules of a Working Set that need no database (issue #85, Decision 0030).

``tasks.working_set`` (approval levels, repository permissions, the verification of
a discarded change, the completion requirements) and ``tasks.domain`` (roles and
operations). The database side is ``test_task_working_set``.
"""

import asyncio
import unittest
import uuid

from paw_backend.authz import RepoPermission
from paw_backend.tasks import (
    EvaluationResult,
    PullRequestState,
    RepoRole,
    RepositoryChangeState,
    ReviewStatus,
    TaskCommand,
    TaskState,
    WorkingSetOperation,
)
from paw_backend.tasks.domain import accepts_role, narrows, plan_transition, role_after
from paw_backend.tasks.errors import IllegalTransitionError
from paw_backend.tasks.working_set import (
    AttemptRepositoryFacts,
    FailClosedChangeInspector,
    approval_level,
    meets,
    obligation,
    required_permission,
    unmet_repositories,
    verify_discarded,
    was_changed,
)
from paw_backend.tools import ApprovalLevel

Op = WorkingSetOperation
R = RepoRole
BASE = "b" * 40
CLEAN = RepositoryChangeState(
    clean=True, head_commit=BASE, branch_pushed=False, open_pull_request=False
)


def facts(**overrides) -> AttemptRepositoryFacts:
    arguments = {
        "repository_id": uuid.uuid4(),
        "role": R.TARGET,
        "starting_commit": BASE,
        "strongest_role": R.TARGET,
        "modified": False,
        "head_commit": None,
        "evaluation": EvaluationResult.PASSED,
        "review": ReviewStatus.APPROVED,
        "pull_request": PullRequestState.OPEN,
    }
    arguments.update(overrides)
    return AttemptRepositoryFacts(**arguments)


class Inspector:
    def __init__(self, answer=CLEAN, *, error=None, delay=0.0) -> None:
        self.answer = answer
        self.error = error
        self.delay = delay
        self.calls = []

    async def inspect(self, task_id, attempt, repository_id):
        self.calls.append((task_id, attempt, repository_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.answer


class RoleAndOperationTest(unittest.TestCase):
    def test_the_roles_are_the_three_of_the_requirements_in_order(self):
        self.assertEqual([r.value for r in R], ["referenced", "working", "target"])
        self.assertLess(R.REFERENCED.strength, R.WORKING.strength)
        self.assertLess(R.WORKING.strength, R.TARGET.strength)

    def test_each_operation_fixes_its_resulting_role(self):
        self.assertEqual(
            {operation: role_after(operation) for operation in Op},
            {
                Op.ADD_REFERENCED: R.REFERENCED,
                Op.SET_WORKING: R.WORKING,
                Op.SET_TARGET: R.TARGET,
                Op.DOWNGRADE_TO_WORKING: R.WORKING,
                Op.DOWNGRADE_TO_REFERENCED: R.REFERENCED,
                Op.REMOVE: None,
            },
        )

    def test_which_roles_each_operation_accepts(self):
        expected = {
            Op.ADD_REFERENCED: {None},
            Op.SET_WORKING: {None, R.REFERENCED},
            Op.SET_TARGET: {None, R.REFERENCED, R.WORKING},
            Op.DOWNGRADE_TO_WORKING: {R.TARGET},
            Op.DOWNGRADE_TO_REFERENCED: {R.WORKING, R.TARGET},
            Op.REMOVE: {R.REFERENCED, R.WORKING, R.TARGET},
        }
        for operation in Op:
            for role in (None, *R):
                with self.subTest(operation=operation.value, role=role):
                    self.assertEqual(
                        accepts_role(operation, role), role in expected[operation]
                    )

    def test_downgrades_and_the_removal_narrow(self):
        self.assertEqual(
            {o for o in Op if narrows(o)},
            {Op.DOWNGRADE_TO_WORKING, Op.DOWNGRADE_TO_REFERENCED, Op.REMOVE},
        )

    def test_the_working_set_command_is_never_a_transition(self):
        for state in TaskState:
            with self.subTest(state=state.value):
                with self.assertRaises(IllegalTransitionError):
                    plan_transition(state, TaskCommand.CHANGE_WORKING_SET)


class ApprovalAndPermissionTest(unittest.TestCase):
    def test_only_adding_a_referenced_repository_is_scoped_auto(self):
        self.assertEqual(
            {o: ApprovalLevel(approval_level(o)) for o in Op},
            {
                Op.ADD_REFERENCED: ApprovalLevel.SCOPED_AUTO,
                Op.SET_WORKING: ApprovalLevel.STRONG_APPROVAL,
                Op.SET_TARGET: ApprovalLevel.STRONG_APPROVAL,
                Op.DOWNGRADE_TO_WORKING: ApprovalLevel.STRONG_APPROVAL,
                Op.DOWNGRADE_TO_REFERENCED: ApprovalLevel.STRONG_APPROVAL,
                Op.REMOVE: ApprovalLevel.STRONG_APPROVAL,
            },
        )

    def test_the_repository_permission_of_each_change(self):
        read, write = RepoPermission.READ, RepoPermission.WRITE
        cases = [
            (Op.ADD_REFERENCED, None, read),
            (Op.SET_WORKING, None, write),
            (Op.SET_WORKING, R.REFERENCED, write),
            (Op.SET_TARGET, None, write),
            (Op.SET_TARGET, R.WORKING, write),
            (Op.DOWNGRADE_TO_WORKING, R.TARGET, write),
            (Op.DOWNGRADE_TO_REFERENCED, R.WORKING, write),
            (Op.DOWNGRADE_TO_REFERENCED, R.TARGET, write),
            # A removal needs the permission of the role it had.
            (Op.REMOVE, R.REFERENCED, read),
            (Op.REMOVE, R.WORKING, write),
            (Op.REMOVE, R.TARGET, write),
        ]
        for operation, current, permission in cases:
            with self.subTest(operation=operation.value, current=current):
                self.assertIs(required_permission(operation, current), permission)


class VerifyDiscardedTest(unittest.IsolatedAsyncioTestCase):
    async def verify(self, inspector, **overrides):
        arguments = {
            "task_id": uuid.uuid4(),
            "attempt": 1,
            "repository_id": uuid.uuid4(),
            "starting_commit": BASE,
            "open_pull_request": False,
            "timeout_seconds": 1.0,
        }
        arguments.update(overrides)
        return await verify_discarded(inspector, **arguments)

    async def test_a_clean_repository_at_its_starting_commit_is_discarded(self):
        inspector = Inspector()
        self.assertEqual(await self.verify(inspector), CLEAN)
        self.assertEqual(len(inspector.calls), 1)

    async def test_anything_else_is_not_discarded(self):
        cases = {
            "dirty worktree": RepositoryChangeState(False, BASE, False, False),
            "another HEAD": RepositoryChangeState(True, "c" * 40, False, False),
            "unknown HEAD": RepositoryChangeState(True, None, False, False),
            "pushed branch": RepositoryChangeState(True, BASE, True, False),
            "open pull request": RepositoryChangeState(True, BASE, False, True),
            "cannot tell": None,
            "not an answer": {"clean": True},
            "truthy but not True": RepositoryChangeState(1, BASE, False, False),
        }
        for label, answer in cases.items():
            with self.subTest(label):
                self.assertIsNone(await self.verify(Inspector(answer)))

    async def test_it_fails_closed(self):
        self.assertIsNone(await self.verify(FailClosedChangeInspector()))
        with self.assertLogs("paw_backend.tasks.working_set", "WARNING") as logs:
            self.assertIsNone(
                await self.verify(Inspector(error=OSError("/secret/path")))
            )
            self.assertIsNone(
                await self.verify(Inspector(delay=0.5), timeout_seconds=0.01)
            )
        # The type of the failure only: an OS error can name paths.
        self.assertNotIn("/secret/path", "\n".join(logs.output))

    async def test_no_inspection_can_verify_an_unknown_baseline_or_a_stored_pr(self):
        for overrides in ({"starting_commit": None}, {"open_pull_request": True}):
            with self.subTest(**overrides):
                inspector = Inspector()
                self.assertIsNone(await self.verify(inspector, **overrides))
                self.assertEqual(inspector.calls, [])


class CompletionTest(unittest.TestCase):
    def test_a_target_needs_evaluation_pull_request_and_approved_review(self):
        self.assertTrue(meets(facts(), R.TARGET))
        self.assertTrue(meets(facts(pull_request=PullRequestState.MERGED), R.TARGET))
        refused = {
            "evaluation not run": {"evaluation": EvaluationResult.NOT_RUN},
            "evaluation failed": {"evaluation": EvaluationResult.FAILED},
            "no pull request": {"pull_request": None},
            "draft pull request": {"pull_request": PullRequestState.DRAFT},
            "closed pull request": {"pull_request": PullRequestState.CLOSED},
            # #85 constraint 5: the review must be approved.
            "review not started": {"review": ReviewStatus.NOT_STARTED},
            "review in progress": {"review": ReviewStatus.IN_REVIEW},
            "changes requested": {"review": ReviewStatus.CHANGES_REQUESTED},
        }
        for label, overrides in refused.items():
            with self.subTest(label):
                self.assertFalse(meets(facts(**overrides), R.TARGET))

    def test_a_working_repository_needs_only_its_evaluation(self):
        self.assertTrue(
            meets(facts(pull_request=None, review=ReviewStatus.NOT_STARTED), R.WORKING)
        )
        self.assertFalse(meets(facts(evaluation=EvaluationResult.FAILED), R.WORKING))

    def test_what_each_repository_is_obliged_to(self):
        cases = [
            ("a target, untouched", {}, R.TARGET),
            (
                "a working one, untouched",
                {"role": R.WORKING, "strongest_role": R.WORKING, "pull_request": None},
                None,
            ),
            (
                "a working one, written to",
                {"role": R.WORKING, "strongest_role": R.WORKING, "modified": True},
                R.WORKING,
            ),
            (
                "a referenced one",
                {
                    "role": R.REFERENCED,
                    "strongest_role": R.REFERENCED,
                    "pull_request": None,
                },
                None,
            ),
            (
                "downgraded after it was changed as a target",
                {"role": R.REFERENCED, "strongest_role": R.TARGET, "modified": True},
                R.TARGET,
            ),
            (
                "removed after it was changed as working",
                {"role": None, "strongest_role": R.WORKING, "modified": True},
                R.WORKING,
            ),
            (
                "removed with a pull request",
                {
                    "role": None,
                    "strongest_role": R.TARGET,
                    "pull_request": PullRequestState.OPEN,
                },
                R.TARGET,
            ),
            (
                "a referenced one that was changed all the same",
                {
                    "role": R.REFERENCED,
                    "strongest_role": R.REFERENCED,
                    "modified": True,
                },
                R.WORKING,
            ),
            ("removed, never changed", {"role": None, "pull_request": None}, None),
        ]
        for label, overrides, expected in cases:
            with self.subTest(label):
                self.assertIs(obligation(facts(**overrides)), expected)

    def test_a_recorded_head_is_a_change_unless_it_is_the_starting_commit(self):
        base = {"pull_request": None, "modified": False}
        self.assertFalse(was_changed(facts(**base)))
        self.assertFalse(was_changed(facts(**base, head_commit=BASE)))
        self.assertTrue(was_changed(facts(**base, head_commit="c" * 40)))
        # Without a starting commit nothing can be told apart: changed.
        self.assertTrue(
            was_changed(facts(**base, head_commit=BASE, starting_commit=None))
        )

    def test_the_unmet_repositories_are_listed(self):
        ok = facts()
        missing_review = facts(review=ReviewStatus.CHANGES_REQUESTED)
        untouched_working = facts(
            role=R.WORKING,
            strongest_role=R.WORKING,
            pull_request=None,
            evaluation=EvaluationResult.NOT_RUN,
        )
        self.assertEqual(unmet_repositories([ok, untouched_working]), ())
        self.assertEqual(
            unmet_repositories([ok, missing_review]), (missing_review.repository_id,)
        )
