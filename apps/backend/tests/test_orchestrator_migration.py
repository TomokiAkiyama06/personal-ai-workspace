"""Revision 0034: models, migration and database must describe the same schema.

The first classes need no server. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the migration up and down, compare it with
the models through Alembic's autogenerate and compare the two catalogs (constraint
definitions, which autogenerate does not see), and check that the limits written
into the database are the ones of ``orchestrator.limits`` and the enums.
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
from paw_backend.orchestrator import limits
from paw_backend.orchestrator.domain import AttemptState, DagState, NodeRole, NodeState
from paw_backend.orchestrator.models import TABLE_NAMES
from paw_backend.tasks.queueing.validation import MAX_APPROACH

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0034"
SCHEMA = "paw_orchestrator_drift_check"
DAGS, NODES, EDGES, ATTEMPTS = TABLE_NAMES


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
    def tables(self):
        return [Base.metadata.tables[name] for name in TABLE_NAMES]

    def test_the_schema_has_the_expected_tables(self):
        self.assertEqual(
            TABLE_NAMES,
            (
                "agent_dags",
                "agent_dag_nodes",
                "agent_dag_edges",
                "agent_dag_node_attempts",
            ),
        )
        for name in TABLE_NAMES:
            self.assertIn(name, Base.metadata.tables)
            self.assertFalse(name.startswith("task"), name)

    def test_every_constraint_and_index_has_a_conventional_name(self):
        prefixes = {
            "PrimaryKeyConstraint": "pk_",
            "ForeignKeyConstraint": "fk_",
            "UniqueConstraint": "uq_",
            "CheckConstraint": "ck_",
        }
        for table in self.tables():
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

    def test_the_foreign_keys_are_the_real_ones(self):
        targets = {}
        for table in self.tables():
            for constraint in table.constraints:
                if isinstance(constraint, ForeignKeyConstraint):
                    targets.setdefault(table.name, []).append(
                        (
                            constraint.referred_table.name,
                            tuple(c.name for c in constraint.columns),
                            constraint.ondelete,
                        )
                    )
        self.assertEqual(
            {name: sorted(found) for name, found in targets.items()},
            {
                DAGS: [("tasks", ("task_id",), None)],
                NODES: [(DAGS, ("dag_id",), None)],
                EDGES: [
                    (NODES, ("dag_id", "depends_on_key"), None),
                    (NODES, ("dag_id", "node_key"), None),
                ],
                ATTEMPTS: [(NODES, ("dag_id", "node_key"), None)],
            },
        )

    def test_the_enum_lists_of_the_database_are_those_of_the_code(self):
        for table, column, enum_class in (
            (DAGS, "state_valid", DagState),
            (NODES, "state_valid", NodeState),
            (NODES, "role_valid", NodeRole),
            (ATTEMPTS, "state_valid", AttemptState),
        ):
            with self.subTest(table=table, check=column):
                listed = set(re.findall(r"'(\w+)'", check_sql(table)[column]))
                self.assertEqual(listed, {member.value for member in enum_class})

    def test_the_limits_of_the_database_are_those_of_the_service(self):
        nodes = check_sql(NODES)
        self.assertIn(f"'^{limits.KEY_PATTERN}$'", nodes["key_format"])
        self.assertIn(f"<= {limits.MAX_GOAL_CHARS}", nodes["goal_length"])
        self.assertIn(
            f"BETWEEN 0 AND {limits.MAX_LADDER_LENGTH - 1}",
            nodes["agent_index_in_range"],
        )
        self.assertIn(f"BETWEEN 0 AND {MAX_APPROACH}", nodes["approach_in_range"])
        self.assertIn(f"<= {limits.DB_MAX_JSON_BYTES}", nodes["result_bounded"])
        self.assertIn(f"<= {limits.DB_MAX_JSON_BYTES}", nodes["input_bounded"])
        self.assertIn(
            f"BETWEEN 1 AND {limits.MAX_NODES}", check_sql(DAGS)["node_count_in_range"]
        )
        # The database is the backstop: it accepts everything the service accepts.
        self.assertGreaterEqual(limits.DB_MAX_JSON_BYTES, limits.MAX_RESULT_BYTES)
        self.assertGreaterEqual(limits.DB_MAX_JSON_BYTES, limits.MAX_NODE_INPUT_BYTES)
        title_length = Base.metadata.tables[NODES].columns["title"].type.length
        self.assertEqual(title_length, limits.MAX_TITLE_CHARS)
        key_length = Base.metadata.tables[NODES].columns["key"].type.length
        self.assertEqual(key_length, limits.MAX_KEY_LENGTH)
        self.assertEqual(
            Base.metadata.tables[NODES].columns["error_class"].type.length,
            limits.MAX_ERROR_CLASS_CHARS,
        )

    def test_the_migration_file_repeats_the_same_limits(self):
        scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
        module = scripts.get_revision(REVISION).module
        self.assertEqual(module.MAX_NODES, limits.MAX_NODES)
        self.assertEqual(module.MAX_LADDER_LENGTH, limits.MAX_LADDER_LENGTH)
        self.assertEqual(module.MAX_APPROACH, MAX_APPROACH)
        self.assertEqual(module.MAX_GOAL_CHARS, limits.MAX_GOAL_CHARS)
        self.assertEqual(module.DB_MAX_JSON_BYTES, limits.DB_MAX_JSON_BYTES)
        self.assertEqual(module.KEY_PATTERN, limits.KEY_PATTERN)
        for name, enum_class in (
            ("DAG_STATES", DagState),
            ("NODE_STATES", NodeState),
            ("NODE_ROLES", NodeRole),
            ("ATTEMPT_STATES", AttemptState),
        ):
            self.assertEqual(
                set(getattr(module, name)), {m.value for m in enum_class}, name
            )
        self.assertEqual(module.down_revision, "0026")


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_upgrade_creates_the_four_tables_parents_first(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")

        positions = [sql.index(f"CREATE TABLE {name} ") for name in TABLE_NAMES]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("REFERENCES tasks (id)", sql)

    def test_downgrade_drops_the_children_before_their_parents(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")

        positions = [sql.index(f"DROP TABLE {name};") for name in reversed(TABLE_NAMES)]
        self.assertEqual(positions, sorted(positions))

    def test_the_revision_follows_the_current_head(self):
        self.assertEqual(previous_revision(), "0026")


def only_orchestrator_objects(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in TABLE_NAMES
    table = getattr(obj, "table", None)
    return table is None or table.name in TABLE_NAMES


def catalog(connection, schema: str) -> dict[str, list[tuple]]:
    """Columns, constraints and indexes of the tables in ``schema``. Schema
    qualifiers are removed so that two schemas can be compared."""
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
class OrchestratorMigrationDatabaseTest(unittest.TestCase):
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
            " AND tablename LIKE 'agent\\_dag%'"
        )
        self.assertEqual(leftovers, [])
        # The migration touches nothing of the layers below it.
        self.assertIn("projects", self.tables())
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
                "include_object": only_orchestrator_objects,
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
                "ALTER TABLE agent_dag_nodes ALTER COLUMN approach DROP DEFAULT",
                "ALTER TABLE agent_dag_nodes ADD COLUMN unexpected text",
                "ALTER TABLE agent_dag_nodes ALTER COLUMN error_class SET NOT NULL",
                "ALTER TABLE agent_dags ALTER COLUMN owner TYPE text",
                "ALTER TABLE agent_dag_node_attempts DROP COLUMN epoch",
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
                "add_column",
                "modify_default",
                "modify_nullable",
                "modify_type",
                "remove_column",
            ],
        )

    def created_catalog(self) -> dict[str, list[tuple]]:
        """The catalog of a schema built from the models with ``create_all``."""
        tables = [Base.metadata.tables["tasks"]] + [
            Base.metadata.tables[name] for name in TABLE_NAMES
        ]
        with self.engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
            connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        try:
            with self.engine.begin() as connection:
                scoped = connection.execution_options(
                    schema_translate_map={None: SCHEMA}
                )
                Base.metadata.create_all(scoped, tables=tables)
            with self.engine.connect() as connection:
                return catalog(connection, SCHEMA)
        finally:
            with self.engine.begin() as connection:
                connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))

    def test_the_migration_and_the_models_produce_the_same_catalog(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection:
            migrated = catalog(connection, "public")
        created = self.created_catalog()

        for kind in ("columns", "constraints", "indexes"):
            with self.subTest(kind):
                self.assertEqual(migrated[kind], created[kind])
        # The comparison is not vacuous.
        names = {row[1] for row in migrated["constraints"]}
        for expected in (
            "ck_agent_dag_nodes_result_bounded",
            "ck_agent_dag_nodes_result_matches_state",
            "ck_agent_dags_owner_matches_epoch",
            "uq_agent_dags_task_id",
            "fk_agent_dags_task_id_tasks",
            "fk_agent_dag_edges_node",
            "fk_agent_dag_edges_dependency",
        ):
            self.assertIn(expected, names)
        self.assertEqual(
            len(migrated["columns"]),
            sum(len(Base.metadata.tables[name].columns) for name in TABLE_NAMES),
        )


if __name__ == "__main__":
    unittest.main()
