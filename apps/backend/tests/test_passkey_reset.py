"""Resetting another account's Passkeys and password (#108, PostgreSQL).

The Owner resets an Admin or a User, an Admin a User (the rule of unlocking an
account); nobody resets the Owner or themselves. The actor's own session needs a
recent Passkey step-up, judged before the target is looked at. One transaction
revokes every Passkey of the target (``admin_reset``), deletes its challenges, ends
every session (``admin``), deletes the password and issues a one-time password reset
token, which the target spends with the ordinary token redemption; the next sign-in
is enrolment-only when the policy requires a Passkey.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from paw_backend.auth.errors import (
    AccountNotFoundError,
    AuthPermissionError,
    InvalidAuthInputError,
    InvalidCredentialsError,
    StepUpMethodInsufficientError,
    StepUpRequiredError,
    TokenRejectedError,
)
from paw_backend.auth.models import AuthMethod, PasskeyGate
from paw_backend.auth.passkeys.models import PasskeyRevokeReason
from paw_backend.auth.passkeys.service import PasskeyService
from paw_backend.auth.reset_tokens import (
    DEFAULT_RESET_TOKEN_TTL_SECONDS,
    MAX_RESET_TOKEN_TTL_SECONDS,
    MIN_RESET_TOKEN_TTL_SECONDS,
    ResetTokenRefused,
    issue_in,
    validate_ttl,
)
from paw_backend.auth.state import StepUpEvidence
from paw_backend.authz import Principal, SystemRole
from paw_backend.identity import TokenPurpose
from paw_backend.identity import tokens as identity_tokens
from paw_backend.identity.redeemer import ELIGIBLE_ROLES

from .auth_support import PASSWORD, requires_postgres
from .passkey_pg_support import (
    ADMIN_PASSWORD,
    OWNER_PASSWORD,
    PasskeyTestCase,
    context,
    device,
)

NEW_PASSWORD = "a passphrase chosen after the reset"


def principal(user) -> Principal:
    return Principal(user.id, SystemRole(user.role))


class ResetCase(PasskeyTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.owner = await self.make_owner()
        self.admin = await self.make_admin()

    async def actor_session(self, user, password=None):
        """An open session of ``user`` with a fresh Passkey step-up (its id)."""
        session, _ = await self.fully_stepped_up(user, password)
        return session.record.id

    async def reset_as(self, actor, target, session_id, ctx=None):
        return await self.passkeys.reset_account(
            principal(actor), target.id, ctx or context(), session_id=session_id
        )

    async def outstanding_tokens(self, user_id: uuid.UUID) -> list:
        return await self.query(
            "SELECT purpose, expires_at, attempts FROM setup_tokens "
            "WHERE user_id = :u AND used_at IS NULL AND revoked_at IS NULL",
            u=user_id,
        )

    async def password_of(self, user_id: uuid.UUID):
        rows = await self.query(
            "SELECT hash FROM password_credentials WHERE user_id = :u", u=user_id
        )
        return rows[0].hash if rows else None


@requires_postgres
class ResetTest(ResetCase):
    async def test_the_owner_resets_an_admin_who_lost_every_device(self):
        # The Admin has two Passkeys, two sessions and a ceremony in flight.
        admin_session, admin_device = await self.enrolled_session(
            self.admin, ADMIN_PASSWORD
        )
        admin_session = (await self.authenticate(admin_session, admin_device)).session
        await self.register(admin_session, device(), name="B")
        other = await self.sign_in(self.admin, password=ADMIN_PASSWORD)
        await self.passkeys.authenticate_begin(other, context())
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)

        result = await self.reset_as(self.owner, self.admin, owner_session)

        self.assertEqual(
            (result.user_id, result.passkeys_revoked, result.sessions_ended),
            (self.admin.id, 2, 2),
        )
        rows = await self.passkey_rows(self.admin.id)
        self.assertEqual(
            [(r.revoked_at, r.revoked_reason) for r in rows],
            [(self.clock.now, "admin_reset")] * 2,
        )
        self.assertEqual(
            await self.query(
                "SELECT * FROM passkey_challenges WHERE user_id = :u", u=self.admin.id
            ),
            [],
        )
        ended = await self.query(
            "SELECT revoked_reason FROM auth_sessions WHERE user_id = :u",
            u=self.admin.id,
        )
        self.assertEqual([r.revoked_reason for r in ended], ["admin", "admin"])
        # The password is gone with them: it no longer signs in.
        self.assertIsNone(await self.password_of(self.admin.id))
        with self.assertRaises(InvalidCredentialsError):
            await self.sign_in(self.admin, password=ADMIN_PASSWORD)
        # One outstanding password reset token, valid for the configured time.
        (token_row,) = await self.outstanding_tokens(self.admin.id)
        self.assertEqual(
            (token_row.purpose, token_row.expires_at, token_row.attempts),
            (
                "password_reset",
                self.clock.now + timedelta(seconds=DEFAULT_RESET_TOKEN_TTL_SECONDS),
                0,
            ),
        )
        self.assertEqual(result.reset_token.expires_at, token_row.expires_at)
        self.assertTrue(result.reset_token.token.startswith("pawst1."))
        # The Owner's own sessions and Passkeys are untouched.
        self.assertIsNone((await self.session_row(owner_session)).revoked_at)
        self.assertEqual(
            [r.revoked_at for r in await self.passkey_rows(self.owner.id)], [None]
        )

    async def test_the_admin_sets_a_new_password_and_enrols_again(self):
        await self.enrolled_session(self.admin, ADMIN_PASSWORD)
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        result = await self.reset_as(self.owner, self.admin, owner_session)

        redeemed = await self.auth.redeem_owner_token(
            result.reset_token.token, NEW_PASSWORD, context()
        )
        self.assertEqual(
            (redeemed.purpose, redeemed.user_id, redeemed.passkey_required),
            (TokenPurpose.PASSWORD_RESET, self.admin.id, True),
        )
        # Not a dead end: the new password opens an enrolment-only session.
        again = await self.sign_in(self.admin, password=NEW_PASSWORD)
        self.assertIs(again.record.passkey_gate, PasskeyGate.ENROLLMENT_REQUIRED)
        registered = await self.register(again, device())
        self.assertIs(registered.auth.record.passkey_gate, PasskeyGate.OPEN)
        # The token is spent: a second use is refused.
        with self.assertRaises(TokenRejectedError):
            await self.auth.redeem_owner_token(
                result.reset_token.token, "yet another passphrase", context()
            )
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.passkey.reset", "allow", "reset")], 1)
        self.assertEqual(
            summary[("user.password_reset_token.redeem", "allow", "redeemed")], 1
        )
        self.assertEqual(
            summary[("user.password_reset_token.redeem", "deny", "token_used")], 1
        )
        self.assertEqual(summary[("auth.password.set", "allow", "password_reset")], 1)
        self.assertNotIn(("owner.token.redeem", "allow", "redeemed"), summary)
        password_set = [
            r for r in await self.audit_rows() if r.action == "auth.password.set"
        ][0]
        self.assertEqual(
            (password_set.actor_id, password_set.actor_role, password_set.resource_id),
            (self.admin.id, "admin", self.admin.id),
        )

    async def test_a_user_whose_passkey_is_optional_signs_in_normally_afterwards(
        self,
    ):
        alice = await self.make_user("alice")
        await self.enrolled_session(alice)
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        result = await self.reset_as(self.owner, alice, owner_session)
        redeemed = await self.auth.redeem_owner_token(
            result.reset_token.token, NEW_PASSWORD, context()
        )
        self.assertFalse(redeemed.passkey_required)
        again = await self.sign_in(alice, password=NEW_PASSWORD)
        self.assertIs(again.record.passkey_gate, PasskeyGate.OPEN)

    async def test_the_audit_row_names_the_actor_and_the_target_only(self):
        alice = await self.make_user("alice")
        await self.enrolled_session(alice)
        admin_session = await self.actor_session(self.admin, ADMIN_PASSWORD)
        result = await self.reset_as(self.admin, alice, admin_session)
        (row,) = [
            r for r in await self.audit_rows() if r.action == "auth.passkey.reset"
        ]
        self.assertEqual(
            (
                row.decision,
                row.reason,
                row.actor_id,
                row.actor_role,
                row.resource_kind,
                row.resource_id,
            ),
            ("allow", "reset", self.admin.id, "admin", "user", alice.id),
        )
        # Neither the token, nor its secret, nor its lookup id is stored in clear.
        stored = await self.everything_stored()
        stored += "\n".join(
            str(r[0]) for r in await self.query("SELECT t::text FROM setup_tokens t")
        )
        _prefix, token_id, secret = result.reset_token.token.split(".")
        self.assertNotIn(secret, stored)
        self.assertNotIn(result.reset_token.token, stored)
        audit_text = "\n".join(
            str(r[0])
            for r in await self.query(
                "SELECT t::text FROM audit_events t WHERE recorded_at >= :s",
                s=self.started_at,
            )
        )
        self.assertNotIn(str(uuid.UUID(hex=token_id)), audit_text)
        self.assertNotIn("alice", audit_text)

    async def test_the_token_is_never_logged(self):
        alice = await self.make_user("alice")
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        with self.no_secret_in_logs() as logs:
            result = await self.reset_as(self.owner, alice, owner_session)
        self.assertNotIn(result.reset_token.token.split(".")[2], logs.text)
        self.assertNotIn("pawst1", logs.text)
        self.assertNotIn(result.reset_token.token, repr(result))

    async def test_an_invited_account_can_be_reset_and_becomes_active(self):
        bob = await self.make_user("bob", status="invited", password=None)
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        result = await self.reset_as(self.owner, bob, owner_session)
        self.assertEqual((result.passkeys_revoked, result.sessions_ended), (0, 0))
        await self.auth.redeem_owner_token(
            result.reset_token.token, NEW_PASSWORD, context()
        )
        self.assertEqual(
            await self.scalar("SELECT status FROM users WHERE id = :u", u=bob.id),
            "active",
        )

    async def test_a_second_reset_replaces_the_first_token(self):
        alice = await self.make_user("alice")
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        first = await self.reset_as(self.owner, alice, owner_session)
        second = await self.reset_as(self.owner, alice, owner_session)
        self.assertEqual(len(await self.outstanding_tokens(alice.id)), 1)
        with self.assertRaises(TokenRejectedError):
            await self.auth.redeem_owner_token(
                first.reset_token.token, NEW_PASSWORD, context()
            )
        await self.auth.redeem_owner_token(
            second.reset_token.token, NEW_PASSWORD, context()
        )
        self.assertEqual(
            (await self.audit_summary())[
                ("user.password_reset_token.redeem", "deny", "token_revoked")
            ],
            1,
        )

    async def test_the_login_lock_of_the_target_is_lifted_by_the_new_password(self):
        alice = await self.make_user("alice")
        for _ in range(5):
            with self.assertRaises(InvalidCredentialsError):
                await self.auth.login("alice", "wrong wrong wrong", context())
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        result = await self.reset_as(self.owner, alice, owner_session)
        await self.auth.redeem_owner_token(
            result.reset_token.token, NEW_PASSWORD, context()
        )
        await self.sign_in(alice, password=NEW_PASSWORD)

    async def test_an_expired_token_is_refused(self):
        alice = await self.make_user("alice")
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        result = await self.reset_as(self.owner, alice, owner_session)
        await self.advance(seconds=DEFAULT_RESET_TOKEN_TTL_SECONDS)
        with self.assertRaises(TokenRejectedError):
            await self.auth.redeem_owner_token(
                result.reset_token.token, NEW_PASSWORD, context()
            )
        self.assertIsNone(await self.password_of(alice.id))


@requires_postgres
class RulesTest(ResetCase):
    async def test_an_admin_resets_a_user_but_not_an_admin_the_owner_or_themselves(
        self,
    ):
        alice = await self.make_user("alice")
        other_admin = await self.make_admin("admin-two")
        admin_session = await self.actor_session(self.admin, ADMIN_PASSWORD)
        await self.reset_as(self.admin, alice, admin_session)
        for target in (other_admin, self.owner, self.admin):
            with self.subTest(target=target.login_name):
                with self.assertRaises(AuthPermissionError):
                    await self.reset_as(self.admin, target, admin_session)
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.passkey.reset", "deny", "role_not_allowed")], 3)
        self.assertEqual(summary[("auth.passkey.reset", "allow", "reset")], 1)
        # Nothing of the refused targets changed.
        for target in (other_admin, self.owner, self.admin):
            self.assertIsNotNone(await self.password_of(target.id))
            self.assertEqual(await self.outstanding_tokens(target.id), [])

    async def test_the_owner_cannot_reset_the_owner(self):
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        with self.assertRaises(AuthPermissionError):
            await self.reset_as(self.owner, self.owner, owner_session)
        self.assertEqual(
            [r.revoked_at for r in await self.passkey_rows(self.owner.id)], [None]
        )
        self.assertIsNone((await self.session_row(owner_session)).revoked_at)
        self.assertEqual(await self.outstanding_tokens(self.owner.id), [])

    async def test_a_user_cannot_reset_anyone(self):
        alice = await self.make_user("alice")
        bob = await self.make_user("bob")
        alice_session = await self.actor_session(alice)
        for target in (bob, alice, self.admin):
            with self.assertRaises(AuthPermissionError):
                await self.reset_as(alice, target, alice_session)
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.passkey.reset", "deny", "role_not_allowed")
            ],
            3,
        )

    async def test_an_unknown_or_closed_account_is_not_found(self):
        gone = await self.make_user("gone", status="deleted")
        leaving = await self.make_user("leaving", status="pending_deletion")
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        for target_id in (uuid.uuid4(), gone.id, leaving.id):
            with self.assertRaises(AccountNotFoundError):
                await self.passkeys.reset_account(
                    principal(self.owner),
                    target_id,
                    context(),
                    session_id=owner_session,
                )
        self.assertEqual(
            (await self.audit_summary())[("auth.passkey.reset", "deny", "not_found")],
            3,
        )


@requires_postgres
class StepUpTest(ResetCase):
    async def test_no_step_up_is_refused_before_the_target_is_looked_at(self):
        alice = await self.make_user("alice")
        auth, _ = await self.enrolled_session(self.owner, OWNER_PASSWORD)
        await self.execute(
            "UPDATE auth_sessions SET stepup_at = NULL, stepup_method = NULL"
        )
        for target_id in (alice.id, uuid.uuid4(), self.owner.id):
            with self.assertRaises(StepUpRequiredError) as caught:
                await self.passkeys.reset_account(
                    principal(self.owner),
                    target_id,
                    context(),
                    session_id=auth.record.id,
                )
            self.assertNotIsInstance(caught.exception, StepUpMethodInsufficientError)
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.passkey.reset", "deny", "step_up_required")
            ],
            3,
        )
        self.assertIsNotNone(await self.password_of(alice.id))

    async def test_a_password_step_up_is_not_enough(self):
        alice = await self.make_user("alice")
        auth, _ = await self.enrolled_session(self.owner, OWNER_PASSWORD)
        stepped = await self.auth.step_up(
            auth, StepUpEvidence(AuthMethod.PASSWORD, OWNER_PASSWORD), context()
        )
        with self.assertRaises(StepUpMethodInsufficientError):
            await self.reset_as(self.owner, alice, stepped.session.record.id)
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.passkey.reset", "deny", "step_up_method_insufficient")
            ],
            1,
        )

    async def test_an_expired_step_up_is_refused(self):
        alice = await self.make_user("alice")
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        await self.advance(minutes=31)
        with self.assertRaises(StepUpRequiredError):
            await self.reset_as(self.owner, alice, owner_session)

    async def test_another_users_session_does_not_count(self):
        alice = await self.make_user("alice")
        admin_session = await self.actor_session(self.admin, ADMIN_PASSWORD)
        # The Owner names the Admin's (stepped-up) session as theirs.
        with self.assertRaises(StepUpRequiredError):
            await self.reset_as(self.owner, alice, admin_session)

    async def test_a_restricted_session_never_counts(self):
        alice = await self.make_user("alice")
        _, owner_device = await self.enrolled_session(self.owner, OWNER_PASSWORD)
        pending = await self.sign_in(self.owner, password=OWNER_PASSWORD)
        self.assertIs(pending.record.passkey_gate, PasskeyGate.ASSERTION_REQUIRED)
        await self.fake_passkey_step_up(pending.record.id)
        with self.assertRaises(StepUpRequiredError):
            await self.reset_as(self.owner, alice, pending.record.id)


@requires_postgres
class TokenBoundaryTest(ResetCase):
    """The web side still cannot mint (or spend) a token for the Owner."""

    async def call_issue(self, user_id, *, created_at=None, expires_at=None):
        created_at = created_at or self.clock.now
        async with self.service_database.session() as session:
            issued = (
                await session.execute(
                    text(
                        "SELECT paw_issue_password_reset_token(:u, :t, :a, :s, :h, "
                        ":c, :e)"
                    ),
                    {
                        "u": user_id,
                        "t": uuid.uuid4(),
                        "a": uuid.uuid4(),
                        "s": b"s" * 16,
                        "h": b"h" * 32,
                        "c": created_at,
                        "e": expires_at or created_at + timedelta(hours=1),
                    },
                )
            ).scalar_one()
            await session.commit()
        return issued

    async def test_the_function_refuses_the_owner_and_accounts_that_are_not_live(
        self,
    ):
        gone = await self.make_user("gone", status="deleted")
        for user_id in (self.owner.id, gone.id, uuid.uuid4()):
            with self.subTest(user=str(user_id)):
                self.assertIs(await self.call_issue(user_id), False)
        self.assertEqual(await self.query("SELECT * FROM setup_tokens"), [])
        self.assertIsNotNone(await self.password_of(self.owner.id))

    async def test_the_function_refuses_a_lifetime_out_of_bounds(self):
        now = self.clock.now
        for expires_at in (
            now,
            now - timedelta(seconds=1),
            now + timedelta(hours=72, seconds=1),
        ):
            with self.subTest(expires_at=expires_at):
                self.assertIs(
                    await self.call_issue(self.admin.id, expires_at=expires_at), False
                )
        self.assertIsNotNone(await self.password_of(self.admin.id))
        self.assertIs(
            await self.call_issue(self.admin.id, expires_at=now + timedelta(hours=72)),
            True,
        )

    async def test_a_reset_token_of_an_account_that_became_the_owner_is_refused(
        self,
    ):
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        result = await self.reset_as(self.owner, self.admin, owner_session)
        # An ownership transfer after the token was issued.
        await self.execute(
            "UPDATE users SET system_role = 'admin' WHERE id = :u", u=self.owner.id
        )
        await self.execute(
            "UPDATE users SET system_role = 'owner' WHERE id = :u", u=self.admin.id
        )
        with self.assertRaises(TokenRejectedError):
            await self.auth.redeem_owner_token(
                result.reset_token.token, NEW_PASSWORD, context()
            )
        self.assertEqual(
            (await self.audit_summary())[
                ("user.password_reset_token.redeem", "deny", "user_not_eligible")
            ],
            1,
        )
        self.assertIsNone(await self.password_of(self.admin.id))

    async def test_an_owner_token_of_an_account_that_is_not_the_owner_is_refused(
        self,
    ):
        """The per-purpose rule does not open the Owner's tokens to other roles."""
        new = identity_tokens.generate()
        await self.execute(
            "INSERT INTO setup_tokens (id, audit_ref, user_id, purpose, salt, "
            "secret_hash, created_at, expires_at, attempts) VALUES "
            "(:i, :a, :u, 'recovery', :s, :h, :c, :e, 0)",
            i=new.token_id,
            a=new.audit_ref,
            u=self.admin.id,
            s=new.salt,
            h=new.secret_hash,
            c=self.clock.now,
            e=self.clock.now + timedelta(hours=1),
        )
        with self.assertRaises(TokenRejectedError):
            await self.auth.redeem_owner_token(new.token, NEW_PASSWORD, context())
        self.assertEqual(
            (await self.audit_summary())[
                ("owner.token.redeem", "deny", "user_not_eligible")
            ],
            1,
        )
        self.assertEqual(
            {purpose: set(roles) for purpose, roles in ELIGIBLE_ROLES.items()},
            {
                TokenPurpose.SETUP: {"owner"},
                TokenPurpose.RECOVERY: {"owner"},
                TokenPurpose.PASSWORD_RESET: {"admin", "user"},
            },
        )

    async def test_issuing_for_the_owner_is_refused_and_changes_nothing(self):
        async with self.service_database.session() as session:
            with self.assertRaises(ResetTokenRefused):
                await issue_in(
                    session, self.owner.id, now=self.clock.now, ttl_seconds=600
                )
            await session.commit()
        self.assertIsNotNone(await self.password_of(self.owner.id))
        self.assertEqual(await self.query("SELECT * FROM setup_tokens"), [])


@requires_postgres
class RaceTest(ResetCase):
    async def test_a_reset_and_a_step_up_of_the_target_do_not_wait_for_each_other(
        self,
    ):
        """The target's Passkeys are locked before its sessions, as a step-up does."""
        auth, admin_device = await self.enrolled_session(self.admin, ADMIN_PASSWORD)
        (passkey,) = await self.passkey_rows(self.admin.id)
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        async with self.database.session() as stepping:
            # A step-up of the Admin in flight: it holds the credential FOR SHARE
            # and will next update its session.
            await stepping.execute(
                text("SELECT id FROM user_passkeys WHERE id = :p FOR SHARE"),
                {"p": passkey.id},
            )
            reset = asyncio.create_task(
                self.reset_as(self.owner, self.admin, owner_session)
            )
            await asyncio.sleep(0.5)
            self.assertFalse(reset.done())  # waiting for the credential
            await stepping.execute(
                text("UPDATE auth_sessions SET rotated_at = now() WHERE id = :s"),
                {"s": auth.record.id},
            )
            await stepping.commit()
        result = await asyncio.wait_for(reset, 10)
        self.assertEqual(result.passkeys_revoked, 1)
        # The session the step-up touched ended with the rest.
        self.assertEqual(
            (await self.session_row(auth.record.id)).revoked_reason, "admin"
        )

    async def test_a_reset_and_a_real_step_up_of_the_target_leave_no_open_session(
        self,
    ):
        auth, admin_device = await self.enrolled_session(self.admin, ADMIN_PASSWORD)
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        options = await self.passkeys.authenticate_begin(auth, context())
        answer = admin_device.get(options)

        async def work(services, index):
            if index == 0:
                return await services.passkeys.reset_account(
                    principal(self.owner),
                    self.admin.id,
                    context(),
                    session_id=owner_session,
                )
            return await self.step_up_on(services, auth, answer)

        results = await self.gather_on_own_engines(2, work)
        self.assertNotIsInstance(results[0], BaseException, results)
        live = await self.query(
            "SELECT id FROM auth_sessions WHERE user_id = :u AND revoked_at IS NULL",
            u=self.admin.id,
        )
        self.assertEqual(live, [], results)
        self.assertEqual(
            [r.revoked_reason for r in await self.passkey_rows(self.admin.id)],
            ["admin_reset"],
        )

    async def step_up_on(self, services, auth, answer):
        from paw_backend.auth.passkeys.types import parse_assertion_credential

        evidence = StepUpEvidence(
            AuthMethod.PASSKEY, assertion=parse_assertion_credential(answer)
        )
        return await services.service.step_up(auth, evidence, context())

    async def test_a_reset_and_a_sign_in_of_the_target_leave_no_session(self):
        alice = await self.make_user("alice")
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)

        async def work(services, index):
            if index == 0:
                return await services.passkeys.reset_account(
                    principal(self.owner), alice.id, context(), session_id=owner_session
                )
            return await services.service.login("alice", PASSWORD, context())

        for _ in range(3):
            await self.gather_on_own_engines(2, work)
            live = await self.query(
                "SELECT id FROM auth_sessions WHERE user_id = :u "
                "AND revoked_at IS NULL",
                u=alice.id,
            )
            # Either the sign-in came first (and its session ended with the reset)
            # or the reset came first (and the old password signs nobody in).
            self.assertEqual(live, [])
            # Put the password back for the next round.
            await self.execute(
                "INSERT INTO password_credentials (user_id, hash, created_at, "
                "changed_at) VALUES (:u, :h, now(), now())",
                u=alice.id,
                h=await self.services.hasher.hash(PASSWORD),
            )

    async def test_a_reset_and_a_registration_of_the_target_leave_no_passkey(self):
        alice = await self.make_user("alice")
        auth = await self.sign_in(alice)
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        newcomer = device()
        options = await self.passkeys.register_begin(auth, context())
        answer = newcomer.create(options)

        async def work(services, index):
            if index == 0:
                return await services.passkeys.reset_account(
                    principal(self.owner), alice.id, context(), session_id=owner_session
                )
            return await services.passkeys.register_finish(
                auth, answer, "new", context()
            )

        results = await self.gather_on_own_engines(2, work)
        self.assertNotIsInstance(results[0], BaseException, results)
        active = [r for r in await self.passkey_rows(alice.id) if r.revoked_at is None]
        self.assertEqual(active, [], results)

    async def test_two_resets_of_one_account_leave_one_token(self):
        alice = await self.make_user("alice")
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)

        async def work(services, index):
            return await services.passkeys.reset_account(
                principal(self.owner), alice.id, context(), session_id=owner_session
            )

        results = await self.gather_on_own_engines(3, work)
        self.assertTrue(all(not isinstance(r, BaseException) for r in results), results)
        (outstanding,) = await self.outstanding_tokens(alice.id)
        self.assertEqual(outstanding.purpose, "password_reset")
        self.assertEqual(
            (await self.audit_summary())[("auth.passkey.reset", "allow", "reset")], 3
        )


@requires_postgres
class ArgumentsTest(ResetCase):
    async def test_every_argument_of_reset_account_is_checked(self):
        alice = await self.make_user("alice")
        owner_session = await self.actor_session(self.owner, OWNER_PASSWORD)
        good = {
            "actor": principal(self.owner),
            "target_user_id": alice.id,
            "context": context(),
            "session_id": owner_session,
        }
        for name, bad in (
            ("actor", None),
            ("actor", self.owner.id),
            ("target_user_id", str(alice.id)),
            ("target_user_id", None),
            ("context", None),
            ("context", "ctx"),
            ("session_id", str(owner_session)),
            ("session_id", None),
        ):
            with self.subTest(name=name, value=repr(bad)[:20]):
                arguments = dict(good, **{name: bad})
                with self.assertRaises(InvalidAuthInputError):
                    await self.passkeys.reset_account(
                        arguments["actor"],
                        arguments["target_user_id"],
                        arguments["context"],
                        session_id=arguments["session_id"],
                    )
        self.assertIsNotNone(await self.password_of(alice.id))

    async def test_the_reset_token_lifetime_is_bounded(self):
        for bad in (
            MIN_RESET_TOKEN_TTL_SECONDS - 1,
            MAX_RESET_TOKEN_TTL_SECONDS + 1,
            True,
            600.0,
            "600",
            None,
        ):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    validate_ttl(bad)
                with self.assertRaises(ValueError):
                    PasskeyService(
                        self.service_database,
                        self.registry,
                        self.services.sessions,
                        self.services.throttle,
                        self.passkeys._audit,
                        self.services.policy,
                        reset_token_ttl_seconds=bad,
                    )
        self.assertEqual(validate_ttl(MIN_RESET_TOKEN_TTL_SECONDS), 600)
        self.assertEqual(
            self.passkeys._reset_ttl, self.settings.password_reset_token_ttl_seconds
        )

    async def test_every_argument_of_issue_in_is_checked(self):
        now = datetime(2030, 1, 1, tzinfo=UTC)
        async with self.service_database.session() as session:
            for kwargs in (
                {"session": None},
                {"user_id": str(self.admin.id)},
                {"now": now.replace(tzinfo=None)},
                {"now": "2030-01-01"},
            ):
                with self.subTest(kwargs=repr(kwargs)[:40]):
                    arguments = {
                        "session": session,
                        "user_id": self.admin.id,
                        "now": now,
                        **kwargs,
                    }
                    with self.assertRaises(InvalidAuthInputError):
                        await issue_in(
                            arguments["session"],
                            arguments["user_id"],
                            now=arguments["now"],
                            ttl_seconds=600,
                        )
            with self.assertRaises(ValueError):
                await issue_in(session, self.admin.id, now=now, ttl_seconds=10)
            await session.rollback()

    async def test_the_reason_of_revoke_all_is_checked(self):
        async with self.service_database.session() as session:
            for bad in ("admin_reset", None, 3):
                with self.subTest(value=bad):
                    with self.assertRaises(InvalidAuthInputError):
                        await self.registry.revoke_all_in(
                            session, self.admin.id, reason=bad
                        )
            revoked = await self.registry.revoke_all_in(
                session, self.admin.id, reason=PasskeyRevokeReason.ADMIN_RESET
            )
            self.assertEqual(revoked, 0)
            await session.rollback()
