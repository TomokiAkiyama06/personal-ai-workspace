"""Decomposing a task into a DAG: the planner proposes, the orchestrator decides."""

import unittest
import uuid

from paw_backend.orchestrator.domain import NodeRole, RunOutcome
from paw_backend.orchestrator.errors import (
    DagAlreadyExistsError,
    DagStateError,
    InvalidPlanError,
    PlanReason,
)
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.runtime import NodeOutcome
from paw_backend.tasks import TaskCommand, TaskNotFoundError, TaskState
from paw_backend.tasks.queueing import BudgetKind

from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    diamond,
    fail,
    make_plan,
    node,
    requires_postgres,
)

Out = RunOutcome


def planned(*nodes: dict) -> NodeOutcome:
    return NodeOutcome.succeeded(NodeResult("a plan"), plan={"nodes": list(nodes)})


CYCLE = planned(node("a", "b"), node("b", "a"))
GOOD = planned(node("a", role="researcher"), node("b", "a"))


@requires_postgres
class PlannerTest(PostgresOrchestratorTestCase):
    async def test_the_planner_decomposes_the_task_and_the_dag_runs(self):
        planner = FakeRuntime("local", script={"plan": GOOD})
        h = self.harness(runtimes={"local": planner})
        task_id = await self.create_task(title="Fix the parser", input={"issue": 7})
        await h.orchestrator.enqueue_task(task_id, preset="standard")

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        (plan_call,) = planner.calls_of("plan")
        self.assertEqual(plan_call.role, NodeRole.PLANNER)
        self.assertEqual(
            (plan_call.title, plan_call.goal), ("Fix the parser", "Fix the parser")
        )
        self.assertEqual(dict(plan_call.input), {"issue": 7})
        self.assertEqual(dict(plan_call.upstream), {})
        dag = await self.store.get(task_id, 1)
        self.assertEqual([n.key for n in dag.nodes], ["a", "b"])
        # The planning call cost a step, like a node.
        usage = {u.kind: u.consumed for u in await h.budget.usage(task_id)}
        self.assertEqual(usage[BudgetKind.STEPS], 3)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.EVALUATING)

    async def test_an_invalid_plan_is_refused_and_the_planner_is_asked_again(self):
        planner = FakeRuntime("local", script={"plan": [CYCLE, GOOD]})
        h = self.harness(runtimes={"local": planner})
        task_id = await self.prepare(h)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(planner.calls_of("plan")), 2)
        self.assertEqual([a.attempt for a in planner.calls_of("plan")], [1, 2])
        dag = await self.store.get(task_id, 1)
        self.assertEqual([n.key for n in dag.nodes], ["a", "b"])
        (record,) = await h.loops.history(task_id)  # the refused plan was a failure
        self.assertEqual(record.approach, 0)

    async def test_a_planner_that_never_gives_an_acceptable_plan_fails_the_task(self):
        for label, script in (
            ("cycles", CYCLE),
            ("no plan", NodeOutcome.succeeded(NodeResult("nothing"))),
            ("an empty plan", planned()),
            ("an unknown role", planned(node("a", role="boss"))),
            ("failures", fail("Down", "the model is down")),
            ("an exception", RuntimeError("boom")),
        ):
            with self.subTest(label):
                planner = FakeRuntime("local", script={"plan": script})
                h = self.harness(runtimes={"local": planner})
                task_id = await self.prepare(h)

                report = await h.orchestrator.run_once("w1")

                self.assertEqual(report.outcome, Out.PLAN_FAILED)
                self.assertEqual(len(planner.calls_of("plan")), 2)  # max_plan_attempts
                self.assertIsNone(await self.store.get(task_id, 1))
                snapshot = await h.tasks.restore(task_id)
                self.assertEqual(snapshot.state, TaskState.FAILED)
                self.assertEqual(snapshot.last_event.reason, "No acceptable plan")
                (entry,) = await self.rows(
                    "SELECT status FROM queue_entries WHERE task_id = :t", t=task_id
                )
                self.assertEqual(entry["status"], "completed")

    async def test_a_planner_that_says_it_cannot_is_not_asked_twice(self):
        planner = FakeRuntime(
            "local", script={"plan": fail("Refused", "no", retryable=False)}
        )
        h = self.harness(runtimes={"local": planner})
        task_id = await self.prepare(h)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.PLAN_FAILED)
        self.assertEqual(len(planner.calls_of("plan")), 1)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.FAILED)

    async def test_the_second_planning_attempt_is_made_by_the_next_agent(self):
        weak = FakeRuntime("local", script={"plan": CYCLE})
        strong = FakeRuntime("codex", script={"plan": GOOD})
        h = self.harness(
            runtimes={"local": weak, "codex": strong}, ladder=("local", "codex")
        )
        await self.prepare(h)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(
            (len(weak.calls_of("plan")), len(strong.calls_of("plan"))), (1, 1)
        )

    async def test_a_plan_submitted_beforehand_skips_the_planner(self):
        planner = FakeRuntime("local", script={"plan": CYCLE})
        h = self.harness(runtimes={"local": planner})
        await self.prepare(h, diamond())

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(planner.calls_of("plan"), [])


@requires_postgres
class SubmitPlanTest(PostgresOrchestratorTestCase):
    async def test_a_plan_is_judged_before_anything_is_stored(self):
        h = self.harness()
        task_id = await self.create_task()
        bad = {"nodes": [node("a", "b"), node("b", "a")]}

        with self.assertRaises(InvalidPlanError) as caught:
            await h.orchestrator.submit_plan(task_id, bad)

        self.assertEqual(caught.exception.reason, PlanReason.CYCLE)
        self.assertIsNone(await self.store.get(task_id, 1))
        self.assertEqual(await self.scalar("SELECT count(*) FROM agent_dags"), 0)

    async def test_a_plan_is_accepted_once_per_attempt_and_again_after_a_restart(self):
        h = self.harness()
        task_id = await self.create_task()
        first = await h.orchestrator.submit_plan(task_id, diamond())
        self.assertEqual(first.attempt, 1)

        with self.assertRaises(DagAlreadyExistsError):
            await h.orchestrator.submit_plan(task_id, diamond())

        # Fail and Restart the task: attempt 2 has no DAG yet and takes a new plan.
        await h.tasks.execute(task_id, TaskCommand.START, actor=self.system)
        await h.tasks.execute(task_id, TaskCommand.FAIL, actor=self.system)
        await h.tasks.execute(task_id, TaskCommand.RESTART, actor=self.user)
        second = await h.orchestrator.submit_plan(task_id, make_plan(node("only")))
        self.assertEqual(second.attempt, 2)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len((await self.store.get(task_id, 1)).nodes), 5)  # history

    async def test_no_plan_is_accepted_for_a_finished_or_unknown_task(self):
        h = self.harness()
        task_id = await self.create_task()
        await h.tasks.execute(task_id, TaskCommand.CANCEL, actor=self.user)

        with self.assertRaises(DagStateError):
            await h.orchestrator.submit_plan(task_id, diamond())
        with self.assertRaises(TaskNotFoundError):
            await h.orchestrator.submit_plan(uuid.uuid4(), diamond())
        self.assertEqual(await self.scalar("SELECT count(*) FROM agent_dags"), 0)


if __name__ == "__main__":
    unittest.main()
