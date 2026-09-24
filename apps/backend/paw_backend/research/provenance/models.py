"""ORM models of the Evidence / Claim Provenance store (Alembic revision ``0052``).

Six tables. Nothing in them is ever updated or deleted by the application: the
application role holds SELECT and INSERT only (see the migration), so evidence
cannot be rewritten in SQL.

* ``research_sources``: a source as it was fetched. Unique per ``(project_id,
  locator, content_hash)``: the same page with other content is another row.
  There is no column for the content itself, only its hash.
* ``research_claims``: a claim text. Unique per ``(project_id,
  text_fingerprint)`` (the fingerprint of the normalised text), so a claim that
  is recorded twice is one row.
* ``research_claim_sources``: which sources support / contradict a claim.
* ``research_claim_uses``: which answer or task used a claim (the trace).
* ``research_claim_relations`` / ``research_source_relations``: symmetric
  ``duplicate`` / ``contradiction`` links between two claims or two sources,
  stored once as an ordered pair (``low_id < high_id``), one per pair.

Every link table carries ``project_id`` and reaches its claims and sources
through a **composite foreign key** ``(id, project_id)``, so a link can never
join rows of two projects, whatever the application does.

``project_id`` and ``created_by`` are plain UUID columns (the projects and users
tables do not exist yet). ``research_claims.task_id`` is a real foreign key to
``tasks.id`` (``ON DELETE SET NULL``). An answer id in ``research_claim_uses``
has no table to point at, so ``ref_id`` is a plain UUID for both kinds.

Allowed values are ``text`` columns with CHECK constraints. The migration repeats
the literals; ``tests/test_provenance_migration.py`` fails when they drift.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base
from paw_backend.tasks.models import TaskRow

TABLE_NAMES = (
    "research_sources",
    "research_claims",
    "research_claim_sources",
    "research_claim_uses",
    "research_claim_relations",
    "research_source_relations",
)

_UUID_DEFAULT = text("gen_random_uuid()")
_EMPTY_STRING = text("''")


class SourceRow(Base):
    """One source as it was fetched (never the full content)."""

    __tablename__ = "research_sources"
    __table_args__ = (
        UniqueConstraint("project_id", "locator", "content_hash"),
        # The target of the composite foreign keys of the link tables.
        UniqueConstraint("id", "project_id"),
        CheckConstraint(
            "source_type IN ('official_docs', 'official_github', 'primary',"
            " 'secondary', 'community', 'unknown')",
            name="source_type_valid",
        ),
        CheckConstraint(
            "char_length(locator) BETWEEN 1 AND 2048", name="locator_length"
        ),
        # A canonical locator is printable ASCII (non-ASCII is percent-encoded),
        # which also keeps the unique index rows small.
        CheckConstraint("locator ~ '^https?://[!-~]+$'", name="locator_shape"),
        CheckConstraint("char_length(title) <= 300", name="title_length"),
        CheckConstraint(
            "content_hash ~ '^sha256:[0-9a-f]{64}$'", name="content_hash_format"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    project_id: Mapped[UUID]
    locator: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, server_default=_EMPTY_STRING)
    content_hash: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Written by the service from its injected clock (no server default).
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ClaimRow(Base):
    """One claim text of a project."""

    __tablename__ = "research_claims"
    __table_args__ = (
        UniqueConstraint("project_id", "text_fingerprint"),
        UniqueConstraint("id", "project_id"),
        CheckConstraint(
            "char_length(claim_text) BETWEEN 1 AND 2000", name="claim_text_length"
        ),
        CheckConstraint(
            "text_fingerprint ~ '^[0-9a-f]{64}$'", name="text_fingerprint_format"
        ),
        Index("ix_research_claims_task_id", "task_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    project_id: Mapped[UUID]
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(TaskRow.id, ondelete="SET NULL")
    )
    created_by: Mapped[UUID]
    claim_text: Mapped[str] = mapped_column(Text)
    # sha256 (hex) of the normalised text: the deduplication key.
    text_fingerprint: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ClaimSourceRow(Base):
    """A source that supports or contradicts a claim."""

    __tablename__ = "research_claim_sources"
    __table_args__ = (
        PrimaryKeyConstraint("claim_id", "source_id"),
        ForeignKeyConstraint(
            ["claim_id", "project_id"],
            ["research_claims.id", "research_claims.project_id"],
        ),
        ForeignKeyConstraint(
            ["source_id", "project_id"],
            ["research_sources.id", "research_sources.project_id"],
        ),
        CheckConstraint("stance IN ('supports', 'contradicts')", name="stance_valid"),
        Index("ix_research_claim_sources_source_id", "source_id"),
    )

    claim_id: Mapped[UUID]
    source_id: Mapped[UUID]
    project_id: Mapped[UUID]
    stance: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ClaimUseRow(Base):
    """An answer or a task that used a claim."""

    __tablename__ = "research_claim_uses"
    __table_args__ = (
        PrimaryKeyConstraint("claim_id", "ref_kind", "ref_id"),
        ForeignKeyConstraint(
            ["claim_id", "project_id"],
            ["research_claims.id", "research_claims.project_id"],
        ),
        CheckConstraint("ref_kind IN ('answer', 'task')", name="ref_kind_valid"),
        # The trace starts from a reference.
        Index("ix_research_claim_uses_reference", "project_id", "ref_kind", "ref_id"),
    )

    claim_id: Mapped[UUID]
    ref_kind: Mapped[str] = mapped_column(Text)
    ref_id: Mapped[UUID]
    project_id: Mapped[UUID]
    created_by: Mapped[UUID]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ClaimRelationRow(Base):
    """A symmetric duplicate / contradiction link between two claims."""

    __tablename__ = "research_claim_relations"
    __table_args__ = (
        PrimaryKeyConstraint("low_id", "high_id"),
        ForeignKeyConstraint(
            ["low_id", "project_id"],
            ["research_claims.id", "research_claims.project_id"],
        ),
        ForeignKeyConstraint(
            ["high_id", "project_id"],
            ["research_claims.id", "research_claims.project_id"],
        ),
        CheckConstraint("low_id < high_id", name="ordered_pair"),
        CheckConstraint("kind IN ('duplicate', 'contradiction')", name="kind_valid"),
        Index("ix_research_claim_relations_high_id", "high_id"),
    )

    low_id: Mapped[UUID]
    high_id: Mapped[UUID]
    project_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(Text)
    created_by: Mapped[UUID]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceRelationRow(Base):
    """A symmetric duplicate / contradiction link between two sources."""

    __tablename__ = "research_source_relations"
    __table_args__ = (
        PrimaryKeyConstraint("low_id", "high_id"),
        ForeignKeyConstraint(
            ["low_id", "project_id"],
            ["research_sources.id", "research_sources.project_id"],
        ),
        ForeignKeyConstraint(
            ["high_id", "project_id"],
            ["research_sources.id", "research_sources.project_id"],
        ),
        CheckConstraint("low_id < high_id", name="ordered_pair"),
        CheckConstraint("kind IN ('duplicate', 'contradiction')", name="kind_valid"),
        Index("ix_research_source_relations_high_id", "high_id"),
    )

    low_id: Mapped[UUID]
    high_id: Mapped[UUID]
    project_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(Text)
    created_by: Mapped[UUID]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# Table objects for SQLAlchemy Core statements (``queries.py``).
SOURCES: Table = SourceRow.__table__  # type: ignore[assignment]
CLAIMS: Table = ClaimRow.__table__  # type: ignore[assignment]
CLAIM_SOURCES: Table = ClaimSourceRow.__table__  # type: ignore[assignment]
CLAIM_USES: Table = ClaimUseRow.__table__  # type: ignore[assignment]
CLAIM_RELATIONS: Table = ClaimRelationRow.__table__  # type: ignore[assignment]
SOURCE_RELATIONS: Table = SourceRelationRow.__table__  # type: ignore[assignment]
