"""Persistent audit of external research sends: ``audit_events.details`` (#87).

Revision ID: 0087
Revises: 0026
Create Date: 2026-09-25

Decision 0010 (approved 2026-09-25) lets the Research Privacy Filter send a query
out only after it has recorded the send, and makes a persistent record the
condition for feeding private-derived context into research. This revision, and
its choices (a JSONB column on ``audit_events``, the CHECK shapes, ``NOT VALID``,
what a downgrade destroys), are proposed in Decision 0023 (Proposed, not yet
approved by the human; see
``docs/decisions/0023-audit-events-details-for-external-send.md``). The record (the
SHA-256 of the query, its length, the provider kinds, the project, how much was
removed) does not fit the columns of ``audit_events`` (ids and enum values), so
this revision adds one nullable JSONB column, ``details``, and two CHECK
constraints that keep it from becoming a free-text column:

* ``ck_audit_events_details_registered``: **``details`` is allowed only for
  registered actions.** It is NULL, or the row's ``action`` is in the registry (today
  only ``research.external_send``) and ``details`` is a JSON object of at most 2048
  bytes (as text). Every other action, existing or invented, has ``details`` NULL:
  a writer with INSERT cannot keep text in such a row, whatever the object looks
  like (Codex review of PR #99).
* ``ck_audit_events_external_send_details``: the closed schema of the one registered
  action. A row whose ``action`` is ``research.external_send`` is an ``allow`` with
  the reason ``send_authorized``, names a ``project_id`` and has ``details`` with
  exactly eight keys (a ``sha256:`` fingerprint, the query length, the provider
  kinds, the counts of withheld pieces per label, three removal counts and
  ``truncated``), each of the shape the constraint spells out. No key can hold a
  query, however the row got there.

Registering another action that needs ``details`` takes a NEW migration: add it to the
registry (replace ``ck_audit_events_details_registered``) and add a closed-schema
constraint of its own. Nothing is registered by default.

Nothing else changes: the append-only triggers, the ``recorded_at`` trigger and the
grants of revision 0025 are untouched. The privileges of the application role on
``audit_events`` are table-level INSERT and SELECT, so they already cover the new
column: this revision grants nothing (``tests/test_privacy_audit_grants.py``
asserts that the role can still not UPDATE or DELETE, column by column).

Both constraints are added ``NOT VALID`` and are not validated afterwards.
PostgreSQL enforces a ``NOT VALID`` constraint on every row inserted from then on
(the table is append-only: there are no updates), and it does not scan the
(ever growing) table under an ``ACCESS EXCLUSIVE`` lock. Nothing that exists when
this revision is first applied can violate them; the only rows that could are those
of ``research.external_send`` that a later downgrade left without their
``details``, and a validating upgrade would then fail on them (a downgrade followed
by an upgrade must work).

The definitions repeat the ones in ``paw_backend.authz.models`` on purpose (a
migration is a frozen snapshot); ``tests/test_privacy_audit_schema.py`` fails when
the two drift apart.

``downgrade()`` drops the column and so DESTROYS THE ``details`` OF EVERY
RECORDED EXTERNAL SEND (the rows stay, their fingerprints and counts are gone).
Development and test databases only; never run it in production.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0087"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTION = "research.external_send"
_REASON = "send_authorized"
_KEYS = (
    "query_fingerprint",
    "query_chars",
    "provider_kinds",
    "withheld",
    "credentials_removed",
    "pieces_matched",
    "abstractions",
    "truncated",
)
_WITHHELD = ("private_source", "private_memory", "raw_conversation", "secret")
_MAX_DETAILS_BYTES = 2048
# The actions that may have ``details``: each has a closed-schema CHECK of its own
# below. Any other action has ``details`` NULL.
_ACTIONS = (_ACTION,)
_DETAILS_REGISTERED = (
    "details IS NULL OR (action IN ("
    + ", ".join(f"'{action}'" for action in _ACTIONS)
    + ") AND jsonb_typeof(details) = 'object' "
    f"AND octet_length(details::text) <= {_MAX_DETAILS_BYTES})"
)


def _keys(names: tuple[str, ...]) -> str:
    return "ARRAY[" + ", ".join(f"'{name}'" for name in names) + "]"


def _external_send_check() -> str:
    number = "'^(0|[1-9][0-9]{0,6})$'"
    conditions = [
        "decision = 'allow'",
        f"reason = '{_REASON}'",
        "project_id IS NOT NULL",
        "jsonb_typeof(details) = 'object'",
        f"details - {_keys(_KEYS)} = '{{}}'::jsonb",
        "jsonb_typeof(details -> 'query_fingerprint') = 'string'",
        "details ->> 'query_fingerprint' ~ '^sha256:[0-9a-f]{64}$'",
        "jsonb_typeof(details -> 'query_chars') = 'number'",
        "details ->> 'query_chars' ~ '^[1-9][0-9]{0,2}$'",
        "jsonb_typeof(details -> 'provider_kinds') = 'array'",
        "(details -> 'provider_kinds')::text ~ "
        '\'^\\["[a-z][a-z0-9_]{0,31}"(, "[a-z][a-z0-9_]{0,31}"){0,7}\\]$\'',
        "jsonb_typeof(details -> 'withheld') = 'object'",
        f"(details -> 'withheld') - {_keys(_WITHHELD)} = '{{}}'::jsonb",
    ]
    for label in _WITHHELD:
        conditions.append(
            f"jsonb_typeof(details -> 'withheld' -> '{label}') = 'number'"
        )
        conditions.append(f"details -> 'withheld' ->> '{label}' ~ {number}")
    for name in ("credentials_removed", "pieces_matched", "abstractions"):
        conditions.append(f"jsonb_typeof(details -> '{name}') = 'number'")
        conditions.append(f"details ->> '{name}' ~ {number}")
    conditions.append("jsonb_typeof(details -> 'truncated') = 'boolean'")
    return f"action <> '{_ACTION}' OR COALESCE(" + " AND ".join(conditions) + ", false)"


_CONSTRAINTS = (
    ("ck_audit_events_details_registered", _DETAILS_REGISTERED),
    ("ck_audit_events_external_send_details", _external_send_check()),
)


def upgrade() -> None:
    op.add_column(
        "audit_events",
        sa.Column("details", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    for name, condition in _CONSTRAINTS:
        op.create_check_constraint(
            op.f(name), "audit_events", sa.text(condition), postgresql_not_valid=True
        )


def downgrade() -> None:
    # DESTROYS the details of every recorded external send (see the docstring).
    for name, _ in reversed(_CONSTRAINTS):
        op.drop_constraint(op.f(name), "audit_events", type_="check")
    op.drop_column("audit_events", "details")
