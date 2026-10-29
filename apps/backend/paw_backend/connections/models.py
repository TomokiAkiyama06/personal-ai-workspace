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
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.connections.domain import (
    ConnectionKind,
    ConnectionStatus,
    CredentialSuspicion,
    FailureCode,
    QuotaMetric,
    QuotaPeriod,
    SessionCollection,
    UsagePurpose,
    UsageStatus,
)
from paw_backend.connections.limits import (
    MAX_DURATION_MS,
    MAX_MODEL_CHARS,
    MAX_QUOTA_LIMIT,
    MAX_TOKENS_PER_CALL,
)
from paw_backend.connections.probe import ProbeOutcome, ProbeState, ProbeTrigger
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
        # Revision ``0280`` (Decision 0096, 5): a login that could not be
        # collected marks the connection until it is checked or replaced.
        CheckConstraint(
            "(suspect_at IS NULL) = (suspect_reason IS NULL)",
            name="suspect_complete",
        ),
        CheckConstraint(
            "suspect_reason IS NULL OR suspect_reason IN ("
            + ", ".join(f"'{member.value}'" for member in CredentialSuspicion)
            + ")",
            name="suspect_reason_valid",
        ),
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
    suspect_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspect_reason: Mapped[str | None] = mapped_column(Text)


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
        # A one-prompt call has no tokens until it settles; a CLI session's row
        # counts them turn by turn (Decision 0083, 5 (b); revision ``0194``).
        CheckConstraint(
            "session OR status <> 'in_flight'"
            " OR (input_tokens IS NULL AND output_tokens IS NULL)",
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
        # The calls still in flight (Alembic revision ``0066``, issue #52): the
        # reaper of abandoned calls and System Health look for them only.
        Index(
            "ix_connection_usage_in_flight_started_at",
            "started_at",
            postgresql_where=text("status = 'in_flight'"),
        ),
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
    # A CLI session's row (Decision 0083, 5; revision ``0194``).
    session: Mapped[bool] = mapped_column(Boolean, server_default=false())


#: The table of the per-connection session slot (revision ``0239``).
SESSION_LEASE_TABLE = "connection_session_leases"


class ConnectionSessionLeaseRow(Base):
    """The one session slot of a connection whose CLI keeps its credential in a
    file it refreshes (Decision 0083, 6: a Codex ChatGPT login). One row per
    connection (keyed by its kind: there is one connection per kind) while a
    session holds the slot: the holder (a random id per acquisition), when it
    was taken, the last heartbeat and the expiry (the database's clock). An
    expired row may be taken over by any process; the holder deletes its own
    row when its session ended. No credential, no task, no content.

    Revision ``0280`` (Decision 0096, 2): a Codex session records on its row,
    before its login is placed, its UUID (``session``), the credential handle
    it was given (an opaque id) and that credential's version
    (``expected_version``); ``kept`` says its CLI could not be stopped (or the
    collection loop took the row over). A row with a ``session`` is never
    taken over by an ordinary acquisition: only the collection loop takes it
    over, once it expired."""

    __tablename__ = SESSION_LEASE_TABLE
    __table_args__ = (
        _in("kind", ConnectionKind, "kind_valid"),
        CheckConstraint("heartbeat_at >= acquired_at", name="heartbeat_after_acquire"),
        CheckConstraint("expires_at > heartbeat_at", name="expires_after_heartbeat"),
        CheckConstraint(
            "(session IS NULL) = (secret_handle IS NULL)"
            " AND (session IS NULL) = (expected_version IS NULL)",
            name="session_complete",
        ),
        CheckConstraint("NOT kept OR session IS NOT NULL", name="kept_has_session"),
        CheckConstraint(
            "collected IS NULL OR (session IS NOT NULL AND collected IN ("
            + ", ".join(f"'{member.value}'" for member in SessionCollection)
            + "))",
            name="collected_valid",
        ),
        CheckConstraint(
            "expected_version IS NULL OR expected_version >= 1",
            name="version_positive",
        ),
        CheckConstraint(
            f"secret_handle IS NULL OR secret_handle ~ '{HANDLE_SQL_PATTERN}'",
            name="handle_shape",
        ),
    )

    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    holder: Mapped[uuid.UUID] = mapped_column(Uuid)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    session: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    secret_handle: Mapped[str | None] = mapped_column(Text)
    expected_version: Mapped[int | None] = mapped_column(BigInteger)
    kept: Mapped[bool] = mapped_column(Boolean, server_default=false())
    collected: Mapped[str | None] = mapped_column(Text)


#: The table of the connections' health probes (revision ``0306``, Decision 0100).
PROBE_TABLE = "connection_probes"


class ConnectionProbeRow(Base):
    """One health probe of a connection's credential (Decision 0100, 3): a
    short CLI session with a fixed prompt and no tools, which belongs to no
    task and no user (it is not a ``connection_usage`` row and is counted into
    no quota). Written with its ``connection.probe`` audit event, in one
    transaction, before anything is sent; finished when the probe ended (or
    ``abandoned`` by the reaper when its process died).

    ``connection_id`` is a plain UUID (the probes stay as history after the
    connection is removed); ``secret_handle`` is the credential that was
    checked (an opaque handle, never the credential; not in the audit) and
    ``credential_version`` the secret store's version of it when the probe
    started. ``actor_id`` is the Admin who caused the probe (the recheck, or
    the attach / replace it followed). No column holds a prompt, an answer or a
    credential. At most one probe of a credential runs at a time (the partial
    unique index)."""

    __tablename__ = PROBE_TABLE
    __table_args__ = (
        _in("kind", ConnectionKind, "kind_valid"),
        _in("trigger", ProbeTrigger, "trigger_valid"),
        _in("state", ProbeState, "state_valid"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ("
            + ", ".join(f"'{member.value}'" for member in ProbeOutcome)
            + ")",
            name="outcome_valid",
        ),
        CheckConstraint(
            "failure_code IS NULL OR failure_code IN ("
            + ", ".join(f"'{member.value}'" for member in FailureCode)
            + ")",
            name="failure_code_valid",
        ),
        CheckConstraint(f"secret_handle ~ '{HANDLE_SQL_PATTERN}'", name="handle_shape"),
        CheckConstraint(
            f"model IS NULL OR model ~ '{MODEL_SQL_PATTERN}'", name="model_shape"
        ),
        # An outcome exactly when the probe finished; an end exactly when it is
        # not running any more.
        CheckConstraint(
            "(state = 'finished') = (outcome IS NOT NULL)",
            name="outcome_matches_state",
        ),
        CheckConstraint(
            "(state = 'running') = (finished_at IS NULL)",
            name="finished_matches_state",
        ),
        CheckConstraint(
            "(finished_at IS NULL) = (duration_ms IS NULL)",
            name="duration_matches_finish",
        ),
        CheckConstraint(
            "failure_code IS NULL OR (outcome IS NOT NULL AND outcome <> 'connected')",
            name="failure_only_when_not_connected",
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
        CheckConstraint(
            "credential_version IS NULL OR credential_version >= 1",
            name="version_positive",
        ),
        # One running probe per credential (Decision 0100, 3 and 7).
        Index(
            "uq_connection_probes_running",
            "connection_id",
            "secret_handle",
            unique=True,
            postgresql_where=text("state = 'running'"),
        ),
        # The spacing of rechecks: a credential's latest probes.
        Index(
            "ix_connection_probes_connection_id_started_at",
            "connection_id",
            "started_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(Text)
    secret_handle: Mapped[str] = mapped_column(Text)
    credential_version: Mapped[int | None] = mapped_column(BigInteger)
    trigger: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    model: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    failure_code: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
