"""DAG orchestrator: DAGs, nodes, edges and node attempts (PAW-034).

Revision ID: 0034
Revises: 0026
Create Date: 2026-09-25

``agent_dags``: one DAG per task attempt, with ``epoch``, the fencing token of the
worker that owns it. ``agent_dag_nodes``: the nodes (state, agent rung, approach,
attempt counters, the result as bounded JSON). ``agent_dag_edges``: the
dependencies (insert-only). ``agent_dag_node_attempts``: one row per start of a
node (never the text of a failure: only its class and its loop signature).
``tasks`` (revision ``0032``) is referenced by a real foreign key.

Privileges of the application role (``PAW_APP_DATABASE_ROLE``, see
``paw_backend.db_roles``): each table gets the least the orchestrator's store
executes. No table gets a table-level UPDATE, no table gets DELETE (a DAG is the
record of what the agents did), and TRUNCATE is never granted.

The tables deliberately do not start with ``task``: the PAW-032 tests inspect
every table with that prefix.

Enum-like columns are text with CHECK constraints whose value lists are written
out here (a migration must not follow later changes of the Python enums; change a
list with a new revision).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from paw_backend.db_roles import grant_app_privileges

revision: str = "0034"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DAG_STATES = ("active", "succeeded", "failed", "cancelled")
NODE_ROLES = ("planner", "worker", "researcher", "reviewer")
NODE_STATES = (
    "pending",
    "ready",
    "running",
    "succeeded",
    "failed",
    "blocked",
    "cancelled",
)
ATTEMPT_STATES = ("running", "succeeded", "failed", "interrupted")
MAX_NODES = 32
MAX_LADDER_LENGTH = 4
MAX_APPROACH = 100
MAX_GOAL_CHARS = 4000
MAX_PLAN_BYTES = 131072
DB_MAX_JSON_BYTES = 65536
KEY_PATTERN = "[a-z][a-z0-9_-]{0,31}"


def _in(column: str, values: Sequence[str], name: str) -> sa.CheckConstraint:
    listed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({listed})", name=op.f(name))


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "agent_dags",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "task_retry_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("epoch", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("owner", sa.String(length=100), nullable=True),
        sa.Column("node_count", sa.Integer(), nullable=False),
        sa.Column("plan_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_dags")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_agent_dags_task_id_tasks")
        ),
        sa.UniqueConstraint("task_id", "attempt", name=op.f("uq_agent_dags_task_id")),
        _in("state", DAG_STATES, "ck_agent_dags_state_valid"),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_agent_dags_attempt_positive")),
        sa.CheckConstraint(
            "task_retry_count >= 0", name=op.f("ck_agent_dags_retry_count_not_negative")
        ),
        sa.CheckConstraint("epoch >= 0", name=op.f("ck_agent_dags_epoch_not_negative")),
        sa.CheckConstraint(
            "(epoch = 0) = (owner IS NULL)",
            name=op.f("ck_agent_dags_owner_matches_epoch"),
        ),
        sa.CheckConstraint(
            f"node_count BETWEEN 1 AND {MAX_NODES}",
            name=op.f("ck_agent_dags_node_count_in_range"),
        ),
        # The accepted plan's size in UTF-8 bytes, as the service declares it: the
        # total spans rows, so no per-row CHECK can measure it, but the database
        # refuses a declaration above the limit (and the application cannot change
        # it: it is not an updatable column).
        sa.CheckConstraint(
            f"plan_bytes BETWEEN 1 AND {MAX_PLAN_BYTES}",
            name=op.f("ck_agent_dags_plan_bytes_in_range"),
        ),
    )
    # ``SELECT ... FOR NO KEY UPDATE`` (the fencing lock) needs UPDATE on at least
    # one column, which this grant provides. The identity of a DAG (its task and
    # attempt) never changes, and a DAG is never deleted.
    grant_app_privileges(
        op,
        "agent_dags",
        select=True,
        insert=True,
        update_columns=("state", "epoch", "owner", "task_retry_count", "updated_at"),
    )

    op.create_table(
        "agent_dag_nodes",
        sa.Column("dag_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column(
            "capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "repositories", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "agent_index",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "approach", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "rung_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_class", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("dag_id", "key", name=op.f("pk_agent_dag_nodes")),
        sa.ForeignKeyConstraint(
            ["dag_id"],
            ["agent_dags.id"],
            name=op.f("fk_agent_dag_nodes_dag_id_agent_dags"),
        ),
        sa.UniqueConstraint(
            "dag_id", "ordinal", name=op.f("uq_agent_dag_nodes_dag_id")
        ),
        _in("role", NODE_ROLES, "ck_agent_dag_nodes_role_valid"),
        _in("state", NODE_STATES, "ck_agent_dag_nodes_state_valid"),
        sa.CheckConstraint(
            f"key ~ '^{KEY_PATTERN}$'", name=op.f("ck_agent_dag_nodes_key_format")
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name=op.f("ck_agent_dag_nodes_ordinal_not_negative")
        ),
        sa.CheckConstraint(
            f"goal <> '' AND char_length(goal) <= {MAX_GOAL_CHARS}",
            name=op.f("ck_agent_dag_nodes_goal_length"),
        ),
        sa.CheckConstraint(
            f"agent_index BETWEEN 0 AND {MAX_LADDER_LENGTH - 1}",
            name=op.f("ck_agent_dag_nodes_agent_index_in_range"),
        ),
        sa.CheckConstraint(
            f"approach BETWEEN 0 AND {MAX_APPROACH}",
            name=op.f("ck_agent_dag_nodes_approach_in_range"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND rung_attempts >= 0"
            " AND rung_attempts <= attempt_count",
            name=op.f("ck_agent_dag_nodes_counters_valid"),
        ),
        sa.CheckConstraint(
            "state <> 'running' OR attempt_count >= 1",
            name=op.f("ck_agent_dag_nodes_running_has_attempt"),
        ),
        sa.CheckConstraint(
            "(state = 'succeeded') = (result IS NOT NULL)",
            name=op.f("ck_agent_dag_nodes_result_matches_state"),
        ),
        sa.CheckConstraint(
            "result IS NULL OR (jsonb_typeof(result) = 'object'"
            f" AND octet_length(result::text) <= {DB_MAX_JSON_BYTES})",
            name=op.f("ck_agent_dag_nodes_result_bounded"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(input) = 'object'"
            f" AND octet_length(input::text) <= {DB_MAX_JSON_BYTES}",
            name=op.f("ck_agent_dag_nodes_input_bounded"),
        ),
        sa.CheckConstraint(
            "capabilities IS NULL OR jsonb_typeof(capabilities) = 'array'",
            name=op.f("ck_agent_dag_nodes_capabilities_is_a_list"),
        ),
        sa.CheckConstraint(
            "repositories IS NULL OR jsonb_typeof(repositories) = 'array'",
            name=op.f("ck_agent_dag_nodes_repositories_is_a_list"),
        ),
    )
    # The store starts, finishes and re-opens nodes: state, rung, approach, the
    # counters, the result and the failure class change; what the plan said
    # (key, order, role, goal, input, what it asked for) is fixed for good.
    grant_app_privileges(
        op,
        "agent_dag_nodes",
        select=True,
        insert=True,
        update_columns=(
            "state",
            "agent_index",
            "approach",
            "attempt_count",
            "rung_attempts",
            "result",
            "error_class",
            "finished_at",
            "updated_at",
        ),
    )

    op.create_table(
        "agent_dag_edges",
        sa.Column("dag_id", sa.Uuid(), nullable=False),
        sa.Column("node_key", sa.String(length=32), nullable=False),
        sa.Column("depends_on_key", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint(
            "dag_id", "node_key", "depends_on_key", name=op.f("pk_agent_dag_edges")
        ),
        sa.ForeignKeyConstraint(
            ["dag_id", "node_key"],
            ["agent_dag_nodes.dag_id", "agent_dag_nodes.key"],
            name=op.f("fk_agent_dag_edges_node"),
        ),
        sa.ForeignKeyConstraint(
            ["dag_id", "depends_on_key"],
            ["agent_dag_nodes.dag_id", "agent_dag_nodes.key"],
            name=op.f("fk_agent_dag_edges_dependency"),
        ),
        sa.CheckConstraint(
            "node_key <> depends_on_key", name=op.f("ck_agent_dag_edges_not_self")
        ),
    )
    # The dependencies are part of the plan: inserted with it, never changed.
    grant_app_privileges(op, "agent_dag_edges", select=True, insert=True)

    op.create_table(
        "agent_dag_node_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dag_id", sa.Uuid(), nullable=False),
        sa.Column("node_key", sa.String(length=32), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("agent_index", sa.SmallInteger(), nullable=False),
        sa.Column("approach", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("error_class", sa.String(length=100), nullable=True),
        sa.Column("failure_signature", sa.String(length=64), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=_now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_dag_node_attempts")),
        sa.ForeignKeyConstraint(
            ["dag_id", "node_key"],
            ["agent_dag_nodes.dag_id", "agent_dag_nodes.key"],
            name=op.f("fk_agent_dag_node_attempts_node"),
        ),
        sa.UniqueConstraint(
            "dag_id",
            "node_key",
            "number",
            name=op.f("uq_agent_dag_node_attempts_dag_id"),
        ),
        _in("state", ATTEMPT_STATES, "ck_agent_dag_node_attempts_state_valid"),
        sa.CheckConstraint(
            "number >= 1 AND epoch >= 1",
            name=op.f("ck_agent_dag_node_attempts_counters_positive"),
        ),
        sa.CheckConstraint(
            f"agent_index BETWEEN 0 AND {MAX_LADDER_LENGTH - 1}"
            f" AND approach BETWEEN 0 AND {MAX_APPROACH}",
            name=op.f("ck_agent_dag_node_attempts_rung_and_approach_in_range"),
        ),
        sa.CheckConstraint(
            "(state = 'running') = (finished_at IS NULL)",
            name=op.f("ck_agent_dag_node_attempts_finished_matches_state"),
        ),
        sa.CheckConstraint(
            "failure_signature IS NULL OR failure_signature ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_agent_dag_node_attempts_signature_format"),
        ),
        sa.CheckConstraint(
            "state <> 'failed' OR error_class IS NOT NULL",
            name=op.f("ck_agent_dag_node_attempts_failed_has_class"),
        ),
    )
    # An attempt is inserted when the node starts and closed when it ends: only
    # how it ended changes.
    grant_app_privileges(
        op,
        "agent_dag_node_attempts",
        select=True,
        insert=True,
        update_columns=("state", "error_class", "failure_signature", "finished_at"),
    )


def downgrade() -> None:
    # The tables that reference another go first.
    op.drop_table("agent_dag_node_attempts")
    op.drop_table("agent_dag_edges")
    op.drop_table("agent_dag_nodes")
    op.drop_table("agent_dags")
