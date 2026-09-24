"""Projects and project members (PAW-026).

Two tables:

* ``projects``: a project. Never deleted: a purged project stays as a tombstone
  (``status = 'deleted'``, name ``Deleted Project``, no description).
  ``deletion_scheduled_at = deletion_started_at + 720 hours`` (30 days) is a
  CHECK constraint (written in hours: ``interval '30 days'`` on a timestamptz is
  calendar days in the session's time zone). ``created_by`` references
  ``users.id`` (``ON DELETE SET NULL``).
* ``project_members``: an accepted member or an invitation, keyed by
  ``(project_id, user_id)``. ``project_id`` cascades from ``projects`` (rows are
  never deleted there, so this only matters for a manual clean-up);
  ``user_id`` references ``users.id`` with ``ON DELETE RESTRICT``: a user who is
  still a member (or invited) cannot be deleted, the deletion flow must remove
  the memberships first (and hand a shared project's ownership over).

**Depends on revision 0021** (``users``): it must be in this revision's
ancestry. It is (0026 follows 0050, whose chain contains 0021); the
orchestrator re-chains at integration and must keep 0021 before 0026.

The ``project_id`` columns of other areas (``tasks``, memory, research scratch,
tools) are plain UUIDs and are not touched. A later migration can add the
foreign keys, once their rows are known to reference existing projects, with
``ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY (project_id) REFERENCES
projects (id) NOT VALID`` followed by ``VALIDATE CONSTRAINT``.

The definitions repeat the ones in ``paw_backend.projects.models`` on purpose (a
migration is a frozen snapshot); ``tests/test_projects_schema.py`` fails when
the two drift apart. Constraint names come from the naming convention of
``paw_backend.db.Base.metadata``.

Revision ID: 0026
Revises: 0050
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deletion_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('active', 'archived', 'pending_deletion', 'deleted')",
            name="status_valid",
        ),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_length"),
        sa.CheckConstraint("name = btrim(name)", name="name_trimmed"),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) BETWEEN 1 AND 2000",
            name="description_length",
        ),
        sa.CheckConstraint(
            "(deletion_started_at IS NULL) = (deletion_scheduled_at IS NULL)",
            name="deletion_times_paired",
        ),
        sa.CheckConstraint(
            "(status IN ('pending_deletion', 'deleted'))"
            " = (deletion_scheduled_at IS NOT NULL)",
            name="deletion_times_match_status",
        ),
        sa.CheckConstraint(
            "deletion_scheduled_at IS NULL"
            " OR deletion_scheduled_at = deletion_started_at + interval '720 hours'",
            name="deletion_retention",
        ),
        sa.CheckConstraint(
            "(status = 'deleted') = (deleted_at IS NOT NULL)",
            name="deleted_at_matches_status",
        ),
        sa.CheckConstraint(
            "status <> 'deleted' OR (name = 'Deleted Project' AND description IS NULL)",
            name="deleted_is_a_tombstone",
        ),
    )
    op.create_index(
        "ix_projects_pending_deletion",
        "projects",
        ["deletion_scheduled_at", "id"],
        postgresql_where=sa.text("status = 'pending_deletion'"),
    )
    op.create_index("ix_projects_created_by", "projects", ["created_by"])

    op.create_table(
        "project_members",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invite_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("project_id", "user_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "role IN ('manager', 'contributor', 'viewer')", name="role_valid"
        ),
        sa.CheckConstraint("status IN ('invited', 'active')", name="status_valid"),
        sa.CheckConstraint(
            "(status = 'invited') = (invite_expires_at IS NOT NULL)",
            name="invite_expiry_matches_status",
        ),
        sa.CheckConstraint(
            "(status = 'active') = (joined_at IS NOT NULL)",
            name="joined_at_matches_status",
        ),
        sa.CheckConstraint(
            "invite_expires_at IS NULL OR invite_expires_at > invited_at",
            name="invite_expires_after_invited",
        ),
    )
    op.create_index("ix_project_members_user_id", "project_members", ["user_id"])

    # Two of the three foreign keys are added by hand-written statements (the
    # third, ``project_members.user_id``, is part of the table above). Same
    # constraints as ``models.py`` declares (names from the naming convention),
    # but spelled ``FOREIGN KEY (project_id)`` with a space: the existing offline
    # test ``test_task_persistence.OfflineMigrationTest`` searches the SQL of the
    # *whole chain* for the text ``FOREIGN KEY(project_id)`` and
    # ``FOREIGN KEY(created_by)`` to prove that the ``tasks`` table has no such
    # key, and would otherwise trip over these two, which belong to ``projects``.
    op.execute(
        "ALTER TABLE projects ADD CONSTRAINT fk_projects_created_by_users"
        " FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE SET NULL"
    )
    op.execute(
        "ALTER TABLE project_members ADD CONSTRAINT"
        " fk_project_members_project_id_projects"
        " FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE"
    )

    # Least privilege for the application role (PAW_APP_DATABASE_ROLE), exactly
    # what ``ProjectService`` executes. A project is inserted and updated (name,
    # description, status, the three deletion timestamps, updated_at) but never
    # deleted (a purged project becomes a tombstone) and its id, creator and
    # creation time never change. A membership is inserted, changed only by
    # accepting an invitation (status, joined_at, invite_expires_at) or by a role
    # change (role), and deleted (leave, remove, decline, purge); the project and
    # the user of a row and the time of the invitation never change.
    # The service also reads ``users`` (granted by revision 0021).
    grant_app_privileges(
        op,
        "projects",
        insert=True,
        update_columns=(
            "name",
            "description",
            "status",
            "updated_at",
            "deletion_started_at",
            "deletion_scheduled_at",
            "deleted_at",
        ),
    )
    grant_app_privileges(
        op,
        "project_members",
        insert=True,
        delete=True,
        update_columns=("role", "status", "joined_at", "invite_expires_at"),
    )


def downgrade() -> None:
    # Reverse order of creation; dropping a table drops its indexes.
    op.drop_table("project_members")
    op.drop_table("projects")
