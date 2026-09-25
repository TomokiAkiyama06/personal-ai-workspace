"""What the orchestrator does when a node fails: retry, alternative, escalation.

Real PostgreSQL and the real loop detector and budget; scripted agents; a manual
clock. The rules are Decision 0007's (budget before loop, ``TRY_ALTERNATIVE`` then
``ESCALATE``) applied per node, and the requirements' failure isolation.
"""

import asyncio
import unittest

from paw_backend.orchestrator.domain import AttemptState, DagState, RunOutcome
from paw_backend.tasks import TaskState, WaitReason
from paw_backend.tasks.queueing import BudgetPreset

from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
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
SECRET = "hunter2-" + "not-a-real-secret"


class AdapterProblem(Exception):
    """An exception class of an adapter: its name must not reach the records."""


def runtimes(**scripts):
    timeline = []
    return {
        label: FakeRuntime(label, script=script, timeline=timeline)
        for label, script in scripts.items()
    }


@requires_postgres
class RetryTest(PostgresOrchestratorTestCase):
    async def test_a_node_that_fails_once_is_retried_and_the_run_succeeds(self):
        rt = runtimes(
            local={"a": [fail("Flaky", "timeout after 3s"), ok("second try")]}
        )
        h = self.harness(runtimes=rt)
        task_id = await self.prepare(h, make_plan(node("a"), node("b", "a")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node("a").result.summary, "second try")
        self.assertEqual(
            (dag.node("a").attempt_count, dag.node("a").rung_attempts), (2, 2)
        )
        attempts = await self.store.attempts(dag.id, "a")
        self.assertEqual(
            [(a.number, a.state, a.error_class) for a in attempts],
            [(1, AttemptState.FAILED, "Flaky"), (2, AttemptState.SUCCEEDED, None)],
        )
        # One retry was charged to the task's budget; a step per start (3 starts).
        usage = {u.kind.value: u.consumed for u in await h.budget.usage(task_id)}
        self.assertEqual((usage["retries"], usage["steps"]), (1, 3))
        # The failure was recorded for loop detection under the node's key.
        (record,) = await h.loops.history(task_id)
        self.assertEqual(record.approach, 0)

    async def test_a_retry_waits_for_the_backoff_and_it_doubles(self):
        rt = runtimes(local={"a": [fail(), fail(), ok()]})
        h = self.harness(runtimes=rt, config={"retry_backoff_seconds": 5.0})
        await self.prepare(h, make_plan(node("a")))
        runtime = rt["local"]

        def backing_off(attempts: int, low: float, high: float) -> bool:
            # The wait for the back-off exists (its length is computed from the
            # failure, so it is found by its range) after the attempt failed.
            return len(runtime.assignments) == attempts and (
                h.clock.waiting_between(low, high) == 1
            )

        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: backing_off(1, 4.9, 5.1), message="the first back-off")
        await h.clock.advance(4.9)
        await quiet()
        self.assertEqual(len(runtime.assignments), 1)  # waiting, not retrying yet
        await h.clock.advance(0.2)
        await until(lambda: backing_off(2, 9.9, 10.1), message="the second back-off")
        # The second wait is twice as long (10 s).
        await h.clock.advance(9.0)
        await quiet()
        self.assertEqual(len(runtime.assignments), 2)
        await h.clock.advance(1.5)
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(runtime.assignments), 3)

    async def test_a_runtime_that_raises_or_times_out_or_answers_nonsense_is_retried(
        self,
    ):
        async def nonsense(_assignment):
            return None  # not a NodeOutcome

        async def slow(_assignment):
            await asyncio.Event().wait()

        rt = runtimes(
            local={
                "a": [RuntimeError(SECRET), ok()],
                "b": [AdapterProblem(SECRET), ok()],
                "c": [nonsense, ok()],
                "d": [slow, ok()],
            }
        )
        h = self.harness(runtimes=rt, config={"node_timeout_seconds": 30.0})
        task_id = await self.prepare(h, make_plan(*(node(k) for k in "abcd")))

        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(rt["local"].calls_of("d")) == 1, message="d to hang")
        await h.clock.advance(31.0)  # d's attempt exceeds its 30 s limit
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        dag = await self.store.get(task_id, 1)
        first_errors = {
            key: (await self.store.attempts(dag.id, key))[0].error_class
            for key in "abcd"
        }
        self.assertEqual(
            first_errors,
            {
                "a": "RuntimeError",
                "b": "AdapterError",  # a foreign class is never named
                "c": "InvalidNodeOutcome",
                "d": "NodeTimeout",
            },
        )
        # No text of any failure was stored anywhere.
        for table in (
            "agent_dags",
            "agent_dag_nodes",
            "agent_dag_node_attempts",
            "loop_failure_signatures",
            "task_logs",
            "task_events",
        ):
            dump = await self.scalar(
                f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t"
            )
            self.assertNotIn(SECRET, dump, table)

    async def test_failure_texts_are_formatted_before_they_reach_loop_detection(self):
        # A lone surrogate would be refused by record_failure (and the failure would
        # never count towards a loop) unless the worker formats the text first.
        rt = runtimes(local={"a": [fail("Dirty", "bad \ud800 text \x00"), ok()]})
        h = self.harness(runtimes=rt)
        task_id = await self.prepare(h, make_plan(node("a")))

        await h.orchestrator.run_once("w1")

        self.assertEqual(len(await h.loops.history(task_id)), 1)

    async def test_the_error_class_a_runtime_names_is_reduced_to_a_safe_name(self):
        rt = runtimes(local={"a": [fail("Bad Class/Name: \u00e9" + "x" * 150), ok()]})
        h = self.harness(runtimes=rt)
        task_id = await self.prepare(h, make_plan(node("a")))

        await h.orchestrator.run_once("w1")

        dag = await self.store.get(task_id, 1)
        (first, _second) = await self.store.attempts(dag.id, "a")
        self.assertEqual(first.error_class, "Bad_Class_Name___" + "x" * 83)
        self.assertEqual(len(first.error_class), 100)
        self.assertEqual(len(await h.loops.history(task_id)), 1)


@requires_postgres
class EscalationTest(PostgresOrchestratorTestCase):
    async def test_a_node_that_keeps_failing_climbs_the_whole_ladder_then_waits(self):
        always = fail("Stuck", "the same failure 42")
        rt = runtimes(local={"a": always}, codex={"a": always}, claude={"a": always})
        h = self.harness(runtimes=rt, ladder=("local", "codex", "claude"))
        # Long: the Standard preset allows 10 retries, the whole ladder takes 11.
        task_id = await self.prepare(
            h,
            make_plan(node("a"), node("b", "a"), node("free")),
            preset=BudgetPreset.LONG,
        )

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.WAITING_FOR_USER)
        seen = [
            (a.agent, a.approach)
            for runtime in rt.values()
            for a in runtime.calls_of("a")
        ]
        self.assertEqual(
            sorted(seen),
            sorted(
                [("local", 0)] * 3
                + [("local", 1)] * 3
                + [("codex", 2)] * 3
                + [("claude", 3)] * 3
            ),
        )
        # In order: 3 x local/0 (retry, retry, then an alternative), 3 x local/1
        # (then the escalation), and so on.
        order = [
            (a.agent, a.approach, a.attempt)
            for a in sorted(
                (a for r in rt.values() for a in r.calls_of("a")),
                key=lambda a: a.attempt,
            )
        ]
        self.assertEqual([n for _, _, n in order], list(range(1, 13)))
        self.assertEqual(
            [(agent, approach) for agent, approach, _ in order],
            [("local", 0)] * 3
            + [("local", 1)] * 3
            + [("codex", 2)] * 3
            + [("claude", 3)] * 3,
        )
        # The task waits for a human; the node is held, its dependent still waits,
        # and the independent node finished.
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(
            (snapshot.state, snapshot.wait_reason), (TaskState.WAITING, WaitReason.USER)
        )
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.state, DagState.ACTIVE)
        self.assertEqual(
            {n.key: n.state.value for n in dag.nodes},
            {"a": "ready", "b": "pending", "free": "succeeded"},
        )
        self.assertEqual((dag.node("a").agent_index, dag.node("a").approach), (2, 3))
        # The queue entry was completed; a human unblocking re-queues the task.
        (entry,) = await self.rows("SELECT status FROM queue_entries")
        self.assertEqual(entry["status"], "completed")

    async def test_an_escalated_node_can_succeed_on_a_stronger_agent(self):
        always = fail("Stuck", "the same failure")
        rt = runtimes(local={"a": always}, codex={"a": ok("fixed by codex")})
        h = self.harness(runtimes=rt, ladder=("local", "codex"))
        task_id = await self.prepare(h, make_plan(node("a")), preset=BudgetPreset.LONG)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node("a").result.summary, "fixed by codex")
        self.assertEqual(dag.node("a").agent_index, 1)
        self.assertEqual(len(rt["local"].calls_of("a")), 6)
        self.assertEqual(len(rt["codex"].calls_of("a")), 1)
        # The new rung starts its own attempt count.
        self.assertEqual(dag.node("a").rung_attempts, 1)
        self.assertEqual(dag.node("a").attempt_count, 7)

    async def test_different_failures_never_look_like_a_loop(self):
        # Ten different failures: no alternative, no escalation, just retries until
        # the attempts of the rung are used up, and then the node fails.
        failures = [
            fail("E", f"failure number {'x' * i}-{chr(97 + i)}") for i in range(9)
        ]
        rt = runtimes(local={"a": failures}, codex={})
        h = self.harness(
            runtimes=rt, ladder=("local", "codex"), config={"max_attempts_per_rung": 4}
        )
        task_id = await self.prepare(h, make_plan(node("a"), node("b", "a")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_FAILED)
        self.assertEqual({a.approach for a in rt["local"].calls_of("a")}, {0})
        self.assertEqual(len(rt["local"].calls_of("a")), 4)  # the cap of the rung
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node("a").state.value, "failed")
        # The fourth failure itself gave the node up (no fifth retry was charged
        # and queued): the node failed with the class of that failure.
        self.assertEqual(dag.node("a").error_class, "E")
        usage = {u.kind.value: u.consumed for u in await h.budget.usage(task_id)}
        self.assertEqual(usage["retries"], 3)
        self.assertEqual(dag.node("b").state.value, "blocked")


@requires_postgres
class IsolationTest(PostgresOrchestratorTestCase):
    async def test_a_failed_node_blocks_only_its_dependents(self):
        rt = runtimes(local={"b": fail("Fatal", "no way", retryable=False)})
        h = self.harness(runtimes=rt)
        task_id = await self.prepare(h, diamond())

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_FAILED)
        self.assertEqual(report.dag_state, DagState.FAILED)
        self.assertEqual(
            await self.states_of(task_id),
            {
                "a": "succeeded",
                "b": "failed",
                "c": "succeeded",
                "d": "blocked",
                "e": "succeeded",
            },
        )
        # The blocked node never ran; the independent ones ran once each.
        self.assertEqual(len(rt["local"].calls_of("d")), 0)
        for key in "ace":
            self.assertEqual(len(rt["local"].calls_of(key)), 1)
        # b was not retried (it said so), and the task failed only now.
        self.assertEqual(len(rt["local"].calls_of("b")), 1)
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.FAILED)
        self.assertEqual(snapshot.last_event.reason, "A required node did not succeed")

    async def test_an_optional_node_that_fails_does_not_fail_the_task(self):
        rt = runtimes(local={"opt": fail("Meh", "skip it", retryable=False)})
        h = self.harness(runtimes=rt)
        task_id = await self.prepare(
            h,
            make_plan(
                node("a"), node("opt", required=False), node("z", "opt", required=False)
            ),
        )

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(
            await self.states_of(task_id),
            {"a": "succeeded", "opt": "failed", "z": "blocked"},
        )
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.EVALUATING)

    async def test_only_the_decomposition_may_propose_nodes(self):
        from paw_backend.orchestrator.result import NodeResult
        from paw_backend.orchestrator.runtime import NodeOutcome

        sneaky = NodeOutcome.succeeded(
            NodeResult("done"), plan={"nodes": [node("more")]}
        )
        rt = runtimes(local={"a": sneaky})
        h = self.harness(runtimes=rt)
        task_id = await self.prepare(h, make_plan(node("a")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_FAILED)
        self.assertEqual(await self.states_of(task_id), {"a": "failed"})
        self.assertEqual(len(rt["local"].calls_of("a")), 1)  # not retryable


if __name__ == "__main__":
    unittest.main()
