"""Evidence / Claim Provenance: sources, claims, links, uses, relations (PAW-052).

Six tables (see ``paw_backend.research.provenance.models``): ``research_sources``,
``research_claims``, ``research_claim_sources``, ``research_claim_uses``,
``research_claim_relations`` and ``research_source_relations``. A source keeps
its canonical locator, type, dates and the hash of what was fetched, never the
content. A claim is unique per project and normalised text. Link tables carry
``project_id`` and reference claims and sources through composite foreign keys
``(id, project_id)``, so a link cannot join two projects.

``project_id`` and ``created_by`` are plain UUIDs (the projects and users tables
do not exist yet). ``research_claims.task_id`` references ``tasks.id``
(revision 0032) with ``ON DELETE SET NULL``.

The definitions repeat the ones in the models on purpose (a migration is a
frozen snapshot); ``tests/test_provenance_migration.py`` fails when the two
drift apart. Constraint names come from the naming convention of
``paw_backend.db.Base.metadata``.

Revision ID: 0052
Revises: 0050
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0052"
down_revision: str | Sequence[str] | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_sources",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "locator", "content_hash"),
        sa.UniqueConstraint("id", "project_id"),
        sa.CheckConstraint(
            "source_type IN ('official_docs', 'official_github', 'primary',"
            " 'secondary', 'community', 'unknown')",
            name="source_type_valid",
        ),
        sa.CheckConstraint(
            "char_length(locator) BETWEEN 1 AND 2048", name="locator_length"
        ),
        sa.CheckConstraint("locator ~ '^https?://[!-~]+$'", name="locator_shape"),
        sa.CheckConstraint("char_length(title) <= 300", name="title_length"),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[0-9a-f]{64}$'", name="content_hash_format"
        ),
    )

    op.create_table(
        "research_claims",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("text_fingerprint", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("project_id", "text_fingerprint"),
        sa.UniqueConstraint("id", "project_id"),
        sa.CheckConstraint(
            "char_length(claim_text) BETWEEN 1 AND 2000", name="claim_text_length"
        ),
        sa.CheckConstraint(
            "text_fingerprint ~ '^[0-9a-f]{64}$'", name="text_fingerprint_format"
        ),
    )
    op.create_index("ix_research_claims_task_id", "research_claims", ["task_id"])

    op.create_table(
        "research_claim_sources",
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("stance", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("claim_id", "source_id"),
        sa.ForeignKeyConstraint(
            ["claim_id", "project_id"],
            ["research_claims.id", "research_claims.project_id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "project_id"],
            ["research_sources.id", "research_sources.project_id"],
        ),
        sa.CheckConstraint(
            "stance IN ('supports', 'contradicts')", name="stance_valid"
        ),
    )
    op.create_index(
        "ix_research_claim_sources_source_id", "research_claim_sources", ["source_id"]
    )

    op.create_table(
        "research_claim_uses",
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("ref_kind", sa.Text(), nullable=False),
        sa.Column("ref_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("claim_id", "ref_kind", "ref_id"),
        sa.ForeignKeyConstraint(
            ["claim_id", "project_id"],
            ["research_claims.id", "research_claims.project_id"],
        ),
        sa.CheckConstraint("ref_kind IN ('answer', 'task')", name="ref_kind_valid"),
    )
    op.create_index(
        "ix_research_claim_uses_reference",
        "research_claim_uses",
        ["project_id", "ref_kind", "ref_id"],
    )

    op.create_table(
        "research_claim_relations",
        sa.Column("low_id", sa.Uuid(), nullable=False),
        sa.Column("high_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("low_id", "high_id"),
        sa.ForeignKeyConstraint(
            ["low_id", "project_id"],
            ["research_claims.id", "research_claims.project_id"],
        ),
        sa.ForeignKeyConstraint(
            ["high_id", "project_id"],
            ["research_claims.id", "research_claims.project_id"],
        ),
        sa.CheckConstraint("low_id < high_id", name="ordered_pair"),
        sa.CheckConstraint("kind IN ('duplicate', 'contradiction')", name="kind_valid"),
    )
    op.create_index(
        "ix_research_claim_relations_high_id", "research_claim_relations", ["high_id"]
    )

    op.create_table(
        "research_source_relations",
        sa.Column("low_id", sa.Uuid(), nullable=False),
        sa.Column("high_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("low_id", "high_id"),
        sa.ForeignKeyConstraint(
            ["low_id", "project_id"],
            ["research_sources.id", "research_sources.project_id"],
        ),
        sa.ForeignKeyConstraint(
            ["high_id", "project_id"],
            ["research_sources.id", "research_sources.project_id"],
        ),
        sa.CheckConstraint("low_id < high_id", name="ordered_pair"),
        sa.CheckConstraint("kind IN ('duplicate', 'contradiction')", name="kind_valid"),
    )
    op.create_index(
        "ix_research_source_relations_high_id", "research_source_relations", ["high_id"]
    )

    # Least privilege for the application role (PAW_APP_DATABASE_ROLE). The
    # provenance store only ever reads and inserts: recorded evidence is never
    # updated (a source, a claim, a stance or a relation that is wrong is
    # answered with a new record, not an edit) and never deleted by the
    # application. So there is no UPDATE and no DELETE on any table.
    grant_app_privileges(op, "research_sources", insert=True)
    grant_app_privileges(op, "research_claims", insert=True)
    grant_app_privileges(op, "research_claim_sources", insert=True)
    grant_app_privileges(op, "research_claim_uses", insert=True)
    grant_app_privileges(op, "research_claim_relations", insert=True)
    grant_app_privileges(op, "research_source_relations", insert=True)


def downgrade() -> None:
    # Reverse order of creation; dropping a table drops its indexes.
    op.drop_table("research_source_relations")
    op.drop_table("research_claim_relations")
    op.drop_table("research_claim_uses")
    op.drop_table("research_claim_sources")
    op.drop_table("research_claims")
    op.drop_table("research_sources")
