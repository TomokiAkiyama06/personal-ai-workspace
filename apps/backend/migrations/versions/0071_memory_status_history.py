"""Record status and stale-state changes of a memory version (issue #90, PAW-040).

Revision 0040 lets the application update four columns of ``memory_versions`` in
place: ``pinned`` and ``importance`` (every change is recorded by a trigger in the
append-only ``memory_metadata_changes``, with its actor), and ``status``
(superseded / deprecated / history) and ``stale_since`` (a stale candidate is
marked, later cleared), which were overwritten without a trace. The independent
review of PAW-040 (PR #71) found that this leaves the history / graph unable to say
when or by whom a version was deprecated or marked or cleared as stale.

This revision makes the same trigger record the two columns as well, in the same
history table (no new table, so no new privilege: the application role already
holds SELECT and INSERT on it, and no UPDATE or DELETE):

* ``memory_metadata_changes`` gets ``old_status`` / ``new_status`` and
  ``old_stale_since`` / ``new_stale_since``. All four are nullable, so that the
  rows revision 0040 wrote stay valid as they are: they have no status (it was not
  recorded and cannot be known afterwards), and neither has a stale time. A row the
  trigger writes always has both statuses; a stale time of NULL then means "not
  marked". ``status_pair``, ``status_valid`` and ``stale_since_needs_status`` keep
  the two kinds of row apart, and ``something_changed`` now counts a change of
  ``status`` or ``stale_since`` too.
* ``paw_record_memory_metadata_change()`` is replaced (its ``search_path`` stays
  pinned, and it still names its table by schema, see revision 0040) and the trigger
  ``tr_memory_versions_record_metadata_change`` is recreated on
  ``UPDATE OF pinned, importance, status, stale_since``. One UPDATE of several of
  the columns is one row; an update that writes the value that is already there
  records nothing; the change time is the database's ``clock_timestamp()``.

**Decision 0026** (approved 2026-09-26,
``docs/decisions/0026-memory-status-change-history.md``) records this contract:
the history table and the Audit of Shared Memory administration (Decision 0009,
sections 12 and 13: the attempt row and the completion row) **coexist**. Audit
stays the record of the attempt and completion of an administration operation;
``memory_metadata_changes`` additionally records old / new ``status`` and
``stale_since``, the actor and the database time of EVERY status or stale change
of a version, in any scope (a delete or restore of a Shared Memory is in both).
Decision 0026 supersedes only the statements of Decision 0009 that said the Audit
is the only record of a delete / restore.

The actor comes from the same two transaction-local settings as before
(``paw_backend.memory.metadata.metadata_change_actor``), and the same rule applies:
**a change of ``status`` or ``stale_since`` without a named actor fails** (NOT NULL
``actor_type``) instead of being attributed to somebody. So every writer that
supersedes, deprecates, restores or marks a version has to name its actor first;
``paw_backend.memory.shared`` does since this revision. (The other option, to record
``system`` when nobody is named, would answer "by whom" wrongly for exactly the
writers that forgot.)

The trigger runs in the transaction of the update after its row lock is held, so
``OLD`` is the row as the last committed update left it: two concurrent updates are
recorded one after the other, and each exactly once.

Downgrade restores the function and trigger of revision 0040. The old shape cannot
hold a row that recorded no pin / importance change, so **the downgrade deletes
the history rows that recorded only a status or stale-state change, and drops the
four new columns (the status and stale values of the rows that stay)**; the rows of
revision 0040 are untouched. That data does not exist before this revision, so it
is not restorable by upgrading again. No privilege changes in either direction.

The definitions repeat the ones in ``paw_backend.memory.models`` on purpose (a
migration is a frozen snapshot); ``tests/test_memory_migration.py`` fails when they
drift apart, and ``tests/test_memory_status_history_migration.py`` runs this
revision up and down on a database that already holds history rows.

Revision ID: 0071
Revises: 0027
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0071"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "memory_metadata_changes"
_STATUSES = "('active', 'superseded', 'deprecated', 'history')"

# The trigger and its function as revision 0071 defines them. See
# ``MemoryMetadataChange`` in ``paw_backend.memory.models`` for why the function
# pins its ``search_path`` and names its table by schema.
_RECORD_FUNCTION = """\
CREATE OR REPLACE FUNCTION paw_record_memory_metadata_change()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    EXECUTE 'INSERT INTO ' || quote_ident(TG_TABLE_SCHEMA)
        || '.memory_metadata_changes ('
        || 'memory_version_id, old_pinned, new_pinned, old_importance,'
        || ' new_importance, old_status, new_status,'
        || ' old_stale_since, new_stale_since, actor_type, actor_user_id'
        || ') VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)'
    USING
        NEW.id, OLD.pinned, NEW.pinned, OLD.importance, NEW.importance,
        OLD.status, NEW.status, OLD.stale_since, NEW.stale_since,
        nullif(current_setting('paw.actor_type', true), ''),
        nullif(current_setting('paw.actor_user_id', true), '')::uuid;
    RETURN NULL;
END
$$"""
_RECORD_TRIGGER = """\
CREATE TRIGGER tr_memory_versions_record_metadata_change
AFTER UPDATE OF pinned, importance, status, stale_since ON memory_versions
FOR EACH ROW
WHEN (OLD.pinned IS DISTINCT FROM NEW.pinned
      OR OLD.importance IS DISTINCT FROM NEW.importance
      OR OLD.status IS DISTINCT FROM NEW.status
      OR OLD.stale_since IS DISTINCT FROM NEW.stale_since)
EXECUTE FUNCTION paw_record_memory_metadata_change()"""

# The function and trigger of revision 0040, restored by the downgrade (a frozen
# copy of ``_RECORD_METADATA_CHANGE_FUNCTION`` / ``_TRIGGER`` there).
_RECORD_FUNCTION_0040 = """\
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
_RECORD_TRIGGER_0040 = """\
CREATE TRIGGER tr_memory_versions_record_metadata_change
AFTER UPDATE OF pinned, importance ON memory_versions
FOR EACH ROW
WHEN (OLD.pinned IS DISTINCT FROM NEW.pinned
      OR OLD.importance IS DISTINCT FROM NEW.importance)
EXECUTE FUNCTION paw_record_memory_metadata_change()"""

_DROP_TRIGGER = (
    "DROP TRIGGER IF EXISTS tr_memory_versions_record_metadata_change"
    " ON memory_versions"
)

_SOMETHING_CHANGED = "something_changed"
_SOMETHING_CHANGED_0040 = "old_pinned <> new_pinned OR old_importance <> new_importance"
_SOMETHING_CHANGED_0071 = (
    _SOMETHING_CHANGED_0040 + " OR old_status IS DISTINCT FROM new_status"
    " OR old_stale_since IS DISTINCT FROM new_stale_since"
)
# name (without the table prefix) -> condition; these exist only from this revision on.
_NEW_CHECKS = {
    "status_pair": "(old_status IS NULL) = (new_status IS NULL)",
    "status_valid": (f"old_status IN {_STATUSES} AND new_status IN {_STATUSES}"),
    "stale_since_needs_status": (
        "old_status IS NOT NULL"
        " OR (old_stale_since IS NULL AND new_stale_since IS NULL)"
    ),
}


def _check_name(name: str) -> str:
    return op.f(f"ck_{_TABLE}_{name}")


def upgrade() -> None:
    for column in ("old_status", "new_status"):
        op.add_column(_TABLE, sa.Column(column, sa.Text(), nullable=True))
    for column in ("old_stale_since", "new_stale_since"):
        op.add_column(
            _TABLE, sa.Column(column, sa.DateTime(timezone=True), nullable=True)
        )
    # The rows of revision 0040 satisfy every new rule as they are (no status, no
    # stale time, and a pin / importance change), so the checks validate at once.
    op.drop_constraint(_check_name(_SOMETHING_CHANGED), _TABLE, type_="check")
    op.create_check_constraint(
        _check_name(_SOMETHING_CHANGED), _TABLE, sa.text(_SOMETHING_CHANGED_0071)
    )
    for name, condition in _NEW_CHECKS.items():
        op.create_check_constraint(_check_name(name), _TABLE, sa.text(condition))
    op.execute(_RECORD_FUNCTION)
    op.execute(_DROP_TRIGGER)
    op.execute(_RECORD_TRIGGER)


def downgrade() -> None:
    op.execute(_DROP_TRIGGER)
    # Rows that recorded only a status / stale-state change have no place in the
    # old shape (its CHECK wants a pin / importance change): they are deleted, see
    # the docstring. The rows of revision 0040 all have such a change.
    op.execute(
        f"DELETE FROM {_TABLE}"
        " WHERE old_pinned = new_pinned AND old_importance = new_importance"
    )
    for name in reversed(_NEW_CHECKS):
        op.drop_constraint(_check_name(name), _TABLE, type_="check")
    op.drop_constraint(_check_name(_SOMETHING_CHANGED), _TABLE, type_="check")
    op.create_check_constraint(
        _check_name(_SOMETHING_CHANGED), _TABLE, sa.text(_SOMETHING_CHANGED_0040)
    )
    for column in ("new_stale_since", "old_stale_since", "new_status", "old_status"):
        op.drop_column(_TABLE, column)
    op.execute(_RECORD_FUNCTION_0040)
    op.execute(_RECORD_TRIGGER_0040)
