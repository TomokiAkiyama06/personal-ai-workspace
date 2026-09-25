"""What the orchestrator hands a running node: tools and a budget (PAW-034).

Both are the orchestrator's objects and both answer to the same :class:`RunGuard`,
which is what makes the acceptance conditions of Decisions 0006 and 0007 hold:

* **Never hand a tool call to a terminated task** (Decision 0006, section 9).
  The Tool Broker checks the task's state only for calls that need an approval;
  every other call trusts its ``TaskContext``. So the duty is the orchestrator's:
  :meth:`NodeToolGateway.call` asks the ``TaskActivityProvider`` (in production
  ``PostgresTaskActivity``: the current state and run, read from the database) for
  **every** call, immediately before it is handed to the runner, and raises
  ``NodeStopped`` unless the answer is ``ACTIVE``. An unknown answer, an error and
  a timeout are refusals too (fail closed).
* **The ``TaskContext`` is built here, per call** (Decision 0006, sections 8 and
  9): the run comes from the task snapshot (``TaskSnapshot.run`` / the Start event's
  ``run``), the delegator from the task, and the grant and the scope are derived
  from the parent's (``scope.py``) from **current** values (the caller's
  ``TaskAuthority`` is asked again for every call), so a narrowed grant, an
  archived project or a changed repository ACL takes effect on the very next call.
* **A node cannot exceed the parent's budget.** ``NodeBudgetHandle.charge`` records
  to the parent task's rows (there is no budget of a node) and raises
  ``NodeStopped`` for the node that crossed the limit and, through the guard, for
  every other node and tool call of the run.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from paw_backend.orchestrator.errors import NodeStopped, StopReason, error_class_of
from paw_backend.tasks import TaskRun
from paw_backend.tasks.queueing import (
    BudgetKind,
    BudgetNotConfiguredError,
    BudgetStatus,
    BudgetTracker,
)
from paw_backend.tools import (
    BudgetProvider,
    TaskActivity,
    TaskActivityProvider,
    TaskContext,
    ToolCall,
    ToolOutcome,
)
from paw_backend.tools import BudgetStatus as ToolBudgetStatus

logger = logging.getLogger(__name__)


class ToolCaller(Protocol):
    """What runs a tool call through the Broker: ``ToolRunner`` is one."""

    async def run(
        self, call: ToolCall, *, approval_id: uuid.UUID | None = None
    ) -> ToolOutcome: ...


class RunGuard:
    """The one place that says whether the run of a task may still act.

    ``stop`` records why it may not (the first reason stays). ``ensure_active``
    raises ``NodeStopped`` for a stopped guard and otherwise asks the activity
    provider about the task and the run.
    """

    def __init__(
        self, task_id: uuid.UUID, run: TaskRun, activity: TaskActivityProvider
    ) -> None:
        self._task_id = task_id
        self._run = run
        self._activity = activity
        self._stop_reason: StopReason | None = None

    @property
    def stop_reason(self) -> StopReason | None:
        return self._stop_reason

    def stop(self, reason: StopReason) -> None:
        if self._stop_reason is None:
            self._stop_reason = reason

    async def ensure_active(self) -> None:
        if self._stop_reason is not None:
            raise NodeStopped(self._stop_reason)
        try:
            activity = await self._activity.check(self._task_id, self._run)
        except Exception as error:  # fail closed: an unreadable task is not active
            logger.error("Task activity check failed (%s)", error_class_of(error))
            raise NodeStopped(StopReason.TASK_ENDED) from None
        if activity is TaskActivity.ACTIVE:
            return
        reason = (
            StopReason.SUPERSEDED
            if activity is TaskActivity.SUPERSEDED
            else StopReason.TASK_ENDED
        )
        self.stop(reason)
        raise NodeStopped(self._stop_reason or reason)


class NodeToolGateway:
    """The ``tools`` of a ``NodeAssignment``."""

    def __init__(
        self,
        guard: RunGuard,
        runner: ToolCaller,
        context_factory: Callable[[], Awaitable[TaskContext]],
    ) -> None:
        self._guard = guard
        self._runner = runner
        self._context_factory = context_factory

    async def call(
        self,
        tool: object,
        arguments: object,
        *,
        approval_id: uuid.UUID | None = None,
    ) -> ToolOutcome:
        # The context first (it reads current grants and scopes), the task check
        # last: the shorter the gap between the check and the hand-over, the
        # smaller the window in which the task can end unseen. For a call that
        # needs an approval the Broker closes that window itself (Decision 0006,
        # section 9).
        context = await self._context_factory()
        await self._guard.ensure_active()
        return await self._runner.run(
            ToolCall(tool, arguments, context), approval_id=approval_id
        )


class NodeBudgetHandle:
    """The ``budget`` of a ``NodeAssignment``: charges go to the parent task."""

    def __init__(
        self, guard: RunGuard, tracker: BudgetTracker, task_id: uuid.UUID
    ) -> None:
        self._guard = guard
        self._tracker = tracker
        self._task_id = task_id

    async def charge(self, kind: BudgetKind, amount: int) -> None:
        if self._guard.stop_reason is not None:
            raise NodeStopped(self._guard.stop_reason)
        # ``record`` validates the kind (runtime is measured by the tracker, not
        # reported) and the amount before anything is written.
        await self._tracker.record(self._task_id, kind, amount)
        verdict = await self._tracker.check(self._task_id)
        if verdict.status is BudgetStatus.EXCEEDED:
            self._guard.stop(StopReason.BUDGET_EXCEEDED)
            raise NodeStopped(StopReason.BUDGET_EXCEEDED)

    async def remaining(self) -> Mapping[BudgetKind, int | None]:
        usage = await self._tracker.usage(self._task_id)
        return {item.kind: item.remaining for item in usage}


class TrackerBudgetProvider(BudgetProvider):
    """The Tool Broker's ``BudgetProvider`` seam, backed by ``BudgetTracker``.

    ``check`` asks whether one more tool call fits (nothing is consumed) and
    ``charge`` records it; the two are separate statements, so two calls that check
    at the same moment can both pass and the limit is exceeded by at most the calls
    in flight (Decision 0006 and ``tools/budget.py``). A task with no budget is
    ``UNKNOWN``: a call that needs a budget is denied.
    """

    def __init__(self, tracker: BudgetTracker) -> None:
        self._tracker = tracker

    async def check(self, task_id: uuid.UUID, tool: str) -> ToolBudgetStatus:
        try:
            verdict = await self._tracker.check(
                task_id, planned={BudgetKind.TOOL_CALLS: 1}
            )
        except BudgetNotConfiguredError:
            return ToolBudgetStatus.UNKNOWN
        if verdict.status is BudgetStatus.EXCEEDED:
            return ToolBudgetStatus.EXCEEDED
        return ToolBudgetStatus.WITHIN_BUDGET

    async def charge(self, task_id: uuid.UUID, tool: str) -> None:
        await self._tracker.record(task_id, BudgetKind.TOOL_CALLS, 1)
