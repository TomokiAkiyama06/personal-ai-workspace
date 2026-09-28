"""Admission on the observed free VRAM (Decision 0042, Proposed).

Processes the scheduler does not manage (another user's vLLM holding tens of GB)
are invisible to its leases but not to the probe. Work that allocates VRAM of
its own waits while the probe does not show that much free beyond the headroom,
and a rate-limited warning goes to the log and the injected sink; an Exclusive
job that could not get its VRAM even with every model unloaded waits before
anything is drained or unloaded. The GPU utilisation plays no part.

The fakes simulate the GPU (``compute_support.py``); nothing real is read,
loaded or allocated.
"""

import asyncio
import random
import unittest

from paw_backend.compute import (
    ComputeRequest,
    ComputeUnavailableError,
    DeploymentState,
    ExclusiveFailure,
    ExclusiveUnavailableError,
    GpuDevice,
    GpuProcess,
    Placement,
    Refusal,
    ResourceClass,
    SchedulerMode,
)
from paw_backend.compute.accounting import (
    DeploymentUsage,
    account,
    free_vram_admits,
    free_vram_after_emptying,
)
from paw_backend.compute.alerts import DeferredWork, VramDeferral
from tests.compute_support import (
    GIB,
    GPU_UUID,
    build,
    embedding_spec,
    main_spec,
    memory_spec,
    settle,
)

CO = ResourceClass.CODING
SU = ResourceClass.SUPPORT
EX = ResourceClass.EXCLUSIVE
HEADROOM = int(96 * GIB * 0.05)  # 4.8 GiB: 5% of the GPU is above 4 GiB
JOB_PID = 7_777  # a process a job started (no model's)


def coding(vram=0, tokens=1_000, **options):
    return ComputeRequest(
        CO, deployment="main", context_tokens=tokens, vram_bytes=vram, **options
    )


class RecordingSink:
    def __init__(self):
        self.events: list[VramDeferral] = []

    def vram_deferred(self, event):
        self.events.append(event)


def build_with_sink(specs=None, **options):
    sink = RecordingSink()
    scheduler, probe, control, clock = build(specs, **options)
    scheduler._warnings._sink = sink  # the same object the constructor wires
    return scheduler, probe, control, clock, sink


class ConstructorTest(unittest.TestCase):
    def test_the_sink_is_checked_and_wired(self):
        from paw_backend.compute import ComputeConfig, ComputeScheduler
        from tests.compute_support import FakeProbe

        config = ComputeConfig(deployments=(main_spec(),))
        with self.assertRaises(TypeError):
            ComputeScheduler(config, FakeProbe(), vram_warnings=object())
        sink = RecordingSink()
        scheduler = ComputeScheduler(config, FakeProbe(), vram_warnings=sink)
        self.assertIs(scheduler._warnings._sink, sink)


class FreeVramAdmissionTest(unittest.IsolatedAsyncioTestCase):
    """80 GiB of the workspace's models on a 96 GiB GPU: 16 GiB observed free,
    11.2 GiB beyond the headroom."""

    async def asyncSetUp(self):
        (
            self.scheduler,
            self.probe,
            self.control,
            self.clock,
            self.sink,
        ) = build_with_sink()
        await self.scheduler.refresh()

    async def test_work_that_needs_vram_starts_when_the_probe_shows_it_free(self):
        status = self.scheduler.status()
        self.assertEqual(status.vram.observed_free, 16 * GIB)
        admitted = await self.scheduler.try_acquire(coding(vram=11 * GIB))
        self.assertIsNotNone(admitted.lease)
        self.assertEqual(admitted.lease.vram_bytes, 11 * GIB)
        self.assertEqual(self.scheduler.status().vram.reserved, 91 * GIB)
        await admitted.lease.release()
        self.assertEqual(self.scheduler.status().vram.reserved, 80 * GIB)

    async def test_the_resident_models_are_not_counted_twice(self):
        # The models' memory is used (vLLM allocated its KV pool when it
        # loaded) and reserved: it is counted once, so 11.2 GiB fit and work
        # inside the footprint is never held back for it.
        self.assertIsNone((await self.scheduler.try_acquire(coding())).refusal)
        self.assertEqual(
            (await self.scheduler.try_acquire(coding(vram=12 * GIB))).refusal,
            Refusal.INSUFFICIENT_FREE_VRAM,
        )
        self.assertIsNotNone(
            (await self.scheduler.try_acquire(coding(vram=11 * GIB))).lease
        )

    async def test_admitted_vram_counts_before_the_probe_shows_it(self):
        first = (await self.scheduler.try_acquire(coding(vram=8 * GIB))).lease
        # Nothing allocated yet: the probe still shows 16 GiB free, but 8 GiB
        # of it are promised.
        self.assertEqual(self.scheduler.status().vram.observed_free, 16 * GIB)
        refused = await self.scheduler.try_acquire(coding(vram=4 * GIB))
        self.assertEqual(refused.refusal, Refusal.INSUFFICIENT_FREE_VRAM)
        await first.release()
        self.assertIsNotNone(
            (await self.scheduler.try_acquire(coding(vram=4 * GIB))).lease
        )

    async def test_what_the_job_allocates_is_absorbed_by_its_lease(self):
        self.probe.external = 2 * GIB  # another workload, there before the lease
        await self.scheduler.refresh()
        lease = (await self.scheduler.try_acquire(coding(vram=8 * GIB))).lease
        self.probe.resident[JOB_PID] = 8 * GIB  # the job allocated its VRAM
        status = await self.scheduler.refresh()
        self.assertEqual(status.vram.external, 2 * GIB)  # not the job's 8 GiB
        self.assertEqual(status.vram.committed, 90 * GIB)
        await lease.release()
        # The job's memory lingers after the release: it is external now.
        status = await self.scheduler.refresh()
        self.assertEqual(status.vram.external, 10 * GIB)
        del self.probe.resident[JOB_PID]
        status = await self.scheduler.refresh()
        self.assertEqual(status.vram.external, 2 * GIB)
        self.assertEqual(status.vram.reserved, 80 * GIB)

    async def test_memory_a_released_lease_leaves_behind_stays_external(self):
        # Two VRAM leases; the first allocated its 5 GiB, the second nothing
        # yet. The first is released but its process keeps the memory (a
        # caching allocator): it must not cover the second lease's promise.
        first = (await self.scheduler.try_acquire(coding(vram=5 * GIB))).lease
        second = (await self.scheduler.try_acquire(coding(vram=5 * GIB))).lease
        self.probe.resident[JOB_PID] = 5 * GIB
        status = await self.scheduler.refresh()
        self.assertEqual(status.vram.external, 0)
        self.assertEqual(status.vram.committed, 90 * GIB)
        await first.release()
        status = await self.scheduler.refresh()
        self.assertEqual(status.vram.external, 5 * GIB)
        self.assertEqual(status.vram.committed, 90 * GIB)
        self.assertEqual(status.vram.available, 6 * GIB - HEADROOM)
        # 6.1 GiB would overcommit the GPU once the second lease allocates.
        self.assertEqual(
            (await self.scheduler.try_acquire(coding(vram=int(6.1 * GIB)))).refusal,
            Refusal.INSUFFICIENT_FREE_VRAM,
        )
        self.assertEqual(
            (await self.scheduler.try_acquire(coding(vram=2 * GIB))).refusal,
            Refusal.INSUFFICIENT_FREE_VRAM,
        )
        del self.probe.resident[JOB_PID]  # the memory is freed at last
        status = await self.scheduler.refresh()
        self.assertEqual(status.vram.external, 0)
        self.assertEqual(status.vram.committed, 85 * GIB)
        await second.release()
        status = await self.scheduler.refresh()
        self.assertEqual(status.vram.committed, 80 * GIB)

    async def test_an_ambiguous_release_errs_on_the_safe_side_and_heals(self):
        # Which lease's process holds the memory is not known: when a lease
        # that allocated nothing is released beside one that did, the memory
        # is taken for the released lease's (external) until the other ends.
        first = (await self.scheduler.try_acquire(coding(vram=5 * GIB))).lease
        second = (await self.scheduler.try_acquire(coding(vram=5 * GIB))).lease
        self.probe.resident[JOB_PID] = 5 * GIB  # the second lease's
        await self.scheduler.refresh()
        await first.release()
        status = await self.scheduler.refresh()
        self.assertEqual(status.vram.external, 5 * GIB)
        self.assertEqual(status.vram.committed, 90 * GIB)  # never less
        await second.release()
        del self.probe.resident[JOB_PID]
        status = await self.scheduler.refresh()
        self.assertEqual(status.vram.external, 0)
        self.assertEqual(status.vram.committed, 80 * GIB)
        self.assertIsNotNone(
            (await self.scheduler.try_acquire(coding(vram=11 * GIB))).lease
        )

    async def test_another_workload_defers_the_work_with_a_warning(self):
        self.probe.external = 10 * GIB  # not under pressure: 1.2 GiB left
        await self.scheduler.refresh()
        self.assertFalse(self.scheduler.status().vram.under_pressure)
        with self.assertLogs("paw_backend.compute", level="WARNING") as logs:
            waiting = asyncio.create_task(
                self.scheduler.acquire(coding(vram=2 * GIB), wait_seconds=600)
            )
            await settle()
        self.assertFalse(waiting.done())  # deferred, not refused
        self.assertIn("Not enough free VRAM for request (coding)", logs.output[0])
        self.assertIn("10240 MiB used by other workloads", logs.output[0])
        self.assertNotIn("9999", "".join(logs.output))  # no pid
        self.assertEqual(self.scheduler.status().vram_waiting, 1)
        [event] = self.sink.events
        self.assertEqual(event.work, DeferredWork.REQUEST)
        self.assertEqual(event.resource_class, CO)
        self.assertEqual(event.requested_bytes, 2 * GIB)
        self.assertEqual(event.observed_free_bytes, 6 * GIB)
        self.assertEqual(event.external_bytes, 10 * GIB)
        self.assertEqual(event.headroom_bytes, HEADROOM)
        self.assertFalse(event.gave_up)
        # Work inside the model's footprint goes on beside it.
        self.assertIsNotNone((await self.scheduler.try_acquire(coding())).lease)

        self.probe.external = 0  # the other workload ended
        await self.scheduler.refresh()
        lease = await waiting
        self.assertEqual(lease.placement, Placement.LOCAL_GPU)
        self.assertEqual(self.scheduler.status().vram_waiting, 0)

    async def test_the_utilisation_plays_no_part(self):
        self.probe.utilization = 100
        await self.scheduler.refresh()
        self.assertIsNotNone((await self.scheduler.try_acquire(coding(vram=GIB))).lease)
        self.probe.utilization = 0
        self.probe.external = 11 * GIB
        await self.scheduler.refresh()
        self.assertEqual(
            (await self.scheduler.try_acquire(coding(vram=GIB))).refusal,
            Refusal.INSUFFICIENT_FREE_VRAM,
        )

    async def test_under_pressure_only_work_that_needs_vram_is_held_back(self):
        self.probe.external = 20 * GIB  # 8.8 GiB over: the relief steps start
        status = await self.scheduler.refresh()
        self.assertTrue(status.vram.under_pressure)
        # Work inside the footprint follows Decision 0037 (the relief steps).
        self.assertIsNotNone((await self.scheduler.try_acquire(coding())).lease)
        self.assertEqual(
            (await self.scheduler.try_acquire(coding(vram=1))).refusal,
            Refusal.INSUFFICIENT_FREE_VRAM,
        )

    async def test_a_wait_that_runs_out_fails_with_the_reason_and_warns(self):
        self.probe.external = 11 * GIB
        await self.scheduler.refresh()
        waiting = asyncio.create_task(
            self.scheduler.acquire(coding(vram=GIB), wait_seconds=30)
        )
        await settle()
        await self.clock.advance(30)
        with self.assertRaises(ComputeUnavailableError) as raised:
            await waiting
        self.assertEqual(raised.exception.reason, Refusal.INSUFFICIENT_FREE_VRAM)
        self.assertEqual([e.gave_up for e in self.sink.events], [False, True])
        self.assertEqual(self.scheduler.status().waiting[CO], 0)

    async def test_no_wait_means_an_immediate_refusal(self):
        self.probe.external = 11 * GIB
        await self.scheduler.refresh()
        with self.assertRaises(ComputeUnavailableError) as raised:
            await self.scheduler.acquire(coding(vram=GIB), wait_seconds=0)
        self.assertEqual(raised.exception.reason, Refusal.INSUFFICIENT_FREE_VRAM)
        self.assertEqual([e.gave_up for e in self.sink.events], [True])

    async def test_work_that_may_use_the_cloud_goes_there_without_a_warning(self):
        self.probe.external = 11 * GIB
        await self.scheduler.refresh()
        lease = await self.scheduler.acquire(
            coding(vram=GIB, allow_cloud=True), wait_seconds=60
        )
        self.assertEqual(lease.placement, Placement.CLOUD)
        self.assertEqual(lease.vram_bytes, 0)
        self.assertEqual(self.sink.events, [])

    async def test_warnings_are_rate_limited(self):
        self.probe.external = 11 * GIB
        await self.scheduler.refresh()
        for _ in range(5):
            await self.scheduler.try_acquire(coding(vram=GIB))
        self.assertEqual(len(self.sink.events), 1)
        await self.clock.advance(299)
        await self.scheduler.refresh()
        await self.scheduler.try_acquire(coding(vram=GIB))
        self.assertEqual(len(self.sink.events), 1)
        await self.clock.advance(1)
        await self.scheduler.refresh()
        await self.scheduler.try_acquire(coding(vram=GIB))
        self.assertEqual(len(self.sink.events), 2)
        # Another class is another kind of warning.
        await self.scheduler.try_acquire(
            ComputeRequest(ResourceClass.INTERACTIVE, deployment="main", vram_bytes=GIB)
        )
        self.assertEqual(len(self.sink.events), 3)

    async def test_a_broken_sink_does_not_stop_admission(self):
        def broken(event):
            raise RuntimeError("hook down")

        self.sink.vram_deferred = broken
        self.probe.external = 11 * GIB
        await self.scheduler.refresh()
        with self.assertLogs("paw_backend.compute", level="ERROR") as logs:
            refused = await self.scheduler.try_acquire(coding(vram=GIB))
        self.assertEqual(refused.refusal, Refusal.INSUFFICIENT_FREE_VRAM)
        self.assertIn("VRAM warning sink failed (RuntimeError)", logs.output[-1])
        self.assertNotIn("hook down", "".join(logs.output))

    async def test_a_stale_reading_admits_nothing_and_does_not_warn(self):
        self.probe.fail = True
        await self.scheduler.refresh()
        self.assertEqual(
            (await self.scheduler.try_acquire(coding(vram=GIB))).refusal,
            Refusal.PROBE_UNAVAILABLE,
        )
        self.assertEqual(self.sink.events, [])

    async def test_later_work_that_needs_vram_does_not_overtake(self):
        self.probe.external = 8 * GIB  # 3.2 GiB left beyond the headroom
        await self.scheduler.refresh()
        large = asyncio.create_task(
            self.scheduler.acquire(
                ComputeRequest(SU, deployment="memory", vram_bytes=6 * GIB),
                wait_seconds=600,
            )
        )
        await settle()
        # A smaller job of the same class on another model would fit, but
        # queues behind the large one ...
        small_request = ComputeRequest(SU, deployment="embed", vram_bytes=GIB)
        self.assertEqual(
            (await self.scheduler.try_acquire(small_request)).refusal,
            Refusal.QUEUED_BEHIND,
        )
        small = asyncio.create_task(
            self.scheduler.acquire(small_request, wait_seconds=600)
        )
        await settle()
        await self.scheduler.refresh()
        self.assertFalse(small.done())
        # ... while work that allocates nothing, and a higher class, go on.
        self.assertIsNotNone(
            (
                await self.scheduler.try_acquire(ComputeRequest(SU, deployment="embed"))
            ).lease
        )
        self.assertIsNotNone((await self.scheduler.try_acquire(coding(vram=GIB))).lease)
        self.probe.external = 0
        await self.scheduler.refresh()
        self.assertEqual((await large).vram_bytes, 6 * GIB)
        self.assertEqual((await small).vram_bytes, GIB)

    async def test_a_cpu_placement_holds_no_vram(self):
        scheduler, probe, _, _, sink = build_with_sink(
            (main_spec(), embedding_spec(initial=DeploymentState.CPU)),
            external=30 * GIB,
        )
        await scheduler.refresh()
        admitted = await scheduler.try_acquire(
            ComputeRequest(SU, deployment="embed", vram_bytes=GIB)
        )
        self.assertEqual(admitted.lease.placement, Placement.LOCAL_CPU)
        self.assertEqual(admitted.lease.vram_bytes, 0)
        self.assertEqual(sink.events, [])

    async def test_a_cpu_placement_does_not_queue_behind_a_vram_waiter(self):
        scheduler, probe, _, _, _ = build_with_sink(
            (main_spec(), memory_spec(), embedding_spec(initial=DeploymentState.CPU)),
            external=8 * GIB,  # 3.2 GiB left beyond the headroom
        )
        await scheduler.refresh()
        large = asyncio.create_task(
            scheduler.acquire(
                ComputeRequest(SU, deployment="memory", vram_bytes=6 * GIB),
                wait_seconds=600,
            )
        )
        await settle()
        self.assertEqual(scheduler.status().vram_waiting, 1)
        request = ComputeRequest(SU, deployment="embed", vram_bytes=GIB)
        admitted = await scheduler.try_acquire(request)
        self.assertEqual(admitted.lease.placement, Placement.LOCAL_CPU)
        self.assertEqual(admitted.lease.vram_bytes, 0)
        # A waiter that would run on the CPU is admitted by the pump too.
        await admitted.lease.release()
        for _ in range(8):
            await scheduler.try_acquire(ComputeRequest(SU, deployment="embed"))
        cpu_waiter = asyncio.create_task(scheduler.acquire(request, wait_seconds=600))
        await settle()
        self.assertFalse(cpu_waiter.done())  # SEQUENCES_FULL on the CPU copy
        [held, *_] = scheduler._deployments["embed"].leases
        await held.release()
        await settle()
        self.assertEqual((await cpu_waiter).placement, Placement.LOCAL_CPU)
        self.assertFalse(large.done())
        large.cancel()

    async def test_only_waiters_for_vram_hold_back_other_models(self):
        # A VRAM request on the memory model waits for a sequence of its own
        # model, not for VRAM: work that needs VRAM on another model goes on.
        for _ in range(4):
            await self.scheduler.try_acquire(ComputeRequest(SU, deployment="memory"))
        waiting = asyncio.create_task(
            self.scheduler.acquire(
                ComputeRequest(SU, deployment="memory", vram_bytes=GIB),
                wait_seconds=600,
            )
        )
        await settle()
        self.assertEqual(self.scheduler.status().vram_waiting, 0)
        admitted = await self.scheduler.try_acquire(
            ComputeRequest(SU, deployment="embed", vram_bytes=GIB)
        )
        self.assertIsNotNone(admitted.lease)
        self.assertFalse(waiting.done())
        waiting.cancel()


class ModelLoadWarningTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_load_blocked_by_another_workload_warns(self):
        scheduler, probe, control, _, sink = build_with_sink(
            (main_spec(initial=DeploymentState.UNLOADED),), external=40 * GIB
        )
        with self.assertLogs("paw_backend.compute", level="WARNING"):
            await scheduler.refresh()
        self.assertEqual(control.actions, [])
        [event] = sink.events
        self.assertEqual(event.work, DeferredWork.MODEL_LOAD)
        self.assertIsNone(event.resource_class)
        self.assertEqual(event.requested_bytes, 66 * GIB)
        self.assertEqual(event.external_bytes, 40 * GIB)
        probe.external = 0
        await scheduler.refresh()
        self.assertEqual(control.actions, [("place:local_gpu", "main")])

    async def test_a_load_that_does_not_fit_beside_our_own_models_is_quiet(self):
        scheduler, _, control, _, sink = build_with_sink(
            (
                main_spec(),
                memory_spec(initial=DeploymentState.UNLOADED, weights_bytes=40 * GIB),
            )
        )
        with self.assertNoLogs("paw_backend.compute", level="WARNING"):
            await scheduler.refresh()
        self.assertEqual(control.actions, [])
        self.assertEqual(sink.events, [])


class ExclusiveFreeVramTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (
            self.scheduler,
            self.probe,
            self.control,
            self.clock,
            self.sink,
        ) = build_with_sink()
        await self.scheduler.refresh()

    async def test_another_workload_defers_the_job_before_anything_is_unloaded(self):
        self.probe.external = 20 * GIB  # 80 GiB cannot be free even if empty
        job = asyncio.create_task(
            self.scheduler.acquire(
                ComputeRequest(EX, vram_bytes=80 * GIB), wait_seconds=600
            )
        )
        await settle()
        self.assertFalse(job.done())
        status = self.scheduler.status()
        self.assertEqual(status.mode, SchedulerMode.NORMAL)  # nothing drains
        self.assertTrue(status.exclusive_waiting_for_vram)
        self.assertEqual(self.control.actions, [])  # nothing unloaded
        self.assertEqual(self.sink.events[0].work, DeferredWork.EXCLUSIVE)
        self.assertEqual(self.sink.events[0].resource_class, EX)
        # Local work goes on meanwhile; a second Exclusive job is refused.
        self.assertIsNotNone((await self.scheduler.try_acquire(coding())).lease)
        with self.assertRaises(ExclusiveUnavailableError) as raised:
            await self.scheduler.acquire(
                ComputeRequest(EX, vram_bytes=GIB), wait_seconds=60
            )
        self.assertEqual(raised.exception.failure, ExclusiveFailure.BUSY)

        self.probe.external = 0
        for lease in list(self.scheduler._deployments["main"].leases):
            await lease.release()
        await self.clock.advance(2)  # the next poll sees the VRAM free
        lease = await job
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.EXCLUSIVE)
        self.assertFalse(self.scheduler.status().exclusive_waiting_for_vram)
        self.assertEqual(
            self.control.actions,
            [("unload", "memory"), ("place:local_cpu", "embed"), ("unload", "main")],
        )
        await lease.release()

    async def test_the_job_gives_up_at_its_deadline_without_unloading(self):
        self.probe.external = 20 * GIB
        job = asyncio.create_task(
            self.scheduler.acquire(
                ComputeRequest(EX, vram_bytes=80 * GIB), wait_seconds=60
            )
        )
        await settle()
        for _ in range(61):
            if job.done():
                break
            await self.clock.advance(1)
        with self.assertRaises(ExclusiveUnavailableError) as raised:
            await job
        self.assertEqual(raised.exception.failure, ExclusiveFailure.NOT_FREED)
        self.assertEqual(self.control.actions, [])
        self.assertTrue(self.sink.events[-1].gave_up)
        status = self.scheduler.status()
        self.assertEqual(status.mode, SchedulerMode.NORMAL)
        self.assertFalse(status.exclusive_waiting_for_vram)
        # The scheduler can serve another Exclusive job afterwards.
        self.probe.external = 0
        lease = await self.scheduler.acquire(
            ComputeRequest(EX, vram_bytes=GIB), wait_seconds=60
        )
        await lease.release()

    async def test_a_probe_lost_while_waiting_gives_up_as_unavailable(self):
        self.probe.external = 20 * GIB
        job = asyncio.create_task(
            self.scheduler.acquire(
                ComputeRequest(EX, vram_bytes=80 * GIB), wait_seconds=10
            )
        )
        await settle()
        self.probe.fail = True
        for _ in range(11):
            if job.done():
                break
            await self.clock.advance(1)
        with self.assertRaises(ExclusiveUnavailableError) as raised:
            await job
        self.assertEqual(raised.exception.failure, ExclusiveFailure.PROBE_UNAVAILABLE)
        self.assertEqual(self.control.actions, [])

    async def test_a_workload_that_arrives_during_the_drain_sends_it_back(self):
        running = (await self.scheduler.try_acquire(coding())).lease
        job = asyncio.create_task(
            self.scheduler.acquire(
                ComputeRequest(EX, vram_bytes=80 * GIB), wait_seconds=600
            )
        )
        await settle()
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.DRAINING)
        self.probe.external = 20 * GIB  # another workload starts meanwhile
        await running.release()
        await settle()
        self.assertFalse(job.done())
        status = self.scheduler.status()
        self.assertEqual(status.mode, SchedulerMode.NORMAL)  # back to normal
        self.assertTrue(status.exclusive_waiting_for_vram)
        self.assertEqual(self.control.actions, [])
        self.assertIsNotNone((await self.scheduler.try_acquire(coding())).lease)
        for lease in list(self.scheduler._deployments["main"].leases):
            await lease.release()
        self.probe.external = 0
        await self.clock.advance(2)
        lease = await job
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.EXCLUSIVE)
        await lease.release()

    async def test_the_resident_models_do_not_defer_the_job(self):
        # 80 GiB of our own models are on the GPU: they will be unloaded, so
        # they do not count against the job (no warning, no wait).
        lease = await self.scheduler.acquire(
            ComputeRequest(EX, vram_bytes=88 * GIB), wait_seconds=60
        )
        self.assertEqual(self.sink.events, [])
        await lease.release()


class AccountingTest(unittest.TestCase):
    def test_free_vram_admits_is_the_available_room(self):
        rng = random.Random(42)
        for _ in range(500):
            total = 96 * GIB
            model = rng.randrange(0, 60) * GIB
            model_used = rng.choice((0, model, model // 2))
            external = rng.randrange(0, 30) * GIB
            used = min(total, model_used + external)
            processes = [GpuProcess(GPU_UUID, 1, model_used)] if model_used else []
            if used > model_used:
                processes.append(GpuProcess(GPU_UUID, 2, used - model_used))
            view = account(
                GpuDevice(0, GPU_UUID, "Fake", total, used, None),
                processes,
                [DeploymentUsage(model, frozenset({1}))] if model else [],
                headroom=HEADROOM,
                extra_reserved=rng.randrange(0, 10) * GIB,
            )
            need = rng.randrange(0, 40) * GIB
            with self.subTest(view=view, need=need):
                self.assertEqual(
                    free_vram_admits(view, need), need <= 0 or view.available >= need
                )
                self.assertLessEqual(view.available, view.observed_free - HEADROOM)

    def test_free_vram_after_emptying_ignores_the_workspace(self):
        view = account(
            GpuDevice(0, GPU_UUID, "Fake", 96 * GIB, 90 * GIB, None),
            [GpuProcess(GPU_UUID, 1, 70 * GIB), GpuProcess(GPU_UUID, 2, 20 * GIB)],
            [DeploymentUsage(66 * GIB, frozenset({1}))],
            headroom=HEADROOM,
        )
        self.assertEqual(view.external, 20 * GIB)
        self.assertEqual(free_vram_after_emptying(view), 96 * GIB - HEADROOM - 20 * GIB)


if __name__ == "__main__":
    unittest.main()
