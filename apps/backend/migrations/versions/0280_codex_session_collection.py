"""What a Codex session leaves for its late collection (issue #280, Decision
0096, 2 and 5).

Revision ID: 0280
Revises: 0279
Create Date: 2026-10-22

A Codex CLI that could not be stopped may refresh its ChatGPT login (a
single-use refresh token) after the session's immediate read-back. Decision
0096 keeps the connection's session slot until the login was collected after
its unit was confirmed stopped, across a restart of the backend:

* ``connection_session_leases`` gains what a later collection needs, written
  BEFORE the login is placed (before the unit starts): ``session`` (the
  session's UUID: its unit and its directory), ``secret_handle`` (the
  handle the session was given, an opaque id, never the value) and
  ``expected_version`` (the version it was given, moved on by the immediate
  write-back); and ``kept`` (its CLI could not be stopped, or the slot was
  taken over by the collection loop). A row with a ``session`` is never taken
  over by an ordinary acquisition, even once expired: the collection loop
  takes it over (Decision 0096, 1 D and 2 B). The three columns are all set
  or all NULL; ``kept`` needs a ``session``.
* ``shared_connections`` gains ``suspect_at`` and
  ``suspect_reason`` (``missing`` / ``invalid``): a collection
  that could not read a login back (Decision 0096, 5) marks the connection in
  the same transaction that gives the slot back, so System Health keeps
  reporting ``collect_failed`` until a health check finds the connection
  ``connected`` or the credential is replaced. Both set or both NULL.

Privileges of the application role (``PAW_APP_DATABASE_ROLE``): UPDATE of the
new columns (the table privileges stay). ``paw_compatibility`` is ``expand``
(Decision 0079, 4): an older release never names the columns, its inserts
take NULL and ``false``, and a row it binds nothing to it treats as before.
``downgrade()`` drops the columns (a kept session is then an ordinary lease
that expires: development and test only).

Written on ``0239``; re-chained onto ``0279`` (#279, merged first).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import configured_app_role

revision: str = "0280"
down_revision: str | Sequence[str] | None = "0279"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Decision 0079 4: what an older release can do with this schema.
paw_compatibility: str = "expand"

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_codex_session_collection_migration.py`` compares them with
# the model.
LEASES = "connection_session_leases"
CONNECTIONS = "shared_connections"
LEASE_COLUMNS = ("session", "secret_handle", "expected_version", "kept")
CONNECTION_COLUMNS = ("suspect_at", "suspect_reason")
SUSPICIONS = ("missing", "invalid")
HANDLE_PATTERN = "^cred_[0-9a-f]{32}$"


def _grant(statement: str) -> None:
    """Run ``statement`` for the application role (none configured: nothing)."""
    role = configured_app_role()
    if role is None:
        return
    quoted = op.get_context().dialect.identifier_preparer.quote_identifier(role)
    op.execute(statement.format(role=quoted))


def upgrade() -> None:
    op.add_column(LEASES, sa.Column("session", sa.Uuid(), nullable=True))
    op.add_column(LEASES, sa.Column("secret_handle", sa.Text(), nullable=True))
    op.add_column(LEASES, sa.Column("expected_version", sa.BigInteger(), nullable=True))
    op.add_column(
        LEASES,
        sa.Column("kept", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_check_constraint(
        op.f("ck_connection_session_leases_session_complete"),
        LEASES,
        "(session IS NULL) = (secret_handle IS NULL)"
        " AND (session IS NULL) = (expected_version IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_connection_session_leases_kept_has_session"),
        LEASES,
        "NOT kept OR session IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_connection_session_leases_version_positive"),
        LEASES,
        "expected_version IS NULL OR expected_version >= 1",
    )
    op.create_check_constraint(
        op.f("ck_connection_session_leases_handle_shape"),
        LEASES,
        f"secret_handle IS NULL OR secret_handle ~ '{HANDLE_PATTERN}'",
    )
    op.add_column(
        CONNECTIONS,
        sa.Column("suspect_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(CONNECTIONS, sa.Column("suspect_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_shared_connections_suspect_complete"),
        CONNECTIONS,
        "(suspect_at IS NULL) = (suspect_reason IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_shared_connections_suspect_reason_valid"),
        CONNECTIONS,
        "suspect_reason IS NULL OR suspect_reason IN ("
        + ", ".join(f"'{reason}'" for reason in SUSPICIONS)
        + ")",
    )
    _grant(f"GRANT UPDATE ({', '.join(LEASE_COLUMNS)}) ON {LEASES} TO {{role}}")
    _grant(
        f"GRANT UPDATE ({', '.join(CONNECTION_COLUMNS)}) ON {CONNECTIONS} TO {{role}}"
    )


def downgrade() -> None:
    # Dropping a column also drops its constraints and its column privilege.
    for column in CONNECTION_COLUMNS:
        op.drop_column(CONNECTIONS, column)
    for column in LEASE_COLUMNS:
        op.drop_column(LEASES, column)
