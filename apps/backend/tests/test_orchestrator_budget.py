"""A sub-agent never exceeds its parent's budget (PAW-034), and what a used-up
budget does (Decision 0007: retries fail the task, the rest wait for a human)."""

import asyncio
import unittest

from paw_backend.orchestrator.domain import RunOutcome
from paw_backend.orchestrator.errors import NodeStateError, NodeStopped, StopReason
from paw_backend.tasks import TaskCommand, TaskNotRunningError, TaskState, WaitReason
from paw_backend.tasks.queueing import (
    BudgetKind,
    BudgetPreset,
    InvalidQueueingArgumentError,
)

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

    async def replaced_while_charging(self, command: TaskCommand):
        """A node that charges only after its task was failed and ``command``
        (Retry / Restart) replaced its run, before the orchestrator looked again
        (an hour between the looks). Returns ``(h, task_id, stopped)``."""
        release = asyncio.Event()
        stopped = []

        async def late_spender(assignment):
            await release.wait()
            try:
                await assignment.budget.charge(BudgetKind.TOKENS, 100)
            except NodeStopped as stop:
                stopped.append(stop.reason)
                raise
            return ok("charged")

        rt = {"local": FakeRuntime("local", script={"spend": late_spender})}
        h = self.harness(runtimes=rt, config={"poll_seconds": 3600.0})
        task_id = await self.prepare(h, make_plan(node("spend")))
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(rt["local"].assignments) == 1, message="the node")
        before = await self.consumed(h, task_id)

        other = self.harness()
        await other.tasks.execute(task_id, TaskCommand.FAIL, actor=self.system)
        await other.tasks.execute(task_id, command, actor=self.user)
        release.set()
        await until(lambda: stopped, message="the refused charge")
        self.assertEqual(await self.consumed(h, task_id), before)
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        return h, task_id, stopped

    async def test_a_replaced_run_cannot_charge_the_budget_after_a_retry(self):
        _, _, stopped = await self.replaced_while_charging(TaskCommand.RETRY)
        self.assertEqual(stopped, [StopReason.SUPERSEDED])

    async def test_a_replaced_run_cannot_charge_the_budget_after_a_restart(self):
        _, _, stopped = await self.replaced_while_charging(TaskCommand.RESTART)
        self.assertEqual(stopped, [StopReason.SUPERSEDED])

    async def test_an_ended_run_cannot_charge_the_budget(self):
        release = asyncio.Event()
        stopped = []

        async def late_spender(assignment):
            await release.wait()
            try:
                await assignment.budget.charge(BudgetKind.TOKENS, 100)
            except NodeStopped as stop:
                stopped.append(stop.reason)
                raise
            return ok("charged")

        rt = {"local": FakeRuntime("local", script={"spend": late_spender})}
        h = self.harness(runtimes=rt, config={"poll_seconds": 3600.0})
        task_id = await self.prepare(h, make_plan(node("spend")))
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(rt["local"].assignments) == 1, message="the node")
        before = await self.consumed(h, task_id)

        await self.harness().tasks.execute(task_id, TaskCommand.FAIL, actor=self.system)
        release.set()
        await until(lambda: stopped, message="the refused charge")

        self.assertEqual(stopped, [StopReason.TASK_ENDED])
        self.assertEqual(await self.consumed(h, task_id), before)
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)

    async def test_a_node_charges_only_what_a_runtime_reports(self):
        # Steps, retries and tool calls are counted by the orchestrator and the
        # Broker (and the runtime by the timer): a node may report tokens and GPU
        # time only. Anything else is refused before anything is written.
        refused = []

        async def cheat(assignment):
            for kind in (
                BudgetKind.STEPS,
                BudgetKind.RETRIES,
                BudgetKind.TOOL_CALLS,
                BudgetKind.RUNTIME_SECONDS,
                "tokens",
                None,
            ):
                try:
                    await assignment.budget.charge(kind, 1000)
                except InvalidQueueingArgumentError as error:
                    refused.append((kind, error.parameter))
            await assignment.budget.charge(BudgetKind.TOKENS, 7)
            await assignment.budget.charge(BudgetKind.GPU_SECONDS, 2)
            return ok()

        rt = {"local": FakeRuntime("local", script={"a": cheat})}
        h = self.harness(runtimes=rt)
        task_id = await self.prepare(h, make_plan(node("a")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(refused), 6)
        self.assertEqual({p for _, p in refused}, {"kind"})
        consumed = await self.consumed(h, task_id)
        self.assertEqual(
            (consumed["steps"], consumed["retries"], consumed["tool_calls"]),
            (1, 0, 0),
        )
        self.assertEqual((consumed["tokens"], consumed["gpu_seconds"]), (7, 2))

    async def test_a_failure_of_a_replaced_run_is_not_counted_for_the_new_one(self):
        # The node fails after its task was failed and retried (before the next
        # look): its failure must not enter the loop history of the new run.
        release = asyncio.Event()

        async def late_failure(_assignment):
            await release.wait()
            return fail("Boom", "late")

        rt = {"local": FakeRuntime("local", script={"a": late_failure})}
        h = self.harness(runtimes=rt, config={"poll_seconds": 3600.0})
        task_id = await self.prepare(h, make_plan(node("a")))
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(rt["local"].assignments) == 1, message="the node")
        before = await self.consumed(h, task_id)

        other = self.harness()
        await other.tasks.execute(task_id, TaskCommand.FAIL, actor=self.system)
        await other.tasks.execute(task_id, TaskCommand.RETRY, actor=self.user)
        release.set()
        await asyncio.wait_for(run, 120)

        self.assertEqual(await h.loops.history(task_id), ())
        after = await self.consumed(h, task_id)
        self.assertEqual(after["retries"], before["retries"])

    async def started_run(self, h, plan):
        """A task whose run a worker holds (claimed, started, DAG taken over)."""
        from paw_backend.orchestrator.gateway import RunGuard
        from paw_backend.orchestrator.orchestrator import _Run

        task_id = await self.prepare(h, plan)
        entry = await h.queue.claim_next("w1")
        event = await h.tasks.execute(task_id, TaskCommand.START, actor=self.system)
        snapshot = await h.tasks.restore(task_id, log_limit=0)
        dag = await self.store.acquire(
            (await self.store.get(task_id, 1)).id, "w1", event.run
        )
        run = _Run(
            snapshot, event.run, entry, "w1", RunGuard(task_id, event.run, h.activity)
        )
        run.epoch = dag.epoch
        return task_id, dag, run

    async def test_a_step_is_charged_only_with_the_start_it_pays_for(self):
        # The start of an attempt and its step are ONE transaction: a refused
        # start (a node that is not ready, a paused task) charges nothing, and a
        # charged step always has its attempt.
        h = self.harness()
        task_id, dag, run = await self.started_run(
            h, make_plan(node("a"), node("b", "a"))
        )

        with self.assertRaises(NodeStateError):  # "b" waits for "a"
            await h.orchestrator._start_node(run, dag.id, "b")
        await h.tasks.execute(task_id, TaskCommand.PAUSE, actor=self.user)
        with self.assertRaises(TaskNotRunningError):
            await h.orchestrator._start_node(run, dag.id, "a")
        self.assertEqual((await self.consumed(h, task_id))["steps"], 0)
        self.assertEqual(await self.states_of(task_id), {"a": "ready", "b": "pending"})

        await h.tasks.execute(task_id, TaskCommand.RESUME, actor=self.user)
        attempt = await h.orchestrator._start_node(run, dag.id, "a")
        self.assertEqual(attempt.number, 1)
        self.assertEqual((await self.consumed(h, task_id))["steps"], 1)
        self.assertEqual(
            await self.states_of(task_id), {"a": "running", "b": "pending"}
        )

    async def test_a_planner_call_starts_with_its_charges_in_one_transaction(self):
        # The step and the retry of a planner call are charged together, and
        # only while the task runs: that charge is the call's durable start.
        h = self.harness()
        task_id, _, run = await self.started_run(h, make_plan(node("a")))
        both = {BudgetKind.STEPS: 1, BudgetKind.RETRIES: 1}

        await h.tasks.execute(task_id, TaskCommand.PAUSE, actor=self.user)
        with self.assertRaises(TaskNotRunningError):
            await h.orchestrator._start_planner_call(run, both)
        consumed = await self.consumed(h, task_id)
        self.assertEqual((consumed["steps"], consumed["retries"]), (0, 0))

        await h.tasks.execute(task_id, TaskCommand.RESUME, actor=self.user)
        await h.orchestrator._start_planner_call(run, both)
        consumed = await self.consumed(h, task_id)
        self.assertEqual((consumed["steps"], consumed["retries"]), (1, 1))

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
