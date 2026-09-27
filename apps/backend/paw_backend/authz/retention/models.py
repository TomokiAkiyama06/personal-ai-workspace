"""ORM model of ``audit_retention_partitions`` (Migration 0086, Issue #86).

An ordinary, mutable bookkeeping table: **not** part of the append-only audit
trail itself (no reject-update/delete triggers, no ``recorded_at``-forcing
trigger). It is how ``AuditRetentionService`` knows which partitions of
``audit_events`` / ``audit_events_archive`` exist and where each one currently
is, without parsing PostgreSQL's own partition-bound catalog text. Only the
privileged role that runs migrations and retention maintenance ever touches it;
the application's low-privilege role (``PAW_APP_DATABASE_ROLE``) is granted
nothing on it (see Migration 0086 and Decision 0027).
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base


class AuditRetentionPartitionRecord(Base):
    """One row per partition of ``audit_events`` / ``audit_events_archive``.

    ``lower_bound`` is ``NULL`` only for ``audit_events_p_legacy`` (``FOR VALUES
    FROM (MINVALUE)``). ``status`` moves ``live -> archived -> purged`` and never
    backwards (enforced by ``rules.py`` and ``service.py``, not by a database
    trigger: unlike ``audit_events``, nothing here needs to survive a
    compromised application role, since it carries no audit content itself).
    """

    __tablename__ = "audit_retention_partitions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('live', 'archived', 'purged')", name="status_valid"
        ),
    )

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    lower_bound: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upper_bound: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
