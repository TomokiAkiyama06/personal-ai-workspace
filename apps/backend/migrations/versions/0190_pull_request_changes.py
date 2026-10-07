"""The changed files of a delivered pull request, and the audit rows of a task by
their resource (issue #185, item 6; Decision 0078).

Revision ID: 0190
Revises: 0188
Create Date: 2026-10-07

* ``pull_request_changes``: one row per pull request record (a repository's
  state in a task attempt, ``task_attempt_repositories``) whose changed files were
  read from GitHub when the Integration Gate delivered it (``integration/
  changes.py``): the commit they were read for, whether GitHub listed more files
  than were kept, the files (path, previous path, status, lines added / deleted)
  and, aligned with them, each file's patch as shown on the PR screen (bounded,
  credentials redacted; ``null`` when GitHub gave none or it was not read). A
  record that is read again replaces its row. It goes with its record (``ON
  DELETE CASCADE``).
* ``ix_audit_events_resource``: the audit rows of one resource (a task, a tool
  approval) in time order, for the PR screen's audit rows. ``audit_events`` is
  partitioned (revision 0086): the index is made on the parent, which makes it on
  every partition (and on every partition created later). Only an index: no row
  or column of the audit trail changes.
* ``ix_tool_approvals_pending_requester``: the pending tool approvals of one
  person (the approvals they are asked for, ``GET /api/v1/approvals``). Partial:
  only ``pending`` rows, which are few.

Privileges of the application role (``PAW_APP_DATABASE_ROLE``): SELECT, INSERT
and UPDATE of the recorded columns on ``pull_request_changes`` (no DELETE:
a row goes only with its record).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from paw_backend.db_roles import grant_app_privileges

revision: str = "0190"
down_revision: str | Sequence[str] | None = "0188"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_pull_request_changes.py`` compares them with the code.
MAX_FILES = 300


def upgrade() -> None:
    op.create_table(
        "pull_request_changes",
        sa.Column("record_id", sa.BigInteger(), nullable=False),
        sa.Column("head_commit", sa.String(length=64), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("files", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("patches", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "head_commit ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'",
            name=op.f("ck_pull_request_changes_head_commit_object_id"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(files) = 'array'"
            f" AND jsonb_array_length(files) <= {MAX_FILES}"
            " AND jsonb_typeof(patches) = 'array'"
            " AND jsonb_array_length(patches) = jsonb_array_length(files)",
            name=op.f("ck_pull_request_changes_files_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["task_attempt_repositories.id"],
            name=op.f("fk_pull_request_changes_record_id_task_attempt_repositories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("record_id", name=op.f("pk_pull_request_changes")),
    )
    grant_app_privileges(
        op,
        "pull_request_changes",
        select=True,
        insert=True,
        update_columns=(
            "head_commit",
            "truncated",
            "files",
            "patches",
            "recorded_at",
        ),
    )
    op.create_index(
        "ix_audit_events_resource", "audit_events", ["resource_id", "occurred_at"]
    )
    op.create_index(
        "ix_tool_approvals_pending_requester",
        "tool_approvals",
        ["requester_user_id", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_tool_approvals_pending_requester", table_name="tool_approvals")
    op.drop_index("ix_audit_events_resource", table_name="audit_events")
    op.drop_table("pull_request_changes")
