"""Tool call state of a step: what a reconnecting client or backend can restore.

Only identity and execution state are stored (never arguments or output).
Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import unittest
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from paw_backend.tasks import (
    InvalidCommandArgumentError,
    StaleAttemptError,
    StepStatus,
    TaskCommand,
    TaskConflictError,
    TaskNotFoundError,
    TaskService,
    TaskState,
    TaskStepError,
    ToolInvocationStatus,
)

from .task_support import PostgresTaskTestCase, requires_postgres

C = TaskCommand
S = TaskState
T = ToolInvocationStatus


@requires_postgres
class ToolInvocationTest(PostgresTaskTestCase):
    async def running_step(self, state: TaskState = S.RUNNING):
        task_id = await self.task_in_state(state)
        step = await self.service.begin_step(task_id, "run-tests", attempt=1)
        return task_id, step

    async def test_a_call_is_recorded_started_and_then_finished(self):
        task_id, step = await self.running_step()
        started = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="shell"
        )
        self.assertEqual(
            (started.tool_name, started.status, started.step_id),
            ("shell", T.STARTED, step.id),
        )
        self.assertIsNone(started.finished_at)
        self.assertIsInstance(started.id, uuid.UUID)

        finished = await self.service.finish_tool_invocation(
            task_id, started.id, T.SUCCEEDED
        )
        self.assertEqual((finished.id, finished.status), (started.id, T.SUCCEEDED))
        self.assertIsNotNone(finished.finished_at)
        self.assertGreaterEqual(finished.finished_at, finished.started_at)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.tool_invocations, (finished,))

    async def test_a_client_that_disconnects_mid_call_leaves_the_call_restorable(self):
        task_id, step = await self.running_step()
        call = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="git-push"
        )
        # A different process (a reconnecting client, a restarted backend).
        snapshot = await TaskService(self.new_database()).restore(task_id)
        self.assertEqual(snapshot.tool_invocations, (call,))
        self.assertEqual(snapshot.tool_invocations[0].status, T.STARTED)
        self.assertEqual(snapshot.current_step.id, step.id)

    async def test_stop_now_interrupts_the_calls_of_the_running_step(self):
        task_id, step = await self.running_step()
        done = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="read-file"
        )
        await self.service.finish_tool_invocation(task_id, done.id, T.SUCCEEDED)
        running = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="shell"
        )
        await self.service.execute(task_id, C.STOP_NOW, actor=self.user)

        snapshot = await self.service.restore(task_id)
        by_id = {call.id: call for call in snapshot.tool_invocations}
        self.assertEqual(by_id[done.id].status, T.SUCCEEDED)
        self.assertEqual(by_id[running.id].status, T.INTERRUPTED)
        self.assertIsNotNone(by_id[running.id].finished_at)
        self.assertEqual(snapshot.current_step.status, StepStatus.INTERRUPTED)

    async def test_a_step_that_ends_takes_its_started_calls_with_it(self):
        for ending in ("fail", "finish", "restart"):
            with self.subTest(ending=ending):
                task_id, step = await self.running_step()
                call = await self.service.begin_tool_invocation(
                    task_id, step_id=step.id, tool_name="shell"
                )
                if ending == "fail":
                    await self.service.execute(task_id, C.FAIL, actor=self.system)
                elif ending == "finish":
                    await self.service.finish_step(
                        task_id, step.id, StepStatus.SUCCEEDED
                    )
                else:
                    await self.service.execute(task_id, C.CANCEL, actor=self.user)
                    await self.service.execute(task_id, C.RESTART, actor=self.user)
                status = await self.scalar(
                    "SELECT status FROM task_tool_invocations WHERE id = :i",
                    i=call.id,
                )
                self.assertEqual(status, "interrupted")

    async def test_a_graceful_cancel_leaves_the_worker_to_finish_its_call(self):
        task_id, step = await self.running_step()
        call = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="shell"
        )
        await self.service.execute(task_id, C.CANCEL, actor=self.user)
        self.assertEqual(
            (await self.service.restore(task_id)).tool_invocations[0].status,
            T.STARTED,
        )
        # Finishing is allowed in any task state.
        finished = await self.service.finish_tool_invocation(
            task_id, call.id, T.INTERRUPTED
        )
        self.assertEqual(finished.status, T.INTERRUPTED)

    async def test_only_the_current_step_s_calls_are_restored(self):
        task_id, first = await self.running_step()
        await self.service.begin_tool_invocation(
            task_id, step_id=first.id, tool_name="old-tool"
        )
        await self.service.finish_step(task_id, first.id, StepStatus.SUCCEEDED)
        second = await self.service.begin_step(task_id, "next", attempt=1)
        current = await self.service.begin_tool_invocation(
            task_id, step_id=second.id, tool_name="new-tool"
        )
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.tool_invocations, (current,))

    async def test_restore_returns_the_latest_hundred_calls_oldest_first(self):
        task_id, step = await self.running_step()
        await self.database_execute(
            "INSERT INTO task_tool_invocations "
            "(id, task_id, step_id, tool_name, status, started_at, finished_at) "
            "SELECT gen_random_uuid(), :t, :s, 'tool-' || n, 'succeeded', "
            "now() + n * interval '1 millisecond', "
            "now() + n * interval '1 millisecond' "
            "FROM generate_series(1, 105) AS n",
            t=task_id,
            s=step.id,
        )
        snapshot = await self.service.restore(task_id)
        names = [call.tool_name for call in snapshot.tool_invocations]
        self.assertEqual(len(names), 100)
        self.assertEqual((names[0], names[-1]), ("tool-6", "tool-105"))

    async def test_the_broker_may_supply_the_invocation_id_once(self):
        task_id, step = await self.running_step()
        broker_id = uuid.uuid4()
        call = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="shell", invocation_id=broker_id
        )
        self.assertEqual(call.id, broker_id)
        with self.assertRaises(TaskConflictError):
            await self.service.begin_tool_invocation(
                task_id, step_id=step.id, tool_name="shell", invocation_id=broker_id
            )

    async def test_a_call_needs_a_running_step_of_an_active_task_and_a_name(self):
        task_id, step = await self.running_step()
        with self.assertRaises(TaskNotFoundError):
            await self.service.begin_tool_invocation(
                uuid.uuid4(), step_id=step.id, tool_name="shell"
            )
        with self.assertRaises(TaskStepError):
            await self.service.begin_tool_invocation(
                task_id, step_id=step.id + 10_000_000, tool_name="shell"
            )
        for name in ("", "   ", "x" * 101):
            with self.assertRaises(InvalidCommandArgumentError):
                await self.service.begin_tool_invocation(
                    task_id, step_id=step.id, tool_name=name
                )
        await self.service.finish_step(task_id, step.id, StepStatus.SUCCEEDED)
        with self.assertRaises(TaskStepError):
            await self.service.begin_tool_invocation(
                task_id, step_id=step.id, tool_name="shell"
            )
        # A paused task starts no new call either.
        paused_task, paused_step = await self.running_step()
        await self.service.execute(paused_task, C.PAUSE, actor=self.user)
        with self.assertRaises(TaskStepError):
            await self.service.begin_tool_invocation(
                paused_task, step_id=paused_step.id, tool_name="shell"
            )
        self.assertEqual((await self.service.restore(task_id)).tool_invocations, ())
        self.assertEqual((await self.service.restore(paused_task)).tool_invocations, ())

    async def test_a_call_of_another_task_cannot_be_reached_through_this_one(self):
        mine, _ = await self.running_step()
        other, other_step = await self.running_step()
        foreign = await self.service.begin_tool_invocation(
            other, step_id=other_step.id, tool_name="shell"
        )
        with self.assertRaises(TaskStepError):
            await self.service.finish_tool_invocation(mine, foreign.id, T.SUCCEEDED)
        with self.assertRaises(TaskStepError):
            await self.service.begin_tool_invocation(
                mine, step_id=other_step.id, tool_name="shell"
            )
        self.assertEqual(
            (await self.service.restore(other)).tool_invocations[0].status, T.STARTED
        )

    async def test_finishing_needs_a_started_call_and_a_final_status(self):
        task_id, step = await self.running_step()
        call = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="shell"
        )
        with self.assertRaises(InvalidCommandArgumentError):
            await self.service.finish_tool_invocation(task_id, call.id, T.STARTED)
        with self.assertRaises(TaskStepError):
            await self.service.finish_tool_invocation(
                task_id, uuid.uuid4(), T.SUCCEEDED
            )
        await self.service.finish_tool_invocation(task_id, call.id, T.FAILED)
        with self.assertRaises(TaskStepError):
            await self.service.finish_tool_invocation(task_id, call.id, T.SUCCEEDED)
        self.assertEqual(
            (await self.service.restore(task_id)).tool_invocations[0].status, T.FAILED
        )

    async def test_a_superseded_worker_cannot_report_a_call_of_the_old_attempt(self):
        task_id, step = await self.running_step()
        call = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="shell"
        )
        await self.service.execute(task_id, C.CANCEL, actor=self.user)
        await self.service.execute(task_id, C.RESTART, actor=self.user)
        with self.assertRaises(StaleAttemptError):
            await self.service.finish_tool_invocation(task_id, call.id, T.SUCCEEDED)
        # Restart already interrupted it; the late success did not overwrite that.
        status = await self.scalar(
            "SELECT status FROM task_tool_invocations WHERE id = :i", i=call.id
        )
        self.assertEqual(status, "interrupted")

    async def database_execute(self, sql: str, **parameters) -> None:
        async with self.database.engine.begin() as connection:
            await connection.execute(text(sql), parameters)


@requires_postgres
class ToolInvocationSchemaTest(PostgresTaskTestCase):
    async def test_the_table_stores_identity_and_state_but_no_call_content(self):
        columns = {
            row[0]
            for row in await self.rows(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'task_tool_invocations'"
            )
        }
        self.assertEqual(
            columns,
            {
                "id",
                "task_id",
                "step_id",
                "tool_name",
                "status",
                "started_at",
                "finished_at",
            },
        )

    async def test_the_database_keeps_finished_at_consistent_with_the_status(self):
        task_id = await self.task_in_state(S.RUNNING)
        step = await self.service.begin_step(task_id, "work", attempt=1)
        for status, finished in (
            ("started", "now()"),
            ("succeeded", "NULL"),
            ("gone", "NULL"),
        ):
            with self.subTest(status=status, finished=finished):
                with self.assertRaises(IntegrityError):
                    async with self.database.engine.begin() as connection:
                        await connection.execute(
                            text(
                                "INSERT INTO task_tool_invocations (id, task_id, "
                                "step_id, tool_name, status, started_at, finished_at) "
                                "VALUES (gen_random_uuid(), :t, :s, 'shell', "
                                f"'{status}', now(), {finished})"
                            ),
                            {"t": task_id, "s": step.id},
                        )

    async def rows(self, sql: str):
        async with self.database.engine.connect() as connection:
            return (await connection.execute(text(sql))).all()


if __name__ == "__main__":
    unittest.main()
