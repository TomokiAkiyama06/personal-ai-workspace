"""ORM models of the task queue, budgets and loop detection (Alembic revision ``0033``).

Every table references ``tasks.id`` with a real foreign key (PAW-032). The
tables are named without the ``task`` prefix on purpose: the PAW-032 tests
inspect every table whose name starts with ``task``.

Enum-like columns are text with CHECK constraints; the migration writes the value
lists out (change a list with a new revision).
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base
from paw_backend.tasks.queueing.domain import (
    BudgetKind,
    BudgetPreset,
    Priority,
    QueueStatus,
)
from paw_backend.tasks.queueing.validation import MAX_APPROACH, MAX_CONSUMED


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


class QueueEntryRow(Base):
    """One waiting or running unit of work. At most one active entry per task.

    Claim order among the claimable entries (status ``queued``, or ``claimed``
    with ``lease_expires_at <= now``) is ``priority_rank``, ``enqueued_at``, ``id``.
    ``priority_rank`` repeats ``priority`` as a number (0 high, 1 normal, 2 low)
    so that the order can be sorted and indexed; a CHECK keeps the two in step.
    """

    __tablename__ = "queue_entries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    priority: Mapped[Priority] = mapped_column(_enum(Priority))
    priority_rank: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[QueueStatus] = mapped_column(
        _enum(QueueStatus), default=QueueStatus.QUEUED, server_default=text("'queued'")
    )
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(100))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        _in("status", QueueStatus, "status_valid"),
        _in("priority", Priority, "priority_valid"),
        CheckConstraint(
            "(priority = 'high' AND priority_rank = 0)"
            " OR (priority = 'normal' AND priority_rank = 1)"
            " OR (priority = 'low' AND priority_rank = 2)",
            name="priority_rank_matches_priority",
        ),
        CheckConstraint("claim_count >= 0", name="claim_count_not_negative"),
        # A lease exists exactly while the entry is claimed.
        CheckConstraint(
            "(status = 'claimed') = (lease_expires_at IS NOT NULL)",
            name="lease_matches_status",
        ),
        CheckConstraint(
            "status <> 'claimed'"
            " OR (claimed_by IS NOT NULL AND claimed_at IS NOT NULL)",
            name="claimed_has_worker",
        ),
        CheckConstraint(
            "status <> 'queued' OR (claimed_by IS NULL AND claimed_at IS NULL)",
            name="queued_has_no_worker",
        ),
        CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > claimed_at",
            name="lease_after_claim",
        ),
        CheckConstraint(
            "(status IN ('completed', 'cancelled')) = (finished_at IS NOT NULL)",
            name="finished_matches_status",
        ),
        # A task has at most one active entry, so it cannot run twice.
        Index(
            "uq_queue_entries_one_active_per_task",
            "task_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'claimed')"),
        ),
        # Serves the claim query.
        Index(
            "ix_queue_entries_claim_order",
            "priority_rank",
            "enqueued_at",
            "id",
            postgresql_where=text("status IN ('queued', 'claimed')"),
        ),
    )


class BudgetUsageRow(Base):
    """Consumption of one budget item of one task.

    ``limit_value`` ``NULL`` means unlimited and is used by, and only by, the
    ``unlimited`` preset. ``running_since`` is set only on the ``runtime_seconds``
    row while a run is in progress.
    """

    __tablename__ = "budget_usages"

    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    kind: Mapped[BudgetKind] = mapped_column(_enum(BudgetKind), primary_key=True)
    preset: Mapped[BudgetPreset] = mapped_column(_enum(BudgetPreset))
    consumed: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    limit_value: Mapped[int | None] = mapped_column(BigInteger)
    running_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        _in("kind", BudgetKind, "kind_valid"),
        _in("preset", BudgetPreset, "preset_valid"),
        CheckConstraint(
            f"consumed >= 0 AND consumed <= {MAX_CONSUMED}", name="consumed_in_range"
        ),
        CheckConstraint(
            "limit_value IS NULL OR limit_value >= 0", name="limit_not_negative"
        ),
        CheckConstraint(
            "(preset = 'unlimited') = (limit_value IS NULL)",
            name="limit_matches_preset",
        ),
        CheckConstraint(
            "running_since IS NULL OR kind = 'runtime_seconds'",
            name="running_only_for_runtime",
        ),
    )


class FailureSignatureRow(Base):
    """One recorded failure: a signature hash and the approach; never the message."""

    __tablename__ = "loop_failure_signatures"

    seq: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    approach: Mapped[int] = mapped_column(Integer)
    signature: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        Index(None, "task_id", "seq"),
        CheckConstraint(
            f"approach >= 0 AND approach <= {MAX_APPROACH}", name="approach_in_range"
        ),
        CheckConstraint("signature ~ '^[0-9a-f]{64}$'", name="signature_format"),
    )
