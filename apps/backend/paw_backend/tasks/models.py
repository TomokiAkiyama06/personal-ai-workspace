"""ORM models of the task lifecycle (Alembic revision ``0032``).

``project_id``, ``created_by`` and ``actor_id`` are plain UUID columns without
foreign keys: the users and projects tables do not exist yet (PAW-021 and the
project issues). They must gain foreign keys when those tables arrive. Until
then the service layer trusts its caller for the ids; authorisation is not done
here (the API layer must do it, see PAW-022 / PAW-025).

The Multi-Repo working set (repositories with the ``referenced`` / ``working`` /
``target`` roles, each with its own worktree / review / pull request state) is
not stored: it is outside PAW-032 and proposed in
``docs/decisions/0014-task-working-set-persistence.md``. ``TaskAttemptRow``
holds the state of a single repository.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
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
from paw_backend.tasks.domain import ActorKind, TaskCommand, TaskState, WaitReason
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
    # The original task input and starting commit: Restart starts over from them.
    input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    starting_commit: Mapped[str | None] = mapped_column(String(64))
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
    )


class TaskAttemptRow(Base):
    """Branch / worktree / review / pull request state of one attempt.

    Restart starts a new attempt and keeps the earlier rows as history.
    """

    __tablename__ = "task_attempts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    number: Mapped[int] = mapped_column(Integer)
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("task_id", "number"),
        _in("review_status", ReviewStatus, "review_status_valid"),
        _in("evaluation_result", EvaluationResult, "evaluation_result_valid"),
        _in("pr_state", PullRequestState, "pr_state_valid"),
        CheckConstraint("number >= 1", name="number_positive"),
        # A pull request is either fully described or absent.
        CheckConstraint(
            "(pr_number IS NULL) = (pr_url IS NULL)"
            " AND (pr_number IS NULL) = (pr_state IS NULL)",
            name="pull_request_complete",
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
    attempt: Mapped[int] = mapped_column(Integer)
    level: Mapped[LogLevel] = mapped_column(_enum(LogLevel))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    __table_args__ = (
        Index(None, "task_id", "seq"),
        _in("level", LogLevel, "level_valid"),
    )


class TaskEventRow(Base):
    """Append-only history: one row per transition, with who / what caused it.

    The database rejects UPDATE and DELETE on this table (a trigger created by
    the migration), so the history cannot be rewritten through the application.
    """

    __tablename__ = "task_events"

    seq: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    attempt: Mapped[int] = mapped_column(Integer)
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
        _in("command", TaskCommand, "command_valid"),
        _in("from_state", TaskState, "from_state_valid"),
        _in("to_state", TaskState, "to_state_valid"),
        _in("wait_reason", WaitReason, "wait_reason_valid"),
        _in("actor_kind", ActorKind, "actor_kind_valid"),
        CheckConstraint(
            "(actor_kind = 'user') = (actor_id IS NOT NULL)",
            name="actor_id_matches_kind",
        ),
    )
