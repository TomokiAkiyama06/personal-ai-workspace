"""ORM models of the Immediate Journal (revision ``0041``).

Three tables, all part of the Memory layer of PAW-040 (they reference its tables
with real foreign keys; ``tests/test_memory_schema.py`` lists the edges):

* ``memory_journal_entries``: one row per user message that entered the Journal.
  The immutable part is the order and the context (conversation, message, turn,
  ``event_sequence``, owner, project / repo at that time, ``recorded_at``); the
  mutable part is the Pending Observation's processing state (``state``,
  ``consolidated_at``, ``outcome``). A row in state ``pending`` IS the Pending
  Observation: the text is the message it points at (not copied), so deleting the
  conversation deletes it (``ON DELETE CASCADE`` through the message).
* ``memory_consolidation_queue``: the background queue. One active job per entry;
  a job carries the priority, a lease with a claim generation (fencing token), the
  retry bookkeeping and the dead-letter state. A dead job leaves its entry
  ``pending``: nothing is lost.
* ``memory_consolidation_keys``: which memory a worker's ``key`` names for one
  owner, and the order key of the entry behind its current version (the guard
  against an older turn overwriting a newer one). Created together with the memory.

The ids of users, projects and repositories are plain UUID columns without foreign
keys, like the rest of the Memory layer (those tables are not part of it).

Allowed values are ``text`` columns with CHECK constraints; the ``StrEnum`` classes
of ``domain.py`` are the single list for the models, the migration repeats the
literals and the drift test compares both.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base
from paw_backend.memory.journal import limits
from paw_backend.memory.journal.domain import (
    EntryState,
    FailureKind,
    JobStatus,
    Priority,
)


def _one_of(column: str, values: type) -> str:
    listed = ", ".join(f"'{member.value}'" for member in values)  # type: ignore[attr-defined]
    return f"{column} IN ({listed})"


_UUID_DEFAULT = text("gen_random_uuid()")


class JournalEntry(Base):
    """A user message in the Journal, and its Pending Observation's state."""

    __tablename__ = "memory_journal_entries"
    __table_args__ = (
        # The message must belong to the conversation. Deleting the message (only
        # its conversation's deletion does that) takes the entry with it.
        ForeignKeyConstraint(
            ["conversation_id", "message_id"],
            ["messages.conversation_id", "messages.id"],
            ondelete="CASCADE",
        ),
        # The order of events of one conversation: no two entries share a number.
        UniqueConstraint("conversation_id", "event_sequence"),
        UniqueConstraint("message_id"),
        CheckConstraint("event_sequence >= 0", name="event_sequence_not_negative"),
        CheckConstraint(_one_of("state", EntryState), name="state_valid"),
        CheckConstraint(
            "(state = 'consolidated') = (consolidated_at IS NOT NULL)",
            name="consolidated_has_time",
        ),
        CheckConstraint(
            "(state = 'consolidated') = (outcome IS NOT NULL)",
            name="consolidated_has_outcome",
        ),
        CheckConstraint(
            "outcome IS NULL OR jsonb_typeof(outcome) = 'object'",
            name="outcome_object",
        ),
        # What the next turn reads: the pending entries of a conversation in order.
        Index(
            "ix_memory_journal_entries_pending",
            "conversation_id",
            "event_sequence",
            postgresql_where=text("state = 'pending'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    conversation_id: Mapped[UUID]
    message_id: Mapped[UUID]
    turn_id: Mapped[UUID]
    # Equal to ``messages.event_sequence`` of the message: ``MemoryJournal`` writes
    # the same number to both, in one transaction, under the conversation's row lock.
    event_sequence: Mapped[int] = mapped_column(BigInteger)
    # The conversation's owner, copied under the row lock. The ACL of everything the
    # consolidator writes: the worker never names an owner.
    owner_user_id: Mapped[UUID]
    project_id: Mapped[UUID | None]
    repo_id: Mapped[UUID | None]
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    state: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    consolidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ConsolidationJob(Base):
    """One unit of background work: consolidate one entry.

    Claim order among the claimable jobs (``queued`` and past ``available_at``, or
    ``claimed`` with an expired lease) is ``priority_rank``, ``enqueued_at``, ``id``.
    ``priority_rank`` repeats ``priority`` as a number so that it can be indexed; a
    CHECK keeps the two in step. Every instant is the DATABASE clock.
    """

    __tablename__ = "memory_consolidation_queue"
    __table_args__ = (
        CheckConstraint(_one_of("status", JobStatus), name="status_valid"),
        CheckConstraint(_one_of("priority", Priority), name="priority_valid"),
        CheckConstraint(
            "(priority = 'high' AND priority_rank = 0)"
            " OR (priority = 'normal' AND priority_rank = 1)"
            " OR (priority = 'low' AND priority_rank = 2)",
            name="priority_rank_matches_priority",
        ),
        CheckConstraint(
            "last_failure IS NULL OR " + _one_of("last_failure", FailureKind),
            name="last_failure_valid",
        ),
        CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        CheckConstraint("deferrals >= 0", name="deferrals_not_negative"),
        CheckConstraint("claim_count >= 0", name="claim_count_not_negative"),
        # A lease exists exactly while the job is claimed.
        CheckConstraint(
            "(status = 'claimed') = (lease_expires_at IS NOT NULL)",
            name="lease_matches_status",
        ),
        CheckConstraint(
            "status <> 'claimed'"
            " OR (claimed_by IS NOT NULL AND claimed_at IS NOT NULL)",
            name="claimed_has_worker",
        ),
        CheckConstraint(
            "status <> 'queued' OR (claimed_by IS NULL AND claimed_at IS NULL)",
            name="queued_has_no_worker",
        ),
        CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > claimed_at",
            name="lease_after_claim",
        ),
        CheckConstraint(
            "(status IN ('completed', 'dead')) = (finished_at IS NOT NULL)",
            name="finished_matches_status",
        ),
        # A dead job has used up its attempts, and only a failure that counts does.
        CheckConstraint(
            "status <> 'dead' OR attempts >= 1", name="dead_has_failed_attempts"
        ),
        # The job's entry can be deleted (with its conversation) whatever the job's
        # state, so the reference needs an index that covers every row; it also
        # serves "the latest job of an entry".
        Index("ix_memory_consolidation_queue_entry_id_id", "entry_id", "id"),
        # An entry has at most one active job, so enqueueing twice is one job.
        Index(
            "uq_memory_consolidation_queue_one_active_per_entry",
            "entry_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'claimed')"),
        ),
        # Serves the claim query.
        Index(
            "ix_memory_consolidation_queue_claim_order",
            "priority_rank",
            "enqueued_at",
            "id",
            postgresql_where=text("status IN ('queued', 'claimed')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("memory_journal_entries.id", ondelete="CASCADE")
    )
    priority: Mapped[str] = mapped_column(Text)
    priority_rank: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    # Not claimable before this instant: the end of a retry delay.
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    # Failures that count toward the dead letter, and deferrals (the worker was
    # not reachable) that never do. Both only ever grow.
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    deferrals: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # The claim generation (fencing token): every claim adds one, nothing lowers it.
    claim_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    claimed_by: Mapped[str | None] = mapped_column(String(limits.MAX_WORKER_ID_CHARS))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConsolidationKey(Base):
    """The memory a worker's ``key`` names for one owner, and its order guard.

    ``applied_*`` is the order key of the entry behind the memory's current
    version. A candidate is applied only when its entry is newer (``rules.is_newer``),
    and the row is updated in the same transaction, so a candidate of an older turn
    that finishes later cannot overwrite a newer one. The row lives and dies with
    its memory (``ON DELETE CASCADE``), and holds no foreign key to the entry so
    that deleting the conversation keeps the guard.
    """

    __tablename__ = "memory_consolidation_keys"
    __table_args__ = (
        CheckConstraint(
            f"char_length(key) BETWEEN 1 AND {limits.MAX_KEY_CHARS}",
            name="key_length",
        ),
        CheckConstraint(
            "applied_event_sequence >= 0", name="applied_sequence_not_negative"
        ),
        UniqueConstraint("memory_id"),
    )

    owner_user_id: Mapped[UUID] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    memory_id: Mapped[UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE")
    )
    applied_conversation_id: Mapped[UUID]
    applied_event_sequence: Mapped[int] = mapped_column(BigInteger)
    applied_recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


TABLE_NAMES: tuple[str, ...] = (
    JournalEntry.__tablename__,
    ConsolidationJob.__tablename__,
    ConsolidationKey.__tablename__,
)
