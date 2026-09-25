"""The orchestrator runs a task's DAG end to end (real PostgreSQL, faked agents).

Skipped unless ``PAW_TEST_DATABASE_URL`` is set. Agents are scripted fakes
(``FakeRuntime``); everything else is real: ``TaskService``, ``TaskQueue`` (leases),
``BudgetTracker``, ``LoopDetector``, the DAG store. Time is a ``ManualClock``.
"""

import asyncio
import unittest

from paw_backend.orchestrator.domain import DagState, RunOutcome
from paw_backend.tasks import TaskState
from paw_backend.tasks.queueing import BudgetKind

from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    diamond,
    make_plan,
    node,
    ok,
    quiet,
    requires_postgres,
    until,
)

Out = RunOutcome


async def usage(harness, task_id, kind: BudgetKind) -> int:
    return next(
        u.consumed for u in await harness.budget.usage(task_id) if u.kind is kind
    )


@requires_postgres
class HappyPathTest(PostgresOrchestratorTestCase):
    async def test_a_dag_runs_to_the_end_and_the_task_goes_to_evaluation(self):
        timeline = []
        runtime = FakeRuntime("local", timeline=timeline)
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(h, diamond())

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(report.task_id, task_id)
        self.assertEqual(report.dag_state, DagState.SUCCEEDED)
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.EVALUATING)
        dag = await self.store.get(task_id, 1)
        self.assertEqual({n.state.value for n in dag.nodes}, {"succeeded"})
        self.assertEqual(dag.epoch, 1)
        self.assertEqual(dag.node("a").result.summary, "a by local")
        self.assertEqual(dag.state, DagState.SUCCEEDED)
        # One step of the budget per node start; the runtime timer ran and stopped.
        self.assertEqual(await usage(h, task_id, BudgetKind.STEPS), 5)
        self.assertEqual(await usage(h, task_id, BudgetKind.RETRIES), 0)
        row = (
            await self.rows(
                "SELECT running_since FROM budget_usages WHERE kind = 'runtime_seconds'"
            )
        )[0]
        self.assertIsNone(row["running_since"])
        # The queue entry was completed.
        (entry,) = await self.rows("SELECT status, claim_count FROM queue_entries")
        self.assertEqual((entry["status"], entry["claim_count"]), ("completed", 1))
        # A second worker finds nothing to do.
        self.assertEqual((await h.orchestrator.run_once("w2")).outcome, Out.IDLE)

    async def test_independent_nodes_start_together_and_dependents_wait(self):
        timeline = []
        runtime = FakeRuntime("local", timeline=timeline)
        for key in ("a", "e", "b", "c"):
            runtime.gate(key)
        h = self.harness(runtimes={"local": runtime})
        await self.prepare(h, diamond())

        def started():
            return [k for kind, k, _ in timeline if kind == "start"]

        def ended():
            return [k for kind, k, _ in timeline if kind == "end"]

        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(started()) == 2, message="the two roots")
        await quiet()
        # The two roots (a: the researcher, e) are both running; nothing else.
        self.assertEqual(sorted(started()), ["a", "e"])

        runtime.gates["e"].set()
        await until(lambda: ended() == ["e"], message="e to end")
        await quiet()
        self.assertEqual(sorted(started()), ["a", "e"])  # b and c wait for a
        runtime.gates["a"].set()
        await until(lambda: len(started()) == 4, message="b and c")
        # b and c are independent of each other: both start before either ends.
        self.assertEqual(started()[2:], ["b", "c"])
        self.assertEqual(ended(), ["e", "a"])
        runtime.gates["c"].set()
        runtime.gates["b"].set()
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        # d (the join) started after both b and c had ended.
        positions = {event: i for i, event in enumerate(timeline)}
        self.assertLess(positions[("end", "b", 1)], positions[("start", "d", 1)])
        self.assertLess(positions[("end", "c", 1)], positions[("start", "d", 1)])

    async def test_the_parallelism_cap_is_respected(self):
        runtime = FakeRuntime("local")
        keys = [f"n{i}" for i in range(6)]
        for key in keys:
            runtime.gate(key)
        h = self.harness(runtimes={"local": runtime}, config={"max_parallel_nodes": 2})
        await self.prepare(h, make_plan(*(node(key) for key in keys)))

        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 2, message="two nodes")
        await quiet()
        self.assertEqual([a.node_key for a in runtime.assignments], ["n0", "n1"])
        runtime.gates["n0"].set()
        await until(lambda: len(runtime.assignments) == 3, message="a third node")
        self.assertEqual([a.node_key for a in runtime.assignments], ["n0", "n1", "n2"])
        for key in keys:
            runtime.gates[key].set()
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        # Never more than two at once.
        live = peak = 0
        for kind, _key, _attempt in runtime.timeline:
            live += 1 if kind == "start" else -1
            peak = max(peak, live)
        self.assertEqual(peak, 2)

    async def test_results_pass_between_nodes_and_only_from_direct_dependencies(self):
        seen = {}

        async def record(assignment):
            seen[assignment.node_key] = {
                k: v.summary for k, v in assignment.upstream.items()
            }
            return ok(
                f"result of {assignment.node_key}",
                changed_files=[f"{assignment.node_key}.py"],
                discovered_facts=["a fact"],
            )

        script = {key: record for key in "abcd"}
        runtime = FakeRuntime("local", script=script)
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(h, diamond())

        await h.orchestrator.run_once("w1")

        self.assertEqual(seen["a"], {})
        self.assertEqual(seen["b"], {"a": "result of a"})
        self.assertEqual(seen["c"], {"a": "result of a"})
        # d depends on b and c and gets their results, not a's.
        self.assertEqual(seen["d"], {"b": "result of b", "c": "result of c"})
        stored = (await self.store.get(task_id, 1)).node("b").result
        self.assertEqual(stored.changed_files, ("b.py",))
        self.assertEqual(stored.discovered_facts, ("a fact",))

    async def test_the_assignment_carries_the_node_and_its_own_agent(self):
        runtime = FakeRuntime("local")
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(
            h,
            make_plan(node("only", title="Only", goal="Do only", input={"k": [1]})),
        )

        await h.orchestrator.run_once("w1")

        (assignment,) = runtime.assignments
        self.assertEqual(assignment.task_id, task_id)
        self.assertEqual(
            (assignment.node_key, assignment.title, assignment.goal, assignment.agent),
            ("only", "Only", "Do only", "local"),
        )
        self.assertEqual(dict(assignment.input), {"k": [1]})
        self.assertEqual((assignment.attempt, assignment.approach), (1, 0))
        # A copy: the runtime cannot change what the DAG stores.
        assignment.input["k"].append(2)
        stored = (await self.store.get(task_id, 1)).node("only")
        self.assertEqual(stored.input, {"k": [1]})


if __name__ == "__main__":
    unittest.main()
