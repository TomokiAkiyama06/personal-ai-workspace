"""The per-connection session slot of a CLI login (issue #239, Decision 0083, 6).

Revision ID: 0239
Revises: 0194
Create Date: 2026-10-20

A Codex ChatGPT login refreshes its credential inside the session (its refresh
token is single-use), so at most one session runs per connection at a time
(Decision 0083, 6; Decision 0087, 4's table). The backend may run in several
processes, so the slot is a database row, not a lock in one process:

* ``connection_session_leases``: one row per connection (keyed by its kind:
  there is one connection per kind) while a session holds the slot: the holder
  (a random id per acquisition), ``acquired_at``, ``heartbeat_at`` and
  ``expires_at`` (the database's clock). A row whose ``expires_at`` passed may
  be taken over by any process; the holder deletes its own row at the end.
  No credential, no task, no content.

Privileges of the application role (``PAW_APP_DATABASE_ROLE``): SELECT, INSERT
and DELETE, and UPDATE of the holder and the three instants only (a takeover
and a heartbeat). ``paw_compatibility`` is ``expand`` (Decision 0079, 4): an
older release never names the table. ``downgrade()`` drops it (a session that
holds a slot then runs without one: development and test only).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0239"
down_revision: str | Sequence[str] | None = "0194"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Decision 0079 4: what an older release can do with this schema.
paw_compatibility: str = "expand"

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_connection_session_lease_migration.py`` compares them with
# the model.
TABLE = "connection_session_leases"
KINDS = ("codex", "claude")
UPDATE_COLUMNS = ("holder", "acquired_at", "heartbeat_at", "expires_at")


def upgrade() -> None:
    op.create_table(
        "connection_session_leases",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("holder", sa.Uuid(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in KINDS) + ")",
            name=op.f("ck_connection_session_leases_kind_valid"),
        ),
        sa.CheckConstraint(
            "heartbeat_at >= acquired_at",
            name=op.f("ck_connection_session_leases_heartbeat_after_acquire"),
        ),
        sa.CheckConstraint(
            "expires_at > heartbeat_at",
            name=op.f("ck_connection_session_leases_expires_after_heartbeat"),
        ),
        sa.PrimaryKeyConstraint("kind", name=op.f("pk_connection_session_leases")),
    )
    grant_app_privileges(
        op,
        "connection_session_leases",
        select=True,
        insert=True,
        delete=True,
        update_columns=UPDATE_COLUMNS,
    )


def downgrade() -> None:
    op.drop_table("connection_session_leases")
