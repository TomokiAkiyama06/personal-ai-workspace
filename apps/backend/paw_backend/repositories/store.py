"""SQL statements of the repository module: one small function per statement.

Every function takes an ``AsyncSession`` whose transaction the *caller* (the
service) already began; a function never commits, rolls back, opens a
transaction, sets a timeout or catches an error. Locks are the caller's business
too, except where a docstring says a function locks. No function here authorizes
anything or decides a rule. Arguments are already validated (UUIDs, aware UTC
datetimes, enum members), so a function never validates or coerces them.

Statements are written with SQLAlchemy Core against the tables below (never with
string formatting of values).
"""

import uuid
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, delete, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz.capabilities import RepoPermission
from paw_backend.authz.subjects import RepoAcl
from paw_backend.projects.models import ProjectRow
from paw_backend.repositories.models import (
    RepositoryCheckoutRow,
    RepositoryRemoteRow,
    RepositoryRow,
)
from paw_backend.repositories.records import (
    Checkout,
    CheckoutState,
    Remote,
    Repository,
    RepositorySource,
)

REPOSITORIES = RepositoryRow.__table__
REMOTES = RepositoryRemoteRow.__table__
CHECKOUTS = RepositoryCheckoutRow.__table__
PROJECTS = ProjectRow.__table__


def _acl(row: Any) -> RepoAcl:
    if row.acl_allowed is None:
        return RepoAcl.inherit(row.id, row.project_id)
    return RepoAcl.override(
        row.id, row.project_id, (RepoPermission(name) for name in row.acl_allowed)
    )


def repository_from_row(row: Any) -> Repository:
    """A :class:`Repository` from a row of ``REPOSITORIES``."""
    return Repository(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        default_branch=row.default_branch,
        source=RepositorySource(row.source),
        acl=_acl(row),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def remote_from_row(row: Any) -> Remote:
    return Remote(row.repository_id, row.project_id, row.url, row.created_at)


def checkout_from_row(row: Any) -> Checkout:
    """A :class:`Checkout` from a row of ``CHECKOUTS``."""
    return Checkout(
        id=row.id,
        repository_id=row.repository_id,
        project_id=row.project_id,
        user_id=row.user_id,
        path=row.path,
        state=CheckoutState(row.state),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# --- repositories ---------------------------------------------------------------


async def insert_repository(
    session: AsyncSession,
    *,
    repository_id: uuid.UUID,
    project_id: uuid.UUID,
    name: str,
    default_branch: str,
    source: RepositorySource,
    created_by: uuid.UUID,
    now: datetime,
) -> Repository:
    """Insert a repository (with ``inherit`` as its ACL) and return it.

    ``sqlalchemy.exc.IntegrityError`` when the project already has a repository of
    this name without regard to case (``uq_repositories_project_id_lower_name``).
    """
    row = (
        await session.execute(
            insert(REPOSITORIES)
            .values(
                id=repository_id,
                project_id=project_id,
                name=name,
                default_branch=default_branch,
                source=source.value,
                acl_allowed=None,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            .returning(*REPOSITORIES.c)
        )
    ).one()
    return repository_from_row(row)


async def get_repository(
    session: AsyncSession,
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    *,
    for_update: bool = False,
    for_share: bool = False,
) -> Repository | None:
    """The repository **of this project**, or ``None`` (not another project's).

    ``for_share`` locks the row ``FOR SHARE``: until the transaction ends the
    repository cannot be deleted, so rows that reference it can be inserted without
    a foreign key error (a delete waits, then removes them with it).
    """
    statement = select(REPOSITORIES).where(
        REPOSITORIES.c.id == repository_id, REPOSITORIES.c.project_id == project_id
    )
    if for_update:
        statement = statement.with_for_update()
    elif for_share:
        statement = statement.with_for_update(read=True)
    row = (await session.execute(statement)).first()
    return None if row is None else repository_from_row(row)


async def get_repository_any_project(
    session: AsyncSession, repository_id: uuid.UUID
) -> Repository | None:
    """The repository with this id in any project (backend-internal reads)."""
    row = (
        await session.execute(
            select(REPOSITORIES).where(REPOSITORIES.c.id == repository_id)
        )
    ).first()
    return None if row is None else repository_from_row(row)


async def list_repositories(
    session: AsyncSession,
    project_id: uuid.UUID,
    limit: int,
    offset: int,
    *,
    permission: RepoPermission | None = None,
) -> list[Repository]:
    """The project's repositories by name (without case), then id; one page.

    With ``permission`` only the repositories whose ACL keeps it are listed: those
    that inherit, and those whose override contains it (the page is filtered in
    SQL, so a page is full whenever enough repositories qualify).
    """
    statement = select(REPOSITORIES).where(REPOSITORIES.c.project_id == project_id)
    if permission is not None:
        statement = statement.where(
            REPOSITORIES.c.acl_allowed.is_(None)
            | REPOSITORIES.c.acl_allowed.any(permission.value)
        )
    rows = await session.execute(
        statement.order_by(func.lower(REPOSITORIES.c.name), REPOSITORIES.c.id)
        .limit(limit)
        .offset(offset)
    )
    return [repository_from_row(row) for row in rows]


async def count_repositories(session: AsyncSession, project_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(REPOSITORIES)
            .where(REPOSITORIES.c.project_id == project_id)
        )
    ).scalar_one()


async def update_default_branch(
    session: AsyncSession,
    repository_id: uuid.UUID,
    default_branch: str,
    now: datetime,
) -> bool:
    """Set the default branch; whether the repository still exists."""
    result = await session.execute(
        update(REPOSITORIES)
        .where(REPOSITORIES.c.id == repository_id)
        .values(default_branch=default_branch, updated_at=now)
    )
    return result.rowcount == 1


async def set_acl(
    session: AsyncSession,
    repository_id: uuid.UUID,
    allowed: Collection[RepoPermission] | None,
    now: datetime,
) -> Repository | None:
    """Store the ACL override (``None``: inherit); the repository, ``None`` if gone."""
    values = None if allowed is None else sorted(p.value for p in allowed)
    row = (
        await session.execute(
            update(REPOSITORIES)
            .where(REPOSITORIES.c.id == repository_id)
            .values(acl_allowed=values, updated_at=now)
            .returning(*REPOSITORIES.c)
        )
    ).first()
    return None if row is None else repository_from_row(row)


async def delete_repository_if_unused(
    session: AsyncSession, repository_id: uuid.UUID
) -> bool:
    """Delete the repository only when nobody has a checkout of it (any state).

    For undoing a registration that a failed operation made: a checkout another
    user made meanwhile is theirs, and it keeps the repository. Whether a row was
    deleted.
    """
    result = await session.execute(
        delete(REPOSITORIES).where(
            REPOSITORIES.c.id == repository_id,
            ~select(CHECKOUTS.c.id)
            .where(CHECKOUTS.c.repository_id == repository_id)
            .exists(),
        )
    )
    return result.rowcount == 1


async def delete_repository(session: AsyncSession, repository_id: uuid.UUID) -> bool:
    """Delete the repository; its remotes and checkouts go with it (CASCADE)."""
    result = await session.execute(
        delete(REPOSITORIES).where(REPOSITORIES.c.id == repository_id)
    )
    return result.rowcount == 1


async def delete_repositories_of_deleted_projects(
    session: AsyncSession, project_ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    """Delete the repositories of these projects **that are Deleted**; the project ids.

    A project that is not (yet) a tombstone keeps its repositories: the caller's
    list is not trusted to say which projects are gone.
    """
    if not project_ids:
        return []
    due = select(PROJECTS.c.id).where(
        PROJECTS.c.id.in_(project_ids), PROJECTS.c.status == "deleted"
    )
    rows = await session.execute(
        delete(REPOSITORIES)
        .where(REPOSITORIES.c.project_id.in_(due))
        .returning(REPOSITORIES.c.project_id)
    )
    return sorted({row.project_id for row in rows})


# --- remotes -----------------------------------------------------------------------


async def insert_remote(
    session: AsyncSession,
    *,
    repository_id: uuid.UUID,
    project_id: uuid.UUID,
    url: str,
    now: datetime,
) -> Remote:
    """Insert one URL. ``IntegrityError`` when the project has this URL already."""
    row = (
        await session.execute(
            insert(REMOTES)
            .values(
                repository_id=repository_id,
                project_id=project_id,
                url=url,
                created_at=now,
            )
            .returning(*REMOTES.c)
        )
    ).one()
    return remote_from_row(row)


async def delete_remote(
    session: AsyncSession, repository_id: uuid.UUID, url: str
) -> bool:
    result = await session.execute(
        delete(REMOTES).where(
            REMOTES.c.repository_id == repository_id, REMOTES.c.url == url
        )
    )
    return result.rowcount == 1


async def list_remotes(session: AsyncSession, repository_id: uuid.UUID) -> list[Remote]:
    """The repository's URLs in a stable order (by URL)."""
    rows = await session.execute(
        select(REMOTES)
        .where(REMOTES.c.repository_id == repository_id)
        .order_by(REMOTES.c.url)
    )
    return [remote_from_row(row) for row in rows]


# --- checkouts -----------------------------------------------------------------------


async def insert_checkout(
    session: AsyncSession,
    *,
    checkout_id: uuid.UUID,
    repository_id: uuid.UUID,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    path: str,
    state: CheckoutState,
    now: datetime,
) -> Checkout:
    """Insert a checkout row.

    ``IntegrityError`` when the user has a checkout of this repository
    (``uq_repository_checkouts_repository_id``) or the path is registered
    (``uq_repository_checkouts_path``).
    """
    row = (
        await session.execute(
            insert(CHECKOUTS)
            .values(
                id=checkout_id,
                repository_id=repository_id,
                project_id=project_id,
                user_id=user_id,
                path=path,
                state=state.value,
                created_at=now,
                updated_at=now,
            )
            .returning(*CHECKOUTS.c)
        )
    ).one()
    return checkout_from_row(row)


async def get_checkout_of(
    session: AsyncSession,
    repository_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Checkout | None:
    """The user's checkout of the repository (any state), or ``None``."""
    statement = select(CHECKOUTS).where(
        CHECKOUTS.c.repository_id == repository_id, CHECKOUTS.c.user_id == user_id
    )
    if for_update:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).first()
    return None if row is None else checkout_from_row(row)


async def mark_ready(
    session: AsyncSession, checkout_id: uuid.UUID, now: datetime
) -> Checkout | None:
    """``pending`` becomes ``ready``; the checkout, ``None`` if gone or not pending."""
    row = (
        await session.execute(
            update(CHECKOUTS)
            .where(CHECKOUTS.c.id == checkout_id, CHECKOUTS.c.state == "pending")
            .values(state="ready", updated_at=now)
            .returning(*CHECKOUTS.c)
        )
    ).first()
    return None if row is None else checkout_from_row(row)


async def delete_checkout(
    session: AsyncSession, checkout_id: uuid.UUID, *, only_pending: bool = False
) -> bool:
    """Delete a checkout row (the files are not touched). Whether a row was deleted."""
    statement = delete(CHECKOUTS).where(CHECKOUTS.c.id == checkout_id)
    if only_pending:
        statement = statement.where(CHECKOUTS.c.state == "pending")
    return (await session.execute(statement)).rowcount == 1


async def list_checkouts_of(
    session: AsyncSession, user_id: uuid.UUID, limit: int, offset: int
) -> list[Checkout]:
    """The user's own checkouts, newest first, then id; one page."""
    rows = await session.execute(
        select(CHECKOUTS)
        .where(CHECKOUTS.c.user_id == user_id)
        .order_by(CHECKOUTS.c.created_at.desc(), CHECKOUTS.c.id)
        .limit(limit)
        .offset(offset)
    )
    return [checkout_from_row(row) for row in rows]


async def nested_checkouts(
    session: AsyncSession, user_id: uuid.UUID, path: str, ancestors: Sequence[str]
) -> list[Checkout]:
    """The user's other ``ready`` checkouts that enclose or are enclosed by ``path``.

    ``ancestors`` are the proper ancestor directories of ``path`` (``/a``,
    ``/a/b`` for ``/a/b/c``): a checkout at one of them encloses this one (found
    through the unique index on the path). A checkout below ``path`` is found by a
    prefix comparison that does not interpret ``%`` or ``_`` in a path
    (``starts_with``). Only this user's rows are looked at: nesting is a fact of one
    user's file system. ``path`` itself is not returned.
    """
    enclosing = CHECKOUTS.c.path.in_(list(ancestors)) if ancestors else literal(False)
    enclosed = func.starts_with(CHECKOUTS.c.path, bindparam("prefix"))
    rows = await session.execute(
        select(CHECKOUTS)
        .where(
            CHECKOUTS.c.user_id == user_id,
            CHECKOUTS.c.state == "ready",
            CHECKOUTS.c.path != path,
            enclosing | enclosed,
        )
        .order_by(CHECKOUTS.c.path),
        {"prefix": path + "/"},
    )
    return [checkout_from_row(row) for row in rows]
