"""The tests, the Evaluator and the review of the integrated result (PAW-035).

Skipped unless ``PAW_TEST_DATABASE_URL`` is set. Real tasks (``TaskService``) and
DAG store; the checks and the integration targets are fakes.
"""

import uuid

from paw_backend.authz import Capability, RepoAcl
from paw_backend.integration import (
    CHECK_ORDER,
    CheckKind,
    CheckVerdict,
    GateOutcome,
    IntegrationGate,
    IntegrationTarget,
    Publication,
    PublishProblem,
    PullRequestNotPublishedError,
)
from paw_backend.integration.changes import (
    ChangedFile,
    ChangeRecorder,
    PullRequestChanges,
    PullRequestChangeStore,
)
from paw_backend.orchestrator.store import DagStore
from paw_backend.tasks import (
    EvaluationResult,
    PullRequestInfo,
    PullRequestState,
    RepoRole,
    ReviewStatus,
    TaskCommand,
    TaskState,
    WorkingSetEntry,
)
from paw_backend.tools import ScopedRepository

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

    async def test_a_result_is_not_recorded_for_a_task_cancelled_during_its_check(
        self,
    ):
        # Codex P2 on PR #130 (gate.py:244): a Cancel does not change the run, so
        # the run fence alone let a check that was still running write ``passed``
        # / ``approved`` into the cancelled task.
        for kind in (CheckKind.EVALUATOR, CheckKind.REVIEW):
            with self.subTest(kind=kind.value):
                task_id = await self.evaluating_task()
                before = await self.review_of(task_id)
                gate = self.gate()

                async def cancel(task_id=task_id):
                    await self.service.execute(
                        task_id, TaskCommand.CANCEL, actor=self.user
                    )

                self.checks[kind].action = cancel

                report = await gate.evaluate(task_id)

                self.assertEqual(report.outcome, GateOutcome.SUPERSEDED)
                review = await self.review_of(task_id)
                if kind is CheckKind.EVALUATOR:
                    self.assertEqual(review.evaluation_result, before.evaluation_result)
                else:
                    self.assertEqual(review.evaluation_result, EvaluationResult.PASSED)
                    self.assertEqual(review.review_status, ReviewStatus.IN_REVIEW)
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


def pull_request(number=1, state=PullRequestState.OPEN) -> PullRequestInfo:
    return PullRequestInfo(number, f"https://github.com/octo/repo/pull/{number}", state)


class FakePublisher:
    """Records what it is asked to publish; answers ``result`` (a
    ``PullRequestInfo``, or an exception to raise) for every repository, or
    ``results[repo_id]`` when given."""

    def __init__(self, log, result=None) -> None:
        self.log = log
        self.result = pull_request() if result is None else result
        self.results = {}
        self.requests = []
        self.action = None

    async def publish(self, request):
        self.requests.append(request)
        self.log.append("publish")
        if self.action is not None:
            await self.action()
        result = self.results.get(request.target.repo_id, self.result)
        if isinstance(result, Exception):
            raise result
        return result


class FakeChangeReader:
    """Answers one changed file (or raises ``error``); records what it read."""

    def __init__(self, log, error=None, state_of=None) -> None:
        self.log = log
        self.error = error
        self.calls = []
        self.state_of = state_of
        self.states = []

    async def read(self, request, pull_request):
        self.calls.append((request, pull_request))
        if self.state_of is not None:
            self.states.append(await self.state_of(request.task.id))
        self.log.append("changes")
        if self.error is not None:
            raise self.error
        return PullRequestChanges(
            request.target.head,
            (
                ChangedFile(
                    "src/app.py", None, "modified", 1, 1, patch="@@ -1 +1 @@\n-a\n+b\n"
                ),
            ),
            False,
        )


class ChangeRecorderStub:
    async def record(self, request, pull_request):
        return True


@requires_postgres
class PublishingGateTest(PostgresOrchestratorTestCase):
    """Issue #132: once every check passed, the gate has the pull request of each
    ``target`` made and recorded, and only then completes the task."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.order: list[str] = []
        self.repo = self.repository_id
        self.targets = FakeTargets(self.repo)
        self.publisher = FakePublisher(self.order)
        self.scoped = {self.repo: RepoRole.TARGET}

    def scoped_repository(self, repo, role):
        return ScopedRepository(
            repo,
            self.project_id,
            f"/srv/checkouts/{repo}",
            RepoAcl.inherit(repo, self.project_id),
            remotes=("https://github.com/octo/repo.git",),
            role=role,
        )

    def gate(self, changes=None, **verdicts) -> IntegrationGate:
        self.checks = {
            kind: Check(kind.value, self.order, verdicts.get(kind.value, True))
            for kind in CHECK_ORDER
        }
        return IntegrationGate(
            tasks=self.service,
            store=DagStore(self.database),
            authority=FakeAuthority(
                repositories=[
                    self.scoped_repository(repo, role)
                    for repo, role in self.scoped.items()
                ]
            ),
            worktrees=self.targets,
            checks={kind: [check] for kind, check in self.checks.items()},
            publisher=self.publisher,
            changes=changes,
        )

    async def snapshot(self, task_id):
        return await self.service.restore(task_id)

    async def messages(self, task_id):
        return [log.message for log in (await self.snapshot(task_id)).recent_logs]

    async def test_the_pull_request_is_made_after_the_review_and_completes_the_task(
        self,
    ):
        task_id = await self.task_in_state(TaskState.EVALUATING)

        report = await self.gate().evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.COMPLETED)
        self.assertEqual(self.order, ["test", "evaluator", "review", "publish"])
        self.assertEqual(report.publications, (Publication(self.repo, pull_request()),))
        (request,) = self.publisher.requests
        self.assertEqual(request.target, target(self.repo))
        self.assertEqual(request.repository.role, RepoRole.TARGET)
        self.assertEqual(request.checks, (("test", 1), ("evaluator", 1), ("review", 1)))
        self.assertEqual(request.run, (await self.snapshot(task_id)).run)
        snapshot = await self.snapshot(task_id)
        self.assertEqual(snapshot.state, TaskState.COMPLETED)
        self.assertEqual(
            snapshot.attempt.repository(self.repo).pull_request, pull_request()
        )
        self.assertIn(
            f"Pull request #1 of repository {self.repo} (open) at {'a' * 40}:"
            " https://github.com/octo/repo/pull/1",
            await self.messages(task_id),
        )

    async def test_the_changes_are_recorded_once_the_pull_request_is(self):
        # Issue #185 item 6: the PR screen's changed files, read when the pull
        # request was delivered and stored with its record.
        task_id = await self.task_in_state(TaskState.EVALUATING)

        async def state_of(task):
            return (await self.snapshot(task)).state

        reader = FakeChangeReader(self.order, state_of=state_of)
        recorder = ChangeRecorder(reader, PullRequestChangeStore(self.database))

        report = await self.gate(changes=recorder).evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.COMPLETED)
        # Read only once the task completed: never on its way (Codex review of
        # #206).
        self.assertEqual(reader.states, [TaskState.COMPLETED])
        self.assertEqual(
            self.order, ["test", "evaluator", "review", "publish", "changes"]
        )
        ((request, pull_request_read),) = reader.calls
        self.assertEqual(request, self.publisher.requests[0])
        self.assertEqual(pull_request_read, pull_request())
        (row,) = await self.rows(
            "SELECT c.head_commit, c.files, c.patches"
            " FROM pull_request_changes c"
            " JOIN task_attempt_repositories r ON r.id = c.record_id"
            " WHERE r.task_id = :t",
            t=task_id,
        )
        self.assertEqual(row["head_commit"], "a" * 40)
        self.assertEqual([item["path"] for item in row["files"]], ["src/app.py"])
        self.assertEqual(row["patches"], ["@@ -1 +1 @@\n-a\n+b\n"])

    async def test_changes_that_cannot_be_read_do_not_hold_the_task(self):
        task_id = await self.task_in_state(TaskState.EVALUATING)
        reader = FakeChangeReader(self.order, error=RuntimeError("gh is down"))
        recorder = ChangeRecorder(reader, PullRequestChangeStore(self.database))

        report = await self.gate(changes=recorder).evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.COMPLETED)
        self.assertEqual((await self.snapshot(task_id)).state, TaskState.COMPLETED)
        self.assertEqual(
            await self.rows(
                "SELECT c.record_id FROM pull_request_changes c"
                " JOIN task_attempt_repositories r ON r.id = c.record_id"
                " WHERE r.task_id = :t",
                t=task_id,
            ),
            [],
        )

    async def test_no_changes_are_read_for_a_pull_request_not_made(self):
        task_id = await self.task_in_state(TaskState.EVALUATING)
        self.publisher.result = PullRequestNotPublishedError(
            PublishProblem.GITHUB_FAILED
        )
        reader = FakeChangeReader(self.order)
        recorder = ChangeRecorder(reader, PullRequestChangeStore(self.database))

        report = await self.gate(changes=recorder).evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.NOT_PUBLISHED)
        self.assertEqual(reader.calls, [])

    def test_changes_need_a_publisher_and_an_async_record(self):
        for publisher, changes in (
            (None, ChangeRecorderStub()),
            (FakePublisher([]), object()),
        ):
            with self.assertRaises(TypeError):
                IntegrationGate(
                    tasks=self.service,
                    store=DagStore(self.database),
                    authority=FakeAuthority(),
                    worktrees=self.targets,
                    checks={kind: [Check(kind.value, [])] for kind in CHECK_ORDER},
                    publisher=publisher,
                    changes=changes,
                )

    async def test_nothing_is_published_when_a_check_did_not_pass(self):
        for kind in CHECK_ORDER:
            with self.subTest(failed=kind.value):
                self.order.clear()
                task_id = await self.task_in_state(TaskState.EVALUATING)
                report = await self.gate(**{kind.value: False}).evaluate(task_id)
                self.assertEqual(report.outcome, GateOutcome.FAILED)
                self.assertNotIn("publish", self.order)
        self.assertEqual(self.publisher.requests, [])

    async def test_nothing_is_published_when_the_result_changed_or_is_dirty(self):
        task_id = await self.task_in_state(TaskState.EVALUATING)
        gate = self.gate()

        async def commit_meanwhile():
            self.targets.current = (target(self.repo, "b" * 40),)

        self.checks[CheckKind.REVIEW].action = commit_meanwhile
        self.assertEqual((await gate.evaluate(task_id)).outcome, GateOutcome.CHANGED)

        task_id = await self.task_in_state(TaskState.EVALUATING)
        self.targets.current = (target(self.repo, clean=False),)
        self.assertEqual(
            (await self.gate().evaluate(task_id)).outcome, GateOutcome.DIRTY
        )
        self.assertEqual(self.publisher.requests, [])

    async def test_a_pull_request_that_was_not_made_keeps_the_task_evaluating(self):
        task_id = await self.task_in_state(TaskState.EVALUATING)
        self.publisher.result = PullRequestNotPublishedError(
            PublishProblem.NOT_AUTHORIZED
        )

        report = await self.gate().evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.NOT_PUBLISHED)
        self.assertEqual(
            report.publications,
            (Publication(self.repo, problem=PublishProblem.NOT_AUTHORIZED),),
        )
        snapshot = await self.snapshot(task_id)
        self.assertEqual(snapshot.state, TaskState.EVALUATING)
        state = snapshot.attempt.repository(self.repo)
        self.assertIsNone(state.pull_request)
        self.assertEqual(state.review.evaluation_result, EvaluationResult.PASSED)
        self.assertEqual(state.review.review_status, ReviewStatus.APPROVED)
        self.assertIn(
            f"Pull request of repository {self.repo} not made (not_authorized)",
            await self.messages(task_id),
        )
        # Once it can be made, the gate completes the task.
        self.publisher.result = pull_request()
        report = await self.gate().evaluate(task_id)
        self.assertEqual(report.outcome, GateOutcome.COMPLETED)

    async def test_a_publisher_that_breaks_made_nothing(self):
        task_id = await self.task_in_state(TaskState.EVALUATING)
        self.publisher.result = RuntimeError("secret-ish")

        report = await self.gate().evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.NOT_PUBLISHED)
        self.assertEqual(report.publications[0].problem, PublishProblem.GITHUB_FAILED)
        self.assertFalse(any("secret-ish" in m for m in await self.messages(task_id)))
        self.publisher.result = "not a pull request"
        report = await self.gate().evaluate(task_id)
        self.assertEqual(report.outcome, GateOutcome.NOT_PUBLISHED)
        self.assertEqual((await self.snapshot(task_id)).state, TaskState.EVALUATING)

    async def test_a_pull_request_that_is_not_delivered_does_not_complete(self):
        # Decision 0030 (section 5): a draft or a closed pull request is recorded
        # as it is, and Complete refuses it.
        for state in (PullRequestState.DRAFT, PullRequestState.CLOSED):
            with self.subTest(state=state.value):
                task_id = await self.task_in_state(TaskState.EVALUATING)
                self.publisher.result = pull_request(4, state)

                report = await self.gate().evaluate(task_id)

                self.assertEqual(report.outcome, GateOutcome.REQUIREMENTS_NOT_MET)
                snapshot = await self.snapshot(task_id)
                self.assertEqual(snapshot.state, TaskState.EVALUATING)
                self.assertEqual(
                    snapshot.attempt.repository(self.repo).pull_request,
                    pull_request(4, state),
                )

    async def test_only_target_repositories_are_published(self):
        working = uuid.uuid4()
        task_id = await self.create_task(
            repositories=[
                WorkingSetEntry(self.repo, RepoRole.TARGET, BASELINE),
                WorkingSetEntry(working, RepoRole.WORKING, BASELINE),
            ]
        )
        await self.service.execute(task_id, TaskCommand.START, actor=self.system)
        await self.service.execute(
            task_id, TaskCommand.BEGIN_EVALUATION, actor=self.system
        )
        self.targets = FakeTargets(self.repo, working)
        self.scoped = {self.repo: RepoRole.TARGET, working: RepoRole.WORKING}

        report = await self.gate().evaluate(task_id)

        self.assertEqual(
            [request.target.repo_id for request in self.publisher.requests],
            [self.repo],
        )
        self.assertEqual(report.outcome, GateOutcome.COMPLETED)
        snapshot = await self.snapshot(task_id)
        self.assertIsNone(snapshot.attempt.repository(working).pull_request)

    async def test_a_target_left_out_of_the_scope_now_gets_no_pull_request(self):
        # The authority leaves out a repository the creator cannot work on now
        # (no ready checkout, its project Deleted): nothing is published for it,
        # and Complete refuses the target without a pull request.
        task_id = await self.task_in_state(TaskState.EVALUATING)
        self.scoped = {}

        report = await self.gate().evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.REQUIREMENTS_NOT_MET)
        self.assertEqual(self.publisher.requests, [])

    async def test_the_scope_is_read_again_before_publishing(self):
        # Codex review of #159: the checks may run long; an archived project,
        # a narrowed ACL, a removed remote or role since the scope was read
        # for the checks must decide the push, not the old scope.
        task_id = await self.task_in_state(TaskState.EVALUATING)
        gate = self.gate()
        authority = gate._authority

        async def archive_meanwhile():
            archived = authority.repositories[0]
            authority.repositories = (
                ScopedRepository(
                    archived.repo_id,
                    archived.project_id,
                    archived.root,
                    RepoAcl.override(archived.repo_id, self.project_id, ()),
                    remotes=(),
                    role=RepoRole.TARGET,
                ),
            )

        self.checks[CheckKind.REVIEW].action = archive_meanwhile

        await gate.evaluate(task_id)

        (request,) = self.publisher.requests
        self.assertEqual(request.repository.remotes, ())
        self.assertEqual(request.repository.acl.allowed, frozenset())

    async def test_the_scope_is_read_again_before_each_target(self):
        # Codex review of #159: publishing one target may take long; what
        # changed meanwhile about the next one decides its push.
        second = uuid.uuid4()
        task_id = await self.create_task(
            repositories=[
                WorkingSetEntry(self.repo, RepoRole.TARGET, BASELINE),
                WorkingSetEntry(second, RepoRole.TARGET, BASELINE),
            ]
        )
        await self.service.execute(task_id, TaskCommand.START, actor=self.system)
        await self.service.execute(
            task_id, TaskCommand.BEGIN_EVALUATION, actor=self.system
        )
        self.targets = FakeTargets(self.repo, second)
        self.scoped = {self.repo: RepoRole.TARGET, second: RepoRole.TARGET}
        gate = self.gate()
        authority = gate._authority

        async def narrow_the_second():
            authority.repositories = (
                authority.repositories[0],
                self.scoped_repository(second, RepoRole.WORKING),
            )

        self.publisher.action = narrow_the_second

        await gate.evaluate(task_id)

        self.assertEqual(
            [request.target.repo_id for request in self.publisher.requests],
            [self.repo],
        )

    async def test_a_task_that_moved_on_while_the_scope_was_read_is_not_published(
        self,
    ):
        # Codex review of #159: the task is cancelled while the scope is read
        # again for publishing: nothing is pushed for the old run.
        task_id = await self.task_in_state(TaskState.EVALUATING)
        gate = self.gate()
        authority = gate._authority
        read = authority.parent_scope

        async def cancel_while_reading(task):
            scope = await read(task)
            if authority.scope_calls > 1:  # the read before publishing
                await self.service.execute(task_id, TaskCommand.CANCEL, actor=self.user)
            return scope

        authority.parent_scope = cancel_while_reading

        report = await gate.evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.SUPERSEDED)
        self.assertEqual(self.publisher.requests, [])
        self.assertEqual((await self.snapshot(task_id)).state, TaskState.CANCELLED)

    async def test_a_target_removed_while_checking_gets_no_pull_request(self):
        task_id = await self.task_in_state(TaskState.EVALUATING)
        gate = self.gate()
        authority = gate._authority

        async def remove_meanwhile():
            authority.repositories = ()

        self.checks[CheckKind.REVIEW].action = remove_meanwhile

        report = await gate.evaluate(task_id)

        self.assertEqual(self.publisher.requests, [])
        self.assertEqual(report.outcome, GateOutcome.REQUIREMENTS_NOT_MET)

    async def test_a_task_that_moved_on_while_publishing_is_left_alone(self):
        task_id = await self.task_in_state(TaskState.EVALUATING)

        async def cancel():
            await self.service.execute(task_id, TaskCommand.CANCEL, actor=self.user)

        self.publisher.action = cancel

        report = await self.gate().evaluate(task_id)

        self.assertEqual(report.outcome, GateOutcome.SUPERSEDED)
        snapshot = await self.snapshot(task_id)
        self.assertEqual(snapshot.state, TaskState.CANCELLED)

    async def test_without_a_publisher_nothing_changes(self):
        # The gate of PAW-035 (no publisher): the task waits for a pull request
        # recorded by someone else.
        task_id = await self.task_in_state(TaskState.EVALUATING)
        self.checks = {kind: Check(kind.value, self.order) for kind in CHECK_ORDER}
        gate = IntegrationGate(
            tasks=self.service,
            store=DagStore(self.database),
            authority=FakeAuthority(
                repositories=[self.scoped_repository(self.repo, RepoRole.TARGET)]
            ),
            worktrees=self.targets,
            checks={kind: [check] for kind, check in self.checks.items()},
        )
        report = await gate.evaluate(task_id)
        self.assertEqual(report.outcome, GateOutcome.REQUIREMENTS_NOT_MET)
        self.assertEqual(report.publications, ())

    def test_a_publisher_must_have_an_async_publish(self):
        with self.assertRaises(TypeError):
            IntegrationGate(
                tasks=self.service,
                store=DagStore(self.database),
                authority=FakeAuthority(),
                worktrees=self.targets,
                checks={kind: [Check(kind.value, [])] for kind in CHECK_ORDER},
                publisher=object(),
            )
