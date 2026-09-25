"""The rules of the project lifecycle and of membership, as pure functions.

No I/O, no clock, no randomness and no global state: the same arguments always
give the same result, and no argument is modified. Every "now" is passed in.
All datetimes must be aware (``tzinfo`` set); a naive one raises ``ValueError``
and a value that is not a ``datetime`` raises ``TypeError``.
Results that are datetimes are in UTC (``datetime.timezone.utc``).

``ProjectService`` (``service.py``) does the validation of caller input, the
authorization, the transactions and the locking, and calls these functions for
the decisions. Nothing here is security relevant on its own: a wrong answer
here is caught by the tests, and the service never takes an authorization
decision from this module.

IMPLEMENTATION NOTE (PAW-026 stubs): every function below is a stub. Replace
the ``raise NotImplementedError`` with the logic its docstring describes. Do
not change a signature or a docstring.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from paw_backend.authz.roles import ProjectRole
from paw_backend.projects.errors import IllegalTransitionError
from paw_backend.projects.records import (
    InviteState,
    LifecycleAction,
    Member,
    MemberStatus,
    ProjectStatus,
    TransitionPlan,
)


def _aware(value):
    if not isinstance(value, datetime):
        raise TypeError("not a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime")


def plan_transition(status: ProjectStatus, action: LifecycleAction) -> TransitionPlan:
    """What ``action`` does to a project that is in ``status``.

    Returns ``TransitionPlan(new_status, changed)``. ``changed`` is ``False``
    for an idempotent repeat (``new_status`` is then ``status``). Raises
    ``IllegalTransitionError(status, action)`` (from
    ``paw_backend.projects.errors``) for every combination that is not in the
    table. ``TypeError`` when ``status`` is not a ``ProjectStatus`` or
    ``action`` is not a ``LifecycleAction`` (a plain string is refused).

    The complete table (``-`` = illegal; ``=`` = allowed, nothing changes)::

        status \\ action   ARCHIVE     UNARCHIVE   BEGIN_DELETION     RESTORE     PURGE
        ACTIVE            ARCHIVED    =           PENDING_DELETION   -           -
        ARCHIVED          =           ACTIVE      PENDING_DELETION   =           -
        PENDING_DELETION  -           -           =                  ARCHIVED    DELETED
        DELETED           -           -           -                  -           -

    Examples: ``(ACTIVE, ARCHIVE)`` -> ``TransitionPlan(ARCHIVED, True)``;
    ``(ARCHIVED, ARCHIVE)`` -> ``TransitionPlan(ARCHIVED, False)``;
    ``(PENDING_DELETION, BEGIN_DELETION)`` -> ``TransitionPlan(PENDING_DELETION,
    False)`` (starting the deletion again does not restart the 30 days);
    ``(ACTIVE, RESTORE)`` and ``(PENDING_DELETION, UNARCHIVE)`` are illegal.
    """
    if not isinstance(status, ProjectStatus) or not isinstance(action, LifecycleAction):
        raise TypeError("status and action must be enums")
    A, R, P, D = (
        ProjectStatus.ACTIVE,
        ProjectStatus.ARCHIVED,
        ProjectStatus.PENDING_DELETION,
        ProjectStatus.DELETED,
    )
    L = LifecycleAction
    table = {
        (A, L.ARCHIVE): (R, True),
        (A, L.UNARCHIVE): (A, False),
        (A, L.BEGIN_DELETION): (P, True),
        (R, L.ARCHIVE): (R, False),
        (R, L.UNARCHIVE): (A, True),
        (R, L.BEGIN_DELETION): (P, True),
        (R, L.RESTORE): (R, False),
        (P, L.BEGIN_DELETION): (P, False),
        (P, L.RESTORE): (R, True),
        (P, L.PURGE): (D, True),
    }
    found = table.get((status, action))
    if found is None:
        raise IllegalTransitionError(status, action)
    return TransitionPlan(*found)


def deletion_schedule(started_at: datetime) -> datetime:
    """The instant a deletion started at ``started_at`` becomes final: +30 days.

    Exactly 30 * 24 hours later (not "one month later"), as a UTC datetime.
    ``ValueError`` when ``started_at`` is naive, ``TypeError`` when it is not a
    ``datetime``. Microseconds are kept.

    Examples: ``2026-01-15T10:30:00+00:00`` -> ``2026-02-14T10:30:00+00:00``;
    ``2026-02-01T00:00:00+00:00`` -> ``2026-03-03T00:00:00+00:00`` (February
    2026 has 28 days); ``2026-01-15T19:30:00+09:00`` ->
    ``2026-02-14T10:30:00+00:00``; ``2028-02-01T12:00:00.000001+00:00`` ->
    ``2028-03-02T12:00:00.000001+00:00`` (2028 is a leap year).
    """
    _aware(started_at)
    return (started_at + timedelta(days=30)).astimezone(UTC)


def restore_window_open(scheduled_at: datetime, now: datetime) -> bool:
    """Whether a Pending deletion project can still be restored at ``now``.

    ``True`` iff ``now < scheduled_at`` (strictly: at the very instant
    ``scheduled_at`` the project is no longer restorable). ``ValueError`` when
    either datetime is naive, ``TypeError`` when either is not a ``datetime``.
    Datetimes with different offsets are compared as instants.

    Examples: ``now`` one microsecond before ``scheduled_at`` -> ``True``;
    ``now == scheduled_at`` -> ``False``; one microsecond after -> ``False``.
    """
    _aware(scheduled_at)
    _aware(now)
    return now < scheduled_at


def purge_due(scheduled_at: datetime, now: datetime) -> bool:
    """Whether the purge may delete a Pending deletion project at ``now``.

    ``True`` iff ``now >= scheduled_at``. It is exactly the opposite of
    :func:`restore_window_open` for the same two arguments: at any instant one
    of them is true and the other false. ``ValueError`` when either datetime is
    naive, ``TypeError`` when either is not a ``datetime``.
    """
    _aware(scheduled_at)
    _aware(now)
    return now >= scheduled_at


def invite_expiry(invited_at: datetime) -> datetime:
    """When an invitation made at ``invited_at`` expires: +14 days, in UTC.

    Exactly 14 * 24 hours later. The 14 days is this literal, not
    ``paw_backend.projects.limits.INVITE_TTL`` (which records the same value and is
    read only by the tests): change both together.
    ``ValueError`` when ``invited_at`` is naive, ``TypeError`` when it is not a
    ``datetime``.

    Examples: ``2026-03-01T00:00:00+00:00`` -> ``2026-03-15T00:00:00+00:00``;
    ``2026-03-01T01:00:00+09:00`` -> ``2026-03-14T16:00:00+00:00``.
    """
    _aware(invited_at)
    return (invited_at + timedelta(days=14)).astimezone(UTC)


def invite_state(existing: Member | None, now: datetime) -> InviteState:
    """What the membership row ``existing`` (or its absence) means at ``now``.

    * ``None`` -> ``InviteState.NONE``;
    * ``existing.status`` is ``ACTIVE`` -> ``InviteState.MEMBER`` (whatever the
      role);
    * ``existing.status`` is ``INVITED`` and ``now < existing.invite_expires_at``
      -> ``InviteState.OPEN``;
    * ``existing.status`` is ``INVITED`` and ``now >= existing.invite_expires_at``
      -> ``InviteState.EXPIRED`` (expired exactly at ``invite_expires_at``).

    ``ValueError`` when ``now`` is naive; ``TypeError`` when ``now`` is not a
    ``datetime`` or ``existing`` is neither ``None`` nor a ``Member``.
    """
    _aware(now)
    if existing is None:
        return InviteState.NONE
    if not isinstance(existing, Member):
        raise TypeError("existing must be a Member or None")
    if existing.status is MemberStatus.ACTIVE:
        return InviteState.MEMBER
    if now < existing.invite_expires_at:
        return InviteState.OPEN
    return InviteState.EXPIRED


def manager_would_remain(
    members: Sequence[Member], user_id: uuid.UUID, new_role: ProjectRole | None
) -> bool:
    """Whether at least one accepted Manager is left after a membership change.

    The change is: ``new_role is None`` -> the member ``user_id`` is removed (or
    leaves); otherwise the member ``user_id`` gets the role ``new_role``.
    Only members with ``status == MemberStatus.ACTIVE`` count as Managers:
    an invitation (``INVITED``) never does, whatever its role, and a change
    never applies to an invitation. If ``user_id`` is not among the ACTIVE
    members the list is unchanged. Returns ``True`` iff after the change at
    least one ACTIVE member has ``role == ProjectRole.MANAGER``.

    ``members`` is not modified. ``TypeError`` if ``user_id`` is not a
    ``uuid.UUID`` or ``new_role`` is neither ``None`` nor a ``ProjectRole``.

    Examples (M = active Manager, C = active Contributor, i = invited Manager):
    ``[M(a), C(b)]``: remove ``a`` -> ``False``; remove ``b`` -> ``True``;
    change ``a`` to VIEWER -> ``False``; change ``a`` to MANAGER -> ``True``;
    change ``b`` to MANAGER -> ``True``. ``[M(a), M(b)]``: remove ``a`` ->
    ``True``. ``[M(a), i(b)]``: remove ``a`` -> ``False`` (the invitation does
    not count). ``[M(a)]``: remove an unknown user ``z`` -> ``True``. ``[]``
    -> ``False`` whatever the change.
    """
    if not isinstance(user_id, uuid.UUID):
        raise TypeError("user_id must be a UUID")
    if new_role is not None and not isinstance(new_role, ProjectRole):
        raise TypeError("new_role must be a ProjectRole or None")
    count = 0
    for m in members:
        if m.status is not MemberStatus.ACTIVE:
            continue
        role = m.role
        if m.user_id == user_id:
            if new_role is None:
                continue
            role = new_role
        if role is ProjectRole.MANAGER:
            count += 1
    return count > 0
