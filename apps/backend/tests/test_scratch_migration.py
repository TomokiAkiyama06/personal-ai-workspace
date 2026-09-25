"""Revision 0050: models, migration and database must describe the same schema.

The first classes need no server. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the migration up and down, compare it with
the models through Alembic's autogenerate, and compare the two catalogs
(constraint definitions and partial indexes, which autogenerate does not see).
"""

import io
import re
import unittest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, create_engine, text

from paw_backend.db import Base
from paw_backend.research.scratch import limits
from paw_backend.research.scratch.models import TABLE_NAMES
from paw_backend.research.scratch.records import PromotionState

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0050"
SCRATCH_SCHEMA = "paw_scratch_drift_check"
ITEMS, LEASES = TABLE_NAMES


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


def check_sql(table: str) -> dict[str, str]:
    """The text of every CHECK constraint of a model table, by its short name."""
    return {
        constraint.name.removeprefix(f"ck_{table}_"): str(constraint.sqltext)
        for constraint in Base.metadata.tables[table].constraints
        if isinstance(constraint, CheckConstraint)
    }


class ModelsMetadataTest(unittest.TestCase):
    def scratch_tables(self):
        return [Base.metadata.tables[name] for name in TABLE_NAMES]

    def test_the_schema_has_the_expected_tables(self):
        self.assertEqual(
            TABLE_NAMES, ("research_scratch_items", "research_scratch_leases")
        )
        for name in TABLE_NAMES:
            self.assertIn(name, Base.metadata.tables)

    def test_every_constraint_and_index_has_a_conventional_name(self):
        prefixes = {
            "PrimaryKeyConstraint": "pk_",
            "ForeignKeyConstraint": "fk_",
            "UniqueConstraint": "uq_",
            "CheckConstraint": "ck_",
        }
        for table in self.scratch_tables():
            for constraint in table.constraints:
                with self.subTest(table=table.name, constraint=constraint.name):
                    name = constraint.name
                    self.assertIsNotNone(name)
                    prefix = prefixes[type(constraint).__name__]
                    self.assertTrue(name.startswith(prefix + table.name))
                    self.assertLessEqual(len(name), 63)
            for index in table.indexes:
                with self.subTest(table=table.name, index=index.name):
                    self.assertTrue(index.name.startswith(f"ix_{table.name}_"))
                    self.assertLessEqual(len(index.name), 63)

    def test_the_only_foreign_key_is_from_a_lease_to_its_item(self):
        # Decision 0013: task_id is a plain UUID, not a foreign key to tasks
        # (SET NULL lost the relation, RESTRICT blocked deleting a task, CASCADE
        # deleted pinned research).
        targets = {}
        for table in self.scratch_tables():
            for constraint in table.constraints:
                if isinstance(constraint, ForeignKeyConstraint):
                    targets[(table.name, constraint.referred_table.name)] = (
                        constraint.ondelete
                    )
        self.assertEqual(targets, {(LEASES, ITEMS): "CASCADE"})

    def test_project_task_and_user_ids_are_plain_uuid_columns(self):
        items = Base.metadata.tables[ITEMS]
        for column_name in ("project_id", "task_id", "created_by"):
            with self.subTest(column_name):
                column = items.columns[column_name]
                self.assertEqual(type(column.type).__name__, "Uuid")
                self.assertEqual(list(column.foreign_keys), [])
        self.assertEqual(
            list(Base.metadata.tables[LEASES].columns["holder_id"].foreign_keys), []
        )

    def test_expires_at_has_no_default_so_it_cannot_drift_from_created_at(self):
        items = Base.metadata.tables[ITEMS]
        for name in ("created_at", "expires_at"):
            self.assertIsNone(items.columns[name].server_default)
            self.assertIsNone(items.columns[name].default)
            self.assertFalse(items.columns[name].nullable)

    def test_the_ttl_and_the_text_limits_of_the_database_match_the_service_limits(self):
        items = check_sql(ITEMS)
        self.assertEqual(limits.SCRATCH_TTL.total_seconds(), 24 * 3600)
        self.assertIn("interval '24 hours'", items["expires_at_matches_ttl"])
        for name, limit in [
            ("query_length", limits.MAX_QUERY_CHARS),
            ("title_length", limits.MAX_TITLE_CHARS),
            ("summary_length", limits.MAX_SUMMARY_CHARS),
            ("content_length", limits.MAX_CONTENT_CHARS),
        ]:
            with self.subTest(name):
                self.assertIn(f"BETWEEN 1 AND {limit}", items[name])
        self.assertEqual(limits.MAX_LEASE_SECONDS, 3600)
        self.assertIn(
            "leased_at + interval '1 hour'", check_sql(LEASES)["lease_window"]
        )
        backstop = int(re.search(r"<= (\d+)", items["source_metadata_size"]).group(1))
        self.assertGreaterEqual(backstop, 4 * limits.MAX_SOURCE_METADATA_BYTES)

    def test_the_promotion_states_of_the_database_are_those_of_the_service(self):
        listed = set(re.findall(r"'(\w+)'", check_sql(ITEMS)["promotion_state_valid"]))
        self.assertEqual(listed, {state.value for state in PromotionState})


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_upgrade_creates_the_two_tables_and_no_memory_object(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")

        self.assertLess(
            sql.index(f"CREATE TABLE {ITEMS} "), sql.index(f"CREATE TABLE {LEASES} ")
        )
        self.assertIn("expires_at = created_at + interval '24 hours'", sql)
        for forbidden in ("memories", "memory_versions", "conversations", "vector"):
            self.assertNotIn(forbidden, sql)

    def test_downgrade_drops_the_leases_before_the_items(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")

        self.assertLess(
            sql.index(f"DROP TABLE {LEASES};"), sql.index(f"DROP TABLE {ITEMS};")
        )


def only_scratch_objects(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in TABLE_NAMES
    table = getattr(obj, "table", None)
    return table is None or table.name in TABLE_NAMES


def catalog(connection, schema: str) -> dict[str, list[tuple]]:
    """Columns, constraints and indexes of the scratch tables in ``schema``.

    Schema qualifiers are removed so that two schemas can be compared.
    """
    params = {"schema": schema, "tables": list(TABLE_NAMES)}

    def clean(value):
        return value.replace(f"{schema}.", "") if isinstance(value, str) else value

    def rows(sql: str) -> list[tuple]:
        result = connection.execute(text(sql), params)
        return sorted(tuple(clean(v) for v in row) for row in result)

    return {
        "columns": rows(
            """
            SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
                   a.attnotnull, pg_get_expr(d.adbin, d.adrelid)
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE n.nspname = :schema AND c.relname = ANY (:tables)
              AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
            """
        ),
        "constraints": rows(
            """
            SELECT c.relname, con.conname, con.contype::text,
                   pg_get_constraintdef(con.oid)
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema AND c.relname = ANY (:tables)
            """
        ),
        "indexes": rows(
            """
            SELECT tablename, indexname, indexdef FROM pg_indexes
            WHERE schemaname = :schema AND tablename = ANY (:tables)
            """
        ),
    }


@requires_postgres
class ScratchMigrationDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        migrate("downgrade", "base")
        self.addCleanup(migrate, "downgrade", "base")
        self.engine = create_engine(sync_database_url())
        self.addCleanup(self.engine.dispose)

    def scalars(self, sql: str, **params) -> list:
        with self.engine.connect() as connection:
            return list(connection.execute(text(sql), params).scalars())

    def tables(self) -> set[str]:
        return set(
            self.scalars("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )

    def version(self) -> list[str]:
        return self.scalars("SELECT version_num FROM alembic_version")

    # -- up and down --------------------------------------------------------

    def test_upgrade_creates_the_schema_and_downgrade_removes_it(self):
        previous = previous_revision()

        migrate("upgrade", REVISION)

        self.assertTrue(set(TABLE_NAMES) <= self.tables())
        self.assertEqual(self.version(), [REVISION])

        migrate("downgrade", previous)

        self.assertEqual(self.tables() & set(TABLE_NAMES), set())
        self.assertEqual(self.version(), [previous])
        leftovers = self.scalars(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            " AND tablename LIKE 'research\\_scratch\\_%'"
        )
        self.assertEqual(leftovers, [])
        # The migration touches nothing of the layers below it.
        self.assertIn("memory_versions", self.tables())
        self.assertIn("tasks", self.tables())

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        previous = previous_revision()

        migrate("upgrade", REVISION)
        migrate("downgrade", previous)
        migrate("upgrade", REVISION)

        self.assertTrue(set(TABLE_NAMES) <= self.tables())

    def test_head_contains_the_schema(self):
        migrate("upgrade", "head")

        self.assertTrue(set(TABLE_NAMES) <= self.tables())

    def test_the_downgrade_leaves_the_tasks_table_alone(self):
        migrate("upgrade", REVISION)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tasks (id, project_id, created_by, title, input,"
                    " state, attempt, retry_count, version, created_at, updated_at)"
                    " VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(),"
                    " 'T', '{}', 'queued', 1, 0, 1, now(), now())"
                )
            )

        migrate("downgrade", previous_revision())

        self.assertEqual(self.scalars("SELECT count(*) FROM tasks"), [1])

    # -- drift --------------------------------------------------------------

    def autogenerate_diff(self, connection) -> list:
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_object": only_scratch_objects,
            },
        )
        return compare_metadata(context, Base.metadata)

    def test_alembic_autogenerate_finds_no_difference_between_models_and_migration(
        self,
    ):
        migrate("upgrade", "head")

        with self.engine.connect() as connection:
            self.assertEqual(self.autogenerate_diff(connection), [])

    def test_the_autogenerate_check_notices_a_schema_that_drifted(self):
        migrate("upgrade", "head")

        with self.engine.connect() as connection, connection.begin() as transaction:
            for statement in (
                "DROP INDEX ix_research_scratch_items_purgeable",
                "ALTER TABLE research_scratch_items ALTER COLUMN pinned DROP DEFAULT",
                "ALTER TABLE research_scratch_items ADD COLUMN unexpected text",
                "ALTER TABLE research_scratch_items ALTER COLUMN summary SET NOT NULL",
                "ALTER TABLE research_scratch_leases"
                " ALTER COLUMN leased_at TYPE timestamp",
            ):
                connection.execute(text(statement))
            diff = self.autogenerate_diff(connection)
            transaction.rollback()

        operations = [
            step
            for entry in diff
            for step in (entry if isinstance(entry, list) else [entry])
        ]
        self.assertEqual(
            sorted({operation[0] for operation in operations}),
            [
                "add_index",
                "modify_default",
                "modify_nullable",
                "modify_type",
                "remove_column",
            ],
        )

    def scratch_catalog(self) -> dict[str, list[tuple]]:
        """The catalog of a schema built from the models with ``create_all``."""
        tables = [Base.metadata.tables[name] for name in TABLE_NAMES]
        with self.engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {SCRATCH_SCHEMA} CASCADE"))
            connection.execute(text(f"CREATE SCHEMA {SCRATCH_SCHEMA}"))
        try:
            with self.engine.begin() as connection:
                scoped = connection.execution_options(
                    schema_translate_map={None: SCRATCH_SCHEMA}
                )
                Base.metadata.create_all(scoped, tables=tables)
            with self.engine.connect() as connection:
                return catalog(connection, SCRATCH_SCHEMA)
        finally:
            with self.engine.begin() as connection:
                connection.execute(
                    text(f"DROP SCHEMA IF EXISTS {SCRATCH_SCHEMA} CASCADE")
                )

    def test_the_migration_and_the_models_produce_the_same_catalog(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection:
            migrated = catalog(connection, "public")
        created = self.scratch_catalog()

        for kind in ("columns", "constraints", "indexes"):
            with self.subTest(kind):
                self.assertEqual(migrated[kind], created[kind])
        # The comparison is not vacuous.
        names = {row[1] for row in migrated["constraints"]}
        self.assertIn("ck_research_scratch_items_expires_at_matches_ttl", names)
        self.assertIn("ck_research_scratch_leases_lease_window", names)
        self.assertNotIn("fk_research_scratch_items_task_id_tasks", names)
        self.assertEqual(
            {name for name in names if name.startswith("fk_")},
            {"fk_research_scratch_leases_item_id_research_scratch_items"},
        )
        self.assertIn(
            "fk_research_scratch_leases_item_id_research_scratch_items", names
        )
        index_definitions = {row[1]: row[2] for row in migrated["indexes"]}
        self.assertIn(
            "WHERE ((NOT pinned) AND (NOT saved)"
            " AND (promotion_state <> 'pending'::text))",
            index_definitions["ix_research_scratch_items_purgeable"],
        )
        self.assertEqual(
            len(migrated["columns"]),
            sum(len(Base.metadata.tables[name].columns) for name in TABLE_NAMES),
        )

    def test_the_catalog_check_notices_a_changed_check_constraint(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection, connection.begin() as transaction:
            before = catalog(connection, "public")
            connection.execute(
                text(
                    "ALTER TABLE research_scratch_items"
                    " DROP CONSTRAINT ck_research_scratch_items_expires_at_matches_ttl"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE research_scratch_items ADD CONSTRAINT"
                    " ck_research_scratch_items_expires_at_matches_ttl"
                    " CHECK (expires_at = created_at + interval '48 hours')"
                )
            )
            after = catalog(connection, "public")
            transaction.rollback()

        self.assertEqual(before["columns"], after["columns"])
        self.assertNotEqual(before["constraints"], after["constraints"])


if __name__ == "__main__":
    unittest.main()
