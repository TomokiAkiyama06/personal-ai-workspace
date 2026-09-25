"""The project service: create, membership and lifecycle of projects (PAW-026).

Who can call it
---------------
The service is not exposed over HTTP by this issue (sessions come with PAW-022).
Every method that changes or reads a project takes the acting
:class:`~paw_backend.authz.Principal` and asks the
:class:`~paw_backend.authz.Authorizer` (default deny, audit by the Authorizer)
on the **stored** state of the project. The ``project_roles`` of the Principal
the caller passes are **ignored**: the role of the actor in the project is read
from ``project_members`` in the same transaction (an accepted member only), so
a Principal built before a member was removed cannot act with the old role.
Only ``Principal.user_id`` and ``Principal.system_role`` are taken from the caller
(the authentication layer resolved them from stored users).

Authorization of each method:

* ``get_project``, ``list_members``: ``project.read`` (Archived is readable;
  the policy refuses a Pending deletion project);
* ``list_invites``, ``invite_member``, ``remove_member``, ``change_role``:
  ``project.members.manage``;
* ``rename_project``, ``set_description``: ``project.settings.manage``;
* ``archive``, ``unarchive``, ``begin_deletion``, ``restore``:
  ``project.lifecycle.manage`` (a Manager, and Owner / Admin without being a
  member);
* ``create_project``: ``project.create``; ``accept_invite``, ``decline_invite``:
  ``project.invitation.respond``; ``leave_project``: ``project.leave`` (see
  "Self service" below);
* ``list_projects``, ``list_my_invites``: the user's own memberships only, by
  identity (a read of the actor's own rows; no capability, no Audit event);
* ``list_all_projects`` (Issue #84): ``admin.projects.manage`` (Owner / Admin,
  membership is not needed and not consulted; see "The administrator's list");
* ``roles_of``, ``purge_expired``: backend-internal, not for users.

Self service (Decision 0022, Proposed; it extends Decision 0004)
-----------------------------------------------------------------
Creating a project, answering one's own invitation and leaving a project used
to be authorized by identity alone and left no Audit event (Decision 0008, an
approved provisional arrangement). They now go through the Authorizer like every
other operation, with an Audit mode of ``REQUIRED``: an allowed **and** a denied
call each write one event, and an allowed call whose event cannot be written is
refused (``ProjectPermissionDeniedError(AUDIT_UNAVAILABLE)``; nothing changed).

* ``project.create`` is ``Scope.SYSTEM``: Owner, Admin and User hold it
  (``SYSTEM`` does not); the resource is the workspace (``Resource.system()``).
  The event cannot name the project, which does not exist yet.
* ``project.invitation.respond`` and ``project.leave`` are ``Scope.SELF``: the
  resource (kind ``project_invitation`` / ``project_membership``, ``project_id``
  set) is owned by the actor, and the service only ever builds it for the actor's
  own row. The policy does not look at the project's state or at the actor's role
  in it (leaving is allowed in every state, Decision 0008); whether an invitation
  or a membership exists is decided by the transaction below.
* None of the three can be delegated to an agent (``delegable=False``).

The **decision is taken before anything else**: after the actor and the arguments
are checked, and before the clock is read, the transaction is opened, the project
row is locked or any row is read. A denial therefore reveals nothing about the
project or the invitation, and the Audit write (bounded by the Authorizer's
timeout) never runs while the project row is locked. The event records the
*decision* for the attempt, not its outcome: an allowed attempt that then finds
no invitation, an expired one, or the last Manager, still has its ``allow``
event and changes nothing. ``accept_invite`` reads the clock **after** the
decision, so that the time spent writing the event does not make an expired
invitation look valid.

Errors that do not disclose existence
-------------------------------------
After the Authorizer denied an action, the service raises
:class:`ProjectNotFoundError` (exactly as for a missing or Deleted project) when
the actor is **not an accepted member** of the project; the decision was
audited anyway. A member (or an Owner / Admin acting on lifecycle) gets
:class:`ProjectStateError` for ``Reason.PROJECT_STATE_FORBIDS`` and
:class:`ProjectPermissionDeniedError` (``reason`` attached) for everything else.
``Reason.AUDIT_UNAVAILABLE`` is always :class:`ProjectPermissionDeniedError`,
so that the API layer can answer 503.
Invitations are answered with :class:`InviteNotFoundError` (no invitation, or
the project cannot be joined) and never reveal anything about the project.

Order of checks (every method)
------------------------------
1. the actor: not a ``Principal`` -> ``ProjectPermissionDeniedError(UNAUTHENTICATED)``
   (no Audit event: an unauthenticated denial is never written to the database);
   ``list_projects`` / ``list_my_invites`` also refuse ``system_role is SYSTEM``
   -> ``ProjectPermissionDeniedError(CAPABILITY_NOT_GRANTED)``;
2. the arguments, in signature order, all before the database is touched
   (``InvalidProjectInputError``; a service on an unconfigured ``Database``
   still reports them);
3. ``create_project``, ``accept_invite``, ``decline_invite``, ``leave_project``:
   the Authorizer (see "Self service"), before the clock and the database;
4. the clock is read **once** and validated (``validate_instant``);
5. one transaction. Every operation that changes something first runs
   ``SELECT ... FOR UPDATE`` on the project row: all changes of one project
   (members, settings, lifecycle, purge) are serialised by that lock, which is
   what makes "the last Manager cannot leave" hold under concurrency. Then: the
   project must exist and not be Deleted (``ProjectNotFoundError``); the
   authorization (every method that is not one of the four of step 3); then the
   rules of the method.
A ``SET LOCAL lock_timeout`` opens every transaction; a lock wait longer than
``lock_timeout_ms`` is :class:`ProjectBusyError`.

Where the Authorizer is called inside the transaction, it is called while the
project row is locked and its audit write is bounded by the Authorizer's own
timeout; the four self service methods decide before the transaction instead.
The audit records the decision, not the outcome: an allowed action can still
fail afterwards (a rule, a database error) and the audit event stays.

Lifecycle (``REQUIREMENTS.md`` "Project lifecycle", Decision 0008)
------------------------------------------------------------------
``domain.plan_transition`` holds the table. A repeat that does not change
anything (archive an Archived project, ``begin_deletion`` of a Pending deletion
project, ``restore`` of an Archived one) succeeds, writes nothing (``updated_at``
and the 30 days do not move) and returns the project as it is. ``begin_deletion``
needs ``confirm_name`` to be exactly the project's name (a typed
confirmation); the 30 days start at the clock's ``now`` and end at
``deletion_scheduled_at``. ``begin_deletion`` also records the request to stop
the project's tasks (``project_task_stops``) **in the same transaction**; the
requirement "running tasks are safe-stopped" is carried out by
:class:`~paw_backend.projects.task_stop.ProjectTaskStopper` (Decision 0008,
section 8), which the orchestrator calls. A repeat that changes nothing writes no
request. ``restore`` is refused with
:class:`DeletionWindowClosedError` at ``now >= deletion_scheduled_at`` and with
:class:`NoManagerError` when no accepted Manager is left (the last Manager may
leave a Pending deletion project). It returns the project as **Archived**.
``purge_expired`` marks due projects Deleted and deletes all their membership
rows (accepted and invited); see ``store.mark_deleted`` for what stays.

Membership
----------
Invite-only: nobody joins by themselves. ``invite_member`` creates an INVITED
row for an existing, active user; ``accept_invite`` turns it into an ACTIVE
member; ``decline_invite`` deletes it. An invitation lasts 14 days
(``domain.invite_expiry``); an expired one cannot be accepted and is replaced
by a new ``invite_member``. Inviting an accepted member, or a user with an open
invitation, is an error (nothing is refreshed). A project holds at most
``MAX_MEMBERS_PER_PROJECT`` accepted members plus open invitations.
The last accepted Manager cannot be removed, leave or be demoted
(:class:`LastManagerError`), except that leaving a Pending deletion project is
always allowed. Invitations never count as Managers.

The administrator's list (Issue #84, Decision 0008 section 6 and Decision 0004)
-------------------------------------------------------------------------------
``list_all_projects`` lets an Owner / Admin (``admin.projects.manage``) find a
project whose id they do not know, to archive, restore or delete it. It returns
the id, name, status, creation time and deletion deadline of **every project that
is not Deleted** and nothing else (``AdminProjectSummary``: no description,
creator, member, invitation or content of another area; managing a project and
reading what is in it are separate, Decision 0004). Membership is neither needed
nor read. Pages are keyset pages with a bounded size (``cursor.py``).
Its checks run in the order above (actor, arguments, **then** the
Authorizer, **then** one read transaction): the Authorizer is called before a
database connection is taken, so a denied caller (or an audit store that is down)
never causes a read of ``projects``. Every authorized-or-denied call writes exactly
one Audit event through the Authorizer (``REQUIRED``: no event, no list): the
actor, ``admin.projects.manage``, the decision, and a resource kind that names
the filter (``project_list_all`` or ``project_list_<status>``). The event holds no
project id, name, cursor or count. A call whose arguments are invalid, and a
caller that is not a ``Principal``, makes no decision and so writes no event.
An agent is not a ``Principal`` and cannot call it; ``admin.projects.manage`` is
also not delegable, so an agent acting for an Owner is denied by the Authorizer.

Errors and logging
------------------
Every error is a :class:`ProjectError` with a fixed message that contains no
caller content. Database errors the service does not handle propagate
unchanged (their text can contain SQL parameters: never show ``str(error)``).
The service logs nothing.
"""

import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from types import MappingProxyType

from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz import Authorizer, Capability, Principal, ProjectState, Resource
from paw_backend.authz.policy import Reason
from paw_backend.authz.roles import ProjectRole, SystemRole
from paw_backend.db import Database
from paw_backend.projects import domain, store
from paw_backend.projects.cursor import decode_cursor, encode_cursor, filter_token
from paw_backend.projects.errors import (
    AlreadyInvitedError,
    AlreadyMemberError,
    ConfirmationMismatchError,
    DeletionWindowClosedError,
    InviteeUnavailableError,
    InviteExpiredError,
    InviteNotFoundError,
    LastManagerError,
    MemberLimitError,
    MemberNotFoundError,
    NoManagerError,
    ProjectNotFoundError,
    ProjectPermissionDeniedError,
    ProjectStateError,
)
from paw_backend.projects.limits import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_LOCK_TIMEOUT_MS,
    DEFAULT_PURGE_BATCH_SIZE,
    MAX_LOCK_TIMEOUT_MS,
    MAX_MEMBERS_PER_PROJECT,
    MIN_LOCK_TIMEOUT_MS,
    utc_now,
)
from paw_backend.projects.records import (
    AdminProjectPage,
    InviteState,
    LifecycleAction,
    Member,
    MemberStatus,
    PendingInvite,
    Project,
    ProjectStatus,
    PurgeResult,
)
from paw_backend.projects.transaction import transaction
from paw_backend.projects.validation import (
    validate_admin_status_filter,
    validate_batch_size,
    validate_confirmation,
    validate_description,
    validate_instant,
    validate_limit,
    validate_name,
    validate_offset,
    validate_project_role,
    validate_status_filter,
    validate_uuid,
)

Clock = Callable[[], datetime]

_HUMAN_ROLES = (SystemRole.OWNER, SystemRole.ADMIN, SystemRole.USER)
# A project that is Deleted is never authorized on; the three others map 1:1.
_AUTHZ_STATE = MappingProxyType(
    {
        ProjectStatus.ACTIVE: ProjectState.ACTIVE,
        ProjectStatus.ARCHIVED: ProjectState.ARCHIVED,
        ProjectStatus.PENDING_DELETION: ProjectState.PENDING_DELETION,
    }
)
# An invitation can be answered while the project can still be joined.
_JOINABLE = (ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED)
# The audit resource kinds of the actor's own invitation / membership row.
_INVITATION_KIND = "project_invitation"
_MEMBERSHIP_KIND = "project_membership"


def _own_row(kind: str, project_id: uuid.UUID, user_id: uuid.UUID) -> Resource:
    """The actor's own invitation / membership in a project (a ``Scope.SELF`` resource).

    The audit event names the project (``project_id``) and the actor. The
    project's state is not part of it: the policy does not consult it for these
    capabilities.
    """
    return Resource(kind=kind, project_id=project_id, owner_id=user_id)


class ProjectService:
    """Projects, their members and their lifecycle. See the module docstring."""

    def __init__(
        self,
        database: Database,
        authorizer: Authorizer,
        *,
        clock: Clock = utc_now,
        lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    ) -> None:
        """``TypeError`` for a wrong type, ``ValueError`` for a bad lock timeout."""
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if not isinstance(authorizer, Authorizer):
            raise TypeError("authorizer must be an Authorizer")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if isinstance(lock_timeout_ms, bool) or not isinstance(lock_timeout_ms, int):
            raise TypeError("lock_timeout_ms must be an int")
        if not MIN_LOCK_TIMEOUT_MS <= lock_timeout_ms <= MAX_LOCK_TIMEOUT_MS:
            raise ValueError("lock_timeout_ms is out of range")
        self._database = database
        self._authorizer = authorizer
        self._clock = clock
        self._lock_timeout_ms = lock_timeout_ms

    # -- plumbing (validation, time, transaction, authorization) -----------------------

    @staticmethod
    def _actor(actor: object) -> Principal:
        if not isinstance(actor, Principal):
            raise ProjectPermissionDeniedError(Reason.UNAUTHENTICATED)
        return actor

    @classmethod
    def _human(cls, actor: object) -> Principal:
        principal = cls._actor(actor)
        if principal.system_role not in _HUMAN_ROLES:
            raise ProjectPermissionDeniedError(Reason.CAPABILITY_NOT_GRANTED)
        return principal

    async def _authorize_own(
        self, actor: Principal, capability: Capability, resource: Resource
    ) -> None:
        """Ask the Authorizer about an act on the actor's own data; refuse if denied.

        Called **before** the clock, the transaction or any row is touched, so a
        denial (or an Audit event that could not be written) changes nothing and
        reveals nothing. The Principal is rebuilt without ``project_roles``: these
        capabilities do not depend on a role in a project, and the caller's
        claims are never passed on.
        """
        decision = await self._authorizer.authorize(
            Principal(actor.user_id, actor.system_role), capability, resource
        )
        if not decision.allowed:
            raise ProjectPermissionDeniedError(decision.reason)

    def _now(self) -> datetime:
        return validate_instant("clock", self._clock())

    def _transaction(self) -> AbstractAsyncContextManager[AsyncSession]:
        """One transaction with the service's lock timeout (module docstring)."""
        return transaction(self._database, self._lock_timeout_ms)

    @staticmethod
    async def _load(
        session: AsyncSession, project_id: uuid.UUID, *, lock: bool
    ) -> Project:
        """The project, locked when ``lock``; missing and Deleted are not found."""
        project = await store.get_project(session, project_id, for_update=lock)
        if project is None or project.status is ProjectStatus.DELETED:
            raise ProjectNotFoundError()
        return project

    async def _guarded(
        self,
        session: AsyncSession,
        actor: Principal,
        capability: Capability,
        project_id: uuid.UUID,
        *,
        lock: bool,
    ) -> tuple[Project, Member | None]:
        """Load the project, read the actor's membership, ask the Authorizer."""
        project = await self._load(session, project_id, lock=lock)
        member = await store.get_member(session, project_id, actor.user_id)
        active = member is not None and member.status is MemberStatus.ACTIVE
        # The actor's role comes from the row just read, never from the caller.
        principal = Principal(
            actor.user_id,
            actor.system_role,
            {project.id: member.role} if active and member is not None else {},
        )
        resource = Resource.project(project.id, _AUTHZ_STATE[project.status])
        decision = await self._authorizer.authorize(principal, capability, resource)
        if decision.allowed:
            return project, member
        if decision.reason is Reason.AUDIT_UNAVAILABLE:
            raise ProjectPermissionDeniedError(decision.reason)
        if not active:
            raise ProjectNotFoundError()
        if decision.reason is Reason.PROJECT_STATE_FORBIDS:
            raise ProjectStateError(project.status)
        raise ProjectPermissionDeniedError(decision.reason)

    # -- create and read ---------------------------------------------------------------

    async def create_project(
        self, actor: Principal, name: str, description: str | None = None
    ) -> Project:
        """Create an ACTIVE project **without a repository**; the actor manages it.

        ``project.create`` (Owner, Admin, User; audited, decided before the
        database is touched). One transaction inserts the project and the actor's
        membership (``MANAGER``, ``ACTIVE``, ``invited_at = joined_at = now``).
        ``name`` and ``description`` are validated (``validate_name`` /
        ``validate_description``). Returns the stored project.
        """
        principal = self._actor(actor)
        name = validate_name(name)
        description = validate_description(description)
        await self._authorize_own(
            principal, Capability.PROJECT_CREATE, Resource.system()
        )
        now = self._now()
        async with self._transaction() as session:
            project = await store.insert_project(
                session,
                name=name,
                description=description,
                created_by=principal.user_id,
                now=now,
            )
            await store.insert_member(
                session,
                Member(
                    project_id=project.id,
                    user_id=principal.user_id,
                    role=ProjectRole.MANAGER,
                    status=MemberStatus.ACTIVE,
                    invited_at=now,
                    invite_expires_at=None,
                    joined_at=now,
                ),
            )
        return project

    async def get_project(self, actor: Principal, project_id: uuid.UUID) -> Project:
        """The project, for a member (``project.read``; not while Pending deletion)."""
        principal = self._actor(actor)
        project_id = validate_uuid("project_id", project_id)
        async with self._transaction() as session:
            project, _ = await self._guarded(
                session, principal, Capability.PROJECT_READ, project_id, lock=False
            )
        return project

    async def list_projects(
        self,
        actor: Principal,
        *,
        status: ProjectStatus = ProjectStatus.ACTIVE,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> tuple[Project, ...]:
        """The projects of the actor's own memberships with this ``status``.

        Self service: it only ever returns the actor's own accepted memberships
        (a system role shows nothing more; an invitation shows nothing). The
        normal list is ``ACTIVE``; ``ARCHIVED`` is the separate "show archived"
        list; ``PENDING_DELETION`` lists only the projects the actor manages
        (they can restore them). ``store.list_projects_of`` documents the order.
        """
        principal = self._human(actor)
        status = validate_status_filter(status)
        limit = validate_limit(limit)
        offset = validate_offset(offset)
        async with self._transaction() as session:
            found = await store.list_projects_of(
                session, principal.user_id, status, limit, offset
            )
        return tuple(found)

    async def list_all_projects(
        self,
        actor: Principal,
        *,
        status: ProjectStatus | str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        cursor: str | None = None,
    ) -> AdminProjectPage:
        """Every project that is not Deleted, for an Owner / Admin (Issue #84).

        Needs ``admin.projects.manage``; the Authorizer records the decision (one
        Audit event per call, no event when an argument is invalid). ``status``
        (``None``: all; else ``ACTIVE`` / ``ARCHIVED`` / ``PENDING_DELETION`` as a
        member or its exact ``str``) filters the list; ``DELETED`` is refused.
        ``limit`` is 1 to 200 (default 50). ``cursor`` is the ``next_cursor`` of
        the previous page, unchanged, with the same ``status``; ``None`` starts at
        the newest project. Newest first, ties by id descending; keyset paging
        (see ``cursor.py``). Only ``AdminProjectSummary`` fields are returned.
        ``InvalidProjectInputError`` for a bad argument or cursor;
        ``ProjectPermissionDeniedError`` when the Authorizer denies (``reason``;
        ``audit_unavailable`` is the 503 case).
        """
        principal = self._actor(actor)
        status = validate_admin_status_filter(status)
        limit = validate_limit(limit)
        after = None if cursor is None else decode_cursor(cursor, status)
        decision = await self._authorizer.authorize(
            principal,
            Capability.ADMIN_PROJECTS_MANAGE,
            Resource(kind=f"project_list_{filter_token(status)}"),
        )
        if not decision.allowed:
            raise ProjectPermissionDeniedError(decision.reason)
        async with self._transaction() as session:
            # One row more than the page tells whether another page follows.
            found = await store.list_projects_page(
                session,
                status,
                None if after is None else (after.created_at, after.id),
                limit + 1,
            )
        page = found[:limit]
        next_cursor = None
        if len(found) > limit:
            last = page[-1]
            next_cursor = encode_cursor(status, last.created_at, last.id)
        return AdminProjectPage(projects=tuple(page), next_cursor=next_cursor)

    async def roles_of(self, user_id: uuid.UUID) -> Mapping[uuid.UUID, ProjectRole]:
        """``{project_id: role}`` of the user's accepted memberships (read-only).

        For the authentication layer (PAW-022), which builds
        ``Principal.project_roles`` with it. Backend-internal: never expose it
        with another user's id. The service itself does not trust the roles of
        the Principals it is given (module docstring).
        """
        user_id = validate_uuid("user_id", user_id)
        async with self._transaction() as session:
            roles = await store.roles_of(session, user_id)
        return MappingProxyType(dict(roles))

    # -- settings ---------------------------------------------------------------

    async def rename_project(
        self, actor: Principal, project_id: uuid.UUID, name: str
    ) -> Project:
        """Set the name (``project.settings.manage``). The same name changes nothing."""
        principal = self._actor(actor)
        project_id = validate_uuid("project_id", project_id)
        name = validate_name(name)
        now = self._now()
        async with self._transaction() as session:
            project, _ = await self._guarded(
                session,
                principal,
                Capability.PROJECT_SETTINGS_MANAGE,
                project_id,
                lock=True,
            )
            if name == project.name:
                return project
            return await store.update_settings(
                session,
                project_id,
                name=name,
                description=project.description,
                now=now,
            )

    async def set_description(
        self, actor: Principal, project_id: uuid.UUID, description: str | None
    ) -> Project:
        """Set or clear (``None`` / blank) the description (settings.manage)."""
        principal = self._actor(actor)
        project_id = validate_uuid("project_id", project_id)
        description = validate_description(description)
        now = self._now()
        async with self._transaction() as session:
            project, _ = await self._guarded(
                session,
                principal,
                Capability.PROJECT_SETTINGS_MANAGE,
                project_id,
                lock=True,
            )
            if description == project.description:
                return project
            return await store.update_settings(
                session,
                project_id,
                name=project.name,
                description=description,
                now=now,
            )

    # -- members and invitations ------------------------------------------------

    async def invite_member(
        self,
        actor: Principal,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        role: ProjectRole,
    ) -> Member:
        """Invite an existing, active user (``project.members.manage``).

        Checks after the authorization, in this order: the invitee is an active
        user (``InviteeUnavailableError``: missing and inactive are the same);
        the state of an existing row (``AlreadyMemberError``,
        ``AlreadyInvitedError``; an *expired* invitation is deleted and
        replaced); the member limit (``MemberLimitError``). Creates an INVITED
        row that expires ``domain.invite_expiry(now)``.
        """
        principal = self._actor(actor)
        project_id = validate_uuid("project_id", project_id)
        user_id = validate_uuid("user_id", user_id)
        role = validate_project_role(role)
        now = self._now()
        async with self._transaction() as session:
            await self._guarded(
                session,
                principal,
                Capability.PROJECT_MEMBERS_MANAGE,
                project_id,
                lock=True,
            )
            if not await store.user_is_active(session, user_id):
                raise InviteeUnavailableError()
            existing = await store.get_member(session, project_id, user_id)
            state = domain.invite_state(existing, now)
            if state is InviteState.MEMBER:
                raise AlreadyMemberError()
            if state is InviteState.OPEN:
                raise AlreadyInvitedError()
            if state is InviteState.EXPIRED:
                await store.delete_member(session, project_id, user_id)
            if await store.count_members(session, project_id, now) >= (
                MAX_MEMBERS_PER_PROJECT
            ):
                raise MemberLimitError()
            return await store.insert_member(
                session,
                Member(
                    project_id=project_id,
                    user_id=user_id,
                    role=role,
                    status=MemberStatus.INVITED,
                    invited_at=now,
                    invite_expires_at=domain.invite_expiry(now),
                    joined_at=None,
                ),
            )

    async def accept_invite(self, actor: Principal, project_id: uuid.UUID) -> Member:
        """Accept the actor's own invitation (``project.invitation.respond``).

        The decision (audited) comes before anything is read; then
        ``InviteNotFoundError``: no row, or the project is missing, Deleted or
        Pending deletion. ``InviteExpiredError``: the invitation's time is over
        (the row stays). An actor who is an accepted member already gets that
        membership back unchanged (idempotent). Otherwise the row becomes
        ACTIVE with ``joined_at = now`` and is returned.
        """
        principal = self._actor(actor)
        project_id = validate_uuid("project_id", project_id)
        await self._authorize_own(
            principal,
            Capability.PROJECT_INVITATION_RESPOND,
            _own_row(_INVITATION_KIND, project_id, principal.user_id),
        )
        # After the decision: the time an Audit write took must not make an
        # expired invitation look valid.
        now = self._now()
        async with self._transaction() as session:
            project = await store.get_project(session, project_id, for_update=True)
            if project is None or project.status not in _JOINABLE:
                raise InviteNotFoundError()
            existing = await store.get_member(session, project_id, principal.user_id)
            state = domain.invite_state(existing, now)
            if state is InviteState.NONE:
                raise InviteNotFoundError()
            if state is InviteState.EXPIRED:
                raise InviteExpiredError()
            if state is InviteState.MEMBER and existing is not None:
                return existing
            return await store.activate_invite(
                session, project_id, principal.user_id, joined_at=now
            )

    async def decline_invite(self, actor: Principal, project_id: uuid.UUID) -> None:
        """Decline the actor's own invitation: the row is deleted.

        ``project.invitation.respond`` (the same capability as accepting; audited,
        decided before anything is read). An open **or expired** invitation can be
        declined. ``InviteNotFoundError`` when there is no invitation (an accepted
        member must ``leave_project``) or the project is missing, Deleted or
        Pending deletion.
        """
        principal = self._actor(actor)
        project_id = validate_uuid("project_id", project_id)
        await self._authorize_own(
            principal,
            Capability.PROJECT_INVITATION_RESPOND,
            _own_row(_INVITATION_KIND, project_id, principal.user_id),
        )
        now = self._now()
        async with self._transaction() as session:
            project = await store.get_project(session, project_id, for_update=True)
            if project is None or project.status not in _JOINABLE:
                raise InviteNotFoundError()
            existing = await store.get_member(session, project_id, principal.user_id)
            state = domain.invite_state(existing, now)
            if state not in (InviteState.OPEN, InviteState.EXPIRED):
                raise InviteNotFoundError()
            await store.delete_member(session, project_id, principal.user_id)

    async def remove_member(
        self, actor: Principal, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        """Remove a member or withdraw an invitation (``project.members.manage``).

        ``MemberNotFoundError`` when the user has no row. Removing the last
        accepted Manager is ``LastManagerError`` (also when a Manager removes
        themselves). Withdrawing an invitation never is.
        """
        principal = self._actor(actor)
        project_id = validate_uuid("project_id", project_id)
        user_id = validate_uuid("user_id", user_id)
        async with self._transaction() as session:
            await self._guarded(
                session,
                principal,
                Capability.PROJECT_MEMBERS_MANAGE,
                project_id,
                lock=True,
            )
            target = await store.get_member(session, project_id, user_id)
            if target is None:
                raise MemberNotFoundError()
            await self._require_manager_left(session, target, project_id, None)
            await store.delete_member(session, project_id, user_id)

    async def leave_project(self, actor: Principal, project_id: uuid.UUID) -> None:
        """The actor leaves the project (``project.leave``; audited, decided first).

        ``ProjectNotFoundError`` when the project is missing or Deleted, or the
        actor is not an accepted member (an invitee declines instead). The last
        accepted Manager cannot leave (``LastManagerError``) unless the project
        is Pending deletion.
        """
        principal = self._actor(actor)
        project_id = validate_uuid("project_id", project_id)
        await self._authorize_own(
            principal,
            Capability.PROJECT_LEAVE,
            _own_row(_MEMBERSHIP_KIND, project_id, principal.user_id),
        )
        async with self._transaction() as session:
            project = await self._load(session, project_id, lock=True)
            member = await store.get_member(session, project_id, principal.user_id)
            if member is None or member.status is not MemberStatus.ACTIVE:
                raise ProjectNotFoundError()
            if project.status is not ProjectStatus.PENDING_DELETION:
                await self._require_manager_left(session, member, project_id, None)
            await store.delete_member(session, project_id, principal.user_id)

    async def change_role(
        self,
        actor: Principal,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        role: ProjectRole,
    ) -> Member:
        """Set the role of an accepted member (``project.members.manage``).

        ``MemberNotFoundError`` when the user is not an accepted member (an
        invitation's role is not changed: withdraw and invite again). The same
        role changes nothing. Demoting the last accepted Manager is
        ``LastManagerError``.
        """
        principal = self._actor(actor)
        project_id = validate_uuid("project_id", project_id)
        user_id = validate_uuid("user_id", user_id)
        role = validate_project_role(role)
        async with self._transaction() as session:
            await self._guarded(
                session,
                principal,
                Capability.PROJECT_MEMBERS_MANAGE,
                project_id,
                lock=True,
            )
            target = await store.get_member(session, project_id, user_id)
            if target is None or target.status is not MemberStatus.ACTIVE:
                raise MemberNotFoundError()
            if target.role is role:
                return target
            await self._require_manager_left(session, target, project_id, role)
            return await store.set_member_role(session, project_id, user_id, role)

    @staticmethod
    async def _require_manager_left(
        session: AsyncSession,
        target: Member,
        project_id: uuid.UUID,
        new_role: ProjectRole | None,
    ) -> None:
        """``LastManagerError`` if changing ``target`` leaves no accepted Manager.

        Only an accepted Manager whose role is removed or lowered can cause it.
        """
        if target.status is not MemberStatus.ACTIVE:
            return
        if target.role is not ProjectRole.MANAGER or new_role is ProjectRole.MANAGER:
            return
        members = await store.list_active_members(session, project_id)
        if not domain.manager_would_remain(members, target.user_id, new_role):
            raise LastManagerError()

    async def list_members(
        self, actor: Principal, project_id: uuid.UUID
    ) -> tuple[Member, ...]:
        """The accepted members (``project.read``), oldest ``joined_at`` first."""
        principal = self._actor(actor)
        project_id = validate_uuid("project_id", project_id)
        async with self._transaction() as session:
            await self._guarded(
                session, principal, Capability.PROJECT_READ, project_id, lock=False
            )
            members = await store.list_active_members(session, project_id)
        return tuple(members)

    async def list_invites(
        self, actor: Principal, project_id: uuid.UUID
    ) -> tuple[Member, ...]:
        """The open invitations (``project.members.manage``), oldest first."""
        principal = self._actor(actor)
        project_id = validate_uuid("project_id", project_id)
        now = self._now()
        async with self._transaction() as session:
            await self._guarded(
                session,
                principal,
                Capability.PROJECT_MEMBERS_MANAGE,
                project_id,
                lock=False,
            )
            invites = await store.list_open_invites(session, project_id, now)
        return tuple(invites)

    async def list_my_invites(self, actor: Principal) -> tuple[PendingInvite, ...]:
        """The open invitations addressed to the actor (self service), oldest first."""
        principal = self._human(actor)
        now = self._now()
        async with self._transaction() as session:
            found = await store.list_open_invites_of(session, principal.user_id, now)
        return tuple(found)

    # -- lifecycle --------------------------------------------------------------

    async def archive(self, actor: Principal, project_id: uuid.UUID) -> Project:
        """Active -> Archived (``project.lifecycle.manage``). Archived: no change."""
        return await self._lifecycle(actor, project_id, LifecycleAction.ARCHIVE)

    async def unarchive(self, actor: Principal, project_id: uuid.UUID) -> Project:
        """Archived -> Active (``project.lifecycle.manage``). Active: no change."""
        return await self._lifecycle(actor, project_id, LifecycleAction.UNARCHIVE)

    async def begin_deletion(
        self, actor: Principal, project_id: uuid.UUID, confirm_name: str
    ) -> Project:
        """Active / Archived -> Pending deletion for 30 days (lifecycle.manage).

        ``confirm_name`` must equal the project's name exactly
        (``ConfirmationMismatchError``, checked after the authorization and
        before the transition, also for a repeat). ``deletion_started_at`` is
        ``now`` and ``deletion_scheduled_at`` is ``domain.deletion_schedule(now)``.
        The same transaction records the request to stop the project's tasks
        (``store.request_task_stop``: written, or re-armed after a restore), so
        the two are committed together or not at all; ``ProjectTaskStopper``
        carries it out. A project that is Pending deletion already is returned
        unchanged: the 30 days do not restart and no request is written.
        """
        confirm_name = validate_confirmation(confirm_name)
        return await self._lifecycle(
            actor, project_id, LifecycleAction.BEGIN_DELETION, confirm_name
        )

    async def restore(self, actor: Principal, project_id: uuid.UUID) -> Project:
        """Pending deletion -> **Archived** within the 30 days (lifecycle.manage).

        Allowed for a Manager of the project and for Owner / Admin (Decision
        0008). ``DeletionWindowClosedError`` from ``deletion_scheduled_at`` on,
        ``NoManagerError`` when no accepted Manager is left. Both deletion
        timestamps are cleared. An Archived project: no change.
        """
        return await self._lifecycle(actor, project_id, LifecycleAction.RESTORE)

    async def _lifecycle(
        self,
        actor: Principal,
        project_id: uuid.UUID,
        action: LifecycleAction,
        confirm_name: str | None = None,
    ) -> Project:
        principal = self._actor(actor)
        project_id = validate_uuid("project_id", project_id)
        now = self._now()
        async with self._transaction() as session:
            project, _ = await self._guarded(
                session,
                principal,
                Capability.PROJECT_LIFECYCLE_MANAGE,
                project_id,
                lock=True,
            )
            if confirm_name is not None and confirm_name != project.name:
                raise ConfirmationMismatchError()
            plan = domain.plan_transition(project.status, action)
            if not plan.changed:
                return project
            started: datetime | None = None
            scheduled: datetime | None = None
            if action is LifecycleAction.BEGIN_DELETION:
                started, scheduled = now, domain.deletion_schedule(now)
            elif action is LifecycleAction.RESTORE:
                # A Pending deletion row always has its deadline (a CHECK
                # constraint); if it were missing the window counts as closed.
                deadline = project.deletion_scheduled_at
                if deadline is None or not domain.restore_window_open(deadline, now):
                    raise DeletionWindowClosedError()
                members = await store.list_active_members(session, project_id)
                if not any(m.role is ProjectRole.MANAGER for m in members):
                    raise NoManagerError()
            changed = await store.set_lifecycle(
                session,
                project_id,
                status=plan.new_status,
                deletion_started_at=started,
                deletion_scheduled_at=scheduled,
                now=now,
            )
            if action is LifecycleAction.BEGIN_DELETION:
                # The same transaction: the request to stop the project's tasks is
                # durable exactly when the deletion is (Decision 0008, section 8);
                # ``ProjectTaskStopper`` carries it out.
                await store.request_task_stop(session, project_id, now=now)
            return changed

    async def purge_expired(
        self,
        now: datetime | None = None,
        *,
        batch_size: int = DEFAULT_PURGE_BATCH_SIZE,
    ) -> PurgeResult:
        """Mark the projects whose 30 days are over Deleted (the Backend's janitor).

        **Not for users or agents**: it takes no actor and asks no Authorizer;
        only the Backend's own scheduled job may call it. ``now`` defaults to
        the clock (validated; naive is refused). In one transaction: the due
        projects (at most ``batch_size``; a project another transaction has
        locked is skipped), for each one: all its membership rows are deleted
        and ``store.mark_deleted`` writes the tombstone. Projects that are not
        due, and Deleted ones, are never touched. Idempotent: a second call finds
        nothing. ``has_more`` says that due projects remain.
        The data of other areas (chat, memory, tasks, repositories) is **not**
        deleted here: those areas delete it for the ids in ``PurgeResult.purged``.
        Nor are the project's tasks stopped here (that is the request that
        ``begin_deletion`` recorded); an unprocessed request stays open.
        """
        instant = self._now() if now is None else validate_instant("now", now)
        batch_size = validate_batch_size(batch_size)
        async with self._transaction() as session:
            due = await store.select_due_project_ids(session, instant, batch_size)
            purged: list[uuid.UUID] = []
            for project_id in due:
                project = await store.get_project(session, project_id, for_update=True)
                if (
                    project is None
                    or project.deletion_scheduled_at is None
                    or not domain.purge_due(project.deletion_scheduled_at, instant)
                ):
                    continue
                domain.plan_transition(project.status, LifecycleAction.PURGE)
                await store.delete_project_members(session, project_id)
                await store.mark_deleted(session, project_id, now=instant)
                purged.append(project_id)
            remaining = await store.count_due_projects(session, instant)
        return PurgeResult(purged=tuple(purged), has_more=remaining > 0)


__all__ = ["Clock", "ProjectService"]
