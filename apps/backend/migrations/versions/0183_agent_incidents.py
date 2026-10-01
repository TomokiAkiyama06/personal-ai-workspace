"""Agent incidents: the out-of-memory failures and the escalations that System
Health counts (issue #183, Decision 0071 Proposed).

Revision ID: 0183
Revises: 0066
Create Date: 2026-10-01

* ``agent_incidents``: one row per incident (``kind``: ``out_of_memory`` or
  ``escalation``) and the database's time. No task, node, agent or text. The
  orchestrator writes it in the transaction of the node's failure
  (``DagStore.fail_node``), or alone for the planner's out-of-memory failure.
* ``ix_agent_incidents_occurred_at``: System Health counts the incidents of the
  last hour / day, and the purge removes the ones past the retention period.

Privileges of the application role (``PAW_APP_DATABASE_ROLE``): SELECT, INSERT and
DELETE (the purge after ``PAW_HEALTH_RETENTION_DAYS``); an incident is never
updated.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0183"
down_revision: str | Sequence[str] | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_agent_incidents_migration.py`` compares them with the model.
KINDS = ("out_of_memory", "escalation")
INDEX = "ix_agent_incidents_occurred_at"


def upgrade() -> None:
    op.create_table(
        "agent_incidents",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in KINDS) + ")",
            name=op.f("ck_agent_incidents_kind_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_incidents")),
    )
    op.create_index(INDEX, "agent_incidents", ["occurred_at"])
    grant_app_privileges(op, "agent_incidents", select=True, insert=True, delete=True)


def downgrade() -> None:
    op.drop_index(INDEX, table_name="agent_incidents")
    op.drop_table("agent_incidents")
