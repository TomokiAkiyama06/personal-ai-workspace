"""QR / link pairing of a new device (PAW-024): the pairing service on PostgreSQL."""

import uuid

from paw_backend.auth.errors import (
    PairingNotFoundError,
    PasskeyRequiredError,
    StepUpMethodInsufficientError,
    StepUpRequiredError,
    ThrottledError,
    TokenRejectedError,
)
from paw_backend.auth.models import AuthMethod, PasskeyGate
from paw_backend.auth.state import StepUpEvidence

from .auth_support import requires_postgres
from .onboarding_support import PASSWORD, OnboardingTestCase
from .passkey_pg_support import PASSKEY_SETTINGS


def wrong(token: str) -> str:
    return token[:-1] + ("A" if token[-1] != "A" else "B")


@requires_postgres
class UserPairingTest(OnboardingTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bob = await self.make_user("bob")
        self.trusted = await self.sign_in(self.bob, step_up=False)

    async def claim(self, token, name="Phone", source="198.51.100.9", **options):
        return await self.pairing.claim(token, name, self.context(source), **options)

    async def test_a_user_adds_a_device_with_the_token_alone(self):
        issued = await self.pairing.issue(self.trusted, self.context())
        self.assertFalse(issued.approval_required)
        self.assertEqual(issued.link_path, f"/pair#{issued.token}")
        self.assertEqual((issued.expires_at - self.clock.now).total_seconds(), 600)

        outcome = await self.claim(issued.token, remember_me=True)

        self.assertTrue(outcome.completed)
        record = outcome.login.session.record
        self.assertEqual(record.user_id, self.bob.id)
        self.assertIs(record.auth_method, AuthMethod.PAIRING)
        self.assertEqual(record.device_label, "Phone")
        self.assertTrue(record.remember_me)
        self.assertIs(record.passkey_gate, PasskeyGate.OPEN)
        self.assertIsNone(record.stepup_at)  # a pairing steps nothing up
        # The new session is a real one.
        async with self.database.session() as session:
            found = await self.auth.sessions.authenticate(session, outcome.login.token)
        self.assertEqual(found.record.id, record.id)
        row = (
            await self.query(
                "SELECT state, created_session FROM device_pairings "
                "WHERE audit_ref = :r",
                r=issued.pairing_id,
            )
        )[0]
        self.assertEqual(tuple(row), ("completed", record.id))
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.pairing.issue", "allow", "issued")], 1)
        self.assertEqual(summary[("auth.pairing.complete", "allow", "completed")], 1)

    async def test_the_same_qr_code_cannot_be_used_twice(self):
        issued = await self.pairing.issue(self.trusted, self.context())
        await self.claim(issued.token)
        with self.assertRaises(TokenRejectedError):
            await self.claim(issued.token, "Laptop")
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.pairing.claim", "deny", "token_used")], 1)

    async def test_the_token_expires_after_ten_minutes(self):
        issued = await self.pairing.issue(self.trusted, self.context())
        await self.advance(minutes=10)
        with self.assertRaises(TokenRejectedError):
            await self.claim(issued.token)
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.pairing.claim", "deny", "token_expired")], 1)

    async def test_one_live_token_at_a_time_and_revoking_ends_it(self):
        first = await self.pairing.issue(self.trusted, self.context())
        second = await self.pairing.issue(self.trusted, self.context())
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM device_pairings WHERE user_id = :id "
                "AND state IN ('issued', 'claimed', 'approved')",
                id=self.bob.id,
            ),
            1,
        )
        with self.assertRaises(TokenRejectedError):
            await self.claim(first.token)
        # Any trusted device of the user may revoke the unused token.
        other = await self.sign_in(self.bob, step_up=False)
        self.assertEqual(await self.pairing.revoke(other, self.context()), 1)
        with self.assertRaises(TokenRejectedError):
            await self.claim(second.token)
        self.assertEqual(await self.pairing.revoke(other, self.context()), 0)
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.pairing.revoke", "allow", "superseded")], 1)
        self.assertEqual(summary[("auth.pairing.revoke", "allow", "revoked")], 1)
        self.assertEqual(summary[("auth.pairing.claim", "deny", "token_revoked")], 2)

    async def test_the_attempts_of_a_token_are_limited(self):
        issued = await self.pairing.issue(self.trusted, self.context())
        for index in range(5):
            with self.assertRaises(TokenRejectedError):
                await self.claim(wrong(issued.token), source=f"10.2.0.{index}")
        with self.assertRaises(TokenRejectedError):
            await self.claim(issued.token, source="10.2.1.1")
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.pairing.claim", "deny", "token_mismatch")], 4)
        self.assertEqual(
            summary[("auth.pairing.claim", "deny", "attempts_exhausted")], 1
        )

    async def test_a_source_that_guesses_is_throttled(self):
        for _ in range(5):
            with self.assertRaises(TokenRejectedError):
                await self.claim("garbage")
        issued = await self.pairing.issue(self.trusted, self.context())
        with self.assertRaises(ThrottledError):
            await self.claim(issued.token)
        # Another source is not affected.
        await self.claim(issued.token, source="10.3.0.1")

    async def test_a_restricted_session_is_not_a_trusted_device(self):
        await self.execute(
            "UPDATE auth_sessions SET passkey_gate = 'enrollment_required' "
            "WHERE id = :id",
            id=self.trusted.record.id,
        )
        with self.assertRaises(PasskeyRequiredError):
            await self.pairing.issue(self.trusted, self.context())

    async def test_the_tokens_are_stored_nowhere(self):
        with self.no_secret_in_logs() as logs:
            issued = await self.pairing.issue(self.trusted, self.context())
            outcome = await self.claim(issued.token)
        stored = await self.everything_stored()
        for value in (
            issued.token,
            issued.token.rsplit(".", 1)[1],
            issued.token.split(".")[1],
            outcome.login.token,
        ):
            self.assertNotIn(value, stored)
            self.assertNotIn(value, logs.text)

    async def test_two_devices_racing_for_one_token_one_wins(self):
        issued = await self.pairing.issue(self.trusted, self.context())

        async def attempt(services, index):
            return await services.pairing.claim(
                issued.token, f"Device {index}", self.context(f"10.4.0.{index}")
            )

        results = await self.gather_on_own_engines(4, attempt)
        won = [r for r in results if not isinstance(r, BaseException)]
        self.assertEqual(len(won), 1, results)
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM auth_sessions WHERE auth_method = 'pairing'"
            ),
            1,
        )


@requires_postgres
class ApprovalTest(OnboardingTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.carol = await self.make_user("carol", role="admin")
        self.trusted = await self.sign_in(self.carol)  # with a Passkey step-up

    async def claimed(self):
        issued = await self.pairing.issue(self.trusted, self.context())
        self.assertTrue(issued.approval_required)
        outcome = await self.pairing.claim(issued.token, "Tablet", self.context())
        self.assertFalse(outcome.completed)
        self.assertIsNotNone(outcome.claim)
        return issued, outcome

    async def test_an_admin_device_needs_the_trusted_devices_approval(self):
        issued, outcome = await self.claimed()

        waiting = await self.pairing.complete(outcome.claim, self.context())
        self.assertFalse(waiting.completed)
        self.assertEqual(waiting.expires_at, outcome.expires_at)
        pending = await self.pairing.pending(self.trusted)
        self.assertEqual([p.pairing_id for p in pending], [issued.pairing_id])
        self.assertEqual(pending[0].device_label, "Tablet")

        await self.pairing.approve(self.trusted, issued.pairing_id, self.context())
        done = await self.pairing.complete(outcome.claim, self.context())

        self.assertTrue(done.completed)
        record = done.login.session.record
        self.assertIs(record.auth_method, AuthMethod.PAIRING)
        self.assertEqual(record.device_label, "Tablet")
        self.assertIs(record.passkey_gate, PasskeyGate.OPEN)
        self.assertEqual(await self.pairing.pending(self.trusted), ())
        with self.assertRaises(TokenRejectedError):
            await self.pairing.complete(outcome.claim, self.context())
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.pairing.claim", "allow", "pending_approval")], 1
        )
        self.assertEqual(summary[("auth.pairing.approve", "allow", "approved")], 1)
        self.assertEqual(summary[("auth.pairing.complete", "allow", "completed")], 1)
        self.assertEqual(summary[("auth.pairing.complete", "deny", "token_used")], 1)

    async def test_approving_needs_a_recent_passkey_step_up(self):
        issued, outcome = await self.claimed()
        other = await self.sign_in(self.carol, step_up=False)
        with self.assertRaises(StepUpRequiredError):
            await self.pairing.approve(other, issued.pairing_id, self.context())
        stepped = await self.auth.step_up(
            other, StepUpEvidence("password", PASSWORD), self.context()
        )
        with self.assertRaises(StepUpMethodInsufficientError):
            await self.pairing.approve(
                stepped.session, issued.pairing_id, self.context()
            )
        waiting = await self.pairing.complete(outcome.claim, self.context())
        self.assertFalse(waiting.completed)
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.pairing.approve", "deny", "step_up_required")], 1
        )

    async def test_a_rejected_device_gets_nothing(self):
        issued, outcome = await self.claimed()
        # Rejecting is the safe side: no step-up needed.
        plain = await self.sign_in(self.carol, step_up=False)
        await self.pairing.reject(plain, issued.pairing_id, self.context())
        with self.assertRaises(TokenRejectedError):
            await self.pairing.complete(outcome.claim, self.context())
        with self.assertRaises(PairingNotFoundError):
            await self.pairing.approve(self.trusted, issued.pairing_id, self.context())
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM auth_sessions WHERE auth_method = 'pairing'"
            ),
            0,
        )

    async def test_the_approval_must_come_in_time(self):
        issued, outcome = await self.claimed()
        await self.advance(minutes=10)
        self.trusted = await self.sign_in(self.carol)
        with self.assertRaises(PairingNotFoundError):
            await self.pairing.approve(self.trusted, issued.pairing_id, self.context())
        with self.assertRaises(TokenRejectedError):
            await self.pairing.complete(outcome.claim, self.context())

    async def test_only_the_same_user_decides(self):
        issued, _ = await self.claimed()
        dave = await self.make_user("dave", role="admin")
        stranger = await self.sign_in(dave)
        for decide in (self.pairing.approve, self.pairing.reject):
            with self.subTest(decide.__name__), self.assertRaises(PairingNotFoundError):
                await decide(stranger, issued.pairing_id, self.context())
        with self.assertRaises(PairingNotFoundError):
            await self.pairing.approve(self.trusted, uuid.uuid4(), self.context())
        self.assertEqual(await self.pairing.pending(stranger), ())

    async def test_polling_with_the_right_claim_is_never_locked_out(self):
        _, outcome = await self.claimed()
        for _ in range(12):
            waiting = await self.pairing.complete(outcome.claim, self.context())
            self.assertFalse(waiting.completed)
        # A wrong claim counts against the token (and the source).
        for _ in range(5):
            with self.assertRaises(TokenRejectedError):
                await self.pairing.complete(
                    wrong(outcome.claim), self.context("10.5.0.1")
                )
        with self.assertRaises(TokenRejectedError):
            await self.pairing.complete(outcome.claim, self.context())

    async def test_the_claim_and_the_pairing_token_are_different_kinds(self):
        issued, outcome = await self.claimed()
        with self.assertRaises(TokenRejectedError):
            await self.pairing.complete(issued.token, self.context())
        with self.assertRaises(TokenRejectedError):
            await self.pairing.claim(outcome.claim, "Tablet", self.context())

    async def test_the_claims_lookup_key_is_not_the_pairing_tokens(self):
        issued, outcome = await self.claimed()
        pairing_key = issued.token.split(".")[1]
        claim_key = outcome.claim.split(".")[1]
        self.assertNotEqual(pairing_key, claim_key)

    async def test_whoever_saw_the_qr_code_cannot_lock_the_waiting_device_out(self):
        issued, outcome = await self.claimed()
        # A bystander knows the pairing token's id (it is in the QR code): claims
        # made up from it, and wrong pairing tokens after the claim, count nothing.
        forged = "pawpc1." + issued.token.split(".")[1] + "." + "x" * 43
        for index in range(6):
            source = self.context(f"10.6.0.{index}")
            with self.assertRaises(TokenRejectedError):
                await self.pairing.complete(forged, source)
            with self.assertRaises(TokenRejectedError):
                await self.pairing.claim(wrong(issued.token), "Evil", source)
        self.assertIsNone(
            await self.scalar(
                "SELECT locked_at FROM device_pairings WHERE audit_ref = :r",
                r=issued.pairing_id,
            )
        )
        await self.pairing.approve(self.trusted, issued.pairing_id, self.context())
        done = await self.pairing.complete(outcome.claim, self.context())
        self.assertTrue(done.completed)


@requires_postgres
class PairingGateTest(OnboardingTestCase):
    """With Passkeys configured, the new session's gate follows Decision 0033."""

    settings_overrides = PASSKEY_SETTINGS

    async def test_a_user_whose_policy_requires_a_passkey_gets_the_sign_in_gate(self):
        await self.execute(
            "UPDATE auth_policy SET version = version + 1, passkey_user = 'required'"
        )
        bob = await self.make_user("bob")
        trusted = await self.sign_in(bob, step_up=False)
        await self.execute(
            "UPDATE auth_sessions SET passkey_gate = 'open' WHERE id = :id",
            id=trusted.record.id,
        )
        issued = await self.pairing.issue(trusted, self.context())
        outcome = await self.pairing.claim(issued.token, "Phone", self.context())
        self.assertIs(
            outcome.login.session.record.passkey_gate, PasskeyGate.ENROLLMENT_REQUIRED
        )

    async def test_an_approved_admin_device_is_open(self):
        carol = await self.make_user("carol", role="admin")
        trusted = await self.sign_in(carol, step_up=False)
        await self.execute(
            "UPDATE auth_sessions SET passkey_gate = 'open' WHERE id = :id",
            id=trusted.record.id,
        )
        await self.fake_passkey_step_up(trusted.record.id)
        issued = await self.pairing.issue(trusted, self.context())
        outcome = await self.pairing.claim(issued.token, "Tablet", self.context())
        await self.pairing.approve(trusted, issued.pairing_id, self.context())
        done = await self.pairing.complete(outcome.claim, self.context())
        self.assertIs(done.login.session.record.passkey_gate, PasskeyGate.OPEN)
