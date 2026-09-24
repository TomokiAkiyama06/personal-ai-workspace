"""The project tables in the split-role deployment (real PostgreSQL).

The migrations run as the owner of the schema and the backend connects as a
NON-superuser application role (``PAW_APP_DATABASE_ROLE``). These tests migrate
the test database that way, then

* run the store and service test classes of this issue as that role, so every
  statement ``ProjectService`` executes is proven to work with exactly the
  privileges revision 0026 grants, and
* check that nothing else is allowed (rewriting an id, a creator or an
  invitation time, deleting a project, truncating, changing the schema, and
  writing to ``users``).

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

from paw_backend.authz import Authorizer
from paw_backend.db import Database
from paw_backend.projects import ProjectService

from . import (
    test_projects_concurrency,
    test_projects_service_access,
    test_projects_service_lifecycle,
    test_projects_service_members,
    test_projects_store,
)
from .projects_support import FakeClock, PostgresProjectTestCase
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate, new_database, requires_postgres

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_projects_app_{_RUN}"
OTHER_ROLE = f"paw_projects_other_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-projects"

# table -> (table-level privileges, columns the application may UPDATE). The
# exact copy of the choices (and their reasons) in migration 0026.
EXPECTED = {
    "projects": (
        {"SELECT", "INSERT"},
        {
            "name",
            "description",
            "status",
            "updated_at",
            "deletion_started_at",
            "deletion_scheduled_at",
            "deleted_at",
        },
    ),
    "project_members": (
        {"SELECT", "INSERT", "DELETE"},
        {"role", "status", "joined_at", "invite_expires_at"},
    ),
}
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
            for role in (APP_ROLE, OTHER_ROLE):
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
    for role in (APP_ROLE, OTHER_ROLE):
        asyncio.run(
            owner_sql(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                f"PASSWORD '{ROLE_PASSWORD}'"
            )
        )
    # Recreate every table with the grants of the split-role deployment.
    migrate("base", downgrade=True)
    migrate(PAW_APP_DATABASE_ROLE=APP_ROLE)


def tearDownModule():
    if TEST_DATABASE_URL:
        asyncio.run(drop_roles())


class AsAppRole:
    """Mixed into a ``PostgresProjectTestCase``: its services connect as the app role.

    Seeding and cleaning still use the owner engine of the base class.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.database = role_database(APP_ROLE)  # what the store tests use
        self.addAsyncCleanup(self.database.dispose)

    def new_service(
        self, clock: FakeClock | None = None, **options: Any
    ) -> ProjectService:
        clock = clock or self.clock
        database = role_database(APP_ROLE)
        self.addAsyncCleanup(database.dispose)
        return ProjectService(
            database, Authorizer(self.sink, clock=clock), clock=clock, **options
        )


def _database_test_classes(module):
    for name, case in vars(module).items():
        if (
            inspect.isclass(case)
            and issubclass(case, PostgresProjectTestCase)
            and case.__module__ == module.__name__
            and unittest.TestLoader().getTestCaseNames(case)
        ):
            yield name, case


# The store and service test classes, unchanged, but every statement runs as the
# unprivileged role. ``ScratchStore`` did the same by hand; the modules here are
# many, so the classes are derived programmatically (one per original class).
for _module in (
    test_projects_store,
    test_projects_service_access,
    test_projects_service_members,
    test_projects_service_lifecycle,
    test_projects_concurrency,
):
    _prefix = _module.__name__.removeprefix("tests.test_projects_")
    for _name, _case in _database_test_classes(_module):
        _derived = f"{_prefix.title().replace('_', '')}{_name}AsAppRole"
        globals()[_derived] = type(
            _derived, (AsAppRole, _case), {"__module__": __name__}
        )
del _module, _prefix, _name, _case, _derived


@requires_postgres
class AppRolePrivilegesTest(PostgresProjectTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = role_database(APP_ROLE)
        self.other = role_database(OTHER_ROLE)
        self.addAsyncCleanup(self.app.dispose)
        self.addAsyncCleanup(self.other.dispose)

    def owner_scalar(self, sql: str, **parameters: Any) -> Any:
        with self.engine.connect() as connection:
            return connection.execute(text(sql), parameters).scalar()

    def owner_rows(self, sql: str) -> list[tuple]:
        with self.engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(sql))]

    async def refused(self, database: Database, sql: str):
        async with database.session() as session:
            with self.assertRaises(DBAPIError) as caught:
                await session.execute(text(sql))
                await session.commit()
        self.assertIsInstance(
            caught.exception.orig, psycopg.errors.InsufficientPrivilege
        )

    async def test_the_derived_test_classes_exist(self):
        derived = [
            name
            for name in globals()
            if name.endswith("AsAppRole") and name != "AsAppRole"
        ]
        # Every database test class of the five modules has a twin.
        self.assertGreaterEqual(len(derived), 30, derived)
        self.assertIn("StoreGetProjectTestAsAppRole", derived)
        self.assertIn("ServiceMembersAcceptInviteTestAsAppRole", derived)
        self.assertIn("ServiceLifecyclePurgeTestAsAppRole", derived)

    async def test_the_service_really_runs_as_a_non_superuser_role(self):
        for database in (self.app, self.other):
            async with database.engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT current_user, rolsuper, rolcreaterole, "
                            "rolbypassrls FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).one()
            self.assertIn(row[0], (APP_ROLE, OTHER_ROLE))
            self.assertEqual(tuple(row[1:]), (False, False, False))

    async def test_the_expectations_cover_every_project_table(self):
        tables = {
            row[0]
            for row in self.owner_rows(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                " AND (tablename = 'projects' OR tablename = 'project_members')"
            )
        }
        self.assertEqual(tables, set(EXPECTED))

    async def test_the_app_role_holds_exactly_the_least_privileges(self):
        for table, (privileges, update_columns) in EXPECTED.items():
            with self.subTest(table=table):
                for privilege in ALL_PRIVILEGES:
                    granted = self.owner_scalar(
                        "SELECT has_table_privilege(:r, :t, :p)",
                        r=APP_ROLE,
                        t=table,
                        p=privilege,
                    )
                    self.assertEqual(granted, privilege in privileges, privilege)
                columns = [
                    row[0]
                    for row in self.owner_rows(
                        "SELECT column_name FROM information_schema.columns "
                        f"WHERE table_name = '{table}'"
                    )
                ]
                updatable = {
                    column
                    for column in columns
                    if self.owner_scalar(
                        "SELECT has_column_privilege(:r, :t, :c, 'UPDATE')",
                        r=APP_ROLE,
                        t=table,
                        c=column,
                    )
                }
                self.assertEqual(updatable, update_columns)

    async def test_a_role_without_grants_reaches_no_project_table(self):
        for table in EXPECTED:
            with self.subTest(table=table):
                await self.refused(self.other, f"SELECT count(*) FROM {table}")
                await self.refused(self.other, f"DELETE FROM {table}")

    async def test_the_app_role_cannot_rewrite_identity_delete_truncate_or_alter(self):
        project_id = self.seed_project()
        user = self.seed_member(project_id)
        before = self.snapshot()

        forbidden = [
            # A project is never deleted (it becomes a tombstone) ...
            "DELETE FROM projects",
            "TRUNCATE projects CASCADE",
            "TRUNCATE project_members",
            # ... and its identity, creator and creation time never change.
            "UPDATE projects SET id = gen_random_uuid()",
            "UPDATE projects SET created_by = NULL",
            "UPDATE projects SET created_at = now()",
            # A membership belongs to one project and one user and one invitation.
            "UPDATE project_members SET user_id = gen_random_uuid()",
            "UPDATE project_members SET project_id = gen_random_uuid()",
            "UPDATE project_members SET invited_at = now()",
            # The schema belongs to the migration role.
            "ALTER TABLE projects ADD COLUMN extra text",
            "ALTER TABLE projects DROP CONSTRAINT ck_projects_deletion_retention",
            "DROP INDEX ix_projects_pending_deletion",
            "DROP TABLE project_members",
            # ``users`` is read, never written, by the project module.
            "UPDATE users SET status = 'deleted'",
            "UPDATE users SET system_role = 'admin'",
            "DELETE FROM users",
        ]
        for sql in forbidden:
            with self.subTest(sql=sql):
                await self.refused(self.app, sql)

        self.assertEqual(self.snapshot(), before)
        self.assertIsNotNone(self.member_row(project_id, user))

    async def test_the_app_role_can_do_what_the_service_does(self):
        project_id = self.seed_project()
        invitee = self.seed_user()
        manager = self.seed_manager(project_id)
        async with self.app.session() as session, session.begin():
            await session.execute(
                text("SELECT id FROM projects WHERE id = :p FOR UPDATE"),
                {"p": project_id},
            )
            await session.execute(
                text("UPDATE projects SET name = 'Renamed', updated_at = now()")
            )
            await session.execute(
                text(
                    "INSERT INTO project_members (project_id, user_id, role, status,"
                    " invited_at, invite_expires_at) VALUES (:p, :u, 'viewer',"
                    " 'invited', now(), now() + interval '14 days')"
                ),
                {"p": project_id, "u": invitee},
            )
            await session.execute(
                text(
                    "UPDATE project_members SET status = 'active', joined_at = now(),"
                    " invite_expires_at = NULL, role = 'contributor'"
                    " WHERE user_id = :u"
                ),
                {"u": invitee},
            )
            selected = (
                await session.execute(text("SELECT count(*) FROM users"))
            ).scalar_one()
            await session.execute(
                text("DELETE FROM project_members WHERE user_id = :u"), {"u": manager}
            )
        self.assertGreaterEqual(selected, 2)
        self.assertEqual(self.project_row(project_id)["name"], "Renamed")
        self.assertEqual(set(self.member_rows(project_id)), {invitee})
        self.assertEqual(self.member_row(project_id, invitee)["role"], "contributor")
        deadline = self.clock.now + timedelta(days=30)
        async with self.app.session() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE projects SET status = 'pending_deletion',"
                    " deletion_started_at = :s, deletion_scheduled_at = :d,"
                    " updated_at = :s"
                ),
                {"s": self.clock.now, "d": deadline},
            )
        self.assertEqual(self.project_row(project_id)["status"], "pending_deletion")


if __name__ == "__main__":
    unittest.main()
