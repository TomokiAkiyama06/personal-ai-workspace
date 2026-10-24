"""The agent a plan assigned each node (issue #297, Decision 0099, 4).

Revision ID: 0282
Revises: 0281
Create Date: 2026-10-24

Decision 0099 (approved 2026-10-23) lets a plan name the agent each node starts
on (``agent``, among the agents ``[orchestrator.assignment]`` allows for the
node's role). The accepted plan stores the assignment explicitly, also for a
node that named none (it gets its role's default), so the record says which
agent each node was given:

* ``agent_dag_nodes.assigned_agent`` (NULL allowed, an agent label:
  ``[a-z][a-z0-9._-]{0,63}``, the ladders' and ``placement_agent``'s form). The
  node's first attempt starts on that agent's rung of the role's ladder
  (``agent_index`` stays the absolute position on the ladder: revision 0034's
  CHECK is unchanged). **Every existing row stays NULL**, and NULL means what
  it meant before: no assignment, the node starts on the first rung (a DAG
  reopened by a Retry runs as it did). A new DAG's nodes are never NULL.

Adding a NULL column without a default rewrites nothing (a short ACCESS
EXCLUSIVE lock on ``agent_dag_nodes``). The application role already has INSERT
on the table, so it writes the column with the node; it has no UPDATE of it
(revision 0034 grants UPDATE of the node's progress columns only): the
assignment is never changed after the node is stored.

``paw_compatibility`` is ``expand`` (Decision 0079, 4): an older release never
names the column. It runs a node stored by this release on the rung recorded in
``agent_index``, which is the assigned agent's position, so a rollback runs
the stored DAGs on the same agents (it only does not refuse a node whose
assigned agent was removed from the ladder).

``downgrade()`` drops the column: it DESTROYS the record of the assignments
(``agent_index`` keeps the rungs). Development and test databases only.

The revision names the issue's part (0282 after 0281) rather than the next
number of the chain: whichever of this and another revision written on the same
head merges second re-chains its ``down_revision``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0282"
down_revision: str | Sequence[str] | None = "0281"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Decision 0079 4: what an older release can do with this schema.
paw_compatibility: str = "expand"

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_dag_node_assignment_migration.py`` compares them with the
# code.
TABLE = "agent_dag_nodes"
COLUMN = "assigned_agent"
AGENT_LABEL_PATTERN = "[a-z][a-z0-9._-]{0,63}"
CHECK = "ck_agent_dag_nodes_assigned_agent_format"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=64), nullable=True))
    op.create_check_constraint(
        op.f(CHECK),
        TABLE,
        f"{COLUMN} IS NULL OR {COLUMN} ~ '^{AGENT_LABEL_PATTERN}$'",
    )


def downgrade() -> None:
    # DESTROYS the record of the assignments (see the docstring).
    op.drop_constraint(op.f(CHECK), TABLE, type_="check")
    op.drop_column(TABLE, COLUMN)
