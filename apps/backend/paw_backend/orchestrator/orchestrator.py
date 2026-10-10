"""The DAG Agent Orchestrator (PAW-034).

``run_entry`` takes one claimed queue entry and drives the task it names to the end
of what the orchestrator can do for it: decompose it into a dependency DAG (the
planner role proposes, ``plan.py`` accepts), run the independent nodes in parallel
(``scheduling.py``), retry, change approach or escalate a failing node, pass the
structured results of finished nodes to the nodes that depend on them, and report
the outcome to the task (``evaluating``, ``failed`` or ``waiting``). What the
orchestrator does **not** do: run an agent (an ``AgentRuntime`` does), run git
(a ``NodeWorkspaces``, PAW-035, prepares the worktrees of the Worker nodes and
integrates their branches: ``workspaces.py``), evaluate or complete the task (the
Evaluator), choose the real parallelism (PAW-036) or persist the working set
(issue #85).

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
* **No tool call of a worker that lost its queue lease** (issue #126, Decision
  0046): ``_context`` puts this worker's claim in ``TaskContext.lease`` and the
  Broker checks it for every call (``gateway.QueueLeaseVerifier``); a refusal for
  a lost lease stops the run (``gateway.NodeToolGateway``).
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
(``_acquire_dag``: the take-over and the proof of the lease commit in one
transaction, so a worker that lost its lease can never raise the epoch after its
replacement did).
"""

import asyncio
import contextlib
import copy
import logging
import uuid
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from paw_backend.authz import AgentGrant, Capability, GrantEscalationError
from paw_backend.orchestrator.config import Clock, OrchestratorConfig, SystemClock
from paw_backend.orchestrator.domain import (
    DagState,
    IncidentKind,
    NextStep,
    NodeRole,
    NodeState,
    RunOutcome,
)
from paw_backend.orchestrator.errors import (
    GRANT_ESCALATION,
    INVALID_OUTCOME,
    NODE_TIMEOUT,
    OUT_OF_MEMORY_CLASSES,
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
    runtime_error_class,
)
from paw_backend.orchestrator.gateway import (
    AttemptFence,
    NodeBudgetHandle,
    NodeToolGateway,
    RunGuard,
    ToolCaller,
)
from paw_backend.orchestrator.limits import MAX_ERROR_CLASS_CHARS
from paw_backend.orchestrator.placement import NodePlacementHandle, content_digest
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
from paw_backend.orchestrator.workspaces import (
    WORKTREE_CONFLICT,
    WORKTREE_UNAVAILABLE,
    IntegrationReport,
    IntegrationRequest,
    IntegrationState,
    NodeWorkspaceRequest,
    NodeWorkspaces,
    NodeWorktree,
    RepositoryIntegration,
    WorktreeConflictError,
    WorktreeUnavailableError,
    gets_worktree,
)
from paw_backend.projects.errors import ProjectBusyError
from paw_backend.tasks import (
    Actor,
    IllegalTransitionError,
    LogLevel,
    ProjectNotActiveError,
    RepositoryNotInAttemptError,
    StaleAttemptError,
    StaleRunError,
    TaskCommand,
    TaskConflictError,
    TaskNotFoundError,
    TaskNotRunningError,
    TaskRun,
    TaskService,
    TaskSnapshot,
    TaskState,
    WaitReason,
    WorktreeState,
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
    QueueLease,
    StaleRuntimeSessionError,
    TaskQueue,
    decide_next_action,
    failure_signature,
)
from paw_backend.tasks.queueing.validation import MAX_APPROACH
from paw_backend.tools import TaskActivityProvider, TaskContext, TaskScope
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger(__name__)

_RETRYING_STEPS = frozenset({NextStep.RETRY, NextStep.ALTERNATIVE, NextStep.ESCALATE})
# The guard's stops that end the whole run for this worker: an attempt that ends
# after one is not settled (``_settle``); the top of the loop closes the run.
_RUN_ENDING_STOPS = frozenset(
    {StopReason.TASK_ENDED, StopReason.SUPERSEDED, StopReason.LEASE_LOST}
)

# Recorded as the reason of the task commands the orchestrator issues (fixed text).
REASON_DAG_SUCCEEDED = "All required nodes succeeded"
REASON_DAG_FAILED = "A required node did not succeed"
REASON_PLAN_FAILED = "No acceptable plan"
REASON_NO_BUDGET = "The task has no budget preset"
REASON_RETRIES = "The retry budget is used up"
REASON_WAIT = "The budget or a repeated failure needs a decision"
REASON_INTERNAL = "The orchestrator stopped on an unexpected error"
REASON_INTEGRATION_CONFLICT = "The integration of the worker branches needs a decision"
REASON_INTEGRATION_FAILED = "The worker branches could not be integrated"
# A command fenced by ``expected_version`` is decided again when another change of
# the task commits between the read and the write; this many times, then it fails.
COMMAND_ATTEMPTS = 5
# The wait for cleaning up after a cancellation (shutdown).
SHUTDOWN_GRACE_SECONDS = 5.0
# The longest the orchestrator waits (real time) for cancelled attempts to end (a
# timed-out runtime, a Cancel, a budget stop): a runtime that ignores its
# cancellation is abandoned (its ``AttemptFence`` is closed) instead of holding
# the node, the task and the queue lease forever.
CANCEL_GRACE_SECONDS = 10.0
HEARTBEAT_FAILURES_TO_LOSE = 3
# The deadline of one heartbeat attempt, as a fraction of the interval (and
# never past the time the run is lost): a stalled heartbeat is a failure.
HEARTBEAT_TIMEOUT_FRACTION = 0.5


class HeartbeatTimeoutError(Exception):
    """A heartbeat attempt did not end within its deadline."""


# The node key a planner runtime is given for a planning attempt.
PLAN_STEP = "plan"
# The planner's name in its agent identity (``agent_id_of``) and in the loop
# detector's records. It is not a valid node key (a key starts with a letter), so
# a plan node keyed ``plan`` never shares an identity or a failure history with
# the planner.
PLANNER_IDENTITY = "@planner"


def _forget(task: asyncio.Future) -> None:
    """Retrieve the end of an abandoned task (so asyncio does not warn about an
    exception nobody read); what it was is of no interest any more."""
    if not task.cancelled():
        task.exception()


async def _cancel_and_wait(
    tasks: list[asyncio.Future], grace: float | None = None
) -> None:
    """Cancel ``tasks`` and wait for them, at most ``grace`` seconds (default
    ``CANCEL_GRACE_SECONDS``).

    A task that has not ended by then (a runtime that swallows ``CancelledError``)
    is abandoned: it is left to run, and its end is retrieved when it comes. The
    attempt it belongs to has its ``AttemptFence`` closed by ``_attempt`` (the
    abandoned runtime can no longer call a tool or charge the budget), and the
    node's state is written through the DAG's fenced store as for any other end.
    """
    grace = CANCEL_GRACE_SECONDS if grace is None else grace
    for task in tasks:
        task.cancel()
    pending = [task for task in tasks if not task.done()]
    if pending:
        _, pending_set = await asyncio.wait(pending, timeout=grace)
        pending = list(pending_set)
    for task in tasks:
        if task in pending:
            task.add_done_callback(_forget)
        else:
            _forget(task)
    if pending:
        logger.error(
            "%d cancelled task(s) did not end within %.0f s and were abandoned",
            len(pending),
            grace,
        )


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
    The application's implementation is ``authority.StoredTaskAuthority``
    (issue #125).

    ``parent_grant(task)``: the grant of the agent that works for the task
    (built by the backend from the task's scope; a model never writes one).
    ``parent_scope(task)``: the task's ``TaskScope`` **as it is now**: the
    paths, hosts, projects (with their current state), credential handles and
    ``repositories``. The orchestrator asks again for every tool call, so an ACL
    that was narrowed or a project that was archived takes effect on the next call.

    **The working-set seam.** The caller builds the repositories here, each as
    a ``ScopedRepository`` with its worktree, its resolved ``RepoAcl``, the URLs
    (**remotes**) that address it (a repository without a registered remote lets
    no call that carries a URL through: Decision 0006, section 8 (d)) and its
    **role** in the stored Working Set (issue #85, Decision 0030):
    ``tools.scope.with_working_set_roles(repositories, working_set)``. ``task`` is
    the snapshot the run was started with: its ``working_set`` may predate a
    change, and an implementation may read the stored one again
    (``TaskService.restore``) to see a repository added since. Either way no
    stale role widens anything: a repository without a role is refused by
    the Tool Broker (``repository_role_unresolved``), and the Broker admits every
    call that touches a repository on the roles stored **now**
    (``TaskService.admit_repository_use``), so a scope older than a downgrade or a
    removal cannot widen anything. A node keeps the roles of its parent
    (``scope.derive_child_scope``); no node role may change the Working Set.
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
    WAITING = "waiting"  # someone else put the task in waiting: quiesce too
    ENDED = "ended"  # completed / failed / evaluating under the run
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"  # a Retry / Restart replaced the run
    LOST = "lost"  # the lease is lost


class _StopKind(StrEnum):
    PAUSE = "pause"  # the task was paused: quiesce
    HOLD = "hold"  # the task was put in waiting by someone else: quiesce
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
    # The runtime asked for the next rung at once (``NodeOutcome.escalate``).
    escalate: bool = False

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
    # The Worker nodes this node depends on directly (PAW-035), in node order.
    upstream_workers: tuple[str, ...] = ()
    # The DAG of a node's attempt (``None`` for the planning call).
    dag_id: uuid.UUID | None = None


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
    proved_at: float = 0.0  # the injected clock when the last lease proof began
    not_before: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A lost lease that the guard learns of (the Broker's ``lease_lost``)
        # wakes the run loop like one the heartbeats found.
        self.guard.on_lease_lost(self.lost.set)

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
        worktrees: NodeWorkspaces | None = None,
    ) -> None:
        """Every collaborator is checked here, once, so that a wrong one fails
        loudly at construction (``TypeError``, or ``InvalidOrchestratorArgumentError``
        for a value): a missing method, a plain function where a coroutine is
        required, a ladder that names an agent nobody runs, a heartbeat interval
        whose ``HEARTBEAT_FAILURES_TO_LOSE`` failures in a row would not end before
        the lease does (the run must give its lease up, fail closed, while the
        lease still holds: otherwise another worker could take the entry over
        while this one still runs its nodes)."""
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
        if worktrees is not None:
            require_async_method(worktrees, "prepare_node", 1)
            require_async_method(worktrees, "integrate", 1)
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
            else queue.lease_seconds / (HEARTBEAT_FAILURES_TO_LOSE + 1)
        )
        # The last successful heartbeat renews the lease for ``lease_seconds``;
        # the run declares the lease lost after the ``HEARTBEAT_FAILURES_TO_LOSE``
        # failures that follow it, ``interval`` apart. That must happen strictly
        # before the lease expires.
        if not 0 < interval * HEARTBEAT_FAILURES_TO_LOSE < queue.lease_seconds:
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
        self._worktrees = worktrees
        self._heartbeat_seconds = float(interval)

    # -- entry points -----------------------------------------------------------

    async def enqueue_task(
        self,
        task_id: uuid.UUID,
        *,
        preset: BudgetPreset,
        priority: Priority = Priority.NORMAL,
    ) -> QueueEntry:
        """Give the task its budget and put it in the queue, in ONE transaction.

        A task without a budget is never run (``BudgetNotConfiguredError`` is not
        read as "unlimited"), so the preset is part of enqueuing. The preset and
        the entry commit together: an enqueue that loses (``TaskAlreadyQueuedError``:
        the task has an active entry, maybe one a worker is running) changes no
        budget, so a duplicate call cannot switch a running task to another preset,
        and no worker can claim the entry before its budget exists. Raises
        ``TaskNotFoundError`` and ``TaskAlreadyQueuedError`` like the queue.
        """
        check_uuid("task_id", task_id)
        if not isinstance(preset, BudgetPreset):
            preset = check_member("preset", preset, BudgetPreset)
        if not isinstance(priority, Priority):
            priority = check_member("priority", priority, Priority)
        database = self._queue.database
        async with database.session() as session, session.begin():
            entry = await self._queue.enqueue_in(session, task_id, priority=priority)
            await self._budget.set_preset_in(session, task_id, preset)
        return entry

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
        # Fenced by the run that was read: a Retry or Restart that commits in
        # between refuses the plan (``StaleRunError``) instead of giving it to
        # the new run unseen.
        return await self._store.create(
            task_id, snapshot.attempt.number, plan, run=snapshot.run
        )

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
        # The proof has a deadline like every heartbeat: a proof that does not
        # come back in time is no proof, and nothing is started (the entry is
        # left to its lease).
        try:
            await self._within(
                self._queue.heartbeat(entry.id, worker_id, entry.claim_count),
                self._heartbeat_seconds * HEARTBEAT_TIMEOUT_FRACTION,
            )
        except (LeaseLostError, HeartbeatTimeoutError):
            return RunReport(RunOutcome.LEASE_LOST, task_id)

        run_id = await self._begin_task(snapshot, entry, worker_id)
        if run_id is None:
            return RunReport(RunOutcome.SKIPPED, task_id)
        guard = RunGuard(task_id, run_id, self._activity)
        run = _Run(snapshot, run_id, entry, worker_id, guard)

        # From here the task runs, and it must never be left running without work:
        # every way out either moves the task on (evaluating, failed, waiting,
        # paused: see ``_drive`` and ``_fail_safely``) or leaves the queue entry
        # claimed, so that its lease expires and the next worker takes the run over.
        heartbeat: asyncio.Task[None] | None = None
        complete_entry = True
        try:
            # A DAG that already ended for good (it succeeded, or failed in this
            # same run) is not run again: the task is only handed on as its
            # outcome says. Nothing runs, so the budget does not gate it (a used-up
            # budget must not keep finished work from its evaluation) and no
            # runtime timer is started.
            ended = await self._store.get(task_id, run.run.attempt)
            if ended is not None and _ended_for_good(ended, run.run):
                return await self._hand_on_ended_dag(run, ended)
            try:
                verdict = await self._budget.check(task_id)
            except BudgetNotConfiguredError:
                await self._end_task(
                    run, TaskCommand.FAIL, Actor.policy(), REASON_NO_BUDGET
                )
                return RunReport(RunOutcome.BUDGET_NOT_CONFIGURED, task_id)
            if verdict.status is BudgetStatus.EXCEEDED:
                action = decide_next_action(
                    verdict, LoopVerdict.CONTINUE, can_escalate=False
                ).action
                return await self._end_after_stop(
                    run, self._stop_for_budget(action), None
                )
            # The lease was proved at the start of the run, but the task was started
            # and the budget read since, and either can stall for longer than the
            # lease: another worker may hold the entry now. ``start_runtime`` is the
            # call only the lease holder may make (``BudgetTracker`` does not read
            # the queue, and a stale caller would take the runtime session over and
            # make the real holder's ``stop_runtime`` stale), so the timer starts
            # only in the transaction that proves the lease
            # (``_start_runtime_with_lease``): a ``LeaseLostError`` starts nothing
            # and leaves the entry to its new holder. ``proved_at`` is taken before
            # that proof begins: the heartbeats lose the run before the lease it
            # renewed can expire.
            run.proved_at = self._clock.monotonic()
            run.generation = await self._start_runtime_with_lease(
                entry, worker_id, run.run
            )
            heartbeat = asyncio.create_task(self._heartbeats(run))
            report = await self._drive_until_lost(run)
            complete_entry = report.outcome is not RunOutcome.LEASE_LOST
            return report
        except asyncio.CancelledError:
            complete_entry = False
            await self._abandon(run)
            raise
        except StaleDagEpochError:
            complete_entry = False  # another worker owns the DAG: so the entry
            return RunReport(RunOutcome.LEASE_LOST, task_id)
        except TaskNotRunningError:
            # New work was refused: the task was paused or put in waiting since
            # the last look (whoever resumes or unblocks it enqueues it again).
            watch = await self._watch(run)
            return RunReport(
                _OUTCOME_OF_WATCH.get(watch, RunOutcome.TASK_ENDED), task_id
            )
        except StaleRunError:
            return RunReport(RunOutcome.SUPERSEDED, task_id)
        except LeaseLostError:
            complete_entry = False
            raise
        except Exception as error:
            logger.error("The orchestrator run failed (%s)", error_class_of(error))
            complete_entry = await self._fail_safely(run)
            return RunReport(RunOutcome.ERROR, task_id)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            # The entry is completed only once the runtime timer is durably
            # stopped: a completed entry is never claimed again, so a timer left
            # running behind it would keep ``running_since`` set and accrue runtime
            # for good. When the stop fails (a transient database error, say) the
            # entry stays claimed; its lease expires and the next worker that
            # claims it settles the timer (``_begin_task``) or takes the run over.
            timer_stopped = await self._stop_runtime(run)
            if complete_entry and timer_stopped and not run.lost.is_set():
                await self._complete_entry(run)

    async def _fail_safely(self, run: _Run) -> bool:
        """After an unexpected error: fail the task (for this run only), so that it
        is not left ``running`` with no work behind it; a human can retry it.

        ``True``: the task no longer runs (failed now, or it had moved on), and the
        entry may be completed. ``False``: the failure could not be written (the
        database is unreachable, say): the entry must stay claimed, its lease will
        expire and the next worker takes the run over.
        """
        if run.epoch:
            # The nodes this run left running are ready again (their attempts
            # interrupted), as a later owner's ``acquire`` would make them. This
            # must succeed BEFORE the task is failed: a failed task is restarted
            # into a new DAG, and nobody would ever take this one over again, so
            # its attempts would stay "running" for ever. When it fails, the task
            # keeps running and the entry claimed: the next worker's take-over
            # interrupts them. (``StaleDagEpochError``: another worker took the DAG
            # over already, and so interrupted them; the entry is its.)
            try:
                dag = await self._store.get(run.task.id, run.run.attempt)
                if dag is not None:
                    await self._store.interrupt(dag.id, run.epoch)
            except StaleDagEpochError:
                return False
            except Exception as error:
                logger.error(
                    "Interrupting the nodes after an error failed (%s); the entry "
                    "is left claimed until its lease expires",
                    error_class_of(error),
                )
                return False
        try:
            await self._end_task(run, TaskCommand.FAIL, Actor.system(), REASON_INTERNAL)
        except StaleRunError:
            return True  # a newer run owns the task: nothing of ours is left to end
        except Exception as error:
            logger.error(
                "Failing the task after an error failed (%s); its lease is left to "
                "expire",
                error_class_of(error),
            )
            return False
        return True

    async def _begin_task(
        self, snapshot: TaskSnapshot, entry: QueueEntry, worker_id: str
    ) -> TaskRun | None:
        """Start the task (queued) or take over a run that lost its worker
        (running). ``None``: the task is in no state that can run, or was replaced
        while it was looked at; the entry is completed (a paused or waiting task is
        queued again by whoever unblocks it, a replaced run by whoever retried or
        restarted it).

        Start is fenced by the version of the snapshot: it is applied only to the
        task as it was seen. A queued task can change only by Start, Fail or
        Cancel, all of which change its state, so a conflict always means that the
        run this entry was claimed for is gone (nothing is started under it).
        """
        task_id = snapshot.id
        if snapshot.state is TaskState.QUEUED:
            try:
                event = await self._tasks.execute(
                    task_id,
                    TaskCommand.START,
                    actor=Actor.system(),
                    expected_version=snapshot.version,
                )
            except (IllegalTransitionError, TaskConflictError, TaskNotFoundError):
                await self._complete_quietly(entry, worker_id)
                return None
            except (ProjectNotActiveError, ProjectBusyError) as error:
                # The Project state gate (Issue #83, Decision 0020) refuses the
                # Start: the project was Archived or put in deletion after this
                # entry was claimed (the claim skips such projects), or its row
                # could not be locked in time. The task stays queued and the entry
                # goes BACK to the queue (not away): it is claimed again once the
                # project is Active, and nothing was started, so no runtime timer,
                # node or budget was touched.
                logger.info("Starting a task was refused (%s)", error_class_of(error))
                with contextlib.suppress(LeaseLostError):
                    await self._queue.release(entry.id, worker_id, entry.claim_count)
                return None
            return event.run
        if snapshot.state is TaskState.RUNNING:
            return snapshot.run
        # A task that no longer runs (paused, waiting, evaluating, failed, ...):
        # the entry is done, but a worker whose ``stop_runtime`` failed may have
        # left the runtime timer running behind it; settle it first.
        if await self._settle_runtime(entry, worker_id):
            await self._complete_quietly(entry, worker_id)
        return None

    async def _complete_quietly(self, entry: QueueEntry, worker_id: str) -> None:
        """End this worker's hold on the entry: completed, or given back when the
        task needs a worker again (``TaskQueue.finish``: a Retry, Restart, Resume
        or Unblock that committed while the entry was still claimed could not
        enqueue, since a task has one active entry; the entry then serves it)."""
        with contextlib.suppress(LeaseLostError):
            await self._queue.finish(entry.id, worker_id, entry.claim_count)

    async def _complete_entry(self, run: _Run) -> None:
        await self._complete_quietly(run.entry, run.worker_id)

    async def _stop_runtime(self, run: _Run) -> bool:
        """Stop this run's runtime session. ``True``: nothing of ours is left
        running (stopped now, never started, or superseded by a newer session).
        ``False``: the stop could not be written; the caller must not complete the
        entry (see ``_run_entry``)."""
        if run.generation is None:
            return True
        try:
            await self._budget.stop_runtime(run.task.id, run.generation)
        except StaleRuntimeSessionError:
            return True  # a newer worker's session: its timer is not ours to stop
        except Exception as error:
            logger.error(
                "Stopping the runtime timer failed (%s); the entry is left claimed "
                "until its lease expires",
                error_class_of(error),
            )
            return False
        return True

    async def _start_runtime_with_lease(
        self, entry: QueueEntry, worker_id: str, run: TaskRun | None
    ) -> int:
        """Start (or take over) the task's runtime session and prove the queue
        lease in ONE transaction; return the session's generation.

        ``start_runtime_in`` share-locks the task row (and, for ``run``, refuses a
        replaced or ended run with ``StaleRunError``) and updates the timer;
        ``heartbeat_in`` then locks the entry, judges the lease by the database
        clock and extends it. Both commit or neither does: a worker whose lease was
        given to another worker (however long an earlier call stalled) raises
        ``LeaseLostError`` and starts nothing, so it can never supersede the
        legitimate holder's session. While the transaction is open the entry row
        is locked and no other worker can claim it (``SKIP LOCKED``); after the
        commit the lease runs a whole ``lease_seconds``. Lock order: task, budget
        row, entry (no path locks them the other way round)."""
        async with self._queue.database.session() as session, session.begin():
            generation = await self._budget.start_runtime_in(
                session, entry.task_id, run=run
            )
            await self._queue.heartbeat_in(
                session, entry.id, worker_id, entry.claim_count
            )
        return generation

    async def _settle_runtime(self, entry: QueueEntry, worker_id: str) -> bool:
        """Stop a runtime session that an earlier worker left running, before an
        entry whose task no longer runs is completed.

        An earlier worker whose ``stop_runtime`` failed left its entry claimed
        (``_run_entry``); this worker claimed it after the lease expired and holds
        it now (one active entry per task, so no other worker runs the task). Only
        the lease holder may call ``start_runtime``, so the session is taken over
        in the transaction that proves the lease (``_start_runtime_with_lease``;
        ``LeaseLostError`` propagates: the entry is someone else's). Taking the
        session over keeps its ``running_since`` (the time is neither lost nor
        counted twice) and stopping it settles that time; a task with no session
        in progress gets a new one stopped at once (0 seconds). ``True``: nothing
        is left running and the entry may be completed. ``False``: the settlement
        could not be written; the entry stays claimed for the next worker.
        """
        try:
            generation = await self._start_runtime_with_lease(entry, worker_id, None)
            await self._budget.stop_runtime(entry.task_id, generation)
        except BudgetNotConfiguredError:
            return True  # no budget: no timer
        except StaleRuntimeSessionError:
            return True  # only a lease holder starts a session: it settles its own
        except Exception as error:
            logger.error(
                "Settling a runtime timer failed (%s); the entry is left claimed "
                "until its lease expires",
                error_class_of(error),
            )
            return False
        return True

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
        """Extend the lease every ``heartbeat_seconds``; lose the run (fail
        closed) before the lease can expire (invariant I11).

        The last proof of the lease STARTED at ``proved`` (``run.proved_at`` for
        the heartbeat just before the runtime timer started), so the lease runs
        at least until ``proved + lease_seconds``. The run is lost when
        ``HEARTBEAT_FAILURES_TO_LOSE`` attempts in a row fail, or at the latest
        ``HEARTBEAT_FAILURES_TO_LOSE × heartbeat_seconds`` after ``proved``
        (strictly less than the lease: checked when the orchestrator is built),
        whichever comes first. Every attempt has a deadline
        (``HEARTBEAT_TIMEOUT_FRACTION`` of the interval, and never past the loss
        deadline): a heartbeat whose connection or query stalls counts as a
        failure instead of holding the loop up while the lease runs out, and is
        abandoned (cancelled). All the times are the injected clock's."""
        interval = self._heartbeat_seconds
        lose_after = interval * HEARTBEAT_FAILURES_TO_LOSE
        deadline = run.proved_at + lose_after
        failures = 0
        while True:
            await self._clock.sleep(
                max(0.0, min(interval, deadline - self._clock.monotonic()))
            )
            remaining = deadline - self._clock.monotonic()
            if remaining <= 0:
                run.lose()
                return
            started = self._clock.monotonic()
            try:
                await self._within(
                    self._queue.heartbeat(
                        run.entry.id, run.worker_id, run.entry.claim_count
                    ),
                    min(interval * HEARTBEAT_TIMEOUT_FRACTION, remaining),
                )
            except LeaseLostError:
                run.lose()
                return
            except Exception as error:
                failures += 1
                logger.warning("Lease heartbeat failed (%s)", error_class_of(error))
                if (
                    failures >= HEARTBEAT_FAILURES_TO_LOSE
                    or self._clock.monotonic() >= deadline
                ):
                    run.lose()  # fail closed: a lease we cannot extend is lost
                    return
                continue
            failures = 0
            deadline = started + lose_after

    async def _within(self, awaitable: Awaitable[object], seconds: float) -> object:
        """``await awaitable`` for at most ``seconds`` of the injected clock;
        ``HeartbeatTimeoutError`` (and the awaitable is cancelled and left to
        end) when the time is up first."""
        call = asyncio.ensure_future(awaitable)
        timer = asyncio.ensure_future(self._clock.sleep(seconds))
        try:
            await asyncio.wait({call, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            timed_out = not call.done()
            if timed_out:
                call.cancel()
                call.add_done_callback(_forget)
        if timed_out:
            raise HeartbeatTimeoutError()
        return call.result()

    async def _start_node(
        self, run: _Run, dag_id: uuid.UUID, key: str
    ) -> AttemptRecord:
        """Start one attempt of a node: ONE transaction checks that the task is
        running in this run (under the task row's share lock), makes the attempt
        durable (the node ``running``, its attempt row) and charges its step. They
        commit together or not at all (a refused start charges nothing, a charged
        step always has its attempt), and the runtime is launched only after the
        commit. A pause or a wait that commits after it is a graceful stop: the
        attempt finishes and its outcome is accepted, nothing new starts."""
        async with self._queue.database.session() as session, session.begin():
            attempt = await self._store.start_node_in(
                session,
                dag_id,
                run.epoch,
                key,
                max_attempts=self._config.max_attempts_per_rung,
            )
            await self._budget.record_in(
                session,
                run.task.id,
                BudgetKind.STEPS,
                1,
                run=run.run,
                require_running=True,
            )
        return attempt

    async def _start_planner_call(
        self, run: _Run, planned: Mapping[BudgetKind, int]
    ) -> None:
        """Start one planner call: ONE transaction checks that the task is running
        in this run and charges the call's step (and, after the first call, its
        retry). That charge is the call's durable start record; the planner is
        launched only after it committed. A pause or a wait that commits after it
        is a graceful stop, as for a node: the call finishes, and its plan is kept
        (``DagStore.create(run=)``: fenced to the run, refused for an ended task),
        like the outcome of a node that was running; no node of it starts until
        the task runs again (``start_node`` requires ``running``)."""
        async with self._queue.database.session() as session, session.begin():
            for kind, amount in planned.items():
                await self._budget.record_in(
                    session,
                    run.task.id,
                    kind,
                    amount,
                    run=run.run,
                    require_running=True,
                )

    async def _hand_on_ended_dag(self, run: _Run, dag: DagRecord) -> RunReport:
        """Hand the task on as a DAG that ended for good says: back to evaluation
        after a Retry of a failed evaluation (the results stand), or failed for a
        crash between closing the DAG and failing the task. The DAG is taken over
        (bound to the lease, ``_acquire_dag``: it records the run and fences the
        worker before) but nothing runs, so neither the budget nor the timer is
        involved; the task command is fenced by the run (``_end_task``).

        With a ``NodeWorkspaces`` (PAW-035), handing a succeeded DAG on integrates
        the Worker branches again (a Retry after a failed integration, an unblock
        after a human resolved a conflict), which runs git: the lease is then
        kept by heartbeats as in ``_drive``, and a run whose lease is lost meanwhile
        issues no task command (``_integrate``)."""
        run.proved_at = self._clock.monotonic()
        taken = await self._acquire_dag(dag.id, run.entry, run.worker_id, run.run)
        run.epoch = taken.epoch
        if taken.state is DagState.ACTIVE:  # re-opened meanwhile: not ended
            raise DagStateError()
        await self._log(run, LogLevel.INFO, f"The DAG had already {taken.state.value}")
        if self._worktrees is None or taken.state is not DagState.SUCCEEDED:
            return await self._finish_dag(run, taken, None)
        heartbeat = asyncio.create_task(self._heartbeats(run))
        try:
            return await self._finish_dag(run, taken, None)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _drive_until_lost(self, run: _Run) -> RunReport:
        """``_drive``, but never past a lost lease.

        ``_drive`` reacts to a lost lease itself whenever it waits (the lost
        waiter), but a call it is in (a database read or write that stalls) can
        outlast the lease. When the heartbeats lose the run, ``_drive`` is given
        ``heartbeat_seconds`` (the injected clock) to end on its own (closing its
        nodes as it does); after that it is cancelled (its ``finally`` cancels the
        running attempts) and the run ends as ``LEASE_LOST``. Every write it could
        still have in flight is fenced (epoch, run, claim or generation)."""
        drive = asyncio.ensure_future(self._drive(run))
        lost = asyncio.ensure_future(run.lost.wait())
        try:
            await asyncio.wait({drive, lost}, return_when=asyncio.FIRST_COMPLETED)
            if not drive.done():
                grace = asyncio.ensure_future(
                    self._clock.sleep(self._heartbeat_seconds)
                )
                try:
                    await asyncio.wait(
                        {drive, grace}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    grace.cancel()
            if drive.done():
                return drive.result()
            await _cancel_and_wait([drive], 2 * CANCEL_GRACE_SECONDS)
            return RunReport(RunOutcome.LEASE_LOST, run.task.id)
        except asyncio.CancelledError:
            await _cancel_and_wait([drive], 2 * CANCEL_GRACE_SECONDS)
            raise
        finally:
            lost.cancel()

    # -- the DAG ------------------------------------------------------------------

    async def _drive(self, run: _Run) -> RunReport:
        task_id = run.task.id
        dag = await self._store.get(task_id, run.run.attempt)
        if dag is None:
            planned = await self._plan(run)
            if isinstance(planned, RunReport):
                return planned
            dag = planned
        if run.lost.is_set():  # the heartbeats gave up while it planned
            return RunReport(RunOutcome.LEASE_LOST, task_id)
        dag = await self._acquire_dag(dag.id, run.entry, run.worker_id, run.run)
        run.epoch = dag.epoch
        if dag.state is not DagState.ACTIVE:
            # A DAG that already ended is not run again (one DAG per task attempt;
            # Decision 0021, section 6). Usually seen before the budget check
            # (``_run_entry``, ``_hand_on_ended_dag``); here when it ended since.
            # (A cancelled DAG cannot be taken over: ``acquire`` refused it.)
            await self._log(
                run, LogLevel.INFO, f"The DAG had already {dag.state.value}"
            )
            return await self._finish_dag(run, dag, None)
        running: dict[str, asyncio.Task[_Finished]] = {}
        numbers: dict[str, int] = {}
        stop: _Stop | None = None
        lost_waiter = asyncio.create_task(run.lost.wait())
        try:
            while True:
                watch = await self._watch(run)
                if watch is _Watch.LOST or watch is _Watch.SUPERSEDED:
                    await self._cancel_all(running)
                    if watch is _Watch.LOST:
                        await self._close_dag_of_a_cancelled_task(run, dag)
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
                elif watch is _Watch.WAITING:
                    # Waiting (an approval, a user, a resource) is a graceful stop
                    # like a pause: no new node starts, the running ones finish,
                    # and the task stays waiting until whoever unblocks it.
                    stop = stop or _Stop(_StopKind.HOLD)
                elif stop is not None and stop.kind in (
                    _StopKind.PAUSE,
                    _StopKind.HOLD,
                ):
                    stop = None  # resumed / unblocked while the nodes were finishing

                # The whole budget is looked at every time the loop wakes (a node
                # ended, or the poll fired): the runtime accrues in the tracker and
                # no node reports it, so nothing else would stop a node that runs
                # past its limit, or one that never ends.
                exhausted = await self._exhausted_budget(run)
                if exhausted is not None:
                    finished_now = {k for k, task in running.items() if task.done()}
                    dag, extra = await self._process_done(
                        run, dag, running, numbers, finished_now
                    )
                    stop = self._sooner(stop, extra)
                    await self._cancel_all(running)
                    numbers.clear()
                    # The nodes that were still running are ready again, their
                    # attempts interrupted (the next run takes them up).
                    dag = await self._store.interrupt(dag.id, run.epoch)
                    stop = self._sooner(stop, exhausted)
                    break

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
                dag, extra = await self._process_done(run, dag, running, numbers, done)
                stop = stop or extra
        finally:
            lost_waiter.cancel()
            await self._cancel_all(running)
            await asyncio.gather(lost_waiter, return_exceptions=True)

        return await self._finish_dag(run, dag, stop)

    async def _acquire_dag(
        self, dag_id: uuid.UUID, entry: QueueEntry, worker_id: str, run: TaskRun
    ) -> DagRecord:
        """Take the DAG over, bound to the queue lease in ONE transaction.

        Planning can take up to a node timeout, and the lease may have been lost
        and the entry claimed by another worker meanwhile (which may have taken
        the DAG over already). A heartbeat before a separate take-over would leave
        a window in which a stale worker raises the epoch after its replacement
        did and fences it (both would then stop). So the take-over
        (``DagStore.acquire_in``, DAG row locked) and the proof of the lease
        (``TaskQueue.heartbeat_in``, entry row locked until the commit, which
        a competing claim skips) commit together or not at all: ``LeaseLostError``
        rolls the epoch back, and a committed take-over was made by the holder of
        a valid lease whose lease then runs for a whole ``lease_seconds``. This is
        the only place where the orchestrator raises an epoch."""
        async with self._queue.database.session() as session, session.begin():
            dag = await self._store.acquire_in(session, dag_id, worker_id, run)
            await self._queue.heartbeat_in(
                session, entry.id, worker_id, entry.claim_count
            )
        return dag

    async def _close_dag_of_a_cancelled_task(self, run: _Run, dag: DagRecord) -> None:
        """After a lost lease, best effort: a task whose entry was cancelled with
        it (the stop of a deleted project cancels both in one transaction) is not
        run by anybody again, so its DAG would stay ``active`` with running nodes
        for ever. When the task is cancelled, close the DAG as a Cancel would; the
        write is fenced by this run's epoch (and so refused if another worker took
        the DAG over). Any failure is only logged."""
        # Nobody can take this DAG over again (the entry was cancelled with the
        # task), so a transient failure is tried again a few times rather than
        # left to a later owner.
        for _ in range(COMMAND_ATTEMPTS):
            try:
                snapshot = await self._tasks.restore(run.task.id, log_limit=0)
                if snapshot.state is TaskState.CANCELLED and snapshot.run == run.run:
                    await self._store.cancel(dag.id, run.epoch)
                return
            except (StaleDagEpochError, TaskNotFoundError):
                return  # another worker owns it, or the task is gone
            except Exception as error:
                logger.info(
                    "Closing the DAG of a cancelled task failed (%s)",
                    error_class_of(error),
                )

    async def _process_done(
        self,
        run: _Run,
        dag: DagRecord,
        running: dict[str, asyncio.Task[_Finished]],
        numbers: dict[str, int],
        done: set[str],
    ) -> tuple[DagRecord, _Stop | None]:
        """Write the outcomes of the nodes that ended, in node order."""
        stop: _Stop | None = None
        for key in sorted(done, key=lambda k: dag.node(k).ordinal):
            task = running.pop(key)
            finished = self._result_of(task)
            try:
                dag, extra = await self._settle(
                    run, dag, dag.node(key), numbers.pop(key), finished
                )
            except StaleRunError:
                # The store refused the outcome: the task ended, or a Retry /
                # Restart replaced this run, after the last look at it (the DAG is
                # fenced by the task's run too). Nothing was written; the next look
                # (``_watch``) sees why and closes the run.
                break
            stop = stop or extra
        return dag, stop

    async def _exhausted_budget(self, run: _Run) -> _Stop | None:
        """What Decision 0007 does with the task's budget as it is now: ``None``
        while every limit holds; otherwise the stop (``retries``: the task fails,
        anything else: it waits for a human). The runtime of a run in progress is
        counted (the tracker adds what has elapsed)."""
        verdict = await self._budget.check(run.task.id)
        if verdict.status is not BudgetStatus.EXCEEDED:
            return None
        action = decide_next_action(
            verdict, LoopVerdict.CONTINUE, can_escalate=False
        ).action
        return self._stop_for_budget(action)

    @staticmethod
    def _sooner(current: _Stop | None, new: _Stop | None) -> _Stop | None:
        """The stop that wins when two apply: failing beats waiting beats pausing
        (a used-up retry budget must not be hidden by a pause or a wait)."""
        order = {
            _StopKind.FAIL: 0,
            _StopKind.WAIT: 1,
            _StopKind.PAUSE: 2,
            _StopKind.HOLD: 2,
        }
        candidates = [stop for stop in (current, new) if stop is not None]
        return min(candidates, key=lambda stop: order[stop.kind], default=None)

    async def _finish_dag(
        self, run: _Run, dag: DagRecord, stop: _Stop | None
    ) -> RunReport:
        task_id = run.task.id
        if stop is not None:
            return await self._end_after_stop(run, stop, dag)
        final = (
            await self._store.finalize(dag.id, run.epoch)
            if dag.state is DagState.ACTIVE
            else dag
        )
        if final.state is DagState.SUCCEEDED:
            integrated = await self._integrate(run, final)
            if integrated is not None:
                return integrated
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

    async def _integrate(self, run: _Run, dag: DagRecord) -> RunReport | None:
        """Integrate the Worker branches of a succeeded DAG (PAW-035) before the
        task goes to evaluation, so that the tests, the Evaluator and the review
        see the integrated result, never a single Worker's branch. ``None`` when
        the task may go on to evaluation (integrated, or nothing to integrate);
        otherwise the task was put in ``waiting`` (a conflict or uncommitted
        changes: a human decides) or ``failed`` (git could not be used; a Retry
        integrates again, the DAG's results stand) and this is the report."""
        if self._worktrees is None:
            return None
        workers = tuple(
            node.key
            for node in dag.nodes
            if node.role is NodeRole.WORKER and node.state is NodeState.SUCCEEDED
        )
        try:
            scope = await self._authority.parent_scope(run.task)
            report = await self._worktrees.integrate(
                IntegrationRequest(
                    task=run.task, run=run.run, workers=workers, scope=scope
                )
            )
            if not isinstance(report, IntegrationReport):
                raise TypeError("integrate must return an IntegrationReport")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return await self._integration_failed(run, dag, error)
        if run.lost.is_set():
            # Another worker may hold the entry now: it integrates again (the
            # merges already made are not repeated) and decides the task.
            return RunReport(RunOutcome.LEASE_LOST, run.task.id, dag.state)
        for repository in report.repositories:
            await self._log(run, *_integration_line(repository))
        try:
            await self._record_integration(run, report)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return await self._integration_failed(run, dag, error)
        if report.clean:
            return None
        ended = await self._end_task(
            run,
            TaskCommand.WAIT,
            Actor.system(),
            REASON_INTEGRATION_CONFLICT,
            WaitReason.USER,
        )
        return RunReport(
            RunOutcome.INTEGRATION_CONFLICT if ended else RunOutcome.TASK_ENDED,
            run.task.id,
            dag.state,
        )

    async def _integration_failed(
        self, run: _Run, dag: DagRecord, error: Exception
    ) -> RunReport:
        """Integrating (or recording it) failed: the task fails, and a Retry
        integrates again (the DAG's results stand). The cause is named by its
        reason or its class only."""
        if run.lost.is_set():
            return RunReport(RunOutcome.LEASE_LOST, run.task.id, dag.state)
        cause = (
            error.reason.value
            if isinstance(error, WorktreeUnavailableError)
            else error_class_of(error)
        )
        await self._log(run, LogLevel.ERROR, f"Integration failed ({cause})")
        ended = await self._end_task(
            run, TaskCommand.FAIL, Actor.system(), REASON_INTEGRATION_FAILED
        )
        return RunReport(
            RunOutcome.INTEGRATION_FAILED if ended else RunOutcome.TASK_ENDED,
            run.task.id,
            dag.state,
        )

    async def _record_integration(self, run: _Run, report: IntegrationReport) -> None:
        """Each integrated repository's integration branch, worktree and commit
        go into its own state in the attempt (``TaskService.update_attempt`` with
        its ``repository_id``; Decision 0036, 11): a Multi-Repo task records every
        repository. The new HEAD resets that repository's review and evaluation
        results (they belonged to another revision). A repository the attempt
        does not have (``RepositoryNotInAttemptError``) is skipped; any other
        failure propagates (Codex P2 on PR #130): the task must not go on to
        evaluate a HEAD the attempt does not name, and the caller fails it so
        that a Retry integrates and records again."""
        for repository in report.repositories:
            if repository.state is not IntegrationState.MERGED:
                continue
            try:
                await self._tasks.update_attempt(
                    run.task.id,
                    run=run.run,
                    repository_id=repository.repo_id,
                    worktree=WorktreeState(
                        repository.branch, repository.path, repository.head
                    ),
                )
            except StaleRunError:
                run.guard.stop(StopReason.SUPERSEDED)
                return
            except RepositoryNotInAttemptError:
                continue

    async def _end_after_stop(
        self, run: _Run, stop: _Stop, dag: DagRecord | None
    ) -> RunReport:
        task_id = run.task.id
        state = None if dag is None else dag.state
        if stop.kind is _StopKind.PAUSE:
            return RunReport(RunOutcome.PAUSED, task_id, state)
        if stop.kind is _StopKind.HOLD:
            return RunReport(RunOutcome.WAITING, task_id, state)
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
        """Issue a task command **for this run only**.

        The task is read afresh and the command is applied with the version that
        was read (``expected_version``), so the comparison and the transition are one
        transaction: a task that was failed, retried and started again meanwhile has
        a newer run, and the command of the old run is refused with a typed
        ``StaleAttemptError`` / ``StaleRunError`` (nothing is written; the caller
        stops). ``False`` when the task no longer accepts the command (it was
        cancelled, completed, ...). A change of the task that commits between the
        read and the write and leaves the run alone (a Pause and a Resume, say) makes
        the command conflict; it is then decided again from a new read, up to
        ``COMMAND_ATTEMPTS`` times.
        """
        for _ in range(COMMAND_ATTEMPTS):
            try:
                snapshot = await self._tasks.restore(run.task.id, log_limit=0)
            except TaskNotFoundError:
                return False
            if snapshot.run != run.run:
                raise (
                    StaleAttemptError()
                    if snapshot.run.attempt != run.run.attempt
                    else StaleRunError()
                )
            try:
                await self._tasks.execute(
                    run.task.id,
                    command,
                    actor=actor,
                    reason=reason,
                    wait_reason=wait_reason,
                    expected_version=snapshot.version,
                )
            except IllegalTransitionError:
                return False
            except TaskConflictError:
                continue
            return True
        raise TaskConflictError()

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
        if state is TaskState.PAUSED:
            return _Watch.PAUSED
        if state is TaskState.WAITING:
            return _Watch.WAITING
        return _Watch.RUNNING

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
                try:
                    dag = await self._store.give_up_node(
                        dag.id, run.epoch, key, error_class="AttemptsExhausted"
                    )
                except StaleRunError:
                    return None  # the run is over: ``_watch`` says why
                continue
            verdict = await self._budget.check(
                run.task.id, planned={BudgetKind.STEPS: 1}
            )
            if verdict.status is BudgetStatus.EXCEEDED:
                decision = decide_next_action(
                    verdict, LoopVerdict.CONTINUE, can_escalate=False
                )
                return self._stop_for_budget(decision.action)
            try:
                attempt = await self._start_node(run, dag.id, key)
            except (StaleRunError, TaskNotRunningError):
                return None  # the run is over or quiesces: ``_watch`` says why
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
            upstream_workers=tuple(
                dependency
                for dependency in node.depends_on
                if dag.node(dependency).role is NodeRole.WORKER
            ),
            dag_id=dag.id,
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
        # An attempt's own wait for its runtime is bounded by the grace, so the
        # attempts themselves get twice as long before they are abandoned.
        await _cancel_and_wait(list(running.values()), 2 * CANCEL_GRACE_SECONDS)
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

    async def _context(
        self,
        run: _Run,
        spec: _Spec,
        worktrees: Mapping[uuid.UUID, NodeWorktree] | None = None,
    ) -> TaskContext:
        """The ``TaskContext`` of one tool call, from CURRENT values: the run comes
        from the task snapshot the worker was started with (``TaskEvent.run``), the
        lease from the claim this worker holds, the delegator from the task, the
        grant and scope derived from the parent's (with the node's own worktrees,
        PAW-035, when it has them)."""
        parent_grant = await self._authority.parent_grant(run.task)
        parent_scope = await self._authority.parent_scope(run.task)
        grant = node_grant(
            parent_grant,
            spec.node,
            spec.role,
            agent_id_of(
                run.task.id,
                run.run,
                PLANNER_IDENTITY if spec.node is None else spec.key,
                spec.attempt,
                # The planner's attempts are counted per claim (``_plan``).
                claim=(
                    (run.entry.id, run.entry.claim_count) if spec.node is None else None
                ),
            ),
        )
        scope = derive_child_scope(
            parent_scope,
            role=spec.role,
            repositories=None if spec.node is None else spec.node.repositories,
            worktrees=worktrees,
        )
        return TaskContext(
            task_id=run.task.id,
            delegator_id=run.task.created_by,
            grant=grant,
            scope=scope,
            primary_project_id=run.task.project_id,
            run=run.run,
            # The fencing token of every tool call (issue #126, Decision 0046):
            # the Broker refuses the call once this claim no longer holds.
            lease=QueueLease.of(run.entry, run.worker_id),
        )

    async def _attempt(self, run: _Run, spec: _Spec) -> _Finished:
        """Run one attempt through the agent runtime; only cancellation escapes."""
        fence = AttemptFence()
        try:
            # Fail early on a grant / scope problem.
            context = await self._context(run, spec)
            # Read only, and the one mapping both the runtime and the tool
            # context see: a runtime cannot clear it (the user's checkout back
            # in scope) or add a root to it (Codex review of PAW-035).
            worktrees = MappingProxyType(
                dict(await self._prepare_worktrees(run, spec, context))
            )
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
                    run.guard,
                    self._tools,
                    lambda: self._context(run, spec, worktrees),
                    fence,
                ),
                budget=NodeBudgetHandle(run.guard, self._budget, run.task.id, fence),
                worktrees=worktrees,
                placement=self._placement(run, spec, fence),
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
        except WorktreeConflictError:
            # The upstream branches conflict: the same merge conflicts again.
            return _Finished.failure(WORKTREE_CONFLICT, retryable=False)
        except WorktreeUnavailableError:
            return _Finished.failure(WORKTREE_UNAVAILABLE)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return _Finished.failure(error_class_of(error))
        finally:
            # The orchestrator no longer waits for this attempt: whatever of it may
            # still run (a runtime that ignored its cancellation) cannot act.
            fence.close()
        if outcome is _TIMED_OUT:
            return _Finished.failure(NODE_TIMEOUT)
        if not isinstance(outcome, NodeOutcome):
            return _Finished.failure(INVALID_OUTCOME)
        return _Finished(outcome=outcome)

    async def _prepare_worktrees(
        self, run: _Run, spec: _Spec, context: TaskContext
    ) -> Mapping[uuid.UUID, NodeWorktree]:
        """The dedicated worktrees of a Worker node that may write (PAW-035): one
        per repository of its scope that has a checkout and is ``working`` /
        ``target`` (``gets_worktree``). None for any other node,
        or when the orchestrator has no ``NodeWorkspaces``."""
        if (
            self._worktrees is None
            or spec.node is None
            or spec.role is not NodeRole.WORKER
            or Capability.PROJECT_REPO_WRITE not in context.grant.capabilities
            or not any(gets_worktree(r) for r in context.scope.repositories)
        ):
            return {}
        prepared = await self._worktrees.prepare_node(
            NodeWorkspaceRequest(
                task=run.task,
                run=run.run,
                node_key=spec.key,
                upstream_workers=spec.upstream_workers,
                scope=context.scope,
            )
        )
        worktrees = dict(prepared)
        # The scope is derived again with the worktrees: a worktree the
        # implementation made up for a repository outside the node's scope is a
        # ScopeEscalation, never a wider scope.
        await self._context(run, spec, worktrees)
        return worktrees

    def _placement(
        self, run: _Run, spec: _Spec, fence: AttemptFence
    ) -> NodePlacementHandle | None:
        """Where a node's attempt runs is recorded on its attempt row (issue
        #133); the planning call has no attempt row, so nothing to record it on
        (``None``: a runtime keeps it off the cloud)."""
        if spec.node is None or spec.dag_id is None:
            return None
        return NodePlacementHandle(
            run.guard,
            self._store,
            dag_id=spec.dag_id,
            epoch=run.epoch,
            node_key=spec.key,
            attempt=spec.attempt,
            agent_id=agent_id_of(run.task.id, run.run, spec.key, spec.attempt),
            content=content_digest(
                node_key=spec.key,
                role=spec.role,
                title=spec.title,
                goal=spec.goal,
                input=spec.input,
                upstream=spec.upstream,
            ),
            fence=fence,
        )

    async def _with_timeout(self, awaitable):
        work = asyncio.ensure_future(awaitable)
        timer = asyncio.ensure_future(
            self._clock.sleep(self._config.node_timeout_seconds)
        )
        try:
            await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                return work.result()
            return _TIMED_OUT
        finally:
            # Bounded: a runtime that swallows its cancellation is abandoned after
            # ``CANCEL_GRACE_SECONDS`` (``_attempt`` closes its fence), so the
            # node still fails and the run goes on.
            await _cancel_and_wait([work, timer])

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
        if run.guard.stop_reason in _RUN_ENDING_STOPS:
            # The run is over for this worker (its lease is gone, its run was
            # replaced, or the task ended): nothing of it is written, whatever the
            # attempt reports. A runtime may have caught the ``NodeStopped`` of a
            # tool call and returned an outcome anyway; after a lease that only
            # ran out, nobody has raised the DAG's epoch yet, so the store would
            # still take it (Decision 0046). The top of the loop closes the run.
            return dag, None
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
                error_class=runtime_error_class(outcome.error_class),
                message=outcome.message,
                retryable=outcome.retryable,
                escalate=outcome.escalate,
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
        * ``decide_next_action`` gets the budget verdict as it is and the loop
          verdict of the node's failures; whether an escalation is possible is
          whether the node's ladder has another agent; only a failure that WILL
          retry is then asked again with one more retry planned (a failure that
          cannot retry costs no retry: ``_retry_step``);
        * ``CONTINUE`` retries, ``TRY_ALTERNATIVE`` changes the approach,
          ``ESCALATE_AGENT`` moves to the next agent (a new approach as well: the
          loop history of the old one must not condemn the new one),
          ``WAIT_FOR_USER`` keeps the node and stops the run, ``FAIL`` gives the
          node up and stops the run;
        * a failure that says it is not retryable, a rung out of attempts and an
          approach beyond the loop detector's range give the node up;
        * a failure that asks for the next rung (``escalate``, Decision 0083,
          section 4) escalates at once when the ladder has another agent and the
          approach counter allows it, whatever the loop verdict (it skips
          ``TRY_ALTERNATIVE``); the budget still comes first. Without another
          agent it goes the ordinary way.
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
                run=run.run,
            )
            verdict = assessment.verdict
        except Exception as error:
            if isinstance(error, StaleRunError):
                raise
            logger.error("Recording a failure failed (%s)", error_class_of(error))
        ladder = self._config.ladders[node.role]
        can_escalate = node.agent_index + 1 < len(ladder)
        # Decision 0007 first, on the budget as it is: a budget that is already
        # used up stops the run whatever the failure says.
        budget = await self._budget.check(run.task.id)
        decision = decide_next_action(budget, verdict, can_escalate=can_escalate)

        stop: _Stop | None = None
        arguments: dict[str, int] = {}
        step = NextStep.GIVE_UP
        if decision.action is NextAction.FAIL:
            stop = _Stop(_StopKind.FAIL)
        elif decision.action is NextAction.WAIT_FOR_USER:
            step, stop = NextStep.HOLD, _Stop(_StopKind.WAIT)
        else:
            step, arguments = self._retry_step(
                node, decision.action, finished, can_escalate
            )
            if step in _RETRYING_STEPS:
                # A retry is reserved from the budget only for a failure that WILL
                # retry (it may, and the rung has an attempt left, or the loop
                # decision escalates): one that cannot retry costs no retry, so the
                # retries counter exactly at its limit does not stop the task for
                # it (an optional node that fails for good while every required node
                # succeeds must let the DAG succeed).
                planned = await self._budget.check(
                    run.task.id, planned={BudgetKind.RETRIES: 1}
                )
                reserved = decide_next_action(
                    planned, verdict, can_escalate=can_escalate
                )
                if reserved.action is NextAction.FAIL:
                    step, arguments, stop = NextStep.GIVE_UP, {}, _Stop(_StopKind.FAIL)
                elif reserved.action is NextAction.WAIT_FOR_USER:
                    step, arguments = NextStep.HOLD, {}
                    stop = _Stop(_StopKind.WAIT)
        if step in _RETRYING_STEPS:
            await self._budget.record(run.task.id, BudgetKind.RETRIES, 1, run=run.run)
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

    def _retry_step(
        self,
        node: NodeRecord,
        action: NextAction,
        finished: _Finished,
        can_escalate: bool,
    ) -> tuple[NextStep, dict[str, int]]:
        """Where a failed node goes for the loop decision ``action`` (before the
        budget is asked for a retry): a retrying step with its arguments, or
        ``GIVE_UP`` for a failure that cannot retry (it said so, the rung is out of
        attempts, or the approach counter is at its end)."""
        if not finished.retryable:
            return NextStep.GIVE_UP, {}
        next_approach = node.approach + 1
        # Decision 0083, section 4: a failure that says only a higher rung can
        # solve it escalates without waiting for the loop detector (which may
        # never fire: it needs the same signature three times in the task's last
        # ten failures) and without an alternative on this rung first.
        if (
            finished.escalate
            and can_escalate
            and next_approach <= MAX_APPROACH
            and action in (NextAction.CONTINUE, NextAction.TRY_ALTERNATIVE)
        ):
            action = NextAction.ESCALATE_AGENT
        if action is NextAction.ESCALATE_AGENT:
            if can_escalate and next_approach <= MAX_APPROACH:
                return NextStep.ESCALATE, {
                    "agent_index": node.agent_index + 1,
                    "approach": next_approach,
                }
            return NextStep.GIVE_UP, {}
        if node.rung_attempts >= self._config.max_attempts_per_rung:
            return NextStep.GIVE_UP, {}
        if action is NextAction.TRY_ALTERNATIVE:
            if next_approach <= MAX_APPROACH:
                return NextStep.ALTERNATIVE, {
                    "agent_index": node.agent_index,
                    "approach": next_approach,
                }
            return NextStep.GIVE_UP, {}
        if action is NextAction.CONTINUE:
            return NextStep.RETRY, {}
        return NextStep.GIVE_UP, {}

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

    async def _plan_attempt(
        self, run: _Run, spec: _Spec
    ) -> _Finished | _Stop | RunReport:
        """One call of the planner, watched the way the running nodes are.

        The runtime accrues in the tracker and the planner reports none of it, so
        nothing but this look would stop a planner that runs past the limit, or one
        that never returns (its own timeout may be much longer than what is left).
        Every ``poll_seconds`` (and when the lease is lost) the task's state and the
        whole budget are read: a task that ended, a run that was replaced or a lost
        lease stops the planner (the ``RunReport`` says how the run ends), a used-up
        budget stops it and returns the ``_Stop`` Decision 0007 decides. The planner
        is always cancelled before this returns (its attempt is never left running).
        A pause lets the planner finish, like a node. ``_Finished`` is what the
        planner returned (it may return at any moment, also with the budget used up:
        the run then ends at the first look of ``_drive``, the plan kept).
        """
        work = asyncio.create_task(self._attempt(run, spec))
        lost_waiter = asyncio.create_task(run.lost.wait())
        try:
            while True:
                timer = asyncio.create_task(
                    self._clock.sleep(self._config.poll_seconds)
                )
                try:
                    await asyncio.wait(
                        {work, timer, lost_waiter}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    timer.cancel()
                    await asyncio.gather(timer, return_exceptions=True)
                if work.done():
                    return self._result_of(work)
                watch = await self._watch(run)
                if watch not in (_Watch.RUNNING, _Watch.PAUSED, _Watch.WAITING):
                    return RunReport(
                        _OUTCOME_OF_WATCH.get(watch, RunOutcome.TASK_ENDED),
                        run.task.id,
                    )
                exhausted = await self._exhausted_budget(run)
                if exhausted is not None:
                    return exhausted
        finally:
            await _cancel_and_wait([work, lost_waiter], 2 * CANCEL_GRACE_SECONDS)

    async def _plan(self, run: _Run) -> DagRecord | RunReport:
        """Ask the planner role for a plan until one is accepted (at most
        ``max_plan_attempts`` times). The planner proposes; ``Plan`` judges: an
        invalid plan is a failed attempt like any other (recorded for loop
        detection under the step ``PLANNER_IDENTITY``), and the next attempt is
        made by the next agent of the planner ladder."""
        task_id = run.task.id
        ladder = self._config.ladders[NodeRole.PLANNER]
        for attempt in range(1, self._config.max_plan_attempts + 1):
            watch = await self._watch(run)
            if watch is not _Watch.RUNNING:
                return RunReport(
                    _OUTCOME_OF_WATCH.get(watch, RunOutcome.TASK_ENDED), task_id
                )
            # Every call of the planner is a step; a call after the first is also a
            # retry (Decision 0021, section 3: the planner follows the same rules
            # as a node, whose retries are charged to ``retries``). The retry is
            # reserved only here, when it is about to be made: a failure that
            # cannot retry leaves the loop below without charging one.
            planned = {BudgetKind.STEPS: 1}
            if attempt > 1:
                planned[BudgetKind.RETRIES] = 1
            verdict = await self._budget.check(task_id, planned=planned)
            if verdict.status is BudgetStatus.EXCEEDED:
                action = decide_next_action(
                    verdict, LoopVerdict.CONTINUE, can_escalate=False
                ).action
                return await self._end_after_stop(
                    run, self._stop_for_budget(action), None
                )
            await self._start_planner_call(run, planned)
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
            finished = await self._plan_attempt(run, spec)
            if isinstance(finished, RunReport):
                return finished
            if isinstance(finished, _Stop):  # the budget ran out while planning
                return await self._end_after_stop(run, finished, None)
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
                failure = runtime_error_class(outcome.error_class)
                message = outcome.message
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
                    dag = await self._store.create(
                        task_id, run.run.attempt, accepted, run=run.run
                    )
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
            if format_error_class(failure) in OUT_OF_MEMORY_CLASSES:
                # A node's is written with its failure (``fail_node``); the
                # planner has no node, so its own (Decision 0071).
                try:
                    await self._store.record_incident(
                        IncidentKind.OUT_OF_MEMORY, task_id=task_id
                    )
                except Exception as error:
                    logger.error(
                        "Recording an incident failed (%s)", error_class_of(error)
                    )
            try:
                await self._loops.record_failure(
                    task_id,
                    attempt=run.run.attempt,
                    error_class=format_error_class(failure),
                    step=PLANNER_IDENTITY,
                    message=format_failure_text(message),
                    run=run.run,
                )
            except StaleRunError:
                raise
            except Exception as error:
                logger.error("Recording a failure failed (%s)", error_class_of(error))
            if not retryable:
                break
        await self._end_task(run, TaskCommand.FAIL, Actor.system(), REASON_PLAN_FAILED)
        return RunReport(RunOutcome.PLAN_FAILED, task_id)


def _integration_line(repository: RepositoryIntegration) -> tuple[LogLevel, str]:
    """The fixed task-log line of one repository's integration (PAW-035): ids,
    node keys and counts only, never a path or a file name."""
    name = f"Integration of repository {repository.repo_id}"
    state = repository.state
    if state is IntegrationState.MERGED:
        return LogLevel.INFO, f"{name}: {len(repository.merged)} branch(es) merged"
    if state is IntegrationState.NOTHING:
        return LogLevel.INFO, f"{name}: nothing to integrate"
    if state is IntegrationState.CONFLICT:
        return (
            LogLevel.WARNING,
            f"{name}: the branch of node {repository.blocking_node} conflicts"
            f" ({len(repository.conflicted_files)} file(s))",
        )
    where = (
        "the integration worktree"
        if repository.blocking_node is None
        else f"the worktree of node {repository.blocking_node}"
    )
    return LogLevel.WARNING, f"{name}: {where} has uncommitted changes"


def _ended_for_good(dag: DagRecord, run: TaskRun) -> bool:
    """Whether a take-over by ``run`` leaves the DAG ended: it succeeded (never
    re-opened, Decision 0021, section 6), or it failed and ``run`` is not a later
    Retry (only a Retry re-opens a failed DAG, ``DagStore.acquire_in``)."""
    if dag.state is DagState.SUCCEEDED:
        return True
    return dag.state is DagState.FAILED and run.retry_count <= dag.task_retry_count


_TIMED_OUT = object()
_OUTCOME_OF_WATCH = {
    _Watch.LOST: RunOutcome.LEASE_LOST,
    _Watch.SUPERSEDED: RunOutcome.SUPERSEDED,
    _Watch.PAUSED: RunOutcome.PAUSED,
    _Watch.WAITING: RunOutcome.WAITING,
}
_OUTCOME_OF_STOP = {
    StopReason.LEASE_LOST: RunOutcome.LEASE_LOST,
    StopReason.SUPERSEDED: RunOutcome.SUPERSEDED,
}
