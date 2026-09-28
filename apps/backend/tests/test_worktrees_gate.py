"""The tests, the Evaluator and the review of the integrated result (PAW-035).

Skipped unless ``PAW_TEST_DATABASE_URL`` is set. Real tasks (``TaskService``) and
DAG store; the checks and the integration targets are fakes.
"""

import uuid

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
    ReviewStatus,
    TaskCommand,
    TaskState,
)

from .orchestrator_support import (
    FakeAuthority,
    PostgresOrchestratorTestCase,
    requires_postgres,
)

REPO = uuid.UUID(int=0x3536)


def target(head="a" * 40) -> IntegrationTarget:
    return IntegrationTarget(REPO, "/srv/w/_integration", "paw/t/1/_integration", head)


class FakeTargets:
    def __init__(self) -> None:
        self.current = (target(),)
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
        self.targets = FakeTargets()

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

    async def messages(self, task_id):
        return [
            log.message for log in (await self.service.restore(task_id)).recent_logs
        ]

    async def test_every_check_passes_in_order_and_the_task_completes(self):
        task_id = await self.evaluating_task()

        report = await self.gate().evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.COMPLETED)
        self.assertEqual(self.order, ["test", "evaluator", "review"])
        self.assertEqual([kind for kind, *_ in report.verdicts], list(CHECK_ORDER))
        for kind, check in self.checks.items():
            (request,) = check.requests
            self.assertEqual(request.kind, kind)
            self.assertEqual(request.targets, (target(),))
            self.assertEqual(request.task_id, task_id)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.COMPLETED)
        self.assertEqual(
            snapshot.attempt.review.evaluation_result, EvaluationResult.PASSED
        )
        self.assertEqual(snapshot.attempt.review.review_status, ReviewStatus.APPROVED)
        messages = await self.messages(task_id)
        self.assertIn("Integration check review passed", messages)
        # What a check said is returned, never stored.
        self.assertFalse(any("summary of" in message for message in messages))

    async def test_failed_tests_stop_before_the_evaluator_and_the_review(self):
        task_id = await self.evaluating_task()

        report = await self.gate(test=False).evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.FAILED)
        self.assertEqual(self.order, ["test"])
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.FAILED)
        self.assertEqual(
            snapshot.attempt.review.evaluation_result, EvaluationResult.FAILED
        )
        self.assertEqual(
            snapshot.attempt.review.review_status, ReviewStatus.NOT_STARTED
        )

    async def test_a_failed_evaluator_stops_before_the_review(self):
        task_id = await self.evaluating_task()
        report = await self.gate(evaluator=False).evaluate(task_id)
        self.assertEqual(report.outcome, GateOutcome.FAILED)
        self.assertEqual(self.order, ["test", "evaluator"])
        snapshot = await self.service.restore(task_id)
        self.assertEqual(
            snapshot.attempt.review.evaluation_result, EvaluationResult.FAILED
        )

    async def test_a_review_that_asks_for_changes_fails_the_task(self):
        task_id = await self.evaluating_task()
        report = await self.gate(review=False).evaluate(task_id)
        self.assertEqual(report.outcome, GateOutcome.FAILED)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.FAILED)
        self.assertEqual(
            snapshot.attempt.review.review_status, ReviewStatus.CHANGES_REQUESTED
        )
        self.assertEqual(
            snapshot.attempt.review.evaluation_result, EvaluationResult.PASSED
        )

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
            self.targets.current = (target("b" * 40),)

        self.checks[CheckKind.REVIEW].action = commit_meanwhile

        report = await gate.evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.CHANGED)
        self.assertEqual((await self.service.restore(task_id)).state, TaskState.FAILED)

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
