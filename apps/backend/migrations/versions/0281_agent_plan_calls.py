"""The calls of the planner and where they ran (issue #297, Decision 0099, 3).

Revision ID: 0281
Revises: 0280
Create Date: 2026-10-24

Decision 0099 (approved 2026-10-23) lets the planner run on the Claude Code
CLI. A send to the cloud is recorded before it happens (Decision 0048, 4), but
the planner's call has no DAG and no attempt row to record it on (the DAG is
made from the plan it returns). This revision adds the record:

* ``agent_plan_calls``: one row per call of the planner, made in the
  transaction that starts the call (the task running in this run, the call's
  step charged, the worker's queue lease proved): the task, its run
  (``task_attempt`` and ``retry_count``: a Retry changes only the count), the
  call's number within the run (``seq``, the largest of the run plus one, given
  in that transaction: a Retry, or a worker that took the run over, never
  collides with an earlier call), its number within the claim that made it
  (``claim_call``, 1 to 5: the rung of the planner ladder) and the worker that
  made it (``owner``);
* the seven placement columns of revision 0133, with the same CHECK
  constraints (the place, agent, model and time all set or all NULL;
  identifiers only; for the cloud the fingerprint and size of what was sent and
  the id of the ``audit_events`` row of the send, written in the same
  transaction). A row whose placement stays NULL is a call that started but was
  never placed (its run was replaced or its worker lost the lease first): it is
  kept, never rewritten;
* ``UNIQUE (task_id, task_attempt, retry_count, seq)``.

A placement is recorded once: the trigger ``tr_agent_plan_calls_placement_once``
refuses any change of the seven columns once ``placement`` is set, whatever
role writes (``ENABLE ALWAYS``, as for the attempts).

Privileges of the application role (``PAW_APP_DATABASE_ROLE``): SELECT, INSERT
and UPDATE of the seven placement columns only. No DELETE: the record of the
calls is kept (as the DAG's tables, Decision 0021, 8).

``paw_compatibility`` is ``expand`` (Decision 0079, 4): an older release never
names the table (and never sends a planner call to the cloud).
``downgrade()`` drops the table, its trigger and its function: it DESTROYS the
record of the planner's calls (the ``audit_events`` rows of the sends stay).
Development and test databases only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0281"
down_revision: str | Sequence[str] | None = "0280"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Decision 0079 4: what an older release can do with this schema.
paw_compatibility: str = "expand"

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_plan_calls_migration.py`` compares them with the code.
TABLE = "agent_plan_calls"
PLACEMENTS = ("local_gpu", "local_cpu", "cloud")
AGENT_LABEL_PATTERN = "[a-z][a-z0-9._-]{0,63}"
MAX_MODEL_CHARS = 128
MODEL_PATTERN = "[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}"
MAX_PLAN_ATTEMPTS = 5
PLACEMENT_COLUMNS = (
    "placement",
    "placement_agent",
    "placement_model",
    "placed_at",
    "content_fingerprint",
    "content_bytes",
    "placement_audit_id",
)

_FUNCTION = """
CREATE FUNCTION paw_keep_plan_call_placement() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.placement IS NOT NULL AND (
        NEW.placement, NEW.placement_agent, NEW.placement_model, NEW.placed_at,
        NEW.content_fingerprint, NEW.content_bytes, NEW.placement_audit_id
    ) IS DISTINCT FROM (
        OLD.placement, OLD.placement_agent, OLD.placement_model, OLD.placed_at,
        OLD.content_fingerprint, OLD.content_bytes, OLD.placement_audit_id
    ) THEN
        RAISE EXCEPTION 'the placement of a planner call is recorded once'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""


def _check(name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{TABLE}_{name}"))


def upgrade() -> None:
    listed = ", ".join(f"'{value}'" for value in PLACEMENTS)
    op.create_table(
        "agent_plan_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("task_attempt", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("claim_call", sa.SmallInteger(), nullable=False),
        sa.Column("owner", sa.String(length=100), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("placement", sa.String(length=16), nullable=True),
        sa.Column("placement_agent", sa.String(length=64), nullable=True),
        sa.Column("placement_model", sa.String(length=MAX_MODEL_CHARS), nullable=True),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_fingerprint", sa.String(length=71), nullable=True),
        sa.Column("content_bytes", sa.Integer(), nullable=True),
        sa.Column("placement_audit_id", sa.Uuid(), nullable=True),
        _check("counters_valid", "task_attempt >= 1 AND retry_count >= 0 AND seq >= 1"),
        _check("claim_call_in_range", f"claim_call BETWEEN 1 AND {MAX_PLAN_ATTEMPTS}"),
        _check("placement_valid", f"placement IN ({listed})"),
        _check(
            "placement_complete",
            "(placement IS NULL) = (placement_agent IS NULL)"
            " AND (placement IS NULL) = (placement_model IS NULL)"
            " AND (placement IS NULL) = (placed_at IS NULL)",
        ),
        _check(
            "placement_agent_format",
            f"placement_agent IS NULL OR placement_agent ~ '^{AGENT_LABEL_PATTERN}$'",
        ),
        _check(
            "placement_model_format",
            f"placement_model IS NULL OR placement_model ~ '^{MODEL_PATTERN}$'",
        ),
        _check(
            "cloud_is_audited",
            "COALESCE(placement = 'cloud', false) = (placement_audit_id IS NOT NULL)"
            " AND (placement_audit_id IS NULL) = (content_fingerprint IS NULL)"
            " AND (placement_audit_id IS NULL) = (content_bytes IS NULL)",
        ),
        _check(
            "content_fingerprint_format",
            "content_fingerprint IS NULL"
            " OR content_fingerprint ~ '^sha256:[0-9a-f]{64}$'",
        ),
        _check(
            "content_bytes_not_negative", "content_bytes IS NULL OR content_bytes >= 0"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_agent_plan_calls_task_id_tasks"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_plan_calls")),
        sa.UniqueConstraint(
            "task_id",
            "task_attempt",
            "retry_count",
            "seq",
            name=op.f("uq_agent_plan_calls_task_id"),
        ),
    )

    op.execute(_FUNCTION)
    op.execute(
        "CREATE TRIGGER tr_agent_plan_calls_placement_once "
        f"BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION paw_keep_plan_call_placement()"
    )
    op.execute(
        f"ALTER TABLE {TABLE} ENABLE ALWAYS TRIGGER tr_agent_plan_calls_placement_once"
    )

    # A call is made once and placed once; nothing deletes it.
    grant_app_privileges(
        op,
        "agent_plan_calls",
        select=True,
        insert=True,
        update_columns=PLACEMENT_COLUMNS,
    )


def downgrade() -> None:
    # DESTROYS the record of the planner's calls (see the docstring).
    op.execute(f"DROP TRIGGER tr_agent_plan_calls_placement_once ON {TABLE}")
    op.execute("DROP FUNCTION paw_keep_plan_call_placement()")
    op.drop_table(TABLE)
