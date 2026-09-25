"""ORM models of repository registration (Alembic revision ``0027``).

* ``repositories``: one row per repository of a project: a canonical name (unique
  in the project, compared without case), the default branch, how it came into
  the project, and the ACL override (``acl_allowed``: ``NULL`` is ``inherit``;
  otherwise the permissions members keep, a subset of ``read`` / ``write`` /
  ``agent``; an override only narrows the project role, Decision 0004).
  ``(id, project_id)`` is unique so that the tables below can reference the pair
  and can never disagree with the repository about its project.
* ``repository_remotes``: the URLs that address a repository (the registration
  the Tool Broker's URL-to-repository mapping needs, Decision 0006 section 8).
  A URL belongs to one repository of a project.
* ``repository_checkouts``: a user's own working copy of a repository. A user has
  one checkout per repository and a directory belongs to one checkout, so
  per-user isolation holds in the database, not only in the code. ``state`` is
  ``pending`` while the directory is being created (a reservation) and ``ready``
  when it can be used.

Rows are removed with their repository (``ON DELETE CASCADE``); the files on disk
and the GitHub repository are never touched by that (``REQUIREMENTS.md``: a
project's deletion does not delete the local checkout).

``repositories.created_by`` and ``repository_checkouts.user_id`` reference
``users`` (revision ``0021``); ``repositories.project_id`` references ``projects``
(revision ``0026``).

Allowed values are ``text`` columns with CHECK constraints. The migration repeats
the literals; ``tests/test_repositories_schema.py`` fails when the two drift apart.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base
from paw_backend.identity.models import (
    UserRow,  # noqa: F401  (FK target in the metadata)
)
from paw_backend.projects.models import (
    ProjectRow,  # noqa: F401  (FK target in the metadata)
)

TABLE_NAMES = ("repositories", "repository_remotes", "repository_checkouts")

_UUID_DEFAULT = text("gen_random_uuid()")

# The regular expressions the database repeats (see ``validation.py``, which
# applies the finer rules of git's ref names on top of them).
NAME_SQL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
BRANCH_SQL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$"
REMOTE_SQL_PATTERN = r"^https://[a-z0-9.-]+/[^\s@\\?#]+$"


class RepositoryRow(Base):
    """One repository of a project."""

    __tablename__ = "repositories"
    __table_args__ = (
        CheckConstraint(
            f"name ~ '{NAME_SQL_PATTERN}' AND lower(name) NOT LIKE '%.git'",
            name="name_valid",
        ),
        CheckConstraint(
            f"default_branch ~ '{BRANCH_SQL_PATTERN}'", name="default_branch_valid"
        ),
        CheckConstraint(
            "source IN ('github_clone', 'existing_path', 'new_local', 'new_github')",
            name="source_valid",
        ),
        CheckConstraint(
            "acl_allowed IS NULL OR (acl_allowed <@ ARRAY['read', 'write', 'agent']"
            "::text[] AND cardinality(acl_allowed) <= 3)",
            name="acl_allowed_valid",
        ),
        # The pair the child tables reference.
        UniqueConstraint("id", "project_id"),
        # A name is unique in its project, compared without case (GitHub does
        # too, and the name becomes a directory name).
        Index(
            "uq_repositories_project_id_lower_name",
            "project_id",
            text("lower(name)"),
            unique=True,
        ),
        Index("ix_repositories_created_by", "created_by"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, server_default=_UUID_DEFAULT
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(Text)
    default_branch: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    acl_allowed: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RepositoryRemoteRow(Base):
    """A URL that addresses a repository."""

    __tablename__ = "repository_remotes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repository_id", "project_id"],
            ["repositories.id", "repositories.project_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            f"char_length(url) BETWEEN 10 AND 1024 AND url ~ '{REMOTE_SQL_PATTERN}'",
            name="url_valid",
        ),
        # A URL addresses one repository of a project (the Tool Broker maps a
        # URL to the repository whose remote it lies below).
        UniqueConstraint("project_id", "url"),
    )

    repository_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    url: Mapped[str] = mapped_column(Text, primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RepositoryCheckoutRow(Base):
    """One user's working copy of a repository (or its reservation)."""

    __tablename__ = "repository_checkouts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repository_id", "project_id"],
            ["repositories.id", "repositories.project_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("state IN ('pending', 'ready')", name="state_valid"),
        CheckConstraint(
            "char_length(path) BETWEEN 2 AND 1024 AND path LIKE '/%'"
            " AND path NOT LIKE '%/' AND path !~ '(^|/)\\.\\.?(/|$)'",
            name="path_valid",
        ),
        # One checkout per user per repository ...
        UniqueConstraint("repository_id", "user_id"),
        # ... and a directory belongs to one checkout.
        UniqueConstraint("path"),
        # "Which checkouts does this user have" (and the RESTRICT check of users).
        Index("ix_repository_checkouts_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, server_default=_UUID_DEFAULT
    )
    repository_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    path: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
