"""ORM models of the projects (Alembic revision ``0026``).

* ``projects``: one row per project, **never deleted**. Deleting a project
  (after the 30 days of Pending deletion) turns the row into a tombstone:
  ``status = 'deleted'``, the name becomes ``Deleted Project`` and the
  description is dropped; the id, the creator's opaque id and the timestamps
  stay (``REQUIREMENTS.md``: the audit trail keeps a minimum as
  ``Deleted Project``). ``deletion_scheduled_at = deletion_started_at + 30 days``
  (720 hours) is a CHECK constraint.
* ``project_members``: an accepted member (``status = 'active'``) or an
  invitation (``status = 'invited'``, with an expiry) of one project, keyed by
  ``(project_id, user_id)``: a user has at most one row per project. Membership
  rows are deleted when the member leaves or is removed, an invitation is
  declined or withdrawn, and when the project is purged.

``project_members.user_id`` and ``projects.created_by`` are foreign keys to
``users`` (revision ``0021``). The ``project_id`` columns of other areas (tasks,
memory, research scratch, ...) are plain UUIDs and stay so; see the README
("Project CRUD") for how a later migration can add the foreign keys.

Allowed values are ``text`` columns with CHECK constraints. The migration
repeats the literals; ``tests/test_projects_schema.py`` fails when the two
drift apart.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base
from paw_backend.identity.models import (
    UserRow,  # noqa: F401  (FK target in the metadata)
)

TABLE_NAMES = ("projects", "project_members")

_UUID_DEFAULT = text("gen_random_uuid()")
_ACTIVE = text("'active'")


class ProjectRow(Base):
    """One project (or the tombstone of a deleted one)."""

    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'archived', 'pending_deletion', 'deleted')",
            name="status_valid",
        ),
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_length"),
        CheckConstraint("name = btrim(name)", name="name_trimmed"),
        CheckConstraint(
            "description IS NULL OR char_length(description) BETWEEN 1 AND 2000",
            name="description_length",
        ),
        # Both deletion timestamps or neither.
        CheckConstraint(
            "(deletion_started_at IS NULL) = (deletion_scheduled_at IS NULL)",
            name="deletion_times_paired",
        ),
        # They are set exactly while the project is Pending deletion or Deleted.
        CheckConstraint(
            "(status IN ('pending_deletion', 'deleted'))"
            " = (deletion_scheduled_at IS NOT NULL)",
            name="deletion_times_match_status",
        ),
        # Pending deletion lasts 30 days (REQUIREMENTS.md "Project lifecycle"),
        # written as 720 hours: ``interval '30 days'`` on a timestamptz adds
        # calendar days in the session's time zone and can be 23 or 25 hours off
        # across a daylight saving change.
        CheckConstraint(
            "deletion_scheduled_at IS NULL"
            " OR deletion_scheduled_at = deletion_started_at + interval '720 hours'",
            name="deletion_retention",
        ),
        CheckConstraint(
            "(status = 'deleted') = (deleted_at IS NOT NULL)",
            name="deleted_at_matches_status",
        ),
        # What a Deleted project keeps: no name, no description.
        CheckConstraint(
            "status <> 'deleted' OR (name = 'Deleted Project' AND description IS NULL)",
            name="deleted_is_a_tombstone",
        ),
        # The purge finds the projects that are due through this index.
        Index(
            "ix_projects_pending_deletion",
            "deletion_scheduled_at",
            "id",
            postgresql_where=text("status = 'pending_deletion'"),
        ),
        Index("ix_projects_created_by", "created_by"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, server_default=_UUID_DEFAULT
    )
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=_ACTIVE)
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deletion_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    deletion_scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectMemberRow(Base):
    """An accepted member or an invitation of one project."""

    __tablename__ = "project_members"
    __table_args__ = (
        CheckConstraint(
            "role IN ('manager', 'contributor', 'viewer')", name="role_valid"
        ),
        CheckConstraint("status IN ('invited', 'active')", name="status_valid"),
        # An expiry exists exactly while the row is an invitation ...
        CheckConstraint(
            "(status = 'invited') = (invite_expires_at IS NOT NULL)",
            name="invite_expiry_matches_status",
        ),
        # ... and a join time exactly once it was accepted.
        CheckConstraint(
            "(status = 'active') = (joined_at IS NOT NULL)",
            name="joined_at_matches_status",
        ),
        CheckConstraint(
            "invite_expires_at IS NULL OR invite_expires_at > invited_at",
            name="invite_expires_after_invited",
        ),
        # "Which projects is this user in" (and the RESTRICT check of users).
        Index("ix_project_members_user_id", "user_id"),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    invited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
