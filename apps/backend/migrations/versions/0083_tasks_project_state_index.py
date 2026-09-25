"""An index on ``tasks (project_id, state)`` (Issue #83, Decision 0008, section 8).

``tasks.project_id`` had no index (PAW-032: the projects did not exist yet). The
project module reads a project's tasks by state: the stop processor of a deleted
project lists its active tasks and asks "is any task of the project active?"
(``paw_backend.projects.store``), and the Project state gate of the task lane
(``tasks.project_gate``) names the project of every task it guards. Without an
index each of those reads scans every task ever created.

The index is a plain (non-partial, non-unique) btree over the two columns. A
statement uses it for ``project_id = ? AND state IN (...)`` when the states are
written into the SQL text (``literal_execute``): see the plan tests in
``tests/test_tasks_project_index.py``. No table is created, so no privilege
changes: the application role already holds SELECT on ``tasks``.

``CREATE INDEX`` blocks writes to ``tasks`` while it runs (it is not built
``CONCURRENTLY``: Alembic runs the revision in a transaction). The table is small
when this revision is applied; the cost is a short pause of task writes.

Revision ID: 0083
Revises: 0022
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0083"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_tasks_project_id_state", "tasks", ["project_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_tasks_project_id_state", table_name="tasks")
