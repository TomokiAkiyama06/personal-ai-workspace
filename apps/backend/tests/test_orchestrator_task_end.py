"""What a task's end undoes, and the retryable after-step (issue #125).

On a real PostgreSQL: the ``TaskService`` listener (``TaskEndCleanup.on_task_event``)
revokes the open approvals of a task that is completed, failed or cancelled and
retires the ``session_only`` memories that came from it; when that did not happen
(the process died after the commit, a step failed, a version was locked) the sweep
finds the residue in the stored state and finishes it. The maintenance loop's real
freshness jobs run in the same database.
"""

import asyncio
import contextlib
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import text

from paw_backend.authz import InMemoryAuditSink, Principal, SystemRole
from paw_backend.memory.versioning import FreshnessMaintenance
from paw_backend.orchestrator.freshness_loop import FreshnessJobLoop
from paw_backend.orchestrator.task_end import (
    TaskEndCleanup,
    TaskEndReport,
    TaskEndResidue,
)
from paw_backend.tasks import Actor, TaskCommand, TaskService, TaskState
from paw_backend.tools import (
    ApprovalOutcome,
    ApprovalService,
    GrantPattern,
    PostgresApprovalStore,
    PostgresTaskGrantStore,
    ScopeStatus,
    TaskGrantStatus,
)

from .gate_support import ALWAYS_ACTIVE
from .support import make_settings
from .task_support import FIRST_RUN, make_completable, single_target
from .tools_store_contract import LIMITS, binding_of, new_approval
from .tools_support import U1
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

    # -- fenced to the end (Codex P1 on PR #151) ------------------------------------

    async def test_a_cleanup_after_a_reopening_leaves_the_new_run_alone(self):
        # The sweep saw the task ended, then a Retry re-opened it, the new run
        # started, opened an approval and wrote a session memory before the
        # cleanup ran.
        task_id = await self.new_task(self.bare_tasks)
        await self.end(task_id, TaskState.FAILED, self.bare_tasks)
        await self.bare_tasks.execute(task_id, TaskCommand.RETRY, actor=SYSTEM)
        await self.bare_tasks.execute(task_id, TaskCommand.START, actor=SYSTEM)
        memory = self.session_memory(task_id)
        approval = await self.open_approval(task_id)

        report = await self.cleanup.finish(task_id)

        self.assertEqual(report, TaskEndReport(task_id, 0, 0))
        self.assertEqual(self.status_of(memory), "active")
        self.assertEqual(await self.approval_status(approval), "pending")

    async def test_a_cleanup_after_a_reopening_before_the_new_run_finishes_the_end(
        self,
    ):
        # Codex P2 on PR #151 (task_end.py:233), Decision 0064: a Retry took the
        # fence first. Until the re-opened task moves (Start), every
        # ``session_only`` memory of the task is the ended run's.
        for reopen, ended in (
            (TaskCommand.RETRY, TaskState.FAILED),
            (TaskCommand.RESTART, TaskState.CANCELLED),
        ):
            with self.subTest(command=reopen.value):
                task_id = await self.new_task(self.bare_tasks)
                memory = self.session_memory(task_id)
                await self.end(task_id, ended, self.bare_tasks)
                survivor = await self.open_approval(task_id)
                await self.bare_tasks.execute(task_id, reopen, actor=SYSTEM)

                report = await self.cleanup.finish(task_id)

                self.assertEqual(report, TaskEndReport(task_id, 1, 1))
                self.assertEqual(self.status_of(memory), "deprecated")
                self.assertEqual(await self.approval_status(survivor), "revoked")
                # Once the new run starts, its memories are its own.
                await self.bare_tasks.execute(task_id, TaskCommand.START, actor=SYSTEM)
                later = self.session_memory(task_id, "new run note")
                self.assertEqual(
                    await self.cleanup.finish(task_id), TaskEndReport(task_id, 0, 0)
                )
                self.assertEqual(self.status_of(later), "active")

    async def test_a_cleanup_that_waited_behind_a_reopening_still_finishes(self):
        # The listener's cleanup of the end waits for the task row while the
        # Retry that re-opens the task holds it; once the Retry commits, the
        # fence must read the Retry's event too (not the snapshot the lock
        # wait began with), and finish the ended run.
        task_id = await self.new_task(self.bare_tasks)
        memory = self.session_memory(task_id)
        await self.end(task_id, TaskState.FAILED, self.bare_tasks)
        with self.engine.connect() as retry:
            version = retry.execute(
                text("SELECT version FROM tasks WHERE id = :t FOR UPDATE"),
                {"t": task_id},
            ).scalar_one()
            retry.execute(
                text("UPDATE tasks SET state = 'queued', version = :v WHERE id = :t"),
                {"t": task_id, "v": version + 1},
            )
            retry.execute(
                text(
                    "INSERT INTO task_events (task_id, attempt, retry_count,"
                    " command, from_state, to_state, actor_kind, task_version,"
                    " created_at) VALUES (:t, 1, 1, 'retry', 'failed', 'queued',"
                    " 'system', :v, now())"
                ),
                {"t": task_id, "v": version + 1},
            )
            finishing = asyncio.create_task(self.cleanup.finish(task_id))
            done, _ = await asyncio.wait({finishing}, timeout=0.5)
            self.assertEqual(done, set())  # the cleanup waits for the task row
            retry.commit()
            report = await asyncio.wait_for(finishing, 10)

        self.assertEqual(report, TaskEndReport(task_id, 0, 1))
        self.assertEqual(self.status_of(memory), "deprecated")

    async def test_a_reopening_waits_until_the_cleanup_is_over(self):
        task_id = await self.new_task(self.bare_tasks)
        memory = self.session_memory(task_id)
        await self.end(task_id, TaskState.FAILED, self.bare_tasks)
        inside, release = asyncio.Event(), asyncio.Event()
        revoke = self.approvals.revoke_task

        async def slow_revoke(ended_id):
            inside.set()
            await release.wait()
            return await revoke(ended_id)

        with patch.object(self.approvals, "revoke_task", slow_revoke):
            finishing = asyncio.create_task(self.cleanup.finish(task_id))
            await asyncio.wait_for(inside.wait(), 10)
            retrying = asyncio.create_task(
                self.bare_tasks.execute(task_id, TaskCommand.RETRY, actor=SYSTEM)
            )
            done, _ = await asyncio.wait({retrying}, timeout=0.5)
            self.assertEqual(done, set())  # the Retry waits for the task row
            release.set()
            report = await asyncio.wait_for(finishing, 10)
            await asyncio.wait_for(retrying, 10)

        self.assertEqual(report, TaskEndReport(task_id, 0, 1))
        self.assertEqual(self.status_of(memory), "deprecated")
        # After the Retry and the new run's Start, a new run's memory is no
        # longer the ended task's.
        await self.bare_tasks.execute(task_id, TaskCommand.START, actor=SYSTEM)
        later = self.session_memory(task_id, "new run note")
        self.assertEqual(
            await self.cleanup.finish(task_id), TaskEndReport(task_id, 0, 0)
        )
        self.assertEqual(self.status_of(later), "active")

    async def test_a_fence_that_cannot_be_taken_is_a_failed_step(self):
        task_id = await self.new_task(self.bare_tasks)
        memory = self.session_memory(task_id)
        await self.end(task_id, TaskState.CANCELLED, self.bare_tasks)
        with self.engine.connect() as locker:
            locker.execute(
                text("SELECT id FROM tasks WHERE id = :t FOR UPDATE"), {"t": task_id}
            )
            with (
                patch("paw_backend.orchestrator.task_end.FENCE_TIMEOUT_SECONDS", 0.2),
                self.assertLogs("paw_backend.orchestrator.task_end", "WARNING"),
            ):
                report = await self.cleanup.finish(task_id)
            locker.rollback()

        self.assertEqual(report, TaskEndReport(task_id, 0, 0, ("fence",)))
        self.assertEqual(self.status_of(memory), "active")
        await self.cleanup.sweep()  # the sweep repeats it
        self.assertEqual(self.status_of(memory), "deprecated")

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

    async def test_the_sweep_revokes_a_grant_the_listener_never_did(self):
        # Codex P2 on PR #216: a task whose approvals were all used, with no
        # session memory, but an active 「このタスクの間は許可」 grant (Decision
        # 0085) is still residue, so the sweep revokes the grant and audits it.
        database = self._database()
        grants = PostgresTaskGrantStore(database)
        approvals = ApprovalService(self.store, InMemoryAuditSink(), grants=grants)
        cleanup = TaskEndCleanup(
            approvals, self.new_freshness(), TaskEndResidue(database)
        )
        task_id = await self.new_task(self.bare_tasks)
        await self.bare_tasks.execute(task_id, TaskCommand.START, actor=SYSTEM)
        now = datetime.now(UTC)
        new = new_approval(
            task_id=task_id,
            expires_at=now + timedelta(hours=1),
            grant_pattern=GrantPattern(ScopeStatus.IN_SCOPE, ()),
        )
        await self.store.open_request(new, now=now, limits=LIMITS)
        granted = await approvals.approve_for_task(
            new.approval_id, Principal(U1, SystemRole.USER, {})
        )
        self.assertEqual(granted.outcome, ApprovalOutcome.APPROVED_FOR_TASK)
        # The waiting call used the approval: no open approval is left.
        consumed = await self.store.consume(
            new.approval_id, binding_of(new), now=datetime.now(UTC)
        )
        self.assertEqual(consumed.value, "consumed")
        await self.end(task_id, TaskState.CANCELLED, self.bare_tasks)
        residue = TaskEndResidue(database)
        self.assertIn(task_id, await residue.task_ids(100))

        await cleanup.sweep()

        record = await grants.get(granted.grant_id)
        self.assertEqual(record.status, TaskGrantStatus.REVOKED)
        self.assertNotIn(task_id, await residue.task_ids(100))

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

    async def test_a_reopened_task_is_residue_until_it_moves(self):
        # Decision 0064: re-opened but not moved yet, its memories are still
        # the ended run's (the listener's cleanup may have lost the fence to
        # the Retry); once it moved (Start), it is not residue any more.
        residue = TaskEndResidue(self._database())
        waiting = await self.new_task(self.bare_tasks)
        left = self.session_memory(waiting)
        await self.end(waiting, TaskState.FAILED, self.bare_tasks)
        survivor = await self.open_approval(waiting)
        await self.bare_tasks.execute(waiting, TaskCommand.RETRY, actor=SYSTEM)
        moved = await self.new_task(self.bare_tasks)
        await self.end(moved, TaskState.FAILED, self.bare_tasks)
        await self.bare_tasks.execute(moved, TaskCommand.RETRY, actor=SYSTEM)
        await self.bare_tasks.execute(moved, TaskCommand.START, actor=SYSTEM)
        kept = self.session_memory(moved)
        await self.open_approval(moved)
        # An event that is no transition (a change of the Working Set: the
        # state stays) does not count as moving.
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO task_events (task_id, attempt, retry_count,"
                    " command, from_state, to_state, actor_kind, task_version,"
                    " created_at) SELECT id, attempt, retry_count,"
                    " 'change_working_set', state, state, 'system', version, now()"
                    " FROM tasks WHERE id = :t"
                ),
                {"t": waiting},
            )

        found = await residue.task_ids(100)
        self.assertIn(waiting, found)
        self.assertNotIn(moved, found)
        await self.cleanup.sweep()
        self.assertEqual(self.status_of(left), "deprecated")
        self.assertEqual(await self.approval_status(survivor), "revoked")
        self.assertEqual(self.status_of(kept), "active")
        self.assertNotIn(waiting, await residue.task_ids(100))

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

    async def test_tasks_that_keep_failing_do_not_starve_the_rest(self):
        ended = []
        for _ in range(3):
            task_id = await self.new_task(self.bare_tasks)
            await self.open_approval(task_id)
            await self.end(task_id, TaskState.CANCELLED, self.bare_tasks)
            ended.append(task_id)
        # The last in id order: at least two residue tasks come before it.
        target = max(ended)
        residue = TaskEndResidue(self._database())
        pending = await residue.task_ids(100)
        self.assertIn(target, pending)
        revoke = self.approvals.revoke_task

        async def fails_but_for_the_target(task_id):
            if task_id != target:
                raise RuntimeError("store down")
            return await revoke(task_id)

        swept = []
        with (
            patch.object(self.approvals, "revoke_task", fails_but_for_the_target),
            self.assertLogs("paw_backend.orchestrator.task_end", "WARNING"),
        ):
            # A sweep of two resumes after the last one: every residue task is
            # reached, however many before it fail each time.
            for _ in range(len(pending) // 2 + 1):
                swept.extend(report.task_id for report in await self.cleanup.sweep(2))
        self.assertIn(target, swept)
        self.assertNotIn(target, await residue.task_ids(100))

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

        class Residue(TaskEndResidue):
            @contextlib.asynccontextmanager
            async def hold_ended(self, task_id):
                yield True  # the task is terminal (the fence has its own tests)

        approvals, freshness = Approvals(), Freshness()
        return (
            TaskEndCleanup(approvals, freshness, Residue(database)),
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

    async def test_a_sweep_past_the_last_task_starts_over_at_once(self):
        # A full sweep that ended on the last residue task leaves its position
        # there; the next sweep finds nothing after it and must start over in
        # the same call, not give up a whole maintenance interval.
        cleanup, approvals, _ = self.cleanup()
        pending = tuple(sorted(uuid.uuid4() for _ in range(2)))

        async def task_ids(limit, *, after=None):
            return tuple(t for t in pending if after is None or t > after)[:limit]

        cleanup._residue.task_ids = task_ids
        first = await cleanup.sweep(2)
        second = await cleanup.sweep(2)

        self.assertEqual([r.task_id for r in first], list(pending))
        self.assertEqual([r.task_id for r in second], list(pending))

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
