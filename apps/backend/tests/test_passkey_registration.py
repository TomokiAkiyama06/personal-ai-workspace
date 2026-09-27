"""Registering a Passkey (PAW-023, PostgreSQL): ceremony, rules, hostile answers."""

import uuid
from unittest.mock import patch

from paw_backend.auth.errors import (
    AuthUnavailableError,
    InvalidAuthInputError,
    PasskeyChallengeError,
    PasskeyExistsError,
    PasskeyLimitError,
    PasskeyVerificationError,
    SessionEndedError,
    StepUpMethodInsufficientError,
    StepUpRequiredError,
    ThrottledError,
)
from paw_backend.auth.models import AuthMethod
from paw_backend.auth.passkeys.models import MAX_PASSKEYS_PER_USER
from paw_backend.auth.state import StepUpEvidence

from .auth_support import PASSWORD, requires_postgres
from .passkey_pg_support import (
    ORIGIN,
    RP_ID,
    PasskeyTestCase,
    context,
    device,
)
from .passkey_support import (
    AT,
    ED,
    EDDSA,
    ES256,
    RS256,
    UP,
    UV,
    NotAllowed,
    b64url,
    unb64url,
)


@requires_postgres
class BeginTest(PasskeyTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.alice = await self.make_user("alice")

    async def test_the_options_carry_the_policy(self):
        auth = await self.sign_in(self.alice)
        options = await self.passkeys.register_begin(auth, context())
        self.assertEqual(options["rp"], {"name": "Personal AI Workspace", "id": RP_ID})
        self.assertEqual(
            options["user"],
            {
                "id": b64url(self.alice.id.bytes),
                "name": "alice",
                "displayName": "alice",
            },
        )
        self.assertEqual(len(unb64url(options["challenge"])), 32)
        self.assertEqual(options["attestation"], "none")
        self.assertEqual(
            options["authenticatorSelection"],
            {
                "residentKey": "preferred",
                "requireResidentKey": False,
                "userVerification": "required",
            },
        )
        self.assertEqual(
            [p["alg"] for p in options["pubKeyCredParams"]], [EDDSA, ES256, RS256]
        )
        self.assertEqual(options["excludeCredentials"], [])
        self.assertEqual(options["timeout"], 300_000)

    async def test_the_challenge_is_stored_for_this_session_with_a_short_expiry(self):
        auth = await self.sign_in(self.alice)
        options = await self.passkeys.register_begin(auth, context())
        rows = await self.query("SELECT * FROM passkey_challenges")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            (row.user_id, row.session_id, row.purpose),
            (self.alice.id, auth.record.id, "register"),
        )
        self.assertEqual(bytes(row.challenge), unb64url(options["challenge"]))
        self.assertEqual((row.expires_at - row.created_at).total_seconds(), 300)
        self.assertEqual(row.created_at, self.clock.now)

    async def test_two_begins_leave_one_challenge_the_second(self):
        auth = await self.sign_in(self.alice)
        await self.passkeys.register_begin(auth, context())
        second = await self.passkeys.register_begin(auth, context())
        rows = await self.query("SELECT challenge FROM passkey_challenges")
        self.assertEqual(
            [bytes(r.challenge) for r in rows], [unb64url(second["challenge"])]
        )

    async def test_a_challenge_is_not_reused_between_begins(self):
        auth = await self.sign_in(self.alice)
        seen = set()
        for _ in range(5):
            options = await self.passkeys.register_begin(auth, context())
            seen.add(options["challenge"])
        self.assertEqual(len(seen), 5)

    async def test_registered_credentials_are_excluded(self):
        auth = await self.sign_in(self.alice)
        first = device()
        registered = await self.register(auth, first)
        # A second ceremony needs a Passkey step-up (the user has a Passkey now).
        stepped = await self.authenticate(registered.auth, first)
        options = await self.passkeys.register_begin(stepped.session, context())
        self.assertEqual(
            [c["id"] for c in options["excludeCredentials"]],
            [b64url(first.credentials[0].credential_id)],
        )
        with self.assertRaises(NotAllowed):  # the authenticator refuses a repeat
            first.create(options)

    async def test_without_configuration_nothing_is_offered(self):
        from paw_backend.auth.errors import PasskeyUnavailableError

        plain = self.build(
            self.service_database,
            settings=self.settings.model_copy(
                update={"passkey_rp_id": None, "passkey_origins": []}
            ),
        )
        auth = await self.sign_in(self.alice)
        with self.assertRaises(PasskeyUnavailableError):
            await plain.passkeys.register_begin(auth, context())
        with self.assertRaises(PasskeyUnavailableError):
            await plain.passkeys.authenticate_begin(auth, context())
        self.assertEqual(await self.query("SELECT * FROM passkey_challenges"), [])


@requires_postgres
class FinishTest(PasskeyTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.alice = await self.make_user("alice")

    async def test_a_user_registers_and_the_credential_is_stored(self):
        auth = await self.sign_in(self.alice)
        authenticator = device()
        registered = await self.register(auth, authenticator, name="Alice's phone")
        credential = authenticator.credentials[0]
        rows = await self.passkey_rows(self.alice.id)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            (
                row.id,
                bytes(row.credential_id),
                row.sign_count,
                row.name,
                row.aaguid,
                row.backup_eligible,
                row.backed_up,
                row.created_at,
                row.last_used_at,
                row.revoked_at,
            ),
            (
                registered.passkey_id,
                credential.credential_id,
                0,
                "Alice's phone",
                None,
                False,
                False,
                self.clock.now,
                None,
                None,
            ),
        )
        self.assertEqual(len(bytes(row.public_key)), 77)  # a COSE ES256 key
        # The challenge was spent, and nothing but ids and enums went to the audit.
        self.assertEqual(await self.query("SELECT * FROM passkey_challenges"), [])
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.passkey.register", "allow", "registered")], 1)
        event = [
            r for r in await self.audit_rows() if r.action == "auth.passkey.register"
        ][0]
        self.assertEqual(
            (event.actor_id, event.actor_role, event.resource_kind, event.resource_id),
            (self.alice.id, "user", "passkey", registered.passkey_id),
        )

    async def test_the_default_name_and_the_list(self):
        auth = await self.sign_in(self.alice)
        registered = await self.register(auth, device())
        listed = await self.passkeys.list_passkeys(registered.auth)
        self.assertEqual(
            [(p.id, p.name, p.backup_eligible, p.last_used_at) for p in listed],
            [(registered.passkey_id, "Passkey", False, None)],
        )

    async def test_the_name_is_checked_before_anything_is_spent(self):
        auth = await self.sign_in(self.alice)
        for bad in ("x" * 65, "a\x00b", "line\nbreak", 5, b"phone", ["a"]):
            with self.subTest(name=repr(bad)[:20]):
                options = await self.passkeys.register_begin(auth, context())
                answer = device().create(options)
                with self.assertRaises(InvalidAuthInputError):
                    await self.passkeys.register_finish(auth, answer, bad, context())
        # The challenge of the last begin is still there: nothing was consumed.
        self.assertEqual(len(await self.query("SELECT * FROM passkey_challenges")), 1)
        self.assertEqual(await self.passkey_rows(), [])

    async def test_every_supported_algorithm_registers_and_authenticates(self):
        for alg, size in ((ES256, 77), (EDDSA, 42), (RS256, 272)):
            with self.subTest(alg=alg):
                user = await self.make_user(f"user-{-alg}")
                auth = await self.sign_in(user)
                authenticator = device()
                registered = await self.register(auth, authenticator, alg=alg)
                key = (await self.passkey_rows(user.id))[0].public_key
                self.assertEqual(len(bytes(key)), size)
                stepped = await self.authenticate(registered.auth, authenticator)
                self.assertEqual(
                    stepped.session.record.stepup_method, AuthMethod.PASSKEY
                )

    async def test_a_synced_passkey_is_recorded_as_such(self):
        auth = await self.sign_in(self.alice)
        await self.register(auth, device(backup_eligible=True, backed_up=True))
        row = (await self.passkey_rows(self.alice.id))[0]
        self.assertEqual((row.backup_eligible, row.backed_up), (True, True))

    async def test_the_authenticator_model_is_recorded_when_it_says_so(self):
        auth = await self.sign_in(self.alice)
        model = uuid.uuid4()
        await self.register(auth, device(), aaguid=model.bytes)
        self.assertEqual((await self.passkey_rows(self.alice.id))[0].aaguid, model)

    async def test_an_answer_is_accepted_once(self):
        auth = await self.sign_in(self.alice)
        options = await self.passkeys.register_begin(auth, context())
        answer = device().create(options)
        await self.passkeys.register_finish(auth, answer, None, context())
        with self.assertRaises(PasskeyChallengeError):
            await self.passkeys.register_finish(auth, answer, None, context())
        self.assertEqual(len(await self.passkey_rows()), 1)
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.passkey.register", "deny", "challenge_invalid")
            ],
            1,
        )

    async def test_no_challenge_no_registration(self):
        auth = await self.sign_in(self.alice)
        answer = device().create(
            {**(await self.passkeys.register_begin(auth, context()))}
        )
        await self.execute("DELETE FROM passkey_challenges")
        with self.assertRaises(PasskeyChallengeError):
            await self.passkeys.register_finish(auth, answer, None, context())

    async def test_a_challenge_expires_at_its_time_not_after(self):
        auth = await self.sign_in(self.alice)
        for seconds, accepted in ((299, True), (300, False), (301, False)):
            with self.subTest(seconds=seconds):
                await self.execute("DELETE FROM user_passkeys")
                options = await self.passkeys.register_begin(auth, context())
                answer = device().create(options)
                self.clock.advance(seconds=seconds)
                if accepted:
                    await self.passkeys.register_finish(auth, answer, None, context())
                else:
                    with self.assertRaises(PasskeyChallengeError):
                        await self.passkeys.register_finish(
                            auth, answer, None, context()
                        )
                self.clock.advance(seconds=-seconds)

    async def test_the_challenge_belongs_to_the_session_that_asked(self):
        first = await self.sign_in(self.alice)
        second = await self.sign_in(self.alice)
        options = await self.passkeys.register_begin(first, context())
        answer = device().create(options)
        with self.assertRaises(PasskeyChallengeError):
            await self.passkeys.register_finish(second, answer, None, context())
        # The first session's challenge is untouched and still works for it.
        self.assertEqual(len(await self.query("SELECT * FROM passkey_challenges")), 1)
        await self.passkeys.register_finish(first, answer, None, context())

    async def test_a_wrong_answer_burns_the_challenge(self):
        auth = await self.sign_in(self.alice)
        options = await self.passkeys.register_begin(auth, context())
        with self.assertRaises(PasskeyVerificationError):
            await self.passkeys.register_finish(
                auth,
                device().create(options, origin="https://evil.example"),
                None,
                context(),
            )
        with self.assertRaises(PasskeyChallengeError):
            await self.passkeys.register_finish(
                auth, device().create(options), None, context()
            )
        self.assertEqual(await self.passkey_rows(), [])

    async def test_hostile_answers_are_refused_and_recorded(self):
        cases = {
            "another origin": {"origin": "https://evil.example"},
            "the http origin": {"origin": "http://paw.example.test"},
            "an explicit default port": {"origin": ORIGIN + ":443"},
            "another port": {"origin": ORIGIN + ":8443"},
            "a trailing slash": {"origin": ORIGIN + "/"},
            "another case": {"origin": "https://PAW.example.test"},
            "a sub-domain": {"origin": "https://sub.paw.example.test"},
            "another rp id": {"rp_id": "evil.example"},
            "a sub-domain rp id": {"rp_id": "sub." + RP_ID},
            "another challenge": {"challenge": b"\x01" * 32},
            "no user verification": {"flags": UP | AT},
            "no user presence": {"flags": UV | AT},
            "no attested credential data": {"flags": UP | UV},
            "extension data flagged": {"flags": UP | UV | AT | ED},
            "an assertion type": {"client_data_type": "webauthn.get"},
            "a cross-origin frame": {"client_data_extra": {"crossOrigin": True}},
            "a top origin": {"client_data_extra": {"topOrigin": ORIGIN}},
            "a packed statement": {
                "fmt": "packed",
                "att_stmt": {"alg": -7, "sig": b"x" * 8},
            },
            "a none statement with content": {"att_stmt": {"x": 1}},
            "an unknown statement": {"fmt": "made-up"},
        }
        auth = await self.sign_in(self.alice)
        for label, doctored in cases.items():
            with self.subTest(label):
                # Failures count against the account (their own test): start clean.
                await self.execute("TRUNCATE auth_throttles")
                options = await self.passkeys.register_begin(auth, context())
                answer = device().create(options, **doctored)
                with self.assertRaises(PasskeyVerificationError):
                    await self.passkeys.register_finish(auth, answer, None, context())
        self.assertEqual(await self.passkey_rows(), [])
        self.assertEqual(await self.query("SELECT * FROM passkey_challenges"), [])
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.passkey.register", "deny", "verification_failed")],
            len(cases),
        )

    async def test_the_attested_credential_must_be_the_one_named(self):
        auth = await self.sign_in(self.alice)
        options = await self.passkeys.register_begin(auth, context())
        answer = device().create(options)
        other = b64url(b"\x07" * 32)
        answer["id"] = answer["rawId"] = other
        with self.assertRaises(PasskeyVerificationError):
            await self.passkeys.register_finish(auth, answer, None, context())
        self.assertEqual(await self.passkey_rows(), [])

    async def test_a_credential_that_exists_is_refused_for_anyone(self):
        bob = await self.make_user("bob")
        bobs = await self.sign_in(bob)
        theirs = device()
        registered = await self.register(bobs, theirs)
        # Alice's device presents Bob's credential id.
        auth = await self.sign_in(self.alice)
        options = await self.passkeys.register_begin(auth, context())
        answer = device().create(
            options, credential_id=theirs.credentials[0].credential_id
        )
        with self.assertRaises(PasskeyExistsError):
            await self.passkeys.register_finish(auth, answer, None, context())
        self.assertEqual(len(await self.passkey_rows()), 1)
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.passkey.register", "deny", "already_registered")
            ],
            1,
        )
        self.assertEqual((await self.passkey_rows())[0].id, registered.passkey_id)

    async def test_a_revoked_credential_id_cannot_be_registered_again(self):
        auth = await self.sign_in(self.alice)
        authenticator = device()
        registered = await self.register(auth, authenticator)
        await self.execute(
            "UPDATE user_passkeys SET revoked_at = now(), revoked_reason = "
            "'revoked_by_user'"
        )
        options = await self.passkeys.register_begin(registered.auth, context())
        answer = device().create(
            options, credential_id=authenticator.credentials[0].credential_id
        )
        with self.assertRaises(PasskeyExistsError):
            await self.passkeys.register_finish(
                registered.auth, answer, None, context()
            )


@requires_postgres
class RulesTest(PasskeyTestCase):
    """Who may register, and when (Decision 0025)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.alice = await self.make_user("alice")

    async def test_a_user_with_a_passkey_needs_a_passkey_step_up_for_another(self):
        auth = await self.sign_in(self.alice)
        first = device()
        registered = await self.register(auth, first)
        with self.assertRaises(StepUpRequiredError):
            await self.passkeys.register_begin(registered.auth, context())
        stepped = await self.authenticate(registered.auth, first)
        second = await self.register(stepped.session, device(), name="Second")
        self.assertEqual(
            sorted(p.name for p in await self.passkeys.list_passkeys(second.auth)),
            ["Passkey", "Second"],
        )
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.passkey.register", "deny", "step_up_required")], 1
        )

    async def test_a_password_step_up_does_not_count_for_a_second_passkey(self):
        auth = await self.sign_in(self.alice)
        registered = await self.register(auth, device())
        stepped = await self.auth.step_up(
            registered.auth, StepUpEvidence(AuthMethod.PASSWORD, PASSWORD), context()
        )
        with self.assertRaises(StepUpMethodInsufficientError):
            await self.passkeys.register_begin(stepped.session, context())
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.passkey.register", "deny", "step_up_method_insufficient")
            ],
            1,
        )

    async def test_the_step_up_window_is_the_policy_value(self):
        auth = await self.sign_in(self.alice)
        first = device()
        registered = await self.register(auth, first)
        stepped = await self.authenticate(registered.auth, first)
        for seconds, accepted in ((1799, True), (1800, False)):
            with self.subTest(seconds=seconds):
                self.clock.advance(seconds=seconds)
                if accepted:
                    await self.passkeys.register_begin(stepped.session, context())
                else:
                    with self.assertRaises(StepUpRequiredError):
                        await self.passkeys.register_begin(stepped.session, context())
                self.clock.advance(seconds=-seconds)
        # A shorter window of the policy shortens it (5 minutes).
        await self.execute(
            "UPDATE auth_policy SET version = version + 1, stepup_window_minutes = 5"
        )
        self.clock.advance(seconds=301)
        with self.assertRaises(StepUpRequiredError):
            await self.passkeys.register_begin(stepped.session, context())

    async def test_a_user_without_a_passkey_needs_a_recent_authentication(self):
        auth = await self.sign_in(self.alice)
        for seconds, accepted in ((1799, True), (1800, False)):
            with self.subTest(seconds=seconds):
                self.clock.advance(seconds=seconds)
                if accepted:
                    await self.passkeys.register_begin(auth, context())
                else:
                    with self.assertRaises(StepUpRequiredError):
                        await self.passkeys.register_begin(auth, context())
                self.clock.advance(seconds=-seconds)
        # A password step-up renews it.
        self.clock.advance(minutes=45)
        stepped = await self.auth.step_up(
            auth, StepUpEvidence(AuthMethod.PASSWORD, PASSWORD), context()
        )
        await self.passkeys.register_begin(stepped.session, context())

    async def test_the_limit_of_passkeys_per_user(self):
        auth = await self.sign_in(self.alice)
        first = device()
        registered = await self.register(auth, first)
        session = (await self.authenticate(registered.auth, first)).session
        for index in range(MAX_PASSKEYS_PER_USER - 1):
            await self.register(session, device(), name=f"key {index}")
        self.assertEqual(
            len(await self.passkey_rows(self.alice.id)), MAX_PASSKEYS_PER_USER
        )
        with self.assertRaises(PasskeyLimitError):
            await self.passkeys.register_begin(session, context())
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.passkey.register", "deny", "limit_reached")
            ],
            1,
        )
        # The limit also holds when the ceremony was begun before it was reached.
        await self.execute(
            "UPDATE user_passkeys SET revoked_at = now(), revoked_reason = "
            "'revoked_by_user' WHERE name = 'key 0'"
        )
        options = await self.passkeys.register_begin(session, context())
        answer = device().create(options)
        await self.execute(
            "UPDATE user_passkeys SET revoked_at = NULL, revoked_reason = NULL"
        )
        with self.assertRaises(PasskeyLimitError):
            await self.passkeys.register_finish(session, answer, None, context())

    async def test_a_session_that_ended_cannot_register(self):
        auth = await self.sign_in(self.alice)
        options = await self.passkeys.register_begin(auth, context())
        answer = device().create(options)
        await self.execute(
            "UPDATE auth_sessions SET revoked_at = now(), revoked_reason = 'logout'"
        )
        with self.assertRaises(SessionEndedError):
            await self.passkeys.register_finish(auth, answer, None, context())
        self.assertEqual(await self.passkey_rows(), [])

    async def test_a_user_who_is_not_active_any_more_cannot_register(self):
        auth = await self.sign_in(self.alice)
        options = await self.passkeys.register_begin(auth, context())
        answer = device().create(options)
        await self.execute("UPDATE users SET status = 'pending_deletion'")
        with self.assertRaises(SessionEndedError):
            await self.passkeys.register_finish(auth, answer, None, context())
        self.assertEqual(await self.passkey_rows(), [])

    async def test_failed_finishes_count_like_wrong_passwords(self):
        auth = await self.sign_in(self.alice)
        for _ in range(4):
            options = await self.passkeys.register_begin(auth, context())
            with self.assertRaises(PasskeyVerificationError):
                await self.passkeys.register_finish(
                    auth,
                    device().create(options, origin="https://evil.example"),
                    None,
                    context(),
                )
        options = await self.passkeys.register_begin(auth, context())
        with self.assertRaises(
            PasskeyVerificationError
        ):  # the 5th attempt is still judged
            await self.passkeys.register_finish(
                auth,
                device().create(options, origin="https://evil.example"),
                None,
                context(),
            )
        options = await self.passkeys.register_begin(auth, context())
        with self.assertRaises(ThrottledError):
            await self.passkeys.register_finish(
                auth, device().create(options), None, context()
            )
        self.assertEqual(await self.passkey_rows(), [])
        self.assertEqual(
            (await self.audit_summary())[("auth.lockout", "deny", "backoff_started")], 1
        )

    async def test_a_success_forgets_the_failures(self):
        auth = await self.sign_in(self.alice)
        for _ in range(4):
            options = await self.passkeys.register_begin(auth, context())
            with self.assertRaises(PasskeyVerificationError):
                await self.passkeys.register_finish(
                    auth,
                    device().create(options, origin="https://evil.example"),
                    None,
                    context(),
                )
        await self.register(auth, device())
        self.assertEqual(
            await self.query(
                "SELECT * FROM auth_throttles WHERE scope = 'login_account'"
            ),
            [],
        )

    async def test_a_refusal_stands_when_the_audit_cannot_record_it(self):
        from .auth_support import RecordingSink

        sink = RecordingSink()
        services = self.build(self.new_database(), sink=sink)
        auth = (await services.service.login("alice", PASSWORD, context())).session
        options = await services.passkeys.register_begin(auth, context())
        sink.fail = True
        with self.assertRaises(PasskeyVerificationError) as caught:
            await services.passkeys.register_finish(
                auth,
                device().create(options, origin="https://evil.example"),
                None,
                context(),
            )
        self.assertNotIn("audit-secret-detail", str(caught.exception))
        self.assertEqual(await self.passkey_rows(), [])

    async def test_the_database_going_away_is_a_typed_error_and_leaves_nothing(self):
        from unittest.mock import patch

        auth = await self.sign_in(self.alice)
        options = await self.passkeys.register_begin(auth, context())
        answer = device().create(options)
        with patch.object(
            self.passkeys._database,
            "run_abortable",
            side_effect=OSError("secret-detail-023"),
        ):
            with self.assertRaises(AuthUnavailableError):
                await self.passkeys.register_finish(auth, answer, None, context())
        self.assertEqual(await self.passkey_rows(), [])


@requires_postgres
class IndeterminateCommitTest(PasskeyTestCase):
    """A deadline that passes AFTER the commit is not a rollback (PAW-022 lesson 2)."""

    async def test_a_registration_whose_answer_is_lost_is_recoverable(self):
        from paw_backend.auth.models import PasskeyGate
        from paw_backend.db import Database

        owner = await self.make_owner()
        auth = await self.sign_in(owner)
        authenticator = device()
        options = await self.passkeys.register_begin(auth, context())
        answer = authenticator.create(options)
        real = Database.run_abortable

        async def commit_then_lose_the_answer(database, work):
            result = await real(database, work)
            if work.__name__ == "store":
                raise TimeoutError  # the deadline passed after the commit
            return result

        with patch.object(Database, "run_abortable", commit_then_lose_the_answer):
            with self.assertRaises(AuthUnavailableError):
                await self.passkeys.register_finish(auth, answer, None, context())
        # It DID commit: the Passkey exists and the session was opened (its id is
        # new, so the client, which never got the cookie, is signed out) ...
        self.assertEqual(len(await self.passkey_rows(owner.id)), 1)
        self.assertEqual((await self.session_row(auth.record.id)).passkey_gate, "open")
        # ... a retry registers nothing twice (its challenge is spent) ...
        with self.assertRaises(PasskeyChallengeError):
            await self.passkeys.register_finish(auth, answer, None, context())
        self.assertEqual(len(await self.passkey_rows(owner.id)), 1)
        # ... and the user gets back in: the next sign-in waits for the Passkey they
        # now have, and completing it opens the session.
        again = await self.sign_in(owner)
        self.assertIs(again.record.passkey_gate, PasskeyGate.ASSERTION_REQUIRED)
        stepped = await self.authenticate(again, authenticator)
        self.assertIs(stepped.session.record.passkey_gate, PasskeyGate.OPEN)
