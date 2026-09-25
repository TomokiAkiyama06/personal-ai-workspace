"""The administrator's project list in the split-role deployment (Issue #84).

The issue adds **no table and no migration**, so it needs no new privilege. These
tests prove it, on a real PostgreSQL with NON-superuser roles:

* every test class of ``test_projects_admin_list`` runs unchanged as the
  application role of migration ``0026`` (``PAW_APP_DATABASE_ROLE``): the list,
  its paging and its Audit row work with exactly the privileges that role already
  has, and the role still has nothing more than before on the project tables;
* the listing works for a role that holds **only ``SELECT`` on five columns of
  ``projects``** (``id``, ``name``, ``status``, ``created_at``,
  ``deletion_scheduled_at``) and nothing else: so the statement never reads the
  description, the creator, the members or any other table, and never writes.

Role names are unique per run and dropped afterwards; the test user must be
allowed to create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import inspect
import unittest
import uuid
from datetime import timedelta
from typing import Any

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from paw_backend.authz import Authorizer, SystemRole
from paw_backend.db import Database
from paw_backend.projects import (
    ProjectPermissionDeniedError,
    ProjectService,
    ProjectStatus,
)

from . import test_projects_admin_list
from .projects_support import T0, FakeClock, requires_postgres
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate, new_database
from .test_projects_admin_list import AdminListTestCase

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_padmin_app_{_RUN}"
COLUMN_ROLE = f"paw_padmin_cols_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-padmin"

# The only columns the list reads, and so the only ones the role below may read.
LIST_COLUMNS = ("id", "name", "status", "created_at", "deletion_scheduled_at")
ALL_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


def role_database(role: str) -> Database:
    url = make_url(TEST_DATABASE_URL).set(username=role, password=ROLE_PASSWORD)
    return Database(
        make_settings(database_url=url.render_as_string(hide_password=False))
    )


async def owner_sql(sql: str) -> None:
    database = new_database()
    try:
        async with database.engine.begin() as connection:
            await connection.execute(text(sql))
    finally:
        await database.dispose()


async def drop_roles() -> None:
    database = new_database()
    try:
        async with database.engine.begin() as connection:
            for role in (APP_ROLE, COLUMN_ROLE):
                exists = await connection.execute(
                    text("SELECT count(*) FROM pg_roles WHERE rolname = :r"),
                    {"r": role},
                )
                if exists.scalar():
                    await connection.execute(text(f"DROP OWNED BY {role}"))
                    await connection.execute(text(f"DROP ROLE {role}"))
    finally:
        await database.dispose()


def setUpModule():
    if not TEST_DATABASE_URL:
        raise unittest.SkipTest("PAW_TEST_DATABASE_URL is not set")
    asyncio.run(drop_roles())
    for role in (APP_ROLE, COLUMN_ROLE):
        asyncio.run(
            owner_sql(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                f"PASSWORD '{ROLE_PASSWORD}'"
            )
        )
    # Recreate every table with the grants of the split-role deployment.
    migrate("base", downgrade=True)
    migrate(PAW_APP_DATABASE_ROLE=APP_ROLE)
    # The second role is not a deployment role: it is the smallest possible reader,
    # to show what the list needs.
    columns = ", ".join(LIST_COLUMNS)
    asyncio.run(owner_sql(f"GRANT SELECT ({columns}) ON projects TO {COLUMN_ROLE}"))


def tearDownModule():
    if TEST_DATABASE_URL:
        asyncio.run(drop_roles())


class AsRole:
    """Mixed into an ``AdminListTestCase``: its services connect as ``ROLE``.

    Seeding, cleaning and reading back still use the owner engine of the base
    class.
    """

    ROLE = APP_ROLE

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.database = self.connect()

    def connect(self) -> Database:
        database = role_database(self.ROLE)
        self.addAsyncCleanup(database.dispose)
        return database

    def new_service(
        self, clock: FakeClock | None = None, **options: Any
    ) -> ProjectService:
        clock = clock or self.clock
        return ProjectService(
            self.connect(), Authorizer(self.sink, clock=clock), clock=clock, **options
        )


def _database_test_classes(module):
    for name, case in vars(module).items():
        if (
            inspect.isclass(case)
            and issubclass(case, AdminListTestCase)
            and case.__module__ == module.__name__
            and unittest.TestLoader().getTestCaseNames(case)
        ):
            yield name, case


# The test classes of the list, unchanged, but every statement of the service runs
# as the unprivileged application role.
for _name, _case in _database_test_classes(test_projects_admin_list):
    _derived = f"{_name}AsAppRole"
    globals()[_derived] = type(_derived, (AsRole, _case), {"__module__": __name__})
del _name, _case, _derived


@requires_postgres
class AdminListPrivilegesTest(AdminListTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = role_database(APP_ROLE)
        self.columns = role_database(COLUMN_ROLE)
        self.addAsyncCleanup(self.app.dispose)
        self.addAsyncCleanup(self.columns.dispose)

    def owner_scalar(self, sql: str, **parameters: Any) -> Any:
        with self.engine.connect() as connection:
            return connection.execute(text(sql), parameters).scalar()

    async def refused(self, database: Database, sql: str):
        async with database.session() as session:
            with self.assertRaises(DBAPIError) as caught:
                await session.execute(text(sql))
                await session.commit()
        self.assertIsInstance(
            caught.exception.orig, psycopg.errors.InsufficientPrivilege
        )

    def service_as(self, database: Database) -> ProjectService:
        return ProjectService(
            database, Authorizer(self.sink, clock=self.clock), clock=self.clock
        )

    async def test_the_derived_test_classes_exist(self):
        derived = sorted(name for name in globals() if name.endswith("AsAppRole"))
        self.assertEqual(
            derived,
            [
                "AdminListAuditInPostgresTestAsAppRole",
                "AdminListConcurrentChangesTestAsAppRole",
                "AdminListContentTestAsAppRole",
                "AdminListPagingTestAsAppRole",
            ],
        )

    async def test_the_roles_are_not_superusers(self):
        for database in (self.app, self.columns):
            async with database.engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT current_user, rolsuper, rolcreaterole, "
                            "rolbypassrls FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).one()
            self.assertIn(row[0], (APP_ROLE, COLUMN_ROLE))
            self.assertEqual(tuple(row[1:]), (False, False, False))

    async def test_the_app_role_has_no_new_privilege_on_the_project_tables(self):
        # Exactly what migration 0026 granted (test_projects_grants.py checks the
        # column lists): this issue changes none of it.
        granted = {
            table: {
                privilege
                for privilege in ALL_PRIVILEGES
                if self.owner_scalar(
                    "SELECT has_table_privilege(:r, :t, :p)",
                    r=APP_ROLE,
                    t=table,
                    p=privilege,
                )
            }
            for table in ("projects", "project_members", "project_task_stops")
        }
        self.assertEqual(
            granted,
            {
                "projects": {"SELECT", "INSERT"},
                "project_members": {"SELECT", "INSERT", "DELETE"},
                "project_task_stops": {"SELECT", "INSERT"},
            },
        )

    async def test_the_column_role_holds_select_on_five_columns_and_nothing_else(self):
        self.assertFalse(
            self.owner_scalar(
                "SELECT has_table_privilege(:r, 'projects', 'SELECT')", r=COLUMN_ROLE
            )
        )
        columns = self.owner_scalar(
            "SELECT array_agg(column_name::text ORDER BY column_name) FROM"
            " information_schema.columns WHERE table_name = 'projects'"
        )
        for column in columns:
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                granted = self.owner_scalar(
                    "SELECT has_column_privilege(:r, 'projects', :c, :p)",
                    r=COLUMN_ROLE,
                    c=column,
                    p=privilege,
                )
                self.assertEqual(
                    granted,
                    privilege == "SELECT" and column in LIST_COLUMNS,
                    (column, privilege),
                )
        for table in (
            "project_members",
            "project_task_stops",
            "users",
            "tasks",
            "audit_events",
            "memories",
        ):
            with self.subTest(table=table):
                await self.refused(self.columns, f"SELECT count(*) FROM {table}")
        for column in ("description", "created_by", "updated_at", "deleted_at"):
            with self.subTest(column=column):
                await self.refused(self.columns, f"SELECT {column} FROM projects")
        await self.refused(self.columns, "SELECT * FROM projects")
        await self.refused(self.columns, "UPDATE projects SET name = 'x'")
        await self.refused(self.columns, "DELETE FROM projects")

    async def test_the_list_needs_select_on_those_five_columns_only(self):
        project = self.seed_project(name="Visible", description="Hidden text")
        self.seed_team(project)
        pending = self.seed_project(
            ProjectStatus.PENDING_DELETION,
            name="Going",
            created_at=T0 + timedelta(minutes=1),
        )
        service = self.service_as(self.columns)
        admin = self.actor(self.seed_user(system_role="admin"), SystemRole.ADMIN)
        # Every filter and both ways of paging read the same five columns.
        for status in (None, "active", ProjectStatus.PENDING_DELETION):
            page = await service.list_all_projects(admin, status=status, limit=1)
            self.assertLessEqual(len(page.projects), 1)
        everything = await service.list_all_projects(admin)
        self.assertEqual(
            [(p.id, p.name) for p in everything.projects],
            [(pending, "Going"), (project, "Visible")],
        )
        second = await service.list_all_projects(admin, limit=1)
        third = await service.list_all_projects(
            admin, limit=1, cursor=second.next_cursor
        )
        self.assertEqual([p.id for p in third.projects], [project])

    async def test_a_denied_caller_gets_nothing_from_any_role(self):
        self.seed_project(name="Secret")
        user = self.actor(self.seed_user())
        for database in (self.app, self.columns):
            with self.assertRaises(ProjectPermissionDeniedError):
                await self.service_as(database).list_all_projects(user)


if __name__ == "__main__":
    unittest.main()
