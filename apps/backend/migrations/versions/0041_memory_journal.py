"""Immediate Journal and background consolidation queue (PAW-041).

Three tables (see ``paw_backend.memory.journal.models``):

* ``memory_journal_entries``: a user message that entered the Journal, with its
  place in the conversation's event sequence and the Pending Observation's
  processing state. A composite foreign key ties it to its message; the message's
  conversation deletes it (``ON DELETE CASCADE``), so no raw text outlives its
  conversation.
* ``memory_consolidation_queue``: the priority queue of the background Memory
  Worker: leases with a claim generation, retry delays, dead letter.
* ``memory_consolidation_keys``: the memory a worker's key names for one owner and
  the order key of the entry behind its current version.

The definitions repeat the ones in ``paw_backend.memory.journal.models`` on purpose
(a migration is a frozen snapshot); ``tests/test_journal_migration.py`` fails when
the two drift apart. Constraint names come from the naming convention of
``paw_backend.db.Base.metadata``.

Revision ID: 0041
Revises: 0026
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from paw_backend.db_roles import grant_app_privileges

revision: str = "0041"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_journal_entries",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("repo_id", sa.Uuid(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "state", sa.Text(), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("consolidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["conversation_id", "message_id"],
            ["messages.conversation_id", "messages.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("conversation_id", "event_sequence"),
        sa.UniqueConstraint("message_id"),
        sa.CheckConstraint("event_sequence >= 0", name="event_sequence_not_negative"),
        sa.CheckConstraint("state IN ('pending', 'consolidated')", name="state_valid"),
        sa.CheckConstraint(
            "(state = 'consolidated') = (consolidated_at IS NOT NULL)",
            name="consolidated_has_time",
        ),
        sa.CheckConstraint(
            "(state = 'consolidated') = (outcome IS NOT NULL)",
            name="consolidated_has_outcome",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR jsonb_typeof(outcome) = 'object'",
            name="outcome_object",
        ),
    )
    op.create_index(
        "ix_memory_journal_entries_pending",
        "memory_journal_entries",
        ["conversation_id", "event_sequence"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    # ``record_user_message`` INSERTs (and reads the id back). The consolidator
    # UPDATEs the processing state of the entry only (``SELECT ... FOR UPDATE``
    # needs an UPDATE privilege and gets it from these columns). Where an entry
    # sits (conversation, message, turn, sequence, owner, context, time) is never
    # rewritten, and an entry is never deleted by the application: it goes with its
    # conversation (the cascade of the foreign key runs as the table's owner).
    grant_app_privileges(
        op,
        "memory_journal_entries",
        insert=True,
        update_columns=("state", "consolidated_at", "outcome"),
    )

    op.create_table(
        "memory_consolidation_queue",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("entry_id", sa.Uuid(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False),
        sa.Column("priority_rank", sa.SmallInteger(), nullable=False),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'queued'"), nullable=False
        ),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "deferrals", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "claim_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("claimed_by", sa.String(length=100), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["entry_id"], ["memory_journal_entries.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'claimed', 'completed', 'dead')", name="status_valid"
        ),
        sa.CheckConstraint(
            "priority IN ('high', 'normal', 'low')", name="priority_valid"
        ),
        sa.CheckConstraint(
            "(priority = 'high' AND priority_rank = 0)"
            " OR (priority = 'normal' AND priority_rank = 1)"
            " OR (priority = 'low' AND priority_rank = 2)",
            name="priority_rank_matches_priority",
        ),
        sa.CheckConstraint(
            "last_failure IS NULL OR last_failure IN ('worker_unavailable',"
            " 'worker_timeout', 'worker_error', 'worker_output_invalid',"
            " 'apply_failed')",
            name="last_failure_valid",
        ),
        sa.CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        sa.CheckConstraint("deferrals >= 0", name="deferrals_not_negative"),
        sa.CheckConstraint("claim_count >= 0", name="claim_count_not_negative"),
        sa.CheckConstraint(
            "(status = 'claimed') = (lease_expires_at IS NOT NULL)",
            name="lease_matches_status",
        ),
        sa.CheckConstraint(
            "status <> 'claimed'"
            " OR (claimed_by IS NOT NULL AND claimed_at IS NOT NULL)",
            name="claimed_has_worker",
        ),
        sa.CheckConstraint(
            "status <> 'queued' OR (claimed_by IS NULL AND claimed_at IS NULL)",
            name="queued_has_no_worker",
        ),
        sa.CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > claimed_at",
            name="lease_after_claim",
        ),
        sa.CheckConstraint(
            "(status IN ('completed', 'dead')) = (finished_at IS NOT NULL)",
            name="finished_matches_status",
        ),
        sa.CheckConstraint(
            "status <> 'dead' OR attempts >= 1", name="dead_has_failed_attempts"
        ),
    )
    op.create_index(
        "ix_memory_consolidation_queue_entry_id_id",
        "memory_consolidation_queue",
        ["entry_id", "id"],
    )
    op.create_index(
        "uq_memory_consolidation_queue_one_active_per_entry",
        "memory_consolidation_queue",
        ["entry_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'claimed')"),
    )
    op.create_index(
        "ix_memory_consolidation_queue_claim_order",
        "memory_consolidation_queue",
        ["priority_rank", "enqueued_at", "id"],
        postgresql_where=sa.text("status IN ('queued', 'claimed')"),
    )
    # enqueue INSERTs (and reads the id back). claim, heartbeat, defer, fail and
    # complete UPDATE the lease, retry and status columns only: what a job is for
    # (``entry_id``, ``priority``, ``priority_rank``, ``enqueued_at``, ``id``) is
    # fixed for its life, so a compromised application cannot re-prioritise or
    # re-point queued work. No DELETE: a finished or dead job is history.
    grant_app_privileges(
        op,
        "memory_consolidation_queue",
        insert=True,
        update_columns=(
            "status",
            "available_at",
            "attempts",
            "deferrals",
            "claim_count",
            "claimed_by",
            "claimed_at",
            "lease_expires_at",
            "last_failure",
            "finished_at",
        ),
    )

    op.create_table(
        "memory_consolidation_keys",
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("applied_conversation_id", sa.Uuid(), nullable=False),
        sa.Column("applied_event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("applied_recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("owner_user_id", "key"),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("memory_id"),
        sa.CheckConstraint("char_length(key) BETWEEN 1 AND 200", name="key_length"),
        sa.CheckConstraint(
            "applied_event_sequence >= 0", name="applied_sequence_not_negative"
        ),
    )
    # A key is registered once (INSERT) and afterwards only its order guard moves
    # (UPDATE of the three ``applied_*`` columns). Which owner and memory a key
    # names never changes. Rows go with their memory (the cascade of the foreign
    # key), not by the application.
    grant_app_privileges(
        op,
        "memory_consolidation_keys",
        insert=True,
        update_columns=(
            "applied_conversation_id",
            "applied_event_sequence",
            "applied_recorded_at",
        ),
    )


def downgrade() -> None:
    # Dropping a table drops its indexes; the children go first.
    op.drop_table("memory_consolidation_keys")
    op.drop_table("memory_consolidation_queue")
    op.drop_table("memory_journal_entries")
