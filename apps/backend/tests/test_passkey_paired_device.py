"""A Passkey added on a newly paired device (Decision 0043, point 11, option A; #154).

An Owner or an Admin whose only Passkeys are bound to their devices cannot step up
on a new device, so could not register a Passkey there. A session created by a
pairing that a trusted device approved (a Passkey Step-up and the confirmation
code, Decision 0033 point 12) counts, for the first Passkey registration of that
session only, as a recent Passkey authentication, when all of these hold:

* the session's ``auth_method`` is ``pairing`` and the ``device_pairings`` row that
  created it is ``completed`` with ``approval_required``;
* the session was created inside the policy's Step-up window;
* the session has not registered a Passkey yet (either way), and no Passkey of the
  user has been revoked since (a revocation forgets every Passkey Step-up).

The registration is not a Step-up (Decision 0025), and its audit row says
``pairing_approved``. Every other case keeps asking for a Passkey Step-up.
"""

from paw_backend.auth.errors import (
    PasskeyChallengeError,
    PasskeyRequiredError,
    SessionEndedError,
    StepUpRequiredError,
)
from paw_backend.auth.models import AuthMethod, PasskeyGate

from .auth_support import requires_postgres
from .passkey_pg_support import ADMIN_PASSWORD, PasskeyTestCase, context, device

REGISTER = "auth.passkey.register"


@requires_postgres
class PairedDeviceTestCase(PasskeyTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.pairing = self.services.pairing

    async def trusted(self, name: str = "carol", role: str = "admin"):
        """``(user, a stepped-up session, the authenticator of that device)``."""
        user = await self.make_user(name, role=role, password=ADMIN_PASSWORD)
        auth, authenticator = await self.fully_stepped_up(user)
        return user, auth, authenticator

    async def pair(self, trusted, label: str = "Tablet"):
        """Pair a new device (approved by ``trusted`` when the role needs it)."""
        issued = await self.pairing.issue(trusted, context())
        outcome = await self.pairing.claim(issued.token, label, context())
        if issued.approval_required:
            await self.pairing.approve(
                trusted,
                issued.pairing_id,
                context(),
                confirmation_code=outcome.confirmation_code,
            )
            outcome = await self.pairing.complete(outcome.claim, context())
        self.assertTrue(outcome.completed)
        return outcome.login.session, issued

    async def pairing_row(self, pairing_id):
        rows = await self.query(
            "SELECT * FROM device_pairings WHERE audit_ref = :i", i=pairing_id
        )
        return rows[0]

    async def forget_step_up(self, session_id) -> None:
        await self.execute(
            "UPDATE auth_sessions SET stepup_at = NULL, stepup_method = NULL "
            "WHERE id = :i",
            i=session_id,
        )


class ApprovedPairingTest(PairedDeviceTestCase):
    async def test_an_approved_paired_session_adds_one_passkey_without_a_step_up(self):
        carol, trusted, _ = await self.trusted()
        paired, issued = await self.pair(trusted)
        self.assertIs(paired.record.auth_method, AuthMethod.PAIRING)
        self.assertIs(paired.record.passkey_gate, PasskeyGate.OPEN)

        registered = await self.register(paired, device())

        self.assertIsNone(registered.login)  # the gate was open: nothing rotates
        self.assertEqual(len(await self.passkey_rows(carol.id)), 2)
        row = await self.pairing_row(issued.pairing_id)
        self.assertIsNotNone(row.passkey_allowance_ended_at)
        summary = await self.audit_summary()
        self.assertEqual(summary[(REGISTER, "allow", "pairing_approved")], 1)
        # The trusted device's own first Passkey is the only plain registration.
        self.assertEqual(summary[(REGISTER, "allow", "registered")], 1)
        rows = [
            r
            for r in await self.audit_rows()
            if (r.action, r.reason) == (REGISTER, "pairing_approved")
        ]
        self.assertEqual(rows[0].actor_id, carol.id)
        self.assertEqual(rows[0].resource_kind, "passkey")
        self.assertEqual(rows[0].resource_id, registered.passkey_id)

    async def test_the_owner_too(self):
        boss, trusted, _ = await self.trusted("boss", role="owner")
        paired, _ = await self.pair(trusted)
        await self.register(paired, device())
        self.assertEqual(len(await self.passkey_rows(boss.id)), 2)

    async def test_the_registration_is_not_a_step_up(self):
        carol, trusted, _ = await self.trusted()
        paired, _ = await self.pair(trusted)
        registered = await self.register(paired, device())

        row = await self.session_row(paired.record.id)
        self.assertIsNone(row.stepup_at)
        self.assertIsNone(row.stepup_method)
        self.assertIsNone(row.passkey_id)
        # An operation that needs a Passkey Step-up is still refused.
        with self.assertRaises(StepUpRequiredError):
            await self.passkeys.revoke(paired, registered.passkey_id, context())

    async def test_a_second_registration_needs_a_passkey_step_up(self):
        carol, trusted, _ = await self.trusted()
        paired, _ = await self.pair(trusted)
        await self.register(paired, device())

        with self.assertRaises(StepUpRequiredError):
            await self.passkeys.register_begin(paired, context())
        self.assertEqual(len(await self.passkey_rows(carol.id)), 2)
        summary = await self.audit_summary()
        self.assertEqual(summary[(REGISTER, "allow", "pairing_approved")], 1)
        self.assertEqual(summary[(REGISTER, "deny", "step_up_required")], 1)

    async def test_the_second_registration_passes_with_a_passkey_step_up(self):
        carol, trusted, _ = await self.trusted()
        paired, _ = await self.pair(trusted)
        added = device()
        registered = await self.register(paired, added)
        stepped = await self.authenticate(registered.auth, added)

        await self.register(stepped.session, device())
        self.assertEqual(len(await self.passkey_rows(carol.id)), 3)
        summary = await self.audit_summary()
        self.assertEqual(summary[(REGISTER, "allow", "pairing_approved")], 1)
        self.assertEqual(summary[(REGISTER, "allow", "registered")], 2)

    async def test_a_finished_registration_cannot_be_replayed(self):
        carol, trusted, _ = await self.trusted()
        paired, _ = await self.pair(trusted)
        authenticator = device()
        options = await self.passkeys.register_begin(paired, context())
        answer = authenticator.create(options)
        await self.passkeys.register_finish(paired, answer, None, context())

        with self.assertRaises(PasskeyChallengeError):
            await self.passkeys.register_finish(paired, answer, None, context())
        self.assertEqual(len(await self.passkey_rows(carol.id)), 2)

    async def test_two_finishes_at_once_register_once(self):
        carol, trusted, _ = await self.trusted()
        paired, _ = await self.pair(trusted)
        options = await self.passkeys.register_begin(paired, context())
        answer = device().create(options)

        async def finish(services, _index):
            return await services.passkeys.register_finish(
                paired, answer, None, context()
            )

        results = await self.gather_on_own_engines(2, finish)
        failures = [r for r in results if isinstance(r, BaseException)]
        self.assertEqual(len(failures), 1, results)
        self.assertEqual(len(await self.passkey_rows(carol.id)), 2)
        summary = await self.audit_summary()
        self.assertEqual(summary[(REGISTER, "allow", "pairing_approved")], 1)

    async def test_the_allowance_is_judged_again_when_the_credential_is_stored(self):
        carol, trusted, _ = await self.trusted()
        paired, issued = await self.pair(trusted)
        options = await self.passkeys.register_begin(paired, context())
        answer = device().create(options)
        # Spent between begin and finish (as another registration would).
        await self.execute(
            "UPDATE device_pairings SET passkey_allowance_ended_at = now() "
            "WHERE audit_ref = :i",
            i=issued.pairing_id,
        )

        with self.assertRaises(StepUpRequiredError):
            await self.passkeys.register_finish(paired, answer, None, context())
        self.assertEqual(len(await self.passkey_rows(carol.id)), 1)

    async def test_the_allowance_ends_with_the_step_up_window(self):
        carol, trusted, _ = await self.trusted()
        paired, issued = await self.pair(trusted)
        self.clock.advance(minutes=31)  # the default window is 30 minutes

        with self.assertRaises(StepUpRequiredError):
            await self.passkeys.register_begin(paired, context())
        self.assertIsNone(
            (await self.pairing_row(issued.pairing_id)).passkey_allowance_ended_at
        )
        summary = await self.audit_summary()
        self.assertEqual(summary[(REGISTER, "deny", "step_up_required")], 1)

    async def test_the_window_is_the_policys(self):
        carol, trusted, _ = await self.trusted()
        paired, _ = await self.pair(trusted)
        await self.execute(
            "UPDATE auth_policy SET version = version + 1, stepup_window_minutes = 5"
        )
        self.clock.advance(minutes=6)

        with self.assertRaises(StepUpRequiredError):
            await self.passkeys.register_begin(paired, context())

    async def test_a_registration_with_a_step_up_spends_the_allowance(self):
        carol, trusted, synced = await self.trusted()
        paired, issued = await self.pair(trusted)
        # A synced Passkey works on the new device: a real Step-up there.
        stepped = await self.authenticate(paired, synced)
        await self.register(stepped.session, device())
        summary = await self.audit_summary()
        self.assertEqual(summary[(REGISTER, "allow", "pairing_approved")], 0)
        self.assertIsNotNone(
            (await self.pairing_row(issued.pairing_id)).passkey_allowance_ended_at
        )

        await self.forget_step_up(paired.record.id)
        with self.assertRaises(StepUpRequiredError):
            await self.passkeys.register_begin(stepped.session, context())

    async def test_revoking_a_passkey_ends_the_allowance(self):
        carol, trusted, _ = await self.trusted()
        spare = await self.register(trusted, device())  # a Step-up is still fresh
        paired, issued = await self.pair(trusted)

        await self.passkeys.revoke(trusted, spare.passkey_id, context())

        self.assertIsNotNone(
            (await self.pairing_row(issued.pairing_id)).passkey_allowance_ended_at
        )
        with self.assertRaises(StepUpRequiredError):
            await self.passkeys.register_begin(paired, context())


class NoAllowanceTest(PairedDeviceTestCase):
    async def test_a_users_pairing_has_no_approval_and_gives_nothing(self):
        bob = await self.make_user("bob")
        trusted, _ = await self.enrolled_session(bob)  # a Passkey, no Step-up
        paired, issued = await self.pair(trusted)
        self.assertFalse(issued.approval_required)
        self.assertIs(paired.record.auth_method, AuthMethod.PAIRING)

        with self.assertRaises(StepUpRequiredError):
            await self.passkeys.register_begin(paired, context())
        self.assertEqual(len(await self.passkey_rows(bob.id)), 1)
        summary = await self.audit_summary()
        self.assertEqual(summary[(REGISTER, "allow", "pairing_approved")], 0)
        self.assertEqual(summary[(REGISTER, "deny", "step_up_required")], 1)

    async def test_a_password_session_gets_nothing(self):
        carol, trusted, synced = await self.trusted()
        await self.pair(trusted)  # an approved pairing of hers exists
        # A fresh password sign-in: first the Passkey gate...
        other = await self.sign_in(carol)
        self.assertIs(other.record.passkey_gate, PasskeyGate.ASSERTION_REQUIRED)
        with self.assertRaises(PasskeyRequiredError):
            await self.passkeys.register_begin(other, context())
        # ...then, the gate open and the Step-up gone, still no registration.
        opened = (await self.authenticate(other, synced)).session
        await self.forget_step_up(opened.record.id)
        with self.assertRaises(StepUpRequiredError):
            await self.passkeys.register_begin(opened, context())

    async def test_the_approving_session_does_not_borrow_it(self):
        carol, trusted, _ = await self.trusted()
        await self.pair(trusted)
        await self.forget_step_up(trusted.record.id)
        with self.assertRaises(StepUpRequiredError):
            await self.passkeys.register_begin(trusted, context())

    async def test_the_allowance_follows_the_rows_created_session(self):
        # A pairing session that no approved row names (here: the row forgot it,
        # as the session purge does) gets nothing.
        carol, trusted, _ = await self.trusted()
        paired, issued = await self.pair(trusted)
        await self.execute(
            "UPDATE device_pairings SET created_session = NULL WHERE audit_ref = :i",
            i=issued.pairing_id,
        )
        with self.assertRaises(StepUpRequiredError):
            await self.passkeys.register_begin(paired, context())

    async def test_an_ended_paired_session_registers_nothing(self):
        carol, trusted, _ = await self.trusted()
        paired, _ = await self.pair(trusted)
        await self.auth.logout(paired, context())
        with self.assertRaises(SessionEndedError):
            await self.passkeys.register_begin(paired, context())
        self.assertEqual(len(await self.passkey_rows(carol.id)), 1)
