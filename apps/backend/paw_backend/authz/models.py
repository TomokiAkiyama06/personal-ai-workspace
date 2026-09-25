"""ORM model of the append-only ``audit_events`` table (migrations ``0025``, ``0087``).

Rows are only ever inserted. UPDATE, DELETE and TRUNCATE are rejected by
triggers created in the migration, and when the application runs as a
separate database role that only holds INSERT and SELECT it cannot remove the
triggers either (see ``apps/backend/README.md`` for exactly what this does and
does not guarantee).

Migration ``0087`` (issue #87, Decision 0010, approved in Decision 0023 on 2026-09-26)
adds the nullable ``details`` column for the one action whose record needs more than
ids and enum values: the persistent audit of an external research send
(``research.external_send``). **``details`` is allowed only for registered actions**:
an action that has no closed schema for it (every action of the audit trail except
that one, existing or invented) has ``details`` NULL, whatever a writer with INSERT
tries. Two CHECK constraints do it. ``ck_audit_events_details_registered``: ``details``
is NULL, or the row's ``action`` is one of ``DETAILS_ACTIONS`` and ``details`` is a
JSON object of at most ``MAX_DETAILS_BYTES`` bytes as text. And one closed-schema
constraint per registered action (today ``ck_audit_events_external_send_details``):
a row of that action has exactly the keys of ``EXTERNAL_SEND_DETAILS_KEYS`` with the
value shapes of ``external_send_check_sql`` (a ``sha256:`` fingerprint, counts, a
boolean and provider kind tokens: no field can hold a query). Registering another
action takes a new migration that adds it to ``DETAILS_ACTIONS`` (the registry
constraint) and adds its own closed-schema constraint. ``AuditEvent`` (``audit.py``)
has no ``details`` field and is unchanged; ``paw_backend.research.privacy.audit``
writes that row.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base

# The action of the persistent audit of an external research send, and what its
# row says (migration 0087 repeats these literals on purpose: a migration is a
# frozen snapshot, ``tests/test_privacy_audit_schema.py`` fails when they drift).
EXTERNAL_SEND_ACTION = "research.external_send"
EXTERNAL_SEND_REASON = "send_authorized"
# ``details`` of that row: the keys, in the order the audit writes them.
EXTERNAL_SEND_DETAILS_KEYS = (
    "query_fingerprint",
    "query_chars",
    "provider_kinds",
    "withheld",
    "credentials_removed",
    "pieces_matched",
    "abstractions",
    "truncated",
)
EXTERNAL_SEND_WITHHELD_KEYS = (
    "private_source",
    "private_memory",
    "raw_conversation",
    "secret",
)
# ``details`` is at most this many bytes as JSON text (a real one has about 300).
MAX_DETAILS_BYTES = 2048
# The registry: the actions that may have ``details`` at all. Each one needs a
# closed-schema CHECK of its own (``external_send_check_sql`` for this one); an
# action that is not here has ``details`` NULL. Adding one is a new migration.
DETAILS_ACTIONS = (EXTERNAL_SEND_ACTION,)
DETAILS_REGISTERED_CHECK = (
    "details IS NULL OR (action IN ("
    + ", ".join(f"'{action}'" for action in DETAILS_ACTIONS)
    + ") AND jsonb_typeof(details) = 'object' "
    f"AND octet_length(details::text) <= {MAX_DETAILS_BYTES})"
)


def _keys(names: tuple[str, ...]) -> str:
    return "ARRAY[" + ", ".join(f"'{name}'" for name in names) + "]"


def external_send_check_sql() -> str:
    """The CHECK of the rows of ``research.external_send``, as one SQL expression.

    A row of that action is an ``allow`` with the fixed reason, names a project
    and has ``details`` with exactly the keys above: a fingerprint
    (``sha256:`` and 64 hex digits), the query length and the counts (JSON numbers
    written as plain non-negative integers), ``truncated`` (a boolean), one to
    eight provider kind tokens (lower case letters, digits, ``_``) and the counts of
    withheld pieces per label. PostgreSQL does not promise an order of evaluation
    inside an ``AND``, so nothing here casts a value: everything is a type test or
    a regular expression on text. ``COALESCE`` turns a missing key (NULL) into a
    violation instead of a pass.
    """
    number = "'^(0|[1-9][0-9]{0,6})$'"
    conditions = [
        "decision = 'allow'",
        f"reason = '{EXTERNAL_SEND_REASON}'",
        "project_id IS NOT NULL",
        "jsonb_typeof(details) = 'object'",
        f"details - {_keys(EXTERNAL_SEND_DETAILS_KEYS)} = '{{}}'::jsonb",
        "jsonb_typeof(details -> 'query_fingerprint') = 'string'",
        "details ->> 'query_fingerprint' ~ '^sha256:[0-9a-f]{64}$'",
        "jsonb_typeof(details -> 'query_chars') = 'number'",
        "details ->> 'query_chars' ~ '^[1-9][0-9]{0,2}$'",
        "jsonb_typeof(details -> 'provider_kinds') = 'array'",
        "(details -> 'provider_kinds')::text ~ "
        '\'^\\["[a-z][a-z0-9_]{0,31}"(, "[a-z][a-z0-9_]{0,31}"){0,7}\\]$\'',
        "jsonb_typeof(details -> 'withheld') = 'object'",
        f"(details -> 'withheld') - {_keys(EXTERNAL_SEND_WITHHELD_KEYS)}"
        " = '{}'::jsonb",
    ]
    for label in EXTERNAL_SEND_WITHHELD_KEYS:
        conditions.append(
            f"jsonb_typeof(details -> 'withheld' -> '{label}') = 'number'"
        )
        conditions.append(f"details -> 'withheld' ->> '{label}' ~ {number}")
    for name in ("credentials_removed", "pieces_matched", "abstractions"):
        conditions.append(f"jsonb_typeof(details -> '{name}') = 'number'")
        conditions.append(f"details ->> '{name}' ~ {number}")
    conditions.append("jsonb_typeof(details -> 'truncated') = 'boolean'")
    return (
        f"action <> '{EXTERNAL_SEND_ACTION}' OR COALESCE("
        + " AND ".join(conditions)
        + ", false)"
    )


class AuditEventRecord(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("decision IN ('allow', 'deny')", name="decision_valid"),
        CheckConstraint(DETAILS_REGISTERED_CHECK, name="details_registered"),
        CheckConstraint(external_send_check_sql(), name="external_send_details"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    # Server-generated, shared by the decisions of one request.
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # The application's clock (when the decision was made) ...
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # ... and the database's clock (when the row was stored), which the
    # application cannot choose.
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    # Who (a user, or the user an agent acts for) and in which role.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor_role: Mapped[str | None] = mapped_column(Text)
    # Set when an agent performed the action on behalf of ``actor_id``.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(Text)
    resource_kind: Mapped[str] = mapped_column(Text)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # "inherit" or "override" for a repository resource, else NULL.
    repo_acl: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    # Set for a change of a user's system role: the role before and after.
    old_role: Mapped[str | None] = mapped_column(Text)
    new_role: Mapped[str | None] = mapped_column(Text)
    # The X-Request-ID the *client* sent (validated, at most 64 characters). It
    # is only a hint for correlating with client logs; it can be forged, so use
    # ``correlation_id`` to tie rows together.
    client_request_id: Mapped[str | None] = mapped_column(Text)
    # Only ``research.external_send`` rows have it (see the module docstring):
    # counts, a query fingerprint and provider kinds, never a query. NULL, not JSON
    # ``null``, when absent.
    details: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
