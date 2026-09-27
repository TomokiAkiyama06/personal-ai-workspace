"""Migration 0124: invitations, device pairing, the user lifecycle (PostgreSQL).

Up, down and up again; no drift between the models and the migration (Alembic's
autogenerate and a catalog comparison); what the two ``SECURITY DEFINER`` functions
allow and refuse; the append-only history; what the downgrade removes.
"""

import io
import re
import unittest
import uuid
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, create_engine, text
from sqlalchemy.exc import DBAPIError

from paw_backend.auth.models import AuthMethod, ThrottleScope
from paw_backend.auth.onboarding.models import (
    TABLE_NAMES,
    InvitationEnd,
    PairingEnd,
    PairingState,
)
from paw_backend.db import Base
from paw_backend.identity import UserStatus

from .memory_support import (
    FOREIGN_KEYS_WITHOUT_INDEX,
    migrate,
    requires_postgres,
    sync_database_url,
)
from .test_migrations import offline_config

REVISION = "0124"
PREVIOUS = "0041"
COMPARED = (*TABLE_NAMES, "auth_sessions", "auth_throttles")
SCHEMA = "paw_onboarding_drift_check"
SOURCE = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0124_invitations_and_pairing.py"
).read_text()


def check_sql(table: str) -> dict[str, str]:
    return {
        constraint.name.removeprefix(f"ck_{table}_"): str(constraint.sqltext)
        for constraint in Base.metadata.tables[table].constraints
        if isinstance(constraint, CheckConstraint)
    }


def literals(sql: str) -> set[str]:
    return set(re.findall(r"'([\w]+)'", sql))


class ModelsTest(unittest.TestCase):
    def test_the_revision_follows_its_declared_previous(self):
        scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
        revision = scripts.get_revision(REVISION)
        self.assertEqual(revision.down_revision, PREVIOUS)
        self.assertTrue(revision.path.endswith("0124_invitations_and_pairing.py"))

    def test_the_names_are_conventional_and_short(self):
        prefixes = {
            "PrimaryKeyConstraint": "pk_",
            "ForeignKeyConstraint": "fk_",
            "UniqueConstraint": "uq_",
            "CheckConstraint": "ck_",
        }
        for name in TABLE_NAMES:
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
                prefix = "uq_" if index.unique else "ix_"
                self.assertTrue(index.name.startswith(f"{prefix}{name}_"))

    def test_the_allowed_values_of_the_database_are_those_of_the_code(self):
        statuses = {s.value for s in UserStatus}
        expected = {
            ("user_invitations", "revoked_reason_valid"): {
                e.value for e in InvitationEnd
            },
            ("device_pairings", "state_valid"): {s.value for s in PairingState},
            ("device_pairings", "ended_reason_valid"): {e.value for e in PairingEnd},
            ("user_status_changes", "old_status_valid"): statuses,
            ("user_status_changes", "new_status_valid"): statuses,
            ("auth_sessions", "auth_method_valid"): {m.value for m in AuthMethod},
            ("auth_throttles", "scope_valid"): {s.value for s in ThrottleScope},
        }
        for (table, name), values in expected.items():
            with self.subTest(table=table, constraint=name):
                self.assertEqual(literals(check_sql(table)[name]), values)
                for value in values:
                    self.assertIn(f"'{value}'", SOURCE)

    def test_nothing_that_is_secret_has_a_column(self):
        for name in TABLE_NAMES:
            for column in Base.metadata.tables[name].columns:
                with self.subTest(table=name, column=column.name):
                    self.assertNotIn(
                        column.name, {"token", "secret", "claim", "password"}
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

    def functions(self) -> set[str]:
        return set(
            self.scalars(
                "SELECT proname FROM pg_proc WHERE proname LIKE 'paw\\_%' "
                "AND pronamespace = 'public'::regnamespace"
            )
        )

    def insert_user(self, name="alice", role="user", status="active") -> uuid.UUID:
        user = uuid.uuid4()
        self.run_sql(
            "INSERT INTO users (id, login_name, system_role, status, passkey_required,"
            " created_at, updated_at) VALUES (:u, :n, :r, :s, :p, now(), now())",
            u=user,
            n=name,
            r=role,
            s=status,
            p=role != "user",
        )
        return user

    # -- up and down -------------------------------------------------------------

    def test_upgrade_creates_and_downgrade_removes(self):
        migrate("upgrade", PREVIOUS)
        self.assertEqual(self.tables() & set(TABLE_NAMES), set())
        migrate("upgrade", REVISION)
        self.assertTrue(set(TABLE_NAMES) <= self.tables())
        self.assertTrue(
            {"paw_invite_user", "paw_change_user_status"} <= self.functions()
        )
        migrate("downgrade", PREVIOUS)
        self.assertEqual(self.tables() & set(TABLE_NAMES), set())
        self.assertFalse(
            {"paw_invite_user", "paw_change_user_status"} & self.functions()
        )
        self.assertTrue({"users", "auth_sessions", "audit_events"} <= self.tables())
        migrate("upgrade", "head")
        self.assertTrue(set(TABLE_NAMES) <= self.tables())

    def test_the_downgrade_removes_what_the_older_constraints_refuse(self):
        migrate("upgrade", REVISION)
        user = self.insert_user()
        self.run_sql(
            "INSERT INTO auth_sessions (id, user_id, token_hash, remember_me,"
            " auth_method, created_at, last_used_at, idle_timeout_seconds,"
            " idle_expires_at, absolute_expires_at) VALUES (gen_random_uuid(), :u,"
            " sha256('a'::bytea), false, 'pairing', now(), now(), 60,"
            " now() + interval '1 day', now() + interval '2 days')",
            u=user,
        )
        self.run_sql(
            "INSERT INTO auth_throttles (scope, key_hash, attempts, last_attempt_at)"
            " VALUES ('pairing_source', sha256('k'::bytea), 1, now())"
        )
        migrate("downgrade", PREVIOUS)
        self.assertEqual(self.scalars("SELECT count(*) FROM auth_sessions"), [0])
        self.assertEqual(self.scalars("SELECT count(*) FROM auth_throttles"), [0])
        self.assertEqual(self.scalars("SELECT count(*) FROM users"), [1])
        self.assertEqual(
            self.refused(
                "INSERT INTO auth_throttles (scope, key_hash, attempts,"
                " last_attempt_at) VALUES ('pairing_global', sha256('k'::bytea), 1,"
                " now())"
            ),
            "23514",
        )

    # -- drift -----------------------------------------------------------------------

    def test_alembic_autogenerate_finds_no_difference(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_object": only_compared,
                },
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_every_foreign_key_has_an_index_for_its_referential_action(self):
        migrate("upgrade", "head")
        self.assertEqual(
            self.scalars(FOREIGN_KEYS_WITHOUT_INDEX, tables=list(TABLE_NAMES)), []
        )

    def test_the_migration_and_the_models_produce_the_same_catalog(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection:
            migrated = catalog(connection, "public")
        tables = [
            Base.metadata.tables[name]
            for name in (
                "users",
                "password_credentials",
                "auth_sessions",
                "auth_throttles",
                "user_passkeys",
                *TABLE_NAMES,
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
                created = catalog(connection, SCHEMA)
        finally:
            with self.engine.begin() as connection:
                connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        for kind in ("columns", "constraints", "indexes"):
            with self.subTest(kind):
                self.assertEqual(migrated[kind], created[kind])

    # -- the functions ---------------------------------------------------------------

    def test_an_invitation_makes_an_invited_user_or_admin_and_never_an_owner(self):
        migrate("upgrade", "head")
        for role, required in (("user", False), ("admin", True)):
            user = uuid.uuid4()
            self.run_sql(
                "SELECT paw_invite_user(:u, :n, :r, now(), NULL)",
                u=user,
                n=f"new-{role}",
                r=role,
            )
            self.assertEqual(
                self.scalars(
                    "SELECT status || ',' || passkey_required FROM users WHERE id = :u",
                    u=user,
                ),
                [f"invited,{str(required).lower()}"],
            )
        for role in ("owner", "system", "nonsense"):
            with self.subTest(role=role):
                self.assertEqual(
                    self.refused(
                        "SELECT paw_invite_user(gen_random_uuid(), 'x-y-z', :r,"
                        " now(), NULL)",
                        r=role,
                    ),
                    "23514",
                )
        self.assertEqual(self.scalars("SELECT count(*) FROM user_status_changes"), [2])

    def test_the_status_function_allows_exactly_four_edges_and_never_the_owner(self):
        migrate("upgrade", "head")
        allowed = {
            ("invited", "active"),
            ("invited", "deleted"),
            ("active", "pending_deletion"),
            ("pending_deletion", "active"),
        }
        for old in ("invited", "active", "pending_deletion", "deleted"):
            for new in ("invited", "active", "pending_deletion", "deleted"):
                with self.subTest(old=old, new=new):
                    user = self.insert_user(
                        f"u-{old}-{new}".replace("_", "-"), status=old
                    )
                    sql = "SELECT paw_change_user_status(:u, :o, :n, now(), NULL)"
                    if (old, new) in allowed:
                        self.assertEqual(
                            self.scalars(sql, u=user, o=old, n=new), [True]
                        )
                    else:
                        self.assertEqual(
                            self.refused(sql, u=user, o=old, n=new), "23514"
                        )
        # A row that is not in ``from`` is left alone (false, no history).
        user = self.insert_user("stays", status="active")
        self.assertEqual(
            self.scalars(
                "SELECT paw_change_user_status(:u, 'invited', 'deleted', now(), NULL)",
                u=user,
            ),
            [False],
        )
        owner = self.insert_user("boss", role="owner")
        self.assertEqual(
            self.scalars(
                "SELECT paw_change_user_status(:u, 'active', 'pending_deletion',"
                " now(), NULL)",
                u=owner,
            ),
            [False],
        )
        self.assertEqual(
            self.scalars("SELECT status FROM users WHERE id = :u", u=owner), ["active"]
        )

    def test_the_history_is_append_only(self):
        migrate("upgrade", "head")
        self.run_sql(
            "SELECT paw_invite_user(gen_random_uuid(), 'bob', 'user', now(), NULL)"
        )
        self.assertEqual(
            self.refused("UPDATE user_status_changes SET new_status = 'active'"),
            "23001",
        )
        self.assertEqual(self.refused("DELETE FROM user_status_changes"), "23001")

    def test_one_outstanding_invitation_and_one_live_pairing_per_user(self):
        migrate("upgrade", "head")
        user = self.insert_user(status="invited")
        invitation = (
            "INSERT INTO user_invitations (id, audit_ref, user_id, invited_by, salt,"
            " secret_hash, created_at, expires_at, attempts) VALUES"
            " (gen_random_uuid(), gen_random_uuid(), :u, :u, :salt, :hash, now(),"
            " now() + interval '1 day', 0)"
        )
        self.run_sql(invitation, u=user, salt=b"\x01" * 16, hash=b"\x02" * 32)
        self.assertEqual(
            self.refused(invitation, u=user, salt=b"\x01" * 16, hash=b"\x02" * 32),
            "23505",
        )
        # A lifetime over 14 days is refused by the database.
        self.assertEqual(
            self.refused(
                invitation.replace("interval '1 day'", "interval '15 days'"),
                u=self.insert_user("other", status="invited"),
                salt=b"\x01" * 16,
                hash=b"\x02" * 32,
            ),
            "23514",
        )
        pairing = (
            "INSERT INTO device_pairings (id, audit_ref, user_id, state,"
            " approval_required, salt, secret_hash, created_at, expires_at, attempts)"
            " VALUES (gen_random_uuid(), gen_random_uuid(), :u, 'issued', false,"
            " :salt, :hash, now(), now() + interval '10 minutes', 0)"
        )
        active = self.insert_user("carol")
        self.run_sql(pairing, u=active, salt=b"\x01" * 16, hash=b"\x02" * 32)
        self.assertEqual(
            self.refused(pairing, u=active, salt=b"\x01" * 16, hash=b"\x02" * 32),
            "23505",
        )
        self.assertEqual(
            self.refused(
                pairing.replace("interval '10 minutes'", "interval '2 hours'"),
                u=self.insert_user("dave"),
                salt=b"\x01" * 16,
                hash=b"\x02" * 32,
            ),
            "23514",
        )
        # A claimed pairing without its claim is refused.
        self.assertEqual(
            self.refused("UPDATE device_pairings SET state = 'claimed'"), "23514"
        )
