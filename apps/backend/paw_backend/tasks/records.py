"""Immutable value objects returned by the task service.

They are plain dataclasses, independent of SQLAlchemy, so that a snapshot can
be handed to the API layer (or serialised) without exposing ORM rows.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from paw_backend.tasks.domain import (
    Actor,
    Interruption,
    TaskCommand,
    TaskState,
    WaitReason,
    interruption_of,
)


class StepStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Cut short by Stop Now or by a graceful stop.
    INTERRUPTED = "interrupted"


class ToolInvocationStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Cut short by Stop Now, by a step that ended, or by a Restart.
    INTERRUPTED = "interrupted"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ReviewStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class EvaluationResult(StrEnum):
    """Result of the machine evaluation (tests, Evaluator) of an attempt."""

    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"


class PullRequestState(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class WorktreeState:
    branch: str | None = None
    path: str | None = None
    head_commit: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewState:
    review_status: ReviewStatus = ReviewStatus.NOT_STARTED
    evaluation_result: EvaluationResult = EvaluationResult.NOT_RUN


@dataclass(frozen=True, slots=True)
class PullRequestInfo:
    number: int
    url: str
    state: PullRequestState


@dataclass(frozen=True, slots=True)
class AttemptSnapshot:
    """Worktree / review / pull request state of one attempt of a task."""

    number: int
    worktree: WorktreeState
    review: ReviewState
    pull_request: PullRequestInfo | None


@dataclass(frozen=True, slots=True)
class StepInfo:
    """A step execution. ``id`` and ``attempt`` are what a worker passes back."""

    id: int
    attempt: int
    sequence: int
    name: str
    status: StepStatus
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class ToolInvocationInfo:
    """A tool call made by a step: identity and execution state only.

    Arguments and output are deliberately not stored here; they belong to the
    Tool Broker (PAW-031), which can use ``id`` to refer to the same call.
    """

    id: uuid.UUID
    step_id: int
    tool_name: str
    status: ToolInvocationStatus
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class LogEntry:
    seq: int
    attempt: int
    level: LogLevel
    message: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """One row of the append-only task history.

    ``seq`` increases monotonically across all tasks. ``step_name`` is the
    latest step at the time of the event, except for Stop Now and Fail: they name
    only a step they actually ended (``None`` if no step was running, even when an
    earlier, finished step exists). ``task_version`` is the task version after
    the event.
    """

    seq: int
    task_id: uuid.UUID
    attempt: int
    command: TaskCommand
    from_state: TaskState | None
    to_state: TaskState
    wait_reason: WaitReason | None
    actor: Actor
    reason: str | None
    step_name: str | None
    detail: dict[str, Any] | None
    task_version: int
    created_at: datetime

    @property
    def interruption(self) -> Interruption | None:
        """Graceful (Pause, Cancel), immediate (Stop Now) or ``None``."""
        return interruption_of(self.command)


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """Everything a new process or a reconnecting client needs to see a task.

    Built only from the database by ``TaskService.restore``.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    created_by: uuid.UUID
    title: str
    input: dict[str, Any]
    starting_commit: str | None
    state: TaskState
    wait_reason: WaitReason | None
    version: int
    agent: str | None
    model: str | None
    attempt: AttemptSnapshot
    retry_count: int
    current_step: StepInfo | None
    recent_logs: tuple[LogEntry, ...]
    last_event: TaskEvent
    created_at: datetime
    updated_at: datetime
    # Earlier attempts (oldest first); Restart keeps them as history.
    previous_attempts: tuple[AttemptSnapshot, ...] = field(default=())
    # Tool calls of the current step, oldest first: every one that is still
    # ``started`` (what the backend would have to resume or abort; a step has at
    # most ``MAX_ACTIVE_TOOL_INVOCATIONS`` of them) and the latest 100 finished.
    tool_invocations: tuple[ToolInvocationInfo, ...] = field(default=())
