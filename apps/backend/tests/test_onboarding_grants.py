"""PAW-024's tables in the split-role deployment (real PostgreSQL).

Migrations run as the owner of the schema; the backend connects as a NON-superuser
application role (``PAW_APP_DATABASE_ROLE``). These tests

* run the service and HTTP test classes of PAW-024 as that role, so every statement
  of the invitation, pairing and lifecycle code is proven to work with exactly the
  privileges revision ``0124`` grants;
* pin the privileges of the three new tables, column by column, and of the two
  functions;
* check what the role must NOT be able to do: change ``users.status`` or create a
  user directly, write or rewrite the status history, re-key a token, delete a
  pairing.

Role names are unique per run and dropped afterwards; the test user must be allowed
to create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import unittest
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from . import (
    test_onboarding_http,
    test_onboarding_invitations,
    test_onboarding_lifecycle,
    test_onboarding_pairing,
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

WEB_ROLE = f"paw_onboarding_web_{RUN_ID}"
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
# table -> (table-level privileges, columns the web role may UPDATE). The exact copy
# of the choices (and their reasons) in migration 0124.
EXPECTED = {
    "user_invitations": (
        {"SELECT", "INSERT"},
        {"attempts", "used_at", "revoked_at", "revoked_reason", "locked_at"},
    ),
    "device_pairings": (
        {"SELECT", "INSERT"},
        {
            "state",
            "approval_required",
            "claim_id",
            "claim_salt",
            "claim_hash",
            "confirm_salt",
            "confirm_hash",
            "confirm_attempts",
            "device_label",
            "remember_me",
            "expires_at",
            "claimed_at",
            "decided_at",
            "decided_by_session",
            "completed_at",
            "created_session",
            "ended_at",
            "ended_reason",
            "attempts",
            "locked_at",
        },
    ),
    "user_status_changes": ({"SELECT"}, set()),
}
FUNCTIONS = (
    "paw_invite_user(uuid, text, text, timestamptz, uuid)",
    "paw_change_user_status(uuid, text, text, timestamptz, uuid)",
)


def owner_engine():
    return create_engine(sync_database_url())


def drop_role(engine) -> None:
    with engine.begin() as connection:
        exists = connection.execute(
            text("SELECT count(*) FROM pg_roles WHERE rolname = :r"), {"r": WEB_ROLE}
        ).scalar()
        if exists:
            connection.execute(text(f"DROP OWNED BY {WEB_ROLE}"))
            connection.execute(text(f"DROP ROLE {WEB_ROLE}"))


def setUpModule():
    if not TEST_DATABASE_URL:
        raise unittest.SkipTest("PAW_TEST_DATABASE_URL is not set")
    migrate("downgrade", "base")
    engine = owner_engine()
    try:
        drop_role(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE ROLE {WEB_ROLE} LOGIN NOSUPERUSER NOCREATEDB "
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
            drop_role(engine)
        finally:
            engine.dispose()


class WebRole:
    migration_environment = {"PAW_APP_DATABASE_ROLE": WEB_ROLE}
    service_role = WEB_ROLE


def as_web_role(case: type) -> type:
    return type(f"{case.__name__}AsWebRole", (WebRole, case), {"__module__": __name__})


for _module, _names in (
    (test_onboarding_invitations, ("InviteTest", "RedeemTest", "NoStrayTest")),
    (test_onboarding_lifecycle, ("DeleteTest", "RestoreTest")),
    (
        test_onboarding_pairing,
        ("UserPairingTest", "ApprovalTest", "PairingGateTest"),
    ),
    (
        test_onboarding_http,
        ("InvitationRoutesTest", "LifecycleRoutesTest", "PairingRoutesTest"),
    ),
):
    for _name in _names:
        _case = as_web_role(getattr(_module, _name))
        _short = _module.__name__.rpartition(".")[2].removeprefix("test_onboarding_")
        _case.__qualname__ = _case.__name__ = f"{_short}_{_name}AsWebRole"
        globals()[_case.__name__] = _case
del _module, _names, _name, _case, _short


@requires_postgres
class PrivilegesTest(PostgresAuthTestCase):
    migration_environment = {"PAW_APP_DATABASE_ROLE": WEB_ROLE}

    def owner_scalar(self, sql: str, **params):
        engine = owner_engine()
        try:
            with engine.connect() as connection:
                return connection.execute(text(sql), params).scalar()
        finally:
            engine.dispose()

    def sqlstate_as_web_role(self, sql: str, **params) -> str | None:
        url = make_settings(database_url=url_for_role(WEB_ROLE)).database_url
        engine = create_engine(url.get_secret_value())
        try:
            with engine.begin() as connection:
                connection.execute(text(sql), params)
        except DBAPIError as error:
            return error.orig.sqlstate
        finally:
            engine.dispose()
        return None

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
                self.assertTrue(columns)
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

    def owner_rows(self, sql: str, **params) -> list:
        engine = owner_engine()
        try:
            with engine.connect() as connection:
                return [tuple(r) for r in connection.execute(text(sql), params)]
        finally:
            engine.dispose()

    def test_the_functions_are_executable_by_the_web_role_only(self):
        for signature in FUNCTIONS:
            with self.subTest(signature):
                self.assertTrue(
                    self.owner_scalar(
                        "SELECT has_function_privilege(:r, :f, 'EXECUTE')",
                        r=WEB_ROLE,
                        f=signature,
                    )
                )
                self.assertFalse(
                    self.owner_scalar(
                        "SELECT has_function_privilege('public', :f, 'EXECUTE')",
                        f=signature,
                    )
                )
                self.assertTrue(
                    self.owner_scalar(
                        "SELECT prosecdef FROM pg_proc WHERE oid = CAST(:f AS "
                        "regprocedure)",
                        f=signature,
                    )
                )

    def test_what_the_web_role_may_not_do(self):
        user = uuid.uuid4()
        self.assertIsNone(
            self.sqlstate_as_web_role(
                "SELECT paw_invite_user(:u, 'bob', 'user', now(), NULL)", u=user
            )
        )
        for sql in (
            "UPDATE users SET status = 'active'",
            "UPDATE users SET system_role = 'admin'",
            "INSERT INTO users (id, login_name, system_role, status, "
            "passkey_required, created_at, updated_at) VALUES (gen_random_uuid(), "
            "'eve', 'admin', 'active', true, now(), now())",
            "INSERT INTO user_status_changes (id, user_id, new_status, changed_at, "
            "recorded_at) VALUES (gen_random_uuid(), "
            f"'{user}', 'active', now(), now())",
            "UPDATE user_status_changes SET new_status = 'active'",
            "DELETE FROM user_status_changes",
            "UPDATE user_invitations SET secret_hash = secret_hash",
            "UPDATE user_invitations SET expires_at = expires_at",
            "UPDATE user_invitations SET user_id = user_id",
            "DELETE FROM user_invitations",
            "UPDATE device_pairings SET secret_hash = secret_hash",
            "UPDATE device_pairings SET user_id = user_id",
            "DELETE FROM device_pairings",
            "TRUNCATE device_pairings",
        ):
            with self.subTest(sql=sql[:50]):
                self.assertEqual(
                    self.sqlstate_as_web_role(sql), INSUFFICIENT_PRIVILEGE, sql
                )
