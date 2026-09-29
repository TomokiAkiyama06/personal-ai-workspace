"""Reading the memories to project from PostgreSQL (PAW-045, Decision 0038).

PostgreSQL is the source of truth; the projection is a view of it. One call reads
the **current version** (the highest ``version_number``) of every memory in one
``REPEATABLE READ, READ ONLY`` transaction, so the files of one run are one
consistent snapshot (a memory moved from one scope to another during the read
appears in exactly one directory).

What is left out (Decision 0038 3): a memory whose current version is
``session_only`` (not Long-term Memory: REQUIREMENTS.md "Session / Task終了後に
保持しない"). Older versions are not projected: the history stays in PostgreSQL
(and in the Git history of the Recovery Repository, PAW-047).

This is a backend-internal job, not a request of a principal: it reads every
scope, and the separation of audiences is done by **where** each memory is
written (``render.directory_for``) and by the file modes (``writer``), not by the
ACL condition of ``memory/acl.py`` (there is no principal to build it from). The
same exception as the freshness jobs (Decision 0034 5), documented in Decision
0038 7.

A database error leaves as ``ProjectionDatabaseError`` with only the SQLSTATE,
detached from the driver's error (whose text can quote a memory).
"""

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, StatementError

from paw_backend.db import Database
from paw_backend.memory.models import FreshnessPolicy, MemoryVersion
from paw_backend.memory.projection.records import ProjectedMemory

_V = MemoryVersion.__table__


class ProjectionDatabaseError(Exception):
    """Reading the memories failed. ``sqlstate`` only; never the driver's text."""

    def __init__(self, sqlstate: str | None) -> None:
        self.sqlstate = sqlstate
        super().__init__(
            "reading the memories failed"
            + (f" (SQLSTATE {sqlstate})" if sqlstate else "")
        )


def _current_versions_statement():
    newest = (
        select(
            _V,
            func.row_number()
            .over(partition_by=_V.c.memory_id, order_by=_V.c.version_number.desc())
            .label("newest"),
        )
    ).subquery("ranked")
    return (
        select(newest)
        .where(
            newest.c.newest == 1,
            newest.c.freshness_policy != FreshnessPolicy.SESSION_ONLY.value,
        )
        .order_by(newest.c.memory_id)
    )


class MemoryProjectionSource:
    """``current_versions()``: the memories to project, one snapshot."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database

    async def current_versions(self) -> list[ProjectedMemory]:
        failure: ProjectionDatabaseError | None = None
        try:
            async with self._database.session() as session, session.begin():
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                rows = (await session.execute(_current_versions_statement())).all()
        except StatementError as error:
            orig = error.orig if isinstance(error, DBAPIError) else None
            sqlstate = getattr(orig, "sqlstate", None)
            failure = ProjectionDatabaseError(
                sqlstate if isinstance(sqlstate, str) else None
            )
        if failure is not None:
            try:
                raise failure from None
            except ProjectionDatabaseError as clean:
                clean.__context__ = None
                raise
        return [
            ProjectedMemory(
                memory_id=row.memory_id,
                version_number=row.version_number,
                scope=row.scope,
                owner_user_id=row.owner_user_id,
                project_id=row.project_id,
                project_group_id=row.project_group_id,
                repo_id=row.repo_id,
                memory_type=row.memory_type,
                title=row.title,
                content=row.content,
                importance=row.importance,
                pinned=row.pinned,
                status=row.status,
                confirmation_state=row.confirmation_state,
                freshness_policy=row.freshness_policy,
                verified_at=row.verified_at,
                revalidate_after=row.revalidate_after,
                revalidate_triggers=tuple(row.revalidate_triggers or ()),
                expires_at=row.expires_at,
                commit_sha=row.commit_sha,
                branch=row.branch,
                stale_since=row.stale_since,
                created_at=row.created_at,
            )
            for row in rows
        ]


__all__ = ["MemoryProjectionSource", "ProjectionDatabaseError"]
