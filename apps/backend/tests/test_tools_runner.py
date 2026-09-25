import asyncio
import time
import unittest
from unittest.mock import patch

from paw_backend.authz import SystemRole
from paw_backend.tools import (
    ApprovalOutcome,
    BrokerReason,
    BudgetStatus,
    ExecutionStatus,
    ToolRunner,
    Verdict,
)
from paw_backend.tools.credentials import MAX_RESULT_NODES
from paw_backend.tools.runner import DEFAULT_EXECUTION_TIMEOUT

from .authz_support import SECRET, FailingSink, principal
from .tools_support import (
    HANDLE,
    REPO,
    ROOT,
    TASK,
    U1,
    FakeBudget,
    FakeExecutor,
    Harness,
    make_call,
)

R = BrokerReason
GITHUB_TOKEN = "ghp_" + "a1B2" * 9
READ = {"path": f"{ROOT}/src/a.py"}
PUSH = {
    "remote": "https://github.com/org/repo.git",
    "repository": str(REPO),
    "credential": HANDLE,
}


class BrokerNeverExecutesTest(unittest.IsolatedAsyncioTestCase):
    async def test_requesting_a_call_runs_nothing(self):
        h = Harness()
        decision = await h.broker.request(make_call("repo.read_file", READ))
        self.assertTrue(decision.allowed)
        self.assertEqual(h.executor.invocations, [])
        self.assertEqual(h.budget.charges, [])

    async def test_the_broker_has_no_execution_entry_point(self):
        h = Harness()
        for name in ("run", "execute", "invoke", "call"):
            self.assertFalse(hasattr(h.broker, name), name)


class RunTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()

    async def test_an_allowed_call_runs_once_with_the_normalised_invocation(self):
        outcome = await self.h.runner.run(
            make_call("repo.read_file", {"path": "src/./a.py"})
        )
        self.assertEqual(
            (outcome.status, outcome.decision.verdict, outcome.error_type),
            (ExecutionStatus.COMPLETED, Verdict.ALLOW, None),
        )
        self.assertEqual(outcome.result, {"ok": True})
        self.assertEqual(len(self.h.executor.invocations), 1)
        invocation = self.h.executor.invocations[0]
        self.assertEqual(invocation.tool, "repo.read_file")
        self.assertEqual(dict(invocation.arguments), {"path": f"{ROOT}/src/a.py"})
        self.assertEqual(invocation.call_hash, outcome.decision.call_hash)
        self.assertEqual(invocation.context.task_id, TASK)

    async def test_the_executor_gets_the_credential_handle_and_never_a_plaintext(self):
        await self.h.runner.run(make_call("git.push", PUSH))
        invocation = self.h.executor.invocations[0]
        self.assertEqual(invocation.arguments["credential"], HANDLE)
        self.assertNotIn("ghp_", repr(dict(invocation.arguments)))

    async def test_a_denied_call_and_a_pending_approval_do_not_run(self):
        for tool, arguments, verdict in (
            ("nope", {}, Verdict.DENY),
            ("repo.read_file", {"path": "/etc/passwd"}, Verdict.DENY),
            ("credentials.read", {"credential": HANDLE}, Verdict.DENY),
            ("repo.delete_tree", {"path": f"{ROOT}/x"}, Verdict.NEEDS_APPROVAL),
        ):
            with self.subTest(tool=tool):
                outcome = await self.h.runner.run(make_call(tool, arguments))
                self.assertEqual(
                    (outcome.status, outcome.decision.verdict, outcome.result),
                    (ExecutionStatus.NOT_EXECUTED, verdict, None),
                )
        self.assertEqual(self.h.executor.invocations, [])
        self.assertEqual(self.h.budget.charges, [])
        self.assertEqual(
            [
                e.reason
                for e in self.h.sink.events
                if e.reason in ("executed", "execution_failed")
            ],
            [],
        )

    async def test_every_run_is_decided_afresh(self):
        call = make_call("repo.read_file", READ)
        first = await self.h.runner.run(call)
        self.h.budget.status = BudgetStatus.EXCEEDED
        second = await self.h.runner.run(call)
        self.assertEqual(first.status, ExecutionStatus.COMPLETED)
        self.assertEqual(
            (second.status, second.decision.reason),
            (ExecutionStatus.NOT_EXECUTED, R.BUDGET_EXCEEDED),
        )
        self.assertEqual(len(self.h.executor.invocations), 1)

    async def test_an_approved_call_runs_once_through_the_runner(self):
        call = make_call("repo.delete_tree", {"path": f"{ROOT}/build"})
        pending = await self.h.runner.run(call)
        approval_id = pending.decision.approval_id
        result = await self.h.service.approve(
            approval_id, principal(SystemRole.USER, U1)
        )
        self.assertEqual(result.outcome, ApprovalOutcome.APPROVED)
        ran = await self.h.runner.run(call, approval_id=approval_id)
        replay = await self.h.runner.run(call, approval_id=approval_id)
        self.assertEqual(
            (ran.status, replay.status, replay.decision.reason),
            (
                ExecutionStatus.COMPLETED,
                ExecutionStatus.NOT_EXECUTED,
                R.APPROVAL_ALREADY_USED,
            ),
        )
        self.assertEqual(len(self.h.executor.invocations), 1)

    async def test_the_run_is_audited_and_charged(self):
        await self.h.runner.run(make_call("repo.read_file", READ))
        rows = [e for e in self.h.sink.events if e.action == "tool.repo.read_file"]
        self.assertEqual(
            [(e.decision, e.reason) for e in rows],
            [("allow", "auto"), ("allow", "executed")],
        )
        self.assertEqual(len({e.correlation_id for e in rows}), 1)
        self.assertEqual(self.h.budget.charges, [(TASK, "repo.read_file")])

    async def test_a_tool_without_a_budget_is_not_charged(self):
        await self.h.runner.run(make_call("free.ping", {}))
        self.assertEqual(self.h.budget.charges, [])


class ExecutorFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_executor_exception_is_reported_by_type_only(self):
        h = Harness(executor=FakeExecutor(error=RuntimeError(SECRET + GITHUB_TOKEN)))
        with self.assertLogs(level="ERROR") as logs:
            outcome = await h.runner.run(make_call("repo.read_file", READ))
        self.assertEqual(
            (outcome.status, outcome.error_type, outcome.result, outcome.redactions),
            (ExecutionStatus.FAILED, "RuntimeError", None, 0),
        )
        self.assertTrue(outcome.decision.allowed)
        everything = "\n".join(logs.output) + repr(outcome)
        everything += "".join(e.model_dump_json() for e in h.sink.events)
        self.assertNotIn(SECRET, everything)
        self.assertNotIn(GITHUB_TOKEN, everything)
        self.assertIn("RuntimeError", "\n".join(logs.output))

    async def test_a_failed_call_is_audited_and_still_charged(self):
        h = Harness(executor=FakeExecutor(error=OSError("disk gone")))
        with self.assertLogs(level="ERROR"):
            await h.runner.run(make_call("repo.read_file", READ))
        last = [e for e in h.sink.events if e.action == "tool.repo.read_file"][-1]
        self.assertEqual((last.decision, last.reason), ("allow", "execution_failed"))
        self.assertEqual(h.budget.charges, [(TASK, "repo.read_file")])

    async def test_an_executor_that_takes_too_long_is_stopped(self):
        class Stuck:
            async def execute(self, invocation):
                await asyncio.Event().wait()

        h = Harness(executor=Stuck())
        runner = ToolRunner(h.broker, h.executor, execution_timeout=0.05)
        with self.assertLogs(level="ERROR"):
            outcome = await runner.run(make_call("repo.read_file", READ))
        self.assertEqual(
            (outcome.status, outcome.error_type),
            (ExecutionStatus.FAILED, "TimeoutError"),
        )

    async def test_a_cancelled_run_is_still_recorded_and_charged(self):
        started = asyncio.Event()

        class Blocks:
            async def execute(self, invocation):
                started.set()
                await asyncio.Event().wait()

        h = Harness(executor=Blocks())
        task = asyncio.create_task(h.runner.run(make_call("repo.read_file", READ)))
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        last = [e for e in h.sink.events if e.action == "tool.repo.read_file"][-1]
        self.assertEqual((last.decision, last.reason), ("allow", "execution_failed"))
        self.assertEqual(h.budget.charges, [(TASK, "repo.read_file")])

    async def test_a_failure_after_the_call_ran_is_still_recorded_and_charged(self):
        h = Harness()
        with patch("paw_backend.tools.runner.redact_value", side_effect=ValueError):
            with self.assertRaises(ValueError):
                await h.runner.run(make_call("repo.read_file", READ))
        last = [e for e in h.sink.events if e.action == "tool.repo.read_file"][-1]
        self.assertEqual((last.decision, last.reason), ("allow", "executed"))
        self.assertEqual(h.budget.charges, [(TASK, "repo.read_file")])

    async def test_a_failing_budget_charge_does_not_change_the_outcome(self):
        class ChargeFails(FakeBudget):
            async def charge(self, task_id, tool):
                raise ConnectionError(SECRET)

        h = Harness(budget=ChargeFails())
        with self.assertLogs(level="ERROR") as logs:
            outcome = await h.runner.run(make_call("repo.read_file", READ))
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("ConnectionError", "\n".join(logs.output))

    async def test_a_failing_execution_audit_does_not_change_the_outcome(self):
        class FailsAfterDecision(FailingSink):
            def __init__(self, ok):
                super().__init__()
                self.ok = ok

            async def record(self, event):
                if event.reason in ("executed", "execution_failed"):
                    self.attempts += 1
                    raise ConnectionError(SECRET)
                await self.ok.record(event)

        h = Harness()
        h.broker._audit = FailsAfterDecision(h.sink)
        with self.assertLogs(level="ERROR") as logs:
            outcome = await h.runner.run(make_call("repo.read_file", READ))
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
        self.assertNotIn(SECRET, "\n".join(logs.output))


class RedactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_result_is_redacted_before_it_is_returned(self):
        result = {
            "stdout": f"remote: using {GITHUB_TOKEN} to push",
            "password": "hunter2",
            "exit_code": 0,
            "items": ["a", f"token {GITHUB_TOKEN}"],
            "blob": b"raw bytes",
        }
        h = Harness(executor=FakeExecutor(result))
        outcome = await h.runner.run(make_call("repo.read_file", READ))
        self.assertEqual(
            outcome.result,
            {
                "stdout": "remote: using [REDACTED] to push",
                "password": "[REDACTED]",
                "exit_code": 0,
                "items": ["a", "token [REDACTED]"],
                "blob": "[UNSUPPORTED]",
            },
        )
        self.assertEqual(outcome.redactions, 4)
        self.assertNotIn(GITHUB_TOKEN, repr(outcome))
        self.assertNotIn("hunter2", repr(outcome))

    async def test_an_object_that_prints_a_secret_is_replaced_not_stringified(self):
        class Leaky:
            def __repr__(self):
                return GITHUB_TOKEN

            __str__ = __repr__

        h = Harness(executor=FakeExecutor({"conn": Leaky()}))
        outcome = await h.runner.run(make_call("repo.read_file", READ))
        self.assertEqual(outcome.result, {"conn": "[UNSUPPORTED]"})

    async def test_a_huge_result_is_cut_before_it_is_read_in_full(self):
        h = Harness(executor=FakeExecutor(["x"] * 2_000_000))
        started = time.monotonic()
        outcome = await h.runner.run(make_call("repo.read_file", READ))
        self.assertLess(time.monotonic() - started, 20.0)  # it took 8.9 s in full
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
        self.assertEqual(len(outcome.result), MAX_RESULT_NODES)
        self.assertEqual(outcome.result[-1], "[TRUNCATED]")
        self.assertEqual(outcome.redactions, 1)

    async def test_a_result_without_secrets_is_returned_as_it_is(self):
        result = {"files": ["a.py", "b.py"], "count": 2, "ok": True, "note": None}
        h = Harness(executor=FakeExecutor(result))
        outcome = await h.runner.run(make_call("repo.read_file", READ))
        self.assertEqual((outcome.result, outcome.redactions), (result, 0))

    async def test_a_result_that_is_none_is_none(self):
        class Silent:
            async def execute(self, invocation):
                return None

        h = Harness(executor=Silent())
        outcome = await h.runner.run(make_call("repo.read_file", READ))
        self.assertEqual(
            (outcome.status, outcome.result), (ExecutionStatus.COMPLETED, None)
        )


class RunnerConstructionTest(unittest.TestCase):
    def test_the_executor_is_validated_up_front(self):
        h = Harness()

        class NoExecute:
            pass

        class SyncExecute:
            def execute(self, invocation):
                return None

        class NoArgument:
            async def execute(self):
                return None

        for executor in (NoExecute(), SyncExecute(), NoArgument(), None, "executor"):
            with self.subTest(executor=type(executor).__name__):
                with self.assertRaises(TypeError):
                    ToolRunner(h.broker, executor)
        with self.assertRaises(TypeError):
            ToolRunner("broker", FakeExecutor())
        for timeout in (0, -1, None, "5", True, 86_401, float("nan")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    ToolRunner(h.broker, FakeExecutor(), execution_timeout=timeout)
        ToolRunner(h.broker, FakeExecutor(), execution_timeout=1.5)
        ToolRunner(h.broker, FakeExecutor(), execution_timeout=86_400)

    def test_a_call_is_bounded_by_default(self):
        h = Harness()
        self.assertEqual(h.runner._execution_timeout, DEFAULT_EXECUTION_TIMEOUT)
        self.assertEqual(DEFAULT_EXECUTION_TIMEOUT, 600.0)

    def test_only_an_allowed_call_can_be_recorded_as_executed(self):
        async def check():
            h = Harness()
            decision = await h.broker.request(make_call("nope", {}))
            with self.assertRaises(ValueError):
                await h.broker.record_execution(decision, succeeded=True)

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
