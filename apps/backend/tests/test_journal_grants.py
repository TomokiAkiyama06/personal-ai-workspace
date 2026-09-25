"""The journal tables in the split-role deployment (real PostgreSQL).

The migrations run as the owner of the schema and the backend connects as a
NON-superuser application role (``PAW_APP_DATABASE_ROLE``). These tests migrate the
test database that way, then

* run the journal, queue and consolidator test classes as that role, so every
  statement the services execute is proven to work with exactly the privileges
  revision 0041 grants (and the ones 0040 gave the Memory tables they also use), and
* check that nothing else is allowed: rewriting where an entry sits, re-prioritising
  a queued job, re-pointing a key, deleting history, changing the schema.

Role names are unique per run and dropped afterwards; the test user must be allowed
to create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import unittest
import uuid

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from paw_backend.db import Database

from . import (
    test_journal_concurrency,
    test_journal_consolidator,
    test_journal_failures,
    test_journal_gpu_unavailable,
    test_journal_queue,
    test_journal_service,
)
from .journal_support import (
    AsyncPostgresJournalTestCase,
    ScriptedWorker,
    memory,
    worker_output,
)
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate, new_database, requires_postgres

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_journal_app_{_RUN}"
OTHER_ROLE = f"paw_journal_other_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-journal"

# table -> (table-level privileges, columns the application may UPDATE). The exact
# copy of the choices (and their reasons) in migration 0041.
EXPECTED = {
    "memory_journal_entries": (
        {"SELECT", "INSERT"},
        {"state", "consolidated_at", "outcome"},
    ),
    "memory_consolidation_queue": (
        {"SELECT", "INSERT"},
        {
            "status",
            "available_at",
            "attempts",
            "deferrals",
            "claim_count",
            "claimed_by",
            "claimed_at",
            "lease_expires_at",
            "last_failure",
            "finished_at",
        },
    ),
    "memory_consolidation_keys": (
        {"SELECT", "INSERT"},
        {"applied_conversation_id", "applied_event_sequence", "applied_recorded_at"},
    ),
}
# What the services also do to the tables of revision 0040 (which 0040 grants):
# the raw message, the row lock on the conversation, the candidate versions.
NEEDED_FROM_0040 = {
    "messages": {"SELECT", "INSERT"},
    "conversations": {"SELECT"},
    "memories": {"SELECT", "INSERT"},
    "memory_versions": {"SELECT", "INSERT"},
    "memory_relations": {"SELECT", "INSERT"},
    "memory_sources": {"SELECT", "INSERT"},
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
    """Mixed into a journal test: its databases connect as the app role."""

    def new_database(self) -> Database:
        database = role_database(APP_ROLE)
        self.addAsyncCleanup(database.dispose)
        return database


# The service test classes, unchanged, but every statement runs as the
# unprivileged role. (Seeding and checking use the owner's engine of the base class.)
class RecordAsAppRole(AsAppRole, test_journal_service.RecordUserMessageTest):
    pass


class SequenceAsAppRole(AsAppRole, test_journal_service.EventSequenceTest):
    pass


class OwnershipAsAppRole(AsAppRole, test_journal_service.OwnershipTest):
    pass


class AuthorizationAsAppRole(AsAppRole, test_journal_service.AuthorizationTest):
    pass


class PendingAsAppRole(AsAppRole, test_journal_service.PendingObservationsTest):
    pass


class PriorityAsAppRole(AsAppRole, test_journal_queue.PriorityOrderTest):
    pass


class LeaseAsAppRole(AsAppRole, test_journal_queue.LeaseTest):
    pass


class RetryAsAppRole(AsAppRole, test_journal_queue.RetryAndDeadLetterTest):
    pass


class EnqueueAsAppRole(AsAppRole, test_journal_queue.EnqueueTest):
    pass


class ClaimAsAppRole(AsAppRole, test_journal_queue.ConcurrentClaimTest):
    pass


class CandidatesAsAppRole(AsAppRole, test_journal_consolidator.CandidateVersionTest):
    pass


class HighRiskAsAppRole(AsAppRole, test_journal_consolidator.HighRiskAndConfirmedTest):
    pass


class VersioningAsAppRole(AsAppRole, test_journal_consolidator.VersioningTest):
    pass


class PrivacyAsAppRole(AsAppRole, test_journal_consolidator.PrivacyBetweenUsersTest):
    pass


class DeletionAsAppRole(AsAppRole, test_journal_consolidator.ConversationDeletionTest):
    pass


class InvalidOutputAsAppRole(AsAppRole, test_journal_failures.InvalidOutputTest):
    pass


class WorkerErrorAsAppRole(AsAppRole, test_journal_failures.WorkerErrorTest):
    pass


class ApplyFailureAsAppRole(AsAppRole, test_journal_failures.ApplyFailureTest):
    pass


class FencingAsAppRole(AsAppRole, test_journal_failures.LeaseFencingEndToEndTest):
    pass


class GpuAsAppRole(AsAppRole, test_journal_gpu_unavailable.GpuUnavailableTest):
    pass


class SequenceConcurrencyAsAppRole(
    AsAppRole, test_journal_concurrency.ConcurrentSequenceTest
):
    pass


class RacingAsAppRole(AsAppRole, test_journal_concurrency.RacingConsolidatorsTest):
    pass


@requires_postgres
class AppRolePrivilegesTest(AsAppRole, AsyncPostgresJournalTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.role_engine = self.new_database()
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

    def snapshot(self) -> dict[str, list[dict]]:
        return {
            table: self.rows(f"SELECT * FROM {table} ORDER BY 1, 2")
            for table in EXPECTED
        }

    async def test_the_services_really_run_as_a_non_superuser_role(self):
        for database in (self.role_engine, self.other):
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

    def test_the_app_role_holds_exactly_the_least_privileges(self):
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

    def test_the_app_role_holds_what_the_services_need_of_the_memory_tables(self):
        for table, privileges in NEEDED_FROM_0040.items():
            for privilege in privileges:
                with self.subTest(table=table, privilege=privilege):
                    self.assertTrue(
                        self.scalar(
                            "SELECT has_table_privilege(:r, :t, :p)",
                            r=APP_ROLE,
                            t=table,
                            p=privilege,
                        )
                    )
        # The conversation row lock (FOR NO KEY UPDATE) needs an UPDATE privilege on
        # at least one column, and 0040 gives ``title`` and ``updated_at``.
        self.assertTrue(
            self.scalar(
                "SELECT has_column_privilege(:r, 'conversations', 'updated_at',"
                " 'UPDATE')",
                r=APP_ROLE,
            )
        )
        # Superseding a version updates its status; nothing else of a version.
        self.assertTrue(
            self.scalar(
                "SELECT has_column_privilege(:r, 'memory_versions', 'status',"
                " 'UPDATE')",
                r=APP_ROLE,
            )
        )
        for column in ("content", "confirmation_state", "scope", "owner_user_id"):
            with self.subTest(column=column):
                self.assertFalse(
                    self.scalar(
                        "SELECT has_column_privilege(:r, 'memory_versions', :c,"
                        " 'UPDATE')",
                        r=APP_ROLE,
                        c=column,
                    )
                )

    async def test_a_role_without_grants_reaches_no_journal_table(self):
        for table in EXPECTED:
            with self.subTest(table=table):
                await self.refused(self.other, f"SELECT count(*) FROM {table}")
                await self.refused(self.other, f"DELETE FROM {table}")

    async def test_the_app_role_cannot_rewrite_the_journal_or_the_schema(self):
        conversation = self.seed_conversation()
        receipt = await self.record("Use tabs.", conversation=conversation)
        await self.record("More.", conversation=conversation)
        worker = ScriptedWorker(worker_output(memory("indent_style")))
        await self.new_consolidator(worker).run_once()
        before = self.snapshot()

        forbidden = [
            # Where an entry sits in time and in the conversation never changes.
            "UPDATE memory_journal_entries SET event_sequence = 99",
            "UPDATE memory_journal_entries SET conversation_id = gen_random_uuid()",
            "UPDATE memory_journal_entries SET message_id = gen_random_uuid()",
            "UPDATE memory_journal_entries SET turn_id = gen_random_uuid()",
            "UPDATE memory_journal_entries SET owner_user_id = gen_random_uuid()",
            "UPDATE memory_journal_entries SET project_id = gen_random_uuid()",
            "UPDATE memory_journal_entries SET recorded_at = now()",
            "UPDATE memory_journal_entries SET id = gen_random_uuid()",
            # An observation is never deleted by the application (it goes with its
            # conversation), and the table is never emptied.
            "DELETE FROM memory_journal_entries",
            "TRUNCATE memory_journal_entries",
            # A queued job cannot be re-prioritised, re-pointed or re-dated.
            "UPDATE memory_consolidation_queue"
            " SET priority = 'high', priority_rank = 0",
            "UPDATE memory_consolidation_queue SET priority_rank = 0",
            "UPDATE memory_consolidation_queue SET enqueued_at = now()",
            "UPDATE memory_consolidation_queue SET entry_id = gen_random_uuid()",
            "UPDATE memory_consolidation_queue SET id = 1",
            # Jobs are history: dead letters are kept.
            "DELETE FROM memory_consolidation_queue",
            "TRUNCATE memory_consolidation_queue",
            # Which owner and memory a key names never changes.
            "UPDATE memory_consolidation_keys SET key = 'other'",
            "UPDATE memory_consolidation_keys SET owner_user_id = gen_random_uuid()",
            "UPDATE memory_consolidation_keys SET memory_id = gen_random_uuid()",
            "UPDATE memory_consolidation_keys SET created_at = now()",
            "DELETE FROM memory_consolidation_keys",
            "TRUNCATE memory_consolidation_keys",
            # A stored message is never edited or deleted one by one.
            "UPDATE messages SET content = 'changed'",
            "UPDATE messages SET event_sequence = 77",
            "DELETE FROM messages",
            # A version's text and confirmation state are never rewritten.
            "UPDATE memory_versions SET content = 'changed'",
            "UPDATE memory_versions SET confirmation_state = 'confirmed'",
            "UPDATE memory_versions SET owner_user_id = gen_random_uuid()",
            "DELETE FROM memory_versions",
            # The schema belongs to the migration role.
            "ALTER TABLE memory_consolidation_queue ADD COLUMN extra text",
            "ALTER TABLE memory_consolidation_queue DROP CONSTRAINT "
            "ck_memory_consolidation_queue_priority_rank_matches_priority",
            "DROP INDEX uq_memory_consolidation_queue_one_active_per_entry",
            "DROP TABLE memory_journal_entries",
            "ALTER TABLE memory_journal_entries DISABLE TRIGGER ALL",
        ]
        for sql in forbidden:
            with self.subTest(sql=sql):
                await self.refused(self.role_engine, sql)

        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.entry_row(receipt.entry_id)["event_sequence"], 0)


if __name__ == "__main__":
    unittest.main()
