"""``NodeOutcome.escalate``: a failure that only a higher rung can solve.

Decision 0083 (Approved), section 4, adds ``escalate`` to Decision 0021's section
3 and lets such a failure skip Decision 0007's "try an alternative first": the
node moves to the next agent of its ladder at once (the budget still comes
first). Without a next agent it is an ordinary retryable failure. Real
PostgreSQL, the real loop detector and budget, scripted agents.
"""

import unittest
from dataclasses import replace

from paw_backend.orchestrator import InvalidOrchestratorArgumentError, NodeOutcome
from paw_backend.orchestrator.domain import NextStep, RunOutcome
from paw_backend.orchestrator.orchestrator import _Finished
from paw_backend.orchestrator.result import NodeResult
from paw_backend.tasks import TaskState
from paw_backend.tasks.queueing import NextAction
from paw_backend.tasks.queueing.validation import MAX_APPROACH

from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    fail,
    make_plan,
    node,
    ok,
    requires_postgres,
)

Out = RunOutcome


def runtimes(**scripts):
    timeline = []
    return {
        label: FakeRuntime(label, script=script, timeline=timeline)
        for label, script in scripts.items()
    }


def escalating(error_class="RuntimeError", message="context_limit"):
    return NodeOutcome.failed(error_class, message, escalate=True)


class EscalateOutcomeTest(unittest.TestCase):
    def test_escalate_is_a_flag_of_a_retryable_failure_only(self):
        outcome = escalating()
        self.assertTrue(outcome.escalate)
        self.assertTrue(outcome.retryable)
        self.assertFalse(NodeOutcome.failed("RuntimeError").escalate)
        self.assertFalse(NodeOutcome.succeeded(NodeResult("done")).escalate)
        # Both at once contradict each other: refused, neither wins.
        with self.assertRaises(InvalidOrchestratorArgumentError) as raised:
            NodeOutcome.failed("RuntimeError", retryable=False, escalate=True)
        self.assertEqual(raised.exception.parameter, "escalate")
        for options in (
            {"error_class": "E", "escalate": 1},
            {"error_class": "E", "escalate": "yes"},
            {"result": NodeResult("done"), "escalate": True},
        ):
            with (
                self.subTest(options),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                NodeOutcome(**options)


@requires_postgres
class EscalateRetryStepTest(PostgresOrchestratorTestCase):
    """``_retry_step`` with ``escalate``, for every loop verdict."""

    async def test_an_escalating_failure_skips_the_retry_and_the_alternative(self):
        h = self.harness()
        dag = await self.make_dag(make_plan(node("a")))
        record = dag.node("a")
        failure = replace(_Finished.failure("RuntimeError"), escalate=True)
        retry = h.orchestrator._retry_step
        up = (NextStep.ESCALATE, {"agent_index": 1, "approach": 1})

        for action in (
            NextAction.CONTINUE,
            NextAction.TRY_ALTERNATIVE,
            NextAction.ESCALATE_AGENT,
        ):
            with self.subTest(action):
                self.assertEqual(retry(record, action, failure, can_escalate=True), up)
        # Without a next rung: the ordinary way.
        self.assertEqual(
            retry(record, NextAction.CONTINUE, failure, can_escalate=False),
            (NextStep.RETRY, {}),
        )
        self.assertEqual(
            retry(record, NextAction.TRY_ALTERNATIVE, failure, can_escalate=False),
            (NextStep.ALTERNATIVE, {"agent_index": 0, "approach": 1}),
        )
        # The approach counter at its end: no escalation, and the ordinary rules
        # (a retry on the rung) apply.
        last = replace(record, approach=MAX_APPROACH)
        self.assertEqual(
            retry(last, NextAction.CONTINUE, failure, can_escalate=True),
            (NextStep.RETRY, {}),
        )
        # Not retryable wins over everything (it cannot be built with escalate,
        # but the run loop's own failures may say so).
        final = replace(failure, retryable=False)
        self.assertEqual(
            retry(record, NextAction.CONTINUE, final, can_escalate=True),
            (NextStep.GIVE_UP, {}),
        )


@requires_postgres
class EscalateRunTest(PostgresOrchestratorTestCase):
    async def test_an_escalating_failure_moves_to_the_next_agent_at_once(self):
        rt = runtimes(local={"a": escalating()}, codex={"a": ok("solved above")})
        h = self.harness(runtimes=rt, ladder=("local", "codex"))
        task_id = await self.prepare(h, make_plan(node("a")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(
            [(a.agent, a.approach) for a in rt["local"].calls_of("a")], [("local", 0)]
        )
        self.assertEqual(
            [(a.agent, a.approach) for a in rt["codex"].calls_of("a")], [("codex", 1)]
        )
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node("a").result.summary, "solved above")
        # The escalation is a retry like any other (Decision 0007's counter).
        usage = {u.kind.value: u.consumed for u in await h.budget.usage(task_id)}
        self.assertEqual(usage["retries"], 1)

    async def test_without_an_escalation_the_same_failure_retries_on_the_rung(self):
        # The reference: the same failure without ``escalate`` stays on its rung.
        rt = runtimes(
            local={"a": [fail("RuntimeError", "context_limit"), ok()]},
            codex={"a": ok()},
        )
        h = self.harness(runtimes=rt, ladder=("local", "codex"))
        await self.prepare(h, make_plan(node("a")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(rt["local"].calls_of("a")), 2)
        self.assertEqual(rt["codex"].calls_of("a"), [])

    async def test_different_failures_that_escalate_do_not_wait_for_the_loop(self):
        # The loop detector needs one signature three times; failures that differ
        # never trigger it. With ``escalate`` the node still climbs, one rung per
        # failure, and the last rung retries as usual.
        rt = runtimes(
            local={"a": escalating("RuntimeError", "step_limit")},
            codex={"a": escalating("ConnectionError", "credential_expired")},
            claude={"a": [escalating("RuntimeError", "no_submission"), ok()]},
        )
        h = self.harness(runtimes=rt, ladder=("local", "codex", "claude"))
        await self.prepare(h, make_plan(node("a")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        seen = sorted(
            (a.attempt, a.agent, a.approach)
            for r in rt.values()
            for a in r.calls_of("a")
        )
        self.assertEqual(
            seen,
            [(1, "local", 0), (2, "codex", 1), (3, "claude", 2), (4, "claude", 2)],
        )

    async def test_the_budget_comes_before_an_escalation(self):
        rt = runtimes(local={"a": escalating()}, codex={"a": ok()})
        h = self.harness(runtimes=rt, ladder=("local", "codex"))
        task_id = await self.prepare(h, make_plan(node("a")))
        await self.owner_sql(
            "UPDATE budget_usages SET limit_value = 0, consumed = 0"
            " WHERE task_id = :t AND kind = 'retries'",
            t=task_id,
        )

        await h.orchestrator.run_once("w1")

        # No retry is left: the escalation would be one, so the node does not climb.
        self.assertEqual(rt["codex"].calls_of("a"), [])
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.FAILED)

    async def test_a_planner_failure_that_escalates_is_an_ordinary_planner_failure(
        self,
    ):
        # The planner already climbs its ladder at every call: nothing changes.
        plan = make_plan(node("a"))

        async def planner(_assignment):
            return NodeOutcome.succeeded(NodeResult("plan"), plan=plan)

        rt = runtimes(
            local={"plan": escalating()},
            codex={"plan": planner},
        )
        h = self.harness(runtimes=rt, ladder=("local", "codex"))
        task_id = await self.prepare(h)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertIsNotNone(await self.store.get(task_id, 1))
