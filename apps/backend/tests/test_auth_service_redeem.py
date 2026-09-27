"""Redeeming an Owner setup / recovery token through the web flow (PAW-022).

The conditions of Decision 0005 (Issue #19): rate limits per source and in total
before the token is looked at; the web role's privileges (tests/test_auth_grants.py);
a recovery ends every session and replaces the password.
"""

import uuid
from unittest.mock import patch

from sqlalchemy import text

from paw_backend.auth import tokens
from paw_backend.auth.context import RequestContext
from paw_backend.auth.errors import (
    AuthUnavailableError,
    InvalidAuthInputError,
    InvalidCredentialsError,
    PasswordPolicyError,
    PasswordProblem,
    ThrottledError,
    TokenRejectedError,
)
from paw_backend.auth.models import PasskeyRequirement
from paw_backend.authz.audit import PostgresAuditSink
from paw_backend.identity import TokenPurpose
from paw_backend.identity.operator import ROOT_UID, OwnerOperator

from .auth_support import PostgresAuthTestCase, requires_postgres
from .identity_support import lookup_id_of, running_as, wrong_secret_for

SOURCE = "203.0.113.7"
OWNER_PASSWORD = "the owner passphrase, set at setup"
RECOVERED_PASSWORD = "a passphrase chosen after recovery"


def context(source: str = SOURCE) -> RequestContext:
    return RequestContext(uuid.uuid4(), tokens.source_bucket(source))


class RedeemTestCase(PostgresAuthTestCase):
    async def asyncSetUp(self):
        self.enterContext(running_as(ROOT_UID))
        await super().asyncSetUp()
        self.operator = OwnerOperator(
            self.database, PostgresAuditSink(self.database), clock=self.clock
        )

    async def setup_token(self, name="boss"):
        return await self.operator.setup_owner(name)

    async def redeem(self, token, password=OWNER_PASSWORD, source=SOURCE):
        return await self.auth.redeem_owner_token(token, password, context(source))

    async def user_row(self, name="boss"):
        return (await self.query("SELECT * FROM users WHERE login_name = :n", n=name))[
            0
        ]

    async def sessions_of(self, user_id):
        return await self.query(
            "SELECT * FROM auth_sessions WHERE user_id = :id", id=user_id
        )

    async def token_row(self, issued):
        return (
            await self.query(
                "SELECT * FROM setup_tokens WHERE audit_ref = :r", r=issued.audit_ref
            )
        )[0]


@requires_postgres
class SetupTest(RedeemTestCase):
    async def test_the_setup_token_sets_the_password_and_activates_the_owner(self):
        issued = await self.setup_token()
        self.assertEqual((await self.user_row()).status, "invited")

        result = await self.redeem(issued.token)

        self.assertEqual(result.purpose, TokenPurpose.SETUP)
        self.assertEqual(result.user_id, issued.user_id)
        # The Passkey requirement of the Owner is the policy's (default: required).
        self.assertTrue(result.passkey_required)
        self.assertEqual((await self.user_row()).status, "active")
        stored = await self.scalar("SELECT hash FROM password_credentials")
        self.assertTrue(stored.startswith("$argon2id$"))
        self.assertNotIn(OWNER_PASSWORD, await self.everything_stored())

    async def test_the_owner_can_log_in_afterwards_and_only_with_that_password(self):
        issued = await self.setup_token()
        await self.redeem(issued.token)
        with self.assertRaises(InvalidCredentialsError):
            await self.auth.login("boss", "not the passphrase", context())
        result = await self.auth.login("boss", OWNER_PASSWORD, context())
        self.assertEqual(result.session.system_role.value, "owner")

    async def test_the_token_is_single_use(self):
        issued = await self.setup_token()
        await self.redeem(issued.token)
        with self.assertRaises(TokenRejectedError):
            await self.redeem(issued.token, "another passphrase entirely")
        # ... and the second try did not replace the password.
        await self.auth.login("boss", OWNER_PASSWORD, context())

    async def test_the_redemption_and_the_password_are_audited(self):
        issued = await self.setup_token()
        await self.redeem(issued.token)
        summary = await self.audit_summary()
        self.assertEqual(summary[("owner.token.redeem", "allow", "redeemed")], 1)
        self.assertEqual(summary[("auth.password.set", "allow", "setup")], 1)
        # A setup has no session to end: no revocation event.
        self.assertNotIn(("auth.session.revoke_all", "allow", "revoked_all"), summary)
        stored = await self.everything_stored()
        for secret in (issued.token, OWNER_PASSWORD, lookup_id_of(issued.token)):
            self.assertNotIn(secret, stored)

    async def test_the_passkey_requirement_reported_is_the_policy_of_the_owner(self):
        await self.execute(
            "UPDATE auth_policy SET version = 2, passkey_owner = 'optional'"
        )
        issued = await self.setup_token()
        result = await self.redeem(issued.token)
        self.assertFalse(result.passkey_required)
        self.assertEqual(PasskeyRequirement.OPTIONAL.value, "optional")


@requires_postgres
class RecoveryTest(RedeemTestCase):
    async def make_owner_with_sessions(self):
        issued = await self.setup_token()
        await self.redeem(issued.token)
        first = await self.auth.login("boss", OWNER_PASSWORD, context())
        second = await self.auth.login("boss", OWNER_PASSWORD, context("198.51.100.4"))
        return issued.user_id, [first, second]

    async def authenticated(self, token):
        async with self.service_database.session() as session:
            found = await self.services.sessions.authenticate(session, token)
            await session.commit()
            return found

    async def test_a_recovery_ends_every_session_and_replaces_the_password(self):
        user_id, sessions = await self.make_owner_with_sessions()
        for session in sessions:
            self.assertIsNotNone(await self.authenticated(session.token))
        before = await self.scalar("SELECT hash FROM password_credentials")

        recovery = await self.operator.recover_owner()
        result = await self.redeem(recovery.token, RECOVERED_PASSWORD)

        self.assertEqual(result.purpose, TokenPurpose.RECOVERY)
        # Every session is over (Decision 0005, point 7; Issue #19).
        for session in sessions:
            self.assertIsNone(await self.authenticated(session.token))
        rows = await self.sessions_of(user_id)
        self.assertEqual({row.revoked_reason for row in rows}, {"recovery"})
        # The old password is invalid; the new one is set.
        self.assertNotEqual(
            await self.scalar("SELECT hash FROM password_credentials"), before
        )
        with self.assertRaises(InvalidCredentialsError):
            await self.auth.login("boss", OWNER_PASSWORD, context())
        again = await self.auth.login("boss", RECOVERED_PASSWORD, context())
        self.assertEqual(again.session.record.user_id, user_id)

    async def test_a_recovery_is_audited_with_the_password_and_the_revocation(self):
        await self.make_owner_with_sessions()
        recovery = await self.operator.recover_owner()
        await self.redeem(recovery.token, RECOVERED_PASSWORD)
        summary = await self.audit_summary()
        self.assertEqual(summary[("owner.token.redeem", "allow", "redeemed")], 2)
        self.assertEqual(summary[("auth.password.set", "allow", "recovery")], 1)
        self.assertEqual(
            summary[("auth.session.revoke_all", "allow", "revoked_all")], 1
        )

    async def test_a_recovery_lifts_the_login_lock_of_the_owner(self):
        await self.make_owner_with_sessions()
        for _ in range(5):
            with self.assertRaises(InvalidCredentialsError):
                await self.auth.login("boss", "wrong wrong wrong", context())
        with self.assertRaises(ThrottledError):
            await self.auth.login("boss", OWNER_PASSWORD, context())
        recovery = await self.operator.recover_owner()
        await self.redeem(recovery.token, RECOVERED_PASSWORD, source="192.0.2.99")
        result = await self.auth.login(
            "boss", RECOVERED_PASSWORD, context("192.0.2.98")
        )
        self.assertEqual(result.session.login_name, "boss")

    async def test_the_owner_locked_out_by_guessing_recovers_without_waiting(self):
        # The requirement: a lock is never permanent AND the Owner has a way back.
        await self.make_owner_with_sessions()
        for _ in range(9):
            self.clock.advance(hours=1)
            with self.assertRaises((InvalidCredentialsError, ThrottledError)):
                await self.auth.login("boss", "wrong wrong wrong", context())
        recovery = await self.operator.recover_owner()
        await self.redeem(recovery.token, RECOVERED_PASSWORD, source="192.0.2.99")
        await self.auth.login("boss", RECOVERED_PASSWORD, context("192.0.2.98"))

    async def test_a_recovery_of_an_owner_without_a_password_yet_just_sets_one(self):
        issued = await self.setup_token()  # never redeemed
        recovery = await self.operator.recover_owner()
        await self.redeem(recovery.token)
        self.assertEqual((await self.user_row()).status, "active")
        with self.assertRaises(TokenRejectedError):
            await self.redeem(issued.token)  # the older token was superseded


@requires_postgres
class RejectionTest(RedeemTestCase):
    async def test_every_bad_token_is_the_same_answer(self):
        issued = await self.setup_token()
        answers = set()
        for bad in (
            "not a token",
            "pawst1.x.y",
            wrong_secret_for(issued.token),
            "pawst1." + str(uuid.uuid4().hex) + "." + "A" * 43,
        ):
            try:
                await self.redeem(bad)
            except TokenRejectedError as error:
                answers.add((type(error), str(error), error.__cause__))
        self.assertEqual(len(answers), 1)
        self.assertEqual((await self.user_row()).status, "invited")

    async def test_nothing_changes_when_the_token_is_refused(self):
        issued = await self.setup_token()
        with self.assertRaises(TokenRejectedError):
            await self.redeem(wrong_secret_for(issued.token))
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM password_credentials"), 0
        )
        self.assertEqual((await self.user_row()).status, "invited")
        self.assertIsNone((await self.token_row(issued)).used_at)

    async def test_an_expired_token_is_refused(self):
        issued = await self.setup_token()
        self.clock.advance(seconds=1801)
        with self.assertRaises(TokenRejectedError):
            await self.redeem(issued.token)

    async def test_a_bad_password_is_refused_before_the_token_is_spent(
        self,
    ):
        issued = await self.setup_token()
        for password, problem in (
            ("short", PasswordProblem.TOO_SHORT),
            ("password123", PasswordProblem.TOO_COMMON),
            ("x" * 400, PasswordProblem.TOO_LONG),
            ("bell\x07 in the password", PasswordProblem.INVALID_CHARACTER),
        ):
            with self.subTest(password=password[:10]):
                with self.assertRaises(PasswordPolicyError) as caught:
                    await self.redeem(issued.token, password)
                self.assertEqual(caught.exception.problem, problem)
        row = await self.token_row(issued)
        self.assertEqual((row.attempts, row.used_at), (0, None))
        await self.redeem(issued.token)  # ... and the token still works

    async def test_a_password_that_is_the_login_name_rolls_back_and_keeps_the_token(
        self,
    ):
        issued = await self.setup_token("bossman-account")
        with self.assertRaises(PasswordPolicyError) as caught:
            await self.redeem(issued.token, "My bossman-account secret")
        self.assertEqual(caught.exception.problem, PasswordProblem.CONTAINS_LOGIN_NAME)
        row = await self.token_row(issued)
        self.assertIsNone(row.used_at)
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM password_credentials"), 0
        )
        await self.redeem(issued.token, OWNER_PASSWORD)

    async def test_the_arguments_are_checked_before_anything_else(self):
        for token, password, context_ in (
            (None, OWNER_PASSWORD, context()),
            (5, OWNER_PASSWORD, context()),
            ("t" * 513, OWNER_PASSWORD, context()),
            ("pawst1.a.b", None, context()),
            ("pawst1.a.b", 12, context()),
            ("pawst1.a.b", OWNER_PASSWORD, None),
            ("pawst1.a.b", OWNER_PASSWORD, "context"),
        ):
            with self.subTest(token=repr(token)[:10], password=repr(password)[:10]):
                with self.assertRaises(InvalidAuthInputError):
                    await self.auth.redeem_owner_token(token, password, context_)
        self.assertEqual(await self.scalar("SELECT count(*) FROM auth_throttles"), 0)


@requires_postgres
class RateLimitTest(RedeemTestCase):
    """Decision 0005 / Issue #19: limits per source and in total, before the token."""

    async def test_a_guessing_source_is_refused_before_the_token_is_looked_at(
        self,
    ):
        issued = await self.setup_token()
        for _ in range(5):
            with self.assertRaises(TokenRejectedError):
                await self.redeem(wrong_secret_for(issued.token))
        attempts_before = (await self.token_row(issued)).attempts
        # The sixth attempt from this source is refused, even with the RIGHT token,
        # and the token's own attempt counter is not touched.
        with self.assertRaises(ThrottledError) as caught:
            await self.redeem(issued.token)
        self.assertEqual(caught.exception.retry_after_seconds, 60)
        self.assertEqual((await self.token_row(issued)).attempts, attempts_before)
        self.assertIsNone((await self.token_row(issued)).used_at)

    async def test_another_source_is_not_affected_by_a_guessing_one(self):
        issued = await self.setup_token()
        # The token allows 5 tries; use the source's 5 on an unknown token id.
        for _ in range(5):
            with self.assertRaises(TokenRejectedError):
                await self.redeem("pawst1." + uuid.uuid4().hex + "." + "A" * 43)
        with self.assertRaises(ThrottledError):
            await self.redeem(issued.token)
        result = await self.redeem(issued.token, source="198.51.100.4")
        self.assertEqual(result.user_id, issued.user_id)

    async def test_the_global_limit_stops_a_swarm_of_sources(self):
        issued = await self.setup_token()
        for index in range(30):
            with self.assertRaises(TokenRejectedError):
                await self.redeem(
                    "pawst1." + uuid.uuid4().hex + "." + "A" * 43,
                    source=f"192.0.2.{index + 1}",
                )
        # Every source is far below its own limit; the total is not.
        with self.assertRaises(ThrottledError) as caught:
            await self.redeem(issued.token, source="198.51.100.77")
        self.assertEqual(caught.exception.retry_after_seconds, 60)
        self.clock.advance(seconds=60)
        result = await self.redeem(issued.token, source="198.51.100.77")
        self.assertEqual(result.user_id, issued.user_id)

    async def test_a_refused_attempt_does_no_hashing_and_reads_no_token(self):
        issued = await self.setup_token()
        for _ in range(5):
            with self.assertRaises(TokenRejectedError):
                await self.redeem("pawst1." + uuid.uuid4().hex + "." + "A" * 43)
        hasher = self.services.service._hasher
        with patch.object(type(hasher), "hash", side_effect=AssertionError("hashed")):
            with patch.object(
                type(self.services.service._redeemer),
                "redeem",
                side_effect=AssertionError("redeemed"),
            ):
                with self.assertRaises(ThrottledError):
                    await self.redeem(issued.token)

    async def test_the_limits_count_successes_too(self):
        issued = await self.setup_token()
        await self.redeem(issued.token)
        rows = await self.query(
            "SELECT scope, attempts FROM auth_throttles ORDER BY scope"
        )
        self.assertEqual(
            [(r.scope, r.attempts) for r in rows],
            [("redeem_global", 1), ("redeem_source", 1)],
        )

    async def test_the_limits_hold_when_the_requests_race(self):
        issued = await self.setup_token()

        async def attempt(services, index):
            return await services.service.redeem_owner_token(
                "pawst1." + uuid.uuid4().hex + "." + "A" * 43, OWNER_PASSWORD, context()
            )

        results = await self.gather_on_own_engines(20, attempt)
        rejected = [r for r in results if isinstance(r, TokenRejectedError)]
        throttled = [r for r in results if isinstance(r, ThrottledError)]
        self.assertEqual(len(rejected) + len(throttled), 20, results)
        self.assertEqual(len(rejected), 5)  # the source's own limit
        # The token's attempts were never spent by the refused ones.
        self.assertEqual((await self.token_row(issued)).attempts, 0)


@requires_postgres
class HookTest(RedeemTestCase):
    async def test_a_credential_invalidator_runs_in_the_same_transaction(self):
        # PAW-023 registers one that revokes Passkeys: it must run inside the
        # redemption, and if it fails the token is not spent.
        seen = []

        async def invalidate(session, user_id):
            seen.append(user_id)
            count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM password_credentials WHERE user_id = :u"
                    ),
                    {"u": user_id},
                )
            ).scalar()
            seen.append(count)

        service = self.services.service
        service._invalidators = (invalidate,)
        issued = await self.setup_token()
        await self.redeem(issued.token)
        # It saw the password row already written (the same transaction).
        self.assertEqual(seen, [issued.user_id, 1])

    async def test_a_failing_invalidator_rolls_everything_back_and_keeps_the_token(
        self,
    ):
        async def invalidate(session, user_id):
            raise RuntimeError("secret-passkey-detail-022")

        self.services.service._invalidators = (invalidate,)
        issued = await self.setup_token()
        with self.assertLogs("paw_backend.auth.service", "ERROR") as logs:
            with self.assertRaises(AuthUnavailableError):
                await self.redeem(issued.token)
        self.assertNotIn("secret-passkey-detail-022", "\n".join(logs.output))
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM password_credentials"), 0
        )
        self.assertIsNone((await self.token_row(issued)).used_at)
        self.assertEqual((await self.user_row()).status, "invited")
        self.services.service._invalidators = ()
        await self.redeem(issued.token)

    async def test_a_recovery_that_cannot_be_recorded_changes_nothing(self):
        from sqlalchemy.exc import OperationalError

        from paw_backend.auth.audit import AuthAudit

        class Failing(AuthAudit):
            async def record_in(self, session, event):
                raise OperationalError(
                    "INSERT", {}, Exception("audit-secret-detail-022")
                )

        user_id = (await self.setup_token()).user_id
        first = await self.operator.recover_owner()
        del first
        recovery = await self.operator.recover_owner()
        self.services.service._audit = Failing(
            self.services.audit_sink, timeout_seconds=1, clock=self.clock
        )
        with self.assertLogs("paw_backend.auth.service", "ERROR") as logs:
            with self.assertRaises(AuthUnavailableError):
                await self.redeem(recovery.token)
        self.assertIn("OperationalError", "\n".join(logs.output))
        self.assertNotIn("audit-secret-detail-022", "\n".join(logs.output))
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM password_credentials"), 0
        )
        self.assertEqual(user_id, (await self.user_row()).id)
        self.assertIsNone((await self.token_row(recovery)).used_at)


if __name__ == "__main__":
    import unittest

    unittest.main()
