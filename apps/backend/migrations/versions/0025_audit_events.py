"""Append-only audit_events table (PAW-025).

Revision ID: 0025
Revises: 0001
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rows can only be appended: UPDATE, DELETE and TRUNCATE raise
# restrict_violation (SQLSTATE 23001). ENABLE ALWAYS keeps the triggers firing
# even when a session sets session_replication_role = replica. The table owner
# can still drop them; production should run the application as a role that
# only holds INSERT and SELECT (see apps/backend/README.md).
_FUNCTION = """
CREATE FUNCTION paw_reject_audit_events_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("actor_role", sa.Text(), nullable=True),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_kind", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=True),
        sa.Column("project_id", sa.Text(), nullable=True),
        sa.Column("repo_id", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "decision IN ('allow', 'deny')",
            name=op.f("ck_audit_events_decision_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        op.f("ix_audit_events_occurred_at"), "audit_events", ["occurred_at"]
    )
    op.execute(_FUNCTION)
    op.execute(
        "CREATE TRIGGER tr_audit_events_reject_update_delete "
        "BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION paw_reject_audit_events_change()"
    )
    op.execute(
        "CREATE TRIGGER tr_audit_events_reject_truncate "
        "BEFORE TRUNCATE ON audit_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION paw_reject_audit_events_change()"
    )
    op.execute(
        "ALTER TABLE audit_events ENABLE ALWAYS TRIGGER "
        "tr_audit_events_reject_update_delete"
    )
    op.execute(
        "ALTER TABLE audit_events ENABLE ALWAYS TRIGGER tr_audit_events_reject_truncate"
    )


def downgrade() -> None:
    # Dropping the table removes its triggers; the audit history goes with it.
    op.drop_table("audit_events")
    op.execute("DROP FUNCTION paw_reject_audit_events_change()")
