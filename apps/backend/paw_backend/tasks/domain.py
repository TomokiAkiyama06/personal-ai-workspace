"""Pure domain rules of the Agent Task lifecycle (no database, no I/O).

REQUIREMENTS.md ("Agent Task lifecycle", "Task pause / cancel / retry /
restart") defines the states and the operator controls. This module turns
them into one explicit transition table so that every state x command pair has
exactly one answer, and so that the persistence layer cannot invent one.

Commands are of two kinds:

* the six operator controls (``CONTROL_COMMANDS``): Pause, Resume, Cancel,
  Retry, Restart and Stop Now;
* lifecycle events that the orchestrator / a worker / a policy reports as the
  work progresses: Start, Wait, Unblock, Begin evaluation, Complete, Fail.

Which commands each state accepts::

    state        accepts
    -----------  ----------------------------------------------------------
    queued       start, fail, cancel
    running      wait, begin_evaluation, fail, pause, cancel, stop_now
    waiting      unblock, fail, cancel, stop_now
    paused       resume, cancel
    evaluating   complete, fail, cancel, stop_now
    completed    (nothing)
    failed       retry, restart
    cancelled    restart
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from paw_backend.tasks.errors import IllegalTransitionError, InvalidCommandArgumentError


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    # Waiting for User / Approval / Resource; see ``WaitReason``.
    WAITING = "waiting"
    PAUSED = "paused"
    # Evaluating / Reviewing (Test, Evaluator, Codex, Claude, ...).
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WaitReason(StrEnum):
    """Why a task is ``waiting``; set if and only if the state is WAITING."""

    USER = "user"  # a decision or more information
    APPROVAL = "approval"  # Merge / Delete / ACL / permission change
    RESOURCE = "resource"  # GPU / cloud quota / repository lock


class TaskCommand(StrEnum):
    # Not a transition of an existing task: ``TaskService.create_task`` records
    # it as the first history event. Every state rejects it.
    CREATE = "create"

    # Lifecycle events reported by the orchestrator, a worker or a policy.
    START = "start"
    WAIT = "wait"
    UNBLOCK = "unblock"
    BEGIN_EVALUATION = "begin_evaluation"
    COMPLETE = "complete"
    FAIL = "fail"

    # Operator controls.
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    RETRY = "retry"
    RESTART = "restart"
    STOP_NOW = "stop_now"


CONTROL_COMMANDS = frozenset(
    {
        TaskCommand.PAUSE,
        TaskCommand.RESUME,
        TaskCommand.CANCEL,
        TaskCommand.RETRY,
        TaskCommand.RESTART,
        TaskCommand.STOP_NOW,
    }
)

# States in which no work happens. Completed accepts nothing; Failed and
# Cancelled accept only Retry / Restart (the explicit re-open rules below).
TERMINAL_STATES = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
)


class Interruption(StrEnum):
    """How hard a command stops the running work."""

    # Stop at the next safe point: finish the current step, keep branch,
    # worktree and partial results, release resources normally.
    GRACEFUL = "graceful"
    # Abort generation, tool calls and sub-agents immediately; clean up
    # processes and locks as far as possible.
    IMMEDIATE = "immediate"


class ActorKind(StrEnum):
    USER = "user"
    # The orchestrator or a worker reporting progress.
    SYSTEM = "system"
    # An automatic rule (loop detection, budget, resource protection).
    POLICY = "policy"


@dataclass(frozen=True, slots=True)
class Actor:
    """Who (or what) triggered a command. Recorded on every history event."""

    kind: ActorKind
    id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if (self.kind is ActorKind.USER) != (self.id is not None):
            raise InvalidCommandArgumentError(
                "A user actor needs an id; others must not have one"
            )

    @classmethod
    def user(cls, user_id: uuid.UUID) -> "Actor":
        return cls(ActorKind.USER, user_id)

    @classmethod
    def system(cls) -> "Actor":
        return cls(ActorKind.SYSTEM)

    @classmethod
    def policy(cls) -> "Actor":
        return cls(ActorKind.POLICY)


@dataclass(frozen=True, slots=True)
class TransitionRule:
    sources: frozenset[TaskState]
    target: TaskState
    interruption: Interruption | None = None


_S = TaskState
_C = TaskCommand

# The single source of truth for "may this command run in this state, and where
# does it lead". Everything not listed here is illegal.
TRANSITIONS: dict[TaskCommand, TransitionRule] = {
    # -- lifecycle events ---------------------------------------------------
    _C.START: TransitionRule(frozenset({_S.QUEUED}), _S.RUNNING),
    _C.WAIT: TransitionRule(frozenset({_S.RUNNING}), _S.WAITING),
    _C.UNBLOCK: TransitionRule(frozenset({_S.WAITING}), _S.RUNNING),
    _C.BEGIN_EVALUATION: TransitionRule(frozenset({_S.RUNNING}), _S.EVALUATING),
    _C.COMPLETE: TransitionRule(frozenset({_S.EVALUATING}), _S.COMPLETED),
    # "Failed: not executable, or evaluation failed."
    _C.FAIL: TransitionRule(
        frozenset({_S.QUEUED, _S.RUNNING, _S.WAITING, _S.EVALUATING}), _S.FAILED
    ),
    # -- operator controls --------------------------------------------------
    # Pause stops "at the current safe boundary" and Resume continues from the
    # saved state, so both only concern a task that is actually running.
    _C.PAUSE: TransitionRule(frozenset({_S.RUNNING}), _S.PAUSED, Interruption.GRACEFUL),
    _C.RESUME: TransitionRule(frozenset({_S.PAUSED}), _S.RUNNING),
    # Cancel ends the task gracefully; branch / worktree / partial results are
    # kept (deleting them is a separate operation).
    _C.CANCEL: TransitionRule(
        frozenset({_S.QUEUED, _S.RUNNING, _S.WAITING, _S.PAUSED, _S.EVALUATING}),
        _S.CANCELLED,
        Interruption.GRACEFUL,
    ),
    # Stop Now is the emergency stop: it also ends the task, but interrupts
    # generation / tool execution / sub-agents immediately. It only applies
    # where something can be executing, so not to queued or paused tasks (use
    # Cancel for those).
    _C.STOP_NOW: TransitionRule(
        frozenset({_S.RUNNING, _S.WAITING, _S.EVALUATING}),
        _S.CANCELLED,
        Interruption.IMMEDIATE,
    ),
    # Retry: run again from the failed step, in the same attempt (same branch /
    # worktree / context), optionally with another agent / model. Failed only.
    _C.RETRY: TransitionRule(frozenset({_S.FAILED}), _S.QUEUED),
    # Restart: start over from the original starting commit and task input as a
    # new attempt (new branch / worktree); earlier attempts stay as history.
    _C.RESTART: TransitionRule(frozenset({_S.FAILED, _S.CANCELLED}), _S.QUEUED),
}


@dataclass(frozen=True, slots=True)
class TransitionPlan:
    command: TaskCommand
    from_state: TaskState
    target: TaskState
    wait_reason: WaitReason | None
    interruption: Interruption | None


def interruption_of(command: TaskCommand) -> Interruption | None:
    """How ``command`` stops running work, or ``None`` if it does not stop it."""
    rule = TRANSITIONS.get(command)
    return rule.interruption if rule else None


def allowed_commands(state: TaskState) -> frozenset[TaskCommand]:
    """The commands ``state`` accepts (for example to enable buttons in a UI)."""
    return frozenset(
        command for command, rule in TRANSITIONS.items() if state in rule.sources
    )


def plan_transition(
    state: TaskState,
    command: TaskCommand,
    *,
    wait_reason: WaitReason | None = None,
) -> TransitionPlan:
    """Return the resulting state of ``command`` in ``state``.

    Raises ``IllegalTransitionError`` when the state does not accept the command
    and ``InvalidCommandArgumentError`` when ``wait_reason`` is missing for Wait
    or given for any other command.
    """
    rule = TRANSITIONS.get(command)
    if rule is None or state not in rule.sources:
        raise IllegalTransitionError(state.value, command.value)
    if command is TaskCommand.WAIT:
        if wait_reason is None:
            raise InvalidCommandArgumentError("Wait needs a wait reason")
    elif wait_reason is not None:
        raise InvalidCommandArgumentError("Only Wait accepts a wait reason")
    return TransitionPlan(
        command=command,
        from_state=state,
        target=rule.target,
        wait_reason=wait_reason,
        interruption=rule.interruption,
    )
