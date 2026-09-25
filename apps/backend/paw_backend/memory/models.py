"""ORM models of the Memory / Conversation schema (revision ``0040``).

The layers are separate tables that never share rows (MEMORY_ARCHITECTURE.md
sections 10, 14 and REQUIREMENTS.md "Raw Conversation / Long-term Memory
Separation"):

* Raw Conversation: ``conversations`` and ``messages``. Kept forever and never
  put into an LLM context as a whole.
* Session state: ``session_states``, one row per conversation (a summary and
  the working state), derived from the raw messages.
* Long-term Memory: ``memories`` (identity), ``memory_versions`` (every edit is
  a new row), ``memory_metadata_changes`` (history of the in-place pin /
  importance edits), ``memory_relations`` (version graph), ``memory_sources``
  (provenance), ``embedding_models`` (each model's one dimension) and
  ``memory_embeddings`` (pgvector).

Users, projects and repositories do not exist yet (PAW-021 / PAW-026 /
PAW-027). Their ids are therefore **plain UUID columns without foreign keys**
(``owner_user_id``, ``project_id``, ``project_group_id``, ``repo_id``,
``actor_user_id``). Nothing in
the database ties them to a row: the Backend must only write ids it has
validated, and PAW-021+ may add the foreign keys in a later migration.

Allowed values (status, scope, ...) are ``text`` columns with CHECK
constraints, not PostgreSQL enum types: a value is added with an ordinary
migration. The ``StrEnum`` classes below are the single list for the models;
the migration repeats the literals, and the drift test compares both.
"""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Interval,
    SmallInteger,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base
from paw_backend.memory.vector import Vector


class MemoryScope(StrEnum):
    """Who may see a memory version; the boundary the ACL filter works on."""

    USER = "user"
    PROJECT = "project"
    # A subset of projects (for example "development projects"), as the
    # Inferred Preference flow emits it. See ``MemoryVersion``.
    PROJECT_GROUP = "project_group"
    REPO = "repo"
    SHARED = "shared"


class MemoryStatus(StrEnum):
    """Lifecycle of a version. Normal LLM context uses ``active`` only."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DEPRECATED = "deprecated"
    HISTORY = "history"


class ConfirmationState(StrEnum):
    """Inferred Preference states (MEMORY_ARCHITECTURE.md section 9)."""

    OBSERVED = "observed"
    INFERRED = "inferred"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class FreshnessPolicy(StrEnum):
    PERMANENT = "permanent"
    REVALIDATE = "revalidate"
    REPO_COMMIT = "repo_commit"
    EXPIRING = "expiring"
    SESSION_ONLY = "session_only"


class ActorType(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class RelationType(StrEnum):
    """Edges of the Memory History Graph, from the newer to the older version."""

    SUPERSEDES = "supersedes"
    EXTENDS = "extends"
    CONFLICTS_WITH = "conflicts_with"
    CONFIRMED_FROM = "confirmed_from"
    REVALIDATED_FROM = "revalidated_from"
    MERGED_FROM = "merged_from"


class SourceType(StrEnum):
    CONVERSATION = "conversation"
    TASK = "task"
    REPO_ANALYSIS = "repo_analysis"
    USER_CONFIRMATION = "user_confirmation"
    PROJECT_DECISION = "project_decision"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    AGENT = "agent"
    TASK = "task"


def _one_of(column: str, values: type[StrEnum]) -> str:
    literals = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({literals})"


_UUID_DEFAULT = text("gen_random_uuid()")
_EMPTY_OBJECT = text("'{}'::jsonb")


def _now_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Raw Conversation
# ---------------------------------------------------------------------------


class Conversation(Base):
    """A conversation. Private to ``owner_user_id``; not even Admin reads it."""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "title IS NULL OR char_length(title) <= 200", name="title_length"
        ),
        Index(
            "ix_conversations_owner_user_id_updated_at", "owner_user_id", "updated_at"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    owner_user_id: Mapped[UUID]
    # Context the conversation happened in. Informational: read access to the
    # conversation never comes from these two, only from ``owner_user_id``.
    project_id: Mapped[UUID | None]
    repo_id: Mapped[UUID | None]
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = _now_column()


class Message(Base):
    """One raw event: a message, tool result, agent result or task event."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "event_sequence"),
        # Redundant with the primary key; the composite foreign key of
        # ``memory_sources`` needs it as its target.
        UniqueConstraint(
            "conversation_id", "id", name="uq_messages_conversation_id_id"
        ),
        CheckConstraint(_one_of("role", MessageRole), name="role_valid"),
        CheckConstraint("event_sequence >= 0", name="event_sequence_not_negative"),
        CheckConstraint(
            "jsonb_typeof(attributes) = 'object'", name="attributes_object"
        ),
        Index("ix_messages_conversation_id_turn_id", "conversation_id", "turn_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    # Logical order (REQUIREMENTS.md "Ordering"): never the completion time of
    # an asynchronous worker. ``event_sequence`` is unique per conversation;
    # the writer assigns it (PAW-041).
    turn_id: Mapped[UUID]
    event_sequence: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=_EMPTY_OBJECT
    )
    created_at: Mapped[datetime] = _now_column()


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


class SessionState(Base):
    """Summary and working state of a conversation (what the next turn loads)."""

    __tablename__ = "session_states"
    __table_args__ = (
        CheckConstraint("jsonb_typeof(state) = 'object'", name="state_object"),
        CheckConstraint(
            "summarized_through_sequence IS NULL OR summarized_through_sequence >= 0",
            name="summarized_through_not_negative",
        ),
    )

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    summary: Mapped[str | None] = mapped_column(Text)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)
    # Highest ``messages.event_sequence`` the summary covers. A writer updates
    # only ``WHERE summarized_through_sequence IS NULL OR < new`` so that a
    # late, older result cannot overwrite a newer one.
    summarized_through_sequence: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = _now_column()


# ---------------------------------------------------------------------------
# Long-term Memory
# ---------------------------------------------------------------------------


class Memory(Base):
    """Identity of a memory. Everything else lives on its versions."""

    __tablename__ = "memories"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    created_at: Mapped[datetime] = _now_column()


# Also the list of allowed scopes: no other value satisfies one of the branches.
_SCOPE_COLUMNS = (
    "(scope = 'user' AND owner_user_id IS NOT NULL AND project_id IS NULL"
    " AND project_group_id IS NULL AND repo_id IS NULL)"
    " OR (scope = 'project' AND owner_user_id IS NULL AND project_id IS NOT NULL"
    " AND project_group_id IS NULL AND repo_id IS NULL)"
    " OR (scope = 'project_group' AND owner_user_id IS NULL AND project_id IS NULL"
    " AND project_group_id IS NOT NULL AND repo_id IS NULL)"
    " OR (scope = 'repo' AND owner_user_id IS NULL AND project_id IS NULL"
    " AND project_group_id IS NULL AND repo_id IS NOT NULL)"
    " OR (scope = 'shared' AND owner_user_id IS NULL AND project_id IS NULL"
    " AND project_group_id IS NULL AND repo_id IS NULL)"
)
_FRESHNESS_FIELDS = (
    "(freshness_policy <> 'revalidate'"
    " OR (verified_at IS NOT NULL AND revalidate_after IS NOT NULL))"
    " AND (freshness_policy <> 'expiring' OR expires_at IS NOT NULL)"
    " AND (freshness_policy <> 'repo_commit' OR commit_sha IS NOT NULL)"
)


class MemoryVersion(Base):
    """One version of a memory. A version is never edited in place.

    Scope and ACL live here, per version: widening a memory (for example
    ``user`` to ``project``) creates a new version and leaves the older, private
    versions private. The columns that decide who may read a row are
    ``scope`` plus exactly one of ``owner_user_id`` / ``project_id`` /
    ``project_group_id`` / ``repo_id`` (none for ``shared``); ``acl.py`` builds
    the query condition.

    ``pinned`` and ``importance`` are the low-risk metadata that
    REQUIREMENTS.md lets a person change at once (Manual Memory Editing), so
    they are updated in place; their change history is kept in
    ``MemoryMetadataChange`` by a trigger. Everything else about a version is
    immutable: a new edit is a new version.

    ``project_group`` is a memory that applies to a set of projects. The
    requirements only show it as the structured form of a free-text preference
    ("apply to the development projects"); they define no project-group entity,
    its membership or its permissions. The schema therefore stores just the
    group's id (a plain UUID, like the other ids) and leaves what a group is to
    the caller: a principal reads a group memory only when the group id is in
    the ``project_group_ids`` the caller supplies (``acl.py``). Membership of a
    project in a group never widens access by itself.
    """

    __tablename__ = "memory_versions"
    __table_args__ = (
        UniqueConstraint("memory_id", "version_number"),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint(_SCOPE_COLUMNS, name="scope_columns"),
        CheckConstraint(
            "char_length(memory_type) BETWEEN 1 AND 64", name="memory_type_length"
        ),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="title_length"),
        CheckConstraint("char_length(content) >= 1", name="content_not_empty"),
        CheckConstraint("importance BETWEEN 0 AND 100", name="importance_range"),
        CheckConstraint(_one_of("status", MemoryStatus), name="status_valid"),
        CheckConstraint(
            _one_of("confirmation_state", ConfirmationState),
            name="confirmation_state_valid",
        ),
        CheckConstraint(
            "NOT (confirmation_state = 'rejected' AND status = 'active')",
            name="rejected_not_active",
        ),
        CheckConstraint(
            _one_of("freshness_policy", FreshnessPolicy), name="freshness_policy_valid"
        ),
        CheckConstraint(_FRESHNESS_FIELDS, name="freshness_fields"),
        CheckConstraint(
            "revalidate_after IS NULL OR revalidate_after > interval '0'",
            name="revalidate_after_positive",
        ),
        CheckConstraint("on_stale IN ('lower_priority')", name="on_stale_valid"),
        CheckConstraint(
            "commit_sha IS NULL OR commit_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'",
            name="commit_sha_format",
        ),
        CheckConstraint(
            "jsonb_typeof(attributes) = 'object'", name="attributes_object"
        ),
        CheckConstraint(_one_of("actor_type", ActorType), name="actor_type_valid"),
        CheckConstraint(
            "actor_type <> 'user' OR actor_user_id IS NOT NULL",
            name="user_actor_has_id",
        ),
        # At most one active version per memory: the "current" version.
        Index(
            "ix_memory_versions_one_active",
            "memory_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        # ACL filter indexes: one per scope column, over the columns the ACL
        # condition and the ``status = active`` filter use.
        Index(
            "ix_memory_versions_owner_user_id_status",
            "owner_user_id",
            "status",
            postgresql_where=text("owner_user_id IS NOT NULL"),
        ),
        Index(
            "ix_memory_versions_project_id_status",
            "project_id",
            "status",
            postgresql_where=text("project_id IS NOT NULL"),
        ),
        Index(
            "ix_memory_versions_project_group_id_status",
            "project_group_id",
            "status",
            postgresql_where=text("project_group_id IS NOT NULL"),
        ),
        Index(
            "ix_memory_versions_repo_id_status",
            "repo_id",
            "status",
            postgresql_where=text("repo_id IS NOT NULL"),
        ),
        Index(
            "ix_memory_versions_shared_status",
            "status",
            postgresql_where=text("scope = 'shared'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    memory_id: Mapped[UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE")
    )
    # 1, 2, 3, ...; the unique (memory_id, version_number) doubles as the
    # optimistic lock: two writers creating "version 13" cannot both succeed.
    version_number: Mapped[int] = mapped_column(Integer)

    # Scope and ACL. Plain UUIDs: users / projects / repos are not tables yet.
    scope: Mapped[str] = mapped_column(Text)
    owner_user_id: Mapped[UUID | None]
    project_id: Mapped[UUID | None]
    project_group_id: Mapped[UUID | None]
    repo_id: Mapped[UUID | None]

    memory_type: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    importance: Mapped[int] = mapped_column(SmallInteger, server_default=text("50"))
    pinned: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    status: Mapped[str] = mapped_column(Text)
    confirmation_state: Mapped[str] = mapped_column(Text)

    # Freshness (MEMORY_ARCHITECTURE.md section 11).
    freshness_policy: Mapped[str] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revalidate_after: Mapped[timedelta | None] = mapped_column(Interval)
    revalidate_triggers: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'::text[]")
    )
    on_stale: Mapped[str] = mapped_column(Text, server_default=text("'lower_priority'"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    commit_sha: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    # Set when the memory became a ``stale_candidate``; it is not invalidated.
    stale_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=_EMPTY_OBJECT
    )
    actor_type: Mapped[str] = mapped_column(Text)
    actor_user_id: Mapped[UUID | None]
    change_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_column()


# Recording the change is done by a trigger so that no writer can skip it. The
# actor cannot be a column of ``memory_versions`` (those are immutable), so the
# writer names it in two transaction-local settings, see ``metadata.py``. The
# insert runs with the writer's own rights: the application role holds INSERT
# on the history table, and no UPDATE or DELETE.
#
# The function runs in the writer's session, and every role holds PostgreSQL's
# default TEMP privilege: a temporary table (or type) named like a table the
# function uses would be found first through the writer's ``search_path``. So
# the function pins its own path (``pg_catalog`` first, ``pg_temp`` explicitly
# last, which also keeps a temporary type from shadowing ``uuid``) and names its
# one table by schema, taken from the table the trigger is on. The statement is
# dynamic because a plpgsql ``INSERT`` cannot take a computed schema (and it
# avoids ``format``: SQLAlchemy's ``DDL`` treats ``%I`` as a placeholder).
RECORD_METADATA_CHANGE_FUNCTION = """\
CREATE OR REPLACE FUNCTION paw_record_memory_metadata_change()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    EXECUTE 'INSERT INTO ' || quote_ident(TG_TABLE_SCHEMA)
        || '.memory_metadata_changes ('
        || 'memory_version_id, old_pinned, new_pinned, old_importance,'
        || ' new_importance, actor_type, actor_user_id'
        || ') VALUES ($1, $2, $3, $4, $5, $6, $7)'
    USING
        NEW.id, OLD.pinned, NEW.pinned, OLD.importance, NEW.importance,
        nullif(current_setting('paw.actor_type', true), ''),
        nullif(current_setting('paw.actor_user_id', true), '')::uuid;
    RETURN NULL;
END
$$"""
# ``%(fullname)s`` is the table with its schema, so a schema built in another
# schema (the drift test) gets its own trigger.
RECORD_METADATA_CHANGE_TRIGGER = """\
CREATE TRIGGER tr_memory_versions_record_metadata_change
AFTER UPDATE OF pinned, importance ON %(fullname)s
FOR EACH ROW
WHEN (OLD.pinned IS DISTINCT FROM NEW.pinned
      OR OLD.importance IS DISTINCT FROM NEW.importance)
EXECUTE FUNCTION paw_record_memory_metadata_change()"""

for _statement in (
    RECORD_METADATA_CHANGE_FUNCTION,
    RECORD_METADATA_CHANGE_TRIGGER,
):
    event.listen(
        MemoryVersion.__table__,
        "after_create",
        DDL(_statement).execute_if(dialect="postgresql"),
    )


class MemoryMetadataChange(Base):
    """One in-place change of ``pinned`` / ``importance`` of a version.

    REQUIREMENTS.md "Manual Memory Editing": Pin and Importance take effect at
    once, but "変更履歴は残す". The row keeps the old and the new value of both
    columns (an unchanged one appears twice with the same value) and who made
    the change. It is append-only for the application (INSERT, no UPDATE or
    DELETE) and is written by the ``memory_versions`` trigger, never by a
    service: the trigger cannot be skipped, and a change without a named actor
    fails on the NOT NULL ``actor_type``. The rows go with their version
    (cascade); the history graph of versions is not affected, because a
    metadata change is not a new version (it must not conflict with a text edit
    that is based on the current version number).

    The actor is *asserted* by the Backend (the same trust as ``actor_user_id``
    of a version); the database does not know users yet (PAW-021), so it cannot
    check the id. ``created_at`` is the statement's ``clock_timestamp()``, so
    two changes of one transaction sort in the order they were made.

    Whoever can read a version can read its history: filter reads with
    ``readable_memory_versions`` like the other tables that join
    ``memory_versions``.
    """

    __tablename__ = "memory_metadata_changes"
    __table_args__ = (
        CheckConstraint(
            "old_importance BETWEEN 0 AND 100 AND new_importance BETWEEN 0 AND 100",
            name="importance_range",
        ),
        CheckConstraint(
            "old_pinned <> new_pinned OR old_importance <> new_importance",
            name="something_changed",
        ),
        CheckConstraint(_one_of("actor_type", ActorType), name="actor_type_valid"),
        CheckConstraint(
            "actor_type <> 'user' OR actor_user_id IS NOT NULL",
            name="user_actor_has_id",
        ),
        Index(
            "ix_memory_metadata_changes_memory_version_id_created_at",
            "memory_version_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    memory_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("memory_versions.id", ondelete="CASCADE")
    )
    old_pinned: Mapped[bool] = mapped_column(Boolean)
    new_pinned: Mapped[bool] = mapped_column(Boolean)
    old_importance: Mapped[int] = mapped_column(SmallInteger)
    new_importance: Mapped[int] = mapped_column(SmallInteger)
    actor_type: Mapped[str] = mapped_column(Text)
    actor_user_id: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class MemoryRelation(Base):
    """An edge of the version graph, pointing from the newer to the older version."""

    __tablename__ = "memory_relations"
    __table_args__ = (
        UniqueConstraint("from_version_id", "to_version_id", "relation_type"),
        CheckConstraint(
            _one_of("relation_type", RelationType), name="relation_type_valid"
        ),
        CheckConstraint(
            "from_version_id <> to_version_id", name="not_self_referencing"
        ),
        Index("ix_memory_relations_to_version_id", "to_version_id"),
        # A version is superseded by at most one version.
        Index(
            "ix_memory_relations_one_successor",
            "to_version_id",
            unique=True,
            postgresql_where=text("relation_type = 'supersedes'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    from_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("memory_versions.id", ondelete="CASCADE")
    )
    to_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("memory_versions.id", ondelete="CASCADE")
    )
    relation_type: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_column()


# The trigger function reads the row again instead of using ``NEW``: a deferred
# trigger event carries the row as the statement wrote it, and the same row may
# have been changed again by a later foreign-key action in the transaction.
#
# The re-read names the table by the schema and name of the table the trigger is
# on, and the function pins its ``search_path`` (``pg_catalog``, then ``pg_temp``
# explicitly): the function runs in the writer's session, and a temporary table
# called ``memory_sources`` (every role may create one) would otherwise answer
# the query with no rows and let the invalid row commit. See
# ``RECORD_METADATA_CHANGE_FUNCTION`` for the same reasoning and why the
# statement is dynamic.
MESSAGE_REQUIRES_CONVERSATION_FUNCTION = """\
CREATE OR REPLACE FUNCTION paw_check_memory_source_message_conversation()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    invalid boolean;
BEGIN
    EXECUTE 'SELECT EXISTS (SELECT 1 FROM ' || quote_ident(TG_TABLE_SCHEMA)
        || '.' || quote_ident(TG_TABLE_NAME)
        || ' WHERE id = $1 AND message_id IS NOT NULL AND conversation_id IS NULL)'
    INTO invalid USING NEW.id;
    IF invalid THEN
        RAISE EXCEPTION 'a source that names a message must name its conversation'
            USING ERRCODE = 'check_violation',
                  TABLE = 'memory_sources',
                  CONSTRAINT = 'tr_memory_sources_message_requires_conversation';
    END IF;
    RETURN NULL;
END
$$"""
# ``%(fullname)s`` is the table with its schema, so a schema built in another
# schema (the drift test) gets its own trigger.
MESSAGE_REQUIRES_CONVERSATION_TRIGGER = """\
CREATE CONSTRAINT TRIGGER tr_memory_sources_message_requires_conversation
AFTER INSERT OR UPDATE OF conversation_id, message_id ON %(fullname)s
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION paw_check_memory_source_message_conversation()"""


# A source of type ``conversation`` with no conversation, no message and (by
# ``conversation_has_no_opaque_reference``) no ``source_ref`` identifies nothing,
# and looks like a source whose conversation was deleted later
# (``ON DELETE SET NULL``), which is legitimate. Only the INSERT can tell them
# apart, so the rule is an INSERT-only trigger: nothing else fires it, and the
# foreign keys' actions and the deletion flow's UPDATE never meet it. It reads
# only ``NEW`` (no table), but pins the ``search_path`` like the other functions.
CONVERSATION_SOURCE_IDENTIFIED_FUNCTION = """\
CREATE OR REPLACE FUNCTION paw_check_memory_source_conversation_identified()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NEW.source_type = 'conversation'
       AND NEW.conversation_id IS NULL AND NEW.message_id IS NULL THEN
        RAISE EXCEPTION 'a new conversation source must name a conversation'
            USING ERRCODE = 'check_violation',
                  TABLE = 'memory_sources',
                  CONSTRAINT = 'tr_memory_sources_conversation_source_identified';
    END IF;
    RETURN NEW;
END
$$"""
CONVERSATION_SOURCE_IDENTIFIED_TRIGGER = """\
CREATE TRIGGER tr_memory_sources_conversation_source_identified
BEFORE INSERT ON %(fullname)s
FOR EACH ROW EXECUTE FUNCTION paw_check_memory_source_conversation_identified()"""


class MemorySource(Base):
    """Provenance: where a version came from. A version may have many sources.

    Deleting a conversation sets ``conversation_id`` / ``message_id`` to NULL and
    keeps the version and its other sources. The deletion flow records the loss
    in ``source_deleted_at`` (a foreign-key action cannot, and a CHECK that
    demanded it would block the delete).

    ``conversation_id`` and ``message_id`` are checked as a pair: when both are
    set, the message must belong to that conversation (composite foreign key).
    Deleting only the message clears ``message_id`` and keeps the conversation
    (``ON DELETE SET NULL (message_id)``).

    A source that names a message must also name its conversation. The
    composite key alone does not say so: with a NULL ``conversation_id`` it is
    skipped (``MATCH SIMPLE``), the database would only know that the message
    exists, and the deletion flow, which finds the memories of a conversation
    by ``conversation_id``, would miss the row. A plain CHECK cannot state the
    rule, because the ``SET NULL`` actions of a conversation delete clear
    ``conversation_id`` and ``message_id`` one after the other, in an order
    that depends on object ids, so the row passes through (NULL, message). The
    rule is therefore a deferred constraint trigger
    (``MESSAGE_REQUIRES_CONVERSATION_FUNCTION`` and ``..._TRIGGER``) that
    judges the row as it is at COMMIT. It is not a table constraint, so
    Alembic does not see it: the migration repeats the DDL and
    ``tests/test_memory_migration.py`` compares both. A violation therefore
    surfaces at COMMIT (or at ``SET CONSTRAINTS ... IMMEDIATE``), not at the
    INSERT.

    A new source of type ``conversation`` must name a conversation or a message
    (``CONVERSATION_SOURCE_IDENTIFIED_FUNCTION``, a ``BEFORE INSERT`` trigger,
    refused at the INSERT). Without it a row with nothing set is accepted by
    the CHECKs and identifies no source. The rule is not a CHECK because that
    state is exactly what deleting the conversation leaves behind, and it stays
    valid there.
    """

    __tablename__ = "memory_sources"
    __table_args__ = (
        CheckConstraint(_one_of("source_type", SourceType), name="source_type_valid"),
        # Only conversation sources use the foreign keys; the others are named
        # by an opaque ``source_ref`` (tasks, repos and decisions are not tables yet).
        CheckConstraint(
            "(conversation_id IS NULL AND message_id IS NULL)"
            " OR source_type = 'conversation'",
            name="conversation_reference_only_for_conversation",
        ),
        CheckConstraint(
            "source_type = 'conversation' OR source_ref IS NOT NULL",
            name="other_sources_have_reference",
        ),
        CheckConstraint(
            "source_ref IS NULL OR source_type <> 'conversation'",
            name="conversation_has_no_opaque_reference",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "message_id"],
            ["messages.conversation_id", "messages.id"],
            ondelete="SET NULL (message_id)",
        ),
        Index("ix_memory_sources_memory_version_id", "memory_version_id"),
        # Foreign key columns need an index for the referential actions: deleting
        # a conversation or message finds its sources through these two.
        Index(
            "ix_memory_sources_conversation_id",
            "conversation_id",
            postgresql_where=text("conversation_id IS NOT NULL"),
        ),
        Index(
            "ix_memory_sources_message_id",
            "message_id",
            postgresql_where=text("message_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    memory_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("memory_versions.id", ondelete="CASCADE")
    )
    source_type: Mapped[str] = mapped_column(Text)
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    source_ref: Mapped[str | None] = mapped_column(Text)
    source_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_column()


for _statement in (
    MESSAGE_REQUIRES_CONVERSATION_FUNCTION,
    MESSAGE_REQUIRES_CONVERSATION_TRIGGER,
    CONVERSATION_SOURCE_IDENTIFIED_FUNCTION,
    CONVERSATION_SOURCE_IDENTIFIED_TRIGGER,
):
    event.listen(
        MemorySource.__table__,
        "after_create",
        DDL(_statement).execute_if(dialect="postgresql"),
    )


class EmbeddingModel(Base):
    """An embedding model and its one dimension.

    Which model (and so which dimension) is used is decided by the PAW-019
    benchmark, so nothing is registered by the migration: registering a model
    is an ordinary insert. Its dimension is fixed once embeddings use it:
    ``memory_embeddings`` references ``(id, dimensions)``, so the database
    refuses a vector of another dimension for the model, and refuses to change
    or delete the model while embeddings exist.
    """

    __tablename__ = "embedding_models"
    __table_args__ = (
        # Redundant with the primary key; the composite foreign key of
        # ``memory_embeddings`` needs it as its target.
        UniqueConstraint("id", "dimensions", name="uq_embedding_models_id_dimensions"),
        CheckConstraint("char_length(id) BETWEEN 1 AND 200", name="id_length"),
        # 16000 is the largest dimension pgvector's ``vector`` type accepts.
        CheckConstraint("dimensions BETWEEN 1 AND 16000", name="dimensions_range"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    dimensions: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now_column()


class MemoryEmbedding(Base):
    """A vector of one memory version, made by one embedding model.

    ``embedding`` has no fixed dimension in the column type (the model is not
    chosen yet), but each model has exactly one (``embedding_models``): the
    composite foreign key ``(embedding_model_id, dimensions)`` and the
    ``vector_dims`` CHECK together make every row of a model the same size, so
    a nearest-neighbour query that filters one ``embedding_model_id`` never
    meets a dimension mismatch. The query must also join ``memory_versions`` to
    apply the ACL condition before ranking. No ANN index exists yet: PAW-043
    adds it once the model is chosen (an HNSW index needs a fixed dimension, so
    it will be a per-model expression index).
    """

    __tablename__ = "memory_embeddings"
    __table_args__ = (
        # NO ACTION on update and delete: a model's dimension cannot change, and
        # the model cannot be removed, while embeddings use it.
        ForeignKeyConstraint(
            ["embedding_model_id", "dimensions"],
            ["embedding_models.id", "embedding_models.dimensions"],
        ),
        # A vector has at least one dimension, so this also keeps it positive.
        CheckConstraint("vector_dims(embedding) = dimensions", name="dimensions_match"),
        Index("ix_memory_embeddings_embedding_model_id", "embedding_model_id"),
    )

    memory_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("memory_versions.id", ondelete="CASCADE"), primary_key=True
    )
    embedding_model_id: Mapped[str] = mapped_column(Text, primary_key=True)
    dimensions: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector())
    created_at: Mapped[datetime] = _now_column()


TABLE_NAMES: tuple[str, ...] = (
    Conversation.__tablename__,
    EmbeddingModel.__tablename__,
    Message.__tablename__,
    SessionState.__tablename__,
    Memory.__tablename__,
    MemoryVersion.__tablename__,
    MemoryMetadataChange.__tablename__,
    MemoryRelation.__tablename__,
    MemorySource.__tablename__,
    MemoryEmbedding.__tablename__,
)
