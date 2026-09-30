"""The one Passkey registration of an approved pairing's session (issue #154).

Revision ID: 0154
Revises: 0147
Create Date: 2026-09-30

Decision 0043, point 11 (option A, approved 2026-09-29): a session created by a
pairing that a trusted device approved (a Passkey Step-up and the confirmation
code, Decision 0033 point 12) counts, for the first Passkey registration of that
session only and inside the policy's Step-up window, as a recent Passkey
authentication. This revision adds what says whether that one registration is
still available:

* ``device_pairings.passkey_allowance_ended_at`` (``timestamptz``, NULL): set when
  the session the pairing created registers a Passkey (with or without a Step-up:
  "the session has not registered a Passkey yet") or when a Passkey of the user is
  revoked (a revocation forgets every Passkey Step-up of the user's sessions, and
  this allowance stands in for one). NULL while the allowance is unspent. The
  registration sets it with ``... WHERE passkey_allowance_ended_at IS NULL``
  under the user's row lock, so two registrations cannot both spend it.
* ``ck_device_pairings_allowance_needs_approval``: it can be set only on a
  ``completed`` pairing with ``approval_required`` (a User's pairing, or one that
  never completed, has no allowance to end).

Privileges: the application role may UPDATE the new column (it already updates
the other state columns of the table); nothing else changes. ``downgrade()``
drops the constraint and the column: the pairings then carry no allowance state,
which the code before this revision does not read.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import configured_app_role

revision: str = "0154"
down_revision: str | Sequence[str] | None = "0147"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "device_pairings"
COLUMN = "passkey_allowance_ended_at"
CONSTRAINT = "ck_device_pairings_allowance_needs_approval"
# Written out here (not imported): a migration keeps the rule it was written with.
ALLOWANCE_RULE = f"{COLUMN} IS NULL OR (state = 'completed' AND approval_required)"


def _grant(statement: str) -> None:
    """Run ``statement`` for the application role (none configured: nothing)."""
    role = configured_app_role()
    if role is None:
        return
    quoted = op.get_context().dialect.identifier_preparer.quote_identifier(role)
    op.execute(statement.format(role=quoted))


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(op.f(CONSTRAINT), TABLE, ALLOWANCE_RULE)
    _grant(f"GRANT UPDATE ({COLUMN}) ON {TABLE} TO {{role}}")


def downgrade() -> None:
    # Dropping the column also drops its column privilege.
    op.drop_constraint(op.f(CONSTRAINT), TABLE, type_="check")
    op.drop_column(TABLE, COLUMN)
