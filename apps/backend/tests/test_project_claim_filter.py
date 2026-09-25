"""Work is not claimed or started in a project that is not Active (Decision 0020, C).

The Project state gate refuses NEW work (create, Retry, Restart, enqueue). The human
decided (2026-09-26) that the queue must also not hand out work that was admitted
before an Archive or a Delete: ``TaskQueue.claim_next`` skips every entry whose
task's project is not Active (the entry stays ``queued`` and resumes after an
Unarchive), and the Start command of a task is refused in such a project. Tasks that
are already RUNNING are not stopped by any of this. The tests use the real
``ProjectStateGate`` on a real PostgreSQL, seed the tasks and entries with the
``AlwaysActiveGate`` services (they ask no project), and read the result with SQL.
The race is arranged with a gate that keeps the Start transaction open (an
explicit barrier), never with a sleep. The plan tests use ``plan()`` (bitmap scans off).
"""

import asyncio
import unittest
from uuid import UUID, uuid4

from sqlalchemy import text

from paw_backend.projects import ProjectStatus
from paw_backend.tasks import (
    Actor,
    IllegalTransitionError,
    ProjectNotActiveError,
    TaskCommand,
    TaskService,
    TaskState,
    WaitReason,
)
from paw_backend.tasks.queueing import Priority, TaskQueue

from .projects_support import requires_postgres
from .queueing_support import at
from .task_support import PostgresTaskTestCase
from .test_project_state_gate import DEADLINE, NOT_ACTIVE, GateTestCase, PausingGate

WORKER = "worker-1"
SYSTEM = Actor.system()


class ClaimTestCase(GateTestCase):
    @property
    def claims(self) -> TaskQueue:
        """The queue under test: real gate, explicit ``now`` for the lease tests."""
        return TaskQueue(self.database, allow_explicit_now=True, project_gate=self.gate)

    async def queued_task(self, project_id: UUID, *, priority: str = "normal") -> UUID:
        """A queued task of the project with an active entry.

        Seeded through the ``AlwaysActiveGate`` services, which ask no project.
        """
        task_id = await self.seed_task(
            TaskState.QUEUED, project_id=project_id, queue=False
        )
        await self.seed_queue.enqueue(task_id, priority=Priority(priority))
        return task_id

    def entry_status_of(self, task_id: UUID) -> str:
        (status,) = self.entry_statuses(task_id)
        return status


@requires_postgres
class ClaimFilterTest(ClaimTestCase):
    async def test_an_entry_of_a_project_that_is_not_active_is_not_claimed(self):
        for status in NOT_ACTIVE:
            with self.subTest(status=status.value):
                project_id = self.project_in(status)
                task_id = await self.queued_task(project_id)

                claimed = await self.claims.claim_next(WORKER)

                self.assertIsNone(claimed)
                self.assertEqual(self.entry_statuses(task_id), ["queued"])
                self.assertEqual(self.task_state(task_id), "queued")

    async def test_an_entry_of_an_active_project_is_claimed(self):
        task_id = await self.queued_task(self.project_id)

        claimed = await self.claims.claim_next(WORKER)

        assert claimed is not None
        self.assertEqual((claimed.task_id, claimed.status.value), (task_id, "claimed"))
        self.assertEqual(self.entry_statuses(task_id), ["claimed"])

    async def test_an_entry_of_an_unknown_project_is_not_claimed(self):
        # Default deny, like the gate of the writes: a task of no project is not work.
        task_id = await self.queued_task(uuid4())

        self.assertIsNone(await self.claims.claim_next(WORKER))
        self.assertEqual(self.entry_statuses(task_id), ["queued"])

    async def test_the_skipped_entry_stays_queued_and_resumes_after_an_unarchive(self):
        task_id = await self.queued_task(self.project_id)
        await self.service.archive(self.manager, self.project_id)

        self.assertIsNone(await self.claims.claim_next(WORKER))
        self.assertEqual(self.entry_statuses(task_id), ["queued"])

        await self.service.unarchive(self.manager, self.project_id)
        claimed = await self.claims.claim_next(WORKER)

        assert claimed is not None
        self.assertEqual(claimed.task_id, task_id)

    async def test_the_skipped_entry_resumes_after_a_restore_and_an_unarchive(self):
        # Decision 0008, section 8: a Restore leaves the project Archived. A task that
        # the stop processor did NOT reach (admitted before the deletion began, say)
        # keeps its entry and runs again once the project is Active.
        task_id = await self.queued_task(self.project_id)
        await self.begin_deletion()
        self.assertIsNone(await self.claims.claim_next(WORKER))

        await self.service.restore(self.manager, self.project_id)
        self.assertIsNone(await self.claims.claim_next(WORKER))
        await self.service.unarchive(self.manager, self.project_id)

        claimed = await self.claims.claim_next(WORKER)
        assert claimed is not None
        self.assertEqual(claimed.task_id, task_id)

    async def test_the_entries_of_other_projects_are_claimed_past_the_skipped_ones(
        self,
    ):
        # The Archived project's entry is first in line (higher priority, older);
        # the claim goes past it to the Active project's, and leaves it queued.
        archived = self.project_in(ProjectStatus.ARCHIVED)
        first = await self.queued_task(archived, priority="high")
        second = await self.queued_task(archived, priority="high")
        live = await self.queued_task(self.project_id, priority="low")

        claimed = await self.claims.claim_next(WORKER)
        nothing_left = await self.claims.claim_next(WORKER)

        assert claimed is not None
        self.assertEqual(claimed.task_id, live)
        self.assertIsNone(nothing_left)
        self.assertEqual(
            [self.entry_status_of(first), self.entry_status_of(second)],
            ["queued", "queued"],
        )

    async def test_the_order_among_the_entries_that_are_claimable_is_unchanged(self):
        archived = self.project_in(ProjectStatus.ARCHIVED)
        await self.queued_task(archived, priority="high")
        low = await self.queued_task(self.project_id, priority="low")
        normal = await self.queued_task(self.project_id, priority="normal")
        high = await self.queued_task(self.project_id, priority="high")

        order = [(await self.claims.claim_next(WORKER)).task_id for _ in range(3)]

        self.assertEqual(order, [high, normal, low])

    async def test_an_expired_lease_is_not_reclaimed_in_a_project_that_is_not_active(
        self,
    ):
        task_id = await self.queued_task(self.project_id)
        first = await self.claims.claim_next("worker-a", at(0))
        assert first is not None
        await self.service.archive(self.manager, self.project_id)

        # The lease ran out (60 s): an Active project's entry would be taken over.
        self.assertIsNone(await self.claims.claim_next("worker-b", at(120)))
        self.assertEqual(self.entry_statuses(task_id), ["claimed"])

        await self.service.unarchive(self.manager, self.project_id)
        taken = await self.claims.claim_next("worker-b", at(121))
        assert taken is not None
        self.assertEqual((taken.task_id, taken.claimed_by), (task_id, "worker-b"))
        self.assertEqual(taken.claim_count, 2)

    async def test_a_worker_that_holds_a_lease_keeps_it_when_the_project_is_archived(
        self,
    ):
        # Running work is not stopped: only NEW claims are filtered.
        await self.queued_task(self.project_id)
        entry = await self.claims.claim_next(WORKER, at(0))
        assert entry is not None
        await self.service.archive(self.manager, self.project_id)

        beat = await self.claims.heartbeat(entry.id, WORKER, entry.claim_count, at(10))
        done = await self.claims.complete(entry.id, WORKER, entry.claim_count, at(20))

        self.assertEqual(
            (beat.status.value, done.status.value), ("claimed", "completed")
        )

    async def test_the_always_active_test_gate_would_have_claimed_it(
        self,
    ):
        # The control of the tests above: the same entry IS claimed by a queue built
        # with the tests' ``AlwaysActiveGate``, so the skip is the real gate's doing
        # and not an artefact of the fixture.
        project_id = self.project_in(ProjectStatus.ARCHIVED)
        task_id = await self.queued_task(project_id)

        claimed = await self.seed_queue.claim_next(WORKER)

        assert claimed is not None
        self.assertEqual(claimed.task_id, task_id)


@requires_postgres
class StartGateTest(ClaimTestCase):
    """The Start command of a task is a way to begin work: refused unless Active."""

    async def test_start_is_refused_unless_the_project_is_active_and_writes_nothing(
        self,
    ):
        for status in NOT_ACTIVE:
            with self.subTest(status=status.value):
                project_id = self.project_in(status)
                task_id = await self.seed_task(
                    TaskState.QUEUED, project_id=project_id, queue=False
                )
                before = (self.task_columns(task_id), self.commands(task_id))

                with self.assertRaises(ProjectNotActiveError):
                    await self.tasks.execute(task_id, TaskCommand.START, actor=SYSTEM)

                self.assertEqual(
                    (self.task_columns(task_id), self.commands(task_id)), before
                )
                self.assertEqual(self.task_state(task_id), "queued")

    async def test_start_works_in_an_active_project(self):
        task_id = await self.seed_task(TaskState.QUEUED, queue=False)

        await self.tasks.execute(task_id, TaskCommand.START, actor=SYSTEM)

        self.assertEqual(self.task_state(task_id), "running")

    async def test_start_of_a_task_that_is_not_queued_is_reported_as_illegal_first(
        self,
    ):
        project_id = self.project_in(ProjectStatus.ARCHIVED)
        running = await self.seed_task(TaskState.RUNNING, project_id=project_id)

        with self.assertRaises(IllegalTransitionError):
            await self.tasks.execute(running, TaskCommand.START, actor=SYSTEM)

    async def test_running_work_goes_on_when_the_project_is_not_active(self):
        # Pause, Resume, Wait, Unblock, Begin evaluation, Complete, Fail, Cancel: all
        # work in every project state, so an Archive stops nothing that already runs.
        project_id = self.project_in(ProjectStatus.ARCHIVED)
        task_id = await self.seed_task(TaskState.RUNNING, project_id=project_id)

        for command, wait in (
            (TaskCommand.PAUSE, None),
            (TaskCommand.RESUME, None),
            (TaskCommand.WAIT, WaitReason.USER),
            (TaskCommand.UNBLOCK, None),
            (TaskCommand.BEGIN_EVALUATION, None),
            (TaskCommand.COMPLETE, None),
        ):
            with self.subTest(command=command.value):
                await self.tasks.execute(
                    task_id, command, actor=SYSTEM, wait_reason=wait
                )

        self.assertEqual(self.task_state(task_id), "completed")

    async def test_an_archive_does_not_stop_a_running_task(self):
        task_id = await self.seed_task(TaskState.RUNNING)
        before = self.commands(task_id)

        await self.service.archive(self.manager, self.project_id)
        await self.new_stopper().stop_project_tasks(self.project_id)  # not deleting

        self.assertEqual(self.task_state(task_id), "running")
        self.assertEqual(self.commands(task_id), before)

    async def test_claim_then_archive_then_start_leaves_the_task_queued(self):
        # The race the human named: the claim succeeded while the project was
        # Active, the Archive committed before the Start. The Start is refused, the
        # worker gives the entry back, and it is claimable again after an Unarchive.
        task_id = await self.queued_task(self.project_id)
        entry = await self.claims.claim_next(WORKER)
        assert entry is not None
        self.assertEqual(entry.task_id, task_id)

        await self.service.archive(self.manager, self.project_id)
        with self.assertRaises(ProjectNotActiveError):
            await self.tasks.execute(task_id, TaskCommand.START, actor=SYSTEM)
        released = await self.claims.release(entry.id, WORKER, entry.claim_count)

        self.assertEqual(released.status.value, "queued")
        self.assertEqual(self.task_state(task_id), "queued")
        self.assertIsNone(await self.claims.claim_next(WORKER))

        await self.service.unarchive(self.manager, self.project_id)
        again = await self.claims.claim_next(WORKER)
        assert again is not None
        self.assertEqual((again.task_id, again.claim_count), (task_id, 2))
        await self.tasks.execute(task_id, TaskCommand.START, actor=SYSTEM)
        self.assertEqual(self.task_state(task_id), "running")

    async def test_a_start_that_holds_the_project_lock_makes_the_archive_wait(self):
        # The other order of the same race: the Start holds the project row
        # (FOR SHARE) inside its transaction, the Archive (FOR UPDATE) waits for it.
        # The Start commits; the Archive commits after it; the task keeps running.
        task_id = await self.queued_task(self.project_id)
        entry = await self.claims.claim_next(WORKER)
        assert entry is not None
        gate = PausingGate(self.gate)
        starting = self.spawn(
            TaskService(self.database, project_gate=gate).execute(
                task_id, TaskCommand.START, actor=SYSTEM
            )
        )
        async with asyncio.timeout(DEADLINE):
            await gate.locked.wait()

        archiving = self.spawn(
            self.new_service(lock_timeout_ms=30_000).archive(
                self.manager, self.project_id
            )
        )
        await self.wait_until_blocked()
        self.assertEqual(self.status(), "active")
        self.assertFalse(archiving.done())

        gate.release.set()
        async with asyncio.timeout(DEADLINE):
            await starting
            await archiving

        self.assertEqual(self.status(), "archived")
        self.assertEqual(self.task_state(task_id), "running")  # not stopped
        heartbeat = await self.claims.heartbeat(entry.id, WORKER, entry.claim_count)
        self.assertEqual(heartbeat.status.value, "claimed")

    async def test_an_archive_that_commits_first_refuses_the_start(self):
        task_id = await self.queued_task(self.project_id)
        holder = self.lock_project(self.project_id)
        starting = self.spawn(
            self.tasks.execute(task_id, TaskCommand.START, actor=SYSTEM)
        )
        await self.wait_until_blocked()

        self.commit_lifecycle_change("archived", holder)

        with self.assertRaises(ProjectNotActiveError):
            async with asyncio.timeout(DEADLINE):
                await starting
        self.assertEqual(self.task_state(task_id), "queued")


@requires_postgres
class ClaimFilterPlanTest(ClaimTestCase):
    """The filter must not slow the claim: it still reads the queue in index order.

    ``claim_next`` picks the first claimable entry by ``ORDER BY priority_rank,
    enqueued_at, id LIMIT 1`` from the partial index ``ix_queue_entries_claim_order``
    and stops at the first row. The project filter is a semi-join checked per
    candidate through the primary keys of ``tasks`` and ``projects``, so the claim
    reads the entries of projects that are not Active that come BEFORE the first
    claimable one (they stay queued: that is the cost of "leave them queued") and
    nothing else. The statement is planned as a prepared statement in both plan
    modes; the entries read are counted with ``EXPLAIN ANALYZE``.
    """

    HISTORY = 20_000
    SKIPPED = 300
    CLAIMABLE = 2_000

    # The helpers of the task tests' base class that explain a statement and record
    # the SQL a service sends (this class is built on the project tests' base).
    plan = PostgresTaskTestCase.plan
    plan_nodes = staticmethod(PostgresTaskTestCase.plan_nodes)
    captured_statements = PostgresTaskTestCase.captured_statements

    async def sql(self, sql: str, **parameters) -> None:
        """Run ``sql`` as the OWNER of the schema (the tests' seeding connection),
        also when the services under test connect as the application role."""
        with self.engine.begin() as connection:
            connection.execute(text(sql), parameters)

    async def busy_queue(self) -> None:
        archived = self.project_in(ProjectStatus.ARCHIVED)
        await self.sql("TRUNCATE tasks CASCADE")
        # A long finished history (completed entries of finished tasks).
        await self.sql(
            "INSERT INTO tasks (id, project_id, created_by, title, input, state,"
            " attempt, retry_count, version, created_at, updated_at)"
            " SELECT gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 't',"
            " '{}', 'completed', 1, 0, 1, now(), now() FROM generate_series(1, :n) g",
            n=self.HISTORY,
        )
        await self.sql(
            "INSERT INTO queue_entries (task_id, priority, priority_rank, status,"
            " enqueued_at, claimed_by, claimed_at, claim_count, finished_at)"
            " SELECT id, 'normal', 1, 'completed', now(), 'w', now(), 1, now()"
            " FROM tasks"
        )
        # Queued work of an Archived project that sorts FIRST (high priority) ...
        await self.sql(
            "INSERT INTO tasks (id, project_id, created_by, title, input, state,"
            " attempt, retry_count, version, created_at, updated_at)"
            " SELECT gen_random_uuid(), :p, gen_random_uuid(), 't', '{}', 'queued',"
            " 1, 0, 1, now(), now() FROM generate_series(1, :n) g",
            p=archived,
            n=self.SKIPPED,
        )
        await self.sql(
            "INSERT INTO queue_entries (task_id, priority, priority_rank, status,"
            " enqueued_at, claim_count) SELECT id, 'high', 0, 'queued',"
            " now() - interval '1 hour', 0 FROM tasks WHERE project_id = :p",
            p=archived,
        )
        # ... and a lot of claimable work of the Active project behind it.
        await self.sql(
            "INSERT INTO tasks (id, project_id, created_by, title, input, state,"
            " attempt, retry_count, version, created_at, updated_at)"
            " SELECT gen_random_uuid(), :p, gen_random_uuid(), 't', '{}', 'queued',"
            " 1, 0, 1, now(), now() FROM generate_series(1, :n) g",
            p=self.project_id,
            n=self.CLAIMABLE,
        )
        await self.sql(
            "INSERT INTO queue_entries (task_id, priority, priority_rank, status,"
            " enqueued_at, claim_count) SELECT id, 'normal', 1, 'queued', now(), 0"
            " FROM tasks WHERE project_id = :p",
            p=self.project_id,
        )
        # Many projects, so that the planner looks a project up by its primary key
        # (with three rows a sequential scan is the cheaper plan).
        await self.sql(
            "INSERT INTO projects (id, name, status, created_at, updated_at)"
            " SELECT gen_random_uuid(), 'P' || g, 'archived', now(), now()"
            " FROM generate_series(1, :n) g",
            n=5_000,
        )
        await self.sql("ANALYZE tasks")
        await self.sql("ANALYZE queue_entries")
        await self.sql("ANALYZE projects")

    async def claim_statement(self) -> tuple:
        with self.captured_statements() as captured:
            claimed = await self.claims.claim_next(WORKER)
        assert claimed is not None  # the claim is real, and past the skipped ones
        selects = [s for s in captured if "FOR UPDATE SKIP LOCKED" in s[0]]
        self.assertEqual(len(selects), 1)
        return selects[0]

    async def test_the_claim_reads_the_queue_in_index_order_through_primary_keys(self):
        await self.busy_queue()
        sql, parameters = await self.claim_statement()

        self.assertIn("FROM projects", sql)  # the filter is really in the statement
        self.assertIn("WHERE tasks.id = queue_entries.task_id)) = 'active'", sql)
        for mode in ("force_custom_plan", "force_generic_plan"):
            with self.subTest(plan_cache_mode=mode):
                (explained,) = await self.plan(sql, parameters, mode)
                nodes = list(self.plan_nodes(explained["Plan"]))
                types = {node["Node Type"] for node in nodes}
                self.assertNotIn("Seq Scan", types)
                # Nothing is sorted: the index hands the entries over in order.
                self.assertFalse({t for t in types if "Sort" in t}, types)
                indexes = {
                    node["Relation Name"]: node["Index Name"]
                    for node in nodes
                    if "Index Name" in node
                }
                self.assertEqual(
                    indexes,
                    {
                        "queue_entries": "ix_queue_entries_claim_order",
                        "tasks": "pk_tasks",
                        "projects": "pk_projects",
                    },
                )

    async def test_the_claim_reads_only_the_skipped_entries_and_the_one_it_takes(self):
        await self.busy_queue()
        sql, parameters = await self.claim_statement()

        for mode in ("force_custom_plan", "force_generic_plan"):
            with self.subTest(plan_cache_mode=mode):
                (explained,) = await self.plan(sql, parameters, mode, analyze=True)
                (scan,) = [
                    node
                    for node in self.plan_nodes(explained["Plan"])
                    if node.get("Relation Name") == "queue_entries"
                ]
                # The entries of the Archived project in front of the first claimable
                # one, that one, and no more: not the 2,000 that wait behind it, and
                # nowhere near the 20,000 of the history.
                read = scan["Actual Rows"] + scan.get("Rows Removed by Filter", 0)
                self.assertLessEqual(read, self.SKIPPED + 5)
                self.assertGreaterEqual(read, self.SKIPPED)

    async def test_the_claim_without_skipped_entries_reads_one_entry(self):
        await self.busy_queue()
        await self.sql("UPDATE projects SET status = 'active', updated_at = now()")
        sql, parameters = await self.claim_statement()

        (explained,) = await self.plan(
            sql, parameters, "force_generic_plan", analyze=True
        )
        (scan,) = [
            node
            for node in self.plan_nodes(explained["Plan"])
            if node.get("Relation Name") == "queue_entries"
        ]
        self.assertLessEqual(
            scan["Actual Rows"] + scan.get("Rows Removed by Filter", 0), 2
        )


if __name__ == "__main__":
    unittest.main()
