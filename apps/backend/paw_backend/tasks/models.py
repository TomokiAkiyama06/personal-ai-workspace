"""ORM models of the task lifecycle (Alembic revision ``0032``; the index
``ix_tasks_project_id_state`` is revision ``0083``; the Working Set, revision
``0085``).

``project_id``, ``created_by`` and ``actor_id`` are plain UUID columns without
foreign keys: the users and projects tables do not exist yet (PAW-021 and the
project issues). They must gain foreign keys when those tables arrive. Until
then the service layer trusts its caller for the ids; authorisation is not done
here (the API layer must do it, see PAW-022 / PAW-025).

The Working Set (issue #85, Decisions 0014 and 0030) is ``task_repositories``: the
repositories of a task and their roles (``referenced`` / ``working`` / ``target``),
each with its own starting commit, kept across Restart. The worktree / review /
pull request state of a repository in an attempt is ``task_attempt_repositories``:
a Single-Repo task is a Working Set of one ``target``, stored the same way (no
second model). ``task_repositories.repository_id`` has no foreign key to
``repositories``: a task's history outlives the registration (a project's purge
deletes its repositories, never its tasks), exactly as ``tasks.project_id``
outlives nothing it points to. The backend checks the registration when a
repository is added (``RepositoryService.working_set_acl``).
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base
from paw_backend.tasks.domain import (
    ActorKind,
    RepoRole,
    TaskCommand,
    TaskState,
    WaitReason,
)
from paw_backend.tasks.records import (
    EvaluationResult,
    LogLevel,
    PullRequestState,
    ReviewStatus,
    StepStatus,
    ToolInvocationStatus,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _enum(enum_class: type[StrEnum], length: int = 24) -> Enum:
    """Store the enum's value as text; the CHECK constraint is added separately."""
    return Enum(
        enum_class,
        native_enum=False,
        create_constraint=False,
        validate_strings=True,
        length=length,
        values_callable=lambda members: [member.value for member in members],
    )


def _in(column: str, enum_class: type[StrEnum], name: str) -> CheckConstraint:
    values = ", ".join(f"'{member.value}'" for member in enum_class)
    return CheckConstraint(f"{column} IN ({values})", name=name)


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    title: Mapped[str] = mapped_column(String(200))
    # The original task input: Restart starts over from it (and from the starting
    # commit of each repository, ``task_repositories.starting_commit``).
    input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[TaskState] = mapped_column(_enum(TaskState))
    wait_reason: Mapped[WaitReason | None] = mapped_column(_enum(WaitReason))
    agent: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(100))
    # Current attempt number (Restart increments it) and total Retry count.
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    # Optimistic concurrency: SQLAlchemy adds "AND version = <loaded>" to every
    # UPDATE and increments it, so a stale writer matches no row.
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        _in("state", TaskState, "state_valid"),
        _in("wait_reason", WaitReason, "wait_reason_valid"),
        # A task has a wait reason exactly while it is waiting.
        CheckConstraint(
            "(state = 'waiting') = (wait_reason IS NOT NULL)",
            name="wait_reason_matches_state",
        ),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("retry_count >= 0", name="retry_count_not_negative"),
        # The tasks of a project, by state (Alembic revision ``0083``, Issue #83):
        # the project module lists a project's active tasks (its stop processor,
        # Decision 0008, section 8) and ``project_id`` has no other index.
        Index("ix_tasks_project_id_state", "project_id", "state"),
        # System Health counts the tasks by state every sample (revision
        # ``0066``): the active ones, and the ones that ended in the last day.
        Index(
            "ix_tasks_active_state",
            "state",
            postgresql_where=text(
                "state IN ('queued', 'running', 'waiting', 'paused', 'evaluating')"
            ),
        ),
        Index(
            "ix_tasks_ended_updated_at",
            "updated_at",
            postgresql_where=text("state IN ('completed', 'cancelled')"),
        ),
    )


class TaskAttemptRow(Base):
    """One attempt of a task. Restart starts a new one and keeps the earlier rows
    as history; the state of each repository in it is ``TaskAttemptRepositoryRow``.
    """

    __tablename__ = "task_attempts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    number: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("task_id", "number"),
        CheckConstraint("number >= 1", name="number_positive"),
    )


class TaskRepositoryRow(Base):
    """One repository of a task's Working Set (kept across Retry and Restart).

    ``starting_commit`` is the repository's own baseline (#85 constraint 1), set
    when it joined the Working Set; ``None`` when it was not known (nothing can
    then be verified as discarded). A repository leaves the Working Set by
    ``removed_at`` (the row stays: nothing of a task is ever deleted); joining it
    again sets a new baseline. ``added_by_kind`` / ``added_by`` are who added it
    or last changed its role; the full history is in ``task_events``.
    """

    __tablename__ = "task_repositories"

    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    # The order the repositories joined in (``restore`` lists them so; rows added
    # in one transaction share ``added_at``).
    seq: Mapped[int] = mapped_column(BigInteger, Identity())
    role: Mapped[RepoRole] = mapped_column(_enum(RepoRole))
    starting_commit: Mapped[str | None] = mapped_column(String(64))
    added_by_kind: Mapped[ActorKind] = mapped_column(_enum(ActorKind))
    added_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        _in("role", RepoRole, "role_valid"),
        _in("added_by_kind", ActorKind, "added_by_kind_valid"),
        CheckConstraint(
            "(added_by_kind = 'user') = (added_by IS NOT NULL)",
            name="added_by_matches_kind",
        ),
    )


class TaskAttemptRepositoryRow(Base):
    """Branch / worktree / review / pull request state of one repository in one
    attempt (Decision 0030, section 2).

    ``strongest_role`` is the strongest role the repository held in the attempt and
    ``modified`` whether a repository write on it, or something executed in it,
    was allowed in the attempt: a
    changed repository keeps the obligations of that role (section 5) until the
    change is verifiably discarded. A repository removed from the Working Set
    keeps its row.
    """

    __tablename__ = "task_attempt_repositories"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    attempt: Mapped[int] = mapped_column(Integer)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    branch: Mapped[str | None] = mapped_column(String(255))
    worktree_path: Mapped[str | None] = mapped_column(String(1024))
    head_commit: Mapped[str | None] = mapped_column(String(64))
    review_status: Mapped[ReviewStatus] = mapped_column(
        _enum(ReviewStatus), default=ReviewStatus.NOT_STARTED
    )
    evaluation_result: Mapped[EvaluationResult] = mapped_column(
        _enum(EvaluationResult), default=EvaluationResult.NOT_RUN
    )
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(String(2048))
    pr_state: Mapped[PullRequestState | None] = mapped_column(_enum(PullRequestState))
    strongest_role: Mapped[RepoRole] = mapped_column(_enum(RepoRole))
    modified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    __table_args__ = (
        # The attempt it belongs to; its task follows from that.
        ForeignKeyConstraint(
            ["task_id", "attempt"], ["task_attempts.task_id", "task_attempts.number"]
        ),
        # A repository of the task's Working Set (a removed one keeps its row).
        ForeignKeyConstraint(
            ["task_id", "repository_id"],
            ["task_repositories.task_id", "task_repositories.repository_id"],
        ),
        UniqueConstraint("task_id", "attempt", "repository_id"),
        _in("review_status", ReviewStatus, "review_status_valid"),
        _in("evaluation_result", EvaluationResult, "evaluation_result_valid"),
        _in("pr_state", PullRequestState, "pr_state_valid"),
        _in("strongest_role", RepoRole, "strongest_role_valid"),
        # A pull request is either fully described or absent.
        CheckConstraint(
            "(pr_number IS NULL) = (pr_url IS NULL)"
            " AND (pr_number IS NULL) = (pr_state IS NULL)",
            name="pull_request_complete",
        ),
    )


class TaskRepositoryWriteRow(Base):
    """A repository write (or something executed) the Tool Broker admitted and
    whose executor may still be running (Codex review of #85, P1).

    ``TaskService.admit_repository_use`` inserts one row per repository under the
    task's row lock, with the id it returns (the reservation); the Tool Broker
    releases it (``released_at``) once the executor returned or failed. While a
    row is neither released nor expired, the repository is not downgraded or
    removed (``RepositoryWriteInFlightError``): a still-clean worktree says nothing
    about a write that has been authorized but not yet made. ``expires_at`` bounds
    a reservation whose executor crashed; after it, a downgrade still needs the
    change to be verified as discarded (the write is recorded as ``modified``).
    Nothing is deleted.
    """

    __tablename__ = "task_repository_writes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # The run that was admitted (``TaskRun``): a Retry keeps the attempt.
    attempt: Mapped[int] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer)
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "attempt"], ["task_attempts.task_id", "task_attempts.number"]
        ),
        ForeignKeyConstraint(
            ["task_id", "repository_id"],
            ["task_repositories.task_id", "task_repositories.repository_id"],
        ),
        CheckConstraint("retry_count >= 0", name="retry_count_not_negative"),
        CheckConstraint("expires_at > admitted_at", name="expires_after_admission"),
        CheckConstraint(
            "released_at IS NULL OR released_at >= admitted_at",
            name="released_after_admission",
        ),
        # The narrowing check reads the live reservations of one repository.
        Index(None, "task_id", "repository_id"),
    )


class TaskAttemptStateArchiveRow(Base):
    """What an attempt's state columns held before revision 0085 (archived by it).

    Before the Working Set an attempt had one branch / worktree / review /
    evaluation / pull request and its task one ``starting_commit``, naming no
    repository, so revision 0085 cannot attribute them to one and does not guess:
    it copies every attempt here before dropping the columns, and its downgrade
    puts them back. Nothing of the application reads or writes it (no privilege);
    it is for an operator who gives such a task its Working Set. The values are
    kept as they were (text, no CHECK: an archive refuses nothing it held).
    """

    __tablename__ = "task_attempt_state_archive"

    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch: Mapped[str | None] = mapped_column(String(255))
    worktree_path: Mapped[str | None] = mapped_column(String(1024))
    head_commit: Mapped[str | None] = mapped_column(String(64))
    review_status: Mapped[str] = mapped_column(String(24))
    evaluation_result: Mapped[str] = mapped_column(String(24))
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(String(2048))
    pr_state: Mapped[str | None] = mapped_column(String(24))
    # The task's ``starting_commit`` (the same for each of its attempts).
    starting_commit: Mapped[str | None] = mapped_column(String(64))
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "number"], ["task_attempts.task_id", "task_attempts.number"]
        ),
    )


class TaskStepRow(Base):
    """One execution of a step. The latest row of an attempt is its current step."""

    __tablename__ = "task_steps"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    attempt: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[StepStatus] = mapped_column(_enum(StepStatus))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("task_id", "attempt", "sequence"),
        # At most one running step per attempt.
        Index(
            "uq_task_steps_one_running",
            "task_id",
            "attempt",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        _in("status", StepStatus, "status_valid"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)",
            name="finished_matches_status",
        ),
    )


class TaskToolInvocationRow(Base):
    """A tool call of a step: identity and status only, never arguments or output."""

    __tablename__ = "task_tool_invocations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    step_id: Mapped[int] = mapped_column(ForeignKey("task_steps.id"))
    tool_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[ToolInvocationStatus] = mapped_column(_enum(ToolInvocationStatus))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(None, "step_id"),
        # Only the calls in flight (a bounded number per step), so that asking
        # for them does not read the step's whole history of finished calls.
        Index(
            "ix_task_tool_invocations_started",
            "step_id",
            postgresql_where=text("status = 'started'"),
        ),
        _in("status", ToolInvocationStatus, "status_valid"),
        CheckConstraint(
            "(status = 'started') = (finished_at IS NULL)",
            name="finished_matches_status",
        ),
    )


# The latest finished calls of a step, newest first (``restore`` returns at most
# 100 of them): PostgreSQL reads them in this order and stops at the limit,
# instead of reading and sorting the step's whole history. Only finished calls, so
# the calls in flight (the other partial index) are not in it.
Index(
    "ix_task_tool_invocations_finished",
    TaskToolInvocationRow.step_id,
    TaskToolInvocationRow.started_at.desc(),
    TaskToolInvocationRow.id.desc(),
    postgresql_where=text("status <> 'started'"),
)


class TaskLogRow(Base):
    __tablename__ = "task_logs"

    seq: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    # The run that wrote the line (a Retry continues the attempt's log).
    attempt: Mapped[int] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer)
    level: Mapped[LogLevel] = mapped_column(_enum(LogLevel))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    __table_args__ = (
        _in("level", LogLevel, "level_valid"),
        CheckConstraint("retry_count >= 0", name="retry_count_not_negative"),
    )


# The lines of one attempt of a task, newest first (``restore`` returns the latest
# ones of the current attempt): PostgreSQL seeks straight to the attempt and reads
# as far as the limit, instead of walking the log of the earlier attempts (a
# Restart leaves them behind) to filter them out. Nothing reads the lines of a
# task across its attempts, and the foreign key on ``task_id`` is served by the
# leading column, so there is no separate index on ``(task_id, seq)``.
Index(
    "ix_task_logs_task_id_attempt_seq",
    TaskLogRow.task_id,
    TaskLogRow.attempt,
    TaskLogRow.seq.desc(),
)


class TaskEventRow(Base):
    """Append-only history: one row per transition, with who / what caused it.

    The database rejects UPDATE and DELETE on this table (a trigger created by
    the migration), so the history cannot be rewritten through the application.
    """

    __tablename__ = "task_events"

    seq: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    # The task's run after the event (for Start: the run of the worker it starts).
    attempt: Mapped[int] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer)
    command: Mapped[TaskCommand] = mapped_column(_enum(TaskCommand))
    from_state: Mapped[TaskState | None] = mapped_column(_enum(TaskState))
    to_state: Mapped[TaskState] = mapped_column(_enum(TaskState))
    wait_reason: Mapped[WaitReason | None] = mapped_column(_enum(WaitReason))
    actor_kind: Mapped[ActorKind] = mapped_column(_enum(ActorKind))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    reason: Mapped[str | None] = mapped_column(String(500))
    step_name: Mapped[str | None] = mapped_column(String(100))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    task_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    __table_args__ = (
        Index(None, "task_id", "seq"),
        # The failures and retries of the last hour / day (System Health,
        # revision ``0066``).
        Index(
            "ix_task_events_retry_fail_created_at",
            "created_at",
            postgresql_where=text("command IN ('retry', 'fail')"),
        ),
        _in("command", TaskCommand, "command_valid"),
        _in("from_state", TaskState, "from_state_valid"),
        _in("to_state", TaskState, "to_state_valid"),
        _in("wait_reason", WaitReason, "wait_reason_valid"),
        _in("actor_kind", ActorKind, "actor_kind_valid"),
        CheckConstraint(
            "(actor_kind = 'user') = (actor_id IS NOT NULL)",
            name="actor_id_matches_kind",
        ),
        CheckConstraint("retry_count >= 0", name="retry_count_not_negative"),
    )


# How many changed files of one pull request are kept (revision 0190 writes the
# same number; ``integration/changes.py`` reads at most this many).
MAX_PULL_REQUEST_FILES = 300


class PullRequestChangesRow(Base):
    """The changed files of a delivered pull request (revision ``0190``, issue
    #185 item 6, Decision 0078 Proposed): read from GitHub when the Integration
    Gate delivered it (``integration/changes.py``), for the PR screen.

    ``files`` holds one object per file (``path``, ``previous_path``, ``status``,
    ``additions``, ``deletions``, ``patch_truncated``), at most
    :data:`MAX_PULL_REQUEST_FILES`, in GitHub's order; ``patches`` the patch of
    each (a string, or ``null`` when GitHub gave none or it was not read), at the
    same index. ``truncated``: GitHub listed more files than were kept. The row
    goes with its record (``task_attempt_repositories``)."""

    __tablename__ = "pull_request_changes"

    record_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("task_attempt_repositories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    head_commit: Mapped[str] = mapped_column(String(64))
    truncated: Mapped[bool] = mapped_column(Boolean)
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    patches: Mapped[list[str | None]] = mapped_column(JSONB)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "head_commit ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'",
            name="head_commit_object_id",
        ),
        CheckConstraint(
            "jsonb_typeof(files) = 'array'"
            f" AND jsonb_array_length(files) <= {MAX_PULL_REQUEST_FILES}"
            " AND jsonb_typeof(patches) = 'array'"
            " AND jsonb_array_length(patches) = jsonb_array_length(files)",
            name="files_shape",
        ),
    )
