"""A sub-agent never exceeds its parent's budget (PAW-034), and what a used-up
budget does (Decision 0007: retries fail the task, the rest wait for a human)."""

import asyncio
import unittest

from paw_backend.orchestrator.domain import RunOutcome
from paw_backend.orchestrator.errors import NodeStopped, StopReason
from paw_backend.tasks import TaskCommand, TaskState, WaitReason
from paw_backend.tasks.queueing import BudgetKind, BudgetPreset

from .orchestrator_support import (
    FakeRuntime,
    FakeTools,
    PostgresOrchestratorTestCase,
    SpyBudget,
    diamond,
    fail,
    make_plan,
    node,
    ok,
    quiet,
    requires_postgres,
    until,
)

Out = RunOutcome


@requires_postgres
class BudgetTest(PostgresOrchestratorTestCase):
    async def set_limit(self, task_id, kind: str, limit: int) -> None:
        await self.owner_sql(
            "UPDATE budget_usages SET limit_value = :l"
            " WHERE task_id = :t AND kind = :k",
            l=limit,
            t=task_id,
            k=kind,
        )

    async def consumed(self, harness, task_id) -> dict[str, int]:
        return {u.kind.value: u.consumed for u in await harness.budget.usage(task_id)}

    async def test_the_steps_budget_stops_new_nodes_and_the_run_can_resume(self):
        rt = {"local": FakeRuntime("local")}
        h = self.harness(runtimes=rt)
        task_id = await self.prepare(h, make_plan(*(node(f"n{i}") for i in range(4))))
        await self.set_limit(task_id, "steps", 2)

        report = await h.orchestrator.run_once("w1")

        # Two nodes fit in the budget; the rest wait for a human.
        self.assertEqual(report.outcome, Out.WAITING_FOR_USER)
        self.assertEqual([a.node_key for a in rt["local"].assignments], ["n0", "n1"])
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(
            (snapshot.state, snapshot.wait_reason), (TaskState.WAITING, WaitReason.USER)
        )
        self.assertEqual((await self.consumed(h, task_id))["steps"], 2)
        self.assertEqual(
            await self.states_of(task_id),
            {"n0": "succeeded", "n1": "succeeded", "n2": "ready", "n3": "ready"},
        )

        # A human raises the limit, unblocks the task and queues it again.
        await self.set_limit(task_id, "steps", 10)
        await h.tasks.execute(task_id, TaskCommand.UNBLOCK, actor=self.user)
        await h.queue.enqueue(task_id)
        report = await h.orchestrator.run_once("w2")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        # The finished nodes did not run again; the DAG was taken over (epoch 2).
        self.assertEqual(
            [a.node_key for a in rt["local"].assignments], ["n0", "n1", "n2", "n3"]
        )
        self.assertEqual((await self.store.get(task_id, 1)).epoch, 2)
        self.assertEqual((await self.consumed(h, task_id))["steps"], 4)

    async def test_a_used_up_retry_budget_fails_the_task_after_the_others_finish(self):
        rt = {"local": FakeRuntime("local", script={"a": fail("Boom", "x")})}
        h = self.harness(runtimes=rt)
        task_id = await self.prepare(
            h, make_plan(node("a"), node("b", "a"), node("free"))
        )
        await self.set_limit(task_id, "retries", 1)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.BUDGET_FAILED)
        # One retry was allowed, the second failure found the budget used up.
        self.assertEqual(len(rt["local"].calls_of("a")), 2)
        self.assertEqual((await self.consumed(h, task_id))["retries"], 1)
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.FAILED)
        self.assertEqual(snapshot.last_event.reason, "The retry budget is used up")
        # The independent node was not abandoned; the dependent never ran.
        self.assertEqual(
            await self.states_of(task_id),
            {"a": "failed", "b": "blocked", "free": "succeeded"},
        )

    async def test_a_node_that_spends_the_budget_stops_every_node_and_tool_call(self):
        tools = FakeTools()
        stopped = []

        async def spender(assignment):
            try:
                await assignment.budget.charge(BudgetKind.TOKENS, 100)
            except NodeStopped as stop:
                stopped.append(("spender", stop.reason))
                raise
            return ok("never")

        async def bystander(assignment):
            await release.wait()
            try:
                await assignment.tools.call("repo.read_file", {"path": "x"})
            except NodeStopped as stop:
                stopped.append(("bystander", stop.reason))
                raise
            return ok("never")

        release = asyncio.Event()
        rt = {
            "local": FakeRuntime("local", script={"spend": spender, "stay": bystander})
        }
        h = self.harness(runtimes=rt, tools=tools)
        task_id = await self.prepare(
            h, make_plan(node("stay"), node("spend"), node("after", "spend"))
        )
        await self.set_limit(task_id, "tokens", 50)

        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(
            lambda: ("spender", StopReason.BUDGET_EXCEEDED) in stopped,
            message="the spender",
        )
        release.set()
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(
            report.outcome, Out.WAITING_FOR_USER
        )  # tokens: a human decides
        # The other node's tool call was refused before it reached the tools.
        self.assertIn(("bystander", StopReason.BUDGET_EXCEEDED), stopped)
        self.assertEqual(tools.calls, [])
        # The overshoot is recorded, and it is the parent task's budget that took it.
        self.assertEqual((await self.consumed(h, task_id))["tokens"], 100)
        self.assertEqual(
            await self.states_of(task_id),
            {"stay": "ready", "spend": "ready", "after": "pending"},
        )
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.WAITING)

    async def test_a_node_can_report_usage_and_read_what_is_left(self):
        seen = {}

        async def frugal(assignment):
            await assignment.budget.charge(BudgetKind.TOKENS, 30)
            await assignment.budget.charge(BudgetKind.GPU_SECONDS, 5)
            seen.update(await assignment.budget.remaining())
            return ok("frugal")

        rt = {"local": FakeRuntime("local", script={"a": frugal})}
        h = self.harness(runtimes=rt)
        task_id = await self.prepare(h, make_plan(node("a")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        consumed = await self.consumed(h, task_id)
        self.assertEqual((consumed["tokens"], consumed["gpu_seconds"]), (30, 5))
        self.assertEqual(seen[BudgetKind.TOKENS], 1_000_000 - 30)
        self.assertEqual(seen[BudgetKind.STEPS], 49)

    async def test_the_runtime_timer_is_not_a_node_charge(self):
        async def sneaky(assignment):
            await assignment.budget.charge(BudgetKind.RUNTIME_SECONDS, 1)
            return ok()

        rt = {"local": FakeRuntime("local", script={"a": sneaky})}
        h = self.harness(runtimes=rt)
        task_id = await self.prepare(h, make_plan(node("a")))

        report = await h.orchestrator.run_once("w1")

        # The tracker refuses the charge every time (the timer is the tracker's own):
        # the same failure repeats, an alternative is tried, and then a human decides.
        self.assertEqual(report.outcome, Out.WAITING_FOR_USER)
        self.assertEqual(len(rt["local"].assignments), 6)
        consumed = await self.consumed(h, task_id)
        self.assertEqual(consumed["steps"], 6)
        self.assertLess(consumed["runtime_seconds"], 60)  # the timer, not a node

    async def test_a_task_without_a_budget_is_failed_and_never_starts_its_timer(self):
        spy = SpyBudget(self.database)
        rt = {"local": FakeRuntime("local")}
        h = self.harness(runtimes=rt, budget=spy)
        task_id = await self.create_task()
        await h.orchestrator.submit_plan(task_id, make_plan(node("a")))
        await h.queue.enqueue(task_id)  # NOT through enqueue_task: no preset

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.BUDGET_NOT_CONFIGURED)
        self.assertEqual(spy.started, [])
        self.assertEqual(rt["local"].assignments, [])
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.FAILED)
        self.assertEqual(snapshot.last_event.reason, "The task has no budget preset")

    async def test_an_unlimited_task_runs_without_limits(self):
        h = self.harness()
        task_id = await self.prepare(h, diamond(), preset=BudgetPreset.UNLIMITED)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual((await self.consumed(h, task_id))["steps"], 5)

    async def test_a_budget_already_used_up_at_the_start_never_runs_a_node(self):
        rt = {"local": FakeRuntime("local")}
        h = self.harness(runtimes=rt)
        task_id = await self.prepare(h, make_plan(node("a")))
        await self.set_limit(task_id, "steps", 0)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.WAITING_FOR_USER)
        self.assertEqual(rt["local"].assignments, [])
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.WAITING)
        await quiet(0.05)


if __name__ == "__main__":
    unittest.main()
