"""ORM model of the append-only ``audit_events`` table (migration ``0025``).

Rows are only ever inserted. UPDATE, DELETE and TRUNCATE are rejected by
triggers created in the migration, and when the application runs as a
separate database role that only holds INSERT and SELECT it cannot remove the
triggers either (see ``apps/backend/README.md`` for exactly what this does and
does not guarantee).
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base


class AuditEventRecord(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("decision IN ('allow', 'deny')", name="decision_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    # Server-generated, shared by the decisions of one request.
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # The application's clock (when the decision was made) ...
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # ... and the database's clock (when the row was stored), which the
    # application cannot choose.
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    # Who (a user, or the user an agent acts for) and in which role.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor_role: Mapped[str | None] = mapped_column(Text)
    # Set when an agent performed the action on behalf of ``actor_id``.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(Text)
    resource_kind: Mapped[str] = mapped_column(Text)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    decision: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    # The X-Request-ID the *client* sent (validated, at most 64 characters). It
    # is only a hint for correlating with client logs; it can be forged, so use
    # ``correlation_id`` to tie rows together.
    client_request_id: Mapped[str | None] = mapped_column(Text)
