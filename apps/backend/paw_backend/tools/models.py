"""ORM models of the tool approvals (Alembic revision ``0031``).

``tool_approvals`` holds the current state of each approval request and
``tool_approval_events`` its append-only history (a trigger rejects UPDATE and
DELETE, like ``task_events``). ``task_id``, ``project_id``, ``agent_id`` and the
user ids are plain UUID columns without foreign keys, as in the task tables:
the users and projects tables do not exist yet, and an approval must stay
readable as evidence when a task is archived.

The invariants that make an approval trustworthy are CHECK constraints as well
as code: only the delegating user can be the approver, never the agent; an
approval is consumed only once it was decided; at most one open approval
exists per exact call.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
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
    # What the approver is shown: normalised paths / hosts / projects, not
    # content. [{"kind": "path", "value": "/srv/..."}]
    targets: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    approver_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
            "status <> 'pending' OR (approver_id IS NULL AND decided_at IS NULL)",
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
        # At most one open approval per exact call: a repeated request finds it.
        Index(
            "uq_tool_approvals_open_call",
            "call_hash",
            unique=True,
            postgresql_where=text("status IN ('pending', 'approved')"),
        ),
        Index("ix_tool_approvals_task_id", "task_id", "created_at"),
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

    __table_args__ = (
        _in("kind", ApprovalEventKind, "kind_valid"),
        CheckConstraint(
            "(kind IN ('approved', 'rejected')) = (actor_user_id IS NOT NULL)",
            name="user_matches_kind",
        ),
        CheckConstraint(
            "(kind IN ('requested', 'consumed')) = (agent_id IS NOT NULL)",
            name="agent_matches_kind",
        ),
        Index("ix_tool_approval_events_approval_id", "approval_id", "seq"),
    )
