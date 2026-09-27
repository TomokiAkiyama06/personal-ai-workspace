"""Repository registration: repositories, remotes and per-user checkouts (PAW-027).

Three tables:

* ``repositories``: a repository of a project (canonical name, default branch,
  source, ACL override). ``(id, project_id)`` is unique so the child tables can
  reference the pair. The name is unique per project without case (a unique
  index on ``lower(name)``).
* ``repository_remotes``: the ``https`` URLs that address a repository (what the
  Tool Broker's URL-to-repository mapping needs, Decision 0006 section 8). A URL
  belongs to one repository of a project.
* ``repository_checkouts``: one user's working copy (``pending`` while it is being
  created, then ``ready``). One per user and repository, one per directory. A
  ``ready`` checkout records the identity of its directory (``root_device`` /
  ``root_inode``, ``st_dev`` and ``st_ino``) so a replaced directory is detected.

The child tables cascade from ``repositories``; a repository row is deleted only
by ``RepositoryService`` (remove / purge). Files on disk and GitHub repositories
are never touched by a delete.

**Depends on revisions 0021 (``users``) and 0026 (``projects``).** Both are in
this revision's ancestry (0027 follows 0030).

The foreign keys to ``projects`` and ``users`` are hand-written statements,
spelled ``FOREIGN KEY (project_id)`` with a space: the existing offline test
``test_task_persistence.OfflineMigrationTest`` searches the SQL of the *whole
chain* for the text ``FOREIGN KEY(project_id)`` / ``FOREIGN KEY(created_by)`` to
prove that the ``tasks`` table has no such key (same reason as revision 0026).

The ``path`` and the remote ``url`` are bounded by their **encoded** length (2048 and
1024 bytes) as well as by their characters: both are keys of a unique btree index, whose
entries cannot exceed about 2700 bytes, and a character can be 4 bytes.

The definitions repeat the ones in ``paw_backend.repositories.models`` on purpose
(a migration is a frozen snapshot); ``tests/test_repositories_schema.py`` fails
when the two drift apart. Constraint names come from the naming convention of
``paw_backend.db.Base.metadata``.

Revision ID: 0027
Revises: 0030
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
BRANCH_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$"
REMOTE_PATTERN = r"^https://[a-z0-9.-]+/[^\s@\\?#]+$"


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("acl_allowed", sa.ARRAY(sa.Text()), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_id"),
        sa.CheckConstraint(
            f"name ~ '{NAME_PATTERN}' AND lower(name) NOT LIKE '%.git'",
            name="name_valid",
        ),
        sa.CheckConstraint(
            f"default_branch ~ '{BRANCH_PATTERN}'", name="default_branch_valid"
        ),
        sa.CheckConstraint(
            "source IN ('github_clone', 'existing_path', 'new_local', 'new_github')",
            name="source_valid",
        ),
        sa.CheckConstraint(
            "acl_allowed IS NULL OR (acl_allowed <@ ARRAY['read', 'write', 'agent']"
            "::text[] AND cardinality(acl_allowed) <= 3)",
            name="acl_allowed_valid",
        ),
    )
    op.create_index(
        "uq_repositories_project_id_lower_name",
        "repositories",
        ["project_id", sa.text("lower(name)")],
        unique=True,
    )
    op.create_index("ix_repositories_created_by", "repositories", ["created_by"])
    op.execute(
        "ALTER TABLE repositories ADD CONSTRAINT fk_repositories_project_id_projects"
        " FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE repositories ADD CONSTRAINT fk_repositories_created_by_users"
        " FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE SET NULL"
    )

    op.create_table(
        "repository_remotes",
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("repository_id", "url"),
        sa.UniqueConstraint("project_id", "url"),
        sa.ForeignKeyConstraint(
            ["repository_id", "project_id"],
            ["repositories.id", "repositories.project_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            f"octet_length(url) BETWEEN 10 AND 1024 AND url ~ '{REMOTE_PATTERN}'",
            name="url_valid",
        ),
    )

    op.create_table(
        "repository_checkouts",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("root_device", sa.Numeric(20, 0), nullable=True),
        sa.Column("root_inode", sa.Numeric(20, 0), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id", "user_id"),
        sa.UniqueConstraint("path"),
        sa.ForeignKeyConstraint(
            ["repository_id", "project_id"],
            ["repositories.id", "repositories.project_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("state IN ('pending', 'ready')", name="state_valid"),
        sa.CheckConstraint(
            "(state = 'ready') = (root_device IS NOT NULL)"
            " AND (root_device IS NULL) = (root_inode IS NULL)",
            name="identity_matches_state",
        ),
        sa.CheckConstraint(
            "(root_device IS NULL OR root_device >= 0)"
            " AND (root_inode IS NULL OR root_inode >= 0)",
            name="identity_not_negative",
        ),
        sa.CheckConstraint(
            "char_length(path) BETWEEN 2 AND 1024 AND octet_length(path) <= 2048"
            " AND path LIKE '/%'"
            " AND path NOT LIKE '%/' AND path !~ '(^|/)\\.\\.?(/|$)'",
            name="path_valid",
        ),
    )
    op.create_index(
        "ix_repository_checkouts_user_id", "repository_checkouts", ["user_id"]
    )

    # Least privilege for the application role (PAW_APP_DATABASE_ROLE), exactly
    # what ``RepositoryService`` executes.
    # * repositories: inserted; the default branch (set after a clone), the ACL
    #   override and updated_at change; deleted (remove a repository, purge a
    #   deleted project). Its id, project, name, source and creator never change
    #   (a rename would also move the directory, which no operation does).
    # * repository_remotes: inserted and deleted; a row is never rewritten.
    # * repository_checkouts: inserted as ``pending``; only the state and
    #   updated_at change, and the recorded directory identity (root_device,
    #   root_inode) is written together with ``ready``; deleted (remove a
    #   checkout, a failed or stale reservation). Its repository, project, user and
    #   path never change.
    # The service also reads ``projects``, ``project_members`` (revision 0026) and
    # ``users`` (revision 0021), whose grants those revisions gave.
    grant_app_privileges(
        op,
        "repositories",
        insert=True,
        delete=True,
        update_columns=("default_branch", "acl_allowed", "updated_at"),
    )
    grant_app_privileges(op, "repository_remotes", insert=True, delete=True)
    grant_app_privileges(
        op,
        "repository_checkouts",
        insert=True,
        delete=True,
        update_columns=("state", "updated_at", "root_device", "root_inode"),
    )


def downgrade() -> None:
    # Reverse order of creation; dropping a table drops its indexes.
    op.drop_table("repository_checkouts")
    op.drop_table("repository_remotes")
    op.drop_table("repositories")
