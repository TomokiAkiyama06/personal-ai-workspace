"""Revision 0022: models, migration and database must describe the same schema.

The first classes need no server. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the migration up and down, compare it with
the models through Alembic's autogenerate and through the catalogs, and check the
constraints, triggers and functions the migration creates.
"""

import io
import re
import unittest
import uuid
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, create_engine, text
from sqlalchemy.exc import DBAPIError

from paw_backend.auth import limits
from paw_backend.auth.models import (
    AuthMethod,
    PasskeyRequirement,
    RevokeReason,
    ThrottleScope,
)
from paw_backend.db import Base

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0022"
TABLES = (
    "password_credentials",
    "auth_sessions",
    "auth_throttles",
    "auth_policy",
    "auth_policy_changes",
)
SCHEMA = "paw_auth_drift_check"

HISTORY_INSERT = """
INSERT INTO auth_policy_changes (id, version, changed_by, changed_at,
    old_passkey_owner, new_passkey_owner, old_passkey_admin, new_passkey_admin,
    old_passkey_user, new_passkey_user, old_recommend_passkey_to_users,
    new_recommend_passkey_to_users, old_stepup_window_minutes,
    new_stepup_window_minutes)
VALUES (gen_random_uuid(), {v}, gen_random_uuid(), now(), {o}, 'required',
    'required', 'required', 'optional', 'optional', true, true, 30, 30)
"""
SESSION_INSERT = """
INSERT INTO auth_sessions (id, user_id, token_hash, remember_me, auth_method,
    device_label, created_at, last_used_at, idle_timeout_seconds, idle_expires_at,
    absolute_expires_at, stepup_at, stepup_method, revoked_at, revoked_reason)
VALUES (gen_random_uuid(), :u, :h, false, {method}, {label}, now(), now(), {idle},
    now() + interval '1 day', now() + interval '{absolute}', {stepup_at},
    {stepup_method}, {revoked_at}, {revoked_reason})
"""


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


def check_sql(table: str) -> dict[str, str]:
    return {
        constraint.name.removeprefix(f"ck_{table}_"): str(constraint.sqltext)
        for constraint in Base.metadata.tables[table].constraints
        if isinstance(constraint, CheckConstraint)
    }


def literals(sql: str) -> set[str]:
    return set(re.findall(r"'(\w+)'", sql))


class ModelsMetadataTest(unittest.TestCase):
    def test_the_schema_has_the_five_tables(self):
        for name in TABLES:
            self.assertIn(name, Base.metadata.tables)

    def test_every_constraint_and_index_has_a_conventional_name(self):
        prefixes = {
            "PrimaryKeyConstraint": "pk_",
            "ForeignKeyConstraint": "fk_",
            "UniqueConstraint": "uq_",
            "CheckConstraint": "ck_",
        }
        for name in TABLES:
            table = Base.metadata.tables[name]
            for constraint in table.constraints:
                with self.subTest(table=name, constraint=constraint.name):
                    self.assertIsNotNone(constraint.name)
                    self.assertTrue(
                        constraint.name.startswith(
                            prefixes[type(constraint).__name__] + name
                        )
                    )
                    self.assertLessEqual(len(constraint.name), 63)
            for index in table.indexes:
                with self.subTest(table=name, index=index.name):
                    self.assertTrue(index.name.startswith(f"ix_{name}_"))
                    self.assertLessEqual(len(index.name), 63)

    def test_the_allowed_values_of_the_database_are_those_of_the_code(self):
        expected = {
            ("auth_sessions", "auth_method_valid"): {m.value for m in AuthMethod},
            ("auth_sessions", "stepup_method_valid"): {m.value for m in AuthMethod},
            ("auth_sessions", "revoked_reason_valid"): {r.value for r in RevokeReason},
            ("auth_throttles", "scope_valid"): {s.value for s in ThrottleScope},
            ("auth_policy", "passkey_owner_valid"): {
                r.value for r in PasskeyRequirement
            },
            ("auth_policy", "passkey_admin_valid"): {
                r.value for r in PasskeyRequirement
            },
            ("auth_policy", "passkey_user_valid"): {
                r.value for r in PasskeyRequirement
            },
        }
        for (table, name), values in expected.items():
            with self.subTest(table=table, constraint=name):
                self.assertEqual(literals(check_sql(table)[name]), values)

    def test_the_migration_repeats_the_values_of_the_code(self):
        source = (
            Path(__file__)
            .resolve()
            .parents[1]
            .joinpath("migrations/versions/0022_login_session_password.py")
            .read_text()
        )
        for method in AuthMethod:
            self.assertIn(f"'{method.value}'", source)
        for reason in RevokeReason:
            self.assertIn(f"'{reason.value}'", source)
        for scope in ThrottleScope:
            self.assertIn(f"'{scope.value}'", source)

    def test_the_database_limits_match_the_code_limits(self):
        self.assertIn(
            f"BETWEEN 1 AND {limits.DEVICE_LABEL_MAX_LENGTH}",
            check_sql("auth_sessions")["device_label_length"],
        )
        self.assertEqual(limits.DEVICE_LABEL_MAX_LENGTH, 64)
        self.assertIn("= 32", check_sql("auth_sessions")["token_hash_length"])
        self.assertIn("= 32", check_sql("auth_throttles")["key_hash_length"])

    def test_no_table_can_hold_a_password_or_a_session_id_by_name(self):
        for name in TABLES:
            for column in Base.metadata.tables[name].columns:
                with self.subTest(table=name, column=column.name):
                    self.assertNotIn(column.name, {"password", "token", "session_id"})

    def test_created_at_style_columns_have_no_server_default_the_service_decides(self):
        for column in ("created_at", "last_used_at", "idle_expires_at"):
            self.assertIsNone(
                Base.metadata.tables["auth_sessions"].columns[column].server_default
            )


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_the_revision_follows_0087(self):
        self.assertEqual(previous_revision(), "0087")

    def test_upgrade_creates_the_tables_and_the_function(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        for name in TABLES:
            self.assertIn(f"CREATE TABLE {name} ", sql)
        self.assertIn("CREATE FUNCTION paw_activate_invited_user", sql)
        self.assertIn("SECURITY DEFINER", sql)
        self.assertIn("SET search_path = pg_catalog, pg_temp", sql)
        self.assertIn("REVOKE ALL ON FUNCTION", sql)

    def test_downgrade_drops_everything_it_created(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        for name in TABLES:
            self.assertIn(f"DROP TABLE {name};", sql)
        for function in (
            "paw_activate_invited_user(uuid, timestamptz)",
            "paw_reject_auth_policy_changes_change()",
            "paw_guard_auth_policy_update()",
        ):
            self.assertIn(f"DROP FUNCTION {function}", sql)

    def test_the_activation_function_changes_only_an_invited_user(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        self.assertIn("status = 'active'", sql)
        self.assertIn("status = 'invited'", sql)
        self.assertNotIn("DELETE FROM", sql)


def only_auth_objects(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in TABLES
    table = getattr(obj, "table", None)
    return table is None or table.name in TABLES


def catalog(connection, schema: str) -> dict[str, list[tuple]]:
    params = {"schema": schema, "tables": list(TABLES)}

    def clean(value):
        return value.replace(f"{schema}.", "") if isinstance(value, str) else value

    def rows(sql: str) -> list[tuple]:
        return sorted(
            tuple(clean(v) for v in row)
            for row in connection.execute(text(sql), params)
        )

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
class AuthMigrationDatabaseTest(unittest.TestCase):
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

    def functions(self) -> set[str]:
        return set(
            self.scalars(
                "SELECT proname FROM pg_proc p JOIN pg_namespace n "
                "ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
                "AND proname LIKE 'paw\\_%'"
            )
        )

    def run_sql(self, sql: str, **params) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(sql), params)

    def call(self, sql: str, **params):
        """The first column of the first row of ``sql``, committed."""
        with self.engine.begin() as connection:
            return connection.execute(text(sql), params).scalar()

    def refused(self, sql: str, **params) -> str:
        """The SQLSTATE of the error ``sql`` raises."""
        with self.assertRaises(DBAPIError) as caught:
            self.run_sql(sql, **params)
        return caught.exception.orig.sqlstate

    # -- up and down -------------------------------------------------------------

    def test_upgrade_creates_the_schema_and_downgrade_removes_it(self):
        previous = previous_revision()

        migrate("upgrade", REVISION)

        self.assertTrue(set(TABLES) <= self.tables())
        self.assertEqual(
            self.scalars("SELECT version_num FROM alembic_version"), [REVISION]
        )
        self.assertTrue(
            {
                "paw_activate_invited_user",
                "paw_guard_auth_policy_update",
                "paw_reject_auth_policy_changes_change",
            }
            <= self.functions()
        )

        migrate("downgrade", previous)

        self.assertEqual(self.tables() & set(TABLES), set())
        self.assertEqual(
            self.scalars("SELECT version_num FROM alembic_version"), [previous]
        )
        self.assertEqual(
            self.functions()
            & {
                "paw_activate_invited_user",
                "paw_guard_auth_policy_update",
                "paw_reject_auth_policy_changes_change",
            },
            set(),
        )
        # Nothing of the layers below it is touched.
        self.assertIn("users", self.tables())
        self.assertIn("audit_events", self.tables())
        self.assertIn("projects", self.tables())

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        migrate("upgrade", REVISION)
        migrate("downgrade", previous_revision())
        migrate("upgrade", REVISION)

        self.assertTrue(set(TABLES) <= self.tables())
        self.assertEqual(self.scalars("SELECT count(*) FROM auth_policy"), [1])

    def test_head_contains_the_schema(self):
        migrate("upgrade", "head")
        self.assertTrue(set(TABLES) <= self.tables())

    def test_the_downgrade_leaves_users_and_the_audit_trail_alone(self):
        migrate("upgrade", REVISION)
        user = uuid.uuid4()
        self.run_sql(
            "INSERT INTO users (id, login_name, system_role, status, passkey_required,"
            " created_at, updated_at) VALUES (:id, 'alice', 'user', 'active', false,"
            " now(), now())",
            id=user,
        )

        migrate("downgrade", previous_revision())

        self.assertEqual(self.scalars("SELECT count(*) FROM users"), [1])

    # -- drift -----------------------------------------------------------------------

    def autogenerate_diff(self, connection) -> list:
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_object": only_auth_objects,
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
                "DROP INDEX ix_auth_sessions_user_id_active",
                "ALTER TABLE auth_sessions ADD COLUMN unexpected text",
                "ALTER TABLE auth_sessions ALTER COLUMN device_label SET NOT NULL",
                "ALTER TABLE auth_throttles ALTER COLUMN attempts TYPE bigint",
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
            ["add_index", "modify_nullable", "modify_type", "remove_column"],
        )

    def created_catalog(self) -> dict[str, list[tuple]]:
        """The catalog of a schema built from the models with ``create_all``."""
        tables = [Base.metadata.tables["users"]] + [
            Base.metadata.tables[name] for name in TABLES
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
            "pk_password_credentials",
            "fk_password_credentials_user_id_users",
            "ck_password_credentials_hash_is_argon2id",
            "uq_auth_sessions_token_hash",
            "fk_auth_sessions_user_id_users",
            "ck_auth_sessions_idle_within_absolute",
            "ck_auth_sessions_revocation_complete",
            "pk_auth_throttles",
            "ck_auth_throttles_scope_valid",
            "ck_auth_policy_single_row",
            "ck_auth_policy_stepup_window_range",
            "uq_auth_policy_changes_version",
        ):
            self.assertIn(expected, names)
        self.assertEqual(
            {row[1] for row in migrated["indexes"] if row[1].startswith("ix_")},
            {
                "ix_auth_sessions_user_id_active",
                "ix_auth_sessions_idle_expires_at",
                "ix_auth_sessions_revoked_at",
                "ix_auth_throttles_last_attempt_at",
            },
        )
        self.assertEqual(
            len(migrated["columns"]),
            sum(len(Base.metadata.tables[name].columns) for name in TABLES),
        )

    # -- the values, constraints and triggers of the migration ------------------

    def test_the_policy_is_seeded_with_the_requirements_default(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection:
            row = connection.execute(text("SELECT * FROM auth_policy")).one()
        self.assertEqual(
            (
                row.id,
                row.version,
                row.passkey_owner,
                row.passkey_admin,
                row.passkey_user,
                row.recommend_passkey_to_users,
                row.stepup_window_minutes,
                row.updated_by,
            ),
            (1, 1, "required", "required", "optional", True, 30, None),
        )

    def test_the_policy_table_holds_one_row_only(self):
        migrate("upgrade", "head")
        self.assertEqual(
            self.refused(
                "INSERT INTO auth_policy (id, version, passkey_owner, passkey_admin,"
                " passkey_user, recommend_passkey_to_users, stepup_window_minutes,"
                " updated_at) VALUES (2, 1, 'required', 'required', 'optional', true,"
                " 30, now())"
            ),
            "23514",
        )
        self.assertEqual(
            self.refused(
                "INSERT INTO auth_policy (id, version, passkey_owner, passkey_admin,"
                " passkey_user, recommend_passkey_to_users, stepup_window_minutes,"
                " updated_at) VALUES (1, 1, 'required', 'required', 'optional', true,"
                " 30, now())"
            ),
            "23505",
        )

    def test_a_policy_change_must_move_the_version_up_by_exactly_one(self):
        migrate("upgrade", "head")
        for version in (1, 3, 0):
            with self.subTest(version=version):
                self.assertEqual(
                    self.refused("UPDATE auth_policy SET version = :v", v=version),
                    "23001",
                )
        self.run_sql("UPDATE auth_policy SET version = 2")
        self.assertEqual(self.scalars("SELECT version FROM auth_policy"), [2])
        self.assertEqual(
            self.refused("UPDATE auth_policy SET id = 1, version = 2"), "23001"
        )

    def test_the_policy_values_are_checked_by_the_database(self):
        migrate("upgrade", "head")
        for column, value in (
            ("passkey_owner", "'sometimes'"),
            ("passkey_admin", "''"),
            ("passkey_user", "'REQUIRED'"),
            ("stepup_window_minutes", "4"),
            ("stepup_window_minutes", "241"),
        ):
            with self.subTest(column=column, value=value):
                self.assertEqual(
                    self.refused(
                        f"UPDATE auth_policy SET version = 2, {column} = {value}"
                    ),
                    "23514",
                )
        for value in (5, 240):
            self.run_sql(
                "UPDATE auth_policy SET version = version + 1,"
                " stepup_window_minutes = :v",
                v=value,
            )

    def test_the_policy_history_is_append_only(self):
        migrate("upgrade", "head")
        self.run_sql(HISTORY_INSERT.format(v=2, o="'required'"))
        self.assertEqual(
            self.refused("UPDATE auth_policy_changes SET version = 3"), "23001"
        )
        self.assertEqual(self.refused("DELETE FROM auth_policy_changes"), "23001")
        self.assertEqual(self.scalars("SELECT count(*) FROM auth_policy_changes"), [1])

    def test_the_history_rejects_a_version_before_the_first_change_and_bad_values(self):
        migrate("upgrade", "head")
        for version, old in ((1, "'required'"), (2, "'maybe'")):
            with self.subTest(version=version, old=old):
                self.assertEqual(
                    self.refused(HISTORY_INSERT.format(v=version, o=old)), "23514"
                )

    def test_a_session_row_is_checked_by_the_database(self):
        migrate("upgrade", "head")
        user = uuid.uuid4()
        self.run_sql(
            "INSERT INTO users (id, login_name, system_role, status, passkey_required,"
            " created_at, updated_at) VALUES (:id, 'alice', 'user', 'active', false,"
            " now(), now())",
            id=user,
        )
        insert = SESSION_INSERT
        good = dict(
            method="'password'",
            label="'laptop'",
            idle=60,
            absolute="2 days",
            stepup_at="NULL",
            stepup_method="NULL",
            revoked_at="NULL",
            revoked_reason="NULL",
        )
        self.run_sql(insert.format(**good), u=user, h=b"h" * 32)
        for changes, why in (
            ({"method": "'sms'"}, "unknown method"),
            ({"label": "''"}, "empty label"),
            ({"label": "'" + "x" * 65 + "'"}, "label too long"),
            ({"idle": 0}, "idle timeout"),
            ({"absolute": "0 seconds"}, "absolute before the idle expiry"),
            ({"stepup_at": "now()"}, "step-up time without its method"),
            ({"stepup_method": "'password'"}, "step-up method without its time"),
            ({"revoked_at": "now()"}, "revoked without a reason"),
            ({"revoked_reason": "'logout'"}, "a reason without a revocation"),
            ({"revoked_at": "now()", "revoked_reason": "'boredom'"}, "unknown reason"),
        ):
            with self.subTest(why=why):
                self.assertEqual(
                    self.refused(
                        insert.format(**{**good, **changes}), u=user, h=b"z" * 32
                    ),
                    "23514",
                )
        # A token hash must be 32 bytes, and unique.
        self.assertEqual(
            self.refused(insert.format(**good), u=user, h=b"short"), "23514"
        )
        self.assertEqual(
            self.refused(insert.format(**good), u=user, h=b"h" * 32), "23505"
        )

    def test_deleting_a_user_deletes_its_password_and_sessions(self):
        migrate("upgrade", "head")
        user = uuid.uuid4()
        self.run_sql(
            "INSERT INTO users (id, login_name, system_role, status, passkey_required,"
            " created_at, updated_at) VALUES (:id, 'alice', 'user', 'active', false,"
            " now(), now())",
            id=user,
        )
        self.run_sql(
            "INSERT INTO password_credentials (user_id, hash, created_at, changed_at)"
            " VALUES (:id, '$argon2id$x', now(), now())",
            id=user,
        )
        self.assertEqual(
            self.refused(
                """INSERT INTO password_credentials
                (user_id, hash, created_at, changed_at)
                VALUES (gen_random_uuid(), '$argon2id$x', now(), now())"""
            ),
            "23503",
        )
        self.assertEqual(
            self.refused(
                "UPDATE password_credentials SET hash = 'plain-text-password'"
            ),
            "23514",
        )
        self.run_sql("DELETE FROM users WHERE id = :id", id=user)
        self.assertEqual(self.scalars("SELECT count(*) FROM password_credentials"), [0])

    def test_the_activation_function_activates_an_invited_user_and_nobody_else(self):
        migrate("upgrade", "head")
        ids = {}
        for status in ("invited", "active", "pending_deletion", "deleted"):
            ids[status] = uuid.uuid4()
            self.run_sql(
                "INSERT INTO users (id, login_name, system_role, status,"
                " passkey_required, created_at, updated_at) VALUES (:id, :name, 'user',"
                " :status, false, now(), now())",
                id=ids[status],
                name=f"user-{status}".replace("_", "-"),
                status=status,
            )
        results = {}
        for status, user in ids.items():
            results[status] = self.call(
                "SELECT paw_activate_invited_user(:id, '2031-01-01T00:00:00Z')", id=user
            )
        self.assertEqual(
            results,
            {
                "invited": True,
                "active": False,
                "pending_deletion": False,
                "deleted": False,
            },
        )
        self.assertEqual(
            sorted(self.scalars("SELECT status FROM users")),
            ["active", "active", "deleted", "pending_deletion"],
        )
        # ... and only the one that was invited changed, with the given time.
        self.assertEqual(
            self.scalars(
                "SELECT status FROM users WHERE updated_at = '2031-01-01T00:00:00Z'"
            ),
            ["active"],
        )

    def test_the_activation_function_is_security_definer_with_a_pinned_search_path(
        self,
    ):
        migrate("upgrade", "head")
        row = (
            self.engine.connect()
            .execute(
                text(
                    "SELECT prosecdef, proconfig FROM pg_proc "
                    "WHERE proname = 'paw_activate_invited_user'"
                )
            )
            .one()
        )
        self.assertTrue(row.prosecdef)
        self.assertEqual(list(row.proconfig), ["search_path=pg_catalog, pg_temp"])

    def test_a_temporary_table_cannot_stand_in_for_users(self):
        migrate("upgrade", "head")
        user = uuid.uuid4()
        self.run_sql(
            "INSERT INTO users (id, login_name, system_role, status, passkey_required,"
            " created_at, updated_at) VALUES (:id, 'alice', 'user', 'invited', false,"
            " now(), now())",
            id=user,
        )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TEMP TABLE users "
                    "(id uuid, status text, updated_at timestamptz)"
                )
            )
            connection.execute(
                text("INSERT INTO pg_temp.users VALUES (:id, 'invited', now())"),
                {"id": user},
            )
            connection.execute(
                text("SELECT paw_activate_invited_user(:id, now())"), {"id": user}
            )
            shadow = connection.execute(
                text("SELECT status FROM pg_temp.users")
            ).scalar()
            # The pooled connection is reused: the shadow must not outlive the test.
            connection.execute(text("DROP TABLE pg_temp.users"))
        self.assertEqual(shadow, "invited")  # the temp table was not touched
        self.assertEqual(self.scalars("SELECT status FROM users"), ["active"])


if __name__ == "__main__":
    unittest.main()
