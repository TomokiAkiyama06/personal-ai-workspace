"""Helpers for the project tests (PAW-026).

Rows are **seeded with SQL**, not through the service, and asserted with SQL, so
that a test of one method does not depend on another method being right. The
service under test runs on its own async engine (as a second backend process
would); the seeding connection is separate and always commits. The clock is
injected: nothing depends on the real time or on the speed of the machine.
"""

import asyncio
import unittest
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text

from paw_backend.authz import Authorizer, InMemoryAuditSink, Principal, SystemRole
from paw_backend.authz.roles import ProjectRole
from paw_backend.db import Database
from paw_backend.projects import Member, MemberStatus, ProjectService, ProjectStatus
from paw_backend.projects.limits import DELETION_RETENTION, INVITE_TTL

from .memory_support import (
    TEST_DATABASE_URL,
    migrate,
    requires_postgres,
    sync_database_url,
)
from .support import make_settings

__all__ = [
    "T0",
    "FakeClock",
    "PostgresProjectTestCase",
    "Team",
    "member",
    "requires_postgres",
]

# The instant every test "starts" at. Nothing here depends on the real clock.
T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """An injectable clock that only moves when the test says so."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def member(
    role: ProjectRole = ProjectRole.CONTRIBUTOR,
    status: MemberStatus = MemberStatus.ACTIVE,
    *,
    user_id: UUID | None = None,
    project_id: UUID | None = None,
    invited_at: datetime = T0,
    expires_at: datetime | None = None,
) -> Member:
    """A Member record; an invitation lasts 14 days from ``invited_at`` by default."""
    if status is MemberStatus.INVITED:
        return Member(
            project_id or uuid4(),
            user_id or uuid4(),
            role,
            status,
            invited_at,
            expires_at or invited_at + INVITE_TTL,
            None,
        )
    return Member(
        project_id or uuid4(),
        user_id or uuid4(),
        role,
        status,
        invited_at,
        None,
        invited_at,
    )


class Team(NamedTuple):
    """The user ids of one accepted member per role."""

    manager: UUID
    contributor: UUID
    viewer: UUID


class PostgresProjectTestCase(unittest.IsolatedAsyncioTestCase):
    """A service with a real Authorizer on a real PostgreSQL and an injected clock."""

    engine: Any

    @classmethod
    def setUpClass(cls) -> None:
        migrate("upgrade", "head")
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.clean_tables()
        cls.engine.dispose()

    @classmethod
    def clean_tables(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(text("TRUNCATE project_members, projects CASCADE"))
            connection.execute(text("TRUNCATE users CASCADE"))

    async def asyncSetUp(self) -> None:
        self.clean_tables()
        self.clock = FakeClock()
        self.sink = InMemoryAuditSink()
        self.database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(self.database.dispose)
        self.service = self.new_service()

    def new_service(
        self, clock: FakeClock | None = None, **options: Any
    ) -> ProjectService:
        """A service on an engine of its own, closed when the test ends."""
        clock = clock or self.clock
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        return ProjectService(
            database, Authorizer(self.sink, clock=clock), clock=clock, **options
        )

    # -- actors -------------------------------------------------------------------

    @staticmethod
    def actor(user_id: UUID, system_role: SystemRole = SystemRole.USER) -> Principal:
        """A Principal **without** project roles: the service must read them itself."""
        return Principal(user_id, system_role)

    # -- asynchronous helpers -------------------------------------------------------

    def spawn(self, coroutine: Coroutine) -> asyncio.Task:
        """Run ``coroutine`` as a task that is cancelled if the test ends early."""
        task = asyncio.ensure_future(coroutine)

        async def stop() -> None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(stop)
        return task

    def assertWaiting(self, task: asyncio.Task) -> None:
        """The task is still running, i.e. it is waiting for a lock."""
        if task.done():
            task.result()
            self.fail("the operation did not wait for the row lock")

    def hold_row_lock(self, sql: str, **parameters: Any):
        """Lock rows from a separate connection until the transaction ends."""
        connection = self.engine.connect()
        self.addCleanup(connection.close)
        transaction = connection.begin()
        self.addCleanup(
            lambda: transaction.rollback() if transaction.is_active else None
        )
        connection.execute(text(sql), parameters)
        return connection, transaction

    def lock_project(self, project_id: UUID):
        return self.hold_row_lock(
            "SELECT id FROM projects WHERE id = :id FOR UPDATE", id=project_id
        )

    # -- seeding (SQL) ---------------------------------------------------------------

    def seed_user(self, *, status: str = "active", system_role: str = "user") -> UUID:
        user_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, login_name, system_role, status,"
                    " passkey_required, created_at, updated_at) VALUES (:id, :login,"
                    " :role, :status, :passkey, :now, :now)"
                ),
                {
                    "id": user_id,
                    "login": "u" + user_id.hex[:12],
                    "role": system_role,
                    "status": status,
                    "passkey": system_role in ("owner", "admin"),
                    "now": T0,
                },
            )
        return user_id

    def seed_project(
        self,
        status: ProjectStatus | str = ProjectStatus.ACTIVE,
        *,
        name: str = "Alpha",
        description: str | None = None,
        created_by: UUID | None = None,
        created_at: datetime = T0,
        deletion_started_at: datetime | None = None,
        deleted_at: datetime | None = None,
    ) -> UUID:
        """Insert a project. Pending deletion / Deleted get a deadline 30 days on.

        ``deletion_started_at`` defaults to ``T0`` for those two statuses.
        """
        status = ProjectStatus(status)
        started = scheduled = None
        if status in (ProjectStatus.PENDING_DELETION, ProjectStatus.DELETED):
            started = deletion_started_at or T0
            scheduled = started + DELETION_RETENTION
        if status is ProjectStatus.DELETED:
            name, description = "Deleted Project", None
            deleted_at = deleted_at or scheduled
        project_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects (id, name, description, status, created_by,"
                    " created_at, updated_at, deletion_started_at,"
                    " deletion_scheduled_at, deleted_at) VALUES (:id, :name, :desc,"
                    " :status, :by, :created, :created, :started, :scheduled,"
                    " :deleted)"
                ),
                {
                    "id": project_id,
                    "name": name,
                    "desc": description,
                    "status": status.value,
                    "by": created_by,
                    "created": created_at,
                    "started": started,
                    "scheduled": scheduled,
                    "deleted": deleted_at,
                },
            )
        return project_id

    def seed_member(
        self,
        project_id: UUID,
        user_id: UUID | None = None,
        role: ProjectRole = ProjectRole.CONTRIBUTOR,
        status: MemberStatus = MemberStatus.ACTIVE,
        *,
        invited_at: datetime = T0,
        expires_at: datetime | None = None,
        joined_at: datetime | None = None,
    ) -> UUID:
        """Insert a membership row; a new active user is created if none is given."""
        user_id = user_id or self.seed_user()
        if status is MemberStatus.INVITED:
            expires_at = expires_at or invited_at + INVITE_TTL
            joined_at = None
        else:
            expires_at = None
            joined_at = joined_at or invited_at
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO project_members (project_id, user_id, role, status,"
                    " invited_at, invite_expires_at, joined_at) VALUES (:project,"
                    " :user, :role, :status, :invited, :expires, :joined)"
                ),
                {
                    "project": project_id,
                    "user": user_id,
                    "role": role.value,
                    "status": status.value,
                    "invited": invited_at,
                    "expires": expires_at,
                    "joined": joined_at,
                },
            )
        return user_id

    def seed_manager(self, project_id: UUID, **values: Any) -> UUID:
        return self.seed_member(project_id, role=ProjectRole.MANAGER, **values)

    def seed_team(self, project_id: UUID) -> Team:
        """One accepted Manager, Contributor and Viewer (new users) in the project."""
        return Team(
            manager=self.seed_manager(project_id),
            contributor=self.seed_member(project_id, role=ProjectRole.CONTRIBUTOR),
            viewer=self.seed_member(project_id, role=ProjectRole.VIEWER),
        )

    def set_project(self, project_id: UUID, **values: Any) -> None:
        """Change columns of a project directly (to set up a state)."""
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE projects SET "
                    + ", ".join(f"{name} = :{name}" for name in values)
                    + " WHERE id = :project_id"
                ),
                {**values, "project_id": project_id},
            )

    # -- reading (SQL) -----------------------------------------------------------------

    def project_row(self, project_id: UUID) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            found = (
                connection.execute(
                    text("SELECT * FROM projects WHERE id = :id"), {"id": project_id}
                )
                .mappings()
                .first()
            )
        return dict(found) if found is not None else None

    def member_row(self, project_id: UUID, user_id: UUID) -> dict[str, Any] | None:
        return self.member_rows(project_id).get(user_id)

    def member_rows(self, project_id: UUID) -> dict[UUID, dict[str, Any]]:
        """The membership rows of one project, keyed by user."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT * FROM project_members WHERE project_id = :id"),
                {"id": project_id},
            ).mappings()
            return {row["user_id"]: dict(row) for row in rows}

    def table_count(self, table: str) -> int:
        with self.engine.connect() as connection:
            return connection.execute(
                text(f"SELECT count(*) FROM {table}")
            ).scalar_one()

    def snapshot(self) -> dict[str, list[tuple]]:
        """Every row of the project tables, to prove a refused call changed nothing."""
        with self.engine.connect() as connection:
            return {
                table: [
                    tuple(row)
                    for row in connection.execute(
                        text(f"SELECT * FROM {table} ORDER BY 1, 2")
                    )
                ]
                for table in ("projects", "project_members", "project_task_stops")
            }

    # -- audit ------------------------------------------------------------------------

    def audit(self) -> list[tuple[str, str, str]]:
        """``(action, decision, reason)`` of every recorded event, in order."""
        return [
            (event.action, str(event.decision), str(event.reason))
            for event in self.sink.events
        ]
