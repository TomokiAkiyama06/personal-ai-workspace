"""An update's maintenance (Issue #54, Decision 0079 3) and its table (0191).

* while the row exists no queue entry is claimed (queued tasks stay queued, in
  order) and queueing still works; ending it lets the queue go on;
* the running tasks are held with the maintenance's reason, the drain waits for
  the live claims to end and repeats the hold; it times out with the status;
* the end resumes only the tasks the maintenance held (not Full GPU Mode's, and
  Full GPU Mode does not resume the maintenance's) and deletes the row;

The revision itself is ``test_deploy_maintenance_migration.py``.
"""

import uuid

from paw_backend.compute import PostgresTaskHolds
from paw_backend.deploy.maintenance import (
    HOLD_REASON,
    RESUME_REASON,
    DeployMaintenance,
    InvalidReleaseNameError,
    task_counts,
)
from paw_backend.tasks import TaskCommand
from paw_backend.tasks.queueing import Priority

from .queueing_support import PostgresQueueingTestCase, requires_postgres


@requires_postgres
class DeployMaintenanceTest(PostgresQueueingTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.owner_sql("TRUNCATE tasks CASCADE")
        await self.owner_sql("DELETE FROM deploy_maintenance")
        self.addAsyncCleanup(self.owner_sql, "DELETE FROM deploy_maintenance")
        self.maintenance = DeployMaintenance(self.database, self.service, self.queue)
        self.claimed = {}

    async def running_task(self, priority=Priority.NORMAL) -> uuid.UUID:
        task_id = await self.create_task()
        await self.queue.enqueue(task_id, priority=priority)
        entry = await self.queue.claim_next("worker-1")
        self.assertEqual(entry.task_id, task_id)
        self.claimed[task_id] = entry
        await self.service.execute(task_id, TaskCommand.START, actor=self.system)
        return task_id

    async def complete_entry(self, task_id: uuid.UUID) -> None:
        entry = self.claimed[task_id]
        await self.queue.complete(entry.id, "worker-1", entry.claim_count)

    async def state(self, task_id):
        (row,) = await self.rows(
            "SELECT state, wait_reason FROM tasks WHERE id = :t", t=task_id
        )
        return row["state"], row["wait_reason"]

    async def test_no_entry_is_claimed_while_the_maintenance_lasts(self):
        first, second = await self.make_tasks(2)
        await self.queue.enqueue(first)
        self.assertTrue(
            await self.maintenance.begin(from_release="r1", to_release="r2")
        )
        await self.queue.enqueue(second)  # queueing still works
        self.assertIsNone(await self.queue.claim_next("worker-1"))
        state = await self.maintenance.state()
        self.assertEqual((state.from_release, state.to_release), ("r1", "r2"))
        # A second begin keeps the first one's row.
        self.assertFalse(await self.maintenance.begin(to_release="r3"))
        self.assertEqual((await self.maintenance.state()).to_release, "r2")
        await self.maintenance.end()
        self.assertIsNone(await self.maintenance.state())
        self.assertEqual((await self.queue.claim_next("worker-1")).task_id, first)
        self.assertEqual((await self.queue.claim_next("worker-1")).task_id, second)

    async def test_release_names_are_checked(self):
        for name in ("", "-r", "a b", "x" * 65, "../r"):
            with self.subTest(name=name), self.assertRaises(InvalidReleaseNameError):
                await self.maintenance.begin(to_release=name)
        self.assertIsNone(await self.maintenance.state())

    async def test_the_running_tasks_are_held_and_drain(self):
        task_id = await self.running_task()
        queued = await self.create_task()
        await self.queue.enqueue(queued)
        await self.maintenance.begin()
        self.assertEqual(await self.maintenance.hold_running(), 1)
        self.assertEqual(await self.state(task_id), ("waiting", "resource"))
        (event,) = await self.rows(
            "SELECT actor_kind, reason FROM task_events WHERE task_id = :t "
            "ORDER BY seq DESC LIMIT 1",
            t=task_id,
        )
        self.assertEqual(event, {"actor_kind": "policy", "reason": HOLD_REASON})
        # Its worker still finishes the node that was running: not drained.
        status = await self.maintenance.drain_status()
        self.assertEqual(
            (status.active_claims, status.running, status.held, status.drained),
            (1, 0, 1, False),
        )
        await self.complete_entry(task_id)
        status = await self.maintenance.drain_status()
        self.assertTrue(status.drained)
        counts = await task_counts(self.database)
        self.assertEqual((counts["waiting"], counts["queued"]), (1, 1))

    async def test_the_drain_repeats_the_hold_and_times_out(self):
        # Claimed just before the maintenance began, started just after it.
        task_id = await self.create_task()
        await self.queue.enqueue(task_id)
        self.claimed[task_id] = await self.queue.claim_next("worker-1")
        await self.maintenance.begin()
        await self.service.execute(task_id, TaskCommand.START, actor=self.system)
        clock = [0.0]

        async def sleep(seconds):
            clock[0] += seconds

        status = await self.maintenance.drain(
            30, poll_seconds=10, sleep=sleep, monotonic=lambda: clock[0]
        )
        self.assertFalse(status.drained)
        self.assertEqual((status.active_claims, status.running), (1, 0))
        self.assertEqual(clock[0], 30)
        self.assertEqual(await self.state(task_id), ("waiting", "resource"))
        await self.complete_entry(task_id)
        status = await self.maintenance.drain(
            30, poll_seconds=10, sleep=sleep, monotonic=lambda: clock[0]
        )
        self.assertTrue(status.drained)

    async def test_the_end_resumes_only_the_maintenances_tasks(self):
        ours = await self.running_task(priority=Priority.HIGH)
        gpu = await self.running_task()
        full_gpu = PostgresTaskHolds(self.service, self.queue)
        self.assertTrue(await full_gpu.hold(gpu))
        await self.maintenance.begin()
        await self.maintenance.hold_running()
        for task_id in (ours, gpu):
            await self.complete_entry(task_id)
        # Full GPU Mode does not resume the maintenance's task ...
        self.assertEqual((await full_gpu.resume_held()).resumed, 1)
        self.assertEqual(await self.state(ours), ("waiting", "resource"))
        await self.service.execute(gpu, TaskCommand.CANCEL, actor=self.system)
        # ... and the maintenance resumes only its own.
        report = await self.maintenance.end()
        self.assertEqual((report.resumed, report.remaining), (1, 0))
        self.assertEqual(await self.state(ours), ("running", None))
        (event,) = await self.rows(
            "SELECT command, reason FROM task_events WHERE task_id = :t "
            "ORDER BY seq DESC LIMIT 1",
            t=ours,
        )
        self.assertEqual(event, {"command": "unblock", "reason": RESUME_REASON})
        self.assertIsNone(await self.maintenance.state())
        entry = await self.queue.claim_next("worker-2")
        self.assertEqual((entry.task_id, entry.priority), (ours, Priority.HIGH))
        # Ending again does nothing more.
        self.assertEqual((await self.maintenance.end()).resumed, 0)
