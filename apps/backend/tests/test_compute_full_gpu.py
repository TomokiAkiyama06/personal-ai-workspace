"""Kaggle / Full GPU Mode (PAW-037, Decision 0055 Proposed).

Start: new local GPU work stops, the tasks whose work holds or waits for the GPU
are held, running work drains (or, with ``preempt``, is asked to stop after the
drain time), the models are unloaded and the probe confirms the VRAM is free.
End: the models come back and, once the main LLM is on the GPU, the held tasks
resume. Only the Owner and Admins may start or end it (audited).

The fakes simulate the GPU and the task store; nothing real is loaded, unloaded,
stopped or signalled.
"""

import asyncio
import dataclasses
import unittest
import uuid

from paw_backend.authz import Authorizer, InMemoryAuditSink, Reason, SystemRole
from paw_backend.compute import (
    ComputeRequest,
    ComputeUnavailableError,
    DeploymentState,
    ExclusiveFailure,
    ExclusiveUnavailableError,
    FullGpuMode,
    FullGpuModeStateError,
    FullGpuPermissionDeniedError,
    FullGpuState,
    HybridRuntime,
    InvalidComputeArgumentError,
    Placement,
    Refusal,
    ResourceClass,
    ResumeReport,
    SchedulerMode,
)
from paw_backend.orchestrator import NodeOutcome, NodeResult
from tests.authz_support import principal, uid
from tests.compute_support import GIB, build, embedding_spec, main_spec, settle
from tests.test_compute_runtimes import assignment

CO = ResourceClass.CODING
IC = ResourceClass.INTERACTIVE
ADMIN = principal(SystemRole.ADMIN, uid(1))
OWNER = principal(SystemRole.OWNER, uid(2))
USER = principal(SystemRole.USER, uid(3))


def coding(task_id=None, tokens=1_000, **options):
    return ComputeRequest(
        CO, deployment="main", context_tokens=tokens, task_id=task_id, **options
    )


class FakeHolds:
    """The task store: ``running`` tasks can be held; ``held`` ones resume."""

    def __init__(self) -> None:
        self.running: set[uuid.UUID] = set()
        self.held: list[uuid.UUID] = []
        self.resumed: list[uuid.UUID] = []
        self.stuck: set[uuid.UUID] = set()  # cannot resume now
        self.fail_hold = False

    async def hold(self, task_id):
        if self.fail_hold:
            raise RuntimeError("database unavailable")
        if task_id not in self.running:
            return False
        self.running.discard(task_id)
        self.held.append(task_id)
        return True

    async def resume_held(self):
        resumed = [task for task in self.held if task not in self.stuck]
        for task in resumed:
            self.held.remove(task)
            self.running.add(task)
            self.resumed.append(task)
        return ResumeReport(len(resumed), len(self.held))


class FullGpuTestCase(unittest.IsolatedAsyncioTestCase):
    specs = None

    async def asyncSetUp(self):
        self.scheduler, self.probe, self.control, self.clock = build(self.specs)
        await self.scheduler.refresh()
        self.holds = FakeHolds()
        self.audit = InMemoryAuditSink()
        self.mode = FullGpuMode(
            self.scheduler,
            self.holds,
            Authorizer(self.audit),
            drain_seconds=60,
            preempt_seconds=30,
            reload_seconds=120,
            sweep_seconds=5,
            clock=self.clock,
        )
        # Nothing was held by an earlier process: the first tick finds nothing.
        await self.mode.tick()
        self.assertEqual(self.mode.status().state, FullGpuState.OFF)

    async def running_task(self):
        task_id = uuid.uuid4()
        self.holds.running.add(task_id)
        lease = (await self.scheduler.try_acquire(coding(task_id))).lease
        self.assertIsNotNone(lease)
        return task_id, lease

    async def reload(self, rounds=4):
        for _ in range(rounds):
            await self.scheduler.refresh()


class AuthorizationTest(FullGpuTestCase):
    async def test_a_user_may_not_start_it_and_nothing_changes(self):
        task_id, lease = await self.running_task()
        with self.assertRaises(FullGpuPermissionDeniedError) as caught:
            await self.mode.start(USER)
        self.assertEqual(caught.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)
        self.assertEqual(self.control.actions, [])
        self.assertEqual(self.holds.held, [])
        self.assertIn(task_id, self.holds.running)
        (event,) = self.audit.events
        self.assertEqual(
            (event.action, event.decision, event.actor_id),
            ("admin.compute.full_gpu", "deny", USER.user_id),
        )
        await lease.release()

    async def test_nobody_may_start_it(self):
        with self.assertRaises(FullGpuPermissionDeniedError) as caught:
            await self.mode.start(None)
        self.assertEqual(caught.exception.reason, Reason.UNAUTHENTICATED)

    async def test_an_admin_and_the_owner_may_and_are_audited(self):
        await self.mode.start(ADMIN)
        with self.assertRaises(FullGpuPermissionDeniedError):
            await self.mode.end(USER)
        self.assertEqual(self.mode.status().state, FullGpuState.ON)
        await self.mode.end(OWNER)
        self.assertEqual(
            [(e.action, e.decision, e.actor_id) for e in self.audit.events],
            [
                ("admin.compute.full_gpu", "allow", ADMIN.user_id),
                ("admin.compute.full_gpu", "deny", USER.user_id),
                ("admin.compute.full_gpu", "allow", OWNER.user_id),
            ],
        )

    async def test_an_unrecorded_decision_starts_nothing(self):
        class FailingSink:
            async def record(self, event):
                raise RuntimeError("audit store down")

        mode = FullGpuMode(
            self.scheduler, self.holds, Authorizer(FailingSink()), clock=self.clock
        )
        with self.assertRaises(FullGpuPermissionDeniedError) as caught:
            await mode.start(ADMIN)
        self.assertEqual(caught.exception.reason, Reason.AUDIT_UNAVAILABLE)
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)


class StartTest(FullGpuTestCase):
    async def test_the_gpu_is_emptied_confirmed_and_new_local_work_stops(self):
        status = await self.mode.start(ADMIN)
        self.assertEqual(status.state, FullGpuState.ON)
        self.assertIsNotNone(status.on_seconds)
        self.assertEqual(
            self.control.actions,
            [
                ("unload", "memory"),  # 4. the Memory Worker
                ("place:local_cpu", "embed"),  # 5. Embedding to the CPU
                ("unload", "main"),  # 6. the main LLM
            ],
        )
        self.assertEqual(self.probe.resident, {})  # 7. nothing of ours on the GPU
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.EXCLUSIVE)
        # 1. New local GPU work is refused; it may go to the cloud.
        refused = await self.scheduler.try_acquire(coding(uuid.uuid4()))
        self.assertEqual(refused.refusal, Refusal.EXCLUSIVE_MODE)
        cloud = await self.scheduler.acquire(coding(allow_cloud=True), wait_seconds=10)
        self.assertEqual(cloud.placement, Placement.CLOUD)
        await cloud.release()

    async def test_the_default_asks_for_the_whole_gpu_but_the_headroom(self):
        self.probe.external = 6 * GIB  # another workload keeps its memory
        await self.scheduler.refresh()
        view = self.scheduler.status().vram
        await self.mode.start(ADMIN)
        lease = self.mode._lease
        self.assertEqual(lease.vram_bytes, view.total - view.headroom - view.external)

    async def test_running_tasks_are_held_and_drain_before_the_unload(self):
        first, first_lease = await self.running_task()
        second, second_lease = await self.running_task()
        start = asyncio.create_task(self.mode.start(OWNER))
        await settle()
        self.assertEqual(self.mode.status().state, FullGpuState.STARTING)
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.DRAINING)
        # 2. / 3. Both tasks are held (waiting for a resource): nothing new of
        # them starts, and their running work is not cut short.
        self.assertCountEqual(self.holds.held, [first, second])
        self.assertFalse(first_lease.revoked.is_set())
        self.assertEqual(self.control.actions, [])  # nothing unloaded under them
        await first_lease.release()
        await settle()
        self.assertFalse(start.done())
        await second_lease.release()
        status = await start
        self.assertEqual(status.state, FullGpuState.ON)
        self.assertEqual(status.held_tasks, 2)
        self.assertFalse(status.preempted)

    async def test_work_that_is_not_a_tasks_or_not_on_the_gpu_is_not_held(self):
        chat = (
            await self.scheduler.try_acquire(
                ComputeRequest(IC, deployment="main", context_tokens=100)
            )
        ).lease
        cpu_task = uuid.uuid4()
        self.holds.running.add(cpu_task)
        # The embedding model serves from its CPU copy.
        await self.control.place("embed", Placement.LOCAL_CPU)
        self.scheduler._deployments["embed"].state = DeploymentState.CPU
        on_cpu = (
            await self.scheduler.try_acquire(
                ComputeRequest(
                    ResourceClass.SUPPORT, deployment="embed", task_id=cpu_task
                )
            )
        ).lease
        self.assertEqual(on_cpu.placement, Placement.LOCAL_CPU)
        start = asyncio.create_task(self.mode.start(ADMIN))
        await settle()
        self.assertEqual(self.holds.held, [])
        await chat.release()  # the chat drains like any local GPU work
        await start
        self.assertIn(cpu_task, self.holds.running)
        await on_cpu.release()

    async def test_a_task_whose_work_waits_for_the_gpu_is_held_during_the_mode(self):
        await self.mode.start(ADMIN)
        late = uuid.uuid4()
        self.holds.running.add(late)
        waiter = asyncio.create_task(
            self.scheduler.acquire(coding(late), wait_seconds=600)
        )
        await settle()
        self.assertEqual(self.holds.held, [])
        await self.mode.tick()
        self.assertEqual(self.holds.held, [late])
        self.assertEqual(self.mode.status().held_tasks, 1)
        await self.mode.tick()  # held once
        self.assertEqual(self.holds.held, [late])
        await self.mode.end(ADMIN)
        await self.reload()
        granted = await waiter  # its node goes on once the main LLM is back
        self.assertEqual(granted.placement, Placement.LOCAL_GPU)
        await granted.release()

    async def test_a_task_refused_without_waiting_is_held_too(self):
        # Codex review (#161, P2): a request that does not wait (``wait_seconds``
        # 0, ``try_acquire``) or finds the line full is never a waiter.
        await self.mode.start(ADMIN)
        no_wait, tried, crowded = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.holds.running.update({no_wait, tried, crowded})
        with self.assertRaises(ComputeUnavailableError):
            await self.scheduler.acquire(coding(no_wait), wait_seconds=0)
        refused = await self.scheduler.try_acquire(coding(tried))
        self.assertEqual(refused.refusal, Refusal.EXCLUSIVE_MODE)
        self.scheduler._config = dataclasses.replace(
            self.scheduler.config, max_waiters=1
        )
        chat = asyncio.create_task(  # not a task's: fills the line of one
            self.scheduler.acquire(
                ComputeRequest(IC, deployment="main", context_tokens=100),
                wait_seconds=600,
            )
        )
        await settle()
        with self.assertRaises(ComputeUnavailableError) as caught:
            await self.scheduler.acquire(coding(crowded), wait_seconds=600)
        self.assertEqual(caught.exception.reason, Refusal.QUEUE_FULL)
        chat.cancel()
        await self.mode.tick()
        self.assertCountEqual(self.holds.held, [no_wait, tried, crowded])
        await self.mode.end(ADMIN)
        # Forgotten once the Exclusive job ended.
        self.assertEqual(self.scheduler.gpu_task_ids(), frozenset())

    async def test_refusals_outside_the_mode_are_not_remembered(self):
        _, lease = await self.running_task()
        too_long = await self.scheduler.try_acquire(coding(uuid.uuid4(), tokens=70_000))
        self.assertEqual(too_long.refusal, Refusal.CONTEXT_TOO_LONG)
        self.assertEqual(self.scheduler.gpu_task_ids(), {lease.task_id})
        await lease.release()

    async def test_a_task_that_cannot_be_held_now_is_tried_again(self):
        self.holds.fail_hold = True
        task_id, lease = await self.running_task()
        start = asyncio.create_task(self.mode.start(ADMIN))
        await settle()
        self.assertEqual(self.holds.held, [])
        self.holds.fail_hold = False
        await self.clock.advance(5)  # the next sweep
        self.assertEqual(self.holds.held, [task_id])
        await lease.release()
        await start

    async def test_a_drain_that_takes_too_long_gives_up_and_the_tasks_resume(self):
        task_id, lease = await self.running_task()
        start = asyncio.create_task(self.mode.start(ADMIN))
        await settle()
        self.assertEqual(self.holds.held, [task_id])
        for _ in range(12):
            await self.clock.advance(5)
        with self.assertRaises(ExclusiveUnavailableError) as caught:
            await start
        self.assertEqual(caught.exception.failure, ExclusiveFailure.DRAIN_TIMEOUT)
        self.assertFalse(lease.revoked.is_set())  # not preempted
        self.assertEqual(self.control.actions, [])  # nothing was unloaded
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)
        status = self.mode.status()
        self.assertEqual(status.state, FullGpuState.RESUMING)
        self.assertEqual(status.last_failure, ExclusiveFailure.DRAIN_TIMEOUT)
        await self.mode.tick()  # the main LLM never left: they resume at once
        self.assertEqual(self.holds.resumed, [task_id])
        self.assertEqual(self.mode.status().state, FullGpuState.OFF)
        await lease.release()

    async def test_preempt_asks_the_work_still_running_to_stop(self):
        task_id, lease = await self.running_task()
        chat = (
            await self.scheduler.try_acquire(
                ComputeRequest(IC, deployment="main", context_tokens=100)
            )
        ).lease

        async def holder(held):
            await held.revoked.wait()  # a cooperative holder stops and releases
            await held.release()

        holders = [asyncio.create_task(holder(held)) for held in (lease, chat)]
        start = asyncio.create_task(self.mode.start(ADMIN, preempt=True))
        await settle()
        await self.clock.advance(55)
        self.assertFalse(lease.revoked.is_set())  # the drain time first
        await self.clock.advance(5)
        self.assertTrue(lease.revoked.is_set())
        self.assertTrue(chat.revoked.is_set())
        status = await start
        await asyncio.gather(*holders)
        self.assertEqual(status.state, FullGpuState.ON)
        self.assertTrue(status.preempted)
        self.assertEqual(self.holds.held, [task_id])

    async def test_preempted_work_that_does_not_stop_gives_up_too(self):
        _, lease = await self.running_task()
        start = asyncio.create_task(self.mode.start(ADMIN, preempt=True))
        await settle()
        for _ in range(12):
            await self.clock.advance(5)
        self.assertTrue(lease.revoked.is_set())
        self.assertFalse(start.done())
        for _ in range(6):
            await self.clock.advance(5)
        with self.assertRaises(ExclusiveUnavailableError) as caught:
            await start
        self.assertEqual(caught.exception.failure, ExclusiveFailure.DRAIN_TIMEOUT)
        self.assertEqual(self.control.actions, [])
        await lease.release()

    async def test_a_second_request_while_it_starts_is_refused_at_once(self):
        _, lease = await self.running_task()
        start = asyncio.create_task(self.mode.start(ADMIN))
        await settle()
        with self.assertRaises(FullGpuModeStateError) as caught:
            await self.mode.start(OWNER)
        self.assertEqual(caught.exception.state, "starting")
        with self.assertRaises(FullGpuModeStateError):
            await self.mode.end(OWNER)
        await lease.release()
        self.assertEqual((await start).state, FullGpuState.ON)

    async def test_it_cannot_start_twice_or_end_when_off(self):
        with self.assertRaises(FullGpuModeStateError):
            await self.mode.end(ADMIN)
        await self.mode.start(ADMIN)
        with self.assertRaises(FullGpuModeStateError):
            await self.mode.start(OWNER)
        await self.mode.end(ADMIN)
        with self.assertRaises(FullGpuModeStateError):
            await self.mode.end(ADMIN)

    async def test_without_a_fresh_reading_it_does_not_start(self):
        self.probe.fail = True
        await self.scheduler.refresh()
        with self.assertRaises(ExclusiveUnavailableError) as caught:
            await self.mode.start(ADMIN)
        self.assertEqual(caught.exception.failure, ExclusiveFailure.PROBE_UNAVAILABLE)
        self.assertEqual(self.control.actions, [])

    async def test_arguments_are_checked_before_anything(self):
        for arguments in (
            {"vram_bytes": "all"},
            {"vram_bytes": True},
            {"drain_seconds": -1},
            {"preempt": "yes"},
        ):
            with self.assertRaises(InvalidComputeArgumentError):
                await self.mode.start(ADMIN, **arguments)
        self.assertEqual(self.audit.events, [])


class EndTest(FullGpuTestCase):
    async def test_the_models_come_back_and_then_the_held_tasks_resume(self):
        task_id, lease = await self.running_task()
        start = asyncio.create_task(self.mode.start(ADMIN))
        await settle()
        await lease.release()
        await start
        self.control.actions.clear()
        status = await self.mode.end(ADMIN)
        self.assertEqual(status.state, FullGpuState.RESUMING)
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)
        await self.mode.tick()
        self.assertEqual(self.holds.resumed, [])  # the main LLM is not back yet
        await self.scheduler.refresh()  # 1. the main LLM
        self.assertEqual(self.control.actions, [("place:local_gpu", "main")])
        await self.mode.tick()
        self.assertEqual(self.holds.resumed, [task_id])  # 3. the tasks resume
        self.assertEqual(self.mode.status().state, FullGpuState.OFF)
        await self.reload(3)  # 2. the support models follow
        self.assertEqual(
            self.control.actions,
            [
                ("place:local_gpu", "main"),
                ("place:local_gpu", "memory"),
                ("place:local_gpu", "embed"),
            ],
        )

    async def test_a_main_llm_that_does_not_come_back_needs_a_human(self):
        task_id, lease = await self.running_task()
        start = asyncio.create_task(self.mode.start(ADMIN))
        await settle()
        await lease.release()
        await start
        self.control.fail.add(("place:local_gpu", "main"))
        await self.mode.end(ADMIN)
        await self.scheduler.refresh()
        await self.clock.advance(121)
        status = await self.mode.tick()
        self.assertTrue(status.needs_human)
        self.assertEqual(status.state, FullGpuState.RESUMING)
        self.assertEqual(self.holds.resumed, [])  # they keep waiting
        self.control.fail.clear()
        await self.clock.advance(60)  # the failed load is tried again
        await self.scheduler.refresh()
        status = await self.mode.tick()
        self.assertEqual(self.holds.resumed, [task_id])
        self.assertFalse(status.needs_human)
        self.assertEqual(status.state, FullGpuState.OFF)

    async def test_tasks_that_cannot_resume_now_are_tried_again(self):
        task_id, lease = await self.running_task()
        self.holds.stuck.add(task_id)
        start = asyncio.create_task(self.mode.start(ADMIN))
        await settle()
        await lease.release()
        await start
        await self.mode.end(ADMIN)
        await self.scheduler.refresh()
        await self.mode.tick()
        self.assertEqual(self.mode.status().state, FullGpuState.RESUMING)
        self.holds.stuck.clear()
        await self.mode.tick()
        self.assertEqual(self.holds.resumed, [task_id])
        self.assertEqual(self.mode.status().state, FullGpuState.OFF)

    async def test_tasks_held_by_an_earlier_process_resume(self):
        leftover = uuid.uuid4()
        self.holds.held.append(leftover)  # held when the last process stopped
        mode = FullGpuMode(
            self.scheduler, self.holds, Authorizer(self.audit), clock=self.clock
        )
        self.assertEqual(mode.status().state, FullGpuState.RESUMING)
        await mode.tick()
        self.assertEqual(self.holds.resumed, [leftover])
        self.assertEqual(mode.status().state, FullGpuState.OFF)

    async def test_it_may_start_again_while_tasks_wait_to_resume(self):
        task_id, lease = await self.running_task()
        self.holds.stuck.add(task_id)
        start = asyncio.create_task(self.mode.start(ADMIN))
        await settle()
        await lease.release()
        await start
        await self.mode.end(ADMIN)
        await self.reload()
        await self.mode.tick()
        self.assertEqual(self.mode.status().state, FullGpuState.RESUMING)
        status = await self.mode.start(ADMIN)
        self.assertEqual(status.state, FullGpuState.ON)
        self.assertEqual(status.held_tasks, 1)  # still counted as held


class ServeTest(FullGpuTestCase):
    async def test_serve_ticks_until_stopped(self):
        stop = asyncio.Event()
        await self.mode.start(ADMIN)
        late = uuid.uuid4()
        self.holds.running.add(late)
        waiter = asyncio.create_task(
            self.scheduler.acquire(coding(late), wait_seconds=600)
        )
        server = asyncio.create_task(self.mode.serve(stop))
        await settle()
        self.assertEqual(self.holds.held, [late])
        stop.set()
        await self.clock.advance(5)
        await server
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter


class HybridRuntimeTest(FullGpuTestCase):
    async def test_a_nodes_task_is_held_and_its_node_finishes(self):
        class Slow:
            def __init__(self):
                self.release = asyncio.Event()

            async def run_node(self, assignment):
                await self.release.wait()
                return NodeOutcome.succeeded(NodeResult(summary="done"))

        local = Slow()
        runtime = HybridRuntime(
            self.scheduler, local, deployment="main", clock=self.clock
        )
        node = assignment()
        self.holds.running.add(node.task_id)
        run = asyncio.create_task(runtime.run_node(node))
        await settle()
        self.assertEqual(self.scheduler.gpu_task_ids(), frozenset({node.task_id}))
        start = asyncio.create_task(self.mode.start(ADMIN))
        await settle()
        self.assertEqual(self.holds.held, [node.task_id])
        local.release.set()  # the running node finishes (drain), then the GPU
        outcome = await run
        self.assertTrue(outcome.ok)
        await start
        self.assertEqual(self.mode.status().state, FullGpuState.ON)

    async def test_a_preempted_node_fails_to_be_retried(self):
        class Endless:
            async def run_node(self, assignment):
                await asyncio.Event().wait()

        runtime = HybridRuntime(
            self.scheduler, Endless(), deployment="main", clock=self.clock
        )
        node = assignment()
        self.holds.running.add(node.task_id)
        run = asyncio.create_task(runtime.run_node(node))
        await settle()
        start = asyncio.create_task(self.mode.start(ADMIN, preempt=True))
        await settle()
        for _ in range(12):
            await self.clock.advance(5)
        outcome = await run
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.retryable)
        await start
        self.assertTrue(self.mode.status().preempted)


class SchedulerSeamTest(unittest.IsolatedAsyncioTestCase):
    """What the scheduler offers Full GPU Mode: the task of a request, the tasks
    on the GPU, and a cooperative stop of local GPU work."""

    async def test_task_id_is_checked(self):
        with self.assertRaises(InvalidComputeArgumentError):
            ComputeRequest(CO, deployment="main", task_id="not-a-uuid")
        with self.assertRaises(InvalidComputeArgumentError):
            ComputeRequest(
                ResourceClass.EXCLUSIVE, vram_bytes=GIB, task_id=uuid.uuid4()
            )

    async def test_revoke_asks_only_local_gpu_leases(self):
        scheduler, *_ = build((main_spec(), embedding_spec()))
        await scheduler.refresh()
        task_id = uuid.uuid4()
        gpu = (await scheduler.try_acquire(coding(task_id))).lease
        self.assertEqual(gpu.task_id, task_id)
        scheduler._deployments["embed"].state = DeploymentState.CPU
        cpu = (
            await scheduler.try_acquire(
                ComputeRequest(
                    ResourceClass.SUPPORT, deployment="embed", task_id=uuid.uuid4()
                )
            )
        ).lease
        self.assertEqual(scheduler.gpu_task_ids(), frozenset({task_id}))
        self.assertEqual(scheduler.revoke_local_gpu(), 1)
        self.assertTrue(gpu.revoked.is_set())
        self.assertFalse(cpu.revoked.is_set())
        for lease in (gpu, cpu):
            await lease.release()
        self.assertEqual(scheduler.gpu_task_ids(), frozenset())
        self.assertEqual(scheduler.revoke_local_gpu(), 0)


if __name__ == "__main__":
    unittest.main()
