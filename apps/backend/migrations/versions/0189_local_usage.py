"""Local usage and the task of an agent incident (issue #187 item 5, Decision 0077).

Revision ID: 0189
Revises: 0188
Create Date: 2026-10-07

* ``local_usage``: one row per call of a local model through ``HybridRuntime``
  (``calls`` 1), or the late time of a call that outlived its node (``calls`` 0):
  the task's creator, the task, where it ran (``local_gpu`` / ``local_cpu``), the
  tokens the runtime reported and the whole seconds it held its lease, and when
  it started. No model, agent or text. Indexed by start (the workspace report)
  and by user and start (one user's report).
* ``agent_incidents.task_id``: the task an incident happened in (the usage report
  counts a user's escalated tasks). The rows written before stay NULL.

Privileges of the application role (``PAW_APP_DATABASE_ROLE``): SELECT and
INSERT on ``local_usage`` (a row is never changed or removed). Those of
``agent_incidents`` are unchanged (a new column of the table needs no grant).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0189"
down_revision: str | Sequence[str] | None = "0188"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_local_usage_migration.py`` compares them with the model.
PLACEMENTS = ("local_gpu", "local_cpu")
MAX_TOKENS = 10**15
MAX_SECONDS = 10**12
TABLE = "local_usage"
INDEXES = ("ix_local_usage_started_at", "ix_local_usage_user_id_started_at")
INCIDENT_INDEX = "ix_agent_incidents_task_id"
INCIDENT_FOREIGN_KEY = "fk_agent_incidents_task_id_tasks"


def upgrade() -> None:
    op.create_table(
        "local_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("placement", sa.String(length=16), nullable=False),
        sa.Column("calls", sa.SmallInteger(), nullable=False),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column("seconds", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "placement IN (" + ", ".join(f"'{value}'" for value in PLACEMENTS) + ")",
            name=op.f("ck_local_usage_placement_valid"),
        ),
        sa.CheckConstraint("calls IN (0, 1)", name=op.f("ck_local_usage_calls_valid")),
        sa.CheckConstraint(
            f"tokens BETWEEN 0 AND {MAX_TOKENS}",
            name=op.f("ck_local_usage_tokens_range"),
        ),
        sa.CheckConstraint(
            f"seconds BETWEEN 0 AND {MAX_SECONDS}",
            name=op.f("ck_local_usage_seconds_range"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_local_usage_task_id_tasks"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_local_usage")),
    )
    op.create_index(INDEXES[0], TABLE, ["started_at"])
    op.create_index(INDEXES[1], TABLE, ["user_id", "started_at"])
    grant_app_privileges(op, "local_usage", select=True, insert=True)

    op.add_column("agent_incidents", sa.Column("task_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f(INCIDENT_FOREIGN_KEY),
        "agent_incidents",
        "tasks",
        ["task_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(INCIDENT_INDEX, "agent_incidents", ["task_id"])


def downgrade() -> None:
    op.drop_index(INCIDENT_INDEX, table_name="agent_incidents")
    op.drop_constraint(
        op.f(INCIDENT_FOREIGN_KEY), "agent_incidents", type_="foreignkey"
    )
    op.drop_column("agent_incidents", "task_id")
    for index in reversed(INDEXES):
        op.drop_index(index, table_name=TABLE)
    op.drop_table(TABLE)
