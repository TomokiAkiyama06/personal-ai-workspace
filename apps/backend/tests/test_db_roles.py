"""``paw_backend.db_roles.grant_app_privileges``: rendered SQL and real PostgreSQL.

The SQL tests drive a real Alembic ``Operations`` in offline mode (no mocks).
The PostgreSQL tests (skipped unless ``PAW_TEST_DATABASE_URL`` is set) create
NON-superuser roles with per-run names and prove what the application role can
and cannot do with a table a "migration" created.
"""

import io
import os
import unittest
import uuid

import psycopg.errors
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from paw_backend.db import Database
from paw_backend.db_roles import (
    AppRoleNotFoundError,
    configured_app_role,
    grant_app_privileges,
    validate_role_name,
)

from .support import make_settings, paw_environment

TEST_DATABASE_URL = os.environ.get("PAW_TEST_DATABASE_URL")
ROLE = "PAW_APP_DATABASE_ROLE"
MIGRATION_URL = "PAW_MIGRATION_DATABASE_URL"
LOGGER = "paw_backend.db_roles"
_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_grants_app_{_RUN}"
OTHER_ROLE = f"paw_grants_other_{_RUN}"
TABLE = f"paw_grants_t_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-grants"


def render(table="widgets", environment=None, **privileges) -> str:
    """The SQL ``grant_app_privileges`` emits, using a real offline Operations."""
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with paw_environment(**(environment or {})):
        grant_app_privileges(Operations(context), table, **privileges)
    return output.getvalue()


def statements(sql: str) -> list[str]:
    return [line.rstrip(";") for line in sql.splitlines() if line.strip()]


class RenderedSqlTest(unittest.TestCase):
    def test_unset_role_grants_nothing_and_is_not_an_error(self):
        for environment in ({}, {ROLE: ""}):
            with self.subTest(environment=environment):
                with self.assertNoLogs(LOGGER, level="DEBUG"):
                    sql = render(insert=True, environment=environment)
                # Nothing is granted; PUBLIC is still stripped (a no-op on a new table).
                self.assertEqual(statements(sql), ["REVOKE ALL ON widgets FROM PUBLIC"])

    def test_the_default_is_select_only_after_revoking_public(self):
        sql = render(environment={ROLE: "paw_app"})
        self.assertEqual(
            statements(sql),
            [
                "REVOKE ALL ON widgets FROM PUBLIC",
                'GRANT SELECT ON widgets TO "paw_app"',
            ],
        )

    def test_exactly_the_requested_privileges_in_one_canonical_order(self):
        cases = {
            ("select", "insert"): "INSERT, SELECT",
            ("insert",): "INSERT",
            ("select", "update"): "SELECT, UPDATE",
            ("select", "delete"): "DELETE, SELECT",
            ("select", "insert", "update", "delete"): "DELETE, INSERT, SELECT, UPDATE",
        }
        for wanted, expected in cases.items():
            with self.subTest(wanted=wanted):
                flags = {
                    name: name in wanted
                    for name in ("select", "insert", "update", "delete")
                }
                sql = render(environment={ROLE: "paw_app"}, **flags)
                self.assertEqual(
                    statements(sql)[1], f'GRANT {expected} ON widgets TO "paw_app"'
                )

    def test_delete_and_truncate_are_never_granted_unless_asked(self):
        for flags in (
            {},
            {"insert": True},
            {"update": True},
            {"update_columns": ["a"]},
        ):
            with self.subTest(flags=flags):
                sql = render(environment={ROLE: "paw_app"}, **flags)
                self.assertNotIn("DELETE", sql)
                self.assertNotIn("TRUNCATE", sql)
        self.assertIn("DELETE", render(environment={ROLE: "paw_app"}, delete=True))
        # TRUNCATE, ALTER, DROP, GRANT OPTION: there is no way to ask for them.
        every = render(
            environment={ROLE: "paw_app"},
            insert=True,
            update=True,
            delete=True,
        )
        for never in ("TRUNCATE", "ALTER", "DROP", "GRANT OPTION", "ALL PRIVILEGES"):
            self.assertNotIn(never, every)

    def test_column_level_update(self):
        sql = render(
            environment={ROLE: "paw_app"}, update_columns=("status", "updated_at")
        )
        self.assertEqual(
            statements(sql),
            [
                "REVOKE ALL ON widgets FROM PUBLIC",
                'GRANT SELECT ON widgets TO "paw_app"',
                'GRANT UPDATE (status, updated_at) ON widgets TO "paw_app"',
            ],
        )

    def test_inconsistent_or_empty_requests_are_refused(self):
        environment = {ROLE: "paw_app"}
        with self.assertRaises(ValueError):
            render(environment=environment, update=True, update_columns=["a"])
        with self.assertRaises(ValueError):
            render(environment=environment, update_columns=[])
        with self.assertRaises(ValueError):
            render(environment=environment, select=False)
        for bad in ("status", b"status"):
            with self.assertRaises(TypeError):
                render(environment=environment, update_columns=bad)

    def test_hostile_role_names_are_rejected_before_any_sql(self):
        for role in (
            "public",
            "PUBLIC",
            "Public",
            "postgres",
            "pg_write_all_data",
            "PG_monitor",
            "none",
            "current_user",
            "session_user",
            'a"b',
            'x"; DROP TABLE audit_events; --',
            "a b",
            "a;b",
            "1abc",
            "a-b",
            "x" * 64,
            "ünï",
            "a\nb",
        ):
            with self.subTest(role=role):
                with self.assertRaises(ValueError) as caught:
                    render(environment={ROLE: role})
                self.assertNotIn(role, str(caught.exception))
                with self.assertRaises(ValueError):
                    validate_role_name(role)
                with paw_environment(**{ROLE: role}):
                    with self.assertRaises(ValueError):
                        configured_app_role()

    def test_hostile_table_and_column_names_are_rejected(self):
        environment = {ROLE: "paw_app"}
        for name in (
            "",
            "a b",
            'a"b',
            "a;b",
            "a.b",
            "1a",
            "x; DROP TABLE y",
            "x" * 64,
            None,
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    render(table=name, environment=environment)
                with self.assertRaises(ValueError):
                    render(environment=environment, update_columns=[name])

    def test_identifiers_are_quoted_the_way_postgresql_needs(self):
        sql = render(
            table="Widgets", environment={ROLE: "PawApp"}, update_columns=["Note"]
        )
        self.assertIn('REVOKE ALL ON "Widgets" FROM PUBLIC', sql)
        self.assertIn('GRANT SELECT ON "Widgets" TO "PawApp"', sql)
        self.assertIn('GRANT UPDATE ("Note") ON "Widgets" TO "PawApp"', sql)
        # A reserved word as a table name is quoted, not interpreted.
        self.assertIn('ON "user"', render(table="user", environment={ROLE: "paw_app"}))

    def test_a_split_configuration_without_a_role_is_warned_about(self):
        secret = "postgresql://owner:0wner-pw@db.internal/paw"
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            render(environment={MIGRATION_URL: secret})
        (line,) = logs.output
        self.assertIn(ROLE, line)
        self.assertIn("widgets", line)
        self.assertNotIn("0wner-pw", line)
        self.assertNotIn("postgresql://", line)

    def test_no_warning_when_the_role_is_set_or_there_is_no_split(self):
        with self.assertNoLogs(LOGGER, level="WARNING"):
            render(environment={MIGRATION_URL: "postgresql://o@h/d", ROLE: "paw_app"})
            render(environment={})

    def test_it_returns_the_granted_role(self):
        output = io.StringIO()
        context = MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
        )
        with paw_environment(**{ROLE: "paw_app"}):
            self.assertEqual(
                grant_app_privileges(Operations(context), "widgets"), "paw_app"
            )
        with paw_environment():
            self.assertIsNone(grant_app_privileges(Operations(context), "widgets"))


@unittest.skipUnless(TEST_DATABASE_URL, "PAW_TEST_DATABASE_URL is not set")
class GrantsOnPostgresTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(self.owner.dispose)
        self.addAsyncCleanup(self.cleanup)
        await self.cleanup()
        for role in (APP_ROLE, OTHER_ROLE):
            await self.owner_sql(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                f"PASSWORD '{ROLE_PASSWORD}'"
            )
        self.app = self.connect_as(APP_ROLE)
        self.other = self.connect_as(OTHER_ROLE)

    def connect_as(self, role: str) -> Database:
        url = make_url(TEST_DATABASE_URL).set(username=role, password=ROLE_PASSWORD)
        database = Database(
            make_settings(database_url=url.render_as_string(hide_password=False))
        )
        self.addAsyncCleanup(database.dispose)
        return database

    async def owner_sql(self, sql: str):
        async with self.owner.session() as session:
            result = await session.execute(text(sql))
            value = result.scalar() if result.returns_rows else None
            await session.commit()
        return value

    async def cleanup(self):
        await self.owner_sql(f"DROP TABLE IF EXISTS {TABLE}")
        for role in (APP_ROLE, OTHER_ROLE):
            if await self.owner_sql(
                f"SELECT count(*) FROM pg_roles WHERE rolname = '{role}'"
            ):
                await self.owner_sql(f"DROP OWNED BY {role}")
                await self.owner_sql(f"DROP ROLE {role}")

    async def migrate(self, environment=None, *, create=True, **privileges):
        """Run a "migration" (create the table, grant) in one transaction."""

        def run(connection):
            operations = Operations(MigrationContext.configure(connection))
            with paw_environment(
                **({ROLE: APP_ROLE} if environment is None else environment)
            ):
                if create:
                    operations.create_table(
                        TABLE,
                        sa.Column("id", sa.Integer(), primary_key=True),
                        sa.Column("name", sa.Text()),
                        sa.Column("note", sa.Text()),
                    )
                grant_app_privileges(operations, TABLE, **privileges)

        async with self.owner.engine.begin() as connection:  # rolls back on error
            await connection.run_sync(run)

    async def refused(self, database: Database, sql: str) -> DBAPIError:
        async with database.session() as session:
            with self.assertRaises(DBAPIError) as caught:
                await session.execute(text(sql))
                await session.commit()
        return caught.exception

    async def allowed(self, database: Database, sql: str):
        async with database.session() as session:
            await session.execute(text(sql))
            await session.commit()

    def assertPrivilegeError(self, error: DBAPIError):
        self.assertIsInstance(error.orig, psycopg.errors.InsufficientPrivilege)

    async def test_select_and_insert_only_cannot_update_or_delete(self):
        await self.migrate(select=True, insert=True)
        await self.allowed(self.app, f"INSERT INTO {TABLE} (id, name) VALUES (1, 'a')")
        async with self.app.session() as session:
            rows = (await session.execute(text(f"SELECT id FROM {TABLE}"))).all()
        self.assertEqual([tuple(row) for row in rows], [(1,)])
        for sql in (
            f"UPDATE {TABLE} SET name = 'b'",
            f"DELETE FROM {TABLE}",
            f"TRUNCATE {TABLE}",
            f"ALTER TABLE {TABLE} ADD COLUMN extra text",
            f"ALTER TABLE {TABLE} DISABLE TRIGGER ALL",
            f"DROP TABLE {TABLE}",
            f"GRANT DELETE ON {TABLE} TO {APP_ROLE}",
        ):
            with self.subTest(sql=sql):
                if sql.startswith("GRANT"):
                    # PostgreSQL only warns and grants nothing without a grant option.
                    await self.allowed(self.app, sql)
                    await self.refused(self.app, f"DELETE FROM {TABLE}")
                else:
                    self.assertPrivilegeError(await self.refused(self.app, sql))

    async def test_select_only_cannot_insert(self):
        await self.migrate()  # the default: select only
        async with self.app.session() as session:
            self.assertEqual(
                (await session.execute(text(f"SELECT count(*) FROM {TABLE}"))).scalar(),
                0,
            )
        error = await self.refused(self.app, f"INSERT INTO {TABLE} (id) VALUES (1)")
        self.assertPrivilegeError(error)

    async def test_column_level_update_works_only_for_the_named_columns(self):
        await self.migrate(update_columns=("name",))
        await self.owner_sql(
            f"INSERT INTO {TABLE} (id, name, note) VALUES (1, 'a', 'x')"
        )
        await self.allowed(self.app, f"UPDATE {TABLE} SET name = 'b' WHERE id = 1")
        self.assertEqual(await self.owner_sql(f"SELECT name FROM {TABLE}"), "b")
        for sql in (
            f"UPDATE {TABLE} SET note = 'y' WHERE id = 1",
            f"UPDATE {TABLE} SET name = 'c', note = 'y' WHERE id = 1",
            f"UPDATE {TABLE} SET id = 2",
            f"DELETE FROM {TABLE}",
            f"INSERT INTO {TABLE} (id) VALUES (3)",
        ):
            with self.subTest(sql=sql):
                self.assertPrivilegeError(await self.refused(self.app, sql))
        self.assertEqual(await self.owner_sql(f"SELECT note FROM {TABLE}"), "x")

    async def test_delete_is_granted_only_when_asked_and_truncate_never(self):
        await self.migrate(select=True, delete=True)
        await self.owner_sql(f"INSERT INTO {TABLE} (id) VALUES (1)")
        await self.allowed(self.app, f"DELETE FROM {TABLE}")
        self.assertPrivilegeError(await self.refused(self.app, f"TRUNCATE {TABLE}"))

    async def test_public_and_other_roles_get_nothing(self):
        def create_and_open_to_public(connection):
            connection.execute(text(f"CREATE TABLE {TABLE} (id integer)"))
            connection.execute(text(f"GRANT SELECT ON {TABLE} TO PUBLIC"))

        async with self.owner.engine.begin() as connection:
            await connection.run_sync(create_and_open_to_public)
        await self.allowed(
            self.other, f"SELECT count(*) FROM {TABLE}"
        )  # PUBLIC could read it ...
        await self.migrate(create=False, select=True)  # ... until the helper ran
        self.assertPrivilegeError(
            await self.refused(self.other, f"SELECT count(*) FROM {TABLE}")
        )
        async with self.app.session() as session:  # the application role can
            await session.execute(text(f"SELECT count(*) FROM {TABLE}"))

    async def test_an_unset_role_grants_nothing_and_raises_nothing(self):
        await self.migrate(environment={}, insert=True)
        self.assertEqual(
            await self.owner_sql(f"SELECT to_regclass('{TABLE}')::text"), TABLE
        )
        self.assertPrivilegeError(
            await self.refused(self.app, f"SELECT count(*) FROM {TABLE}")
        )

    async def test_a_missing_role_fails_loudly_and_leaves_no_table_behind(self):
        with self.assertRaises(AppRoleNotFoundError) as caught:
            await self.migrate(environment={ROLE: f"paw_grants_missing_{_RUN}"})
        self.assertIn("does not exist", str(caught.exception))
        self.assertIsNone(await self.owner_sql(f"SELECT to_regclass('{TABLE}')"))

    async def test_hostile_role_names_are_rejected_and_nothing_is_granted(self):
        await self.migrate(environment={}, create=True)  # table without grants
        for role in (
            "public",
            "PUBLIC",
            "postgres",
            "pg_monitor",
            'x"; DROP TABLE y; --',
        ):
            with self.subTest(role=role):
                with self.assertRaises(ValueError):
                    await self.migrate(environment={ROLE: role}, create=False)
        acl = await self.owner_sql(
            f"SELECT relacl::text FROM pg_class WHERE oid = '{TABLE}'::regclass"
        )
        owner = await self.owner_sql("SELECT current_user")
        grantees = {
            entry.split("=")[0] for entry in (acl or "").strip("{}").split(",") if entry
        }
        # No PUBLIC entry (an empty grantee) and nobody but the owner.
        self.assertLessEqual(grantees, {owner})
        self.assertPrivilegeError(
            await self.refused(self.other, f"SELECT count(*) FROM {TABLE}")
        )


if __name__ == "__main__":
    unittest.main()
