"""Agent Task lifecycle: tasks, attempts, steps, tool calls, logs and events (PAW-032).

Revision ID: 0032
Revises: 0025
Create Date: 2026-09-24

``project_id``, ``created_by`` and ``actor_id`` carry no foreign keys because
the users and projects tables do not exist yet; add them together with those
tables. ``task_events`` is append-only: a trigger rejects UPDATE and DELETE.

The application role (``PAW_APP_DATABASE_ROLE``) gets the least privileges that
``TaskService`` needs on each table: rows of the history and the log are only
added and read; the other tables are added, read and updated on the columns the
service changes (never the identity of a row, its title or its input). Nothing
is ever deleted, so DELETE is granted nowhere.

Enum-like columns are text with CHECK constraints whose value lists are
written out here (a migration must not follow later changes of the Python
enums; change a list with a new revision).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from paw_backend.db_roles import grant_app_privileges

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TASK_STATES = tuple(
    "queued running waiting paused evaluating completed failed cancelled".split()
)
WAIT_REASONS = ("user", "approval", "resource")
COMMANDS = tuple(
    "create start wait unblock begin_evaluation complete fail "
    "pause resume cancel retry restart stop_now".split()
)
ACTOR_KINDS = ("user", "system", "policy")
REVIEW_STATUSES = ("not_started", "in_review", "approved", "changes_requested")
EVALUATION_RESULTS = ("not_run", "passed", "failed")
PR_STATES = ("draft", "open", "merged", "closed")
STEP_STATUSES = ("running", "succeeded", "failed", "interrupted")
TOOL_STATUSES = ("started", "succeeded", "failed", "interrupted")
LOG_LEVELS = ("debug", "info", "warning", "error")


def _in(column: str, values: Sequence[str], name: str) -> sa.CheckConstraint:
    listed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({listed})", name=op.f(name))


def _now() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("starting_commit", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("wait_reason", sa.String(length=24), nullable=True),
        sa.Column("agent", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        _now(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        _in("state", TASK_STATES, "ck_tasks_state_valid"),
        _in("wait_reason", WAIT_REASONS, "ck_tasks_wait_reason_valid"),
        sa.CheckConstraint(
            "(state = 'waiting') = (wait_reason IS NOT NULL)",
            name=op.f("ck_tasks_wait_reason_matches_state"),
        ),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_tasks_attempt_positive")),
        sa.CheckConstraint(
            "retry_count >= 0", name=op.f("ck_tasks_retry_count_not_negative")
        ),
    )
    grant_app_privileges(
        op,
        "tasks",
        insert=True,
        update_columns=(
            "state",
            "wait_reason",
            "agent",
            "model",
            "attempt",
            "retry_count",
            "version",
            "updated_at",
        ),
    )

    op.create_table(
        "task_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("worktree_path", sa.String(length=1024), nullable=True),
        sa.Column("head_commit", sa.String(length=64), nullable=True),
        sa.Column("review_status", sa.String(length=24), nullable=False),
        sa.Column("evaluation_result", sa.String(length=24), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(length=2048), nullable=True),
        sa.Column("pr_state", sa.String(length=24), nullable=True),
        _now(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_attempts")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_task_attempts_task_id_tasks")
        ),
        sa.UniqueConstraint("task_id", "number", name=op.f("uq_task_attempts_task_id")),
        _in("review_status", REVIEW_STATUSES, "ck_task_attempts_review_status_valid"),
        _in(
            "evaluation_result",
            EVALUATION_RESULTS,
            "ck_task_attempts_evaluation_result_valid",
        ),
        _in("pr_state", PR_STATES, "ck_task_attempts_pr_state_valid"),
        sa.CheckConstraint(
            "number >= 1", name=op.f("ck_task_attempts_number_positive")
        ),
        sa.CheckConstraint(
            "(pr_number IS NULL) = (pr_url IS NULL)"
            " AND (pr_number IS NULL) = (pr_state IS NULL)",
            name=op.f("ck_task_attempts_pull_request_complete"),
        ),
    )
    grant_app_privileges(
        op,
        "task_attempts",
        insert=True,
        update_columns=(
            "branch",
            "worktree_path",
            "head_commit",
            "review_status",
            "evaluation_result",
            "pr_number",
            "pr_url",
            "pr_state",
            "updated_at",
        ),
    )

    op.create_table(
        "task_steps",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_steps")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_task_steps_task_id_tasks")
        ),
        sa.UniqueConstraint(
            "task_id", "attempt", "sequence", name=op.f("uq_task_steps_task_id")
        ),
        _in("status", STEP_STATUSES, "ck_task_steps_status_valid"),
        sa.CheckConstraint(
            "sequence >= 1", name=op.f("ck_task_steps_sequence_positive")
        ),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)",
            name=op.f("ck_task_steps_finished_matches_status"),
        ),
    )
    grant_app_privileges(
        op, "task_steps", insert=True, update_columns=("status", "finished_at")
    )
    op.create_index(
        "uq_task_steps_one_running",
        "task_steps",
        ["task_id", "attempt"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "task_tool_invocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("step_id", sa.BigInteger(), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_tool_invocations")),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_task_tool_invocations_task_id_tasks"),
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["task_steps.id"],
            name=op.f("fk_task_tool_invocations_step_id_task_steps"),
        ),
        _in("status", TOOL_STATUSES, "ck_task_tool_invocations_status_valid"),
        sa.CheckConstraint(
            "(status = 'started') = (finished_at IS NULL)",
            name=op.f("ck_task_tool_invocations_finished_matches_status"),
        ),
    )
    grant_app_privileges(
        op,
        "task_tool_invocations",
        insert=True,
        update_columns=("status", "finished_at"),
    )
    op.create_index(
        op.f("ix_task_tool_invocations_step_id"), "task_tool_invocations", ["step_id"]
    )

    op.create_table(
        "task_logs",
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=24), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        _now(),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_task_logs")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_task_logs_task_id_tasks")
        ),
        _in("level", LOG_LEVELS, "ck_task_logs_level_valid"),
    )
    grant_app_privileges(op, "task_logs", insert=True)
    op.create_index(op.f("ix_task_logs_task_id"), "task_logs", ["task_id", "seq"])

    op.create_table(
        "task_events",
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("command", sa.String(length=24), nullable=False),
        sa.Column("from_state", sa.String(length=24), nullable=True),
        sa.Column("to_state", sa.String(length=24), nullable=False),
        sa.Column("wait_reason", sa.String(length=24), nullable=True),
        sa.Column("actor_kind", sa.String(length=24), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("step_name", sa.String(length=100), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("task_version", sa.Integer(), nullable=False),
        _now(),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_task_events")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_task_events_task_id_tasks")
        ),
        _in("command", COMMANDS, "ck_task_events_command_valid"),
        _in("from_state", TASK_STATES, "ck_task_events_from_state_valid"),
        _in("to_state", TASK_STATES, "ck_task_events_to_state_valid"),
        _in("wait_reason", WAIT_REASONS, "ck_task_events_wait_reason_valid"),
        _in("actor_kind", ACTOR_KINDS, "ck_task_events_actor_kind_valid"),
        sa.CheckConstraint(
            "(actor_kind = 'user') = (actor_id IS NOT NULL)",
            name=op.f("ck_task_events_actor_id_matches_kind"),
        ),
    )
    grant_app_privileges(op, "task_events", insert=True)
    op.create_index(op.f("ix_task_events_task_id"), "task_events", ["task_id", "seq"])

    # The history is append-only: not even the application role may rewrite it.
    op.execute(
        """
        CREATE FUNCTION task_events_reject_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'task_events is append-only'
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER task_events_append_only
        BEFORE UPDATE OR DELETE ON task_events
        FOR EACH ROW EXECUTE FUNCTION task_events_reject_change()
        """
    )


def downgrade() -> None:
    # Dropping the table drops its trigger (DROP TABLE fires no row triggers).
    op.drop_table("task_events")
    op.execute("DROP FUNCTION task_events_reject_change()")
    op.drop_table("task_logs")
    op.drop_table("task_tool_invocations")
    op.drop_table("task_steps")
    op.drop_table("task_attempts")
    op.drop_table("tasks")
