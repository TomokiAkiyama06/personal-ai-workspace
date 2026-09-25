"""Revision 0041: models, migration and database must describe the same schema.

The first classes need no server. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the migration up and down, compare it with the
models through Alembic's autogenerate, and compare the two catalogs (constraint
definitions and partial indexes, which autogenerate does not see). Nothing here
assumes 0041 is the head: the previous revision is read from the script directory.
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
from paw_backend.memory.journal import limits
from paw_backend.memory.journal.domain import (
    EntryState,
    FailureKind,
    JobStatus,
    Priority,
)
from paw_backend.memory.journal.models import TABLE_NAMES

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0041"
ENTRIES = "memory_journal_entries"
JOBS = "memory_consolidation_queue"
KEYS = "memory_consolidation_keys"
DRIFT_SCHEMA = "paw_journal_drift_check"
# The tables a schema built from the models needs for the foreign keys to resolve.
REFERENCED = ("conversations", "messages", "memories")
CHECKS = {
    ENTRIES: {
        "event_sequence_not_negative",
        "state_valid",
        "consolidated_has_time",
        "consolidated_has_outcome",
        "outcome_object",
    },
    JOBS: {
        "status_valid",
        "priority_valid",
        "priority_rank_matches_priority",
        "last_failure_valid",
        "attempts_not_negative",
        "deferrals_not_negative",
        "claim_count_not_negative",
        "lease_matches_status",
        "claimed_has_worker",
        "queued_has_no_worker",
        "lease_after_claim",
        "finished_matches_status",
        "dead_has_failed_attempts",
    },
    KEYS: {"key_length", "applied_sequence_not_negative"},
}


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


def listed(members) -> str:
    return ", ".join(f"'{member.value}'" for member in members)


class ModelsMetadataTest(unittest.TestCase):
    def tables(self):
        return [Base.metadata.tables[name] for name in TABLE_NAMES]

    def test_the_schema_has_exactly_the_three_tables(self):
        self.assertEqual(set(TABLE_NAMES), {ENTRIES, JOBS, KEYS})
        for name in TABLE_NAMES:
            self.assertIn(name, Base.metadata.tables)

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
                    self.assertIsNotNone(constraint.name)
                    prefix = prefixes[type(constraint).__name__]
                    self.assertTrue(constraint.name.startswith(prefix + table.name))
                    self.assertLessEqual(len(constraint.name), 63)
            for index in table.indexes:
                with self.subTest(table=table.name, index=index.name):
                    # A unique partial index is named like a constraint, as in 0033.
                    self.assertTrue(
                        index.name.startswith(
                            (f"ix_{table.name}_", f"uq_{table.name}_")
                        )
                    )
                    self.assertLessEqual(len(index.name), 63)

    def test_the_check_constraints_are_the_documented_sets(self):
        for table in self.tables():
            names = {
                c.name.removeprefix(f"ck_{table.name}_")
                for c in table.constraints
                if isinstance(c, CheckConstraint)
            }
            with self.subTest(table.name):
                self.assertEqual(names, CHECKS[table.name])

    def test_the_only_foreign_keys_stay_inside_the_memory_layer(self):
        targets = {}
        for table in self.tables():
            for constraint in table.constraints:
                if isinstance(constraint, ForeignKeyConstraint):
                    targets.setdefault(table.name, set()).add(
                        constraint.referred_table.name
                    )
        self.assertEqual(
            targets,
            {ENTRIES: {"messages"}, JOBS: {ENTRIES}, KEYS: {"memories"}},
        )

    def test_the_people_and_the_context_are_plain_uuid_columns(self):
        for table, column in (
            (ENTRIES, "owner_user_id"),
            (ENTRIES, "project_id"),
            (ENTRIES, "repo_id"),
            (KEYS, "owner_user_id"),
            (KEYS, "applied_conversation_id"),
        ):
            with self.subTest(table=table, column=column):
                found = Base.metadata.tables[table].columns[column]
                self.assertEqual(type(found.type).__name__, "Uuid")
                self.assertEqual(list(found.foreign_keys), [])

    def test_the_defaults(self):
        entries, jobs = (Base.metadata.tables[n] for n in (ENTRIES, JOBS))
        self.assertEqual(str(entries.columns["state"].server_default.arg), "'pending'")
        self.assertEqual(str(jobs.columns["status"].server_default.arg), "'queued'")
        for name in ("attempts", "deferrals", "claim_count"):
            self.assertEqual(str(jobs.columns[name].server_default.arg), "0")
        for name in ("enqueued_at", "available_at"):
            self.assertIn("clock_timestamp", str(jobs.columns[name].server_default.arg))
        self.assertIn(
            "clock_timestamp", str(entries.columns["recorded_at"].server_default.arg)
        )


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str, **environment: str) -> str:
        output = io.StringIO()
        with paw_environment(
            PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw", **environment
        ):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def upgrade_sql(self, **environment: str) -> str:
        return self.sql("upgrade", f"{previous_revision()}:{REVISION}", **environment)

    def test_upgrade_creates_the_three_tables_and_nothing_else(self):
        sql = self.upgrade_sql()
        for table in (ENTRIES, JOBS, KEYS):
            self.assertIn(f"CREATE TABLE {table} ", sql)
        self.assertEqual(sql.count("CREATE TABLE"), 3)
        self.assertNotIn("ALTER TABLE", sql)
        self.assertNotIn("CREATE EXTENSION", sql)
        self.assertNotIn("CREATE TRIGGER", sql)

    def test_the_enum_lists_of_the_checks_are_the_python_enums(self):
        sql = " ".join(self.upgrade_sql().split())
        for expected in (
            f"state IN ({listed(EntryState)})",
            f"status IN ({listed(JobStatus)})",
            f"priority IN ({listed(Priority)})",
            f"last_failure IN ({listed(FailureKind)})",
        ):
            with self.subTest(expected):
                self.assertIn(expected, sql)

    def test_the_limits_of_the_code_are_in_the_check_constraints(self):
        sql = self.upgrade_sql()
        self.assertIn(f"char_length(key) BETWEEN 1 AND {limits.MAX_KEY_CHARS}", sql)
        self.assertEqual(limits.MAX_WORKER_ID_CHARS, 100)
        self.assertIn("claimed_by VARCHAR(100)", sql)

    def test_the_grants_are_the_least_privileges_of_the_services(self):
        sql = " ".join(self.upgrade_sql(PAW_APP_DATABASE_ROLE="paw_app").split())
        for table in (ENTRIES, JOBS, KEYS):
            self.assertIn(f"REVOKE ALL ON {table} FROM PUBLIC", sql)
            self.assertIn(f'GRANT INSERT, SELECT ON {table} TO "paw_app"', sql)
        self.assertIn(
            f'GRANT UPDATE (state, consolidated_at, outcome) ON {ENTRIES} TO "paw_app"',
            sql,
        )
        self.assertIn(
            "GRANT UPDATE (status, available_at, attempts, deferrals, claim_count,"
            " claimed_by, claimed_at, lease_expires_at, last_failure, finished_at)"
            f' ON {JOBS} TO "paw_app"',
            sql,
        )
        self.assertIn(
            "GRANT UPDATE (applied_conversation_id, applied_event_sequence,"
            f' applied_recorded_at) ON {KEYS} TO "paw_app"',
            sql,
        )
        grants = re.findall(r"GRANT [^;]*;", sql)
        self.assertEqual(len(grants), 6)  # INSERT + SELECT, then UPDATE (columns)
        for forbidden in ("DELETE", "TRUNCATE", "ALL PRIVILEGES", "GRANT OPTION"):
            for grant in grants:
                self.assertNotIn(forbidden, grant)

    def test_without_an_application_role_nothing_is_granted(self):
        sql = self.upgrade_sql()
        self.assertNotIn("GRANT", sql)
        self.assertEqual(sql.count("REVOKE ALL"), 3)

    def test_downgrade_drops_the_three_tables_children_first(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertEqual(sql.count("DROP TABLE"), 3)
        positions = [sql.index(f"DROP TABLE {t};") for t in (KEYS, JOBS, ENTRIES)]
        self.assertEqual(positions, sorted(positions))

    def test_the_revision_follows_the_recorded_head_of_the_lane(self):
        # The orchestrator re-chains at integration; the id and its file are fixed.
        scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
        revision = scripts.get_revision(REVISION)
        self.assertTrue(revision.path.endswith("0041_memory_journal.py"))
        self.assertIsInstance(revision.down_revision, str)


def only_journal(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in TABLE_NAMES
    table = getattr(obj, "table", None)
    return table is None or table.name in TABLE_NAMES


def catalog(connection, schema: str) -> dict[str, list[tuple]]:
    """Columns, constraints and indexes of the journal tables in ``schema``."""
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
class MigrationDatabaseTest(unittest.TestCase):
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

    def test_upgrade_creates_the_tables_and_downgrade_removes_them(self):
        previous = previous_revision()

        migrate("upgrade", REVISION)

        self.assertTrue(set(TABLE_NAMES) <= self.tables())
        self.assertEqual(
            self.scalars("SELECT version_num FROM alembic_version"), [REVISION]
        )

        migrate("downgrade", previous)

        self.assertFalse(set(TABLE_NAMES) & self.tables())
        self.assertEqual(
            self.scalars("SELECT version_num FROM alembic_version"), [previous]
        )
        self.assertEqual(
            self.scalars(
                "SELECT indexname FROM pg_indexes WHERE tablename = ANY (:t)",
                t=list(TABLE_NAMES),
            ),
            [],
        )
        self.assertIn("messages", self.tables())

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        migrate("upgrade", REVISION)
        migrate("downgrade", previous_revision())
        migrate("upgrade", REVISION)
        self.assertTrue(set(TABLE_NAMES) <= self.tables())

    def test_head_contains_the_tables_and_base_removes_them(self):
        migrate("upgrade", "head")
        self.assertTrue(set(TABLE_NAMES) <= self.tables())
        migrate("downgrade", "base")
        self.assertFalse(set(TABLE_NAMES) & self.tables())

    def test_the_downgrade_leaves_the_memory_tables_and_their_rows_alone(self):
        migrate("upgrade", REVISION)
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO memories DEFAULT VALUES"))
            connection.execute(
                text(
                    "INSERT INTO conversations (owner_user_id)"
                    " VALUES (gen_random_uuid())"
                )
            )

        migrate("downgrade", previous_revision())

        self.assertEqual(self.scalars("SELECT count(*) FROM memories"), [1])
        self.assertEqual(self.scalars("SELECT count(*) FROM conversations"), [1])

    def autogenerate_diff(self, connection) -> list:
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_object": only_journal,
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
                f"DROP INDEX ix_{ENTRIES}_pending",
                f"ALTER TABLE {JOBS} ALTER COLUMN attempts DROP DEFAULT",
                f"ALTER TABLE {JOBS} ADD COLUMN unexpected text",
                f"ALTER TABLE {ENTRIES} ALTER COLUMN outcome SET NOT NULL",
                f"ALTER TABLE {KEYS} ALTER COLUMN applied_recorded_at TYPE timestamp",
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

    def created_catalog(self) -> dict[str, list[tuple]]:
        """The catalog of a schema built from the models with ``create_all``."""
        tables = [Base.metadata.tables[name] for name in (*REFERENCED, *TABLE_NAMES)]
        with self.engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {DRIFT_SCHEMA} CASCADE"))
            connection.execute(text(f"CREATE SCHEMA {DRIFT_SCHEMA}"))
        try:
            with self.engine.begin() as connection:
                scoped = connection.execution_options(
                    schema_translate_map={None: DRIFT_SCHEMA}
                )
                Base.metadata.create_all(scoped, tables=tables)
            with self.engine.connect() as connection:
                return catalog(connection, DRIFT_SCHEMA)
        finally:
            with self.engine.begin() as connection:
                connection.execute(
                    text(f"DROP SCHEMA IF EXISTS {DRIFT_SCHEMA} CASCADE")
                )

    def test_the_migration_and_the_models_produce_the_same_catalog(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection:
            migrated = catalog(connection, "public")
        created = self.created_catalog()
        for kind in ("columns", "constraints", "indexes"):
            with self.subTest(kind):
                self.assertEqual(migrated[kind], created[kind])
        names = {row[1] for row in migrated["constraints"]}
        self.assertIn(f"ck_{JOBS}_dead_has_failed_attempts", names)
        self.assertIn(f"fk_{ENTRIES}_conversation_id_messages", names)
        self.assertEqual(
            len(migrated["columns"]),
            sum(len(Base.metadata.tables[n].columns) for n in TABLE_NAMES),
        )

    def test_the_catalog_check_notices_a_changed_check_constraint(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection, connection.begin() as transaction:
            before = catalog(connection, "public")
            connection.execute(
                text(f"ALTER TABLE {KEYS} DROP CONSTRAINT ck_{KEYS}_key_length")
            )
            connection.execute(
                text(
                    f"ALTER TABLE {KEYS} ADD CONSTRAINT ck_{KEYS}_key_length"
                    " CHECK (char_length(key) BETWEEN 1 AND 300)"
                )
            )
            after = catalog(connection, "public")
            transaction.rollback()
        self.assertEqual(before["columns"], after["columns"])
        self.assertNotEqual(before["constraints"], after["constraints"])


if __name__ == "__main__":
    unittest.main()
