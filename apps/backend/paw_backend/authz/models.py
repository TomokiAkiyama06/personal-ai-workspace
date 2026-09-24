"""ORM model of the append-only ``audit_events`` table (migration ``0025``).

Rows are only ever inserted. UPDATE, DELETE and TRUNCATE are rejected by
triggers created in the migration, so no code path (including a bug in this
application) can rewrite or drop the history through SQL DML.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base


class AuditEventRecord(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("decision IN ('allow', 'deny')", name="decision_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Who (a user, or the user an agent acts for) and in which role.
    actor_id: Mapped[str | None] = mapped_column(Text)
    actor_role: Mapped[str | None] = mapped_column(Text)
    # Set when an agent performed the action on behalf of ``actor_id``.
    agent_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    resource_kind: Mapped[str] = mapped_column(Text)
    resource_id: Mapped[str | None] = mapped_column(Text)
    project_id: Mapped[str | None] = mapped_column(Text)
    repo_id: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
