"""What a task's end undoes, and the retryable after-step (issue #125).

On a real PostgreSQL: the ``TaskService`` listener (``TaskEndCleanup.on_task_event``)
revokes the open approvals of a task that is completed, failed or cancelled and
retires the ``session_only`` memories that came from it; when that did not happen
(the process died after the commit, a step failed, a version was locked) the sweep
finds the residue in the stored state and finishes it. The maintenance loop's real
freshness jobs run in the same database.
"""

import asyncio
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import text

from paw_backend.authz import InMemoryAuditSink
from paw_backend.memory.versioning import FreshnessMaintenance
from paw_backend.orchestrator.freshness_loop import FreshnessJobLoop
from paw_backend.orchestrator.task_end import (
    TaskEndCleanup,
    TaskEndReport,
    TaskEndResidue,
)
from paw_backend.tasks import Actor, TaskCommand, TaskService, TaskState
from paw_backend.tools import ApprovalService, PostgresApprovalStore

from .gate_support import ALWAYS_ACTIVE
from .support import make_settings
from .task_support import FIRST_RUN, make_completable, single_target
from .tools_store_contract import LIMITS, new_approval
from .versioning_support import T0, PostgresVersioningTestCase, requires_postgres

SYSTEM = Actor.system()


@requires_postgres
class TaskEndTest(PostgresVersioningTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.project_id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.repository_id = uuid.uuid4()
        database = self._database()
        self.store = PostgresApprovalStore(database)
        self.approvals = ApprovalService(self.store, InMemoryAuditSink())
        self.cleanup = TaskEndCleanup(
            self.approvals, self.new_freshness(), TaskEndResidue(database)
        )
        self.tasks = TaskService(
            database, project_gate=ALWAYS_ACTIVE, listeners=[self.cleanup.on_task_event]
        )
        # The same database without the listener: a process that died between the
        # commit of the terminal transition and its listener.
        self.bare_tasks = TaskService(database, project_gate=ALWAYS_ACTIVE)

    # -- seeding ---------------------------------------------------------------

    async def new_task(self, service=None) -> uuid.UUID:
        event = await (service or self.tasks).create_task(
            project_id=self.project_id,
            created_by=self.user_id,
            title="Fix the parser",
            repositories=single_target(self.repository_id),
        )
        return event.task_id

    async def end(self, task_id, state: TaskState, service=None) -> None:
        service = service or self.tasks
        if state is TaskState.CANCELLED:
            await service.execute(task_id, TaskCommand.CANCEL, actor=SYSTEM)
            return
        await service.execute(task_id, TaskCommand.START, actor=SYSTEM)
        if state is TaskState.FAILED:
            await service.execute(task_id, TaskCommand.FAIL, actor=SYSTEM)
            return
        await service.execute(task_id, TaskCommand.BEGIN_EVALUATION, actor=SYSTEM)
        await make_completable(service, task_id, self.repository_id, FIRST_RUN)
        await service.execute(task_id, TaskCommand.COMPLETE, actor=SYSTEM)

    def session_memory(self, task_id, title="task note"):
        me = self.user()
        seeded = self.seed(title, owner=me.user_id, freshness="session_only")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO memory_sources (memory_version_id, source_type,"
                    " source_ref) VALUES (:v, 'task', :r)"
                ),
                {"v": seeded.version_id, "r": str(task_id)},
            )
        return seeded

    async def open_approval(self, task_id) -> uuid.UUID:
        now = datetime.now(UTC)
        new = new_approval(task_id=task_id, expires_at=now + timedelta(hours=1))
        await self.store.open_request(new, now=now, limits=LIMITS)
        return new.approval_id

    def status_of(self, seeded) -> str:
        return self.versions(seeded.memory_id)[0].status

    async def approval_status(self, approval_id) -> str:
        return (await self.store.get(approval_id)).status.value

    # -- right after the commit ------------------------------------------------

    async def test_each_end_revokes_the_approvals_and_retires_the_memories(self):
        for state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
            with self.subTest(state=state.value):
                task_id = await self.new_task()
                memory = self.session_memory(task_id)
                approval = await self.open_approval(task_id)

                await self.end(task_id, state)

                self.assertEqual(self.status_of(memory), "deprecated")
                self.assertEqual(await self.approval_status(approval), "revoked")
                self.assertNotIn(
                    task_id, await TaskEndResidue(self._database()).task_ids(100)
                )

    async def test_a_task_that_has_not_ended_keeps_them(self):
        task_id = await self.new_task()
        memory = self.session_memory(task_id)
        approval = await self.open_approval(task_id)
        await self.tasks.execute(task_id, TaskCommand.START, actor=SYSTEM)
        await self.tasks.execute(task_id, TaskCommand.PAUSE, actor=SYSTEM)

        self.assertEqual(self.status_of(memory), "active")
        self.assertEqual(await self.approval_status(approval), "pending")
        # ... and the sweep does not touch a live task either.
        await self.cleanup.sweep()
        self.assertEqual(self.status_of(memory), "active")
        self.assertEqual(await self.approval_status(approval), "pending")

    async def test_only_the_tasks_own_session_memories_are_retired(self):
        task_id, other = await self.new_task(), uuid.uuid4()
        mine = self.session_memory(task_id)
        theirs = self.session_memory(other, "another task's")
        long_term = self.seed("kept", owner=self.user().user_id)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO memory_sources (memory_version_id, source_type,"
                    " source_ref) VALUES (:v, 'task', :r)"
                ),
                {"v": long_term.version_id, "r": str(task_id)},
            )

        await self.end(task_id, TaskState.CANCELLED)

        self.assertEqual(self.status_of(mine), "deprecated")
        self.assertEqual(self.status_of(theirs), "active")
        self.assertEqual(self.status_of(long_term), "active")  # not session_only

    async def test_more_memories_than_one_batch_are_all_retired(self):
        freshness = self.new_freshness(batch=2)
        cleanup = TaskEndCleanup(
            self.approvals, freshness, TaskEndResidue(self._database())
        )
        task_id = await self.new_task(self.bare_tasks)
        memories = [self.session_memory(task_id, f"note {n}") for n in range(5)]
        await self.end(task_id, TaskState.CANCELLED, self.bare_tasks)

        report = await cleanup.finish(task_id)

        self.assertEqual(report, TaskEndReport(task_id, 0, 5))
        self.assertEqual({self.status_of(m) for m in memories}, {"deprecated"})

    async def test_a_reopened_task_loses_the_approvals_that_survived_its_end(self):
        task_id = await self.new_task(self.bare_tasks)
        await self.end(task_id, TaskState.FAILED, self.bare_tasks)
        survivor = await self.open_approval(task_id)  # left by a failed cleanup

        await self.tasks.execute(task_id, TaskCommand.RETRY, actor=SYSTEM)

        self.assertEqual(await self.approval_status(survivor), "revoked")

    # -- the retryable after-step -------------------------------------------------

    async def test_the_sweep_finishes_what_the_listener_never_did(self):
        task_id = await self.new_task(self.bare_tasks)
        memory = self.session_memory(task_id)
        approval = await self.open_approval(task_id)
        await self.end(task_id, TaskState.CANCELLED, self.bare_tasks)
        self.assertEqual(self.status_of(memory), "active")
        residue = TaskEndResidue(self._database())
        self.assertIn(task_id, await residue.task_ids(100))

        reports = await self.cleanup.sweep()

        self.assertIn(TaskEndReport(task_id, 1, 1), reports)
        self.assertEqual(self.status_of(memory), "deprecated")
        self.assertEqual(await self.approval_status(approval), "revoked")
        self.assertNotIn(task_id, await residue.task_ids(100))
        # Running it again changes nothing.
        self.assertNotIn(task_id, [r.task_id for r in await self.cleanup.sweep()])
        self.assertEqual(len(self.changes(memory.version_id)), 1)

    async def test_a_locked_version_is_finished_by_a_later_sweep(self):
        task_id = await self.new_task()
        memory = self.session_memory(task_id)
        with self.engine.connect() as locker:
            locker.execute(
                text("SELECT id FROM memory_versions WHERE id = :v FOR UPDATE"),
                {"v": memory.version_id},
            )
            await self.end(task_id, TaskState.CANCELLED)  # SKIP LOCKED: left
            self.assertEqual(self.status_of(memory), "active")
            locker.rollback()

        await self.cleanup.sweep()

        self.assertEqual(self.status_of(memory), "deprecated")

    async def test_a_reopened_task_is_not_residue(self):
        task_id = await self.new_task(self.bare_tasks)
        memory = self.session_memory(task_id)
        await self.end(task_id, TaskState.FAILED, self.bare_tasks)
        await self.bare_tasks.execute(task_id, TaskCommand.RETRY, actor=SYSTEM)

        self.assertNotIn(task_id, await TaskEndResidue(self._database()).task_ids(100))
        await self.cleanup.sweep()
        self.assertEqual(self.status_of(memory), "active")

    async def test_the_residue_is_bounded(self):
        ended = []
        for _ in range(3):
            task_id = await self.new_task(self.bare_tasks)
            await self.open_approval(task_id)
            self.session_memory(task_id)
            await self.end(task_id, TaskState.CANCELLED, self.bare_tasks)
            ended.append(task_id)
        residue = TaskEndResidue(self._database())
        self.assertEqual(len(await residue.task_ids(2)), 2)
        found = await residue.task_ids(100)
        self.assertEqual(list(found), sorted(found))
        self.assertTrue(set(ended) <= set(found))

    # -- the maintenance loop's cycle, on the real jobs ------------------------------

    async def test_a_cycle_sweeps_marks_stale_and_expires(self):
        task_id = await self.new_task(self.bare_tasks)
        memory = self.session_memory(task_id)
        await self.end(task_id, TaskState.CANCELLED, self.bare_tasks)
        me = self.user()
        due = self.seed(
            "due",
            owner=me.user_id,
            freshness="revalidate",
            verified_at=T0 - timedelta(days=90),
            revalidate_after=timedelta(days=90),
        )
        expired = self.seed(
            "gone", owner=me.user_id, freshness="expiring", expires_at=T0
        )
        freshness = self.new_freshness()
        loop = FreshnessJobLoop(
            freshness,
            TaskEndCleanup(self.approvals, freshness, TaskEndResidue(self._database())),
        )

        report = await loop.run_cycle()

        self.assertEqual(report.failed, ())
        self.assertGreaterEqual(report.finished_tasks, 1)
        self.assertEqual((report.marked_stale, report.expired), (1, 1))
        self.assertEqual(self.status_of(memory), "deprecated")
        self.assertEqual(self.versions(due.memory_id)[0].stale_since, T0)
        self.assertEqual(self.status_of(expired), "deprecated")


class FinishTest(unittest.IsolatedAsyncioTestCase):
    """One step that fails does not stop the other (no database needed)."""

    def cleanup(self, *, approvals_fail=False, memories_fail=False):
        from paw_backend.db import Database

        database = Database(make_settings())

        class Approvals(ApprovalService):
            def __init__(self) -> None:  # no store: revoke_task is replaced
                self.calls = []

            async def revoke_task(self, task_id):
                self.calls.append(task_id)
                if approvals_fail:
                    raise RuntimeError("store down: secret detail")
                return 2

        class Freshness(FreshnessMaintenance):
            def __init__(self) -> None:
                super().__init__(database)
                self.calls = []

            async def end_task(self, task_id):
                self.calls.append(task_id)
                if memories_fail:
                    raise RuntimeError("db down: secret detail")
                return 0

        approvals, freshness = Approvals(), Freshness()
        return (
            TaskEndCleanup(approvals, freshness, TaskEndResidue(database)),
            approvals,
            freshness,
        )

    async def test_a_failed_revocation_still_retires_the_memories(self):
        cleanup, approvals, freshness = self.cleanup(approvals_fail=True)
        task_id = uuid.uuid4()
        with self.assertLogs("paw_backend.orchestrator.task_end", "WARNING") as logs:
            report = await cleanup.finish(task_id)
        self.assertEqual(report.failed, ("approvals",))
        self.assertFalse(report.done)
        self.assertEqual(freshness.calls, [task_id])
        self.assertNotIn("secret detail", "\n".join(logs.output))

    async def test_a_failed_retirement_still_revokes(self):
        cleanup, approvals, _ = self.cleanup(memories_fail=True)
        task_id = uuid.uuid4()
        with self.assertLogs("paw_backend.orchestrator.task_end", "WARNING"):
            report = await cleanup.finish(task_id)
        self.assertEqual(report, TaskEndReport(task_id, 2, 0, ("memories",)))
        self.assertEqual(approvals.calls, [task_id])

    async def test_a_stalled_retirement_is_cut_off_and_left_to_the_sweep(self):
        cleanup, approvals, freshness = self.cleanup()
        stalled = asyncio.Event()

        async def stall(task_id):
            stalled.set()
            await asyncio.Event().wait()

        freshness.end_task = stall
        task_id = uuid.uuid4()
        with (
            patch("paw_backend.orchestrator.task_end.RETIRE_TIMEOUT_SECONDS", 0.05),
            self.assertLogs("paw_backend.orchestrator.task_end", "WARNING") as logs,
        ):
            report = await asyncio.wait_for(cleanup.finish(task_id), 5)
        self.assertTrue(stalled.is_set())
        self.assertEqual(report, TaskEndReport(task_id, 2, 0, ("memories",)))
        self.assertIn("TimeoutError", "\n".join(logs.output))

    async def test_the_listener_acts_on_the_end_and_on_a_reopening_only(self):
        cleanup, approvals, freshness = self.cleanup()

        class Event:
            def __init__(self, from_state, to_state) -> None:
                self.task_id = uuid.uuid4()
                self.from_state = from_state
                self.to_state = to_state

        await cleanup.on_task_event(Event(TaskState.QUEUED, TaskState.RUNNING))
        self.assertEqual((approvals.calls, freshness.calls), ([], []))
        ended = Event(TaskState.RUNNING, TaskState.FAILED)
        await cleanup.on_task_event(ended)
        self.assertEqual((approvals.calls, freshness.calls), ([ended.task_id],) * 2)
        reopened = Event(TaskState.FAILED, TaskState.QUEUED)
        await cleanup.on_task_event(reopened)
        self.assertEqual(approvals.calls[-1], reopened.task_id)
        self.assertEqual(freshness.calls, [ended.task_id])  # memories stay retired
        await cleanup.on_task_event(object())  # not an event: ignored

    def test_it_refuses_what_it_cannot_use(self):
        cleanup, approvals, freshness = self.cleanup()
        residue = cleanup._residue
        with self.assertRaises(TypeError):
            TaskEndCleanup(object(), freshness, residue)
        with self.assertRaises(TypeError):
            TaskEndCleanup(approvals, object(), residue)
        with self.assertRaises(TypeError):
            TaskEndCleanup(approvals, freshness, object())
        with self.assertRaises(TypeError):
            TaskEndResidue(object())


if __name__ == "__main__":
    unittest.main()
