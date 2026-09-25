"""``ConnectionService.execute`` finishes the settlement before it reports a cancel.

A call that ran is recorded (usage row) and charged (task token budget) even when the
caller is cancelled meanwhile. The settlement used to be ``asyncio.shield``-ed only: a
cancellation that arrived while a slow settlement was still working made ``execute``
return at once and left the settlement as an untracked background task, which
event-loop shutdown or a database disposal cancels (a call that ran could stay
``in_flight`` for good and its tokens uncharged). ``execute`` now retains the
settlement task and waits for it to end before it lets the cancellation through, as
``ToolRunner`` does for its accounting (``test_tools_runner_accounting.py``).
"""

import asyncio
import unittest

from paw_backend.tasks.queueing import BudgetKind, BudgetPreset

from .connections_support import requires_postgres
from .test_connections_budget import TaskBudgetCase
from .test_connections_execute import ExecuteCase

DEADLINE = 30  # generous: only a hang can reach it


class Gate:
    """A wrapper of an async method that waits for ``release`` before it runs."""

    def __init__(self, original) -> None:
        self.original = original
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, *args, **kwargs):
        self.entered.set()
        await self.release.wait()
        return await self.original(*args, **kwargs)


class CancelCase:
    async def start(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.addAsyncCleanup(self.stop, task)
        return task

    async def stop(self, task: asyncio.Task) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def cancel_and_check_it_waits(self, task: asyncio.Task, times: int = 1):
        for _ in range(times):
            task.cancel()
            await asyncio.sleep(0.05)  # generous: the cancel must not get through
        self.assertFalse(task.done())


@requires_postgres
class CancelDuringSettlementTest(CancelCase, ExecuteCase):
    async def gated_settle(self) -> Gate:
        gate = Gate(self.service._store.settle)
        self.service._store.settle = gate
        return gate

    def usage_status(self) -> list[str]:
        return [row["status"] for row in self.usage_rows()]

    async def test_a_cancel_during_a_slow_settlement_waits_for_it(self):
        gate = await self.gated_settle()
        task = await self.start(self.call())
        await asyncio.wait_for(gate.entered.wait(), DEADLINE)
        self.assertEqual(self.usage_status(), ["in_flight"])

        await self.cancel_and_check_it_waits(task)
        self.assertEqual(self.usage_status(), ["in_flight"])  # not settled yet

        gate.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
        # The row exists, settled, by the time the caller learns of the cancel.
        (row,) = self.usage_rows()
        self.assertEqual(
            (row["status"], row["input_tokens"], row["output_tokens"]),
            ("succeeded", 10, 5),
        )
        self.assertIsNotNone(row["finished_at"])

    async def test_repeated_cancels_do_not_cut_the_settlement_short(self):
        gate = await self.gated_settle()
        task = await self.start(self.call())
        await asyncio.wait_for(gate.entered.wait(), DEADLINE)

        await self.cancel_and_check_it_waits(task, times=3)

        gate.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
        self.assertEqual(self.usage_status(), ["succeeded"])

    async def test_a_call_cancelled_while_running_is_settled_before_the_cancel_passes(
        self,
    ):
        gate = await self.gated_settle()
        self.codex.gate = asyncio.Event()  # the adapter never answers
        task = await self.start(self.call())
        await asyncio.wait_for(self.codex.started.wait(), DEADLINE)

        task.cancel()  # the call is cut short ...
        await asyncio.wait_for(gate.entered.wait(), DEADLINE)
        await self.cancel_and_check_it_waits(task)  # ... and a second cancel arrives
        self.assertEqual(self.usage_status(), ["in_flight"])

        gate.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
        (row,) = self.usage_rows()
        self.assertEqual((row["status"], row["failure_code"]), ("cancelled", None))
        self.assertIsNotNone(row["duration_ms"])

    async def test_a_timeout_around_execute_still_becomes_a_timeout_error(self):
        # The cancellation is preserved: ``asyncio.timeout`` turns the cancel it
        # delivered into ``TimeoutError`` once ``execute`` lets it through.
        gate = await self.gated_settle()
        asyncio.get_running_loop().call_later(0.3, gate.release.set)
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.1):
                await self.call()
        self.assertEqual(self.usage_status(), ["succeeded"])

    async def test_a_cancel_of_a_failed_call_waits_for_its_settlement_too(self):
        from paw_backend.connections import AdapterFailure, FailureCode

        self.codex.error = AdapterFailure(FailureCode.RATE_LIMITED)
        gate = await self.gated_settle()
        task = await self.start(self.call())
        await asyncio.wait_for(gate.entered.wait(), DEADLINE)

        await self.cancel_and_check_it_waits(task)

        gate.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
        (row,) = self.usage_rows()
        self.assertEqual(
            (row["status"], row["failure_code"]), ("failed", "rate_limited")
        )

    async def test_without_a_cancel_the_outcome_is_unchanged(self):
        gate = await self.gated_settle()
        gate.release.set()
        result = await self.call()
        self.assertEqual(result.text, "an answer")
        self.assertEqual(self.usage_status(), ["succeeded"])
        self.assertEqual(result.duration_ms, self.usage_rows()[0]["duration_ms"])


@requires_postgres
class CancelDuringBudgetChargeTest(CancelCase, TaskBudgetCase):
    async def test_a_cancel_during_a_slow_budget_charge_waits_for_it(self):
        await self.with_budget(BudgetPreset.STANDARD)
        gate = Gate(self.budget.record)
        self.budget.record = gate
        task = await self.start(self.call())
        await asyncio.wait_for(gate.entered.wait(), DEADLINE)

        await self.cancel_and_check_it_waits(task)
        self.assertEqual(await self.consumed(BudgetKind.TOKENS), 0)

        gate.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
        # The usage row and the budget charge both exist when the caller learns.
        self.assertEqual([r["status"] for r in self.usage_rows()], ["succeeded"])
        self.assertEqual(await self.consumed(BudgetKind.TOKENS), 15)


if __name__ == "__main__":
    unittest.main()
