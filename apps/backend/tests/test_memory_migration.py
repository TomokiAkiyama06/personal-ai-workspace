"""Revision 0040: models, migration and database must describe the same schema.

The first classes need no server. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the migration up and down, compare it with
the models through Alembic's autogenerate, and compare the two catalogs
(constraint definitions and partial indexes, which autogenerate does not see).
"""

import io
import unittest
from enum import StrEnum

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, create_engine, text

from paw_backend.db import Base
from paw_backend.memory import models
from paw_backend.memory.models import TABLE_NAMES

from .memory_support import (
    MEMORY_TABLES,
    migrate,
    requires_postgres,
    sync_database_url,
)
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0040"
SCRATCH_SCHEMA = "paw_drift_check"


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


class ModelsMetadataTest(unittest.TestCase):
    def memory_tables(self):
        return [Base.metadata.tables[name] for name in TABLE_NAMES]

    def test_the_schema_has_the_expected_tables(self):
        self.assertEqual(set(TABLE_NAMES), set(MEMORY_TABLES))

    def test_every_constraint_and_index_has_a_conventional_name(self):
        prefixes = {
            "PrimaryKeyConstraint": "pk_",
            "ForeignKeyConstraint": "fk_",
            "UniqueConstraint": "uq_",
            "CheckConstraint": "ck_",
        }
        for table in self.memory_tables():
            for constraint in table.constraints:
                if not constraint.columns and not isinstance(
                    constraint, CheckConstraint
                ):
                    continue
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

    def test_no_foreign_key_leaves_the_memory_schema(self):
        # Users, projects and repos are not tables yet: their ids are plain UUIDs.
        targets = set()
        for table in self.memory_tables():
            for constraint in table.constraints:
                if isinstance(constraint, ForeignKeyConstraint):
                    targets.add(constraint.referred_table.name)
        self.assertEqual(
            targets,
            {
                "conversations",
                "messages",
                "memories",
                "memory_versions",
                "embedding_models",
            },
        )

    def test_user_project_and_repo_ids_are_plain_uuid_columns(self):
        for table_name, column_name in [
            ("conversations", "owner_user_id"),
            ("conversations", "project_id"),
            ("conversations", "repo_id"),
            ("memory_versions", "owner_user_id"),
            ("memory_versions", "project_id"),
            ("memory_versions", "project_group_id"),
            ("memory_versions", "repo_id"),
            ("memory_versions", "actor_user_id"),
        ]:
            with self.subTest(table=table_name, column=column_name):
                column = Base.metadata.tables[table_name].columns[column_name]
                self.assertEqual(type(column.type).__name__, "Uuid")
                self.assertEqual(list(column.foreign_keys), [])

    def test_allowed_values_follow_the_design_documents(self):
        expected: dict[type[StrEnum], set[str]] = {
            models.MemoryScope: {"user", "project", "project_group", "repo", "shared"},
            models.MemoryStatus: {"active", "superseded", "deprecated", "history"},
            models.ConfirmationState: {
                "observed",
                "inferred",
                "confirmed",
                "rejected",
            },
            models.FreshnessPolicy: {
                "permanent",
                "revalidate",
                "repo_commit",
                "expiring",
                "session_only",
            },
            models.RelationType: {
                "supersedes",
                "extends",
                "conflicts_with",
                "confirmed_from",
                "revalidated_from",
                "merged_from",
            },
            models.SourceType: {
                "conversation",
                "task",
                "repo_analysis",
                "user_confirmation",
                "project_decision",
            },
            models.MessageRole: {"user", "assistant", "tool", "agent", "task"},
            models.ActorType: {"user", "agent", "system"},
        }
        for enum, values in expected.items():
            with self.subTest(enum.__name__):
                self.assertEqual({member.value for member in enum}, values)

    def test_the_embedding_column_has_no_fixed_dimension_and_no_ann_index(self):
        table = Base.metadata.tables["memory_embeddings"]
        self.assertEqual(type(table.columns["embedding"].type).__name__, "Vector")
        self.assertEqual(
            sorted(index.name for index in table.indexes),
            ["ix_memory_embeddings_embedding_model_id"],
        )


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_upgrade_enables_pgvector_before_the_tables_that_use_it(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")

        extension = sql.index("CREATE EXTENSION IF NOT EXISTS vector")
        self.assertLess(extension, sql.index("CREATE TABLE memory_embeddings"))
        self.assertIn("embedding vector NOT NULL", sql)
        self.assertNotIn("vector(", sql)
        for table in MEMORY_TABLES:
            self.assertIn(f"CREATE TABLE {table} ", sql)

    def test_upgrade_creates_no_approximate_index(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}").lower()

        self.assertNotIn("hnsw", sql)
        self.assertNotIn("ivfflat", sql)

    def test_downgrade_drops_the_tables_before_the_extension(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")

        self.assertLess(
            sql.index("DROP TABLE memory_embeddings"),
            sql.index("DROP EXTENSION IF EXISTS vector"),
        )
        for table in MEMORY_TABLES:
            self.assertIn(f"DROP TABLE {table};", sql)


def only_memory_objects(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in MEMORY_TABLES
    table = getattr(obj, "table", None)
    return table is None or table.name in MEMORY_TABLES


def catalog(connection, schema: str) -> dict[str, list[tuple]]:
    """Columns, constraints, indexes and triggers of the memory tables in ``schema``.

    Schema qualifiers are removed so that two schemas can be compared.
    """
    params = {"schema": schema, "tables": list(MEMORY_TABLES)}

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
        # Triggers are not part of the metadata Alembic compares: the models
        # create them with DDL events and the migration repeats the DDL.
        "triggers": rows(
            """
            SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid)
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema AND c.relname = ANY (:tables)
              AND NOT t.tgisinternal
            """
        ),
    }


@requires_postgres
class MemoryMigrationDatabaseTest(unittest.TestCase):
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

    def extensions(self) -> list[str]:
        return self.scalars("SELECT extname FROM pg_extension WHERE extname = 'vector'")

    def version(self) -> list[str]:
        return self.scalars("SELECT version_num FROM alembic_version")

    # -- up and down --------------------------------------------------------

    def test_upgrade_creates_the_schema_and_downgrade_removes_it_with_the_extension(
        self,
    ):
        previous = previous_revision()

        migrate("upgrade", REVISION)

        self.assertTrue(set(MEMORY_TABLES) <= self.tables())
        self.assertEqual(self.extensions(), ["vector"])
        self.assertEqual(self.version(), [REVISION])

        migrate("downgrade", previous)

        self.assertEqual(self.tables() & set(MEMORY_TABLES), set())
        self.assertEqual(self.extensions(), [])
        self.assertEqual(self.version(), [previous])
        leftovers = self.scalars(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname = 'public' AND indexname LIKE 'ix\\_memory\\_%'"
        )
        self.assertEqual(leftovers, [])
        self.assertEqual(
            self.scalars("SELECT count(*) FROM pg_type WHERE typname = 'vector'"), [0]
        )

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        previous = previous_revision()

        migrate("upgrade", REVISION)
        migrate("downgrade", previous)
        migrate("upgrade", REVISION)

        self.assertTrue(set(MEMORY_TABLES) <= self.tables())
        self.assertEqual(self.extensions(), ["vector"])

    def test_an_extension_created_beforehand_by_an_administrator_is_accepted(self):
        with self.engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        migrate("upgrade", REVISION)

        self.assertTrue(set(MEMORY_TABLES) <= self.tables())
        self.assertEqual(self.extensions(), ["vector"])

    def test_head_contains_the_schema(self):
        migrate("upgrade", "head")

        self.assertTrue(set(MEMORY_TABLES) <= self.tables())

    # -- drift --------------------------------------------------------------

    def autogenerate_diff(self, connection) -> list:
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_object": only_memory_objects,
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
                "DROP INDEX ix_memory_versions_one_active",
                "ALTER TABLE memory_versions ALTER COLUMN pinned DROP DEFAULT",
                # (not ``importance``: a trigger uses it, so its type cannot change)
                "ALTER TABLE memory_versions ALTER COLUMN version_number TYPE bigint",
                "ALTER TABLE memory_versions ADD COLUMN unexpected text",
                "ALTER TABLE memory_versions ALTER COLUMN title DROP NOT NULL",
            ):
                connection.execute(text(statement))
            diff = self.autogenerate_diff(connection)
            transaction.rollback()

        # Column-level changes come nested in a list per table.
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

        for kind in ("columns", "constraints", "indexes", "triggers"):
            with self.subTest(kind):
                self.assertEqual(migrated[kind], created[kind])
        # The comparison is not vacuous.
        names = {row[1] for row in migrated["constraints"]}
        self.assertIn("ck_memory_versions_status_valid", names)
        self.assertIn("ck_memory_versions_freshness_fields", names)
        self.assertIn("fk_memory_sources_conversation_id_conversations", names)
        # The rule of a non-conversation source's reference: the two schemas
        # agree on its text, and it is not the bare ``IS NOT NULL`` any more.
        reference_rules = {
            row[3]
            for row in migrated["constraints"]
            if row[1] == "ck_memory_sources_other_sources_have_reference"
        }
        self.assertEqual(len(reference_rules), 1)
        self.assertIn("char_length(source_ref) >= 1", reference_rules.pop())
        self.assertEqual(
            [row[1:] for row in migrated["triggers"]],
            [
                (
                    "tr_memory_sources_conversation_source_identified",
                    "CREATE TRIGGER tr_memory_sources_conversation_source_identified"
                    " BEFORE INSERT ON memory_sources FOR EACH ROW EXECUTE FUNCTION"
                    " paw_check_memory_source_conversation_identified()",
                ),
                (
                    "tr_memory_sources_message_requires_conversation",
                    "CREATE CONSTRAINT TRIGGER"
                    " tr_memory_sources_message_requires_conversation"
                    " AFTER INSERT OR UPDATE OF conversation_id, message_id"
                    " ON memory_sources DEFERRABLE INITIALLY DEFERRED"
                    " FOR EACH ROW EXECUTE FUNCTION"
                    " paw_check_memory_source_message_conversation()",
                ),
                (
                    "tr_memory_versions_record_metadata_change",
                    "CREATE TRIGGER tr_memory_versions_record_metadata_change"
                    " AFTER UPDATE OF pinned, importance, status, stale_since"
                    " ON memory_versions"
                    " FOR EACH ROW WHEN (((old.pinned IS DISTINCT FROM new.pinned)"
                    " OR (old.importance IS DISTINCT FROM new.importance)"
                    " OR (old.status IS DISTINCT FROM new.status)"
                    " OR (old.stale_since IS DISTINCT FROM new.stale_since)))"
                    " EXECUTE FUNCTION paw_record_memory_metadata_change()",
                ),
            ],
        )
        index_definitions = {row[1]: row[2] for row in migrated["indexes"]}
        self.assertIn(
            "WHERE (status = 'active'::text)",
            index_definitions["ix_memory_versions_one_active"],
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
                    "ALTER TABLE memory_versions"
                    " DROP CONSTRAINT ck_memory_versions_status_valid"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE memory_versions ADD CONSTRAINT"
                    " ck_memory_versions_status_valid"
                    " CHECK (status IN ('active', 'superseded'))"
                )
            )
            after = catalog(connection, "public")
            transaction.rollback()

        self.assertEqual(before["columns"], after["columns"])
        self.assertNotEqual(before["constraints"], after["constraints"])

    def test_the_catalog_check_notices_a_changed_trigger(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection, connection.begin() as transaction:
            before = catalog(connection, "public")
            connection.execute(
                text(
                    "DROP TRIGGER tr_memory_sources_message_requires_conversation"
                    " ON memory_sources"
                )
            )
            connection.execute(
                text(
                    "CREATE CONSTRAINT TRIGGER"
                    " tr_memory_sources_message_requires_conversation"
                    " AFTER INSERT ON memory_sources"
                    " FOR EACH ROW EXECUTE FUNCTION"
                    " paw_check_memory_source_message_conversation()"
                )
            )
            after = catalog(connection, "public")
            transaction.rollback()

        self.assertNotEqual(before["constraints"], after["constraints"])
        self.assertNotEqual(before["triggers"], after["triggers"])

    def function_definition(self, connection, name: str) -> str:
        return connection.execute(
            text(
                "SELECT pg_get_functiondef(p.oid) FROM pg_proc p"
                " WHERE p.pronamespace = 'public'::regnamespace"
                "   AND p.proname = :name"
            ),
            {"name": name},
        ).scalar_one()

    def test_the_trigger_functions_of_the_migration_equal_the_models_definitions(self):
        migrate("upgrade", "head")
        # name -> (the models' DDL, a fragment that proves the body is compared)
        functions = {
            "paw_check_memory_source_message_conversation": (
                models.MESSAGE_REQUIRES_CONVERSATION_FUNCTION,
                "message_id IS NOT NULL AND conversation_id IS NULL",
            ),
            "paw_check_memory_source_conversation_identified": (
                models.CONVERSATION_SOURCE_IDENTIFIED_FUNCTION,
                "NEW.conversation_id IS NULL AND NEW.message_id IS NULL",
            ),
            "paw_record_memory_metadata_change": (
                models.RECORD_METADATA_CHANGE_FUNCTION,
                "OLD.pinned, NEW.pinned, OLD.importance, NEW.importance,"
                "\n        OLD.status, NEW.status, OLD.stale_since, NEW.stale_since",
            ),
        }

        for name, (model_ddl, fragment) in functions.items():
            with self.subTest(name):
                with (
                    self.engine.connect() as connection,
                    connection.begin() as transaction,
                ):
                    migrated = self.function_definition(connection, name)
                    connection.execute(text(model_ddl))
                    from_models = self.function_definition(connection, name)
                    transaction.rollback()

                self.assertEqual(migrated, from_models)
                self.assertIn(fragment, migrated)

    def test_every_trigger_function_of_the_memory_tables_pins_its_search_path(self):
        # A temporary table (every role may create one) is searched first by a
        # name without a schema, and a trigger function runs in the writer's
        # session: a function that leaves the path open can be pointed at a
        # look-alike. ``pg_temp`` must be named, and last (otherwise it comes
        # first, also for types).
        migrate("upgrade", "head")

        with self.engine.connect() as connection:
            functions = {
                name: config
                for name, config in connection.execute(
                    text(
                        "SELECT DISTINCT p.proname, p.proconfig"
                        " FROM pg_trigger t"
                        " JOIN pg_class c ON c.oid = t.tgrelid"
                        " JOIN pg_proc p ON p.oid = t.tgfoid"
                        " WHERE c.relnamespace = 'public'::regnamespace"
                        "   AND c.relname = ANY (:tables) AND NOT t.tgisinternal"
                    ),
                    {"tables": list(MEMORY_TABLES)},
                )
            }

        self.assertEqual(
            functions,
            {
                "paw_check_memory_source_conversation_identified": [
                    "search_path=pg_catalog, pg_temp"
                ],
                "paw_check_memory_source_message_conversation": [
                    "search_path=pg_catalog, pg_temp"
                ],
                "paw_record_memory_metadata_change": [
                    "search_path=pg_catalog, pg_temp"
                ],
            },
        )

    def test_downgrade_drops_the_trigger_functions(self):
        migrate("upgrade", "head")
        migrate("downgrade", "base")

        with self.engine.connect() as connection:
            leftovers = connection.execute(
                text(
                    "SELECT proname FROM pg_proc"
                    " WHERE pronamespace = 'public'::regnamespace"
                    "   AND (proname LIKE 'paw\\_check\\_memory%'"
                    "        OR proname LIKE 'paw\\_record\\_memory%')"
                )
            ).scalars()
            self.assertEqual(list(leftovers), [])


if __name__ == "__main__":
    unittest.main()
