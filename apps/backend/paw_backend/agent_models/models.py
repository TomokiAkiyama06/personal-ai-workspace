"""ORM models of the Admin's policy of the CLI agents' models (revision
``0341``; Decision 0103, 9 A; issue #341).

* ``agent_model_policies``: a kind's allowed models, one row each (the primary
  key ``(kind, model)``), with ``is_default`` on exactly one of them (a partial
  unique index allows at most one per kind; the service writes a kind's rows
  together, always with one default), the Admin's short ``description`` and who
  wrote them when (``updated_by`` a plain UUID, as the probes' ``actor_id``:
  the rows outlive the user). A kind without rows has no policy: the runtime
  configuration's model is its only allowed one (Decision 0103, 9 A).
* ``agent_model_policy_changes``: the history of the changes, append only (the
  application role may SELECT and INSERT, never UPDATE or DELETE): one row per
  allowed model of each kind a change wrote, the rows of one change sharing
  ``change_id`` (the correlation id of its ``admin.agent_models.update`` audit
  event, which holds no model name: Decision 0103, 7 A).

Timestamps have no default: the store writes the database's clock.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.agent_models.policy import MAX_DESCRIPTION_CHARS
from paw_backend.connections.domain import ConnectionKind
from paw_backend.connections.models import MODEL_SQL_PATTERN
from paw_backend.db import Base

POLICY_TABLE = "agent_model_policies"
CHANGE_TABLE = "agent_model_policy_changes"
_KINDS = ", ".join(f"'{kind.value}'" for kind in ConnectionKind)


def _shared() -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint(f"kind IN ({_KINDS})", name="kind_valid"),
        CheckConstraint(f"model ~ '{MODEL_SQL_PATTERN}'", name="model_shape"),
        CheckConstraint(
            "description IS NULL OR char_length(description) BETWEEN 1 AND "
            f"{MAX_DESCRIPTION_CHARS}",
            name="description_length",
        ),
    )


class AgentModelPolicyRow(Base):
    """One model a kind allows (see the module)."""

    __tablename__ = POLICY_TABLE
    __table_args__ = (
        *_shared(),
        Index(
            "uq_agent_model_policies_default",
            "kind",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    is_default: Mapped[bool] = mapped_column(Boolean)
    description: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentModelPolicyChangeRow(Base):
    """One allowed model of a kind as one change wrote it (append only)."""

    __tablename__ = CHANGE_TABLE
    __table_args__ = (
        *_shared(),
        Index("ix_agent_model_policy_changes_changed_at", "changed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    change_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean)
    description: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


__all__ = [
    "CHANGE_TABLE",
    "POLICY_TABLE",
    "AgentModelPolicyChangeRow",
    "AgentModelPolicyRow",
]
