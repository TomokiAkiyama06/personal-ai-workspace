"""Queue, budget, loop detection and escalation working together with the PAW-032
task lifecycle: the acceptance criteria as one flow. Skipped unless
``PAW_TEST_DATABASE_URL`` is set."""

from paw_backend.tasks import Actor, TaskCommand, TaskState, WaitReason
from paw_backend.tasks.queueing import (
    ACTION_TASK_COMMANDS,
    BudgetKind,
    BudgetPreset,
    Decision,
    DecisionReason,
    LoopVerdict,
    NextAction,
    Priority,
    decide_next_action,
)

from .queueing_support import PostgresQueueingTestCase, at, requires_postgres

K = BudgetKind
A = NextAction


@requires_postgres
class FlowTest(PostgresQueueingTestCase):
    async def start_task(self, preset: BudgetPreset):
        (task_id,) = await self.make_tasks(1)
        await self.budget.set_preset(task_id, preset)
        entry = await self.queue.enqueue(task_id, now=at(0), priority=Priority.NORMAL)
        claimed = await self.queue.claim_next("local-worker", at(1))
        self.assertEqual(claimed.id, entry.id)
        await self.service.execute(task_id, TaskCommand.START, actor=self.system)
        self.runtime_generation = await self.budget.start_runtime(task_id)
        return task_id, claimed

    async def fail_step(self, task_id, approach: int):
        assessment = await self.loop_detector.record_failure(
            task_id,
            attempt=1,
            error_class="AssertionError",
            step="run_tests",
            message="assert result == 42",
            approach=approach,
        )
        await self.budget.record(task_id, K.STEPS, 1)
        return assessment

    async def apply(self, task_id, decision: Decision) -> TaskState:
        """Issue the PAW-032 command that the decision maps to (if any)."""
        mapped = ACTION_TASK_COMMANDS[decision.action]
        if mapped is not None:
            command, wait_reason = mapped
            await self.service.execute(
                task_id,
                command,
                actor=Actor.policy(),
                wait_reason=wait_reason,
                reason=decision.reason.value,
            )
        return (await self.service.restore(task_id)).state

    async def test_a_looping_task_tries_an_alternative_then_escalates_then_waits(self):
        task_id, _ = await self.start_task(BudgetPreset.STANDARD)
        actions = []
        for approach in (0, 1):
            for _ in range(3):
                assessment = await self.fail_step(task_id, approach)
                decision = decide_next_action(
                    await self.budget.check(task_id),
                    assessment.verdict,
                    can_escalate=True,
                )
                actions.append(decision.action)
                self.assertEqual(await self.apply(task_id, decision), TaskState.RUNNING)
        self.assertEqual(
            actions,
            [A.CONTINUE, A.CONTINUE, A.TRY_ALTERNATIVE]
            + [A.CONTINUE, A.CONTINUE, A.ESCALATE_AGENT],
        )
        # The stronger agent fails the same way and nobody stronger is left.
        assessment = await self.fail_step(task_id, 2)
        assessment = await self.fail_step(task_id, 2)
        assessment = await self.fail_step(task_id, 2)
        self.assertEqual(assessment.verdict, LoopVerdict.ESCALATE)
        decision = decide_next_action(
            await self.budget.check(task_id), assessment.verdict, can_escalate=False
        )
        self.assertEqual(
            decision,
            Decision(
                A.WAIT_FOR_USER,
                DecisionReason.LOOP_ESCALATION_UNAVAILABLE,
                (),
                LoopVerdict.ESCALATE,
            ),
        )
        self.assertEqual(await self.apply(task_id, decision), TaskState.WAITING)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.wait_reason, WaitReason.USER)

    async def test_an_exhausted_budget_stops_the_task_at_a_safe_boundary(self):
        task_id, entry = await self.start_task(BudgetPreset.STANDARD)
        await self.budget.record(task_id, K.TOKENS, 10**9)
        budget = await self.budget.check(task_id)
        decision = decide_next_action(budget, LoopVerdict.CONTINUE, can_escalate=True)
        self.assertEqual(
            decision,
            Decision(
                A.WAIT_FOR_USER,
                DecisionReason.BUDGET_EXCEEDED,
                (K.TOKENS,),
                LoopVerdict.CONTINUE,
            ),
        )
        self.assertEqual(await self.apply(task_id, decision), TaskState.WAITING)
        # The worker hands the entry back; the runtime clock stops.
        self.clock.set(120)
        await self.queue.release(entry.id, "local-worker", entry.claim_count, at(20))
        runtime = await self.budget.stop_runtime(task_id, self.runtime_generation)
        self.assertEqual(runtime.consumed, 120)

    async def test_exhausted_retries_fail_the_task_which_can_then_be_retried(self):
        task_id, entry = await self.start_task(BudgetPreset.STANDARD)
        await self.budget.record(task_id, K.RETRIES, 10**6)
        decision = decide_next_action(
            await self.budget.check(task_id),
            LoopVerdict.TRY_ALTERNATIVE,
            can_escalate=True,
        )
        self.assertEqual((decision.action, decision.exceeded), (A.FAIL, (K.RETRIES,)))
        self.assertEqual(await self.apply(task_id, decision), TaskState.FAILED)
        await self.queue.complete(entry.id, "local-worker", entry.claim_count, at(30))
        # A human retries (PAW-032) with a bigger preset; the task is queued again.
        await self.service.execute(task_id, TaskCommand.RETRY, actor=self.user)
        await self.budget.set_preset(task_id, BudgetPreset.LONG)
        self.assertEqual((await self.budget.check(task_id)).exceeded, (K.RETRIES,))
        again = await self.queue.enqueue(task_id, now=at(40), priority=Priority.HIGH)
        self.assertNotEqual(again.id, entry.id)

    async def test_the_unlimited_preset_is_never_stopped_by_the_budget_but_by_a_loop(
        self,
    ):
        task_id, _ = await self.start_task(BudgetPreset.UNLIMITED)
        for kind in (K.STEPS, K.RETRIES, K.TOOL_CALLS, K.TOKENS, K.GPU_SECONDS):
            await self.budget.record(task_id, kind, 10**12)
        self.clock.set(10**7)
        budget = await self.budget.check(task_id)
        self.assertEqual(budget.exceeded, ())
        self.assertEqual(
            decide_next_action(budget, LoopVerdict.CONTINUE, can_escalate=True).action,
            A.CONTINUE,
        )
        for _ in range(3):
            assessment = await self.loop_detector.record_failure(
                task_id,
                attempt=1,
                error_class="E",
                step="s",
                message="same",
                approach=1,
            )
        self.assertEqual(assessment.verdict, LoopVerdict.ESCALATE)
        self.assertEqual(
            decide_next_action(budget, assessment.verdict, can_escalate=True).action,
            A.ESCALATE_AGENT,
        )

    async def test_the_three_priorities_start_in_order_and_do_not_preempt(self):
        low, normal, high = await self.make_tasks(3)
        await self.queue.enqueue(low, now=at(0), priority=Priority.LOW)
        await self.queue.enqueue(normal, now=at(1), priority=Priority.NORMAL)
        first = await self.queue.claim_next("w1", at(2))
        self.assertEqual(first.task_id, normal)
        await self.queue.enqueue(high, now=at(3), priority=Priority.HIGH)
        # The running NORMAL entry keeps running; HIGH goes before the waiting LOW.
        self.assertEqual((await self.queue.claim_next("w2", at(4))).task_id, high)
        self.assertEqual((await self.queue.claim_next("w3", at(5))).task_id, low)
        self.assertIsNone(await self.queue.claim_next("w4", at(6)))
