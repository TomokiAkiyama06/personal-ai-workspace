"""The task state machine: every state x command pair, without a database."""

import unittest
import uuid

from paw_backend.tasks import (
    CONTROL_COMMANDS,
    TERMINAL_STATES,
    Actor,
    ActorKind,
    IllegalTransitionError,
    Interruption,
    InvalidCommandArgumentError,
    StaleAttemptError,
    StaleRunError,
    TaskCommand,
    TaskRun,
    TaskState,
    WaitReason,
    allowed_commands,
    interruption_of,
    plan_transition,
)

S = TaskState
C = TaskCommand

# Written state by state, from REQUIREMENTS.md, deliberately in the opposite
# orientation of ``domain.TRANSITIONS`` (which is keyed by command).
EXPECTED: dict[TaskState, dict[TaskCommand, TaskState]] = {
    S.QUEUED: {C.START: S.RUNNING, C.FAIL: S.FAILED, C.CANCEL: S.CANCELLED},
    S.RUNNING: {
        C.WAIT: S.WAITING,
        C.BEGIN_EVALUATION: S.EVALUATING,
        C.FAIL: S.FAILED,
        C.PAUSE: S.PAUSED,
        C.CANCEL: S.CANCELLED,
        C.STOP_NOW: S.CANCELLED,
    },
    S.WAITING: {
        C.UNBLOCK: S.RUNNING,
        C.FAIL: S.FAILED,
        C.CANCEL: S.CANCELLED,
        C.STOP_NOW: S.CANCELLED,
    },
    S.PAUSED: {C.RESUME: S.RUNNING, C.CANCEL: S.CANCELLED},
    S.EVALUATING: {
        C.COMPLETE: S.COMPLETED,
        C.FAIL: S.FAILED,
        C.CANCEL: S.CANCELLED,
        C.STOP_NOW: S.CANCELLED,
    },
    S.COMPLETED: {},
    S.FAILED: {C.RETRY: S.QUEUED, C.RESTART: S.QUEUED},
    S.CANCELLED: {C.RESTART: S.QUEUED},
}


def plan(state, command):
    reason = WaitReason.USER if command is C.WAIT else None
    return plan_transition(state, command, wait_reason=reason)


class EnumTest(unittest.TestCase):
    def test_states_are_exactly_the_eight_required_by_the_issue(self):
        self.assertEqual(
            {state.value for state in TaskState},
            {
                "queued",
                "running",
                "waiting",
                "paused",
                "evaluating",
                "completed",
                "failed",
                "cancelled",
            },
        )

    def test_wait_reasons_cover_user_approval_and_resource(self):
        self.assertEqual(
            {reason.value for reason in WaitReason}, {"user", "approval", "resource"}
        )

    def test_operator_controls_are_the_six_named_in_the_issue(self):
        self.assertEqual(
            {command.value for command in CONTROL_COMMANDS},
            {"pause", "resume", "cancel", "retry", "restart", "stop_now"},
        )

    def test_terminal_states(self):
        self.assertEqual(TERMINAL_STATES, {S.COMPLETED, S.FAILED, S.CANCELLED})


class TransitionMatrixTest(unittest.TestCase):
    def test_expected_table_covers_every_state(self):
        self.assertEqual(set(EXPECTED), set(TaskState))

    def test_every_state_and_command_pair_is_allowed_or_rejected_as_specified(self):
        checked = 0
        for state in TaskState:
            for command in TaskCommand:
                with self.subTest(state=state.value, command=command.value):
                    checked += 1
                    target = EXPECTED[state].get(command)
                    if target is None:
                        with self.assertRaises(IllegalTransitionError) as caught:
                            plan(state, command)
                        self.assertEqual(caught.exception.state, state.value)
                        self.assertEqual(caught.exception.command, command.value)
                    else:
                        result = plan(state, command)
                        self.assertEqual(result.target, target)
                        self.assertEqual(result.from_state, state)
        self.assertEqual(checked, 8 * 13)

    def test_allowed_commands_lists_exactly_the_accepted_commands(self):
        for state in TaskState:
            with self.subTest(state=state.value):
                self.assertEqual(allowed_commands(state), frozenset(EXPECTED[state]))

    def test_completed_accepts_nothing(self):
        self.assertEqual(allowed_commands(S.COMPLETED), frozenset())

    def test_failed_and_cancelled_accept_only_the_reopen_controls(self):
        self.assertEqual(allowed_commands(S.FAILED), {C.RETRY, C.RESTART})
        self.assertEqual(allowed_commands(S.CANCELLED), {C.RESTART})

    def test_create_is_never_a_transition_of_an_existing_task(self):
        for state in TaskState:
            with self.subTest(state=state.value):
                self.assertNotIn(C.CREATE, allowed_commands(state))

    def test_full_happy_path(self):
        state = S.QUEUED
        for command in (C.START, C.PAUSE, C.RESUME, C.BEGIN_EVALUATION, C.COMPLETE):
            state = plan(state, command).target
        self.assertEqual(state, S.COMPLETED)


class CommandSemanticsTest(unittest.TestCase):
    def test_cancel_is_graceful_and_stop_now_is_immediate(self):
        self.assertEqual(interruption_of(C.CANCEL), Interruption.GRACEFUL)
        self.assertEqual(interruption_of(C.STOP_NOW), Interruption.IMMEDIATE)
        self.assertEqual(plan(S.RUNNING, C.CANCEL).interruption, Interruption.GRACEFUL)
        self.assertEqual(
            plan(S.RUNNING, C.STOP_NOW).interruption, Interruption.IMMEDIATE
        )

    def test_pause_is_graceful_and_other_commands_do_not_interrupt(self):
        self.assertEqual(interruption_of(C.PAUSE), Interruption.GRACEFUL)
        for command in (
            C.START,
            C.RESUME,
            C.RETRY,
            C.RESTART,
            C.COMPLETE,
            C.FAIL,
            C.CREATE,
        ):
            with self.subTest(command=command.value):
                self.assertIsNone(interruption_of(command))

    def test_stop_now_reaches_the_same_state_as_cancel_but_covers_fewer_states(self):
        self.assertEqual(
            plan(S.RUNNING, C.STOP_NOW).target, plan(S.RUNNING, C.CANCEL).target
        )
        for state in (S.QUEUED, S.PAUSED):
            with self.subTest(state=state.value):
                self.assertIn(C.CANCEL, allowed_commands(state))
                self.assertNotIn(C.STOP_NOW, allowed_commands(state))

    def test_retry_and_restart_both_requeue_but_only_failed_can_retry(self):
        for command in (C.RETRY, C.RESTART):
            self.assertEqual(plan(S.FAILED, command).target, S.QUEUED)
        self.assertEqual(plan(S.CANCELLED, C.RESTART).target, S.QUEUED)
        with self.assertRaises(IllegalTransitionError):
            plan(S.CANCELLED, C.RETRY)

    def test_pause_only_applies_to_a_running_task(self):
        pausable = [state for state in TaskState if C.PAUSE in allowed_commands(state)]
        self.assertEqual(pausable, [S.RUNNING])

    def test_resume_only_leaves_paused(self):
        resumable = [
            state for state in TaskState if C.RESUME in allowed_commands(state)
        ]
        self.assertEqual(resumable, [S.PAUSED])


class WaitReasonTest(unittest.TestCase):
    def test_wait_needs_a_reason_and_carries_it_into_the_plan(self):
        for reason in WaitReason:
            with self.subTest(reason=reason.value):
                result = plan_transition(S.RUNNING, C.WAIT, wait_reason=reason)
                self.assertEqual(result.target, S.WAITING)
                self.assertEqual(result.wait_reason, reason)

    def test_wait_without_a_reason_is_rejected(self):
        with self.assertRaises(InvalidCommandArgumentError):
            plan_transition(S.RUNNING, C.WAIT)

    def test_other_commands_reject_a_wait_reason(self):
        with self.assertRaises(InvalidCommandArgumentError):
            plan_transition(S.RUNNING, C.PAUSE, wait_reason=WaitReason.USER)

    def test_leaving_waiting_clears_the_reason(self):
        for command in (C.UNBLOCK, C.CANCEL, C.FAIL, C.STOP_NOW):
            with self.subTest(command=command.value):
                self.assertIsNone(plan(S.WAITING, command).wait_reason)

    def test_illegal_transition_is_reported_before_a_bad_argument(self):
        with self.assertRaises(IllegalTransitionError):
            plan_transition(S.PAUSED, C.WAIT)


class ErrorAndActorTest(unittest.TestCase):
    def test_illegal_transition_message_names_only_the_state_and_command(self):
        with self.assertRaises(IllegalTransitionError) as caught:
            plan(S.COMPLETED, C.PAUSE)
        self.assertEqual(
            str(caught.exception), "Command pause is not allowed in state completed"
        )
        self.assertEqual(caught.exception.code, "illegal_transition")

    def test_user_actor_requires_an_id(self):
        with self.assertRaises(InvalidCommandArgumentError):
            Actor(ActorKind.USER)

    def test_system_and_policy_actors_must_not_carry_an_id(self):
        for kind in (ActorKind.SYSTEM, ActorKind.POLICY):
            with (
                self.subTest(kind=kind.value),
                self.assertRaises(InvalidCommandArgumentError),
            ):
                Actor(kind, uuid.uuid4())

    def test_actor_constructors(self):
        user_id = uuid.uuid4()
        self.assertEqual(Actor.user(user_id), Actor(ActorKind.USER, user_id))
        self.assertEqual(Actor.system(), Actor(ActorKind.SYSTEM))
        self.assertEqual(Actor.policy(), Actor(ActorKind.POLICY))


class TaskRunTest(unittest.TestCase):
    def test_a_run_is_the_attempt_and_the_retry_count(self):
        run = TaskRun(2, 3)
        self.assertEqual((run.attempt, run.retry_count), (2, 3))
        self.assertEqual(run, TaskRun(2, 3))
        # Two runs are the same only if both numbers are.
        self.assertNotEqual(run, TaskRun(2, 4))
        self.assertNotEqual(run, TaskRun(3, 3))
        self.assertEqual(TaskRun(1, 0), TaskRun(1, 0))
        self.assertEqual(len({TaskRun(1, 0), TaskRun(1, 0), TaskRun(1, 1)}), 2)

    def test_the_counters_are_bounded_by_their_integer_columns(self):
        largest = 2**31 - 1
        self.assertEqual(TaskRun(largest, largest).attempt, largest)
        self.assertEqual(TaskRun(1, 0).retry_count, 0)
        for attempt, retry_count in (
            (0, 0),  # an attempt starts at 1
            (-1, 0),
            (1, -1),  # a retry count starts at 0
            (largest + 1, 0),
            (1, largest + 1),
        ):
            with (
                self.subTest(attempt=attempt, retry_count=retry_count),
                self.assertRaises(InvalidCommandArgumentError),
            ):
                TaskRun(attempt, retry_count)

    def test_a_counter_must_be_an_integer(self):
        # ``bool`` is an ``int``; a float or text would be coerced by the driver.
        for bad in (True, False, 1.0, "1", "secret-value", None, b"1"):
            with (
                self.subTest(value=bad),
                self.assertRaises(InvalidCommandArgumentError) as caught,
            ):
                TaskRun(bad, 0)
            # The error names the field, never the value it was given.
            self.assertNotIn("secret-value", str(caught.exception))
            with (
                self.subTest(retry_count=bad),
                self.assertRaises(InvalidCommandArgumentError),
            ):
                TaskRun(1, bad)

    def test_a_stale_attempt_is_a_stale_run(self):
        # A worker that only asks "was I superseded?" catches ``StaleRunError``;
        # the attempt case keeps its own code and message.
        self.assertTrue(issubclass(StaleAttemptError, StaleRunError))
        self.assertEqual(
            (StaleRunError.code, str(StaleRunError())),
            ("stale_run", "The task has moved on to a newer run"),
        )
        self.assertEqual(
            (StaleAttemptError.code, str(StaleAttemptError())),
            ("stale_attempt", "The task has moved on to a newer attempt"),
        )


if __name__ == "__main__":
    unittest.main()
