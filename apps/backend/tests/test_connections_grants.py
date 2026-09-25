"""The connection tables in the split-role deployment (real PostgreSQL).

The migrations run as the owner of the schema and the backend connects as a
NON-superuser application role (``PAW_APP_DATABASE_ROLE``). These tests migrate the
test database that way, then

* run the service test classes of this issue as that role, so every statement
  ``ConnectionService`` executes (the row locks of the admission included) is proven
  to work with exactly the privileges revision 0030 grants, and
* check that nothing else is allowed (rewriting an identity column, a settled record's
  owner or start, deleting history, truncating, changing the schema, writing to
  ``users`` or ``tasks``).

Role names are unique per run and dropped afterwards; the test user must be allowed to
create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
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

from paw_backend.authz import Authorizer
from paw_backend.connections import ConnectionService
from paw_backend.db import Database

from . import (
    test_connections_admin,
    test_connections_concurrency,
    test_connections_execute,
    test_connections_quota_admin,
    test_connections_quota_enforcement,
)
from .connections_support import PostgresConnectionTestCase
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate, new_database, requires_postgres

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_connections_app_{_RUN}"
OTHER_ROLE = f"paw_connections_other_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-connections"

# table -> (table-level privileges, columns the application may UPDATE). The exact
# copy of the choices (and their reasons) in migration 0030.
EXPECTED = {
    "shared_connections": (
        {"SELECT", "INSERT", "DELETE"},
        {"secret_handle", "status", "enabled", "checked_at", "updated_at"},
    ),
    "connection_quotas": (
        {"SELECT", "INSERT", "DELETE"},
        {"limit_value", "updated_at"},
    ),
    # History: written by the admission, settled once, never deleted.
    "connection_usage": (
        {"SELECT", "INSERT"},
        {
            "status",
            "failure_code",
            "input_tokens",
            "output_tokens",
            "finished_at",
            "duration_ms",
        },
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
    """Mixed into a ``PostgresConnectionTestCase``: its services connect as the app
    role. Seeding and reading still use the owner engine of the base class."""

    def new_service(
        self, *, authorizer_sink: Any = None, audit_sink: Any = None, **options: Any
    ) -> ConnectionService:
        database = role_database(APP_ROLE)
        self.addAsyncCleanup(database.dispose)
        return ConnectionService(
            database,
            Authorizer(authorizer_sink or self.sink),
            audit_sink or self.sink,
            self.adapters,
            self.resolver,
            **options,
        )


def _database_test_classes(module):
    for name, case in vars(module).items():
        if (
            inspect.isclass(case)
            and issubclass(case, PostgresConnectionTestCase)
            and case.__module__ == module.__name__
            and unittest.TestLoader().getTestCaseNames(case)
        ):
            yield name, case


# The service test classes, unchanged, but every statement of the service runs as the
# unprivileged role: one derived class per original class.
for _module in (
    test_connections_admin,
    test_connections_quota_admin,
    test_connections_execute,
    test_connections_quota_enforcement,
    test_connections_concurrency,
):
    _prefix = _module.__name__.removeprefix("tests.test_connections_")
    for _name, _case in _database_test_classes(_module):
        _derived = f"{_prefix.title().replace('_', '')}{_name}AsAppRole"
        globals()[_derived] = type(
            _derived, (AsAppRole, _case), {"__module__": __name__}
        )
del _module, _prefix, _name, _case, _derived


@requires_postgres
class AppRolePrivilegesTest(PostgresConnectionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = role_database(APP_ROLE)
        self.other = role_database(OTHER_ROLE)
        self.addAsyncCleanup(self.app.dispose)
        self.addAsyncCleanup(self.other.dispose)

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
        for expected in (
            "AdminConnectTestAsAppRole",
            "QuotaAdminSetQuotaTestAsAppRole",
            "ExecuteHappyPathTestAsAppRole",
            "ExecuteSecretIsolationTestAsAppRole",
            "QuotaEnforcementRunningTaskTestAsAppRole",
            "ConcurrencyQuotaIsExactUnderConcurrencyTestAsAppRole",
            "ConcurrencyLockOrderingTestAsAppRole",
        ):
            self.assertIn(expected, derived)

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

    async def test_the_expectations_cover_every_connection_table(self):
        tables = {
            row["tablename"]
            for row in self.rows(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                " AND tablename IN ('shared_connections', 'connection_quotas',"
                " 'connection_usage')"
            )
        }
        self.assertEqual(tables, set(EXPECTED))

    async def test_the_app_role_holds_exactly_the_least_privileges(self):
        for table, (privileges, update_columns) in EXPECTED.items():
            with self.subTest(table=table):
                for privilege in ALL_PRIVILEGES:
                    granted = self.scalar(
                        "SELECT has_table_privilege(:r, :t, :p)",
                        r=APP_ROLE,
                        t=table,
                        p=privilege,
                    )
                    self.assertEqual(granted, privilege in privileges, privilege)
                columns = [
                    row["column_name"]
                    for row in self.rows(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :t",
                        t=table,
                    )
                ]
                updatable = {
                    column
                    for column in columns
                    if self.scalar(
                        "SELECT has_column_privilege(:r, :t, :c, 'UPDATE')",
                        r=APP_ROLE,
                        t=table,
                        c=column,
                    )
                }
                self.assertEqual(updatable, update_columns)

    async def test_a_role_without_grants_reaches_no_connection_table(self):
        for table in EXPECTED:
            with self.subTest(table=table):
                await self.refused(self.other, f"SELECT count(*) FROM {table}")
                await self.refused(self.other, f"DELETE FROM {table}")

    async def test_the_app_role_cannot_rewrite_identity_delete_truncate_or_alter(self):
        self.seed_connection()
        self.seed_quota(self.user, 5)
        task = self.seed_task(self.user)
        self.seed_usage(self.user, task)
        before = (
            self.rows("SELECT * FROM shared_connections"),
            self.rows("SELECT * FROM connection_quotas"),
            self.rows("SELECT * FROM connection_usage"),
        )

        forbidden = [
            # A connection's identity, kind and creation time never change ...
            "UPDATE shared_connections SET id = gen_random_uuid()",
            "UPDATE shared_connections SET kind = 'claude'",
            "UPDATE shared_connections SET created_at = now()",
            "TRUNCATE shared_connections",
            # ... a quota belongs to one user, kind, metric and period ...
            "UPDATE connection_quotas SET user_id = gen_random_uuid()",
            "UPDATE connection_quotas SET kind = 'claude'",
            "UPDATE connection_quotas SET metric = 'tokens'",
            "UPDATE connection_quotas SET period = 'week'",
            "UPDATE connection_quotas SET created_at = now()",
            "TRUNCATE connection_quotas",
            # ... and a usage record is history: it is never deleted, and who, what,
            # which model and purpose, and when it started are never rewritten.
            "DELETE FROM connection_usage",
            "TRUNCATE connection_usage",
            "UPDATE connection_usage SET id = gen_random_uuid()",
            "UPDATE connection_usage SET user_id = gen_random_uuid()",
            "UPDATE connection_usage SET task_id = gen_random_uuid()",
            "UPDATE connection_usage SET project_id = gen_random_uuid()",
            "UPDATE connection_usage SET kind = 'claude'",
            "UPDATE connection_usage SET model = 'other'",
            "UPDATE connection_usage SET purpose = 'chat'",
            "UPDATE connection_usage SET started_at = now()",
            # The schema belongs to the migration role.
            "ALTER TABLE shared_connections ADD COLUMN extra text",
            "ALTER TABLE shared_connections DROP CONSTRAINT"
            " ck_shared_connections_handle_shape",
            "ALTER TABLE connection_usage DISABLE TRIGGER ALL",
            "DROP INDEX ix_connection_usage_user_id_kind_started_at",
            "DROP TABLE connection_usage",
            "DROP TABLE connection_quotas",
            "DROP TABLE shared_connections",
            # ``users`` and ``tasks`` are read (and their rows locked), never written
            # by this module.
            "UPDATE users SET status = 'deleted'",
            "UPDATE users SET system_role = 'admin'",
            "DELETE FROM users",
            "UPDATE tasks SET created_by = gen_random_uuid()",
            "UPDATE tasks SET project_id = gen_random_uuid()",
            "DELETE FROM tasks",
        ]
        for sql in forbidden:
            with self.subTest(sql=sql):
                await self.refused(self.app, sql)

        after = (
            self.rows("SELECT * FROM shared_connections"),
            self.rows("SELECT * FROM connection_quotas"),
            self.rows("SELECT * FROM connection_usage"),
        )
        self.assertEqual(after, before)

    async def test_the_app_role_can_do_what_the_service_does(self):
        # The exact statements of the admission, on the tables as the app role.
        self.seed_connection()
        self.seed_quota(self.user, 5)
        task = self.seed_task(self.user)
        async with self.app.session() as session, session.begin():
            for sql in (
                "SELECT state FROM tasks WHERE id = :t FOR SHARE",
                "SELECT id FROM shared_connections WHERE kind = 'codex' FOR SHARE",
                "SELECT metric FROM connection_quotas WHERE user_id = :u FOR UPDATE",
                "SELECT count(*) FROM connection_usage WHERE user_id = :u",
                "SELECT status FROM users WHERE id = :u",
            ):
                await session.execute(text(sql), {"t": task, "u": self.user})
            await session.execute(
                text(
                    "INSERT INTO connection_usage (user_id, task_id, project_id,"
                    " kind, model, purpose, status, started_at) VALUES (:u, :t,"
                    " gen_random_uuid(), 'codex', 'm', 'coding', 'in_flight', now())"
                ),
                {"t": task, "u": self.user},
            )
            await session.execute(
                text(
                    "UPDATE connection_usage SET status = 'succeeded',"
                    " failure_code = NULL, input_tokens = 1, output_tokens = 2,"
                    " finished_at = now(), duration_ms = 0"
                )
            )
        self.assertEqual(self.usage_rows()[0]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
