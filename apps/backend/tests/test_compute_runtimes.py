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

from paw_backend.compute import (
    ComputeRequest,
    ComputeUnavailableError,
    DeploymentState,
    HybridRuntime,
    PlacedEmbedder,
    ResourceClass,
    ScheduledMemoryWorker,
    estimate_context_tokens,
)
from paw_backend.memory.journal import WorkerUnavailableError
from paw_backend.orchestrator import NodeOutcome, NodeResult, NodeRole
from paw_backend.orchestrator.errors import NodeStopped, StopReason
from paw_backend.orchestrator.runtime import NodeAssignment, validate_runtime
from paw_backend.tasks.queueing import BudgetKind
from tests.compute_support import build, embedding_spec, main_spec, memory_spec, settle

IC = ResourceClass.INTERACTIVE


class FakeBudget:
    def __init__(self) -> None:
        self.charges: list[tuple[BudgetKind, int]] = []

    async def charge(self, kind, amount):
        self.charges.append((kind, amount))

    async def remaining(self):
        return {}


class FakeTools:
    async def call(self, tool, arguments, *, approval_id=None):
        raise AssertionError("not used")


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

    async def test_node_stopped_passes_through(self):
        self.local.error = NodeStopped(StopReason.TASK_ENDED)
        self.local.seconds = 0
        work = assignment()
        with self.assertRaises(NodeStopped):
            await self.runtime().run_node(work)
        self.assertEqual(work.budget.charges, [])
        self.assertEqual(self.scheduler.status().leases[ResourceClass.CODING], 0)

    async def test_the_resource_class_is_the_runtimes(self):
        runtime = self.runtime(resource_class=IC)
        task = asyncio.create_task(runtime.run_node(assignment()))
        await settle()
        self.assertEqual(self.scheduler.status().leases[IC], 1)
        await self.clock.advance(90.5)
        await task
        with self.assertRaises(ValueError):
            self.runtime(resource_class=ResourceClass.EXCLUSIVE)


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
