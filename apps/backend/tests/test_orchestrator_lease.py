"""Leases, take-overs and crashes (real PostgreSQL, separate connection pools).

Only the worker that holds the queue lease starts the runtime timer or writes the
DAG; a worker that lost its lease stops; a crashed worker's nodes are picked up
by the next lease holder, and whatever the crashed worker reports later is refused
at every level (queue, runtime timer, DAG).
"""

import asyncio
import unittest
from datetime import UTC, datetime

from paw_backend.orchestrator.domain import AttemptState, DagState, RunOutcome
from paw_backend.orchestrator.errors import StaleDagEpochError
from paw_backend.orchestrator.result import NodeResult
from paw_backend.tasks import TaskCommand, TaskState
from paw_backend.tasks.queueing import (
    LeaseLostError,
    StaleRuntimeSessionError,
    TaskQueue,
)

from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    SpyBudget,
    diamond,
    make_plan,
    node,
    requires_postgres,
    until,
)

Out = RunOutcome
LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


def final_e_summary(dag) -> str:
    return dag.node("e").result.summary


class FlakyQueue(TaskQueue):
    """A queue whose heartbeat can be made to fail with a database-like error."""

    broken = False

    async def heartbeat(self, *args, **kwargs):
        if self.broken:
            raise ConnectionError("the database is gone")
        return await super().heartbeat(*args, **kwargs)


@requires_postgres
class LeaseTest(PostgresOrchestratorTestCase):
    async def expire_the_lease(self) -> None:
        await self.owner_sql(
            "UPDATE queue_entries SET claimed_at = now() - interval '10 seconds',"
            " lease_expires_at = now() - interval '5 seconds' WHERE status = 'claimed'"
        )

    async def test_a_worker_whose_lease_has_expired_does_nothing(self):
        queue = TaskQueue(self.database, allow_explicit_now=True)
        spy = SpyBudget(self.database)
        runtime = FakeRuntime("local")
        h = self.harness(queue=queue, budget=spy, runtimes={"local": runtime})
        task_id = await self.prepare(h, diamond())
        entry = await queue.claim_next("w1", LONG_AGO)  # a lease that ended in 2020

        report = await h.orchestrator.run_entry(entry, "w1")

        self.assertEqual(report.outcome, Out.LEASE_LOST)
        # It never started the task, the runtime timer, or a node.
        self.assertEqual(spy.started, [])
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.QUEUED)
        self.assertEqual(runtime.assignments, [])
        self.assertEqual((await self.store.get(task_id, 1)).epoch, 0)

    async def test_two_workers_racing_for_one_entry_run_it_once(self):
        runtime = FakeRuntime("local")
        first = self.harness(runtimes={"local": runtime})
        second = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(first, diamond())

        reports = await asyncio.gather(
            first.orchestrator.run_once("w1"), second.orchestrator.run_once("w2")
        )

        self.assertEqual(
            sorted(r.outcome.value for r in reports), ["dag_succeeded", "idle"]
        )
        self.assertEqual(sorted(a.node_key for a in runtime.assignments), list("abcde"))
        (entry,) = await self.rows("SELECT claim_count, status FROM queue_entries")
        self.assertEqual((entry["claim_count"], entry["status"]), (1, "completed"))
        self.assertEqual((await self.store.get(task_id, 1)).epoch, 1)

    async def test_the_lease_is_extended_while_nodes_run(self):
        runtime = FakeRuntime("local")
        runtime.gate("only")
        h = self.harness(runtimes={"local": runtime})
        await self.prepare(h, make_plan(node("only")))
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="the node")
        before = await self.scalar("SELECT lease_expires_at FROM queue_entries")

        await h.clock.advance(20.0)  # the heartbeat interval: a third of the lease
        after = before
        for _ in range(500):
            after = await self.scalar("SELECT lease_expires_at FROM queue_entries")
            if after > before:
                break
            await asyncio.sleep(0.02)
        self.assertGreater(after, before)
        runtime.gates["only"].set()
        self.assertEqual((await asyncio.wait_for(run, 120)).outcome, Out.DAG_SUCCEEDED)

    async def test_a_worker_that_loses_its_lease_stops_and_the_next_one_takes_over(
        self,
    ):
        spy_a = SpyBudget(self.database)
        runtime_a = FakeRuntime("local")
        runtime_a.gate("a")
        a = self.harness(runtimes={"local": runtime_a}, budget=spy_a)
        task_id = await self.prepare(a, diamond())
        run_a = asyncio.create_task(a.orchestrator.run_once("w1"))
        await until(lambda: len(runtime_a.assignments) == 2, message="a and e")
        dag = await self.store.get(task_id, 1)

        # w1 is slow: its lease ends and w2 claims the entry.
        await self.expire_the_lease()
        runtime_b = FakeRuntime("local")
        spy_b = SpyBudget(self.database)
        b = self.harness(runtimes={"local": runtime_b}, budget=spy_b)
        run_b = asyncio.create_task(b.orchestrator.run_once("w2"))
        report_b = await asyncio.wait_for(run_b, 120)

        # w2 took the DAG over (epoch 2), re-ran what w1 had left running, and won.
        self.assertEqual(report_b.outcome, Out.DAG_SUCCEEDED)
        # e had finished under w1 and is kept; a was running and runs again.
        self.assertEqual(
            sorted(x.node_key for x in runtime_b.assignments), list("abcd")
        )
        self.assertEqual(sorted(x.node_key for x in runtime_a.assignments), ["a", "e"])
        self.assertEqual(
            final_e_summary(await self.store.get(task_id, 1)), "e by local"
        )
        final = await self.store.get(task_id, 1)
        self.assertEqual(
            (final.epoch, final.owner, final.state), (2, "w2", DagState.SUCCEEDED)
        )
        self.assertEqual(
            [(x.number, x.state) for x in await self.store.attempts(dag.id, "a")],
            [(1, AttemptState.INTERRUPTED), (2, AttemptState.SUCCEEDED)],
        )
        self.assertEqual(
            (await self.rows("SELECT claim_count FROM queue_entries"))[0][
                "claim_count"
            ],
            2,
        )

        # w1 notices at its next heartbeat and stops without writing anything.
        await until(
            lambda: a.clock.waiting_for(20.0) >= 1, message="w1's heartbeat timer"
        )
        await a.clock.advance(20.0)
        report_a = await asyncio.wait_for(run_a, 120)
        self.assertEqual(report_a.outcome, Out.LEASE_LOST)
        self.assertEqual(await self.store.get(task_id, 1), final)
        self.assertEqual((await a.tasks.restore(task_id)).state, TaskState.EVALUATING)
        # Each worker started the runtime timer once and w1's stop did not touch w2's.
        self.assertEqual((len(spy_a.started), len(spy_b.started)), (1, 1))
        generation = await self.scalar(
            "SELECT runtime_generation FROM budget_usages"
            " WHERE kind = 'runtime_seconds'"
        )
        self.assertEqual(generation, 2)

    async def test_a_crash_mid_node_is_recovered_and_the_zombie_is_refused(self):
        h = self.harness()
        task_id = await self.prepare(h, diamond())
        # What worker "crashed" did before it died: claimed, started the task and
        # the timer, took the DAG over and started node a.
        entry = await h.queue.claim_next("crashed")
        event = await h.tasks.execute(task_id, TaskCommand.START, actor=self.system)
        generation = await h.budget.start_runtime(task_id)
        dag = await self.store.get(task_id, 1)
        taken = await self.store.acquire(dag.id, "crashed", event.run)
        started = await self.store.start_node(dag.id, taken.epoch, "a", max_attempts=6)
        await self.expire_the_lease()

        report = await h.orchestrator.run_once("rescuer")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        attempts = await self.store.attempts(dag.id, "a")
        self.assertEqual(
            [(x.number, x.state, x.epoch) for x in attempts],
            [(1, AttemptState.INTERRUPTED, 1), (2, AttemptState.SUCCEEDED, 2)],
        )
        self.assertEqual(
            sorted(a.node_key for a in h.runtimes["local"].assignments), list("abcde")
        )
        # The zombie wakes up and reports: refused by the queue, the runtime timer
        # and the DAG, and nothing changes.
        final = await self.store.get(task_id, 1)
        with self.assertRaises(LeaseLostError):
            await h.queue.complete(entry.id, "crashed", entry.claim_count)
        with self.assertRaises(StaleRuntimeSessionError):
            await h.budget.stop_runtime(task_id, generation)
        with self.assertRaises(StaleDagEpochError):
            await self.store.complete_node(
                dag.id, 1, "a", started.number, NodeResult("zombie")
            )
        self.assertEqual(await self.store.get(task_id, 1), final)

    async def test_heartbeats_that_keep_failing_count_as_a_lost_lease(self):
        queue = FlakyQueue(self.database)
        runtime = FakeRuntime("local")
        runtime.gate("only")
        h = self.harness(queue=queue, runtimes={"local": runtime})
        task_id = await self.prepare(h, make_plan(node("only")))
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="the node")

        queue.broken = True
        for _ in range(3):
            await until(
                lambda: h.clock.waiting_for(20.0) >= 1, message="a heartbeat timer"
            )
            await h.clock.advance(20.0)
        report = await asyncio.wait_for(run, 120)

        self.assertEqual(report.outcome, Out.LEASE_LOST)
        self.assertEqual(
            await self.states_of(task_id), {"only": "running"}
        )  # untouched


if __name__ == "__main__":
    unittest.main()
