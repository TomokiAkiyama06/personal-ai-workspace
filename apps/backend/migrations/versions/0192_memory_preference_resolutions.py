"""The answers to held Inferred Preference candidates (issue #38, PAW-044;
Decision 0081).

Revision ID: 0192
Revises: 0188
Create Date: 2026-10-07

* ``memory_preference_resolutions``: one row per held candidate (an item of a
  consolidated journal entry's outcome, PAW-041) the person answered: confirmed
  (with the memory the confirmation wrote) or rejected. It goes with its journal
  entry (``ON DELETE CASCADE``), so deleting the conversation or erasing the user
  deletes it; the memory a confirmation wrote may go first (``ON DELETE SET NULL``).
  The owner is a plain UUID, like the rest of the Memory layer.
* ``ix_memory_journal_entries_owner_user_id_recorded_at``: the candidates and their
  evidence are read per owner, newest first; the journal had no index on its owner.

Privileges of the application role (``PAW_APP_DATABASE_ROLE``): SELECT and INSERT
on ``memory_preference_resolutions`` (an answer is never changed or removed by the
application; the cascades do that). Nothing changes on other tables.

The new table is empty. The index is built with a plain ``CREATE INDEX``, which
blocks writes to ``memory_journal_entries`` while it runs (a few rows per message
of a personal workspace).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0192"
down_revision: str | Sequence[str] | None = "0190"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_preference_migration.py`` compares them with the code.
RESOLUTIONS = ("confirmed", "rejected")


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "memory_preference_resolutions",
        sa.Column("entry_id", sa.Uuid(), nullable=False),
        sa.Column("item_index", sa.SmallInteger(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("resolution", sa.Text(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=True),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"resolution IN ({_listed(RESOLUTIONS)})",
            name=op.f("ck_memory_preference_resolutions_resolution_valid"),
        ),
        sa.CheckConstraint(
            "item_index >= 0",
            name=op.f("ck_memory_preference_resolutions_item_index_not_negative"),
        ),
        sa.CheckConstraint(
            "resolution = 'confirmed' OR memory_id IS NULL",
            name=op.f("ck_memory_preference_resolutions_only_confirmed_has_memory"),
        ),
        sa.ForeignKeyConstraint(
            ["entry_id"],
            ["memory_journal_entries.id"],
            name=op.f(
                "fk_memory_preference_resolutions_entry_id_memory_journal_entries"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["memory_id"],
            ["memories.id"],
            name=op.f("fk_memory_preference_resolutions_memory_id_memories"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint(
            "entry_id", "item_index", name=op.f("pk_memory_preference_resolutions")
        ),
    )
    op.create_index(
        "ix_memory_preference_resolutions_memory_id",
        "memory_preference_resolutions",
        ["memory_id"],
        postgresql_where=sa.text("memory_id IS NOT NULL"),
    )
    op.create_index(
        "ix_memory_preference_resolutions_owner_user_id",
        "memory_preference_resolutions",
        ["owner_user_id"],
    )
    grant_app_privileges(op, "memory_preference_resolutions", select=True, insert=True)
    op.create_index(
        "ix_memory_journal_entries_owner_user_id_recorded_at",
        "memory_journal_entries",
        ["owner_user_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_journal_entries_owner_user_id_recorded_at",
        table_name="memory_journal_entries",
    )
    op.drop_table("memory_preference_resolutions")
