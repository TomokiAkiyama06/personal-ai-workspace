"""The authentication tables in the split-role deployment (real PostgreSQL).

Migrations run as the owner of the schema; the backend connects as a NON-superuser
application role (``PAW_APP_DATABASE_ROLE``). These tests

* run the service test classes of PAW-022 as that role, so every statement the
  code executes is proven to work with exactly the privileges revision ``0022``
  grants (and, through the redeemer, that the web role can spend an Owner token:
  Decision 0005, condition "the web role keeps minimum privileges");
* pin the exact privileges, table by table and column by column;
* check what the role must NOT be able to do: reactivate or delete a user, change
  a role or a status, extend a session, rewrite the policy history, create or
  alter a token, truncate, change the schema.

Role names are unique per run and dropped afterwards; the test user must be
allowed to create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import unittest
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from . import (
    test_auth_http,
    test_auth_http_admin,
    test_auth_service_account,
    test_auth_service_admin,
    test_auth_service_login,
    test_auth_service_redeem,
    test_auth_sessions,
    test_auth_throttle,
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

WEB_ROLE = f"paw_auth_web_{RUN_ID}"
OTHER_ROLE = f"paw_auth_other_{RUN_ID}"
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
# copy of the choices (and their reasons) in migration 0022.
EXPECTED = {
    "password_credentials": ({"SELECT", "INSERT"}, {"hash", "changed_at"}),
    "auth_sessions": (
        {"SELECT", "INSERT", "DELETE"},
        {
            "token_hash",
            "last_used_at",
            "idle_expires_at",
            "stepup_at",
            "stepup_method",
            "rotated_at",
            "revoked_at",
            "revoked_reason",
        },
    ),
    "auth_throttles": (
        {"SELECT", "INSERT", "DELETE"},
        {"attempts", "last_attempt_at", "locked_until"},
    ),
    "auth_policy": (
        {"SELECT"},
        {
            "version",
            "passkey_owner",
            "passkey_admin",
            "passkey_user",
            "recommend_passkey_to_users",
            "stepup_window_minutes",
            "updated_at",
            "updated_by",
        },
    ),
    "auth_policy_changes": ({"SELECT", "INSERT"}, set()),
    # PAW-021's table, unchanged by 0022: the web role still cannot touch a status.
    "users": ({"SELECT"}, {"updated_at"}),
    "setup_tokens": ({"SELECT"}, {"attempts", "used_at", "locked_at"}),
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


# The service and HTTP test classes of PAW-022, unchanged, but every statement of
# the application runs as the unprivileged role.
for _module, _names in (
    (
        test_auth_sessions,
        ("CreateTest", "AuthenticateTest", "RotateTest", "RevokeTest", "ListTest"),
    ),
    (
        test_auth_throttle,
        (
            "BackoffTest",
            "SuccessTest",
            "RateLimitTest",
            "ConcurrencyTest",
            "ClockTest",
            "PurgeTest",
        ),
    ),
    (
        test_auth_service_login,
        (
            "SuccessTest",
            "FailureTest",
            "BackoffTest",
            "RehashAndRaceTest",
            "FailClosedTest",
        ),
    ),
    (
        test_auth_service_account,
        ("LogoutAndDevicesTest", "ChangePasswordTest", "StepUpTest"),
    ),
    (
        test_auth_service_redeem,
        ("SetupTest", "RecoveryTest", "RejectionTest", "RateLimitTest", "HookTest"),
    ),
    (test_auth_service_admin, ("UnlockTest", "PolicyTest")),
    (
        test_auth_http,
        (
            "LoginTest",
            "SessionRoutesTest",
            "PasswordRoutesTest",
            "SameSiteSettingTest",
        ),
    ),
    (
        test_auth_http_admin,
        ("RedeemRouteTest", "RoleMatrixTest", "CsrfEndToEndTest", "ProviderTest"),
    ),
):
    for _name in _names:
        _case = as_web_role(getattr(_module, _name))
        _short = _module.__name__.rpartition(".")[2].removeprefix("test_auth_")
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

    def seed_user(self, status: str = "active", role: str = "user") -> uuid.UUID:
        user_id = uuid.uuid4()
        engine = owner_engine()
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO users (id, login_name, system_role, status, "
                        "passkey_required, created_at, updated_at) VALUES (:id, :n, "
                        ":role, :status, :req, now(), now())"
                    ),
                    {
                        "id": user_id,
                        "n": f"user-{user_id.hex[:8]}",
                        "role": role,
                        "status": status,
                        "req": role in ("owner", "admin"),
                    },
                )
        finally:
            engine.dispose()
        return user_id


@requires_postgres
class PrivilegesTest(Refusing):
    def test_the_web_role_is_a_plain_non_superuser_role(self):
        row = self.owner_rows(
            "SELECT rolsuper, rolcreaterole, rolbypassrls FROM pg_roles "
            "WHERE rolname = :r",
            r=WEB_ROLE,
        )[0]
        self.assertEqual(row, (False, False, False))

    def test_the_expectations_cover_every_table_of_the_feature(self):
        self.assertEqual(
            {name for name in EXPECTED if name.startswith(("auth_", "password_"))},
            {
                "password_credentials",
                "auth_sessions",
                "auth_throttles",
                "auth_policy",
                "auth_policy_changes",
            },
        )

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

    def test_a_role_without_grants_and_public_reach_no_table(self):
        for table in EXPECTED:
            if table in ("users", "setup_tokens"):
                continue
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

    def test_the_activation_function_may_be_run_by_the_web_role_only(self):
        signature = "paw_activate_invited_user(uuid, timestamptz)"
        self.assertTrue(
            self.owner_scalar(
                "SELECT has_function_privilege(:r, :f, 'EXECUTE')",
                r=WEB_ROLE,
                f=signature,
            )
        )
        self.assertFalse(
            self.owner_scalar(
                "SELECT has_function_privilege(:r, :f, 'EXECUTE')",
                r=OTHER_ROLE,
                f=signature,
            )
        )
        self.refused(
            OTHER_ROLE, "SELECT paw_activate_invited_user(gen_random_uuid(), now())"
        )


@requires_postgres
class WhatTheWebRoleMustNotDoTest(Refusing):
    def test_it_cannot_change_who_a_user_is_or_whether_a_user_lives(self):
        user = self.seed_user()
        for sql in (
            "UPDATE users SET status = 'deleted'",
            "UPDATE users SET status = 'active'",
            "UPDATE users SET system_role = 'owner'",
            "UPDATE users SET login_name = 'someone-else'",
            "UPDATE users SET passkey_required = false",
            "DELETE FROM users",
            "TRUNCATE users CASCADE",
            "INSERT INTO users (id, login_name, system_role, status, passkey_required, "
            "created_at, updated_at) VALUES (gen_random_uuid(), 'made-up-owner', "
            "'owner', 'active', true, now(), now())",
        ):
            with self.subTest(sql=sql[:50]):
                self.refused(WEB_ROLE, sql)
        self.assertEqual(
            self.owner_scalar("SELECT status FROM users WHERE id = :id", id=user),
            "active",
        )

    def test_it_cannot_create_or_rewrite_an_owner_token(self):
        for sql in (
            """INSERT INTO setup_tokens (id, audit_ref, user_id, purpose, salt,
            secret_hash, created_at, expires_at, attempts) VALUES (gen_random_uuid(),
            gen_random_uuid(), gen_random_uuid(), 'recovery', '\\x00', '\\x00',
            now(), now() + interval '1 hour', 0)""",
            "UPDATE setup_tokens SET revoked_at = now()",
            "UPDATE setup_tokens SET expires_at = now() + interval '10 years'",
            "UPDATE setup_tokens SET secret_hash = '\\x00'",
            "DELETE FROM setup_tokens",
        ):
            with self.subTest(sql=sql[:50]):
                self.refused(WEB_ROLE, sql)

    def test_the_activation_function_activates_an_invited_user_and_nothing_else(self):
        invited = self.seed_user("invited")
        others = {
            status: self.seed_user(status)
            for status in ("active", "pending_deletion", "deleted")
        }
        engine = self.engine_of(WEB_ROLE)
        with engine.begin() as connection:
            results = {
                "invited": connection.execute(
                    text("SELECT paw_activate_invited_user(:id, now())"),
                    {"id": invited},
                ).scalar(),
                **{
                    status: connection.execute(
                        text("SELECT paw_activate_invited_user(:id, now())"),
                        {"id": user},
                    ).scalar()
                    for status, user in others.items()
                },
            }
        self.assertEqual(
            results,
            {
                "invited": True,
                "active": False,
                "pending_deletion": False,
                "deleted": False,
            },
        )
        statuses = dict(
            self.owner_rows(
                "SELECT id, status FROM users WHERE id = ANY(:ids)",
                ids=[invited, *others.values()],
            )
        )
        self.assertEqual(statuses[invited], "active")
        self.assertEqual(statuses[others["pending_deletion"]], "pending_deletion")
        self.assertEqual(statuses[others["deleted"]], "deleted")

    def test_it_cannot_extend_or_move_a_session(self):
        user = self.seed_user()
        engine = owner_engine()
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """INSERT INTO auth_sessions (id, user_id, token_hash,
                        remember_me, auth_method, created_at, last_used_at,
                        idle_timeout_seconds, idle_expires_at, absolute_expires_at)
                        VALUES (gen_random_uuid(), :u, :h, false, 'password', now(),
                        now(), 60, now() + interval '1 day',
                        now() + interval '2 days')"""
                    ),
                    {"u": user, "h": b"h" * 32},
                )
        finally:
            engine.dispose()
        for column in (
            "absolute_expires_at = now() + interval '10 years'",
            "idle_timeout_seconds = 100000000",
            "user_id = gen_random_uuid()",
            "created_at = now() - interval '1 year'",
            "remember_me = true",
            "auth_method = 'passkey'",
            "device_label = 'x'",
            "id = gen_random_uuid()",
        ):
            with self.subTest(column=column):
                self.refused(WEB_ROLE, f"UPDATE auth_sessions SET {column}")
        # ... while what a session's life changes is allowed.
        self.assertIsNone(
            self.sqlstate_as(
                WEB_ROLE,
                "UPDATE auth_sessions SET last_used_at = now(), "
                "idle_expires_at = now() + interval '1 hour'",
            )
        )

    def test_it_cannot_rewrite_a_password_row_beyond_the_hash(self):
        user = self.seed_user()
        engine = owner_engine()
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO password_credentials (user_id, hash, created_at, "
                        "changed_at) VALUES (:u, '$argon2id$x', now(), now())"
                    ),
                    {"u": user},
                )
        finally:
            engine.dispose()
        self.refused(
            WEB_ROLE, "UPDATE password_credentials SET user_id = gen_random_uuid()"
        )
        self.refused(WEB_ROLE, "UPDATE password_credentials SET created_at = now()")
        self.refused(WEB_ROLE, "DELETE FROM password_credentials")
        self.assertIsNone(
            self.sqlstate_as(
                WEB_ROLE,
                "UPDATE password_credentials "
                "SET hash = '$argon2id$y', changed_at = now()",
            )
        )

    def test_it_cannot_delete_or_seed_the_policy_or_rewrite_its_history(self):
        self.refused(WEB_ROLE, "DELETE FROM auth_policy")
        self.refused(WEB_ROLE, "TRUNCATE auth_policy")
        self.refused(WEB_ROLE, "UPDATE auth_policy SET id = 2, version = version + 1")
        self.refused(
            WEB_ROLE,
            """INSERT INTO auth_policy (id, version, passkey_owner, passkey_admin,
            passkey_user, recommend_passkey_to_users, stepup_window_minutes,
            updated_at) VALUES (1, 9, 'optional', 'optional', 'optional', false,
            240, now())""",
        )
        for statement in (
            "UPDATE auth_policy_changes SET version = 99",
            "DELETE FROM auth_policy_changes",
            "TRUNCATE auth_policy_changes",
        ):
            with self.subTest(statement=statement):
                self.refused(WEB_ROLE, statement)

    def test_it_can_change_the_policy_only_through_the_versioned_update(self):
        self.assertIsNone(
            self.sqlstate_as(
                WEB_ROLE,
                "UPDATE auth_policy SET version = version + 1, "
                "passkey_user = 'required'",
            )
        )
        # Not a second time with the same version (the trigger of the migration).
        self.assertEqual(
            self.sqlstate_as(
                WEB_ROLE, "UPDATE auth_policy SET passkey_user = 'optional'"
            ),
            "23001",
        )

    def test_it_cannot_truncate_or_change_the_schema(self):
        for table in EXPECTED:
            self.refused(WEB_ROLE, f"TRUNCATE {table} CASCADE")
        for sql in (
            "ALTER TABLE auth_sessions ADD COLUMN x int",
            "DROP TABLE auth_sessions",
            "ALTER TABLE auth_policy DISABLE TRIGGER ALL",
            "DROP TRIGGER tr_auth_policy_guard_update ON auth_policy",
            "CREATE TRIGGER x AFTER INSERT ON auth_sessions FOR EACH ROW "
            "EXECUTE FUNCTION paw_guard_auth_policy_update()",
            "DROP FUNCTION paw_activate_invited_user(uuid, timestamptz)",
            "CREATE OR REPLACE FUNCTION paw_activate_invited_user(uuid, timestamptz) "
            "RETURNS boolean LANGUAGE sql AS 'SELECT true'",
        ):
            with self.subTest(sql=sql[:50]):
                self.assertIsNotNone(self.sqlstate_as(WEB_ROLE, sql), sql)

    def test_it_cannot_hand_its_privileges_on(self):
        # PostgreSQL only warns when a role without a grant option tries to grant:
        # what counts is that nothing was granted.
        for table in EXPECTED:
            self.sqlstate_as(WEB_ROLE, f"GRANT ALL ON {table} TO PUBLIC")
            self.sqlstate_as(WEB_ROLE, f"GRANT SELECT ON {table} TO {OTHER_ROLE}")
            with self.subTest(table=table):
                self.assertFalse(
                    self.owner_scalar(
                        "SELECT has_table_privilege(:r, :t, 'SELECT')",
                        r=OTHER_ROLE,
                        t=table,
                    )
                )
                self.assertFalse(
                    self.owner_scalar(
                        "SELECT has_table_privilege(:r, :t, 'DELETE')",
                        r=OTHER_ROLE,
                        t=table,
                    )
                )


class ModuleTest(unittest.TestCase):
    def test_the_generated_classes_exist_and_run_as_the_web_role(self):
        generated = [
            value
            for name, value in globals().items()
            if name.endswith("AsWebRole") and isinstance(value, type)
        ]
        self.assertEqual(len(generated), 34)
        for case in generated:
            with self.subTest(case=case.__name__):
                self.assertEqual(case.service_role, WEB_ROLE)
                self.assertEqual(
                    case.migration_environment, {"PAW_APP_DATABASE_ROLE": WEB_ROLE}
                )
        self.assertNotEqual(WEB_ROLE, OTHER_ROLE)
        self.assertTrue(WEB_ROLE.endswith(RUN_ID))


if __name__ == "__main__":
    unittest.main()
