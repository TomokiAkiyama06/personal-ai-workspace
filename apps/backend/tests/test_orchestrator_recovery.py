"""A running task always has queued work, whatever state its DAG is in (PAW-034).

A worker that ends a run leaves the task in a state that somebody drives on: the
queue entry it completes is the last thing it does, so before it does, the task
must have left ``running`` (evaluating, failed, waiting, paused) or the entry must
stay claimed (its lease will expire and the next worker takes the run over).

* A DAG that already SUCCEEDED (the evaluation failed, the task was retried) goes
  straight back to evaluation: one DAG per task attempt, and its results stand
  (Decision 0021, section 6). Redoing the work is a Restart.
* A DAG that already ended in the same run (a crash after the DAG was closed and
  before the task command) is finished the same way it would have been.
* An unexpected state or error fails the task safely; when even that cannot be
  written, the entry is left claimed.
"""

import unittest

from paw_backend.orchestrator import RunOutcome
from paw_backend.orchestrator.domain import DagState
from paw_backend.orchestrator.orchestrator import REASON_INTERNAL
from paw_backend.tasks import TaskCommand, TaskError, TaskRun, TaskState

from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    fail,
    make_plan,
    node,
    requires_postgres,
)

Out = RunOutcome
SECRET = "hunter2-" + "not-a-real-secret"


class BrokenStep(Exception):
    """A failure of the orchestrator's own code path (not of a node)."""


@requires_postgres
class RetryAfterASucceededDagTest(PostgresOrchestratorTestCase):
    async def cycle(self, h, task_id, worker):
        """The Evaluator fails the task, a human retries it, a worker runs it."""
        await h.tasks.execute(task_id, TaskCommand.FAIL, actor=self.system)
        await h.tasks.execute(task_id, TaskCommand.RETRY, actor=self.user)
        await h.queue.enqueue(task_id)
        return await h.orchestrator.run_once(worker)

    async def test_a_retry_after_a_failed_evaluation_goes_back_to_evaluation(self):
        runtime = FakeRuntime("local")
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(h, make_plan(node("a"), node("b", "a")))
        first = await h.orchestrator.run_once("w1")
        self.assertEqual(first.outcome, Out.DAG_SUCCEEDED)
        steps = await self.scalar(
            "SELECT consumed FROM budget_usages WHERE kind = 'steps'"
        )

        report = await self.cycle(h, task_id, "w2")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(report.dag_state, DagState.SUCCEEDED)
        snapshot = await h.tasks.restore(task_id, log_limit=50)
        self.assertIn(
            "The DAG had already succeeded",
            [log.message for log in snapshot.recent_logs],
        )
        self.assertEqual(
            (snapshot.state, snapshot.run), (TaskState.EVALUATING, TaskRun(1, 1))
        )
        # The results stand: no node ran again, no step was charged, the DAG is the
        # same one (and was taken over: epoch 2).
        self.assertEqual(sorted(a.node_key for a in runtime.assignments), ["a", "b"])
        self.assertEqual(
            await self.scalar(
                "SELECT consumed FROM budget_usages WHERE kind = 'steps'"
            ),
            steps,
        )
        dag = await self.store.get(task_id, 1)
        self.assertEqual(
            (dag.state, dag.epoch, dag.task_retry_count), (DagState.SUCCEEDED, 2, 1)
        )
        # The entry of the second run is completed, and the task has left `running`.
        entries = await self.rows("SELECT status FROM queue_entries ORDER BY id")
        self.assertEqual([e["status"] for e in entries], ["completed", "completed"])

    async def test_it_can_happen_again_and_again(self):
        runtime = FakeRuntime("local")
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(h, make_plan(node("a")))
        await h.orchestrator.run_once("w1")

        for round_ in range(1, 4):
            with self.subTest(round=round_):
                report = await self.cycle(h, task_id, f"w{round_ + 1}")
                self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
                snapshot = await h.tasks.restore(task_id)
                self.assertEqual(
                    (snapshot.state, snapshot.retry_count),
                    (TaskState.EVALUATING, round_),
                )
        self.assertEqual(len(runtime.assignments), 1)

    async def test_a_restart_after_a_failed_evaluation_does_the_work_again(self):
        runtime = FakeRuntime("local")
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(h, make_plan(node("a")))
        await h.orchestrator.run_once("w1")
        await h.tasks.execute(task_id, TaskCommand.FAIL, actor=self.system)
        await h.tasks.execute(task_id, TaskCommand.RESTART, actor=self.user)
        await h.queue.enqueue(task_id)
        # The new attempt has no DAG yet: the planner is asked (a plan is stored
        # for it), and the nodes run again.
        await h.orchestrator.submit_plan(task_id, make_plan(node("a")))

        report = await h.orchestrator.run_once("w2")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(runtime.calls_of("a")), 2)
        self.assertEqual((await self.store.get(task_id, 2)).state, DagState.SUCCEEDED)


@requires_postgres
class ADagThatAlreadyEndedTest(PostgresOrchestratorTestCase):
    async def crashed_after_closing_the_dag(self, plan_nodes, fail_node=False):
        """A task whose worker died right after the DAG was closed."""
        h = self.harness()
        task_id = await self.prepare(h, make_plan(*plan_nodes))
        entry = await h.queue.claim_next("dead")
        event = await h.tasks.execute(task_id, TaskCommand.START, actor=self.system)
        dag = await self.store.get(task_id, 1)
        taken = await self.store.acquire(dag.id, "dead", event.run)
        from paw_backend.orchestrator.domain import NextStep

        for key in [n["key"] for n in plan_nodes]:
            attempt = await self.store.start_node(
                dag.id, taken.epoch, key, max_attempts=6
            )
            if fail_node:
                await self.store.fail_node(
                    dag.id,
                    taken.epoch,
                    key,
                    attempt.number,
                    error_class="E",
                    signature="a" * 64,
                    step=NextStep.GIVE_UP,
                )
            else:
                from paw_backend.orchestrator.result import NodeResult

                await self.store.complete_node(
                    dag.id, taken.epoch, key, attempt.number, NodeResult("done")
                )
        closed = await self.store.finalize(dag.id, taken.epoch)
        await self.owner_sql(
            "UPDATE queue_entries SET claimed_at = now() - interval '10 seconds',"
            " lease_expires_at = now() - interval '5 seconds' WHERE status = 'claimed'"
        )
        return h, task_id, closed, entry

    async def test_a_succeeded_dag_of_the_same_run_goes_to_evaluation(self):
        h, task_id, closed, _ = await self.crashed_after_closing_the_dag([node("a")])
        self.assertEqual(closed.state, DagState.SUCCEEDED)

        report = await h.orchestrator.run_once("rescuer")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.EVALUATING)

    async def test_a_failed_dag_of_the_same_run_fails_the_task(self):
        h, task_id, closed, _ = await self.crashed_after_closing_the_dag(
            [node("a")], fail_node=True
        )
        self.assertEqual(closed.state, DagState.FAILED)

        report = await h.orchestrator.run_once("rescuer")

        self.assertEqual(report.outcome, Out.DAG_FAILED)
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.FAILED)
        self.assertEqual(snapshot.last_event.reason, "A required node did not succeed")
        # It is not opened again by the same run: only a Retry does that.
        self.assertEqual((await self.store.get(task_id, 1)).state, DagState.FAILED)

    async def test_a_cancelled_dag_of_a_running_task_fails_the_task_safely(self):
        h, task_id, _, _ = await self.crashed_after_closing_the_dag([node("a")])
        dag = await self.store.get(task_id, 1)
        await self.owner_sql("UPDATE agent_dags SET state = 'cancelled'")

        report = await h.orchestrator.run_once("rescuer")

        self.assertEqual(report.outcome, Out.ERROR)
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.FAILED)
        self.assertEqual(snapshot.last_event.reason, REASON_INTERNAL)
        (entry,) = await self.rows("SELECT status FROM queue_entries")
        self.assertEqual(entry["status"], "completed")
        self.assertEqual((await self.store.get(task_id, 1)).id, dag.id)


@requires_postgres
class UnexpectedErrorsTest(PostgresOrchestratorTestCase):
    async def test_an_unexpected_error_fails_the_task_and_leaks_nothing(self):
        h = self.harness()
        task_id = await self.prepare(h, make_plan(node("a")))

        async def broken(*args, **kwargs):
            raise BrokenStep(SECRET)

        h.store.finalize = broken

        with self.assertLogs("paw_backend.orchestrator.orchestrator", "ERROR") as logs:
            report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.ERROR)
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.FAILED)
        self.assertEqual(snapshot.last_event.reason, REASON_INTERNAL)
        (entry,) = await self.rows("SELECT status FROM queue_entries")
        self.assertEqual(entry["status"], "completed")
        # The type is logged, the message never.
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("AdapterError", "\n".join(logs.output))
        for table in (
            "task_events",
            "task_logs",
            "agent_dag_nodes",
            "agent_dag_node_attempts",
        ):
            dump = await self.scalar(
                f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t"
            )
            self.assertNotIn(SECRET, dump, table)
        # A human can retry it: it does not stay in a limbo.
        await h.tasks.execute(task_id, TaskCommand.RETRY, actor=self.user)

    async def test_when_even_the_failure_cannot_be_written_the_entry_stays_claimed(
        self,
    ):
        h = self.harness()
        task_id = await self.prepare(h, make_plan(node("a")))
        original = h.tasks.execute

        async def broken_store(*args, **kwargs):
            raise BrokenStep(SECRET)

        h.store.finalize = broken_store

        async def refusing(tid, command, **kwargs):
            if command is TaskCommand.FAIL:
                raise ConnectionError(SECRET)  # the database is unreachable
            return await original(tid, command, **kwargs)

        h.tasks.execute = refusing

        with self.assertLogs("paw_backend.orchestrator.orchestrator", "ERROR"):
            report = await h.orchestrator.run_once("w1")

        # The task still runs, and the entry is still claimed: its lease will
        # expire and another worker takes the run over. Never "running, no work".
        self.assertEqual(report.outcome, Out.ERROR)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.RUNNING)
        (entry,) = await self.rows("SELECT status FROM queue_entries")
        self.assertEqual(entry["status"], "claimed")
        await self.owner_sql(
            "UPDATE queue_entries SET claimed_at = now() - interval '10 seconds',"
            " lease_expires_at = now() - interval '5 seconds'"
        )
        rescuer = self.harness()
        report = await rescuer.orchestrator.run_once("w2")
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(
            (await rescuer.tasks.restore(task_id)).state, TaskState.EVALUATING
        )

    async def test_a_refused_start_returns_the_entry_to_the_queue(self):
        h = self.harness()
        task_id = await self.prepare(h, make_plan(node("a")))
        original = h.tasks.execute

        class NotActive(TaskError):
            """Stands in for the Project state gate's refusal (issue #83)."""

            code = "project_not_active"

        async def gated(tid, command, **kwargs):
            if command is TaskCommand.START:
                raise NotActive("the project is not active")
            return await original(tid, command, **kwargs)

        h.tasks.execute = gated

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.SKIPPED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.QUEUED)
        (entry,) = await self.rows("SELECT status, claim_count FROM queue_entries")
        # Back in the queue (claimable again once the project is Active): not
        # completed, which would leave a queued task no worker ever claims.
        self.assertEqual((entry["status"], entry["claim_count"]), ("queued", 1))
        h.tasks.execute = original
        report = await h.orchestrator.run_once("w2")
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)

    async def test_the_run_of_a_node_that_fails_for_good_still_ends_the_task(self):
        runtime = FakeRuntime(
            "local", script={"a": fail("Fatal", "x", retryable=False)}
        )
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(h, make_plan(node("a")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_FAILED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.FAILED)


if __name__ == "__main__":
    unittest.main()
