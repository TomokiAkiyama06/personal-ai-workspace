"""Revision 0030: models, migration and database must describe the same schema.

The first classes need no server. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the migration up and down, compare it with the
models through Alembic's autogenerate and through the catalogs, and prove every CHECK
constraint, key and referential action by violating it.
"""

import io
import re
import unittest
import uuid
from datetime import UTC, datetime, timedelta

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, create_engine, text

from paw_backend.connections import limits
from paw_backend.connections.domain import (
    ConnectionKind,
    ConnectionStatus,
    FailureCode,
    QuotaMetric,
    QuotaPeriod,
    UsagePurpose,
    UsageStatus,
)
from paw_backend.connections.models import TABLE_NAMES
from paw_backend.db import Base

from .memory_support import (
    MemoryDatabaseTestCase,
    migrate,
    requires_postgres,
    sync_database_url,
)
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0030"


def handle(number: int = 1) -> str:
    """A well-formed credential handle (``cred_`` + 32 hex characters)."""
    return "cred_" + f"{number:032x}"


SCHEMA = "paw_connections_drift_check"
CONNECTIONS, QUOTAS, USAGE = TABLE_NAMES
T0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


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
    return set(re.findall(r"'([a-z0-9_]+)'", sql))


class ModelsMetadataTest(unittest.TestCase):
    def connection_tables(self):
        return [Base.metadata.tables[name] for name in TABLE_NAMES]

    def test_the_schema_has_the_expected_tables(self):
        self.assertEqual(
            TABLE_NAMES, ("shared_connections", "connection_quotas", "connection_usage")
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
        for table in self.connection_tables():
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

    def test_the_foreign_keys_and_their_referential_actions(self):
        found = {}
        for table in self.connection_tables():
            for constraint in table.constraints:
                if isinstance(constraint, ForeignKeyConstraint):
                    found[(table.name, constraint.elements[0].parent.name)] = (
                        constraint.referred_table.name,
                        constraint.ondelete,
                    )
        self.assertEqual(
            found,
            {
                (QUOTAS, "user_id"): ("users", "CASCADE"),
                (USAGE, "task_id"): ("tasks", "RESTRICT"),
            },
        )

    def test_the_usage_history_keeps_no_foreign_key_to_a_user_or_a_project(self):
        usage = Base.metadata.tables[USAGE]
        for name in ("user_id", "project_id"):
            self.assertEqual(list(usage.columns[name].foreign_keys), [], name)

    def test_the_credential_is_only_ever_a_handle_column(self):
        connections = Base.metadata.tables[CONNECTIONS]
        self.assertIn("secret_handle", connections.columns)
        for table in self.connection_tables():
            for column in table.columns:
                with self.subTest(table=table.name, column=column.name):
                    # No column can hold a plaintext, a prompt or an answer.
                    self.assertNotRegex(
                        column.name,
                        r"secret$|password|token$|api_key|credential|prompt|"
                        r"answer|response|content|body",
                    )

    def test_a_quota_is_keyed_by_user_kind_metric_and_period(self):
        quotas = Base.metadata.tables[QUOTAS]
        self.assertEqual(
            [column.name for column in quotas.primary_key.columns],
            ["user_id", "kind", "metric", "period"],
        )
        self.assertTrue(quotas.columns["limit_value"].nullable)  # NULL = Unlimited

    def test_one_connection_per_kind(self):
        connections = Base.metadata.tables[CONNECTIONS]
        unique = [
            [column.name for column in constraint.columns]
            for constraint in connections.constraints
            if type(constraint).__name__ == "UniqueConstraint"
        ]
        self.assertEqual(unique, [["kind"]])

    def test_timestamps_have_no_default_so_the_clock_of_the_module_decides(self):
        for table, names in {
            CONNECTIONS: ("created_at", "updated_at"),
            QUOTAS: ("created_at", "updated_at"),
            USAGE: ("started_at",),
        }.items():
            for name in names:
                column = Base.metadata.tables[table].columns[name]
                with self.subTest(table=table, column=name):
                    self.assertIsNone(column.server_default)
                    self.assertIsNone(column.default)
                    self.assertFalse(column.nullable)

    def test_the_allowed_values_of_the_database_are_those_of_the_module(self):
        connections, quotas, usage = (check_sql(name) for name in TABLE_NAMES)
        kinds = {kind.value for kind in ConnectionKind}
        for label, sql in (
            ("connections", connections["kind_valid"]),
            ("quotas", quotas["kind_valid"]),
            ("usage", usage["kind_valid"]),
        ):
            self.assertEqual(literals(sql), kinds, label)
        self.assertEqual(
            literals(connections["status_valid"]), {s.value for s in ConnectionStatus}
        )
        self.assertEqual(
            literals(quotas["metric_valid"]), {m.value for m in QuotaMetric}
        )
        self.assertEqual(
            literals(quotas["period_valid"]), {p.value for p in QuotaPeriod}
        )
        self.assertEqual(
            literals(usage["purpose_valid"]), {p.value for p in UsagePurpose}
        )
        self.assertEqual(
            literals(usage["status_valid"]), {s.value for s in UsageStatus}
        )
        self.assertEqual(
            literals(usage["failure_code_valid"]), {f.value for f in FailureCode}
        )

    def test_the_limits_of_the_database_match_the_module_limits(self):
        quotas, usage = check_sql(QUOTAS), check_sql(USAGE)
        self.assertIn(f"BETWEEN 0 AND {limits.MAX_QUOTA_LIMIT}", quotas["limit_range"])
        self.assertIn(
            f"BETWEEN 0 AND {limits.MAX_DURATION_MS}", usage["duration_range"]
        )
        for name in ("input_tokens_range", "output_tokens_range"):
            self.assertIn(f"BETWEEN 0 AND {limits.MAX_TOKENS_PER_CALL}", usage[name])
        self.assertIn(f"{{0,{limits.MAX_MODEL_CHARS - 1}}}", usage["model_shape"])

    def test_the_handle_check_has_the_shape_of_the_credential_handle(self):
        from paw_backend.tools.credentials import CREDENTIAL_HANDLE_PATTERN

        sql = check_sql(CONNECTIONS)["handle_shape"]
        self.assertIn(f"^{CREDENTIAL_HANDLE_PATTERN}$", sql)


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_the_revision_follows_the_projects_revision(self):
        self.assertEqual(previous_revision(), "0026")

    def test_upgrade_creates_the_three_tables(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        for table in TABLE_NAMES:
            self.assertIn(f"CREATE TABLE {table} (", sql)
        self.assertIn("REFERENCES users (id) ON DELETE CASCADE", sql)
        self.assertIn("REFERENCES tasks (id) ON DELETE RESTRICT", sql)
        self.assertIn("CONSTRAINT uq_shared_connections_kind UNIQUE (kind)", sql)
        self.assertIn(f"UPDATE alembic_version SET version_num='{REVISION}'", sql)

    def test_upgrade_touches_no_table_of_another_area(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        for other in (
            "ALTER TABLE tasks",
            "ALTER TABLE users",
            "ALTER TABLE projects",
            "ALTER TABLE audit_events",
        ):
            self.assertNotIn(other, sql)

    def test_downgrade_drops_the_three_tables(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        for table in TABLE_NAMES:
            self.assertIn(f"DROP TABLE {table};", sql)
        self.assertLess(
            sql.index(f"DROP TABLE {USAGE};"), sql.index(f"DROP TABLE {QUOTAS};")
        )

    def test_the_migration_grants_the_application_role_least_privileges(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        # Offline without PAW_APP_DATABASE_ROLE only the REVOKE is rendered; the
        # exact privilege sets are proven by ``test_connections_grants``.
        for table in TABLE_NAMES:
            self.assertIn(f"REVOKE ALL ON {table} FROM PUBLIC", sql)

    def test_the_offline_sql_holds_no_credential(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        self.assertNotIn("db.invalid", sql)
        self.assertNotIn("u:p@", sql)


def only_connection_objects(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in TABLE_NAMES
    table = getattr(obj, "table", None)
    return table is None or table.name in TABLE_NAMES


def catalog(connection, schema: str) -> dict[str, list[tuple]]:
    """Columns, constraints and indexes of the connection tables in ``schema``."""
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
class ConnectionMigrationDatabaseTest(unittest.TestCase):
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
            " AND tablename = ANY(:tables)",
            tables=list(TABLE_NAMES),
        )
        self.assertEqual(leftovers, [])
        # The migration touches nothing of the layers below it.
        self.assertIn("users", self.tables())
        self.assertIn("tasks", self.tables())

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        previous = previous_revision()
        migrate("upgrade", REVISION)
        migrate("downgrade", previous)
        migrate("upgrade", REVISION)
        self.assertTrue(set(TABLE_NAMES) <= self.tables())
        self.assertEqual(self.version(), [REVISION])

    def test_upgrade_head_downgrade_base_upgrade_head(self):
        migrate("upgrade", "head")
        self.assertTrue(set(TABLE_NAMES) <= self.tables())
        migrate("downgrade", "base")
        self.assertEqual(self.tables() & set(TABLE_NAMES), set())
        self.assertNotIn("users", self.tables())
        migrate("upgrade", "head")
        self.assertTrue(set(TABLE_NAMES) <= self.tables())

    def test_the_downgrade_leaves_the_users_and_tasks_and_their_rows_alone(self):
        migrate("upgrade", REVISION)
        user, task = uuid.uuid4(), uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, login_name, system_role, status,"
                    " passkey_required, created_at, updated_at) VALUES"
                    " (:u, 'someone', 'user', 'active', false, now(), now())"
                ),
                {"u": user},
            )
            connection.execute(
                text(
                    "INSERT INTO tasks (id, project_id, created_by, title, input,"
                    " state, attempt, retry_count, version, created_at, updated_at)"
                    " VALUES (:t, gen_random_uuid(), :u, 'T', CAST('{}' AS jsonb),"
                    " 'queued', 1, 0, 1, now(), now())"
                ),
                {"t": task, "u": user},
            )

        migrate("downgrade", previous_revision())

        self.assertEqual(self.scalars("SELECT count(*) FROM users"), [1])
        self.assertEqual(self.scalars("SELECT count(*) FROM tasks"), [1])

    def autogenerate_diff(self, connection) -> list:
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_object": only_connection_objects,
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
                "DROP INDEX ix_connection_usage_task_id_kind",
                "ALTER TABLE shared_connections ALTER COLUMN enabled DROP DEFAULT",
                "ALTER TABLE shared_connections ADD COLUMN unexpected text",
                "ALTER TABLE connection_usage ALTER COLUMN finished_at SET NOT NULL",
                "ALTER TABLE connection_quotas ALTER COLUMN updated_at TYPE timestamp",
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
        tables = [
            Base.metadata.tables[name] for name in ("users", "tasks", *TABLE_NAMES)
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
        names = {row[1] for row in migrated["constraints"]}
        for expected in (
            "uq_shared_connections_kind",
            "ck_shared_connections_handle_shape",
            "fk_connection_quotas_user_id_users",
            "fk_connection_usage_task_id_tasks",
            "ck_connection_usage_failure_matches_status",
            "pk_connection_quotas",
        ):
            self.assertIn(expected, names)
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
                    "ALTER TABLE connection_quotas"
                    " DROP CONSTRAINT ck_connection_quotas_limit_range"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE connection_quotas ADD CONSTRAINT"
                    " ck_connection_quotas_limit_range CHECK (limit_value IS NULL"
                    " OR limit_value BETWEEN 0 AND 5)"
                )
            )
            after = catalog(connection, "public")
            transaction.rollback()
        self.assertEqual(before["columns"], after["columns"])
        self.assertNotEqual(before["constraints"], after["constraints"])


@requires_postgres
class ConstraintsTest(MemoryDatabaseTestCase):
    """Every rule of the database, proven by breaking it (one rule at a time)."""

    def user(self) -> uuid.UUID:
        return self.session.execute(
            text(
                "INSERT INTO users (id, login_name, system_role, status,"
                " passkey_required, created_at, updated_at) VALUES"
                " (gen_random_uuid(), 'u' || substr(md5(random()::text), 1, 12),"
                " 'user', 'active', false, :now, :now) RETURNING id"
            ),
            {"now": T0},
        ).scalar_one()

    def task(self, user: uuid.UUID | None = None) -> uuid.UUID:
        return self.session.execute(
            text(
                "INSERT INTO tasks (id, project_id, created_by, title, input, state,"
                " attempt, retry_count, version, created_at, updated_at) VALUES"
                " (gen_random_uuid(), gen_random_uuid(), :u, 'T', CAST('{}' AS jsonb),"
                " 'running', 1, 0, 1, :now, :now) RETURNING id"
            ),
            {"u": user or uuid.uuid4(), "now": T0},
        ).scalar_one()

    # -- shared_connections --------------------------------------------------

    def connection_values(self, **overrides):
        values = {
            "kind": "codex",
            "secret_handle": handle(1),
            "status": "connected",
            "created_at": T0,
            "updated_at": T0,
        }
        values.update(overrides)
        return values

    def add_connection(self, **overrides):
        self.session.execute(
            text(
                "INSERT INTO shared_connections (kind, secret_handle, status,"
                " created_at, updated_at) VALUES (:kind, :secret_handle, :status,"
                " :created_at, :updated_at)"
            ),
            self.connection_values(**overrides),
        )

    def violates_connection(self, **overrides) -> str | None:
        return self.violation(lambda: self.add_connection(**overrides))

    def test_a_valid_connection_of_every_kind_and_status_is_accepted(self):
        for kind in ConnectionKind:
            for status in ConnectionStatus:
                with self.subTest(kind=kind.value, status=status.value):
                    self.assertIsNone(
                        self.violates_connection(kind=kind.value, status=status.value)
                    )
                    self.session.execute(text("DELETE FROM shared_connections"))

    def test_a_connection_is_enabled_by_default(self):
        self.add_connection()
        self.assertTrue(
            self.session.execute(
                text("SELECT enabled FROM shared_connections")
            ).scalar()
        )

    def test_the_kind_must_be_known(self):
        for kind in ("bogus", "", "CODEX", "codex "):
            with self.subTest(kind=kind):
                self.assertEqual(
                    self.violates_connection(kind=kind),
                    "ck_shared_connections_kind_valid",
                )

    def test_the_status_must_be_known(self):
        for status in ("bogus", "", "Connected", "disabled"):
            with self.subTest(status=status):
                self.assertEqual(
                    self.violates_connection(status=status),
                    "ck_shared_connections_status_valid",
                )

    def test_there_is_one_connection_per_kind(self):
        self.add_connection()
        self.assertEqual(
            self.violates_connection(secret_handle=handle(2)),
            "uq_shared_connections_kind",
        )
        self.assertIsNone(self.violates_connection(kind="claude"))

    def test_only_a_credential_handle_can_be_stored_never_a_plaintext(self):
        for value in (
            "sk-" + "ant-" + "a" * 40,
            "plaintext-password",
            "",
            "cred_" + "a" * 31,
            "cred_" + "a" * 33,
            "cred_" + "A" * 32,
            "cred_" + "g" * 32,
            "xcred_" + "a" * 32,
            "cred_" + "a" * 32 + "x",
            "cred_" + "a" * 32 + "\n",
        ):
            with self.subTest(value=value[:12]):
                self.assertEqual(
                    self.violates_connection(secret_handle=value),
                    "ck_shared_connections_handle_shape",
                )
        self.assertIsNone(self.violates_connection(secret_handle=handle(2**127)))

    # -- connection_quotas ---------------------------------------------------

    def quota(self, **overrides):
        values = {
            "user_id": self.user(),
            "kind": "codex",
            "metric": "requests",
            "period": "day",
            "limit_value": 5,
            "created_at": T0,
            "updated_at": T0,
        }
        values.update(overrides)
        self.session.execute(
            text(
                "INSERT INTO connection_quotas (user_id, kind, metric, period,"
                " limit_value, created_at, updated_at) VALUES (:user_id, :kind,"
                " :metric, :period, :limit_value, :created_at, :updated_at)"
            ),
            values,
        )

    def violates_quota(self, **overrides) -> str | None:
        return self.violation(lambda: self.quota(**overrides))

    def test_a_quota_of_every_metric_and_period_is_accepted(self):
        user = self.user()
        for metric in QuotaMetric:
            for period in QuotaPeriod:
                with self.subTest(metric=metric.value, period=period.value):
                    self.assertIsNone(
                        self.violates_quota(
                            user_id=user, metric=metric.value, period=period.value
                        )
                    )

    def test_a_limit_is_a_number_in_range_or_null_for_unlimited(self):
        for good in (0, 1, 10**12, None):
            with self.subTest(limit=good):
                self.assertIsNone(self.violates_quota(limit_value=good))
        for bad in (-1, 10**12 + 1):
            with self.subTest(limit=bad):
                self.assertEqual(
                    self.violates_quota(limit_value=bad),
                    "ck_connection_quotas_limit_range",
                )

    def test_kind_metric_and_period_must_be_known(self):
        self.assertEqual(
            self.violates_quota(kind="bogus"), "ck_connection_quotas_kind_valid"
        )
        self.assertEqual(
            self.violates_quota(metric="gpu_seconds"),
            "ck_connection_quotas_metric_valid",
        )
        self.assertEqual(
            self.violates_quota(period="hour"), "ck_connection_quotas_period_valid"
        )

    def test_a_quota_needs_an_existing_user(self):
        self.assertEqual(
            self.violates_quota(user_id=uuid.uuid4()),
            "fk_connection_quotas_user_id_users",
        )

    def test_a_quota_is_unique_per_user_kind_metric_and_period(self):
        user = self.user()
        self.assertIsNone(self.violates_quota(user_id=user))
        self.assertEqual(self.violates_quota(user_id=user), "pk_connection_quotas")
        self.assertIsNone(self.violates_quota(user_id=user, period="week"))
        self.assertIsNone(self.violates_quota(user_id=user, kind="claude"))

    def test_deleting_a_user_deletes_their_quotas(self):
        user = self.user()
        self.quota(user_id=user)
        self.session.execute(text("DELETE FROM users WHERE id = :u"), {"u": user})
        self.assertEqual(
            self.session.execute(
                text("SELECT count(*) FROM connection_quotas")
            ).scalar(),
            0,
        )

    # -- connection_usage ----------------------------------------------------

    def usage_values(self, **overrides):
        values = {
            "user_id": uuid.uuid4(),
            "task_id": self.task(),
            "project_id": uuid.uuid4(),
            "kind": "codex",
            "model": "test-model-1",
            "purpose": "coding",
            "status": "in_flight",
            "failure_code": None,
            "input_tokens": None,
            "output_tokens": None,
            "started_at": T0,
            "finished_at": None,
            "duration_ms": None,
        }
        values.update(overrides)
        return values

    def usage(self, **overrides):
        values = self.usage_values(**overrides)
        columns = ", ".join(values)
        marks = ", ".join(f":{name}" for name in values)
        self.session.execute(
            text(f"INSERT INTO connection_usage ({columns}) VALUES ({marks})"), values
        )

    def violates_usage(self, **overrides) -> str | None:
        return self.violation(lambda: self.usage(**overrides))

    def settled(self, **overrides) -> dict:
        values = {
            "status": "succeeded",
            "input_tokens": 10,
            "output_tokens": 5,
            "finished_at": T0 + timedelta(seconds=2),
            "duration_ms": 2000,
        }
        values.update(overrides)
        return values

    def test_a_call_in_flight_and_a_settled_call_are_accepted(self):
        self.assertIsNone(self.violates_usage())
        self.assertIsNone(self.violates_usage(**self.settled()))
        self.assertIsNone(
            self.violates_usage(
                **self.settled(status="failed", failure_code="rate_limited")
            )
        )
        self.assertIsNone(
            self.violates_usage(
                **self.settled(
                    status="cancelled", input_tokens=None, output_tokens=None
                )
            )
        )
        for purpose in UsagePurpose:
            self.assertIsNone(self.violates_usage(purpose=purpose.value))

    def test_kind_purpose_status_and_failure_code_must_be_known(self):
        self.assertEqual(
            self.violates_usage(kind="x"), "ck_connection_usage_kind_valid"
        )
        self.assertEqual(
            self.violates_usage(purpose="write my thesis"),
            "ck_connection_usage_purpose_valid",
        )
        self.assertEqual(
            self.violates_usage(**self.settled(status="done")),
            "ck_connection_usage_status_valid",
        )
        self.assertEqual(
            self.violates_usage(**self.settled(status="failed", failure_code="oops")),
            "ck_connection_usage_failure_code_valid",
        )

    def test_the_purpose_is_a_category_and_never_free_text(self):
        for text_value in (
            "Fix the bug in login.py",
            "chat about the secret plan",
            "CODING",
            "",
        ):
            with self.subTest(text=text_value):
                self.assertEqual(
                    self.violates_usage(purpose=text_value),
                    "ck_connection_usage_purpose_valid",
                )

    def test_the_model_is_a_name_and_never_text(self):
        for good in ("gpt-5", "claude-opus-4", "vendor/model:tag", "a" * 100):
            self.assertIsNone(self.violates_usage(model=good), good)
        for bad in (
            "",
            "a" * 101,
            "has space",
            "line\nbreak",
            "-x",
            "prompt: hi there",
        ):
            with self.subTest(model=bad):
                self.assertEqual(
                    self.violates_usage(model=bad), "ck_connection_usage_model_shape"
                )

    def test_a_failure_code_exactly_when_the_call_failed(self):
        self.assertEqual(
            self.violates_usage(**self.settled(status="failed")),
            "ck_connection_usage_failure_matches_status",
        )
        self.assertEqual(
            self.violates_usage(**self.settled(failure_code="timeout")),
            "ck_connection_usage_failure_matches_status",
        )
        self.assertEqual(
            self.violates_usage(failure_code="timeout"),
            "ck_connection_usage_failure_matches_status",
        )

    def test_a_call_has_an_end_and_a_duration_exactly_when_it_is_settled(self):
        self.assertEqual(
            self.violates_usage(**self.settled(finished_at=None, duration_ms=None)),
            "ck_connection_usage_finished_matches_status",
        )
        self.assertEqual(
            self.violates_usage(
                finished_at=T0 + timedelta(seconds=1), duration_ms=1000
            ),
            "ck_connection_usage_finished_matches_status",
        )
        self.assertEqual(
            self.violates_usage(**self.settled(duration_ms=None)),
            "ck_connection_usage_duration_matches_finish",
        )
        # An end without a duration is the same rule from the other side.
        self.assertEqual(
            self.violates_usage(**self.settled(finished_at=None)),
            "ck_connection_usage_duration_matches_finish",
        )

    def test_a_call_in_flight_has_no_tokens_yet(self):
        self.assertEqual(
            self.violates_usage(input_tokens=1),
            "ck_connection_usage_no_tokens_in_flight",
        )
        self.assertEqual(
            self.violates_usage(output_tokens=1),
            "ck_connection_usage_no_tokens_in_flight",
        )

    def test_a_call_cannot_end_before_it_started(self):
        self.assertEqual(
            self.violates_usage(**self.settled(finished_at=T0 - timedelta(seconds=1))),
            "ck_connection_usage_finish_after_start",
        )
        self.assertIsNone(
            self.violates_usage(**self.settled(finished_at=T0, duration_ms=0))
        )

    def test_durations_and_tokens_are_bounded(self):
        for name, high in (
            ("duration_ms", limits.MAX_DURATION_MS),
            ("input_tokens", limits.MAX_TOKENS_PER_CALL),
            ("output_tokens", limits.MAX_TOKENS_PER_CALL),
        ):
            with self.subTest(column=name):
                self.assertIsNone(self.violates_usage(**self.settled(**{name: high})))
                self.assertEqual(
                    self.violates_usage(**self.settled(**{name: high + 1})),
                    f"ck_connection_usage_{name}_range"
                    if name != "duration_ms"
                    else "ck_connection_usage_duration_range",
                )
                self.assertEqual(
                    self.violates_usage(**self.settled(**{name: -1})),
                    f"ck_connection_usage_{name}_range"
                    if name != "duration_ms"
                    else "ck_connection_usage_duration_range",
                )

    def test_a_usage_row_needs_an_existing_task(self):
        self.assertEqual(
            self.violates_usage(task_id=uuid.uuid4()),
            "fk_connection_usage_task_id_tasks",
        )

    def test_a_task_with_usage_cannot_be_deleted(self):
        task = self.task()
        self.usage(task_id=task)
        self.assertEqual(
            self.violation(
                lambda: self.session.execute(
                    text("DELETE FROM tasks WHERE id = :t"), {"t": task}
                )
            ),
            "fk_connection_usage_task_id_tasks",
        )

    def test_the_user_of_a_usage_row_is_history_not_a_reference(self):
        # A user that does not exist (or was deleted) does not block the row.
        self.assertIsNone(self.violates_usage(user_id=uuid.uuid4()))


if __name__ == "__main__":
    unittest.main()
