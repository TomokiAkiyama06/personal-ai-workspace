"""Index of the freshness jobs of Memory versioning (PAW-042).

One index, no table: ``ix_memory_versions_freshness_due`` on
``memory_versions (freshness_policy)`` over the **active** versions whose policy is
not ``permanent``. ``paw_backend.memory.versioning.freshness`` marks stale
candidates (``revalidate`` past its interval or hit by an event, ``repo_commit``
behind the repository's head), deprecates expired ``expiring`` versions and ends
``session_only`` ones; each run looks for the active versions of one policy. The
history (superseded, deprecated, history versions) grows without bound and most
memories are ``permanent``, so without this index every run would read all of them.

No column, constraint or trigger changes: the freshness columns and the history of
``status`` / ``stale_since`` changes exist since revisions 0040 and 0071. No table is
created, so nothing is granted.

The definition repeats the one in ``paw_backend.memory.models``; Alembic's
autogenerate shows no difference (``tests/test_memory_freshness_migration.py``).

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | Sequence[str] | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_memory_versions_freshness_due"
PREDICATE = "status = 'active' AND freshness_policy <> 'permanent'"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "memory_versions",
        ["freshness_policy"],
        postgresql_where=sa.text(PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="memory_versions")
