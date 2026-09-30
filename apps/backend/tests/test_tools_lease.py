"""The Broker checks the worker's queue lease on every tool call (issue #126,
Decision 0046).

The lease in ``TaskContext.lease`` is the fencing token: a worker whose lease was
lost, expired or taken over is refused (``lease_lost``) before anything runs and
before an approval is opened or used, and a lease whose state cannot be read is
refused too (``lease_unavailable``): fail closed. The budget charge of a call is
made for the run the call ran for.
"""

import asyncio
import contextlib
import unittest

from paw_backend.authz import Authorizer, InMemoryAuditSink, SystemRole
from paw_backend.tasks.queueing import QueueLease
from paw_backend.tools import (
    ApprovalOutcome,
    ApprovalStatus,
    BrokerReason,
    BudgetStatus,
    ExecutionStatus,
    InMemoryApprovalStore,
    LeaseStatus,
    TaskContext,
    TaskRun,
    ToolBroker,
    Verdict,
)

from .authz_support import SECRET, principal
from .tools_support import (
    LEASE,
    ROOT,
    RUN,
    TASK,
    U1,
    FakeLease,
    Harness,
    make_call,
    make_context,
    sample_registry,
)

R = BrokerReason
READ = {"path": f"{ROOT}/src/a.py"}
DELETE = {"path": f"{ROOT}/build"}


class LeaseCheckTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()

    async def run_call(self, tool="repo.read_file", arguments=None, **kw):
        return await self.h.runner.run(
            make_call(tool, READ if arguments is None else arguments, **kw)
        )

    def tool_rows(self):
        return [(e.decision, e.reason) for e in self.h.tool_events()]

    async def test_every_call_asks_about_the_lease_it_carries(self):
        lease = QueueLease(42, "worker-7", 3)
        context = make_context(lease=lease)
        for tool, arguments in (
            ("repo.read_file", READ),
            ("free.ping", {}),  # a call that needs no budget asks as well
        ):
            with self.subTest(tool=tool):
                outcome = await self.run_call(tool, arguments, context=context)
                self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
        self.assertEqual(self.h.lease.checks, [(TASK, lease), (TASK, lease)])

    async def test_a_lost_lease_runs_nothing_and_charges_nothing(self):
        self.h.lease.answer = LeaseStatus.LOST
        outcome = await self.run_call()
        self.assertEqual(outcome.status, ExecutionStatus.NOT_EXECUTED)
        self.assertEqual(
            (outcome.decision.verdict, outcome.decision.reason),
            (Verdict.DENY, R.LEASE_LOST),
        )
        self.assertIsNone(outcome.decision.invocation)
        self.assertEqual(self.h.executor.invocations, [])
        self.assertEqual(self.h.budget.charges, [])
        # The refusal is audited with its stable code.
        self.assertEqual(self.tool_rows(), [("deny", "lease_lost")])

    async def test_a_lease_that_cannot_be_read_is_refused(self):
        class NotAnAnswer(FakeLease):
            async def check(self, task_id, lease):
                return "held"  # a string, not a LeaseStatus: an adapter bug

        for lease in (
            FakeLease(LeaseStatus.UNKNOWN),
            FakeLease(error=ConnectionError(SECRET)),
            NotAnAnswer(),
        ):
            with self.subTest(lease=type(lease).__name__):
                h = Harness(lease=lease)
                with (
                    self.assertLogs(level="ERROR")
                    if lease.error
                    else contextlib.nullcontext()
                ):
                    outcome = await h.runner.run(make_call("repo.read_file", READ))
                self.assertEqual(outcome.decision.reason, R.LEASE_UNAVAILABLE)
                self.assertEqual(h.executor.invocations, [])

    async def test_the_error_of_a_failing_check_is_not_logged(self):
        h = Harness(lease=FakeLease(error=ConnectionError(SECRET)))
        with self.assertLogs(level="ERROR") as logs:
            await h.runner.run(make_call("repo.read_file", READ))
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("ConnectionError", "\n".join(logs.output))

    async def test_a_check_that_hangs_is_refused_in_time(self):
        class Stuck(FakeLease):
            async def check(self, task_id, lease):
                await asyncio.Event().wait()

        h = Harness(lease=Stuck(), timeout_seconds=0.05)
        with self.assertLogs(level="ERROR"):
            decision = await asyncio.wait_for(
                h.broker.request(make_call("repo.read_file", READ)), 5
            )
        self.assertEqual(decision.reason, R.LEASE_UNAVAILABLE)

    async def test_a_call_refused_on_its_own_does_not_ask_about_the_lease(self):
        # The lease is checked after every check that can refuse the call by
        # itself (closest to the hand-over): an unknown tool, a path out of
        # scope and a used-up budget are refused with their own reasons.
        self.h.budget.status = BudgetStatus.EXCEEDED
        for tool, arguments, reason in (
            ("no.such.tool", {}, R.UNKNOWN_TOOL),
            ("repo.read_file", {"path": "/etc/passwd"}, R.PATH_OUT_OF_SCOPE),
            ("repo.read_file", READ, R.BUDGET_EXCEEDED),
        ):
            with self.subTest(tool=tool, reason=reason):
                decision = await self.h.broker.request(make_call(tool, arguments))
                self.assertEqual(decision.reason, reason)
        self.assertEqual(self.h.lease.checks, [])

    async def test_a_worker_that_lost_its_lease_opens_no_approval(self):
        self.h.lease.answer = LeaseStatus.LOST
        decision = await self.h.broker.request(make_call("repo.delete_tree", DELETE))
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.LEASE_LOST)
        )
        self.assertIsNone(decision.approval_id)
        self.assertEqual(self.h.approvals._records, {})
        self.assertEqual(self.h.events, [])  # nobody was asked to approve

    async def test_an_approval_is_not_used_up_by_a_worker_that_lost_its_lease(self):
        opened = await self.h.broker.request(make_call("repo.delete_tree", DELETE))
        self.assertEqual(opened.verdict, Verdict.NEEDS_APPROVAL)
        approved = await self.h.service.approve(
            opened.approval_id, principal(SystemRole.USER, U1)
        )
        self.assertEqual(approved.outcome, ApprovalOutcome.APPROVED)

        self.h.lease.answer = LeaseStatus.LOST
        stale = await self.h.runner.run(
            make_call("repo.delete_tree", DELETE), approval_id=opened.approval_id
        )
        self.assertEqual(
            (stale.decision.verdict, stale.decision.reason, stale.status),
            (Verdict.DENY, R.LEASE_LOST, ExecutionStatus.NOT_EXECUTED),
        )
        record = await self.h.approvals.get(opened.approval_id)
        self.assertEqual(record.status, ApprovalStatus.APPROVED)  # still unused
        self.assertEqual(self.h.executor.invocations, [])

        # The worker that holds the lease (the same run) can still use it.
        self.h.lease.answer = LeaseStatus.HELD
        used = await self.h.runner.run(
            make_call("repo.delete_tree", DELETE), approval_id=opened.approval_id
        )
        self.assertEqual(used.status, ExecutionStatus.COMPLETED)

    async def test_the_charge_is_made_for_the_run_the_call_ran_for(self):
        retried = TaskRun(1, 1)
        await self.run_call()
        await self.run_call(context=make_context(run=retried))
        self.assertEqual(self.h.budget.charges, [(TASK, "repo.read_file")] * 2)
        self.assertEqual(self.h.budget.charged_runs, [RUN, retried])


class LeaseWiringTest(unittest.IsolatedAsyncioTestCase):
    def build(self, **overrides):
        sink = InMemoryAuditSink()
        arguments = {
            "registry": sample_registry(),
            "authorizer": Authorizer(sink),
            "approvals": InMemoryApprovalStore(),
            "audit": sink,
        }
        arguments.update(overrides)
        return ToolBroker(**arguments)

    async def test_a_broker_without_a_lease_verifier_refuses_every_call(self):
        h = Harness(lease=None)  # fail closed: no verifier installed
        outcome = await h.runner.run(make_call("free.ping", {}))
        decision = outcome.decision
        self.assertEqual(h.executor.invocations, [])
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.LEASE_UNAVAILABLE)
        )

    def test_the_lease_verifier_is_validated_up_front(self):
        class NoCheck:
            pass

        class SyncCheck:
            def check(self, task_id, lease):
                return LeaseStatus.HELD

        class OneArgument:
            async def check(self, lease):
                return LeaseStatus.HELD

        for verifier in (NoCheck(), SyncCheck(), OneArgument()):
            with self.subTest(verifier=type(verifier).__name__):
                with self.assertRaises(TypeError):
                    self.build(lease=verifier)
        self.assertIsInstance(self.build(lease=FakeLease()), ToolBroker)

    def test_a_budget_charge_must_take_the_run(self):
        class OldCharge:
            async def check(self, task_id, tool):
                return None

            async def charge(self, task_id, tool):
                return None

        with self.assertRaises(TypeError):
            self.build(budget=OldCharge())

    def test_a_task_context_must_carry_a_lease(self):
        for lease in (None, (1, "w1", 1), {"entry_id": 1}, "w1"):
            with self.subTest(lease=lease):
                with self.assertRaises(TypeError):
                    make_context(lease=lease)
        context = make_context()
        self.assertEqual(context.lease, LEASE)
        with self.assertRaises(TypeError):  # the lease cannot be left out
            TaskContext(
                task_id=context.task_id,
                delegator_id=context.delegator_id,
                grant=context.grant,
                scope=context.scope,
                primary_project_id=context.primary_project_id,
                run=context.run,
            )


if __name__ == "__main__":
    unittest.main()
