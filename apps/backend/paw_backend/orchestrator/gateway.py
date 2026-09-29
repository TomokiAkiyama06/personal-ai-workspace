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
* **A worker that lost its queue lease runs no tool** (issue #126, Decision
  0046). The ``TaskContext`` carries the worker's lease (``TaskContext.lease``) and
  the Broker checks it for every call (:class:`QueueLeaseVerifier`, the database's
  answer). A call refused with ``lease_lost`` stops the run here as well
  (``StopReason.LEASE_LOST``): no other node of the run hands over another call,
  and the orchestrator ends the run as it does when its heartbeats lose the lease.
* **A node cannot exceed the parent's budget.** ``NodeBudgetHandle.charge`` records
  to the parent task's rows (there is no budget of a node) and raises
  ``NodeStopped`` for the node that crossed the limit and, through the guard, for
  every other node and tool call of the run.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import NoReturn, Protocol

from paw_backend.orchestrator.errors import NodeStopped, StopReason, error_class_of
from paw_backend.tasks import StaleRunError, TaskRun
from paw_backend.tasks.queueing import (
    BudgetKind,
    BudgetNotConfiguredError,
    BudgetStatus,
    BudgetTracker,
    InvalidQueueingArgumentError,
    QueueLease,
    TaskQueue,
)
from paw_backend.tools import (
    BrokerReason,
    BudgetProvider,
    LeaseStatus,
    LeaseVerifier,
    TaskActivity,
    TaskActivityProvider,
    TaskContext,
    ToolCall,
    ToolOutcome,
    Verdict,
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
        self._on_lease_lost: list[Callable[[], None]] = []

    @property
    def stop_reason(self) -> StopReason | None:
        return self._stop_reason

    @property
    def run(self) -> TaskRun:
        """The run this guard speaks for (what a fenced write presents)."""
        return self._run

    async def refused(self) -> NoReturn:
        """A fenced write for this run was refused (``StaleRunError``: the run was
        replaced or the task ended). Stop the run for the reason the task gives
        now, and raise ``NodeStopped``."""
        await self.ensure_active()
        # The task looks active again (read after the refusal): the run the write
        # was refused for is not the one that acts now, so it is stopped anyway.
        self.stop(StopReason.SUPERSEDED)
        raise NodeStopped(self._stop_reason or StopReason.SUPERSEDED)

    def on_lease_lost(self, callback: Callable[[], None]) -> None:
        """Call ``callback`` (synchronously, no arguments) whenever the run is
        stopped for a lost lease. The orchestrator wakes its run loop with it, so
        that a loss the Broker read (``NodeToolGateway``) ends the run and its
        other nodes at once, as a loss the heartbeats found does, and not only at
        the next heartbeat, poll or node timeout (Codex review of PR #144)."""
        self._on_lease_lost.append(callback)

    def stop(self, reason: StopReason) -> None:
        if self._stop_reason is None:
            self._stop_reason = reason
        if reason is StopReason.LEASE_LOST:
            for callback in self._on_lease_lost:
                callback()

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


class AttemptFence:
    """Closes the tools and the budget of ONE attempt once the orchestrator no
    longer waits for it.

    An attempt whose runtime ignores its cancellation (a timeout, a Cancel, a
    budget stop) is abandoned after a bounded wait so that the node, the task and
    the queue lease are not held by it. Its coroutine may still be running; the
    fence makes every later tool call and budget charge of that attempt raise
    ``NodeStopped(StopReason.ABANDONED)``, so an abandoned runtime cannot act for
    the task any more. The orchestrator closes the fence whenever it stops waiting
    for an attempt (also after a normal end: nothing may act after its outcome).
    """

    __slots__ = ("_closed",)

    def __init__(self) -> None:
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True

    def ensure_open(self) -> None:
        if self._closed:
            raise NodeStopped(StopReason.ABANDONED)


class NodeToolGateway:
    """The ``tools`` of a ``NodeAssignment``."""

    def __init__(
        self,
        guard: RunGuard,
        runner: ToolCaller,
        context_factory: Callable[[], Awaitable[TaskContext]],
        fence: AttemptFence | None = None,
    ) -> None:
        self._guard = guard
        self._runner = runner
        self._context_factory = context_factory
        self._fence = fence or AttemptFence()

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
        self._fence.ensure_open()
        context = await self._context_factory()
        await self._guard.ensure_active()
        self._fence.ensure_open()
        outcome = await self._runner.run(
            ToolCall(tool, arguments, context), approval_id=approval_id
        )
        if (
            isinstance(outcome, ToolOutcome)
            and outcome.decision.verdict is Verdict.DENY
            and outcome.decision.reason is BrokerReason.LEASE_LOST
        ):
            # The Broker read that this worker's lease is gone (lost, expired or
            # taken over): the whole run stops, not only this call. (A lease that
            # could not be read, ``lease_unavailable``, refuses the call only; the
            # heartbeats decide about the lease.)
            self._guard.stop(StopReason.LEASE_LOST)
            raise NodeStopped(StopReason.LEASE_LOST)
        return outcome


# What a node may report through ``NodeBudgetHandle.charge``.
NODE_CHARGEABLE_KINDS = frozenset({BudgetKind.TOKENS, BudgetKind.GPU_SECONDS})


class NodeBudgetHandle:
    """The ``budget`` of a ``NodeAssignment``: charges go to the parent task."""

    def __init__(
        self,
        guard: RunGuard,
        tracker: BudgetTracker,
        task_id: uuid.UUID,
        fence: AttemptFence | None = None,
    ) -> None:
        self._guard = guard
        self._tracker = tracker
        self._task_id = task_id
        self._fence = fence or AttemptFence()

    async def charge(self, kind: BudgetKind, amount: int) -> None:
        # Only what a runtime measures itself: steps and retries are counted by
        # the orchestrator, tool calls by the Broker, the runtime by the timer. A
        # node that reports one of those would spend (or forge) the counters the
        # orchestrator decides by; it is refused before anything is written.
        # (The tracker's own error for a kind it does not take.)
        if not isinstance(kind, BudgetKind) or kind not in NODE_CHARGEABLE_KINDS:
            raise InvalidQueueingArgumentError("kind")
        # An abandoned attempt's charge is refused (nothing is recorded): the
        # orchestrator no longer counts that attempt's work.
        self._fence.ensure_open()
        if self._guard.stop_reason is not None:
            raise NodeStopped(self._guard.stop_reason)
        # ``record`` validates the kind (runtime is measured by the tracker, not
        # reported) and the amount before anything is written. The charge is
        # fenced by the run (``run=``): the tracker checks the task's current run
        # under the task row's share lock in the transaction of the increment, so
        # a run that a Fail + Retry / Restart replaced since the last look spends
        # nothing of the run that took over (the usage is kept per task).
        try:
            await self._tracker.record(self._task_id, kind, amount, run=self._guard.run)
        except StaleRunError:
            await self._guard.refused()
        verdict = await self._tracker.check(self._task_id)
        if verdict.status is BudgetStatus.EXCEEDED:
            self._guard.stop(StopReason.BUDGET_EXCEEDED)
            raise NodeStopped(StopReason.BUDGET_EXCEEDED)

    async def remaining(self) -> Mapping[BudgetKind, int | None]:
        # The same gates as ``charge``: an abandoned attempt, or a run that was
        # told to stop, learns nothing more about the task's budget.
        self._fence.ensure_open()
        if self._guard.stop_reason is not None:
            raise NodeStopped(self._guard.stop_reason)
        usage = await self._tracker.usage(self._task_id)
        return {item.kind: item.remaining for item in usage}


class TrackerBudgetProvider(BudgetProvider):
    """The Tool Broker's ``BudgetProvider`` seam, backed by ``BudgetTracker``.

    ``check`` asks whether one more tool call fits (nothing is consumed) and
    ``charge`` records it; the two are separate statements, so two calls that check
    at the same moment can both pass and the limit is exceeded by at most the calls
    in flight (Decision 0006 and ``tools/budget.py``). A task with no budget is
    ``UNKNOWN``: a call that needs a budget is denied.

    ``charge`` is fenced by the run the call ran for (issue #126, Decision 0046;
    as ``NodeBudgetHandle.charge``): the tracker checks the task's current run
    under the task row's share lock in the transaction of the increment. A call
    whose run was replaced (Fail + Retry / Restart) or whose task ended while it
    ran is not charged (``StaleRunError``, logged; its audit row says it ran), so
    it spends nothing of the run that took over. Only calls that were already in
    flight can be affected: the gateway and the Broker refuse every new one.
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

    async def charge(self, task_id: uuid.UUID, run: TaskRun, tool: str) -> None:
        try:
            await self._tracker.record(task_id, BudgetKind.TOOL_CALLS, 1, run=run)
        except StaleRunError:
            # Not a failure of the store: the run the call ran for is no longer
            # the task's (or the task ended). Nothing is charged to its successor.
            logger.info("A tool call of a replaced or ended run was not charged")


class QueueLeaseVerifier(LeaseVerifier):
    """The Tool Broker's ``LeaseVerifier`` seam, backed by the ``TaskQueue``.

    ``check`` asks the database whether the lease is valid now
    (``TaskQueue.holds_lease``: the entry belongs to the task, is claimed by the
    worker with that claim generation, and has not expired by the database
    clock). A read at one instant, no lock, nothing changed; an error propagates
    and the Broker refuses the call (``lease_unavailable``).
    """

    def __init__(self, queue: TaskQueue) -> None:
        if not isinstance(queue, TaskQueue):
            raise TypeError("queue must be a TaskQueue")
        self._queue = queue

    async def check(self, task_id: uuid.UUID, lease: QueueLease) -> LeaseStatus:
        held = await self._queue.holds_lease(task_id, lease)
        return LeaseStatus.HELD if held else LeaseStatus.LOST
