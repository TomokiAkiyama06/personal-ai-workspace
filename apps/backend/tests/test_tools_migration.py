"""Revision 0031: models, migration and database must describe the same schema.

The offline class needs no server. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the migration up and down, compare it
with the models through Alembic's autogenerate and compare constraint and index
names.
"""

import asyncio
import io
import unittest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

from paw_backend.db import Base
from paw_backend.tools.models import TABLE_NAMES

from .support import paw_environment
from .task_support import migrate, new_database, requires_postgres
from .test_migrations import offline_config

REVISION = "0031"
PREVIOUS = "0040"
FUNCTION = "tool_approval_events_reject_change"


def only_tool_objects(obj, name, type_, reflected, compare_to):
    table = obj if type_ == "table" else getattr(obj, "table", None)
    return table is not None and table.name in TABLE_NAMES


class ModelsTest(unittest.TestCase):
    def test_the_tables(self):
        self.assertEqual(TABLE_NAMES, ("tool_approvals", "tool_approval_events"))
        for name in TABLE_NAMES:
            self.assertIn(name, Base.metadata.tables)

    def test_every_constraint_and_index_has_a_conventional_name(self):
        prefixes = {
            "PrimaryKeyConstraint": "pk_",
            "ForeignKeyConstraint": "fk_",
            "UniqueConstraint": "uq_",
            "CheckConstraint": "ck_",
        }
        for name in TABLE_NAMES:
            table = Base.metadata.tables[name]
            for constraint in table.constraints:
                with self.subTest(table=name, constraint=constraint.name):
                    prefix = prefixes[type(constraint).__name__]
                    self.assertTrue(str(constraint.name).startswith(prefix))
                    self.assertIn(name, str(constraint.name))
            for index in table.indexes:
                with self.subTest(table=name, index=index.name):
                    self.assertRegex(str(index.name), r"^(ix|uq)_tool_approval")


class OfflineMigrationTest(unittest.TestCase):
    def render(self, direction: str, revisions: str) -> str:
        output = io.StringIO()
        config = offline_config(output)
        with paw_environment(PAW_DATABASE_URL="postgresql://paw:pw@db.internal/paw"):
            if direction == "up":
                command.upgrade(config, revisions, sql=True)
            else:
                command.downgrade(config, revisions, sql=True)
        return output.getvalue()

    def test_the_revision_follows_the_memory_schema(self):
        scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
        self.assertEqual(scripts.get_revision(REVISION).down_revision, PREVIOUS)

    def test_upgrade_creates_the_tables_the_index_and_the_append_only_trigger(self):
        sql = self.render("up", f"{PREVIOUS}:{REVISION}")
        self.assertNotIn("db.internal", sql)
        self.assertNotIn("paw:pw", sql)
        for table in TABLE_NAMES:
            self.assertIn(f"CREATE TABLE {table} (", sql)
        self.assertIn("CREATE UNIQUE INDEX uq_tool_approvals_open_call", sql)
        self.assertIn("WHERE status IN ('pending', 'approved')", sql)
        self.assertIn("CREATE TRIGGER tool_approval_events_append_only", sql)
        self.assertIn("BEFORE UPDATE OR DELETE ON tool_approval_events", sql)
        self.assertIn(
            "CONSTRAINT ck_tool_approvals_approver_is_delegating_user CHECK", sql
        )
        self.assertIn(f"UPDATE alembic_version SET version_num='{REVISION}'", sql)
        self.assertNotIn("FOREIGN KEY(task_id)", sql)
        self.assertNotIn("FOREIGN KEY(requester_user_id)", sql)

    def test_downgrade_drops_the_history_table_before_its_function(self):
        sql = self.render("down", f"{REVISION}:{PREVIOUS}")
        self.assertLess(
            sql.index("DROP TABLE tool_approval_events"),
            sql.index(f"DROP FUNCTION {FUNCTION}()"),
        )
        self.assertLess(
            sql.index(f"DROP FUNCTION {FUNCTION}()"),
            sql.index("DROP TABLE tool_approvals"),
        )
        self.assertIn(f"UPDATE alembic_version SET version_num='{PREVIOUS}'", sql)


@requires_postgres
class DatabaseMigrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await asyncio.to_thread(migrate)
        self.addAsyncCleanup(asyncio.to_thread, migrate)
        self.database = new_database()
        self.addAsyncCleanup(self.database.dispose)

    async def scalars(self, sql: str, **params) -> list:
        async with self.database.engine.connect() as connection:
            return list((await connection.execute(text(sql), params)).scalars())

    async def tool_tables(self) -> set[str]:
        return set(
            await self.scalars(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename LIKE 'tool\\_approval%'"
            )
        )

    async def function_exists(self) -> bool:
        count = await self.scalars(
            "SELECT count(*) FROM pg_proc WHERE proname = :name", name=FUNCTION
        )
        return count == [1]

    async def test_downgrade_removes_everything_and_upgrade_restores_it(self):
        self.assertEqual(await self.tool_tables(), set(TABLE_NAMES))
        self.assertTrue(await self.function_exists())
        self.assertEqual(
            await self.scalars("SELECT version_num FROM alembic_version"), [REVISION]
        )

        await asyncio.to_thread(migrate, PREVIOUS, downgrade=True)
        self.assertEqual(await self.tool_tables(), set())
        self.assertFalse(await self.function_exists())
        self.assertEqual(
            await self.scalars("SELECT version_num FROM alembic_version"), [PREVIOUS]
        )
        # the earlier revisions are untouched
        self.assertIn("tasks", await self.scalars("SELECT tablename FROM pg_tables"))

        await asyncio.to_thread(migrate)
        self.assertEqual(await self.tool_tables(), set(TABLE_NAMES))
        self.assertTrue(await self.function_exists())

    async def test_downgrade_to_base_and_back(self):
        await asyncio.to_thread(migrate, "base", downgrade=True)
        self.assertEqual(await self.tool_tables(), set())
        await asyncio.to_thread(migrate)
        self.assertEqual(await self.tool_tables(), set(TABLE_NAMES))

    async def test_upgrade_is_idempotent_at_head(self):
        await asyncio.to_thread(migrate)
        self.assertEqual(await self.tool_tables(), set(TABLE_NAMES))

    async def test_alembic_autogenerate_finds_no_difference(self):
        async with self.database.engine.connect() as connection:

            def diff(sync_connection):
                context = MigrationContext.configure(
                    sync_connection,
                    opts={
                        "compare_type": True,
                        "compare_server_default": True,
                        "include_object": only_tool_objects,
                    },
                )
                return compare_metadata(context, Base.metadata)

            differences = await connection.run_sync(diff)
        self.assertEqual(differences, [])

    async def test_every_constraint_and_index_is_named_by_the_metadata(self):
        expected = set()
        for name in TABLE_NAMES:
            table = Base.metadata.tables[name]
            expected |= {str(c.name) for c in table.constraints}
            expected |= {str(i.name) for i in table.indexes}
        constraints = await self.scalars(
            "SELECT conname FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname LIKE 'tool\\_approval%' "
            "AND c.contype IN ('p', 'u', 'f', 'c')"
        )
        indexes = await self.scalars(
            "SELECT i.relname FROM pg_index x "
            "JOIN pg_class i ON i.oid = x.indexrelid "
            "JOIN pg_class t ON t.oid = x.indrelid "
            "WHERE t.relname LIKE 'tool\\_approval%' AND NOT EXISTS "
            "(SELECT 1 FROM pg_constraint c WHERE c.conindid = x.indexrelid)"
        )
        self.assertEqual(set(constraints) | set(indexes), expected)
        for name in (
            "pk_tool_approvals",
            "fk_tool_approval_events_approval_id_tool_approvals",
            "ck_tool_approvals_approver_is_delegating_user",
            "uq_tool_approvals_open_call",
            "ix_tool_approval_events_approval_id",
        ):
            self.assertIn(name, expected)

    async def test_the_open_call_index_is_partial_and_unique(self):
        (definition,) = await self.scalars(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'uq_tool_approvals_open_call'"
        )
        self.assertIn("UNIQUE", definition)
        self.assertIn("(call_hash)", definition)
        self.assertIn("status", definition.split("WHERE", 1)[1])
        self.assertIn("'pending'", definition)
        self.assertIn("'approved'", definition)
        self.assertNotIn("'consumed'", definition)

    async def test_the_trigger_is_installed_on_the_history_table_only(self):
        triggers = await self.scalars(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
            "AND tgrelid = 'tool_approval_events'::regclass"
        )
        self.assertEqual(triggers, ["tool_approval_events_append_only"])
        none = await self.scalars(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
            "AND tgrelid = 'tool_approvals'::regclass"
        )
        self.assertEqual(none, [])


if __name__ == "__main__":
    unittest.main()
