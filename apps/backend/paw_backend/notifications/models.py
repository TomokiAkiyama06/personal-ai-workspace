"""The stored notifications (issue #188, Alembic revision ``0188``).

* ``notifications``: one row per notification (``domain.NewNotification``): its
  key, kind, severity, category, params (codes and numbers, JSON) and audience,
  exactly one of ``recipient_user_id`` and ``audience_capability``.
  ``resolved_at`` is set when the producer's condition is over (System Health
  back to ``info``): the Notification Center no longer lists it.
* ``notification_receipts``: what one user did with one notification (read,
  dismissed), so that the state follows the user across devices. A
  notification of an audience has one receipt per reader, made on the first
  read / dismiss; no receipt means unread.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base
from paw_backend.notifications.domain import (
    KEY_MAX_CHARS,
    KIND_PATTERN,
    PARAMS_MAX_BYTES,
    Category,
    Severity,
)

TABLE_NAMES = ("notifications", "notification_receipts")


def _in(column: str, values, name: str) -> CheckConstraint:
    listed = ", ".join(f"'{member.value}'" for member in values)
    return CheckConstraint(f"{column} IN ({listed})", name=name)


class NotificationRow(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            f"char_length(key) BETWEEN 1 AND {KEY_MAX_CHARS}", name="key_length"
        ),
        CheckConstraint(f"kind ~ '{KIND_PATTERN}'", name="kind_shape"),
        _in("severity", Severity, "severity_valid"),
        _in("category", Category, "category_valid"),
        CheckConstraint(
            "(recipient_user_id IS NULL) <> (audience_capability IS NULL)",
            name="one_audience",
        ),
        CheckConstraint(
            "jsonb_typeof(params) = 'object'"
            f" AND octet_length(params::text) <= {PARAMS_MAX_BYTES * 2}",
            name="params_shape",
        ),
        # A user's own notifications, newest first; an audience's likewise.
        Index(
            "ix_notifications_recipient_created_at",
            "recipient_user_id",
            "created_at",
            postgresql_where=text("recipient_user_id IS NOT NULL"),
        ),
        Index(
            "ix_notifications_audience_created_at",
            "audience_capability",
            "created_at",
            postgresql_where=text("audience_capability IS NOT NULL"),
        ),
        # Resolving a key, dismissing an entry (its earlier notifications).
        Index("ix_notifications_key", "key"),
        # The purge by age.
        Index("ix_notifications_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    key: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    audience_capability: Mapped[str | None] = mapped_column(Text)
    # Context only (no foreign key): see the revision.
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    params: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationReceiptRow(Base):
    __tablename__ = "notification_receipts"
    __table_args__ = (
        CheckConstraint(
            "dismissed_at IS NULL OR read_at IS NOT NULL", name="dismissed_is_read"
        ),
        # A user's receipts (the erasure of their data).
        Index("ix_notification_receipts_user_id", "user_id"),
    )

    notification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
