"""The Passkey requirement is enforced at sign-in (PAW-023, PostgreSQL).

A role whose requirement is ``required`` signs in with its password into a
RESTRICTED session: it may only register a Passkey (none registered yet) or complete
a Passkey authentication (one is). Never a dead end: the password login always works
and ``owner-recover`` is untouched. The policy applies to new sessions only.
"""

import asyncio

from sqlalchemy import text

from paw_backend.auth.errors import (
    PasskeyRequiredError,
    StepUpRequiredError,
)
from paw_backend.auth.models import PasskeyGate
from paw_backend.authz.audit import PostgresAuditSink
from paw_backend.identity.operator import ROOT_UID, OwnerOperator

from .auth_support import requires_postgres
from .identity_support import running_as
from .passkey_pg_support import (
    OWNER_PASSWORD,
    PasskeyTestCase,
    context,
    device,
)


def set_policy_sql(**columns) -> str:
    sets = ", ".join(f"{name} = '{value}'" for name, value in columns.items())
    return f"UPDATE auth_policy SET version = version + 1, {sets}"


@requires_postgres
class SignInGateTest(PasskeyTestCase):
    async def gate_and_reason(self, user, *, service=None):
        """The gate a fresh password sign-in gets and the reason of its audit row."""
        before = len(await self.audit_rows())
        result = await (service or self.auth).login(
            user.login_name, user.password, context()
        )
        rows = (await self.audit_rows())[before:]
        (event,) = [r for r in rows if r.action == "auth.login"]
        stored = (await self.session_row(result.session.record.id)).passkey_gate
        self.assertEqual(stored, result.session.record.passkey_gate.value)
        return result.session.record.passkey_gate, event.reason

    async def test_the_gate_of_every_role_and_state(self):
        owner = await self.make_owner()
        admin = await self.make_admin()
        alice = await self.make_user("alice")
        # The defaults are the requirements': Owner and Admin required, User optional.
        self.assertEqual(
            await self.gate_and_reason(owner),
            (PasskeyGate.ENROLLMENT_REQUIRED, "authenticated_enrollment_only"),
        )
        self.assertEqual(
            await self.gate_and_reason(admin),
            (PasskeyGate.ENROLLMENT_REQUIRED, "authenticated_enrollment_only"),
        )
        self.assertEqual(
            await self.gate_and_reason(alice), (PasskeyGate.OPEN, "authenticated")
        )
        # With a Passkey registered the restricted session waits for it instead.
        for user in (owner, admin):
            auth = await self.sign_in(user)
            await self.register(auth, device())
            self.assertEqual(
                await self.gate_and_reason(user),
                (PasskeyGate.ASSERTION_REQUIRED, "authenticated_passkey_pending"),
                user.login_name,
            )
        # A User with a Passkey is not restricted: the requirement is optional.
        await self.register(await self.sign_in(alice), device())
        self.assertEqual(
            await self.gate_and_reason(alice), (PasskeyGate.OPEN, "authenticated")
        )

    async def test_the_policy_decides_not_the_users_column(self):
        owner = await self.make_owner()
        alice = await self.make_user("alice")
        await self.execute(
            set_policy_sql(passkey_owner="optional", passkey_user="required")
        )
        # The Owner's ``users.passkey_required`` is still true (a CHECK keeps it so);
        # the policy says optional, and the policy is what sign-in reads.
        self.assertTrue(
            await self.scalar(
                "SELECT passkey_required FROM users WHERE id = :i", i=owner.id
            )
        )
        self.assertEqual(
            await self.gate_and_reason(owner), (PasskeyGate.OPEN, "authenticated")
        )
        # A User's column is false, the policy says required: restricted.
        self.assertFalse(
            await self.scalar(
                "SELECT passkey_required FROM users WHERE id = :i", i=alice.id
            )
        )
        self.assertEqual(
            await self.gate_and_reason(alice),
            (PasskeyGate.ENROLLMENT_REQUIRED, "authenticated_enrollment_only"),
        )

    async def test_nothing_is_enforced_where_passkeys_are_not_configured(self):
        owner = await self.make_owner()
        plain = self.build(
            self.service_database,
            settings=self.settings.model_copy(
                update={"passkey_rp_id": None, "passkey_origins": []}
            ),
        )
        # An Owner who could never register a Passkey is not held in an enrolment.
        self.assertEqual(
            await self.gate_and_reason(owner, service=plain.service),
            (PasskeyGate.OPEN, "authenticated"),
        )
        view = await plain.service.view(
            (await plain.service.login("boss", OWNER_PASSWORD, context())).session
        )
        self.assertFalse(view.auth.passkey.available)
        self.assertEqual(view.auth.passkey.requirement.value, "required")

    async def test_the_session_says_what_it_needs(self):
        owner = await self.make_owner()
        auth = await self.sign_in(owner)
        state = (await self.auth.view(auth)).auth.passkey
        self.assertEqual(
            (
                state.gate,
                state.next_step,
                state.available,
                state.enrolled,
                state.enrollment_required,
            ),
            (PasskeyGate.ENROLLMENT_REQUIRED, "register", True, False, True),
        )
        registered = await self.register(auth, device())
        again = await self.sign_in(owner)
        state = (await self.auth.view(again)).auth.passkey
        self.assertEqual(
            (state.gate, state.next_step, state.enrolled, state.enrollment_required),
            (PasskeyGate.ASSERTION_REQUIRED, "authenticate", True, False),
        )
        opened = (await self.auth.view(registered.auth)).auth.passkey
        self.assertEqual((opened.gate, opened.next_step), (PasskeyGate.OPEN, None))

    async def test_a_policy_change_never_touches_a_running_session(self):
        owner = await self.make_owner()
        await self.execute(set_policy_sql(passkey_owner="optional"))
        running = await self.sign_in(owner)
        self.assertIs(running.record.passkey_gate, PasskeyGate.OPEN)
        await self.execute(set_policy_sql(passkey_owner="required"))
        row = await self.session_row(running.record.id)
        self.assertEqual((row.passkey_gate, row.revoked_at), ("open", None))
        # ... and a NEW sign-in is restricted.
        self.assertIs(
            (await self.sign_in(owner)).record.passkey_gate,
            PasskeyGate.ENROLLMENT_REQUIRED,
        )

    async def test_a_session_from_before_the_feature_is_open(self):
        owner = await self.make_owner()
        auth = await self.sign_in(owner)
        # A row as the column's default would make it (an old session).
        await self.execute("UPDATE auth_sessions SET passkey_gate = DEFAULT")
        self.assertEqual((await self.session_row(auth.record.id)).passkey_gate, "open")


@requires_postgres
class RestrictedSessionTest(PasskeyTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.owner = await self.make_owner()

    async def test_an_enrolment_session_may_register_and_authenticate_is_impossible(
        self,
    ):
        from paw_backend.auth.errors import NoPasskeyError

        auth = await self.sign_in(self.owner)
        await self.passkeys.register_begin(auth, context())  # allowed
        with self.assertRaises(NoPasskeyError):  # nothing to authenticate with
            await self.passkeys.authenticate_begin(auth, context())

    async def test_registering_opens_the_session_binds_it_and_is_not_a_step_up(self):
        auth = await self.sign_in(self.owner)
        authenticator = device()
        registered = await self.register(auth, authenticator)
        # The session's id was rotated (its authority changed) ...
        self.assertIsNotNone(registered.login)
        row = await self.session_row(auth.record.id)
        self.assertEqual(
            (row.passkey_gate, row.passkey_id, row.stepup_at, row.stepup_method),
            ("open", registered.passkey_id, None, None),
        )
        self.assertNotEqual(bytes(row.token_hash), auth.token_hash)
        self.assertEqual(row.rotated_at, self.clock.now)
        # ... and enrolling is NOT a Passkey step-up: a credential a password vouched
        # for proves nothing yet, so sensitive operations still ask for one.
        self.assertIs(registered.auth.record.passkey_gate, PasskeyGate.OPEN)
        self.assertIsNone(registered.auth.record.stepup_at)
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.passkey.register", "allow", "registered")], 1)

    async def test_an_assertion_session_may_only_authenticate(self):
        auth = await self.sign_in(self.owner)
        authenticator = device()
        await self.register(auth, authenticator)
        again = await self.sign_in(self.owner)
        self.assertIs(again.record.passkey_gate, PasskeyGate.ASSERTION_REQUIRED)
        # It may not add a credential, whatever else it has.
        with self.assertRaises(PasskeyRequiredError):
            await self.passkeys.register_begin(again, context())
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.passkey.register", "deny", "gate_not_allowed")], 1
        )
        # It authenticates: the gate opens, the id rotates, a step-up is recorded and
        # the session is bound to the Passkey that opened it.
        stepped = await self.authenticate(again, authenticator)
        row = await self.session_row(again.record.id)
        passkey = (await self.passkey_rows(self.owner.id))[0]
        self.assertEqual(
            (row.passkey_gate, row.passkey_id, row.stepup_method),
            ("open", passkey.id, "passkey"),
        )
        self.assertIs(stepped.session.record.passkey_gate, PasskeyGate.OPEN)
        self.assertEqual(stepped.session.record.passkey_id, passkey.id)

    async def test_an_open_sessions_step_up_does_not_bind_it(self):
        auth, authenticator = await self.enrolled_session(self.owner)
        opened_by = (await self.session_row(auth.record.id)).passkey_id
        second = device()
        stepped = await self.authenticate(auth, authenticator)
        registered = await self.register(stepped.session, second)
        # The step-up of an open session leaves its binding alone.
        again = await self.authenticate(registered.auth, second)
        self.assertEqual(again.session.record.passkey_id, opened_by)

    async def test_a_session_whose_passkeys_are_gone_may_enrol_again(self):
        auth = await self.sign_in(self.owner)
        await self.register(auth, device())
        waiting = await self.sign_in(self.owner)
        self.assertIs(waiting.record.passkey_gate, PasskeyGate.ASSERTION_REQUIRED)
        # The Passkey is lost (revoked) after this session started waiting for it.
        await self.execute(
            "UPDATE user_passkeys SET revoked_at = now(), revoked_reason = "
            "'revoked_by_user'"
        )
        registered = await self.register(waiting, device())
        self.assertIs(registered.auth.record.passkey_gate, PasskeyGate.OPEN)

    async def test_a_restricted_session_cannot_revoke(self):
        auth = await self.sign_in(self.owner)
        registered = await self.register(auth, device())
        again = await self.sign_in(self.owner)
        with self.assertRaises(PasskeyRequiredError):
            await self.passkeys.revoke(again, registered.passkey_id, context())
        self.assertEqual(len(await self.passkey_rows(self.owner.id)), 1)
        self.assertIsNone((await self.passkey_rows(self.owner.id))[0].revoked_at)

    async def test_an_owner_is_never_without_a_way_in(self):
        """Password sign-in works in every state: the requirement never locks out."""
        for _ in range(3):
            auth = await self.sign_in(self.owner)
            self.assertIs(auth.record.passkey_gate, PasskeyGate.ENROLLMENT_REQUIRED)
        registered = await self.register(auth, device())
        # Every Passkey lost: the next sign-in is an enrolment, not a dead end.
        await self.execute(
            "UPDATE user_passkeys SET revoked_at = now(), revoked_reason = "
            "'revoked_by_user'"
        )
        again = await self.sign_in(self.owner)
        self.assertIs(again.record.passkey_gate, PasskeyGate.ENROLLMENT_REQUIRED)
        await self.register(again, device())
        self.assertIsNotNone(registered.login)  # the first enrolment opened its session
        active = [r for r in await self.passkey_rows(self.owner.id) if not r.revoked_at]
        self.assertEqual(len(active), 1)


@requires_postgres
class RecoveryTest(PasskeyTestCase):
    """``owner-recover`` invalidates the Passkeys (Decision 0005, point 7)."""

    async def asyncSetUp(self):
        self.enterContext(running_as(ROOT_UID))
        await super().asyncSetUp()
        self.operator = OwnerOperator(
            self.database, PostgresAuditSink(self.database), clock=self.clock
        )

    async def test_a_recovery_revokes_every_passkey_and_the_owner_can_enrol_again(self):
        setup = await self.operator.setup_owner("boss")
        await self.auth.redeem_owner_token(setup.token, OWNER_PASSWORD, context())
        auth = await self.auth.login("boss", OWNER_PASSWORD, context())
        first, second = device(), device()
        registered = await self.register(auth.session, first)
        stepped = await self.authenticate(registered.auth, first)
        await self.register(stepped.session, second)
        # A challenge in flight too.
        await self.passkeys.authenticate_begin(stepped.session, context())
        self.assertEqual(len(await self.query("SELECT * FROM passkey_challenges")), 1)

        recovery = await self.operator.recover_owner()
        result = await self.auth.redeem_owner_token(
            recovery.token, "a passphrase chosen after recovery", context()
        )

        rows = await self.passkey_rows(setup.user_id)
        self.assertEqual(
            sorted((r.revoked_reason, r.revoked_at is not None) for r in rows),
            [("recovery", True), ("recovery", True)],
        )
        self.assertEqual(await self.query("SELECT * FROM passkey_challenges"), [])
        self.assertEqual(
            len(
                await self.query("SELECT * FROM auth_sessions WHERE revoked_at IS NULL")
            ),
            0,
        )
        # Not a dead end: the new password signs in, into an enrolment session.
        again = await self.auth.login(
            "boss", "a passphrase chosen after recovery", context()
        )
        self.assertIs(
            again.session.record.passkey_gate, PasskeyGate.ENROLLMENT_REQUIRED
        )
        await self.register(again.session, device())
        self.assertTrue(result.passkey_required)

    async def test_a_recovery_and_a_step_up_in_flight_do_not_wait_for_each_other(self):
        """Passkeys are revoked before sessions are ended: no lock cycle."""
        setup = await self.operator.setup_owner("boss")
        await self.auth.redeem_owner_token(setup.token, OWNER_PASSWORD, context())
        auth = await self.auth.login("boss", OWNER_PASSWORD, context())
        await self.register(auth.session, device())
        recovery = await self.operator.recover_owner()
        async with self.database.session() as stepping:
            await stepping.execute(text("SELECT id FROM user_passkeys FOR SHARE"))
            redeem = asyncio.create_task(
                self.auth.redeem_owner_token(
                    recovery.token, "a passphrase chosen after recovery", context()
                )
            )
            await asyncio.sleep(0.7)
            self.assertFalse(redeem.done())  # waiting for the credential
            await stepping.execute(text("UPDATE auth_sessions SET rotated_at = now()"))
            await stepping.commit()
        result = await asyncio.wait_for(redeem, 15)
        self.assertEqual(result.purpose.value, "recovery")
        self.assertEqual(
            len(
                await self.query("SELECT * FROM auth_sessions WHERE revoked_at IS NULL")
            ),
            0,
        )

    async def test_the_passkeys_and_the_token_end_together_or_not_at_all(self):
        setup = await self.operator.setup_owner("boss")
        await self.auth.redeem_owner_token(setup.token, OWNER_PASSWORD, context())
        auth = await self.auth.login("boss", OWNER_PASSWORD, context())
        await self.register(auth.session, device())
        recovery = await self.operator.recover_owner()
        # A password that is the login name is refused only after the token is read:
        # everything rolls back, the Passkey included.
        from paw_backend.auth.errors import PasswordPolicyError

        with self.assertRaises(PasswordPolicyError):
            await self.auth.redeem_owner_token(recovery.token, "boss" * 5, context())
        rows = await self.passkey_rows(setup.user_id)
        self.assertEqual([r.revoked_at for r in rows], [None])
        self.assertEqual(
            len(
                await self.query("SELECT * FROM auth_sessions WHERE revoked_at IS NULL")
            ),
            1,
        )


@requires_postgres
class GateOperationsTest(PasskeyTestCase):
    async def test_a_sensitive_operation_asks_for_a_step_up_after_enrolling(self):
        owner = await self.make_owner()
        alice = await self.make_user("alice")
        auth, authenticator = await self.enrolled_session(owner)
        from paw_backend.authz import Principal, SystemRole

        actor = Principal(owner.id, SystemRole.OWNER)
        with self.assertRaises(StepUpRequiredError):
            await self.auth.unlock_account(
                actor, alice.id, context(), session_id=auth.record.id
            )
        stepped = await self.authenticate(auth, authenticator)
        await self.auth.unlock_account(
            actor, alice.id, context(), session_id=stepped.session.record.id
        )
        # Not for long: the window is the policy's.
        self.clock.advance(minutes=31)
        with self.assertRaises(StepUpRequiredError):
            await self.auth.unlock_account(
                actor, alice.id, context(), session_id=stepped.session.record.id
            )
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.unlock", "allow", "unlocked")], 1)
        self.assertEqual(summary[("auth.unlock", "deny", "step_up_required")], 2)


@requires_postgres
class GateRaceTest(PasskeyTestCase):
    async def test_a_sign_in_counts_the_passkeys_under_the_lock_a_registration_holds(
        self,
    ):
        """The gate is decided on the count the sign-in reads while it holds the row."""
        owner = await self.make_owner()
        async with self.database.session() as registration:
            # A registration in flight: it holds the user's row and is about to add one.
            await registration.execute(
                text("SELECT id FROM users WHERE id = :i FOR UPDATE"), {"i": owner.id}
            )
            sign_in = asyncio.create_task(
                self.auth.login("boss", OWNER_PASSWORD, context())
            )
            await asyncio.sleep(0.5)
            self.assertFalse(sign_in.done())  # waiting for the row, not deciding
            await registration.execute(
                text(
                    "INSERT INTO user_passkeys (id, user_id, credential_id, "
                    "public_key, sign_count, name, backup_eligible, backed_up, "
                    "created_at) VALUES (gen_random_uuid(), :u, :c, :k, 0, 'p', "
                    "false, false, now())"
                ),
                {"u": owner.id, "c": b"c" * 16, "k": b"k" * 16},
            )
            await registration.commit()
        result = await sign_in
        self.assertIs(
            result.session.record.passkey_gate, PasskeyGate.ASSERTION_REQUIRED
        )

    async def test_two_sign_ins_and_a_registration_never_leave_an_inconsistent_gate(
        self,
    ):
        owner = await self.make_owner()
        first = await self.sign_in(owner)
        newcomer = device()

        async def work(services, index):
            if index == 0:
                return await services.passkeys.register_finish(
                    first,
                    newcomer.create(
                        await services.passkeys.register_begin(first, context())
                    ),
                    None,
                    context(),
                )
            return await services.service.login("boss", OWNER_PASSWORD, context())

        results = await self.gather_on_own_engines(3, work)
        self.assertFalse([r for r in results if isinstance(r, BaseException)], results)
        rows = await self.query(
            "SELECT passkey_gate FROM auth_sessions WHERE revoked_at IS NULL"
        )
        passkeys = await self.passkey_rows(owner.id)
        self.assertEqual(len(passkeys), 1)
        # Each sign-in that ran after the Passkey existed waits for it; each that ran
        # before is an enrolment (which can still register): both are valid, and
        # nothing is ever "open" without a Passkey having opened it.
        gates = sorted(row.passkey_gate for row in rows)
        self.assertTrue(
            set(gates) <= {"enrollment_required", "assertion_required", "open"}, gates
        )
        opened = [
            r
            for r in await self.query("SELECT * FROM auth_sessions")
            if r.passkey_gate == "open"
        ]
        self.assertTrue(all(r.passkey_id == passkeys[0].id for r in opened))
