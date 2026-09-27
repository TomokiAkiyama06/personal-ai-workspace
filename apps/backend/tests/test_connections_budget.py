"""The task's own token budget (PAW-033 ``BudgetTracker``) next to the user's quota."""

import asyncio
import logging
import unittest

from sqlalchemy import text

from paw_backend.connections import (
    AdapterFailure,
    ConnectionCallError,
    ConnectionKind,
    FailureCode,
    QuotaExceededError,
    RefusalReason,
    TaskBudgetError,
)
from paw_backend.db import Database
from paw_backend.tasks.queueing import (
    PRESET_LIMITS,
    BudgetKind,
    BudgetPreset,
    BudgetTracker,
)

from .connections_fakes import CANARY
from .connections_support import PostgresConnectionTestCase, requires_postgres
from .support import make_settings
from .task_support import TEST_DATABASE_URL

CODEX = ConnectionKind.CODEX
TOKEN_LIMIT = PRESET_LIMITS[BudgetPreset.STANDARD][BudgetKind.TOKENS]


@requires_postgres
class TaskBudgetCase(PostgresConnectionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.seed_connection(CODEX)
        self.seed_quota(self.user, 3)
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        self.budget = BudgetTracker(database)
        self.service = self.new_service(budget=self.budget)
        self.task = self.seed_task(self.user)
        self.project = self.project_of(self.task)

    async def with_budget(self, preset=BudgetPreset.STANDARD, task=None):
        await self.budget.set_preset(task or self.task, preset)

    async def consumed(self, kind=BudgetKind.TOKENS, task=None) -> int:
        usage = await self.budget.usage(task or self.task)
        return next(u.consumed for u in usage if u.kind is kind)

    def call(self, task=None, **request):
        task = task or self.task
        return self.service.execute(
            self.principal(self.user),
            self.context(task, self.user, self.project_of(task)),
            CODEX,
            self.request(**request),
        )

    def refusals(self):
        return [e for e in self.sink.events if e.action == "connection.use"]


@requires_postgres
class ChargeTest(TaskBudgetCase):
    async def test_a_call_charges_its_tokens_to_the_task_budget(self):
        await self.with_budget()
        await self.call()
        self.assertEqual(await self.consumed(), 15)  # 10 in + 5 out

    async def test_calls_add_up_and_only_the_token_item_moves(self):
        await self.with_budget()
        await self.call()
        await self.call()
        usage = {u.kind: u.consumed for u in await self.budget.usage(self.task)}
        self.assertEqual(usage[BudgetKind.TOKENS], 30)
        for kind in BudgetKind:
            if kind is not BudgetKind.TOKENS:
                self.assertEqual(usage[kind], 0, kind.value)

    async def test_a_call_that_failed_charges_nothing(self):
        await self.with_budget()
        self.codex.error = AdapterFailure(FailureCode.RATE_LIMITED)
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            with self.assertRaises(ConnectionCallError):
                await self.call()
        self.assertEqual(await self.consumed(), 0)

    async def test_a_call_whose_tokens_are_unknown_charges_nothing(self):
        await self.with_budget()
        self.codex.input_tokens = self.codex.output_tokens = None
        await self.call()
        self.assertEqual(await self.consumed(), 0)

    async def test_a_cancelled_call_charges_nothing(self):
        await self.with_budget()
        self.codex.gate = asyncio.Event()
        running = asyncio.ensure_future(self.call())
        await asyncio.wait_for(self.codex.started.wait(), 30)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(await self.consumed(), 0)

    async def test_the_budget_of_another_task_is_not_touched(self):
        other = self.seed_task(self.user)
        await self.with_budget()
        await self.with_budget(task=other)
        await self.call()
        self.assertEqual(await self.consumed(task=other), 0)

    async def test_a_charge_that_fails_does_not_fail_the_call(self):
        await self.with_budget()

        async def broken(*args):
            raise RuntimeError("budget store down " + CANARY)

        self.budget.record = broken
        with self.assertLogs("paw_backend.connections", logging.ERROR) as logs:
            result = await self.call()
        self.assertEqual(result.text, "an answer")
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn(CANARY, text)
        self.assertEqual(self.usage_rows()[0]["status"], "succeeded")


@requires_postgres
class BudgetRefusalTest(TaskBudgetCase):
    async def test_a_task_whose_token_budget_is_used_up_may_not_call(self):
        await self.with_budget()
        await self.budget.record(self.task, BudgetKind.TOKENS, TOKEN_LIMIT)

        with self.assertRaises(TaskBudgetError) as caught:
            await self.call()

        self.assertEqual(caught.exception.reason, RefusalReason.TASK_BUDGET_EXCEEDED)
        self.assertEqual(self.usage_rows(), [])
        self.assertEqual(self.codex.requests, [])
        self.assertEqual(self.resolver.resolved, [])
        (event,) = self.refusals()
        self.assertEqual(
            (event.decision, event.reason, event.actor_id),
            ("deny", "task_budget_exceeded", self.user),
        )

    async def test_the_limit_itself_is_the_last_amount_that_may_be_used(self):
        # "May the task do one more?": consumed + 1 > limit. One below is allowed.
        await self.with_budget()
        await self.budget.record(self.task, BudgetKind.TOKENS, TOKEN_LIMIT - 1)
        await self.call()  # allowed; it reports 15 tokens, so the budget is passed
        self.assertEqual(await self.consumed(), TOKEN_LIMIT + 14)
        with self.assertRaises(TaskBudgetError):
            await self.call()
        self.assertEqual(len(self.usage_rows()), 1)

    async def test_an_unlimited_budget_never_refuses(self):
        await self.with_budget(BudgetPreset.UNLIMITED)
        await self.budget.record(self.task, BudgetKind.TOKENS, 10**12)
        await self.call()

    async def test_a_task_without_a_budget_is_refused_no_budget_is_not_unlimited(self):
        with self.assertRaises(TaskBudgetError) as caught:
            await self.call()
        self.assertEqual(
            caught.exception.reason, RefusalReason.TASK_BUDGET_NOT_CONFIGURED
        )
        (event,) = self.refusals()
        self.assertEqual(event.reason, "task_budget_not_configured")
        self.assertEqual(self.usage_rows(), [])

    async def test_only_the_token_item_decides(self):
        await self.with_budget()
        for kind in (
            BudgetKind.STEPS,
            BudgetKind.TOOL_CALLS,
            BudgetKind.RETRIES,
            BudgetKind.GPU_SECONDS,
        ):
            limit = PRESET_LIMITS[BudgetPreset.STANDARD][kind]
            await self.budget.record(self.task, kind, limit + 1)
        await self.call()  # steps, tool calls, ... belong to other seams

    async def test_a_refusal_by_the_budget_does_not_use_up_the_user_quota(self):
        self.clear_quotas_and_seed(1)
        await self.with_budget()
        await self.budget.record(self.task, BudgetKind.TOKENS, TOKEN_LIMIT)
        for _ in range(3):
            with self.assertRaises(TaskBudgetError):
                await self.call()
        other = self.seed_task(self.user)
        await self.with_budget(task=other)
        await self.call(task=other)  # the one request of the quota is still there
        self.assertEqual(len(self.usage_rows()), 1)

    async def test_the_budget_also_stops_a_task_that_is_already_running(self):
        await self.with_budget()
        await self.call()
        await self.budget.record(self.task, BudgetKind.TOKENS, TOKEN_LIMIT)
        with self.assertRaises(TaskBudgetError):
            await self.call()

    async def test_a_running_task_with_a_budget_continues_past_the_user_quota(self):
        self.clear_quotas_and_seed(1)
        await self.with_budget()
        await self.call()
        with self.assertRaises(QuotaExceededError):  # a NEW task is refused ...
            other = self.seed_task(self.user)
            await self.with_budget(task=other)
            await self.call(task=other)
        await self.call()  # ... the running one goes on
        self.assertEqual(await self.consumed(), 30)

    async def test_a_tracker_that_fails_stops_the_call_and_runs_nothing(self):
        await self.with_budget()

        async def broken(*args, **kwargs):
            raise RuntimeError("budget store down")

        self.budget.check = broken
        with self.assertRaises(RuntimeError):
            await self.call()
        self.assertEqual(self.usage_rows(), [])
        self.assertEqual(self.codex.requests, [])

    async def test_the_refusal_is_an_error_with_a_fixed_message(self):
        with self.assertRaises(TaskBudgetError) as caught:
            await self.call()
        self.assertEqual(
            str(caught.exception),
            "The task budget refuses the call: task_budget_not_configured",
        )

    # -- helper ---------------------------------------------------------------

    def clear_quotas_and_seed(self, limit: int) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM connection_quotas"))
        self.seed_quota(self.user, limit)


@requires_postgres
class WithoutABudgetTest(PostgresConnectionTestCase):
    async def test_a_service_without_a_tracker_asks_no_budget_question(self):
        self.seed_connection(CODEX)
        self.seed_quota(self.user, 3)
        task = self.seed_task(self.user)
        # No budget rows exist for the task, and none are needed.
        result = await self.service.execute(
            self.principal(self.user),
            self.context(task, self.user, self.project_of(task)),
            CODEX,
            self.request(),
        )
        self.assertEqual(result.text, "an answer")


if __name__ == "__main__":
    unittest.main()
