"""Agent Task lifecycle: states, commands and their persistence (PAW-032).

There is no HTTP surface yet: authentication and RBAC (PAW-022 / PAW-025) come
first. See ``apps/backend/README.md`` for the state machine and the data model.
"""

from paw_backend.tasks.domain import (
    CONTROL_COMMANDS,
    TERMINAL_STATES,
    TRANSITIONS,
    Actor,
    ActorKind,
    Interruption,
    TaskCommand,
    TaskState,
    WaitReason,
    allowed_commands,
    interruption_of,
    plan_transition,
)
from paw_backend.tasks.errors import (
    IllegalTransitionError,
    InvalidCommandArgumentError,
    TaskConflictError,
    TaskError,
    TaskNotFoundError,
    TaskStepError,
)

__all__ = [
    "CONTROL_COMMANDS",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "Actor",
    "ActorKind",
    "IllegalTransitionError",
    "Interruption",
    "InvalidCommandArgumentError",
    "TaskCommand",
    "TaskConflictError",
    "TaskError",
    "TaskNotFoundError",
    "TaskState",
    "TaskStepError",
    "WaitReason",
    "allowed_commands",
    "interruption_of",
    "plan_transition",
]
