"""The commands the orchestrator gives a task are fenced to the run it works for.

The terminal commands (Begin evaluation, Fail, Wait) and Start are decided from
what the orchestrator saw a moment earlier. If the task is failed, retried and
started again in that moment, the replacement run is a different run of the same
task, and a command of the old run must not touch it. Each command therefore reads
the task afresh, refuses (a typed ``StaleRunError``, nothing written) when the run
is no longer the worker's, and is applied with ``expected_version`` so that the
comparison and the transition are one transaction (Decision 0021, section 8).

The races are made with barriers: a wrapper runs the interfering commands (Fail,
Retry, Start by "another worker") at the exact point in the orchestrator's code
where the race window is, and then lets the orchestrator go on.
"""

import unittest

from paw_backend.orchestrator import RunOutcome
from paw_backend.orchestrator.orchestrator import Orchestrator
from paw_backend.tasks import TaskCommand, TaskRun, TaskService, TaskState

from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    fail,
    gate_kwargs,
    make_plan,
    node,
    requires_postgres,
)

Out = RunOutcome


class Interference:
    """Fail, Retry and Start of the same task, run once, by 'another worker'."""

    def __init__(self, case: PostgresOrchestratorTestCase, task_id, tasks) -> None:
        self.case = case
        self.task_id = task_id
        self.tasks = tasks  # a TaskService that is NOT wrapped
        self.happened = 0

    async def __call__(self) -> None:
        if self.happened:
            return
        self.happened += 1
        for command, actor in (
            (TaskCommand.FAIL, self.case.system),
            (TaskCommand.RETRY, self.case.user),
            (TaskCommand.START, self.case.system),
        ):
            await self.tasks.execute(self.task_id, command, actor=actor)


@requires_postgres
class TerminalCommandsAreFencedTest(PostgresOrchestratorTestCase):
    """The race window of the finding: after the last look at the task, before the
    command."""

    async def scenario(self, plan, *, script=None, setup=None):
        runtime = FakeRuntime("local", script=script)
        h = self.harness(runtimes={"local": runtime})
        task_id = await self.prepare(h, plan)
        if setup is not None:
            await setup(h, task_id)
        interference = Interference(
            self, task_id, TaskService(self.new_database(), **gate_kwargs(TaskService))
        )

        original = Orchestrator._end_task

        async def raced(orchestrator, *args, **kwargs):
            await (
                interference()
            )  # the window: after the last _watch(), before the command
            return await original(orchestrator, *args, **kwargs)

        Orchestrator._end_task = raced
        self.addCleanup(setattr, Orchestrator, "_end_task", original)
        report = await h.orchestrator.run_once("w1")
        return h, task_id, report, interference

    async def assert_untouched_replacement(self, h, task_id, report, interference):
        self.assertEqual(interference.happened, 1)
        self.assertEqual(report.outcome, Out.SUPERSEDED)
        snapshot = await h.tasks.restore(task_id)
        # The task is the replacement run's: running, as Start left it.
        self.assertEqual(snapshot.state, TaskState.RUNNING)
        self.assertEqual(snapshot.run, TaskRun(1, 1))
        self.assertEqual(snapshot.last_event.command, TaskCommand.START)
        commands = [event.command for event in await h.tasks.history(task_id)]
        self.assertEqual(commands.count(TaskCommand.BEGIN_EVALUATION), 0)
        self.assertEqual(commands.count(TaskCommand.WAIT), 0)
        self.assertEqual(commands.count(TaskCommand.FAIL), 1)  # the interfering one

    async def test_begin_evaluation_of_an_old_run_does_not_reach_the_new_run(self):
        h, task_id, report, interference = await self.scenario(make_plan(node("a")))

        await self.assert_untouched_replacement(h, task_id, report, interference)

    async def test_fail_of_an_old_run_does_not_reach_the_new_run(self):
        h, task_id, report, interference = await self.scenario(
            make_plan(node("a")),
            script={"a": fail("Fatal", "no way", retryable=False)},
        )

        await self.assert_untouched_replacement(h, task_id, report, interference)

    async def test_the_wait_of_a_budget_stop_does_not_reach_the_new_run(self):
        async def limit(h, task_id):
            await self.owner_sql(
                "UPDATE budget_usages SET limit_value = 1"
                " WHERE task_id = :t AND kind = 'steps'",
                t=task_id,
            )

        h, task_id, report, interference = await self.scenario(
            make_plan(node("a"), node("b")), setup=limit
        )

        await self.assert_untouched_replacement(h, task_id, report, interference)

    async def test_the_fail_of_a_used_up_retry_budget_does_not_reach_the_new_run(self):
        async def limit(h, task_id):
            await self.owner_sql(
                "UPDATE budget_usages SET limit_value = 0"
                " WHERE task_id = :t AND kind = 'retries'",
                t=task_id,
            )

        h, task_id, report, interference = await self.scenario(
            make_plan(node("a")), script={"a": fail("Boom", "x")}, setup=limit
        )

        await self.assert_untouched_replacement(h, task_id, report, interference)

    async def test_a_restart_in_the_window_is_a_stale_attempt(self):
        h = self.harness()
        task_id = await self.prepare(h, make_plan(node("a")))
        other = TaskService(self.new_database(), **gate_kwargs(TaskService))
        original = Orchestrator._end_task
        seen = []

        async def raced(orchestrator, *args, **kwargs):
            if not seen:
                seen.append(1)
                await other.execute(task_id, TaskCommand.CANCEL, actor=self.user)
                await other.execute(task_id, TaskCommand.RESTART, actor=self.user)
                await other.execute(task_id, TaskCommand.START, actor=self.system)
            return await original(orchestrator, *args, **kwargs)

        Orchestrator._end_task = raced
        self.addCleanup(setattr, Orchestrator, "_end_task", original)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.SUPERSEDED)
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(
            (snapshot.state, snapshot.run), (TaskState.RUNNING, TaskRun(2, 0))
        )

    async def test_a_command_whose_task_ended_meanwhile_is_reported_as_ended(self):
        h = self.harness()
        task_id = await self.prepare(h, make_plan(node("a")))
        other = TaskService(self.new_database(), **gate_kwargs(TaskService))
        original = Orchestrator._end_task

        async def raced(orchestrator, *args, **kwargs):
            await other.execute(task_id, TaskCommand.CANCEL, actor=self.user)
            return await original(orchestrator, *args, **kwargs)

        Orchestrator._end_task = raced
        self.addCleanup(setattr, Orchestrator, "_end_task", original)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.TASK_ENDED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.CANCELLED)


@requires_postgres
class TheVersionFenceIsInTheTransactionTest(PostgresOrchestratorTestCase):
    """A change that commits between the fresh read and the command's own
    transaction: ``expected_version`` turns it into a conflict, and the command is
    decided again from a new read."""

    def wrap_execute(self, h, action):
        original = h.tasks.execute
        calls = []

        async def wrapped(task_id, command, **kwargs):
            if command is TaskCommand.BEGIN_EVALUATION and not calls:
                calls.append(1)
                await action(task_id)  # after the orchestrator's read, before its write
            return await original(task_id, command, **kwargs)

        h.tasks.execute = wrapped
        return original

    async def test_a_replacement_between_the_read_and_the_write_is_refused(self):
        h = self.harness()
        task_id = await self.prepare(h, make_plan(node("a")))
        other = TaskService(self.new_database(), **gate_kwargs(TaskService))

        async def replace(task_id):
            for command, actor in (
                (TaskCommand.FAIL, self.system),
                (TaskCommand.RETRY, self.user),
                (TaskCommand.START, self.system),
            ):
                await other.execute(task_id, command, actor=actor)

        self.wrap_execute(h, replace)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.SUPERSEDED)
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(
            (snapshot.state, snapshot.run), (TaskState.RUNNING, TaskRun(1, 1))
        )

    async def test_a_change_that_leaves_the_run_alone_is_only_a_retry_of_the_command(
        self,
    ):
        h = self.harness()
        task_id = await self.prepare(h, make_plan(node("a")))
        other = TaskService(self.new_database(), **gate_kwargs(TaskService))

        async def pause_and_resume(task_id):
            # The version moves, the run does not.
            await other.execute(task_id, TaskCommand.PAUSE, actor=self.user)
            await other.execute(task_id, TaskCommand.RESUME, actor=self.user)

        self.wrap_execute(h, pause_and_resume)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.EVALUATING)


@requires_postgres
class StartIsFencedTest(PostgresOrchestratorTestCase):
    async def test_a_restart_between_the_read_and_the_start_skips_the_old_entry(self):
        h = self.harness()
        task_id = await self.prepare(h, make_plan(node("a")))
        other = TaskService(self.new_database(), **gate_kwargs(TaskService))
        original = h.tasks.execute
        seen = []

        async def wrapped(tid, command, **kwargs):
            if command is TaskCommand.START and not seen:
                seen.append(1)
                await other.execute(tid, TaskCommand.CANCEL, actor=self.user)
                await other.execute(tid, TaskCommand.RESTART, actor=self.user)
            return await original(tid, command, **kwargs)

        h.tasks.execute = wrapped

        report = await h.orchestrator.run_once("w1")

        # The replacement (attempt 2) was not started under the old entry.
        self.assertEqual(report.outcome, Out.SKIPPED)
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(
            (snapshot.state, snapshot.attempt.number), (TaskState.QUEUED, 2)
        )
        (entry,) = await self.rows("SELECT status FROM queue_entries")
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM agent_dags"), 1
        )  # the plan only
        self.assertEqual((await self.store.get(task_id, 1)).epoch, 0)  # nobody drove it

    async def test_a_retry_between_the_read_and_the_start_skips_the_old_entry(self):
        h = self.harness()
        task_id = await self.prepare(h, make_plan(node("a")))
        other = TaskService(self.new_database(), **gate_kwargs(TaskService))
        original = h.tasks.execute
        seen = []

        async def wrapped(tid, command, **kwargs):
            if command is TaskCommand.START and not seen:
                seen.append(1)
                # Fail from queued and Retry: the run changes (retry_count 1).
                await other.execute(tid, TaskCommand.FAIL, actor=self.system)
                await other.execute(tid, TaskCommand.RETRY, actor=self.user)
            return await original(tid, command, **kwargs)

        h.tasks.execute = wrapped

        report = await h.orchestrator.run_once("w1")

        # A replaced run: skipped, not started.
        self.assertEqual(report.outcome, Out.SKIPPED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.QUEUED)


if __name__ == "__main__":
    unittest.main()
