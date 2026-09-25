"""The runtime budget is enforced while nodes run (PAW-034).

``runtime_seconds`` accrues in ``BudgetTracker`` while the task runs, and no node
reports it. The orchestrator therefore looks at the whole budget every time it
wakes (a node ended, or the poll of the task's state fired) and stops the run when
a limit is crossed: the running nodes are cancelled cleanly (their attempts are
``interrupted`` and the nodes ``ready`` again), no node starts, and the task goes
where Decision 0007 sends it (``retries``: failed; everything else: waiting for a
human).

Time: the tracker gets a settable clock (its test seam) that the tests move with
the orchestrator's ``ManualClock``; nothing waits for real time.
"""

import asyncio
import unittest

from paw_backend.orchestrator import RunOutcome
from paw_backend.orchestrator.domain import AttemptState
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.runtime import NodeOutcome
from paw_backend.tasks import TaskCommand, TaskState, WaitReason
from paw_backend.tasks.queueing import BudgetKind, BudgetPreset, BudgetTracker

from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    fail,
    hang,
    make_plan,
    node,
    requires_postgres,
    until,
)
from .queueing_support import FakeClock

Out = RunOutcome
STANDARD_RUNTIME = 3_600


class TrackerClock:
    """A tracker whose clock the test moves (its test seam), in a harness."""

    def build(self, runtime, **options):
        self.fake = FakeClock()  # the tracker's clock (its test seam)
        tracker = BudgetTracker(
            self.new_database(), clock=self.fake, allow_explicit_clock=True
        )
        return self.harness(runtimes={"local": runtime}, budget=tracker, **options)

    async def runtime_used(self, h, task_id) -> int:
        usage = await h.budget.usage(task_id)
        return next(u.consumed for u in usage if u.kind.value == "runtime_seconds")


@requires_postgres
class RuntimeBudgetTest(TrackerClock, PostgresOrchestratorTestCase):
    async def test_a_node_that_never_finishes_is_stopped_when_the_runtime_is_used_up(
        self,
    ):
        runtime = FakeRuntime("local", script={"a": hang})
        h = self.build(runtime)
        task_id = await self.prepare(h, make_plan(node("a"), node("b", "a")))
        real_interrupt = h.store.interrupt
        ended_when_interrupted = []

        async def interrupt(*args, **kwargs):
            # The node's coroutine must be cancelled BEFORE its attempt is written
            # off, so that nothing runs on for an attempt that is already closed.
            ended_when_interrupted.append(
                [e for e in runtime.timeline if e[0] == "end"]
            )
            return await real_interrupt(*args, **kwargs)

        h.store.interrupt = interrupt
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="the node")

        # Before the limit: the poll finds nothing to stop.
        self.fake.set(STANDARD_RUNTIME - 10)
        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll timer")
        await h.clock.advance(2.0)
        self.assertFalse(running.done())
        # Past the limit: the next poll stops the run.
        self.fake.set(STANDARD_RUNTIME + 5)
        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the next poll")
        await h.clock.advance(2.0)
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.WAITING_FOR_USER)
        self.assertEqual(ended_when_interrupted, [[("end", "a", 1)]])
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(
            (snapshot.state, snapshot.wait_reason), (TaskState.WAITING, WaitReason.USER)
        )
        # The node was cancelled cleanly: its coroutine ended, its attempt is
        # interrupted, and it is ready again for whoever resumes the task.
        self.assertEqual(
            [e for e in runtime.timeline if e[0] == "end"], [("end", "a", 1)]
        )
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node("a").state.value, "ready")
        self.assertEqual(dag.node("b").state.value, "pending")
        (attempt,) = await self.store.attempts(dag.id, "a")
        self.assertEqual(attempt.state, AttemptState.INTERRUPTED)
        # The timer was settled, the entry completed, no node started afterwards.
        self.assertGreaterEqual(await self.runtime_used(h, task_id), STANDARD_RUNTIME)
        self.assertEqual(len(runtime.assignments), 1)
        (entry,) = await self.rows("SELECT status FROM queue_entries")
        self.assertEqual(entry["status"], "completed")

    async def test_a_node_that_succeeds_after_the_limit_starts_no_more_nodes(self):
        runtime = FakeRuntime("local")
        runtime.gate("a")
        h = self.build(runtime)
        task_id = await self.prepare(h, make_plan(node("a"), node("b", "a")))
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="the node")

        self.fake.set(STANDARD_RUNTIME + 1)  # the limit passes while the node runs
        runtime.gates["a"].set()  # ... and the node still returns success
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.WAITING_FOR_USER)
        self.assertEqual(
            await self.states_of(task_id), {"a": "succeeded", "b": "ready"}
        )  # its result stands; the dependent did not start
        self.assertEqual([a.node_key for a in runtime.assignments], ["a"])

    async def test_a_node_that_finished_at_the_same_moment_keeps_its_result(self):
        runtime = FakeRuntime("local", script={"slow": hang})
        runtime.gate("fast")
        h = self.build(runtime)
        task_id = await self.prepare(h, make_plan(node("fast"), node("slow")))
        real_check = h.budget.check
        released = []

        async def check(task_id, **kwargs):
            # The poll's look at the budget: while it is being made, the fast node
            # ends (a node ends at any moment), so the loop finds a finished node
            # that it has not written yet when the budget turns out to be used up.
            if not kwargs and len(runtime.assignments) == 2 and not released:
                released.append(1)
                runtime.gates["fast"].set()
                await h.clock.settle()
            return await real_check(task_id, **kwargs)

        h.budget.check = check
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 2, message="both nodes")
        self.fake.set(STANDARD_RUNTIME + 1)
        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll timer")
        await h.clock.advance(2.0)
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.WAITING_FOR_USER)
        self.assertEqual(released, [1])
        self.assertEqual(
            await self.states_of(task_id), {"fast": "succeeded", "slow": "ready"}
        )
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node("fast").result.summary, "fast by local")

    async def test_the_run_can_be_resumed_after_a_human_raises_the_limit(self):
        runtime = FakeRuntime("local", script={"a": [hang]})
        h = self.build(runtime)
        task_id = await self.prepare(h, make_plan(node("a")))
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="the node")
        self.fake.set(STANDARD_RUNTIME + 1)
        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll timer")
        await h.clock.advance(2.0)
        await asyncio.wait_for(running, 120)
        await self.owner_sql(
            "UPDATE budget_usages SET limit_value = 100000"
            " WHERE kind = 'runtime_seconds'"
        )
        runtime.script["a"] = []  # the next attempt succeeds
        await h.tasks.execute(task_id, TaskCommand.UNBLOCK, actor=self.user)
        await h.queue.enqueue(task_id)

        report = await h.orchestrator.run_once("w2")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(runtime.calls_of("a")), 2)

    async def test_an_unlimited_task_is_never_stopped_for_its_runtime(self):
        runtime = FakeRuntime("local")
        runtime.gate("a")
        h = self.build(runtime)
        await self.prepare(h, make_plan(node("a")), preset=BudgetPreset.UNLIMITED)
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="the node")
        self.fake.set(10**7)
        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll timer")
        await h.clock.advance(2.0)
        self.assertFalse(running.done())

        runtime.gates["a"].set()
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)

    async def test_the_poll_checks_every_limit_not_only_the_runtime(self):
        # Another limit crossed by something other than the run itself (a human
        # lowered it, another process charged the task): the poll sees it too.
        runtime = FakeRuntime("local", script={"a": hang})
        h = self.build(runtime)
        task_id = await self.prepare(h, make_plan(node("a")))
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="the node")
        await h.budget.record(task_id, BudgetKind.TOKENS, 2_000_000)

        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll timer")
        await h.clock.advance(2.0)
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.WAITING_FOR_USER)
        self.assertEqual(await self.states_of(task_id), {"a": "ready"})

    async def test_a_used_up_retry_budget_wins_over_a_wait_that_was_already_decided(
        self,
    ):
        # Node ``a`` fails the same way until nothing is left to try (a wait for a
        # human is decided) while ``b`` still runs; then the poll finds the retry
        # budget used up: failing the task beats waiting for a human.
        same = fail("Stuck", "the same failure")
        runtime = FakeRuntime("local", script={"a": same, "b": hang})
        h = self.build(runtime)
        task_id = await self.prepare(h, make_plan(node("a"), node("b")))
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(
            lambda: len(runtime.calls_of("a")) == 6, message="six attempts of a"
        )
        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll timer")
        await h.budget.record(task_id, BudgetKind.RETRIES, 11)  # Standard allows 10
        await h.clock.advance(2.0)
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.BUDGET_FAILED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.FAILED)

    async def test_a_used_up_retry_budget_found_on_a_poll_fails_the_task(self):
        runtime = FakeRuntime("local", script={"a": hang})
        h = self.build(runtime)
        task_id = await self.prepare(h, make_plan(node("a")))
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="the node")
        await h.budget.record(task_id, BudgetKind.RETRIES, 11)  # Standard allows 10

        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll timer")
        await h.clock.advance(2.0)
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.BUDGET_FAILED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.FAILED)
        self.assertEqual(await self.states_of(task_id), {"a": "ready"})


PLANNER_TIMEOUT = 7_200.0  # longer than the runtime limit of the Standard preset
PLAN = NodeOutcome.succeeded(
    NodeResult("a plan"), plan={"nodes": [node("a"), node("b", "a")]}
)


@requires_postgres
class PlannerRuntimeBudgetTest(TrackerClock, PostgresOrchestratorTestCase):
    """The budget is polled while the planner runs, as it is while nodes run: a
    planner that never returns must not keep a task past its runtime limit (the
    planner's own timeout is longer than the limit here)."""

    def build(self, runtime, **options):
        options.setdefault("config", {"node_timeout_seconds": PLANNER_TIMEOUT})
        return super().build(runtime, **options)

    async def poll(self, h) -> None:
        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll timer")
        await h.clock.advance(2.0)

    def planner_ends(self, planner) -> list:
        return [e for e in planner.timeline if e[0] == "end"]

    async def test_a_planner_that_never_returns_is_stopped_at_the_runtime_limit(self):
        planner = FakeRuntime("local", script={"plan": hang})
        h = self.build(planner)
        task_id = await self.prepare(h)
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(planner.calls_of("plan")) == 1, message="the planner")

        # Before the limit the poll finds nothing to stop.
        self.fake.set(STANDARD_RUNTIME - 10)
        await self.poll(h)
        self.assertFalse(running.done())
        self.assertEqual(self.planner_ends(planner), [])  # still planning
        # Past the limit the next poll stops the run, well before the planner's
        # own timeout (which the clock never reaches).
        self.fake.set(STANDARD_RUNTIME + 5)
        await self.poll(h)
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.WAITING_FOR_USER)
        self.assertLess(h.clock.monotonic(), PLANNER_TIMEOUT)
        # The planner was cancelled cleanly, and not asked again.
        self.assertEqual(self.planner_ends(planner), [("end", "plan", 1)])
        self.assertEqual(len(planner.calls_of("plan")), 1)
        self.assertIsNone(await self.store.get(task_id, 1))
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(
            (snapshot.state, snapshot.wait_reason), (TaskState.WAITING, WaitReason.USER)
        )
        self.assertGreaterEqual(await self.runtime_used(h, task_id), STANDARD_RUNTIME)
        (entry,) = await self.rows("SELECT status FROM queue_entries")
        self.assertEqual(entry["status"], "completed")

    async def test_a_planner_that_returns_just_before_the_limit_gets_its_plan_run(self):
        planner = FakeRuntime("local", script={"plan": PLAN})
        planner.gate("plan")
        h = self.build(planner)
        task_id = await self.prepare(h)
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(planner.calls_of("plan")) == 1, message="the planner")

        self.fake.set(STANDARD_RUNTIME - 1)
        await self.poll(h)  # one second short of the limit: nothing to stop
        self.assertFalse(running.done())
        planner.gates["plan"].set()
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(
            await self.states_of(task_id), {"a": "succeeded", "b": "succeeded"}
        )
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.EVALUATING)

    async def test_a_plan_that_arrives_after_the_limit_is_kept_and_starts_nothing(self):
        planner = FakeRuntime("local", script={"plan": PLAN})
        planner.gate("plan")
        h = self.build(planner)
        task_id = await self.prepare(h)
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(planner.calls_of("plan")) == 1, message="the planner")

        self.fake.set(STANDARD_RUNTIME + 1)  # passes with no poll in between
        planner.gates["plan"].set()
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.WAITING_FOR_USER)
        self.assertEqual(await self.states_of(task_id), {"a": "ready", "b": "pending"})
        self.assertEqual([a.node_key for a in planner.assignments], ["plan"])

    async def test_a_used_up_retry_budget_found_while_planning_fails_the_task(self):
        planner = FakeRuntime("local", script={"plan": hang})
        h = self.build(planner)
        task_id = await self.prepare(h)
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(planner.calls_of("plan")) == 1, message="the planner")
        await h.budget.record(task_id, BudgetKind.RETRIES, 11)  # Standard allows 10

        await self.poll(h)
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.BUDGET_FAILED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.FAILED)
        self.assertIsNone(await self.store.get(task_id, 1))
        self.assertEqual(self.planner_ends(planner), [("end", "plan", 1)])

    async def test_a_cancelled_task_stops_the_planner_at_the_next_poll(self):
        planner = FakeRuntime("local", script={"plan": hang})
        h = self.build(planner)
        task_id = await self.prepare(h)
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(planner.calls_of("plan")) == 1, message="the planner")
        await h.tasks.execute(task_id, TaskCommand.CANCEL, actor=self.user)

        await self.poll(h)
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.TASK_ENDED)
        self.assertEqual(self.planner_ends(planner), [("end", "plan", 1)])
        self.assertIsNone(await self.store.get(task_id, 1))
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.CANCELLED)

    async def test_a_restarted_task_stops_the_old_planner_at_the_next_poll(self):
        planner = FakeRuntime("local", script={"plan": hang})
        h = self.build(planner)
        task_id = await self.prepare(h)
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(planner.calls_of("plan")) == 1, message="the planner")
        await h.tasks.execute(task_id, TaskCommand.CANCEL, actor=self.user)
        await h.tasks.execute(task_id, TaskCommand.RESTART, actor=self.user)

        await self.poll(h)
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.SUPERSEDED)
        self.assertEqual(self.planner_ends(planner), [("end", "plan", 1)])
        self.assertIsNone(await self.store.get(task_id, 1))

    async def test_a_pause_lets_the_planner_finish(self):
        planner = FakeRuntime("local", script={"plan": PLAN})
        planner.gate("plan")
        h = self.build(planner)
        task_id = await self.prepare(h)
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(planner.calls_of("plan")) == 1, message="the planner")
        await h.tasks.execute(task_id, TaskCommand.PAUSE, actor=self.user)

        await self.poll(h)  # the pause is seen; the planner still runs
        self.assertFalse(running.done())
        planner.gates["plan"].set()
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.PAUSED)
        self.assertEqual(await self.states_of(task_id), {"a": "ready", "b": "pending"})
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.PAUSED)


if __name__ == "__main__":
    unittest.main()
