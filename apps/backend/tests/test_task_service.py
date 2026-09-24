"""Task commands, steps, logs and history on a real PostgreSQL.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set (see test_postgres_integration).
"""

import logging
import unittest
import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from paw_backend.tasks import (
    ActorKind,
    EvaluationResult,
    IllegalTransitionError,
    Interruption,
    InvalidCommandArgumentError,
    LogLevel,
    PullRequestInfo,
    PullRequestState,
    ReviewState,
    ReviewStatus,
    StepStatus,
    TaskCommand,
    TaskConflictError,
    TaskNotFoundError,
    TaskService,
    TaskState,
    TaskStepError,
    WaitReason,
    WorktreeState,
)

from .task_support import PostgresTaskTestCase, requires_postgres
from .test_task_domain import EXPECTED

C = TaskCommand
S = TaskState
WORKTREE = WorktreeState("agent/task-1", "/srv/worktrees/task-1", "b" * 40)


@requires_postgres
class CreateTaskTest(PostgresTaskTestCase):
    async def test_new_task_is_queued_at_version_one_with_a_create_event(self):
        event = await self.service.create_task(
            project_id=self.project_id,
            created_by=self.user_id,
            title="Fix the parser",
            input={"prompt": "Make it faster", "files": ["a.py"]},
            starting_commit="a" * 40,
            agent="codex",
            model="model-x",
        )
        self.assertEqual(
            (event.command, event.from_state, event.to_state, event.task_version),
            (C.CREATE, None, S.QUEUED, 1),
        )
        self.assertEqual(
            (event.actor.kind, event.actor.id), (ActorKind.USER, self.user_id)
        )

        snapshot = await self.service.restore(event.task_id)
        self.assertEqual(snapshot.state, S.QUEUED)
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(snapshot.attempt.number, 1)
        self.assertEqual(snapshot.retry_count, 0)
        self.assertEqual(snapshot.title, "Fix the parser")
        self.assertEqual(
            snapshot.input, {"prompt": "Make it faster", "files": ["a.py"]}
        )
        self.assertEqual(snapshot.starting_commit, "a" * 40)
        self.assertEqual((snapshot.agent, snapshot.model), ("codex", "model-x"))
        self.assertEqual(
            (snapshot.project_id, snapshot.created_by), (self.project_id, self.user_id)
        )
        self.assertIsNone(snapshot.current_step)
        self.assertEqual(snapshot.recent_logs, ())
        self.assertEqual(snapshot.attempt.worktree, WorktreeState())
        self.assertEqual(snapshot.attempt.review, ReviewState())
        self.assertIsNone(snapshot.attempt.pull_request)

    async def test_invalid_creation_arguments_are_rejected_without_echoing_them(self):
        secret = "SECRET-MARKER"
        cases = {
            "blank title": {"title": "   "},
            "long title": {"title": secret * 40},
            "input that is not JSON": {"input": {"x": object()}},
            "oversized input": {"input": {"blob": secret * 40000}},
            "long commit": {"starting_commit": "c" * 65},
        }
        for name, overrides in cases.items():
            with (
                self.subTest(name),
                self.assertRaises(InvalidCommandArgumentError) as caught,
            ):
                await self.create_task(**overrides)
            self.assertNotIn(secret, str(caught.exception))

    async def test_title_at_the_limit_is_accepted(self):
        task_id = await self.create_task(title="t" * 200)
        self.assertEqual((await self.service.restore(task_id)).title, "t" * 200)


@requires_postgres
class TransitionTest(PostgresTaskTestCase):
    async def test_database_follows_the_transition_table_for_every_state_and_command(
        self,
    ):
        checked = 0
        for state in TaskState:
            for command in TaskCommand:
                with self.subTest(state=state.value, command=command.value):
                    checked += 1
                    task_id = await self.task_in_state(state)
                    before = await self.service.restore(task_id)
                    wait_reason = WaitReason.APPROVAL if command is C.WAIT else None
                    target = EXPECTED[state].get(command)
                    if target is None:
                        with self.assertRaises(IllegalTransitionError):
                            await self.service.execute(
                                task_id,
                                command,
                                actor=self.system,
                                wait_reason=wait_reason,
                            )
                        after = await self.service.restore(task_id)
                        self.assertEqual(after.state, state)
                        self.assertEqual(after.version, before.version)
                        self.assertEqual(after.last_event, before.last_event)
                    else:
                        event = await self.service.execute(
                            task_id, command, actor=self.system, wait_reason=wait_reason
                        )
                        self.assertEqual(
                            (event.from_state, event.to_state), (state, target)
                        )
                        after = await self.service.restore(task_id)
                        self.assertEqual(after.state, target)
                        self.assertEqual(after.version, before.version + 1)
                        self.assertEqual(after.last_event, event)
        self.assertEqual(checked, 8 * 13)

    async def test_completed_task_rejects_every_command(self):
        task_id = await self.task_in_state(S.COMPLETED)
        for command in TaskCommand:
            with (
                self.subTest(command=command.value),
                self.assertRaises(IllegalTransitionError),
            ):
                await self.service.execute(
                    task_id,
                    command,
                    actor=self.system,
                    wait_reason=WaitReason.USER if command is C.WAIT else None,
                )
        self.assertEqual((await self.service.restore(task_id)).state, S.COMPLETED)

    async def test_history_records_every_transition_with_who_and_what(self):
        task_id = await self.create_task()
        steps = [
            (C.START, self.system, None, None),
            (C.WAIT, self.system, WaitReason.APPROVAL, "needs merge approval"),
            (C.UNBLOCK, self.user, None, "approved"),
            (C.BEGIN_EVALUATION, self.system, None, None),
            (C.COMPLETE, self.system, None, None),
        ]
        for command, actor, wait_reason, reason in steps:
            await self.service.execute(
                task_id, command, actor=actor, wait_reason=wait_reason, reason=reason
            )

        events = await self.service.history(task_id)
        self.assertEqual(
            [event.command for event in events],
            [C.CREATE, C.START, C.WAIT, C.UNBLOCK, C.BEGIN_EVALUATION, C.COMPLETE],
        )
        self.assertEqual(
            [(event.from_state, event.to_state) for event in events],
            [
                (None, S.QUEUED),
                (S.QUEUED, S.RUNNING),
                (S.RUNNING, S.WAITING),
                (S.WAITING, S.RUNNING),
                (S.RUNNING, S.EVALUATING),
                (S.EVALUATING, S.COMPLETED),
            ],
        )
        self.assertEqual([event.task_version for event in events], [1, 2, 3, 4, 5, 6])
        self.assertEqual(
            [event.seq for event in events], sorted({event.seq for event in events})
        )
        self.assertEqual(
            [(event.actor.kind, event.actor.id) for event in events],
            [
                (ActorKind.USER, self.user_id),
                (ActorKind.SYSTEM, None),
                (ActorKind.SYSTEM, None),
                (ActorKind.USER, self.user_id),
                (ActorKind.SYSTEM, None),
                (ActorKind.SYSTEM, None),
            ],
        )
        self.assertEqual(
            [event.wait_reason for event in events],
            [None, None, WaitReason.APPROVAL, None, None, None],
        )
        self.assertEqual(events[2].reason, "needs merge approval")
        self.assertEqual(events[3].reason, "approved")
        self.assertTrue(all(event.attempt == 1 for event in events))
        self.assertEqual(
            [event.created_at for event in events],
            sorted(event.created_at for event in events),
        )

    async def test_history_can_resume_after_a_sequence_number(self):
        task_id = await self.task_in_state(S.RUNNING)
        events = await self.service.history(task_id)
        later = await self.service.history(task_id, after_seq=events[0].seq)
        self.assertEqual(later, events[1:])
        self.assertEqual(
            await self.service.history(task_id, after_seq=events[-1].seq), []
        )

    async def test_pause_and_resume_keep_the_step_worktree_and_logs(self):
        task_id = await self.task_in_state(S.RUNNING)
        await self.service.begin_step(task_id, "implement", attempt=1)
        await self.service.add_log(task_id, "editing parser.py", attempt=1)
        await self.service.update_attempt(task_id, attempt=1, worktree=WORKTREE)

        paused = await self.service.execute(
            task_id, C.PAUSE, actor=self.user, reason="lunch"
        )
        self.assertEqual(
            (paused.to_state, paused.interruption), (S.PAUSED, Interruption.GRACEFUL)
        )
        self.assertEqual(paused.step_name, "implement")
        during = await self.service.restore(task_id)

        await self.service.execute(task_id, C.RESUME, actor=self.user)
        after = await self.service.restore(task_id)
        self.assertEqual(after.state, S.RUNNING)
        self.assertEqual(after.attempt, during.attempt)
        self.assertEqual(after.attempt.worktree, WORKTREE)
        self.assertEqual(after.current_step, during.current_step)
        self.assertEqual(after.current_step.name, "implement")
        self.assertEqual(
            [log.message for log in after.recent_logs], ["editing parser.py"]
        )

    async def test_cancel_is_graceful_and_keeps_artifacts_and_the_running_step(self):
        task_id = await self.task_in_state(S.RUNNING)
        step = await self.service.begin_step(task_id, "implement", attempt=1)
        await self.service.update_attempt(task_id, attempt=1, worktree=WORKTREE)

        event = await self.service.execute(
            task_id, C.CANCEL, actor=self.user, reason="not needed"
        )
        self.assertEqual(
            (event.to_state, event.interruption), (S.CANCELLED, Interruption.GRACEFUL)
        )
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.attempt.worktree, WORKTREE)
        # The worker finishes its step itself; Cancel does not touch it.
        self.assertEqual(snapshot.current_step.status, StepStatus.RUNNING)
        finished = await self.service.finish_step(
            task_id, step.id, StepStatus.INTERRUPTED
        )
        self.assertEqual(finished.status, StepStatus.INTERRUPTED)

    async def test_stop_now_interrupts_the_step_immediately_and_logs_why(self):
        task_id = await self.task_in_state(S.RUNNING)
        await self.service.begin_step(task_id, "run-tests", attempt=1)
        await self.service.update_attempt(task_id, attempt=1, worktree=WORKTREE)

        event = await self.service.execute(
            task_id, C.STOP_NOW, actor=self.user, reason="agent loop"
        )
        self.assertEqual(
            (event.to_state, event.interruption), (S.CANCELLED, Interruption.IMMEDIATE)
        )
        self.assertEqual(event.step_name, "run-tests")
        self.assertEqual(event.reason, "agent loop")

        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.current_step.name, "run-tests")
        self.assertEqual(snapshot.current_step.status, StepStatus.INTERRUPTED)
        self.assertIsNotNone(snapshot.current_step.finished_at)
        self.assertEqual(len(snapshot.recent_logs), 1)
        self.assertEqual(snapshot.recent_logs[0].level, LogLevel.WARNING)
        self.assertEqual(
            snapshot.recent_logs[0].message,
            "Stop Now: interrupted step 'run-tests' (reason: agent loop)",
        )
        # Nothing is deleted: the worktree state is still there.
        self.assertEqual(snapshot.attempt.worktree, WORKTREE)

    async def test_stop_now_without_a_running_step_still_logs(self):
        task_id = await self.task_in_state(S.WAITING)
        await self.service.execute(task_id, C.STOP_NOW, actor=self.system)
        snapshot = await self.service.restore(task_id)
        self.assertIsNone(snapshot.current_step)
        self.assertEqual(
            [log.message for log in snapshot.recent_logs],
            ["Stop Now: no step was running"],
        )
        self.assertIsNone(snapshot.last_event.step_name)

    async def test_stop_now_after_the_step_finished_does_not_call_it_interrupted(self):
        for finished_as in (
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.INTERRUPTED,
        ):
            with self.subTest(step=finished_as.value):
                task_id = await self.task_in_state(S.RUNNING)
                step = await self.service.begin_step(task_id, "run-tests", attempt=1)
                await self.service.finish_step(task_id, step.id, finished_as)

                event = await self.service.execute(
                    task_id, C.STOP_NOW, actor=self.user, reason="just in case"
                )
                self.assertEqual(event.to_state, S.CANCELLED)
                self.assertIsNone(event.step_name)
                snapshot = await self.service.restore(task_id)
                # The finished step keeps the outcome its worker recorded.
                self.assertEqual(snapshot.current_step.status, finished_as)
                (log,) = snapshot.recent_logs
                self.assertEqual(
                    log.message, "Stop Now: no step was running (reason: just in case)"
                )
                self.assertNotIn("interrupted", log.message)

    async def test_fail_after_the_step_finished_names_no_step(self):
        task_id = await self.task_in_state(S.RUNNING)
        step = await self.service.begin_step(task_id, "run-tests", attempt=1)
        await self.service.finish_step(task_id, step.id, StepStatus.FAILED)
        event = await self.service.execute(task_id, C.FAIL, actor=self.system)
        self.assertIsNone(event.step_name)
        self.assertEqual(
            (await self.service.restore(task_id)).current_step.status, StepStatus.FAILED
        )

    async def test_other_commands_still_name_the_latest_step(self):
        task_id = await self.task_in_state(S.RUNNING)
        step = await self.service.begin_step(task_id, "run-tests", attempt=1)
        await self.service.finish_step(task_id, step.id, StepStatus.SUCCEEDED)
        event = await self.service.execute(task_id, C.PAUSE, actor=self.user)
        self.assertEqual(event.step_name, "run-tests")

    async def test_cancel_and_stop_now_events_are_distinguishable(self):
        cancelled = await self.task_in_state(S.RUNNING)
        stopped = await self.task_in_state(S.RUNNING)
        cancel_event = await self.service.execute(cancelled, C.CANCEL, actor=self.user)
        stop_event = await self.service.execute(stopped, C.STOP_NOW, actor=self.user)
        self.assertEqual(cancel_event.to_state, stop_event.to_state)
        self.assertNotEqual(cancel_event.interruption, stop_event.interruption)
        restored = await self.service.restore(stopped)
        self.assertEqual(restored.last_event.command, C.STOP_NOW)
        self.assertEqual(restored.last_event.interruption, Interruption.IMMEDIATE)

    async def test_fail_ends_the_running_step_as_failed(self):
        task_id = await self.task_in_state(S.RUNNING)
        await self.service.begin_step(task_id, "run-tests", attempt=1)
        event = await self.service.execute(
            task_id, C.FAIL, actor=self.system, reason="tests red"
        )
        self.assertEqual((event.to_state, event.step_name), (S.FAILED, "run-tests"))
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.current_step.status, StepStatus.FAILED)

    async def test_retry_reruns_the_failed_step_in_the_same_attempt(self):
        task_id = await self.create_task(agent="local", model="small")
        await self.service.execute(task_id, C.START, actor=self.system)
        await self.service.begin_step(task_id, "implement", attempt=1)
        await self.service.update_attempt(task_id, attempt=1, worktree=WORKTREE)
        await self.service.add_log(task_id, "compile error", attempt=1)
        await self.service.execute(task_id, C.FAIL, actor=self.system)

        event = await self.service.execute(
            task_id, C.RETRY, actor=self.user, agent="codex", reason="escalate"
        )
        self.assertEqual((event.from_state, event.to_state), (S.FAILED, S.QUEUED))
        self.assertEqual(event.step_name, "implement")
        self.assertEqual(event.attempt, 1)
        self.assertEqual(event.detail, {"agent": {"from": "local", "to": "codex"}})

        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.attempt.number, 1)
        self.assertEqual(snapshot.previous_attempts, ())
        self.assertEqual(snapshot.retry_count, 1)
        self.assertEqual((snapshot.agent, snapshot.model), ("codex", "small"))
        # Same branch / worktree / logs are reused.
        self.assertEqual(snapshot.attempt.worktree, WORKTREE)
        self.assertEqual(
            [log.message for log in snapshot.recent_logs], ["compile error"]
        )
        # The failed step stays as history; the next run starts a new step row.
        self.assertEqual(snapshot.current_step.status, StepStatus.FAILED)
        await self.service.execute(task_id, C.START, actor=self.system)
        rerun = await self.service.begin_step(task_id, "implement", attempt=1)
        self.assertEqual(rerun.sequence, 2)

    async def test_retry_history_counts_every_retry(self):
        task_id = await self.task_in_state(S.FAILED)
        for expected in (1, 2, 3):
            await self.service.execute(task_id, C.RETRY, actor=self.user)
            await self.service.execute(task_id, C.START, actor=self.system)
            await self.service.execute(task_id, C.FAIL, actor=self.system)
            self.assertEqual(
                (await self.service.restore(task_id)).retry_count, expected
            )
        events = await self.service.history(task_id)
        self.assertEqual([e.command for e in events].count(C.RETRY), 3)

    async def test_restart_starts_a_new_attempt_and_keeps_the_old_one_as_history(self):
        task_id = await self.create_task(
            starting_commit="a" * 40, input={"prompt": "p"}
        )
        await self.service.execute(task_id, C.START, actor=self.system)
        await self.service.begin_step(task_id, "implement", attempt=1)
        await self.service.add_log(task_id, "attempt one log", attempt=1)
        await self.service.update_attempt(
            task_id,
            attempt=1,
            worktree=WORKTREE,
            review=ReviewState(ReviewStatus.CHANGES_REQUESTED, EvaluationResult.FAILED),
            pull_request=PullRequestInfo(
                7, "https://example.test/pr/7", PullRequestState.OPEN
            ),
        )
        await self.service.execute(task_id, C.FAIL, actor=self.system)

        event = await self.service.execute(
            task_id, C.RESTART, actor=self.user, model="big"
        )
        self.assertEqual((event.from_state, event.to_state), (S.FAILED, S.QUEUED))
        self.assertEqual(event.attempt, 2)
        self.assertEqual(event.step_name, "implement")
        self.assertEqual(
            event.detail, {"model": {"from": None, "to": "big"}, "previous_attempt": 1}
        )

        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.attempt.number, 2)
        # The new attempt has its own (empty) branch / worktree / review / PR.
        self.assertEqual(snapshot.attempt.worktree, WorktreeState())
        self.assertEqual(snapshot.attempt.review, ReviewState())
        self.assertIsNone(snapshot.attempt.pull_request)
        self.assertIsNone(snapshot.current_step)
        self.assertEqual(snapshot.recent_logs, ())
        # Starting point and input are unchanged, and Restart is not a Retry.
        self.assertEqual(snapshot.starting_commit, "a" * 40)
        self.assertEqual(snapshot.input, {"prompt": "p"})
        self.assertEqual(snapshot.retry_count, 0)
        self.assertEqual(snapshot.model, "big")
        # The old attempt is kept intact.
        (old,) = snapshot.previous_attempts
        self.assertEqual(old.number, 1)
        self.assertEqual(old.worktree, WORKTREE)
        self.assertEqual(old.review.review_status, ReviewStatus.CHANGES_REQUESTED)
        self.assertEqual(old.pull_request.number, 7)
        old_logs = await self.scalar(
            "SELECT count(*) FROM task_logs WHERE task_id = :id AND attempt = 1",
            id=task_id,
        )
        self.assertEqual(old_logs, 1)

    async def test_cancelled_task_can_be_restarted(self):
        task_id = await self.task_in_state(S.CANCELLED)
        await self.service.execute(task_id, C.RESTART, actor=self.user)
        snapshot = await self.service.restore(task_id)
        self.assertEqual((snapshot.state, snapshot.attempt.number), (S.QUEUED, 2))
        self.assertEqual(len(snapshot.previous_attempts), 1)

    async def test_agent_or_model_is_only_accepted_by_retry_and_restart(self):
        task_id = await self.task_in_state(S.RUNNING)
        with self.assertRaises(InvalidCommandArgumentError):
            await self.service.execute(task_id, C.PAUSE, actor=self.user, agent="codex")
        self.assertEqual((await self.service.restore(task_id)).state, S.RUNNING)

    async def test_command_argument_errors_and_unknown_tasks(self):
        task_id = await self.task_in_state(S.RUNNING)
        secret = "SECRET-MARKER"
        with self.assertRaises(InvalidCommandArgumentError):
            await self.service.execute(task_id, C.WAIT, actor=self.system)
        with self.assertRaises(InvalidCommandArgumentError):
            await self.service.execute(
                task_id, C.PAUSE, actor=self.user, wait_reason=WaitReason.USER
            )
        with self.assertRaises(InvalidCommandArgumentError) as caught:
            await self.service.execute(
                task_id, C.PAUSE, actor=self.user, reason=secret * 100
            )
        self.assertNotIn(secret, str(caught.exception))
        await self.service.execute(task_id, C.PAUSE, actor=self.user, reason="r" * 500)
        with self.assertRaises(TaskNotFoundError):
            await self.service.execute(uuid.uuid4(), C.PAUSE, actor=self.user)
        with self.assertRaises(TaskNotFoundError):
            await self.service.restore(uuid.uuid4())
        with self.assertRaises(TaskNotFoundError):
            await self.service.history(uuid.uuid4())

    async def test_illegal_command_does_not_disturb_step_or_history(self):
        task_id = await self.task_in_state(S.RUNNING)
        await self.service.begin_step(task_id, "implement", attempt=1)
        before = await self.service.history(task_id)
        with self.assertRaises(IllegalTransitionError):
            await self.service.execute(task_id, C.RESUME, actor=self.user)
        self.assertEqual(await self.service.history(task_id), before)
        self.assertEqual(
            (await self.service.restore(task_id)).current_step.status,
            StepStatus.RUNNING,
        )


@requires_postgres
class StepAndLogTest(PostgresTaskTestCase):
    async def test_steps_are_numbered_and_only_one_runs_at_a_time(self):
        task_id = await self.task_in_state(S.RUNNING)
        first = await self.service.begin_step(task_id, "plan", attempt=1)
        self.assertEqual((first.sequence, first.status), (1, StepStatus.RUNNING))
        with self.assertRaises(TaskStepError):
            await self.service.begin_step(task_id, "implement", attempt=1)
        done = await self.service.finish_step(task_id, first.id, StepStatus.SUCCEEDED)
        self.assertEqual((done.name, done.status), ("plan", StepStatus.SUCCEEDED))
        self.assertIsNotNone(done.finished_at)
        second = await self.service.begin_step(task_id, "implement", attempt=1)
        self.assertEqual(second.sequence, 2)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(
            (snapshot.current_step.name, snapshot.current_step.sequence),
            ("implement", 2),
        )

    async def test_a_step_may_start_while_running_waiting_or_evaluating_only(self):
        for state in TaskState:
            with self.subTest(state=state.value):
                task_id = await self.task_in_state(state)
                if state in (S.RUNNING, S.WAITING, S.EVALUATING):
                    step = await self.service.begin_step(task_id, "work", attempt=1)
                    self.assertEqual(step.status, StepStatus.RUNNING)
                else:
                    with self.assertRaises(TaskStepError):
                        await self.service.begin_step(task_id, "work", attempt=1)
                    self.assertIsNone(
                        (await self.service.restore(task_id)).current_step
                    )

    async def test_finishing_needs_a_running_step_and_a_final_status(self):
        task_id = await self.task_in_state(S.RUNNING)
        with self.assertRaises(TaskStepError):
            await self.service.finish_step(task_id, 999_999_999, StepStatus.SUCCEEDED)
        step = await self.service.begin_step(task_id, "plan", attempt=1)
        with self.assertRaises(InvalidCommandArgumentError):
            await self.service.finish_step(task_id, step.id, StepStatus.RUNNING)
        await self.service.finish_step(task_id, step.id, StepStatus.FAILED)
        with self.assertRaises(TaskStepError):
            await self.service.finish_step(task_id, step.id, StepStatus.FAILED)

    async def test_a_step_of_another_task_cannot_be_finished_through_this_one(self):
        mine = await self.task_in_state(S.RUNNING)
        other = await self.task_in_state(S.RUNNING)
        foreign = await self.service.begin_step(other, "plan", attempt=1)
        with self.assertRaises(TaskStepError):
            await self.service.finish_step(mine, foreign.id, StepStatus.SUCCEEDED)
        current = (await self.service.restore(other)).current_step
        self.assertEqual(current.status, StepStatus.RUNNING)

    async def test_step_name_is_validated(self):
        task_id = await self.task_in_state(S.RUNNING)
        for name in ("", "  ", "x" * 101):
            with (
                self.subTest(length=len(name)),
                self.assertRaises(InvalidCommandArgumentError),
            ):
                await self.service.begin_step(task_id, name, attempt=1)

    async def test_step_and_log_calls_reject_unknown_tasks(self):
        unknown = uuid.uuid4()
        with self.assertRaises(TaskNotFoundError):
            await self.service.begin_step(unknown, "plan", attempt=1)
        with self.assertRaises(TaskNotFoundError):
            await self.service.finish_step(unknown, 1, StepStatus.SUCCEEDED)
        with self.assertRaises(TaskNotFoundError):
            await self.service.add_log(unknown, "hello", attempt=1)
        with self.assertRaises(TaskNotFoundError):
            await self.service.update_attempt(unknown, attempt=1, worktree=WORKTREE)

    async def test_recent_logs_are_the_latest_n_in_order(self):
        task_id = await self.task_in_state(S.RUNNING)
        for number in range(1, 6):
            await self.service.add_log(task_id, f"line {number}", attempt=1)
        snapshot = await self.service.restore(task_id, log_limit=3)
        self.assertEqual(
            [log.message for log in snapshot.recent_logs],
            ["line 3", "line 4", "line 5"],
        )
        self.assertEqual(
            [log.seq for log in snapshot.recent_logs],
            sorted(log.seq for log in snapshot.recent_logs),
        )
        self.assertEqual(
            (await self.service.restore(task_id, log_limit=0)).recent_logs, ()
        )
        self.assertEqual(len((await self.service.restore(task_id)).recent_logs), 5)

    async def test_log_limit_is_bounded(self):
        task_id = await self.task_in_state(S.RUNNING)
        for limit in (-1, 1001):
            with (
                self.subTest(limit=limit),
                self.assertRaises(InvalidCommandArgumentError),
            ):
                await self.service.restore(task_id, log_limit=limit)

    async def test_logs_are_accepted_in_every_state_and_keep_their_level(self):
        for state in TaskState:
            with self.subTest(state=state.value):
                task_id = await self.task_in_state(state)
                entry = await self.service.add_log(
                    task_id, "cleanup", attempt=1, level=LogLevel.ERROR
                )
                self.assertEqual((entry.level, entry.attempt), (LogLevel.ERROR, 1))
                snapshot = await self.service.restore(task_id)
                self.assertEqual(snapshot.recent_logs, (entry,))

    async def test_a_long_log_line_is_truncated_at_the_limit(self):
        task_id = await self.task_in_state(S.RUNNING)
        exact = await self.service.add_log(task_id, "a" * 8000, attempt=1)
        long = await self.service.add_log(task_id, "b" * 9000, attempt=1)
        self.assertEqual(len(exact.message), 8000)
        self.assertEqual(len(long.message), 8000)
        self.assertTrue(long.message.endswith("...[truncated]"))

    async def test_attempt_state_updates_replace_only_the_given_groups(self):
        task_id = await self.task_in_state(S.RUNNING)
        await self.service.update_attempt(task_id, attempt=1, worktree=WORKTREE)
        review = ReviewState(ReviewStatus.APPROVED, EvaluationResult.PASSED)
        result = await self.service.update_attempt(task_id, attempt=1, review=review)
        self.assertEqual((result.worktree, result.review), (WORKTREE, review))
        self.assertIsNone(result.pull_request)
        pull_request = PullRequestInfo(
            3, "https://example.test/pr/3", PullRequestState.MERGED
        )
        # A pull request can change after the task is finished.
        await self.service.execute(task_id, C.BEGIN_EVALUATION, actor=self.system)
        await self.service.execute(task_id, C.COMPLETE, actor=self.system)
        await self.service.update_attempt(task_id, attempt=1, pull_request=pull_request)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(
            (
                snapshot.attempt.worktree,
                snapshot.attempt.review,
                snapshot.attempt.pull_request,
            ),
            (WORKTREE, review, pull_request),
        )


@requires_postgres
class HistoryAndListenerTest(PostgresTaskTestCase):
    async def test_event_history_cannot_be_updated_or_deleted(self):
        task_id = await self.task_in_state(S.RUNNING)
        before = await self.service.history(task_id)
        for statement in (
            "UPDATE task_events SET reason = 'edited' WHERE task_id = :id",
            "DELETE FROM task_events WHERE task_id = :id",
        ):
            with self.subTest(statement=statement):
                async with self.database.engine.connect() as connection:
                    with self.assertRaises(DBAPIError) as caught:
                        await connection.execute(text(statement), {"id": task_id})
                self.assertIn("append-only", str(caught.exception.orig))
        self.assertEqual(await self.service.history(task_id), before)

    async def test_listener_receives_each_committed_event_after_the_commit(self):
        received = []
        seen_in_database = []

        async def listener(event):
            received.append(event)
            seen_in_database.append(await self.service.history(event.task_id))

        service = TaskService(self.database, listeners=[listener])
        task_id = await self.create_task(service)
        await service.execute(task_id, C.START, actor=self.system)
        await service.execute(task_id, C.PAUSE, actor=self.user, reason="break")

        self.assertEqual(
            [event.command for event in received], [C.CREATE, C.START, C.PAUSE]
        )
        self.assertEqual(received, await service.history(task_id))
        # When a listener runs, its own event is already durable.
        self.assertEqual([len(history) for history in seen_in_database], [1, 2, 3])

    async def test_listener_is_not_called_for_rejected_or_conflicting_commands(self):
        received = []

        async def listener(event):
            received.append(event.command)

        service = TaskService(self.database, listeners=[listener])
        task_id = await self.create_task(service)
        with self.assertRaises(IllegalTransitionError):
            await service.execute(task_id, C.PAUSE, actor=self.user)
        with self.assertRaises(TaskConflictError):
            await service.execute(
                task_id, C.START, actor=self.system, expected_version=9
            )
        self.assertEqual(received, [C.CREATE])

    async def test_failing_listener_does_not_undo_or_fail_the_command(self):
        async def broken(event):
            raise RuntimeError("password=hunter2")

        service = TaskService(self.database, listeners=[broken])
        with self.assertLogs(
            "paw_backend.tasks.service", level=logging.WARNING
        ) as logs:
            task_id = await self.create_task(service)
            await service.execute(task_id, C.START, actor=self.system)
        self.assertEqual((await self.service.restore(task_id)).state, S.RUNNING)
        # Only the exception type is logged, never its message.
        self.assertEqual(len(logs.records), 2)
        for line in logs.output:
            self.assertIn("RuntimeError", line)
            self.assertNotIn("hunter2", line)


if __name__ == "__main__":
    unittest.main()
