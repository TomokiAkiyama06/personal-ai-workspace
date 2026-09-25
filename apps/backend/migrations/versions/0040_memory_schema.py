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
``embedding_models`` (empty here) gives each model exactly one dimension, which
``memory_embeddings`` references.

Privileges of the application role (``PAW_APP_DATABASE_ROLE``): every table
gets the least the eventual services need, see ``_grant_app_privileges``. In
short, the history is append-only for the application (no DELETE on versions,
messages or relations; UPDATE only of the few columns the design says change
in place), and deleting a conversation or a memory works through the foreign
keys' cascade, which PostgreSQL runs with the owner's rights.

``memory_sources`` also gets a deferred constraint trigger: a source that names
a message must name its conversation. A CHECK cannot state that, because the
foreign keys' SET NULL actions of a conversation delete pass through the state
(NULL conversation, message); the trigger judges the row at COMMIT instead.

``memory_versions`` keeps ``pinned`` and ``importance`` updatable in place
(REQUIREMENTS.md "Manual Memory Editing": low-risk metadata takes effect at
once), but "変更履歴は残す": a trigger records every change, with its actor, in the
append-only ``memory_metadata_changes``. The writer names the actor with
``paw_backend.memory.metadata.metadata_change_actor``; a change without one fails.

The constraint definitions and the triggers repeat the ones in
``paw_backend.memory.models`` on purpose (a migration is a frozen snapshot);
``tests/test_memory_migration.py`` fails when the two drift apart. Constraint
names come from the naming convention of ``paw_backend.db.Base.metadata``.

Revision ID: 0040
Revises: 0032
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from paw_backend.db_roles import grant_app_privileges

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


# See ``MemorySource`` in ``paw_backend.memory.models`` for why this is a trigger.
_MESSAGE_REQUIRES_CONVERSATION_FUNCTION = """\
CREATE OR REPLACE FUNCTION paw_check_memory_source_message_conversation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM memory_sources
        WHERE id = NEW.id AND message_id IS NOT NULL AND conversation_id IS NULL
    ) THEN
        RAISE EXCEPTION 'a source that names a message must name its conversation'
            USING ERRCODE = 'check_violation',
                  TABLE = 'memory_sources',
                  CONSTRAINT = 'tr_memory_sources_message_requires_conversation';
    END IF;
    RETURN NULL;
END
$$"""
_MESSAGE_REQUIRES_CONVERSATION_TRIGGER = """\
CREATE CONSTRAINT TRIGGER tr_memory_sources_message_requires_conversation
AFTER INSERT OR UPDATE OF conversation_id, message_id ON memory_sources
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION paw_check_memory_source_message_conversation()"""


# See ``MemoryMetadataChange`` in ``paw_backend.memory.models``.
_RECORD_METADATA_CHANGE_FUNCTION = """\
CREATE OR REPLACE FUNCTION paw_record_memory_metadata_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO memory_metadata_changes (
        memory_version_id, old_pinned, new_pinned, old_importance, new_importance,
        actor_type, actor_user_id
    ) VALUES (
        NEW.id, OLD.pinned, NEW.pinned, OLD.importance, NEW.importance,
        nullif(current_setting('paw.actor_type', true), ''),
        nullif(current_setting('paw.actor_user_id', true), '')::uuid
    );
    RETURN NULL;
END
$$"""
_RECORD_METADATA_CHANGE_TRIGGER = """\
CREATE TRIGGER tr_memory_versions_record_metadata_change
AFTER UPDATE OF pinned, importance ON memory_versions
FOR EACH ROW
WHEN (OLD.pinned IS DISTINCT FROM NEW.pinned
      OR OLD.importance IS DISTINCT FROM NEW.importance)
EXECUTE FUNCTION paw_record_memory_metadata_change()"""


def _grant_app_privileges() -> None:
    """The least privileges the application role needs on each table.

    Nothing is granted when no application role is configured (single-role
    development). TRUNCATE, ALTER, DROP and GRANT are never given. A referential
    action (``ON DELETE CASCADE`` / ``SET NULL``) runs with the rights of the
    owner of the table it changes, so deleting a conversation or a memory
    removes its messages, session state, versions and embeddings without the
    application holding DELETE on those tables.
    """
    # Raw Conversation. A conversation is renamed and touched, never re-owned:
    # ``owner_user_id`` (the ACL boundary) and the project / repo context are
    # not updatable. Deleting a conversation is a product feature (REQUIREMENTS
    # "Conversation deletion"), so DELETE is granted here.
    grant_app_privileges(
        op,
        "conversations",
        select=True,
        insert=True,
        delete=True,
        update_columns=("title", "updated_at"),
    )
    # Raw events are append-only: no UPDATE (nothing rewrites history) and no
    # DELETE (a whole conversation is deleted through its cascade; deleting a
    # single message is not a documented flow).
    grant_app_privileges(op, "messages", select=True, insert=True)
    # The summary and working state are rewritten as the conversation goes on
    # (guarded by ``summarized_through_sequence``), but the row is created once
    # and removed only with its conversation (cascade): no DELETE, and the key
    # ``conversation_id`` cannot change.
    grant_app_privileges(
        op,
        "session_states",
        select=True,
        insert=True,
        update_columns=(
            "summary",
            "state",
            "summarized_through_sequence",
            "updated_at",
        ),
    )
    # Identity only, nothing to update. DELETE removes a whole memory with all
    # its versions (cascade): the documented deletion flows (a conversation
    # deleted together with the memories derived from it, an Admin deleting a
    # Shared memory, erasing a deleted user's private memory). Deleting one
    # version on its own is not granted, see ``memory_versions``.
    grant_app_privileges(op, "memories", select=True, insert=True, delete=True)
    # A version is never edited in place (a new version is inserted instead), so
    # the content, scope / ACL columns, confirmation state and freshness
    # settings are immutable. Only what the design changes in place is
    # updatable: ``status`` (superseded / deprecated / history), ``stale_since``
    # (stale candidate marking), and the low-risk metadata ``pinned`` and
    # ``importance`` (every change is recorded by a trigger, see
    # ``memory_metadata_changes``). No DELETE: history is kept.
    grant_app_privileges(
        op,
        "memory_versions",
        select=True,
        insert=True,
        update_columns=("status", "stale_since", "pinned", "importance"),
    )
    # The history of the in-place pin / importance edits is written by the
    # trigger of ``memory_versions`` with the writer's own rights, so INSERT is
    # needed; it is append-only (no UPDATE, no DELETE), and goes with its
    # version by cascade.
    grant_app_privileges(op, "memory_metadata_changes", select=True, insert=True)
    # Edges of the history graph are append-only.
    grant_app_privileges(op, "memory_relations", select=True, insert=True)
    # Provenance is append-only, except that the deletion flow records a lost
    # source in ``source_deleted_at``. The foreign keys' SET NULL clears the
    # conversation / message references without any privilege of the application.
    grant_app_privileges(
        op,
        "memory_sources",
        select=True,
        insert=True,
        update_columns=("source_deleted_at",),
    )
    # A registry of the models chosen by the benchmark: register (insert) and
    # read. A model's dimension never changes; retiring a model is an
    # administrator's job, so neither UPDATE nor DELETE.
    grant_app_privileges(op, "embedding_models", select=True, insert=True)
    # Derived, regenerable data (not history). A model's vectors are removed
    # when it is retired or its embeddings are regenerated (LOW-priority job),
    # hence DELETE; a stored vector is never updated in place.
    grant_app_privileges(op, "memory_embeddings", select=True, insert=True, delete=True)


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
        "memory_metadata_changes",
        _uuid_pk(),
        sa.Column("memory_version_id", sa.Uuid(), nullable=False),
        sa.Column("old_pinned", sa.Boolean(), nullable=False),
        sa.Column("new_pinned", sa.Boolean(), nullable=False),
        sa.Column("old_importance", sa.SmallInteger(), nullable=False),
        sa.Column("new_importance", sa.SmallInteger(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["memory_version_id"], ["memory_versions.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "old_importance BETWEEN 0 AND 100 AND new_importance BETWEEN 0 AND 100",
            name="importance_range",
        ),
        sa.CheckConstraint(
            "old_pinned <> new_pinned OR old_importance <> new_importance",
            name="something_changed",
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
        "ix_memory_metadata_changes_memory_version_id_created_at",
        "memory_metadata_changes",
        ["memory_version_id", "created_at"],
    )
    op.execute(_RECORD_METADATA_CHANGE_FUNCTION)
    op.execute(_RECORD_METADATA_CHANGE_TRIGGER)

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
    op.create_index(
        "ix_memory_sources_message_id",
        "memory_sources",
        ["message_id"],
        postgresql_where=sa.text("message_id IS NOT NULL"),
    )
    op.execute(_MESSAGE_REQUIRES_CONVERSATION_FUNCTION)
    op.execute(_MESSAGE_REQUIRES_CONVERSATION_TRIGGER)

    # Nothing is registered here: the model (and dimension) is chosen by the
    # PAW-019 benchmark and registered with an ordinary insert.
    op.create_table(
        "embedding_models",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "dimensions", name="uq_embedding_models_id_dimensions"
        ),
        sa.CheckConstraint("char_length(id) BETWEEN 1 AND 200", name="id_length"),
        sa.CheckConstraint("dimensions BETWEEN 1 AND 16000", name="dimensions_range"),
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
        # One dimension per model: NO ACTION, so neither the dimension of a
        # model nor the model itself can change while embeddings use it.
        sa.ForeignKeyConstraint(
            ["embedding_model_id", "dimensions"],
            ["embedding_models.id", "embedding_models.dimensions"],
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

    _grant_app_privileges()


def downgrade() -> None:
    # Reverse order of creation; dropping a table drops its indexes and grants.
    op.drop_table("memory_embeddings")
    op.drop_table("embedding_models")
    op.drop_table("memory_sources")  # its trigger goes with it
    op.execute("DROP FUNCTION IF EXISTS paw_check_memory_source_message_conversation()")
    op.drop_table("memory_relations")
    op.drop_table("memory_metadata_changes")
    op.drop_table("memory_versions")  # its trigger goes with it
    op.execute("DROP FUNCTION IF EXISTS paw_record_memory_metadata_change()")
    op.drop_table("memories")
    op.drop_table("session_states")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.execute("DROP EXTENSION IF EXISTS vector")
