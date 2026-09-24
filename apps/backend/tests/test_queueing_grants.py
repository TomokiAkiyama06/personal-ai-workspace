"""The queue, budget and loop tables in the split-role deployment (real PostgreSQL).

The migrations run as the owner of the schema and the backend connects as a
NON-superuser application role (``PAW_APP_DATABASE_ROLE``). These tests migrate
the test database that way, then

* run the ``TaskQueue`` / ``BudgetTracker`` / ``LoopDetector`` test classes as
  that role, so every statement the services execute is proven to work with
  exactly the privileges revision 0033 grants, and
* check that nothing else is allowed (re-prioritising a queued task, deleting
  history, changing keys, changing the schema).

Role names are unique per run and dropped afterwards; the test user must be
allowed to create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import unittest
import uuid

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from paw_backend.db import Database
from paw_backend.tasks.queueing import BudgetKind, BudgetPreset, Priority

from . import (
    test_queueing_budget,
    test_queueing_flow,
    test_queueing_loop_db,
    test_queueing_queue,
)
from .queueing_support import T0, PostgresQueueingTestCase, at
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate, new_database, requires_postgres

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_queueing_app_{_RUN}"
OTHER_ROLE = f"paw_queueing_other_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-queueing"
SIGNATURE = "a" * 64

# table -> (table-level privileges, columns the application may UPDATE). The
# exact copy of the choices (and their reasons) in migration 0033.
EXPECTED = {
    "queue_entries": (
        {"SELECT", "INSERT"},
        {
            "status",
            "claimed_by",
            "claimed_at",
            "lease_expires_at",
            "claim_count",
            "finished_at",
        },
    ),
    "budget_usages": (
        {"SELECT", "INSERT"},
        {"preset", "limit_value", "consumed", "running_since"},
    ),
    "loop_failure_signatures": ({"SELECT", "INSERT", "DELETE"}, set()),
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
    """Mixed into a ``PostgresTaskTestCase``: its databases connect as the app role."""

    def new_database(self) -> Database:
        database = role_database(APP_ROLE)
        self.addAsyncCleanup(database.dispose)
        return database


# The service test classes, unchanged, but every statement runs as the
# unprivileged role. (Classes that need raw owner SQL are not repeated here.)
class EnqueueAsAppRole(AsAppRole, test_queueing_queue.EnqueueTest):
    pass


class ClaimOrderAsAppRole(AsAppRole, test_queueing_queue.ClaimOrderTest):
    pass


class LeaseAsAppRole(AsAppRole, test_queueing_queue.LeaseTest):
    pass


class ReleaseCompleteCancelAsAppRole(
    AsAppRole, test_queueing_queue.ReleaseCompleteCancelTest
):
    pass


class ConcurrencyAsAppRole(AsAppRole, test_queueing_queue.ConcurrencyTest):
    pass


class BudgetConfigurationAsAppRole(AsAppRole, test_queueing_budget.ConfigurationTest):
    pass


class BudgetRecordAsAppRole(AsAppRole, test_queueing_budget.RecordTest):
    pass


class BudgetCheckAsAppRole(AsAppRole, test_queueing_budget.CheckTest):
    pass


class BudgetRuntimeAsAppRole(AsAppRole, test_queueing_budget.RuntimeTest):
    pass


class RecordFailureAsAppRole(AsAppRole, test_queueing_loop_db.RecordFailureTest):
    pass


class HistoryAsAppRole(AsAppRole, test_queueing_loop_db.HistoryTest):
    pass


class ClearAsAppRole(AsAppRole, test_queueing_loop_db.ClearTest):
    pass


class FlowAsAppRole(AsAppRole, test_queueing_flow.FlowTest):
    pass


@requires_postgres
class AppRolePrivilegesTest(AsAppRole, PostgresQueueingTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.other = role_database(OTHER_ROLE)
        self.addAsyncCleanup(self.other.dispose)

    async def refused(self, database: Database, sql: str, **parameters):
        async with database.session() as session:
            with self.assertRaises(DBAPIError) as caught:
                await session.execute(text(sql), parameters)
                await session.commit()
        self.assertIsInstance(
            caught.exception.orig, psycopg.errors.InsufficientPrivilege
        )

    async def snapshot(self) -> dict[str, list[dict]]:
        return {
            table: await self.rows(f"SELECT * FROM {table} ORDER BY 1, 2")
            for table in EXPECTED
        }

    async def test_the_services_really_run_as_a_non_superuser_role(self):
        for database in (self.database, self.other):
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

    async def test_the_expectations_cover_every_queueing_table(self):
        tables = {
            row["tablename"]
            for row in await self.rows(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
                "AND tablename = ANY(:names)",
                names=list(EXPECTED),
            )
        }
        self.assertEqual(tables, set(EXPECTED))

    async def test_the_app_role_holds_exactly_the_least_privileges(self):
        for table, (privileges, update_columns) in EXPECTED.items():
            with self.subTest(table=table):
                for privilege in ALL_PRIVILEGES:
                    granted = await self.scalar(
                        "SELECT has_table_privilege(:r, :t, :p)",
                        r=APP_ROLE,
                        t=table,
                        p=privilege,
                    )
                    self.assertEqual(granted, privilege in privileges, privilege)
                columns = [
                    row["column_name"]
                    for row in await self.rows(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :t",
                        t=table,
                    )
                ]
                updatable = set()
                for column in columns:
                    if await self.scalar(
                        "SELECT has_column_privilege(:r, :t, :c, 'UPDATE')",
                        r=APP_ROLE,
                        t=table,
                        c=column,
                    ):
                        updatable.add(column)
                self.assertEqual(updatable, update_columns)

    async def test_a_role_without_grants_reaches_no_queueing_table(self):
        for table in EXPECTED:
            with self.subTest(table=table):
                await self.refused(self.other, f"SELECT count(*) FROM {table}")
                await self.refused(self.other, f"DELETE FROM {table}")

    async def test_the_app_role_cannot_rewrite_keys_history_or_the_schema(self):
        (task_id,) = await self.make_tasks(1)
        entry = await self.queue.enqueue(task_id, now=at(0), priority=Priority.LOW)
        await self.queue.claim_next("w1", at(1))
        await self.budget.set_preset(task_id, BudgetPreset.STANDARD)
        await self.budget.record(task_id, BudgetKind.TOKENS, 5)
        await self.loop_detector.record_failure(
            task_id, error_class="E", step="s", message="m"
        )
        before = await self.snapshot()

        forbidden = [
            # A queued task cannot be re-prioritised, re-assigned or re-dated.
            "UPDATE queue_entries SET priority = 'high', priority_rank = 0",
            "UPDATE queue_entries SET priority_rank = 0",
            "UPDATE queue_entries SET enqueued_at = now()",
            "UPDATE queue_entries SET task_id = gen_random_uuid()",
            "UPDATE queue_entries SET id = 1",
            # Entries are never deleted: cancelling and completing are statuses.
            "DELETE FROM queue_entries",
            "TRUNCATE queue_entries",
            # A budget's key and origin never change, and a budget is never removed.
            "UPDATE budget_usages SET kind = 'steps'",
            "UPDATE budget_usages SET task_id = gen_random_uuid()",
            "UPDATE budget_usages SET created_at = now()",
            "DELETE FROM budget_usages",
            "TRUNCATE budget_usages",
            # A stored failure is never edited (rows leave only through the window).
            "UPDATE loop_failure_signatures SET signature = repeat('b', 64)",
            "UPDATE loop_failure_signatures SET approach = 0",
            "UPDATE loop_failure_signatures SET task_id = gen_random_uuid()",
            "TRUNCATE loop_failure_signatures",
            # The schema belongs to the migration role.
            "ALTER TABLE queue_entries ADD COLUMN extra text",
            "ALTER TABLE queue_entries DROP CONSTRAINT "
            "ck_queue_entries_priority_rank_matches_priority",
            "DROP INDEX uq_queue_entries_one_active_per_task",
            "DROP TABLE budget_usages",
            "ALTER TABLE loop_failure_signatures DISABLE TRIGGER ALL",
        ]
        for sql in forbidden:
            with self.subTest(sql=sql):
                await self.refused(self.database, sql)

        self.assertEqual(await self.snapshot(), before)
        row = await self.entry_row(entry.id)
        self.assertEqual((row["priority"], row["status"]), ("low", "claimed"))

    async def test_the_app_role_deletes_only_the_failure_window(self):
        (task_id,) = await self.make_tasks(1)
        for _ in range(12):
            await self.loop_detector.record_failure(
                task_id, error_class="E", step="s", message="m"
            )
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM loop_failure_signatures"), 10
        )
        self.assertEqual(await self.loop_detector.clear(task_id), 10)
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM loop_failure_signatures"), 0
        )

    async def test_the_app_role_can_run_the_whole_lifecycle_through_a_second_process(
        self,
    ):
        queue = self.new_queue()
        budget = self.new_budget()
        (task_id,) = await self.make_tasks(1)
        await budget.set_preset(task_id, BudgetPreset.LONG)
        await queue.enqueue(task_id, now=T0, priority=Priority.HIGH)
        claimed = await self.new_queue().claim_next("w1", at(1))
        await self.new_queue().heartbeat(claimed.id, "w1", at(2))
        await budget.start_runtime(task_id)
        await budget.record(task_id, BudgetKind.STEPS, 3)
        self.clock.set(30)
        usage = await budget.stop_runtime(task_id)
        await self.new_queue().complete(claimed.id, "w1", at(31))
        self.assertEqual(usage.consumed, 30)
        row = await self.entry_row(claimed.id)
        self.assertEqual(row["status"], "completed")


if __name__ == "__main__":
    unittest.main()
