"""Research Scratch Store: items with a 24 hour TTL and leases (PAW-050).

Two tables, separate from Long-term Memory (no foreign key to or from any
memory table): ``research_scratch_items`` and ``research_scratch_leases``.
``expires_at = created_at + 24 hours`` is a CHECK constraint (a generated column
cannot be used: ``timestamptz + interval`` is not immutable). Deletion is
deferred for pinned items, items whose promotion is pending and items with an
active lease; a lease lasts at most one hour.

``project_id`` and ``created_by`` are plain UUIDs (the projects and users tables
do not exist yet). ``task_id`` references ``tasks.id`` (revision 0032) with
``ON DELETE SET NULL``.

The definitions repeat the ones in ``paw_backend.research.scratch.models`` on
purpose (a migration is a frozen snapshot); ``tests/test_scratch_migration.py``
fails when the two drift apart. Constraint names come from the naming
convention of ``paw_backend.db.Base.metadata``.

Revision ID: 0050
Revises: 0040
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from paw_backend.db_roles import grant_app_privileges

revision: str = "0050"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_scratch_items",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column(
            "source_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "promotion_state",
            sa.Text(),
            server_default=sa.text("'none'"),
            nullable=False,
        ),
        sa.Column("promotion_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "expires_at = created_at + interval '24 hours'",
            name="expires_at_matches_ttl",
        ),
        sa.CheckConstraint(
            "promotion_state IN ('none', 'pending', 'promoted', 'rejected')",
            name="promotion_state_valid",
        ),
        sa.CheckConstraint(
            "(promotion_state = 'pending') = (promotion_requested_at IS NOT NULL)",
            name="promotion_requested_matches_state",
        ),
        sa.CheckConstraint(
            "summary IS NOT NULL OR content IS NOT NULL", name="has_content"
        ),
        sa.CheckConstraint(
            "query IS NULL OR char_length(query) BETWEEN 1 AND 1000",
            name="query_length",
        ),
        sa.CheckConstraint(
            "title IS NULL OR char_length(title) BETWEEN 1 AND 500",
            name="title_length",
        ),
        sa.CheckConstraint(
            "summary IS NULL OR char_length(summary) BETWEEN 1 AND 8000",
            name="summary_length",
        ),
        sa.CheckConstraint(
            "content IS NULL OR char_length(content) BETWEEN 1 AND 100000",
            name="content_length",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_metadata) = 'object'", name="source_metadata_object"
        ),
        sa.CheckConstraint(
            "octet_length(source_metadata::text) <= 65536",
            name="source_metadata_size",
        ),
    )
    op.create_index(
        "ix_research_scratch_items_project_id_created_at",
        "research_scratch_items",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_research_scratch_items_task_id", "research_scratch_items", ["task_id"]
    )
    op.create_index(
        "ix_research_scratch_items_purgeable",
        "research_scratch_items",
        ["expires_at", "id"],
        postgresql_where=sa.text("NOT pinned AND promotion_state <> 'pending'"),
    )

    op.create_table(
        "research_scratch_leases",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("holder_id", sa.Uuid(), nullable=False),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("item_id", "holder_id"),
        sa.ForeignKeyConstraint(
            ["item_id"], ["research_scratch_items.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "expires_at > leased_at AND expires_at <= leased_at + interval '1 hour'",
            name="lease_window",
        ),
    )

    # Least privilege for the application role (PAW_APP_DATABASE_ROLE), exactly
    # what ``ScratchStore`` executes: an item is inserted, pinned, put into or
    # resolved in the promotion workflow (three columns) and deleted by the
    # purge; a lease is inserted or renewed (two columns) and deleted. Nothing
    # else may change: an item's content, its owner, its project and above all
    # its expiry (only pinning exempts it from the purge) stay as written.
    grant_app_privileges(
        op,
        "research_scratch_items",
        insert=True,
        delete=True,
        update_columns=("pinned", "promotion_state", "promotion_requested_at"),
    )
    grant_app_privileges(
        op,
        "research_scratch_leases",
        insert=True,
        delete=True,
        update_columns=("leased_at", "expires_at"),
    )


def downgrade() -> None:
    # Reverse order of creation; dropping a table drops its indexes.
    op.drop_table("research_scratch_leases")
    op.drop_table("research_scratch_items")
