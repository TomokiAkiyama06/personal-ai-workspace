"""Task-scoped approval grants: "このタスクの間は許可" (Decision 0085).

Revision ID: 0193
Revises: 0191
Create Date: 2026-10-08

* ``tool_approvals.grant_pattern`` (JSONB, NULL allowed): how the approved call
  may be granted for the rest of its task (``tools/grant_pattern.py``), written by
  the broker when it opens the approval; NULL means it never can be. It is part
  of what identifies the call: the update trigger now refuses to change it (the
  function is replaced with the same body plus this column) and the application
  role gets no UPDATE on it.
* ``tool_task_grants``: one grant per approval it was made from (unique), bound
  to the task's run, agent, user and tool; ``active`` or ``revoked``. Triggers: a
  row is born ``active`` and unrevoked; the only change is ``active`` ->
  ``revoked`` (setting ``revoked_at`` and, for a person, ``revoked_by``), and
  everything that identifies the grant is immutable; DELETE and TRUNCATE are
  refused. CHECK: the agent is not the user; a revocation has its time.
* ``tool_task_grant_uses``: one row per call a grant let run (the grant, the
  call's hash, the correlation id of the broker's decision, the run, the time),
  append-only.

Every trigger is ``ENABLE ALWAYS`` (as for ``tool_approvals``). Privileges of the
application role: SELECT, INSERT and UPDATE of the state columns (``status``,
``revoked_at``, ``revoked_by``) on ``tool_task_grants``; SELECT and INSERT on
``tool_task_grant_uses``. Like the approvals, the ids carry no foreign keys to
users, projects or tasks (evidence stays readable when a task is archived).

The new tables are empty and the new column is NULL for every existing approval
(an approval opened before this revision is never granted for a task); adding a
nullable column takes a short ACCESS EXCLUSIVE lock on ``tool_approvals`` and
rewrites nothing. ``paw_compatibility`` is ``expand`` (Decision 0079 4): a release
without this revision runs on a schema with it (it never names the column or the
new tables, its inserts leave the column NULL and the replaced trigger function
accepts every change the old one did), so rolling the application back does not
need the database restored.
``downgrade()`` drops them and DESTROYS THE GRANT HISTORY (development and test
only).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from paw_backend.db_roles import grant_app_privileges

revision: str = "0193"
down_revision: str | Sequence[str] | None = "0191"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Decision 0079 4: what an older release can do with this schema.
paw_compatibility: str = "expand"

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_task_grants_postgres.py`` compares them with the code.
STATUSES = ("active", "revoked")

# Migration 0031's function with ``grant_pattern`` added to what cannot change.
_APPROVALS_UPDATE_FUNCTION = """
CREATE OR REPLACE FUNCTION tool_approvals_check_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.task_id IS DISTINCT FROM OLD.task_id
       OR NEW.task_attempt IS DISTINCT FROM OLD.task_attempt
       OR NEW.task_retry_count IS DISTINCT FROM OLD.task_retry_count
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
       OR NEW.requester_user_id IS DISTINCT FROM OLD.requester_user_id
       OR NEW.tool IS DISTINCT FROM OLD.tool
       OR NEW.level IS DISTINCT FROM OLD.level
       OR NEW.call_hash IS DISTINCT FROM OLD.call_hash
       OR NEW.targets IS DISTINCT FROM OLD.targets
       OR NEW.summary IS DISTINCT FROM OLD.summary
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.grant_pattern IS DISTINCT FROM OLD.grant_pattern THEN
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

# Migration 0031's function as it was (``downgrade``).
_APPROVALS_UPDATE_FUNCTION_0031 = _APPROVALS_UPDATE_FUNCTION.replace(
    """       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.grant_pattern IS DISTINCT FROM OLD.grant_pattern THEN""",
    """       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN""",
)

_GRANTS_INSERT_FUNCTION = """
CREATE FUNCTION tool_task_grants_check_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status <> 'active'
       OR NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL THEN
        RAISE EXCEPTION 'a task grant is created active'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

_GRANTS_UPDATE_FUNCTION = """
CREATE FUNCTION tool_task_grants_check_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.approval_id IS DISTINCT FROM OLD.approval_id
       OR NEW.task_id IS DISTINCT FROM OLD.task_id
       OR NEW.task_attempt IS DISTINCT FROM OLD.task_attempt
       OR NEW.task_retry_count IS DISTINCT FROM OLD.task_retry_count
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
       OR NEW.requester_user_id IS DISTINCT FROM OLD.requester_user_id
       OR NEW.tool IS DISTINCT FROM OLD.tool
       OR NEW.pattern IS DISTINCT FROM OLD.pattern
       OR NEW.summary IS DISTINCT FROM OLD.summary
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'what a task grant covers cannot change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NOT (OLD.status = 'active' AND NEW.status = 'revoked') THEN
        RAISE EXCEPTION 'this task grant state change is not allowed'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

_REJECT_GRANT_REMOVAL_FUNCTION = """
CREATE FUNCTION tool_task_grants_reject_removal() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'tool_task_grants rows are kept as evidence'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

_REJECT_USE_CHANGE_FUNCTION = """
CREATE FUNCTION tool_task_grant_uses_reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'tool_task_grant_uses is append-only'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

_FUNCTIONS = (
    ("tool_task_grants_check_insert", _GRANTS_INSERT_FUNCTION),
    ("tool_task_grants_check_update", _GRANTS_UPDATE_FUNCTION),
    ("tool_task_grants_reject_removal", _REJECT_GRANT_REMOVAL_FUNCTION),
    ("tool_task_grant_uses_reject_change", _REJECT_USE_CHANGE_FUNCTION),
)

_TRIGGERS = (
    (
        "tool_task_grants",
        "tool_task_grants_created_active",
        "BEFORE INSERT ON tool_task_grants FOR EACH ROW "
        "EXECUTE FUNCTION tool_task_grants_check_insert()",
    ),
    (
        "tool_task_grants",
        "tool_task_grants_state_machine",
        "BEFORE UPDATE ON tool_task_grants FOR EACH ROW "
        "EXECUTE FUNCTION tool_task_grants_check_update()",
    ),
    (
        "tool_task_grants",
        "tool_task_grants_no_delete",
        "BEFORE DELETE ON tool_task_grants FOR EACH ROW "
        "EXECUTE FUNCTION tool_task_grants_reject_removal()",
    ),
    (
        "tool_task_grants",
        "tool_task_grants_no_truncate",
        "BEFORE TRUNCATE ON tool_task_grants FOR EACH STATEMENT "
        "EXECUTE FUNCTION tool_task_grants_reject_removal()",
    ),
    (
        "tool_task_grant_uses",
        "tool_task_grant_uses_append_only",
        "BEFORE UPDATE OR DELETE ON tool_task_grant_uses FOR EACH ROW "
        "EXECUTE FUNCTION tool_task_grant_uses_reject_change()",
    ),
    (
        "tool_task_grant_uses",
        "tool_task_grant_uses_no_truncate",
        "BEFORE TRUNCATE ON tool_task_grant_uses FOR EACH STATEMENT "
        "EXECUTE FUNCTION tool_task_grant_uses_reject_change()",
    ),
)


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.add_column(
        "tool_approvals",
        sa.Column(
            "grant_pattern", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.create_check_constraint(
        op.f("ck_tool_approvals_grant_pattern_only_approval"),
        "tool_approvals",
        "grant_pattern IS NULL"
        " OR (level = 'approval' AND jsonb_typeof(grant_pattern) = 'object')",
    )
    op.execute(_APPROVALS_UPDATE_FUNCTION)

    op.create_table(
        "tool_task_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("task_attempt", sa.Integer(), nullable=False),
        sa.Column("task_retry_count", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("requester_user_id", sa.Uuid(), nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("pattern", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_task_grants")),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["tool_approvals.id"],
            name=op.f("fk_tool_task_grants_approval_id_tool_approvals"),
        ),
        sa.UniqueConstraint(
            "approval_id", name=op.f("uq_tool_task_grants_approval_id")
        ),
        sa.CheckConstraint(
            f"status IN ({_listed(STATUSES)})",
            name=op.f("ck_tool_task_grants_status_valid"),
        ),
        sa.CheckConstraint(
            "agent_id <> requester_user_id",
            name=op.f("ck_tool_task_grants_agent_is_not_user"),
        ),
        sa.CheckConstraint(
            "task_attempt >= 1", name=op.f("ck_tool_task_grants_task_attempt_positive")
        ),
        sa.CheckConstraint(
            "task_retry_count >= 0",
            name=op.f("ck_tool_task_grants_task_retry_count_not_negative"),
        ),
        sa.CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL)"
            " AND (revoked_by IS NULL OR revoked_at IS NOT NULL)",
            name=op.f("ck_tool_task_grants_revoked_matches_status"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(pattern) = 'object'",
            name=op.f("ck_tool_task_grants_pattern_shape"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(summary) = 'array'"
            " AND jsonb_array_length(summary) BETWEEN 1 AND 16",
            name=op.f("ck_tool_task_grants_summary_shape"),
        ),
    )
    op.create_index(
        "ix_tool_task_grants_task_id",
        "tool_task_grants",
        ["task_id", "requester_user_id", "created_at"],
    )

    op.create_table(
        "tool_task_grant_uses",
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("call_hash", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("task_attempt", sa.Integer(), nullable=False),
        sa.Column("task_retry_count", sa.Integer(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_tool_task_grant_uses")),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["tool_task_grants.id"],
            name=op.f("fk_tool_task_grant_uses_grant_id_tool_task_grants"),
        ),
        sa.CheckConstraint(
            "call_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_tool_task_grant_uses_call_hash_sha256"),
        ),
    )
    op.create_index(
        "ix_tool_task_grant_uses_grant_id",
        "tool_task_grant_uses",
        ["grant_id", "seq"],
    )

    for _name, function in _FUNCTIONS:
        op.execute(function)
    for table, name, definition in _TRIGGERS:
        op.execute(f"CREATE TRIGGER {name} {definition}")
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}")

    _grant_app_privileges()


def downgrade() -> None:
    # Dropping a table drops its triggers and grants. DESTROYS THE GRANT HISTORY
    # (development and test only).
    op.drop_table("tool_task_grant_uses")
    op.drop_table("tool_task_grants")
    for name, _function in _FUNCTIONS:
        op.execute(f"DROP FUNCTION {name}()")
    op.execute(_APPROVALS_UPDATE_FUNCTION_0031)
    op.drop_constraint(
        op.f("ck_tool_approvals_grant_pattern_only_approval"),
        "tool_approvals",
        type_="check",
    )
    op.drop_column("tool_approvals", "grant_pattern")


# ---------------------------------------------------------------------------
# GRANTS: least privilege for the application role, through the shared
# ``paw_backend.db_roles.grant_app_privileges`` helper (PAW-025):
#
#   tool_task_grants      SELECT, INSERT, and UPDATE of the state columns only
#                         (status, revoked_at, revoked_by); no DELETE, no
#                         TRUNCATE
#   tool_task_grant_uses  SELECT, INSERT; no UPDATE, no DELETE, no TRUNCATE
#
# ``tool_approvals`` keeps its grants (migration 0031): ``grant_pattern`` is not
# among the columns the application role may update.
# ---------------------------------------------------------------------------
_APP_UPDATE_COLUMNS = ("status", "revoked_at", "revoked_by")


def _grant_app_privileges() -> None:
    grant_app_privileges(
        op, "tool_task_grants", insert=True, update_columns=_APP_UPDATE_COLUMNS
    )
    grant_app_privileges(op, "tool_task_grant_uses", insert=True)
