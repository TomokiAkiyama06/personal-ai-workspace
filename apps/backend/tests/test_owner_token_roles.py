"""The web role and the operator role of the Owner tokens, on a real PostgreSQL.

Whoever can INSERT into ``setup_tokens`` (or change a user's role) can take over
the Owner. Migration ``0021`` therefore gives the web application's role
(``PAW_APP_DATABASE_ROLE``) only what redeeming needs, and the server-local
commands their own role (``PAW_OPERATOR_DATABASE_ROLE``). These tests use two
NON-superuser roles (created and dropped here; the test user must be allowed to
create roles) and check both what each may do and what it may not.
"""

import unittest
import uuid
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from paw_backend.app import create_app
from paw_backend.identity import SetupTokenRejectedError, TokenPurpose, tokens
from paw_backend.identity.diagnostics import (
    read_token_table_access,
    warn_if_tokens_can_be_minted,
)

from .identity_support import (
    ROLE_PASSWORD,
    T0,
    PostgresIdentityTestCase,
    migrate,
    requires_postgres,
    role_name,
    sync_database_url,
    url_for_role,
    wrong_secret_for,
)
from .support import FakeDatabase, make_client, make_settings

WEB_ROLE = role_name("web")
OPERATOR_ROLE = role_name("op")
INSUFFICIENT_PRIVILEGE = "42501"
RESTRICT_VIOLATION = "23001"


def drop_roles(engine) -> None:
    with engine.begin() as connection:
        for role in (WEB_ROLE, OPERATOR_ROLE):
            exists = connection.execute(
                text("SELECT count(*) FROM pg_roles WHERE rolname = :r"), {"r": role}
            ).scalar()
            if exists:
                connection.execute(text(f"DROP OWNED BY {role}"))
                connection.execute(text(f"DROP ROLE {role}"))


class RoleSplitTestCase(PostgresIdentityTestCase):
    """Migrated with both roles; the operator and the redeemer use their own."""

    @classmethod
    def setUpClass(cls) -> None:
        migrate("downgrade", "base")
        engine = create_engine(sync_database_url())
        try:
            drop_roles(engine)
            with engine.begin() as connection:
                for role in (WEB_ROLE, OPERATOR_ROLE):
                    connection.execute(
                        text(
                            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB "
                            f"NOCREATEROLE PASSWORD '{ROLE_PASSWORD}'"
                        )
                    )
        finally:
            engine.dispose()
        migrate(
            "upgrade",
            "head",
            PAW_APP_DATABASE_ROLE=WEB_ROLE,
            PAW_OPERATOR_DATABASE_ROLE=OPERATOR_ROLE,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        migrate("downgrade", "base")
        engine = create_engine(sync_database_url())
        try:
            drop_roles(engine)
        finally:
            engine.dispose()

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.web_db = self.new_database(url_for_role(WEB_ROLE))
        self.operator_db = self.new_database(url_for_role(OPERATOR_ROLE))
        self.operator = self.make_operator(self.operator_db)
        self.redeemer = self.make_redeemer(self.web_db)

    async def refused_for(self, database, sql: str, **params) -> str:
        """The SQLSTATE of the error ``sql`` raises as ``database``'s role."""
        with self.assertRaises(DBAPIError) as caught:
            async with database.session() as session:
                await session.execute(text(sql), params)
                await session.commit()
        return caught.exception.orig.sqlstate

    async def run_as(self, database, sql: str, **params) -> None:
        async with database.session() as session:
            await session.execute(text(sql), params)
            await session.commit()

    async def outstanding_token_count(self) -> int:
        return await self.scalar(
            "SELECT count(*) FROM setup_tokens "
            "WHERE used_at IS NULL AND revoked_at IS NULL"
        )


@requires_postgres
class WebRoleTest(RoleSplitTestCase):
    async def test_the_web_role_can_redeem_what_the_operator_issued(self):
        issued = await self.operator.setup_owner("boss")
        seen = []

        async def apply(session, redemption):
            seen.append(redemption.purpose)
            await session.execute(
                text("UPDATE users SET updated_at = :now WHERE id = :id"),
                {"now": T0 + timedelta(seconds=5), "id": redemption.user_id},
            )

        redemption = await self.redeemer.redeem(issued.token, apply=apply)

        self.assertEqual(
            (redemption.user_id, seen), (issued.user_id, [TokenPurpose.SETUP])
        )
        self.assertEqual(
            await self.scalar("SELECT updated_at FROM users"),
            T0 + timedelta(seconds=5),
        )
        # The web role also writes the audit rows of failed attempts.
        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(wrong_secret_for(issued.token))
        summary = await self.audit_summary()
        self.assertEqual(summary[("owner.token.redeem", "allow", "redeemed")], 1)
        self.assertEqual(summary[("owner.token.redeem", "deny", "token_mismatch")], 1)

    async def test_the_web_role_can_do_what_redeeming_needs_and_no_more(self):
        issued = await self.operator.setup_owner("boss")
        user = issued.user_id

        allowed = (
            ("SELECT id FROM users WHERE id = :u FOR UPDATE", {"u": user}),
            ("UPDATE users SET updated_at = now() WHERE id = :u", {"u": user}),
            ("UPDATE setup_tokens SET attempts = attempts + 1", {}),
            ("UPDATE setup_tokens SET locked_at = now()", {}),
            ("UPDATE setup_tokens SET used_at = now()", {}),
            ("SELECT * FROM setup_tokens", {}),
            ("SELECT * FROM users", {}),
        )
        for sql, params in allowed:
            with self.subTest(sql):
                await self.run_as(self.web_db, sql, **params)

    async def test_the_web_role_cannot_create_tokens_users_or_roles(self):
        issued = await self.operator.setup_owner("boss")
        forbidden = {
            "insert a token": (
                "INSERT INTO setup_tokens (id, audit_ref, user_id, purpose, salt, "
                "secret_hash, created_at, expires_at, attempts) VALUES (:i, :r, :u, "
                "'recovery', :salt, :hash, now(), now() + interval '1 hour', 0)",
                {
                    "i": uuid.uuid4(),
                    "r": uuid.uuid4(),
                    "u": issued.user_id,
                    "salt": bytes(16),
                    "hash": bytes(32),
                },
            ),
            "insert a user": (
                "INSERT INTO users VALUES (:i, 'mallory', 'owner', 'active', true, "
                "now(), now())",
                {"i": uuid.uuid4()},
            ),
            "change a role": ("UPDATE users SET system_role = 'admin'", {}),
            "change a status": ("UPDATE users SET status = 'deleted'", {}),
            "change a login name": ("UPDATE users SET login_name = 'mallory'", {}),
            "change the passkey flag": (
                "UPDATE users SET passkey_required = false",
                {},
            ),
            "revoke a token": ("UPDATE setup_tokens SET revoked_at = now()", {}),
            "replace a hash": (
                "UPDATE setup_tokens SET secret_hash = :h",
                {"h": bytes([9]) * 32},
            ),
            "replace a salt": (
                "UPDATE setup_tokens SET salt = :s",
                {"s": bytes([9]) * 16},
            ),
            "extend a token": (
                "UPDATE setup_tokens SET expires_at = expires_at + interval '1 year'",
                {},
            ),
            "change a purpose": ("UPDATE setup_tokens SET purpose = 'recovery'", {}),
            "hand a token to another user": (
                "UPDATE setup_tokens SET user_id = :u",
                {"u": uuid.uuid4()},
            ),
            "delete a user": ("DELETE FROM users", {}),
            "delete a token": ("DELETE FROM setup_tokens", {}),
            "truncate the users": ("TRUNCATE users CASCADE", {}),
        }
        for name, (sql, params) in forbidden.items():
            with self.subTest(name):
                self.assertEqual(
                    await self.refused_for(self.web_db, sql, **params),
                    INSUFFICIENT_PRIVILEGE,
                )

        self.assertEqual(await self.scalar("SELECT system_role FROM users"), "owner")
        self.assertEqual(await self.scalar("SELECT count(*) FROM setup_tokens"), 1)

    async def test_the_reviewed_takeover_no_longer_works_from_the_web_role(self):
        # The review's attack, step by step, as only the web role.
        issued = await self.operator.setup_owner("boss")
        forged = tokens.generate()

        step_revoke = await self.refused_for(
            self.web_db, "UPDATE setup_tokens SET revoked_at = now()"
        )
        step_insert = await self.refused_for(
            self.web_db,
            "INSERT INTO setup_tokens (id, audit_ref, user_id, purpose, salt, "
            "secret_hash, created_at, expires_at, attempts) VALUES (:i, :r, :u, "
            "'recovery', :salt, :hash, now(), now() + interval '1 hour', 0)",
            i=forged.token_id,
            r=forged.audit_ref,
            u=issued.user_id,
            salt=forged.salt,
            hash=forged.secret_hash,
        )
        step_promote = await self.refused_for(
            self.web_db,
            "UPDATE users SET system_role = 'admin' WHERE system_role = 'owner'",
        )

        self.assertEqual(
            (step_revoke, step_insert, step_promote), (INSUFFICIENT_PRIVILEGE,) * 3
        )
        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(forged.token)
        # The Owner's real token is untouched and still works.
        self.assertEqual(
            (await self.redeemer.redeem(issued.token)).user_id, issued.user_id
        )

    async def test_the_web_role_can_burn_a_token_but_not_revive_or_forge_one(self):
        # What is NOT guarded: the web role can spend the outstanding token
        # (availability, not takeover). The operator's recovery is the way out.
        issued = await self.operator.setup_owner("boss")
        await self.run_as(self.web_db, "UPDATE setup_tokens SET used_at = now()")

        with self.assertRaises(SetupTokenRejectedError):
            await self.redeemer.redeem(issued.token)
        self.assertEqual(
            await self.refused_for(
                self.web_db, "UPDATE setup_tokens SET used_at = NULL"
            ),
            RESTRICT_VIOLATION,
        )
        recovery = await self.operator.recover_owner()
        self.assertEqual(
            (await self.redeemer.redeem(recovery.token)).purpose, TokenPurpose.RECOVERY
        )

    async def test_catalog_privileges_of_the_web_role(self):
        async def table(privilege: str, name: str) -> bool:
            return await self.scalar(
                "SELECT has_table_privilege(:r, :t, :p)",
                r=WEB_ROLE,
                t=name,
                p=privilege,
            )

        async def column(privilege: str, name: str, col: str) -> bool:
            return await self.scalar(
                "SELECT has_column_privilege(:r, :t, :c, :p)",
                r=WEB_ROLE,
                t=name,
                c=col,
                p=privilege,
            )

        for name in ("users", "setup_tokens"):
            self.assertEqual(
                [
                    await table(privilege, name)
                    for privilege in (
                        "SELECT",
                        "INSERT",
                        "UPDATE",
                        "DELETE",
                        "TRUNCATE",
                    )
                ],
                [True, False, False, False, False],
                name,
            )
        writable = {
            ("users", "updated_at"),
            ("setup_tokens", "attempts"),
            ("setup_tokens", "used_at"),
            ("setup_tokens", "locked_at"),
        }
        columns = await self.query(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_name IN ('users', 'setup_tokens')"
        )
        self.assertEqual(
            {(t, c) for t, c in columns if await column("UPDATE", t, c)},
            writable,
        )


@requires_postgres
class OperatorRoleTest(RoleSplitTestCase):
    async def test_the_operator_role_can_set_up_recover_and_replace(self):
        setup = await self.operator.setup_owner("boss")
        recovery = await self.operator.recover_owner()
        await self.execute("UPDATE users SET status = 'deleted'")

        replacement = await self.operator.setup_owner(
            "newboss", replace_non_live_owner=True
        )

        self.assertEqual(
            (setup.purpose, recovery.purpose, replacement.login_name),
            (TokenPurpose.SETUP, TokenPurpose.RECOVERY, "newboss"),
        )
        self.assertEqual(
            await self.query("SELECT login_name, system_role FROM users ORDER BY 1"),
            [("boss", "user"), ("newboss", "owner")],
        )
        summary = await self.audit_summary()  # written by the operator's role
        self.assertEqual(summary[("owner.replace", "allow", "replaced")], 1)
        self.assertEqual(summary[("owner.create", "allow", "created")], 2)

    async def test_the_web_role_redeems_what_the_operator_role_recovered(self):
        await self.operator.setup_owner("boss")
        recovery = await self.operator.recover_owner()

        redemption = await self.redeemer.redeem(recovery.token)

        self.assertEqual(redemption.purpose, TokenPurpose.RECOVERY)

    async def test_the_operator_role_cannot_redeem_a_token(self):
        issued = await self.operator.setup_owner("boss")
        wrong_role = self.make_redeemer(self.operator_db)

        with self.assertRaises(ProgrammingError):
            await wrong_role.redeem(issued.token)

        self.assertEqual(await self.outstanding_token_count(), 1)

    async def test_the_operator_role_cannot_rewrite_users_tokens_or_the_audit(self):
        await self.operator.setup_owner("boss")
        forbidden = {
            "change a status": "UPDATE users SET status = 'active'",
            "change a login name": "UPDATE users SET login_name = 'x1234'",
            "clear the passkey flag": "UPDATE users SET passkey_required = false",
            "consume a token": "UPDATE setup_tokens SET used_at = now()",
            "reset the attempts": "UPDATE setup_tokens SET attempts = 0",
            "replace a hash": "UPDATE setup_tokens SET secret_hash = '\\x00'",
            "delete a user": "DELETE FROM users",
            "delete a token": "DELETE FROM setup_tokens",
            "truncate users": "TRUNCATE users CASCADE",
            "read the audit": "SELECT * FROM audit_events",
            "rewrite the audit": "UPDATE audit_events SET reason = 'x'",
            "delete the audit": "DELETE FROM audit_events",
            "truncate the audit": "TRUNCATE audit_events",
        }
        for name, sql in forbidden.items():
            with self.subTest(name):
                self.assertEqual(
                    await self.refused_for(self.operator_db, sql),
                    INSUFFICIENT_PRIVILEGE,
                )

    async def test_the_operator_role_may_write_the_audit_and_nothing_else_there(self):
        await self.operator.setup_owner("boss")

        self.assertEqual(
            (await self.audit_summary())[("owner.create", "allow", "created")], 1
        )

    async def test_catalog_privileges_of_the_operator_role(self):
        async def column(privilege: str, name: str, col: str) -> bool:
            return await self.scalar(
                "SELECT has_column_privilege(:r, :t, :c, :p)",
                r=OPERATOR_ROLE,
                t=name,
                c=col,
                p=privilege,
            )

        columns = await self.query(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_name IN ('users', 'setup_tokens')"
        )
        updatable = {(t, c) for t, c in columns if await column("UPDATE", t, c)}
        self.assertEqual(
            updatable,
            {
                ("users", "system_role"),
                ("users", "updated_at"),
                ("setup_tokens", "revoked_at"),
            },
        )
        for name in ("users", "setup_tokens"):
            self.assertEqual(
                [
                    await self.scalar(
                        "SELECT has_table_privilege(:r, :t, :p)",
                        r=OPERATOR_ROLE,
                        t=name,
                        p=privilege,
                    )
                    for privilege in ("SELECT", "INSERT", "DELETE", "TRUNCATE")
                ],
                [True, True, False, False],
                name,
            )


@requires_postgres
class DiagnosticTest(RoleSplitTestCase):
    async def test_the_web_role_is_reported_as_unable_to_mint_tokens(self):
        access = await read_token_table_access(self.web_db)

        self.assertFalse(access.can_mint_owner_token)
        self.assertFalse(access.owns)
        with self.assertNoLogs("paw_backend.identity.diagnostics", level="WARNING"):
            await warn_if_tokens_can_be_minted(self.web_db, 3.0)

    async def test_a_role_that_can_insert_tokens_or_change_roles_is_warned_about(self):
        for name, database in (
            ("operator", self.operator_db),
            ("schema owner", self.database),
        ):
            with self.subTest(name):
                access = await read_token_table_access(database)
                self.assertTrue(access.can_mint_owner_token)
                with self.assertLogs(
                    "paw_backend.identity.diagnostics", level="WARNING"
                ) as logs:
                    await warn_if_tokens_can_be_minted(database, 3.0)
                text_ = "\n".join(logs.output)
                self.assertIn("can create Owner tokens", text_)
                self.assertIn("PAW_OPERATOR_DATABASE_ROLE", text_)
                self.assertNotIn(ROLE_PASSWORD, text_)
                self.assertNotIn(OPERATOR_ROLE, text_)

    async def test_the_check_is_silent_when_the_database_cannot_be_reached(self):
        unreachable = self.new_database("postgresql://u:pw@127.0.0.1:1/none")

        with self.assertNoLogs("paw_backend.identity.diagnostics", level="WARNING"):
            await warn_if_tokens_can_be_minted(unreachable, 2.0)

    async def test_the_check_is_silent_without_a_database(self):
        with self.assertNoLogs("paw_backend.identity.diagnostics", level="WARNING"):
            await warn_if_tokens_can_be_minted(FakeDatabase(), 2.0)

    async def test_the_application_runs_the_check_at_startup(self):
        calls = []

        async def recorder(database, timeout_seconds):
            calls.append((database, timeout_seconds))

        database = FakeDatabase()
        with patch("paw_backend.app.warn_if_tokens_can_be_minted", recorder):
            app = create_app(make_settings(), database=database)
            with make_client(app):
                pass

        self.assertEqual([call[0] for call in calls], [database])


if __name__ == "__main__":
    unittest.main()
