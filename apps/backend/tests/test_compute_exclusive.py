"""The Exclusive class (PAW-036): a job that needs the GPU to itself (Kaggle, a
Model Benchmark). New local GPU work stops, running work drains, every model is
unloaded (support models may move to the CPU), the scheduler confirms from the
probe that the workspace's processes are gone and the VRAM is free, and only then
grants the lease. Releasing it brings the models back.

The fakes simulate the GPU; nothing real is loaded, unloaded or stopped."""

import asyncio
import unittest

from paw_backend.compute import (
    ComputeRequest,
    ExclusiveFailure,
    ExclusiveUnavailableError,
    Placement,
    Refusal,
    ResourceClass,
    SchedulerMode,
)
from tests.compute_support import GIB, build, settle

EX = ResourceClass.EXCLUSIVE
CO = ResourceClass.CODING


def exclusive(vram=80 * GIB):
    return ComputeRequest(EX, vram_bytes=vram)


def coding(tokens=1_000, **options):
    return ComputeRequest(CO, deployment="main", context_tokens=tokens, **options)


class ExclusiveTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler, self.probe, self.control, self.clock = build()
        await self.scheduler.refresh()

    async def test_the_gpu_is_emptied_and_checked_before_the_lease(self):
        lease = await self.scheduler.acquire(exclusive(), wait_seconds=60)
        self.assertEqual(lease.placement, Placement.LOCAL_GPU)
        self.assertEqual(lease.vram_bytes, 80 * GIB)
        self.assertEqual(
            self.control.actions,
            [
                ("unload", "memory"),
                ("place:local_cpu", "embed"),  # the embedding model may use the CPU
                ("unload", "main"),
            ],
        )
        self.assertEqual(self.probe.resident, {})  # the fake GPU holds nothing of ours
        status = self.scheduler.status()
        self.assertEqual(status.mode, SchedulerMode.EXCLUSIVE)
        self.assertEqual(status.leases[EX], 1)
        self.assertEqual(status.vram.reserved, 80 * GIB)
        # Local GPU work is refused; CPU work goes on.
        self.assertEqual(
            (await self.scheduler.try_acquire(coding())).refusal, Refusal.EXCLUSIVE_MODE
        )
        embed = await self.scheduler.try_acquire(
            ComputeRequest(ResourceClass.SUPPORT, deployment="embed")
        )
        self.assertEqual(embed.lease.placement, Placement.LOCAL_CPU)
        await embed.lease.release()

        await lease.release()
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)
        self.control.actions.clear()
        for _ in range(4):
            await self.scheduler.refresh()
        # The models come back, the main model first.
        self.assertEqual(
            self.control.actions,
            [
                ("place:local_gpu", "main"),
                ("place:local_gpu", "memory"),
                ("place:local_gpu", "embed"),
            ],
        )
        self.assertIsNotNone((await self.scheduler.try_acquire(coding())).lease)

    async def test_running_work_is_drained_first(self):
        running = (await self.scheduler.try_acquire(coding())).lease
        job = asyncio.create_task(self.scheduler.acquire(exclusive(), wait_seconds=600))
        await settle()
        self.assertFalse(job.done())
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.DRAINING)
        self.assertEqual(self.control.actions, [])  # nothing unloaded under it
        self.assertEqual(
            (await self.scheduler.try_acquire(coding())).refusal, Refusal.EXCLUSIVE_MODE
        )
        await running.release()
        lease = await job
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.EXCLUSIVE)
        await lease.release()

    async def test_new_work_goes_to_the_cloud_while_exclusive(self):
        lease = await self.scheduler.acquire(exclusive(), wait_seconds=60)
        offloaded = await self.scheduler.acquire(
            coding(allow_cloud=True), wait_seconds=60
        )
        self.assertEqual(offloaded.placement, Placement.CLOUD)
        await lease.release()

    async def test_local_waiters_resume_after_the_exclusive_job(self):
        lease = await self.scheduler.acquire(exclusive(), wait_seconds=60)
        waiter = asyncio.create_task(self.scheduler.acquire(coding(), wait_seconds=600))
        await settle()
        self.assertFalse(waiter.done())
        await lease.release()
        await self.scheduler.refresh()  # the main model is loaded again
        granted = await waiter
        self.assertEqual(granted.placement, Placement.LOCAL_GPU)

    async def test_a_drain_that_takes_too_long_gives_up_and_changes_nothing(self):
        running = (await self.scheduler.try_acquire(coding())).lease
        job = asyncio.create_task(self.scheduler.acquire(exclusive(), wait_seconds=30))
        await settle()
        await self.clock.advance(30)
        with self.assertRaises(ExclusiveUnavailableError) as raised:
            await job
        self.assertEqual(raised.exception.failure, ExclusiveFailure.DRAIN_TIMEOUT)
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)
        self.assertEqual(self.control.actions, [])
        self.assertFalse(
            running.revoked.is_set()
        )  # a running coding task is not stopped
        await running.release()

    async def test_only_one_exclusive_job_at_a_time(self):
        lease = await self.scheduler.acquire(exclusive(), wait_seconds=60)
        with self.assertRaises(ExclusiveUnavailableError) as raised:
            await self.scheduler.acquire(exclusive(), wait_seconds=60)
        self.assertEqual(raised.exception.failure, ExclusiveFailure.BUSY)
        await lease.release()

    async def test_memory_that_is_not_freed_refuses_the_lease(self):
        self.control.linger.add("main")  # the runtime did not give its memory back
        with self.assertRaises(ExclusiveUnavailableError) as raised:
            await self._acquire_with_time(exclusive(), verify=61)
        self.assertEqual(raised.exception.failure, ExclusiveFailure.NOT_FREED)
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)

    async def test_another_workloads_memory_refuses_a_lease_that_would_not_fit(self):
        self.probe.external = 20 * GIB
        with self.assertRaises(ExclusiveUnavailableError) as raised:
            await self._acquire_with_time(exclusive(80 * GIB), verify=61)
        self.assertEqual(raised.exception.failure, ExclusiveFailure.NOT_FREED)

    async def test_a_process_of_the_workspace_still_on_the_gpu_refuses_the_lease(self):
        # The runtime reports a pid that still holds memory after the unload.
        original = self.control.processes

        async def processes(name):
            return frozenset({4242}) if name == "main" else await original(name)

        self.control.processes = processes
        self.probe.resident[4242] = 1 * GIB
        with self.assertRaises(ExclusiveUnavailableError) as raised:
            await self._acquire_with_time(exclusive(10 * GIB), verify=61)
        self.assertEqual(raised.exception.failure, ExclusiveFailure.NOT_FREED)

    async def test_an_unload_that_fails_aborts(self):
        self.control.fail.add(("unload", "main"))
        with self.assertLogs("paw_backend.compute", level="WARNING"):
            with self.assertRaises(ExclusiveUnavailableError) as raised:
                await self.scheduler.acquire(exclusive(), wait_seconds=60)
        self.assertEqual(raised.exception.failure, ExclusiveFailure.CANNOT_UNLOAD)
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)

    async def test_without_a_model_control_the_gpu_cannot_be_emptied(self):
        scheduler, *_ = build(control=False)
        await scheduler.refresh()
        with self.assertRaises(ExclusiveUnavailableError) as raised:
            await scheduler.acquire(exclusive(), wait_seconds=60)
        self.assertEqual(raised.exception.failure, ExclusiveFailure.CANNOT_UNLOAD)

    async def test_without_a_probe_nothing_is_unloaded(self):
        # The release of the VRAM could not be confirmed: refuse before acting.
        self.probe.fail = True
        with self.assertRaises(ExclusiveUnavailableError) as raised:
            await self.scheduler.acquire(exclusive(), wait_seconds=60)
        self.assertEqual(raised.exception.failure, ExclusiveFailure.PROBE_UNAVAILABLE)
        self.assertEqual(self.control.actions, [])
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)

    async def test_a_probe_that_fails_during_the_check_refuses_the_lease(self):
        original = self.control.unload

        async def unload(name):
            await original(name)
            self.probe.fail = True

        self.control.unload = unload
        with self.assertRaises(ExclusiveUnavailableError) as raised:
            await self._acquire_with_time(exclusive(), verify=61)
        self.assertEqual(raised.exception.failure, ExclusiveFailure.NOT_FREED)

    async def _acquire_with_time(self, request, *, verify):
        job = asyncio.create_task(self.scheduler.acquire(request, wait_seconds=60))
        await settle()
        for _ in range(verify):
            if job.done():
                break
            await self.clock.advance(1)
        return await job


if __name__ == "__main__":
    unittest.main()
