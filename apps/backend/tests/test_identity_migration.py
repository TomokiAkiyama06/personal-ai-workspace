"""Revision 0021: models, migration and database must describe the same schema.

The first classes need no server. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the migration up and down, check every
constraint the design relies on, compare the migration with the models
(Alembic's autogenerate and a catalog comparison, which also sees partial
indexes and CHECK definitions) and check the grants to the application role.
"""

import io
import unittest
import uuid
from datetime import UTC, datetime, timedelta

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from paw_backend.authz import SystemRole
from paw_backend.db import Base
from paw_backend.identity import (
    PASSKEY_REQUIRED_ROLES,
    TokenPurpose,
    UserStatus,
    passkey_required_for,
)
from paw_backend.identity.models import USER_ROLES

from .identity_support import (
    migrate,
    requires_postgres,
    sync_database_url,
)
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0021"
TABLES = ("users", "setup_tokens")
SCRATCH_SCHEMA = "paw_drift_check_021"
NOW = datetime(2030, 1, 1, tzinfo=UTC)


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


class ModelsMetadataTest(unittest.TestCase):
    def tables(self):
        return [Base.metadata.tables[name] for name in TABLES]

    def test_the_two_tables_are_registered(self):
        self.assertEqual(
            {name for name in Base.metadata.tables} & set(TABLES), set(TABLES)
        )

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
                    self.assertTrue(index.name.startswith(f"uq_{table.name}_"))
                    self.assertTrue(index.unique)

    def test_the_tokens_belong_to_a_user_and_go_with_it(self):
        foreign_keys = [
            constraint
            for constraint in Base.metadata.tables["setup_tokens"].constraints
            if isinstance(constraint, ForeignKeyConstraint)
        ]

        self.assertEqual(len(foreign_keys), 1)
        self.assertEqual(foreign_keys[0].referred_table.name, "users")
        self.assertEqual(foreign_keys[0].ondelete, "CASCADE")

    def test_the_allowed_values_follow_the_design(self):
        self.assertEqual(
            {status.value for status in UserStatus},
            {"invited", "active", "pending_deletion", "deleted"},
        )
        self.assertEqual(
            {purpose.value for purpose in TokenPurpose}, {"setup", "recovery"}
        )
        self.assertEqual(
            {role.value for role in USER_ROLES}, {"owner", "admin", "user"}
        )
        checks = {
            constraint.name: str(constraint.sqltext)
            for table in self.tables()
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        self.assertIn(
            "'invited', 'active', 'pending_deletion', 'deleted'",
            checks["ck_users_status_valid"],
        )

    def test_a_passkey_is_mandatory_for_the_owner_and_admin_only(self):
        self.assertEqual(
            set(PASSKEY_REQUIRED_ROLES), {SystemRole.OWNER, SystemRole.ADMIN}
        )
        self.assertEqual(
            {role: passkey_required_for(role) for role in SystemRole},
            {
                SystemRole.OWNER: True,
                SystemRole.ADMIN: True,
                SystemRole.USER: False,
                SystemRole.SYSTEM: False,
            },
        )

    def test_the_users_table_has_no_credential_column(self):
        names = {column.name for column in Base.metadata.tables["users"].columns}

        self.assertEqual(
            names,
            {
                "id",
                "login_name",
                "system_role",
                "status",
                "passkey_required",
                "created_at",
                "updated_at",
            },
        )


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str, **environment: str) -> str:
        output = io.StringIO()
        variables = {
            "PAW_DATABASE_URL": "postgresql://u:p@db.invalid/paw",
            **environment,
        }
        with paw_environment(**variables):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_upgrade_creates_the_tables_and_the_single_owner_index(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")

        self.assertIn("CREATE TABLE users ", sql)
        self.assertIn("CREATE TABLE setup_tokens ", sql)
        self.assertIn(
            "CREATE UNIQUE INDEX uq_users_single_owner ON users (system_role) "
            "WHERE system_role = 'owner'",
            sql,
        )
        self.assertNotIn("GRANT", sql)

    def test_the_web_role_gets_no_insert_and_no_broad_update(self):
        sql = self.sql(
            "upgrade",
            f"{previous_revision()}:{REVISION}",
            PAW_APP_DATABASE_ROLE="paw_app",
        )

        self.assertIn('GRANT SELECT ON users, setup_tokens TO "paw_app"', sql)
        self.assertIn('GRANT UPDATE (updated_at) ON users TO "paw_app"', sql)
        self.assertIn(
            'GRANT UPDATE (attempts, used_at, locked_at) ON setup_tokens TO "paw_app"',
            sql,
        )
        grants = [line for line in sql.splitlines() if line.startswith("GRANT")]
        self.assertEqual(len(grants), 3)
        for line in grants:
            self.assertNotIn("INSERT", line)
            self.assertNotIn("DELETE", line)

    def test_the_operator_role_may_insert_and_update_only_what_the_commands_need(
        self,
    ):
        sql = self.sql(
            "upgrade",
            f"{previous_revision()}:{REVISION}",
            PAW_OPERATOR_DATABASE_ROLE="paw_op",
        )

        self.assertIn('GRANT SELECT, INSERT ON users, setup_tokens TO "paw_op"', sql)
        self.assertIn(
            'GRANT UPDATE (system_role, updated_at) ON users TO "paw_op"', sql
        )
        self.assertIn('GRANT UPDATE (revoked_at) ON setup_tokens TO "paw_op"', sql)
        self.assertIn("IF to_regclass('audit_events') IS NOT NULL", sql)
        self.assertIn('GRANT INSERT ON audit_events TO "paw_op"', sql)
        self.assertNotIn("DELETE", sql.replace("ON DELETE CASCADE", ""))
        self.assertNotIn('TO "paw_app"', sql)

    def test_a_hostile_role_name_never_reaches_the_sql(self):
        output = io.StringIO()
        with paw_environment(
            PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw",
            PAW_OPERATOR_DATABASE_ROLE='x"; DROP TABLE users; --',
        ):
            with self.assertRaises(ValidationError):
                command.upgrade(offline_config(output), "head", sql=True)
        self.assertNotIn("DROP TABLE users", output.getvalue())

    def test_the_setup_token_guard_trigger_is_created_and_dropped(self):
        up = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        down = self.sql("downgrade", f"{REVISION}:{previous_revision()}")

        self.assertIn("CREATE FUNCTION paw_guard_setup_tokens_update()", up)
        self.assertIn(
            "ALTER TABLE setup_tokens ENABLE ALWAYS TRIGGER "
            "tr_setup_tokens_guard_update",
            up,
        )
        self.assertIn("DROP FUNCTION paw_guard_setup_tokens_update()", down)

    def test_a_shared_role_and_a_missing_operator_role_are_warned_about(self):
        with self.assertLogs("paw_backend.migrations.0021", level="WARNING") as logs:
            self.sql(
                "upgrade",
                f"{previous_revision()}:{REVISION}",
                PAW_APP_DATABASE_ROLE="paw_same",
                PAW_OPERATOR_DATABASE_ROLE="paw_same",
            )
        self.assertIn("the same role", "\n".join(logs.output))
        with self.assertLogs("paw_backend.migrations.0021", level="WARNING") as logs:
            self.sql(
                "upgrade",
                f"{previous_revision()}:{REVISION}",
                PAW_MIGRATION_DATABASE_URL="postgresql://own:pw@db.invalid/paw",
            )
        text_ = "\n".join(logs.output)
        self.assertIn("PAW_OPERATOR_DATABASE_ROLE", text_)
        self.assertNotIn("pw@", text_)
        with self.assertNoLogs("paw_backend.migrations.0021", level="WARNING"):
            self.sql("upgrade", f"{previous_revision()}:{REVISION}")

    def test_downgrade_drops_the_tokens_before_the_users(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")

        self.assertLess(
            sql.index("DROP TABLE setup_tokens"), sql.index("DROP TABLE users")
        )


def catalog(connection, schema: str) -> dict[str, list[tuple]]:
    """Columns, constraints and indexes of the two tables in ``schema``.

    Schema qualifiers are removed so that two schemas can be compared.
    """
    params = {"schema": schema, "tables": list(TABLES)}

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


def only_identity_objects(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in TABLES
    table = getattr(obj, "table", None)
    return table is None or table.name in TABLES


@requires_postgres
class IdentityMigrationDatabaseTest(unittest.TestCase):
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

    # -- up and down --------------------------------------------------------

    def test_upgrade_creates_the_tables_and_downgrade_removes_them(self):
        previous = previous_revision()

        migrate("upgrade", REVISION)

        self.assertTrue(set(TABLES) <= self.tables())
        self.assertEqual(
            self.scalars("SELECT version_num FROM alembic_version"), [REVISION]
        )

        migrate("downgrade", previous)

        self.assertEqual(self.tables() & set(TABLES), set())
        self.assertEqual(
            self.scalars(
                "SELECT indexname FROM pg_indexes "
                "WHERE indexname LIKE 'uq\\_users\\_%' "
                "OR indexname LIKE 'uq\\_setup\\_tokens\\_%'"
            ),
            [],
        )
        migrate("upgrade", REVISION)
        self.assertTrue(set(TABLES) <= self.tables())

    def test_head_contains_the_tables(self):
        migrate("upgrade", "head")

        self.assertTrue(set(TABLES) <= self.tables())

    # -- drift --------------------------------------------------------------

    def autogenerate_diff(self, connection) -> list:
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_object": only_identity_objects,
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
            connection.execute(text("ALTER TABLE users ADD COLUMN unexpected text"))
            connection.execute(
                text("ALTER TABLE users ALTER COLUMN login_name DROP NOT NULL")
            )
            diff = self.autogenerate_diff(connection)
            transaction.rollback()

        operations = [
            step
            for entry in diff
            for step in (entry if isinstance(entry, list) else [entry])
        ]
        self.assertEqual(
            sorted({operation[0] for operation in operations}),
            ["modify_nullable", "remove_column"],
        )

    def scratch_catalog(self) -> dict[str, list[tuple]]:
        """The catalog of a schema built from the models with ``create_all``."""
        tables = [Base.metadata.tables[name] for name in TABLES]
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
        self.assertIn("ck_users_passkey_required_for_privileged", names)
        self.assertIn("fk_setup_tokens_user_id_users", names)
        definitions = {row[1]: row[2] for row in migrated["indexes"]}
        self.assertIn(
            "WHERE (system_role = 'owner'::text)", definitions["uq_users_single_owner"]
        )
        self.assertIn(
            "WHERE ((used_at IS NULL) AND (revoked_at IS NULL))",
            definitions["uq_setup_tokens_one_outstanding"],
        )

    # -- what the database itself enforces ----------------------------------

    def insert_user(self, connection, **overrides) -> uuid.UUID:
        values = {
            "id": uuid.uuid4(),
            "login_name": f"user{uuid.uuid4().hex[:8]}",
            "system_role": "user",
            "status": "active",
            "passkey_required": False,
            "created_at": NOW,
            "updated_at": NOW,
        }
        values.update(overrides)
        connection.execute(
            text(
                "INSERT INTO users VALUES (:id, :login_name, :system_role, :status, "
                ":passkey_required, :created_at, :updated_at)"
            ),
            values,
        )
        return values["id"]

    def insert_token(self, connection, user_id, **overrides) -> uuid.UUID:
        values = {
            "id": uuid.uuid4(),
            "audit_ref": uuid.uuid4(),
            "user_id": user_id,
            "purpose": "setup",
            "salt": bytes(16),
            "secret_hash": bytes(32),
            "created_at": NOW,
            "expires_at": NOW + timedelta(minutes=30),
            "used_at": None,
            "revoked_at": None,
            "attempts": 0,
            "locked_at": None,
            "issued_by_uid": None,
            "issued_by_sudo_uid": None,
        }
        values.update(overrides)
        connection.execute(
            text(
                "INSERT INTO setup_tokens (id, audit_ref, user_id, purpose, salt, "
                "secret_hash, created_at, expires_at, used_at, revoked_at, attempts, "
                "locked_at, issued_by_uid, issued_by_sudo_uid) VALUES (:id, "
                ":audit_ref, :user_id, :purpose, :salt, :secret_hash, :created_at, "
                ":expires_at, :used_at, :revoked_at, :attempts, :locked_at, "
                ":issued_by_uid, :issued_by_sudo_uid)"
            ),
            values,
        )
        return values["id"]

    def violated(self, action) -> str:
        """The constraint that ``action(connection)`` violates (it must)."""
        with self.assertRaises(IntegrityError) as caught:
            with self.engine.begin() as connection:
                action(connection)
        return caught.exception.orig.diag.constraint_name

    def test_a_second_owner_is_rejected_by_the_database(self):
        migrate("upgrade", "head")
        with self.engine.begin() as connection:
            self.insert_user(connection, system_role="owner", passkey_required=True)

        def second(connection):
            self.insert_user(connection, system_role="owner", passkey_required=True)

        self.assertEqual(self.violated(second), "uq_users_single_owner")
        # Admins and users are not limited.
        with self.engine.begin() as connection:
            self.insert_user(connection, system_role="admin", passkey_required=True)
            self.insert_user(connection, system_role="admin", passkey_required=True)

    def test_an_owner_can_be_replaced_when_the_old_one_is_demoted_first(self):
        migrate("upgrade", "head")
        with self.engine.begin() as connection:
            old = self.insert_user(
                connection, system_role="owner", passkey_required=True
            )
            new = self.insert_user(
                connection, system_role="admin", passkey_required=True
            )
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE users SET system_role = 'admin' WHERE id = :id"),
                {"id": old},
            )
            connection.execute(
                text("UPDATE users SET system_role = 'owner' WHERE id = :id"),
                {"id": new},
            )

        self.assertEqual(
            self.scalars("SELECT id FROM users WHERE system_role = 'owner'"), [new]
        )

    def test_the_login_name_must_be_unique_and_normalised(self):
        migrate("upgrade", "head")
        with self.engine.begin() as connection:
            self.insert_user(connection, login_name="alice")

        self.assertEqual(
            self.violated(lambda c: self.insert_user(c, login_name="alice")),
            "uq_users_login_name",
        )
        for name in (
            "Alice",
            "ab",
            "a" * 65,
            ".abc",
            "abc-",
            "a b c",
            "abc\n",
            "ａｂｃ",
        ):
            with self.subTest(name):
                self.assertEqual(
                    self.violated(lambda c, n=name: self.insert_user(c, login_name=n)),
                    "ck_users_login_name_normalised",
                )
        with self.engine.begin() as connection:
            for name in ("abc", "a" * 64, "a.b_c-d", "007"):
                self.insert_user(connection, login_name=name)

    def test_a_passkey_is_required_of_an_owner_or_admin_but_not_of_a_user(self):
        migrate("upgrade", "head")

        for role in ("owner", "admin"):
            with self.subTest(role):
                self.assertEqual(
                    self.violated(
                        lambda c, r=role: self.insert_user(
                            c, system_role=r, passkey_required=False
                        )
                    ),
                    "ck_users_passkey_required_for_privileged",
                )
        with self.engine.begin() as connection:
            self.insert_user(connection, system_role="user", passkey_required=False)
            self.insert_user(connection, system_role="user", passkey_required=True)

    def test_role_and_status_are_limited_to_the_documented_values(self):
        migrate("upgrade", "head")

        self.assertEqual(
            self.violated(lambda c: self.insert_user(c, system_role="system")),
            "ck_users_system_role_valid",
        )
        self.assertEqual(
            self.violated(lambda c: self.insert_user(c, status="suspended")),
            "ck_users_status_valid",
        )
        with self.engine.begin() as connection:
            for status in ("invited", "active", "pending_deletion", "deleted"):
                self.insert_user(connection, status=status)

    def test_token_rows_are_constrained(self):
        migrate("upgrade", "head")
        with self.engine.begin() as connection:
            user = self.insert_user(connection)
        cases = {
            "ck_setup_tokens_purpose_valid": {"purpose": "login"},
            "ck_setup_tokens_salt_length": {"salt": bytes(15)},
            "ck_setup_tokens_secret_hash_length": {"secret_hash": bytes(31)},
            "ck_setup_tokens_expires_after_creation": {"expires_at": NOW},
            "ck_setup_tokens_attempts_not_negative": {"attempts": -1},
            "ck_setup_tokens_used_or_revoked": {"used_at": NOW, "revoked_at": NOW},
            "ck_setup_tokens_issued_by_uid_range": {"issued_by_uid": -1},
            "ck_setup_tokens_issued_by_sudo_uid_range": {
                "issued_by_sudo_uid": 4_294_967_296
            },
        }
        for constraint, overrides in cases.items():
            with self.subTest(constraint):
                self.assertEqual(
                    self.violated(
                        lambda c, o=overrides: self.insert_token(c, user, **o)
                    ),
                    constraint,
                )
        self.assertEqual(
            self.violated(lambda c: self.insert_token(c, uuid.uuid4())),
            "fk_setup_tokens_user_id_users",
        )

    def test_a_user_has_at_most_one_outstanding_token(self):
        migrate("upgrade", "head")
        with self.engine.begin() as connection:
            user = self.insert_user(connection)
            self.insert_token(connection, user)

        self.assertEqual(
            self.violated(lambda c: self.insert_token(c, user)),
            "uq_setup_tokens_one_outstanding",
        )
        # A used or revoked token is not outstanding, however many there are.
        with self.engine.begin() as connection:
            self.insert_token(connection, user, used_at=NOW)
            self.insert_token(connection, user, used_at=NOW)
            self.insert_token(connection, user, revoked_at=NOW)

    def test_the_audit_reference_is_unique_and_not_the_lookup_id(self):
        migrate("upgrade", "head")
        ref = uuid.uuid4()
        with self.engine.begin() as connection:
            first = self.insert_user(connection)
            self.insert_token(connection, first, audit_ref=ref)
            second = self.insert_user(connection)

        self.assertEqual(
            self.violated(lambda c: self.insert_token(c, second, audit_ref=ref)),
            "uq_setup_tokens_audit_ref",
        )
        self.assertEqual(
            self.scalars("SELECT count(*) FROM setup_tokens WHERE id = audit_ref"), [0]
        )

    def test_deleting_a_user_removes_the_tokens(self):
        migrate("upgrade", "head")
        with self.engine.begin() as connection:
            user = self.insert_user(connection)
            self.insert_token(connection, user)
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user})

        self.assertEqual(self.scalars("SELECT count(*) FROM setup_tokens"), [0])

    # -- database roles -------------------------------------------------------
    # The privileges themselves are tested with real non-superuser roles in
    # test_owner_token_roles.py.

    def test_a_missing_role_fails_the_migration_loudly(self):
        for setting in ("PAW_APP_DATABASE_ROLE", "PAW_OPERATOR_DATABASE_ROLE"):
            with self.subTest(setting):
                with self.assertRaises(RuntimeError):
                    migrate("upgrade", "head", **{setting: "paw_no_such_role_021"})
                migrate("downgrade", "base")

    # -- the trigger that guards the token rows ------------------------------

    def token_row(self, connection, **overrides) -> uuid.UUID:
        user = self.insert_user(connection)
        return self.insert_token(connection, user, **overrides)

    def refused(self, sql: str, **params) -> str:
        """The SQLSTATE of the error ``sql`` raises (it must raise)."""
        with self.assertRaises(DBAPIError) as caught:
            with self.engine.begin() as connection:
                connection.execute(text(sql), params)
        return caught.exception.orig.sqlstate

    def test_a_token_row_can_only_change_as_a_token_lives(self):
        migrate("upgrade", "head")
        with self.engine.begin() as connection:
            token = self.token_row(connection)
        allowed = (
            "UPDATE setup_tokens SET attempts = attempts + 1 WHERE id = :id",
            "UPDATE setup_tokens SET locked_at = :now WHERE id = :id",
            "UPDATE setup_tokens SET revoked_at = :now WHERE id = :id",
        )
        with self.engine.begin() as connection:
            for sql in allowed:
                connection.execute(text(sql), {"id": token, "now": NOW})

        self.assertEqual(
            self.scalars("SELECT attempts FROM setup_tokens WHERE id = :id", id=token),
            [1],
        )

    def test_a_token_row_cannot_be_forged_or_revived_by_any_role(self):
        migrate("upgrade", "head")
        with self.engine.begin() as connection:
            fresh = self.token_row(connection)
            used = self.token_row(connection, used_at=NOW, attempts=2)
            revoked = self.token_row(connection, revoked_at=NOW)
            locked = self.token_row(connection, locked_at=NOW, attempts=5)
        cases = {
            "salt": ("UPDATE setup_tokens SET salt = :b16 WHERE id = :id", fresh),
            "secret_hash": (
                "UPDATE setup_tokens SET secret_hash = :b32 WHERE id = :id",
                fresh,
            ),
            "expiry": (
                "UPDATE setup_tokens SET expires_at = expires_at + interval '1 day' "
                "WHERE id = :id",
                fresh,
            ),
            "purpose": (
                "UPDATE setup_tokens SET purpose = 'recovery' WHERE id = :id",
                fresh,
            ),
            "owner of the token": (
                "UPDATE setup_tokens SET user_id = gen_random_uuid() WHERE id = :id",
                fresh,
            ),
            "audit_ref": (
                "UPDATE setup_tokens SET audit_ref = gen_random_uuid() WHERE id = :id",
                fresh,
            ),
            "issued_by_uid": (
                "UPDATE setup_tokens SET issued_by_uid = 0 WHERE id = :id",
                fresh,
            ),
            "un-use a token": (
                "UPDATE setup_tokens SET used_at = NULL WHERE id = :id",
                used,
            ),
            "un-revoke a token": (
                "UPDATE setup_tokens SET revoked_at = NULL WHERE id = :id",
                revoked,
            ),
            "un-lock a token": (
                "UPDATE setup_tokens SET locked_at = NULL WHERE id = :id",
                locked,
            ),
            "reset the attempts": (
                "UPDATE setup_tokens SET attempts = 0 WHERE id = :id",
                used,
            ),
        }
        for name, (sql, token) in cases.items():
            with self.subTest(name):
                self.assertEqual(
                    self.refused(
                        sql, id=token, b16=bytes([1]) * 16, b32=bytes([1]) * 32
                    ),
                    "23001",  # restrict_violation
                )


if __name__ == "__main__":
    unittest.main()
