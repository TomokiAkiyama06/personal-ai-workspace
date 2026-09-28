"""The orchestrator and the Project state gate (Issue #83, Decision 0020).

The task lane REQUIRES a project gate. With the real ``ProjectStateGate`` on a real
PostgreSQL these tests show what the orchestrator does when a project is not
Active: the queue does not hand out its entries (the claim skips them), a Start
that the gate refuses gives the claimed entry back to the queue (the task stays
``queued``; nothing was started; it runs after the project is Active again), and
work that already RUNS is not stopped (the commands that end a run never ask the
gate). The project's state is changed with SQL: only the gate's reading of the
``status`` column matters here.
"""

import asyncio
import unittest
import uuid

from sqlalchemy import true

from paw_backend.orchestrator.domain import RunOutcome
from paw_backend.projects import ProjectStateGate
from paw_backend.projects.errors import ProjectBusyError
from paw_backend.tasks import ProjectNotActiveError, TaskCommand, TaskState
from paw_backend.tasks.queueing import BudgetPreset, TaskQueue

from .gate_support import ALWAYS_ACTIVE
from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    SpyBudget,
    make_plan,
    node,
    requires_postgres,
    until,
)

Out = RunOutcome
PENDING_DELETION_TIMES = (
    "deletion_started_at = now(), deletion_scheduled_at = now() + interval '720 hours'"
)
NO_TIMES = "deletion_started_at = NULL, deletion_scheduled_at = NULL"


class BusyGate:
    """A gate whose lock is never granted in time (its documented, retryable error)."""

    async def require_active(self, session, project_id) -> None:
        raise ProjectBusyError()

    def active_condition(self, project_id):
        return true()


@requires_postgres
class ProjectGateTest(PostgresOrchestratorTestCase):
    async def new_project(self) -> uuid.UUID:
        project_id = uuid.uuid4()
        await self.owner_sql(
            "INSERT INTO projects (id, name, status, created_at, updated_at)"
            " VALUES (:id, :name, 'active', now(), now())",
            id=project_id,
            name=f"Project {project_id.hex}",
        )
        self.addAsyncCleanup(
            self.owner_sql, "DELETE FROM projects WHERE id = :id", id=project_id
        )
        return project_id

    async def set_status(self, project_id, status: str) -> None:
        """Put the project in ``status`` (the deletion times go with the status)."""
        times = PENDING_DELETION_TIMES if status == "pending_deletion" else NO_TIMES
        await self.owner_sql(
            f"UPDATE projects SET status = :status, updated_at = now(), {times}"
            " WHERE id = :id",
            id=project_id,
            status=status,
        )

    async def queued_task(self, h, project_id, plan=None) -> uuid.UUID:
        """A task of the project with a plan, a budget and a queue entry."""
        task_id = await self.create_task(project_id=project_id)
        await h.orchestrator.submit_plan(task_id, plan or make_plan(node("a")))
        await h.orchestrator.enqueue_task(task_id, preset=BudgetPreset.STANDARD)
        return task_id

    async def entry(self) -> dict:
        (entry,) = await self.rows(
            "SELECT status, claimed_by, claim_count FROM queue_entries"
        )
        return entry

    def gated(self, **options):
        spy = SpyBudget(self.database)
        runtime = FakeRuntime("local")
        h = self.harness(
            project_gate=ProjectStateGate(),
            budget=spy,
            runtimes={"local": runtime},
            **options,
        )
        return h, runtime, spy

    async def test_a_queued_task_of_an_archived_project_is_not_started_and_runs_later(
        self,
    ):
        h, runtime, spy = self.gated()
        project_id = await self.new_project()
        task_id = await self.queued_task(h, project_id)
        await self.set_status(project_id, "archived")

        report = await h.orchestrator.run_once("w1")

        # The claim skipped the entry: nothing was claimed, started or charged.
        self.assertEqual(report.outcome, Out.IDLE)
        entry = await self.entry()
        self.assertEqual((entry["status"], entry["claim_count"]), ("queued", 0))
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.QUEUED)
        self.assertEqual((runtime.assignments, spy.started), ([], []))

        # After the Unarchive the same entry is claimed and the task runs.
        await self.set_status(project_id, "active")
        report = await h.orchestrator.run_once("w2")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual([a.node_key for a in runtime.assignments], ["a"])
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.EVALUATING)
        entry = await self.entry()
        self.assertEqual((entry["status"], entry["claim_count"]), ("completed", 1))

    async def check_a_start_the_gate_refuses(self, status: str) -> None:
        h, runtime, spy = self.gated()
        project_id = await self.new_project()
        task_id = await self.queued_task(h, project_id)
        entry = await h.queue.claim_next("w1")  # claimed while the project is Active
        assert entry is not None
        await self.set_status(project_id, status)

        report = await h.orchestrator.run_entry(entry, "w1")

        # The gate refused the Start: the entry is queued again (its place and its
        # generation kept), the task is still queued, and nothing of the run
        # happened: no runtime timer, no node, no epoch on the DAG.
        self.assertEqual(report.outcome, Out.SKIPPED)
        self.assertEqual(
            await self.entry(),
            {"status": "queued", "claimed_by": None, "claim_count": 1},
        )
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.QUEUED)
        self.assertEqual((runtime.assignments, spy.started), ([], []))
        self.assertEqual((await self.store.get(task_id, 1)).epoch, 0)

        # The claim goes past it while the project is not Active, and takes it as
        # a newer generation once the project is.
        self.assertEqual((await h.orchestrator.run_once("w1")).outcome, Out.IDLE)
        await self.set_status(project_id, "active")
        report = await h.orchestrator.run_once("w2")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual([a.node_key for a in runtime.assignments], ["a"])
        entry_row = await self.entry()
        self.assertEqual(
            (entry_row["status"], entry_row["claim_count"]), ("completed", 2)
        )

    async def test_an_archive_after_the_claim_gives_the_entry_back(self):
        await self.check_a_start_the_gate_refuses("archived")

    async def test_a_deletion_after_the_claim_gives_the_entry_back(self):
        await self.check_a_start_the_gate_refuses("pending_deletion")

    async def test_a_gate_that_cannot_lock_in_time_gives_the_entry_back_too(self):
        # ``ProjectBusyError`` is the gate's retryable error (its lock wait timed
        # out): the entry is not lost and not stuck until its lease ends.
        runtime = FakeRuntime("local")
        spy = SpyBudget(self.database)
        h = self.harness(
            project_gate=BusyGate(),
            queue=TaskQueue(self.database, project_gate=ALWAYS_ACTIVE),
            budget=spy,
            runtimes={"local": runtime},
        )
        task_id = await self.queued_task(h, await self.new_project())

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.SKIPPED)
        self.assertEqual(
            await self.entry(),
            {"status": "queued", "claimed_by": None, "claim_count": 1},
        )
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.QUEUED)
        self.assertEqual((runtime.assignments, spy.started), ([], []))

    async def test_a_task_is_not_queued_in_an_archived_project(self):
        h, _, _ = self.gated()
        project_id = await self.new_project()
        task_id = await self.create_task(project_id=project_id)
        await self.set_status(project_id, "archived")

        with self.assertRaises(ProjectNotActiveError):
            await h.orchestrator.enqueue_task(task_id, preset=BudgetPreset.STANDARD)

        self.assertEqual(await self.rows("SELECT * FROM queue_entries"), [])
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.QUEUED)

    async def test_a_running_task_finishes_when_its_project_is_archived_meanwhile(self):
        # Decision 0020: work that already RUNS is not stopped, and the commands that
        # end its run (begin evaluation here) never ask the gate.
        h, runtime, _ = self.gated()
        runtime.gate("a")
        project_id = await self.new_project()
        task_id = await self.queued_task(h, project_id)
        run = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(runtime.assignments) == 1, message="the node")

        await self.set_status(project_id, "archived")
        runtime.gates["a"].set()
        report = await asyncio.wait_for(run, 120)

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.EVALUATING)
        self.assertEqual((await self.entry())["status"], "completed")

    async def test_a_crashed_run_in_an_archived_project_resumes_after_the_unarchive(
        self,
    ):
        # The task is running when its worker dies and the project is archived; the
        # lease ends. The claim skips the entry while the project is not Active (the
        # gate applies to every claim), and the next Active claim takes the run over
        # (no Start: the task is already running).
        h, runtime, _ = self.gated()
        project_id = await self.new_project()
        task_id = await self.queued_task(h, project_id)
        entry = await h.queue.claim_next("crashed")
        assert entry is not None
        await h.tasks.execute(task_id, TaskCommand.START, actor=self.system)
        await self.set_status(project_id, "archived")
        await self.owner_sql(
            "UPDATE queue_entries SET claimed_at = now() - interval '10 seconds',"
            " lease_expires_at = now() - interval '5 seconds'"
            " WHERE status = 'claimed'"
        )

        self.assertEqual((await h.orchestrator.run_once("w2")).outcome, Out.IDLE)
        self.assertEqual(runtime.assignments, [])

        await self.set_status(project_id, "active")
        report = await h.orchestrator.run_once("w2")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual([a.node_key for a in runtime.assignments], ["a"])
        self.assertEqual((await self.entry())["claim_count"], 2)


if __name__ == "__main__":
    unittest.main()
