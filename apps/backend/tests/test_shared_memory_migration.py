"""Revision 0046: models, migration and database must describe the same schema.

The first classes need no server. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the migration up and down, compare it with
the models through Alembic's autogenerate, compare the two catalogs (constraint
definitions and partial indexes, which autogenerate does not see) and try every
constraint. Nothing here assumes 0046 is the head: the previous revision is read
from the script directory.
"""

import io
import unittest
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, create_engine, text

from paw_backend.db import Base
from paw_backend.memory.shared import limits
from paw_backend.memory.shared.models import TABLE_NAMES

from .memory_support import (
    MemoryDatabaseTestCase,
    migrate,
    requires_postgres,
    sync_database_url,
)
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0046"
TABLE = "shared_memory_candidates"
DRIFT_SCHEMA = "paw_shared_drift_check"
CHECKS = {
    "state_valid",
    "origin_scope_valid",
    "memory_type_length",
    "title_length",
    "content_length",
    "importance_range",
    "policy_subjects_count",
    "reason_length",
    "decision_reason_length",
    "pending_has_no_decision",
    "decided_has_decider",
    "memory_only_when_approved",
}


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


class ModelsMetadataTest(unittest.TestCase):
    def table(self):
        return Base.metadata.tables[TABLE]

    def test_the_schema_has_exactly_the_candidates_table(self):
        self.assertEqual(TABLE_NAMES, (TABLE,))
        self.assertIn(TABLE, Base.metadata.tables)

    def test_every_constraint_and_index_has_a_conventional_name(self):
        prefixes = {
            "PrimaryKeyConstraint": "pk_",
            "ForeignKeyConstraint": "fk_",
            "UniqueConstraint": "uq_",
            "CheckConstraint": "ck_",
        }
        table = self.table()
        for constraint in table.constraints:
            with self.subTest(constraint=constraint.name):
                self.assertIsNotNone(constraint.name)
                prefix = prefixes[type(constraint).__name__]
                self.assertTrue(constraint.name.startswith(prefix + TABLE))
                self.assertLessEqual(len(constraint.name), 63)
        for index in table.indexes:
            with self.subTest(index=index.name):
                self.assertTrue(index.name.startswith(f"ix_{TABLE}_"))
                self.assertLessEqual(len(index.name), 63)

    def test_the_check_constraints_are_the_documented_set(self):
        names = {
            c.name.removeprefix(f"ck_{TABLE}_")
            for c in self.table().constraints
            if isinstance(c, CheckConstraint)
        }
        self.assertEqual(names, CHECKS)

    def test_there_is_no_foreign_key(self):
        # The Memory layer tables stay free of links to other subsystems'
        # tables (tests/test_memory_schema.py), so the table has none.
        foreign_keys = [
            c for c in self.table().constraints if isinstance(c, ForeignKeyConstraint)
        ]
        self.assertEqual(foreign_keys, [])

    def test_the_people_and_the_origin_are_plain_uuid_columns(self):
        table = self.table()
        for name in (
            "proposer_user_id",
            "proposer_agent_id",
            "decided_by",
            "origin_version_id",
            "memory_id",
        ):
            with self.subTest(name):
                self.assertEqual(type(table.columns[name].type).__name__, "Uuid")
                self.assertEqual(list(table.columns[name].foreign_keys), [])

    def test_the_columns_that_must_be_present(self):
        table = self.table()
        required = {
            "id",
            "state",
            "proposer_user_id",
            "origin_scope",
            "memory_type",
            "title",
            "content",
            "importance",
            "policy_subjects",
            "created_at",
        }
        optional = {
            "proposer_agent_id",
            "origin_version_id",
            "reason",
            "decided_by",
            "decided_at",
            "decision_reason",
            "memory_id",
        }
        self.assertEqual({c.name for c in table.columns if not c.nullable}, required)
        self.assertEqual({c.name for c in table.columns if c.nullable}, optional)

    def test_the_state_defaults_to_pending(self):
        default = self.table().columns["state"].server_default.arg
        self.assertEqual(str(default), "'pending'")

    def test_there_are_indexes_for_the_review_queue_and_the_limit(self):
        names = {index.name for index in self.table().indexes}
        self.assertEqual(
            names,
            {
                f"ix_{TABLE}_state_created_at",
                f"ix_{TABLE}_proposer_user_id_state",
            },
        )


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str, **environment: str) -> str:
        output = io.StringIO()
        with paw_environment(
            PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw", **environment
        ):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_upgrade_creates_only_the_candidates_table(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        self.assertIn(f"CREATE TABLE {TABLE} ", sql)
        self.assertEqual(sql.count("CREATE TABLE"), 1)
        self.assertNotIn("ALTER TABLE memor", sql)
        self.assertNotIn("CREATE EXTENSION", sql)
        self.assertNotIn("REFERENCES", sql)

    def test_the_limits_of_the_service_are_in_the_check_constraints(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        for expected in (
            f"char_length(title) BETWEEN 1 AND {limits.MAX_TITLE_CHARS}",
            f"char_length(content) BETWEEN 1 AND {limits.MAX_CONTENT_CHARS}",
            f"char_length(memory_type) BETWEEN 1 AND {limits.MAX_MEMORY_TYPE_CHARS}",
            f"cardinality(policy_subjects) <= {limits.MAX_POLICY_SUBJECTS}",
        ):
            with self.subTest(expected):
                self.assertIn(expected, sql)
        self.assertEqual(sql.count(f"BETWEEN 1 AND {limits.MAX_REASON_CHARS}"), 2)

    def test_the_grants_are_the_least_privileges_of_the_service(self):
        sql = self.sql(
            "upgrade",
            f"{previous_revision()}:{REVISION}",
            PAW_APP_DATABASE_ROLE="paw_app",
        )
        self.assertIn(f"REVOKE ALL ON {TABLE} FROM PUBLIC", sql)
        self.assertIn(f'GRANT INSERT, SELECT ON {TABLE} TO "paw_app"', sql)
        self.assertIn(
            f"GRANT UPDATE (state, decided_by, decided_at, decision_reason, memory_id)"
            f' ON {TABLE} TO "paw_app"',
            sql,
        )
        for forbidden in ("DELETE", "TRUNCATE", "ALL PRIVILEGES", "GRANT OPTION"):
            self.assertNotIn(forbidden, sql.split("REVOKE ALL")[1])

    def test_without_an_application_role_nothing_is_granted(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        self.assertNotIn("GRANT", sql)
        self.assertIn(f"REVOKE ALL ON {TABLE} FROM PUBLIC", sql)

    def test_downgrade_drops_the_table_and_nothing_else(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertEqual(sql.count("DROP TABLE"), 1)
        self.assertIn(f"DROP TABLE {TABLE};", sql)


def only_candidates(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in TABLE_NAMES
    table = getattr(obj, "table", None)
    return table is None or table.name in TABLE_NAMES


def catalog(connection, schema: str) -> dict[str, list[tuple]]:
    """Columns, constraints and indexes of the candidates table in ``schema``."""
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

    def test_upgrade_creates_the_table_and_downgrade_removes_it(self):
        previous = previous_revision()

        migrate("upgrade", REVISION)

        self.assertIn(TABLE, self.tables())
        self.assertEqual(
            self.scalars("SELECT version_num FROM alembic_version"), [REVISION]
        )

        migrate("downgrade", previous)

        self.assertNotIn(TABLE, self.tables())
        self.assertEqual(
            self.scalars("SELECT version_num FROM alembic_version"), [previous]
        )
        self.assertEqual(
            self.scalars(
                "SELECT indexname FROM pg_indexes WHERE tablename = :t", t=TABLE
            ),
            [],
        )
        self.assertIn("memories", self.tables())

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        migrate("upgrade", REVISION)
        migrate("downgrade", previous_revision())
        migrate("upgrade", REVISION)
        self.assertIn(TABLE, self.tables())

    def test_head_contains_the_table(self):
        migrate("upgrade", "head")
        self.assertIn(TABLE, self.tables())

    def test_the_downgrade_leaves_the_memory_tables_and_their_rows_alone(self):
        migrate("upgrade", REVISION)
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO memories DEFAULT VALUES"))

        migrate("downgrade", previous_revision())

        self.assertEqual(self.scalars("SELECT count(*) FROM memories"), [1])

    def autogenerate_diff(self, connection) -> list:
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_object": only_candidates,
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
                f"DROP INDEX ix_{TABLE}_state_created_at",
                f"ALTER TABLE {TABLE} ALTER COLUMN importance DROP DEFAULT",
                f"ALTER TABLE {TABLE} ADD COLUMN unexpected text",
                f"ALTER TABLE {TABLE} ALTER COLUMN reason SET NOT NULL",
                f"ALTER TABLE {TABLE} ALTER COLUMN decided_at TYPE timestamp",
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
        tables = [Base.metadata.tables[TABLE]]
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
        self.assertIn(f"ck_{TABLE}_pending_has_no_decision", names)
        self.assertFalse({n for n in names if n.startswith("fk_")})
        self.assertEqual(
            len(migrated["columns"]), len(Base.metadata.tables[TABLE].columns)
        )

    def test_the_catalog_check_notices_a_changed_check_constraint(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection, connection.begin() as transaction:
            before = catalog(connection, "public")
            connection.execute(
                text(f"ALTER TABLE {TABLE} DROP CONSTRAINT ck_{TABLE}_title_length")
            )
            connection.execute(
                text(
                    f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_{TABLE}_title_length"
                    " CHECK (char_length(title) BETWEEN 1 AND 300)"
                )
            )
            after = catalog(connection, "public")
            transaction.rollback()
        self.assertEqual(before["columns"], after["columns"])
        self.assertNotEqual(before["constraints"], after["constraints"])


@requires_postgres
class ConstraintBehaviourTest(MemoryDatabaseTestCase):
    """Every constraint of the table, tried on the migrated schema."""

    def insert(self, **overrides: Any):
        values: dict[str, Any] = {
            "proposer_user_id": uuid4(),
            "origin_scope": "user",
            "memory_type": "rule",
            "title": "T",
            "content": "C",
        }
        values.update(overrides)
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        return self.session.execute(
            text(
                f"INSERT INTO {TABLE} ({columns}) VALUES ({placeholders}) RETURNING id"
            ),
            values,
        ).scalar_one()

    def decided(self, state: str, **overrides: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            "state": state,
            "decided_by": uuid4(),
            "decided_at": "2026-09-24T12:00:00+00:00",
        }
        values.update(overrides)
        return values

    def violated(self, **overrides: Any) -> str | None:
        return self.violation(lambda: self.insert(**overrides))

    def name(self, short: str) -> str:
        return f"ck_{TABLE}_{short}"

    def test_a_minimal_row_gets_the_defaults(self):
        candidate_id = self.insert()
        row = (
            self.session.execute(
                text(f"SELECT * FROM {TABLE} WHERE id = :i"), {"i": candidate_id}
            )
            .mappings()
            .one()
        )
        self.assertEqual(
            (
                row["state"],
                row["importance"],
                row["policy_subjects"],
                row["decided_by"],
                row["memory_id"],
            ),
            ("pending", 50, [], None, None),
        )
        self.assertIsNotNone(row["created_at"])

    def test_the_state_and_the_origin_scope_are_closed_sets(self):
        self.assertEqual(
            self.violated(**self.decided("bogus")), self.name("state_valid")
        )
        for scope in ("shared", "org", "", "USER"):
            with self.subTest(scope=scope):
                self.assertEqual(
                    self.violated(origin_scope=scope), self.name("origin_scope_valid")
                )
        for scope in ("user", "project", "project_group", "repo"):
            self.assertIsNone(self.violated(origin_scope=scope))

    def test_the_text_length_boundaries(self):
        cases = [
            ("memory_type", limits.MAX_MEMORY_TYPE_CHARS, "memory_type_length"),
            ("title", limits.MAX_TITLE_CHARS, "title_length"),
            ("content", limits.MAX_CONTENT_CHARS, "content_length"),
            ("reason", limits.MAX_REASON_CHARS, "reason_length"),
        ]
        for column, maximum, constraint in cases:
            with self.subTest(column=column):
                self.assertIsNone(self.violated(**{column: "a" * maximum}))
                self.assertEqual(
                    self.violated(**{column: "a" * (maximum + 1)}),
                    self.name(constraint),
                )
                self.assertEqual(self.violated(**{column: ""}), self.name(constraint))

    def test_the_decision_reason_boundary(self):
        base = self.decided("rejected")
        self.assertIsNone(self.violated(**base, decision_reason="a" * 500))
        self.assertEqual(
            self.violated(**base, decision_reason="a" * 501),
            self.name("decision_reason_length"),
        )
        self.assertEqual(
            self.violated(**base, decision_reason=""),
            self.name("decision_reason_length"),
        )

    def test_the_importance_range(self):
        for value in (0, 100):
            self.assertIsNone(self.violated(importance=value))
        for value in (-1, 101):
            self.assertEqual(
                self.violated(importance=value), self.name("importance_range")
            )

    def test_at_most_twenty_subjects(self):
        self.assertIsNone(self.violated(policy_subjects=[f"s{n}" for n in range(20)]))
        self.assertEqual(
            self.violated(policy_subjects=[f"s{n}" for n in range(21)]),
            self.name("policy_subjects_count"),
        )

    def test_a_pending_candidate_has_no_decision(self):
        for column, value in (
            ("decided_by", uuid4()),
            ("decided_at", "2026-09-24T12:00:00+00:00"),
            ("decision_reason", "why"),
        ):
            with self.subTest(column=column):
                self.assertEqual(
                    self.violated(state="pending", **{column: value}),
                    self.name("pending_has_no_decision"),
                )

    def test_a_pending_candidate_has_no_memory(self):
        memory_id = uuid4()
        self.assertIn(
            self.violated(state="pending", memory_id=memory_id),
            {
                self.name("pending_has_no_decision"),
                self.name("memory_only_when_approved"),
            },
        )

    def test_a_decided_candidate_names_who_and_when(self):
        for state in ("approved", "rejected"):
            with self.subTest(state=state):
                self.assertEqual(
                    self.violated(state=state), self.name("decided_has_decider")
                )
                self.assertEqual(
                    self.violated(state=state, decided_by=uuid4()),
                    self.name("decided_has_decider"),
                )
                self.assertEqual(
                    self.violated(state=state, decided_at="2026-09-24T12:00:00+00:00"),
                    self.name("decided_has_decider"),
                )
                self.assertIsNone(self.violated(**self.decided(state)))

    def test_only_an_approved_candidate_points_at_a_memory(self):
        memory_id = self.add_memory()
        self.assertIsNone(
            self.violated(**self.decided("approved", memory_id=memory_id))
        )
        self.assertEqual(
            self.violated(**self.decided("rejected", memory_id=memory_id)),
            self.name("memory_only_when_approved"),
        )

    def test_the_memory_id_is_a_plain_uuid_that_the_database_does_not_check(self):
        self.assertIsNone(self.violated(**self.decided("approved", memory_id=uuid4())))

    def test_the_review_queue_index_is_used_for_the_pending_list(self):
        for _ in range(3):
            self.insert()
        plan = "\n".join(
            self.session.execute(
                text(
                    "EXPLAIN SELECT id FROM shared_memory_candidates"
                    " WHERE state = 'pending' ORDER BY created_at LIMIT 5"
                )
            ).scalars()
        )
        self.assertIn(TABLE, plan)


if __name__ == "__main__":
    unittest.main()
