"""Stored notifications and their per-user read / dismissed state (issue #188,
Decision 0070 Proposed).

Revision ID: 0188
Revises: 0066
Create Date: 2026-10-01

* ``notifications``: one row per notification: the aggregation key, the kind and
  params (codes and numbers only), the severity and category, the project it is
  about, and its audience: exactly one of ``recipient_user_id`` (one user) and
  ``audience_capability`` (the holders of a system-wide capability, decided when
  they read). ``resolved_at`` hides a notification whose condition is over.
  ``project_id`` is context only (no foreign key, like ``tasks.project_id``: a
  project's deletion does not have to wait for its notifications, which go with
  the retention purge).
* ``notification_receipts``: one user's read / dismissed time of one notification
  (no row: unread). A dismissed notification is also read.

Privileges of the application role (``PAW_APP_DATABASE_ROLE``): SELECT, INSERT,
UPDATE (``resolved_at`` only) and DELETE (the purge after the retention period)
on ``notifications``; SELECT, INSERT and UPDATE (``read_at``, ``dismissed_at``)
on ``notification_receipts``. A notification's content and audience cannot be
rewritten, and a receipt cannot be moved to another notification or user.

Both tables are new: nothing else is locked or rewritten.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from paw_backend.db_roles import grant_app_privileges

revision: str = "0188"
down_revision: str | Sequence[str] | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_notifications_migration.py`` compares them with the code.
KIND_PATTERN = r"^[a-z][a-z0-9_]{0,40}(\.[a-z][a-z0-9_]{0,40}){1,3}$"
KEY_MAX_CHARS = 200
PARAMS_MAX_TEXT_BYTES = 4000
SEVERITIES = ("info", "warning", "error", "critical")
CATEGORIES = ("task", "system")


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("recipient_user_id", sa.Uuid(), nullable=True),
        sa.Column("audience_capability", sa.Text(), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"char_length(key) BETWEEN 1 AND {KEY_MAX_CHARS}",
            name=op.f("ck_notifications_key_length"),
        ),
        sa.CheckConstraint(
            f"kind ~ '{KIND_PATTERN}'", name=op.f("ck_notifications_kind_shape")
        ),
        sa.CheckConstraint(
            f"severity IN ({_listed(SEVERITIES)})",
            name=op.f("ck_notifications_severity_valid"),
        ),
        sa.CheckConstraint(
            f"category IN ({_listed(CATEGORIES)})",
            name=op.f("ck_notifications_category_valid"),
        ),
        sa.CheckConstraint(
            "(recipient_user_id IS NULL) <> (audience_capability IS NULL)",
            name=op.f("ck_notifications_one_audience"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(params) = 'object'"
            f" AND octet_length(params::text) <= {PARAMS_MAX_TEXT_BYTES}",
            name=op.f("ck_notifications_params_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["recipient_user_id"],
            ["users.id"],
            name=op.f("fk_notifications_recipient_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index(
        "ix_notifications_recipient_created_at",
        "notifications",
        ["recipient_user_id", "created_at"],
        postgresql_where=sa.text("recipient_user_id IS NOT NULL"),
    )
    op.create_index(
        "ix_notifications_audience_created_at",
        "notifications",
        ["audience_capability", "created_at"],
        postgresql_where=sa.text("audience_capability IS NOT NULL"),
    )
    op.create_index("ix_notifications_key", "notifications", ["key"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])
    grant_app_privileges(
        op,
        "notifications",
        select=True,
        insert=True,
        delete=True,
        update_columns=("resolved_at",),
    )

    op.create_table(
        "notification_receipts",
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "dismissed_at IS NULL OR read_at IS NOT NULL",
            name=op.f("ck_notification_receipts_dismissed_is_read"),
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name=op.f("fk_notification_receipts_notification_id_notifications"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_receipts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "notification_id", "user_id", name=op.f("pk_notification_receipts")
        ),
    )
    op.create_index(
        "ix_notification_receipts_user_id", "notification_receipts", ["user_id"]
    )
    grant_app_privileges(
        op,
        "notification_receipts",
        select=True,
        insert=True,
        update_columns=("read_at", "dismissed_at"),
    )


def downgrade() -> None:
    op.drop_table("notification_receipts")
    op.drop_table("notifications")
