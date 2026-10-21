"""The read-only mark of a task (issue #279, Decision 0094, 1 A).

Revision ID: 0279
Revises: 0194
Create Date: 2026-10-21

``tasks.read_only`` (boolean, ``false`` for every existing task): the task was
created to read only. The backend then keeps its Working Set ``referenced``,
refuses a plan with a ``worker`` node and completes it when its DAG succeeded,
without an evaluation or a pull request (``tasks/domain.py``,
``READ_ONLY_TRANSITIONS``).

Adding a column with a constant default rewrites nothing (a short ACCESS
EXCLUSIVE lock on ``tasks``). ``paw_compatibility`` is ``expand`` (Decision
0079, 4): an older release never names the column and its inserts take the
default, so every task it creates is an ordinary one. ``downgrade()`` drops the
column (development and test only): a read-only task that is still active
would then be run as an ordinary one, so the downgrade is refused while one is
(a finished one keeps its history in ``task_events``).

The revision names the issue (#279) rather than the next number of the chain:
whichever of this and another revision written on the same head merges second
re-chains its ``down_revision``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0279"
down_revision: str | Sequence[str] | None = "0194"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Decision 0079 4: what an older release can do with this schema.
paw_compatibility: str = "expand"


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("read_only", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM tasks WHERE read_only AND state IN"
        " ('queued', 'running', 'waiting', 'paused', 'evaluating')) THEN"
        " RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE ="
        " 'revision 0279: an active read-only task would run as an ordinary one';"
        " END IF; END $$"
    )
    op.drop_column("tasks", "read_only")
