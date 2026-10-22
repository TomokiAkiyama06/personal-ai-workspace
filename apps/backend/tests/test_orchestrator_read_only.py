"""The orchestrator on a read-only task (issue #279, Decision 0094, 1 A).

(a) a plan with a ``worker`` node is refused like any other invalid plan (the
next planner is asked); (b) a task whose Working Set is ``referenced`` only
starts and runs; (c) a DAG that succeeded completes the task at once, without
an evaluation or a pull request, and the task keeps the summary of each node;
no node is ever given a capability or a tool that writes.
"""

import unittest
import uuid

from paw_backend.agents.local import node_prompts
from paw_backend.agents.toolkit import NodeToolkit
from paw_backend.authz import Capability
from paw_backend.orchestrator.domain import WRITING_CAPABILITIES, RunOutcome
from paw_backend.orchestrator.errors import InvalidPlanError, PlanReason
from paw_backend.orchestrator.plan import refuse_writers
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.runtime import NodeOutcome
from paw_backend.tasks import RepoRole, TaskState, WorkingSetEntry

from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    make_plan,
    node,
    requires_postgres,
)
from .task_support import BASELINE

Out = RunOutcome


def planned(*nodes: dict) -> NodeOutcome:
    return NodeOutcome.succeeded(NodeResult("a plan"), plan={"nodes": list(nodes)})


READERS = planned(
    node("read", role="researcher"), node("check", "read", role="reviewer")
)
WITH_WORKER = planned(node("read", role="researcher"), node("impl", "read"))


class RefuseWritersTest(unittest.TestCase):
    def test_a_worker_node_is_refused(self):
        with self.assertRaises(InvalidPlanError) as raised:
            refuse_writers(make_plan(node("a", role="researcher"), node("b", "a")))
        self.assertIs(raised.exception.reason, PlanReason.WORKER_IN_READ_ONLY_TASK)

    def test_readers_only_are_accepted(self):
        plan = make_plan(node("a", role="researcher"), node("b", "a", role="reviewer"))
        self.assertIs(refuse_writers(plan), plan)


@requires_postgres
class ReadOnlyRunTest(PostgresOrchestratorTestCase):
    async def read_only_task(self, harness) -> uuid.UUID:
        task_id = await self.create_task(
            repositories=[
                WorkingSetEntry(self.repository_id, RepoRole.REFERENCED, BASELINE)
            ],
            read_only=True,
        )
        await harness.orchestrator.enqueue_task(task_id, preset="standard")
        return task_id

    async def test_a_succeeded_dag_completes_the_task_with_its_summaries(self):
        runtime = FakeRuntime(
            "local",
            script={
                "plan": READERS,
                "read": NodeOutcome.succeeded(NodeResult("The parser is LL(1).")),
                "check": NodeOutcome.succeeded(NodeResult("Nothing contradicts it.")),
            },
        )
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.read_only_task(h)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        snapshot = await h.tasks.restore(task_id, log_limit=0)
        self.assertEqual(snapshot.state, TaskState.COMPLETED)
        history = await h.tasks.history(task_id)
        self.assertEqual(
            [event.command.value for event in history],
            ["create", "start", "complete"],
        )
        self.assertEqual(
            history[-1].detail,
            {
                "result": {
                    "nodes": [
                        {
                            "key": "read",
                            "role": "researcher",
                            "title": "Node read",
                            "summary": "The parser is LL(1).",
                        },
                        {
                            "key": "check",
                            "role": "reviewer",
                            "title": "Node check",
                            "summary": "Nothing contradicts it.",
                        },
                    ]
                }
            },
        )
        (entry,) = await self.rows(
            "SELECT status FROM queue_entries WHERE task_id = :t", t=task_id
        )
        self.assertEqual(entry["status"], "completed")

    async def test_a_plan_with_a_worker_is_refused_and_the_next_planner_asked(self):
        first = FakeRuntime("local", script={"plan": WITH_WORKER})
        second = FakeRuntime("cloud", script={"plan": READERS})
        h = self.harness(
            runtimes={"local": first, "cloud": second}, ladder=("local", "cloud")
        )
        task_id = await self.read_only_task(h)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(first.calls_of("plan")), 1)
        self.assertEqual(len(second.calls_of("plan")), 1)
        dag = await self.store.get(task_id, 1)
        self.assertEqual([n.key for n in dag.nodes], ["read", "check"])
        self.assertEqual(len(await h.loops.history(task_id)), 1)
        self.assertEqual(
            (await h.tasks.restore(task_id, log_limit=0)).state, TaskState.COMPLETED
        )

    async def test_only_plans_with_workers_fail_the_task(self):
        runtime = FakeRuntime("local", script={"plan": WITH_WORKER})
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.read_only_task(h)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.PLAN_FAILED)
        self.assertIsNone(await self.store.get(task_id, 1))
        self.assertEqual(
            (await h.tasks.restore(task_id, log_limit=0)).state, TaskState.FAILED
        )

    async def test_a_submitted_plan_with_a_worker_is_refused(self):
        h = self.harness()
        task_id = await self.read_only_task(h)
        with self.assertRaises(InvalidPlanError) as raised:
            await h.orchestrator.submit_plan(task_id, make_plan(node("impl")))
        self.assertIs(raised.exception.reason, PlanReason.WORKER_IN_READ_ONLY_TASK)
        self.assertIsNone(await self.store.get(task_id, 1))

    async def test_no_node_gets_a_writing_capability_or_tool(self):
        seen = []

        async def look(assignment):
            context = await assignment.tools._context_factory()
            seen.append((assignment.node_key, context.grant.capabilities))
            toolkit = NodeToolkit(assignment)
            seen.append(
                (assignment.node_key, {t["function"]["name"] for t in toolkit.tools})
            )
            if assignment.node_key == "plan":
                system, _ = node_prompts(toolkit)
                seen.append(("prompt", system))
                return READERS
            return NodeOutcome.succeeded(NodeResult(f"{assignment.node_key} read"))

        runtime = FakeRuntime(
            "local", script={"plan": look, "read": look, "check": look}
        )
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.read_only_task(h)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        grants = [caps for _, caps in seen if isinstance(caps, frozenset)]
        tools = [names for _, names in seen if isinstance(names, set)]
        self.assertEqual(len(grants), 3)
        for capabilities in grants:
            self.assertFalse(capabilities & WRITING_CAPABILITIES)
            self.assertNotIn(Capability.PROJECT_TASK_RUN, capabilities)
        for names in tools:
            self.assertFalse(names & {"str_replace", "write_file"})
        # The planner is told that a worker node is refused.
        (prompt,) = [text for key, text in seen if key == "prompt"]
        self.assertIn("a plan with a worker node is refused", prompt)
        self.assertIn("Roles: researcher", prompt)
        self.assertTrue(
            all(a.read_only_task for a in runtime.assignments), runtime.assignments
        )
        self.assertEqual(
            (await h.tasks.restore(task_id, log_limit=0)).state, TaskState.COMPLETED
        )

    async def test_a_task_that_is_not_read_only_still_goes_to_evaluation(self):
        runtime = FakeRuntime("local", script={"plan": READERS})
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(h)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(
            (await h.tasks.restore(task_id, log_limit=0)).state, TaskState.EVALUATING
        )
        self.assertFalse(any(a.read_only_task for a in runtime.assignments))


if __name__ == "__main__":
    unittest.main()
