"""The Passkey tables in the split-role deployment (real PostgreSQL).

Migrations run as the owner of the schema; the backend connects as a NON-superuser
application role (``PAW_APP_DATABASE_ROLE``). These tests

* run the service and HTTP test classes of PAW-023 as that role, so every statement
  the Passkey code executes is proven to work with exactly the privileges revision
  ``0023`` grants;
* pin the exact privileges of the two new tables, column by column, and the two
  columns added to ``auth_sessions``;
* check what the role must NOT be able to do: re-key or hand over a credential,
  delete a Passkey, move a challenge to another session or user, relabel how a
  session signed in, empty a table.

Role names are unique per run and dropped afterwards; the test user must be allowed
to create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import unittest
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from . import (
    test_passkey_authenticate,
    test_passkey_gate,
    test_passkey_http,
    test_passkey_registration,
    test_passkey_revoke,
    test_passkey_sensitive,
)
from .auth_support import (
    ROLE_PASSWORD,
    TEST_DATABASE_URL,
    PostgresAuthTestCase,
    migrate,
    requires_postgres,
    sync_database_url,
    url_for_role,
)
from .identity_support import RUN_ID
from .support import make_settings

WEB_ROLE = f"paw_passkey_web_{RUN_ID}"
OTHER_ROLE = f"paw_passkey_other_{RUN_ID}"
INSUFFICIENT_PRIVILEGE = "42501"

ALL_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
# table -> (table-level privileges, columns the web role may UPDATE). The exact
# copy of the choices (and their reasons) in migration 0023.
EXPECTED = {
    "user_passkeys": (
        {"SELECT", "INSERT"},
        {"sign_count", "last_used_at", "backed_up", "revoked_at", "revoked_reason"},
    ),
    "passkey_challenges": (
        {"SELECT", "INSERT", "DELETE"},
        {"challenge", "created_at", "expires_at"},
    ),
}
# What 0023 adds to ``auth_sessions`` (the rest is pinned by test_auth_grants).
NEW_SESSION_COLUMNS = {"passkey_gate", "passkey_id"}
NOT_UPDATABLE_SESSION_COLUMNS = {
    "id",
    "user_id",
    "remember_me",
    "auth_method",
    "device_label",
    "created_at",
    "idle_timeout_seconds",
    "absolute_expires_at",
}


def owner_engine():
    return create_engine(sync_database_url())


def drop_roles(engine) -> None:
    with engine.begin() as connection:
        for role in (WEB_ROLE, OTHER_ROLE):
            exists = connection.execute(
                text("SELECT count(*) FROM pg_roles WHERE rolname = :r"), {"r": role}
            ).scalar()
            if exists:
                connection.execute(text(f"DROP OWNED BY {role}"))
                connection.execute(text(f"DROP ROLE {role}"))


def setUpModule():
    if not TEST_DATABASE_URL:
        raise unittest.SkipTest("PAW_TEST_DATABASE_URL is not set")
    migrate("downgrade", "base")
    engine = owner_engine()
    try:
        drop_roles(engine)
        with engine.begin() as connection:
            for role in (WEB_ROLE, OTHER_ROLE):
                connection.execute(
                    text(
                        f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB "
                        f"NOCREATEROLE PASSWORD '{ROLE_PASSWORD}'"
                    )
                )
    finally:
        engine.dispose()


def tearDownModule():
    if TEST_DATABASE_URL:
        migrate("downgrade", "base")
        engine = owner_engine()
        try:
            drop_roles(engine)
        finally:
            engine.dispose()


class WebRole:
    """Mixed into a test case: migrated for, and connecting as, the web role."""

    migration_environment = {"PAW_APP_DATABASE_ROLE": WEB_ROLE}
    service_role = WEB_ROLE


def as_web_role(case: type) -> type:
    return type(f"{case.__name__}AsWebRole", (WebRole, case), {"__module__": __name__})


# The service and HTTP test classes of PAW-023, unchanged, but every statement of the
# application runs as the unprivileged role.
for _module, _names in (
    (test_passkey_registration, ("BeginTest", "FinishTest", "RulesTest")),
    (test_passkey_authenticate, ("BeginTest", "StepUpTest")),
    (
        test_passkey_gate,
        (
            "SignInGateTest",
            "RestrictedSessionTest",
            "RecoveryTest",
            "GateOperationsTest",
        ),
    ),
    (test_passkey_revoke, ("RevokeTest", "SessionsEndTest", "RaceTest")),
    (
        test_passkey_sensitive,
        ("UnlockTest", "PolicyTest", "StrongApprovalTest"),
    ),
    (
        test_passkey_http,
        (
            "RouteInventoryTest",
            "OwnerStoryTest",
            "PasskeyRoutesTest",
            "NotConfiguredTest",
            "RelyingPartyTest",
        ),
    ),
):
    for _name in _names:
        _case = as_web_role(getattr(_module, _name))
        _short = _module.__name__.rpartition(".")[2].removeprefix("test_passkey_")
        _case.__qualname__ = _case.__name__ = f"{_short}_{_name}AsWebRole"
        globals()[_case.__name__] = _case
del _module, _names, _name, _case, _short


class Refusing(PostgresAuthTestCase):
    """Migrated for the web role; statements are run as the web / the other role."""

    migration_environment = {"PAW_APP_DATABASE_ROLE": WEB_ROLE}

    def engine_of(self, role: str):
        url = make_settings(database_url=url_for_role(role)).database_url
        engine = create_engine(url.get_secret_value())
        self.addCleanup(engine.dispose)
        return engine

    def owner_scalar(self, sql: str, **params):
        engine = owner_engine()
        try:
            with engine.connect() as connection:
                return connection.execute(text(sql), params).scalar()
        finally:
            engine.dispose()

    def owner_rows(self, sql: str, **params) -> list:
        engine = owner_engine()
        try:
            with engine.connect() as connection:
                return [tuple(r) for r in connection.execute(text(sql), params)]
        finally:
            engine.dispose()

    def sqlstate_as(self, role: str, sql: str, **params) -> str | None:
        """The SQLSTATE of the error ``sql`` raises as ``role``; ``None`` if it ran."""
        engine = self.engine_of(role)
        try:
            with engine.begin() as connection:
                connection.execute(text(sql), params)
        except DBAPIError as error:
            return error.orig.sqlstate
        return None

    def refused(self, role: str, sql: str, **params) -> None:
        self.assertEqual(
            self.sqlstate_as(role, sql, **params), INSUFFICIENT_PRIVILEGE, sql
        )

    def seed(self) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        """A user, a session of it and a Passkey of it, written by the owner."""
        user, session, passkey = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        engine = owner_engine()
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO users (id, login_name, system_role, status, "
                        "passkey_required, created_at, updated_at) VALUES (:u, :n, "
                        "'user', 'active', false, now(), now())"
                    ),
                    {"u": user, "n": f"user-{user.hex[:8]}"},
                )
                connection.execute(
                    text(
                        "INSERT INTO auth_sessions (id, user_id, token_hash, "
                        "remember_me, auth_method, created_at, last_used_at, "
                        "idle_timeout_seconds, idle_expires_at, absolute_expires_at) "
                        "VALUES (:s, :u, :h, false, 'password', now(), now(), 60, "
                        "now() + interval '1 day', now() + interval '2 days')"
                    ),
                    {"s": session, "u": user, "h": b"h" * 32},
                )
                connection.execute(
                    text(
                        "INSERT INTO user_passkeys (id, user_id, credential_id, "
                        "public_key, sign_count, name, backup_eligible, backed_up, "
                        "created_at) VALUES (:p, :u, :c, :k, 0, 'phone', false, "
                        "false, now())"
                    ),
                    {"p": passkey, "u": user, "c": b"c" * 16, "k": b"k" * 16},
                )
                connection.execute(
                    text(
                        "INSERT INTO passkey_challenges (id, user_id, session_id, "
                        "purpose, challenge, created_at, expires_at) VALUES "
                        "(gen_random_uuid(), :u, :s, 'register', :c, now(), "
                        "now() + interval '5 minutes')"
                    ),
                    {"u": user, "s": session, "c": b"x" * 32},
                )
        finally:
            engine.dispose()
        return user, session, passkey


@requires_postgres
class PrivilegesTest(Refusing):
    def test_the_web_role_holds_exactly_the_least_privileges(self):
        for table, (privileges, update_columns) in EXPECTED.items():
            with self.subTest(table=table):
                for privilege in ALL_PRIVILEGES:
                    granted = self.owner_scalar(
                        "SELECT has_table_privilege(:r, :t, :p)",
                        r=WEB_ROLE,
                        t=table,
                        p=privilege,
                    )
                    self.assertEqual(granted, privilege in privileges, privilege)
                columns = [
                    row[0]
                    for row in self.owner_rows(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :t AND table_schema = current_schema()",
                        t=table,
                    )
                ]
                updatable = {
                    column
                    for column in columns
                    if self.owner_scalar(
                        "SELECT has_column_privilege(:r, :t, :c, 'UPDATE')",
                        r=WEB_ROLE,
                        t=table,
                        c=column,
                    )
                }
                self.assertEqual(updatable, update_columns)

    def test_the_two_session_columns_may_be_updated_and_the_rest_still_may_not(self):
        for column in NEW_SESSION_COLUMNS:
            self.assertTrue(
                self.owner_scalar(
                    "SELECT has_column_privilege(:r, 'auth_sessions', :c, 'UPDATE')",
                    r=WEB_ROLE,
                    c=column,
                ),
                column,
            )
        for column in NOT_UPDATABLE_SESSION_COLUMNS:
            self.assertFalse(
                self.owner_scalar(
                    "SELECT has_column_privilege(:r, 'auth_sessions', :c, 'UPDATE')",
                    r=WEB_ROLE,
                    c=column,
                ),
                column,
            )

    def test_a_role_without_grants_and_public_reach_no_table(self):
        for table in EXPECTED:
            with self.subTest(table=table):
                self.refused(OTHER_ROLE, f"SELECT count(*) FROM {table}")
                self.refused(OTHER_ROLE, f"DELETE FROM {table}")
                for privilege in ALL_PRIVILEGES:
                    self.assertFalse(
                        self.owner_scalar(
                            "SELECT has_table_privilege(:r, :t, :p)",
                            r=OTHER_ROLE,
                            t=table,
                            p=privilege,
                        )
                    )

    def test_the_expectations_cover_every_table_of_the_feature(self):
        self.assertEqual(set(EXPECTED), {"user_passkeys", "passkey_challenges"})


@requires_postgres
class WhatTheWebRoleMustNotDoTest(Refusing):
    def test_it_cannot_rekey_hand_over_or_delete_a_credential(self):
        _user, _session, passkey = self.seed()
        for sql in (
            "UPDATE user_passkeys SET credential_id = '\\x0011223344556677'",
            "UPDATE user_passkeys SET public_key = '\\x0011223344556677'",
            "UPDATE user_passkeys SET user_id = gen_random_uuid()",
            "UPDATE user_passkeys SET name = 'renamed'",
            "UPDATE user_passkeys SET backup_eligible = true",
            "UPDATE user_passkeys SET created_at = now()",
            "UPDATE user_passkeys SET aaguid = gen_random_uuid()",
            "DELETE FROM user_passkeys",
            "TRUNCATE user_passkeys",
        ):
            with self.subTest(sql=sql[:60]):
                self.refused(WEB_ROLE, sql)
        self.assertEqual(
            self.owner_scalar(
                "SELECT name FROM user_passkeys WHERE id = :p", p=passkey
            ),
            "phone",
        )

    def test_it_can_use_and_revoke_a_credential(self):
        _user, _session, passkey = self.seed()
        for sql in (
            "UPDATE user_passkeys SET sign_count = 3, last_used_at = now(), "
            "backed_up = false",
            "UPDATE user_passkeys SET revoked_at = now(), "
            "revoked_reason = 'revoked_by_user'",
        ):
            self.assertIsNone(self.sqlstate_as(WEB_ROLE, sql), sql)
        self.assertEqual(
            self.owner_scalar(
                "SELECT sign_count FROM user_passkeys WHERE id = :p", p=passkey
            ),
            3,
        )

    def test_it_cannot_move_a_challenge_or_empty_the_table(self):
        self.seed()
        for sql in (
            "UPDATE passkey_challenges SET user_id = gen_random_uuid()",
            "UPDATE passkey_challenges SET session_id = gen_random_uuid()",
            "UPDATE passkey_challenges SET purpose = 'authenticate'",
            "UPDATE passkey_challenges SET id = gen_random_uuid()",
            "TRUNCATE passkey_challenges",
        ):
            with self.subTest(sql=sql[:60]):
                self.refused(WEB_ROLE, sql)
        # A challenge is consumed by deleting it, and replaced by an upsert.
        self.assertIsNone(self.sqlstate_as(WEB_ROLE, "DELETE FROM passkey_challenges"))

    def test_it_cannot_relabel_how_a_session_signed_in(self):
        self.seed()
        for sql in (
            "UPDATE auth_sessions SET auth_method = 'passkey'",
            "UPDATE auth_sessions SET user_id = gen_random_uuid()",
            "UPDATE auth_sessions SET absolute_expires_at = now() + interval '9 years'",
        ):
            with self.subTest(sql=sql[:60]):
                self.refused(WEB_ROLE, sql)
        # It can lift the gate and bind it (a registration or an assertion does).
        for sql in (
            "UPDATE auth_sessions SET passkey_gate = 'open'",
            "UPDATE auth_sessions SET passkey_id = NULL",
        ):
            self.assertIsNone(self.sqlstate_as(WEB_ROLE, sql), sql)

    def test_it_cannot_change_the_schema(self):
        for sql in (
            "CREATE TABLE passkey_extra (x int)",
            "ALTER TABLE user_passkeys ADD COLUMN x int",
            "DROP TABLE passkey_challenges",
            "ALTER TABLE auth_sessions DROP COLUMN passkey_gate",
        ):
            with self.subTest(sql=sql[:60]):
                self.refused(WEB_ROLE, sql)
