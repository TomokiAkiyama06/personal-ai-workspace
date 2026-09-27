"""A runtime that ignores its cancellation cannot hold a run forever (PAW-034).

When an attempt times out, or the run cancels its nodes (a Cancel, a budget stop)
or its planner, the orchestrator cancels the runtime's coroutine and waits for it
at most ``CANCEL_GRACE_SECONDS``. A runtime that swallows ``CancelledError`` is
abandoned: the node still fails (or the DAG is cancelled), the run ends, the
queue entry is completed and the runtime timer stopped, and the abandoned
coroutine's tools and budget are closed (``NodeStopped(ABANDONED)``).
"""

import asyncio
import unittest
from unittest import mock

from paw_backend.orchestrator import orchestrator as orchestrator_module
from paw_backend.orchestrator.domain import DagState, RunOutcome
from paw_backend.orchestrator.errors import NodeStopped, StopReason
from paw_backend.tasks import TaskCommand, TaskState
from paw_backend.tasks.queueing import BudgetKind

from .orchestrator_support import (
    FakeRuntime,
    FakeTools,
    PostgresOrchestratorTestCase,
    make_plan,
    node,
    ok,
    requires_postgres,
    until,
)

Out = RunOutcome
GRACE = 0.2


class Stubborn:
    """An agent runtime behaviour that swallows every cancellation until the test
    releases it, then tries to act for the task."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cancellations = 0
        self.after: dict[str, object] = {}
        self.finished = asyncio.Event()

    async def __call__(self, assignment):
        self.started.set()
        try:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1  # and carries on: a misbehaving runtime
            for name, act in (
                (
                    "tool",
                    lambda: assignment.tools.call("repo.read_file", {"path": "a.py"}),
                ),
                ("budget", lambda: assignment.budget.charge(BudgetKind.TOKENS, 5)),
                ("remaining", lambda: assignment.budget.remaining()),
            ):
                try:
                    await act()
                    self.after[name] = "allowed"
                except NodeStopped as stopped:
                    self.after[name] = stopped.reason
            return ok("late")
        finally:
            self.finished.set()


@requires_postgres
class StubbornRuntimeTest(PostgresOrchestratorTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        patcher = mock.patch.object(orchestrator_module, "CANCEL_GRACE_SECONDS", GRACE)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stubborn = Stubborn()

    async def asyncTearDown(self):
        self.stubborn.release.set()
        await super().asyncTearDown()

    async def let_it_act(self) -> None:
        """Release the abandoned coroutine and wait for what it tried."""
        self.stubborn.release.set()
        await asyncio.wait_for(self.stubborn.finished.wait(), 30)

    async def assert_closed(self, tools: FakeTools) -> None:
        await self.let_it_act()
        self.assertEqual(
            self.stubborn.after,
            {
                "tool": StopReason.ABANDONED,
                "budget": StopReason.ABANDONED,
                "remaining": StopReason.ABANDONED,
            },
        )
        self.assertEqual(tools.calls, [])
        tokens = await self.scalar(
            "SELECT consumed FROM budget_usages WHERE kind = 'tokens'"
        )
        self.assertEqual(tokens, 0)

    async def assert_released(self, task_id) -> None:
        (entry,) = await self.rows(
            "SELECT status FROM queue_entries WHERE task_id = :t", t=task_id
        )
        self.assertNotEqual(entry["status"], "claimed")
        running_since = await self.scalar(
            "SELECT running_since FROM budget_usages WHERE kind = 'runtime_seconds'"
        )
        self.assertIsNone(running_since)

    async def test_a_timed_out_runtime_that_ignores_cancellation_is_abandoned(self):
        tools = FakeTools()
        runtime = FakeRuntime("local", script={"a": [self.stubborn, ok("second")]})
        h = self.harness(
            runtimes={"local": runtime},
            tools=tools,
            config={"node_timeout_seconds": 30.0},
        )
        task_id = await self.prepare(h, make_plan(node("a")))

        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await asyncio.wait_for(self.stubborn.started.wait(), 30)
        await until(lambda: h.clock.waiting_for(30.0) >= 1, message="the timeout")
        with self.assertLogs("paw_backend.orchestrator.orchestrator", "ERROR") as logs:
            await h.clock.advance(31.0)
            report = await asyncio.wait_for(run, 60)

        self.assertIn("abandoned", "\n".join(logs.output))
        self.assertGreaterEqual(self.stubborn.cancellations, 1)
        self.assertFalse(self.stubborn.finished.is_set())  # still running, abandoned
        # The node failed for its timeout and was retried; the run went on.
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        dag = await self.store.get(task_id, 1)
        attempts = await self.store.attempts(dag.id, "a")
        self.assertEqual(attempts[0].error_class, "NodeTimeout")
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.EVALUATING)
        await self.assert_released(task_id)
        await self.assert_closed(tools)

    async def test_a_cancel_does_not_wait_forever_for_a_stubborn_node(self):
        tools = FakeTools()
        runtime = FakeRuntime("local", script={"a": self.stubborn})
        h = self.harness(runtimes={"local": runtime}, tools=tools)
        task_id = await self.prepare(h, make_plan(node("a"), node("b", "a")))

        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await asyncio.wait_for(self.stubborn.started.wait(), 30)
        await h.tasks.execute(task_id, TaskCommand.CANCEL, actor=self.user)
        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll timer")
        with self.assertLogs("paw_backend.orchestrator.orchestrator", "ERROR"):
            await h.clock.advance(2.0)
            report = await asyncio.wait_for(run, 60)

        self.assertEqual(report.outcome, Out.TASK_ENDED)
        self.assertEqual(report.dag_state, DagState.CANCELLED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.CANCELLED)
        await self.assert_released(task_id)
        await self.assert_closed(tools)

    async def test_a_cancel_does_not_wait_forever_for_a_stubborn_planner(self):
        tools = FakeTools()
        runtime = FakeRuntime("local", script={"plan": self.stubborn})
        h = self.harness(runtimes={"local": runtime}, tools=tools)
        task_id = await self.prepare(h)  # no plan: the planner is asked

        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await asyncio.wait_for(self.stubborn.started.wait(), 30)
        await h.tasks.execute(task_id, TaskCommand.CANCEL, actor=self.user)
        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll timer")
        with self.assertLogs("paw_backend.orchestrator.orchestrator", "ERROR"):
            await h.clock.advance(2.0)
            report = await asyncio.wait_for(run, 60)

        self.assertEqual(report.outcome, Out.TASK_ENDED)
        self.assertIsNone(await self.store.get(task_id, 1))
        await self.assert_released(task_id)
        await self.assert_closed(tools)

    async def test_a_runtime_that_honours_its_cancellation_is_not_abandoned(self):
        # The grace is only an upper bound: a well-behaved runtime ends at once and
        # nothing is logged as abandoned.
        async def polite(_assignment):
            await asyncio.Event().wait()

        runtime = FakeRuntime("local", script={"a": [polite, ok()]})
        h = self.harness(
            runtimes={"local": runtime}, config={"node_timeout_seconds": 30.0}
        )
        await self.prepare(h, make_plan(node("a")))
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.calls_of("a")) == 1, message="a to start")
        await until(lambda: h.clock.waiting_for(30.0) >= 1, message="the timeout")
        with self.assertNoLogs("paw_backend.orchestrator.orchestrator", "ERROR"):
            await h.clock.advance(31.0)
            report = await asyncio.wait_for(run, 60)
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.stubborn.release.set()


if __name__ == "__main__":
    unittest.main()
