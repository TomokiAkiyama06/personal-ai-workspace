"""ORM model of the Shared Memory Candidate table (revision ``0046``).

Shared Memory itself lives in the PAW-040 tables (``memories`` and
``memory_versions`` with ``scope = 'shared'``); this is the only table PAW-046
adds. A candidate is not stored there because a ``shared`` version is readable
by every user (``memory.acl``): a proposal still holds content that came from a
private memory, and it must not be visible to anyone but the Owner and Admin
until one of them approves it. Versions are also never edited in place, while a
candidate moves from ``pending`` to ``approved`` or ``rejected``.

``proposer_user_id``, ``proposer_agent_id``, ``decided_by``,
``origin_version_id`` and ``memory_id`` are plain UUID columns without foreign
keys: users and agents are not tables this issue may depend on, the origin may
be deleted, and ``tests/test_memory_schema.py`` keeps the Conversation / Memory
layer tables free of links to the tables of other subsystems (a candidate
belongs to the administration of Shared Memory, not to the layers). The Backend
writes only ids it has just created (``memory_id`` is the memory an approval
wrote in the same transaction).

The definitions are repeated by hand in the migration; ``tests/
test_shared_memory_migration.py`` fails when the two drift apart.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    Index,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base
from paw_backend.memory.shared import limits

TABLE_NAME = "shared_memory_candidates"
TABLE_NAMES = (TABLE_NAME,)


class SharedMemoryCandidateRow(Base):
    """One proposed shared memory. Never deleted by the application."""

    __tablename__ = TABLE_NAME
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'approved', 'rejected')", name="state_valid"
        ),
        CheckConstraint(
            "origin_scope IN ('user', 'project', 'project_group', 'repo')",
            name="origin_scope_valid",
        ),
        CheckConstraint(
            f"char_length(memory_type) BETWEEN 1 AND {limits.MAX_MEMORY_TYPE_CHARS}",
            name="memory_type_length",
        ),
        CheckConstraint(
            f"char_length(title) BETWEEN 1 AND {limits.MAX_TITLE_CHARS}",
            name="title_length",
        ),
        CheckConstraint(
            f"char_length(content) BETWEEN 1 AND {limits.MAX_CONTENT_CHARS}",
            name="content_length",
        ),
        CheckConstraint("importance BETWEEN 0 AND 100", name="importance_range"),
        CheckConstraint(
            f"cardinality(policy_subjects) <= {limits.MAX_POLICY_SUBJECTS}",
            name="policy_subjects_count",
        ),
        CheckConstraint(
            "reason IS NULL OR char_length(reason) BETWEEN 1 AND "
            f"{limits.MAX_REASON_CHARS}",
            name="reason_length",
        ),
        CheckConstraint(
            "decision_reason IS NULL OR char_length(decision_reason) BETWEEN 1 AND "
            f"{limits.MAX_REASON_CHARS}",
            name="decision_reason_length",
        ),
        # A pending candidate has no decision; a decided one has who and when.
        CheckConstraint(
            "state <> 'pending' OR (decided_by IS NULL AND decided_at IS NULL"
            " AND decision_reason IS NULL AND memory_id IS NULL)",
            name="pending_has_no_decision",
        ),
        CheckConstraint(
            "state = 'pending' OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="decided_has_decider",
        ),
        # Only an approved candidate points at a memory.
        CheckConstraint(
            "memory_id IS NULL OR state = 'approved'", name="memory_only_when_approved"
        ),
        Index("ix_shared_memory_candidates_state_created_at", "state", "created_at"),
        Index(
            "ix_shared_memory_candidates_proposer_user_id_state",
            "proposer_user_id",
            "state",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    state: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    proposer_user_id: Mapped[UUID]
    proposer_agent_id: Mapped[UUID | None]
    origin_scope: Mapped[str] = mapped_column(Text)
    origin_version_id: Mapped[UUID | None]
    memory_type: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    importance: Mapped[int] = mapped_column(SmallInteger, server_default=text("50"))
    policy_subjects: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'::text[]")
    )
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    decided_by: Mapped[UUID | None]
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    memory_id: Mapped[UUID | None]
