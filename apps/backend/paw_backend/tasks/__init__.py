"""Agent Task lifecycle: states, commands and their persistence (PAW-032).

There is no HTTP surface yet: authentication and RBAC (PAW-022 / PAW-025) come
first. See ``apps/backend/README.md`` for the state machine and the data model.
The Project state gate (``ProjectGate``, Issue #83) is the one place where the task
lane looks at a project; ``projects.task_gate`` implements it.
"""

from paw_backend.tasks.domain import (
    CONTROL_COMMANDS,
    TERMINAL_STATES,
    TRANSITIONS,
    Actor,
    ActorKind,
    Interruption,
    TaskCommand,
    TaskRun,
    TaskState,
    WaitReason,
    allowed_commands,
    interruption_of,
    plan_transition,
)
from paw_backend.tasks.errors import (
    IllegalTransitionError,
    InvalidCommandArgumentError,
    ProjectNotActiveError,
    StaleAttemptError,
    StaleRunError,
    TaskConflictError,
    TaskError,
    TaskNotFoundError,
    TaskStepError,
)
from paw_backend.tasks.project_gate import ProjectGate
from paw_backend.tasks.records import (
    AttemptSnapshot,
    EvaluationResult,
    LogEntry,
    LogLevel,
    PullRequestInfo,
    PullRequestState,
    ReviewState,
    ReviewStatus,
    StepInfo,
    StepStatus,
    TaskEvent,
    TaskSnapshot,
    ToolInvocationInfo,
    ToolInvocationStatus,
    WorktreeState,
)
from paw_backend.tasks.service import InTransactionStep, TaskService, TransitionListener

__all__ = [
    "CONTROL_COMMANDS",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "Actor",
    "ActorKind",
    "AttemptSnapshot",
    "EvaluationResult",
    "IllegalTransitionError",
    "InTransactionStep",
    "Interruption",
    "InvalidCommandArgumentError",
    "LogEntry",
    "LogLevel",
    "ProjectGate",
    "ProjectNotActiveError",
    "PullRequestInfo",
    "PullRequestState",
    "ReviewState",
    "ReviewStatus",
    "StaleAttemptError",
    "StaleRunError",
    "StepInfo",
    "StepStatus",
    "TaskCommand",
    "TaskConflictError",
    "TaskError",
    "TaskEvent",
    "TaskNotFoundError",
    "TaskRun",
    "TaskService",
    "TaskSnapshot",
    "TaskState",
    "TaskStepError",
    "ToolInvocationInfo",
    "ToolInvocationStatus",
    "TransitionListener",
    "WaitReason",
    "WorktreeState",
    "allowed_commands",
    "interruption_of",
    "plan_transition",
]
