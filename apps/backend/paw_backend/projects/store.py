"""SQL statements of the project module: one small function per statement.

Every function takes an ``AsyncSession`` whose transaction the *caller* (the
service) already began; a function never commits, rolls back, opens a
transaction, sets a timeout or catches an error. Locks are the caller's
business too, except where a docstring says a function locks. No function here
authorizes anything or decides a rule: rules live in ``domain.py`` and
``service.py``. Arguments are already validated (UUIDs, aware UTC datetimes,
enum members), so a function never validates or coerces them.

Statements are written with SQLAlchemy Core against the tables below (never
with string formatting of values). The helper functions ``project_from_row`` and
``member_from_row`` turn a result row into the records; a function that
"returns a Project" returns exactly what the row holds after the statement.

IMPLEMENTATION NOTE (PAW-026 stubs): every ``async def`` below is a stub.
Replace the ``raise NotImplementedError`` with the statement its docstring
describes. Do not change a signature or a docstring. Do not commit.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz.roles import ProjectRole
from paw_backend.identity.models import UserRow
from paw_backend.projects.limits import DELETED_PROJECT_NAME
from paw_backend.projects.models import (
    ProjectMemberRow,
    ProjectRow,
    ProjectTaskStopRow,
)
from paw_backend.projects.records import (
    Member,
    MemberStatus,
    PendingInvite,
    Project,
    ProjectStatus,
)
from paw_backend.tasks.domain import TERMINAL_STATES
from paw_backend.tasks.models import TaskRow
from paw_backend.tasks.queueing.domain import ACTIVE_QUEUE_STATUSES
from paw_backend.tasks.queueing.models import QueueEntryRow

PROJECTS = ProjectRow.__table__
MEMBERS = ProjectMemberRow.__table__
TASK_STOPS = ProjectTaskStopRow.__table__
USERS = UserRow.__table__
# The task tables belong to PAW-032 (``tasks``) and PAW-033 (``queue_entries``).
# The project module only ever READS them: tasks and entries are stopped through
# ``TaskService`` / ``TaskQueue``, never by an UPDATE.
TASKS = TaskRow.__table__
QUEUE_ENTRIES = QueueEntryRow.__table__


def project_from_row(row: Any) -> Project:
    """A :class:`Project` from a row of ``PROJECTS`` (any object with its columns)."""
    return Project(
        id=row.id,
        name=row.name,
        description=row.description,
        status=ProjectStatus(row.status),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deletion_started_at=row.deletion_started_at,
        deletion_scheduled_at=row.deletion_scheduled_at,
        deleted_at=row.deleted_at,
    )


def member_from_row(row: Any) -> Member:
    """A :class:`Member` from a row of ``MEMBERS`` (any object with its columns)."""
    return Member(
        project_id=row.project_id,
        user_id=row.user_id,
        role=ProjectRole(row.role),
        status=MemberStatus(row.status),
        invited_at=row.invited_at,
        invite_expires_at=row.invite_expires_at,
        joined_at=row.joined_at,
    )


# --- projects ------------------------------------------------------------------


async def get_project(
    session: AsyncSession, project_id: uuid.UUID, *, for_update: bool = False
) -> Project | None:
    """The project with this id in **any** status (a tombstone too), or ``None``.

    With ``for_update=True`` the statement is ``SELECT ... FOR UPDATE`` (it
    waits for a row lock held by another transaction), which is how every
    changing operation of the service starts. Nothing else is locked.
    """
    statement = select(PROJECTS).where(PROJECTS.c.id == project_id)
    if for_update:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).first()
    return None if row is None else project_from_row(row)


async def get_project_for_share(
    session: AsyncSession, project_id: uuid.UUID
) -> Project | None:
    """Like :func:`get_project`, with ``SELECT ... FOR SHARE`` (waits for a writer).

    Every lifecycle change locks the row ``FOR UPDATE`` first, so while this
    transaction holds the share lock the project cannot be archived, deleted or
    restored (concurrent share locks do not exclude each other).
    """
    row = (
        await session.execute(
            select(PROJECTS)
            .where(PROJECTS.c.id == project_id)
            .with_for_update(read=True)
        )
    ).first()
    return None if row is None else project_from_row(row)


async def insert_project(
    session: AsyncSession,
    *,
    name: str,
    description: str | None,
    created_by: uuid.UUID,
    now: datetime,
) -> Project:
    """Insert a new ACTIVE project and return it.

    The id is generated by the database (``gen_random_uuid()``, the column
    default). ``created_at`` and ``updated_at`` are both ``now``; the three
    deletion columns are ``None``.
    """
    row = (
        await session.execute(
            insert(PROJECTS)
            .values(
                name=name,
                description=description,
                status="active",
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            .returning(*PROJECTS.c)
        )
    ).one()
    return project_from_row(row)


async def update_settings(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    name: str,
    description: str | None,
    now: datetime,
) -> Project:
    """Set ``name`` and ``description`` and ``updated_at = now``; return the project.

    Nothing else changes (not the status). ``sqlalchemy.exc.NoResultFound`` if
    there is no such project.
    """
    row = (
        await session.execute(
            update(PROJECTS)
            .where(PROJECTS.c.id == project_id)
            .values(name=name, description=description, updated_at=now)
            .returning(*PROJECTS.c)
        )
    ).one_or_none()
    if row is None:
        raise NoResultFound()
    return project_from_row(row)


async def set_lifecycle(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    status: ProjectStatus,
    deletion_started_at: datetime | None,
    deletion_scheduled_at: datetime | None,
    now: datetime,
) -> Project:
    """Set ``status``, both deletion timestamps and ``updated_at = now``.

    ``None`` for a deletion timestamp clears it.

    Returns the project. The name, the description and ``deleted_at`` are not
    touched. ``sqlalchemy.exc.NoResultFound`` if there is no such project.
    """
    row = (
        await session.execute(
            update(PROJECTS)
            .where(PROJECTS.c.id == project_id)
            .values(
                status=status.value,
                deletion_started_at=deletion_started_at,
                deletion_scheduled_at=deletion_scheduled_at,
                updated_at=now,
            )
            .returning(*PROJECTS.c)
        )
    ).one_or_none()
    if row is None:
        raise NoResultFound()
    return project_from_row(row)


async def mark_deleted(
    session: AsyncSession, project_id: uuid.UUID, *, now: datetime
) -> Project:
    """Turn the project into its tombstone and return it.

    ``status = DELETED``, ``deleted_at = now``, ``updated_at = now``,
    ``name = paw_backend.projects.limits.DELETED_PROJECT_NAME``,
    ``description = None``. ``created_by``, ``created_at`` and both deletion
    timestamps keep their values. ``sqlalchemy.exc.NoResultFound`` if there is
    no such project.
    """
    row = (
        await session.execute(
            update(PROJECTS)
            .where(PROJECTS.c.id == project_id)
            .values(
                status="deleted",
                deleted_at=now,
                updated_at=now,
                name=DELETED_PROJECT_NAME,
                description=None,
            )
            .returning(*PROJECTS.c)
        )
    ).one_or_none()
    if row is None:
        raise NoResultFound()
    return project_from_row(row)


async def select_due_project_ids(
    session: AsyncSession, now: datetime, limit: int
) -> list[uuid.UUID]:
    """Ids of Pending deletion projects that are due at ``now``, oldest first.

    Due means ``status = 'pending_deletion'`` and ``deletion_scheduled_at <=
    now`` (equal counts). At most ``limit`` ids, ordered by
    ``deletion_scheduled_at`` then ``id``. The rows are locked with
    ``FOR UPDATE SKIP LOCKED``: a project another transaction holds a lock on
    is silently left out, and the statement never waits.
    """
    statement = (
        select(PROJECTS.c.id)
        .where(
            PROJECTS.c.status == "pending_deletion",
            PROJECTS.c.deletion_scheduled_at <= now,
        )
        .order_by(PROJECTS.c.deletion_scheduled_at, PROJECTS.c.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list((await session.execute(statement)).scalars())


async def count_due_projects(session: AsyncSession, now: datetime) -> int:
    """How many Pending deletion projects are due at ``now`` (same rule as above).

    Plain ``count``: no lock and no ``SKIP LOCKED``.
    """
    statement = (
        select(func.count())
        .select_from(PROJECTS)
        .where(
            PROJECTS.c.status == "pending_deletion",
            PROJECTS.c.deletion_scheduled_at <= now,
        )
    )
    return (await session.execute(statement)).scalar_one()


# --- members and invitations -------------------------------------------------------


async def get_member(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> Member | None:
    """The membership row (accepted or invited, expired or not) or ``None``."""
    row = (
        await session.execute(
            select(MEMBERS).where(
                MEMBERS.c.project_id == project_id, MEMBERS.c.user_id == user_id
            )
        )
    ).first()
    return None if row is None else member_from_row(row)


async def insert_member(session: AsyncSession, member: Member) -> Member:
    """Insert ``member`` exactly as given and return the stored row as a Member.

    A row for the same ``(project_id, user_id)`` violates the primary key
    (``sqlalchemy.exc.IntegrityError``, not handled here).
    """
    row = (
        await session.execute(
            insert(MEMBERS)
            .values(
                project_id=member.project_id,
                user_id=member.user_id,
                role=member.role.value,
                status=member.status.value,
                invited_at=member.invited_at,
                invite_expires_at=member.invite_expires_at,
                joined_at=member.joined_at,
            )
            .returning(*MEMBERS.c)
        )
    ).one()
    return member_from_row(row)


async def activate_invite(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    joined_at: datetime,
) -> Member:
    """Turn an INVITED row into an ACTIVE one and return it.

    ``status = ACTIVE``, ``joined_at = joined_at``, ``invite_expires_at =
    None``; ``role`` and ``invited_at`` are kept. Only an INVITED row is
    changed: ``sqlalchemy.exc.NoResultFound`` when there is no row or it is
    ACTIVE already.
    """
    row = (
        await session.execute(
            update(MEMBERS)
            .where(
                MEMBERS.c.project_id == project_id,
                MEMBERS.c.user_id == user_id,
                MEMBERS.c.status == "invited",
            )
            .values(status="active", joined_at=joined_at, invite_expires_at=None)
            .returning(*MEMBERS.c)
        )
    ).one_or_none()
    if row is None:
        raise NoResultFound()
    return member_from_row(row)


async def set_member_role(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    role: ProjectRole,
) -> Member:
    """Set the role of an ACTIVE member and return the row.

    Only an ACTIVE row is changed (an invitation's role is never changed):
    ``sqlalchemy.exc.NoResultFound`` when there is no such ACTIVE row.
    """
    row = (
        await session.execute(
            update(MEMBERS)
            .where(
                MEMBERS.c.project_id == project_id,
                MEMBERS.c.user_id == user_id,
                MEMBERS.c.status == "active",
            )
            .values(role=role.value)
            .returning(*MEMBERS.c)
        )
    ).one_or_none()
    if row is None:
        raise NoResultFound()
    return member_from_row(row)


async def delete_member(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Delete the row of this user in this project (any status).

    ``True`` if a row was deleted, ``False`` if there was none. Other rows are
    never touched.
    """
    result = await session.execute(
        delete(MEMBERS).where(
            MEMBERS.c.project_id == project_id, MEMBERS.c.user_id == user_id
        )
    )
    return result.rowcount > 0


async def delete_project_members(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Delete every row (accepted members and invitations) of one project.

    Returns how many rows were deleted (``0`` for none). Rows of other projects
    are never touched.
    """
    result = await session.execute(
        delete(MEMBERS).where(MEMBERS.c.project_id == project_id)
    )
    return result.rowcount


async def list_active_members(
    session: AsyncSession, project_id: uuid.UUID
) -> list[Member]:
    """The ACTIVE members of one project, ordered by ``joined_at`` then ``user_id``."""
    rows = await session.execute(
        select(MEMBERS)
        .where(MEMBERS.c.project_id == project_id, MEMBERS.c.status == "active")
        .order_by(MEMBERS.c.joined_at, MEMBERS.c.user_id)
    )
    return [member_from_row(r) for r in rows]


async def list_open_invites(
    session: AsyncSession, project_id: uuid.UUID, now: datetime
) -> list[Member]:
    """The open invitations of one project at ``now``.

    Ordered by ``invited_at``, then ``user_id``.

    Open means ``status = 'invited'`` and ``invite_expires_at > now`` (an
    invitation whose expiry equals ``now`` is expired and is left out).
    """
    rows = await session.execute(
        select(MEMBERS)
        .where(
            MEMBERS.c.project_id == project_id,
            MEMBERS.c.status == "invited",
            MEMBERS.c.invite_expires_at > now,
        )
        .order_by(MEMBERS.c.invited_at, MEMBERS.c.user_id)
    )
    return [member_from_row(r) for r in rows]


async def count_members(
    session: AsyncSession, project_id: uuid.UUID, now: datetime
) -> int:
    """ACTIVE members plus open invitations of one project at ``now``.

    Expired invitations are not counted.
    """
    statement = (
        select(func.count())
        .select_from(MEMBERS)
        .where(
            MEMBERS.c.project_id == project_id,
            or_(
                MEMBERS.c.status == "active",
                and_(MEMBERS.c.status == "invited", MEMBERS.c.invite_expires_at > now),
            ),
        )
    )
    return (await session.execute(statement)).scalar_one()


# --- what one user sees ----------------------------------------------------


async def list_projects_of(
    session: AsyncSession,
    user_id: uuid.UUID,
    status: ProjectStatus,
    limit: int,
    offset: int,
) -> list[Project]:
    """The projects in ``status`` in which ``user_id`` is an ACTIVE member.

    An invitation does not make a project visible. For ``PENDING_DELETION``
    only the projects where the user's role is ``MANAGER`` are returned (only a
    Manager can restore); for the other statuses every role sees the project.
    Ordered by ``created_at`` newest first, then ``id`` ascending; ``limit`` and
    ``offset`` are applied after the ordering.
    """
    statement = (
        select(PROJECTS)
        .join(MEMBERS, MEMBERS.c.project_id == PROJECTS.c.id)
        .where(
            MEMBERS.c.user_id == user_id,
            MEMBERS.c.status == "active",
            PROJECTS.c.status == status.value,
        )
    )
    if status is ProjectStatus.PENDING_DELETION:
        statement = statement.where(MEMBERS.c.role == "manager")
    statement = (
        statement.order_by(PROJECTS.c.created_at.desc(), PROJECTS.c.id)
        .limit(limit)
        .offset(offset)
    )
    return [project_from_row(r) for r in await session.execute(statement)]


async def roles_of(
    session: AsyncSession, user_id: uuid.UUID
) -> dict[uuid.UUID, ProjectRole]:
    """``{project_id: role}`` of the ACTIVE memberships of ``user_id``.

    Only projects whose status is not ``DELETED`` (invitations never count).
    An empty dict for a user without membership.
    """
    rows = await session.execute(
        select(MEMBERS.c.project_id, MEMBERS.c.role)
        .join(PROJECTS, PROJECTS.c.id == MEMBERS.c.project_id)
        .where(
            MEMBERS.c.user_id == user_id,
            MEMBERS.c.status == "active",
            PROJECTS.c.status != "deleted",
        )
    )
    return {row.project_id: ProjectRole(row.role) for row in rows}


async def list_open_invites_of(
    session: AsyncSession, user_id: uuid.UUID, now: datetime
) -> list[PendingInvite]:
    """The open invitations addressed to ``user_id`` at ``now``.

    Open means ``status = 'invited'`` and ``invite_expires_at > now``, and the
    project's status is ``ACTIVE`` or ``ARCHIVED`` (an invitation to a Pending
    deletion or Deleted project is not shown). ``PendingInvite.project_name`` is
    the project's name, ``expires_at`` the invitation's ``invite_expires_at``.
    Ordered by ``invited_at`` then ``project_id``.
    """
    rows = await session.execute(
        select(
            MEMBERS.c.project_id,
            PROJECTS.c.name,
            MEMBERS.c.role,
            MEMBERS.c.invited_at,
            MEMBERS.c.invite_expires_at,
        )
        .join(PROJECTS, PROJECTS.c.id == MEMBERS.c.project_id)
        .where(
            MEMBERS.c.user_id == user_id,
            MEMBERS.c.status == "invited",
            MEMBERS.c.invite_expires_at > now,
            PROJECTS.c.status.in_(["active", "archived"]),
        )
        .order_by(MEMBERS.c.invited_at, MEMBERS.c.project_id)
    )
    return [
        PendingInvite(
            r.project_id, r.name, ProjectRole(r.role), r.invited_at, r.invite_expires_at
        )
        for r in rows
    ]


async def user_is_active(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Whether ``users`` has a row with this id and ``status = 'active'``."""
    row = (
        await session.execute(
            select(USERS.c.id).where(USERS.c.id == user_id, USERS.c.status == "active")
        )
    ).first()
    return row is not None


# --- stopping the tasks of a project (Decision 0008, section 8) -----------------------


async def request_task_stop(
    session: AsyncSession, project_id: uuid.UUID, *, now: datetime
) -> None:
    """Record that the tasks of the project must be stopped (or re-arm the request).

    ``INSERT ... ON CONFLICT (project_id) DO UPDATE``: the project's single row is
    created with ``requested_at = now`` and ``processed_at = NULL``; if a row
    exists (a deletion that was restored and is begun again) it becomes that new,
    open request. Called in the transaction of ``begin_deletion``.
    """
    statement = pg_insert(TASK_STOPS).values(
        project_id=project_id, requested_at=now, processed_at=None
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[TASK_STOPS.c.project_id],
            set_={
                "requested_at": statement.excluded.requested_at,
                "processed_at": None,
            },
        )
    )


async def list_open_task_stops(session: AsyncSession, limit: int) -> list[uuid.UUID]:
    """Project ids whose request is not processed, oldest ``requested_at`` first.

    At most ``limit`` ids, ties broken by id. Plain read: no lock.
    """
    statement = (
        select(TASK_STOPS.c.project_id)
        .where(TASK_STOPS.c.processed_at.is_(None))
        .order_by(TASK_STOPS.c.requested_at, TASK_STOPS.c.project_id)
        .limit(limit)
    )
    return list((await session.execute(statement)).scalars())


async def mark_task_stop_processed(
    session: AsyncSession, project_id: uuid.UUID, *, now: datetime
) -> bool:
    """Set ``processed_at = now`` of an open request; ``False`` if there is none.

    A request that is processed already is left as it is (its first
    ``processed_at`` stays), and so is a project that has no row.
    """
    result = await session.execute(
        update(TASK_STOPS)
        .where(
            TASK_STOPS.c.project_id == project_id, TASK_STOPS.c.processed_at.is_(None)
        )
        .values(processed_at=now)
    )
    return result.rowcount > 0


async def select_active_task_ids(
    session: AsyncSession, project_id: uuid.UUID, limit: int
) -> list[uuid.UUID]:
    """Ids of the project's tasks that are not in a terminal state, oldest first.

    Active means queued, running, waiting, paused or evaluating (everything but
    completed, failed and cancelled: ``paw_backend.tasks.TERMINAL_STATES``). At
    most ``limit`` ids, ordered by ``created_at`` then ``id``. A plain read.
    """
    statement = (
        select(TASKS.c.id)
        .where(
            TASKS.c.project_id == project_id,
            TASKS.c.state.notin_(sorted(TERMINAL_STATES)),
        )
        .order_by(TASKS.c.created_at, TASKS.c.id)
        .limit(limit)
    )
    return list((await session.execute(statement)).scalars())


async def is_task_terminal(session: AsyncSession, task_id: uuid.UUID) -> bool:
    """Whether the task exists and is completed, failed or cancelled. A plain read.

    ``False`` for an unknown id (there is nothing to reconcile for it).
    """
    state = (
        await session.execute(select(TASKS.c.state).where(TASKS.c.id == task_id))
    ).scalar_one_or_none()
    return state in TERMINAL_STATES


async def has_active_task(session: AsyncSession, project_id: uuid.UUID) -> bool:
    """Whether at least one task of the project is active (same rule as above)."""
    return bool(await select_active_task_ids(session, project_id, 1))


async def select_active_entry_task_ids(
    session: AsyncSession,
    project_id: uuid.UUID,
    limit: int,
    *,
    terminal_tasks_only: bool = False,
) -> list[uuid.UUID]:
    """Ids of the project's tasks that have an active queue entry, oldest entry first.

    Active means ``queued`` or ``claimed`` (``ACTIVE_QUEUE_STATUSES``). Found
    through the project (``queue_entries`` joined to ``tasks``), whatever state
    the TASK is in: a terminal task can still have an entry (a restart that raced
    with a stop enqueued it). ``terminal_tasks_only`` keeps only the entries of
    tasks that are completed, failed or cancelled: the entry of a task that is
    still active is the way it runs again if its project is restored. At most one
    entry per task is active (a unique index), so the ids are distinct. At most
    ``limit`` ids. A plain read.
    """
    conditions = [
        TASKS.c.project_id == project_id,
        QUEUE_ENTRIES.c.status.in_(sorted(ACTIVE_QUEUE_STATUSES)),
    ]
    if terminal_tasks_only:
        conditions.append(TASKS.c.state.in_(sorted(TERMINAL_STATES)))
    statement = (
        select(QUEUE_ENTRIES.c.task_id)
        .join(TASKS, TASKS.c.id == QUEUE_ENTRIES.c.task_id)
        .where(*conditions)
        .order_by(QUEUE_ENTRIES.c.id)
        .limit(limit)
    )
    return list((await session.execute(statement)).scalars())


async def has_active_queue_entry(session: AsyncSession, project_id: uuid.UUID) -> bool:
    """Whether a task of the project has an active queue entry (same rule as above)."""
    return bool(await select_active_entry_task_ids(session, project_id, 1))
