"""The length limit of a memory's text (issue #147).

Revision ID: 0147
Revises: 0129
Create Date: 2026-09-29

``memory_versions.content`` had no upper bound in the schema (revision 0040
only refuses an empty text); every service that writes a version refuses a text
over 20,000 characters (``memory/versioning/limits.py``,
``memory/shared/limits.py``; the Immediate Journal's bound is lower). This
revision repeats that bound as a CHECK constraint, so a text the projection
could not scan whole (Decision 0038, 5 and 10) can no longer be stored:

* ``ck_memory_versions_content_length``: ``char_length(content) <= 20000``.

A version is never edited in place, so a text over the limit that is already
stored cannot be shortened by the application. The upgrade does not shorten or
delete it either (Decision 0053, 1): it first looks for such rows and, when there
is one, stops with a message that counts them, names the first ids and says how
to list them all. Nothing is changed then; the rows are dealt with by hand and
the upgrade is run again. The constraint is added validated, so the limit holds
for every row once the revision is applied.

No privilege changes: the application role cannot drop a constraint of a table
it does not own.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0147"
down_revision: str | Sequence[str] | None = "0129"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out here (not imported): a migration keeps the value it was written
# with. ``tests/test_memory_content_length_migration.py`` compares it with the
# model and the services.
MAX_CONTENT_CHARS = 20_000
CONSTRAINT = "ck_memory_versions_content_length"
# How many ids of overlong rows the message names; the hint lists them all.
NAMED_ROWS = 5

_LIST_ROWS = (
    "SELECT id, memory_id, status, char_length(content) FROM memory_versions"
    f" WHERE char_length(content) > {MAX_CONTENT_CHARS} ORDER BY memory_id, id"
)

_REFUSE_OVERLONG_ROWS = f"""
DO $$
DECLARE
    overlong bigint;
    named text;
BEGIN
    SELECT count(*) INTO overlong
    FROM memory_versions
    WHERE char_length(content) > {MAX_CONTENT_CHARS};
    IF overlong > 0 THEN
        SELECT string_agg(id::text, ', ' ORDER BY id) INTO named
        FROM (
            SELECT id FROM memory_versions
            WHERE char_length(content) > {MAX_CONTENT_CHARS}
            ORDER BY id
            LIMIT {NAMED_ROWS}
        ) AS first_rows;
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = format(
                'revision {revision}: %s memory_versions row(s) have content longer'
                ' than {MAX_CONTENT_CHARS} characters (first ids: %s); nothing was'
                ' changed',
                overlong, named
            ),
            HINT = 'Decision 0053: list them with: {_LIST_ROWS}; deal with them by'
                ' hand, then run the upgrade again.';
    END IF;
END
$$
"""


def upgrade() -> None:
    op.execute(_REFUSE_OVERLONG_ROWS)
    op.execute(
        f"ALTER TABLE memory_versions ADD CONSTRAINT {CONSTRAINT}"
        f" CHECK (char_length(content) <= {MAX_CONTENT_CHARS})"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE memory_versions DROP CONSTRAINT {CONSTRAINT}")
