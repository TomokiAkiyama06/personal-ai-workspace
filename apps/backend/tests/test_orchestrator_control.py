"""Pause, Cancel, Stop Now, Retry and Restart while a DAG runs (real PostgreSQL).

The orchestrator reads the task's state whenever a node ends and on every poll of
the manual clock, and it never hands a tool call to a task that is not active.
"""

import asyncio
import unittest

from paw_backend.orchestrator.domain import AttemptState, DagState, RunOutcome
from paw_backend.orchestrator.errors import NodeStopped, StopReason
from paw_backend.tasks import TaskCommand, TaskState

from .orchestrator_support import (
    FakeRuntime,
    FakeTools,
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


@requires_postgres
class ControlTest(PostgresOrchestratorTestCase):
    async def running_run(self, plan, **options):
        """A run in progress with the gated nodes waiting; returns everything."""
        runtime = FakeRuntime("local")
        for key in options.pop("gates", ()):
            runtime.gate(key)
        h = self.harness(runtimes={"local": runtime}, **options)
        task_id = await self.prepare(h, plan)
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        return h, runtime, task_id, run

    async def poll(self, h, seconds: float = 2.0) -> None:
        """Let the orchestrator's poll of the task's state fire."""
        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll timer")
        await h.clock.advance(seconds)

    async def test_a_cancel_stops_the_running_nodes_and_the_dag(self):
        h, runtime, task_id, run = await self.running_run(diamond(), gates=("a", "e"))
        await until(lambda: len(runtime.assignments) == 2, message="two nodes")
        await h.tasks.execute(task_id, TaskCommand.CANCEL, actor=self.user)

        await self.poll(h)
        report = await asyncio.wait_for(run, 120)

        self.assertEqual(report.outcome, Out.TASK_ENDED)
        self.assertEqual(report.dag_state, DagState.CANCELLED)
        self.assertEqual(
            await self.states_of(task_id),
            {
                "a": "cancelled",
                "b": "cancelled",
                "c": "cancelled",
                "d": "cancelled",
                "e": "cancelled",
            },
        )
        dag = await self.store.get(task_id, 1)
        self.assertEqual(
            {a.state for a in await self.store.attempts(dag.id)},
            {AttemptState.INTERRUPTED},
        )
        # The runtime saw its coroutines cancelled: both nodes ended without results.
        self.assertEqual(len([e for e in runtime.timeline if e[0] == "end"]), 2)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.CANCELLED)
        # Nothing was left running: the timer stopped and the entry is not claimable.
        row = (
            await self.rows(
                "SELECT running_since FROM budget_usages WHERE kind = 'runtime_seconds'"
            )
        )[0]
        self.assertIsNone(row["running_since"])

    async def test_a_stop_now_is_handled_like_a_cancel(self):
        h, runtime, task_id, run = await self.running_run(
            make_plan(node("a")), gates=("a",)
        )
        await until(lambda: len(runtime.assignments) == 1, message="the node")
        await h.tasks.execute(
            task_id, TaskCommand.STOP_NOW, actor=self.user, reason="emergency"
        )

        await self.poll(h)
        report = await asyncio.wait_for(run, 120)

        self.assertEqual(report.outcome, Out.TASK_ENDED)
        self.assertEqual(await self.states_of(task_id), {"a": "cancelled"})

    async def test_a_tool_call_of_an_active_task_is_handed_to_the_tools(self):
        tools = FakeTools()
        outcomes = []

        async def caller(assignment):
            await release.wait()
            try:
                await assignment.tools.call("repo.read_file", {"path": "a.py"})
            except NodeStopped as stop:
                outcomes.append(stop.reason)
                raise
            return ok()

        release = asyncio.Event()
        runtime = FakeRuntime("local", script={"a": caller})
        h = self.harness(runtimes={"local": runtime}, tools=tools)
        await self.prepare(h, make_plan(node("a")))
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="the node")

        # While the task is active a call goes through to the tools.
        release.set()
        await until(
            lambda: len(tools.calls) + len(outcomes) == 1, message="the first call"
        )
        report = await asyncio.wait_for(run, 120)
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(tools.calls), 1)

    async def test_a_cancelled_task_is_never_handed_a_tool_call(self):
        tools = FakeTools()
        outcomes = []

        async def caller(assignment):
            await release.wait()
            try:
                await assignment.tools.call("repo.read_file", {"path": "a.py"})
            except NodeStopped as stop:
                outcomes.append(stop.reason)
                raise
            return ok()

        release = asyncio.Event()
        runtime = FakeRuntime("local", script={"a": caller})
        h = self.harness(runtimes={"local": runtime}, tools=tools)
        task_id = await self.prepare(h, make_plan(node("a")))
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="the node")
        await h.tasks.execute(task_id, TaskCommand.CANCEL, actor=self.user)

        # The node has not polled yet: it wakes and calls a tool of a dead task.
        release.set()
        report = await asyncio.wait_for(run, 120)

        self.assertEqual(outcomes, [StopReason.TASK_ENDED])
        self.assertEqual(tools.calls, [])  # nothing was handed to the Broker
        self.assertEqual(report.outcome, Out.TASK_ENDED)
        self.assertEqual(await self.states_of(task_id), {"a": "cancelled"})

    async def test_a_pause_lets_running_nodes_finish_and_starts_no_more(self):
        h, runtime, task_id, run = await self.running_run(diamond(), gates=("a", "e"))
        await until(lambda: len(runtime.assignments) == 2, message="two nodes")
        await h.tasks.execute(task_id, TaskCommand.PAUSE, actor=self.user)

        await self.poll(h)  # the orchestrator sees the pause; the nodes still run
        await quiet()
        self.assertFalse(run.done())
        runtime.gates["a"].set()
        runtime.gates["e"].set()
        report = await asyncio.wait_for(run, 120)

        self.assertEqual(report.outcome, Out.PAUSED)
        # a and e finished; b and c became ready but were not started.
        self.assertEqual(
            await self.states_of(task_id),
            {
                "a": "succeeded",
                "b": "ready",
                "c": "ready",
                "d": "pending",
                "e": "succeeded",
            },
        )
        self.assertEqual(len(runtime.assignments), 2)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.PAUSED)
        (entry,) = await self.rows("SELECT status FROM queue_entries")
        self.assertEqual(entry["status"], "completed")

        # Resume and queue again: the DAG continues where it stopped.
        await h.tasks.execute(task_id, TaskCommand.RESUME, actor=self.user)
        await h.queue.enqueue(task_id)
        report = await h.orchestrator.run_once("w2")
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(
            sorted(a.node_key for a in runtime.assignments), ["a", "b", "c", "d", "e"]
        )

    async def test_a_pause_that_is_resumed_before_the_nodes_finish_is_ignored(self):
        h, runtime, task_id, run = await self.running_run(
            make_plan(node("a"), node("b", "a")), gates=("a",)
        )
        await until(lambda: len(runtime.assignments) == 1, message="the node")
        await h.tasks.execute(task_id, TaskCommand.PAUSE, actor=self.user)
        await self.poll(h)
        await h.tasks.execute(task_id, TaskCommand.RESUME, actor=self.user)
        await self.poll(h)
        runtime.gates["a"].set()

        report = await asyncio.wait_for(run, 120)

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)

    async def test_a_restart_replaces_the_run_and_the_old_worker_writes_nothing(self):
        h, runtime, task_id, run = await self.running_run(
            make_plan(node("a"), node("b", "a")), gates=("a",)
        )
        await until(lambda: len(runtime.assignments) == 1, message="the node")
        before = await self.store.get(task_id, 1)
        await h.tasks.execute(task_id, TaskCommand.CANCEL, actor=self.user)
        await h.tasks.execute(task_id, TaskCommand.RESTART, actor=self.user)

        await self.poll(h)
        report = await asyncio.wait_for(run, 120)

        self.assertEqual(report.outcome, Out.SUPERSEDED)
        # Attempt 1's DAG is exactly as the worker left it: no write after the
        # replacement, not even a cancel.
        after = await self.store.get(task_id, 1)
        self.assertEqual(after.epoch, before.epoch)
        self.assertEqual(after.state, DagState.ACTIVE)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.QUEUED)
        self.assertEqual((await h.tasks.restore(task_id)).attempt.number, 2)

    async def test_a_retry_replaces_the_run_of_a_failed_task_too(self):
        h, runtime, task_id, run = await self.running_run(
            make_plan(node("a")), gates=("a",)
        )
        await until(lambda: len(runtime.assignments) == 1, message="the node")
        await h.tasks.execute(task_id, TaskCommand.FAIL, actor=self.system)
        await h.tasks.execute(task_id, TaskCommand.RETRY, actor=self.user)

        await self.poll(h)
        report = await asyncio.wait_for(run, 120)

        self.assertEqual(report.outcome, Out.SUPERSEDED)

    async def test_a_task_that_failed_under_the_run_leaves_the_nodes_ready(self):
        h, runtime, task_id, run = await self.running_run(
            make_plan(node("a")), gates=("a",)
        )
        await until(lambda: len(runtime.assignments) == 1, message="the node")
        await h.tasks.execute(task_id, TaskCommand.FAIL, actor=self.system)

        await self.poll(h)
        report = await asyncio.wait_for(run, 120)

        self.assertEqual(report.outcome, Out.TASK_ENDED)
        self.assertEqual(
            await self.states_of(task_id), {"a": "ready"}
        )  # a Retry resumes it
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.state, DagState.ACTIVE)
        # A Retry of the task: the next run takes the DAG over and finishes it.
        await h.tasks.execute(task_id, TaskCommand.RETRY, actor=self.user)
        await h.queue.enqueue(task_id)
        runtime.gates["a"].set()
        report = await h.orchestrator.run_once("w2")
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)

    async def test_entries_of_tasks_that_cannot_run_are_completed_and_skipped(self):
        h = self.harness()
        cases = {}
        for label, commands in {
            "paused": [TaskCommand.START, TaskCommand.PAUSE],
            "waiting": [TaskCommand.START, TaskCommand.WAIT],
            "evaluating": [TaskCommand.START, TaskCommand.BEGIN_EVALUATION],
            "cancelled": [TaskCommand.CANCEL],
            "failed": [TaskCommand.START, TaskCommand.FAIL],
        }.items():
            task_id = await self.create_task()
            for command in commands:
                await h.tasks.execute(
                    task_id,
                    command,
                    actor=self.system,
                    wait_reason="user" if command is TaskCommand.WAIT else None,
                )
            await h.queue.enqueue(task_id)
            cases[label] = task_id

        for label, task_id in cases.items():
            with self.subTest(label):
                report = await h.orchestrator.run_once("w1")
                self.assertEqual(
                    (report.outcome, report.task_id), (Out.SKIPPED, task_id)
                )
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM queue_entries WHERE status = 'completed'"
            ),
            5,
        )
        self.assertEqual(await self.scalar("SELECT count(*) FROM agent_dags"), 0)
        self.assertEqual((await h.orchestrator.run_once("w1")).outcome, Out.IDLE)


if __name__ == "__main__":
    unittest.main()
