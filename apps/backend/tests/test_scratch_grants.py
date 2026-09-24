"""The scratch tables in the split-role deployment (real PostgreSQL).

The migrations run as the owner of the schema and the backend connects as a
NON-superuser application role (``PAW_APP_DATABASE_ROLE``). These tests migrate
the test database that way, then

* run the ``ScratchStore`` test classes as that role, so every statement the
  store executes is proven to work with exactly the privileges revision 0050
  grants, and
* check that nothing else is allowed (extending an expiry, rewriting content,
  changing keys, truncating, changing the schema).

Role names are unique per run and dropped afterwards; the test user must be
allowed to create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import unittest
import uuid
from datetime import timedelta
from typing import Any

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from paw_backend.db import Database
from paw_backend.research.scratch import ScratchStore

from . import (
    test_scratch_concurrency,
    test_scratch_purge,
    test_scratch_store_items,
    test_scratch_store_use,
)
from .scratch_support import FakeClock, PostgresScratchTestCase
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate, new_database, requires_postgres

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_scratch_app_{_RUN}"
OTHER_ROLE = f"paw_scratch_other_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-scratch"

# table -> (table-level privileges, columns the application may UPDATE). The
# exact copy of the choices (and their reasons) in migration 0050.
EXPECTED = {
    "research_scratch_items": (
        {"SELECT", "INSERT", "DELETE"},
        {"pinned", "promotion_state", "promotion_requested_at"},
    ),
    "research_scratch_leases": (
        {"SELECT", "INSERT", "DELETE"},
        {"leased_at", "expires_at"},
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
    """Mixed into a ``PostgresScratchTestCase``: its stores connect as the app role.

    Seeding and cleaning still use the owner engine of the base class.
    """

    def new_store(self, clock: FakeClock | None = None, **options: Any) -> ScratchStore:
        database = role_database(APP_ROLE)
        self.addAsyncCleanup(database.dispose)
        return ScratchStore(database, clock=clock or self.clock, **options)


# The store test classes, unchanged, but every store statement runs as the
# unprivileged role.
class AddAsAppRole(AsAppRole, test_scratch_store_items.AddTest):
    pass


class GetAsAppRole(AsAppRole, test_scratch_store_items.GetTest):
    pass


class ListItemsAsAppRole(AsAppRole, test_scratch_store_items.ListItemsTest):
    pass


class PinAsAppRole(AsAppRole, test_scratch_store_items.PinTest):
    pass


class AcquireUseAsAppRole(AsAppRole, test_scratch_store_use.AcquireUseTest):
    pass


class ReleaseUseAsAppRole(AsAppRole, test_scratch_store_use.ReleaseUseTest):
    pass


class RequestPromotionAsAppRole(AsAppRole, test_scratch_store_use.RequestPromotionTest):
    pass


class ResolvePromotionAsAppRole(AsAppRole, test_scratch_store_use.ResolvePromotionTest):
    pass


class PurgeCountsAsAppRole(AsAppRole, test_scratch_purge.PurgeCountsTest):
    pass


class PurgeBatchAsAppRole(AsAppRole, test_scratch_purge.PurgeBatchTest):
    pass


class DeferredDeletionAsAppRole(AsAppRole, test_scratch_purge.DeferredDeletionTest):
    pass


class RetentionWorkflowAsAppRole(AsAppRole, test_scratch_purge.RetentionWorkflowTest):
    pass


class PurgeSkipsLockedRowsAsAppRole(
    AsAppRole, test_scratch_concurrency.PurgeSkipsLockedRowsTest
):
    pass


class PurgeRechecksBeforeDeletingAsAppRole(
    AsAppRole, test_scratch_concurrency.PurgeRechecksBeforeDeletingTest
):
    pass


class OperationsWaitForTheRowLockAsAppRole(
    AsAppRole, test_scratch_concurrency.OperationsWaitForTheRowLockTest
):
    pass


class PurgeRacesLeaseAsAppRole(AsAppRole, test_scratch_concurrency.PurgeRacesLeaseTest):
    pass


@requires_postgres
class AppRolePrivilegesTest(PostgresScratchTestCase):
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

    def snapshot(self) -> dict[str, list[tuple]]:
        return {
            table: self.owner_rows(f"SELECT * FROM {table} ORDER BY 1, 2")
            for table in EXPECTED
        }

    async def test_the_store_really_runs_as_a_non_superuser_role(self):
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

    async def test_the_expectations_cover_every_scratch_table(self):
        tables = {
            row[0]
            for row in self.owner_rows(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
                "AND tablename LIKE 'research\\_scratch\\_%'"
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

    async def test_a_role_without_grants_reaches_no_scratch_table(self):
        for table in EXPECTED:
            with self.subTest(table=table):
                await self.refused(self.other, f"SELECT count(*) FROM {table}")
                await self.refused(self.other, f"DELETE FROM {table}")

    async def test_the_app_role_cannot_extend_rewrite_or_truncate(self):
        item = self.seed_item()
        self.seed_lease(item, self.clock.now + timedelta(minutes=5))
        before = self.snapshot()

        forbidden = [
            # The expiry only moves by pinning: it cannot be extended in SQL.
            "UPDATE research_scratch_items SET expires_at = now() + interval '30 days'",
            # What was collected is never rewritten, re-assigned or re-dated.
            "UPDATE research_scratch_items SET content = 'changed'",
            "UPDATE research_scratch_items SET summary = 'changed'",
            "UPDATE research_scratch_items SET source_metadata = '{}'::jsonb",
            "UPDATE research_scratch_items SET project_id = gen_random_uuid()",
            "UPDATE research_scratch_items SET created_by = gen_random_uuid()",
            "UPDATE research_scratch_items SET created_at = now()",
            "UPDATE research_scratch_items SET id = gen_random_uuid()",
            # A lease belongs to one item and one holder.
            "UPDATE research_scratch_leases SET item_id = gen_random_uuid()",
            "UPDATE research_scratch_leases SET holder_id = gen_random_uuid()",
            "TRUNCATE research_scratch_items CASCADE",
            "TRUNCATE research_scratch_leases",
            # The schema belongs to the migration role.
            "ALTER TABLE research_scratch_items ADD COLUMN extra text",
            "ALTER TABLE research_scratch_items DROP CONSTRAINT "
            "ck_research_scratch_items_source_metadata_object",
            "DROP INDEX ix_research_scratch_items_purgeable",
            "DROP TABLE research_scratch_leases",
        ]
        for sql in forbidden:
            with self.subTest(sql=sql):
                await self.refused(self.app, sql)

        self.assertEqual(self.snapshot(), before)

    async def test_the_app_role_can_pin_and_delete(self):
        item = self.seed_item()
        async with self.app.session() as session:
            await session.execute(
                text("UPDATE research_scratch_items SET pinned = true")
            )
            await session.commit()
        self.assertTrue(
            self.owner_scalar(
                "SELECT pinned FROM research_scratch_items WHERE id = :i", i=item
            )
        )
        async with self.app.session() as session:
            await session.execute(text("DELETE FROM research_scratch_items"))
            await session.commit()
        self.assertEqual(
            self.owner_scalar("SELECT count(*) FROM research_scratch_items"), 0
        )


if __name__ == "__main__":
    unittest.main()
