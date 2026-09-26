"""Grant the application role DELETE on the six provenance tables (Issue #88, 0028).

Decision 0011 (PAW-052) gave the application role SELECT and INSERT only on
``research_sources``, ``research_claims``, ``research_claim_sources``,
``research_claim_uses``, ``research_claim_relations`` and
``research_source_relations``, and left "what happens to this data when its
project is deleted" to a later Decision. Decision 0028 answers it: when a
project is purged (``ProjectService.purge_expired`` turns it into a
tombstone), its provenance is deleted, not kept forever or anonymised, by the
new ``ProvenanceStore.purge_projects``.

This revision creates no table and touches no column, constraint or index; it
only widens the six ``GRANT`` statements of revision ``0052`` to add DELETE,
through the same :func:`~paw_backend.db_roles.grant_app_privileges` helper
used to create them (passing ``select`` and ``insert`` again is required by the
helper, which always starts from ``REVOKE ALL ... FROM PUBLIC``; it re-grants
exactly the same SELECT and INSERT revision ``0052`` already granted, so this
revision's ``upgrade`` never narrows anything). UPDATE stays ungranted: nothing
here lets the application rewrite a source, a claim, a stance or a relation
(Decision 0011 section 6.1 is unchanged: a wrong record is corrected with a new
one, never edited). ``tests/test_provenance_grants.py`` runs the store's tests,
including ``purge_projects`` (``tests/test_provenance_purge.py``), under a
non-superuser role with exactly this grant.

``downgrade`` cannot use the helper (it only ever grants, on top of
``REVOKE ALL ... FROM PUBLIC``; it has no "narrow an existing grant" mode), so it
issues the ``REVOKE DELETE`` by hand, quoted the same way
``grant_app_privileges`` quotes its table and role, after the same
role-exists check (fail loudly, before anything changes; a single-role
deployment, no ``PAW_APP_DATABASE_ROLE``, has nothing to revoke from and is a
no-op, exactly as ``upgrade`` is a no-op for it).

Revision ID: 0088
Revises: 0071
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import (
    AppRoleNotFoundError,
    configured_app_role,
    grant_app_privileges,
)

revision: str = "0088"
down_revision: str | Sequence[str] | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The exact six tables of revision 0052, in the same order. A tuple (not a loop
# variable) so every ``grant_app_privileges`` call below names its table with a
# literal string: ``tests/test_migration_grants.py`` parses the source and
# cannot verify a table name that is not a literal.
_TABLES = (
    "research_sources",
    "research_claims",
    "research_claim_sources",
    "research_claim_uses",
    "research_claim_relations",
    "research_source_relations",
)


def upgrade() -> None:
    grant_app_privileges(op, "research_sources", insert=True, delete=True)
    grant_app_privileges(op, "research_claims", insert=True, delete=True)
    grant_app_privileges(op, "research_claim_sources", insert=True, delete=True)
    grant_app_privileges(op, "research_claim_uses", insert=True, delete=True)
    grant_app_privileges(op, "research_claim_relations", insert=True, delete=True)
    grant_app_privileges(op, "research_source_relations", insert=True, delete=True)


def _quoted_role_and_preparer() -> tuple[str, object] | tuple[None, None]:
    """The app role, quoted, and the dialect's identifier preparer.

    ``(None, None)`` in a single-role deployment (no ``PAW_APP_DATABASE_ROLE``):
    nothing was granted to revoke, so ``downgrade`` is a no-op for it, exactly as
    ``upgrade`` is. Online (not rendering SQL), a role that does not exist fails
    loudly here, before anything is revoked (the same check
    ``grant_app_privileges`` makes).
    """
    role = configured_app_role()
    if role is None:
        return None, None
    context = op.get_context()
    if not context.as_sql:
        found = op.get_bind().execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
        )
        if found.first() is None:
            raise AppRoleNotFoundError(
                "PAW_APP_DATABASE_ROLE names a PostgreSQL role that does not "
                "exist; create it before running this migration."
            )
    preparer = context.dialect.identifier_preparer
    return preparer.quote_identifier(role), preparer


def downgrade() -> None:
    quoted_role, preparer = _quoted_role_and_preparer()
    if quoted_role is None:
        return
    for table in _TABLES:
        quoted_table = preparer.quote(table)
        op.execute(f"REVOKE DELETE ON {quoted_table} FROM {quoted_role}")
