"""Tool approvals: requests, their state and an append-only history (PAW-031).

Revision ID: 0031
Revises: 0040
Create Date: 2026-09-24

``tool_approvals`` is the current state of each approval request;
``tool_approval_events`` is its history. The ids carry no foreign keys to users,
projects or tasks (see ``paw_backend/tools/models.py``).

The database enforces the rules an approval stands on, so that a bug (or a
compromise) of the application cannot approve its own request, replay a used
approval or rewrite what was approved:

* CHECK constraints: only the delegating user can be the approver (never the
  agent); a strong approval carries a step-up; a summary the approver can read;
  at most one open approval per exact call (partial unique index);
* a BEFORE INSERT trigger: a row is born ``pending`` and undecided;
* a BEFORE UPDATE trigger: the only legal changes are ``pending`` ->
  ``approved`` / ``rejected`` / ``revoked`` / ``expired`` and ``approved`` ->
  ``consumed`` / ``revoked`` / ``expired``, each setting only its own columns;
  everything that identifies the call (ids, tool, level, ``call_hash``,
  targets, summary, ``created_at``, ``expires_at``) is immutable;
* DELETE and TRUNCATE are refused on both tables, and UPDATE / DELETE /
  TRUNCATE on the history. Every trigger is ``ENABLE ALWAYS``: they also fire
  under ``session_replication_role = replica`` (as for ``audit_events``);
* privileges (the marked block at the end): ``PUBLIC`` gets nothing, and the
  application role gets SELECT + INSERT and UPDATE of the state columns only.

What is NOT guaranteed: the role that owns the tables (the migration role) and
superusers can disable triggers; a role that may run the legal transitions can
still run them (the application process that exposes approving must be the
authenticated approval endpoint: the agent-facing process should not hold this
role's credentials, see the README section on the split); the expiry is
compared against the application's clock, not the database's.

Enum-like columns are text with CHECK constraints whose value lists are
written out here: a migration must not follow later changes of the Python
enums (change a list with a new revision).

``downgrade()`` drops the tables and DESTROYS THE APPROVAL HISTORY: development
and test use only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from paw_backend.db_roles import grant_app_privileges

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("pending", "approved", "rejected", "consumed", "revoked", "expired")
LEVELS = ("approval", "strong_approval")
EVENT_KINDS = ("requested", "approved", "rejected", "consumed", "revoked", "expired")

_INSERT_FUNCTION = """
CREATE FUNCTION tool_approvals_check_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status <> 'pending'
       OR NEW.approver_id IS NOT NULL OR NEW.decided_at IS NOT NULL
       OR NEW.consumed_at IS NOT NULL OR NEW.revoked_at IS NOT NULL
       OR NEW.revoked_by IS NOT NULL OR NEW.step_up_verified THEN
        RAISE EXCEPTION 'a tool approval is created pending and undecided'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

_UPDATE_FUNCTION = """
CREATE FUNCTION tool_approvals_check_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.task_id IS DISTINCT FROM OLD.task_id
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
       OR NEW.requester_user_id IS DISTINCT FROM OLD.requester_user_id
       OR NEW.tool IS DISTINCT FROM OLD.tool
       OR NEW.level IS DISTINCT FROM OLD.level
       OR NEW.call_hash IS DISTINCT FROM OLD.call_hash
       OR NEW.targets IS DISTINCT FROM OLD.targets
       OR NEW.summary IS DISTINCT FROM OLD.summary
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION 'what a tool approval was granted for cannot change'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NOT ((OLD.status = 'pending'
             AND NEW.status IN ('approved', 'rejected', 'revoked', 'expired'))
            OR (OLD.status = 'approved'
                AND NEW.status IN ('consumed', 'revoked', 'expired'))) THEN
        RAISE EXCEPTION 'this tool approval state change is not allowed'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Each change sets its own columns and leaves the others as they were.
    IF NEW.status IN ('approved', 'rejected') THEN
        IF NEW.consumed_at IS NOT NULL OR NEW.revoked_at IS NOT NULL
           OR NEW.revoked_by IS NOT NULL
           OR (NEW.status = 'rejected' AND NEW.step_up_verified) THEN
            RAISE EXCEPTION 'this tool approval change sets other columns'
                USING ERRCODE = 'restrict_violation';
        END IF;
    ELSIF NEW.status = 'consumed' THEN
        IF NEW.approver_id IS DISTINCT FROM OLD.approver_id
           OR NEW.decided_at IS DISTINCT FROM OLD.decided_at
           OR NEW.step_up_verified IS DISTINCT FROM OLD.step_up_verified
           OR NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL THEN
            RAISE EXCEPTION 'this tool approval change sets other columns'
                USING ERRCODE = 'restrict_violation';
        END IF;
    ELSIF NEW.status = 'revoked' THEN
        IF NEW.approver_id IS DISTINCT FROM OLD.approver_id
           OR NEW.decided_at IS DISTINCT FROM OLD.decided_at
           OR NEW.step_up_verified IS DISTINCT FROM OLD.step_up_verified
           OR NEW.consumed_at IS NOT NULL THEN
            RAISE EXCEPTION 'this tool approval change sets other columns'
                USING ERRCODE = 'restrict_violation';
        END IF;
    ELSE  -- expired
        IF NEW.approver_id IS DISTINCT FROM OLD.approver_id
           OR NEW.decided_at IS DISTINCT FROM OLD.decided_at
           OR NEW.step_up_verified IS DISTINCT FROM OLD.step_up_verified
           OR NEW.consumed_at IS NOT NULL
           OR NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL THEN
            RAISE EXCEPTION 'this tool approval change sets other columns'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

_REJECT_APPROVALS_FUNCTION = """
CREATE FUNCTION tool_approvals_reject_removal() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'tool_approvals rows are kept as evidence'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

_REJECT_EVENTS_FUNCTION = """
CREATE FUNCTION tool_approval_events_reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'tool_approval_events is append-only'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

_TRIGGERS = (
    (
        "tool_approvals",
        "tool_approvals_created_pending",
        "BEFORE INSERT ON tool_approvals FOR EACH ROW "
        "EXECUTE FUNCTION tool_approvals_check_insert()",
    ),
    (
        "tool_approvals",
        "tool_approvals_state_machine",
        "BEFORE UPDATE ON tool_approvals FOR EACH ROW "
        "EXECUTE FUNCTION tool_approvals_check_update()",
    ),
    (
        "tool_approvals",
        "tool_approvals_no_delete",
        "BEFORE DELETE ON tool_approvals FOR EACH ROW "
        "EXECUTE FUNCTION tool_approvals_reject_removal()",
    ),
    (
        "tool_approvals",
        "tool_approvals_no_truncate",
        "BEFORE TRUNCATE ON tool_approvals FOR EACH STATEMENT "
        "EXECUTE FUNCTION tool_approvals_reject_removal()",
    ),
    (
        "tool_approval_events",
        "tool_approval_events_append_only",
        "BEFORE UPDATE OR DELETE ON tool_approval_events FOR EACH ROW "
        "EXECUTE FUNCTION tool_approval_events_reject_change()",
    ),
    (
        "tool_approval_events",
        "tool_approval_events_no_truncate",
        "BEFORE TRUNCATE ON tool_approval_events FOR EACH STATEMENT "
        "EXECUTE FUNCTION tool_approval_events_reject_change()",
    ),
)


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
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approver_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "step_up_verified",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Uuid(), nullable=True),
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
            "status <> 'pending' OR (approver_id IS NULL AND decided_at IS NULL"
            " AND NOT step_up_verified)",
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
        sa.CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL)"
            " AND (revoked_by IS NULL OR revoked_at IS NOT NULL)",
            name=op.f("ck_tool_approvals_revoked_matches_status"),
        ),
        sa.CheckConstraint(
            "level <> 'strong_approval' OR status NOT IN ('approved', 'consumed')"
            " OR step_up_verified",
            name=op.f("ck_tool_approvals_strong_needs_step_up"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(summary) = 'array'"
            " AND jsonb_array_length(summary) BETWEEN 1 AND 16",
            name=op.f("ck_tool_approvals_summary_shape"),
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
    op.create_index(
        "ix_tool_approvals_call_hash", "tool_approvals", ["call_hash", "status"]
    )

    op.create_table(
        "tool_approval_events",
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_tool_approval_events")),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["tool_approvals.id"],
            name=op.f("fk_tool_approval_events_approval_id_tool_approvals"),
        ),
        _in("kind", EVENT_KINDS, "ck_tool_approval_events_kind_valid"),
        sa.CheckConstraint(
            "kind = 'revoked' OR (kind IN ('approved', 'rejected'))"
            " = (actor_user_id IS NOT NULL)",
            name=op.f("ck_tool_approval_events_user_matches_kind"),
        ),
        sa.CheckConstraint(
            "(kind IN ('requested', 'consumed')) = (agent_id IS NOT NULL)",
            name=op.f("ck_tool_approval_events_agent_matches_kind"),
        ),
        sa.CheckConstraint(
            "(kind = 'requested') = (summary IS NOT NULL)",
            name=op.f("ck_tool_approval_events_summary_matches_kind"),
        ),
    )
    op.create_index(
        "ix_tool_approval_events_approval_id",
        "tool_approval_events",
        ["approval_id", "seq"],
    )

    # The state machine and the append-only guard. ENABLE ALWAYS: they also
    # fire under session_replication_role = replica.
    for function in (
        _INSERT_FUNCTION,
        _UPDATE_FUNCTION,
        _REJECT_APPROVALS_FUNCTION,
        _REJECT_EVENTS_FUNCTION,
    ):
        op.execute(function)
    for table, name, definition in _TRIGGERS:
        op.execute(f"CREATE TRIGGER {name} {definition}")
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}")

    _grant_app_privileges()


def downgrade() -> None:
    # Dropping a table drops its triggers and grants (DROP TABLE fires no row
    # triggers). DESTROYS THE APPROVAL HISTORY (development and test only).
    op.drop_table("tool_approval_events")
    op.drop_table("tool_approvals")
    for function in (
        "tool_approvals_check_insert",
        "tool_approvals_check_update",
        "tool_approvals_reject_removal",
        "tool_approval_events_reject_change",
    ):
        op.execute(f"DROP FUNCTION {function}()")


# ---------------------------------------------------------------------------
# GRANTS: least privilege for the application role, through the shared
# ``paw_backend.db_roles.grant_app_privileges`` helper (PAW-025):
#
#   tool_approvals        SELECT, INSERT, and UPDATE of the state columns only
#                         (status, approver_id, decided_at, consumed_at,
#                         step_up_verified, revoked_at, revoked_by); no
#                         DELETE, no TRUNCATE
#   tool_approval_events  SELECT, INSERT; no UPDATE, no DELETE, no TRUNCATE
#
# ``PUBLIC`` gets nothing. The state columns are the only ones that ever
# change; the trigger above additionally limits *which* changes are legal.
# ---------------------------------------------------------------------------
_APP_UPDATE_COLUMNS = (
    "status",
    "approver_id",
    "decided_at",
    "consumed_at",
    "step_up_verified",
    "revoked_at",
    "revoked_by",
)


def _grant_app_privileges() -> None:
    grant_app_privileges(
        op, "tool_approvals", insert=True, update_columns=_APP_UPDATE_COLUMNS
    )
    grant_app_privileges(op, "tool_approval_events", insert=True)
