"""VRAM pressure and model residency (PAW-036): the relief steps in the order the
requirements give (background stopped, Memory Worker unloaded, Embedding /
Reranker on the CPU, admission suppressed, context reduced, a human asked to
change the main model), the way back, and the residency policy.

The fakes simulate the GPU: a model the scheduler unloads frees its memory on the
fake probe. Nothing loads, unloads or allocates anything real."""

import asyncio
import unittest

from paw_backend.compute import (
    ComputeRequest,
    DeploymentState,
    Placement,
    Refusal,
    Relief,
    ResidencyPolicy,
    ResourceClass,
)
from tests.compute_support import (
    GIB,
    build,
    embedding_spec,
    main_spec,
    memory_spec,
    settle,
)

IC = ResourceClass.INTERACTIVE
CO = ResourceClass.CODING
SU = ResourceClass.SUPPORT
BG = ResourceClass.BACKGROUND


def request(cls=CO, deployment="main", tokens=1_000):
    return ComputeRequest(cls, deployment=deployment, context_tokens=tokens)


class PressureTest(unittest.IsolatedAsyncioTestCase):
    async def test_background_is_stopped_first(self):
        scheduler, probe, control, _ = build()
        await scheduler.refresh()
        background = (await scheduler.try_acquire(request(BG))).lease
        coding = (await scheduler.try_acquire(request(CO))).lease
        probe.external = 12 * GIB  # another workload takes VRAM
        status = await scheduler.refresh()
        self.assertTrue(status.vram.under_pressure)
        self.assertEqual(status.relief, Relief.BACKGROUND_STOPPED)
        self.assertTrue(background.revoked.is_set())
        self.assertFalse(coding.revoked.is_set())
        self.assertEqual(
            (await scheduler.try_acquire(request(BG))).refusal,
            Refusal.BACKGROUND_PAUSED,
        )
        self.assertIsNotNone((await scheduler.try_acquire(request(CO))).lease)
        self.assertEqual(control.actions, [])

    async def test_then_the_memory_worker_is_unloaded(self):
        scheduler, probe, control, _ = build()
        await scheduler.refresh()
        probe.external = 12 * GIB
        await scheduler.refresh()
        status = await scheduler.refresh()
        self.assertEqual(control.actions, [("unload", "memory")])
        self.assertEqual(status.relief, Relief.MEMORY_WORKER_UNLOADED)
        self.assertEqual(status.deployment("memory").state, DeploymentState.UNLOADED)
        self.assertEqual(
            (await scheduler.try_acquire(request(BG, "memory"))).refusal,
            Refusal.NOT_RESIDENT,
        )
        # The freed 12 GiB end the pressure; nothing else is touched.
        status = await scheduler.refresh()
        self.assertFalse(status.vram.under_pressure)
        self.assertEqual(status.relief, Relief.MEMORY_WORKER_UNLOADED)
        self.assertEqual(control.actions, [("unload", "memory")])

    async def test_every_step_in_order_and_the_main_model_is_kept(self):
        scheduler, probe, control, _ = build()
        await scheduler.refresh()
        probe.external = 30 * GIB
        steps = []
        for _ in range(8):
            steps.append((await scheduler.refresh()).relief)
        self.assertEqual(
            steps,
            [
                Relief.BACKGROUND_STOPPED,
                Relief.MEMORY_WORKER_UNLOADED,
                Relief.SUPPORT_ON_CPU,
                Relief.ADMISSION_SUPPRESSED,
                Relief.CONTEXT_REDUCED,
                Relief.MAIN_CHANGE_NEEDED,
                Relief.MAIN_CHANGE_NEEDED,
                Relief.MAIN_CHANGE_NEEDED,
            ],
        )
        self.assertEqual(
            control.actions, [("unload", "memory"), ("place:local_cpu", "embed")]
        )
        status = scheduler.status()
        self.assertTrue(status.needs_human)
        self.assertEqual(status.deployment("main").state, DeploymentState.GPU)
        self.assertEqual(status.deployment("embed").state, DeploymentState.CPU)
        # Embedding requests now run on the CPU.
        embed = (await scheduler.try_acquire(request(SU, "embed"))).lease
        self.assertEqual(embed.placement, Placement.LOCAL_CPU)
        # New local requests are held back, except interactive ones...
        self.assertEqual(
            (await scheduler.try_acquire(request(CO))).refusal,
            Refusal.ADMISSION_SUPPRESSED,
        )
        # ... whose context is reduced (half of 65,536).
        self.assertIsNotNone(
            (await scheduler.try_acquire(request(IC, tokens=32_768))).lease
        )
        self.assertEqual(
            (await scheduler.try_acquire(request(IC, tokens=32_769))).refusal,
            Refusal.CONTEXT_REDUCED,
        )

    async def test_a_busy_memory_worker_is_drained_before_it_is_unloaded(self):
        scheduler, probe, control, _ = build()
        await scheduler.refresh()
        job = (await scheduler.try_acquire(request(BG, "memory"))).lease
        probe.external = 30 * GIB
        await scheduler.refresh()  # background stopped: the job is asked to stop
        self.assertTrue(job.revoked.is_set())
        status = await scheduler.refresh()
        self.assertEqual(control.actions, [])  # it still runs
        self.assertTrue(status.deployment("memory").draining)
        self.assertEqual(status.relief, Relief.BACKGROUND_STOPPED)
        # No new work reaches a draining deployment.
        self.assertEqual(
            (await scheduler.try_acquire(request(SU, "memory"))).refusal,
            Refusal.NOT_RESIDENT,
        )
        await scheduler.refresh()
        self.assertEqual(control.actions, [])
        await job.release()
        status = await scheduler.refresh()
        self.assertEqual(control.actions, [("unload", "memory")])
        self.assertEqual(status.relief, Relief.MEMORY_WORKER_UNLOADED)

    async def test_a_drain_is_called_off_when_the_pressure_ends(self):
        scheduler, probe, control, _ = build()
        await scheduler.refresh()
        job = (await scheduler.try_acquire(request(BG, "memory"))).lease
        probe.external = 30 * GIB
        await scheduler.refresh()
        status = await scheduler.refresh()
        self.assertTrue(status.deployment("memory").draining)
        probe.external = 0
        status = await scheduler.refresh()
        self.assertFalse(status.deployment("memory").draining)
        await job.release()
        for _ in range(3):
            await scheduler.refresh()
        self.assertEqual(control.actions, [])  # the memory worker stayed
        self.assertIsNotNone((await scheduler.try_acquire(request(BG, "memory"))).lease)

    async def test_a_support_model_without_cpu_fallback_is_unloaded(self):
        specs = (main_spec(), memory_spec(), embedding_spec(cpu_fallback=False))
        scheduler, probe, control, _ = build(specs)
        await scheduler.refresh()
        probe.external = 30 * GIB
        for _ in range(3):
            await scheduler.refresh()
        self.assertEqual(control.actions, [("unload", "memory"), ("unload", "embed")])

    async def test_an_always_resident_support_model_without_fallback_stays(self):
        specs = (
            main_spec(),
            memory_spec(),
            embedding_spec(cpu_fallback=False, residency=ResidencyPolicy.ALWAYS),
        )
        scheduler, probe, control, _ = build(specs)
        await scheduler.refresh()
        probe.external = 30 * GIB
        for _ in range(3):
            await scheduler.refresh()
        self.assertEqual(control.actions, [("unload", "memory")])

    async def test_memory_that_lingers_after_an_unload_keeps_the_pressure(self):
        scheduler, probe, control, _ = build()
        control.linger.add("memory")  # the runtime did not really give it back
        await scheduler.refresh()
        probe.external = 12 * GIB
        for _ in range(3):
            status = await scheduler.refresh()
        self.assertTrue(status.vram.under_pressure)  # counted as external now
        self.assertEqual(status.relief, Relief.SUPPORT_ON_CPU)

    async def test_a_failed_action_is_recorded_and_the_steps_go_on(self):
        scheduler, probe, control, _ = build()
        control.fail.add(("unload", "memory"))
        await scheduler.refresh()
        probe.external = 30 * GIB
        with self.assertLogs("paw_backend.compute", level="WARNING") as logs:
            await scheduler.refresh()
            status = await scheduler.refresh()
        self.assertEqual(status.deployment("memory").state, DeploymentState.FAILED)
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("fake failure", "\n".join(logs.output))
        # A failed model is counted as still holding its memory.
        self.assertEqual(status.vram.reserved, 80 * GIB)
        status = await scheduler.refresh()
        self.assertEqual(status.relief, Relief.SUPPORT_ON_CPU)

    async def test_without_a_model_control_the_scheduler_only_observes(self):
        scheduler, probe, _, _ = build(control=False)
        await scheduler.refresh()
        probe.external = 30 * GIB
        steps = [(await scheduler.refresh()).relief for _ in range(4)]
        self.assertEqual(
            steps,
            [
                Relief.BACKGROUND_STOPPED,
                Relief.MEMORY_WORKER_UNLOADED,
                Relief.SUPPORT_ON_CPU,
                Relief.ADMISSION_SUPPRESSED,
            ],
        )
        status = scheduler.status()
        self.assertEqual(status.deployment("memory").state, DeploymentState.GPU)
        self.assertEqual(status.deployment("embed").state, DeploymentState.GPU)

    async def test_nothing_is_done_without_a_fresh_sample(self):
        scheduler, probe, control, _ = build()
        await scheduler.refresh()
        probe.external = 30 * GIB
        probe.fail = True
        for _ in range(4):
            status = await scheduler.refresh()
        self.assertEqual(status.relief, Relief.NONE)
        self.assertEqual(control.actions, [])


class RestoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_steps_are_undone_in_reverse_once_there_is_room(self):
        scheduler, probe, control, _ = build()
        await scheduler.refresh()
        probe.external = 30 * GIB
        for _ in range(6):
            await scheduler.refresh()
        control.actions.clear()
        probe.external = 0
        steps = [(await scheduler.refresh()).relief for _ in range(7)]
        self.assertEqual(
            steps,
            [
                Relief.CONTEXT_REDUCED,
                Relief.ADMISSION_SUPPRESSED,
                Relief.SUPPORT_ON_CPU,
                Relief.MEMORY_WORKER_UNLOADED,  # the embedding model is back
                Relief.BACKGROUND_STOPPED,  # the memory worker is back
                Relief.NONE,
                Relief.NONE,
            ],
        )
        self.assertEqual(
            control.actions,
            [("place:local_gpu", "embed"), ("place:local_gpu", "memory")],
        )
        status = scheduler.status()
        self.assertFalse(status.needs_human)
        for name in ("main", "memory", "embed"):
            self.assertEqual(status.deployment(name).state, DeploymentState.GPU)
        self.assertIsNotNone((await scheduler.try_acquire(request(BG))).lease)

    async def test_a_model_is_brought_back_only_with_a_margin(self):
        scheduler, probe, control, _ = build()
        await scheduler.refresh()
        probe.external = 12 * GIB
        for _ in range(2):
            await scheduler.refresh()  # the memory worker was unloaded
        control.actions.clear()
        # 11.2 GiB free: the 12 GiB worker does not fit with a margin.
        for _ in range(3):
            status = await scheduler.refresh()
        self.assertEqual(control.actions, [])
        self.assertEqual(status.relief, Relief.MEMORY_WORKER_UNLOADED)
        probe.external = 5 * GIB  # 18.2 GiB free: 12 + 4.8 of margin fit
        await scheduler.refresh()
        self.assertEqual(control.actions, [("place:local_gpu", "memory")])


class ResidencyTest(unittest.IsolatedAsyncioTestCase):
    async def test_models_are_loaded_when_they_fit(self):
        specs = (
            main_spec(initial=DeploymentState.UNLOADED),
            memory_spec(initial=DeploymentState.UNLOADED),
            embedding_spec(initial=DeploymentState.UNLOADED),
        )
        scheduler, probe, control, _ = build(specs)
        for _ in range(4):
            await scheduler.refresh()
        # One action per refresh, the main model first.
        self.assertEqual(
            control.actions,
            [
                ("place:local_gpu", "main"),
                ("place:local_gpu", "memory"),
                ("place:local_gpu", "embed"),
            ],
        )

    async def test_an_if_room_model_is_not_loaded_without_room(self):
        scheduler, probe, control, _ = build(
            (main_spec(), memory_spec(initial=DeploymentState.UNLOADED)),
            external=15 * GIB,
        )
        for _ in range(3):
            await scheduler.refresh()
        self.assertEqual(control.actions, [])

    async def test_an_always_model_is_not_loaded_into_pressure(self):
        scheduler, probe, control, _ = build(
            (main_spec(initial=DeploymentState.UNLOADED),), external=40 * GIB
        )
        for _ in range(3):
            await scheduler.refresh()
        self.assertEqual(control.actions, [])

    async def test_a_failed_load_is_retried_after_a_delay(self):
        scheduler, probe, control, clock = build(
            (main_spec(), memory_spec(initial=DeploymentState.UNLOADED))
        )
        control.fail.add(("place:local_gpu", "memory"))
        with self.assertLogs("paw_backend.compute", level="WARNING"):
            await scheduler.refresh()
        self.assertEqual(
            scheduler.status().deployment("memory").state, DeploymentState.FAILED
        )
        control.fail.clear()
        await scheduler.refresh()
        self.assertEqual(len(control.actions), 1)  # not yet
        await clock.advance(61)
        await scheduler.refresh()
        self.assertEqual(len(control.actions), 2)
        self.assertEqual(
            scheduler.status().deployment("memory").state, DeploymentState.GPU
        )

    async def test_the_scheduler_does_not_load_without_a_control(self):
        scheduler, probe, _, _ = build(
            (main_spec(initial=DeploymentState.UNLOADED),), control=False
        )
        await scheduler.refresh()
        self.assertEqual(
            scheduler.status().deployment("main").state, DeploymentState.UNLOADED
        )


if __name__ == "__main__":
    unittest.main()


class ParentPidTest(unittest.IsolatedAsyncioTestCase):
    """vLLM / SGLang hold the GPU memory in a child of the unit's MainPID. A pids
    command that names only the parent must not make the model's memory count
    twice (once reserved, once external) and start the relief steps."""

    async def test_memory_held_by_a_child_does_not_cause_pressure(self):
        scheduler, probe, control, _ = build()
        original = control.processes

        async def parent_only(name):
            # The parent (not on the GPU), not the child that holds the memory.
            return frozenset(pid + 100_000 for pid in await original(name))

        control.processes = parent_only
        for _ in range(8):
            await scheduler.refresh()
        status = scheduler.status()
        self.assertEqual(status.relief, Relief.NONE)
        self.assertFalse(status.needs_human)
        self.assertEqual(status.vram.external, 0)
        self.assertGreater(status.vram.available, 0)
        self.assertEqual(control.actions, [])
        admission = await scheduler.try_acquire(request())
        self.assertIsNotNone(admission.lease)
        await admission.lease.release()


class SlowActionTest(unittest.IsolatedAsyncioTestCase):
    """A model action (a load can take minutes) does not stop the probe: the
    main model keeps admitting while another deployment loads."""

    async def test_a_slow_load_of_another_model_does_not_stop_main_admissions(self):
        scheduler, probe, control, clock = build(
            (
                main_spec(),
                memory_spec(initial=DeploymentState.UNLOADED),
            )
        )
        control.gate = asyncio.Event()
        refresh = asyncio.create_task(scheduler.refresh())
        await settle()
        self.assertEqual(control.actions, [("place:local_gpu", "memory")])
        self.assertFalse(refresh.done())
        await clock.advance(20)  # longer than the probe's maximum age (15 s)
        for _ in range(4):
            await clock.advance(5)
        self.assertTrue(scheduler.status().probe_ok)
        admission = await scheduler.try_acquire(
            ComputeRequest(IC, deployment="main", context_tokens=1_000)
        )
        self.assertIsNone(admission.refusal)
        await admission.lease.release()
        self.assertEqual(
            (await scheduler.try_acquire(request(BG, "memory"))).refusal,
            Refusal.NOT_RESIDENT,
        )
        control.gate.set()
        await refresh
        self.assertEqual(
            scheduler.status().deployment("memory").state, DeploymentState.GPU
        )
        # One action at a time: the loop did not start a second one.
        self.assertEqual(control.actions, [("place:local_gpu", "memory")])
