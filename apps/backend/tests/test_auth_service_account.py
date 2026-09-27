"""Logout, devices, password change and Step-up (PAW-022, PostgreSQL)."""

import uuid
from datetime import timedelta

from paw_backend.auth import tokens
from paw_backend.auth.context import RequestContext
from paw_backend.auth.errors import (
    InvalidAuthInputError,
    InvalidCredentialsError,
    PasswordPolicyError,
    PasswordProblem,
    SessionEndedError,
    SessionNotFoundError,
    ThrottledError,
)
from paw_backend.auth.models import AuthMethod, RevokeReason
from paw_backend.auth.state import StepUpEvidence

from .auth_support import PASSWORD, T0, PostgresAuthTestCase, requires_postgres

SOURCE = "203.0.113.7"
NEW_PASSWORD = "an entirely different passphrase"


def context(source: str = SOURCE) -> RequestContext:
    return RequestContext(uuid.uuid4(), tokens.source_bucket(source))


class AccountTestCase(PostgresAuthTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.alice = await self.make_user("alice")

    async def login(self, name="alice", password=PASSWORD, **options):
        return await self.auth.login(name, password, context(), **options)

    async def authenticated(self, token):
        async with self.service_database.session() as session:
            found = await self.services.sessions.authenticate(session, token)
            await session.commit()
            return found

    async def session_rows(self):
        return {row.id: row for row in await self.query("SELECT * FROM auth_sessions")}

    async def stored_hash(self, user_id=None):
        return await self.scalar(
            "SELECT hash FROM password_credentials WHERE user_id = :id",
            id=user_id or self.alice.id,
        )


@requires_postgres
class LogoutAndDevicesTest(AccountTestCase):
    async def test_logout_ends_the_session_and_says_so_in_the_audit_trail(self):
        result = await self.login()
        await self.auth.logout(result.session, context())
        self.assertIsNone(await self.authenticated(result.token))
        row = (await self.session_rows())[result.session.record.id]
        self.assertEqual((row.revoked_at, row.revoked_reason), (T0, "logout"))
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.logout", "allow", "logged_out")], 1)
        logout = [r for r in await self.audit_rows() if r.action == "auth.logout"][0]
        self.assertEqual(
            (logout.actor_id, logout.resource_kind, logout.resource_id),
            (self.alice.id, "session", result.session.record.id),
        )

    async def test_a_second_logout_of_the_same_session_writes_nothing_more(self):
        result = await self.login()
        await self.auth.logout(result.session, context())
        await self.auth.logout(result.session, context())
        self.assertEqual(
            (await self.audit_summary())[("auth.logout", "allow", "logged_out")], 1
        )

    async def test_logout_ends_only_this_device(self):
        phone = await self.login(device_label="phone")
        laptop = await self.login(device_label="laptop")
        await self.auth.logout(phone.session, context())
        self.assertIsNone(await self.authenticated(phone.token))
        self.assertIsNotNone(await self.authenticated(laptop.token))

    async def test_the_device_list_shows_the_users_own_valid_sessions(self):
        bob = await self.make_user("bobby", password="another good passphrase")
        await self.auth.login("bobby", "another good passphrase", context())
        phone = await self.login(device_label="phone")
        self.clock.advance(minutes=5)
        laptop = await self.login(device_label="laptop")
        ended = await self.login(device_label="old")
        await self.auth.logout(ended.session, context())
        listed = await self.auth.list_sessions(laptop.session)
        self.assertEqual([r.device_label for r in listed], ["laptop", "phone"])
        self.assertEqual({r.user_id for r in listed}, {self.alice.id})
        self.assertEqual(listed[1].id, phone.session.record.id)
        self.assertNotEqual(bob.id, self.alice.id)

    async def test_a_device_can_be_signed_out_from_another_one(self):
        phone = await self.login(device_label="phone")
        laptop = await self.login(device_label="laptop")
        await self.auth.revoke_session(
            laptop.session, phone.session.record.id, context()
        )
        self.assertIsNone(await self.authenticated(phone.token))
        self.assertIsNotNone(await self.authenticated(laptop.token))
        row = (await self.session_rows())[phone.session.record.id]
        self.assertEqual(row.revoked_reason, "revoked_by_user")
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.session.revoke", "allow", "revoked")], 1)

    async def test_the_current_session_can_be_revoked_by_its_id_too(self):
        result = await self.login()
        await self.auth.revoke_session(
            result.session, result.session.record.id, context()
        )
        self.assertIsNone(await self.authenticated(result.token))

    async def test_a_session_of_another_user_or_an_unknown_id_is_not_found(self):
        await self.make_user("bobby", password="another good passphrase")
        theirs = await self.auth.login("bobby", "another good passphrase", context())
        mine = await self.login()
        for session_id in (theirs.session.record.id, uuid.uuid4()):
            with self.subTest(session_id=session_id):
                with self.assertRaises(SessionNotFoundError):
                    await self.auth.revoke_session(mine.session, session_id, context())
        self.assertIsNotNone(await self.authenticated(theirs.token))
        self.assertNotIn(
            ("auth.session.revoke", "allow", "revoked"), await self.audit_summary()
        )

    async def test_a_session_that_already_ended_is_not_found(self):
        old = await self.login()
        mine = await self.login()
        await self.auth.logout(old.session, context())
        with self.assertRaises(SessionNotFoundError):
            await self.auth.revoke_session(
                mine.session, old.session.record.id, context()
            )

    async def test_sign_every_other_device_out_keeps_the_current_one(self):
        keep = await self.login()
        others = [await self.login() for _ in range(3)]
        count = await self.auth.revoke_other_sessions(keep.session, context())
        self.assertEqual(count, 3)
        self.assertIsNotNone(await self.authenticated(keep.token))
        for other in others:
            self.assertIsNone(await self.authenticated(other.token))
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.session.revoke_others", "allow", "revoked_others")
            ],
            1,
        )

    async def test_the_lifecycle_can_end_every_session_of_a_user(self):
        results = [await self.login() for _ in range(2)]
        count = await self.auth.revoke_all_sessions_of(
            self.alice.id, RevokeReason.ACCOUNT_CLOSED
        )
        self.assertEqual(count, 2)
        for result in results:
            self.assertIsNone(await self.authenticated(result.token))
        rows = await self.session_rows()
        self.assertEqual({r.revoked_reason for r in rows.values()}, {"account_closed"})


@requires_postgres
class ChangePasswordTest(AccountTestCase):
    async def change(self, result, current=PASSWORD, new=NEW_PASSWORD, **options):
        return await self.auth.change_password(
            result.session, current, new, context(), **options
        )

    async def test_the_new_password_works_and_the_old_one_does_not(self):
        result = await self.login()
        before = await self.stored_hash()
        await self.change(result)
        self.assertNotEqual(await self.stored_hash(), before)
        with self.assertRaises(InvalidCredentialsError):
            await self.login(password=PASSWORD)
        self.assertEqual(
            (await self.login(password=NEW_PASSWORD)).session.login_name, "alice"
        )

    async def test_the_stored_value_is_argon2id_and_holds_no_plaintext(self):
        result = await self.login()
        await self.change(result)
        stored = await self.stored_hash()
        self.assertTrue(stored.startswith("$argon2id$"))
        self.assertNotIn(NEW_PASSWORD, await self.everything_stored())
        self.assertNotIn(PASSWORD, await self.everything_stored())

    async def test_the_session_id_is_rotated_and_the_old_one_stops_working(self):
        result = await self.login()
        changed = await self.change(result)
        self.assertNotEqual(changed.token, result.token)
        self.assertIsNone(await self.authenticated(result.token))
        found = await self.authenticated(changed.token)
        self.assertEqual(found.record.id, result.session.record.id)  # the same device

    async def test_by_default_the_other_sessions_are_kept(self):
        current = await self.login()
        other = await self.login()
        await self.change(current)
        self.assertIsNotNone(await self.authenticated(other.token))
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.password.change", "allow", "changed")], 1)
        self.assertNotIn(
            ("auth.session.revoke_others", "allow", "revoked_others"), summary
        )

    async def test_on_request_every_other_session_is_ended(self):
        current = await self.login()
        others = [await self.login() for _ in range(2)]
        changed = await self.change(current, revoke_other_sessions=True)
        for other in others:
            self.assertIsNone(await self.authenticated(other.token))
        self.assertIsNotNone(await self.authenticated(changed.token))
        rows = await self.session_rows()
        self.assertEqual(
            sorted(r.revoked_reason for r in rows.values() if r.revoked_at is not None),
            ["password_changed", "password_changed"],
        )
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.session.revoke_others", "allow", "revoked_others")
            ],
            1,
        )

    async def test_a_wrong_current_password_changes_nothing_and_is_audited(self):
        result = await self.login()
        before = await self.stored_hash()
        with self.assertRaises(InvalidCredentialsError):
            await self.change(result, current="not my password")
        self.assertEqual(await self.stored_hash(), before)
        self.assertIsNotNone(await self.authenticated(result.token))
        row = [
            r for r in await self.audit_rows() if r.action == "auth.password.change"
        ][0]
        self.assertEqual(
            (row.decision, row.reason, row.actor_id),
            ("deny", "invalid_credentials", self.alice.id),
        )

    async def test_wrong_current_passwords_count_towards_the_accounts_backoff(self):
        result = await self.login()
        for _ in range(5):
            with self.assertRaises(InvalidCredentialsError):
                await self.change(result, current="not my password")
        with self.assertRaises(ThrottledError):
            await self.change(result)  # the right one, while locked
        # The lock is the account's: a login is refused too.
        with self.assertRaises(ThrottledError):
            await self.login()
        self.assertEqual(
            (await self.audit_summary())[("auth.lockout", "deny", "backoff_started")], 1
        )

    async def test_a_new_password_that_breaks_the_policy_is_refused_before_counting(
        self,
    ):
        result = await self.login()
        cases = {
            "short": PasswordProblem.TOO_SHORT,
            "password123": PasswordProblem.TOO_COMMON,
            PASSWORD: PasswordProblem.SAME_AS_CURRENT,
            "xx" + "alice" + "ab": PasswordProblem.TOO_SHORT,
            "x" * 300: PasswordProblem.TOO_LONG,
        }
        for new, problem in cases.items():
            with self.subTest(new=new[:12]):
                with self.assertRaises(PasswordPolicyError) as caught:
                    await self.change(result, new=new)
                self.assertEqual(caught.exception.problem, problem)
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM auth_throttles WHERE scope = 'login_account'"
            ),
            0,
        )

    async def test_a_new_password_that_contains_the_login_name_is_refused(self):
        await self.make_user("longername", password="some other passphrase")
        other = await self.auth.login("longername", "some other passphrase", context())
        with self.assertRaises(PasswordPolicyError) as caught:
            await self.auth.change_password(
                other.session,
                "some other passphrase",
                "my LongerName secret",
                context(),
            )
        self.assertEqual(caught.exception.problem, PasswordProblem.CONTAINS_LOGIN_NAME)

    async def test_a_session_that_ended_meanwhile_cannot_change_the_password(self):
        result = await self.login()
        await self.auth.logout(result.session, context())
        before = await self.stored_hash()
        with self.assertRaises(SessionEndedError):
            await self.change(result)
        self.assertEqual(await self.stored_hash(), before)
        # ... and the attempt did not leave the counter or the audit trail changed.
        self.assertNotIn(
            ("auth.password.change", "allow", "changed"), await self.audit_summary()
        )

    async def test_of_two_changes_with_the_same_current_password_exactly_one_wins(self):
        result = await self.login()

        async def change(services, index):
            return await services.service.change_password(
                result.session, PASSWORD, f"new passphrase number {index}", context()
            )

        results = await self.gather_on_own_engines(4, change)
        winners = [r for r in results if not isinstance(r, Exception)]
        losers = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(winners), 1, results)
        self.assertTrue(
            all(
                isinstance(e, InvalidCredentialsError | SessionEndedError)
                for e in losers
            ),
            results,
        )
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL"
            ),
            1,
        )

    async def test_a_login_racing_a_password_change_never_ends_up_with_the_old_password(
        self,
    ):
        # The change takes the user row exclusively: a login that verified the old
        # password and creates its session afterwards is refused (see the login tests).
        result = await self.login()
        await self.change(result)
        with self.assertRaises(InvalidCredentialsError):
            await self.login(password=PASSWORD)

    async def test_no_password_reaches_a_log_line(self):
        result = await self.login()
        with self.no_secret_in_logs(
            PASSWORD, NEW_PASSWORD, application_only=("$argon2id$",)
        ):
            await self.change(result)
            with self.assertRaises(InvalidCredentialsError):
                await self.change(
                    await self.login(password=NEW_PASSWORD), current="the wrong one"
                )


@requires_postgres
class StepUpTest(AccountTestCase):
    async def test_the_password_proves_it_again_and_the_session_is_rotated(self):
        result = await self.login()
        self.clock.advance(minutes=3)
        stepped = await self.auth.step_up(
            result.session, StepUpEvidence(AuthMethod.PASSWORD, PASSWORD), context()
        )
        self.assertNotEqual(stepped.token, result.token)
        self.assertIsNone(await self.authenticated(result.token))
        found = await self.authenticated(stepped.token)
        self.assertEqual(found.record.stepup_at, T0 + timedelta(minutes=3))
        self.assertEqual(found.record.stepup_method, AuthMethod.PASSWORD)
        view = await self.auth.view(found)
        self.assertTrue(view.auth.step_up.satisfied)
        self.assertEqual(view.auth.step_up.valid_until, T0 + timedelta(minutes=33))
        self.assertEqual(
            (await self.audit_summary())[("auth.step_up", "allow", "verified")], 1
        )

    async def test_a_step_up_is_only_valid_within_the_window(self):
        result = await self.login()
        stepped = await self.auth.step_up(
            result.session, StepUpEvidence(AuthMethod.PASSWORD, PASSWORD), context()
        )
        self.clock.advance(minutes=29, seconds=59)
        self.assertTrue(
            (
                await self.auth.view(await self.authenticated(stepped.token))
            ).auth.step_up.satisfied
        )
        self.clock.advance(seconds=1)
        self.assertFalse(
            (
                await self.auth.view(await self.authenticated(stepped.token))
            ).auth.step_up.satisfied
        )

    async def test_a_session_that_never_stepped_up_says_so(self):
        result = await self.login()
        view = await self.auth.view(result.session)
        step = view.auth.step_up
        self.assertEqual(
            (step.method, step.verified_at, step.valid_until, step.satisfied),
            (None, None, None, False),
        )
        self.assertEqual(step.window_minutes, 30)

    async def test_a_wrong_password_is_refused_counted_and_audited(self):
        result = await self.login()
        with self.assertRaises(InvalidCredentialsError):
            await self.auth.step_up(
                result.session,
                StepUpEvidence(AuthMethod.PASSWORD, "wrong wrong wrong"),
                context(),
            )
        self.assertIsNone((await self.authenticated(result.token)).record.stepup_at)
        row = [r for r in await self.audit_rows() if r.action == "auth.step_up"][0]
        self.assertEqual((row.decision, row.reason), ("deny", "invalid_credentials"))
        counter = await self.scalar(
            "SELECT attempts FROM auth_throttles WHERE scope = 'login_account'"
        )
        self.assertEqual(counter, 1)

    async def test_wrong_proofs_lock_the_account_like_wrong_passwords_do(self):
        result = await self.login()
        for _ in range(5):
            with self.assertRaises(InvalidCredentialsError):
                await self.auth.step_up(
                    result.session,
                    StepUpEvidence(AuthMethod.PASSWORD, "wrong wrong wrong"),
                    context(),
                )
        with self.assertRaises(ThrottledError):
            await self.auth.step_up(
                result.session, StepUpEvidence(AuthMethod.PASSWORD, PASSWORD), context()
            )

    async def test_a_proof_without_a_password_is_wrong(self):
        result = await self.login()
        with self.assertRaises(InvalidCredentialsError):
            await self.auth.step_up(
                result.session, StepUpEvidence(AuthMethod.PASSWORD, None), context()
            )

    async def test_a_method_nobody_registered_is_refused_before_the_database(self):
        result = await self.login()
        before = await self.scalar("SELECT sum(attempts) FROM auth_throttles")
        with self.assertRaises(InvalidAuthInputError):
            await self.auth.step_up(
                result.session, StepUpEvidence(AuthMethod.PASSKEY, None), context()
            )
        self.assertEqual(
            await self.scalar("SELECT sum(attempts) FROM auth_throttles"), before
        )

    async def test_a_passkey_verifier_can_be_registered_without_changing_anything_else(
        self,
    ):
        seen = []

        class PasskeyVerifier:
            method = AuthMethod.PASSKEY

            async def verify(self, user_id, login_name, evidence):
                seen.append((user_id, login_name))
                return True

        from paw_backend.auth.service import AuthService

        service = self.services.service
        extended = AuthService(
            self.service_database,
            hasher=service._hasher,
            sessions=service._sessions,
            throttle=service._throttle,
            audit=service._audit,
            policy=service._policy,
            step_up_verifiers={AuthMethod.PASSKEY: PasskeyVerifier()},
        )
        result = await self.login()
        stepped = await extended.step_up(
            result.session, StepUpEvidence(AuthMethod.PASSKEY, None), context()
        )
        self.assertEqual(seen, [(self.alice.id, "alice")])
        found = await self.authenticated(stepped.token)
        self.assertEqual(found.record.stepup_method, AuthMethod.PASSKEY)

    async def test_a_session_that_ended_meanwhile_cannot_step_up(self):
        result = await self.login()
        await self.auth.logout(result.session, context())
        with self.assertRaises(SessionEndedError):
            await self.auth.step_up(
                result.session, StepUpEvidence(AuthMethod.PASSWORD, PASSWORD), context()
            )

    async def test_a_success_clears_the_counter_of_the_wrong_proofs(self):
        result = await self.login()
        with self.assertRaises(InvalidCredentialsError):
            await self.auth.step_up(
                result.session,
                StepUpEvidence(AuthMethod.PASSWORD, "wrong wrong wrong"),
                context(),
            )
        await self.auth.step_up(
            result.session, StepUpEvidence(AuthMethod.PASSWORD, PASSWORD), context()
        )
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM auth_throttles WHERE scope = 'login_account'"
            ),
            0,
        )

    async def test_two_step_ups_at_once_leave_exactly_one_valid_session_id(self):
        result = await self.login()

        async def step(services, index):
            return await services.service.step_up(
                result.session, StepUpEvidence(AuthMethod.PASSWORD, PASSWORD), context()
            )

        results = await self.gather_on_own_engines(4, step)
        winners = [r for r in results if not isinstance(r, Exception)]
        self.assertEqual(len(winners), 1, results)
        self.assertTrue(
            all(
                isinstance(e, SessionEndedError)
                for e in results
                if isinstance(e, Exception)
            ),
            results,
        )
        self.assertIsNotNone(await self.authenticated(winners[0].token))


if __name__ == "__main__":
    import unittest

    unittest.main()
