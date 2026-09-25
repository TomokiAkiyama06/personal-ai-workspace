"""Shared fixtures for the PAW-034 tests (not a test module: no ``test_`` prefix)."""

import asyncio
import heapq
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import text

from paw_backend.authz import AgentGrant, Capability, ProjectState
from paw_backend.orchestrator.config import OrchestratorConfig
from paw_backend.orchestrator.orchestrator import Orchestrator
from paw_backend.orchestrator.plan import Plan
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.runtime import NodeAssignment, NodeOutcome
from paw_backend.orchestrator.store import DagStore
from paw_backend.tasks import TaskRun, TaskService
from paw_backend.tasks.queueing import (
    BudgetPreset,
    BudgetTracker,
    LoopDetector,
    TaskQueue,
)
from paw_backend.tools import PostgresTaskActivity, TaskScope

from .authz_support import uid
from .task_support import PostgresTaskTestCase, new_database, requires_postgres

__all__ = [
    "PARENT_AGENT",
    "ROOT",
    "FakeAuthority",
    "FakeRuntime",
    "FakeTools",
    "Harness",
    "ManualClock",
    "PostgresOrchestratorTestCase",
    "SpyBudget",
    "diamond",
    "fail",
    "hang",
    "make_plan",
    "node",
    "ok",
    "quiet",
    "requires_postgres",
    "until",
]

TABLES = (
    "agent_dag_node_attempts",
    "agent_dag_edges",
    "agent_dag_nodes",
    "agent_dags",
)
PARENT_AGENT = uid(900)
ROOT = "/srv/paw-orch/worktree"


def node(key: str, *depends_on: str, role: str = "worker", **overrides) -> dict:
    data = {
        "key": key,
        "role": role,
        "title": f"Node {key}",
        "goal": f"Do {key}",
        "depends_on": list(depends_on),
    }
    data.update(overrides)
    return data


def make_plan(*nodes: dict) -> Plan:
    return Plan.from_mapping({"nodes": list(nodes)})


def diamond() -> Plan:
    """a -> (b, c) -> d, and an independent e."""
    return make_plan(
        node("a", role="researcher"),
        node("b", "a"),
        node("c", "a"),
        node("d", "b", "c", role="reviewer"),
        node("e"),
    )


# -- time, agents and rights --------------------------------------------------------


class ManualClock:
    """Time that only moves when the test says so (``advance``)."""

    def __init__(self) -> None:
        self._now = 0.0
        self._sleepers: list[tuple[float, int, float, asyncio.Future]] = []
        self._counter = 0

    def monotonic(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self._counter += 1
        heapq.heappush(
            self._sleepers, (self._now + seconds, self._counter, seconds, future)
        )
        await future

    @property
    def sleeping(self) -> int:
        """How many sleeps are waiting (cancelled ones do not count)."""
        return sum(1 for *_, future in self._sleepers if not future.done())

    def waiting_for(self, seconds: float) -> int:
        """How many sleeps of exactly ``seconds`` are waiting: a test that must
        act after the orchestrator started a particular wait (its poll of the
        task's state, a heartbeat) looks for that one."""
        return sum(
            1
            for _, _, duration, future in self._sleepers
            if duration == seconds and not future.done()
        )

    def waiting_between(self, low: float, high: float) -> int:
        """How many sleeps of ``low`` to ``high`` seconds are waiting (a wait whose
        length is computed, such as a back-off, is found by its range)."""
        return sum(
            1
            for _, _, duration, future in self._sleepers
            if low <= duration <= high and not future.done()
        )

    async def advance(self, seconds: float) -> None:
        """Move time forward; every sleep that falls due wakes, in order."""
        target = self._now + seconds
        while self._sleepers and self._sleepers[0][0] <= target:
            deadline, _, _, future = heapq.heappop(self._sleepers)
            self._now = max(self._now, deadline)
            if not future.done():
                future.set_result(None)
            await self.settle()
        self._now = target
        await self.settle()

    @staticmethod
    async def settle(rounds: int = 40) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0)


Behaviour = Callable[[NodeAssignment], Awaitable[NodeOutcome]] | NodeOutcome | Exception


def ok(summary: str = "done", **fields) -> NodeOutcome:
    return NodeOutcome.succeeded(NodeResult(summary, **fields))


def fail(error_class="Boom", message="it broke", *, retryable=True) -> NodeOutcome:
    return NodeOutcome.failed(error_class, message, retryable=retryable)


class FakeRuntime:
    """A scripted agent runtime.

    ``script`` maps a node key to what its attempts do, one entry per attempt (the
    last entry repeats): an outcome, an exception to raise, or an async function of
    the assignment. A key without a script succeeds. ``timeline`` (shared between
    runtimes) records ``("start", key, attempt)`` and ``("end", key, attempt)``.
    """

    def __init__(self, label: str = "local", script=None, timeline=None) -> None:
        self.label = label
        self.script: dict[str, list[Behaviour]] = {
            key: list(value) if isinstance(value, list) else [value]
            for key, value in (script or {}).items()
        }
        self.timeline: list[tuple[str, str, int]] = (
            timeline if timeline is not None else []
        )
        self.assignments: list[NodeAssignment] = []
        self.gates: dict[str, asyncio.Event] = {}

    def gate(self, key: str) -> asyncio.Event:
        """A node started while its gate exists waits for ``set()``."""
        return self.gates.setdefault(key, asyncio.Event())

    def calls_of(self, key: str) -> list[NodeAssignment]:
        return [a for a in self.assignments if a.node_key == key]

    async def run_node(self, assignment: NodeAssignment) -> NodeOutcome:
        self.assignments.append(assignment)
        key = assignment.node_key
        self.timeline.append(("start", key, assignment.attempt))
        try:
            if key in self.gates:
                await self.gates[key].wait()
            behaviours = self.script.get(key)
            if not behaviours:
                return ok(f"{key} by {self.label}")
            index = len(self.calls_of(key)) - 1
            behaviour = behaviours[min(index, len(behaviours) - 1)]
            if isinstance(behaviour, Exception):
                raise behaviour
            if isinstance(behaviour, NodeOutcome):
                return behaviour
            return await behaviour(assignment)
        finally:
            self.timeline.append(("end", key, assignment.attempt))


async def hang(_assignment: NodeAssignment) -> NodeOutcome:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


class FakeAuthority:
    """The caller's ``TaskAuthority``: a fixed grant and a fixed scope."""

    def __init__(self, *, repositories=(), capabilities=None, handles=None) -> None:
        self.repositories = tuple(repositories)
        self.capabilities = frozenset(
            capabilities
            if capabilities is not None
            else {
                Capability.PROJECT_READ,
                Capability.PROJECT_MEMORY_USE,
                Capability.PROJECT_TASK_RUN,
                Capability.PROJECT_REPO_WRITE,
                Capability.PROJECT_PR_CREATE,
            }
        )
        self.handles = handles if handles is not None else {}
        self.grant_calls = 0
        self.scope_calls = 0

    async def parent_grant(self, task) -> AgentGrant:
        self.grant_calls += 1
        return AgentGrant(PARENT_AGENT, self.capabilities, {task.project_id})

    async def parent_scope(self, task) -> TaskScope:
        self.scope_calls += 1
        return TaskScope(
            path_roots=[ROOT],
            hosts=["github.com"],
            projects={task.project_id: ProjectState.ACTIVE},
            credential_handles=self.handles,
            repositories=self.repositories,
        )


class FakeTools:
    """A tool runner that only records the calls it is handed."""

    def __init__(self) -> None:
        self.calls: list = []

    async def run(self, call, *, approval_id=None):
        self.calls.append(call)
        return "ran"


class Harness:
    """A whole orchestrator over real components, with the agents faked."""

    def __init__(self, database, **options) -> None:
        self.database = database
        self.clock = options.pop("clock", None) or ManualClock()
        self.tasks = TaskService(database, listeners=options.pop("task_listeners", ()))
        self.queue = options.pop("queue", None) or TaskQueue(database)
        self.budget = options.pop("budget", None) or BudgetTracker(database)
        self.loops = LoopDetector(database)
        self.store = DagStore(database)
        self.runtimes = options.pop("runtimes", None) or {"local": FakeRuntime()}
        self.authority = options.pop("authority", None) or FakeAuthority()
        self.tools = options.pop("tools", None) or FakeTools()
        ladder = options.pop("ladder", ("local",))
        ladders = options.pop("ladders", None)
        settings = {
            "max_parallel_nodes": 4,
            "node_timeout_seconds": 600.0,
            "poll_seconds": 2.0,
            "retry_backoff_seconds": 0.0,
        }
        settings.update(options.pop("config", {}))
        self.config = (
            OrchestratorConfig(ladders, **settings)
            if ladders is not None
            else OrchestratorConfig.uniform(ladder, **settings)
        )
        self.activity = options.pop("activity", None) or PostgresTaskActivity(database)
        self.orchestrator = Orchestrator(
            tasks=self.tasks,
            queue=self.queue,
            budget=self.budget,
            loops=self.loops,
            store=self.store,
            activity=self.activity,
            tools=self.tools,
            authority=self.authority,
            runtimes=self.runtimes,
            config=self.config,
            clock=self.clock,
        )
        assert not options, options


class PostgresOrchestratorTestCase(PostgresTaskTestCase):
    """Real tasks (PAW-032) in a migrated database and empty PAW-034 tables."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Always as the owner of the schema, also when the test's own connections
        # use the unprivileged application role (test_orchestrator_grants).
        await self.owner_sql(
            "TRUNCATE " + ", ".join(TABLES) + ", queue_entries, budget_usages,"
            " loop_failure_signatures"
        )
        self.store = DagStore(self.database)

    async def owner_sql(self, sql: str, **parameters) -> None:
        owner = new_database()
        try:
            async with owner.engine.begin() as connection:
                await connection.execute(text(sql), parameters)
        finally:
            await owner.dispose()

    async def rows(self, sql: str, **parameters) -> list[dict]:
        async with self.database.engine.connect() as connection:
            result = await connection.execute(text(sql), parameters)
            return [dict(row) for row in result.mappings()]

    def new_store(self) -> DagStore:
        """A store on its own engine, as a second worker process would have."""
        return DagStore(self.new_database())

    def harness(self, **options) -> Harness:
        """A whole orchestrator (its own engine, like a worker process)."""
        return Harness(self.new_database(), **options)

    async def make_dag(self, plan: Plan | None = None, *, task_id=None):
        task_id = task_id or await self.create_task()
        return await self.store.create(task_id, 1, plan or diamond())

    async def taken_dag(self, plan: Plan | None = None, owner: str = "w1"):
        """A DAG that worker ``owner`` has taken over (epoch 1)."""
        dag = await self.make_dag(plan)
        return await self.store.acquire(dag.id, owner, TaskRun(1, 0))

    async def prepare(
        self,
        harness: Harness,
        plan: Plan | None = None,
        *,
        preset: BudgetPreset = BudgetPreset.STANDARD,
    ) -> uuid.UUID:
        """A task with a stored plan (if given), a budget and a queue entry."""
        task_id = await self.create_task()
        if plan is not None:
            await harness.orchestrator.submit_plan(task_id, plan)
        await harness.orchestrator.enqueue_task(task_id, preset=preset)
        return task_id

    async def states_of(self, task_id, attempt: int = 1) -> dict[str, str]:
        dag = await self.store.get(task_id, attempt)
        return {n.key: n.state.value for n in dag.nodes}


async def until(predicate, limit: float = 60.0, message: str = "condition") -> None:
    """Wait (real time, generously) until ``predicate()`` holds, else fail."""
    try:
        async with asyncio.timeout(limit):
            while not predicate():  # noqa: ASYNC110
                await asyncio.sleep(0.01)
    except TimeoutError:
        raise AssertionError(f"timed out waiting for {message}") from None


async def quiet(seconds: float = 0.25) -> None:
    """Give a run that should NOT do something a moment to (wrongly) do it."""
    await asyncio.sleep(seconds)


class SpyBudget(BudgetTracker):
    """A real tracker that remembers which lease-only calls were made."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.started: list[uuid.UUID] = []
        self.stopped: list[tuple[uuid.UUID, int]] = []

    async def start_runtime(self, task_id):
        self.started.append(task_id)
        return await super().start_runtime(task_id)

    async def stop_runtime(self, task_id, generation):
        self.stopped.append((task_id, generation))
        return await super().stop_runtime(task_id, generation)
