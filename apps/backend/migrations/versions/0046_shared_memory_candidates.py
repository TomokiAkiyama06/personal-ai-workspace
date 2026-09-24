"""Shared Memory Candidates (PAW-046).

One table, ``shared_memory_candidates``: a proposed shared memory with its
proposer, its provenance and, once an Owner or Admin has decided, the decision.
Shared Memory itself stays in the PAW-040 tables (``scope = 'shared'``); a
candidate is kept out of them because a ``shared`` version is readable by every
user while a candidate still holds content that came from a private memory, and
because a candidate changes state in place while a version never does.

``proposer_user_id``, ``proposer_agent_id``, ``decided_by``,
``origin_version_id`` and ``memory_id`` are plain UUIDs (no foreign keys): the
Memory layer tables stay free of links to other subsystems' tables
(``tests/test_memory_schema.py``).

The definitions repeat the ones in ``paw_backend.memory.shared.models`` on
purpose (a migration is a frozen snapshot); ``tests/
test_shared_memory_migration.py`` fails when the two drift apart. Constraint
names come from the naming convention of ``paw_backend.db.Base.metadata``.

Revision ID: 0046
Revises: 0050
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0046"
down_revision: str | Sequence[str] | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shared_memory_candidates",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column(
            "state", sa.Text(), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("proposer_user_id", sa.Uuid(), nullable=False),
        sa.Column("proposer_agent_id", sa.Uuid(), nullable=True),
        sa.Column("origin_scope", sa.Text(), nullable=False),
        sa.Column("origin_version_id", sa.Uuid(), nullable=True),
        sa.Column("memory_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "importance",
            sa.SmallInteger(),
            server_default=sa.text("50"),
            nullable=False,
        ),
        sa.Column(
            "policy_subjects",
            sa.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("memory_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "state IN ('pending', 'approved', 'rejected')", name="state_valid"
        ),
        sa.CheckConstraint(
            "origin_scope IN ('user', 'project', 'project_group', 'repo')",
            name="origin_scope_valid",
        ),
        sa.CheckConstraint(
            "char_length(memory_type) BETWEEN 1 AND 64", name="memory_type_length"
        ),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="title_length"),
        sa.CheckConstraint(
            "char_length(content) BETWEEN 1 AND 20000", name="content_length"
        ),
        sa.CheckConstraint("importance BETWEEN 0 AND 100", name="importance_range"),
        sa.CheckConstraint(
            "cardinality(policy_subjects) <= 20", name="policy_subjects_count"
        ),
        sa.CheckConstraint(
            "reason IS NULL OR char_length(reason) BETWEEN 1 AND 500",
            name="reason_length",
        ),
        sa.CheckConstraint(
            "decision_reason IS NULL OR char_length(decision_reason) BETWEEN 1 AND 500",
            name="decision_reason_length",
        ),
        sa.CheckConstraint(
            "state <> 'pending' OR (decided_by IS NULL AND decided_at IS NULL"
            " AND decision_reason IS NULL AND memory_id IS NULL)",
            name="pending_has_no_decision",
        ),
        sa.CheckConstraint(
            "state = 'pending' OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="decided_has_decider",
        ),
        sa.CheckConstraint(
            "memory_id IS NULL OR state = 'approved'", name="memory_only_when_approved"
        ),
    )
    op.create_index(
        "ix_shared_memory_candidates_state_created_at",
        "shared_memory_candidates",
        ["state", "created_at"],
    )
    op.create_index(
        "ix_shared_memory_candidates_proposer_user_id_state",
        "shared_memory_candidates",
        ["proposer_user_id", "state"],
    )

    # Least privilege for the application role (PAW_APP_DATABASE_ROLE), exactly
    # what ``SharedMemoryService`` executes on this table: a candidate is
    # proposed (INSERT), read, and decided once (UPDATE of the five decision
    # columns; ``SELECT ... FOR UPDATE`` needs an UPDATE privilege and gets it
    # from these). What was proposed (content, proposer, provenance, creation
    # time) and the identity of the candidate are never rewritten, and a
    # candidate is never deleted: it is the record of the decision.
    grant_app_privileges(
        op,
        "shared_memory_candidates",
        insert=True,
        update_columns=(
            "state",
            "decided_by",
            "decided_at",
            "decision_reason",
            "memory_id",
        ),
    )


def downgrade() -> None:
    # Dropping the table drops its indexes.
    op.drop_table("shared_memory_candidates")
