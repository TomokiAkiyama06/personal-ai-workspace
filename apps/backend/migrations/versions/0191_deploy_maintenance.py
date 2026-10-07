"""The maintenance of an update: no task starts while it lasts (issue #54,
Decision 0079 Proposed).

Revision ID: 0191
Revises: 0188
Create Date: 2026-10-07

* ``deploy_maintenance``: at most one row (``id = 1``). While it exists the task
  queue hands out no entry (``TaskQueue.claim_next``), so no new task starts
  during an update; queued tasks stay queued. The server-local deploy commands
  (``python -m paw_backend.cli deploy-*``) write it as the table owner.

Privileges of the application role (``PAW_APP_DATABASE_ROLE``): SELECT only. The
application cannot begin or end a maintenance.

The table is new: nothing else is locked or rewritten. ``paw_compatibility`` is
``expand`` (Decision 0079 4): a release without this revision runs on a schema
with it (it never reads the table), so rolling the application back does not need
the database restored.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0191"
down_revision: str | Sequence[str] | None = "0188"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Decision 0079 4: what an older release can do with this schema.
paw_compatibility: str = "expand"

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_deploy_maintenance_migration.py`` compares it with the code.
RELEASE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


def upgrade() -> None:
    op.create_table(
        "deploy_maintenance",
        sa.Column("id", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("from_release", sa.String(length=64), nullable=True),
        sa.Column("to_release", sa.String(length=64), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_deploy_maintenance_single_row")),
        sa.CheckConstraint(
            f"from_release IS NULL OR from_release ~ '{RELEASE_NAME_PATTERN}'",
            name=op.f("ck_deploy_maintenance_from_release_shape"),
        ),
        sa.CheckConstraint(
            f"to_release IS NULL OR to_release ~ '{RELEASE_NAME_PATTERN}'",
            name=op.f("ck_deploy_maintenance_to_release_shape"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deploy_maintenance")),
    )
    grant_app_privileges(op, "deploy_maintenance", select=True)


def downgrade() -> None:
    op.drop_table("deploy_maintenance")
