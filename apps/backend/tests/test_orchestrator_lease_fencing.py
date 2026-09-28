"""Tool calls are fenced by the queue lease, and charged to their run (issue #126,
Decision 0046). Real PostgreSQL, the real Tool Broker behind a node's tools.

Before #126 the only check between a node and the Broker was the task's run
(``RunGuard``): a take-over keeps the run the same, so a worker whose lease had
run out (or been claimed by another worker) could still hand calls to the Broker
until its next heartbeat noticed the loss (audit B10 of PR #106). Now the
``TaskContext`` carries the worker's lease and the Broker asks the queue about it
for every call.

Deterministic: the orchestrator's clock is a ``ManualClock`` that the tests never
move, so no heartbeat runs and nothing but the Broker's check can notice that the
lease is gone; a node waits on an ``asyncio.Event`` at the point the test takes
the lease away. Nothing sleeps.
"""

import asyncio
import unittest
import uuid
from types import SimpleNamespace

from paw_backend.authz import (
    Authorizer,
    InMemoryAuditSink,
    ProjectRole,
    SystemRole,
)
from paw_backend.orchestrator.domain import RunOutcome
from paw_backend.orchestrator.errors import NodeStopped, StopReason
from paw_backend.orchestrator.gateway import (
    NodeToolGateway,
    QueueLeaseVerifier,
    RunGuard,
    TrackerBudgetProvider,
)
from paw_backend.tasks import TaskCommand, TaskRun
from paw_backend.tasks.domain import RepoRole
from paw_backend.tasks.queueing import BudgetKind, BudgetTracker, QueueLease, TaskQueue
from paw_backend.tasks.records import WorkingSetEntry
from paw_backend.tools import (
    ApprovalService,
    BrokerDecision,
    BrokerReason,
    ExecutionStatus,
    LexicalPathResolver,
    PostgresApprovalStore,
    PostgresTaskActivity,
    TaskActivity,
    ToolBroker,
    ToolOutcome,
    ToolRegistry,
    ToolRunner,
    Verdict,
)

from .authz_support import StaticDirectory, principal
from .gate_support import ALWAYS_ACTIVE
from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    make_plan,
    node,
    ok,
    requires_postgres,
    until,
)
from .task_support import BASELINE
from .test_orchestrator_tools import READ, Authority, RestoringGate
from .tools_support import REPO, ROOT, FakeExecutor, make_context, sample_specs

Out = RunOutcome
DELETE = {"path": f"{ROOT}/build"}


class GatedExecutor(FakeExecutor):
    """An executor whose calls wait for ``release`` once ``hold`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.hold = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, invocation):
        if self.hold:
            self.entered.set()
            await self.release.wait()
        return await super().execute(invocation)


class LeaseFencingTestCase(PostgresOrchestratorTestCase):
    # The task's Working Set holds the repository of the tools' scope (#85), as
    # in test_orchestrator_tools.
    working_set = (WorkingSetEntry(REPO, RepoRole.TARGET, BASELINE),)

    async def create_task(self, service=None, **overrides):
        overrides.setdefault("repositories", list(self.working_set))
        return await super().create_task(service, **overrides)

    def stack(self, executor=None):
        """The Broker and what it needs, on one database (shared by the workers
        a test builds with ``worker``)."""
        database = self.new_database()
        sink = InMemoryAuditSink()
        directory = StaticDirectory(
            principal(
                SystemRole.USER,
                self.user_id,
                {self.project_id: ProjectRole.CONTRIBUTOR},
            )
        )
        store = PostgresApprovalStore(database)
        tracker = BudgetTracker(database)
        queue = TaskQueue(database, project_gate=ALWAYS_ACTIVE)
        executor = executor or FakeExecutor()
        gate = RestoringGate()
        broker = ToolBroker(
            ToolRegistry(sample_specs()),
            Authorizer(sink, directory=directory),
            store,
            sink,
            budget=TrackerBudgetProvider(tracker),
            task_activity=PostgresTaskActivity(database),
            lease=QueueLeaseVerifier(queue),
            path_resolver=LexicalPathResolver(),
            use_gate=gate,
        )
        return SimpleNamespace(
            gate=gate,
            sink=sink,
            store=store,
            tracker=tracker,
            queue=queue,
            executor=executor,
            runner=ToolRunner(broker, executor),
            approvals=ApprovalService(store, sink),
        )

    def worker(self, stack, runtime):
        h = self.harness(
            runtimes={"local": runtime},
            tools=stack.runner,
            authority=Authority(self.project_id),
            budget=stack.tracker,
            queue=stack.queue,
            task_listeners=[stack.approvals.revoke_on_task_end],
            config={"poll_seconds": 3600.0},
        )
        # The Working Set's use gate is a task service's (any worker's will do).
        stack.gate.service = stack.gate.service or h.tasks
        return h

    def two_calls(self, seen, gate, second=("repo.read_file", READ)):
        """A node that calls a tool, waits for ``gate``, then calls again. Every
        outcome (or the reason it was stopped) is appended to ``seen``."""

        async def behave(assignment):
            seen.append(await assignment.tools.call("repo.read_file", READ))
            await gate.wait()
            try:
                seen.append(await assignment.tools.call(*second))
            except NodeStopped as stop:
                seen.append(stop.reason)
                raise
            return ok("called twice")

        return behave

    def tool_rows(self, stack):
        return [
            (e.decision, e.reason)
            for e in stack.sink.events
            if e.action.startswith("tool.") and not e.action.startswith("tool.approval")
        ]

    async def entry(self):
        (row,) = await self.rows(
            "SELECT status, claimed_by, claim_count FROM queue_entries"
        )
        return row["status"], row["claimed_by"], row["claim_count"]

    async def tool_calls_used(self, stack, task_id) -> int:
        usage = {u.kind: u.consumed for u in await stack.tracker.usage(task_id)}
        return usage[BudgetKind.TOOL_CALLS]

    async def expire_the_lease(self) -> None:
        await self.owner_sql(
            "UPDATE queue_entries SET claimed_at = now() - interval '10 seconds',"
            " lease_expires_at = now() - interval '5 seconds' WHERE status = 'claimed'"
        )


@requires_postgres
class ToolCallAfterLostLeaseTest(LeaseFencingTestCase):
    async def lose_the_lease_between_two_calls(self, lose, second=None):
        """Worker w1 runs a node that calls a tool; ``lose()`` takes its lease
        away; then the node calls again. Returns ``(stack, task_id, seen, report)``."""
        stack = self.stack()
        seen, gate = [], asyncio.Event()
        kwargs = {} if second is None else {"second": second}
        runtime = FakeRuntime(
            "local", script={"impl": [self.two_calls(seen, gate, **kwargs)]}
        )
        h = self.worker(stack, runtime)
        task_id = await self.prepare(h, make_plan(node("impl")))
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(seen) == 1, message="the first call")
        self.assertEqual(seen[0].status, ExecutionStatus.COMPLETED)

        await lose(h)
        gate.set()
        report = await asyncio.wait_for(running, 120)
        return stack, task_id, seen, report

    async def assert_refused_as_stale(self, stack, task_id, seen, report):
        # The second call never reached the executor: the node was stopped for
        # the lost lease, and so was the run (it neither completed nor released
        # the entry: that is its new holder's, or its lease's).
        self.assertEqual(seen[1], StopReason.LEASE_LOST)
        (invocation,) = stack.executor.invocations
        self.assertEqual(invocation.context.lease.worker_id, "w1")
        self.assertEqual(report.outcome, Out.LEASE_LOST)
        self.assertEqual(
            self.tool_rows(stack),
            [("allow", "auto"), ("allow", "executed"), ("deny", "lease_lost")],
        )
        # Only the call that ran was charged.
        self.assertEqual(await self.tool_calls_used(stack, task_id), 1)

    async def test_a_call_after_another_worker_took_the_entry_over_is_refused(self):
        async def take_over(h):
            await self.expire_the_lease()
            replacement = await h.queue.claim_next("w2")
            self.assertEqual(replacement.claim_count, 2)

        result = await self.lose_the_lease_between_two_calls(take_over)
        await self.assert_refused_as_stale(*result)
        self.assertEqual(await self.entry(), ("claimed", "w2", 2))

    async def test_a_stale_claim_of_the_same_worker_id_is_refused(self):
        # A restarted worker with a stable id claims the entry again: the worker id
        # matches, only the claim generation (the fencing token) does not.
        async def reclaim_by_the_same_id(h):
            await self.expire_the_lease()
            again = await h.queue.claim_next("w1")
            self.assertEqual(again.claim_count, 2)

        result = await self.lose_the_lease_between_two_calls(reclaim_by_the_same_id)
        await self.assert_refused_as_stale(*result)
        self.assertEqual(await self.entry(), ("claimed", "w1", 2))

    async def test_a_call_after_the_lease_ran_out_is_refused_before_any_take_over(
        self,
    ):
        async def run_out(h):
            await self.expire_the_lease()

        result = await self.lose_the_lease_between_two_calls(run_out)
        await self.assert_refused_as_stale(*result)
        self.assertEqual(await self.entry(), ("claimed", "w1", 1))

    async def test_a_worker_that_lost_its_lease_asks_no_human_for_an_approval(self):
        async def take_over(h):
            await self.expire_the_lease()
            await h.queue.claim_next("w2")

        stack, task_id, seen, report = await self.lose_the_lease_between_two_calls(
            take_over, second=("repo.delete_tree", DELETE)
        )
        self.assertEqual(seen[1], StopReason.LEASE_LOST)
        self.assertEqual(report.outcome, Out.LEASE_LOST)
        self.assertEqual(
            await self.rows(
                "SELECT id FROM tool_approvals WHERE task_id = :t", t=task_id
            ),
            [],
        )
        self.assertEqual(self.tool_rows(stack)[-1], ("deny", "lease_lost"))

    async def test_a_runtime_that_swallows_the_stop_cannot_settle_its_node(self):
        # A runtime that catches every tool error (NodeStopped included) and still
        # returns an outcome. The lease only ran out, so nobody took the DAG over
        # and its epoch is still this worker's: without a look at the guard the
        # stale worker would complete the node after the Broker told it the lease
        # was gone (Codex review of PR #144).
        stack = self.stack()
        seen, gate = [], asyncio.Event()

        async def swallow(assignment):
            seen.append(await assignment.tools.call("repo.read_file", READ))
            await gate.wait()
            try:
                await assignment.tools.call("repo.read_file", READ)
            except NodeStopped as stop:
                seen.append(stop.reason)
            return ok("done anyway")

        runtime = FakeRuntime("local", script={"impl": [swallow]})
        h = self.worker(stack, runtime)
        task_id = await self.prepare(h, make_plan(node("impl")))
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(seen) == 1, message="the first call")

        await self.expire_the_lease()
        gate.set()
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(seen[1], StopReason.LEASE_LOST)
        self.assertEqual(report.outcome, Out.LEASE_LOST)
        self.assertNotEqual((await self.states_of(task_id))["impl"], "succeeded")
        self.assertEqual(await self.entry(), ("claimed", "w1", 1))

    async def test_the_context_carries_the_claim_of_the_worker(self):
        stack = self.stack()
        seen, gate = [], asyncio.Event()
        gate.set()
        runtime = FakeRuntime("local", script={"impl": [self.two_calls(seen, gate)]})
        h = self.worker(stack, runtime)
        await self.prepare(h, make_plan(node("impl")))
        entry = await h.queue.claim_next("w1")

        report = await h.orchestrator.run_entry(entry, "w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(
            {invocation.context.lease for invocation in stack.executor.invocations},
            {QueueLease(entry.id, "w1", entry.claim_count)},
        )


@requires_postgres
class TakeOverConcurrencyTest(LeaseFencingTestCase):
    async def test_the_replacement_calls_tools_while_the_stale_worker_is_refused(self):
        """Two workers of one task at once: A (stale, its node still running) and
        B (the new lease holder, which takes the DAG over and runs the node again).
        B's calls run; A's call after the take-over is refused; each call is
        charged once to the task's run."""
        stack = self.stack()
        seen_a, gate_a = [], asyncio.Event()
        runtime_a = FakeRuntime(
            "local", script={"impl": [self.two_calls(seen_a, gate_a)]}
        )
        a = self.worker(stack, runtime_a)
        task_id = await self.prepare(a, make_plan(node("impl")))
        run_a = asyncio.create_task(a.orchestrator.run_once("wA"))
        await until(lambda: len(seen_a) == 1, message="A's first call")

        # A's lease runs out and B claims the entry and takes the run over.
        await self.expire_the_lease()
        seen_b, gate_b = [], asyncio.Event()
        runtime_b = FakeRuntime(
            "local", script={"impl": [self.two_calls(seen_b, gate_b)]}
        )
        b = self.worker(stack, runtime_b)
        entry_b = await b.queue.claim_next("wB")
        self.assertEqual((entry_b.claimed_by, entry_b.claim_count), ("wB", 2))
        run_b = asyncio.create_task(b.orchestrator.run_entry(entry_b, "wB"))
        await until(lambda: len(seen_b) == 1, message="B's first call")
        self.assertEqual(seen_b[0].status, ExecutionStatus.COMPLETED)

        # Now A's node calls again, while B holds the lease: refused.
        gate_a.set()
        report_a = await asyncio.wait_for(run_a, 120)
        self.assertEqual(seen_a[1], StopReason.LEASE_LOST)
        self.assertEqual(report_a.outcome, Out.LEASE_LOST)

        # B goes on unaffected and finishes the DAG.
        gate_b.set()
        report_b = await asyncio.wait_for(run_b, 120)
        self.assertEqual(seen_b[1].status, ExecutionStatus.COMPLETED)
        self.assertEqual(report_b.outcome, Out.DAG_SUCCEEDED)

        leases = [
            (i.context.lease.worker_id, i.context.lease.claim_count)
            for i in stack.executor.invocations
        ]
        self.assertEqual(leases, [("wA", 1), ("wB", 2), ("wB", 2)])
        self.assertEqual(await self.tool_calls_used(stack, task_id), 3)
        self.assertEqual(await self.entry(), ("completed", "wB", 2))


@requires_postgres
class ToolCallChargedToItsRunTest(LeaseFencingTestCase):
    async def run_a_call_across(self, *commands):
        """A call is executing when ``commands`` replace (or end) its run. Returns
        ``(stack, task_id, outcome, running)`` once the call has been accounted."""
        executor = GatedExecutor()
        stack = self.stack(executor)
        seen = []

        async def one_call(assignment):
            seen.append(await assignment.tools.call("repo.read_file", READ))
            return ok("called")

        runtime = FakeRuntime("local", script={"impl": [one_call]})
        h = self.worker(stack, runtime)
        task_id = await self.prepare(h, make_plan(node("impl")))
        executor.hold = True
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await asyncio.wait_for(executor.entered.wait(), 60)

        other = self.harness()
        for command, actor in commands:
            await other.tasks.execute(task_id, command, actor=actor)
        executor.release.set()
        await until(lambda: len(seen) == 1, message="the accounted call")
        return stack, task_id, seen[0], running

    async def finish(self, running):
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)

    async def test_a_call_whose_run_was_replaced_is_not_charged_to_the_new_run(
        self,
    ):
        stack, task_id, outcome, running = await self.run_a_call_across(
            (TaskCommand.FAIL, self.system), (TaskCommand.RETRY, self.user)
        )
        try:
            # The call ran (and is audited as executed) ...
            self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
            self.assertEqual(self.tool_rows(stack)[-1], ("allow", "executed"))
            (invocation,) = stack.executor.invocations
            self.assertEqual(invocation.context.run, TaskRun(1, 0))
            # ... but the run it ran for is gone: nothing of the run that took
            # over (1.1) is spent on it.
            self.assertEqual(
                (await self.harness().tasks.restore(task_id)).run, TaskRun(1, 1)
            )
            self.assertEqual(await self.tool_calls_used(stack, task_id), 0)
        finally:
            await self.finish(running)

    async def test_a_call_whose_task_ended_meanwhile_is_not_charged(self):
        stack, task_id, outcome, running = await self.run_a_call_across(
            (TaskCommand.FAIL, self.system)
        )
        try:
            self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
            self.assertEqual(await self.tool_calls_used(stack, task_id), 0)
        finally:
            await self.finish(running)

    async def test_a_call_of_the_current_run_is_charged_to_it(self):
        stack, task_id, outcome, running = await self.run_a_call_across()
        try:
            self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
            self.assertEqual(await self.tool_calls_used(stack, task_id), 1)
        finally:
            await self.finish(running)


class GatewayLeaseRefusalTest(unittest.IsolatedAsyncioTestCase):
    """``NodeToolGateway``: a refusal for a LOST lease stops the whole run; one
    for a lease that could not be read refuses only that call."""

    def gateway(self, reason):
        class Active:
            async def check(self, task_id, run):
                return TaskActivity.ACTIVE

        class Refusing:
            async def run(self, call, *, approval_id=None):
                decision = BrokerDecision(Verdict.DENY, reason, uuid.uuid4())
                return ToolOutcome(decision, ExecutionStatus.NOT_EXECUTED)

        context = make_context()
        guard = RunGuard(context.task_id, context.run, Active())

        async def factory():
            return context

        return guard, NodeToolGateway(guard, Refusing(), factory)

    async def test_a_lost_lease_stops_the_run(self):
        guard, gateway = self.gateway(BrokerReason.LEASE_LOST)
        with self.assertRaises(NodeStopped) as caught:
            await gateway.call("repo.read_file", READ)
        self.assertEqual(caught.exception.reason, StopReason.LEASE_LOST)
        self.assertEqual(guard.stop_reason, StopReason.LEASE_LOST)
        with self.assertRaises(NodeStopped):  # no other call of the run is handed on
            await gateway.call("repo.read_file", READ)

    async def test_an_unreadable_lease_refuses_only_the_call(self):
        guard, gateway = self.gateway(BrokerReason.LEASE_UNAVAILABLE)
        outcome = await gateway.call("repo.read_file", READ)
        self.assertEqual(outcome.decision.reason, BrokerReason.LEASE_UNAVAILABLE)
        self.assertIsNone(guard.stop_reason)


if __name__ == "__main__":
    unittest.main()
