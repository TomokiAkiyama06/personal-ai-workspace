"""The reset of another account's Passkeys in the split-role deployment (#108).

Migrations run as the owner of the schema; the backend connects as a NON-superuser
application role (``PAW_APP_DATABASE_ROLE``). These tests

* run the service and HTTP test classes of #108 as that role, so every statement of
  the reset and of the redemption of its token is proven to work with exactly the
  privileges the migrations grant;
* pin what revision ``0108`` grants: EXECUTE on ``paw_issue_password_reset_token``
  for the web role only (not PUBLIC, not another role), a ``SECURITY DEFINER``
  function with a pinned ``search_path``, and NO new table privilege: the web role
  still cannot INSERT a token row (so it cannot mint the Owner's) nor DELETE a
  password by itself.

Role names are unique per run and dropped afterwards; the test user must be allowed
to create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import unittest
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from . import test_passkey_reset, test_passkey_reset_http
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

WEB_ROLE = f"paw_pkreset_web_{RUN_ID}"
OTHER_ROLE = f"paw_pkreset_other_{RUN_ID}"
INSUFFICIENT_PRIVILEGE = "42501"
FUNCTION = (
    "paw_issue_password_reset_token(uuid, uuid, uuid, bytea, bytea, timestamptz, "
    "timestamptz)"
)


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


for _module, _names in (
    (
        test_passkey_reset,
        (
            "ResetTest",
            "RulesTest",
            "StepUpTest",
            "TokenBoundaryTest",
            "RaceTest",
            "ArgumentsTest",
        ),
    ),
    (test_passkey_reset_http, ("ResetStoryTest",)),
):
    for _name in _names:
        _case = as_web_role(getattr(_module, _name))
        _short = _module.__name__.rpartition(".")[2].removeprefix("test_passkey_")
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

    def sqlstate_as(self, role: str, sql: str, **params) -> str | None:
        url = make_settings(database_url=url_for_role(role)).database_url
        engine = create_engine(url.get_secret_value())
        try:
            with engine.begin() as connection:
                connection.execute(text(sql), params)
        except DBAPIError as error:
            return error.orig.sqlstate
        finally:
            engine.dispose()
        return None

    def test_only_the_web_role_may_execute_the_function(self):
        self.assertTrue(
            self.owner_scalar(
                "SELECT has_function_privilege(:r, :f, 'EXECUTE')",
                r=WEB_ROLE,
                f=FUNCTION,
            )
        )
        self.assertFalse(
            self.owner_scalar(
                "SELECT has_function_privilege(:r, :f, 'EXECUTE')",
                r=OTHER_ROLE,
                f=FUNCTION,
            )
        )
        self.assertFalse(
            self.owner_scalar(
                "SELECT bool_or(a.grantee = 0) FROM pg_proc p, "
                "aclexplode(p.proacl) a WHERE p.proname = "
                "'paw_issue_password_reset_token'"
            )
        )
        call = (
            "SELECT paw_issue_password_reset_token(gen_random_uuid(), "
            "gen_random_uuid(), gen_random_uuid(), '\\x00', '\\x00', now(), "
            "now() + interval '1 hour')"
        )
        self.assertEqual(self.sqlstate_as(OTHER_ROLE, call), INSUFFICIENT_PRIVILEGE)
        # The web role may call it (it answers false for a user that is not there).
        self.assertIsNone(self.sqlstate_as(WEB_ROLE, call))

    def test_the_function_is_a_definer_with_a_pinned_search_path(self):
        self.assertTrue(
            self.owner_scalar(
                "SELECT prosecdef FROM pg_proc "
                "WHERE proname = 'paw_issue_password_reset_token'"
            )
        )
        self.assertEqual(
            self.owner_scalar(
                "SELECT array_to_string(proconfig, ',') FROM pg_proc "
                "WHERE proname = 'paw_issue_password_reset_token'"
            ),
            "search_path=pg_catalog, pg_temp",
        )

    def test_the_web_role_still_cannot_mint_a_token_or_delete_a_password(self):
        for table, privilege in (
            ("setup_tokens", "INSERT"),
            ("setup_tokens", "DELETE"),
            ("password_credentials", "DELETE"),
        ):
            with self.subTest(table=table, privilege=privilege):
                self.assertFalse(
                    self.owner_scalar(
                        "SELECT has_table_privilege(:r, :t, :p)",
                        r=WEB_ROLE,
                        t=table,
                        p=privilege,
                    )
                )
        for column in ("purpose", "user_id", "expires_at", "revoked_at"):
            with self.subTest(column=column):
                self.assertFalse(
                    self.owner_scalar(
                        "SELECT has_column_privilege(:r, 'setup_tokens', :c, 'UPDATE')",
                        r=WEB_ROLE,
                        c=column,
                    )
                )
        self.assertEqual(
            self.sqlstate_as(
                WEB_ROLE,
                "INSERT INTO setup_tokens (id, audit_ref, user_id, purpose, salt, "
                "secret_hash, created_at, expires_at, attempts) VALUES "
                "(:i, :i, :i, 'recovery', '\\x00', '\\x00', now(), "
                "now() + interval '1 hour', 0)",
                i=uuid.uuid4(),
            ),
            INSUFFICIENT_PRIVILEGE,
        )
        self.assertEqual(
            self.sqlstate_as(WEB_ROLE, "DELETE FROM password_credentials"),
            INSUFFICIENT_PRIVILEGE,
        )
