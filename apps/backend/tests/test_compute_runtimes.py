"""What plugs the scheduler into the rest of the backend (PAW-036):

* ``HybridRuntime``: an orchestrator ``AgentRuntime`` that runs a node on the
  local model when the scheduler admits it, and on a cloud agent when the local
  GPU is busy and the injected policy allows it (Local / Cloud hybrid);
* ``ScheduledMemoryWorker``: a Memory Worker that raises
  ``WorkerUnavailableError`` when the scheduler has no room for it (or unloaded
  it), so the journal defers the job instead of failing it (Decision 0018);
* ``PlacedEmbedder``: an Embedder that uses the GPU or the CPU copy of the model,
  as the scheduler placed it (CPU fallback).
"""

import asyncio
import unittest
import uuid
from unittest import mock

from paw_backend.compute import (
    ComputeRequest,
    ComputeUnavailableError,
    DeploymentState,
    HybridRuntime,
    PlacedEmbedder,
    Placement,
    ResourceClass,
    ScheduledMemoryWorker,
    TrackerLateGpuCharge,
    estimate_context_tokens,
)
from paw_backend.compute import runtimes as runtimes_module
from paw_backend.memory.journal import WorkerUnavailableError
from paw_backend.orchestrator import (
    ExecutionPlacement,
    InvalidOrchestratorArgumentError,
    NodeOutcome,
    NodeResult,
    NodeRole,
)
from paw_backend.orchestrator.errors import NodeStopped, StopReason
from paw_backend.orchestrator.runtime import NodeAssignment, validate_runtime
from paw_backend.tasks.queueing import BudgetKind
from tests.compute_support import (
    GIB,
    build,
    embedding_spec,
    main_spec,
    memory_spec,
    settle,
)

IC = ResourceClass.INTERACTIVE


class FakeBudget:
    def __init__(self, gpu_seconds_left=None) -> None:
        self.charges: list[tuple[BudgetKind, int]] = []
        self.gpu_seconds_left = gpu_seconds_left

    async def charge(self, kind, amount):
        self.charges.append((kind, amount))

    async def remaining(self):
        if self.gpu_seconds_left is None:
            return {}
        return {BudgetKind.GPU_SECONDS: self.gpu_seconds_left}


class FakeTools:
    async def call(self, tool, arguments, *, approval_id=None):
        raise AssertionError("not used")


class FakePlacement:
    """Records the placements a runtime reports (the orchestrator writes them to
    the attempt row and, for the cloud, to ``audit_events``)."""

    def __init__(self, error=None) -> None:
        self.records: list[tuple[ExecutionPlacement, str, str]] = []
        self.error = error

    async def record(self, placement, *, agent, model):
        if self.error is not None:
            raise self.error
        self.records.append((placement, agent, model))


def assignment(goal="Fix the bug", **overrides):
    values = dict(
        task_id=uuid.uuid4(),
        node_key="work",
        role=NodeRole.WORKER,
        title="Work",
        goal=goal,
        input={"files": ["a.py"]},
        upstream={},
        agent="local",
        attempt=1,
        approach=0,
        tools=FakeTools(),
        budget=FakeBudget(),
        placement=FakePlacement(),
    )
    values.update(overrides)
    return NodeAssignment(**values)


class Recorder:
    """A runtime that records the assignments it was given."""

    def __init__(self, name, clock=None, seconds=0.0, error=None):
        self.name = name
        self.clock = clock
        self.seconds = seconds
        self.error = error
        self.calls = []

    async def run_node(self, assignment):
        self.calls.append(assignment.node_key)
        if self.clock is not None and self.seconds:
            await self.clock.sleep(self.seconds)
        if self.error is not None:
            raise self.error
        return NodeOutcome.succeeded(NodeResult(summary=f"done by {self.name}"))


class Policy:
    def __init__(self, allows=True):
        self.allows_value = allows
        self.asked = 0

    async def allows(self, assignment):
        self.asked += 1
        return self.allows_value


def fill_main(scheduler):
    """Take the whole pool of the main model."""

    async def fill():
        leases = []
        for _ in range(3):
            admission = await scheduler.try_acquire(
                ComputeRequest(IC, deployment="main", context_tokens=35_000)
            )
            leases.append(admission.lease)
        return leases

    return fill()


class EstimateTest(unittest.TestCase):
    def test_the_estimate_grows_with_the_input_and_keeps_an_output_reserve(self):
        small = estimate_context_tokens(assignment())
        large = estimate_context_tokens(assignment(goal="x" * 30_000))
        self.assertGreaterEqual(small, 8_192)
        self.assertGreaterEqual(large - small, 9_000)
        self.assertEqual(
            estimate_context_tokens(assignment(), output_tokens=0) + 8_192, small
        )

    def test_upstream_results_count(self):
        upstream = {"a": NodeResult(summary="y" * 1_500)}
        self.assertGreater(
            estimate_context_tokens(assignment(upstream=upstream)),
            estimate_context_tokens(assignment()),
        )


class HybridRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler, self.probe, self.control, self.clock = build()
        await self.scheduler.refresh()
        self.local = Recorder("local", self.clock, seconds=90.5)
        self.cloud = Recorder("cloud")
        self.policy = Policy()

    def runtime(self, **options):
        values = dict(
            deployment="main",
            cloud=self.cloud,
            cloud_policy=self.policy,
            cloud_agent="codex",
            cloud_model="gpt-5-codex",
            clock=self.clock,
            wait_seconds=120,
        )
        values.update(options)
        return HybridRuntime(self.scheduler, self.local, **values)

    async def test_it_is_a_runtime_the_orchestrator_accepts(self):
        validate_runtime(self.runtime(), "local")

    async def test_local_first_and_the_gpu_time_is_charged(self):
        work = assignment()
        task = asyncio.create_task(self.runtime().run_node(work))
        await settle()
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 1)
        await self.clock.advance(90.5)
        outcome = await task
        self.assertEqual(outcome.result.summary, "done by local")
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(work.budget.charges, [(BudgetKind.GPU_SECONDS, 91)])
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)

    async def test_a_busy_gpu_sends_the_node_to_the_cloud(self):
        await fill_main(self.scheduler)
        work = assignment(goal="x" * 90_000)  # ~30,000 tokens + the output reserve
        outcome = await self.runtime().run_node(work)
        self.assertEqual(outcome.result.summary, "done by cloud")
        self.assertEqual(self.local.calls, [])
        self.assertEqual(work.budget.charges, [])  # no local GPU time
        self.assertEqual(self.scheduler.status().cloud_leases, 0)

    async def test_the_cloud_is_used_only_when_the_policy_allows(self):
        await fill_main(self.scheduler)
        self.policy.allows_value = False
        task = asyncio.create_task(
            self.runtime().run_node(assignment(goal="x" * 90_000))
        )
        await settle()
        await self.clock.advance(120)
        outcome = await task
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_class, "ComputeUnavailable")
        self.assertTrue(outcome.retryable)
        self.assertEqual(self.cloud.calls, [])

    async def test_without_a_cloud_runtime_the_node_waits_for_local(self):
        held = await fill_main(self.scheduler)
        runtime = self.runtime(cloud=None, cloud_policy=None)
        task = asyncio.create_task(runtime.run_node(assignment(goal="x" * 90_000)))
        await settle()
        self.assertFalse(task.done())
        self.assertEqual(self.policy.asked, 0)
        await held[0].release()
        await settle()
        await self.clock.advance(90.5)
        self.assertEqual((await task).result.summary, "done by local")

    async def test_a_cloud_runtime_needs_a_policy(self):
        with self.assertRaises(TypeError):
            HybridRuntime(
                self.scheduler, self.local, deployment="main", cloud=self.cloud
            )
        with self.assertRaises(TypeError):
            HybridRuntime(self.scheduler, object(), deployment="main")

    async def test_the_lease_is_released_when_the_local_runtime_fails(self):
        self.local.error = RuntimeError("boom")
        self.local.seconds = 0
        with self.assertRaises(RuntimeError):
            await self.runtime().run_node(assignment())
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)

    async def test_gpu_time_is_charged_when_the_local_runtime_raises(self):
        self.local.error = RuntimeError("boom")
        work = assignment()
        task = asyncio.create_task(self.runtime().run_node(work))
        await settle()
        await self.clock.advance(90.5)
        with self.assertRaises(RuntimeError):
            await task
        self.assertEqual(work.budget.charges, [(BudgetKind.GPU_SECONDS, 91)])
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)

    async def test_gpu_time_is_charged_when_the_node_is_cancelled(self):
        work = assignment()
        task = asyncio.create_task(self.runtime().run_node(work))
        await settle()
        await self.clock.advance(30)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(work.budget.charges, [(BudgetKind.GPU_SECONDS, 30)])
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)

    async def test_the_runtimes_error_wins_over_a_charge_that_stops_the_node(self):
        self.local.error = RuntimeError("boom")

        async def charge(kind, amount):
            work.budget.charges.append((kind, amount))
            raise NodeStopped(StopReason.TASK_ENDED)

        work = assignment()
        work.budget.charge = charge
        task = asyncio.create_task(self.runtime().run_node(work))
        await settle()
        await self.clock.advance(90.5)
        with self.assertRaises(RuntimeError):
            await task
        self.assertEqual(work.budget.charges, [(BudgetKind.GPU_SECONDS, 91)])

    async def test_node_stopped_passes_through(self):
        self.local.error = NodeStopped(StopReason.TASK_ENDED)
        self.local.seconds = 0
        work = assignment()
        with self.assertRaises(NodeStopped):
            await self.runtime().run_node(work)
        self.assertEqual(work.budget.charges, [])
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)

    async def test_no_gpu_time_left_stops_the_node_before_the_local_runtime(self):
        work = assignment(budget=FakeBudget(gpu_seconds_left=0))
        with self.assertRaises(NodeStopped) as caught:
            await self.runtime().run_node(work)
        self.assertIs(caught.exception.reason, StopReason.BUDGET_EXCEEDED)
        self.assertEqual(self.local.calls, [])
        self.assertEqual(work.budget.charges, [])
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)

    async def test_the_local_runtime_is_bounded_by_the_gpu_time_left(self):
        # 60 s left, the local runtime would take 90.5 s: it is cancelled at 60 s,
        # 60 s are charged (not 91) and the node stops on the budget.
        work = assignment(budget=FakeBudget(gpu_seconds_left=60))
        task = asyncio.create_task(self.runtime().run_node(work))
        await settle()
        await self.clock.advance(60)
        await settle()
        with self.assertRaises(NodeStopped) as caught:
            await task
        self.assertIs(caught.exception.reason, StopReason.BUDGET_EXCEEDED)
        self.assertEqual(work.budget.charges, [(BudgetKind.GPU_SECONDS, 60)])
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)

    async def test_a_node_within_the_gpu_time_left_runs_as_before(self):
        work = assignment(budget=FakeBudget(gpu_seconds_left=100))
        task = asyncio.create_task(self.runtime().run_node(work))
        await settle()
        await self.clock.advance(90.5)
        outcome = await task
        self.assertEqual(outcome.result.summary, "done by local")
        self.assertEqual(work.budget.charges, [(BudgetKind.GPU_SECONDS, 91)])

    async def test_concurrent_nodes_of_a_task_share_the_gpu_time_left(self):
        # 60 s left and two nodes of the same task at once: the GPU time falls
        # twice as fast, both are stopped at 30 s (60 in all, not 120).
        task_id = uuid.uuid4()
        budget = FakeBudget(gpu_seconds_left=60)
        first = assignment(task_id=task_id, node_key="a", budget=budget)
        second = assignment(task_id=task_id, node_key="b", budget=budget)
        runtime = self.runtime()
        tasks = [
            asyncio.create_task(runtime.run_node(first)),
            asyncio.create_task(runtime.run_node(second)),
        ]
        await settle()
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 2)
        await self.clock.advance(30)
        await settle()
        for task in tasks:
            with self.assertRaises(NodeStopped):
                await task
        self.assertEqual(
            budget.charges,
            [(BudgetKind.GPU_SECONDS, 30), (BudgetKind.GPU_SECONDS, 30)],
        )
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)

    async def test_nodes_of_other_tasks_do_not_share_the_gpu_time(self):
        first = assignment(budget=FakeBudget(gpu_seconds_left=60))
        second = assignment(budget=FakeBudget(gpu_seconds_left=60))
        runtime = self.runtime()
        tasks = [
            asyncio.create_task(runtime.run_node(first)),
            asyncio.create_task(runtime.run_node(second)),
        ]
        await settle()
        await self.clock.advance(30)
        await settle()
        self.assertFalse(any(task.done() for task in tasks))
        await self.clock.advance(30)
        await settle()
        for task in tasks:
            with self.assertRaises(NodeStopped):
                await task
        self.assertEqual(first.budget.charges, [(BudgetKind.GPU_SECONDS, 60)])

    async def test_a_runtime_that_ignores_cancellation_keeps_its_lease(self):
        release = asyncio.Event()

        class Stubborn:
            async def run_node(self, assignment):
                while True:
                    try:
                        await release.wait()
                        return NodeOutcome.succeeded(NodeResult(summary="late"))
                    except asyncio.CancelledError:
                        continue

        runtime = HybridRuntime(
            self.scheduler, Stubborn(), deployment="main", clock=self.clock
        )
        work = assignment(budget=FakeBudget(gpu_seconds_left=10))
        with mock.patch.object(runtimes_module, "CANCEL_GRACE_SECONDS", 0.01):
            task = asyncio.create_task(runtime.run_node(work))
            await settle()
            await self.clock.advance(10)
            with self.assertRaises(NodeStopped):
                await task
        # The runtime still runs: its GPU capacity is not given away.
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 1)
        self.assertEqual(work.budget.charges, [(BudgetKind.GPU_SECONDS, 10)])
        await self.clock.advance(25)
        release.set()
        await settle()
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)
        # The GPU time it went on using is charged when it ends.
        self.assertEqual(
            work.budget.charges,
            [(BudgetKind.GPU_SECONDS, 10), (BudgetKind.GPU_SECONDS, 25)],
        )

    async def test_a_late_call_is_charged_through_the_late_charge(self):
        # The orchestrator closes the node's attempt once run_node returns: its
        # budget refuses later charges. The time the call went on using is
        # charged to the task through the late charge instead.
        release = asyncio.Event()

        class Stubborn:
            async def run_node(self, assignment):
                while True:
                    try:
                        await release.wait()
                        return NodeOutcome.succeeded(NodeResult(summary="late"))
                    except asyncio.CancelledError:
                        continue

        class ClosingBudget(FakeBudget):
            closed = False

            async def charge(self, kind, amount):
                if self.closed:
                    raise NodeStopped(StopReason.ABANDONED)
                await super().charge(kind, amount)

        class Late:
            def __init__(self):
                self.charges = []

            async def charge(self, task_id, seconds):
                self.charges.append((task_id, seconds))

        late = Late()
        runtime = HybridRuntime(
            self.scheduler,
            Stubborn(),
            deployment="main",
            late_gpu_charge=late,
            clock=self.clock,
        )
        budget = ClosingBudget(gpu_seconds_left=10)
        work = assignment(budget=budget)
        try:
            with mock.patch.object(runtimes_module, "CANCEL_GRACE_SECONDS", 0.01):
                task = asyncio.create_task(runtime.run_node(work))
                await settle()
                await self.clock.advance(10)
                with self.assertRaises(NodeStopped):
                    await task
            budget.closed = True  # the attempt is closed
            await self.clock.advance(25)
        finally:
            release.set()
            await settle()
        self.assertEqual(budget.charges, [(BudgetKind.GPU_SECONDS, 10)])
        self.assertEqual(late.charges, [(work.task_id, 25)])
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)

    async def test_the_tracker_late_charge_records_gpu_seconds(self):
        class Tracker:
            def __init__(self):
                self.records = []

            async def record(self, task_id, kind, amount, *, run=None):
                self.records.append((task_id, kind, amount, run))

        tracker = Tracker()
        task_id = uuid.uuid4()
        await TrackerLateGpuCharge(tracker).charge(task_id, 7)
        self.assertEqual(tracker.records, [(task_id, BudgetKind.GPU_SECONDS, 7, None)])
        with self.assertRaises(TypeError):
            TrackerLateGpuCharge(object())
        with self.assertRaises(TypeError):
            HybridRuntime(
                self.scheduler, self.local, deployment="main", late_gpu_charge=object()
            )

    async def test_a_second_cancel_while_the_charge_is_written_keeps_it(self):
        stored = asyncio.Event()
        writing = asyncio.Event()

        class SlowBudget(FakeBudget):
            async def charge(self, kind, amount):
                writing.set()
                await stored.wait()
                await super().charge(kind, amount)

        class Slow:
            async def run_node(self, assignment):
                await asyncio.Event().wait()

        runtime = HybridRuntime(
            self.scheduler, Slow(), deployment="main", clock=self.clock
        )
        budget = SlowBudget(gpu_seconds_left=100)
        work = assignment(budget=budget)
        task = asyncio.create_task(runtime.run_node(work))
        await settle()
        await self.clock.advance(12)
        task.cancel()  # the local call stops; its time is being charged
        await writing.wait()
        task.cancel()  # a second one, while the charge is written
        with self.assertRaises(asyncio.CancelledError):
            await task
        stored.set()
        await settle()
        self.assertEqual(budget.charges, [(BudgetKind.GPU_SECONDS, 12)])
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)

    async def test_a_second_cancel_during_the_grace_wait_keeps_the_lease(self):
        release = asyncio.Event()

        class Stubborn:
            async def run_node(self, assignment):
                while True:
                    try:
                        await release.wait()
                        return NodeOutcome.succeeded(NodeResult(summary="late"))
                    except asyncio.CancelledError:
                        continue

        runtime = HybridRuntime(
            self.scheduler, Stubborn(), deployment="main", clock=self.clock
        )
        work = assignment(budget=FakeBudget(gpu_seconds_left=100))
        task = asyncio.create_task(runtime.run_node(work))
        await settle()
        task.cancel()  # the first cancel: the grace wait starts
        await settle()
        self.assertFalse(task.done())
        task.cancel()  # a second one, during the grace wait
        try:
            with self.assertRaises(asyncio.CancelledError):
                await task
            # The runtime still runs: its GPU capacity is not given away.
            self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 1)
        finally:
            release.set()  # the stubborn runtime ends (the test never hangs)
            await settle()
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)

    async def test_a_revoked_background_lease_stops_the_local_work(self):
        # VRAM pressure revokes Background leases (the first relief step): the
        # node's local call is stopped, its time charged, the lease given back.
        runtime = self.runtime(
            resource_class=ResourceClass.BACKGROUND, cloud=None, cloud_policy=None
        )
        work = assignment()
        task = asyncio.create_task(runtime.run_node(work))
        await settle()
        self.assertEqual(self.scheduler.status().leases[ResourceClass.BACKGROUND], 1)
        await self.clock.advance(20)
        self.probe.external = 12 * GIB
        await self.scheduler.refresh()
        await settle()
        outcome = await task
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_class, "ComputeUnavailable")
        self.assertTrue(outcome.retryable)
        self.assertEqual(work.budget.charges, [(BudgetKind.GPU_SECONDS, 20)])
        self.assertEqual(self.scheduler.status().leases[ResourceClass.BACKGROUND], 0)

    async def test_the_cloud_policy_is_asked_again_before_the_cloud_runs(self):
        await fill_main(self.scheduler)
        answers = iter([True, False])

        async def allows(assignment):
            return next(answers)

        self.policy.allows = allows
        outcome = await self.runtime().run_node(assignment(goal="x" * 90_000))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_class, "ComputeUnavailable")
        self.assertEqual(self.cloud.calls, [])

    async def test_a_failing_cloud_policy_keeps_the_node_local(self):
        async def allows(assignment):
            raise RuntimeError("policy backend down")

        self.policy.allows = allows
        work = assignment()
        task = asyncio.create_task(self.runtime().run_node(work))
        await settle()
        await self.clock.advance(90.5)
        self.assertEqual((await task).result.summary, "done by local")

    async def test_a_charge_in_flight_is_not_counted_twice_by_a_new_node(self):
        # 100 s left. Node a runs 40 s and charges them; while its charge is
        # visible but not finished, node b of the same task starts. b must have
        # the 60 s that are left, not 60 - 40.
        gate = asyncio.Event()
        charged = asyncio.Event()

        class Ledger(FakeBudget):
            async def charge(self, kind, amount):
                self.charges.append((kind, amount))
                self.gpu_seconds_left -= amount
                charged.set()
                await gate.wait()

        task_id = uuid.uuid4()
        budget = Ledger(gpu_seconds_left=100)
        quick = Recorder("local", self.clock, seconds=40)
        slow = Recorder("local", self.clock, seconds=1_000)
        first = HybridRuntime(
            self.scheduler, quick, deployment="main", clock=self.clock
        )
        second = HybridRuntime(
            self.scheduler, slow, deployment="main", clock=self.clock
        )
        a = asyncio.create_task(
            first.run_node(assignment(task_id=task_id, node_key="a", budget=budget))
        )
        await settle()
        await self.clock.advance(40)
        await settle()
        self.assertTrue(charged.is_set())
        await self.scheduler.refresh()  # a fresh sample after the 40 s
        b = asyncio.create_task(
            second.run_node(assignment(task_id=task_id, node_key="b", budget=budget))
        )
        await settle()
        gate.set()
        await settle()
        self.assertTrue((await a).ok)
        await self.clock.advance(59)
        await settle()
        self.assertFalse(b.done())
        await self.clock.advance(1)
        await settle()
        with self.assertRaises(NodeStopped):
            await b
        self.assertEqual(budget.charges[-1], (BudgetKind.GPU_SECONDS, 60))

    async def test_a_bounded_node_that_is_cancelled_is_charged_and_released(self):
        work = assignment(budget=FakeBudget(gpu_seconds_left=100))
        task = asyncio.create_task(self.runtime().run_node(work))
        await settle()
        await self.clock.advance(30)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(work.budget.charges, [(BudgetKind.GPU_SECONDS, 30)])
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)

    async def test_the_cloud_does_not_need_gpu_time(self):
        await fill_main(self.scheduler)
        work = assignment(goal="x" * 90_000, budget=FakeBudget(gpu_seconds_left=0))
        outcome = await self.runtime().run_node(work)
        self.assertEqual(outcome.result.summary, "done by cloud")
        self.assertEqual(work.budget.charges, [])

    async def test_without_charging_the_gpu_time_is_not_bounded(self):
        work = assignment(budget=FakeBudget(gpu_seconds_left=0))
        task = asyncio.create_task(
            self.runtime(charge_gpu_seconds=False).run_node(work)
        )
        await settle()
        await self.clock.advance(90.5)
        self.assertEqual((await task).result.summary, "done by local")
        self.assertEqual(work.budget.charges, [])

    async def test_the_resource_class_is_the_runtimes(self):
        runtime = self.runtime(resource_class=IC)
        task = asyncio.create_task(runtime.run_node(assignment()))
        await settle()
        self.assertEqual(self.scheduler.status().leases[IC], 1)
        await self.clock.advance(90.5)
        await task
        with self.assertRaises(ValueError):
            self.runtime(resource_class=ResourceClass.EXCLUSIVE)


class HybridPlacementTest(unittest.IsolatedAsyncioTestCase):
    """Issue #133 (Decision 0037's 14): where each attempt runs is recorded
    through the assignment's ``placement`` before it runs there; for the cloud
    that record is the audit of the send, so what cannot be recorded is not
    sent."""

    async def asyncSetUp(self):
        self.scheduler, self.probe, self.control, self.clock = build()
        await self.scheduler.refresh()
        self.placement = FakePlacement()
        self.local = Recorder("local")
        self.cloud = Recorder("cloud")
        self.policy = Policy()
        placement = self.placement
        seen = []

        class Watching(Recorder):
            async def run_node(self, assignment):
                # What was on record when the runtime began.
                seen.append(list(placement.records))
                return await super().run_node(assignment)

        self.cloud = Watching("cloud")
        self.seen = seen

    def runtime(self, **options):
        values = dict(
            deployment="main",
            cloud=self.cloud,
            cloud_policy=self.policy,
            cloud_agent="codex",
            cloud_model="gpt-5-codex",
            clock=self.clock,
            wait_seconds=120,
        )
        values.update(options)
        return HybridRuntime(self.scheduler, self.local, **values)

    async def test_the_compute_placements_are_the_orchestrators(self):
        self.assertEqual(
            [member.value for member in Placement],
            [member.value for member in ExecutionPlacement],
        )

    async def test_a_local_attempt_records_the_gpu_its_agent_and_its_model(self):
        outcome = await self.runtime().run_node(
            assignment(agent="local-coder", placement=self.placement)
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(
            self.placement.records,
            [(ExecutionPlacement.LOCAL_GPU, "local-coder", "main")],
        )

    async def test_the_local_model_id_can_be_named(self):
        runtime = self.runtime(local_model="Qwen/Qwen3-Coder-30B-A3B-Instruct")
        await runtime.run_node(assignment(placement=self.placement))
        self.assertEqual(
            self.placement.records,
            [
                (
                    ExecutionPlacement.LOCAL_GPU,
                    "local",
                    "Qwen/Qwen3-Coder-30B-A3B-Instruct",
                )
            ],
        )

    async def test_a_cloud_attempt_is_on_record_before_the_cloud_runs(self):
        await fill_main(self.scheduler)
        outcome = await self.runtime().run_node(
            assignment(goal="x" * 90_000, placement=self.placement)
        )
        self.assertEqual(outcome.result.summary, "done by cloud")
        expected = [(ExecutionPlacement.CLOUD, "codex", "gpt-5-codex")]
        self.assertEqual(self.placement.records, expected)
        self.assertEqual(self.seen, [expected])

    async def test_without_a_placement_nothing_goes_to_the_cloud(self):
        # A planner call (or any caller that cannot record the send): the policy
        # is not even asked, the node waits for the local GPU.
        held = await fill_main(self.scheduler)
        task = asyncio.create_task(
            self.runtime().run_node(assignment(goal="x" * 90_000, placement=None))
        )
        await settle()
        self.assertFalse(task.done())
        self.assertEqual(self.policy.asked, 0)
        self.assertEqual(self.scheduler.status().cloud_leases, 0)
        await held[0].release()
        await settle()
        outcome = await task
        self.assertEqual(outcome.result.summary, "done by local")
        self.assertEqual(self.cloud.calls, [])

    async def test_a_send_that_cannot_be_recorded_is_not_sent(self):
        await fill_main(self.scheduler)
        placement = FakePlacement(error=RuntimeError("database down"))
        with self.assertLogs("paw_backend.compute.runtimes", "WARNING"):
            outcome = await self.runtime().run_node(
                assignment(goal="x" * 90_000, placement=placement)
            )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_class, "ComputeUnavailable")
        self.assertTrue(outcome.retryable)
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(self.scheduler.status().cloud_leases, 0)

    async def test_a_local_attempt_that_cannot_be_recorded_does_not_run(self):
        placement = FakePlacement(error=RuntimeError("database down"))
        work = assignment(placement=placement, budget=FakeBudget(100))
        with self.assertLogs("paw_backend.compute.runtimes", "WARNING"):
            outcome = await self.runtime().run_node(work)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_class, "ComputeUnavailable")
        self.assertTrue(outcome.retryable)
        self.assertEqual(self.local.calls, [])
        self.assertEqual(work.budget.charges, [])
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)

    async def test_a_stopped_attempt_stops_at_the_record(self):
        await fill_main(self.scheduler)
        placement = FakePlacement(error=NodeStopped(StopReason.TASK_ENDED))
        with self.assertRaises(NodeStopped):
            await self.runtime().run_node(
                assignment(goal="x" * 90_000, placement=placement)
            )
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(self.scheduler.status().cloud_leases, 0)

    async def test_a_cloud_runtime_needs_its_agent_and_model(self):
        for missing in ("cloud_agent", "cloud_model"):
            with self.subTest(missing), self.assertRaises(TypeError):
                self.runtime(**{missing: None})
        for name, value in (
            ("cloud_agent", "Codex"),
            ("cloud_agent", "codex\n"),
            ("cloud_model", "gpt 5"),
            ("cloud_model", "x" * 129),
            ("local_model", "-bad"),
        ):
            with (
                self.subTest(name=name, value=value),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                self.runtime(**{name: value})


class MemoryWorkerTest(unittest.IsolatedAsyncioTestCase):
    class Worker:
        def __init__(self):
            self.calls = 0

        async def extract(self, input_text):
            self.calls += 1
            return '{"memories": []}'

    async def test_the_worker_runs_under_a_background_lease(self):
        scheduler, *_ = build()
        await scheduler.refresh()
        inner = self.Worker()
        worker = ScheduledMemoryWorker(inner, scheduler, deployment="memory")
        self.assertEqual(await worker.extract("hello"), '{"memories": []}')
        self.assertEqual(inner.calls, 1)
        self.assertEqual(scheduler.status().leases[ResourceClass.BACKGROUND], 0)

    async def test_an_unloaded_worker_is_unavailable_not_failed(self):
        scheduler, *_ = build(
            (main_spec(), memory_spec(initial=DeploymentState.UNLOADED)), control=False
        )
        await scheduler.refresh()
        inner = self.Worker()
        worker = ScheduledMemoryWorker(inner, scheduler, deployment="memory")
        with self.assertRaises(WorkerUnavailableError):
            await worker.extract("hello")
        self.assertEqual(inner.calls, 0)

    async def test_pressure_makes_the_worker_unavailable(self):
        scheduler, probe, *_ = build()
        await scheduler.refresh()
        probe.external = 12 * 1024**3
        await scheduler.refresh()  # background stopped
        worker = ScheduledMemoryWorker(self.Worker(), scheduler, deployment="memory")
        with self.assertRaises(WorkerUnavailableError):
            await worker.extract("hello")

    async def test_a_revoked_lease_stops_a_running_job(self):
        scheduler, probe, *_ = build()
        await scheduler.refresh()
        started = asyncio.Event()

        class Hung:
            async def extract(self, input_text):
                started.set()
                await asyncio.Event().wait()

        worker = ScheduledMemoryWorker(Hung(), scheduler, deployment="memory")
        job = asyncio.create_task(worker.extract("hello"))
        await started.wait()
        probe.external = 12 * 1024**3
        refreshing = asyncio.create_task(scheduler.refresh())  # revokes Background
        await settle()
        try:
            self.assertTrue(job.done())
            with self.assertRaises(WorkerUnavailableError):
                await job
            self.assertEqual(scheduler.status().leases[ResourceClass.BACKGROUND], 0)
        finally:
            job.cancel()
            refreshing.cancel()

    async def test_an_observation_too_long_for_the_model_is_a_failure(self):
        scheduler, *_ = build()
        await scheduler.refresh()
        inner = self.Worker()
        worker = ScheduledMemoryWorker(inner, scheduler, deployment="memory")
        # 16,384 tokens at most: ~60,000 bytes can never be taken.
        with self.assertRaises(ComputeUnavailableError) as raised:
            await worker.extract("x" * 60_000)
        self.assertNotIsInstance(raised.exception, WorkerUnavailableError)
        self.assertEqual(inner.calls, 0)

    async def test_the_consolidator_accepts_it(self):
        from paw_backend.memory.journal.worker import check_worker

        scheduler, *_ = build()
        check_worker(
            ScheduledMemoryWorker(self.Worker(), scheduler, deployment="memory")
        )


class EmbedderTest(unittest.IsolatedAsyncioTestCase):
    class Embedder:
        def __init__(self, name, model_id="e5", dimensions=3):
            self.name = name
            self.model_id = model_id
            self.dimensions = dimensions
            self.calls = 0

        async def embed(self, texts):
            self.calls += 1
            return [[1.0, 0.0, 0.0] for _ in texts]

    async def test_gpu_or_cpu_as_the_scheduler_placed_the_model(self):
        scheduler, probe, *_ = build()
        await scheduler.refresh()
        gpu, cpu = self.Embedder("gpu"), self.Embedder("cpu")
        embedder = PlacedEmbedder(scheduler, deployment="embed", gpu=gpu, cpu=cpu)
        self.assertEqual((embedder.model_id, embedder.dimensions), ("e5", 3))
        await embedder.embed(["a"])
        self.assertEqual((gpu.calls, cpu.calls), (1, 0))
        probe.external = 30 * 1024**3
        for _ in range(3):
            await scheduler.refresh()  # the embedding model moved to the CPU
        await embedder.embed(["a"])
        self.assertEqual((gpu.calls, cpu.calls), (1, 1))

    async def test_a_revoked_lease_stops_a_gpu_embedding_call(self):
        scheduler, *_ = build()
        await scheduler.refresh()
        started = asyncio.Event()

        class Hung(self.Embedder):
            async def embed(self, texts):
                started.set()
                await asyncio.Event().wait()

        embedder = PlacedEmbedder(scheduler, deployment="embed", gpu=Hung("gpu"))
        call = asyncio.create_task(embedder.embed(["hello"]))
        await started.wait()
        lease = next(iter(scheduler._deployments["embed"].leases))
        lease.revoked.set()  # a relief step is about to move the model
        await settle()
        try:
            self.assertTrue(call.done())
            with self.assertRaises(ComputeUnavailableError):
                await call
            self.assertTrue(lease.released)
        finally:
            call.cancel()

    async def test_no_room_is_an_error_the_retrieval_degrades_on(self):
        scheduler, *_ = build(
            (main_spec(), embedding_spec(initial=DeploymentState.UNLOADED)),
            control=False,
        )
        await scheduler.refresh()
        embedder = PlacedEmbedder(
            scheduler, deployment="embed", gpu=self.Embedder("gpu")
        )
        with self.assertRaises(ComputeUnavailableError):
            await embedder.embed(["a"])

    async def test_the_cpu_copy_must_be_the_same_model(self):
        scheduler, *_ = build()
        with self.assertRaises(ValueError):
            PlacedEmbedder(
                scheduler,
                deployment="embed",
                gpu=self.Embedder("gpu"),
                cpu=self.Embedder("cpu", model_id="other"),
            )


if __name__ == "__main__":
    unittest.main()
