"""A worker that is stopped leaves nothing behind (real PostgreSQL).

Cancelling ``run_once`` (a shutdown) hands the queue entry back and makes the nodes
that were running ready for the next worker; ``serve`` runs entries until it is
stopped, waits on the injected clock when there is nothing to do, survives an entry
that raises, and ends at once when asked.
"""

import asyncio
import unittest

from paw_backend.orchestrator.domain import AttemptState, RunOutcome
from paw_backend.tasks import TaskState

from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    SpyBudget,
    make_plan,
    node,
    requires_postgres,
    until,
)

Out = RunOutcome


@requires_postgres
class ShutdownTest(PostgresOrchestratorTestCase):
    async def test_cancelling_a_run_releases_the_entry_and_readies_the_nodes(self):
        spy = SpyBudget(self.database)
        runtime = FakeRuntime("local")
        runtime.gate("a")
        h = self.harness(runtimes={"local": runtime}, budget=spy)
        task_id = await self.prepare(h, make_plan(node("a"), node("b", "a")))
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="the node")

        run.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await run

        # The entry is back in the queue for the next worker, the node is ready
        # again with its attempt marked interrupted, the timer is stopped.
        (entry,) = await self.rows("SELECT status, claim_count FROM queue_entries")
        self.assertEqual((entry["status"], entry["claim_count"]), ("queued", 1))
        self.assertEqual(await self.states_of(task_id), {"a": "ready", "b": "pending"})
        dag = await self.store.get(task_id, 1)
        (attempt,) = await self.store.attempts(dag.id, "a")
        self.assertEqual(attempt.state, AttemptState.INTERRUPTED)
        self.assertEqual(len(spy.stopped), 1)
        row = (
            await self.rows(
                "SELECT running_since FROM budget_usages WHERE kind = 'runtime_seconds'"
            )
        )[0]
        self.assertIsNone(row["running_since"])
        # The task is still running (a worker took it over): the next one finishes it.
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.RUNNING)
        runtime.gates["a"].set()
        other = self.harness(runtimes={"local": FakeRuntime("local")})
        report = await other.orchestrator.run_once("w2")
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual((await self.store.get(task_id, 1)).epoch, 2)

    async def test_serve_runs_entries_waits_on_the_clock_and_stops_when_asked(self):
        h = self.harness()
        stop = asyncio.Event()
        served = asyncio.create_task(h.orchestrator.serve("w1", stop, idle_seconds=5.0))
        await until(lambda: h.clock.sleeping >= 1, message="the idle wait")

        task_id = await self.prepare(h, make_plan(node("a")))
        await h.clock.advance(5.1)  # the idle wait ends: the entry is claimed and run
        for _ in range(500):
            if (await h.tasks.restore(task_id)).state is TaskState.EVALUATING:
                break
            await asyncio.sleep(0.02)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.EVALUATING)

        stop.set()
        await asyncio.wait_for(served, 120)
        self.assertEqual(h.clock.sleeping, 0)  # no timer is left behind

    async def test_serve_survives_an_entry_that_raises(self):
        h = self.harness()
        stop = asyncio.Event()
        calls = []
        original = h.queue.claim_next

        async def broken(worker_id, now=None):
            calls.append(worker_id)
            if len(calls) == 1:
                raise ConnectionError("the database is gone: secret detail")
            return await original(worker_id, now)

        h.queue.claim_next = broken
        with self.assertLogs("paw_backend.orchestrator.orchestrator", "ERROR") as logs:
            served = asyncio.create_task(
                h.orchestrator.serve("w1", stop, idle_seconds=1.0)
            )
            await until(
                lambda: h.clock.sleeping >= 1, message="the wait after the error"
            )
            await h.clock.advance(1.1)
            await until(lambda: len(calls) >= 2, message="the next attempt")
            stop.set()
            await asyncio.wait_for(served, 120)

        self.assertIn("ConnectionError", "\n".join(logs.output))
        self.assertNotIn("secret detail", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
