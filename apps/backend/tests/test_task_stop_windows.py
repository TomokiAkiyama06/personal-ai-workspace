"""The three windows of Decision 0008, section 8, closed by issue #83.

``ProjectTaskStopper`` used to cancel a task and its queue entry in two commits and
to check the project with a plain read, which left three windows (Decision 0008,
section 8, items 5, 7 and 8):

1. a Restore that commits after the read and before the Cancel still cancelled that
   one task of the restored project;
2. another caller that Restarted the task between the Cancel and the cancel of its
   entry lost the entry of the restarted task (the cancel had no condition);
3. a crash between the two commits, followed by a Restore, left a cancelled task
   with an active entry that a worker could claim and never start.

Each test below interleaves the race through a seam that the OLD implementation had
as well (``TaskService.execute`` before / after the command, or a database trigger
that fails the entry update), so that it fails on the old code and passes now. Real
PostgreSQL; every test runs once as the owner and once as the unprivileged
application role (``test_projects_grants``). No test sleeps to arrange an order.
"""

import contextlib
import unittest
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from paw_backend.projects import TaskStopResult
from paw_backend.tasks import Actor, TaskCommand, TaskService, TaskState
from paw_backend.tasks.queueing import TaskAlreadyQueuedError

from . import test_projects_task_stop as stop_tests
from .projects_support import requires_postgres

TRIGGER = "paw_test_refuse_entry_cancel"


class WindowTestCase(stop_tests.TaskStopTestCase):
    async def seed_queued(self, count: int) -> list[UUID]:
        return [await self.seed_task(TaskState.QUEUED) for _ in range(count)]

    def assert_untouched(self, task_ids: list[UUID]) -> None:
        for task_id in task_ids:
            self.assertEqual(self.task_state(task_id), "queued")
            self.assertEqual(self.entry_statuses(task_id), ["queued"])


@requires_postgres
class RestoreRacingTheStopperTest(WindowTestCase):
    async def test_a_restore_after_the_check_and_before_the_cancel_cancels_nothing(
        self,
    ):
        # The Restore commits right before the Cancel command of the first task,
        # after everything the stopper had read said "Pending deletion". The
        # project is Archived now: not one of its tasks may be cancelled.
        tasks = await self.seed_queued(3)
        service, manager, project_id = self.service, self.manager, self.project_id
        restored: list[bool] = []

        class Restoring(TaskService):
            async def execute(self, task_id, command, **options):
                if not restored:
                    restored.append(True)
                    await service.restore(manager, project_id)
                return await super().execute(task_id, command, **options)

        await self.begin_deletion()

        result = await self.new_stopper(Restoring(self.database)).stop_project_tasks(
            self.project_id
        )

        self.assertEqual(restored, [True])
        self.assertEqual(self.status(), "archived")
        self.assertEqual(result, TaskStopResult(self.project_id, (), 0, done=True))
        self.assert_untouched(tasks)
        for task_id in tasks:
            self.assertEqual(self.commands(task_id), ["create"])  # no Cancel event

    async def test_a_restore_after_the_cancel_of_the_first_task_spares_the_rest(self):
        tasks = await self.seed_queued(3)
        service, manager, project_id = self.service, self.manager, self.project_id
        restored: list[bool] = []

        class Restoring(TaskService):
            async def execute(self, task_id, command, **options):
                result = await super().execute(task_id, command, **options)
                if not restored:
                    restored.append(True)
                    await service.restore(manager, project_id)
                return result

        await self.begin_deletion()

        result = await self.new_stopper(Restoring(self.database)).stop_project_tasks(
            self.project_id
        )

        (first,) = result.stopped
        self.assertEqual(self.status(), "archived")
        self.assertEqual(self.task_state(first), "cancelled")
        self.assertEqual(self.entry_statuses(first), ["cancelled"])
        self.assert_untouched([t for t in tasks if t != first])
        self.assertTrue(result.done)


@requires_postgres
class RestartRacingTheStopperTest(WindowTestCase):
    async def test_a_restart_right_after_the_cancel_keeps_the_entry_it_enqueues(self):
        # The task is cancelled by the stopper; at once another caller Restarts it
        # and enqueues the new attempt (tolerating "already queued", which is what
        # the old entry looked like). The entry of the restarted task must not be
        # cancelled by the stopper's own, older, entry cancel.
        task_id = await self.seed_task(TaskState.QUEUED)
        actor = Actor.user(self.team.manager)
        tasks, queue = self.seed_tasks, self.seed_queue
        raced: list[bool] = []

        class Racing(TaskService):
            async def execute(self, task_id, command, **options):
                result = await super().execute(task_id, command, **options)
                if not raced:
                    raced.append(True)
                    await tasks.execute(task_id, TaskCommand.RESTART, actor=actor)
                    with contextlib.suppress(TaskAlreadyQueuedError):
                        await queue.enqueue(task_id)
                return result

        await self.begin_deletion()

        first = await self.new_stopper(Racing(self.database)).stop_project_tasks(
            self.project_id
        )

        # The restarted task is active, so the request stays open ...
        self.assertEqual((first.stopped, first.done), ((task_id,), False))
        self.assertEqual(self.task_state(task_id), "queued")
        # ... and it still has an active entry: the one it enqueued.
        self.assertEqual(self.entry_statuses(task_id), ["cancelled", "queued"])

        # A Restore before the next run: the task runs, with that entry.
        await self.service.restore(self.manager, self.project_id)
        rerun = await self.new_stopper().stop_project_tasks(self.project_id)

        self.assertEqual(rerun, TaskStopResult(self.project_id, (), 0, done=True))
        claimed = await self.seed_queue.claim_next("worker-1")
        assert claimed is not None
        self.assertEqual(claimed.task_id, task_id)


@requires_postgres
class CrashBetweenTheCommitsTest(WindowTestCase):
    """A failure of the entry cancel, then a Restore: no dead entry may remain.

    The failure is injected in the database, a trigger that refuses the update of a
    queue entry to ``cancelled``, so it does not depend on how the stopper reaches
    the queue. It stands for a crash between "the Cancel committed" and "the entry
    was cancelled" (the process is gone, nothing runs again before the Restore).
    """

    def refuse_entry_cancels(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE FUNCTION {TRIGGER}() RETURNS trigger LANGUAGE plpgsql"
                    " AS $$ BEGIN IF NEW.status = 'cancelled' AND"
                    " OLD.status <> 'cancelled' THEN RAISE EXCEPTION 'injected';"
                    " END IF; RETURN NEW; END $$"
                )
            )
            connection.execute(
                text(
                    f"CREATE TRIGGER {TRIGGER} BEFORE UPDATE ON queue_entries"
                    f" FOR EACH ROW EXECUTE FUNCTION {TRIGGER}()"
                )
            )
        self.addCleanup(self.allow_entry_cancels)

    def allow_entry_cancels(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(f"DROP TRIGGER IF EXISTS {TRIGGER} ON queue_entries")
            )
            connection.execute(text(f"DROP FUNCTION IF EXISTS {TRIGGER}()"))

    async def test_a_cancelled_task_never_keeps_an_active_entry_after_a_restore(self):
        task_id = await self.seed_task(TaskState.QUEUED)
        await self.begin_deletion()
        self.refuse_entry_cancels()

        with self.assertRaises(DBAPIError):
            await self.new_stopper().stop_project_tasks(self.project_id)
        self.allow_entry_cancels()  # the "process" is gone: nothing retries
        await self.service.restore(self.manager, self.project_id)
        rerun = await self.new_stopper().stop_project_tasks(self.project_id)

        # The stopper touches nothing of a live project, so this is what the crash
        # left: not "cancelled task, active entry" (a worker would claim the entry
        # and could not start the task) but the task as it was, with its entry.
        self.assertEqual(rerun, TaskStopResult(self.project_id, (), 0, done=True))
        self.assertEqual(
            (self.task_state(task_id), self.entry_statuses(task_id)),
            ("queued", ["queued"]),
        )
        claimed = await self.seed_queue.claim_next("worker-1")
        assert claimed is not None
        self.assertEqual(claimed.task_id, task_id)

    async def test_the_same_failure_in_a_project_that_stays_deleted_is_repeated(self):
        task_id = await self.seed_task(TaskState.QUEUED)
        await self.begin_deletion()
        self.refuse_entry_cancels()
        with self.assertRaises(DBAPIError):
            await self.new_stopper().stop_project_tasks(self.project_id)
        self.assertEqual(self.entry_statuses(task_id), ["queued"])

        self.allow_entry_cancels()
        rerun = await self.new_stopper().stop_project_tasks(self.project_id)

        self.assertEqual(
            rerun, TaskStopResult(self.project_id, (task_id,), 1, done=True)
        )
        self.assertEqual(
            (self.task_state(task_id), self.entry_statuses(task_id)),
            ("cancelled", ["cancelled"]),
        )


if __name__ == "__main__":
    unittest.main()
