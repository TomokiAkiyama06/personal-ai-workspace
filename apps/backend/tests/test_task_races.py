"""Interleavings that the task row lock and the attempt binding must get right.

Every test controls the order of two real transactions on real PostgreSQL: a
statement is only started once the previous one is provably blocked on a lock
(``wait_for_lock_waiters``), so the interleaving is the same on every run.
Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import unittest

from sqlalchemy import text

from paw_backend.tasks import (
    StaleAttemptError,
    StepStatus,
    TaskCommand,
    TaskService,
    TaskState,
    TaskStepError,
    WorktreeState,
)

from .task_support import PostgresTaskTestCase, requires_postgres

C = TaskCommand
S = TaskState
REPEATS = 3

# Commands that end a task, with the state they start from and the outcome of
# a step that is running when they arrive.
ENDING_COMMANDS = (
    (C.STOP_NOW, S.RUNNING, S.CANCELLED),
    (C.FAIL, S.RUNNING, S.FAILED),
    (C.CANCEL, S.RUNNING, S.CANCELLED),
    (C.COMPLETE, S.EVALUATING, S.COMPLETED),
)


@requires_postgres
class BeginStepRacesWithEndingCommandsTest(PostgresTaskTestCase):
    async def running_steps(self, task_id) -> int:
        return await self.scalar(
            "SELECT count(*) FROM task_steps WHERE task_id = :i AND status = 'running'",
            i=task_id,
        )

    async def step_count(self, task_id) -> int:
        return await self.scalar(
            "SELECT count(*) FROM task_steps WHERE task_id = :i", i=task_id
        )

    async def test_a_step_that_is_being_started_is_seen_by_the_command_that_follows(
        self,
    ):
        """begin_step holds the task lock and is about to commit its step.

        The command arrives while it is in flight. It must wait for the commit
        and then deal with that step, instead of deciding on a snapshot without it.
        """
        for _ in range(REPEATS):
            for command, start, target in ENDING_COMMANDS:
                with self.subTest(command=command.value):
                    task_id = await self.task_in_state(start)
                    stepper = TaskService(self.new_database())
                    ender = TaskService(self.new_database())

                    async with self.database.engine.connect() as blocker:
                        # An uncommitted row with the key begin_step is about to
                        # use makes its INSERT wait while it holds the task lock.
                        await blocker.execute(
                            text(
                                "INSERT INTO task_steps (task_id, attempt, sequence, "
                                "name, status, started_at, finished_at) VALUES "
                                "(:id, 1, 1, 'blocker', 'succeeded', now(), now())"
                            ),
                            {"id": task_id},
                        )
                        begin = asyncio.create_task(
                            stepper.begin_step(task_id, "work", attempt=1)
                        )
                        await self.wait_for_lock_waiters(1)
                        end = asyncio.create_task(
                            ender.execute(task_id, command, actor=self.system)
                        )
                        await self.wait_for_lock_waiters(2)
                        await blocker.rollback()

                    step = await begin
                    self.assertEqual(step.status, StepStatus.RUNNING)
                    if command is C.COMPLETE:
                        # A task cannot complete while a step still runs.
                        with self.assertRaises(TaskStepError):
                            await end
                        self.assertEqual(await self.running_steps(task_id), 1)
                        snapshot = await self.service.restore(task_id)
                        self.assertEqual(snapshot.state, S.EVALUATING)
                        await self.service.finish_step(
                            task_id, step.id, StepStatus.SUCCEEDED
                        )
                        await self.service.execute(
                            task_id, C.COMPLETE, actor=self.system
                        )
                    else:
                        event = await end
                        # The command saw the step that had just been committed.
                        self.assertEqual(event.step_name, "work")
                        self.assertEqual(event.to_state, target)
                        if command is C.CANCEL:
                            # Graceful: the step is the worker's to finish.
                            self.assertEqual(await self.running_steps(task_id), 1)
                            await self.service.finish_step(
                                task_id, step.id, StepStatus.INTERRUPTED
                            )
                        else:
                            expected = (
                                StepStatus.INTERRUPTED
                                if command is C.STOP_NOW
                                else StepStatus.FAILED
                            )
                            current = (await self.service.restore(task_id)).current_step
                            self.assertEqual(current.status, expected)
                    self.assertEqual(await self.running_steps(task_id), 0)

    async def test_no_step_can_start_after_the_command_that_ended_the_task(self):
        """The command holds the task lock; begin_step queues behind it."""
        for _ in range(REPEATS):
            for command, start, target in ENDING_COMMANDS:
                with self.subTest(command=command.value):
                    task_id = await self.task_in_state(start)
                    finished = await self.service.begin_step(
                        task_id, "before", attempt=1
                    )
                    await self.service.finish_step(
                        task_id, finished.id, StepStatus.SUCCEEDED
                    )
                    ender = TaskService(self.new_database())
                    stepper = TaskService(self.new_database())

                    async with self.database.engine.connect() as blocker:
                        # The command takes the task lock, then waits for the row
                        # of the latest step, which the blocker holds.
                        await blocker.execute(
                            text("SELECT 1 FROM task_steps WHERE id = :s FOR UPDATE"),
                            {"s": finished.id},
                        )
                        end = asyncio.create_task(
                            ender.execute(task_id, command, actor=self.system)
                        )
                        await self.wait_for_lock_waiters(1)
                        begin = asyncio.create_task(
                            stepper.begin_step(task_id, "late", attempt=1)
                        )
                        await self.wait_for_lock_waiters(2)
                        await blocker.rollback()

                    event = await end
                    self.assertEqual(event.to_state, target)
                    with self.assertRaises(TaskStepError):
                        await begin
                    # Only the finished step exists: nothing started afterwards.
                    self.assertEqual(await self.step_count(task_id), 1)
                    self.assertEqual(await self.running_steps(task_id), 0)
                    self.assertEqual(
                        (await self.service.restore(task_id)).state, target
                    )

    async def test_tool_call_start_also_waits_for_a_transition_and_is_rejected(self):
        task_id = await self.task_in_state(S.RUNNING)
        step = await self.service.begin_step(task_id, "work", attempt=1)
        ender = TaskService(self.new_database())
        tooler = TaskService(self.new_database())

        async with self.database.engine.connect() as blocker:
            await blocker.execute(
                text("SELECT 1 FROM task_steps WHERE id = :s FOR UPDATE"),
                {"s": step.id},
            )
            end = asyncio.create_task(
                ender.execute(task_id, C.STOP_NOW, actor=self.user)
            )
            await self.wait_for_lock_waiters(1)
            tool = asyncio.create_task(
                tooler.begin_tool_invocation(
                    task_id, step_id=step.id, tool_name="shell"
                )
            )
            await self.wait_for_lock_waiters(2)
            await blocker.rollback()

        await end
        with self.assertRaises(TaskStepError):
            await tool
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.tool_invocations, ())
        self.assertEqual(snapshot.current_step.status, StepStatus.INTERRUPTED)


@requires_postgres
class SupersededWorkerTest(PostgresTaskTestCase):
    async def cancelled_and_restarted(self):
        """Attempt 1 is cancelled gracefully while its worker is mid-step, the user
        restarts, and attempt 2 is running its own step."""
        task_id = await self.task_in_state(S.RUNNING)
        old_step = await self.service.begin_step(task_id, "old", attempt=1)
        await self.service.add_log(task_id, "attempt one, before", attempt=1)
        await self.service.execute(task_id, C.CANCEL, actor=self.user)
        await self.service.execute(task_id, C.RESTART, actor=self.user)
        await self.service.execute(task_id, C.START, actor=self.system)
        new_step = await self.service.begin_step(task_id, "new", attempt=2)
        return task_id, old_step, new_step

    async def test_restart_closes_the_step_a_graceful_cancel_left_running(self):
        task_id = await self.task_in_state(S.RUNNING)
        old_step = await self.service.begin_step(task_id, "old", attempt=1)
        await self.service.execute(task_id, C.CANCEL, actor=self.user)
        self.assertEqual(
            (await self.service.restore(task_id)).current_step.status,
            StepStatus.RUNNING,
        )
        await self.service.execute(task_id, C.RESTART, actor=self.user)
        status = await self.scalar(
            "SELECT status FROM task_steps WHERE id = :s", s=old_step.id
        )
        self.assertEqual(status, "interrupted")
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM task_steps "
                "WHERE task_id = :i AND status = 'running'",
                i=task_id,
            ),
            0,
        )

    async def test_a_delayed_worker_of_the_old_attempt_cannot_touch_the_new_one(self):
        task_id, old_step, new_step = await self.cancelled_and_restarted()
        before = await self.service.restore(task_id)

        delayed = (
            self.service.finish_step(task_id, old_step.id, StepStatus.SUCCEEDED),
            self.service.add_log(task_id, "late line", attempt=1),
            self.service.begin_step(task_id, "late", attempt=1),
            self.service.update_attempt(
                task_id, attempt=1, worktree=WorktreeState("stale", "/stale", "c" * 40)
            ),
            self.service.begin_tool_invocation(
                task_id, step_id=old_step.id, tool_name="shell"
            ),
        )
        for call in delayed:
            with self.assertRaises(StaleAttemptError) as caught:
                await call
            self.assertEqual(caught.exception.code, "stale_attempt")
            self.assertEqual(
                str(caught.exception), "The task has moved on to a newer attempt"
            )

        after = await self.service.restore(task_id)
        # Nothing about the new attempt (or the task) changed.
        self.assertEqual(after, before)
        self.assertEqual(after.current_step, new_step)
        self.assertEqual(after.current_step.status, StepStatus.RUNNING)
        self.assertEqual(after.recent_logs, ())
        self.assertEqual(after.attempt.worktree, WorktreeState())
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM task_logs WHERE task_id = :i AND attempt = 2",
                i=task_id,
            ),
            0,
        )
        # The old attempt's record is as the restart left it.
        (old,) = after.previous_attempts
        self.assertEqual(old.worktree, WorktreeState())
        # The new attempt's own worker is unaffected and can finish its step.
        finished = await self.service.finish_step(
            task_id, new_step.id, StepStatus.SUCCEEDED
        )
        self.assertEqual(finished.status, StepStatus.SUCCEEDED)

    async def test_a_late_log_can_only_name_the_current_attempt(self):
        task_id, _old, _new = await self.cancelled_and_restarted()
        entry = await self.service.add_log(task_id, "from attempt two", attempt=2)
        self.assertEqual(entry.attempt, 2)
        # An attempt that never existed is refused just like a superseded one.
        with self.assertRaises(StaleAttemptError):
            await self.service.add_log(task_id, "from the future", attempt=3)

    async def test_within_one_attempt_a_step_id_stops_an_old_worker_after_a_retry(self):
        task_id = await self.task_in_state(S.RUNNING)
        first = await self.service.begin_step(task_id, "implement", attempt=1)
        await self.service.execute(task_id, C.FAIL, actor=self.system)
        await self.service.execute(task_id, C.RETRY, actor=self.user)
        await self.service.execute(task_id, C.START, actor=self.system)
        second = await self.service.begin_step(task_id, "implement", attempt=1)

        # The worker of the failed run reports late; its step is long closed.
        with self.assertRaises(TaskStepError):
            await self.service.finish_step(task_id, first.id, StepStatus.SUCCEEDED)
        current = (await self.service.restore(task_id)).current_step
        self.assertEqual((current.id, current.status), (second.id, StepStatus.RUNNING))

    async def test_a_restart_racing_a_stale_log_keeps_the_line_in_its_own_attempt(self):
        task_id = await self.task_in_state(S.CANCELLED)
        writer = TaskService(self.new_database())
        restarter = TaskService(self.new_database())

        async with self.database.engine.connect() as blocker:
            # Hold the task row so that the restart is in flight but uncommitted.
            # NO KEY UPDATE, like the restart's own UPDATE: it does not block the
            # foreign-key check of a log insert.
            await blocker.execute(
                text("SELECT 1 FROM tasks WHERE id = :i FOR NO KEY UPDATE"),
                {"i": task_id},
            )
            restart = asyncio.create_task(
                restarter.execute(task_id, C.RESTART, actor=self.user)
            )
            await self.wait_for_lock_waiters(1)
            # The log is written while attempt 1 is still current.
            async with asyncio.timeout(10):
                entry = await writer.add_log(task_id, "last words", attempt=1)
            await blocker.rollback()
        await restart
        self.assertEqual(entry.attempt, 1)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.attempt.number, 2)
        self.assertEqual(snapshot.recent_logs, ())


if __name__ == "__main__":
    unittest.main()
