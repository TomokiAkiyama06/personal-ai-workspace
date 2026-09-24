"""Tool approvals: requests, their state and an append-only history (PAW-031).

Revision ID: 0031
Revises: 0040
Create Date: 2026-09-24

``tool_approvals`` is the current state of each approval request;
``tool_approval_events`` is its history, which a trigger keeps append-only
(UPDATE and DELETE are rejected). The ids carry no foreign keys to users,
projects or tasks (see ``paw_backend/tools/models.py``).

Enum-like columns are text with CHECK constraints whose value lists are
written out here: a migration must not follow later changes of the Python
enums (change a list with a new revision).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("pending", "approved", "rejected", "consumed", "expired")
LEVELS = ("approval", "strong_approval")
EVENT_KINDS = ("requested", "approved", "rejected", "consumed", "expired")


def _in(column: str, values: Sequence[str], name: str) -> sa.CheckConstraint:
    listed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({listed})", name=op.f(name))


def upgrade() -> None:
    op.create_table(
        "tool_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("requester_user_id", sa.Uuid(), nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=24), nullable=False),
        sa.Column("call_hash", sa.String(length=64), nullable=False),
        sa.Column("targets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approver_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_approvals")),
        _in("status", STATUSES, "ck_tool_approvals_status_valid"),
        _in("level", LEVELS, "ck_tool_approvals_level_valid"),
        sa.CheckConstraint(
            "call_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_tool_approvals_call_hash_sha256"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_tool_approvals_expires_after_creation"),
        ),
        sa.CheckConstraint(
            "agent_id <> requester_user_id",
            name=op.f("ck_tool_approvals_agent_is_not_user"),
        ),
        sa.CheckConstraint(
            "approver_id IS NULL OR approver_id = requester_user_id",
            name=op.f("ck_tool_approvals_approver_is_delegating_user"),
        ),
        sa.CheckConstraint(
            "status <> 'pending' OR (approver_id IS NULL AND decided_at IS NULL)",
            name=op.f("ck_tool_approvals_pending_is_undecided"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('approved', 'rejected', 'consumed')"
            " OR (approver_id IS NOT NULL AND decided_at IS NOT NULL)",
            name=op.f("ck_tool_approvals_decision_has_approver"),
        ),
        sa.CheckConstraint(
            "(status = 'consumed') = (consumed_at IS NOT NULL)",
            name=op.f("ck_tool_approvals_consumed_matches_status"),
        ),
    )
    op.create_index(
        "uq_tool_approvals_open_call",
        "tool_approvals",
        ["call_hash"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'approved')"),
    )
    op.create_index(
        "ix_tool_approvals_task_id", "tool_approvals", ["task_id", "created_at"]
    )

    op.create_table(
        "tool_approval_events",
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_tool_approval_events")),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["tool_approvals.id"],
            name=op.f("fk_tool_approval_events_approval_id_tool_approvals"),
        ),
        _in("kind", EVENT_KINDS, "ck_tool_approval_events_kind_valid"),
        sa.CheckConstraint(
            "(kind IN ('approved', 'rejected')) = (actor_user_id IS NOT NULL)",
            name=op.f("ck_tool_approval_events_user_matches_kind"),
        ),
        sa.CheckConstraint(
            "(kind IN ('requested', 'consumed')) = (agent_id IS NOT NULL)",
            name=op.f("ck_tool_approval_events_agent_matches_kind"),
        ),
    )
    op.create_index(
        "ix_tool_approval_events_approval_id",
        "tool_approval_events",
        ["approval_id", "seq"],
    )

    # The history is append-only: not even the application role may rewrite it.
    op.execute(
        """
        CREATE FUNCTION tool_approval_events_reject_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'tool_approval_events is append-only'
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tool_approval_events_append_only
        BEFORE UPDATE OR DELETE ON tool_approval_events
        FOR EACH ROW EXECUTE FUNCTION tool_approval_events_reject_change()
        """
    )


def downgrade() -> None:
    # Dropping the table drops its trigger (DROP TABLE fires no row triggers).
    # Like the audit trail, the approval history is discarded: development and
    # test use only.
    op.drop_table("tool_approval_events")
    op.execute("DROP FUNCTION tool_approval_events_reject_change()")
    op.drop_table("tool_approvals")
