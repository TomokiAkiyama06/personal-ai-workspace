"""Baseline: an empty schema.

Revision ID: 0001
Revises:
Create Date: 2026-09-24
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Intentionally empty: it only puts `alembic_version` in place so that
    # later issues (users, tasks, memory with pgvector) start from a versioned
    # database.
    pass


def downgrade() -> None:
    pass
