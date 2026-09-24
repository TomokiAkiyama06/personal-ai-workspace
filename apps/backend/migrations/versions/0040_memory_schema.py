"""Memory / Conversation schema with pgvector (PAW-040).

Raw Conversation (conversations, messages), Session state (session_states) and
Long-term Memory (memories, memory_versions, memory_relations, memory_sources,
memory_embeddings) as separate tables. Users, projects and repositories are
plain UUID columns without foreign keys: those tables do not exist yet (the
same goes for the id of a project group, the fifth scope).

The migration enables the ``vector`` extension. ``CREATE EXTENSION`` needs a
role that may create it (a superuser, or the extension is "trusted"); if an
administrator created it beforehand, ``IF NOT EXISTS`` makes this a no-op. The
downgrade drops it after the tables that use it (and fails, rather than
cascades, if another object depends on it).

The ``embedding`` column has no fixed dimension and there is no ANN index: the
embedding model is chosen by the PAW-019 benchmark and PAW-043 adds the index.

The constraint definitions repeat the ones in ``paw_backend.memory.models`` on
purpose (a migration is a frozen snapshot); ``tests/test_memory_migration.py``
fails when the two drift apart. Constraint names come from the naming
convention of ``paw_backend.db.Base.metadata``.

Revision ID: 0040
Revises: 0032
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class _Vector(sa.types.UserDefinedType):
    """``vector`` without a dimension."""

    cache_ok = True

    def get_col_spec(self, **kw) -> str:
        return "vector"


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
    )


def _now(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _empty_object(name: str) -> sa.Column:
    return sa.Column(
        name, postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Raw Conversation -------------------------------------------------------
    op.create_table(
        "conversations",
        _uuid_pk(),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("repo_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "title IS NULL OR char_length(title) <= 200", name="title_length"
        ),
    )
    op.create_index(
        "ix_conversations_owner_user_id_updated_at",
        "conversations",
        ["owner_user_id", "updated_at"],
    )

    op.create_table(
        "messages",
        _uuid_pk(),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        _empty_object("attributes"),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("conversation_id", "event_sequence"),
        sa.UniqueConstraint(
            "conversation_id", "id", name="uq_messages_conversation_id_id"
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'tool', 'agent', 'task')", name="role_valid"
        ),
        sa.CheckConstraint("event_sequence >= 0", name="event_sequence_not_negative"),
        sa.CheckConstraint(
            "jsonb_typeof(attributes) = 'object'", name="attributes_object"
        ),
    )
    op.create_index(
        "ix_messages_conversation_id_turn_id",
        "messages",
        ["conversation_id", "turn_id"],
    )

    # Session state ----------------------------------------------------------
    op.create_table(
        "session_states",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        _empty_object("state"),
        sa.Column("summarized_through_sequence", sa.BigInteger(), nullable=True),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("conversation_id"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("jsonb_typeof(state) = 'object'", name="state_object"),
        sa.CheckConstraint(
            "summarized_through_sequence IS NULL OR summarized_through_sequence >= 0",
            name="summarized_through_not_negative",
        ),
    )

    # Long-term Memory -------------------------------------------------------
    op.create_table(
        "memories",
        _uuid_pk(),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "memory_versions",
        _uuid_pk(),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("project_group_id", sa.Uuid(), nullable=True),
        sa.Column("repo_id", sa.Uuid(), nullable=True),
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
            "pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("confirmation_state", sa.Text(), nullable=False),
        sa.Column("freshness_policy", sa.Text(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revalidate_after", sa.Interval(), nullable=True),
        sa.Column(
            "revalidate_triggers",
            sa.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "on_stale",
            sa.Text(),
            server_default=sa.text("'lower_priority'"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("commit_sha", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("stale_since", sa.DateTime(timezone=True), nullable=True),
        _empty_object("attributes"),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("change_reason", sa.Text(), nullable=True),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("memory_id", "version_number"),
        sa.CheckConstraint("version_number >= 1", name="version_number_positive"),
        sa.CheckConstraint(
            "(scope = 'user' AND owner_user_id IS NOT NULL AND project_id IS NULL"
            " AND project_group_id IS NULL AND repo_id IS NULL)"
            " OR (scope = 'project' AND owner_user_id IS NULL"
            " AND project_id IS NOT NULL"
            " AND project_group_id IS NULL AND repo_id IS NULL)"
            " OR (scope = 'project_group' AND owner_user_id IS NULL"
            " AND project_id IS NULL"
            " AND project_group_id IS NOT NULL AND repo_id IS NULL)"
            " OR (scope = 'repo' AND owner_user_id IS NULL AND project_id IS NULL"
            " AND project_group_id IS NULL AND repo_id IS NOT NULL)"
            " OR (scope = 'shared' AND owner_user_id IS NULL AND project_id IS NULL"
            " AND project_group_id IS NULL AND repo_id IS NULL)",
            name="scope_columns",
        ),
        sa.CheckConstraint(
            "char_length(memory_type) BETWEEN 1 AND 64", name="memory_type_length"
        ),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="title_length"),
        sa.CheckConstraint("char_length(content) >= 1", name="content_not_empty"),
        sa.CheckConstraint("importance BETWEEN 0 AND 100", name="importance_range"),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'deprecated', 'history')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "confirmation_state IN ('observed', 'inferred', 'confirmed', 'rejected')",
            name="confirmation_state_valid",
        ),
        sa.CheckConstraint(
            "NOT (confirmation_state = 'rejected' AND status = 'active')",
            name="rejected_not_active",
        ),
        sa.CheckConstraint(
            "freshness_policy IN ('permanent', 'revalidate', 'repo_commit',"
            " 'expiring', 'session_only')",
            name="freshness_policy_valid",
        ),
        sa.CheckConstraint(
            "(freshness_policy <> 'revalidate'"
            " OR (verified_at IS NOT NULL AND revalidate_after IS NOT NULL))"
            " AND (freshness_policy <> 'expiring' OR expires_at IS NOT NULL)"
            " AND (freshness_policy <> 'repo_commit' OR commit_sha IS NOT NULL)",
            name="freshness_fields",
        ),
        sa.CheckConstraint(
            "revalidate_after IS NULL OR revalidate_after > interval '0'",
            name="revalidate_after_positive",
        ),
        sa.CheckConstraint("on_stale IN ('lower_priority')", name="on_stale_valid"),
        sa.CheckConstraint(
            "commit_sha IS NULL OR commit_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'",
            name="commit_sha_format",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(attributes) = 'object'", name="attributes_object"
        ),
        sa.CheckConstraint(
            "actor_type IN ('user', 'agent', 'system')", name="actor_type_valid"
        ),
        sa.CheckConstraint(
            "actor_type <> 'user' OR actor_user_id IS NOT NULL",
            name="user_actor_has_id",
        ),
    )
    op.create_index(
        "ix_memory_versions_one_active",
        "memory_versions",
        ["memory_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_memory_versions_owner_user_id_status",
        "memory_versions",
        ["owner_user_id", "status"],
        postgresql_where=sa.text("owner_user_id IS NOT NULL"),
    )
    op.create_index(
        "ix_memory_versions_project_id_status",
        "memory_versions",
        ["project_id", "status"],
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )
    op.create_index(
        "ix_memory_versions_project_group_id_status",
        "memory_versions",
        ["project_group_id", "status"],
        postgresql_where=sa.text("project_group_id IS NOT NULL"),
    )
    op.create_index(
        "ix_memory_versions_repo_id_status",
        "memory_versions",
        ["repo_id", "status"],
        postgresql_where=sa.text("repo_id IS NOT NULL"),
    )
    op.create_index(
        "ix_memory_versions_shared_status",
        "memory_versions",
        ["status"],
        postgresql_where=sa.text("scope = 'shared'"),
    )

    op.create_table(
        "memory_relations",
        _uuid_pk(),
        sa.Column("from_version_id", sa.Uuid(), nullable=False),
        sa.Column("to_version_id", sa.Uuid(), nullable=False),
        sa.Column("relation_type", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["from_version_id"], ["memory_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["to_version_id"], ["memory_versions.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("from_version_id", "to_version_id", "relation_type"),
        sa.CheckConstraint(
            "relation_type IN ('supersedes', 'extends', 'conflicts_with',"
            " 'confirmed_from', 'revalidated_from', 'merged_from')",
            name="relation_type_valid",
        ),
        sa.CheckConstraint(
            "from_version_id <> to_version_id", name="not_self_referencing"
        ),
    )
    op.create_index(
        "ix_memory_relations_to_version_id", "memory_relations", ["to_version_id"]
    )
    op.create_index(
        "ix_memory_relations_one_successor",
        "memory_relations",
        ["to_version_id"],
        unique=True,
        postgresql_where=sa.text("relation_type = 'supersedes'"),
    )

    op.create_table(
        "memory_sources",
        _uuid_pk(),
        sa.Column("memory_version_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("source_deleted_at", sa.DateTime(timezone=True), nullable=True),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["memory_version_id"], ["memory_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        # The message must belong to the conversation when both are set. Only
        # the message column is cleared when the message is deleted.
        sa.ForeignKeyConstraint(
            ["conversation_id", "message_id"],
            ["messages.conversation_id", "messages.id"],
            ondelete="SET NULL (message_id)",
        ),
        sa.CheckConstraint(
            "source_type IN ('conversation', 'task', 'repo_analysis',"
            " 'user_confirmation', 'project_decision')",
            name="source_type_valid",
        ),
        sa.CheckConstraint(
            "(conversation_id IS NULL AND message_id IS NULL)"
            " OR source_type = 'conversation'",
            name="conversation_reference_only_for_conversation",
        ),
        sa.CheckConstraint(
            "source_type = 'conversation' OR source_ref IS NOT NULL",
            name="other_sources_have_reference",
        ),
        sa.CheckConstraint(
            "source_ref IS NULL OR source_type <> 'conversation'",
            name="conversation_has_no_opaque_reference",
        ),
    )
    op.create_index(
        "ix_memory_sources_memory_version_id", "memory_sources", ["memory_version_id"]
    )
    op.create_index(
        "ix_memory_sources_conversation_id",
        "memory_sources",
        ["conversation_id"],
        postgresql_where=sa.text("conversation_id IS NOT NULL"),
    )

    op.create_table(
        "memory_embeddings",
        sa.Column("memory_version_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_model_id", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", _Vector(), nullable=False),
        _now("created_at"),
        sa.PrimaryKeyConstraint("memory_version_id", "embedding_model_id"),
        sa.ForeignKeyConstraint(
            ["memory_version_id"], ["memory_versions.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "char_length(embedding_model_id) BETWEEN 1 AND 200", name="model_id_length"
        ),
        sa.CheckConstraint(
            "vector_dims(embedding) = dimensions", name="dimensions_match"
        ),
    )
    op.create_index(
        "ix_memory_embeddings_embedding_model_id",
        "memory_embeddings",
        ["embedding_model_id"],
    )


def downgrade() -> None:
    # Reverse order of creation; dropping a table drops its indexes.
    op.drop_table("memory_embeddings")
    op.drop_table("memory_sources")
    op.drop_table("memory_relations")
    op.drop_table("memory_versions")
    op.drop_table("memories")
    op.drop_table("session_states")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.execute("DROP EXTENSION IF EXISTS vector")
