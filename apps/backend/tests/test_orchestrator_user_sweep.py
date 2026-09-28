"""Stopping the tasks of users whose deletion began (Issue #127, real PostgreSQL).

The user is deleted through the real ``UserLifecycleService``; tasks are seeded and
stopped through the real ``TaskService`` and ``TaskQueue`` (built with the test gate
that admits every project) and read back with SQL.
"""

import asyncio
import unittest
import uuid
from unittest import mock

from sqlalchemy import text

from paw_backend.authz import PostgresAuditSink
from paw_backend.orchestrator import user_sweep
from paw_backend.orchestrator.errors import InvalidOrchestratorArgumentError
from paw_backend.orchestrator.user_sweep import (
    STOP_REASON,
    UserBusyError,
    UserTaskStopLoop,
    UserTaskStopper,
    build_user_stop_loop,
)
from paw_backend.tasks import Actor, TaskService, TaskState
from paw_backend.tasks.queueing import TaskQueue

from .auth_support import TEST_DATABASE_URL, requires_postgres
from .gate_support import ALWAYS_ACTIVE
from .onboarding_support import OnboardingTestCase
from .task_support import PATH_TO_STATE


class UserSweepTestCase(OnboardingTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.execute("TRUNCATE tasks CASCADE")
        _, self.admin, self.admin_auth = await self.administrator()
        # Seeding uses the owner; the stopper uses the service role (the web role
        # in ``test_onboarding_grants``).
        self.seed_tasks = TaskService(self.database, project_gate=ALWAYS_ACTIVE)
        self.seed_queue = TaskQueue(self.database, project_gate=ALWAYS_ACTIVE)
        self.bob = await self.make_user("bob")
        self.carol = await self.make_user("carol")

    def stopper(self, **options) -> UserTaskStopper:
        database = self.service_database
        return UserTaskStopper(
            database,
            TaskService(database, project_gate=ALWAYS_ACTIVE),
            TaskQueue(database, project_gate=ALWAYS_ACTIVE),
            PostgresAuditSink(database),
            **options,
        )

    async def delete(self, user) -> None:
        await self.lifecycle.delete_user(
            self.admin, user.id, self.context(), session_id=self.admin_auth.record.id
        )

    async def seed(
        self, user, state: TaskState = TaskState.QUEUED, *, queue: bool = True
    ) -> uuid.UUID:
        event = await self.seed_tasks.create_task(
            project_id=uuid.uuid4(), created_by=user.id, title="Agent work"
        )
        for command, wait_reason in PATH_TO_STATE[state]:
            await self.seed_tasks.execute(
                event.task_id, command, actor=Actor.system(), wait_reason=wait_reason
            )
        if state is TaskState.QUEUED and queue:
            await self.seed_queue.enqueue(event.task_id)
        return event.task_id

    async def state(self, task_id) -> str:
        return await self.scalar("SELECT state FROM tasks WHERE id = :id", id=task_id)

    async def entries(self, task_id) -> list[str]:
        rows = await self.query(
            "SELECT status FROM queue_entries WHERE task_id = :id ORDER BY id",
            id=task_id,
        )
        return [row.status for row in rows]

    async def wait_until_blocked_on_a_lock(self) -> None:
        """Until another backend waits for a row lock (the stop's ``FOR SHARE``)."""
        for _ in range(500):
            waiting = await self.scalar(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND query LIKE '%FOR SHARE%'"
            )
            if waiting:
                return
            await asyncio.sleep(0.01)
        self.fail("the stop never waited for the user's row lock")

    async def last_event(self, task_id):
        return (
            await self.query(
                "SELECT command, actor_kind, reason FROM task_events "
                "WHERE task_id = :id ORDER BY seq DESC LIMIT 1",
                id=task_id,
            )
        )[0]


@requires_postgres
class StopTest(UserSweepTestCase):
    async def test_every_active_task_of_a_deleted_user_is_cancelled(self):
        queued = await self.seed(self.bob)
        running = await self.seed(self.bob, TaskState.RUNNING)
        paused = await self.seed(self.bob, TaskState.PAUSED)
        waiting = await self.seed(self.bob, TaskState.WAITING)
        done = await self.seed(self.bob, TaskState.COMPLETED)
        carols = await self.seed(self.carol, TaskState.RUNNING)
        await self.delete(self.bob)
        stopper = self.stopper()

        self.assertEqual(await stopper.stopping_user_ids(), (self.bob.id,))
        result = await stopper.stop_user_tasks(self.bob.id)

        self.assertTrue(result.done)
        self.assertEqual(set(result.stopped), {queued, running, paused, waiting})
        self.assertEqual(result.cancelled_entries, 1)
        for task_id in (queued, running, paused, waiting):
            self.assertEqual(await self.state(task_id), "cancelled")
            event = await self.last_event(task_id)
            self.assertEqual(
                (event.command, event.actor_kind, event.reason),
                ("cancel", "policy", STOP_REASON),
            )
        self.assertEqual(await self.entries(queued), ["cancelled"])
        self.assertEqual(await self.state(done), "completed")
        self.assertEqual(await self.state(carols), "running")
        audited = [
            (row.decision, row.reason, row.resource_kind, row.resource_id)
            for row in await self.audit_rows()
            if row.action == "auth.user.task_stop"
        ]
        self.assertEqual(
            sorted(audited, key=lambda r: str(r[3])),
            sorted(
                [
                    ("allow", "user_deletion", "task", t)
                    for t in (queued, running, paused, waiting)
                ],
                key=lambda r: str(r[3]),
            ),
        )
        self.assertEqual(await stopper.stopping_user_ids(), ())

    async def test_a_second_call_changes_nothing(self):
        await self.seed(self.bob, TaskState.RUNNING)
        await self.delete(self.bob)
        stopper = self.stopper()
        await stopper.stop_user_tasks(self.bob.id)

        again = await stopper.stop_user_tasks(self.bob.id)

        self.assertEqual((again.stopped, again.cancelled_entries), ((), 0))
        self.assertTrue(again.done)

    async def test_an_active_user_s_tasks_are_never_touched(self):
        task = await self.seed(self.bob, TaskState.RUNNING)
        stopper = self.stopper()

        self.assertEqual(await stopper.stopping_user_ids(), ())
        result = await stopper.stop_user_tasks(self.bob.id)

        self.assertEqual(result.stopped, ())
        self.assertTrue(result.done)
        self.assertEqual(await self.state(task), "running")

    async def test_a_restore_that_committed_first_keeps_the_task(self):
        task = await self.seed(self.bob, TaskState.RUNNING)
        await self.delete(self.bob)
        await self.execute(
            "UPDATE users SET status = 'active' WHERE id = :id", id=self.bob.id
        )

        outcome, entry = await self.stopper()._stop_task(self.bob.id, task)

        self.assertEqual((outcome, entry), ("live", False))
        self.assertEqual(await self.state(task), "running")

    async def test_a_restore_holding_the_row_lock_is_waited_for_and_wins(self):
        # Decision 0043 B: a restore that commits first is never undone. The stop's
        # FOR SHARE must wait for the restore's FOR NO KEY UPDATE and then read the
        # restored status inside the cancel's own transaction.
        task = await self.seed(self.bob, TaskState.RUNNING)
        await self.delete(self.bob)
        holder = self.new_database(TEST_DATABASE_URL)
        async with holder.session() as session, session.begin():
            await session.execute(
                text("SELECT 1 FROM users WHERE id = :id FOR NO KEY UPDATE"),
                {"id": self.bob.id},
            )
            await session.execute(
                text("UPDATE users SET status = 'active' WHERE id = :id"),
                {"id": self.bob.id},
            )
            stop = asyncio.create_task(self.stopper()._stop_task(self.bob.id, task))
            await self.wait_until_blocked_on_a_lock()
            self.assertFalse(stop.done())

        self.assertEqual(await stop, ("live", False))
        self.assertEqual(await self.state(task), "running")

    async def test_a_row_locked_too_long_is_reported_busy_with_nothing_cancelled(self):
        task = await self.seed(self.bob, TaskState.RUNNING)
        await self.delete(self.bob)
        holder = self.new_database(TEST_DATABASE_URL)
        with mock.patch.object(user_sweep, "USER_LOCK_TIMEOUT_MS", 100):
            async with holder.session() as session, session.begin():
                await session.execute(
                    text("SELECT 1 FROM users WHERE id = :id FOR NO KEY UPDATE"),
                    {"id": self.bob.id},
                )
                with self.assertRaises(UserBusyError):
                    await self.stopper().stop_user_tasks(self.bob.id)

        self.assertEqual(await self.state(task), "running")
        self.assertEqual((await self.last_event(task)).command, "start")
        self.assertEqual(
            [r for r in await self.audit_rows() if r.action == "auth.user.task_stop"],
            [],
        )

    async def test_an_entry_left_behind_a_finished_task_is_cancelled(self):
        task = await self.seed(self.bob)
        await self.execute("UPDATE tasks SET state = 'failed' WHERE id = :id", id=task)
        await self.delete(self.bob)
        stopper = self.stopper()

        self.assertEqual(await stopper.stopping_user_ids(), (self.bob.id,))
        result = await stopper.stop_user_tasks(self.bob.id)

        self.assertEqual(result.stopped, ())
        self.assertEqual(result.cancelled_entries, 1)
        self.assertTrue(result.done)
        self.assertEqual(await self.entries(task), ["cancelled"])

    async def test_a_small_batch_needs_more_calls(self):
        tasks = [await self.seed(self.bob, TaskState.RUNNING) for _ in range(3)]
        await self.delete(self.bob)
        stopper = self.stopper(batch_size=2)

        first = await stopper.stop_user_tasks(self.bob.id)
        second = await stopper.stop_user_tasks(self.bob.id)

        self.assertFalse(first.done)
        self.assertTrue(second.done)
        self.assertEqual(set(first.stopped + second.stopped), set(tasks))

    async def test_the_loop_stops_every_listed_user_in_one_cycle(self):
        await self.seed(self.bob, TaskState.RUNNING)
        await self.seed(self.carol)
        await self.delete(self.bob)
        await self.delete(self.carol)
        loop = UserTaskStopLoop(self.stopper(batch_size=1), rounds_per_user=5)

        report = await loop.run_cycle()

        self.assertEqual(
            (report.users, report.stopped_tasks, report.unfinished, report.failed),
            (2, 2, 0, 0),
        )
        self.assertEqual(report.cancelled_entries, 1)


class LoopArgumentTest(unittest.TestCase):
    def test_bad_arguments_fail_at_construction(self):
        class Stopper:
            async def stopping_user_ids(self, limit):
                return ()

            async def stop_user_tasks(self, user_id):
                raise AssertionError

        for options in (
            {"interval_seconds": 1},
            {"users_per_cycle": 0},
            {"rounds_per_user": 0},
        ):
            with (
                self.subTest(options),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                UserTaskStopLoop(Stopper(), **options)
        with self.assertRaises(TypeError):
            UserTaskStopLoop(object())

    def test_the_builder_requires_a_project_gate(self):
        with self.assertRaises(TypeError):
            build_user_stop_loop(object())  # type: ignore[call-arg]


if __name__ == "__main__":
    unittest.main()
