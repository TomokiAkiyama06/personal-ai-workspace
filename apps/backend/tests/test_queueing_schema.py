"""PAW-033 tables: constraints, indexes, migration up/down and drift.

Skipped (except the offline SQL tests) unless ``PAW_TEST_DATABASE_URL`` is set.
These tests pass against the stubs: the schema is not stubbed.
"""

import asyncio
import io
import unittest
import uuid

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from paw_backend.db import Base
from paw_backend.tasks.queueing import models as queueing_models
from paw_backend.tasks.queueing.validation import MAX_APPROACH, MAX_CONSUMED

from .queueing_support import PostgresQueueingTestCase, at, requires_postgres
from .support import paw_environment
from .task_support import migrate, new_database
from .test_migrations import offline_config

QUEUEING_TABLES = ("queue_entries", "budget_usages", "loop_failure_signatures")
SIGNATURE = "a" * 64


class MetadataTest(unittest.TestCase):
    def test_the_models_define_exactly_the_three_tables(self):
        self.assertEqual(
            {
                table.name
                for table in Base.metadata.tables.values()
                if table.name in QUEUEING_TABLES
            },
            set(QUEUEING_TABLES),
        )
        self.assertEqual(queueing_models.QueueEntryRow.__tablename__, "queue_entries")

    def test_no_table_name_starts_with_task(self):
        # The PAW-032 tests inspect every table named ``task%``.
        for name in QUEUEING_TABLES:
            self.assertFalse(name.startswith("task"))

    def test_every_table_references_tasks_with_a_real_foreign_key(self):
        for name in QUEUEING_TABLES:
            table = Base.metadata.tables[name]
            targets = {
                fk.target_fullname
                for fk in table.foreign_keys
                if fk.parent.name == "task_id"
            }
            with self.subTest(table=name):
                self.assertEqual(targets, {"tasks.id"})

    def test_every_constraint_and_index_is_named(self):
        for name in QUEUEING_TABLES:
            table = Base.metadata.tables[name]
            for constraint in table.constraints:
                with self.subTest(table=name, constraint=constraint):
                    self.assertTrue(str(constraint.name))
            for index in table.indexes:
                with self.subTest(table=name, index=index):
                    self.assertTrue(str(index.name))


class OfflineMigrationTest(unittest.TestCase):
    """SQL rendering needs no database, so this runs everywhere."""

    def render(self, direction: str) -> str:
        output = io.StringIO()
        config = offline_config(output)
        with paw_environment(PAW_DATABASE_URL="postgresql://paw:pw@db.internal/paw"):
            if direction == "up":
                command.upgrade(config, "base:head", sql=True)
            else:
                command.downgrade(config, "head:base", sql=True)
        return output.getvalue()

    def test_upgrade_creates_the_tables_after_the_task_tables(self):
        sql = self.render("up")
        self.assertNotIn("db.internal", sql)
        for table in QUEUEING_TABLES:
            self.assertIn(f"CREATE TABLE {table} (", sql)
        self.assertLess(
            sql.index("CREATE TABLE tasks ("), sql.index("CREATE TABLE queue_entries (")
        )
        self.assertIn("FOREIGN KEY(task_id) REFERENCES tasks (id)", sql)
        self.assertIn("UPDATE alembic_version SET version_num='0033'", sql)
        self.assertIn("CREATE UNIQUE INDEX uq_queue_entries_one_active_per_task", sql)
        self.assertIn("CONSTRAINT ck_budget_usages_limit_matches_preset CHECK", sql)

    def test_downgrade_drops_the_three_tables(self):
        sql = self.render("down")
        for table in QUEUEING_TABLES:
            self.assertIn(f"DROP TABLE {table}", sql)
        self.assertLess(
            sql.index("DROP TABLE queue_entries"), sql.index("DROP TABLE tasks")
        )


@requires_postgres
class MigrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await asyncio.to_thread(migrate)
        # Leave the database at head for the other tests.
        self.addAsyncCleanup(asyncio.to_thread, migrate)

    async def tables(self) -> set[str]:
        database = new_database()
        try:
            async with database.engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = "
                        "current_schema() AND tablename = ANY(:names)"
                    ),
                    {"names": list(QUEUEING_TABLES)},
                )
                return {row[0] for row in rows}
        finally:
            await database.dispose()

    async def test_downgrade_removes_the_tables_and_upgrade_restores_them(self):
        self.assertEqual(await self.tables(), set(QUEUEING_TABLES))
        await asyncio.to_thread(migrate, "base", downgrade=True)
        self.assertEqual(await self.tables(), set())
        await asyncio.to_thread(migrate)
        self.assertEqual(await self.tables(), set(QUEUEING_TABLES))

    async def test_downgrading_this_revision_keeps_the_task_tables(self):
        script = ScriptDirectory.from_config(offline_config(io.StringIO()))
        parent = script.get_revision("0033").down_revision
        await asyncio.to_thread(migrate, parent, downgrade=True)
        self.assertEqual(await self.tables(), set())
        database = new_database()
        try:
            async with database.engine.connect() as connection:
                present = (
                    await connection.execute(text("SELECT to_regclass('tasks')"))
                ).scalar()
        finally:
            await database.dispose()
        self.assertEqual(present, "tasks")

    async def test_upgrade_is_idempotent_at_head(self):
        await asyncio.to_thread(migrate)
        self.assertEqual(await self.tables(), set(QUEUEING_TABLES))

    async def test_the_migration_matches_the_orm_models(self):
        def only_queueing_objects(obj, name, type_, reflected, compare_to):
            table = obj if type_ == "table" else getattr(obj, "table", None)
            return table is not None and table.name in QUEUEING_TABLES

        database = new_database()
        try:
            async with database.engine.connect() as connection:

                def diff(sync_connection):
                    context = MigrationContext.configure(
                        sync_connection, opts={"include_object": only_queueing_objects}
                    )
                    return compare_metadata(context, Base.metadata)

                differences = await connection.run_sync(diff)
        finally:
            await database.dispose()
        self.assertEqual(differences, [])

    async def test_every_constraint_and_index_is_named_by_the_naming_convention(self):
        expected = set()
        for table_name in QUEUEING_TABLES:
            table = Base.metadata.tables[table_name]
            expected |= {str(c.name) for c in table.constraints}
            expected |= {str(i.name) for i in table.indexes}
        database = new_database()
        try:
            async with database.engine.connect() as connection:
                constraints = await connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint c "
                        "JOIN pg_class t ON t.oid = c.conrelid "
                        "WHERE t.relname = ANY(:names) "
                        "AND c.contype IN ('p', 'u', 'f', 'c')"
                    ),
                    {"names": list(QUEUEING_TABLES)},
                )
                indexes = await connection.execute(
                    text(
                        "SELECT i.relname FROM pg_index x "
                        "JOIN pg_class i ON i.oid = x.indexrelid "
                        "JOIN pg_class t ON t.oid = x.indrelid "
                        "WHERE t.relname = ANY(:names) AND NOT EXISTS "
                        "(SELECT 1 FROM pg_constraint c "
                        "WHERE c.conindid = x.indexrelid)"
                    ),
                    {"names": list(QUEUEING_TABLES)},
                )
                actual = {row[0] for row in constraints} | {row[0] for row in indexes}
        finally:
            await database.dispose()
        self.assertEqual(actual, expected)
        for name in (
            "pk_queue_entries",
            "fk_queue_entries_task_id_tasks",
            "uq_queue_entries_one_active_per_task",
            "ix_queue_entries_claim_order",
            "pk_budget_usages",
            "fk_budget_usages_task_id_tasks",
            "ck_budget_usages_limit_matches_preset",
            "pk_loop_failure_signatures",
            "fk_loop_failure_signatures_task_id_tasks",
            "ix_loop_failure_signatures_task_id",
        ):
            self.assertIn(name, actual)


@requires_postgres
class QueueEntryConstraintTest(PostgresQueueingTestCase):
    async def insert(self, task_id, **overrides) -> None:
        values = {
            "task_id": task_id,
            "priority": "normal",
            "priority_rank": 1,
            "status": "queued",
            "enqueued_at": at(0),
            "claimed_by": None,
            "claimed_at": None,
            "lease_expires_at": None,
            "finished_at": None,
        }
        values.update(overrides)
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        async with self.database.engine.begin() as connection:
            await connection.execute(
                text(f"INSERT INTO queue_entries ({columns}) VALUES ({placeholders})"),
                values,
            )

    async def assertRejected(self, task_id, **overrides):
        with self.assertRaises(IntegrityError):
            await self.insert(task_id, **overrides)
        self.assertEqual(await self.scalar("SELECT count(*) FROM queue_entries"), 0)

    async def test_a_queued_entry_has_the_defaults(self):
        (task_id,) = await self.make_tasks(1)
        await self.insert(task_id)
        (row,) = await self.rows("SELECT * FROM queue_entries")
        self.assertEqual(
            (row["status"], row["claim_count"], row["priority"], row["priority_rank"]),
            ("queued", 0, "normal", 1),
        )

    async def test_the_three_priorities_have_the_ranks_0_1_2(self):
        for priority, rank in (("high", 0), ("normal", 1), ("low", 2)):
            (task_id,) = await self.make_tasks(1)
            await self.insert(task_id, priority=priority, priority_rank=rank)
        self.assertEqual(await self.scalar("SELECT count(*) FROM queue_entries"), 3)

    async def test_a_priority_and_rank_that_disagree_are_rejected(self):
        (task_id,) = await self.make_tasks(1)
        await self.assertRejected(task_id, priority="high", priority_rank=1)
        await self.assertRejected(task_id, priority="low", priority_rank=0)
        await self.assertRejected(task_id, priority="urgent", priority_rank=0)

    async def test_an_unknown_status_is_rejected(self):
        (task_id,) = await self.make_tasks(1)
        await self.assertRejected(task_id, status="running")

    async def test_an_entry_cannot_reference_a_missing_task(self):
        await self.assertRejected(uuid.uuid4())

    async def test_a_claimed_entry_needs_a_worker_and_a_lease(self):
        (task_id,) = await self.make_tasks(1)
        claimed = dict(
            status="claimed",
            claimed_by="w1",
            claimed_at=at(0),
            lease_expires_at=at(60),
        )
        await self.assertRejected(task_id, **{**claimed, "claimed_by": None})
        await self.assertRejected(task_id, **{**claimed, "claimed_at": None})
        await self.assertRejected(task_id, **{**claimed, "lease_expires_at": None})
        # The lease must end after it began.
        await self.assertRejected(task_id, **{**claimed, "lease_expires_at": at(0)})
        await self.insert(task_id, **claimed)

    async def test_a_lease_only_exists_while_claimed(self):
        (task_id,) = await self.make_tasks(1)
        await self.assertRejected(task_id, lease_expires_at=at(60))
        await self.assertRejected(
            task_id, status="completed", lease_expires_at=at(60), finished_at=at(1)
        )

    async def test_a_queued_entry_has_no_worker(self):
        (task_id,) = await self.make_tasks(1)
        await self.assertRejected(task_id, claimed_by="w1")
        await self.assertRejected(task_id, claimed_at=at(0))

    async def test_finished_at_is_set_exactly_for_finished_entries(self):
        (task_id,) = await self.make_tasks(1)
        await self.assertRejected(task_id, status="completed")
        await self.assertRejected(task_id, status="cancelled")
        await self.assertRejected(task_id, finished_at=at(1))
        await self.insert(task_id, status="cancelled", finished_at=at(1))

    async def test_a_negative_claim_count_is_rejected(self):
        (task_id,) = await self.make_tasks(1)
        await self.assertRejected(task_id, claim_count=-1)

    async def test_a_task_has_at_most_one_active_entry(self):
        (task_id,) = await self.make_tasks(1)
        await self.insert(task_id)
        with self.assertRaises(IntegrityError):
            await self.insert(task_id)
        with self.assertRaises(IntegrityError):
            await self.insert(
                task_id,
                status="claimed",
                claimed_by="w",
                claimed_at=at(0),
                lease_expires_at=at(5),
            )
        self.assertEqual(await self.scalar("SELECT count(*) FROM queue_entries"), 1)

    async def test_finished_entries_do_not_block_a_new_one(self):
        (task_id,) = await self.make_tasks(1)
        await self.insert(task_id, status="completed", finished_at=at(1))
        await self.insert(task_id, status="cancelled", finished_at=at(2))
        await self.insert(task_id)
        self.assertEqual(await self.scalar("SELECT count(*) FROM queue_entries"), 3)


@requires_postgres
class BudgetConstraintTest(PostgresQueueingTestCase):
    async def insert(self, task_id, **overrides) -> None:
        values = {
            "task_id": task_id,
            "kind": "steps",
            "preset": "standard",
            "consumed": 0,
            "limit_value": 50,
            "running_since": None,
        }
        values.update(overrides)
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        async with self.database.engine.begin() as connection:
            await connection.execute(
                text(f"INSERT INTO budget_usages ({columns}) VALUES ({placeholders})"),
                values,
            )

    async def test_a_valid_row_and_its_defaults(self):
        (task_id,) = await self.make_tasks(1)
        async with self.database.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO budget_usages (task_id, kind, preset, limit_value) "
                    "VALUES (:t, 'tokens', 'long', 10)"
                ),
                {"t": task_id},
            )
        row = await self.budget_row(task_id, "tokens")
        self.assertEqual((row["consumed"], row["limit_value"]), (0, 10))
        self.assertIsNotNone(row["created_at"])

    async def test_one_row_per_task_and_kind(self):
        (task_id,) = await self.make_tasks(1)
        await self.insert(task_id)
        with self.assertRaises(IntegrityError):
            await self.insert(task_id)
        await self.insert(task_id, kind="tokens")

    async def test_invalid_kind_and_preset_are_rejected(self):
        (task_id,) = await self.make_tasks(1)
        with self.assertRaises(IntegrityError):
            await self.insert(task_id, kind="cost")
        with self.assertRaises(IntegrityError):
            await self.insert(task_id, preset="huge")

    async def test_a_row_cannot_reference_a_missing_task(self):
        with self.assertRaises(IntegrityError):
            await self.insert(uuid.uuid4())

    async def test_consumed_is_between_zero_and_the_cap(self):
        (task_id,) = await self.make_tasks(1)
        with self.assertRaises(IntegrityError):
            await self.insert(task_id, consumed=-1)
        with self.assertRaises(IntegrityError):
            await self.insert(task_id, consumed=MAX_CONSUMED + 1)
        await self.insert(task_id, consumed=MAX_CONSUMED)

    async def test_the_limit_is_null_exactly_for_the_unlimited_preset(self):
        (task_id,) = await self.make_tasks(1)
        with self.assertRaises(IntegrityError):
            await self.insert(task_id, preset="unlimited", limit_value=5)
        with self.assertRaises(IntegrityError):
            await self.insert(task_id, preset="standard", limit_value=None)
        with self.assertRaises(IntegrityError):
            await self.insert(task_id, limit_value=-1)
        await self.insert(task_id, preset="unlimited", limit_value=None)
        await self.insert(task_id, kind="tokens", limit_value=0)

    async def test_only_the_runtime_row_can_be_running(self):
        (task_id,) = await self.make_tasks(1)
        with self.assertRaises(IntegrityError):
            await self.insert(task_id, kind="steps", running_since=at(0))
        await self.insert(task_id, kind="runtime_seconds", running_since=at(0))


@requires_postgres
class FailureSignatureConstraintTest(PostgresQueueingTestCase):
    async def insert(self, task_id, signature=SIGNATURE, approach=0) -> None:
        async with self.database.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO loop_failure_signatures "
                    "(task_id, approach, signature) VALUES (:t, :a, :s)"
                ),
                {"t": task_id, "a": approach, "s": signature},
            )

    async def test_valid_rows_are_ordered_by_seq(self):
        (task_id,) = await self.make_tasks(1)
        await self.insert(task_id, "a" * 64)
        await self.insert(task_id, "b" * 64, approach=MAX_APPROACH)
        rows = await self.rows("SELECT * FROM loop_failure_signatures ORDER BY seq")
        self.assertEqual([row["signature"] for row in rows], ["a" * 64, "b" * 64])
        self.assertLess(rows[0]["seq"], rows[1]["seq"])
        self.assertIsNotNone(rows[0]["created_at"])

    async def test_only_a_lowercase_sha256_hex_is_accepted(self):
        (task_id,) = await self.make_tasks(1)
        for bad in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "", "a" * 63 + "\n"):
            # 65 characters do not even fit the column (a DataError).
            with self.subTest(bad=bad), self.assertRaises(DBAPIError):
                await self.insert(task_id, bad)

    async def test_the_approach_is_bounded(self):
        (task_id,) = await self.make_tasks(1)
        for bad in (-1, MAX_APPROACH + 1):
            with self.subTest(bad=bad), self.assertRaises(IntegrityError):
                await self.insert(task_id, approach=bad)

    async def test_a_row_cannot_reference_a_missing_task(self):
        with self.assertRaises(IntegrityError):
            await self.insert(uuid.uuid4())

    async def test_the_table_has_no_column_that_could_hold_the_message(self):
        columns = await self.rows(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'loop_failure_signatures' ORDER BY ordinal_position"
        )
        self.assertEqual(
            [c["column_name"] for c in columns],
            ["seq", "task_id", "approach", "signature", "created_at"],
        )


if __name__ == "__main__":
    unittest.main()
