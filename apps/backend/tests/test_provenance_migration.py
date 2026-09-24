"""Revision 0052: models, migration and database must describe the same schema.

The first classes need no server. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the migration up and down, compare it with
the models through Alembic's autogenerate, and compare the two catalogs
(constraint definitions and indexes).
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
from paw_backend.research.provenance import limits
from paw_backend.research.provenance.models import TABLE_NAMES
from paw_backend.research.provenance.records import (
    EntityKind,
    ReferenceKind,
    RelationKind,
    Stance,
)
from paw_backend.research.providers.contract import SourceType

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0052"
PROVENANCE_SCHEMA = "paw_provenance_drift_check"
SOURCES, CLAIMS, CLAIM_SOURCES, USES, CLAIM_RELATIONS, SOURCE_RELATIONS = TABLE_NAMES


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


def literals(sql: str) -> set[str]:
    return set(re.findall(r"'(\w+)'", sql))


class ModelsMetadataTest(unittest.TestCase):
    def provenance_tables(self):
        return [Base.metadata.tables[name] for name in TABLE_NAMES]

    def test_the_schema_has_the_expected_tables(self):
        self.assertEqual(
            TABLE_NAMES,
            (
                "research_sources",
                "research_claims",
                "research_claim_sources",
                "research_claim_uses",
                "research_claim_relations",
                "research_source_relations",
            ),
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
        for table in self.provenance_tables():
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

    def test_foreign_keys_are_composite_with_the_project_except_the_task(self):
        found = {}
        for table in self.provenance_tables():
            for constraint in table.constraints:
                if isinstance(constraint, ForeignKeyConstraint):
                    found[constraint.name] = (
                        [column.name for column in constraint.columns],
                        sorted(
                            f"{element.column.table.name}.{element.column.name}"
                            for element in constraint.elements
                        ),
                        constraint.ondelete,
                    )
        self.assertEqual(
            found,
            {
                "fk_research_claims_task_id_tasks": (
                    ["task_id"],
                    ["tasks.id"],
                    "SET NULL",
                ),
                "fk_research_claim_sources_claim_id_research_claims": (
                    ["claim_id", "project_id"],
                    ["research_claims.id", "research_claims.project_id"],
                    None,
                ),
                "fk_research_claim_sources_source_id_research_sources": (
                    ["source_id", "project_id"],
                    ["research_sources.id", "research_sources.project_id"],
                    None,
                ),
                "fk_research_claim_uses_claim_id_research_claims": (
                    ["claim_id", "project_id"],
                    ["research_claims.id", "research_claims.project_id"],
                    None,
                ),
                "fk_research_claim_relations_low_id_research_claims": (
                    ["low_id", "project_id"],
                    ["research_claims.id", "research_claims.project_id"],
                    None,
                ),
                "fk_research_claim_relations_high_id_research_claims": (
                    ["high_id", "project_id"],
                    ["research_claims.id", "research_claims.project_id"],
                    None,
                ),
                "fk_research_source_relations_low_id_research_sources": (
                    ["low_id", "project_id"],
                    ["research_sources.id", "research_sources.project_id"],
                    None,
                ),
                "fk_research_source_relations_high_id_research_sources": (
                    ["high_id", "project_id"],
                    ["research_sources.id", "research_sources.project_id"],
                    None,
                ),
            },
        )

    def test_project_and_user_ids_are_plain_uuid_columns(self):
        for table in self.provenance_tables():
            for name in ("project_id", "created_by", "ref_id"):
                if name in table.columns:
                    with self.subTest(table=table.name, column=name):
                        self.assertEqual(
                            type(table.columns[name].type).__name__, "Uuid"
                        )
        # No table exists for users, answers or projects: nothing to point at.
        for table in self.provenance_tables():
            for name in ("created_by", "ref_id"):
                if name in table.columns:
                    self.assertEqual(list(table.columns[name].foreign_keys), [])
        for name in (SOURCES, CLAIMS):
            self.assertEqual(
                list(Base.metadata.tables[name].columns["project_id"].foreign_keys), []
            )

    def test_created_at_has_no_default_so_the_service_clock_decides(self):
        for name in TABLE_NAMES:
            with self.subTest(name):
                column = Base.metadata.tables[name].columns["created_at"]
                self.assertIsNone(column.server_default)
                self.assertFalse(column.nullable)

    def test_the_sources_table_stores_no_content(self):
        columns = {column.name for column in Base.metadata.tables[SOURCES].columns}
        self.assertEqual(
            columns,
            {
                "id",
                "project_id",
                "locator",
                "source_type",
                "title",
                "content_hash",
                "fetched_at",
                "published_at",
                "created_at",
            },
        )

    def test_the_database_limits_match_the_service_limits(self):
        self.assertIn(
            f"BETWEEN 1 AND {limits.MAX_CLAIM_TEXT_CHARS}",
            check_sql(CLAIMS)["claim_text_length"],
        )
        self.assertIn(
            f"<= {limits.MAX_SOURCE_TITLE_CHARS}", check_sql(SOURCES)["title_length"]
        )
        self.assertIn(
            f"BETWEEN 1 AND {limits.MAX_SOURCE_LOCATOR_CHARS}",
            check_sql(SOURCES)["locator_length"],
        )
        self.assertEqual(limits.MAX_CLAIM_TEXT_CHARS, 2000)
        self.assertEqual(limits.MAX_SOURCE_TITLE_CHARS, 300)
        self.assertEqual(limits.MAX_SOURCE_LOCATOR_CHARS, 2048)

    def test_the_allowed_values_of_the_database_are_those_of_the_service(self):
        expected = {
            (SOURCES, "source_type_valid"): {member.value for member in SourceType},
            (CLAIM_SOURCES, "stance_valid"): {member.value for member in Stance},
            (USES, "ref_kind_valid"): {member.value for member in ReferenceKind},
            (CLAIM_RELATIONS, "kind_valid"): {member.value for member in RelationKind},
            (SOURCE_RELATIONS, "kind_valid"): {member.value for member in RelationKind},
        }
        for (table, name), values in expected.items():
            with self.subTest(table=table, constraint=name):
                self.assertEqual(literals(check_sql(table)[name]), values)
        self.assertEqual({member.value for member in EntityKind}, {"claim", "source"})


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_the_revision_follows_0046(self):
        self.assertEqual(previous_revision(), "0046")

    def test_upgrade_creates_the_six_tables_parents_first(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")

        positions = [sql.index(f"CREATE TABLE {name} ") for name in TABLE_NAMES]
        self.assertEqual(positions, sorted(positions))
        for forbidden in ("memories", "memory_versions", "conversations", "vector"):
            self.assertNotIn(forbidden, sql)

    def test_downgrade_drops_the_children_before_their_parents(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")

        positions = [sql.index(f"DROP TABLE {name};") for name in TABLE_NAMES]
        self.assertEqual(positions, sorted(positions, reverse=True))


def only_provenance_objects(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in TABLE_NAMES
    table = getattr(obj, "table", None)
    return table is None or table.name in TABLE_NAMES


def catalog(connection, schema: str) -> dict[str, list[tuple]]:
    """Columns, constraints and indexes of the provenance tables in ``schema``.

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
class ProvenanceMigrationDatabaseTest(unittest.TestCase):
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
            " AND (tablename LIKE 'research\\_claim%'"
            " OR tablename LIKE 'research\\_source%')"
        )
        self.assertEqual(leftovers, [])
        # The migration touches nothing of the layers below it.
        self.assertIn("research_scratch_items", self.tables())
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
                "include_object": only_provenance_objects,
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
                "DROP INDEX ix_research_claim_uses_reference",
                "ALTER TABLE research_sources ALTER COLUMN title DROP DEFAULT",
                "ALTER TABLE research_claims ADD COLUMN unexpected text",
                "ALTER TABLE research_claims ALTER COLUMN task_id SET NOT NULL",
                "ALTER TABLE research_claim_sources"
                " ALTER COLUMN created_at TYPE timestamp",
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

    def provenance_catalog(self) -> dict[str, list[tuple]]:
        """The catalog of a schema built from the models with ``create_all``."""
        # The tasks table is created too: the claims table has a foreign key to it.
        tables = [Base.metadata.tables["tasks"]] + [
            Base.metadata.tables[name] for name in TABLE_NAMES
        ]
        with self.engine.begin() as connection:
            connection.execute(
                text(f"DROP SCHEMA IF EXISTS {PROVENANCE_SCHEMA} CASCADE")
            )
            connection.execute(text(f"CREATE SCHEMA {PROVENANCE_SCHEMA}"))
        try:
            with self.engine.begin() as connection:
                scoped = connection.execution_options(
                    schema_translate_map={None: PROVENANCE_SCHEMA}
                )
                Base.metadata.create_all(scoped, tables=tables)
            with self.engine.connect() as connection:
                return catalog(connection, PROVENANCE_SCHEMA)
        finally:
            with self.engine.begin() as connection:
                connection.execute(
                    text(f"DROP SCHEMA IF EXISTS {PROVENANCE_SCHEMA} CASCADE")
                )

    def test_the_migration_and_the_models_produce_the_same_catalog(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection:
            migrated = catalog(connection, "public")
        created = self.provenance_catalog()

        for kind in ("columns", "constraints", "indexes"):
            with self.subTest(kind):
                self.assertEqual(migrated[kind], created[kind])
        # The comparison is not vacuous.
        names = {row[1] for row in migrated["constraints"]}
        for expected in (
            "ck_research_sources_content_hash_format",
            "ck_research_sources_locator_shape",
            "ck_research_claims_claim_text_length",
            "ck_research_claim_relations_ordered_pair",
            "uq_research_sources_project_id",
            "uq_research_sources_id",
            "uq_research_claims_project_id",
            "uq_research_claims_id",
            "fk_research_claims_task_id_tasks",
            "fk_research_claim_sources_claim_id_research_claims",
            "fk_research_source_relations_high_id_research_sources",
        ):
            self.assertIn(expected, names)
        self.assertEqual(
            {row[1] for row in migrated["indexes"] if row[1].startswith("ix_")},
            {
                "ix_research_claims_task_id",
                "ix_research_claim_sources_source_id",
                "ix_research_claim_uses_reference",
                "ix_research_claim_relations_high_id",
                "ix_research_source_relations_high_id",
            },
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
                    "ALTER TABLE research_claim_relations"
                    " DROP CONSTRAINT ck_research_claim_relations_ordered_pair"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE research_claim_relations ADD CONSTRAINT"
                    " ck_research_claim_relations_ordered_pair"
                    " CHECK (low_id <> high_id)"
                )
            )
            after = catalog(connection, "public")
            transaction.rollback()

        self.assertEqual(before["columns"], after["columns"])
        self.assertNotEqual(before["constraints"], after["constraints"])


if __name__ == "__main__":
    unittest.main()
