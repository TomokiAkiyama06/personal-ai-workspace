"""Session usage rows of the shared connections (Decision 0083, 5).

Revision ID: 0194
Revises: 0193
Create Date: 2026-10-12

A CLI session through a shared connection (``ConnectionService.execute_session``)
counts its tokens turn by turn while it runs (Decision 0083, 5 (b), which
supersedes this part of Decision 0016, 5 for sessions only):

* ``connection_usage.session`` (boolean, ``false`` for every existing row and
  every one-prompt call): the row is a session's;
* the CHECK ``no_tokens_in_flight`` now holds for the rows that are not a
  session's only: a session's ``in_flight`` row carries what was counted so far
  (the reaper keeps it when it closes an abandoned row).

Adding a column with a constant default rewrites nothing (a short ACCESS
EXCLUSIVE lock on ``connection_usage``); the CHECK is replaced in the same
transaction. ``paw_compatibility`` is ``expand`` (Decision 0079, 4): an older
release never names the column, its inserts take the default, and the relaxed
CHECK accepts every row it writes. ``downgrade()`` forgets the tokens counted so
far by the sessions still ``in_flight`` (the old CHECK would not hold), puts the
old CHECK back and drops the column (development and test only).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0194"
down_revision: str | Sequence[str] | None = "0193"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Decision 0079 4: what an older release can do with this schema.
paw_compatibility: str = "expand"


def upgrade() -> None:
    op.add_column(
        "connection_usage",
        sa.Column("session", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.drop_constraint(
        op.f("ck_connection_usage_no_tokens_in_flight"),
        "connection_usage",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_connection_usage_no_tokens_in_flight"),
        "connection_usage",
        "session OR status <> 'in_flight'"
        " OR (input_tokens IS NULL AND output_tokens IS NULL)",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE connection_usage SET input_tokens = NULL, output_tokens = NULL"
        " WHERE session AND status = 'in_flight'"
    )
    op.drop_constraint(
        op.f("ck_connection_usage_no_tokens_in_flight"),
        "connection_usage",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_connection_usage_no_tokens_in_flight"),
        "connection_usage",
        "status <> 'in_flight' OR (input_tokens IS NULL AND output_tokens IS NULL)",
    )
    op.drop_column("connection_usage", "session")
