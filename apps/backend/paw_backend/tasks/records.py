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
    RepoRole,
    TaskCommand,
    TaskRun,
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
class WorkingSetEntry:
    """One repository of the Working Set a task is created with.

    ``starting_commit`` is the repository's baseline (Decision 0030, #85
    constraint 1): what "the change was discarded" and "the repository was
    changed" are judged against, for every attempt (a Restart reuses it).
    ``None`` means it is not known: nothing can then be verified as discarded
    (fail-closed). Checked by ``TaskService.create_task``.
    """

    repository_id: uuid.UUID
    role: RepoRole
    starting_commit: str | None = None


@dataclass(frozen=True, slots=True)
class WorkingSetRepository:
    """A repository of the task's Working Set as stored (``task_repositories``).

    ``added_by`` is who put it into the Working Set (or last changed its role:
    ``updated_at``); the history of every change is in ``task_events``.
    """

    repository_id: uuid.UUID
    role: RepoRole
    starting_commit: str | None
    added_by: Actor
    added_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AttemptRepositorySnapshot:
    """Worktree / review / pull request state of one repository in one attempt.

    ``strongest_role`` is the strongest role the repository held in the attempt
    and ``modified`` whether a repository write was allowed in it (Decision 0030,
    section 5): a repository that was changed keeps the obligations of that role
    after a downgrade or a removal, until the change is verifiably discarded. A
    repository removed from the Working Set keeps its row here.
    """

    repository_id: uuid.UUID
    worktree: WorktreeState
    review: ReviewState
    pull_request: PullRequestInfo | None
    strongest_role: RepoRole
    modified: bool


@dataclass(frozen=True, slots=True)
class AttemptSnapshot:
    """The repositories of one attempt of a task, each with its own state."""

    number: int
    repositories: tuple[AttemptRepositorySnapshot, ...] = field(default=())

    def repository(self, repository_id: uuid.UUID) -> AttemptRepositorySnapshot:
        """The state of ``repository_id`` in this attempt (``KeyError`` if none)."""
        for repository in self.repositories:
            if repository.repository_id == repository_id:
                return repository
        raise KeyError("the repository is not part of this attempt")


@dataclass(frozen=True, slots=True)
class RepositoryChangeState:
    """What the backend found in a repository, to verify a discarded change.

    Reported by a ``RepositoryChangeInspector`` (``tasks.working_set``) from the
    repository itself, never from what an agent says (AGENTS.md 1.3).
    """

    clean: bool
    head_commit: str | None
    branch_pushed: bool
    open_pull_request: bool


@dataclass(frozen=True, slots=True)
class StepInfo:
    """A step execution. ``id`` is what a worker passes back to finish it."""

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
    """A log line, tagged with the run (attempt and retry count) that wrote it.

    A Retry continues the attempt's log, so the lines of the runs of one attempt
    are told apart by ``retry_count``.
    """

    seq: int
    attempt: int
    retry_count: int
    level: LogLevel
    message: str
    created_at: datetime

    @property
    def run(self) -> TaskRun:
        return TaskRun(self.attempt, self.retry_count)


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """One row of the append-only task history.

    ``seq`` increases monotonically across all tasks. ``step_name`` is the
    latest step at the time of the event, except for Stop Now and Fail: they name
    only a step they actually ended (``None`` if no step was running, even when an
    earlier, finished step exists). ``task_version`` is the task version after
    the event. ``attempt`` and ``retry_count`` are the task's after the event: for
    Start they are the run the worker is started for (``run``).
    """

    seq: int
    task_id: uuid.UUID
    attempt: int
    retry_count: int
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
    def run(self) -> TaskRun:
        """The run of the task after this event (what a Start event hands a worker)."""
        return TaskRun(self.attempt, self.retry_count)

    @property
    def interruption(self) -> Interruption | None:
        """Graceful (Pause, Cancel), immediate (Stop Now) or ``None``."""
        return interruption_of(self.command)


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """Everything a new process or a reconnecting client needs to see a task.

    Built only from the database by ``TaskService.restore``. ``working_set`` is
    the task's Working Set (issue #85, Decisions 0014 and 0030): the repositories
    and their roles, ordered by when they were added. The Tool Broker, the UI and
    the Orchestrator read the roles from here and keep no copy of their own. The
    state of each repository in an attempt is in ``attempt.repositories``.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    created_by: uuid.UUID
    title: str
    input: dict[str, Any]
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
    working_set: tuple[WorkingSetRepository, ...] = field(default=())

    def repository(self, repository_id: uuid.UUID) -> WorkingSetRepository | None:
        """The Working Set's entry for ``repository_id`` (``None``: not in it)."""
        for repository in self.working_set:
            if repository.repository_id == repository_id:
                return repository
        return None

    @property
    def run(self) -> TaskRun:
        """The run a worker started now would belong to."""
        return TaskRun(self.attempt.number, self.retry_count)
