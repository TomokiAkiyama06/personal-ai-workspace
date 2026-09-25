"""ORM models of the Research Scratch Store (Alembic revision ``0050``).

Research Scratch is temporary and belongs to Research, not to memory
(REQUIREMENTS.md "Research Scratch"; MEMORY_ARCHITECTURE.md "Scratchpad is
separate from permanent Memory"). It therefore has its own tables that no
foreign key connects to a Long-term Memory table in either direction. A result
becomes a Memory Candidate only through an explicit promotion flow (a later
issue); the store just records ``promotion_state``.

* ``research_scratch_items``: one research result. ``expires_at`` is
  ``created_at + 24 hours``, enforced by a CHECK constraint (a generated column
  is impossible: ``timestamptz + interval`` is not immutable). Four things
  defer deletion: ``pinned``, ``saved``, ``promotion_state = 'pending'`` and an
  active row in ``research_scratch_leases`` (the item is in use). ``pinned``
  (a temporary keep) and ``saved`` (a user's explicit save) are two separate
  markers because REQUIREMENTS.md lists "Pin済み" and "Userが明示保存" as separate
  reasons: each is set and cleared on its own, so clearing one never removes the
  other, and the item stays while either is set (decision 0013).
* ``research_scratch_leases``: who is using an item right now. A lease has an
  end (at most one hour) so that a crashed worker cannot keep an item alive.

``project_id`` and ``created_by`` are plain UUID columns: the projects and users
tables do not exist yet. ``task_id`` is a plain UUID column too, with no foreign
key to ``tasks.id`` (decision 0013): deleting a task is never blocked by
research (RESTRICT would), pinned research is never deleted with it (CASCADE
would) and the item keeps its Project / Task relation (SET NULL lost it). That the
task exists, in the same project, is checked by ``ScratchStore.add`` under a row
lock; afterwards the id is a record of which task the item was made for.

Allowed values are ``text`` columns with CHECK constraints. The migration
repeats the literals; ``tests/test_scratch_migration.py`` fails when the two
drift apart.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base

TABLE_NAMES = ("research_scratch_items", "research_scratch_leases")

_UUID_DEFAULT = text("gen_random_uuid()")
_EMPTY_OBJECT = text("'{}'::jsonb")


class ScratchItemRow(Base):
    """One research result (query, source metadata, summary / content)."""

    __tablename__ = "research_scratch_items"
    __table_args__ = (
        # The 24 hour TTL: expires_at can never be chosen freely.
        CheckConstraint(
            "expires_at = created_at + interval '24 hours'",
            name="expires_at_matches_ttl",
        ),
        CheckConstraint(
            "promotion_state IN ('none', 'pending', 'promoted', 'rejected')",
            name="promotion_state_valid",
        ),
        # A request time exists exactly while a promotion is pending.
        CheckConstraint(
            "(promotion_state = 'pending') = (promotion_requested_at IS NOT NULL)",
            name="promotion_requested_matches_state",
        ),
        CheckConstraint(
            "summary IS NOT NULL OR content IS NOT NULL", name="has_content"
        ),
        CheckConstraint(
            "query IS NULL OR char_length(query) BETWEEN 1 AND 1000",
            name="query_length",
        ),
        CheckConstraint(
            "title IS NULL OR char_length(title) BETWEEN 1 AND 500",
            name="title_length",
        ),
        CheckConstraint(
            "summary IS NULL OR char_length(summary) BETWEEN 1 AND 8000",
            name="summary_length",
        ),
        CheckConstraint(
            "content IS NULL OR char_length(content) BETWEEN 1 AND 100000",
            name="content_length",
        ),
        CheckConstraint(
            "jsonb_typeof(source_metadata) = 'object'", name="source_metadata_object"
        ),
        # A backstop only (4x the service limit of 16384 bytes of compact JSON):
        # the text form of a jsonb value is longer than the compact form.
        CheckConstraint(
            "octet_length(source_metadata::text) <= 65536",
            name="source_metadata_size",
        ),
        Index(
            "ix_research_scratch_items_project_id_created_at",
            "project_id",
            "created_at",
        ),
        Index("ix_research_scratch_items_task_id", "task_id"),
        # The purge scans expired rows that are not exempt through the item's
        # own columns; the lease exemption is checked against the leases table.
        Index(
            "ix_research_scratch_items_purgeable",
            "expires_at",
            "id",
            postgresql_where=text(
                "NOT pinned AND NOT saved AND promotion_state <> 'pending'"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    project_id: Mapped[UUID]
    # No foreign key (0013): see the module doc.
    task_id: Mapped[UUID | None]
    created_by: Mapped[UUID]
    query: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=_EMPTY_OBJECT
    )
    # Both are written by the service from its injected clock; there is no
    # server default, so a row cannot exist without an explicit created_at.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pinned: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # A user's explicit save, independent of ``pinned`` (decision 0013).
    saved: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    promotion_state: Mapped[str] = mapped_column(Text, server_default=text("'none'"))
    promotion_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class ScratchLeaseRow(Base):
    """One holder's lease on one item; the item is in use while one is active."""

    __tablename__ = "research_scratch_leases"
    __table_args__ = (
        # 1 second .. 1 hour: a lease that is never renewed ends by itself.
        CheckConstraint(
            "expires_at > leased_at AND expires_at <= leased_at + interval '1 hour'",
            name="lease_window",
        ),
    )

    item_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_scratch_items.id", ondelete="CASCADE"), primary_key=True
    )
    # A task id or a worker run id; a plain UUID.
    holder_id: Mapped[UUID] = mapped_column(primary_key=True)
    leased_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
