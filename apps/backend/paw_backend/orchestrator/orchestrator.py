"""The DAG Agent Orchestrator (PAW-034).

``run_entry`` takes one claimed queue entry and drives the task it names to the end
of what the orchestrator can do for it: decompose it into a dependency DAG (the
planner role proposes, ``plan.py`` accepts), run the independent nodes in parallel
(``scheduling.py``), retry, change approach or escalate a failing node, pass the
structured results of finished nodes to the nodes that depend on them, and report
the outcome to the task (``evaluating``, ``failed`` or ``waiting``). What the
orchestrator does **not** do: run an agent (an ``AgentRuntime`` does), integrate
worktrees (PAW-035), evaluate or complete the task (the Evaluator), choose the
real parallelism (PAW-036) or persist the working set (issue #85).

The duties that earlier Decisions gave to "the orchestrator" and where each is met
--------------------------------------------------------------------------------
* **Only the lease holder calls ``BudgetTracker.start_runtime``** (Decision 0007,
  10): ``_run_entry`` proves the lease with a heartbeat immediately before, and a
  worker whose heartbeat says the lease is lost never reaches it. ``stop_runtime``
  passes the generation ``start_runtime`` returned; a superseded session is
  refused by the tracker.
* **Failure texts are formatted before ``record_failure``** (Decision 0007, 9):
  ``format_failure_text`` replaces what UTF-8 cannot encode (a lone surrogate would
  be refused, and the failure would never reach loop detection); the error class
  is reduced to a fixed name (``errors.error_class_of``).
* **Never hand a tool call to a terminated task** and **build ``TaskContext.run``
  from the task** (Decision 0006, 9): ``gateway.py``.
* **Write repositories are ``TaskScope.repositories``, with remotes registered**
  (Decision 0006, 8): they come from the caller's ``TaskAuthority.parent_scope``
  (the seam until issue #85 persists the working set, Decision 0014) and reach a
  node unchanged and narrowed only (``scope.py``).
* **Pending-deletion projects are swept periodically** (Decision 0008, 8):
  ``project_sweep.py``.

Time and concurrency
--------------------
Every wait goes through the injected :class:`Clock`. The queue lease and the
runtime timer are the database's clock (``TaskQueue``, ``BudgetTracker``). One
async task per running node; their outcomes are processed one at a time, in node
order, by the loop of ``_drive``, so the writes of one run are sequential and their
order is a function of the DAG (the store's row lock also serialises them against
another worker). All the state is in PostgreSQL: a crashed worker leaves nothing
in memory that matters, and the next lease holder takes the DAG over
(``DagStore.acquire``).
"""

import asyncio
import contextlib
import copy
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from paw_backend.authz import AgentGrant, GrantEscalationError
from paw_backend.orchestrator.config import Clock, OrchestratorConfig, SystemClock
from paw_backend.orchestrator.domain import (
    DagState,
    NextStep,
    NodeRole,
    RunOutcome,
)
from paw_backend.orchestrator.errors import (
    GRANT_ESCALATION,
    INVALID_OUTCOME,
    NODE_TIMEOUT,
    DagAlreadyExistsError,
    DagStateError,
    InvalidOrchestratorArgumentError,
    InvalidPlanError,
    NodeStopped,
    ScopeEscalationError,
    StaleDagEpochError,
    StaleNodeAttemptError,
    StopReason,
    error_class_of,
)
from paw_backend.orchestrator.gateway import (
    NodeBudgetHandle,
    NodeToolGateway,
    RunGuard,
    ToolCaller,
)
from paw_backend.orchestrator.limits import MAX_ERROR_CLASS_CHARS
from paw_backend.orchestrator.plan import Plan
from paw_backend.orchestrator.records import AttemptRecord, DagRecord, NodeRecord
from paw_backend.orchestrator.runtime import (
    AgentRuntime,
    NodeAssignment,
    NodeOutcome,
    validate_runtime,
)
from paw_backend.orchestrator.scheduling import DagVerdict, dag_verdict, ready_batch
from paw_backend.orchestrator.scope import agent_id_of, derive_child_scope, node_grant
from paw_backend.orchestrator.store import DagStore
from paw_backend.orchestrator.validation import (
    check_member,
    check_uuid,
    check_worker_id,
)
from paw_backend.tasks import (
    Actor,
    IllegalTransitionError,
    LogLevel,
    StaleRunError,
    TaskCommand,
    TaskConflictError,
    TaskNotFoundError,
    TaskRun,
    TaskService,
    TaskSnapshot,
    TaskState,
    WaitReason,
)
from paw_backend.tasks.queueing import (
    BudgetKind,
    BudgetNotConfiguredError,
    BudgetPreset,
    BudgetStatus,
    BudgetTracker,
    LeaseLostError,
    LoopDetector,
    LoopVerdict,
    NextAction,
    Priority,
    QueueEntry,
    StaleRuntimeSessionError,
    TaskQueue,
    decide_next_action,
    failure_signature,
)
from paw_backend.tasks.queueing.validation import MAX_APPROACH
from paw_backend.tools import TaskActivityProvider, TaskContext, TaskScope
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger(__name__)

# Recorded as the reason of the task commands the orchestrator issues (fixed text).
REASON_DAG_SUCCEEDED = "All required nodes succeeded"
REASON_DAG_FAILED = "A required node did not succeed"
REASON_PLAN_FAILED = "No acceptable plan"
REASON_NO_BUDGET = "The task has no budget preset"
REASON_RETRIES = "The retry budget is used up"
REASON_WAIT = "The budget or a repeated failure needs a decision"
# The wait for cleaning up after a cancellation (shutdown).
SHUTDOWN_GRACE_SECONDS = 5.0
HEARTBEAT_FAILURES_TO_LOSE = 3
# The planning attempts are named like a node in the loop detector's records.
PLAN_STEP = "plan"


def format_failure_text(text: object) -> str:
    """A failure text as ``LoopDetector.record_failure`` accepts it.

    The detector refuses a lone surrogate (UTF-8 cannot encode it) and then the
    failure is not recorded at all, so it never counts towards a loop. The worker
    formats the text first (Decision 0007, 9): what cannot be encoded becomes
    ``?``. A value that is not text becomes empty.
    """
    if not isinstance(text, str):
        return ""
    return text.encode("utf-8", errors="replace").decode("utf-8")


def format_error_class(name: object) -> str:
    """A name for the failure records: ``[A-Za-z0-9_.-]`` only, at most 100 long."""
    if not isinstance(name, str) or not name:
        return "Error"
    cleaned = "".join(
        c if c.isascii() and (c.isalnum() or c in "_.-") else "_" for c in name
    )
    return cleaned[:MAX_ERROR_CLASS_CHARS] or "Error"


class TaskAuthority:
    """What the caller supplies about the task's rights (a Protocol in spirit).

    ``parent_grant(task)``: the grant of the agent that works for the task
    (built by the backend from the task's scope; a model never writes one).
    ``parent_scope(task)``: the task's ``TaskScope`` **as it is now**: the
    paths, hosts, projects (with their current state), credential handles and
    ``repositories``. The orchestrator asks again for every tool call, so an ACL
    that was narrowed or a project that was archived takes effect on the next call.

    **The working-set seam.** The repositories of a Multi-Repo task are not
    persisted yet (issue #85, Decision 0014): the caller decides them here, each
    as a ``ScopedRepository`` with its worktree, its resolved ``RepoAcl`` and the
    URLs (**remotes**) that address it: a repository without a registered remote
    lets no call that carries a URL through (Decision 0006, section 8 (d)). When
    #85 lands, an implementation of this seam reads the stored working set.
    """

    async def parent_grant(self, task: TaskSnapshot) -> AgentGrant:
        raise NotImplementedError

    async def parent_scope(self, task: TaskSnapshot) -> TaskScope:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class RunReport:
    """What one ``run_entry`` did: ``outcome``, the task, the DAG's final state."""

    outcome: RunOutcome
    task_id: uuid.UUID | None = None
    dag_state: DagState | None = None


class _Watch(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    ENDED = "ended"  # completed / failed / evaluating under the run
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"  # a Retry / Restart replaced the run
    LOST = "lost"  # the lease is lost


class _StopKind(StrEnum):
    PAUSE = "pause"  # the task was paused: quiesce
    WAIT = "wait"  # budget or loop: a human decides (task waits)
    FAIL = "fail"  # the retry budget is used up (task fails)


@dataclass(slots=True)
class _Stop:
    kind: _StopKind


@dataclass(frozen=True, slots=True)
class _Finished:
    """How one attempt ended, as the run loop sees it."""

    outcome: NodeOutcome | None = None
    stopped: StopReason | None = None
    error_class: str = "Error"
    message: str = ""
    retryable: bool = True

    @classmethod
    def failure(
        cls, error_class: str, message: str = "", *, retryable: bool = True
    ) -> "_Finished":
        return cls(error_class=error_class, message=message, retryable=retryable)


@dataclass(frozen=True, slots=True)
class _Spec:
    """One attempt to run: a node of the DAG, or the planning call."""

    key: str
    role: NodeRole
    title: str
    goal: str
    input: Mapping[str, object]
    upstream: Mapping[str, object]
    agent: str
    attempt: int
    approach: int
    node: NodeRecord | None


@dataclass(slots=True)
class _Run:
    """The state of one ``run_entry`` (never shared between runs)."""

    task: TaskSnapshot
    run: TaskRun
    entry: QueueEntry
    worker_id: str
    guard: RunGuard
    lost: asyncio.Event = field(default_factory=asyncio.Event)
    epoch: int = 0
    generation: int | None = None
    not_before: dict[str, float] = field(default_factory=dict)

    def lose(self) -> None:
        self.guard.stop(StopReason.LEASE_LOST)
        self.lost.set()


class Orchestrator:
    def __init__(
        self,
        *,
        tasks: TaskService,
        queue: TaskQueue,
        budget: BudgetTracker,
        loops: LoopDetector,
        store: DagStore,
        activity: TaskActivityProvider,
        tools: ToolCaller,
        authority: TaskAuthority,
        runtimes: Mapping[str, AgentRuntime],
        config: OrchestratorConfig,
        clock: Clock | None = None,
    ) -> None:
        """Every collaborator is checked here, once, so that a wrong one fails
        loudly at construction (``TypeError``, or ``InvalidOrchestratorArgumentError``
        for a value): a missing method, a plain function where a coroutine is
        required, a ladder that names an agent nobody runs, a heartbeat that is
        not shorter than the lease."""
        for name, value, expected in (
            ("tasks", tasks, TaskService),
            ("queue", queue, TaskQueue),
            ("budget", budget, BudgetTracker),
            ("loops", loops, LoopDetector),
            ("store", store, DagStore),
            ("config", config, OrchestratorConfig),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"{name} must be a {expected.__name__}")
        require_async_method(activity, "check", 2)
        require_async_method(tools, "run", 1)
        require_async_method(authority, "parent_grant", 1)
        require_async_method(authority, "parent_scope", 1)
        clock = clock or SystemClock()
        if not callable(getattr(clock, "monotonic", None)):
            raise TypeError("clock must have monotonic()")
        require_async_method(clock, "sleep", 1)
        if not isinstance(runtimes, Mapping):
            raise TypeError("runtimes must map agent labels to runtimes")
        for label in runtimes:
            if not isinstance(label, str):
                raise TypeError("runtimes must map agent labels to runtimes")
        for label, runtime in runtimes.items():
            validate_runtime(runtime, label)
        if not config.labels <= set(runtimes):
            raise InvalidOrchestratorArgumentError("runtimes")
        interval = (
            config.heartbeat_seconds
            if config.heartbeat_seconds is not None
            else queue.lease_seconds / 3
        )
        if not 0 < interval < queue.lease_seconds:
            raise InvalidOrchestratorArgumentError("heartbeat_seconds")
        self._tasks = tasks
        self._queue = queue
        self._budget = budget
        self._loops = loops
        self._store = store
        self._activity = activity
        self._tools = tools
        self._authority = authority
        self._runtimes = dict(runtimes)
        self._config = config
        self._clock = clock
        self._heartbeat_seconds = float(interval)

    # -- entry points -----------------------------------------------------------

    async def enqueue_task(
        self,
        task_id: uuid.UUID,
        *,
        preset: BudgetPreset,
        priority: Priority = Priority.NORMAL,
    ) -> QueueEntry:
        """Give the task its budget and put it in the queue.

        A task without a budget is never run (``BudgetNotConfiguredError`` is not
        read as "unlimited"), so the preset is part of enqueuing. Raises
        ``TaskNotFoundError`` and ``TaskAlreadyQueuedError`` like the queue.
        """
        check_uuid("task_id", task_id)
        if not isinstance(preset, BudgetPreset):
            preset = check_member("preset", preset, BudgetPreset)
        if not isinstance(priority, Priority):
            priority = check_member("priority", priority, Priority)
        await self._budget.set_preset(task_id, preset)
        return await self._queue.enqueue(task_id, priority=priority)

    async def submit_plan(
        self, task_id: uuid.UUID, plan: Plan | Mapping[str, object]
    ) -> DagRecord:
        """Accept a plan for the task's current attempt and store it as its DAG.

        ``plan`` is a ``Plan`` or a planner's mapping (``{"nodes": [...]}``); it is
        judged before anything is stored (``InvalidPlanError``: a cycle, an unknown
        dependency, too many nodes, ...). ``DagAlreadyExistsError`` when the
        attempt has a DAG already. The task must not be finished.
        """
        check_uuid("task_id", task_id)
        if not isinstance(plan, Plan):
            plan = Plan.from_mapping(plan)
        snapshot = await self._tasks.restore(task_id, log_limit=0)
        if snapshot.state in (
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        ):
            raise DagStateError()
        return await self._store.create(task_id, snapshot.attempt.number, plan)

    async def run_once(self, worker_id: str) -> RunReport:
        """Claim the next queue entry and run it (``IDLE`` when there is none)."""
        check_worker_id(worker_id)
        entry = await self._queue.claim_next(worker_id)
        if entry is None:
            return RunReport(RunOutcome.IDLE)
        return await self.run_entry(entry, worker_id)

    async def serve(
        self, worker_id: str, stop: asyncio.Event, *, idle_seconds: float = 1.0
    ) -> None:
        """Run entries until ``stop`` is set; wait ``idle_seconds`` when there is
        nothing to claim. Returns when stopped; cancelling it stops it at once
        (the entry in hand is released and its running nodes are made ready)."""
        check_worker_id(worker_id)
        if not isinstance(stop, asyncio.Event):
            raise InvalidOrchestratorArgumentError("stop")
        if (
            isinstance(idle_seconds, bool)
            or not isinstance(idle_seconds, int | float)
            or not 0 < idle_seconds <= 3600
        ):
            raise InvalidOrchestratorArgumentError("idle_seconds")
        while not stop.is_set():
            try:
                report = await self.run_once(worker_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # one bad entry must not end the worker
                logger.error("Orchestrator run failed (%s)", error_class_of(error))
                report = RunReport(RunOutcome.IDLE)
            if report.outcome is RunOutcome.IDLE:
                await self._sleep_or_stop(idle_seconds, stop)

    async def run_entry(self, entry: QueueEntry, worker_id: str) -> RunReport:
        """Run the task of a queue entry this worker claimed, then complete the
        entry (or release it when the worker is cancelled). See the module."""
        if not isinstance(entry, QueueEntry):
            raise InvalidOrchestratorArgumentError("entry")
        check_worker_id(worker_id)
        if entry.claimed_by != worker_id:
            raise InvalidOrchestratorArgumentError("worker_id")
        try:
            return await self._run_entry(entry, worker_id)
        except LeaseLostError:
            return RunReport(RunOutcome.LEASE_LOST, entry.task_id)

    # -- one entry ----------------------------------------------------------------

    async def _run_entry(self, entry: QueueEntry, worker_id: str) -> RunReport:
        task_id = entry.task_id
        try:
            snapshot = await self._tasks.restore(task_id, log_limit=0)
        except TaskNotFoundError:
            return RunReport(RunOutcome.SKIPPED, task_id)
        # Prove the lease NOW, before anything that only the lease holder may do.
        try:
            await self._queue.heartbeat(entry.id, worker_id, entry.claim_count)
        except LeaseLostError:
            return RunReport(RunOutcome.LEASE_LOST, task_id)

        run_id = await self._begin_task(snapshot, entry, worker_id)
        if run_id is None:
            return RunReport(RunOutcome.SKIPPED, task_id)
        guard = RunGuard(task_id, run_id, self._activity)
        run = _Run(snapshot, run_id, entry, worker_id, guard)

        try:
            verdict = await self._budget.check(task_id)
        except BudgetNotConfiguredError:
            await self._end_task(
                run, TaskCommand.FAIL, Actor.policy(), REASON_NO_BUDGET
            )
            await self._complete_entry(run)
            return RunReport(RunOutcome.BUDGET_NOT_CONFIGURED, task_id)
        if verdict.status is BudgetStatus.EXCEEDED:
            action = decide_next_action(
                verdict, LoopVerdict.CONTINUE, can_escalate=False
            ).action
            report = await self._end_after_stop(
                run, self._stop_for_budget(action), None
            )
            await self._complete_entry(run)
            return report

        heartbeat: asyncio.Task[None] | None = None
        complete_entry = True
        try:
            run.generation = await self._budget.start_runtime(task_id)
            heartbeat = asyncio.create_task(self._heartbeats(run))
            report = await self._drive(run)
            complete_entry = report.outcome is not RunOutcome.LEASE_LOST
            return report
        except asyncio.CancelledError:
            complete_entry = False
            await self._abandon(run)
            raise
        except StaleDagEpochError:
            complete_entry = False  # another worker owns the DAG: so the entry
            return RunReport(RunOutcome.LEASE_LOST, task_id)
        except StaleRunError:
            return RunReport(RunOutcome.SUPERSEDED, task_id)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            await self._stop_runtime(run)
            if complete_entry and not run.lost.is_set():
                await self._complete_entry(run)

    async def _begin_task(
        self, snapshot: TaskSnapshot, entry: QueueEntry, worker_id: str
    ) -> TaskRun | None:
        """Start the task (queued) or take over a run that lost its worker
        (running). ``None``: the task is in no state that can run; the entry is
        completed (a paused or waiting task is queued again by whoever unblocks it)."""
        task_id = snapshot.id
        if snapshot.state is TaskState.QUEUED:
            try:
                event = await self._tasks.execute(
                    task_id, TaskCommand.START, actor=Actor.system()
                )
            except (IllegalTransitionError, TaskConflictError):
                await self._complete_quietly(entry, worker_id)
                return None
            return event.run
        if snapshot.state is TaskState.RUNNING:
            return snapshot.run
        await self._complete_quietly(entry, worker_id)
        return None

    async def _complete_quietly(self, entry: QueueEntry, worker_id: str) -> None:
        with contextlib.suppress(LeaseLostError):
            await self._queue.complete(entry.id, worker_id, entry.claim_count)

    async def _complete_entry(self, run: _Run) -> None:
        await self._complete_quietly(run.entry, run.worker_id)

    async def _stop_runtime(self, run: _Run) -> None:
        if run.generation is None:
            return
        try:
            await self._budget.stop_runtime(run.task.id, run.generation)
        except StaleRuntimeSessionError:
            pass  # a newer worker's session: its timer is not ours to stop
        except Exception as error:
            logger.error(
                "Stopping the runtime timer failed (%s)", error_class_of(error)
            )

    async def _abandon(self, run: _Run) -> None:
        """Best effort on cancellation (shutdown): the entry goes back to the queue
        and the nodes that were running are ready again for the next worker."""

        async def release() -> None:
            if run.epoch:
                dag = await self._store.get(run.task.id, run.run.attempt)
                if dag is not None:
                    await self._store.interrupt(dag.id, run.epoch)
            await self._queue.release(
                run.entry.id, run.worker_id, run.entry.claim_count
            )

        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(release(), SHUTDOWN_GRACE_SECONDS)

    async def _heartbeats(self, run: _Run) -> None:
        failures = 0
        while True:
            await self._clock.sleep(self._heartbeat_seconds)
            try:
                await self._queue.heartbeat(
                    run.entry.id, run.worker_id, run.entry.claim_count
                )
                failures = 0
            except LeaseLostError:
                run.lose()
                return
            except Exception as error:
                failures += 1
                logger.warning("Lease heartbeat failed (%s)", error_class_of(error))
                if failures >= HEARTBEAT_FAILURES_TO_LOSE:
                    run.lose()  # fail closed: a lease we cannot extend is lost
                    return

    # -- the DAG ------------------------------------------------------------------

    async def _drive(self, run: _Run) -> RunReport:
        task_id = run.task.id
        dag = await self._store.get(task_id, run.run.attempt)
        if dag is None:
            planned = await self._plan(run)
            if isinstance(planned, RunReport):
                return planned
            dag = planned
        dag = await self._store.acquire(dag.id, run.worker_id, run.run)
        run.epoch = dag.epoch
        running: dict[str, asyncio.Task[_Finished]] = {}
        numbers: dict[str, int] = {}
        stop: _Stop | None = None
        lost_waiter = asyncio.create_task(run.lost.wait())
        try:
            while True:
                watch = await self._watch(run)
                if watch is _Watch.LOST or watch is _Watch.SUPERSEDED:
                    await self._cancel_all(running)
                    outcome = (
                        RunOutcome.LEASE_LOST
                        if watch is _Watch.LOST
                        else RunOutcome.SUPERSEDED
                    )
                    return RunReport(outcome, task_id)
                if watch in (_Watch.ENDED, _Watch.CANCELLED):
                    await self._cancel_all(running)
                    final = await (
                        self._store.cancel(dag.id, run.epoch)
                        if watch is _Watch.CANCELLED
                        else self._store.interrupt(dag.id, run.epoch)
                    )
                    return RunReport(RunOutcome.TASK_ENDED, task_id, final.state)
                if watch is _Watch.PAUSED:
                    stop = stop or _Stop(_StopKind.PAUSE)
                elif stop is not None and stop.kind is _StopKind.PAUSE:
                    stop = None  # resumed while the nodes were finishing

                if stop is None:
                    stop = await self._dispatch(run, dag, running, numbers)
                    dag = await self._store.get_by_id(dag.id)

                if not running:
                    if (
                        stop is not None
                        or dag_verdict(dag.views()) is not DagVerdict.ACTIVE
                    ):
                        break
                    await self._wait_for_backoff(run, lost_waiter)
                    continue

                done = await self._wait_for(run, running, lost_waiter)
                for key in sorted(done, key=lambda k: dag.node(k).ordinal):
                    task = running.pop(key)
                    finished = self._result_of(task)
                    dag, extra = await self._settle(
                        run, dag, dag.node(key), numbers.pop(key), finished
                    )
                    stop = stop or extra
        finally:
            lost_waiter.cancel()
            await self._cancel_all(running)
            await asyncio.gather(lost_waiter, return_exceptions=True)

        return await self._finish_dag(run, dag, stop)

    async def _finish_dag(
        self, run: _Run, dag: DagRecord, stop: _Stop | None
    ) -> RunReport:
        task_id = run.task.id
        if stop is not None:
            return await self._end_after_stop(run, stop, dag)
        final = await self._store.finalize(dag.id, run.epoch)
        if final.state is DagState.SUCCEEDED:
            ended = await self._end_task(
                run, TaskCommand.BEGIN_EVALUATION, Actor.system(), REASON_DAG_SUCCEEDED
            )
            return RunReport(
                RunOutcome.DAG_SUCCEEDED if ended else RunOutcome.TASK_ENDED,
                task_id,
                final.state,
            )
        ended = await self._end_task(
            run, TaskCommand.FAIL, Actor.system(), REASON_DAG_FAILED
        )
        return RunReport(
            RunOutcome.DAG_FAILED if ended else RunOutcome.TASK_ENDED,
            task_id,
            final.state,
        )

    async def _end_after_stop(
        self, run: _Run, stop: _Stop, dag: DagRecord | None
    ) -> RunReport:
        task_id = run.task.id
        state = None if dag is None else dag.state
        if stop.kind is _StopKind.PAUSE:
            return RunReport(RunOutcome.PAUSED, task_id, state)
        if stop.kind is _StopKind.WAIT:
            ended = await self._end_task(
                run, TaskCommand.WAIT, Actor.policy(), REASON_WAIT, WaitReason.USER
            )
            return RunReport(
                RunOutcome.WAITING_FOR_USER if ended else RunOutcome.TASK_ENDED,
                task_id,
                state,
            )
        ended = await self._end_task(
            run, TaskCommand.FAIL, Actor.policy(), REASON_RETRIES
        )
        return RunReport(
            RunOutcome.BUDGET_FAILED if ended else RunOutcome.TASK_ENDED,
            task_id,
            state,
        )

    @staticmethod
    def _stop_for_budget(action: NextAction) -> _Stop:
        return _Stop(_StopKind.FAIL if action is NextAction.FAIL else _StopKind.WAIT)

    async def _end_task(
        self,
        run: _Run,
        command: TaskCommand,
        actor: Actor,
        reason: str,
        wait_reason: WaitReason | None = None,
    ) -> bool:
        """Issue a task command; ``False`` when the task no longer accepts it (it
        was cancelled, retried, ... meanwhile)."""
        try:
            await self._tasks.execute(
                run.task.id,
                command,
                actor=actor,
                reason=reason,
                wait_reason=wait_reason,
            )
        except (IllegalTransitionError, TaskConflictError):
            return False
        return True

    async def _watch(self, run: _Run) -> _Watch:
        """What the task is doing now, judged from the database (and the guard)."""
        reason = run.guard.stop_reason
        if run.lost.is_set() or reason is StopReason.LEASE_LOST:
            return _Watch.LOST
        if reason is StopReason.SUPERSEDED:
            return _Watch.SUPERSEDED
        try:
            snapshot = await self._tasks.restore(run.task.id, log_limit=0)
        except TaskNotFoundError:
            return _Watch.ENDED
        if snapshot.run != run.run:
            return _Watch.SUPERSEDED
        state = snapshot.state
        if state is TaskState.CANCELLED:
            return _Watch.CANCELLED
        if state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.EVALUATING):
            return _Watch.ENDED
        if state is TaskState.QUEUED or reason is StopReason.TASK_ENDED:
            return _Watch.ENDED
        return _Watch.PAUSED if state is TaskState.PAUSED else _Watch.RUNNING

    async def _dispatch(
        self,
        run: _Run,
        dag: DagRecord,
        running: dict[str, asyncio.Task[_Finished]],
        numbers: dict[str, int],
    ) -> _Stop | None:
        """Start the ready nodes there is room for. ``_Stop`` when the budget stops
        the run before all of them could start."""
        now = self._clock.monotonic()
        waiting = frozenset(k for k, until in run.not_before.items() if until > now)
        keys = ready_batch(
            dag.views(),
            self._config.max_parallel_nodes - len(running),
            exclude=waiting,
        )
        for key in keys:
            node = dag.node(key)
            if node.rung_attempts >= self._config.max_attempts_per_rung:
                # A run that resumed a node whose rung had no attempt left.
                dag = await self._store.give_up_node(
                    dag.id, run.epoch, key, error_class="AttemptsExhausted"
                )
                continue
            verdict = await self._budget.check(
                run.task.id, planned={BudgetKind.STEPS: 1}
            )
            if verdict.status is BudgetStatus.EXCEEDED:
                decision = decide_next_action(
                    verdict, LoopVerdict.CONTINUE, can_escalate=False
                )
                return self._stop_for_budget(decision.action)
            await self._budget.record(run.task.id, BudgetKind.STEPS, 1)
            attempt = await self._store.start_node(
                dag.id, run.epoch, key, max_attempts=self._config.max_attempts_per_rung
            )
            spec = self._spec_of(dag, node, attempt)
            running[key] = asyncio.create_task(self._attempt(run, spec))
            numbers[key] = attempt.number
        return None

    def _spec_of(
        self, dag: DagRecord, node: NodeRecord, attempt: AttemptRecord
    ) -> _Spec:
        ladder = self._config.ladders[node.role]
        upstream = {
            dependency: dag.node(dependency).result for dependency in node.depends_on
        }
        return _Spec(
            key=node.key,
            role=node.role,
            title=node.title,
            goal=node.goal,
            input=node.input,
            upstream=upstream,
            agent=ladder[min(attempt.agent_index, len(ladder) - 1)],
            attempt=attempt.number,
            approach=attempt.approach,
            node=node,
        )

    async def _wait_for(
        self,
        run: _Run,
        running: dict[str, asyncio.Task[_Finished]],
        lost_waiter: asyncio.Task,
    ) -> set[str]:
        """Wait until a node ends, the lease is lost or the poll time is up; the
        keys of the nodes that ended (possibly none)."""
        timer = asyncio.create_task(self._clock.sleep(self._config.poll_seconds))
        try:
            await asyncio.wait(
                {*running.values(), timer, lost_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        return {key for key, task in running.items() if task.done()}

    async def _wait_for_backoff(self, run: _Run, lost_waiter: asyncio.Task) -> None:
        now = self._clock.monotonic()
        pending = [until - now for until in run.not_before.values() if until > now]
        delay = min(pending) if pending else self._config.poll_seconds
        timer = asyncio.create_task(self._clock.sleep(delay))
        try:
            await asyncio.wait(
                {timer, lost_waiter}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)

    async def _sleep_or_stop(self, seconds: float, stop: asyncio.Event) -> None:
        timer = asyncio.create_task(self._clock.sleep(seconds))
        waiter = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({timer, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (timer, waiter):
                task.cancel()
            await asyncio.gather(timer, waiter, return_exceptions=True)

    @staticmethod
    async def _cancel_all(running: dict[str, asyncio.Task[_Finished]]) -> None:
        tasks = list(running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        running.clear()

    @staticmethod
    def _result_of(task: asyncio.Task[_Finished]) -> _Finished:
        if task.cancelled():
            return _Finished(stopped=StopReason.SHUTDOWN)
        error = task.exception()
        if error is not None:  # _attempt catches everything: a bug of ours
            return _Finished.failure(error_class_of(error))
        return task.result()

    # -- one attempt --------------------------------------------------------------

    async def _context(self, run: _Run, spec: _Spec) -> TaskContext:
        """The ``TaskContext`` of one tool call, from CURRENT values: the run comes
        from the task snapshot the worker was started with (``TaskEvent.run``), the
        delegator from the task, the grant and scope derived from the parent's."""
        parent_grant = await self._authority.parent_grant(run.task)
        parent_scope = await self._authority.parent_scope(run.task)
        grant = node_grant(
            parent_grant,
            spec.node,
            spec.role,
            agent_id_of(run.task.id, spec.key, spec.attempt),
        )
        scope = derive_child_scope(
            parent_scope,
            role=spec.role,
            repositories=None if spec.node is None else spec.node.repositories,
        )
        return TaskContext(
            task_id=run.task.id,
            delegator_id=run.task.created_by,
            grant=grant,
            scope=scope,
            primary_project_id=run.task.project_id,
            run=run.run,
        )

    async def _attempt(self, run: _Run, spec: _Spec) -> _Finished:
        """Run one attempt through the agent runtime; only cancellation escapes."""
        try:
            await self._context(run, spec)  # fail early on a grant / scope problem
            assignment = NodeAssignment(
                task_id=run.task.id,
                node_key=spec.key,
                role=spec.role,
                title=spec.title,
                goal=spec.goal,
                input=copy.deepcopy(dict(spec.input)),
                upstream=dict(spec.upstream),
                agent=spec.agent,
                attempt=spec.attempt,
                approach=spec.approach,
                tools=NodeToolGateway(
                    run.guard, self._tools, lambda: self._context(run, spec)
                ),
                budget=NodeBudgetHandle(run.guard, self._budget, run.task.id),
            )
            outcome = await self._with_timeout(
                self._runtimes[spec.agent].run_node(assignment)
            )
        except NodeStopped as stopped:
            return _Finished(stopped=stopped.reason)
        except (GrantEscalationError, ScopeEscalationError) as error:
            return _Finished.failure(
                GRANT_ESCALATION
                if isinstance(error, GrantEscalationError)
                else "ScopeEscalation",
                retryable=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return _Finished.failure(error_class_of(error))
        if outcome is _TIMED_OUT:
            return _Finished.failure(NODE_TIMEOUT)
        if not isinstance(outcome, NodeOutcome):
            return _Finished.failure(INVALID_OUTCOME)
        return _Finished(outcome=outcome)

    async def _with_timeout(self, awaitable):
        work = asyncio.ensure_future(awaitable)
        timer = asyncio.ensure_future(
            self._clock.sleep(self._config.node_timeout_seconds)
        )
        try:
            await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                return work.result()
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            return _TIMED_OUT
        finally:
            for task in (work, timer):
                task.cancel()
            await asyncio.gather(work, timer, return_exceptions=True)

    # -- what an ended attempt means ------------------------------------------------

    async def _settle(
        self,
        run: _Run,
        dag: DagRecord,
        node: NodeRecord,
        number: int,
        finished: _Finished,
    ) -> tuple[DagRecord, _Stop | None]:
        """Write the outcome of an attempt. ``(dag, stop)``: ``stop`` when the
        outcome means the run must not start more nodes (a budget or a loop)."""
        store, epoch = self._store, run.epoch
        if finished.stopped is not None:
            reason = finished.stopped
            if reason is StopReason.BUDGET_EXCEEDED:
                dag = await store.fail_node(
                    dag.id,
                    epoch,
                    node.key,
                    number,
                    error_class="BudgetExceeded",
                    signature=failure_signature("BudgetExceeded", node.key, ""),
                    step=NextStep.HOLD,
                )
                return dag, await self._budget_stop(run)
            if run.guard.stop_reason is reason and reason in (
                StopReason.TASK_ENDED,
                StopReason.SUPERSEDED,
                StopReason.LEASE_LOST,
            ):
                return dag, None  # the top of the loop sees it and closes the DAG
            dag = await store.fail_node(
                dag.id,
                epoch,
                node.key,
                number,
                error_class="Stopped",
                signature=failure_signature("Stopped", node.key, ""),
                step=NextStep.HOLD,
            )
            return dag, None

        outcome = finished.outcome
        if outcome is not None and outcome.ok:
            if outcome.plan is not None:  # only the decomposition may propose nodes
                finished = _Finished.failure(INVALID_OUTCOME, retryable=False)
            else:
                try:
                    dag = await store.complete_node(
                        dag.id, epoch, node.key, number, outcome.result
                    )
                except StaleNodeAttemptError:
                    return dag, None
                await self._log(
                    run, LogLevel.INFO, f"Node {node.key} succeeded (attempt {number})"
                )
                return dag, None
        elif outcome is not None:
            finished = _Finished(
                error_class=outcome.error_class or "Error",
                message=outcome.message,
                retryable=outcome.retryable,
            )
        return await self._settle_failure(run, dag, node, number, finished)

    async def _budget_stop(self, run: _Run) -> _Stop:
        verdict = await self._budget.check(run.task.id)
        decision = decide_next_action(verdict, LoopVerdict.CONTINUE, can_escalate=False)
        if decision.action is NextAction.FAIL:
            return _Stop(_StopKind.FAIL)
        return _Stop(_StopKind.WAIT)

    async def _settle_failure(
        self,
        run: _Run,
        dag: DagRecord,
        node: NodeRecord,
        number: int,
        finished: _Finished,
    ) -> tuple[DagRecord, _Stop | None]:
        """A failed attempt: record it, decide (Decision 0007: budget before loop),
        and put the node where the decision says. The rules:

        * the failure text is formatted, then hashed by the loop detector
          (``step`` is the node key: nodes are told apart); the text is never stored;
        * ``decide_next_action`` gets the budget verdict (with one more retry
          planned) and the loop verdict of the node's failures; whether an escalation
          is possible is whether the node's ladder has another agent;
        * ``CONTINUE`` retries, ``TRY_ALTERNATIVE`` changes the approach,
          ``ESCALATE_AGENT`` moves to the next agent (a new approach as well: the
          loop history of the old one must not condemn the new one),
          ``WAIT_FOR_USER`` keeps the node and stops the run, ``FAIL`` gives the
          node up and stops the run;
        * a failure that says it is not retryable, a rung out of attempts and an
          approach beyond the loop detector's range give the node up.
        """
        error_class = format_error_class(finished.error_class)
        message = format_failure_text(finished.message)
        signature = failure_signature(error_class, node.key, message)
        verdict = LoopVerdict.CONTINUE
        try:
            assessment = await self._loops.record_failure(
                run.task.id,
                attempt=run.run.attempt,
                error_class=error_class,
                step=node.key,
                message=message,
                approach=node.approach,
            )
            verdict = assessment.verdict
        except Exception as error:
            if isinstance(error, StaleRunError):
                raise
            logger.error("Recording a failure failed (%s)", error_class_of(error))
        ladder = self._config.ladders[node.role]
        can_escalate = node.agent_index + 1 < len(ladder)
        budget = await self._budget.check(run.task.id, planned={BudgetKind.RETRIES: 1})
        decision = decide_next_action(budget, verdict, can_escalate=can_escalate)

        stop: _Stop | None = None
        arguments: dict[str, int] = {}
        step = NextStep.GIVE_UP
        if decision.action is NextAction.FAIL:
            stop = _Stop(_StopKind.FAIL)
        elif decision.action is NextAction.WAIT_FOR_USER:
            step, stop = NextStep.HOLD, _Stop(_StopKind.WAIT)
        elif finished.retryable:
            from_limit = node.rung_attempts >= self._config.max_attempts_per_rung
            next_approach = node.approach + 1
            if decision.action is NextAction.ESCALATE_AGENT:
                if can_escalate and next_approach <= MAX_APPROACH:
                    step = NextStep.ESCALATE
                    arguments = {
                        "agent_index": node.agent_index + 1,
                        "approach": next_approach,
                    }
            elif not from_limit:
                if (
                    decision.action is NextAction.TRY_ALTERNATIVE
                    and next_approach <= MAX_APPROACH
                ):
                    step = NextStep.ALTERNATIVE
                    arguments = {
                        "agent_index": node.agent_index,
                        "approach": next_approach,
                    }
                elif decision.action is NextAction.CONTINUE:
                    step = NextStep.RETRY
        if step in (NextStep.RETRY, NextStep.ALTERNATIVE, NextStep.ESCALATE):
            await self._budget.record(run.task.id, BudgetKind.RETRIES, 1)
            run.not_before[node.key] = self._clock.monotonic() + self._config.backoff(
                node.rung_attempts
            )
        dag = await self._store.fail_node(
            dag.id,
            run.epoch,
            node.key,
            number,
            error_class=error_class,
            signature=signature,
            step=step,
            **arguments,
        )
        await self._log(
            run,
            LogLevel.WARNING,
            f"Node {node.key} attempt {number} failed ({error_class}): {step.value}",
        )
        return dag, stop

    async def _log(self, run: _Run, level: LogLevel, message: str) -> None:
        """A fixed-format line in the task log (never a failure text). A stale run
        is a stopped run; any other error is only logged."""
        try:
            await self._tasks.add_log(run.task.id, message, run=run.run, level=level)
        except StaleRunError:
            run.guard.stop(StopReason.SUPERSEDED)
        except Exception as error:
            logger.warning("Task log failed (%s)", error_class_of(error))

    # -- the decomposition ------------------------------------------------------------

    async def _plan(self, run: _Run) -> DagRecord | RunReport:
        """Ask the planner role for a plan until one is accepted (at most
        ``max_plan_attempts`` times). The planner proposes; ``Plan`` judges: an
        invalid plan is a failed attempt like any other (recorded for loop
        detection under the step ``plan``), and the next attempt is made by the
        next agent of the planner ladder."""
        task_id = run.task.id
        ladder = self._config.ladders[NodeRole.PLANNER]
        for attempt in range(1, self._config.max_plan_attempts + 1):
            watch = await self._watch(run)
            if watch is not _Watch.RUNNING:
                return RunReport(
                    _OUTCOME_OF_WATCH.get(watch, RunOutcome.TASK_ENDED), task_id
                )
            verdict = await self._budget.check(task_id, planned={BudgetKind.STEPS: 1})
            if verdict.status is BudgetStatus.EXCEEDED:
                return await self._end_after_stop(
                    run, await self._budget_stop(run), None
                )
            await self._budget.record(task_id, BudgetKind.STEPS, 1)
            spec = _Spec(
                key=PLAN_STEP,
                role=NodeRole.PLANNER,
                title=run.task.title,
                goal=run.task.title,
                input=run.task.input,
                upstream={},
                agent=ladder[min(attempt - 1, len(ladder) - 1)],
                attempt=attempt,
                approach=0,
                node=None,
            )
            finished = await self._attempt(run, spec)
            if finished.stopped is not None:
                if finished.stopped is StopReason.BUDGET_EXCEEDED:
                    stop = await self._budget_stop(run)
                    return await self._end_after_stop(run, stop, None)
                return RunReport(
                    _OUTCOME_OF_STOP.get(finished.stopped, RunOutcome.TASK_ENDED),
                    task_id,
                )
            failure, message, retryable = (
                finished.error_class,
                finished.message,
                finished.retryable,
            )
            outcome = finished.outcome
            if outcome is not None and not outcome.ok:
                failure, message = outcome.error_class or "Error", outcome.message
                retryable = outcome.retryable
            elif outcome is not None and outcome.plan is None:
                failure, message, retryable = "NoPlan", "", True
            elif outcome is not None:
                try:
                    accepted = (
                        outcome.plan
                        if isinstance(outcome.plan, Plan)
                        else Plan.from_mapping(outcome.plan)
                    )
                    dag = await self._store.create(task_id, run.run.attempt, accepted)
                except DagAlreadyExistsError:
                    # Another worker planned this attempt first: its plan stands.
                    return await self._store.get(task_id, run.run.attempt)
                except InvalidPlanError:
                    failure, message, retryable = "InvalidPlan", "", True
                else:
                    await self._log(
                        run,
                        LogLevel.INFO,
                        f"Plan accepted: {len(accepted.nodes)} nodes",
                    )
                    return dag
            try:
                await self._loops.record_failure(
                    task_id,
                    attempt=run.run.attempt,
                    error_class=format_error_class(failure),
                    step=PLAN_STEP,
                    message=format_failure_text(message),
                )
            except StaleRunError:
                raise
            except Exception as error:
                logger.error("Recording a failure failed (%s)", error_class_of(error))
            if not retryable:
                break
        await self._end_task(run, TaskCommand.FAIL, Actor.system(), REASON_PLAN_FAILED)
        return RunReport(RunOutcome.PLAN_FAILED, task_id)


_TIMED_OUT = object()
_OUTCOME_OF_WATCH = {
    _Watch.LOST: RunOutcome.LEASE_LOST,
    _Watch.SUPERSEDED: RunOutcome.SUPERSEDED,
    _Watch.PAUSED: RunOutcome.PAUSED,
}
_OUTCOME_OF_STOP = {
    StopReason.LEASE_LOST: RunOutcome.LEASE_LOST,
    StopReason.SUPERSEDED: RunOutcome.SUPERSEDED,
}
