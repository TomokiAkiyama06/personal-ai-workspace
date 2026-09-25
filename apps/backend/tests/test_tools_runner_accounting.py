"""``ToolRunner.run`` finishes the execution accounting before it reports a cancel.

A tool call that ran is audited and charged even when the caller is cancelled
meanwhile. The record used to be ``asyncio.shield``-ed only: a cancellation that
arrived while a slow audit or budget adapter was still working made ``run``
return at once and left the accounting as an untracked background task, which
event-loop shutdown cancels (an executed tool could lose its audit row and its
budget charge). ``run`` now retains the accounting task and waits for it to end
before it lets the cancellation through (issue #90, finding on #74).
"""

import asyncio
import unittest

from paw_backend.authz import InMemoryAuditSink

from .tools_support import ROOT, TASK, FakeBudget, Harness, make_call

CALL = "repo.read_file"
READ = {"path": f"{ROOT}/src/a.py"}
DEADLINE = 30  # generous: only a hang can reach it


class GatedSink:
    """The broker's audit sink: the row of an executed call waits for ``release``."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def record(self, event) -> None:
        if event.reason in ("executed", "execution_failed"):
            self.entered.set()
            await self.release.wait()
        await self.inner.record(event)


class GatedBudget(FakeBudget):
    """A budget whose charge waits for ``release``."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def charge(self, task_id, tool) -> None:
        self.entered.set()
        await self.release.wait()
        await super().charge(task_id, tool)


def execution_rows(sink) -> list[tuple[str, str]]:
    return [
        (e.decision, e.reason)
        for e in sink.events
        if e.action == f"tool.{CALL}" and e.reason in ("executed", "execution_failed")
    ]


class CancelDuringAccountingTest(unittest.IsolatedAsyncioTestCase):
    async def start(self, harness) -> asyncio.Task:
        task = asyncio.create_task(harness.runner.run(make_call(CALL, READ)))
        self.addAsyncCleanup(self.stop, task)
        return task

    async def stop(self, task: asyncio.Task) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_a_cancel_during_a_slow_audit_write_waits_for_it(self):
        sink = InMemoryAuditSink()
        gated = GatedSink(sink)
        h = Harness(sink=sink, broker_sink=gated)
        task = await self.start(h)
        await asyncio.wait_for(gated.entered.wait(), DEADLINE)

        task.cancel()
        await asyncio.sleep(0.2)  # generous: the cancel must not get through yet
        self.assertFalse(task.done())
        self.assertEqual(execution_rows(sink), [])

        gated.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
        # Both records exist by the time the caller learns of the cancellation.
        self.assertEqual(execution_rows(sink), [("allow", "executed")])
        self.assertEqual(h.budget.charges, [(TASK, CALL)])

    async def test_a_cancel_during_a_slow_budget_charge_waits_for_it(self):
        budget = GatedBudget()
        h = Harness(budget=budget)
        task = await self.start(h)
        await asyncio.wait_for(budget.entered.wait(), DEADLINE)

        task.cancel()
        await asyncio.sleep(0.2)
        self.assertFalse(task.done())
        self.assertEqual(budget.charges, [])

        budget.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
        self.assertEqual(budget.charges, [(TASK, CALL)])
        self.assertEqual(execution_rows(h.sink), [("allow", "executed")])

    async def test_repeated_cancels_do_not_cut_the_accounting_short(self):
        sink = InMemoryAuditSink()
        gated = GatedSink(sink)
        h = Harness(sink=sink, broker_sink=gated)
        task = await self.start(h)
        await asyncio.wait_for(gated.entered.wait(), DEADLINE)

        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.05)
        self.assertFalse(task.done())

        gated.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
        self.assertEqual(execution_rows(sink), [("allow", "executed")])
        self.assertEqual(h.budget.charges, [(TASK, CALL)])

    async def test_a_timeout_around_run_still_becomes_a_timeout_error(self):
        # The cancellation outcome is preserved: ``asyncio.timeout`` turns the
        # cancel it delivered into ``TimeoutError`` once ``run`` lets it through.
        sink = InMemoryAuditSink()
        gated = GatedSink(sink)
        h = Harness(sink=sink, broker_sink=gated)
        asyncio.get_running_loop().call_later(0.3, gated.release.set)
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.1):
                await h.runner.run(make_call(CALL, READ))
        self.assertEqual(execution_rows(sink), [("allow", "executed")])
        self.assertEqual(h.budget.charges, [(TASK, CALL)])

    async def test_without_a_cancel_the_outcome_is_unchanged(self):
        h = Harness()
        outcome = await h.runner.run(make_call(CALL, READ))
        self.assertEqual(outcome.result, {"ok": True})
        self.assertEqual(execution_rows(h.sink), [("allow", "executed")])
        self.assertEqual(h.budget.charges, [(TASK, CALL)])


if __name__ == "__main__":
    unittest.main()
