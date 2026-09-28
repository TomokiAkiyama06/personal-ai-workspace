"""Invite-only registration (PAW-024): the invitation service on PostgreSQL."""

from paw_backend.auth.errors import (
    AccountNotFoundError,
    AccountStateError,
    AuthPermissionError,
    InvalidAuthInputError,
    InvalidCredentialsError,
    InvitationNotFoundError,
    LoginNameTakenError,
    PasswordPolicyError,
    StepUpMethodInsufficientError,
    StepUpRequiredError,
    ThrottledError,
    TokenRejectedError,
)
from paw_backend.auth.state import StepUpEvidence
from paw_backend.authz.roles import SystemRole

from .auth_support import requires_postgres
from .onboarding_support import PASSWORD, OnboardingTestCase

NEW_PASSWORD = "a brand new passphrase here"


def wrong(token: str) -> str:
    last = "A" if token[-1] != "A" else "B"
    return token[:-1] + last


@requires_postgres
class InviteTest(OnboardingTestCase):
    async def test_an_admin_invites_a_user_who_sets_a_password_and_signs_in(self):
        _, admin, auth = await self.administrator()

        issued = await self.invitations.invite(
            admin, "Bob", SystemRole.USER, self.context(), session_id=auth.record.id
        )

        self.assertEqual(issued.login_name, "bob")  # normalised
        row = (
            await self.query(
                "SELECT system_role, status, passkey_required FROM users "
                "WHERE id = :id",
                id=issued.user_id,
            )
        )[0]
        self.assertEqual(tuple(row), ("user", "invited", False))
        self.assertEqual(await self.history_of(issued.user_id), [(None, "invited")])
        # An invited user cannot sign in yet (no password, not active).
        with self.assertRaises(InvalidCredentialsError):
            await self.auth.login("bob", NEW_PASSWORD, self.context())

        redeemed = await self.invitations.redeem(
            issued.token, NEW_PASSWORD, self.context()
        )

        self.assertEqual(redeemed.user_id, issued.user_id)
        self.assertEqual(await self.status_of(issued.user_id), "active")
        self.assertEqual(
            await self.history_of(issued.user_id),
            [(None, "invited"), ("invited", "active")],
        )
        # No session was made by the redemption; the normal sign-in works now.
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM auth_sessions WHERE user_id = :id",
                id=issued.user_id,
            ),
            0,
        )
        login = await self.auth.login("bob", NEW_PASSWORD, self.context())
        self.assertEqual(login.session.record.user_id, issued.user_id)
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.invitation.issue", "allow", "issued")], 1)
        self.assertEqual(summary[("auth.invitation.redeem", "allow", "redeemed")], 1)

    async def test_the_token_is_single_use(self):
        _, admin, auth = await self.administrator()
        issued = await self.invitations.invite(
            admin, "bob", SystemRole.USER, self.context(), session_id=auth.record.id
        )
        await self.invitations.redeem(issued.token, NEW_PASSWORD, self.context())

        with self.assertRaises(TokenRejectedError):
            await self.invitations.redeem(
                issued.token, "another fine passphrase", self.context()
            )
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.invitation.redeem", "deny", "token_used")], 1)

    async def test_the_token_and_the_password_are_stored_nowhere(self):
        _, admin, auth = await self.administrator()
        with self.no_secret_in_logs() as logs:
            issued = await self.invitations.invite(
                admin, "bob", SystemRole.USER, self.context(), session_id=auth.record.id
            )
            await self.invitations.redeem(issued.token, NEW_PASSWORD, self.context())
        stored = await self.everything_stored()
        secret = issued.token.rsplit(".", 1)[1]
        for value in (issued.token, secret, NEW_PASSWORD, issued.token.split(".")[1]):
            self.assertNotIn(value, stored)
            self.assertNotIn(value, logs.text)

    async def test_only_the_owner_invites_an_admin(self):
        _, admin, auth = await self.administrator()
        with self.assertRaises(AuthPermissionError):
            await self.invitations.invite(
                admin,
                "carol",
                SystemRole.ADMIN,
                self.context(),
                session_id=auth.record.id,
            )
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM users WHERE login_name = 'carol'"),
            0,
        )
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.invitation.issue", "deny", "role_not_allowed")], 1
        )

        _, owner, owner_auth = await self.administrator("owner")
        issued = await self.invitations.invite(
            owner,
            "carol",
            SystemRole.ADMIN,
            self.context(),
            session_id=owner_auth.record.id,
        )
        row = (
            await self.query(
                "SELECT system_role, passkey_required FROM users WHERE id = :id",
                id=issued.user_id,
            )
        )[0]
        self.assertEqual(tuple(row), ("admin", True))

    async def test_a_user_invites_nobody_and_nobody_invites_an_owner(self):
        user = await self.make_user("plain")
        auth = await self.sign_in(user)
        with self.assertRaises(AuthPermissionError):
            await self.invitations.invite(
                self.principal(user),
                "bob",
                SystemRole.USER,
                self.context(),
                session_id=auth.record.id,
            )
        _, owner, owner_auth = await self.administrator("owner")
        for role in (SystemRole.OWNER, SystemRole.SYSTEM, "user"):
            with self.subTest(role=role), self.assertRaises(InvalidAuthInputError):
                await self.invitations.invite(
                    owner, "bob", role, self.context(), session_id=owner_auth.record.id
                )

    async def test_inviting_needs_a_recent_passkey_step_up(self):
        admin_user = await self.make_user("admin-one", role="admin")
        admin = self.principal(admin_user)
        auth = await self.sign_in(admin_user, step_up=False)
        with self.assertRaises(StepUpRequiredError):
            await self.invitations.invite(
                admin, "bob", SystemRole.USER, self.context(), session_id=auth.record.id
            )
        # A password step-up is not enough.
        stepped = await self.auth.step_up(
            auth, StepUpEvidence("password", PASSWORD), self.context()
        )
        with self.assertRaises(StepUpMethodInsufficientError):
            await self.invitations.invite(
                admin,
                "bob",
                SystemRole.USER,
                self.context(),
                session_id=stepped.session.record.id,
            )
        # An expired Passkey step-up is not enough either.
        await self.fake_passkey_step_up(stepped.session.record.id)
        await self.advance(minutes=31)
        with self.assertRaises(StepUpRequiredError):
            await self.invitations.invite(
                admin,
                "bob",
                SystemRole.USER,
                self.context(),
                session_id=stepped.session.record.id,
            )
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM users WHERE login_name = 'bob'"), 0
        )
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.invitation.issue", "deny", "step_up_required")], 2
        )
        self.assertEqual(
            summary[("auth.invitation.issue", "deny", "step_up_method_insufficient")],
            1,
        )

    async def test_a_taken_login_name_is_refused(self):
        _, admin, auth = await self.administrator()
        await self.make_user("bob")
        with self.assertRaises(LoginNameTakenError):
            await self.invitations.invite(
                admin, "BOB", SystemRole.USER, self.context(), session_id=auth.record.id
            )
        with self.assertRaises(InvalidAuthInputError):
            await self.invitations.invite(
                admin,
                "no spaces",
                SystemRole.USER,
                self.context(),
                session_id=auth.record.id,
            )
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.invitation.issue", "deny", "login_name_taken")], 1
        )


@requires_postgres
class RedeemTest(OnboardingTestCase):
    async def invite(self, name: str = "bob"):
        _, admin, auth = await self.administrator()
        self.admin, self.admin_auth = admin, auth
        return await self.invitations.invite(
            admin, name, SystemRole.USER, self.context(), session_id=auth.record.id
        )

    async def test_an_expired_token_is_refused(self):
        issued = await self.invite()
        await self.advance(hours=72)
        with self.assertRaises(TokenRejectedError):
            await self.invitations.redeem(issued.token, NEW_PASSWORD, self.context())
        self.assertEqual(await self.status_of(issued.user_id), "invited")
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.invitation.redeem", "deny", "token_expired")], 1
        )

    async def test_the_attempts_are_limited_and_a_locked_token_stays_locked(self):
        issued = await self.invite()
        for index in range(5):
            with self.subTest(attempt=index), self.assertRaises(TokenRejectedError):
                await self.invitations.redeem(
                    wrong(issued.token), NEW_PASSWORD, self.context(f"10.0.0.{index}")
                )
        with self.assertRaises(TokenRejectedError):
            await self.invitations.redeem(
                issued.token, NEW_PASSWORD, self.context("10.0.1.1")
            )
        self.assertEqual(await self.status_of(issued.user_id), "invited")
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.invitation.redeem", "deny", "token_mismatch")], 4
        )
        self.assertEqual(
            summary[("auth.invitation.redeem", "deny", "attempts_exhausted")], 1
        )
        # The try after the lock is logged only: five rows, not six.
        redeems = sum(
            count
            for (action, _, _), count in summary.items()
            if action == "auth.invitation.redeem"
        )
        self.assertEqual(redeems, 5)

    async def test_a_bad_password_does_not_spend_the_token(self):
        issued = await self.invite("longname")
        with self.assertRaises(PasswordPolicyError):
            await self.invitations.redeem(issued.token, "short", self.context())
        # Only known once the token is read: the name inside the password.
        with self.assertRaises(PasswordPolicyError):
            await self.invitations.redeem(
                issued.token, "my name is longname ok", self.context()
            )
        self.assertEqual(
            await self.scalar(
                "SELECT attempts FROM user_invitations WHERE user_id = :id",
                id=issued.user_id,
            ),
            0,
        )
        await self.invitations.redeem(issued.token, NEW_PASSWORD, self.context())
        self.assertEqual(await self.status_of(issued.user_id), "active")

    async def test_an_unknown_or_malformed_token_leaves_no_row(self):
        issued = await self.invite()
        before = await self.audit_summary()
        other = "pawiv1." + "0" * 32 + "." + "A" * 43
        for token in (other, "garbage", "pawpr1." + issued.token[7:]):
            with self.subTest(token=token[:10]), self.assertRaises(TokenRejectedError):
                await self.invitations.redeem(token, NEW_PASSWORD, self.context())
        self.assertEqual(await self.audit_summary(), before)

    async def test_reissuing_ends_the_old_token(self):
        issued = await self.invite()
        again = await self.invitations.reissue(
            self.admin,
            issued.user_id,
            self.context(),
            session_id=self.admin_auth.record.id,
        )
        self.assertNotEqual(again.token, issued.token)
        with self.assertRaises(TokenRejectedError):
            await self.invitations.redeem(issued.token, NEW_PASSWORD, self.context())
        await self.invitations.redeem(again.token, NEW_PASSWORD, self.context())
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.invitation.issue", "allow", "reissued")], 1)
        self.assertEqual(summary[("auth.invitation.revoke", "allow", "superseded")], 1)
        self.assertEqual(
            summary[("auth.invitation.redeem", "deny", "token_revoked")], 1
        )
        # Once active, there is nothing to reissue.
        with self.assertRaises(AccountStateError):
            await self.invitations.reissue(
                self.admin,
                issued.user_id,
                self.context(),
                session_id=self.admin_auth.record.id,
            )

    async def test_revoking_keeps_the_user_invited(self):
        issued = await self.invite()
        await self.invitations.revoke(
            self.admin,
            issued.user_id,
            self.context(),
            session_id=self.admin_auth.record.id,
        )
        with self.assertRaises(TokenRejectedError):
            await self.invitations.redeem(issued.token, NEW_PASSWORD, self.context())
        self.assertEqual(await self.status_of(issued.user_id), "invited")
        with self.assertRaises(InvitationNotFoundError):
            await self.invitations.revoke(
                self.admin,
                issued.user_id,
                self.context(),
                session_id=self.admin_auth.record.id,
            )

    async def test_an_admin_cannot_reissue_the_invitation_of_an_admin(self):
        _, owner, owner_auth = await self.administrator("owner")
        issued = await self.invitations.invite(
            owner,
            "carol",
            SystemRole.ADMIN,
            self.context(),
            session_id=owner_auth.record.id,
        )
        _, admin, auth = await self.administrator()
        with self.assertRaises(AuthPermissionError):
            await self.invitations.reissue(
                admin, issued.user_id, self.context(), session_id=auth.record.id
            )
        # The invited Admin's token still works.
        await self.invitations.redeem(issued.token, NEW_PASSWORD, self.context())

    async def test_two_redemptions_at_once_one_wins(self):
        issued = await self.invite()

        async def attempt(services, index):
            return await services.invitations.redeem(
                issued.token, f"{NEW_PASSWORD} {index}", self.context(f"10.1.0.{index}")
            )

        results = await self.gather_on_own_engines(4, attempt)
        won = [r for r in results if not isinstance(r, BaseException)]
        self.assertEqual(len(won), 1, results)
        self.assertTrue(
            all(isinstance(r, TokenRejectedError) for r in results if r not in won)
        )
        self.assertEqual(
            await self.history_of(issued.user_id),
            [(None, "invited"), ("invited", "active")],
        )

    async def test_the_redeem_scopes_limit_the_source(self):
        issued = await self.invite()
        for _ in range(4):
            with self.assertRaises(TokenRejectedError):
                await self.invitations.redeem("garbage", NEW_PASSWORD, self.context())
        # The fifth attempt starts the lock (it still runs), the next is refused.
        with self.assertRaises(TokenRejectedError):
            await self.invitations.redeem("garbage", NEW_PASSWORD, self.context())
        with self.assertRaises(ThrottledError):
            await self.invitations.redeem(issued.token, NEW_PASSWORD, self.context())
        self.assertEqual(await self.status_of(issued.user_id), "invited")


@requires_postgres
class NoStrayTest(OnboardingTestCase):
    async def test_the_owner_is_never_a_target(self):
        owner_user = await self.make_user("boss", role="owner")
        _, admin, auth = await self.administrator()
        with self.assertRaises(AccountNotFoundError):
            await self.invitations.reissue(
                admin, owner_user.id, self.context(), session_id=auth.record.id
            )
        with self.assertRaises(AccountNotFoundError):
            await self.invitations.revoke(
                admin, owner_user.id, self.context(), session_id=auth.record.id
            )
