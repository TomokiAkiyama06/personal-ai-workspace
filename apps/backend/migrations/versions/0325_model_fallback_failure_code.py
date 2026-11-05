"""The failure code ``model_fallback`` (issue #325, Decision 0102, 3).

Revision ID: 0325
Revises: 0306
Create Date: 2026-11-05

Decision 0102 (approved 2026-11-05) finishes a health probe whose configured
model was refused and another model answered (or was about to: the CLI's
fallback, #325) as an ``unavailable`` that does not change the connection's
status, with the new failure code ``model_fallback``. ``FailureCode`` is the
source of the ``failure_code`` CHECK of two tables, and the human's answer 1 A
(2026-11-05) keeps it one enum, so both CHECKs are rebuilt with the new value
added (nothing else changes):

* ``connection_probes.ck_connection_probes_failure_code_valid`` (revision 0306):
  a probe's row may now be ``model_fallback``;
* ``connection_usage.ck_connection_usage_failure_code_valid`` (revision 0030):
  accepts it too, though no usage row is written with it (a node's session
  only logs its fallback: Decision 0102, 2).

Each CHECK is dropped and added again in the same transaction; adding it scans
its table under a short ACCESS EXCLUSIVE lock (every existing row holds: the
list only grows). No column, index or privilege changes.

``paw_compatibility`` is ``expand`` (Decision 0079, 4): an older release never
writes ``model_fallback``, and the wider CHECKs accept every row it writes.
After a rollback that keeps the database, the older release never reads a
probe's ``failure_code`` back (only ``trigger``, ``state``, ``outcome`` and the
times), and no usage row holds the new value, so the rows a newer release wrote
read as before (an ``unavailable`` probe).

``downgrade()`` puts the old CHECKs back; it FAILS (the CHECK does not hold)
while a row holds ``model_fallback``: development and test only.

The revision names the issue (0325 after 0306) rather than the next number of
the chain: whichever of this and another revision written on the same head
merges second re-chains its ``down_revision``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0325"
down_revision: str | Sequence[str] | None = "0306"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Decision 0079 4: what an older release can do with this schema.
paw_compatibility: str = "expand"

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_model_fallback_migration.py`` compares them with the model
# and with revisions 0030 and 0306.
PREVIOUS_FAILURE_CODES = (
    "rate_limited",
    "unavailable",
    "expired",
    "timeout",
    "invalid_response",
    "internal_error",
)
FAILURE_CODES = (*PREVIOUS_FAILURE_CODES, "model_fallback")
CHECKS = (
    ("connection_probes", "ck_connection_probes_failure_code_valid"),
    ("connection_usage", "ck_connection_usage_failure_code_valid"),
)


def _condition(values: Sequence[str]) -> str:
    listed = ", ".join(f"'{value}'" for value in values)
    return f"failure_code IS NULL OR failure_code IN ({listed})"


def _rebuild(values: Sequence[str]) -> None:
    for table, name in CHECKS:
        op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, _condition(values))


def upgrade() -> None:
    _rebuild(FAILURE_CODES)


def downgrade() -> None:
    # Fails while a row holds ``model_fallback`` (see the docstring).
    _rebuild(PREVIOUS_FAILURE_CODES)
