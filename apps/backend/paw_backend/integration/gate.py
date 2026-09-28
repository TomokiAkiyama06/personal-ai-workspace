"""The tests, the Evaluator and the review of the integrated result (PAW-035).

``REQUIREMENTS.md`` ("Repository isolation", "Review independence") orders the
work after the Worker nodes as::

    Worker(s) -> Integration -> Executable Evaluator -> Reviewer
              -> Merge Ready -> Human Merge

The orchestrator integrates the Worker branches and puts the task in
``evaluating`` (``Orchestrator._integrate``). :class:`IntegrationGate` is what
runs while the task is evaluating: it hands the **integration worktrees** (never
a single Worker's branch, never the user's checkout) to the checks of each kind,
in the fixed order :data:`CHECK_ORDER` (``test`` -> ``evaluator`` -> ``review``),
and stops at the first check that does not pass. It records the outcome on the
task attempt (``ReviewState``: ``evaluation_result`` after the tests and the
Evaluator, ``review_status`` for the review) and then completes the task (every
check passed: the result is ready for the human's merge decision; nothing is
merged or pushed) or fails it.

The checks themselves are other issues (PAW-011 / PAW-013 the Evaluator, the
Codex / Claude reviewers): a check is any object with ``async check(request) ->
CheckVerdict``. The gate is not the one that decides independence (a reviewer
that is not the implementing agent is the configuration's), and it never stores
what a check said: only which kind passed or failed (a verdict's text could hold
anything the check read).

Guarantees:

* **Only the run that was evaluated is completed.** The task command is fenced by
  the run the gate read and by the version it read just before (as
  ``Orchestrator._end_task``): a task retried or restarted meanwhile is left alone.
* **Only what was checked is completed.** The checks read the integration
  worktrees, so a worktree must be exactly its commit: one with uncommitted or
  untracked changes (or a merge in progress) is not checked at all (``DIRTY``:
  the task fails). The integration branches and the worktrees' state are read
  again after the last check; a branch that moved (a human or an agent committed
  to it meanwhile) or a worktree that was written to fails the task instead of
  completing an unchecked commit (``CHANGED``). The commit of every repository
  that completes is written to the task log (Merge Ready is that commit).
* **Every kind must be configured.** A gate without a test, an Evaluator or a
  review check cannot be built (``TypeError``): skipping a kind is not a
  configuration.
"""

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from paw_backend.integration.coordinator import (
    GitWorktreeCoordinator,
    IntegrationTarget,
)
from paw_backend.orchestrator.domain import DagState, NodeRole, NodeState
from paw_backend.orchestrator.errors import error_class_of
from paw_backend.orchestrator.orchestrator import TaskAuthority
from paw_backend.orchestrator.store import DagStore
from paw_backend.orchestrator.workspaces import IntegrationRequest
from paw_backend.tasks import (
    Actor,
    EvaluationResult,
    IllegalTransitionError,
    LogLevel,
    ReviewState,
    ReviewStatus,
    StaleRunError,
    TaskCommand,
    TaskConflictError,
    TaskNotFoundError,
    TaskRun,
    TaskService,
    TaskSnapshot,
    TaskState,
)
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger(__name__)

REASON_PASSED = "The integrated result passed the tests, the Evaluator and the review"
REASON_CHECK_FAILED = "A check of the integrated result did not pass"
REASON_CHANGED = "The integrated result changed while it was checked"
REASON_DIRTY = "The integrated result has uncommitted changes"
COMMAND_ATTEMPTS = 5
MAX_SUMMARY_CHARS = 2000


class CheckKind(StrEnum):
    TEST = "test"  # build / tests on the integration worktree
    EVALUATOR = "evaluator"  # the executable Evaluator (acceptance, regressions)
    REVIEW = "review"  # an independent reviewer (Codex / Claude / ...)


CHECK_ORDER = (CheckKind.TEST, CheckKind.EVALUATOR, CheckKind.REVIEW)


@dataclass(frozen=True, slots=True)
class CheckRequest:
    """What one check is given: where the integrated result of each repository
    is (the integration worktree, its branch and the commit to check)."""

    task_id: uuid.UUID
    run: TaskRun
    kind: CheckKind
    targets: tuple[IntegrationTarget, ...]


@dataclass(frozen=True, slots=True)
class CheckVerdict:
    """``passed`` decides; ``summary`` is for the caller of the gate only
    (returned in the report, never stored or logged)."""

    passed: bool
    summary: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if type(self.passed) is not bool:
            raise TypeError("passed must be a bool")
        if not isinstance(self.summary, str):
            raise TypeError("summary must be a str")
        object.__setattr__(self, "summary", self.summary[:MAX_SUMMARY_CHARS])


class GateOutcome(StrEnum):
    COMPLETED = "completed"  # every check passed: the task completed (merge ready)
    FAILED = "failed"  # a check did not pass: the task failed
    CHANGED = "changed"  # an integration branch moved during the checks: failed
    # An integration worktree is not its commit (uncommitted / untracked changes,
    # a merge in progress): nothing was checked, failed.
    DIRTY = "dirty"
    NOT_EVALUATING = "not_evaluating"  # the task is not evaluating: nothing done
    SUPERSEDED = "superseded"  # the task moved on under the gate: nothing written


@dataclass(frozen=True, slots=True)
class GateReport:
    outcome: GateOutcome
    task_id: uuid.UUID
    # ``(kind, passed, summary)`` of every check that ran, in order.
    verdicts: tuple[tuple[CheckKind, bool, str], ...] = ()
    targets: tuple[IntegrationTarget, ...] = ()


class IntegrationGate:
    def __init__(
        self,
        *,
        tasks: TaskService,
        store: DagStore,
        authority: TaskAuthority,
        worktrees: GitWorktreeCoordinator,
        checks: Mapping[CheckKind, Sequence[object]],
    ) -> None:
        if not isinstance(tasks, TaskService):
            raise TypeError("tasks must be a TaskService")
        if not isinstance(store, DagStore):
            raise TypeError("store must be a DagStore")
        require_async_method(authority, "parent_scope", 1)
        require_async_method(worktrees, "targets", 1)
        if not isinstance(checks, Mapping):
            raise TypeError("checks must map each CheckKind to its checks")
        ordered: dict[CheckKind, tuple[object, ...]] = {}
        for kind in CHECK_ORDER:
            configured = tuple(checks.get(kind, ()))
            if not configured:
                raise TypeError(f"a {kind.value} check is required")
            for check in configured:
                require_async_method(check, "check", 1)
            ordered[kind] = configured
        if set(checks) - set(CHECK_ORDER):
            raise TypeError("checks must map each CheckKind to its checks")
        self._tasks = tasks
        self._store = store
        self._authority = authority
        self._worktrees = worktrees
        self._checks = ordered

    async def evaluate(self, task_id: uuid.UUID) -> GateReport:
        """Run every check on the task's integrated result and complete or fail
        the task (see the module docstring)."""
        if not isinstance(task_id, uuid.UUID):
            raise TypeError("task_id must be a UUID")
        task = await self._tasks.restore(task_id, log_limit=0)
        if task.state is not TaskState.EVALUATING:
            return GateReport(GateOutcome.NOT_EVALUATING, task_id)
        run = task.run
        request = await self._request(task)
        targets = await self._worktrees.targets(request)
        verdicts: list[tuple[CheckKind, bool, str]] = []
        if not all(target.clean for target in targets):
            return await self._end(task, run, GateOutcome.DIRTY, verdicts, targets)
        try:
            for kind in CHECK_ORDER:
                if not await self._still_evaluating(task_id, run):
                    return GateReport(
                        GateOutcome.SUPERSEDED, task_id, tuple(verdicts), targets
                    )
                if kind is CheckKind.REVIEW:
                    await self._record(task, run, review=ReviewStatus.IN_REVIEW)
                passed = await self._run_kind(task, run, kind, targets, verdicts)
                if kind is CheckKind.EVALUATOR or (
                    kind is CheckKind.TEST and not passed
                ):
                    await self._record(
                        task,
                        run,
                        evaluation=EvaluationResult.PASSED
                        if passed
                        else EvaluationResult.FAILED,
                    )
                if kind is CheckKind.REVIEW:
                    await self._record(
                        task,
                        run,
                        review=ReviewStatus.APPROVED
                        if passed
                        else ReviewStatus.CHANGES_REQUESTED,
                    )
                if not passed:
                    return await self._end(
                        task, run, GateOutcome.FAILED, verdicts, targets
                    )
        except StaleRunError:
            return GateReport(GateOutcome.SUPERSEDED, task_id, tuple(verdicts), targets)
        if await self._worktrees.targets(request) != targets:
            return await self._end(task, run, GateOutcome.CHANGED, verdicts, targets)
        try:
            for target in targets:
                await self._log(
                    task,
                    run,
                    f"Integration of repository {target.repo_id}"
                    f" checked at {target.head}",
                )
        except StaleRunError:
            return GateReport(GateOutcome.SUPERSEDED, task_id, tuple(verdicts), targets)
        return await self._end(task, run, GateOutcome.COMPLETED, verdicts, targets)

    async def _still_evaluating(self, task_id: uuid.UUID, run: TaskRun) -> bool:
        """The next kind of check runs only while the task is still evaluating
        in the run that was read (a Cancel or a Retry meanwhile stops the gate)."""
        try:
            snapshot = await self._tasks.restore(task_id, log_limit=0)
        except TaskNotFoundError:
            return False
        return snapshot.state is TaskState.EVALUATING and snapshot.run == run

    async def _request(self, task: TaskSnapshot) -> IntegrationRequest:
        dag = await self._store.get(task.id, task.run.attempt)
        workers: tuple[str, ...] = ()
        if dag is not None and dag.state is DagState.SUCCEEDED:
            workers = tuple(
                node.key
                for node in dag.nodes
                if node.role is NodeRole.WORKER and node.state is NodeState.SUCCEEDED
            )
        scope = await self._authority.parent_scope(task)
        return IntegrationRequest(task=task, run=task.run, workers=workers, scope=scope)

    async def _run_kind(
        self,
        task: TaskSnapshot,
        run: TaskRun,
        kind: CheckKind,
        targets: tuple[IntegrationTarget, ...],
        verdicts: list[tuple[CheckKind, bool, str]],
    ) -> bool:
        request = CheckRequest(task.id, run, kind, targets)
        for check in self._checks[kind]:
            try:
                verdict = await check.check(request)
                if not isinstance(verdict, CheckVerdict):
                    raise TypeError("a check must return a CheckVerdict")
            except Exception as error:
                # A check that breaks did not pass; what it raised is not stored.
                logger.warning(
                    "Integration check %s failed (%s)",
                    kind.value,
                    error_class_of(error),
                )
                verdict = CheckVerdict(False)
            verdicts.append((kind, verdict.passed, verdict.summary))
            word = "passed" if verdict.passed else "did not pass"
            await self._log(task, run, f"Integration check {kind.value} {word}")
            if not verdict.passed:
                return False
        return True

    async def _record(
        self,
        task: TaskSnapshot,
        run: TaskRun,
        *,
        evaluation: EvaluationResult | None = None,
        review: ReviewStatus | None = None,
    ) -> None:
        """Change one field of the attempt's ``ReviewState``: ``evaluation_result``
        once the tests failed or the Evaluator ran, ``review_status`` for the
        review. Fenced by the run (``StaleRunError`` after a Retry / Restart)."""
        current = (await self._tasks.restore(task.id, log_limit=0)).attempt.review
        await self._tasks.update_attempt(
            task.id,
            run=run,
            review=ReviewState(
                current.review_status if review is None else review,
                current.evaluation_result if evaluation is None else evaluation,
            ),
        )

    async def _log(self, task: TaskSnapshot, run: TaskRun, message: str) -> None:
        try:
            await self._tasks.add_log(task.id, message, run=run, level=LogLevel.INFO)
        except StaleRunError:
            raise
        except Exception as error:
            logger.warning("Task log failed (%s)", error_class_of(error))

    async def _end(
        self,
        task: TaskSnapshot,
        run: TaskRun,
        outcome: GateOutcome,
        verdicts: list[tuple[CheckKind, bool, str]],
        targets: tuple[IntegrationTarget, ...],
    ) -> GateReport:
        command, reason = {
            GateOutcome.COMPLETED: (TaskCommand.COMPLETE, REASON_PASSED),
            GateOutcome.FAILED: (TaskCommand.FAIL, REASON_CHECK_FAILED),
            GateOutcome.CHANGED: (TaskCommand.FAIL, REASON_CHANGED),
            GateOutcome.DIRTY: (TaskCommand.FAIL, REASON_DIRTY),
        }[outcome]
        done = await self._command(task.id, run, command, reason)
        return GateReport(
            outcome if done else GateOutcome.SUPERSEDED,
            task.id,
            tuple(verdicts),
            targets,
        )

    async def _command(
        self, task_id: uuid.UUID, run: TaskRun, command: TaskCommand, reason: str
    ) -> bool:
        """``command`` for ``run`` only, fenced by the version read just before."""
        for _ in range(COMMAND_ATTEMPTS):
            try:
                snapshot = await self._tasks.restore(task_id, log_limit=0)
            except TaskNotFoundError:
                return False
            if snapshot.run != run or snapshot.state is not TaskState.EVALUATING:
                return False
            try:
                await self._tasks.execute(
                    task_id,
                    command,
                    actor=Actor.system(),
                    reason=reason,
                    expected_version=snapshot.version,
                )
            except IllegalTransitionError:
                return False
            except TaskConflictError:
                continue
            return True
        raise TaskConflictError()
