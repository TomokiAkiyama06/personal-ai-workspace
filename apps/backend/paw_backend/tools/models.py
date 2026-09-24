"""ORM models of the tool approvals (Alembic revision ``0031``).

``tool_approvals`` holds the current state of each approval request and
``tool_approval_events`` its append-only history (a trigger rejects UPDATE and
DELETE, like ``task_events``). ``task_id``, ``project_id``, ``agent_id`` and the
user ids are plain UUID columns without foreign keys, as in the task tables:
the users and projects tables do not exist yet, and an approval must stay
readable as evidence when a task is archived.

The invariants that make an approval trustworthy are enforced by the database
as well as by code (migration ``0031``): CHECK constraints (only the delegating
user can be the approver, never the agent; a strong approval carries a
step-up; at most one open approval exists per exact call) and triggers that
allow only the legal state changes (``pending`` -> ``approved`` / ``rejected`` /
``revoked`` / ``expired``, ``approved`` -> ``consumed`` / ``revoked`` /
``expired``), keep everything that identifies the call immutable, and refuse
DELETE and TRUNCATE. Triggers are not visible to Alembic's autogenerate, so
``tests/test_tools_migration.py`` checks them.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base
from paw_backend.tools.approval_types import (
    ApprovalEventKind,
    ApprovalStatus,
)

TABLE_NAMES = ("tool_approvals", "tool_approval_events")
LEVEL_VALUES = ("approval", "strong_approval")


def _in(column: str, values: type[StrEnum] | tuple[str, ...], name: str):
    members = [m.value for m in values] if isinstance(values, type) else values
    listed = ", ".join(f"'{value}'" for value in members)
    return CheckConstraint(f"{column} IN ({listed})", name=name)


class ToolApprovalRow(Base):
    __tablename__ = "tool_approvals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # The agent that asked, and the human user it acts for (the only one who
    # may approve).
    agent_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    requester_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    tool: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(24))
    # SHA-256 of tool + normalised arguments + task + requester.
    call_hash: Mapped[str] = mapped_column(String(64))
    # The typed targets of the call: normalised paths / hosts / projects.
    # [{"kind": "path", "value": "/srv/..."}]
    targets: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    # What the approver is shown: every argument by name with a bounded,
    # redacted value. [{"name": "package", "kind": "text", "value": "..."}]
    summary: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    approver_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A strong approval was granted after a confirmed step-up (PAW-023).
    step_up_verified: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL with ``revoked_at`` set: revoked by the system (the task ended).
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    __table_args__ = (
        _in("status", ApprovalStatus, "status_valid"),
        _in("level", LEVEL_VALUES, "level_valid"),
        CheckConstraint("call_hash ~ '^[0-9a-f]{64}$'", name="call_hash_sha256"),
        CheckConstraint("expires_at > created_at", name="expires_after_creation"),
        CheckConstraint("agent_id <> requester_user_id", name="agent_is_not_user"),
        # Only the delegating user decides: not the agent, not anybody else.
        CheckConstraint(
            "approver_id IS NULL OR approver_id = requester_user_id",
            name="approver_is_delegating_user",
        ),
        CheckConstraint(
            "status <> 'pending' OR (approver_id IS NULL AND decided_at IS NULL"
            " AND NOT step_up_verified)",
            name="pending_is_undecided",
        ),
        CheckConstraint(
            "status NOT IN ('approved', 'rejected', 'consumed')"
            " OR (approver_id IS NOT NULL AND decided_at IS NOT NULL)",
            name="decision_has_approver",
        ),
        CheckConstraint(
            "(status = 'consumed') = (consumed_at IS NOT NULL)",
            name="consumed_matches_status",
        ),
        CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL)"
            " AND (revoked_by IS NULL OR revoked_at IS NOT NULL)",
            name="revoked_matches_status",
        ),
        # A strong approval is granted only with a step-up.
        CheckConstraint(
            "level <> 'strong_approval' OR status NOT IN ('approved', 'consumed')"
            " OR step_up_verified",
            name="strong_needs_step_up",
        ),
        CheckConstraint(
            "jsonb_typeof(summary) = 'array'"
            " AND jsonb_array_length(summary) BETWEEN 1 AND 16",
            name="summary_shape",
        ),
        # At most one open approval per exact call: a repeated request finds it.
        Index(
            "uq_tool_approvals_open_call",
            "call_hash",
            unique=True,
            postgresql_where=text("status IN ('pending', 'approved')"),
        ),
        Index("ix_tool_approvals_task_id", "task_id", "created_at"),
        # The cooldown after a rejection looks calls up by their hash.
        Index("ix_tool_approvals_call_hash", "call_hash", "status"),
    )


class ToolApprovalEventRow(Base):
    __tablename__ = "tool_approval_events"

    seq: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tool_approvals.id"))
    kind: Mapped[str] = mapped_column(String(24))
    # The human who approved / rejected; or the agent that requested / used it.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # What was requested (the ``requested`` row only).
    summary: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)

    __table_args__ = (
        _in("kind", ApprovalEventKind, "kind_valid"),
        # A human decides (approved / rejected); a revocation is by a human or
        # by the system; every other change has no human actor.
        CheckConstraint(
            "kind = 'revoked' OR (kind IN ('approved', 'rejected'))"
            " = (actor_user_id IS NOT NULL)",
            name="user_matches_kind",
        ),
        CheckConstraint(
            "(kind = 'requested') = (summary IS NOT NULL)",
            name="summary_matches_kind",
        ),
        CheckConstraint(
            "(kind IN ('requested', 'consumed')) = (agent_id IS NOT NULL)",
            name="agent_matches_kind",
        ),
        Index("ix_tool_approval_events_approval_id", "approval_id", "seq"),
    )
