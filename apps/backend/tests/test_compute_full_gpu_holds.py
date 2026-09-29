"""Holding and resuming tasks for Full GPU Mode in PostgreSQL (PAW-037,
Decision 0055 Proposed): ``PostgresTaskHolds`` over the real ``TaskService`` and
``TaskQueue``.

* A running task is held: ``waiting`` for a resource, by the policy actor, with
  the Full GPU reason. Any other state is left alone.
* Only the tasks Full GPU Mode held are found held: not a task a person or
  another rule put in ``waiting``, also for a resource.
* Resuming unblocks the task and puts it back in the queue with its priority,
  in one transaction; a task whose worker still holds its entry is unblocked
  without a second entry; a task that changed since it was read is left for the
  next call.
* End to end with ``FullGpuMode``: the fakes stand for the GPU only.
"""

import asyncio
import uuid

from paw_backend.authz import Authorizer, InMemoryAuditSink, SystemRole
from paw_backend.compute import (
    HOLD_REASON,
    RESUME_REASON,
    ComputeRequest,
    FullGpuMode,
    FullGpuState,
    PostgresTaskHolds,
    ResourceClass,
)
from paw_backend.tasks import Actor, TaskCommand, TaskState, WaitReason
from paw_backend.tasks.queueing import Priority

from .authz_support import principal, uid
from .compute_support import build, settle
from .queueing_support import PostgresQueueingTestCase, requires_postgres


@requires_postgres
class PostgresTaskHoldsTest(PostgresQueueingTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Every task of the (shared) test database that an earlier test left.
        await self.owner_sql("TRUNCATE tasks CASCADE")
        self.holds = PostgresTaskHolds(self.service, self.queue)
        self.claimed = {}

    async def running_task(self, priority=Priority.NORMAL) -> uuid.UUID:
        """A task that was queued, claimed and started, as a worker runs it."""
        task_id = await self.create_task()
        await self.queue.enqueue(task_id, priority=priority)
        entry = await self.queue.claim_next("worker-1")
        self.assertEqual(entry.task_id, task_id)
        self.claimed[task_id] = entry
        await self.service.execute(task_id, TaskCommand.START, actor=self.system)
        return task_id

    async def complete_entry(self, task_id: uuid.UUID) -> None:
        """What the orchestrator does once a waiting task's nodes finished."""
        entry = self.claimed[task_id]
        await self.queue.complete(entry.id, "worker-1", entry.claim_count)

    async def state(self, task_id):
        row = (
            await self.rows(
                "SELECT state, wait_reason, version FROM tasks WHERE id = :t",
                t=task_id,
            )
        )[0]
        return row["state"], row["wait_reason"]

    async def last_event(self, task_id):
        return (
            await self.rows(
                "SELECT command, actor_kind, reason, wait_reason FROM task_events "
                "WHERE task_id = :t ORDER BY seq DESC LIMIT 1",
                t=task_id,
            )
        )[0]

    async def entries(self, task_id):
        return await self.rows(
            "SELECT status, priority FROM queue_entries WHERE task_id = :t ORDER BY id",
            t=task_id,
        )

    async def test_a_running_task_is_held_by_the_policy(self):
        task_id = await self.running_task()
        self.assertTrue(await self.holds.hold(task_id))
        self.assertEqual(await self.state(task_id), ("waiting", "resource"))
        self.assertEqual(
            await self.last_event(task_id),
            {
                "command": "wait",
                "actor_kind": "policy",
                "reason": HOLD_REASON,
                "wait_reason": "resource",
            },
        )
        self.assertEqual(await self.holds.held(), [(task_id, 3)])

    async def test_a_task_that_is_not_running_is_left_alone(self):
        for state in (
            TaskState.QUEUED,
            TaskState.WAITING,
            TaskState.PAUSED,
            TaskState.EVALUATING,
            TaskState.CANCELLED,
        ):
            task_id = await self.task_in_state(state)
            before = await self.state(task_id)
            self.assertFalse(await self.holds.hold(task_id), state)
            self.assertEqual(await self.state(task_id), before)
        self.assertFalse(await self.holds.hold(uuid.uuid4()))  # no such task
        self.assertEqual(await self.holds.held(), [])

    async def test_only_the_tasks_full_gpu_mode_held_are_held(self):
        held = await self.running_task()
        await self.holds.hold(held)
        by_person = await self.running_task()
        await self.service.execute(
            by_person,
            TaskCommand.WAIT,
            actor=self.user,
            wait_reason=WaitReason.RESOURCE,
            reason=HOLD_REASON,
        )
        other_rule = await self.running_task()
        await self.service.execute(
            other_rule,
            TaskCommand.WAIT,
            actor=Actor.policy(),
            wait_reason=WaitReason.RESOURCE,
            reason="repository lock",
        )
        for_approval = await self.running_task()
        await self.service.execute(
            for_approval,
            TaskCommand.WAIT,
            actor=Actor.policy(),
            wait_reason=WaitReason.APPROVAL,
            reason=HOLD_REASON,
        )
        # Held once, unblocked, then waiting for something else: not held.
        again = await self.running_task()
        await self.holds.hold(again)
        await self.service.execute(again, TaskCommand.UNBLOCK, actor=self.system)
        await self.service.execute(
            again,
            TaskCommand.WAIT,
            actor=self.system,
            wait_reason=WaitReason.RESOURCE,
        )
        self.assertEqual([task for task, _ in await self.holds.held()], [held])

    async def test_resuming_unblocks_and_queues_again_with_its_priority(self):
        task_id = await self.running_task(priority=Priority.HIGH)
        await self.holds.hold(task_id)
        await self.complete_entry(task_id)  # its nodes finished: quiesced
        report = await self.holds.resume_held()
        self.assertEqual((report.resumed, report.remaining), (1, 0))
        self.assertEqual(await self.state(task_id), ("running", None))
        event = await self.last_event(task_id)
        self.assertEqual(
            (event["command"], event["actor_kind"], event["reason"]),
            ("unblock", "policy", RESUME_REASON),
        )
        self.assertEqual(
            await self.entries(task_id),
            [
                {"status": "completed", "priority": "high"},
                {"status": "queued", "priority": "high"},
            ],
        )
        entry = await self.queue.claim_next("worker-2")
        self.assertEqual(entry.task_id, task_id)
        self.assertEqual(await self.holds.held(), [])
        self.assertEqual((await self.holds.resume_held()).resumed, 0)

    async def test_a_task_whose_worker_still_has_it_gets_no_second_entry(self):
        task_id = await self.running_task()
        await self.holds.hold(task_id)
        report = await self.holds.resume_held()  # its entry is still claimed
        self.assertEqual(report.resumed, 1)
        self.assertEqual(await self.state(task_id), ("running", None))
        self.assertEqual(
            await self.entries(task_id), [{"status": "claimed", "priority": "normal"}]
        )

    async def test_a_task_that_changed_since_it_was_read_is_left_for_later(self):
        task_id = await self.running_task()
        await self.holds.hold(task_id)
        await self.complete_entry(task_id)
        ((_, version),) = await self.holds.held()
        # Something else wrote the task after it was read (a newer version).
        await self.owner_sql(
            "UPDATE tasks SET version = version + 1 WHERE id = :t", t=task_id
        )
        self.assertFalse(await self.holds._resume(task_id, version))
        self.assertEqual(await self.state(task_id), ("waiting", "resource"))
        self.assertEqual(
            await self.entries(task_id), [{"status": "completed", "priority": "normal"}]
        )
        self.assertEqual((await self.holds.resume_held()).resumed, 1)

    async def test_a_held_task_cancelled_meanwhile_is_not_resumed(self):
        task_id = await self.running_task()
        await self.holds.hold(task_id)
        await self.service.execute(task_id, TaskCommand.CANCEL, actor=self.user)
        report = await self.holds.resume_held()
        self.assertEqual((report.resumed, report.remaining), (0, 0))
        self.assertEqual(await self.state(task_id), ("cancelled", None))

    async def test_many_held_tasks_are_resumed_in_batches(self):
        from paw_backend.compute import holds as holds_module

        tasks = [await self.running_task() for _ in range(5)]
        for task_id in tasks:
            await self.holds.hold(task_id)
            await self.complete_entry(task_id)
        original = holds_module._BATCH
        holds_module._BATCH = 2
        try:
            report = await self.holds.resume_held()
        finally:
            holds_module._BATCH = original
        self.assertEqual((report.resumed, report.remaining), (5, 0))
        for task_id in tasks:
            self.assertEqual(await self.state(task_id), ("running", None))

    async def test_full_gpu_mode_end_to_end(self):
        scheduler, _, control, clock = build()
        await scheduler.refresh()
        mode = FullGpuMode(
            scheduler,
            self.holds,
            Authorizer(InMemoryAuditSink()),
            drain_seconds=60,
            clock=clock,
        )
        await mode.tick()
        admin = principal(SystemRole.ADMIN, uid(1))
        task_id = await self.running_task()
        lease = (
            await scheduler.try_acquire(
                ComputeRequest(
                    ResourceClass.CODING,
                    deployment="main",
                    context_tokens=1_000,
                    task_id=task_id,
                )
            )
        ).lease
        start = asyncio.create_task(mode.start(admin))
        for _ in range(50):
            await settle()
            if await self.state(task_id) == ("waiting", "resource"):
                break
            await asyncio.sleep(0.05)
        self.assertEqual(await self.state(task_id), ("waiting", "resource"))
        await lease.release()  # its node finished
        await self.complete_entry(task_id)
        self.assertEqual((await start).state, FullGpuState.ON)
        await mode.end(admin)
        await scheduler.refresh()  # the main LLM is back
        status = await mode.tick()
        self.assertEqual(status.state, FullGpuState.OFF)
        self.assertEqual(await self.state(task_id), ("running", None))
        self.assertEqual(
            [entry["status"] for entry in await self.entries(task_id)],
            ["completed", "queued"],
        )
        self.assertIn(("unload", "main"), control.actions)
