"""Admission of the Compute Resource Scheduler (PAW-036): resource classes,
KV-based dynamic concurrency, priority among waiters, the probe's freshness and
hybrid Local / Cloud placement. Fakes only: no GPU is touched."""

import asyncio
import unittest

from paw_backend.compute import (
    ComputeRequest,
    ComputeUnavailableError,
    DeploymentState,
    InvalidComputeArgumentError,
    Placement,
    Refusal,
    ResourceClass,
    SchedulerMode,
)
from tests.compute_support import GIB, build, embedding_spec, main_spec, settle

IC = ResourceClass.INTERACTIVE
CO = ResourceClass.CODING
SU = ResourceClass.SUPPORT
BG = ResourceClass.BACKGROUND

# The main model's pool: 20 GiB / 160 KiB = 131,072 tokens, 90% of it usable.
CAPACITY = 131_072


def request(cls=CO, deployment="main", tokens=1_000, **options):
    return ComputeRequest(cls, deployment=deployment, context_tokens=tokens, **options)


class AdmissionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler, self.probe, self.control, self.clock = build()
        await self.scheduler.refresh()

    async def test_a_request_that_fits_gets_a_local_gpu_lease(self):
        admission = await self.scheduler.try_acquire(request(tokens=8_000))
        self.assertIsNone(admission.refusal)
        lease = admission.lease
        self.assertEqual(lease.placement, Placement.LOCAL_GPU)
        self.assertEqual(lease.deployment, "main")
        self.assertEqual(lease.tokens, 8_000)
        status = self.scheduler.status()
        self.assertEqual(status.leases[CO], 1)
        self.assertEqual(status.deployment("main").reserved_tokens, 8_000)
        await lease.release()
        await lease.release()  # idempotent
        status = self.scheduler.status()
        self.assertEqual(status.leases[CO], 0)
        self.assertEqual(status.deployment("main").reserved_tokens, 0)

    async def test_a_lease_is_an_async_context_manager(self):
        async with (await self.scheduler.try_acquire(request())).lease as lease:
            self.assertEqual(self.scheduler.status().leases[CO], 1)
            self.assertFalse(lease.released)
        self.assertTrue(lease.released)
        self.assertEqual(self.scheduler.status().leases[CO], 0)

    async def test_long_contexts_admit_fewer_requests_than_short_ones(self):
        # Dynamic concurrency: the same pool holds 3 long or 16 short requests.
        long_leases = []
        while (
            admission := await self.scheduler.try_acquire(request(IC, tokens=35_000))
        ).lease:
            long_leases.append(admission.lease)
        self.assertEqual(len(long_leases), 3)
        self.assertEqual(admission.refusal, Refusal.KV_FULL)
        for lease in long_leases:
            await lease.release()
        short = []
        while (
            admission := await self.scheduler.try_acquire(request(IC, tokens=2_000))
        ).lease:
            short.append(admission.lease)
        self.assertEqual(len(short), 16)  # the runtime's max_sequences
        self.assertEqual(admission.refusal, Refusal.SEQUENCES_FULL)

    async def test_parallelism_is_reported_for_a_context_length(self):
        self.assertEqual(self.scheduler.parallelism("main", 35_000, IC), 3)
        self.assertEqual(self.scheduler.parallelism("main", 2_000, IC), 16)
        lease = (await self.scheduler.try_acquire(request(IC, tokens=35_000))).lease
        self.assertEqual(self.scheduler.parallelism("main", 35_000, IC), 2)
        await lease.release()
        with self.assertRaises(InvalidComputeArgumentError):
            self.scheduler.parallelism("nope", 1, IC)
        with self.assertRaises(InvalidComputeArgumentError):
            self.scheduler.parallelism("main", -1, IC)
        # Held back by more than capacity: nothing would be admitted.
        self.assertEqual(self.scheduler.parallelism("main", 70_000, IC), 0)  # too long
        self.probe.fail = True
        await self.scheduler.refresh()
        self.assertEqual(self.scheduler.parallelism("main", 2_000, IC), 0)

    async def test_the_runtimes_observed_kv_usage_is_respected(self):
        self.control.kv["main"] = 0.85  # someone else's requests fill the pool
        await self.scheduler.refresh()
        admission = await self.scheduler.try_acquire(request(IC, tokens=8_000))
        self.assertEqual(admission.refusal, Refusal.KV_FULL)
        self.assertEqual(
            self.scheduler.status().deployment("main").observed_kv_fraction, 0.85
        )

    async def test_a_reading_is_published_only_with_what_the_models_said(self):
        # No reading yet (the probe failed); the next one is being taken and the
        # model control is slow to answer: admission does not use the new
        # reading beside the previous KV use (none), it waits for all of it.
        self.probe.fail = True
        await self.scheduler.refresh()
        self.probe.fail = False
        self.control.kv["main"] = 0.85
        answer = asyncio.Event()
        processes = self.control.processes

        async def slow_processes(deployment):
            await answer.wait()
            return await processes(deployment)

        self.control.processes = slow_processes
        refreshing = asyncio.create_task(self.scheduler.refresh())
        await settle()
        try:
            admission = await self.scheduler.try_acquire(request(IC, tokens=8_000))
            self.assertEqual(admission.refusal, Refusal.PROBE_UNAVAILABLE)
        finally:
            answer.set()
            await refreshing
        admission = await self.scheduler.try_acquire(request(IC, tokens=8_000))
        self.assertEqual(admission.refusal, Refusal.KV_FULL)

    async def test_background_leaves_room_for_interactive_and_coding(self):
        # 70,000 tokens reserved: 53% of the pool.
        held = [
            (await self.scheduler.try_acquire(request(IC, tokens=35_000))).lease
            for _ in range(2)
        ]
        background = await self.scheduler.try_acquire(request(BG, tokens=20_000))
        self.assertEqual(background.refusal, Refusal.KV_FULL)  # 90% * 70% = 63%
        coding = await self.scheduler.try_acquire(request(CO, tokens=20_000))
        self.assertIsNotNone(coding.lease)
        await coding.lease.release()
        for lease in held:
            await lease.release()

    async def test_a_context_longer_than_the_deployment_allows_is_refused(self):
        admission = await self.scheduler.try_acquire(request(tokens=65_537))
        self.assertEqual(admission.refusal, Refusal.CONTEXT_TOO_LONG)
        self.assertIsNotNone(
            (await self.scheduler.try_acquire(request(tokens=65_536))).lease
        )

    async def test_a_model_that_is_not_resident_is_refused(self):
        scheduler, *_ = build(
            (main_spec(), embedding_spec(initial=DeploymentState.UNLOADED)),
            control=False,
        )
        await scheduler.refresh()
        admission = await scheduler.try_acquire(request(SU, "embed"))
        self.assertEqual(admission.refusal, Refusal.NOT_RESIDENT)

    async def test_a_model_on_the_cpu_gets_a_cpu_lease_without_kv(self):
        scheduler, *_ = build(
            (main_spec(), embedding_spec(initial=DeploymentState.CPU)), control=False
        )
        await scheduler.refresh()
        admission = await scheduler.try_acquire(request(SU, "embed", tokens=8_192))
        self.assertEqual(admission.lease.placement, Placement.LOCAL_CPU)
        self.assertEqual(admission.lease.tokens, 0)
        self.assertEqual(
            (await scheduler.try_acquire(request(SU, "embed", tokens=8_193))).refusal,
            Refusal.CONTEXT_TOO_LONG,
        )

    async def test_the_gpu_kv_share_does_not_limit_a_cpu_lease(self):
        # A KV pool of 10,000 tokens: Support may take 90% * 85% = 7,650 of it on
        # the GPU. On the CPU no KV is reserved: only the model's maximum counts.
        spec = embedding_spec(
            initial=DeploymentState.CPU,
            kv_pool_bytes=10_000 * 1024,
            kv_bytes_per_token=1024,
        )
        scheduler, *_ = build((main_spec(), spec), control=False)
        await scheduler.refresh()
        admission = await scheduler.try_acquire(request(SU, "embed", tokens=8_000))
        self.assertIsNotNone(admission.lease)
        self.assertEqual(admission.lease.placement, Placement.LOCAL_CPU)
        self.assertEqual(
            (await scheduler.try_acquire(request(SU, "embed", tokens=8_193))).refusal,
            Refusal.CONTEXT_TOO_LONG,
        )

    async def test_on_the_gpu_the_kv_share_still_limits_the_context(self):
        spec = embedding_spec(kv_pool_bytes=10_000 * 1024, kv_bytes_per_token=1024)
        scheduler, *_ = build((main_spec(), spec), control=False)
        await scheduler.refresh()
        self.assertEqual(
            (await scheduler.try_acquire(request(SU, "embed", tokens=8_000))).refusal,
            Refusal.CONTEXT_TOO_LONG,
        )

    async def test_cpu_leases_do_not_need_the_probe(self):
        scheduler, probe, _, clock = build(
            (main_spec(), embedding_spec(initial=DeploymentState.CPU)), control=False
        )
        probe.fail = True
        await scheduler.refresh()
        self.assertIsNotNone((await scheduler.try_acquire(request(SU, "embed"))).lease)
        self.assertEqual(
            (await scheduler.try_acquire(request(CO))).refusal,
            Refusal.PROBE_UNAVAILABLE,
        )

    async def test_without_a_fresh_sample_gpu_admission_fails_closed(self):
        scheduler, probe, _, clock = build(probe_max_age_seconds=15)
        # Never sampled.
        self.assertEqual(
            (await scheduler.try_acquire(request())).refusal, Refusal.PROBE_UNAVAILABLE
        )
        await scheduler.refresh()
        self.assertIsNotNone((await scheduler.try_acquire(request())).lease)
        await clock.advance(16)  # the sample is too old
        self.assertEqual(
            (await scheduler.try_acquire(request())).refusal, Refusal.PROBE_UNAVAILABLE
        )
        probe.fail = True
        await scheduler.refresh()
        status = scheduler.status()
        self.assertFalse(status.probe_ok)
        self.assertEqual(
            (await scheduler.try_acquire(request())).refusal, Refusal.PROBE_UNAVAILABLE
        )
        probe.fail = False
        await scheduler.refresh()
        self.assertTrue(scheduler.status().probe_ok)

    async def test_the_configured_gpu_must_be_in_the_sample(self):
        scheduler, *_ = build(gpu_index=1)
        await scheduler.refresh()
        self.assertFalse(scheduler.status().probe_ok)
        self.assertEqual(
            (await scheduler.try_acquire(request())).refusal, Refusal.PROBE_UNAVAILABLE
        )

    async def test_status_reports_actual_reserved_and_headroom(self):
        status = self.scheduler.status()
        self.assertEqual(status.mode, SchedulerMode.NORMAL)
        vram = status.vram
        self.assertEqual(vram.total, 96 * GIB)
        self.assertEqual(vram.actual, 80 * GIB)
        self.assertEqual(vram.reserved, 80 * GIB)
        self.assertEqual(vram.headroom, int(96 * GIB * 0.05))
        self.assertEqual(vram.available, 96 * GIB - int(96 * GIB * 0.05) - 80 * GIB)
        self.assertEqual(status.utilization_percent, 10)
        main = status.deployment("main")
        self.assertEqual(main.capacity_tokens, CAPACITY)
        self.assertEqual(main.state, DeploymentState.GPU)
        self.assertIsNone(status.deployment("nope"))


class ValidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_requests_are_checked(self):
        cases = [
            dict(resource_class="coding", deployment="main"),
            dict(resource_class=CO, deployment=None),
            dict(resource_class=CO, deployment="main", context_tokens=-1),
            dict(resource_class=CO, deployment="main", context_tokens=True),
            # Decision 0042: a shared request may name the VRAM it allocates of
            # its own (``test_compute_free_vram.py``), never a negative amount,
            # a bool or more than any GPU.
            dict(resource_class=CO, deployment="main", vram_bytes=-1),
            dict(resource_class=CO, deployment="main", vram_bytes=True),
            dict(resource_class=CO, deployment="main", vram_bytes=(1 << 50) + 1),
            dict(resource_class=CO, deployment="main", vram_bytes=1.5),
            dict(resource_class=ResourceClass.EXCLUSIVE, vram_bytes=0),
            dict(
                resource_class=ResourceClass.EXCLUSIVE, deployment="main", vram_bytes=1
            ),
            dict(
                resource_class=ResourceClass.EXCLUSIVE, vram_bytes=1, allow_cloud=True
            ),
            dict(resource_class=CO, deployment="main", allow_cloud="yes"),
        ]
        for case in cases:
            with (
                self.subTest(case=case),
                self.assertRaises(InvalidComputeArgumentError),
            ):
                ComputeRequest(**case)

    async def test_an_unknown_deployment_is_refused_loudly(self):
        scheduler, *_ = build()
        await scheduler.refresh()
        with self.assertRaises(InvalidComputeArgumentError):
            await scheduler.try_acquire(request(deployment="nope"))
        with self.assertRaises(InvalidComputeArgumentError):
            await scheduler.acquire(request(deployment="nope"), wait_seconds=1)

    async def test_try_acquire_does_not_take_exclusive_requests(self):
        scheduler, *_ = build()
        with self.assertRaises(InvalidComputeArgumentError):
            await scheduler.try_acquire(
                ComputeRequest(ResourceClass.EXCLUSIVE, vram_bytes=GIB)
            )

    async def test_acquire_checks_its_timeouts(self):
        scheduler, *_ = build()
        for options in (
            dict(wait_seconds=-1),
            dict(wait_seconds=True),
            dict(wait_seconds=1, cloud_after_seconds=-1),
        ):
            with (
                self.subTest(options=options),
                self.assertRaises(InvalidComputeArgumentError),
            ):
                await scheduler.acquire(request(), **options)


class WaitingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler, self.probe, self.control, self.clock = build()
        await self.scheduler.refresh()
        # Fill the pool: 3 long interactive requests (105,000 of 117,964 tokens).
        self.held = [
            (await self.scheduler.try_acquire(request(IC, tokens=35_000))).lease
            for _ in range(3)
        ]

    async def test_a_waiter_is_granted_when_capacity_is_released(self):
        waiter = asyncio.create_task(
            self.scheduler.acquire(request(CO, tokens=30_000), wait_seconds=60)
        )
        await settle()
        self.assertFalse(waiter.done())
        self.assertEqual(self.scheduler.status().waiting[CO], 1)
        await self.held[0].release()
        lease = await waiter
        self.assertEqual(lease.placement, Placement.LOCAL_GPU)
        self.assertEqual(self.scheduler.status().waiting[CO], 0)

    async def test_a_waiter_times_out_with_the_reason(self):
        waiter = asyncio.create_task(
            self.scheduler.acquire(request(CO, tokens=30_000), wait_seconds=60)
        )
        await settle()
        await self.clock.advance(60)
        with self.assertRaises(ComputeUnavailableError) as raised:
            await waiter
        self.assertEqual(raised.exception.reason, Refusal.KV_FULL)
        self.assertEqual(self.scheduler.status().waiting[CO], 0)
        # Its capacity was never taken.
        await self.held[0].release()
        self.assertEqual(
            self.scheduler.status().deployment("main").reserved_tokens, 70_000
        )

    async def test_higher_classes_are_served_first_and_fifo_within_a_class(self):
        order = []

        async def wait(cls, name):
            lease = await self.scheduler.acquire(
                request(cls, tokens=30_000), wait_seconds=600
            )
            order.append(name)
            return lease

        tasks = [
            asyncio.create_task(wait(BG, "background")),
            asyncio.create_task(wait(CO, "coding-1")),
            asyncio.create_task(wait(IC, "interactive")),
            asyncio.create_task(wait(CO, "coding-2")),
        ]
        await settle()
        for held in self.held:
            await held.release()
            await settle()
        # 3 * 35,000 released: room for 3 of the 30,000-token waiters.
        self.assertEqual(order, ["interactive", "coding-1", "coding-2"])
        self.assertFalse(tasks[0].done())
        for task in tasks[1:]:
            await (await task).release()
        await settle()
        self.assertEqual(order[-1], "background")
        await (await tasks[0]).release()

    async def test_a_new_request_does_not_jump_the_queue(self):
        waiter = asyncio.create_task(
            self.scheduler.acquire(request(CO, tokens=30_000), wait_seconds=60)
        )
        await settle()
        # A small request of the same class would fit, but it arrived later.
        admission = await self.scheduler.try_acquire(request(CO, tokens=1_000))
        self.assertEqual(admission.refusal, Refusal.QUEUED_BEHIND)
        # A higher class is not behind a coding waiter.
        interactive = await self.scheduler.try_acquire(request(IC, tokens=1_000))
        self.assertIsNotNone(interactive.lease)
        waiter.cancel()

    async def test_a_cancelled_waiter_leaves_the_queue(self):
        waiter = asyncio.create_task(
            self.scheduler.acquire(request(CO, tokens=30_000), wait_seconds=60)
        )
        await settle()
        waiter.cancel()
        await settle()
        self.assertEqual(self.scheduler.status().waiting[CO], 0)
        await self.held[0].release()
        self.assertEqual(
            self.scheduler.status().deployment("main").reserved_tokens, 70_000
        )

    async def test_the_queue_is_bounded(self):
        scheduler, *_ = build(max_waiters=1)
        await scheduler.refresh()
        for _ in range(3):
            self.assertIsNotNone(
                (await scheduler.try_acquire(request(IC, tokens=35_000))).lease
            )
        first = asyncio.create_task(
            scheduler.acquire(request(tokens=30_000), wait_seconds=60)
        )
        await settle()
        with self.assertRaises(ComputeUnavailableError) as raised:
            await scheduler.acquire(request(tokens=30_000), wait_seconds=60)
        self.assertEqual(raised.exception.reason, Refusal.QUEUE_FULL)
        first.cancel()

    async def test_timeout_zero_does_not_wait(self):
        with self.assertRaises(ComputeUnavailableError):
            await self.scheduler.acquire(request(CO, tokens=30_000), wait_seconds=0)


class ServeTest(unittest.IsolatedAsyncioTestCase):
    async def test_serve_refreshes_until_stopped(self):
        scheduler, probe, _, clock = build()
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler.serve(stop, interval=5))
        await settle()
        self.assertEqual(probe.calls, 1)
        await clock.advance(5)
        self.assertEqual(probe.calls, 2)
        stop.set()
        await settle()
        await asyncio.wait_for(task, 1)
        self.assertEqual(probe.calls, 2)

    async def test_serve_goes_on_after_an_error(self):
        scheduler, probe, _, clock = build()

        async def broken():
            raise RuntimeError("boom")

        original = probe.sample
        probe.sample = broken
        stop = asyncio.Event()
        with self.assertLogs("paw_backend.compute", level="WARNING"):
            task = asyncio.create_task(scheduler.serve(stop, interval=5))
            await settle()
        probe.sample = original
        await clock.advance(5)
        self.assertTrue(scheduler.status().probe_ok)
        stop.set()
        await asyncio.wait_for(task, 1)

    async def test_serve_checks_its_arguments(self):
        scheduler, *_ = build()
        with self.assertRaises(InvalidComputeArgumentError):
            await scheduler.serve(object())
        with self.assertRaises(InvalidComputeArgumentError):
            await scheduler.serve(asyncio.Event(), interval=0)


class HybridPlacementTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler, self.probe, self.control, self.clock = build()
        await self.scheduler.refresh()

    async def test_local_first_when_there_is_room(self):
        lease = await self.scheduler.acquire(request(allow_cloud=True), wait_seconds=60)
        self.assertEqual(lease.placement, Placement.LOCAL_GPU)

    async def test_a_busy_local_gpu_sends_the_work_to_the_cloud(self):
        for _ in range(3):
            await self.scheduler.try_acquire(request(IC, tokens=35_000))
        lease = await self.scheduler.acquire(
            request(tokens=30_000, allow_cloud=True), wait_seconds=60
        )
        self.assertEqual(lease.placement, Placement.CLOUD)
        self.assertEqual(lease.tokens, 0)
        self.assertEqual(self.scheduler.status().cloud_leases, 1)
        await lease.release()
        self.assertEqual(self.scheduler.status().cloud_leases, 0)

    async def test_the_cloud_can_be_used_after_a_short_wait_for_local(self):
        held = [
            (await self.scheduler.try_acquire(request(IC, tokens=35_000))).lease
            for _ in range(3)
        ]
        waiter = asyncio.create_task(
            self.scheduler.acquire(
                request(tokens=30_000, allow_cloud=True),
                wait_seconds=600,
                cloud_after_seconds=30,
            )
        )
        await settle()
        self.assertFalse(waiter.done())
        await self.clock.advance(30)
        self.assertEqual((await waiter).placement, Placement.CLOUD)
        await self.scheduler.refresh()  # a fresh reading after the time moved
        # And local wins when capacity frees within the wait.
        waiter = asyncio.create_task(
            self.scheduler.acquire(
                request(tokens=30_000, allow_cloud=True),
                wait_seconds=600,
                cloud_after_seconds=30,
            )
        )
        await settle()
        await held[0].release()
        self.assertEqual((await waiter).placement, Placement.LOCAL_GPU)

    async def test_too_long_a_context_goes_to_the_cloud_when_allowed(self):
        lease = await self.scheduler.acquire(
            request(tokens=200_000, allow_cloud=True), wait_seconds=60
        )
        self.assertEqual(lease.placement, Placement.CLOUD)
        with self.assertRaises(ComputeUnavailableError) as raised:
            await self.scheduler.acquire(request(tokens=200_000), wait_seconds=60)
        # A request that can never fit is refused at once, not after the timeout.
        self.assertEqual(raised.exception.reason, Refusal.CONTEXT_TOO_LONG)

    async def test_no_fresh_probe_sends_allowed_work_to_the_cloud(self):
        self.probe.fail = True
        await self.scheduler.refresh()
        lease = await self.scheduler.acquire(request(allow_cloud=True), wait_seconds=60)
        self.assertEqual(lease.placement, Placement.CLOUD)


class ObservedOnGpuTest(unittest.IsolatedAsyncioTestCase):
    """``DeploymentStatus.observed_on_gpu``: what the GPU shows, beside the
    configured or acted-on ``state`` (Codex review #168)."""

    def observed(self, scheduler, name="main"):
        return scheduler.status().deployment(name).observed_on_gpu

    async def test_a_running_model_is_seen(self):
        scheduler, *_ = build()
        self.assertIs(self.observed(scheduler), False)  # no reading yet
        await scheduler.refresh()
        self.assertIs(self.observed(scheduler), True)

    async def test_a_configured_model_whose_runtime_is_not_running(self):
        scheduler, probe, control, _ = build()
        probe.resident.pop(control.pids.pop("main"))
        await scheduler.refresh()
        state = scheduler.status().deployment("main").state
        self.assertIs(state, DeploymentState.GPU)  # as configured
        self.assertIs(self.observed(scheduler), False)

    async def test_unknown_without_a_control_or_on_a_stale_reading(self):
        scheduler, *_ = build(control=False)
        await scheduler.refresh()
        self.assertIsNone(self.observed(scheduler))
        scheduler, _, _, clock = build()
        await scheduler.refresh()
        await clock.advance(3_600)
        self.assertIsNone(self.observed(scheduler))


if __name__ == "__main__":
    unittest.main()
