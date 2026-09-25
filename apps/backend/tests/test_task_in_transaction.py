"""``execute(..., in_transaction=step)``: a step in the command's transaction.

Issue #83 (Decision 0008, section 8): the stop processor of a deleted project must
cancel a task and its queue entry in ONE transaction and hold the project row while
it does. ``execute`` therefore lets the caller run a coroutine in the command's own
transaction, after the command's writes and before the commit. These tests prove the
contract with plain SQL: what the step sees, what it can commit, and that whatever
it raises takes the whole command with it.
"""

import unittest
import uuid

from sqlalchemy import text

from paw_backend.tasks import (
    Actor,
    IllegalTransitionError,
    TaskCommand,
    TaskConflictError,
    TaskNotFoundError,
    TaskService,
    TaskState,
)

from .gate_support import ALWAYS_ACTIVE
from .task_support import PostgresTaskTestCase, requires_postgres


class Boom(Exception):
    """An error of the step, to be told apart from every error of the service."""


@requires_postgres
class InTransactionStepTest(PostgresTaskTestCase):
    async def state_and_version(self, task_id: uuid.UUID) -> tuple:
        async with self.database.engine.connect() as connection:
            row = await connection.execute(
                text("SELECT state, version, agent FROM tasks WHERE id = :id"),
                {"id": task_id},
            )
            return tuple(row.one())

    async def test_the_step_runs_in_the_transaction_of_the_command_before_its_commit(
        self,
    ):
        task_id = await self.task_in_state(TaskState.RUNNING)
        seen: dict = {}

        async def step(session, step_task, step_project):
            row = await session.execute(
                text("SELECT state, version FROM tasks WHERE id = :id"),
                {"id": step_task},
            )
            seen["own session"] = tuple(row.one())
            seen["events in own session"] = (
                await session.execute(
                    text("SELECT count(*) FROM task_events WHERE task_id = :id"),
                    {"id": step_task},
                )
            ).scalar_one()
            # Another connection sees nothing of the command yet.
            seen["other connection"] = await self.state_and_version(step_task)
            seen["ids"] = (step_task, step_project)

        event = await self.service.execute(
            task_id,
            TaskCommand.CANCEL,
            actor=self.system,
            in_transaction=step,
        )

        # Created, Start and (now) Cancel: three events, all visible to the step.
        self.assertEqual(seen["own session"], ("cancelled", 3))
        self.assertEqual(seen["events in own session"], 3)
        self.assertEqual(seen["other connection"], ("running", 2, None))
        self.assertEqual(seen["ids"], (task_id, self.project_id))
        self.assertEqual((event.to_state, event.task_version), (TaskState.CANCELLED, 3))
        self.assertEqual(await self.state_and_version(task_id), ("cancelled", 3, None))

    async def test_what_the_step_writes_commits_together_with_the_command(self):
        task_id = await self.task_in_state(TaskState.RUNNING)

        async def step(session, step_task, step_project):
            await session.execute(
                text("UPDATE tasks SET agent = 'from-the-step' WHERE id = :id"),
                {"id": step_task},
            )

        await self.service.execute(
            task_id, TaskCommand.CANCEL, actor=self.system, in_transaction=step
        )

        self.assertEqual(
            await self.state_and_version(task_id), ("cancelled", 3, "from-the-step")
        )

    async def test_a_step_that_raises_takes_the_whole_command_with_it(self):
        task_id = await self.task_in_state(TaskState.RUNNING)
        heard: list = []

        async def listener(event):
            heard.append(event)

        service = TaskService(
            self.database, listeners=[listener], project_gate=ALWAYS_ACTIVE
        )
        raised = Boom()
        writes: list[str] = []

        async def step(session, step_task, step_project):
            await session.execute(
                text("UPDATE tasks SET agent = 'half-done' WHERE id = :id"),
                {"id": step_task},
            )
            writes.append("wrote")
            raise raised

        with self.assertRaises(Boom) as caught:
            await service.execute(
                task_id, TaskCommand.CANCEL, actor=self.system, in_transaction=step
            )

        self.assertIs(caught.exception, raised)  # not wrapped, not replaced
        self.assertEqual(writes, ["wrote"])
        # The state change, its event and the step's own write are all gone.
        self.assertEqual(await self.state_and_version(task_id), ("running", 2, None))
        self.assertEqual(
            [event.command for event in await self.service.history(task_id)],
            [TaskCommand.CREATE, TaskCommand.START],
        )
        self.assertEqual(heard, [])  # a listener hears only of a committed transition

    async def test_listeners_run_after_the_step_and_the_commit(self):
        task_id = await self.task_in_state(TaskState.RUNNING)
        order: list[str] = []
        committed_when_heard: list[tuple] = []

        async def listener(event):
            order.append("listener")
            committed_when_heard.append(await self.state_and_version(task_id))

        async def step(session, step_task, step_project):
            order.append("step")

        service = TaskService(
            self.database, listeners=[listener], project_gate=ALWAYS_ACTIVE
        )
        await service.execute(
            task_id, TaskCommand.CANCEL, actor=self.system, in_transaction=step
        )

        self.assertEqual(order, ["step", "listener"])
        self.assertEqual(committed_when_heard, [("cancelled", 3, None)])

    async def test_the_step_is_not_called_when_the_command_is_refused(self):
        running = await self.task_in_state(TaskState.RUNNING)
        calls: list[uuid.UUID] = []

        async def step(session, step_task, step_project):
            calls.append(step_task)

        with self.assertRaises(IllegalTransitionError):
            await self.service.execute(
                running, TaskCommand.START, actor=self.system, in_transaction=step
            )
        with self.assertRaises(TaskConflictError):
            await self.service.execute(
                running,
                TaskCommand.CANCEL,
                actor=self.system,
                expected_version=1,  # the task is at version 2
                in_transaction=step,
            )
        with self.assertRaises(TaskNotFoundError):
            await self.service.execute(
                uuid.uuid4(),
                TaskCommand.CANCEL,
                actor=self.system,
                in_transaction=step,
            )

        self.assertEqual(calls, [])
        self.assertEqual(await self.state_and_version(running), ("running", 2, None))

    async def test_the_step_is_called_once_for_every_command_it_is_given_to(self):
        task_id = await self.task_in_state(TaskState.QUEUED)
        calls: list[str] = []

        async def step(session, step_task, step_project):
            calls.append(str(step_task))

        for command in (TaskCommand.START, TaskCommand.PAUSE, TaskCommand.RESUME):
            await self.service.execute(
                task_id, command, actor=Actor.system(), in_transaction=step
            )

        self.assertEqual(calls, [str(task_id)] * 3)


if __name__ == "__main__":
    unittest.main()
