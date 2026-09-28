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
    # Not a transition either: ``TaskService.change_working_set`` records a change
    # of the task's Working Set (issue #85, Decision 0030) with it. The state does
    # not change; every state rejects it as a command of ``execute``.
    CHANGE_WORKING_SET = "change_working_set"
    # Not a transition either: ``TaskService.release_stale_repository_write``
    # records that a human released, by hand, the repository write reservation of
    # a process that crashed (issue #129, Decision 0048 Proposed). The state does
    # not change; every state rejects it as a command of ``execute``.
    RELEASE_REPOSITORY_WRITE = "release_repository_write"

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


def enum_member[E: StrEnum](name: str, enum_class: type[E], value: object) -> E:
    """``value`` as a member of ``enum_class``, or ``InvalidCommandArgumentError``.

    Accepts the member itself or its serialised value (an exact ``str``, such as
    ``"stop_now"`` read from JSON) and returns the member, so that the code after
    it compares by identity and stores the member. Anything else is refused: a
    ``str`` subclass (its methods could lie), a member of another enum that has the
    same text, bytes, numbers, ``None``. The error names the argument and the
    valid values and never echoes ``value``.
    """
    if type(value) is enum_class:
        return value
    if type(value) is str:
        try:
            return enum_class(value)
        except ValueError:
            pass
    allowed = ", ".join(member.value for member in enum_class)
    raise InvalidCommandArgumentError(f"{name} must be one of: {allowed}")


@dataclass(frozen=True, slots=True)
class Actor:
    """Who (or what) triggered a command. Recorded on every history event.

    ``kind`` may be given as its serialised value (``"user"``); it is stored as
    the ``ActorKind`` member. ``id`` is a ``uuid.UUID`` (a UUID as text is
    refused), and only a user actor has one.
    """

    kind: ActorKind
    id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        kind = enum_member("actor kind", ActorKind, self.kind)
        if kind is not self.kind:
            object.__setattr__(self, "kind", kind)  # frozen: normalise in place
        if self.id is not None and not isinstance(self.id, uuid.UUID):
            raise InvalidCommandArgumentError("An actor id must be a UUID")
        if (kind is ActorKind.USER) != (self.id is not None):
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


# ``tasks.attempt`` and ``tasks.retry_count`` are 32-bit ``INTEGER`` columns.
_MAX_COUNTER = 2**31 - 1


@dataclass(frozen=True, slots=True)
class TaskRun:
    """Which run of a task a worker belongs to: the attempt and the retry count.

    Retry runs a failed task again inside the same attempt, so ``attempt`` alone
    cannot tell the run that failed from the one that Retry started. Restart
    increments ``attempt`` and Retry increments ``retry_count``; neither ever
    decreases, and every re-opening of a failed or cancelled task changes one of
    them, so two runs of a task are equal only if they are the same run. A worker
    learns its run from the event that started it (``TaskEvent.run`` of Start) and
    passes it back with everything it writes about the attempt (``begin_step``,
    ``add_log``, ``update_attempt``). The Tool Broker (PAW-031) binds approvals to
    the same pair.
    """

    attempt: int
    retry_count: int

    def __post_init__(self) -> None:
        for name, value, minimum in (
            ("attempt", self.attempt, 1),
            ("retry_count", self.retry_count, 0),
        ):
            # ``bool`` is an ``int`` in Python; text or a float is not a counter.
            if type(value) is not int or not minimum <= value <= _MAX_COUNTER:
                raise InvalidCommandArgumentError(
                    f"{name} must be an integer from {minimum} to {_MAX_COUNTER}"
                )


class RepoRole(StrEnum):
    """The role of a repository in a task's Working Set (``REQUIREMENTS.md``,
    "Multi-Repo Task / Working Set"; Decision 0030).

    * ``referenced``: investigate, search, read only;
    * ``working``: may also be edited and tested;
    * ``target``: may also get a pull request (what the task delivers).
    """

    REFERENCED = "referenced"
    WORKING = "working"
    TARGET = "target"

    @property
    def strength(self) -> int:
        """``referenced`` < ``working`` < ``target``: what the role lets a task do."""
        return _ROLE_STRENGTH[self]


_ROLE_STRENGTH = {RepoRole.REFERENCED: 0, RepoRole.WORKING: 1, RepoRole.TARGET: 2}


class WorkingSetOperation(StrEnum):
    """One change of a Working Set (Decision 0030, section 3).

    The resulting role is fixed by the operation, never chosen by an argument: a
    tool of the Tool Broker does exactly one of these (``ToolSpec``), so that the
    approval level of the tool can be the level of its operation.
    """

    # A repository that is not in the Working Set, as ``referenced``.
    ADD_REFERENCED = "add_referenced"
    # Add as ``working``, or promote a ``referenced`` one.
    SET_WORKING = "set_working"
    # Add as ``target``, or promote a ``referenced`` / ``working`` one.
    SET_TARGET = "set_target"
    # ``target`` -> ``working``.
    DOWNGRADE_TO_WORKING = "downgrade_to_working"
    # ``working`` / ``target`` -> ``referenced``.
    DOWNGRADE_TO_REFERENCED = "downgrade_to_referenced"
    # Out of the Working Set (whatever its role).
    REMOVE = "remove"


# The roles an operation accepts the repository in (``None``: not in the Working
# Set), and the role it leaves it in (``None``: removed).
_OPERATION_RULES: dict[
    WorkingSetOperation, tuple[frozenset[RepoRole | None], RepoRole | None]
] = {
    WorkingSetOperation.ADD_REFERENCED: (frozenset({None}), RepoRole.REFERENCED),
    WorkingSetOperation.SET_WORKING: (
        frozenset({None, RepoRole.REFERENCED}),
        RepoRole.WORKING,
    ),
    WorkingSetOperation.SET_TARGET: (
        frozenset({None, RepoRole.REFERENCED, RepoRole.WORKING}),
        RepoRole.TARGET,
    ),
    WorkingSetOperation.DOWNGRADE_TO_WORKING: (
        frozenset({RepoRole.TARGET}),
        RepoRole.WORKING,
    ),
    WorkingSetOperation.DOWNGRADE_TO_REFERENCED: (
        frozenset({RepoRole.WORKING, RepoRole.TARGET}),
        RepoRole.REFERENCED,
    ),
    WorkingSetOperation.REMOVE: (
        frozenset({RepoRole.REFERENCED, RepoRole.WORKING, RepoRole.TARGET}),
        None,
    ),
}


def accepts_role(operation: WorkingSetOperation, role: RepoRole | None) -> bool:
    """Whether ``operation`` applies to a repository now in ``role`` (``None``:
    not in the Working Set)."""
    return role in _OPERATION_RULES[operation][0]


def role_after(operation: WorkingSetOperation) -> RepoRole | None:
    """The role ``operation`` leaves the repository in (``None``: removed)."""
    return _OPERATION_RULES[operation][1]


def narrows(operation: WorkingSetOperation) -> bool:
    """A downgrade or a removal: what the task may do with the repository shrinks."""
    return operation in (
        WorkingSetOperation.DOWNGRADE_TO_WORKING,
        WorkingSetOperation.DOWNGRADE_TO_REFERENCED,
        WorkingSetOperation.REMOVE,
    )


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
    # Cancel for those). The reason and the interrupted step must be kept, so
    # ``TaskService.execute`` requires a reason for it (the table does not
    # know about arguments).
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

    Raises ``InvalidCommandArgumentError`` when ``wait_reason`` is not a
    ``WaitReason`` (or its value), whatever the state; ``IllegalTransitionError``
    when the state does not accept the command; and, after that check,
    ``InvalidCommandArgumentError`` when ``wait_reason`` is missing for Wait or
    given for any other command (an illegal transition is reported first).
    """
    if wait_reason is not None:
        wait_reason = enum_member("wait_reason", WaitReason, wait_reason)
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
