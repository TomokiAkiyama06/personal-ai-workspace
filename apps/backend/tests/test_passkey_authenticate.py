"""The Passkey Step-up (PAW-023, PostgreSQL): assertions, counters, hostile answers."""

import asyncio
from unittest.mock import patch

from sqlalchemy import text

from paw_backend.auth.errors import (
    InvalidCredentialsError,
    NoPasskeyError,
    SessionEndedError,
    ThrottledError,
)
from paw_backend.auth.models import AuthMethod
from paw_backend.auth.passkeys.service import PasskeyStepUpVerifier
from paw_backend.auth.tokens import hash_session_token

from .auth_support import requires_postgres
from .passkey_pg_support import (
    ORIGIN,
    RP_ID,
    PasskeyTestCase,
    context,
    device,
)
from .passkey_support import UP, UV, b64url, unb64url


@requires_postgres
class BeginTest(PasskeyTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.alice = await self.make_user("alice")

    async def test_the_options_name_the_users_credentials_only(self):
        bob = await self.make_user("bob")
        alice_session, phone = await self.enrolled_session(self.alice)
        stepped = await self.authenticate(alice_session, phone)
        laptop = device()
        laptop_registered = await self.register(stepped.session, laptop)
        await self.enrolled_session(bob)  # a credential that is not Alice's
        options = await self.passkeys.authenticate_begin(
            laptop_registered.auth, context()
        )
        self.assertEqual(options["rpId"], RP_ID)
        self.assertEqual(options["userVerification"], "required")
        self.assertEqual(options["timeout"], 300_000)
        self.assertEqual(len(unb64url(options["challenge"])), 32)
        self.assertEqual(
            sorted(c["id"] for c in options["allowCredentials"]),
            sorted(
                b64url(c.credential_id)
                for c in (phone.credentials[0], laptop.credentials[0])
            ),
        )
        self.assertTrue(
            all(c["type"] == "public-key" for c in options["allowCredentials"])
        )

    async def test_a_revoked_credential_is_not_offered(self):
        auth, phone = await self.enrolled_session(self.alice)
        await self.execute(
            "UPDATE user_passkeys SET revoked_at = now(), revoked_reason = "
            "'revoked_by_user'"
        )
        with self.assertRaises(NoPasskeyError):
            await self.passkeys.authenticate_begin(auth, context())

    async def test_without_a_passkey_there_is_nothing_to_authenticate_with(self):
        auth = await self.sign_in(self.alice)
        with self.assertRaises(NoPasskeyError):
            await self.passkeys.authenticate_begin(auth, context())
        self.assertEqual(await self.query("SELECT * FROM passkey_challenges"), [])

    async def test_the_challenge_is_stored_for_the_session_and_replaced(self):
        auth, phone = await self.enrolled_session(self.alice)
        first = await self.passkeys.authenticate_begin(auth, context())
        second = await self.passkeys.authenticate_begin(auth, context())
        rows = await self.query(
            "SELECT * FROM passkey_challenges WHERE purpose = 'authenticate'"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(bytes(rows[0].challenge), unb64url(second["challenge"]))
        self.assertNotEqual(first["challenge"], second["challenge"])
        self.assertEqual(rows[0].session_id, auth.record.id)


@requires_postgres
class StepUpTest(PasskeyTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.alice = await self.make_user("alice")
        self.auth_session, self.phone = await self.enrolled_session(self.alice)

    async def test_a_passkey_step_up_is_recorded_and_the_session_id_rotates(self):
        old = self.auth_session
        self.clock.advance(minutes=5)
        stepped = await self.authenticate(old, self.phone)
        record = stepped.session.record
        self.assertEqual(record.stepup_method, AuthMethod.PASSKEY)
        self.assertEqual(record.stepup_at, self.clock.now)
        self.assertEqual(record.id, old.record.id)  # the same session ...
        self.assertNotEqual(stepped.token, "")
        # ... under a new id: the old one no longer matches the stored hash.
        row = await self.session_row(record.id)
        self.assertEqual(bytes(row.token_hash), hash_session_token(stepped.token))
        self.assertNotEqual(bytes(row.token_hash), old.token_hash)
        self.assertEqual(row.rotated_at, self.clock.now)
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.passkey.authenticate", "allow", "verified")], 1)
        self.assertNotIn(("auth.step_up", "allow", "verified"), summary)

    async def test_the_counter_and_the_last_use_are_stored(self):
        self.clock.advance(minutes=1)
        await self.authenticate(self.auth_session, self.phone)
        row = (await self.passkey_rows(self.alice.id))[0]
        self.assertEqual((row.sign_count, row.last_used_at), (1, self.clock.now))

    async def test_the_step_up_is_reported_with_its_method_and_window(self):
        stepped = await self.authenticate(self.auth_session, self.phone)
        view = await self.auth.view(stepped.session)
        step_up = view.auth.step_up
        self.assertEqual(
            (step_up.method, step_up.window_minutes, step_up.satisfied),
            (AuthMethod.PASSKEY, 30, True),
        )
        self.assertEqual(
            (step_up.valid_until - step_up.verified_at).total_seconds(), 1800
        )

    async def test_the_step_up_lasts_the_policys_window_and_not_a_second_more(self):
        stepped = await self.authenticate(self.auth_session, self.phone)
        for seconds, satisfied in ((1799, True), (1800, False)):
            with self.subTest(seconds=seconds):
                self.clock.advance(seconds=seconds)
                self.assertEqual(await self._satisfied(stepped.session), satisfied)
                self.clock.advance(seconds=-seconds)

    async def _satisfied(self, session) -> bool:
        from dataclasses import replace

        checked = replace(session, checked_at=self.clock.now)
        return (await self.auth.view(checked)).auth.step_up.satisfied

    async def test_an_answer_is_used_once(self):
        options = await self.passkeys.authenticate_begin(self.auth_session, context())
        answer = self.phone.get(options)
        stepped = await self.step_up_with(self.auth_session, answer)
        with self.assertRaises(InvalidCredentialsError):
            await self.step_up_with(stepped.session, answer)
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.passkey.authenticate", "deny", "challenge_invalid")], 1
        )

    async def test_the_challenge_expires_at_the_database_clock(self):
        for seconds, accepted in ((299, True), (300, False)):
            with self.subTest(seconds=seconds):
                options = await self.passkeys.authenticate_begin(
                    self.auth_session, context()
                )
                answer = self.phone.get(options)
                self.clock.advance(seconds=seconds)
                if accepted:
                    stepped = await self.step_up_with(self.auth_session, answer)
                    self.auth_session = stepped.session
                else:
                    with self.assertRaises(InvalidCredentialsError):
                        await self.step_up_with(self.auth_session, answer)
                self.clock.advance(seconds=-seconds)

    async def test_the_challenge_belongs_to_the_session(self):
        other = await self.sign_in(self.alice)
        options = await self.passkeys.authenticate_begin(self.auth_session, context())
        answer = self.phone.get(options)
        with self.assertRaises(InvalidCredentialsError):
            await self.step_up_with(other, answer)
        # Nothing was recorded for either; the first session's challenge is intact.
        self.assertIsNone((await self.session_row(other.record.id)).stepup_method)
        stepped = await self.step_up_with(self.auth_session, answer)
        self.assertEqual(stepped.session.record.stepup_method, AuthMethod.PASSKEY)

    async def test_a_register_challenge_cannot_answer_an_authentication(self):
        options = await self.passkeys.register_begin(
            (await self.authenticate(self.auth_session, self.phone)).session, context()
        )
        # An assertion over the REGISTRATION challenge, sent as a step-up.
        answer = self.phone.get(
            {
                "rpId": RP_ID,
                "challenge": options["challenge"],
                "userVerification": "required",
            }
        )
        rows = await self.query("SELECT purpose FROM passkey_challenges")
        self.assertEqual([r.purpose for r in rows], ["register"])
        with self.assertRaises(InvalidCredentialsError):
            await self.step_up_with(self.auth_session, answer)

    async def test_hostile_assertions_are_refused_and_recorded(self):
        bob = await self.make_user("bob")
        _, bobs_device = await self.enrolled_session(bob)
        stranger = device()  # a credential the server has never seen
        await self.register(await self.sign_in(await self.make_user("carol")), stranger)
        cases = {
            "another origin": (
                {"origin": "https://evil.example"},
                "verification_failed",
            ),
            "the http origin": (
                {"origin": "http://paw.example.test"},
                "verification_failed",
            ),
            "another port": ({"origin": ORIGIN + ":8443"}, "verification_failed"),
            "another rp id hash": ({"rp_id": "evil.example"}, "verification_failed"),
            "another challenge": ({"challenge": b"\x02" * 32}, "verification_failed"),
            "no user verification": ({"flags": UP}, "verification_failed"),
            "no user presence": ({"flags": UV}, "verification_failed"),
            "a registration type": (
                {"client_data_type": "webauthn.create"},
                "verification_failed",
            ),
            "a cross-origin frame": (
                {"client_data_extra": {"crossOrigin": True}},
                "verification_failed",
            ),
            "a top origin": (
                {"client_data_extra": {"topOrigin": ORIGIN}},
                "verification_failed",
            ),
            "another users handle": (
                {"user_handle": b"\x09" * 16},
                "verification_failed",
            ),
            "a signature of another key": (
                {"sign_with": _other_key()},
                "verification_failed",
            ),
        }
        for label, (doctored, _reason) in cases.items():
            with self.subTest(label):
                await self.execute("TRUNCATE auth_throttles")
                options = await self.passkeys.authenticate_begin(
                    self.auth_session, context()
                )
                answer = self.phone.get(options, **doctored)
                with self.assertRaises(InvalidCredentialsError):
                    await self.step_up_with(self.auth_session, answer)
        for label, authenticator in (
            ("another users credential", bobs_device),
            ("an unknown credential", stranger),
        ):
            with self.subTest(label):
                await self.execute("TRUNCATE auth_throttles")
                options = await self.passkeys.authenticate_begin(
                    self.auth_session, context()
                )
                # (A real authenticator would refuse: this one is made to insist.)
                answer = authenticator.get(
                    options, credential=authenticator.credentials[0]
                )
                with self.assertRaises(InvalidCredentialsError):
                    await self.step_up_with(self.auth_session, answer)
        # A tampered signature.
        options = await self.passkeys.authenticate_begin(self.auth_session, context())
        answer = self.phone.get(options)
        signature = bytearray(unb64url(answer["response"]["signature"]))
        signature[-1] ^= 1
        answer["response"]["signature"] = b64url(bytes(signature))
        with self.assertRaises(InvalidCredentialsError):
            await self.step_up_with(self.auth_session, answer)
        row = await self.session_row(self.auth_session.record.id)
        self.assertIsNone(row.stepup_method)
        self.assertEqual((await self.passkey_rows(self.alice.id))[0].sign_count, 0)
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.passkey.authenticate", "deny", "verification_failed")],
            len(cases) + 1,
        )
        self.assertEqual(
            summary[("auth.passkey.authenticate", "deny", "unknown_credential")], 2
        )

    async def test_the_signature_counter_policy(self):
        # (stored, presented, accepted): the count must go up, or stay 0 for an
        # authenticator that has no counter.
        cases = (
            (0, 0, True),
            (0, 1, True),
            (1, 1, False),
            (5, 4, False),
            (5, 5, False),
            (5, 6, True),
            (6, 0, False),
            (4_294_967_294, 4_294_967_295, True),
            (4_294_967_295, 4_294_967_295, False),
            (7, 4_294_967_295, True),
        )
        for stored, presented, accepted in cases:
            with self.subTest(stored=stored, presented=presented):
                await self.execute("UPDATE user_passkeys SET sign_count = :n", n=stored)
                await self.execute("TRUNCATE auth_throttles")
                options = await self.passkeys.authenticate_begin(
                    self.auth_session, context()
                )
                answer = self.phone.get(options, count=presented)
                if accepted:
                    stepped = await self.step_up_with(self.auth_session, answer)
                    self.auth_session = stepped.session
                else:
                    with self.assertRaises(InvalidCredentialsError):
                        await self.step_up_with(self.auth_session, answer)
                self.assertEqual(
                    (await self.passkey_rows(self.alice.id))[0].sign_count,
                    presented if accepted else stored,
                )
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.passkey.authenticate", "deny", "sign_count_regression")],
            sum(1 for *_, accepted in cases if not accepted),
        )

    async def test_a_regression_is_logged_without_the_credential(self):
        await self.execute("UPDATE user_passkeys SET sign_count = 9")
        options = await self.passkeys.authenticate_begin(self.auth_session, context())
        answer = self.phone.get(options, count=3)
        with self.no_secret_in_logs(answer["id"]) as logs:
            with self.assertRaises(InvalidCredentialsError):
                await self.step_up_with(self.auth_session, answer)
        self.assertIn("did not advance the signature counter", logs.application_text)

    async def test_an_authenticator_without_a_counter_is_accepted_repeatedly(self):
        dave = await self.make_user("dave")
        auth = await self.sign_in(dave)
        flat = device(counts=False)
        registered = await self.register(auth, flat)
        session = registered.auth
        for _ in range(3):
            stepped = await self.authenticate(session, flat)
            session = stepped.session
        row = (await self.passkey_rows(dave.id))[0]
        self.assertEqual(row.sign_count, 0)

    async def test_a_synced_credentials_backup_state_follows_the_authenticator(self):
        erin = await self.make_user("erin")
        auth = await self.sign_in(erin)
        synced = device(backup_eligible=True, backed_up=False)
        registered = await self.register(auth, synced)
        synced.backed_up = True
        await self.authenticate(registered.auth, synced)
        row = (await self.passkey_rows(erin.id))[0]
        self.assertEqual((row.backup_eligible, row.backed_up), (True, True))

    async def test_backup_eligibility_never_changes(self):
        auth = await self.sign_in(await self.make_user("frank"))
        eligible = device(backup_eligible=False)
        registered = await self.register(auth, eligible)
        eligible.backup_eligible = True  # the authenticator now claims the opposite
        with self.assertRaises(InvalidCredentialsError):
            await self.authenticate(registered.auth, eligible)
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.passkey.authenticate", "deny", "verification_failed")
            ],
            1,
        )

    async def test_failures_count_like_wrong_passwords_and_a_success_resets_them(self):
        for _ in range(4):
            options = await self.passkeys.authenticate_begin(
                self.auth_session, context()
            )
            with self.assertRaises(InvalidCredentialsError):
                await self.step_up_with(
                    self.auth_session,
                    self.phone.get(options, origin="https://evil.example"),
                )
        stepped = await self.authenticate(self.auth_session, self.phone)
        self.assertEqual(
            await self.query(
                "SELECT * FROM auth_throttles WHERE scope = 'login_account'"
            ),
            [],
        )
        session = stepped.session
        for _ in range(5):
            options = await self.passkeys.authenticate_begin(session, context())
            with self.assertRaises(InvalidCredentialsError):
                await self.step_up_with(
                    session, self.phone.get(options, origin="https://evil.example")
                )
        options = await self.passkeys.authenticate_begin(session, context())
        with self.assertRaises(ThrottledError):
            await self.step_up_with(session, self.phone.get(options))
        self.assertEqual(
            (await self.audit_summary())[("auth.lockout", "deny", "backoff_started")], 1
        )

    async def test_a_passkey_step_up_of_a_session_that_ended_is_refused(self):
        options = await self.passkeys.authenticate_begin(self.auth_session, context())
        answer = self.phone.get(options)
        await self.execute(
            "UPDATE auth_sessions SET revoked_at = now(), revoked_reason = 'logout'"
        )
        with self.assertRaises(SessionEndedError):
            await self.step_up_with(self.auth_session, answer)

    async def test_the_credential_is_confirmed_again_when_the_step_up_is_recorded(self):
        # The assertion verifies, then the Passkey is revoked before the step-up is
        # recorded: no step-up results, and the refusal says why.
        original = PasskeyStepUpVerifier.verify

        async def verify_then_revoke(verifier, user_id, login_name, evidence):
            proven = await original(verifier, user_id, login_name, evidence)
            await self.execute(
                "UPDATE user_passkeys SET revoked_at = now(), revoked_reason = "
                "'revoked_by_user'"
            )
            return proven

        options = await self.passkeys.authenticate_begin(self.auth_session, context())
        answer = self.phone.get(options)
        with patch.object(PasskeyStepUpVerifier, "verify", verify_then_revoke):
            with self.assertRaises(InvalidCredentialsError):
                await self.step_up_with(self.auth_session, answer)
        row = await self.session_row(self.auth_session.record.id)
        self.assertIsNone(row.stepup_method)
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.passkey.authenticate", "deny", "unknown_credential")
            ],
            1,
        )

    async def test_a_revocation_that_is_underway_is_waited_for(self):
        """The recording transaction waits for a revocation that holds the row."""
        options = await self.passkeys.authenticate_begin(self.auth_session, context())
        answer = self.phone.get(options)
        holder = self.database  # the owner's engine
        async with holder.session() as blocker:
            await blocker.execute(
                text(
                    "UPDATE user_passkeys SET revoked_at = now(), "
                    "revoked_reason = 'revoked_by_user'"
                )
            )
            task = asyncio.create_task(self.step_up_with(self.auth_session, answer))
            await asyncio.sleep(0.5)
            self.assertFalse(task.done())  # it is waiting, not deciding on old data
            await blocker.commit()
        with self.assertRaises(InvalidCredentialsError):
            await task
        row = await self.session_row(self.auth_session.record.id)
        self.assertIsNone(row.stepup_method)

    async def test_the_secrets_stay_out_of_the_logs_and_the_audit(self):
        options = await self.passkeys.authenticate_begin(self.auth_session, context())
        answer = self.phone.get(options)
        secrets_ = [
            answer["id"],
            answer["response"]["signature"],
            options["challenge"],
        ]
        with self.no_secret_in_logs(*secrets_):
            await self.step_up_with(self.auth_session, answer)
        everything = await self.everything_stored()
        for secret in (answer["response"]["signature"], options["challenge"]):
            self.assertNotIn(secret, everything)
        self.assertNotIn(answer["id"], everything)


def _other_key():
    from .passkey_support import new_key

    return new_key(-7)[0]
