"""Interleavings that the task row lock and the run binding must get right.

Every test controls the order of two real transactions on real PostgreSQL: a
statement is only started once the previous one is provably blocked on a lock
(``wait_for_lock_waiters``), so the interleaving is the same on every run.
Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import unittest

from sqlalchemy import text

from paw_backend.tasks import (
    EvaluationResult,
    InvalidCommandArgumentError,
    PullRequestInfo,
    PullRequestState,
    ReviewState,
    ReviewStatus,
    StaleAttemptError,
    StaleRunError,
    StepStatus,
    TaskCommand,
    TaskRun,
    TaskService,
    TaskState,
    TaskStepError,
    ToolInvocationStatus,
    WorktreeState,
)

from .gate_support import ALWAYS_ACTIVE
from .task_support import (
    FIRST_RUN,
    PostgresTaskTestCase,
    command_reason,
    requires_postgres,
)

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
                    stepper = TaskService(
                        self.new_database(), project_gate=ALWAYS_ACTIVE
                    )
                    ender = TaskService(self.new_database(), project_gate=ALWAYS_ACTIVE)

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
                            stepper.begin_step(task_id, "work", run=FIRST_RUN)
                        )
                        await self.wait_for_lock_waiters(1)
                        end = asyncio.create_task(
                            ender.execute(
                                task_id,
                                command,
                                actor=self.system,
                                reason=command_reason(command),
                            )
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
                        task_id, "before", run=FIRST_RUN
                    )
                    await self.service.finish_step(
                        task_id, finished.id, StepStatus.SUCCEEDED
                    )
                    ender = TaskService(self.new_database(), project_gate=ALWAYS_ACTIVE)
                    stepper = TaskService(
                        self.new_database(), project_gate=ALWAYS_ACTIVE
                    )

                    async with self.database.engine.connect() as blocker:
                        # The command takes the task lock, then waits for the row
                        # of the latest step, which the blocker holds.
                        await blocker.execute(
                            text("SELECT 1 FROM task_steps WHERE id = :s FOR UPDATE"),
                            {"s": finished.id},
                        )
                        end = asyncio.create_task(
                            ender.execute(
                                task_id,
                                command,
                                actor=self.system,
                                reason=command_reason(command),
                            )
                        )
                        await self.wait_for_lock_waiters(1)
                        begin = asyncio.create_task(
                            stepper.begin_step(task_id, "late", run=FIRST_RUN)
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
        step = await self.service.begin_step(task_id, "work", run=FIRST_RUN)
        ender = TaskService(self.new_database(), project_gate=ALWAYS_ACTIVE)
        tooler = TaskService(self.new_database(), project_gate=ALWAYS_ACTIVE)

        async with self.database.engine.connect() as blocker:
            await blocker.execute(
                text("SELECT 1 FROM task_steps WHERE id = :s FOR UPDATE"),
                {"s": step.id},
            )
            end = asyncio.create_task(
                ender.execute(task_id, C.STOP_NOW, actor=self.user, reason="agent loop")
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
        old_step = await self.service.begin_step(task_id, "old", run=FIRST_RUN)
        await self.service.add_log(task_id, "attempt one, before", run=FIRST_RUN)
        await self.service.execute(task_id, C.CANCEL, actor=self.user)
        await self.service.execute(task_id, C.RESTART, actor=self.user)
        await self.service.execute(task_id, C.START, actor=self.system)
        new_step = await self.service.begin_step(task_id, "new", run=TaskRun(2, 0))
        return task_id, old_step, new_step

    async def test_restart_closes_the_step_a_graceful_cancel_left_running(self):
        task_id = await self.task_in_state(S.RUNNING)
        old_step = await self.service.begin_step(task_id, "old", run=FIRST_RUN)
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
            self.service.add_log(task_id, "late line", run=FIRST_RUN),
            self.service.begin_step(task_id, "late", run=FIRST_RUN),
            self.service.update_attempt(
                task_id,
                run=FIRST_RUN,
                worktree=WorktreeState("stale", "/stale", "c" * 40),
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
        entry = await self.service.add_log(
            task_id, "from attempt two", run=TaskRun(2, 0)
        )
        self.assertEqual(entry.attempt, 2)
        # An attempt that never existed is refused just like a superseded one.
        with self.assertRaises(StaleAttemptError):
            await self.service.add_log(task_id, "from the future", run=TaskRun(3, 0))

    async def test_within_one_attempt_a_step_id_stops_an_old_worker_after_a_retry(self):
        task_id = await self.task_in_state(S.RUNNING)
        first = await self.service.begin_step(task_id, "implement", run=FIRST_RUN)
        await self.service.execute(task_id, C.FAIL, actor=self.system)
        await self.service.execute(task_id, C.RETRY, actor=self.user)
        await self.service.execute(task_id, C.START, actor=self.system)
        second = await self.service.begin_step(task_id, "implement", run=TaskRun(1, 1))

        # The worker of the failed run reports late; its step is long closed.
        with self.assertRaises(TaskStepError):
            await self.service.finish_step(task_id, first.id, StepStatus.SUCCEEDED)
        current = (await self.service.restore(task_id)).current_step
        self.assertEqual((current.id, current.status), (second.id, StepStatus.RUNNING))

    async def test_a_restart_racing_a_stale_log_keeps_the_line_in_its_own_attempt(self):
        task_id = await self.task_in_state(S.CANCELLED)
        writer = TaskService(self.new_database(), project_gate=ALWAYS_ACTIVE)
        restarter = TaskService(self.new_database(), project_gate=ALWAYS_ACTIVE)

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
                entry = await writer.add_log(task_id, "last words", run=FIRST_RUN)
            await blocker.rollback()
        await restart
        self.assertEqual(entry.attempt, 1)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.attempt.number, 2)
        self.assertEqual(snapshot.recent_logs, ())


@requires_postgres
class RetriedRunTest(PostgresTaskTestCase):
    """Retry runs the same attempt again: only the run tells the two workers apart."""

    NEW_WORKTREE = WorktreeState("feature", "/work/new", "a" * 40)
    NEW_REVIEW = ReviewState(ReviewStatus.IN_REVIEW, EvaluationResult.NOT_RUN)
    NEW_PULL_REQUEST = PullRequestInfo(
        7, "https://example.test/pr/7", PullRequestState.DRAFT
    )

    async def failed_and_retried(self):
        """Run (1, 0) fails mid-step with a tool call in flight; the user retries;
        run (1, 1) is running its own step, tool call, log line and attempt state."""
        task_id = await self.task_in_state(S.RUNNING)
        old_run = TaskRun(1, 0)
        old_step = await self.service.begin_step(task_id, "implement", run=old_run)
        old_call = await self.service.begin_tool_invocation(
            task_id, step_id=old_step.id, tool_name="shell"
        )
        await self.service.execute(task_id, C.FAIL, actor=self.system)
        await self.service.execute(task_id, C.RETRY, actor=self.user)
        started = await self.service.execute(task_id, C.START, actor=self.system)
        new_run = started.run
        self.assertEqual((old_run, new_run), (TaskRun(1, 0), TaskRun(1, 1)))
        new_step = await self.service.begin_step(task_id, "implement", run=new_run)
        new_call = await self.service.begin_tool_invocation(
            task_id, step_id=new_step.id, tool_name="shell"
        )
        await self.service.add_log(task_id, "new run line", run=new_run)
        await self.service.update_attempt(
            task_id,
            run=new_run,
            worktree=self.NEW_WORKTREE,
            review=self.NEW_REVIEW,
            pull_request=self.NEW_PULL_REQUEST,
        )
        return task_id, old_run, old_step, old_call, new_run, new_step, new_call

    async def row_counts(self, task_id) -> tuple[int, int, int]:
        return tuple(
            [
                await self.scalar(
                    f"SELECT count(*) FROM {table} WHERE task_id = :i", i=task_id
                )
                for table in ("task_steps", "task_tool_invocations", "task_logs")
            ]
        )

    async def test_a_delayed_worker_of_the_failed_run_cannot_touch_the_new_run(self):
        (
            task_id,
            old_run,
            old_step,
            old_call,
            new_run,
            new_step,
            new_call,
        ) = await self.failed_and_retried()
        before = await self.service.restore(task_id)
        counts = await self.row_counts(task_id)
        self.assertEqual(before.attempt.worktree, self.NEW_WORKTREE)

        # What it writes about the attempt names its run: Retry left the attempt
        # number as it was, so the run's retry count is all that tells it apart.
        delayed = {
            "worktree": lambda: self.service.update_attempt(
                task_id,
                run=old_run,
                worktree=WorktreeState("stale", "/stale", "c" * 40),
            ),
            "review and evaluation": lambda: self.service.update_attempt(
                task_id,
                run=old_run,
                review=ReviewState(ReviewStatus.APPROVED, EvaluationResult.PASSED),
            ),
            "pull request": lambda: self.service.update_attempt(
                task_id,
                run=old_run,
                pull_request=PullRequestInfo(
                    99, "https://example.test/pr/99", PullRequestState.MERGED
                ),
            ),
            "log": lambda: self.service.add_log(task_id, "late line", run=old_run),
            "step": lambda: self.service.begin_step(task_id, "late", run=old_run),
        }
        for label, call in delayed.items():
            with self.subTest(write=label):
                with self.assertRaises(StaleRunError) as caught:
                    await call()
                # The attempt is the current one, so this is not a stale attempt.
                self.assertIs(type(caught.exception), StaleRunError)
                self.assertEqual(caught.exception.code, "stale_run")
                self.assertEqual(
                    str(caught.exception), "The task has moved on to a newer run"
                )

        # What it writes about a step or a tool call names that step or call, and
        # the ones the failed run left behind are closed for good.
        with self.assertRaises(TaskStepError):
            await self.service.finish_step(task_id, old_step.id, StepStatus.SUCCEEDED)
        with self.assertRaises(TaskStepError):
            await self.service.begin_tool_invocation(
                task_id, step_id=old_step.id, tool_name="late-tool"
            )
        with self.assertRaises(TaskStepError):
            await self.service.finish_tool_invocation(
                task_id, old_call.id, ToolInvocationStatus.SUCCEEDED
            )
        self.assertEqual(
            await self.scalar(
                "SELECT status FROM task_tool_invocations WHERE id = :i", i=old_call.id
            ),
            "interrupted",
        )

        # Nothing was written: not the new run's state, not a step, call or line.
        after = await self.service.restore(task_id)
        self.assertEqual(after, before)
        self.assertEqual(await self.row_counts(task_id), counts)
        self.assertEqual(after.attempt.worktree, self.NEW_WORKTREE)
        self.assertEqual(after.attempt.review, self.NEW_REVIEW)
        self.assertEqual(after.attempt.pull_request, self.NEW_PULL_REQUEST)
        self.assertEqual(after.current_step.id, new_step.id)
        self.assertEqual(after.current_step.status, StepStatus.RUNNING)
        self.assertEqual(
            [(call.id, call.status) for call in after.tool_invocations],
            [(new_call.id, ToolInvocationStatus.STARTED)],
        )
        self.assertEqual([log.message for log in after.recent_logs], ["new run line"])

    async def test_the_current_run_is_not_affected_and_still_writes(self):
        (
            task_id,
            _old,
            _step,
            _call,
            new_run,
            new_step,
            new_call,
        ) = await self.failed_and_retried()
        review = ReviewState(ReviewStatus.APPROVED, EvaluationResult.PASSED)
        result = await self.service.update_attempt(task_id, run=new_run, review=review)
        self.assertEqual(
            (result.worktree, result.review, result.pull_request),
            (self.NEW_WORKTREE, review, self.NEW_PULL_REQUEST),
        )
        entry = await self.service.add_log(task_id, "still mine", run=new_run)
        self.assertEqual(entry.run, new_run)
        finished_call = await self.service.finish_tool_invocation(
            task_id, new_call.id, ToolInvocationStatus.SUCCEEDED
        )
        self.assertEqual(finished_call.status, ToolInvocationStatus.SUCCEEDED)
        finished = await self.service.finish_step(
            task_id, new_step.id, StepStatus.SUCCEEDED
        )
        self.assertEqual(finished.status, StepStatus.SUCCEEDED)

    async def test_only_the_exact_run_is_current(self):
        (
            task_id,
            _old,
            _step,
            _call,
            new_run,
            _new_step,
            _new_call,
        ) = await self.failed_and_retried()
        self.assertEqual(new_run, TaskRun(1, 1))
        # A retry count from the future is no more current than one from the past.
        with self.assertRaises(StaleRunError) as caught:
            await self.service.add_log(task_id, "next retry", run=TaskRun(1, 2))
        self.assertIs(type(caught.exception), StaleRunError)
        # A different attempt is a stale attempt (which is a stale run) whatever
        # the retry count is, checked first.
        for run in (TaskRun(2, 1), TaskRun(2, 0), TaskRun(2, 2), TaskRun(3, 1)):
            with self.subTest(run=run), self.assertRaises(StaleAttemptError) as caught:
                await self.service.update_attempt(
                    task_id, run=run, worktree=WorktreeState("stale", "/s", "d" * 40)
                )
        self.assertTrue(issubclass(StaleAttemptError, StaleRunError))
        self.assertEqual(
            (await self.service.restore(task_id)).attempt.worktree, self.NEW_WORKTREE
        )

    async def test_a_restart_after_a_retry_replaces_both_runs_of_the_old_attempt(self):
        task_id, old_run, _s, _c, new_run, _ns, _nc = await self.failed_and_retried()
        await self.service.execute(task_id, C.FAIL, actor=self.system)
        restarted = await self.service.execute(task_id, C.RESTART, actor=self.user)
        # Restart keeps the retry count, so the run differs by the attempt.
        self.assertEqual(restarted.run, TaskRun(2, 1))
        for run in (old_run, new_run):
            with self.subTest(run=run), self.assertRaises(StaleAttemptError):
                await self.service.add_log(task_id, "late", run=run)

    async def test_a_bare_attempt_number_is_not_a_run(self):
        task_id = await self.task_in_state(S.RUNNING)
        for bad in (1, (1, 0), None, "1"):
            calls = {
                "begin_step": lambda bad=bad: self.service.begin_step(
                    task_id, "work", run=bad
                ),
                "add_log": lambda bad=bad: self.service.add_log(
                    task_id, "line", run=bad
                ),
                "update_attempt": lambda bad=bad: self.service.update_attempt(
                    task_id, run=bad, worktree=WorktreeState("b", "/p", "e" * 40)
                ),
            }
            for label, call in calls.items():
                with self.subTest(method=label, run=bad):
                    with self.assertRaises(InvalidCommandArgumentError) as caught:
                        await call()
                    self.assertEqual(
                        str(caught.exception),
                        "run must be a TaskRun (the attempt and the retry count)",
                    )
        # The old keyword is gone: a worker cannot pass just the attempt.
        with self.assertRaises(TypeError):
            await self.service.add_log(task_id, "line", attempt=1)
        self.assertEqual(await self.row_counts(task_id), (0, 0, 0))
        self.assertEqual(
            (await self.service.restore(task_id)).attempt.worktree, WorktreeState()
        )

    async def test_a_stale_write_that_waits_for_the_retry_is_refused_after_it(self):
        task_id = await self.task_in_state(S.FAILED)
        retrier = TaskService(self.new_database(), project_gate=ALWAYS_ACTIVE)
        worker = TaskService(self.new_database(), project_gate=ALWAYS_ACTIVE)
        old_run = TaskRun(1, 0)

        async with self.database.engine.connect() as blocker:
            # Hold the task row: the Retry, then the two stale writes of the run it
            # replaces queue up behind it, in this order (each one only once the
            # previous one is provably waiting).
            await blocker.execute(
                text("SELECT 1 FROM tasks WHERE id = :i FOR NO KEY UPDATE"),
                {"i": task_id},
            )
            retry = asyncio.create_task(
                retrier.execute(task_id, C.RETRY, actor=self.user)
            )
            await self.wait_for_lock_waiters(1)
            late_step = asyncio.create_task(
                worker.begin_step(task_id, "late", run=old_run)
            )
            await self.wait_for_lock_waiters(2)
            late_state = asyncio.create_task(
                worker.update_attempt(
                    task_id,
                    run=old_run,
                    worktree=WorktreeState("stale", "/s", "c" * 40),
                )
            )
            await self.wait_for_lock_waiters(3)
            await blocker.rollback()
        async with asyncio.timeout(30):
            retried = await retry
            # The run is read after the lock is taken: the Retry has committed, so
            # the delayed worker is stale (not "a step cannot start now").
            with self.assertRaises(StaleRunError):
                await late_step
            with self.assertRaises(StaleRunError):
                await late_state
        self.assertEqual(retried.run, TaskRun(1, 1))
        snapshot = await self.service.restore(task_id)
        self.assertEqual((snapshot.state, snapshot.run), (S.QUEUED, TaskRun(1, 1)))
        self.assertEqual(snapshot.attempt.worktree, WorktreeState())
        self.assertIsNone(snapshot.current_step)

    async def test_a_retry_racing_a_late_log_keeps_the_line_with_its_own_run(self):
        task_id = await self.task_in_state(S.FAILED)
        writer = TaskService(self.new_database(), project_gate=ALWAYS_ACTIVE)
        retrier = TaskService(self.new_database(), project_gate=ALWAYS_ACTIVE)

        async with self.database.engine.connect() as blocker:
            # The Retry is in flight but uncommitted, so run (1, 0) is still the
            # current run. NO KEY UPDATE does not block the log's foreign key check.
            await blocker.execute(
                text("SELECT 1 FROM tasks WHERE id = :i FOR NO KEY UPDATE"),
                {"i": task_id},
            )
            retry = asyncio.create_task(
                retrier.execute(task_id, C.RETRY, actor=self.user)
            )
            await self.wait_for_lock_waiters(1)
            async with asyncio.timeout(10):
                entry = await writer.add_log(task_id, "last words", run=TaskRun(1, 0))
            await blocker.rollback()
        await retry

        self.assertEqual((entry.run, entry.message), (TaskRun(1, 0), "last words"))
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.run, TaskRun(1, 1))
        # Retry continues the attempt's log; the line says which run wrote it.
        (line,) = snapshot.recent_logs
        self.assertEqual((line.message, line.run), ("last words", TaskRun(1, 0)))
        self.assertEqual(
            await self.scalar(
                "SELECT retry_count FROM task_logs WHERE task_id = :i", i=task_id
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
