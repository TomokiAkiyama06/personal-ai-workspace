"""Task queue, budgets and loop detection (PAW-033).

Builds on the PAW-032 task lifecycle (``paw_backend.tasks``) without changing it.
There is no HTTP surface and no orchestration (PAW-034): these are the building
blocks the orchestrator calls. See ``apps/backend/README.md``.
"""

from paw_backend.tasks.queueing.budget import BudgetTracker
from paw_backend.tasks.queueing.domain import (
    ACTION_TASK_COMMANDS,
    ACTIVE_QUEUE_STATUSES,
    DEFAULT_LOOP_POLICY,
    PRESET_LIMITS,
    PRIORITY_RANKS,
    RECORDABLE_KINDS,
    BudgetKind,
    BudgetPreset,
    BudgetStatus,
    BudgetUsage,
    BudgetVerdict,
    Decision,
    DecisionReason,
    FailureRecord,
    LoopAssessment,
    LoopPolicy,
    LoopVerdict,
    NextAction,
    Priority,
    QueueEntry,
    QueueStatus,
    limit_for,
)
from paw_backend.tasks.queueing.errors import (
    BudgetNotConfiguredError,
    InvalidQueueingArgumentError,
    LeaseLostError,
    QueueingError,
    TaskAlreadyQueuedError,
)
from paw_backend.tasks.queueing.escalation import decide_next_action
from paw_backend.tasks.queueing.loop import (
    LoopDetector,
    evaluate_loop,
    failure_signature,
    normalize_failure_message,
)
from paw_backend.tasks.queueing.task_queue import TaskQueue

__all__ = [
    "ACTION_TASK_COMMANDS",
    "ACTIVE_QUEUE_STATUSES",
    "DEFAULT_LOOP_POLICY",
    "PRESET_LIMITS",
    "PRIORITY_RANKS",
    "RECORDABLE_KINDS",
    "BudgetKind",
    "BudgetNotConfiguredError",
    "BudgetPreset",
    "BudgetStatus",
    "BudgetTracker",
    "BudgetUsage",
    "BudgetVerdict",
    "Decision",
    "DecisionReason",
    "FailureRecord",
    "InvalidQueueingArgumentError",
    "LeaseLostError",
    "LoopAssessment",
    "LoopDetector",
    "LoopPolicy",
    "LoopVerdict",
    "NextAction",
    "Priority",
    "QueueEntry",
    "QueueStatus",
    "QueueingError",
    "TaskAlreadyQueuedError",
    "TaskQueue",
    "decide_next_action",
    "evaluate_loop",
    "failure_signature",
    "limit_for",
    "normalize_failure_message",
]
