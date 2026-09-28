"""The tests, the Evaluator and the review of the integrated result (PAW-035).

Skipped unless ``PAW_TEST_DATABASE_URL`` is set. Real tasks (``TaskService``) and
DAG store; the checks and the integration targets are fakes.
"""

import uuid

from paw_backend.authz import Capability
from paw_backend.integration import (
    CHECK_ORDER,
    CheckKind,
    CheckVerdict,
    GateOutcome,
    IntegrationGate,
    IntegrationTarget,
)
from paw_backend.orchestrator.store import DagStore
from paw_backend.tasks import (
    EvaluationResult,
    RepoRole,
    ReviewStatus,
    TaskCommand,
    TaskState,
    WorkingSetEntry,
)

from .orchestrator_support import (
    FakeAuthority,
    PostgresOrchestratorTestCase,
    requires_postgres,
)
from .task_support import BASELINE, OPEN_PULL_REQUEST


def target(repo: uuid.UUID, head="a" * 40, *, clean=True) -> IntegrationTarget:
    return IntegrationTarget(
        repo, f"/srv/w/{repo}/_integration", "paw/t/1/_integration", head, clean
    )


class FakeTargets:
    def __init__(self, *repos: uuid.UUID) -> None:
        self.current = tuple(target(repo) for repo in repos)
        self.asked = 0

    async def targets(self, request):
        self.asked += 1
        return self.current


class Check:
    def __init__(self, name, log, verdict=True, *, action=None) -> None:
        self.name = name
        self.log = log
        self.verdict = verdict
        self.action = action
        self.requests = []

    async def check(self, request):
        self.requests.append(request)
        self.log.append(self.name)
        if self.action is not None:
            await self.action()
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return CheckVerdict(self.verdict, f"summary of {self.name}")


@requires_postgres
class GateTest(PostgresOrchestratorTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.order: list[str] = []
        self.repo = self.repository_id  # the task's target (``create_task``)
        self.targets = FakeTargets(self.repo)

    def gate(self, **verdicts) -> IntegrationGate:
        self.checks = {
            kind: Check(kind.value, self.order, verdicts.get(kind.value, True))
            for kind in CHECK_ORDER
        }
        return IntegrationGate(
            tasks=self.service,
            store=DagStore(self.database),
            authority=FakeAuthority(),
            worktrees=self.targets,
            checks={kind: [check] for kind, check in self.checks.items()},
        )

    async def evaluating_task(self):
        return await self.task_in_state(TaskState.EVALUATING)

    async def deliver(self, task_id, repo=None):
        """Record the pull request Complete requires of a ``target`` (Decision
        0030, section 5): the gate does not make one (Decision 0036, 10)."""
        run = (await self.service.restore(task_id, log_limit=0)).run
        await self.service.update_attempt(
            task_id,
            run=run,
            repository_id=repo or self.repo,
            pull_request=OPEN_PULL_REQUEST,
        )

    async def review_of(self, task_id, repo=None):
        snapshot = await self.service.restore(task_id, log_limit=0)
        return snapshot.attempt.repository(repo or self.repo).review

    async def messages(self, task_id):
        return [
            log.message for log in (await self.service.restore(task_id)).recent_logs
        ]

    async def test_every_check_passes_in_order_and_the_task_completes(self):
        task_id = await self.evaluating_task()
        await self.deliver(task_id)

        report = await self.gate().evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.COMPLETED)
        self.assertEqual(self.order, ["test", "evaluator", "review"])
        self.assertEqual([kind for kind, *_ in report.verdicts], list(CHECK_ORDER))
        for kind, check in self.checks.items():
            (request,) = check.requests
            self.assertEqual(request.kind, kind)
            self.assertEqual(request.targets, (target(self.repo),))
            self.assertEqual(request.task_id, task_id)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.COMPLETED)
        review = await self.review_of(task_id)
        self.assertEqual(review.evaluation_result, EvaluationResult.PASSED)
        self.assertEqual(review.review_status, ReviewStatus.APPROVED)
        messages = await self.messages(task_id)
        self.assertIn("Integration check review passed", messages)
        # Which commit of each repository is Merge Ready is in the task log.
        self.assertIn(
            f"Integration of repository {self.repo} checked at {'a' * 40}", messages
        )
        # What a check said is returned, never stored.
        self.assertFalse(any("summary of" in message for message in messages))

    async def test_failed_tests_stop_before_the_evaluator_and_the_review(self):
        task_id = await self.evaluating_task()

        report = await self.gate(test=False).evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.FAILED)
        self.assertEqual(self.order, ["test"])
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.FAILED)
        review = await self.review_of(task_id)
        self.assertEqual(review.evaluation_result, EvaluationResult.FAILED)
        self.assertEqual(review.review_status, ReviewStatus.NOT_STARTED)

    async def test_a_failed_evaluator_stops_before_the_review(self):
        task_id = await self.evaluating_task()
        report = await self.gate(evaluator=False).evaluate(task_id)
        self.assertEqual(report.outcome, GateOutcome.FAILED)
        self.assertEqual(self.order, ["test", "evaluator"])
        review = await self.review_of(task_id)
        self.assertEqual(review.evaluation_result, EvaluationResult.FAILED)

    async def test_a_review_that_asks_for_changes_fails_the_task(self):
        task_id = await self.evaluating_task()
        report = await self.gate(review=False).evaluate(task_id)
        self.assertEqual(report.outcome, GateOutcome.FAILED)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.FAILED)
        review = await self.review_of(task_id)
        self.assertEqual(review.review_status, ReviewStatus.CHANGES_REQUESTED)
        self.assertEqual(review.evaluation_result, EvaluationResult.PASSED)

    async def test_a_check_that_breaks_did_not_pass(self):
        task_id = await self.evaluating_task()
        report = await self.gate(evaluator=RuntimeError("secret-ish")).evaluate(task_id)
        self.assertEqual(report.outcome, GateOutcome.FAILED)
        self.assertFalse(
            any("secret-ish" in message for message in await self.messages(task_id))
        )

    async def test_an_integration_that_moved_during_the_checks_is_not_completed(self):
        task_id = await self.evaluating_task()
        gate = self.gate()

        async def commit_meanwhile():
            self.targets.current = (target(self.repo, "b" * 40),)

        self.checks[CheckKind.REVIEW].action = commit_meanwhile

        report = await gate.evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.CHANGED)
        self.assertEqual((await self.service.restore(task_id)).state, TaskState.FAILED)

    async def test_a_dirty_integration_worktree_is_not_checked(self):
        # Uncommitted changes in the integration worktree: the checks would
        # read files that are not the commit the task would complete with.
        task_id = await self.evaluating_task()
        self.targets.current = (target(self.repo, clean=False),)

        report = await self.gate().evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.DIRTY)
        self.assertEqual(self.order, [])
        self.assertEqual((await self.service.restore(task_id)).state, TaskState.FAILED)

    async def test_an_integration_made_dirty_during_the_checks_is_not_completed(self):
        task_id = await self.evaluating_task()
        gate = self.gate()

        async def write_meanwhile():
            self.targets.current = (
                target(self.repo, clean=False),
            )  # the head is unchanged

        self.checks[CheckKind.TEST].action = write_meanwhile

        report = await gate.evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.CHANGED)
        self.assertEqual((await self.service.restore(task_id)).state, TaskState.FAILED)

    async def test_without_a_delivered_pull_request_the_task_is_not_completed(self):
        # Decision 0030 (section 5): a target completes only with a delivered pull
        # request; the gate makes none (Decision 0036, 10). Every check passed:
        # the results are recorded, the task is neither completed nor failed.
        task_id = await self.evaluating_task()

        report = await self.gate().evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.REQUIREMENTS_NOT_MET)
        self.assertEqual(self.order, ["test", "evaluator", "review"])
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.EVALUATING)
        review = await self.review_of(task_id)
        self.assertEqual(review.evaluation_result, EvaluationResult.PASSED)
        self.assertEqual(review.review_status, ReviewStatus.APPROVED)
        # Once the pull request is there, the gate completes the task.
        await self.deliver(task_id)
        self.order.clear()
        report = await self.gate().evaluate(task_id)
        self.assertEqual(report.outcome, GateOutcome.COMPLETED)
        self.assertEqual(
            (await self.service.restore(task_id)).state, TaskState.COMPLETED
        )

    async def test_each_repository_gets_its_own_results(self):
        # Multi-Repo (#85): every checked repository has its own review state.
        other = uuid.uuid4()
        task_id = await self.create_task(
            repositories=[
                WorkingSetEntry(self.repo, RepoRole.TARGET, BASELINE),
                WorkingSetEntry(other, RepoRole.WORKING, BASELINE),
            ]
        )
        await self.service.execute(task_id, TaskCommand.START, actor=self.system)
        await self.service.execute(
            task_id, TaskCommand.BEGIN_EVALUATION, actor=self.system
        )
        await self.deliver(task_id)
        self.targets = FakeTargets(self.repo, other)

        report = await self.gate(review=False).evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.FAILED)
        for repo in (self.repo, other):
            with self.subTest(repo=repo):
                review = await self.review_of(task_id, repo)
                self.assertEqual(review.evaluation_result, EvaluationResult.PASSED)
                self.assertEqual(review.review_status, ReviewStatus.CHANGES_REQUESTED)

    async def test_a_write_that_may_still_run_keeps_the_result_unrecorded(self):
        # A write the Tool Broker admitted and has not released (#85): a passing
        # result is refused, and nothing is completed or failed.
        task_id = await self.evaluating_task()
        await self.deliver(task_id)
        gate = self.gate()

        async def write_meanwhile():
            run = (await self.service.restore(task_id, log_limit=0)).run
            await self.service.admit_repository_use(
                task_id,
                run,
                [self.repo],
                capability=Capability.PROJECT_REPO_WRITE,
                executes=False,
            )

        self.checks[CheckKind.TEST].action = write_meanwhile

        report = await gate.evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.NOT_RECORDED)
        self.assertEqual(
            (await self.service.restore(task_id)).state, TaskState.EVALUATING
        )
        review = await self.review_of(task_id)
        self.assertNotEqual(review.evaluation_result, EvaluationResult.PASSED)

    async def test_a_repository_the_attempt_does_not_have_is_not_recorded(self):
        task_id = await self.evaluating_task()
        await self.deliver(task_id)
        self.targets = FakeTargets(self.repo, uuid.uuid4())

        report = await self.gate().evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.NOT_RECORDED)
        self.assertEqual(
            (await self.service.restore(task_id)).state, TaskState.EVALUATING
        )

    async def test_a_task_that_moved_on_is_left_alone(self):
        task_id = await self.evaluating_task()
        gate = self.gate()

        async def cancel():
            await self.service.execute(task_id, TaskCommand.CANCEL, actor=self.user)

        self.checks[CheckKind.TEST].action = cancel

        report = await gate.evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.SUPERSEDED)
        self.assertEqual(self.order, ["test"])  # nothing ran after the Cancel
        self.assertEqual(
            (await self.service.restore(task_id)).state, TaskState.CANCELLED
        )

    async def test_a_task_that_is_not_evaluating_is_not_checked(self):
        task_id = await self.task_in_state(TaskState.RUNNING)
        report = await self.gate().evaluate(task_id)
        self.assertEqual(report.outcome, GateOutcome.NOT_EVALUATING)
        self.assertEqual(self.order, [])

    def test_every_kind_of_check_is_required(self):
        def build(checks):
            return IntegrationGate(
                tasks=self.service,
                store=DagStore(self.database),
                authority=FakeAuthority(),
                worktrees=self.targets,
                checks=checks,
            )

        complete = {kind: [Check(kind.value, [])] for kind in CHECK_ORDER}
        build(complete)
        for kind in CHECK_ORDER:
            with self.subTest(missing=kind.value), self.assertRaises(TypeError):
                build({k: v for k, v in complete.items() if k is not kind})
        with self.assertRaises(TypeError):
            build({**complete, CheckKind.TEST: []})
        with self.assertRaises(TypeError):
            build({**complete, "lint": [Check("lint", [])]})
        with self.assertRaises(TypeError):
            build({**complete, CheckKind.TEST: [object()]})

    def test_a_verdict_is_a_bool_and_a_bounded_summary(self):
        with self.assertRaises(TypeError):
            CheckVerdict(1)
        self.assertEqual(len(CheckVerdict(True, "x" * 5000).summary), 2000)
