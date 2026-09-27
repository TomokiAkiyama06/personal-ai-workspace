"""Hand-overs between the orchestrator, the queue and the task commands (PAW-034).

The invariants of the audit of issue #30 / PR #106 that these tests hold:

* **A task that needs a worker has an active queue entry.** A Retry, Restart,
  Resume or Unblock that commits while the worker still holds the task's entry
  cannot enqueue (one active entry per task); the worker then gives its entry back
  (``TaskQueue.finish``) instead of completing it, so the entry serves the task.
* **A budget preset changes only with a new queue entry.** ``enqueue_task`` sets
  the preset and inserts the entry in one transaction: a losing or duplicate
  enqueue changes no budget (a running task cannot be switched to Unlimited).
* **Waiting is a graceful stop.** A task put in waiting by someone else starts no
  new node; the running ones finish; an Unblock while they finish carries on.
"""

import asyncio
import unittest

from paw_backend.orchestrator import RunOutcome
from paw_backend.orchestrator.domain import DagState
from paw_backend.tasks import TaskCommand, TaskService, TaskState, WaitReason
from paw_backend.tasks.queueing import (
    BudgetPreset,
    LeaseLostError,
    QueueStatus,
    TaskAlreadyQueuedError,
    TaskQueue,
)

from .gate_support import ALWAYS_ACTIVE
from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    fail,
    make_plan,
    node,
    ok,
    requires_postgres,
    until,
)

Out = RunOutcome


async def preset_of(case, task_id) -> set[str]:
    rows = await case.rows(
        "SELECT DISTINCT preset FROM budget_usages WHERE task_id = :t", t=task_id
    )
    return {row["preset"] for row in rows}


@requires_postgres
class EnqueueIsAtomicTest(PostgresOrchestratorTestCase):
    async def test_a_losing_enqueue_does_not_change_the_budget(self):
        h = self.harness()
        task_id = await self.prepare(h, make_plan(node("a")))  # standard
        with self.assertRaises(TaskAlreadyQueuedError):
            await h.orchestrator.enqueue_task(task_id, preset=BudgetPreset.UNLIMITED)
        self.assertEqual(await preset_of(self, task_id), {"standard"})
        self.assertEqual(await self.scalar("SELECT count(*) FROM queue_entries"), 1)

    async def test_a_duplicate_enqueue_of_a_running_task_leaves_its_budget(self):
        runtime = FakeRuntime("local")
        runtime.gate("a")
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(h, make_plan(node("a")))
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="a running")

        with self.assertRaises(TaskAlreadyQueuedError):
            await h.orchestrator.enqueue_task(task_id, preset=BudgetPreset.UNLIMITED)

        self.assertEqual(await preset_of(self, task_id), {"standard"})
        runtime.gates["a"].set()
        self.assertEqual((await asyncio.wait_for(run, 60)).outcome, Out.DAG_SUCCEEDED)

    async def test_concurrent_enqueues_commit_one_entry_and_its_own_preset(self):
        h = self.harness()
        task_id = await self.create_task()
        other = self.harness()
        results = await asyncio.gather(
            h.orchestrator.enqueue_task(task_id, preset=BudgetPreset.STANDARD),
            other.orchestrator.enqueue_task(task_id, preset=BudgetPreset.UNLIMITED),
            return_exceptions=True,
        )
        won = [
            preset
            for preset, result in zip(("standard", "unlimited"), results, strict=True)
            if not isinstance(result, BaseException)
        ]
        lost = [r for r in results if isinstance(r, BaseException)]
        self.assertEqual(len(won), 1)
        self.assertEqual(len(lost), 1)
        self.assertIsInstance(lost[0], TaskAlreadyQueuedError)
        self.assertEqual(await preset_of(self, task_id), set(won))

    async def test_an_unknown_task_writes_nothing(self):
        import uuid

        from paw_backend.tasks import TaskNotFoundError

        h = self.harness()
        with self.assertRaises(TaskNotFoundError):
            await h.orchestrator.enqueue_task(uuid.uuid4(), preset="standard")
        self.assertEqual(await self.scalar("SELECT count(*) FROM budget_usages"), 0)


@requires_postgres
class FinishTest(PostgresOrchestratorTestCase):
    """``TaskQueue.finish``: complete, or give back when the task needs a worker."""

    async def claimed(self, h, state_commands=()):
        task_id = await self.create_task()
        await h.orchestrator.enqueue_task(task_id, preset="standard")
        entry = await h.queue.claim_next("w1")
        for command, kwargs in state_commands:
            await h.tasks.execute(task_id, command, **kwargs)
        return task_id, entry

    async def test_the_state_of_the_task_decides(self):
        system, user = {"actor": self.system}, {"actor": self.user}
        start = (TaskCommand.START, system)
        cases = {
            "queued": ((), QueueStatus.QUEUED),
            "running": ((start,), QueueStatus.QUEUED),
            "paused": (
                (start, (TaskCommand.PAUSE, user)),
                QueueStatus.COMPLETED,
            ),
            "waiting": (
                (
                    start,
                    (TaskCommand.WAIT, {**system, "wait_reason": WaitReason.USER}),
                ),
                QueueStatus.COMPLETED,
            ),
            "evaluating": (
                (start, (TaskCommand.BEGIN_EVALUATION, system)),
                QueueStatus.COMPLETED,
            ),
            "failed": ((start, (TaskCommand.FAIL, system)), QueueStatus.COMPLETED),
            "cancelled": ((start, (TaskCommand.CANCEL, user)), QueueStatus.COMPLETED),
            "retried": (
                (start, (TaskCommand.FAIL, system), (TaskCommand.RETRY, user)),
                QueueStatus.QUEUED,
            ),
            "restarted": (
                (start, (TaskCommand.CANCEL, user), (TaskCommand.RESTART, user)),
                QueueStatus.QUEUED,
            ),
            "resumed": (
                (start, (TaskCommand.PAUSE, user), (TaskCommand.RESUME, user)),
                QueueStatus.QUEUED,
            ),
        }
        for label, (commands, expected) in cases.items():
            with self.subTest(label):
                h = self.harness()
                task_id, entry = await self.claimed(h, commands)
                done = await h.queue.finish(entry.id, "w1", entry.claim_count)
                self.assertEqual(done.status, expected)
                if expected is QueueStatus.QUEUED:
                    # Given back: claimable, and the task's enqueue is not needed.
                    again = await h.queue.claim_next("w2")
                    self.assertEqual(again.task_id, task_id)
                    self.assertEqual(again.claim_count, entry.claim_count + 1)
                    await h.queue.complete(again.id, "w2", again.claim_count)
                else:
                    await h.queue.enqueue(task_id)  # the next enqueue works
                    await h.queue.cancel(task_id)

    async def test_finish_needs_the_lease(self):
        h = self.harness()
        _task_id, entry = await self.claimed(h)
        for worker, generation in (("w2", entry.claim_count), ("w1", 99)):
            with self.subTest(worker=worker), self.assertRaises(LeaseLostError):
                await h.queue.finish(entry.id, worker, generation)
        with self.assertRaises(LeaseLostError):
            await h.queue.finish(10**9, "w1", 1)

    async def test_a_command_in_flight_is_waited_for(self):
        # finish share-locks the task: a Retry that holds the task row commits
        # first and is seen (the entry is given back), never lost in between.
        h = self.harness()
        task_id, entry = await self.claimed(
            h, ((TaskCommand.START, {"actor": self.system}),)
        )
        await h.tasks.execute(task_id, TaskCommand.FAIL, actor=self.system)
        other = TaskService(self.new_database(), project_gate=ALWAYS_ACTIVE)

        async def retry_holding_the_row():
            async def slow(session, task_id, project_id):
                await asyncio.sleep(0.3)  # the row is locked meanwhile

            await other.execute(
                task_id, TaskCommand.RETRY, actor=self.user, in_transaction=slow
            )

        retry = asyncio.create_task(retry_holding_the_row())
        await asyncio.sleep(0.1)
        done = await h.queue.finish(entry.id, "w1", entry.claim_count)
        await retry
        self.assertEqual(done.status, QueueStatus.QUEUED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.QUEUED)


@requires_postgres
class ReplacementWhileTheEntryIsHeldTest(PostgresOrchestratorTestCase):
    """A Retry / Restart / Resume / Unblock that commits after the orchestrator's
    last look at the task and before it ends its hold on the entry: the caller's
    enqueue is refused, and the entry serves the task instead."""

    def interfere_before_finish(self, h, action):
        original = h.queue.finish
        calls = []

        async def finish(*args, **kwargs):
            if not calls:
                calls.append(1)
                await action()
            return await original(*args, **kwargs)

        h.queue.finish = finish
        return calls

    async def enqueue_refused(self, h, task_id):
        with self.assertRaises(TaskAlreadyQueuedError):
            await h.orchestrator.enqueue_task(task_id, preset="standard")

    async def assert_served(self, h, task_id, expected_state):
        (entry,) = await self.rows(
            "SELECT status FROM queue_entries WHERE task_id = :t AND"
            " status IN ('queued', 'claimed')",
            t=task_id,
        )
        self.assertEqual(entry["status"], "queued")
        self.assertEqual((await h.tasks.restore(task_id)).state, expected_state)

    async def test_a_retry_after_the_run_failed_the_task(self):
        runtime = FakeRuntime(
            "local", script={"a": [fail("TimeoutError", retryable=False), ok()]}
        )
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(h, make_plan(node("a")))

        async def retry():
            await h.tasks.execute(task_id, TaskCommand.RETRY, actor=self.user)
            await self.enqueue_refused(h, task_id)

        self.interfere_before_finish(h, retry)
        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_FAILED)
        await self.assert_served(h, task_id, TaskState.QUEUED)
        report = await h.orchestrator.run_once("w2")
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)

    async def test_a_restart_after_the_run_failed_the_task(self):
        runtime = FakeRuntime(
            "local", script={"a": [fail("TimeoutError", retryable=False), ok()]}
        )
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(h, make_plan(node("a")))

        async def restart():
            await h.tasks.execute(task_id, TaskCommand.RESTART, actor=self.user)
            await h.orchestrator.submit_plan(task_id, make_plan(node("a")))
            await self.enqueue_refused(h, task_id)

        self.interfere_before_finish(h, restart)
        await h.orchestrator.run_once("w1")

        await self.assert_served(h, task_id, TaskState.QUEUED)
        report = await h.orchestrator.run_once("w2")
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual((await h.tasks.restore(task_id)).attempt.number, 2)

    async def test_a_resume_after_the_run_quiesced_for_a_pause(self):
        runtime = FakeRuntime("local")
        runtime.gate("a")
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(h, make_plan(node("a"), node("b", "a")))

        async def resume():
            await h.tasks.execute(task_id, TaskCommand.RESUME, actor=self.user)
            await self.enqueue_refused(h, task_id)

        self.interfere_before_finish(h, resume)
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="a running")
        await h.tasks.execute(task_id, TaskCommand.PAUSE, actor=self.user)
        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll")
        await h.clock.advance(2.0)
        runtime.gates["a"].set()
        report = await asyncio.wait_for(run, 60)

        self.assertEqual(report.outcome, Out.PAUSED)
        await self.assert_served(h, task_id, TaskState.RUNNING)
        report = await h.orchestrator.run_once("w2")  # takes the run over
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(runtime.calls_of("b")), 1)

    async def test_an_unblock_after_the_run_waited_for_its_budget(self):
        runtime = FakeRuntime("local")
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(h, make_plan(node("a")))
        await self.owner_sql(
            "UPDATE budget_usages SET limit_value = 0 WHERE task_id = :t"
            " AND kind = 'steps'",
            t=task_id,
        )

        async def unblock():
            await self.owner_sql(
                "UPDATE budget_usages SET limit_value = 10 WHERE task_id = :t"
                " AND kind = 'steps'",
                t=task_id,
            )
            await h.tasks.execute(task_id, TaskCommand.UNBLOCK, actor=self.user)
            await self.enqueue_refused(h, task_id)

        self.interfere_before_finish(h, unblock)
        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.WAITING_FOR_USER)
        await self.assert_served(h, task_id, TaskState.RUNNING)
        report = await h.orchestrator.run_once("w2")
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)

    async def test_without_a_replacement_the_entry_is_completed(self):
        h = self.harness()
        task_id = await self.prepare(h, make_plan(node("a")))
        await h.orchestrator.run_once("w1")
        (entry,) = await self.rows("SELECT status FROM queue_entries")
        self.assertEqual(entry["status"], "completed")
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.EVALUATING)


@requires_postgres
class WaitingIsAGracefulStopTest(PostgresOrchestratorTestCase):
    async def start_with_a_running(self):
        runtime = FakeRuntime("local")
        runtime.gate("a")
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(h, make_plan(node("a"), node("b"), node("c", "a")))
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 2, message="a and b")
        return h, runtime, task_id, run

    async def wait_externally(self, h, task_id):
        await h.tasks.execute(
            task_id,
            TaskCommand.WAIT,
            actor=self.system,
            wait_reason=WaitReason.APPROVAL,
        )
        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll")
        await h.clock.advance(2.0)

    async def test_no_new_node_starts_while_the_task_waits(self):
        h, runtime, task_id, run = await self.start_with_a_running()
        await self.wait_externally(h, task_id)
        runtime.gates["a"].set()  # a finishes; c (after a) must not start
        report = await asyncio.wait_for(run, 60)

        self.assertEqual(report.outcome, Out.WAITING)
        self.assertEqual(runtime.calls_of("c"), [])
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.state, DagState.ACTIVE)
        self.assertEqual(dag.node("a").state.value, "succeeded")  # kept
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(
            (snapshot.state, snapshot.wait_reason),
            (TaskState.WAITING, WaitReason.APPROVAL),
        )
        (entry,) = await self.rows("SELECT status FROM queue_entries")
        self.assertEqual(entry["status"], "completed")
        self.assertIsNone(
            await self.scalar(
                "SELECT running_since FROM budget_usages WHERE kind = 'runtime_seconds'"
            )
        )

        # Unblocked and enqueued, the next worker finishes the DAG.
        await h.tasks.execute(task_id, TaskCommand.UNBLOCK, actor=self.user)
        await h.orchestrator.enqueue_task(task_id, preset="standard")
        report = await h.orchestrator.run_once("w2")
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(runtime.calls_of("c")), 1)
        self.assertEqual(len(runtime.calls_of("a")), 1)  # not redone

    async def test_an_unblock_while_the_nodes_finish_carries_on(self):
        h, runtime, task_id, run = await self.start_with_a_running()
        await self.wait_externally(h, task_id)
        await h.tasks.execute(task_id, TaskCommand.UNBLOCK, actor=self.user)
        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll")
        await h.clock.advance(2.0)
        runtime.gates["a"].set()
        report = await asyncio.wait_for(run, 60)
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(runtime.calls_of("c")), 1)

    async def test_a_task_waiting_during_planning_stops_before_the_nodes(self):
        gate = asyncio.Event()

        async def plan(assignment):
            await gate.wait()
            from paw_backend.orchestrator.result import NodeResult
            from paw_backend.orchestrator.runtime import NodeOutcome

            return NodeOutcome.succeeded(
                NodeResult("plan"), plan={"nodes": [node("a")]}
            )

        planner = FakeRuntime("local", script={"plan": plan})
        h = self.harness(runtimes={"local": planner})
        task_id = await self.prepare(h)
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(planner.assignments) == 1, message="the planner")
        await h.tasks.execute(
            task_id, TaskCommand.WAIT, actor=self.system, wait_reason=WaitReason.USER
        )
        await until(lambda: h.clock.waiting_for(2.0) >= 1, message="the poll")
        await h.clock.advance(2.0)
        gate.set()
        report = await asyncio.wait_for(run, 60)

        self.assertEqual(report.outcome, Out.WAITING)
        self.assertEqual(planner.calls_of("a"), [])
        self.assertIsNotNone(await self.store.get(task_id, 1))  # the plan is kept
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.WAITING)


@requires_postgres
class CancelledWithItsEntryTest(PostgresOrchestratorTestCase):
    async def test_the_dag_of_a_task_cancelled_with_its_entry_is_closed(self):
        # The stop of a deleted project cancels the task and its entry in one
        # transaction; the worker learns it from its heartbeat (lease lost) and,
        # as nobody will run that DAG again, closes it (fenced by its epoch).
        runtime = FakeRuntime("local")
        runtime.gate("a")
        # The poll of the task's state comes after the heartbeat: the lease is
        # found lost first.
        h = self.harness(runtimes={"local": runtime}, config={"poll_seconds": 60.0})
        task_id = await self.prepare(h, make_plan(node("a"), node("b", "a")))
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="a running")

        async def cancel_entry(session, tid, _project_id):
            await h.queue.cancel_in(session, tid)

        await h.tasks.execute(
            task_id, TaskCommand.CANCEL, actor=self.user, in_transaction=cancel_entry
        )
        await until(lambda: h.clock.waiting_for(15.0) >= 1, message="the heartbeat")
        await h.clock.advance(15.0)
        report = await asyncio.wait_for(run, 60)

        self.assertEqual(report.outcome, Out.LEASE_LOST)
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.state, DagState.CANCELLED)
        self.assertEqual({n.state.value for n in dag.nodes}, {"cancelled"})
        self.assertIsNone(
            await self.scalar(
                "SELECT running_since FROM budget_usages WHERE kind = 'runtime_seconds'"
            )
        )


class QueueDatabaseTest(unittest.TestCase):
    def test_the_queue_exposes_its_database(self):
        from .test_orchestrator_argument_validation import database

        db = database()
        self.assertIs(TaskQueue(db, project_gate=ALWAYS_ACTIVE).database, db)


if __name__ == "__main__":
    unittest.main()
