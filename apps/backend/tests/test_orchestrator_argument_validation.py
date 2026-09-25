"""Every argument of every public method is validated before the database is used.

A wrong type or value raises the module's typed error (never ``AttributeError`` /
``TypeError`` from a value the code tried to use, never a SQLAlchemy error) and
the message never holds the value. The database of these tests is NOT configured:
a call that reached it would raise ``DatabaseNotConfiguredError`` instead of the
typed error, so a passing case proves that nothing was sent.
"""

import inspect
import unittest
import uuid

from paw_backend.db import Database
from paw_backend.orchestrator import (
    DagStore,
    NextStep,
    NodeBudgetHandle,
    NodeOutcome,
    NodeResult,
    NodeRole,
    Orchestrator,
    OrchestratorConfig,
    Plan,
    RunGuard,
    validate_runtime,
)
from paw_backend.orchestrator.errors import (
    InvalidOrchestratorArgumentError,
    InvalidPlanError,
)
from paw_backend.orchestrator.plan import PlanNode
from paw_backend.tasks import (
    TaskRun,
    TaskService,
)
from paw_backend.tasks.queueing import (
    BudgetPreset,
    BudgetTracker,
    LoopDetector,
    Priority,
    QueueEntry,
    QueueStatus,
    TaskQueue,
)
from paw_backend.tools import PostgresTaskActivity

from .orchestrator_support import (
    FakeAuthority,
    FakeRuntime,
    FakeTools,
    ManualClock,
    make_plan,
    node,
)
from .support import make_settings

CANARY = "CANARY-9d41e0"
GOOD_ID = uuid.uuid4()
DAG = uuid.uuid4()
RESULT = NodeResult("done")
SIGNATURE = "a" * 64


class TextSubclass(str):
    pass


def not_a_uuid(*, allow_none=False):
    real = uuid.uuid4()
    values = [CANARY, "", str(real), real.hex, real.int, 0, True, 1.5, object(), [real]]
    return values if allow_none else [*values, None]


def not_an_int(low=None, high=None, *, allow_none=False):
    values = [CANARY, "1", 1.0, True, False, b"1", object(), [1]]
    if low is not None:
        values.append(low - 1)
    if high is not None:
        values.append(high + 1)
    return values if allow_none else [*values, None]


def not_text(*, blank=True):
    values = [None, 5, b"x", TextSubclass("k"), object(), ["k"], "a\x00b", "a\ud800b"]
    return [*values, "", "   "] if blank else values


def database() -> Database:
    return Database(make_settings())  # no URL: touching it raises


def entry(**overrides) -> QueueEntry:
    data = {
        "id": 1,
        "task_id": GOOD_ID,
        "priority": Priority.NORMAL,
        "status": QueueStatus.CLAIMED,
        "enqueued_at": None,
        "claimed_by": "w1",
        "claimed_at": None,
        "lease_expires_at": None,
        "claim_count": 1,
        "finished_at": None,
    }
    data.update(overrides)
    return QueueEntry(**data)


class StoreArgumentTest(unittest.IsolatedAsyncioTestCase):
    def store(self) -> DagStore:
        return DagStore(database())

    def cases(self):
        plan = make_plan(node("a"))
        run = TaskRun(1, 0)
        big = 2**31
        epoch = lambda: not_an_int(1, big - 1)  # noqa: E731
        valid_fail = {
            "error_class": "E",
            "signature": SIGNATURE,
            "step": NextStep.RETRY,
            "agent_index": None,
            "approach": None,
        }
        # method -> (valid keyword arguments, {argument: wrong values})
        return {
            "create": (
                {"task_id": GOOD_ID, "attempt": 1, "plan": plan},
                {
                    "task_id": not_a_uuid(),
                    "attempt": not_an_int(1, big - 1) + [0],
                    "plan": [None, {"nodes": []}, CANARY, plan.to_mapping(), object()],
                },
            ),
            "get": (
                {"task_id": GOOD_ID, "attempt": 1},
                {"task_id": not_a_uuid(), "attempt": not_an_int(1, big - 1) + [0]},
            ),
            "get_by_id": ({"dag_id": DAG}, {"dag_id": not_a_uuid()}),
            "attempts": (
                {"dag_id": DAG, "node_key": "a"},
                {
                    "dag_id": not_a_uuid(),
                    "node_key": [v for v in not_text() if v is not None]
                    + [CANARY * 20],
                },
            ),
            "acquire": (
                {"dag_id": DAG, "owner": "w1", "run": run},
                {
                    "dag_id": not_a_uuid(),
                    "owner": [None, "", " w", "w w", "w" * 101, 5, b"w", CANARY + "\n"],
                    "run": [None, (1, 0), 1, CANARY, object()],
                },
            ),
            "start_node": (
                {"dag_id": DAG, "epoch": 1, "key": "a", "max_attempts": 6},
                {
                    "dag_id": not_a_uuid(),
                    "epoch": epoch() + [0],
                    "key": not_text(),
                    "max_attempts": not_an_int(1, 20) + [0],
                },
            ),
            "complete_node": (
                {
                    "dag_id": DAG,
                    "epoch": 1,
                    "key": "a",
                    "attempt_number": 1,
                    "result": RESULT,
                },
                {
                    "dag_id": not_a_uuid(),
                    "epoch": epoch() + [0],
                    "key": not_text(),
                    "attempt_number": not_an_int(1, big - 1) + [0],
                    "result": [None, {"summary": "x"}, CANARY, object()],
                },
            ),
            "fail_node": (
                {
                    "dag_id": DAG,
                    "epoch": 1,
                    "key": "a",
                    "attempt_number": 1,
                    **valid_fail,
                },
                {
                    "dag_id": not_a_uuid(),
                    "epoch": epoch() + [0],
                    "key": not_text(),
                    "attempt_number": not_an_int(1, big - 1) + [0],
                    "error_class": not_text(),
                    "signature": [None, "", "A" * 64, "a" * 63, "g" * 64, 5, CANARY],
                    "step": [None, "sideways", NextStep, 5, CANARY],
                    "agent_index": [0, 1],  # only with alternative / escalate
                    "approach": [0, 1],
                },
            ),
            "give_up_node": (
                {"dag_id": DAG, "epoch": 1, "key": "a", "error_class": "E"},
                {
                    "dag_id": not_a_uuid(),
                    "epoch": epoch() + [0],
                    "key": not_text(),
                    "error_class": not_text(),
                },
            ),
            "interrupt": (
                {"dag_id": DAG, "epoch": 1},
                {"dag_id": not_a_uuid(), "epoch": epoch() + [0]},
            ),
            "cancel": (
                {"dag_id": DAG, "epoch": 1},
                {"dag_id": not_a_uuid(), "epoch": epoch() + [0]},
            ),
            "finalize": (
                {"dag_id": DAG, "epoch": 1},
                {"dag_id": not_a_uuid(), "epoch": epoch() + [0]},
            ),
        }

    async def test_every_public_method_and_argument_is_in_the_table(self):
        public = {
            name
            for name, member in inspect.getmembers(
                DagStore, inspect.iscoroutinefunction
            )
            if not name.startswith("_")
        }
        cases = self.cases()
        self.assertEqual(public, set(cases))
        for name, (valid, wrong) in cases.items():
            signature = inspect.signature(getattr(DagStore, name))
            arguments = set(signature.parameters) - {"self"}
            self.assertEqual(set(valid), arguments, name)
            self.assertLessEqual(set(wrong), arguments, name)

    async def test_every_wrong_value_is_refused_before_the_database(self):
        checked = 0
        for name, (valid, wrong) in self.cases().items():
            for argument, values in wrong.items():
                for value in values:
                    with self.subTest(
                        method=name, argument=argument, value=repr(value)[:40]
                    ):
                        arguments = dict(valid)
                        arguments[argument] = value
                        if argument in ("agent_index", "approach"):
                            arguments["step"] = NextStep.RETRY  # not allowed with retry
                        with self.assertRaises(
                            InvalidOrchestratorArgumentError
                        ) as caught:
                            await getattr(self.store(), name)(**arguments)
                        self.assertNotIn(CANARY, str(caught.exception))
                        self.assertEqual(caught.exception.parameter, argument)
                        checked += 1
        self.assertGreater(checked, 300)

    async def test_the_step_decides_which_extra_arguments_are_needed(self):
        valid = {
            "dag_id": DAG,
            "epoch": 1,
            "key": "a",
            "attempt_number": 1,
            "error_class": "E",
            "signature": SIGNATURE,
        }
        for step in (NextStep.ALTERNATIVE, NextStep.ESCALATE):
            for extra in (
                {},
                {"agent_index": 0},
                {"approach": 1},
                {"agent_index": 4, "approach": 1},
                {"agent_index": 0, "approach": 101},
                {"agent_index": True, "approach": 1},
                {"agent_index": 0, "approach": "1"},
            ):
                with (
                    self.subTest(step=step.value, extra=extra),
                    self.assertRaises(InvalidOrchestratorArgumentError),
                ):
                    await self.store().fail_node(**valid, step=step, **extra)
        for step in (NextStep.RETRY, NextStep.HOLD, NextStep.GIVE_UP):
            with (
                self.subTest(step=step.value),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                await self.store().fail_node(
                    **valid, step=step, agent_index=1, approach=1
                )

    async def test_the_step_may_be_given_as_its_serialised_value(self):
        # Accepted and normalised: it gets as far as the (unconfigured) database.
        from paw_backend.db import DatabaseNotConfiguredError

        with self.assertRaises(DatabaseNotConfiguredError):
            await self.store().fail_node(
                DAG, 1, "a", 1, error_class="E", signature=SIGNATURE, step="retry"
            )

    def test_the_constructor_wants_a_database(self):
        for value in (None, object(), "postgresql://x", database):
            with self.subTest(value=repr(value)[:30]), self.assertRaises(TypeError):
                DagStore(value)


class OrchestratorArgumentTest(unittest.IsolatedAsyncioTestCase):
    def build(self, **overrides) -> Orchestrator:
        db = database()
        arguments = {
            "tasks": TaskService(db),
            "queue": TaskQueue(db),
            "budget": BudgetTracker(db),
            "loops": LoopDetector(db),
            "store": DagStore(db),
            "activity": PostgresTaskActivity(db),
            "tools": FakeTools(),
            "authority": FakeAuthority(),
            "runtimes": {"local": FakeRuntime()},
            "config": OrchestratorConfig.uniform(["local"]),
            "clock": ManualClock(),
        }
        arguments.update(overrides)
        return Orchestrator(**arguments)

    def test_the_constructor_checks_every_collaborator_up_front(self):
        class NoAsync:
            def check(
                self, task_id, run
            ):  # a plain function where a coroutine is needed
                return None

        class WrongArity:
            async def check(self, task_id):
                return None

        class SyncRuntime:
            def run_node(self, assignment):
                return None

        class NoRuntime:
            pass

        class WrongRuntimeArity:
            async def run_node(self):
                return None

        class SyncClock:
            def monotonic(self):
                return 0.0

            def sleep(self, seconds):
                return None

        cases = [
            ("tasks", {"tasks": object()}, TypeError),
            ("tasks None", {"tasks": None}, TypeError),
            ("queue", {"queue": object()}, TypeError),
            ("budget", {"budget": object()}, TypeError),
            ("loops", {"loops": object()}, TypeError),
            ("store", {"store": object()}, TypeError),
            ("config", {"config": {"local": 1}}, TypeError),
            ("activity missing", {"activity": object()}, TypeError),
            ("activity not a coroutine", {"activity": NoAsync()}, TypeError),
            ("activity arity", {"activity": WrongArity()}, TypeError),
            ("tools missing", {"tools": object()}, TypeError),
            ("authority missing", {"authority": object()}, TypeError),
            ("runtimes not a mapping", {"runtimes": [FakeRuntime()]}, TypeError),
            ("runtime label not text", {"runtimes": {5: FakeRuntime()}}, TypeError),
            ("runtime None", {"runtimes": {"local": None}}, TypeError),
            (
                "runtime without run_node",
                {"runtimes": {"local": NoRuntime()}},
                TypeError,
            ),
            (
                "runtime not a coroutine",
                {"runtimes": {"local": SyncRuntime()}},
                TypeError,
            ),
            ("runtime arity", {"runtimes": {"local": WrongRuntimeArity()}}, TypeError),
            (
                "a ladder names an agent nobody runs",
                {"config": OrchestratorConfig.uniform(["local", "codex"])},
                InvalidOrchestratorArgumentError,
            ),
            (
                "the heartbeat is not shorter than the lease",
                {
                    "config": OrchestratorConfig.uniform(
                        ["local"], heartbeat_seconds=60.0
                    )
                },
                InvalidOrchestratorArgumentError,
            ),
            ("clock without monotonic", {"clock": object()}, TypeError),
            ("clock with a plain sleep", {"clock": SyncClock()}, TypeError),
        ]
        for label, overrides, error in cases:
            with self.subTest(label), self.assertRaises(error):
                self.build(**overrides)
        self.build()  # the baseline is valid

    async def test_every_public_method_and_argument_is_in_the_table(self):
        public = {
            name
            for name, member in inspect.getmembers(Orchestrator, inspect.isfunction)
            if not name.startswith("_")
        }
        self.assertEqual(
            public, {"enqueue_task", "submit_plan", "run_once", "serve", "run_entry"}
        )

    async def test_enqueue_task_refuses_wrong_arguments_before_the_database(self):
        for label, arguments in [
            *[("task_id", {"task_id": v}) for v in not_a_uuid()],
            *[
                ("preset", {"preset": v})
                for v in [None, "huge", 5, CANARY, BudgetPreset]
            ],
            *[
                ("priority", {"priority": v})
                for v in [None, "urgent", 5, CANARY, Priority]
            ],
        ]:
            with self.subTest(label, value=repr(arguments)[:50]):
                call = {"task_id": GOOD_ID, "preset": BudgetPreset.STANDARD}
                call.update(arguments)
                with self.assertRaises(InvalidOrchestratorArgumentError) as caught:
                    await self.build().enqueue_task(**call)
                self.assertNotIn(CANARY, str(caught.exception))

    async def test_enqueue_task_accepts_the_serialised_enum_values(self):
        from paw_backend.db import DatabaseNotConfiguredError

        with self.assertRaises(DatabaseNotConfiguredError):
            await self.build().enqueue_task(GOOD_ID, preset="long", priority="high")

    async def test_submit_plan_refuses_a_bad_task_id_or_plan_before_the_database(self):
        for value in not_a_uuid():
            with (
                self.subTest(task_id=repr(value)[:30]),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                await self.build().submit_plan(value, make_plan(node("a")))
        for value in [
            None,
            [],
            "plan",
            5,
            {},
            {"nodes": []},
            {"nodes": [{"key": "a"}]},
            {"nodes": [node("a", "a")]},
            object(),
        ]:
            with (
                self.subTest(plan=repr(value)[:30]),
                self.assertRaises(InvalidPlanError),
            ):
                await self.build().submit_plan(GOOD_ID, value)

    async def test_run_once_refuses_a_bad_worker_id(self):
        for value in [
            None,
            "",
            " w",
            "w w",
            "-w",
            "w" * 101,
            5,
            b"w",
            TextSubclass("w\n"),
            "w\x00",
        ]:
            with (
                self.subTest(worker=repr(value)[:30]),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                await self.build().run_once(value)

    async def test_run_entry_refuses_a_bad_entry_or_worker(self):
        cases = [
            ("no entry", None, "w1"),
            ("a dict", {"id": 1}, "w1"),
            ("a task id", GOOD_ID, "w1"),
            ("a bad worker", entry(), None),
            ("a bad worker id", entry(), "w w"),
            ("another worker's entry", entry(claimed_by="w2"), "w1"),
            ("an unclaimed entry", entry(claimed_by=None), "w1"),
        ]
        for label, value, worker in cases:
            with (
                self.subTest(label),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                await self.build().run_entry(value, worker)

    async def test_serve_refuses_bad_arguments(self):
        import asyncio

        for label, worker, stop, idle in [
            ("worker", "w w", asyncio.Event(), 1.0),
            ("stop None", "w1", None, 1.0),
            ("stop a flag", "w1", True, 1.0),
            ("idle zero", "w1", asyncio.Event(), 0),
            ("idle negative", "w1", asyncio.Event(), -1),
            ("idle a bool", "w1", asyncio.Event(), True),
            ("idle text", "w1", asyncio.Event(), "1"),
            ("idle huge", "w1", asyncio.Event(), 3601),
        ]:
            with (
                self.subTest(label),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                await self.build().serve(worker, stop, idle_seconds=idle)


class ValueObjectArgumentTest(unittest.IsolatedAsyncioTestCase):
    def test_the_config_refuses_bad_values(self):
        cases = [
            ("no ladders", {"ladders": None}),
            ("ladders as a list", {"ladders": ["local"]}),
            ("a role missing", {"ladders": {NodeRole.WORKER: ("local",)}}),
            ("an empty ladder", {"ladders": {r: () for r in NodeRole}}),
            ("a ladder that is text", {"ladders": {r: "local" for r in NodeRole}}),
            (
                "a repeated agent",
                {"ladders": {r: ("local", "local") for r in NodeRole}},
            ),
            ("a bad label", {"ladders": {r: ("Local",) for r in NodeRole}}),
            (
                "a ladder too long",
                {"ladders": {r: tuple(f"a{i}" for i in range(5)) for r in NodeRole}},
            ),
            ("parallel zero", {"max_parallel_nodes": 0}),
            ("parallel too high", {"max_parallel_nodes": 17}),
            ("parallel a bool", {"max_parallel_nodes": True}),
            ("timeout zero", {"node_timeout_seconds": 0}),
            ("timeout too long", {"node_timeout_seconds": 86_401}),
            ("timeout NaN", {"node_timeout_seconds": float("nan")}),
            ("poll zero", {"poll_seconds": 0}),
            ("poll a bool", {"poll_seconds": True}),
            ("heartbeat zero", {"heartbeat_seconds": 0}),
            ("backoff negative", {"retry_backoff_seconds": -1}),
            ("backoff too long", {"retry_backoff_seconds": 61}),
            ("attempts zero", {"max_attempts_per_rung": 0}),
            ("attempts too many", {"max_attempts_per_rung": 21}),
            ("plan attempts zero", {"max_plan_attempts": 0}),
            ("plan attempts too many", {"max_plan_attempts": 6}),
        ]
        for label, overrides in cases:
            options = {"ladders": {r: ("local",) for r in NodeRole}}
            options.update(overrides)
            with (
                self.subTest(label),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                OrchestratorConfig(**options)

    def test_the_config_backoff_doubles_up_to_a_minute(self):
        config = OrchestratorConfig.uniform(["local"], retry_backoff_seconds=5.0)
        self.assertEqual(
            [config.backoff(n) for n in (0, 1, 2, 3, 4, 5, 6)],
            [0, 5, 10, 20, 40, 60, 60],
        )
        off = OrchestratorConfig.uniform(["local"], retry_backoff_seconds=0)
        self.assertEqual(off.backoff(3), 0.0)

    def test_an_outcome_is_either_a_result_or_a_failure(self):
        NodeOutcome.succeeded(RESULT)
        NodeOutcome.failed("E", "text", retryable=False)
        cases = [
            ("nothing", {}),
            ("both", {"result": RESULT, "error_class": "E"}),
            ("a result that is not one", {"result": {"summary": "x"}}),
            ("a plan without a result", {"error_class": "E", "plan": {"nodes": []}}),
            ("a plan that is text", {"result": RESULT, "plan": "nodes"}),
            ("a blank error class", {"error_class": "  "}),
            ("an error class with a newline", {"error_class": "E\nF"}),
            ("an error class with a surrogate", {"error_class": "E\ud800"}),
            ("a long error class", {"error_class": "E" * 201}),
            ("a message that is not text", {"error_class": "E", "message": 5}),
            ("retryable as a number", {"error_class": "E", "retryable": 1}),
        ]
        for label, options in cases:
            with (
                self.subTest(label),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                NodeOutcome(**options)
        # A message with anything in it is fine: the orchestrator formats it.
        NodeOutcome.failed("E", "bad \ud800 text")

    def test_the_outcome_keeps_the_failure_text_out_of_its_repr(self):
        outcome = NodeOutcome.failed("Boom", "token=hunter2")
        self.assertNotIn("hunter2", repr(outcome))

    async def test_a_node_charge_is_validated_before_the_database(self):
        from paw_backend.tasks.queueing import BudgetKind, InvalidQueueingArgumentError

        guard = RunGuard(GOOD_ID, TaskRun(1, 0), PostgresTaskActivity(database()))
        handle = NodeBudgetHandle(guard, BudgetTracker(database()), GOOD_ID)
        for kind, amount in [
            ("tokens", 5),
            (None, 5),
            (BudgetKind.TOKENS, -1),
            (BudgetKind.TOKENS, 1.5),
            (BudgetKind.TOKENS, True),
            (BudgetKind.RUNTIME_SECONDS, 5),
        ]:
            with (
                self.subTest(kind=kind, amount=amount),
                self.assertRaises(InvalidQueueingArgumentError),
            ):
                await handle.charge(kind, amount)

    def test_a_runtime_must_have_an_async_run_node(self):
        validate_runtime(FakeRuntime(), "x")
        for value in (None, object(), lambda a: a):
            with self.subTest(value=repr(value)[:30]), self.assertRaises(TypeError):
                validate_runtime(value, "x")

    def test_plan_nodes_and_plans_refuse_wrong_types(self):
        for value in (None, 5, "a", [], object()):
            with (
                self.subTest(value=repr(value)[:20]),
                self.assertRaises(InvalidPlanError),
            ):
                Plan(value)
            with (
                self.subTest(node=repr(value)[:20]),
                self.assertRaises(InvalidPlanError),
            ):
                PlanNode.from_mapping(value)


if __name__ == "__main__":
    unittest.main()
