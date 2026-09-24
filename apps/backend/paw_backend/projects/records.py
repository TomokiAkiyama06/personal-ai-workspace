"""Value objects of the project module: statuses, records and small results.

All records are immutable. The ones with a rule that ties fields together
(:class:`Member`) refuse an inconsistent combination when they are built, the
same combinations that the CHECK constraints of the database refuse.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from paw_backend.authz.roles import ProjectRole


class ProjectStatus(StrEnum):
    """Lifecycle status of a project (``REQUIREMENTS.md`` "Project lifecycle").

    The first three values are the ones of ``paw_backend.authz.ProjectState``.
    ``DELETED`` exists only here: a deleted project is a tombstone that no
    decision is ever taken on.
    """

    ACTIVE = "active"
    ARCHIVED = "archived"
    PENDING_DELETION = "pending_deletion"
    DELETED = "deleted"


class MemberStatus(StrEnum):
    """``INVITED`` is an invitation that was not accepted yet (not a member)."""

    INVITED = "invited"
    ACTIVE = "active"


class LifecycleAction(StrEnum):
    """What can be done to the lifecycle status of a project."""

    ARCHIVE = "archive"
    UNARCHIVE = "unarchive"
    BEGIN_DELETION = "begin_deletion"
    RESTORE = "restore"  # Pending deletion -> Archived
    PURGE = "purge"  # Pending deletion -> Deleted, after the 30 days


class InviteState(StrEnum):
    """What a membership row means for an invitation at one instant."""

    NONE = "none"  # no row at all
    OPEN = "open"  # an invitation that can still be accepted
    EXPIRED = "expired"  # an invitation whose time is over
    MEMBER = "member"  # an accepted member


def _aware(name: str, value: object) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be an aware datetime")


@dataclass(frozen=True, slots=True)
class Project:
    """One project as stored. ``description`` is ``None`` when there is none.

    ``deletion_started_at`` / ``deletion_scheduled_at`` are set while the
    project is Pending deletion (``scheduled = started + 30 days``: the instant
    from which the purge may delete it and before which it can be restored) and
    stay as a fact on the tombstone (``DELETED``); they are ``None`` otherwise.
    ``deleted_at`` is set only on a tombstone. ``created_by`` is an opaque user
    id (``None`` once that user row is gone).
    """

    id: uuid.UUID
    name: str
    description: str | None
    status: ProjectStatus
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    deletion_started_at: datetime | None
    deletion_scheduled_at: datetime | None
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class Member:
    """A membership row: an accepted member or an invitation.

    ``INVITED``: ``invite_expires_at`` is set (the invitation can be accepted
    while ``now < invite_expires_at``) and ``joined_at`` is ``None``.
    ``ACTIVE``: ``joined_at`` is set and ``invite_expires_at`` is ``None``.
    ``invited_at`` is when the row was created (for the creator of a project
    it is the creation instant).
    """

    project_id: uuid.UUID
    user_id: uuid.UUID
    role: ProjectRole
    status: MemberStatus
    invited_at: datetime
    invite_expires_at: datetime | None
    joined_at: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, uuid.UUID) or not isinstance(
            self.user_id, uuid.UUID
        ):
            raise ValueError("project_id and user_id must be UUIDs")
        if not isinstance(self.role, ProjectRole):
            raise ValueError("role must be a ProjectRole")
        if not isinstance(self.status, MemberStatus):
            raise ValueError("status must be a MemberStatus")
        _aware("invited_at", self.invited_at)
        if self.status is MemberStatus.INVITED:
            if self.joined_at is not None or self.invite_expires_at is None:
                raise ValueError("an invitation has an expiry and no join time")
            _aware("invite_expires_at", self.invite_expires_at)
            if self.invite_expires_at <= self.invited_at:
                raise ValueError("an invitation must expire after it was made")
        else:
            if self.invite_expires_at is not None or self.joined_at is None:
                raise ValueError("a member has a join time and no invitation expiry")
            _aware("joined_at", self.joined_at)


@dataclass(frozen=True, slots=True)
class PendingInvite:
    """An open invitation as its invitee sees it (``list_my_invites``)."""

    project_id: uuid.UUID
    project_name: str
    role: ProjectRole
    invited_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TransitionPlan:
    """The outcome of :func:`paw_backend.projects.domain.plan_transition`.

    ``changed`` is ``False`` for an idempotent repeat (archiving an archived
    project): nothing is written and ``new_status`` equals the current status.
    """

    new_status: ProjectStatus
    changed: bool


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """What one ``purge_expired`` run did.

    ``purged`` lists the ids marked Deleted (oldest ``deletion_scheduled_at``
    first, ties by id). ``has_more`` is true when projects that are due remain
    (the batch was full): call again.
    """

    purged: tuple[uuid.UUID, ...]
    has_more: bool
