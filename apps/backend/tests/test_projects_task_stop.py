"""Stopping the tasks of a project whose deletion began (real PostgreSQL).

Requirement (``REQUIREMENTS.md`` "Pending deletion"): when the deletion starts, the
running tasks are safe-stopped. ``ProjectService.begin_deletion`` records that in
the SAME transaction as the lifecycle change (an outbox row in
``project_task_stops``); ``ProjectTaskStopper`` (Decision 0008, section 8) does it,
through the real ``TaskService`` and ``TaskQueue`` and never by writing task
state itself. Tasks are seeded through the real services and read back with SQL.
The processor is idempotent and re-runnable; every test runs once as the owner
and once as the unprivileged application role (``test_projects_grants``).
"""

import asyncio
import unittest
import unittest.mock
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from paw_backend.authz.roles import ProjectRole
from paw_backend.db import Database
from paw_backend.projects import (
    ConfirmationMismatchError,
    InvalidProjectInputError,
    ProjectNotFoundError,
    ProjectStatus,
    ProjectTaskStopper,
    TaskStopResult,
    store,
    task_stop,
)
from paw_backend.projects.task_stop import STOP_REASON
from paw_backend.tasks import (
    Actor,
    StepStatus,
    TaskCommand,
    TaskConflictError,
    TaskError,
    TaskRun,
    TaskService,
    TaskState,
    TaskStepError,
)
from paw_backend.tasks.queueing import LeaseLostError, TaskQueue

from .gate_support import ALWAYS_ACTIVE
from .projects_support import T0, requires_postgres
from .support import make_settings
from .task_support import PATH_TO_STATE, TEST_DATABASE_URL
from .test_projects_service_lifecycle import LifecycleTestCase

ACTIVE_STATES = (
    TaskState.QUEUED,
    TaskState.RUNNING,
    TaskState.WAITING,
    TaskState.PAUSED,
    TaskState.EVALUATING,
)
TERMINAL_STATES = (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED)
DAYS_30 = timedelta(days=30)
DEADLINE = 30


class TaskStopTestCase(LifecycleTestCase):
    """A project with a team, real task / queue services and the stopper to test."""

    @classmethod
    def clean_tables(cls) -> None:
        super().clean_tables()
        with cls.engine.begin() as connection:
            connection.execute(text("TRUNCATE tasks CASCADE"))

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # Seeding always uses the owner of the schema, also when the stopper under
        # test connects as the application role.
        self.owner = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(self.owner.dispose)
        self.seed_tasks = TaskService(self.owner, project_gate=ALWAYS_ACTIVE)
        self.seed_queue = TaskQueue(self.owner, project_gate=ALWAYS_ACTIVE)

    def new_stopper(
        self, task_service: TaskService | None = None, **options: Any
    ) -> ProjectTaskStopper:
        # ``self.database`` is the application role's in ``test_projects_grants``.
        return ProjectTaskStopper(
            self.database,
            task_service or TaskService(self.database, project_gate=ALWAYS_ACTIVE),
            TaskQueue(self.database, project_gate=ALWAYS_ACTIVE),
            clock=self.clock,
            **options,
        )

    async def seed_task(
        self,
        state: TaskState = TaskState.QUEUED,
        *,
        project_id: UUID | None = None,
        queue: bool = True,
    ) -> UUID:
        """A task of the project in ``state``; a queued one also gets a queue entry."""
        event = await self.seed_tasks.create_task(
            project_id=project_id or self.project_id,
            created_by=self.team.manager,
            title="Fix the parser",
        )
        for command, wait_reason in PATH_TO_STATE[state]:
            await self.seed_tasks.execute(
                event.task_id, command, actor=Actor.system(), wait_reason=wait_reason
            )
        if state is TaskState.QUEUED and queue:
            await self.seed_queue.enqueue(event.task_id)
        return event.task_id

    async def begin_deletion(self) -> None:
        await self.service.begin_deletion(self.manager, self.project_id, "Alpha")

    # -- reading (SQL) ------------------------------------------------------------

    def scalars(self, sql: str, **parameters: Any) -> list[Any]:
        with self.engine.connect() as connection:
            return list(connection.execute(text(sql), parameters).scalars())

    def task_state(self, task_id: UUID) -> str:
        (state,) = self.scalars("SELECT state FROM tasks WHERE id = :id", id=task_id)
        return state

    def entry_statuses(self, task_id: UUID) -> list[str]:
        return self.scalars(
            "SELECT status FROM queue_entries WHERE task_id = :id ORDER BY id",
            id=task_id,
        )

    def commands(self, task_id: UUID) -> list[str]:
        return self.scalars(
            "SELECT command FROM task_events WHERE task_id = :id ORDER BY seq",
            id=task_id,
        )

    def blocked_backends(self) -> int:
        return self.scalars(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE datname = current_database() AND wait_event_type = 'Lock'"
        )[0]

    async def wait_until_blocked(self, count: int = 1) -> None:
        """Wait until ``count`` backends wait for a lock held by another one.

        The interleavings of the tests are controlled by this (a step is started
        only once the previous one is provably waiting on the lock), never by
        sleeping for a while.
        """
        async with asyncio.timeout(DEADLINE):
            while True:
                if await asyncio.to_thread(self.blocked_backends) >= count:
                    return
                await asyncio.sleep(0.02)

    def outbox(self, project_id: UUID | None = None) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text("SELECT * FROM project_task_stops WHERE project_id = :id"),
                    {"id": project_id or self.project_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None


@requires_postgres
class BeginDeletionRecordsTheStopTest(TaskStopTestCase):
    async def test_beginning_deletion_records_one_request_with_the_deletion(self):
        self.clock.advance(days=2, hours=3, microseconds=5)
        self.assertIsNone(self.outbox())

        await self.begin_deletion()

        self.assertEqual(
            self.outbox(),
            {
                "project_id": self.project_id,
                "requested_at": self.clock.now,
                "processed_at": None,
            },
        )
        self.assertEqual(self.status(), "pending_deletion")
        self.assertEqual(self.table_count("project_task_stops"), 1)

    async def test_the_stop_is_recorded_atomically_with_the_lifecycle_change(self):
        # The outbox insert fails: the whole transaction, including the transition
        # of the project, is rolled back (a request written after the commit, or a
        # transition committed before it, would leave Pending deletion behind).
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE FUNCTION paw_test_refuse_stop() RETURNS trigger"
                    " LANGUAGE plpgsql AS $$ BEGIN"
                    " RAISE EXCEPTION 'refused by the test'; END $$"
                )
            )
            connection.execute(
                text(
                    "CREATE TRIGGER paw_test_refuse_stop BEFORE INSERT OR UPDATE"
                    " ON project_task_stops FOR EACH ROW"
                    " EXECUTE FUNCTION paw_test_refuse_stop()"
                )
            )

        def drop() -> None:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "DROP TRIGGER IF EXISTS paw_test_refuse_stop"
                        " ON project_task_stops"
                    )
                )
                connection.execute(text("DROP FUNCTION IF EXISTS paw_test_refuse_stop"))

        self.addCleanup(drop)
        before = self.snapshot()

        with self.assertRaises(DBAPIError):
            await self.begin_deletion()

        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.status(), "active")
        self.assertIsNone(self.project_row(self.project_id)["deletion_started_at"])
        self.assertIsNone(self.outbox())

    async def test_a_refused_deletion_records_nothing(self):
        before = self.snapshot()
        for wrong in ("alpha", "Alpha ", ""):
            with self.subTest(wrong=wrong):
                with self.assertRaises(ConfirmationMismatchError):
                    await self.service.begin_deletion(
                        self.manager, self.project_id, wrong
                    )
        await self.forbidden_for_non_managers(
            lambda actor: self.service.begin_deletion(actor, self.project_id, "Alpha")
        )
        with self.assertRaises(ProjectNotFoundError):
            await self.service.begin_deletion(self.manager, uuid4(), "Alpha")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.table_count("project_task_stops"), 0)

    async def test_a_deleted_project_records_nothing(self):
        deleted = self.seed_project(ProjectStatus.DELETED)
        self.seed_member(deleted, self.team.manager, ProjectRole.MANAGER)
        with self.assertRaises(ProjectNotFoundError):
            await self.service.begin_deletion(self.manager, deleted, "Deleted Project")
        self.assertIsNone(self.outbox(deleted))

    async def test_a_repeat_writes_nothing_also_not_to_a_processed_request(self):
        await self.begin_deletion()
        await self.new_stopper().stop_project_tasks(self.project_id)
        first = self.outbox()
        self.assertEqual(first["processed_at"], T0)
        self.clock.advance(days=10)

        await self.begin_deletion()

        self.assertEqual(self.outbox(), first)

    async def test_a_new_deletion_after_a_restore_is_a_new_request(self):
        await self.begin_deletion()
        await self.new_stopper().stop_project_tasks(self.project_id)
        self.clock.advance(days=3)
        await self.service.restore(self.manager, self.project_id)
        await self.service.unarchive(self.manager, self.project_id)
        self.clock.advance(days=4)

        await self.begin_deletion()

        self.assertEqual(
            self.outbox(),
            {
                "project_id": self.project_id,
                "requested_at": T0 + timedelta(days=7),
                "processed_at": None,
            },
        )
        self.assertEqual(
            await self.new_stopper().pending_project_ids(), (self.project_id,)
        )


@requires_postgres
class StopProjectTasksTest(TaskStopTestCase):
    async def test_a_queued_task_and_its_queue_entry_are_cancelled(self):
        task_id = await self.seed_task(TaskState.QUEUED)
        self.assertEqual(self.entry_statuses(task_id), ["queued"])
        await self.begin_deletion()

        result = await self.new_stopper().stop_project_tasks(self.project_id)

        self.assertEqual(
            result,
            TaskStopResult(
                project_id=self.project_id,
                stopped=(task_id,),
                cancelled_entries=1,
                done=True,
            ),
        )
        self.assertEqual(self.task_state(task_id), "cancelled")
        self.assertEqual(self.entry_statuses(task_id), ["cancelled"])
        self.assertEqual(self.outbox()["processed_at"], self.clock.now)

    async def test_a_running_task_is_stopped_gracefully_through_the_task_service(self):
        task_id = await self.seed_task(TaskState.RUNNING)
        step = await self.seed_tasks.begin_step(
            task_id, "edit files", run=TaskRun(1, 0)
        )
        entry = await self.seed_queue.enqueue(task_id)
        claimed = await self.seed_queue.claim_next("worker-1")
        assert claimed is not None and claimed.id == entry.id
        await self.begin_deletion()
        events_before = len(self.commands(task_id))

        result = await self.new_stopper().stop_project_tasks(self.project_id)

        self.assertEqual(result.stopped, (task_id,))
        self.assertEqual(result.cancelled_entries, 1)
        self.assertTrue(result.done)
        self.assertEqual(self.task_state(task_id), "cancelled")
        # One event by the task service: the command, and who asked why.
        self.assertEqual(len(self.commands(task_id)), events_before + 1)
        (event,) = (await self.seed_tasks.history(task_id))[-1:]
        self.assertEqual(
            (
                event.command,
                event.from_state,
                event.to_state,
                event.actor,
                event.reason,
                event.interruption.value,
            ),
            (
                TaskCommand.CANCEL,
                TaskState.RUNNING,
                TaskState.CANCELLED,
                Actor.policy(),
                STOP_REASON,
                "graceful",
            ),
        )
        # Safe stop: the worker closes its own step at a safe boundary and can
        # still do so; it can no longer start a new one; it lost its lease.
        with self.assertRaises(TaskStepError):
            await self.seed_tasks.begin_step(task_id, "one more", run=TaskRun(1, 0))
        with self.assertRaises(LeaseLostError):
            await self.seed_queue.heartbeat(claimed.id, "worker-1", claimed.claim_count)
        finished = await self.seed_tasks.finish_step(
            task_id, step.id, StepStatus.SUCCEEDED
        )
        self.assertEqual(finished.status, StepStatus.SUCCEEDED)
        self.assertEqual(self.entry_statuses(task_id), ["cancelled"])

    async def test_every_active_state_is_stopped_and_no_other_state_is_touched(self):
        tasks = {state: await self.seed_task(state) for state in ACTIVE_STATES}
        finished = {state: await self.seed_task(state) for state in TERMINAL_STATES}
        events = {task_id: self.commands(task_id) for task_id in finished.values()}
        await self.begin_deletion()

        result = await self.new_stopper().stop_project_tasks(self.project_id)

        self.assertEqual(set(result.stopped), set(tasks.values()))
        self.assertEqual(len(result.stopped), 5)
        for state, task_id in tasks.items():
            with self.subTest(stopped=state.value):
                self.assertEqual(self.task_state(task_id), "cancelled")
        for state, task_id in finished.items():
            with self.subTest(untouched=state.value):
                self.assertEqual(self.task_state(task_id), state.value)
                self.assertEqual(self.commands(task_id), events[task_id])
        self.assertEqual(result.cancelled_entries, 1)  # only the queued task had one
        self.assertTrue(result.done)

    async def test_running_it_again_changes_nothing(self):
        task_id = await self.seed_task(TaskState.RUNNING)
        await self.begin_deletion()
        stopper = self.new_stopper()
        await stopper.stop_project_tasks(self.project_id)
        history = self.commands(task_id)
        processed_at = self.outbox()["processed_at"]
        self.clock.advance(hours=5)

        again = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(again, TaskStopResult(self.project_id, (), 0, done=True))
        self.assertEqual(self.commands(task_id), history)
        self.assertEqual(self.task_state(task_id), "cancelled")
        self.assertEqual(self.outbox()["processed_at"], processed_at)

    async def test_the_tasks_of_another_project_are_untouched(self):
        other = self.seed_project(name="Beta")
        mine = await self.seed_task(TaskState.RUNNING)
        theirs_running = await self.seed_task(TaskState.RUNNING, project_id=other)
        theirs_queued = await self.seed_task(TaskState.QUEUED, project_id=other)
        events = self.commands(theirs_running), self.commands(theirs_queued)
        await self.begin_deletion()

        result = await self.new_stopper().stop_project_tasks(self.project_id)

        self.assertEqual(result.stopped, (mine,))
        self.assertEqual(self.task_state(theirs_running), "running")
        self.assertEqual(self.task_state(theirs_queued), "queued")
        self.assertEqual(self.entry_statuses(theirs_queued), ["queued"])
        self.assertEqual(
            (self.commands(theirs_running), self.commands(theirs_queued)), events
        )
        self.assertIsNone(self.outbox(other))

    async def test_a_task_created_after_the_deletion_began_is_stopped_on_a_rerun(self):
        # The seed services use ``AlwaysActiveGate`` (issue #83): they admit work in
        # a Pending deletion project like a write that slipped past the real gate, and
        # the processor catches what slipped through.
        await self.begin_deletion()
        stopper = self.new_stopper()
        first = await stopper.stop_project_tasks(self.project_id)
        self.assertEqual(first.stopped, ())
        self.assertTrue(first.done)

        late = await self.seed_task(TaskState.QUEUED)
        late_running = await self.seed_task(TaskState.RUNNING)
        result = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(set(result.stopped), {late, late_running})
        self.assertEqual(result.cancelled_entries, 1)
        self.assertTrue(result.done)
        self.assertEqual(self.task_state(late), "cancelled")
        self.assertEqual(self.task_state(late_running), "cancelled")
        self.assertEqual(self.entry_statuses(late), ["cancelled"])

    async def test_a_queue_entry_created_by_a_raced_restart_does_not_survive(self):
        # Review finding (PAW-026 round 4): another caller cancels the task,
        # restarts it and enqueues the new attempt after the stopper listed the
        # task; the stopper's own Cancel ends the restarted task again. The new
        # entry must not outlive the stop, and the request must not be marked
        # processed while it is active.
        task_id = await self.seed_task(TaskState.QUEUED)
        actor = Actor.user(self.team.manager)
        service, queue = self.seed_tasks, self.seed_queue
        raced: list[bool] = []

        class Racing(TaskService):
            async def execute(self, task_id, command, **options):
                if not raced:
                    raced.append(True)
                    await service.execute(task_id, TaskCommand.CANCEL, actor=actor)
                    await service.execute(task_id, TaskCommand.RESTART, actor=actor)
                    await queue.cancel(task_id)  # the entry of the first attempt
                    await queue.enqueue(task_id)
                return await super().execute(task_id, command, **options)

        await self.begin_deletion()

        result = await self.new_stopper(
            Racing(self.database, project_gate=ALWAYS_ACTIVE)
        ).stop_project_tasks(self.project_id)

        self.assertEqual(raced, [True])
        self.assertEqual(self.task_state(task_id), "cancelled")
        self.assertEqual(self.entry_statuses(task_id), ["cancelled", "cancelled"])
        self.assertEqual(result.cancelled_entries, 1)
        self.assertTrue(result.done)
        self.assertEqual(self.outbox()["processed_at"], self.clock.now)
        self.assertEqual(await self.seed_queue.claim_next("worker-1"), None)

    async def test_a_restart_during_the_stop_waits_for_it_and_is_stopped_later(self):
        # Decision 0008, section 8, item 8 (a), closed by issue #83: the Cancel of a
        # task and the cancel of its entry are ONE transaction, so another caller
        # cannot Restart the task between them. A Restart that arrives while the stop
        # holds the task row waits, and runs after the commit: the task is then
        # queued (active) without an entry, so the request stays open and the next
        # run (the project is still Pending deletion) stops it.
        task_id = await self.seed_task(TaskState.QUEUED)
        actor = Actor.user(self.team.manager)
        service, spawn, blocked = self.seed_tasks, self.spawn, self.wait_until_blocked
        restarts: list[asyncio.Task] = []
        test = self

        class RestartingQueue(TaskQueue):
            async def cancel_in(self, session, task_id, now=None, **options):
                if not restarts:
                    restarts.append(
                        spawn(
                            service.execute(task_id, TaskCommand.RESTART, actor=actor)
                        )
                    )
                    await blocked()  # it cannot pass the task row lock the stop holds
                    test.assertFalse(restarts[0].done())
                return await super().cancel_in(session, task_id, now, **options)

        class Joining(TaskService):
            async def execute(self, task_id, command, **options):
                result = await super().execute(task_id, command, **options)
                async with asyncio.timeout(DEADLINE):
                    await restarts[0]  # the Restart runs after the stop's commit
                return result

        await self.begin_deletion()
        stopper = ProjectTaskStopper(
            self.database,
            Joining(self.database, project_gate=ALWAYS_ACTIVE),
            RestartingQueue(self.database, project_gate=ALWAYS_ACTIVE),
            clock=self.clock,
        )

        first = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual((first.stopped, first.done), ((task_id,), False))
        self.assertEqual(self.task_state(task_id), "queued")
        self.assertEqual(self.entry_statuses(task_id), ["cancelled"])
        self.assertIsNone(self.outbox()["processed_at"])

        second = await self.new_stopper().stop_project_tasks(self.project_id)

        self.assertEqual((second.stopped, second.done), ((task_id,), True))
        self.assertEqual(self.task_state(task_id), "cancelled")
        self.assertEqual(self.outbox()["processed_at"], self.clock.now)

    async def test_the_sweep_leaves_the_entry_of_a_task_that_is_still_active(self):
        # A task that the Cancel of step 3 did not reach (created after the list)
        # keeps its entry: a Restore before the next run must find it runnable.
        # Only the entry behind a terminal task is cancelled by the sweep.
        await self.begin_deletion()
        stopper = self.new_stopper()
        self.assertTrue((await stopper.stop_project_tasks(self.project_id)).done)
        active = await self.seed_task(TaskState.QUEUED)
        finished = await self.seed_task(TaskState.CANCELLED)
        await self.seed_queue.enqueue(finished)
        select = store.select_active_task_ids
        calls: list[bool] = []

        async def blind_first_time(session, project, limit):
            calls.append(True)
            if len(calls) == 1:
                return []  # step 2 of this run does not see the new task
            return await select(session, project, limit)

        with unittest.mock.patch.object(
            store, "select_active_task_ids", blind_first_time
        ):
            result = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(result, TaskStopResult(self.project_id, (), 1, done=False))
        self.assertEqual(self.entry_statuses(active), ["queued"])
        self.assertEqual(self.task_state(active), "queued")
        self.assertEqual(self.entry_statuses(finished), ["cancelled"])

    async def test_a_restart_after_the_sweep_listed_the_entry_keeps_the_entry(self):
        # Decision 0008, section 8, item 8 (a): the task was terminal when the sweep
        # listed its entry; another caller Restarts it before the entry is cancelled.
        # The entry is now the restarted task's own, and the conditional cancel
        # (judged with the task row share-locked) leaves it alone.
        finished = await self.seed_task(TaskState.CANCELLED)
        await self.seed_queue.enqueue(finished)
        actor = Actor.user(self.team.manager)
        service = self.seed_tasks
        list_entries = store.select_active_entry_task_ids
        restarted: list[bool] = []

        async def restart_after_listing(session, project, limit, **options):
            found = await list_entries(session, project, limit, **options)
            if not restarted:
                restarted.append(True)
                await service.execute(finished, TaskCommand.RESTART, actor=actor)
            return found

        await self.begin_deletion()
        stopper = self.new_stopper()

        with unittest.mock.patch.object(
            store, "select_active_entry_task_ids", restart_after_listing
        ):
            first = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(restarted, [True])
        self.assertEqual((first.stopped, first.cancelled_entries), ((), 0))
        self.assertFalse(first.done)  # the restarted task is active
        self.assertEqual(self.task_state(finished), "queued")
        self.assertEqual(self.entry_statuses(finished), ["queued"])

        second = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(
            second, TaskStopResult(self.project_id, (finished,), 1, done=True)
        )
        self.assertEqual(self.entry_statuses(finished), ["cancelled"])

    async def test_an_entry_of_a_finished_task_is_found_by_project_on_a_rerun(self):
        # The task is terminal, so ``select_active_task_ids`` cannot list it: the
        # entry is found through the project (queue_entries joined to tasks).
        await self.begin_deletion()
        stopper = self.new_stopper()
        self.assertTrue((await stopper.stop_project_tasks(self.project_id)).done)
        finished = await self.seed_task(TaskState.CANCELLED)
        await self.seed_queue.enqueue(finished)
        other = self.seed_project(name="Beta")
        theirs = await self.seed_task(TaskState.CANCELLED, project_id=other)
        await self.seed_queue.enqueue(theirs)
        claimed = await self.seed_queue.claim_next("worker-1")
        assert claimed is not None
        self.assertEqual(self.entry_statuses(finished), ["claimed"])
        events = self.commands(finished)

        result = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(result, TaskStopResult(self.project_id, (), 1, done=True))
        self.assertEqual(self.entry_statuses(finished), ["cancelled"])
        with self.assertRaises(LeaseLostError):  # the worker lost its lease
            await self.seed_queue.heartbeat(claimed.id, "worker-1", claimed.claim_count)
        self.assertEqual(self.commands(finished), events)  # no second Cancel
        self.assertEqual(self.entry_statuses(theirs), ["queued"])

    async def test_the_request_stays_open_while_an_entry_appears_after_the_sweep(self):
        # An entry is enqueued after the sweep and before the check that marks the
        # request processed: the task is terminal, so only the check under the
        # project lock can see it. The request is not marked processed; the next run
        # cancels the entry.
        finished = await self.seed_task(TaskState.CANCELLED)
        await self.seed_queue.enqueue(finished)
        queue = self.seed_queue
        raced: list[bool] = []
        lock_the_project = store.get_project_for_share

        async def enqueue_then_lock(session, project_id):
            if not raced:
                raced.append(True)
                await queue.enqueue(finished)
            return await lock_the_project(session, project_id)

        await self.begin_deletion()
        stopper = self.new_stopper()

        with unittest.mock.patch.object(
            store, "get_project_for_share", enqueue_then_lock
        ):
            first = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual((first.cancelled_entries, first.done), (1, False))
        self.assertEqual(self.entry_statuses(finished), ["cancelled", "queued"])
        self.assertIsNone(self.outbox()["processed_at"])
        self.assertEqual(await stopper.pending_project_ids(), (self.project_id,))

        second = await stopper.stop_project_tasks(self.project_id)
        self.assertEqual((second.cancelled_entries, second.done), (1, True))
        self.assertEqual(self.entry_statuses(finished), ["cancelled", "cancelled"])
        self.assertEqual(self.outbox()["processed_at"], self.clock.now)

    async def test_a_project_restored_meanwhile_keeps_its_entries_in_the_sweep(self):
        # The project is restored right after the first task was stopped (that task
        # and its entry are one committed transaction): the sweep reads the project
        # again and leaves the entries of a live project alone.
        running = await self.seed_task(TaskState.RUNNING)
        await self.seed_queue.enqueue(running)
        finished = await self.seed_task(TaskState.CANCELLED)
        await self.seed_queue.enqueue(finished)
        await self.begin_deletion()
        stopper = self.new_stopper(self.restoring_service())

        result = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(self.status(), "archived")
        self.assertEqual(
            (result.stopped, result.cancelled_entries, result.done),
            ((running,), 1, True),
        )
        self.assertEqual(self.entry_statuses(running), ["cancelled"])
        self.assertEqual(self.entry_statuses(finished), ["queued"])

    async def seed_queued(self, count: int) -> list[UUID]:
        """``count`` queued tasks of the project, each with an active queue entry."""
        return [await self.seed_task(TaskState.QUEUED) for _ in range(count)]

    def cancelled_tasks(self, task_ids: list[UUID]) -> list[UUID]:
        return [t for t in task_ids if self.task_state(t) == "cancelled"]

    def assert_alive(self, task_ids: list[UUID]) -> None:
        """Untouched: the task is still queued and so is its queue entry."""
        for task_id in task_ids:
            self.assertEqual(self.task_state(task_id), "queued")
            self.assertEqual(self.entry_statuses(task_id), ["queued"])

    def restoring_service(self) -> TaskService:
        """A task service that restores the project after its first Cancel."""
        service, manager, project_id = self.service, self.manager, self.project_id
        restored: list[bool] = []

        class Restoring(TaskService):
            async def execute(self, task_id, command, **options):
                result = await super().execute(task_id, command, **options)
                if not restored:
                    restored.append(True)
                    await service.restore(manager, project_id)
                return result

        return Restoring(self.database, project_gate=ALWAYS_ACTIVE)

    async def test_a_restore_between_two_tasks_stops_the_batch(self):
        # Six active tasks; the project is restored right after the first one was
        # stopped. The other five belong to a live (Archived) project now: they
        # must not be cancelled just because they were listed while it was
        # Pending deletion.
        tasks = await self.seed_queued(6)
        await self.begin_deletion()
        stopper = self.new_stopper(self.restoring_service())

        result = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(self.status(), "archived")
        self.assertEqual(len(result.stopped), 1)
        self.assertEqual(self.cancelled_tasks(tasks), list(result.stopped))
        self.assertEqual(result.cancelled_entries, 1)
        (first,) = result.stopped
        self.assertEqual(self.entry_statuses(first), ["cancelled"])
        self.assert_alive([t for t in tasks if t != first])
        # The request is moot (the deletion was restored): processed, nothing left
        # for the orchestrator to call again, and no task of the live project hurt.
        self.assertTrue(result.done)
        self.assertEqual(self.outbox()["processed_at"], self.clock.now)

    async def test_a_restore_during_the_stop_of_a_task_waits_for_its_commit(self):
        # Decision 0008, section 8, item 5, closed by issue #83: the stop of a task
        # (its Cancel and its entry cancel) holds the project row FOR SHARE, so a
        # Restore that arrives meanwhile waits for the commit. That task is
        # cancelled WITH its entry while the project is still Pending deletion; the
        # Restore commits afterwards and the next tasks are not touched.
        tasks = await self.seed_queued(6)
        service = self.new_service(lock_timeout_ms=30_000)
        manager, project_id = self.manager, self.project_id
        spawn, blocked = self.spawn, self.wait_until_blocked
        restores: list[asyncio.Task] = []
        test = self

        class Restoring(TaskQueue):
            async def cancel_in(self, session, task_id, now=None, **options):
                if not restores:
                    restores.append(spawn(service.restore(manager, project_id)))
                    await blocked()  # the Restore waits for the row the stop holds
                    test.assertEqual(test.status(), "pending_deletion")
                return await super().cancel_in(session, task_id, now, **options)

        class Joining(TaskService):
            async def execute(self, task_id, command, **options):
                result = await super().execute(task_id, command, **options)
                if restores:
                    async with asyncio.timeout(DEADLINE):
                        await restores[0]  # it runs once the stop committed
                return result

        await self.begin_deletion()
        stopper = ProjectTaskStopper(
            self.database,
            Joining(self.database, project_gate=ALWAYS_ACTIVE),
            Restoring(self.database, project_gate=ALWAYS_ACTIVE),
            clock=self.clock,
        )

        result = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(self.status(), "archived")
        self.assertEqual(len(result.stopped), 1)
        (first,) = result.stopped
        self.assertEqual(self.cancelled_tasks(tasks), [first])
        self.assertEqual(self.entry_statuses(first), ["cancelled"])
        self.assertEqual(result.cancelled_entries, 1)
        self.assert_alive([t for t in tasks if t != first])
        self.assertTrue(result.done)

    async def test_a_restore_after_the_ids_were_listed_cancels_nothing(self):
        # The restore commits between the list of the ids and the first command.
        tasks = await self.seed_queued(6)
        service, manager, project_id = self.service, self.manager, self.project_id
        select = store.select_active_task_ids
        restored: list[bool] = []

        async def listing_then_restore(session, project, limit):
            found = await select(session, project, limit)
            if not restored:
                restored.append(True)
                await service.restore(manager, project_id)
            return found

        await self.begin_deletion()
        with unittest.mock.patch.object(
            store, "select_active_task_ids", listing_then_restore
        ):
            result = await self.new_stopper().stop_project_tasks(self.project_id)

        self.assertEqual(self.status(), "archived")
        self.assertEqual(result, TaskStopResult(self.project_id, (), 0, done=True))
        self.assert_alive(tasks)

    async def test_a_restore_between_two_stray_entries_stops_the_sweep(self):
        # Six terminal tasks that still have an active entry each (found by the
        # sweep only). The project is restored after the first entry was cancelled
        # (its transaction committed) and before the second one is looked at.
        finished = [await self.seed_task(TaskState.CANCELLED) for _ in range(6)]
        for task_id in finished:
            await self.seed_queue.enqueue(task_id)
        service, manager, project_id = self.service, self.manager, self.project_id
        lock_the_project = task_stop.share_lock_status
        locks: list[bool] = []

        async def restoring_before_the_second_lock(session, project, timeout_ms):
            locks.append(True)
            if len(locks) == 2:
                # This transaction has begun but holds no lock yet.
                await service.restore(manager, project_id)
            return await lock_the_project(session, project, timeout_ms)

        await self.begin_deletion()

        with unittest.mock.patch.object(
            task_stop, "share_lock_status", restoring_before_the_second_lock
        ):
            result = await self.new_stopper().stop_project_tasks(self.project_id)

        self.assertEqual(self.status(), "archived")
        self.assertEqual((result.stopped, result.cancelled_entries), ((), 1))
        self.assertEqual(len(locks), 2)
        statuses = sorted(self.entry_statuses(t)[0] for t in finished)
        self.assertEqual(statuses, ["cancelled"] + ["queued"] * 5)
        self.assertTrue(result.done)
        self.assertEqual(self.outbox()["processed_at"], self.clock.now)

    async def test_a_restore_after_the_stray_entries_were_listed_cancels_none(self):
        finished = [await self.seed_task(TaskState.CANCELLED) for _ in range(6)]
        for task_id in finished:
            await self.seed_queue.enqueue(task_id)
        service, manager, project_id = self.service, self.manager, self.project_id
        select = store.select_active_entry_task_ids
        restored: list[bool] = []

        async def listing_then_restore(session, project, limit, **options):
            found = await select(session, project, limit, **options)
            if not restored:
                restored.append(True)
                await service.restore(manager, project_id)
            return found

        await self.begin_deletion()
        with unittest.mock.patch.object(
            store, "select_active_entry_task_ids", listing_then_restore
        ):
            result = await self.new_stopper().stop_project_tasks(self.project_id)

        self.assertEqual(self.status(), "archived")
        self.assertEqual(result, TaskStopResult(self.project_id, (), 0, done=True))
        self.assertEqual([self.entry_statuses(t) for t in finished], [["queued"]] * 6)

    async def test_a_project_that_is_deleted_again_keeps_being_stopped(self):
        # The recheck looks at the state, not at the request: a restore followed by
        # a NEW deletion leaves a Pending deletion project, whose tasks are stopped.
        tasks = await self.seed_queued(4)
        service, manager, project_id = self.service, self.manager, self.project_id
        again: list[bool] = []

        class Flapping(TaskService):
            async def execute(self, task_id, command, **options):
                result = await super().execute(task_id, command, **options)
                if not again:
                    again.append(True)
                    await service.restore(manager, project_id)
                    await service.begin_deletion(manager, project_id, "Alpha")
                return result

        await self.begin_deletion()
        result = await self.new_stopper(
            Flapping(self.database, project_gate=ALWAYS_ACTIVE)
        ).stop_project_tasks(self.project_id)

        self.assertEqual(self.status(), "pending_deletion")
        self.assertEqual(sorted(result.stopped), sorted(tasks))
        self.assertTrue(result.done)
        self.assertEqual(self.cancelled_tasks(tasks), list(result.stopped))

    async def test_more_stray_entries_than_a_batch_need_several_runs(self):
        finished = [await self.seed_task(TaskState.CANCELLED) for _ in range(3)]
        for task_id in finished:
            await self.seed_queue.enqueue(task_id)
        await self.begin_deletion()
        stopper = self.new_stopper(batch_size=2)

        first = await stopper.stop_project_tasks(self.project_id)
        self.assertEqual((first.cancelled_entries, first.done), (2, False))
        self.assertIsNone(self.outbox()["processed_at"])

        second = await stopper.stop_project_tasks(self.project_id)
        self.assertEqual((second.cancelled_entries, second.done), (1, True))
        self.assertEqual(
            [self.entry_statuses(task_id) for task_id in finished],
            [["cancelled"]] * 3,
        )

    async def test_the_request_stays_open_while_a_task_is_left_active(self):
        tasks = [await self.seed_task(TaskState.RUNNING) for _ in range(5)]
        await self.begin_deletion()
        stopper = self.new_stopper(batch_size=2)

        first = await stopper.stop_project_tasks(self.project_id)
        self.assertEqual((len(first.stopped), first.done), (2, False))
        self.assertIsNone(self.outbox()["processed_at"])
        self.assertEqual(await stopper.pending_project_ids(), (self.project_id,))

        second = await stopper.stop_project_tasks(self.project_id)
        self.assertEqual((len(second.stopped), second.done), (2, False))
        self.assertIsNone(self.outbox()["processed_at"])

        third = await stopper.stop_project_tasks(self.project_id)
        self.assertEqual((len(third.stopped), third.done), (1, True))
        self.assertEqual(self.outbox()["processed_at"], self.clock.now)
        self.assertEqual(await stopper.pending_project_ids(), ())
        stopped = first.stopped + second.stopped + third.stopped
        self.assertEqual(sorted(stopped), sorted(tasks))
        self.assertEqual({self.task_state(task_id) for task_id in tasks}, {"cancelled"})

    async def test_a_task_that_finishes_meanwhile_is_not_an_error(self):
        finishing = await self.seed_task(TaskState.RUNNING)
        staying = await self.seed_task(TaskState.QUEUED)
        seeded = self.seed_tasks

        class Racing(TaskService):
            """The worker fails its task just before the stopper cancels it."""

            async def execute(self, task_id, command, **options):
                if task_id == finishing:
                    await seeded.execute(
                        task_id, TaskCommand.FAIL, actor=Actor.system()
                    )
                return await super().execute(task_id, command, **options)

        await self.begin_deletion()
        stopper = self.new_stopper(Racing(self.database, project_gate=ALWAYS_ACTIVE))

        result = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(result.stopped, (staying,))
        self.assertTrue(result.done)
        self.assertEqual(self.task_state(finishing), "failed")
        self.assertEqual(self.task_state(staying), "cancelled")

    async def test_a_concurrent_writer_leaves_the_task_for_the_next_run(self):
        task_id = await self.seed_task(TaskState.RUNNING)
        conflicts = [TaskConflictError()]

        class Busy(TaskService):
            async def execute(self, task_id, command, **options):
                if conflicts:
                    raise conflicts.pop()
                return await super().execute(task_id, command, **options)

        await self.begin_deletion()
        stopper = self.new_stopper(Busy(self.database, project_gate=ALWAYS_ACTIVE))

        first = await stopper.stop_project_tasks(self.project_id)
        self.assertEqual((first.stopped, first.done), ((), False))
        self.assertEqual(self.task_state(task_id), "running")
        self.assertIsNone(self.outbox()["processed_at"])

        second = await stopper.stop_project_tasks(self.project_id)
        self.assertEqual((second.stopped, second.done), ((task_id,), True))
        self.assertEqual(self.task_state(task_id), "cancelled")

    async def test_another_task_error_is_not_swallowed(self):
        await self.seed_task(TaskState.RUNNING)

        class Broken(TaskService):
            async def execute(self, task_id, command, **options):
                raise TaskError("boom")

        await self.begin_deletion()
        with self.assertRaises(TaskError):
            await self.new_stopper(
                Broken(self.database, project_gate=ALWAYS_ACTIVE)
            ).stop_project_tasks(self.project_id)
        self.assertIsNone(self.outbox()["processed_at"])

    async def test_a_restored_project_keeps_its_tasks(self):
        running = await self.seed_task(TaskState.RUNNING)
        await self.begin_deletion()
        await self.service.restore(self.manager, self.project_id)
        self.assertEqual(self.status(), "archived")

        result = await self.new_stopper().stop_project_tasks(self.project_id)

        self.assertEqual(result, TaskStopResult(self.project_id, (), 0, done=True))
        self.assertEqual(self.task_state(running), "running")
        self.assertEqual(self.outbox()["processed_at"], self.clock.now)

    async def test_a_project_that_was_never_deleted_keeps_its_tasks(self):
        running = await self.seed_task(TaskState.RUNNING)
        queued = await self.seed_task(TaskState.QUEUED)
        for status in ("active", "archived"):
            with self.subTest(status=status):
                self.set_project(self.project_id, status=status)
                result = await self.new_stopper().stop_project_tasks(self.project_id)
                self.assertEqual(
                    result, TaskStopResult(self.project_id, (), 0, done=True)
                )
        self.assertEqual(self.task_state(running), "running")
        self.assertEqual(self.task_state(queued), "queued")
        self.assertEqual(self.entry_statuses(queued), ["queued"])
        self.assertIsNone(self.outbox())

    async def test_a_purged_project_still_gets_its_tasks_stopped(self):
        running = await self.seed_task(TaskState.RUNNING)
        await self.begin_deletion()
        self.clock.advance(days=30)
        purged = await self.service.purge_expired()
        self.assertEqual(purged.purged, (self.project_id,))
        self.assertEqual(self.status(), "deleted")
        self.assertIsNone(self.outbox()["processed_at"])

        result = await self.new_stopper().stop_project_tasks(self.project_id)

        self.assertEqual((result.stopped, result.done), ((running,), True))
        self.assertEqual(self.task_state(running), "cancelled")

    async def test_the_request_is_completed_under_the_lock_of_the_project(self):
        # Another transaction is changing the project (every lifecycle change holds
        # the row FOR UPDATE): the request is not completed behind its back.
        await self.begin_deletion()
        _, transaction = self.lock_project(self.project_id)
        stopping = self.spawn(self.new_stopper().stop_project_tasks(self.project_id))
        await asyncio.sleep(0.3)
        self.assertWaiting(stopping)
        self.assertIsNone(self.outbox()["processed_at"])

        transaction.rollback()
        async with asyncio.timeout(DEADLINE):
            result = await stopping

        self.assertTrue(result.done)
        self.assertEqual(self.outbox()["processed_at"], self.clock.now)

    async def test_the_request_is_processed_only_by_the_project_it_names(self):
        other = self.seed_project(name="Beta")
        await self.begin_deletion()
        await self.new_stopper().stop_project_tasks(other)
        self.assertIsNone(self.outbox()["processed_at"])
        self.assertIsNone(self.outbox(other))

    async def test_a_missing_project_and_bad_arguments_are_refused(self):
        stopper = self.new_stopper()
        with self.assertRaises(ProjectNotFoundError):
            await stopper.stop_project_tasks(uuid4())
        for bad in (None, 5, "not-a-uuid", "A" * 36):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidProjectInputError):
                    await stopper.stop_project_tasks(bad)

    async def test_it_needs_real_collaborators_and_a_sane_batch(self):
        database = self.database
        service, queue = (
            TaskService(database, project_gate=ALWAYS_ACTIVE),
            TaskQueue(database, project_gate=ALWAYS_ACTIVE),
        )
        for arguments in (
            (None, service, queue),
            (database, object(), queue),
            (database, service, object()),
        ):
            with self.subTest(arguments=[type(a).__name__ for a in arguments]):
                with self.assertRaises(TypeError):
                    ProjectTaskStopper(*arguments)
        with self.assertRaises(TypeError):
            ProjectTaskStopper(database, service, queue, clock=5)
        for bad in (True, "5", 0, 501):
            with self.subTest(batch_size=bad):
                with self.assertRaises((InvalidProjectInputError, TypeError)):
                    ProjectTaskStopper(database, service, queue, batch_size=bad)


@requires_postgres
class InterruptedTaskCancelTest(TaskStopTestCase):
    """The Cancel of a task and the cancel of its queue entry are ONE transaction.

    They used to be two commits of two separate parts, so a task that is still
    active had to keep its entry through every failure that could fall between them
    (Decision 0008, section 8, items 5, 7 and 8: entry first lost the entry of a
    task that a Restore then brought back; task first left a cancelled task with an
    active entry after a crash), and a reconciliation by state repaired the gap.
    Issue #83 removed the gap: ``TaskService.execute`` runs the entry cancel
    (``TaskQueue.cancel_in``) in the Cancel's own transaction, so an error, a
    cancelled stopper or a crash leaves BOTH untouched, and a Restore that follows
    finds a task that still has the entry it needs. These tests interrupt the
    transaction from inside and read the result with SQL.
    """

    async def restore(self) -> None:
        await self.service.restore(self.manager, self.project_id)

    def failing_queue(self, errors: list[BaseException]) -> TaskQueue:
        """A queue whose ``cancel_in`` raises the next of ``errors`` (a call each).

        Once ``errors`` is used up the calls go through. ``calls`` lists the task ids
        it was asked about.
        """
        calls: list[UUID] = []

        class Failing(TaskQueue):
            async def cancel_in(self, session, task_id, now=None, **options):
                calls.append(task_id)
                if errors:
                    raise errors.pop(0)
                return await super().cancel_in(session, task_id, now, **options)

        queue = Failing(self.database, project_gate=ALWAYS_ACTIVE)
        queue.calls = calls  # type: ignore[attr-defined]
        return queue

    def stopper_with(self, queue: TaskQueue) -> ProjectTaskStopper:
        return ProjectTaskStopper(
            self.database,
            TaskService(self.database, project_gate=ALWAYS_ACTIVE),
            queue,
            clock=self.clock,
        )

    async def test_the_task_and_its_queue_entry_commit_together(self):
        task_id = await self.seed_task(TaskState.QUEUED)
        seen: dict[str, Any] = {}
        test = self

        class Watching(TaskQueue):
            async def cancel_in(self, session, task_id, now=None, **options):
                cancelled = await super().cancel_in(session, task_id, now, **options)
                state = await session.execute(
                    text("SELECT state FROM tasks WHERE id = :id"), {"id": task_id}
                )
                # In the stop's own transaction the task is cancelled already; another
                # connection still sees the task AND its entry as they were.
                seen["own session"] = state.scalar_one()
                seen["another connection"] = (
                    test.task_state(task_id),
                    test.entry_statuses(task_id),
                )
                seen["arguments"] = (cancelled, options)
                return cancelled

        await self.begin_deletion()

        result = await self.stopper_with(
            Watching(self.database, project_gate=ALWAYS_ACTIVE)
        ).stop_project_tasks(self.project_id)

        self.assertEqual(
            seen,
            {
                "own session": "cancelled",
                "another connection": ("queued", ["queued"]),
                "arguments": (True, {}),
            },
        )
        self.assertEqual(result, TaskStopResult(self.project_id, (task_id,), 1, True))
        self.assertEqual(self.task_state(task_id), "cancelled")
        self.assertEqual(self.entry_statuses(task_id), ["cancelled"])

    async def test_a_failing_entry_cancel_takes_the_cancel_with_it(self):
        # The entry cancel fails after the Cancel was written: the Cancel is rolled
        # back with it. A Restore before the next run then finds a queued task that
        # still has its entry (with the two commits of the old order it found a
        # cancelled task with an active entry, or, with the entry first, a queued
        # task without one).
        task_id = await self.seed_task(TaskState.QUEUED)
        queue = self.failing_queue([RuntimeError("first")])
        await self.begin_deletion()

        with self.assertRaises(RuntimeError) as raised:
            await self.stopper_with(queue).stop_project_tasks(self.project_id)

        self.assertEqual(str(raised.exception), "first")
        self.assertEqual(self.task_state(task_id), "queued")
        self.assertEqual(self.entry_statuses(task_id), ["queued"])
        self.assertEqual(self.commands(task_id), ["create"])  # no Cancel event
        self.assertIsNone(self.outbox()["processed_at"])

        await self.restore()
        rerun = await self.stopper_with(queue).stop_project_tasks(self.project_id)

        self.assertEqual(rerun, TaskStopResult(self.project_id, (), 0, done=True))
        self.assertEqual(self.task_state(task_id), "queued")
        self.assertEqual(self.entry_statuses(task_id), ["queued"])
        claimed = await self.seed_queue.claim_next("worker-1")
        assert claimed is not None
        self.assertEqual(claimed.task_id, task_id)  # it still runs

    async def test_a_failing_entry_cancel_is_repeated_by_the_next_run(self):
        task_id = await self.seed_task(TaskState.QUEUED)
        queue = self.failing_queue([RuntimeError("first")])
        stopper = self.stopper_with(queue)
        await self.begin_deletion()
        with self.assertRaises(RuntimeError):
            await stopper.stop_project_tasks(self.project_id)

        rerun = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(
            rerun, TaskStopResult(self.project_id, (task_id,), 1, done=True)
        )
        self.assertEqual(self.task_state(task_id), "cancelled")
        self.assertEqual(self.entry_statuses(task_id), ["cancelled"])
        self.assertEqual(len(queue.calls), 2)  # type: ignore[attr-defined]

    async def test_a_failing_sweep_leaves_the_entry_for_the_next_run(self):
        # The entry of a task that is terminal already is cancelled in a transaction
        # of its own; a failure there leaves the entry, and the rerun cancels it.
        finished = await self.seed_task(TaskState.CANCELLED)
        await self.seed_queue.enqueue(finished)
        queue = self.failing_queue([RuntimeError("first")])
        stopper = self.stopper_with(queue)
        await self.begin_deletion()
        with self.assertRaises(RuntimeError):
            await stopper.stop_project_tasks(self.project_id)
        self.assertEqual(self.entry_statuses(finished), ["queued"])

        rerun = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(rerun, TaskStopResult(self.project_id, (), 1, done=True))
        self.assertEqual(self.entry_statuses(finished), ["cancelled"])

    async def test_a_cancelled_stopper_leaves_the_task_and_its_entry_together(self):
        task_id = await self.seed_task(TaskState.QUEUED)
        inside = asyncio.Event()

        class Hanging(TaskQueue):
            async def cancel_in(self, session, task_id, now=None, **options):
                await super().cancel_in(session, task_id, now, **options)
                inside.set()
                await asyncio.Event().wait()

        await self.begin_deletion()
        stopping = self.spawn(
            self.stopper_with(
                Hanging(self.database, project_gate=ALWAYS_ACTIVE)
            ).stop_project_tasks(self.project_id)
        )
        async with asyncio.timeout(DEADLINE):
            await inside.wait()

        # The entry cancel and the Cancel are written but not committed.
        stopping.cancel()
        with self.assertRaises(asyncio.CancelledError):
            async with asyncio.timeout(DEADLINE):
                await stopping

        self.assertEqual(self.task_state(task_id), "queued")
        self.assertEqual(self.entry_statuses(task_id), ["queued"])
        self.assertEqual(self.commands(task_id), ["create"])
        self.assertIsNone(self.outbox()["processed_at"])

    async def test_a_cancellation_after_the_commit_needs_no_reconciliation(self):
        # The transaction committed and a transition listener (which runs after the
        # commit) was cancelled: ``execute`` raises although the task is terminal.
        # The entry was cancelled in the same transaction: nothing is left to repair.
        task_id = await self.seed_task(TaskState.QUEUED)
        restore = self.restore
        seen: list[str] = []

        async def listener(event) -> None:
            seen.append(event.command)
            await restore()
            raise asyncio.CancelledError()

        await self.begin_deletion()
        stopper = self.new_stopper(
            TaskService(self.database, listeners=[listener], project_gate=ALWAYS_ACTIVE)
        )

        with self.assertRaises(asyncio.CancelledError):
            await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(len(seen), 1)
        self.assertEqual(self.task_state(task_id), "cancelled")
        self.assertEqual(self.entry_statuses(task_id), ["cancelled"])

    async def test_a_failing_cancel_after_a_restore_leaves_the_task_its_entry(self):
        task_id = await self.seed_task(TaskState.QUEUED)
        restore = self.restore

        class Failing(TaskService):
            async def execute(self, task_id, command, **options):
                await restore()
                raise TaskError("boom")

        await self.begin_deletion()
        with self.assertRaises(TaskError):
            await self.new_stopper(
                Failing(self.database, project_gate=ALWAYS_ACTIVE)
            ).stop_project_tasks(self.project_id)

        # The project is live again and the task is queued: it still has its entry
        # (it would never run again without one), and the request is still open.
        self.assertEqual(self.status(), "archived")
        self.assertEqual(self.task_state(task_id), "queued")
        self.assertEqual(self.entry_statuses(task_id), ["queued"])
        self.assertIsNone(self.outbox()["processed_at"])

        rerun = await self.new_stopper().stop_project_tasks(self.project_id)

        self.assertEqual(rerun, TaskStopResult(self.project_id, (), 0, done=True))
        self.assertEqual(self.task_state(task_id), "queued")
        self.assertEqual(self.entry_statuses(task_id), ["queued"])
        self.assertEqual(self.outbox()["processed_at"], self.clock.now)

    async def test_a_conflict_after_a_restore_leaves_the_task_its_entry(self):
        task_id = await self.seed_task(TaskState.QUEUED)
        restore = self.restore

        class Busy(TaskService):
            async def execute(self, task_id, command, **options):
                await restore()
                raise TaskConflictError()

        await self.begin_deletion()

        result = await self.new_stopper(
            Busy(self.database, project_gate=ALWAYS_ACTIVE)
        ).stop_project_tasks(self.project_id)

        self.assertEqual(result, TaskStopResult(self.project_id, (), 0, done=True))
        self.assertEqual(self.task_state(task_id), "queued")
        self.assertEqual(self.entry_statuses(task_id), ["queued"])

    async def test_a_conflict_keeps_the_entry_of_the_task_that_stays_active(self):
        task_id = await self.seed_task(TaskState.QUEUED)
        conflicts = [TaskConflictError()]

        class Busy(TaskService):
            async def execute(self, task_id, command, **options):
                if conflicts:
                    raise conflicts.pop()
                return await super().execute(task_id, command, **options)

        await self.begin_deletion()
        stopper = self.new_stopper(Busy(self.database, project_gate=ALWAYS_ACTIVE))

        first = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(first, TaskStopResult(self.project_id, (), 0, done=False))
        self.assertEqual(self.task_state(task_id), "queued")
        self.assertEqual(self.entry_statuses(task_id), ["queued"])

        second = await stopper.stop_project_tasks(self.project_id)

        self.assertEqual(
            second, TaskStopResult(self.project_id, (task_id,), 1, done=True)
        )
        self.assertEqual(self.entry_statuses(task_id), ["cancelled"])

    async def test_a_task_that_ended_by_itself_has_its_entry_cancelled(self):
        task_id = await self.seed_task(TaskState.QUEUED)
        seeded = self.seed_tasks

        class Racing(TaskService):
            """A user cancels the task just before the stopper does."""

            async def execute(self, task_id, command, **options):
                await seeded.execute(task_id, TaskCommand.CANCEL, actor=Actor.system())
                return await super().execute(task_id, command, **options)

        await self.begin_deletion()

        result = await self.new_stopper(
            Racing(self.database, project_gate=ALWAYS_ACTIVE)
        ).stop_project_tasks(self.project_id)

        self.assertEqual(result, TaskStopResult(self.project_id, (), 1, done=True))
        self.assertEqual(self.entry_statuses(task_id), ["cancelled"])

    async def test_a_cancelled_stopper_after_a_restore_leaves_the_task_its_entry(self):
        task_id = await self.seed_task(TaskState.QUEUED)
        restore = self.restore
        inside = asyncio.Event()

        class Hanging(TaskService):
            async def execute(self, task_id, command, **options):
                await restore()
                inside.set()
                await asyncio.Event().wait()

        await self.begin_deletion()
        stopping = self.spawn(
            self.new_stopper(
                Hanging(self.database, project_gate=ALWAYS_ACTIVE)
            ).stop_project_tasks(self.project_id)
        )
        async with asyncio.timeout(DEADLINE):
            await inside.wait()

        stopping.cancel()
        with self.assertRaises(asyncio.CancelledError):
            async with asyncio.timeout(DEADLINE):
                await stopping

        self.assertEqual(self.status(), "archived")
        self.assertEqual(self.task_state(task_id), "queued")
        self.assertEqual(self.entry_statuses(task_id), ["queued"])
        self.assertIsNone(self.outbox()["processed_at"])


@requires_postgres
class TaskReadsTest(TaskStopTestCase):
    """The plain read the sweep uses to find the entries behind terminal tasks."""

    async def test_entries_can_be_limited_to_the_tasks_that_are_terminal(self):
        active = await self.seed_task(TaskState.QUEUED)
        running = await self.seed_task(TaskState.RUNNING)
        await self.seed_queue.enqueue(running)
        finished = await self.seed_task(TaskState.CANCELLED)
        await self.seed_queue.enqueue(finished)
        other = self.seed_project(name="Beta")
        theirs = await self.seed_task(TaskState.CANCELLED, project_id=other)
        await self.seed_queue.enqueue(theirs)

        async with self.database.session() as session, session.begin():
            every = await store.select_active_entry_task_ids(
                session, self.project_id, 10
            )
            terminal = await store.select_active_entry_task_ids(
                session, self.project_id, 10, terminal_tasks_only=True
            )

        self.assertEqual(every, [active, running, finished])
        self.assertEqual(terminal, [finished])


@requires_postgres
class PendingProjectIdsTest(TaskStopTestCase):
    async def test_open_requests_are_listed_oldest_first_and_bounded(self):
        projects = []
        for index in range(3):
            project = self.seed_project(name=f"P{index}")
            self.seed_manager(project, user_id=self.team.manager)
            self.clock.advance(minutes=1)
            await self.service.begin_deletion(self.manager, project, f"P{index}")
            projects.append(project)
        stopper = self.new_stopper()

        self.assertEqual(await stopper.pending_project_ids(), tuple(projects))
        self.assertEqual(await stopper.pending_project_ids(2), tuple(projects[:2]))

        await stopper.stop_project_tasks(projects[0])
        self.assertEqual(await stopper.pending_project_ids(), tuple(projects[1:]))

    async def test_nothing_is_listed_without_a_request(self):
        self.assertEqual(await self.new_stopper().pending_project_ids(), ())

    async def test_the_limit_is_validated(self):
        for bad in (0, -1, 501, True, None, "3"):
            with self.subTest(limit=repr(bad)):
                with self.assertRaises(InvalidProjectInputError):
                    await self.new_stopper().pending_project_ids(bad)


if __name__ == "__main__":
    unittest.main()
