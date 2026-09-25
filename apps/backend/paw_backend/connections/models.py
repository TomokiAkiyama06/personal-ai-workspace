"""ORM models of the shared connections, quotas and usage (revision ``0030``).

* ``shared_connections``: at most one row per :class:`ConnectionKind` (workspace
  level, not per user). ``secret_handle`` is the opaque handle of the credential
  in the secret store, **never** the credential: a CHECK constraint accepts only
  the ``cred_`` + 32 hex shape, so a plaintext cannot be written by mistake.
  ``status`` is what the last health check found out; ``enabled`` is the admin's
  switch, a separate column so that a health check can never re-enable a
  connection an admin disabled.
* ``connection_quotas``: one limit per ``(user_id, kind, metric, period)``;
  ``limit_value`` NULL is the explicit **Unlimited**. ``user_id`` references
  ``users`` and cascades (a deleted user has no quota).
* ``connection_usage``: one row per call, written by the admission in the same
  transaction as the quota check (``in_flight``) and settled when the call ends.
  ``user_id`` and ``project_id`` are plain UUIDs (a usage row stays readable as
  history after the user is anonymised; ``project_id`` is the task's, copied at
  admission), ``task_id`` references ``tasks`` (``RESTRICT``: a task with usage is
  never deleted). There is no column for a prompt, an answer or a credential.

Timestamps have no default: the module writes the DATABASE's clock (or the test
clock of ``ConnectionStore``) into them explicitly, so that one instant decides the
window of a quota check and the ``started_at`` of the row it admits.

Allowed values are ``text`` columns with CHECK constraints. The migration repeats
the literals; ``tests/test_connections_schema.py`` fails when the two drift apart.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.connections.domain import (
    ConnectionKind,
    ConnectionStatus,
    FailureCode,
    QuotaMetric,
    QuotaPeriod,
    UsagePurpose,
    UsageStatus,
)
from paw_backend.connections.limits import (
    MAX_DURATION_MS,
    MAX_MODEL_CHARS,
    MAX_QUOTA_LIMIT,
    MAX_TOKENS_PER_CALL,
)
from paw_backend.db import Base
from paw_backend.identity.models import UserRow  # noqa: F401  (FK target)
from paw_backend.tasks.models import TaskRow  # noqa: F401  (FK target)

TABLE_NAMES = ("shared_connections", "connection_quotas", "connection_usage")
HANDLE_SQL_PATTERN = "^cred_[0-9a-f]{32}$"
MODEL_SQL_PATTERN = f"^[A-Za-z0-9][A-Za-z0-9._:/-]{{0,{MAX_MODEL_CHARS - 1}}}$"

_UUID_DEFAULT = text("gen_random_uuid()")


def _in(column: str, values: type[StrEnum], name: str) -> CheckConstraint:
    listed = ", ".join(f"'{member.value}'" for member in values)
    return CheckConstraint(f"{column} IN ({listed})", name=name)


class SharedConnectionRow(Base):
    """The workspace's Codex or Claude connection: a kind, a handle and a status."""

    __tablename__ = "shared_connections"
    __table_args__ = (
        UniqueConstraint("kind"),
        _in("kind", ConnectionKind, "kind_valid"),
        _in("status", ConnectionStatus, "status_valid"),
        CheckConstraint(f"secret_handle ~ '{HANDLE_SQL_PATTERN}'", name="handle_shape"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=_UUID_DEFAULT
    )
    kind: Mapped[str] = mapped_column(Text)
    secret_handle: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConnectionQuotaRow(Base):
    """One limit (a number, or NULL = Unlimited) of one user for one kind, metric
    and period."""

    __tablename__ = "connection_quotas"
    __table_args__ = (
        _in("kind", ConnectionKind, "kind_valid"),
        _in("metric", QuotaMetric, "metric_valid"),
        _in("period", QuotaPeriod, "period_valid"),
        CheckConstraint(
            f"limit_value IS NULL OR limit_value BETWEEN 0 AND {MAX_QUOTA_LIMIT}",
            name="limit_range",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    metric: Mapped[str] = mapped_column(Text, primary_key=True)
    period: Mapped[str] = mapped_column(Text, primary_key=True)
    limit_value: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConnectionUsageRow(Base):
    """One call through a shared connection, attributed to a user and a task."""

    __tablename__ = "connection_usage"
    __table_args__ = (
        _in("kind", ConnectionKind, "kind_valid"),
        _in("purpose", UsagePurpose, "purpose_valid"),
        _in("status", UsageStatus, "status_valid"),
        CheckConstraint(
            "failure_code IS NULL OR failure_code IN ("
            + ", ".join(f"'{member.value}'" for member in FailureCode)
            + ")",
            name="failure_code_valid",
        ),
        CheckConstraint(f"model ~ '{MODEL_SQL_PATTERN}'", name="model_shape"),
        # A failure code exactly when the call failed.
        CheckConstraint(
            "(status = 'failed') = (failure_code IS NOT NULL)",
            name="failure_matches_status",
        ),
        # A call that has not settled has no end, no duration and no tokens yet.
        CheckConstraint(
            "(status = 'in_flight') = (finished_at IS NULL)",
            name="finished_matches_status",
        ),
        CheckConstraint(
            "(finished_at IS NULL) = (duration_ms IS NULL)",
            name="duration_matches_finish",
        ),
        CheckConstraint(
            "status <> 'in_flight' OR (input_tokens IS NULL AND output_tokens IS NULL)",
            name="no_tokens_in_flight",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="finish_after_start",
        ),
        CheckConstraint(
            f"duration_ms IS NULL OR duration_ms BETWEEN 0 AND {MAX_DURATION_MS}",
            name="duration_range",
        ),
        CheckConstraint(
            f"input_tokens IS NULL OR input_tokens BETWEEN 0 AND {MAX_TOKENS_PER_CALL}",
            name="input_tokens_range",
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens BETWEEN 0 AND "
            f"{MAX_TOKENS_PER_CALL}",
            name="output_tokens_range",
        ),
        # The window sums of a quota check: one user, one kind, from an instant.
        Index(
            "ix_connection_usage_user_id_kind_started_at",
            "user_id",
            "kind",
            "started_at",
        ),
        # "Has this task used the connection before?" (a continuing task).
        Index("ix_connection_usage_task_id_kind", "task_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=_UUID_DEFAULT
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    failure_code: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
