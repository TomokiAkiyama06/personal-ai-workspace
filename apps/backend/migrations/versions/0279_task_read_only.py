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
EXCLUSIVE lock on ``tasks``). The revision does **not** declare
``paw_compatibility = "expand"`` (Decision 0079, 4; Codex review of #284): the
release before it runs on this schema, but it would run an unfinished read-only
task as an ordinary one (its Start needs a ``target``; a DAG that succeeded goes
to evaluation and waits for a pull request), so an Application-only rollback
would break every such task. Undeclared, it is not backward compatible, and a
rollback over it restores the database from its restore point.

``downgrade()`` drops the column (development and test only). A read-only task
that can still run (any state but ``completed``: a failed or cancelled one can
be retried or restarted) would then be run as an ordinary one, so the downgrade
is refused while one exists (a completed one keeps its history in
``task_events``).

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

# Decision 0079 4: no ``paw_compatibility`` (not backward compatible; module
# docstring). A rollback over this revision restores the database first.


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("read_only", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM tasks WHERE read_only AND state <>"
        " 'completed') THEN"
        " RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE ="
        " 'revision 0279: a read-only task that can still run would run as an"
        " ordinary one';"
        " END IF; END $$"
    )
    op.drop_column("tasks", "read_only")
