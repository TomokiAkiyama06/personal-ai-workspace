"""The repository tables in the split-role deployment (real PostgreSQL).

The migrations run as the owner of the schema and the backend connects as a
NON-superuser application role (``PAW_APP_DATABASE_ROLE``). These tests migrate the
test database that way, then

* run the service test classes of this issue as that role, so every statement
  ``RepositoryService`` executes is proven to work with exactly the privileges
  revision 0027 grants, and
* check that nothing else is allowed (rewriting an id, a name, a path or an owner,
  truncating, changing the schema, and writing to ``users`` or ``projects``).

Role names are unique per run and dropped afterwards; the test user must be allowed
to create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import inspect
import unittest
import uuid
from typing import Any

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from paw_backend.db import Database

from . import (
    test_repositories_accounts,
    test_repositories_concurrency,
    test_repositories_min_uid,
    test_repositories_path_length,
    test_repositories_scope_roots,
    test_repositories_service_checkout,
    test_repositories_service_manage,
    test_repositories_service_register,
    test_repositories_unregister_race,
)
from .repositories_support import PostgresRepositoryTestCase
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate, new_database, requires_postgres

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_repos_app_{_RUN}"
OTHER_ROLE = f"paw_repos_other_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-repositories"

# table -> (table-level privileges, columns the application may UPDATE). The exact
# copy of the choices (and their reasons) in migration 0027.
EXPECTED = {
    "repositories": (
        {"SELECT", "INSERT", "DELETE"},
        {"default_branch", "acl_allowed", "updated_at"},
    ),
    "repository_remotes": ({"SELECT", "INSERT", "DELETE"}, set()),
    "repository_checkouts": (
        {"SELECT", "INSERT", "DELETE"},
        {"state", "updated_at", "root_device", "root_inode"},
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
    """Mixed into a ``PostgresRepositoryTestCase``: services connect as the app role.

    Seeding and cleaning still use the owner engine of the base class.
    """

    def service_database(self) -> Database:
        database = role_database(APP_ROLE)
        return database

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.database = role_database(APP_ROLE)
        self.addAsyncCleanup(self.database.dispose)


def _database_test_classes(module):
    for name, case in vars(module).items():
        if (
            inspect.isclass(case)
            and issubclass(case, PostgresRepositoryTestCase)
            and case.__module__ == module.__name__
            and unittest.TestLoader().getTestCaseNames(case)
        ):
            yield name, case


# The service test classes, unchanged, but every statement runs as the unprivileged
# role (one derived class per original class).
for _module in (
    test_repositories_service_register,
    test_repositories_service_checkout,
    test_repositories_service_manage,
    test_repositories_concurrency,
    test_repositories_unregister_race,
    test_repositories_min_uid,
    test_repositories_path_length,
    test_repositories_scope_roots,
    test_repositories_accounts,
):
    _prefix = _module.__name__.removeprefix("tests.test_repositories_")
    for _name, _case in _database_test_classes(_module):
        _derived = f"{_prefix.title().replace('_', '')}{_name}AsAppRole"
        globals()[_derived] = type(
            _derived, (AsAppRole, _case), {"__module__": __name__}
        )
del _module, _prefix, _name, _case, _derived


@requires_postgres
class AppRolePrivilegesTest(PostgresRepositoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = role_database(APP_ROLE)
        self.other = role_database(OTHER_ROLE)
        self.addAsyncCleanup(self.app.dispose)
        self.addAsyncCleanup(self.other.dispose)
        self.project_id = self.seed_project()
        self.user = self.seed_user()
        self.repository = self.seed_repository(
            self.project_id, remotes=("https://github.com/acme/tool",)
        )
        self.checkout = self.seed_checkout(
            self.repository, self.project_id, self.user, "/home/x/tool"
        )

    def owner_scalar(self, sql: str, **parameters: Any) -> Any:
        with self.engine.connect() as connection:
            return connection.execute(text(sql), parameters).scalar()

    def owner_rows(self, sql: str) -> list[tuple]:
        with self.engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(sql))]

    def snapshot(self):
        return (
            self.owner_rows("SELECT * FROM repositories ORDER BY id"),
            self.owner_rows("SELECT * FROM repository_remotes ORDER BY url"),
            self.owner_rows("SELECT * FROM repository_checkouts ORDER BY id"),
            self.owner_rows("SELECT id, status, name FROM projects ORDER BY id"),
            self.owner_rows("SELECT id, status, system_role FROM users ORDER BY id"),
        )

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
        self.assertGreaterEqual(len(derived), 15, derived)
        self.assertIn("ServiceRegisterRegisterExistingTestAsAppRole", derived)
        self.assertIn("ServiceRegisterCloneFromGitHubTestAsAppRole", derived)
        self.assertIn("ServiceCheckoutCreateCheckoutTestAsAppRole", derived)
        self.assertIn("ServiceManagePurgeTestAsAppRole", derived)
        self.assertIn("ConcurrencyRowLockTestAsAppRole", derived)
        self.assertIn("AccountsLoginNameAccountDirectoryTestAsAppRole", derived)
        self.assertIn(
            "UnregisterRaceUnregisterDuringCreateGitHubTestAsAppRole", derived
        )
        self.assertIn("MinUidMinimumUidOnTheServicePathTestAsAppRole", derived)
        self.assertIn("ScopeRootsReplacedRootTestAsAppRole", derived)
        self.assertIn("PathLengthGeneratedPathLengthTestAsAppRole", derived)

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

    async def test_the_expectations_cover_every_repository_table(self):
        tables = {
            row[0]
            for row in self.owner_rows(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                " AND tablename LIKE 'repositor%'"
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

    async def test_a_role_without_grants_reaches_no_repository_table(self):
        for table in EXPECTED:
            with self.subTest(table=table):
                await self.refused(self.other, f"SELECT count(*) FROM {table}")
                await self.refused(self.other, f"DELETE FROM {table}")

    async def test_the_app_role_cannot_rewrite_identity_truncate_or_alter(self):
        before = self.snapshot()
        forbidden = [
            # A repository never changes its project, name (a rename would leave the
            # directory behind), source or creator ...
            "UPDATE repositories SET id = gen_random_uuid()",
            "UPDATE repositories SET project_id = gen_random_uuid()",
            "UPDATE repositories SET name = 'renamed'",
            "UPDATE repositories SET source = 'new_local'",
            "UPDATE repositories SET created_by = NULL",
            "UPDATE repositories SET created_at = now()",
            # ... a remote is never rewritten (deleted and inserted instead) ...
            "UPDATE repository_remotes SET url = 'https://github.com/x/y'",
            "UPDATE repository_remotes SET repository_id = gen_random_uuid()",
            "UPDATE repository_remotes SET project_id = gen_random_uuid()",
            # ... and a checkout never changes its owner, repository or directory.
            "UPDATE repository_checkouts SET user_id = gen_random_uuid()",
            "UPDATE repository_checkouts SET path = '/home/other/x'",
            "UPDATE repository_checkouts SET repository_id = gen_random_uuid()",
            "UPDATE repository_checkouts SET project_id = gen_random_uuid()",
            "UPDATE repository_checkouts SET created_at = now()",
            "UPDATE repository_checkouts SET id = gen_random_uuid()",
            "TRUNCATE repositories CASCADE",
            "TRUNCATE repository_remotes",
            "TRUNCATE repository_checkouts",
            # The schema belongs to the migration role.
            "ALTER TABLE repositories ADD COLUMN extra text",
            "ALTER TABLE repositories DROP CONSTRAINT ck_repositories_name_valid",
            "DROP INDEX uq_repositories_project_id_lower_name",
            "DROP TABLE repository_checkouts",
            "DROP TABLE repository_remotes",
            "DROP TABLE repositories CASCADE",
            # The project and the user tables are read (and locked), never written,
            # by the repository module.
            "UPDATE projects SET created_by = NULL",
            "DELETE FROM projects",
            "UPDATE users SET status = 'deleted'",
            "DELETE FROM users",
        ]
        for sql in forbidden:
            with self.subTest(sql=sql):
                await self.refused(self.app, sql)
        self.assertEqual(self.snapshot(), before)

    async def test_the_app_role_can_do_what_the_service_does(self):
        second_project = self.seed_project(name="Beta")
        async with self.app.session() as session, session.begin():
            # Read the state the authorization needs, lock the project.
            await session.execute(
                text("SELECT id FROM projects WHERE id = :p FOR UPDATE"),
                {"p": self.project_id},
            )
            await session.execute(
                text("SELECT id FROM projects WHERE id = :p FOR SHARE"),
                {"p": self.project_id},
            )
            await session.execute(text("SELECT count(*) FROM project_members"))
            await session.execute(text("SELECT login_name FROM users"))
            # register / clone: repository, remote, pending checkout ...
            repository = (
                await session.execute(
                    text(
                        "INSERT INTO repositories (project_id, name, default_branch,"
                        " source, acl_allowed, created_by, created_at, updated_at)"
                        " VALUES (:p, 'new', 'main', 'github_clone', NULL, :u, now(),"
                        " now()) RETURNING id"
                    ),
                    {"p": second_project, "u": self.user},
                )
            ).scalar_one()
            await session.execute(
                text(
                    "INSERT INTO repository_remotes (repository_id, url, project_id,"
                    " created_at) VALUES (:r, 'https://github.com/a/b', :p, now())"
                ),
                {"r": repository, "p": second_project},
            )
            checkout = (
                await session.execute(
                    text(
                        "INSERT INTO repository_checkouts (repository_id, project_id,"
                        " user_id, path, state, created_at, updated_at) VALUES (:r, :p,"
                        " :u, '/home/x/new', 'pending', now(), now()) RETURNING id"
                    ),
                    {"r": repository, "p": second_project, "u": self.user},
                )
            ).scalar_one()
            # ... then the branch, the ACL and the state.
            await session.execute(
                text(
                    "UPDATE repositories SET default_branch = 'develop',"
                    " updated_at = now() WHERE id = :r"
                ),
                {"r": repository},
            )
            await session.execute(
                text(
                    "UPDATE repositories SET acl_allowed = ARRAY['read'],"
                    " updated_at = now() WHERE id = :r"
                ),
                {"r": repository},
            )
            await session.execute(
                text(
                    "UPDATE repository_checkouts SET state = 'ready',"
                    " root_device = 7, root_inode = 9, updated_at = now()"
                    " WHERE id = :c AND state = 'pending'"
                ),
                {"c": checkout},
            )
            await session.execute(
                text(
                    "SELECT * FROM repository_checkouts WHERE user_id = :u FOR UPDATE"
                ),
                {"u": self.user},
            )
            # remove: remote, checkout, repository; purge of a Deleted project.
            await session.execute(
                text(
                    "DELETE FROM repository_remotes WHERE repository_id = :r"
                    " AND url = 'https://github.com/a/b'"
                ),
                {"r": repository},
            )
            await session.execute(
                text("DELETE FROM repository_checkouts WHERE id = :c"), {"c": checkout}
            )
            await session.execute(
                text("DELETE FROM repositories WHERE id = :r"), {"r": repository}
            )
            await session.execute(
                text(
                    "DELETE FROM repositories WHERE project_id IN"
                    " (SELECT id FROM projects WHERE id = :p AND status = 'deleted')"
                ),
                {"p": self.project_id},
            )
        self.assertEqual(
            self.owner_scalar("SELECT count(*) FROM repositories WHERE name = 'new'"), 0
        )
        self.assertEqual(
            self.owner_scalar(
                "SELECT count(*) FROM repositories WHERE id = :r", r=self.repository
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
