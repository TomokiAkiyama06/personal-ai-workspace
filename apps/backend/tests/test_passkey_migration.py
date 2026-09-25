"""Migration 0023: Passkeys, challenges and the session gate (PostgreSQL).

Up, down and up again; no drift between the models and the migration (Alembic's
autogenerate and a catalog comparison); every constraint the database enforces,
tried with values on both sides of its boundary; what the downgrade destroys and
what it must leave alone.
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

from paw_backend.auth import models as auth_models
from paw_backend.auth.models import PasskeyGate, RevokeReason
from paw_backend.auth.passkeys.models import (
    CHALLENGE_BYTES,
    CREDENTIAL_ID_MAX_BYTES,
    CREDENTIAL_ID_MIN_BYTES,
    PASSKEY_NAME_MAX_LENGTH,
    PUBLIC_KEY_MAX_BYTES,
    PUBLIC_KEY_MIN_BYTES,
    SIGN_COUNT_MAX,
    PasskeyPurpose,
    PasskeyRevokeReason,
)
from paw_backend.db import Base

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0023"
PREVIOUS = "0027"
NEW_TABLES = ("user_passkeys", "passkey_challenges")
COMPARED = (*NEW_TABLES, "auth_sessions")
SCHEMA = "paw_passkey_drift_check"
VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def check_sql(table: str) -> dict[str, str]:
    return {
        constraint.name.removeprefix(f"ck_{table}_"): str(constraint.sqltext)
        for constraint in Base.metadata.tables[table].constraints
        if isinstance(constraint, CheckConstraint)
    }


def literals(sql: str) -> set[str]:
    return set(re.findall(r"'([\w]+)'", sql))


class ModelsTest(unittest.TestCase):
    def test_the_revision_follows_0027(self):
        scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
        self.assertEqual(scripts.get_revision(REVISION).down_revision, PREVIOUS)

    def test_the_names_are_conventional_and_short(self):
        prefixes = {
            "PrimaryKeyConstraint": "pk_",
            "ForeignKeyConstraint": "fk_",
            "UniqueConstraint": "uq_",
            "CheckConstraint": "ck_",
        }
        for name in NEW_TABLES:
            table = Base.metadata.tables[name]
            for constraint in table.constraints:
                with self.subTest(table=name, constraint=constraint.name):
                    self.assertTrue(
                        constraint.name.startswith(
                            prefixes[type(constraint).__name__] + name
                        )
                    )
                    self.assertLessEqual(len(constraint.name), 63)
            for index in table.indexes:
                self.assertTrue(index.name.startswith(f"ix_{name}_"))
                self.assertLessEqual(len(index.name), 63)

    def test_the_allowed_values_of_the_database_are_those_of_the_code(self):
        expected = {
            ("user_passkeys", "revoked_reason_valid"): {
                r.value for r in PasskeyRevokeReason
            },
            ("passkey_challenges", "purpose_valid"): {p.value for p in PasskeyPurpose},
            ("auth_sessions", "passkey_gate_valid"): {g.value for g in PasskeyGate},
            ("auth_sessions", "revoked_reason_valid"): {r.value for r in RevokeReason},
        }
        for (table, name), values in expected.items():
            with self.subTest(table=table, constraint=name):
                self.assertEqual(literals(check_sql(table)[name]), values)
        self.assertIn("passkey_revoked", {r.value for r in RevokeReason})

    def test_the_migration_repeats_the_values_of_the_code(self):
        source = VERSIONS.joinpath("0023_passkeys.py").read_text()
        for value in (
            *(r.value for r in PasskeyRevokeReason),
            *(p.value for p in PasskeyPurpose),
            *(g.value for g in PasskeyGate),
            *(r.value for r in RevokeReason),
        ):
            self.assertIn(f"'{value}'", source)

    def test_the_limits_of_the_database_are_those_of_the_code(self):
        passkeys = check_sql("user_passkeys")
        self.assertIn(
            f"BETWEEN {CREDENTIAL_ID_MIN_BYTES} AND {CREDENTIAL_ID_MAX_BYTES}",
            passkeys["credential_id_length"],
        )
        self.assertIn(
            f"BETWEEN {PUBLIC_KEY_MIN_BYTES} AND {PUBLIC_KEY_MAX_BYTES}",
            passkeys["public_key_length"],
        )
        self.assertIn(f"AND {SIGN_COUNT_MAX}", passkeys["sign_count_range"])
        self.assertIn(f"AND {PASSKEY_NAME_MAX_LENGTH}", passkeys["name_length"])
        self.assertIn(
            f"= {CHALLENGE_BYTES}", check_sql("passkey_challenges")["challenge_length"]
        )
        # The migration writes the same numbers (it is a snapshot, not an import).
        source = VERSIONS.joinpath("0023_passkeys.py").read_text()
        for number in (
            f"BETWEEN {CREDENTIAL_ID_MIN_BYTES} AND {CREDENTIAL_ID_MAX_BYTES}",
            f"BETWEEN {PUBLIC_KEY_MIN_BYTES} AND {PUBLIC_KEY_MAX_BYTES}",
            f"BETWEEN 0 AND {SIGN_COUNT_MAX}",
            f"BETWEEN 1 AND {PASSKEY_NAME_MAX_LENGTH}",
            f"octet_length(challenge) = {CHALLENGE_BYTES}",
        ):
            self.assertIn(number, source)

    def test_nothing_that_is_secret_has_a_column(self):
        for name in NEW_TABLES:
            for column in Base.metadata.tables[name].columns:
                with self.subTest(table=name, column=column.name):
                    self.assertNotIn(
                        column.name, {"private_key", "secret", "password", "token"}
                    )

    def test_the_session_columns_are_declared_by_the_auth_models(self):
        columns = Base.metadata.tables["auth_sessions"].columns
        self.assertFalse(columns["passkey_gate"].nullable)
        self.assertEqual(str(columns["passkey_gate"].server_default.arg), "'open'")
        self.assertTrue(columns["passkey_id"].nullable)
        self.assertIs(
            auth_models.AuthSessionRow.__table__, Base.metadata.tables["auth_sessions"]
        )


def only_compared(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in COMPARED
    table = getattr(obj, "table", None)
    return table is None or table.name in COMPARED


def catalog(connection, schema: str) -> dict[str, list[tuple]]:
    params = {"schema": schema, "tables": list(COMPARED)}

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
class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        migrate("downgrade", "base")
        self.addCleanup(migrate, "downgrade", "base")
        self.engine = create_engine(sync_database_url())
        self.addCleanup(self.engine.dispose)

    def scalars(self, sql: str, **params) -> list:
        with self.engine.connect() as connection:
            return list(connection.execute(text(sql), params).scalars())

    def run_sql(self, sql: str, **params) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(sql), params)

    def refused(self, sql: str, **params) -> str:
        with self.assertRaises(DBAPIError) as caught:
            self.run_sql(sql, **params)
        return caught.exception.orig.sqlstate

    def tables(self) -> set[str]:
        return set(
            self.scalars("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )

    def columns(self, table: str) -> set[str]:
        return set(
            self.scalars(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t",
                t=table,
            )
        )

    # -- up and down -------------------------------------------------------------

    def test_upgrade_creates_and_downgrade_removes(self):
        migrate("upgrade", PREVIOUS)
        self.assertEqual(self.tables() & set(NEW_TABLES), set())
        self.assertNotIn("passkey_gate", self.columns("auth_sessions"))

        migrate("upgrade", REVISION)
        self.assertTrue(set(NEW_TABLES) <= self.tables())
        self.assertTrue({"passkey_gate", "passkey_id"} <= self.columns("auth_sessions"))
        self.assertEqual(
            self.scalars("SELECT version_num FROM alembic_version"), [REVISION]
        )

        migrate("downgrade", PREVIOUS)
        self.assertEqual(self.tables() & set(NEW_TABLES), set())
        self.assertFalse({"passkey_gate", "passkey_id"} & self.columns("auth_sessions"))
        self.assertEqual(
            self.scalars("SELECT version_num FROM alembic_version"), [PREVIOUS]
        )
        # Nothing below it is touched.
        self.assertTrue(
            {"users", "auth_sessions", "audit_events", "auth_policy"} <= self.tables()
        )

    def test_it_can_be_applied_again(self):
        migrate("upgrade", REVISION)
        migrate("downgrade", PREVIOUS)
        migrate("upgrade", REVISION)
        self.assertTrue(set(NEW_TABLES) <= self.tables())
        migrate("downgrade", "base")
        migrate("upgrade", "head")
        self.assertTrue(set(NEW_TABLES) <= self.tables())

    def test_the_offline_sql_of_the_migration_is_rendered(self):
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            command.upgrade(offline_config(output), f"{PREVIOUS}:{REVISION}", sql=True)
        sql = output.getvalue()
        for expected in (
            "CREATE TABLE user_passkeys",
            "CREATE TABLE passkey_challenges",
            "ALTER TABLE auth_sessions ADD COLUMN passkey_gate",
            "ck_auth_sessions_revoked_reason_valid",
        ):
            self.assertIn(expected, sql)

    def test_sessions_that_exist_stay_open(self):
        migrate("upgrade", PREVIOUS)
        user = uuid.uuid4()
        self.run_sql(
            "INSERT INTO users (id, login_name, system_role, status, passkey_required,"
            " created_at, updated_at) VALUES (:u, 'boss', 'owner', 'active', true,"
            " now(), now())",
            u=user,
        )
        self.run_sql(
            "INSERT INTO auth_sessions (id, user_id, token_hash, remember_me,"
            " auth_method, created_at, last_used_at, idle_timeout_seconds,"
            " idle_expires_at, absolute_expires_at) VALUES (gen_random_uuid(), :u,"
            " sha256('a'::bytea), false, 'password', now(), now(), 60,"
            " now() + interval '1 day', now() + interval '2 days')",
            u=user,
        )
        migrate("upgrade", REVISION)
        row = self.scalars("SELECT passkey_gate FROM auth_sessions")
        self.assertEqual(row, ["open"])
        self.assertEqual(self.scalars("SELECT passkey_id FROM auth_sessions"), [None])

    def test_the_downgrade_relabels_sessions_and_leaves_users_alone(self):
        migrate("upgrade", REVISION)
        user = self.insert_user()
        self.insert_session(user, revoked="passkey_revoked")
        migrate("downgrade", PREVIOUS)
        self.assertEqual(self.scalars("SELECT count(*) FROM users"), [1])
        self.assertEqual(
            self.scalars("SELECT revoked_reason FROM auth_sessions"), ["admin"]
        )
        self.assertEqual(
            self.refused("UPDATE auth_sessions SET revoked_reason = 'passkey_revoked'"),
            "23514",
        )

    # -- drift -----------------------------------------------------------------------

    def autogenerate_diff(self, connection) -> list:
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_object": only_compared,
            },
        )
        return compare_metadata(context, Base.metadata)

    def test_alembic_autogenerate_finds_no_difference(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection:
            self.assertEqual(self.autogenerate_diff(connection), [])

    def test_the_autogenerate_check_notices_a_drift(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection, connection.begin() as transaction:
            for statement in (
                "DROP INDEX ix_user_passkeys_user_id_active",
                "ALTER TABLE passkey_challenges ADD COLUMN unexpected text",
                "ALTER TABLE user_passkeys ALTER COLUMN aaguid SET NOT NULL",
                "ALTER TABLE auth_sessions ALTER COLUMN passkey_gate DROP DEFAULT",
            ):
                connection.execute(text(statement))
            diff = self.autogenerate_diff(connection)
            transaction.rollback()
        kinds = sorted(
            {
                step[0]
                for entry in diff
                for step in (entry if isinstance(entry, list) else [entry])
            }
        )
        self.assertEqual(
            kinds,
            ["add_index", "modify_default", "modify_nullable", "remove_column"],
        )

    def created_catalog(self) -> dict[str, list[tuple]]:
        tables = [Base.metadata.tables["users"]] + [
            Base.metadata.tables[name]
            for name in (
                "password_credentials",
                "auth_sessions",
                "auth_throttles",
                "auth_policy",
                "auth_policy_changes",
                *NEW_TABLES,
            )
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
            "pk_user_passkeys",
            "uq_user_passkeys_credential_id",
            "fk_user_passkeys_user_id_users",
            "ck_user_passkeys_sign_count_range",
            "pk_passkey_challenges",
            "uq_passkey_challenges_session_id",
            "fk_passkey_challenges_session_id_auth_sessions",
            "fk_auth_sessions_passkey_id_user_passkeys",
            "ck_auth_sessions_passkey_gate_valid",
            "ck_auth_sessions_passkey_binds_open_session",
        ):
            self.assertIn(expected, names)

    # -- what the database enforces --------------------------------------------------

    def insert_user(self, login: str = "alice", role: str = "user") -> uuid.UUID:
        user = uuid.uuid4()
        self.run_sql(
            "INSERT INTO users (id, login_name, system_role, status, passkey_required,"
            " created_at, updated_at) VALUES (:u, :n, :r, 'active', :p, now(), now())",
            u=user,
            n=login,
            r=role,
            p=role != "user",
        )
        return user

    def insert_session(
        self,
        user: uuid.UUID,
        *,
        gate: str = "open",
        passkey: uuid.UUID | None = None,
        revoked: str | None = None,
    ) -> uuid.UUID:
        session = uuid.uuid4()
        self.run_sql(
            "INSERT INTO auth_sessions (id, user_id, token_hash, remember_me,"
            " auth_method, created_at, last_used_at, idle_timeout_seconds,"
            " idle_expires_at, absolute_expires_at, passkey_gate, passkey_id,"
            " revoked_at, revoked_reason) VALUES (:s, :u, :h, false, 'password',"
            " now(), now(), 60, now() + interval '1 day', now() + interval '2 days',"
            " :g, :p, CASE WHEN CAST(:r AS text) IS NULL THEN NULL ELSE now() END,"
            " :r)",
            s=session,
            u=user,
            h=session.bytes * 2,
            g=gate,
            p=passkey,
            r=revoked,
        )
        return session

    def passkey_sql(self, **columns) -> tuple[str, dict]:
        values = {
            "id": uuid.uuid4(),
            "user_id": None,
            "credential_id": b"\x01" * 16,
            "public_key": b"\x02" * 16,
            "sign_count": 0,
            "name": "phone",
            "aaguid": None,
            "backup_eligible": False,
            "backed_up": False,
            "created_at": None,
            "revoked_at": None,
            "revoked_reason": None,
        }
        values.update(columns)
        return (
            "INSERT INTO user_passkeys (id, user_id, credential_id, public_key,"
            " sign_count, name, aaguid, backup_eligible, backed_up, created_at,"
            " revoked_at, revoked_reason) VALUES (:id, :user_id, :credential_id,"
            " :public_key, :sign_count, :name, :aaguid, :backup_eligible, :backed_up,"
            " COALESCE(:created_at, now()), :revoked_at, :revoked_reason)",
            values,
        )

    def add_passkey(self, user: uuid.UUID, **columns) -> uuid.UUID:
        sql, values = self.passkey_sql(user_id=user, **columns)
        self.run_sql(sql, **values)
        return values["id"]

    def test_a_passkey_row_is_checked_by_the_database(self):
        migrate("upgrade", "head")
        user = self.insert_user()
        good = {
            "credential_id": b"\x01" * 16,
            "public_key": b"\x02" * 16,
            "sign_count": 0,
            "name": "n",
        }
        accepted = (
            {**good, "credential_id": b"\x03" * 1023},
            {**good, "credential_id": b"\x04" * 16, "public_key": b"\x05" * 2048},
            {**good, "credential_id": b"\x06" * 17, "sign_count": SIGN_COUNT_MAX},
            {**good, "credential_id": b"\x07" * 18, "name": "x" * 64},
            {
                **good,
                "credential_id": b"\x08" * 19,
                "backup_eligible": True,
                "backed_up": True,
            },
            {
                **good,
                "credential_id": b"\x09" * 20,
                "revoked_at": "2030-01-01T00:00:00Z",
                "revoked_reason": "recovery",
            },
        )
        for columns in accepted:
            with self.subTest(accepted=repr(columns)[:60]):
                self.add_passkey(user, **columns)
        refused = {
            "a credential id of 15 bytes": {**good, "credential_id": b"\x01" * 15},
            "a credential id of 1024 bytes": {**good, "credential_id": b"\x0a" * 1024},
            "a public key of 15 bytes": {
                **good,
                "credential_id": b"\x0b" * 16,
                "public_key": b"x" * 15,
            },
            "a public key of 2049 bytes": {
                **good,
                "credential_id": b"\x0c" * 16,
                "public_key": b"x" * 2049,
            },
            "a negative counter": {
                **good,
                "credential_id": b"\x0d" * 16,
                "sign_count": -1,
            },
            "a counter over 32 bits": {
                **good,
                "credential_id": b"\x0e" * 16,
                "sign_count": SIGN_COUNT_MAX + 1,
            },
            "an empty name": {**good, "credential_id": b"\x0f" * 16, "name": ""},
            "a name of 65 characters": {
                **good,
                "credential_id": b"\x10" * 16,
                "name": "x" * 65,
            },
            "backed up but not eligible": {
                **good,
                "credential_id": b"\x11" * 16,
                "backed_up": True,
            },
            "a revocation without a reason": {
                **good,
                "credential_id": b"\x12" * 16,
                "revoked_at": "2030-01-01T00:00:00Z",
            },
            "a reason without a revocation": {
                **good,
                "credential_id": b"\x13" * 16,
                "revoked_reason": "recovery",
            },
            "an unknown reason": {
                **good,
                "credential_id": b"\x14" * 16,
                "revoked_at": "2030-01-01T00:00:00Z",
                "revoked_reason": "admin",
            },
        }
        for label, columns in refused.items():
            with self.subTest(label):
                sql, values = self.passkey_sql(user_id=user, **columns)
                self.assertEqual(self.refused(sql, **values), "23514", label)

    def test_a_credential_id_is_unique_across_users_and_a_user_deletes_its_passkeys(
        self,
    ):
        migrate("upgrade", "head")
        alice, bob = self.insert_user("alice"), self.insert_user("bob")
        self.add_passkey(alice, credential_id=b"\x21" * 16)
        sql, values = self.passkey_sql(user_id=bob, credential_id=b"\x21" * 16)
        self.assertEqual(self.refused(sql, **values), "23505")
        sql, values = self.passkey_sql(user_id=uuid.uuid4(), credential_id=b"\x22" * 16)
        self.assertEqual(self.refused(sql, **values), "23503")
        self.run_sql("DELETE FROM users WHERE id = :u", u=alice)
        self.assertEqual(self.scalars("SELECT count(*) FROM user_passkeys"), [0])

    def challenge_sql(self, user, session, **columns) -> tuple[str, dict]:
        values = {
            "id": uuid.uuid4(),
            "u": user,
            "s": session,
            "purpose": "register",
            "challenge": b"\x31" * 32,
            "lifetime": "300 seconds",
        }
        values.update(columns)
        return (
            "INSERT INTO passkey_challenges (id, user_id, session_id, purpose,"
            " challenge, created_at, expires_at) VALUES (:id, :u, :s, :purpose,"
            " :challenge, now(), now() + CAST(:lifetime AS interval))",
            values,
        )

    def test_a_challenge_row_is_checked_by_the_database(self):
        migrate("upgrade", "head")
        user = self.insert_user()
        session = self.insert_session(user)
        for lifetime in ("1 second", "1 hour"):
            sql, values = self.challenge_sql(
                user, self.insert_session(user), lifetime=lifetime
            )
            self.run_sql(sql, **values)
        cases = {
            "an unknown purpose": {"purpose": "login"},
            "a challenge of 31 bytes": {"challenge": b"x" * 31},
            "a challenge of 33 bytes": {"challenge": b"x" * 33},
            "no lifetime": {"lifetime": "0 seconds"},
            "a lifetime over an hour": {"lifetime": "61 minutes"},
            "a negative lifetime": {"lifetime": "-1 second"},
        }
        for label, columns in cases.items():
            with self.subTest(label):
                sql, values = self.challenge_sql(user, session, **columns)
                self.assertEqual(self.refused(sql, **values), "23514", label)
        sql, values = self.challenge_sql(user, session)
        self.run_sql(sql, **values)
        # One challenge per (session, purpose); another purpose is another row.
        sql, values = self.challenge_sql(user, session)
        self.assertEqual(self.refused(sql, **values), "23505")
        sql, values = self.challenge_sql(user, session, purpose="authenticate")
        self.run_sql(sql, **values)
        # A challenge goes with its session, and with its user.
        self.run_sql("DELETE FROM auth_sessions WHERE id = :s", s=session)
        self.assertEqual(
            self.scalars(
                "SELECT count(*) FROM passkey_challenges WHERE session_id = :s",
                s=session,
            ),
            [0],
        )

    def test_the_session_columns_are_checked_by_the_database(self):
        migrate("upgrade", "head")
        user = self.insert_user()
        passkey = self.add_passkey(user)
        self.assertEqual(self.scalars("SELECT count(*) FROM auth_sessions"), [0])
        for gate in ("open", "enrollment_required", "assertion_required"):
            self.insert_session(user, gate=gate)
        with self.assertRaises(DBAPIError) as caught:
            self.insert_session(user, gate="closed")
        self.assertEqual(caught.exception.orig.sqlstate, "23514")
        # Only an open session is bound to a Passkey.
        for gate in ("enrollment_required", "assertion_required"):
            with self.assertRaises(DBAPIError) as caught:
                self.insert_session(user, gate=gate, passkey=passkey)
            self.assertEqual(caught.exception.orig.sqlstate, "23514", gate)
        bound = self.insert_session(user, gate="open", passkey=passkey)
        # Deleting the Passkey row leaves the session, unbound.
        self.run_sql("DELETE FROM user_passkeys WHERE id = :p", p=passkey)
        self.assertEqual(
            self.scalars("SELECT passkey_id FROM auth_sessions WHERE id = :s", s=bound),
            [None],
        )
        # The default of the column is open.
        self.run_sql(
            "INSERT INTO auth_sessions (id, user_id, token_hash, remember_me,"
            " auth_method, created_at, last_used_at, idle_timeout_seconds,"
            " idle_expires_at, absolute_expires_at) VALUES (gen_random_uuid(), :u,"
            " sha256('z'::bytea), false, 'password', now(), now(), 60,"
            " now() + interval '1 day', now() + interval '2 days')",
            u=user,
        )
        self.assertEqual(
            self.scalars(
                "SELECT passkey_gate FROM auth_sessions "
                "WHERE token_hash = sha256('z'::bytea)"
            ),
            ["open"],
        )
        # The new revocation reason is accepted; an unknown one is not.
        self.run_sql(
            "UPDATE auth_sessions SET revoked_at = now(), "
            "revoked_reason = 'passkey_revoked' WHERE id = :s",
            s=bound,
        )
        self.assertEqual(
            self.refused(
                "UPDATE auth_sessions SET revoked_at = now(), revoked_reason = 'x' "
                "WHERE id = :s",
                s=bound,
            ),
            "23514",
        )
