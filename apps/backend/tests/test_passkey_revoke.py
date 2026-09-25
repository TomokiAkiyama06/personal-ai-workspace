"""Revoking a Passkey (a device): what ends, what stays, and the races."""

import asyncio
import uuid

from sqlalchemy import text

from paw_backend.auth.errors import (
    InvalidAuthInputError,
    InvalidCredentialsError,
    LastPasskeyError,
    PasskeyNotFoundError,
    SessionEndedError,
    StepUpMethodInsufficientError,
    StepUpRequiredError,
)
from paw_backend.auth.models import AuthMethod
from paw_backend.auth.state import StepUpEvidence

from .auth_support import PASSWORD, requires_postgres
from .passkey_pg_support import (
    ADMIN_PASSWORD,
    OWNER_PASSWORD,
    PasskeyTestCase,
    context,
    device,
)


class RevokeCase(PasskeyTestCase):
    async def user_with_two_passkeys(self, user, password=None):
        """An open session with a fresh passkey step-up and two passkeys (A, B)."""
        auth, first = await self.enrolled_session(user, password)
        stepped = await self.authenticate(auth, first)
        second = device()
        registered = await self.register(stepped.session, second, name="B")
        session = (await self.authenticate(registered.auth, first)).session
        rows = {
            row.name: row.id for row in await self.passkey_rows(user.id)
        }  # "Passkey" is A
        return session, first, second, rows["Passkey"], rows["B"]


@requires_postgres
class RevokeTest(RevokeCase):
    async def test_a_user_revokes_a_device_and_it_is_gone_everywhere(self):
        alice = await self.make_user("alice")
        session, first, second, a_id, b_id = await self.user_with_two_passkeys(alice)
        await self.passkeys.authenticate_begin(session, context())  # an open challenge
        result = await self.passkeys.revoke(session, a_id, context())
        self.assertEqual(
            (result.sessions_ended, result.current_session_ended), (0, False)
        )
        rows = {r.id: r for r in await self.passkey_rows(alice.id)}
        self.assertEqual(
            (rows[a_id].revoked_at, rows[a_id].revoked_reason),
            (self.clock.now, "revoked_by_user"),
        )
        self.assertIsNone(rows[b_id].revoked_at)
        self.assertEqual(
            [p.id for p in await self.passkeys.list_passkeys(session)], [b_id]
        )
        # The open challenge is forgotten with it, and the credential cannot be used.
        self.assertEqual(await self.query("SELECT * FROM passkey_challenges"), [])
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.passkey.revoke", "allow", "revoked")], 1)
        event = [
            r for r in await self.audit_rows() if r.action == "auth.passkey.revoke"
        ][0]
        self.assertEqual(
            (event.actor_id, event.resource_kind, event.resource_id),
            (alice.id, "passkey", a_id),
        )

    async def test_a_revoked_credential_cannot_step_up(self):
        alice = await self.make_user("alice")
        session, first, second, a_id, b_id = await self.user_with_two_passkeys(alice)
        await self.passkeys.revoke(session, a_id, context())
        # The options no longer name it; an authenticator that insists is refused.
        options = await self.passkeys.authenticate_begin(session, context())
        self.assertEqual(len(options["allowCredentials"]), 1)
        # (the step-up was forgotten by the revocation: the user steps up again)
        answer = first.get(options, credential=first.credentials[0])
        with self.assertRaises(InvalidCredentialsError):
            await self.step_up_with(session, answer)
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.passkey.authenticate", "deny", "unknown_credential")
            ],
            1,
        )
        stepped = await self.authenticate(session, second)
        self.assertEqual(stepped.session.record.stepup_method, AuthMethod.PASSKEY)

    async def test_a_step_up_is_needed_and_forgotten_afterwards(self):
        alice = await self.make_user("alice")
        session, first, second, a_id, b_id = await self.user_with_two_passkeys(alice)
        await self.passkeys.revoke(session, a_id, context())
        # Every passkey step-up of the user's sessions is gone, this one's included.
        row = await self.session_row(session.record.id)
        self.assertEqual((row.stepup_at, row.stepup_method), (None, None))
        with self.assertRaises(StepUpRequiredError):
            await self.passkeys.revoke(session, b_id, context())
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.passkey.revoke", "deny", "step_up_required")
            ],
            1,
        )

    async def test_a_user_whose_role_does_not_require_a_passkey_may_use_any_step_up(
        self,
    ):
        alice = await self.make_user("alice")
        session, first, second, a_id, b_id = await self.user_with_two_passkeys(alice)
        await self.execute(
            "UPDATE auth_sessions SET stepup_at = NULL, stepup_method = NULL"
        )
        stepped = await self.auth.step_up(
            session, StepUpEvidence(AuthMethod.PASSWORD, PASSWORD), context()
        )
        await self.passkeys.revoke(stepped.session, a_id, context())
        # ... and may remove the last one too: nothing requires it.
        await self.execute(
            "UPDATE auth_sessions SET stepup_at = NULL, stepup_method = NULL"
        )
        stepped = await self.auth.step_up(
            stepped.session, StepUpEvidence(AuthMethod.PASSWORD, PASSWORD), context()
        )
        await self.passkeys.revoke(stepped.session, b_id, context())
        self.assertEqual(await self.passkeys.list_passkeys(stepped.session), [])

    async def test_a_required_role_needs_a_passkey_step_up_a_password_one_is_refused(
        self,
    ):
        owner = await self.make_owner()
        session, first, second, a_id, b_id = await self.user_with_two_passkeys(owner)
        await self.execute(
            "UPDATE auth_sessions SET stepup_at = NULL, stepup_method = NULL"
        )
        stepped = await self.auth.step_up(
            session, StepUpEvidence(AuthMethod.PASSWORD, OWNER_PASSWORD), context()
        )
        with self.assertRaises(StepUpMethodInsufficientError):
            await self.passkeys.revoke(stepped.session, a_id, context())
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.passkey.revoke", "deny", "step_up_method_insufficient")
            ],
            1,
        )
        self.assertEqual(
            [r.revoked_at for r in await self.passkey_rows(owner.id)], [None, None]
        )

    async def test_the_last_passkey_of_a_required_role_cannot_be_revoked(self):
        admin = await self.make_admin()
        auth, only = await self.enrolled_session(admin)
        session = (await self.authenticate(auth, only)).session
        (row,) = await self.passkey_rows(admin.id)
        with self.assertRaises(LastPasskeyError):
            await self.passkeys.revoke(session, row.id, context())
        # Rolled back completely: still active, sessions and step-up untouched.
        (row,) = await self.passkey_rows(admin.id)
        self.assertIsNone(row.revoked_at)
        session_row = await self.session_row(session.record.id)
        self.assertIsNone(session_row.revoked_at)
        self.assertEqual(session_row.stepup_method, "passkey")
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.passkey.revoke", "deny", "last_passkey")
            ],
            1,
        )
        # With a replacement registered, the old one can go.
        second = device()
        await self.register(session, second)
        await self.passkeys.revoke(session, row.id, context())

    async def test_an_unknown_or_foreign_or_finished_passkey_is_not_found(self):
        alice = await self.make_user("alice")
        bob = await self.make_user("bob")
        session, first, second, a_id, b_id = await self.user_with_two_passkeys(alice)
        bobs_session, _ = await self.enrolled_session(bob)
        bobs_id = (await self.passkey_rows(bob.id))[0].id
        await self.passkeys.revoke(session, a_id, context())
        session = (await self.authenticate(session, second)).session
        for label, target in (
            ("unknown", uuid.uuid4()),
            ("another user's", bobs_id),
            ("already revoked", a_id),
        ):
            with self.subTest(label):
                with self.assertRaises(PasskeyNotFoundError):
                    await self.passkeys.revoke(session, target, context())
        self.assertIsNone((await self.passkey_rows(bob.id))[0].revoked_at)
        self.assertEqual(
            (await self.audit_summary())[("auth.passkey.revoke", "deny", "not_found")],
            3,
        )
        # Bob's session is untouched.
        self.assertIsNone((await self.session_row(bobs_session.record.id)).revoked_at)

    async def test_the_argument_is_a_uuid(self):
        alice = await self.make_user("alice")
        session, *_ = await self.user_with_two_passkeys(alice)
        for bad in (None, "x", str(uuid.uuid4()), 5, b"x"):
            with self.subTest(value=repr(bad)[:20]):
                with self.assertRaises(InvalidAuthInputError):
                    await self.passkeys.revoke(session, bad, context())


@requires_postgres
class SessionsEndTest(RevokeCase):
    """The sessions a Passkey opened end with it; the rest are only forgotten."""

    async def test_the_sessions_a_passkey_opened_end_with_it(self):
        admin = await self.make_admin()
        # S1 enrolled A (bound to A) and added B.
        s1, a_device = await self.enrolled_session(admin)
        s1 = (await self.authenticate(s1, a_device)).session
        b_device = device()
        registered = await self.register(s1, b_device, name="B")
        s1 = registered.auth
        rows = {r.name: r.id for r in await self.passkey_rows(admin.id)}
        a_id, b_id = rows["Passkey"], rows["B"]
        # S2 signs in and is opened by B; S3 by A.
        s2 = (
            await self.authenticate(
                await self.sign_in(admin, password=ADMIN_PASSWORD), b_device
            )
        ).session
        s3 = (
            await self.authenticate(
                await self.sign_in(admin, password=ADMIN_PASSWORD), a_device
            )
        ).session
        self.assertEqual(
            [(await self.session_row(s.record.id)).passkey_id for s in (s1, s2, s3)],
            [a_id, b_id, a_id],
        )
        # S2 revokes A (after a fresh step-up with B).
        s2 = (await self.authenticate(s2, b_device)).session
        result = await self.passkeys.revoke(s2, a_id, context())
        self.assertEqual(
            (result.sessions_ended, result.current_session_ended), (2, False)
        )
        ended = {s.record.id: await self.session_row(s.record.id) for s in (s1, s2, s3)}
        self.assertEqual(
            [(r.revoked_reason) for r in (ended[s1.record.id], ended[s3.record.id])],
            ["passkey_revoked", "passkey_revoked"],
        )
        self.assertIsNone(ended[s2.record.id].revoked_at)
        # The survivor's own step-up was forgotten too.
        self.assertIsNone(ended[s2.record.id].stepup_method)

    async def test_revoking_the_passkey_that_signed_this_session_in_signs_it_out(self):
        admin = await self.make_admin()
        s1, a_device = await self.enrolled_session(admin)
        s1 = (await self.authenticate(s1, a_device)).session
        b_device = device()
        await self.register(s1, b_device, name="B")
        (a_id,) = [
            r.id for r in await self.passkey_rows(admin.id) if r.name == "Passkey"
        ]
        # A session opened by A, now stepped up with B, revokes A.
        s2 = (
            await self.authenticate(
                await self.sign_in(admin, password=ADMIN_PASSWORD), a_device
            )
        ).session
        s2 = (await self.authenticate(s2, b_device)).session
        result = await self.passkeys.revoke(s2, a_id, context())
        self.assertEqual(
            (result.sessions_ended, result.current_session_ended), (2, True)
        )
        self.assertEqual(
            (await self.session_row(s2.record.id)).revoked_reason, "passkey_revoked"
        )

    async def test_every_passkey_step_up_of_the_user_is_forgotten_and_others_stay(self):
        alice = await self.make_user("alice")
        s1, first, second, a_id, b_id = await self.user_with_two_passkeys(alice)
        other = await self.sign_in(alice)  # a second device, passkey step-up
        other = (await self.authenticate(other, second)).session
        password_only = await self.sign_in(alice)  # a third, password step-up only
        password_only = (
            await self.auth.step_up(
                password_only, StepUpEvidence(AuthMethod.PASSWORD, PASSWORD), context()
            )
        ).session
        bob = await self.make_user("bob")
        bobs, bobs_device = await self.enrolled_session(bob)
        bobs = (await self.authenticate(bobs, bobs_device)).session
        await self.passkeys.revoke(s1, a_id, context())
        methods = {
            name: (await self.session_row(s.record.id)).stepup_method
            for name, s in (
                ("s1", s1),
                ("other", other),
                ("password", password_only),
                ("bob", bobs),
            )
        }
        self.assertEqual(
            methods,
            {"s1": None, "other": None, "password": "password", "bob": "passkey"},
        )

    async def test_a_session_that_ended_with_its_passkey_no_longer_authenticates(self):
        admin = await self.make_admin()
        s1, a_device = await self.enrolled_session(admin)
        stepped = await self.authenticate(s1, a_device)  # the cookie S1's client holds
        b_device = device()
        await self.register(stepped.session, b_device, name="B")
        (a_id,) = [
            r.id for r in await self.passkey_rows(admin.id) if r.name == "Passkey"
        ]
        signed_in = await self.sign_in(admin, password=ADMIN_PASSWORD)
        s2 = (await self.authenticate(signed_in, b_device)).session
        async with self.service_database.session() as session:
            live = await self.services.sessions.authenticate(session, stepped.token)
        self.assertEqual(live.record.id, stepped.session.record.id)
        await self.passkeys.revoke(s2, a_id, context())
        async with self.service_database.session() as session:
            gone = await self.services.sessions.authenticate(session, stepped.token)
        self.assertIsNone(gone)


@requires_postgres
class RaceTest(RevokeCase):
    async def test_two_revocations_of_one_passkey_end_it_once(self):
        alice = await self.make_user("alice")
        session, first, second, a_id, b_id = await self.user_with_two_passkeys(alice)

        async def revoke(services, _index):
            return await services.passkeys.revoke(session, a_id, context())

        results = await self.gather_on_own_engines(4, revoke)
        wins = [r for r in results if not isinstance(r, BaseException)]
        # A loser finds the step-up forgotten (the winner's revocation clears every
        # passkey step-up of the user) or the passkey gone: refused either way.
        losers = [type(r) for r in results if isinstance(r, BaseException)]
        self.assertEqual(len(wins), 1, results)
        self.assertTrue(
            all(kind in (StepUpRequiredError, PasskeyNotFoundError) for kind in losers),
            results,
        )
        self.assertEqual(
            (await self.audit_summary())[("auth.passkey.revoke", "allow", "revoked")], 1
        )
        rows = {r.id: r for r in await self.passkey_rows(alice.id)}
        self.assertIsNotNone(rows[a_id].revoked_at)
        self.assertIsNone(rows[b_id].revoked_at)

    async def test_two_devices_cannot_both_be_revoked_from_a_required_role(self):
        owner = await self.make_owner()
        session, first, second, a_id, b_id = await self.user_with_two_passkeys(owner)
        targets = (a_id, b_id)

        async def revoke(services, index):
            return await services.passkeys.revoke(session, targets[index], context())

        results = await self.gather_on_own_engines(2, revoke)
        wins = [r for r in results if not isinstance(r, BaseException)]
        self.assertEqual(len(wins), 1, results)
        (loser,) = [r for r in results if isinstance(r, BaseException)]
        # (the winner may have ended this very session, which the loser then finds)
        self.assertIsInstance(
            loser, StepUpRequiredError | LastPasskeyError | SessionEndedError
        )
        rows = await self.passkey_rows(owner.id)
        self.assertEqual(len([r for r in rows if r.revoked_at is None]), 1)

    async def test_a_registration_and_a_revocation_of_the_last_one_serialise(self):
        owner = await self.make_owner()
        auth, only = await self.enrolled_session(owner)
        session = (await self.authenticate(auth, only)).session
        (row,) = await self.passkey_rows(owner.id)
        newcomer = device()

        async def work(services, index):
            if index == 0:
                return await services.passkeys.revoke(session, row.id, context())
            options = await services.passkeys.register_begin(session, context())
            answer = newcomer.create(options)
            return await services.passkeys.register_finish(
                session, answer, "new", context()
            )

        results = await self.gather_on_own_engines(2, work)
        active = [r for r in await self.passkey_rows(owner.id) if r.revoked_at is None]
        # Whatever the order, a required role is never left without a passkey.
        self.assertGreaterEqual(len(active), 1, results)

    async def test_a_revocation_and_a_step_up_of_its_session_do_not_wait_for_each_other(
        self,
    ):
        """The credential is locked before any session, by both."""
        alice = await self.make_user("alice")
        session, first, second, a_id, b_id = await self.user_with_two_passkeys(alice)
        async with self.database.session() as stepping:
            # A step-up in flight: it holds the credential FOR SHARE and will next
            # update its session.
            await stepping.execute(
                text("SELECT id FROM user_passkeys WHERE id = :p FOR SHARE"),
                {"p": a_id},
            )
            revoke = asyncio.create_task(self.passkeys.revoke(session, a_id, context()))
            await asyncio.sleep(0.5)
            self.assertFalse(revoke.done())  # waiting for the credential
            # If the revocation already held the session (a lock the step-up needs)
            # this would be a deadlock; it must simply go through.
            await stepping.execute(
                text("UPDATE auth_sessions SET rotated_at = now() WHERE id = :s"),
                {"s": session.record.id},
            )
            await stepping.commit()
        result = await asyncio.wait_for(revoke, 10)
        self.assertEqual(result.sessions_ended, 0)
        self.assertIsNotNone(
            {r.id: r for r in await self.passkey_rows(alice.id)}[a_id].revoked_at
        )
