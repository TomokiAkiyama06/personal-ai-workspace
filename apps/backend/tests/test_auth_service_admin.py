"""An administrator's unlock and the Owner's policy (PAW-022, PostgreSQL)."""

import uuid
from datetime import timedelta

from paw_backend.auth import tokens
from paw_backend.auth.context import RequestContext
from paw_backend.auth.errors import (
    AccountNotFoundError,
    AuthPermissionError,
    InvalidAuthInputError,
    InvalidCredentialsError,
    PolicyVersionConflictError,
    StepUpMethodInsufficientError,
    StepUpRequiredError,
    ThrottledError,
)
from paw_backend.auth.models import AuthMethod, PasskeyRequirement
from paw_backend.auth.service import AuthService
from paw_backend.auth.state import StepUpEvidence
from paw_backend.authz import Principal, SystemRole

from .auth_support import PASSWORD, T0, PostgresAuthTestCase, requires_postgres

SOURCE = "203.0.113.7"
OTHER = "198.51.100.4"


class FakePasskeyVerifier:
    """Stands in for PAW-023's verifier: accepts nothing by itself."""

    method = AuthMethod.PASSKEY

    async def verify(self, user_id, login_name, evidence) -> bool:
        return False


def context(source: str = SOURCE) -> RequestContext:
    return RequestContext(uuid.uuid4(), tokens.source_bucket(source))


class AdminTestCase(PostgresAuthTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.owner = await self.make_user(
            "boss", role="owner", password="owner passphrase"
        )
        self.admin = await self.make_user(
            "admin-one", role="admin", password="admin passphrase"
        )
        self.other_admin = await self.make_user(
            "admin-two", role="admin", password="admin passphrase"
        )
        self.alice = await self.make_user("alice")

    def principal(self, user) -> Principal:
        return Principal(user.id, SystemRole(user.role))

    async def open_session(self, user, password, *, step_up=True) -> uuid.UUID:
        """A signed-in session of ``user``, with a fresh Passkey step-up by default.

        Unlocking an account is a sensitive operation (PAW-023): it needs a Passkey
        step-up of the actor's own session. The step-up is written by the fixture
        here (the ceremony itself is tested in ``test_passkey_*``).
        """
        logged = await self.auth.login(user.login_name, password, context())
        if step_up:
            await self.fake_passkey_step_up(logged.session.record.id)
        return logged.session.record.id

    async def lock(self, name, password="wrong wrong wrong", times=5):
        for _ in range(times):
            with self.assertRaises(InvalidCredentialsError):
                await self.auth.login(name, password, context(OTHER))

    async def is_locked(self, name, password) -> bool:
        try:
            await self.auth.login(name, password, context())
        except ThrottledError:
            return True
        return False


@requires_postgres
class UnlockTest(AdminTestCase):
    async def test_an_admin_can_unlock_a_user(self):
        session_id = await self.open_session(self.admin, "admin passphrase")
        await self.lock("alice")
        self.assertTrue(await self.is_locked("alice", PASSWORD))
        await self.auth.unlock_account(
            self.principal(self.admin), self.alice.id, context(), session_id=session_id
        )
        self.assertFalse(await self.is_locked("alice", PASSWORD))
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.unlock", "allow", "unlocked")], 1)
        row = [r for r in await self.audit_rows() if r.action == "auth.unlock"][0]
        self.assertEqual(
            (row.actor_id, row.actor_role, row.resource_kind, row.resource_id),
            (self.admin.id, "admin", "user", self.alice.id),
        )

    async def test_the_owner_can_unlock_anyone_an_admin_included(self):
        session_id = await self.open_session(self.owner, "owner passphrase")
        await self.lock("admin-one")
        await self.lock("boss")
        for target in (self.admin, self.owner):
            await self.auth.unlock_account(
                self.principal(self.owner), target.id, context(), session_id=session_id
            )
        self.assertFalse(await self.is_locked("admin-one", "admin passphrase"))
        self.assertFalse(await self.is_locked("boss", "owner passphrase"))

    async def test_an_admin_cannot_unlock_an_admin_or_the_owner(self):
        session_id = await self.open_session(self.admin, "admin passphrase")
        await self.lock("admin-two")
        await self.lock("boss")
        for target in (self.other_admin, self.owner, self.admin):
            with self.subTest(target=target.login_name):
                with self.assertRaises(AuthPermissionError):
                    await self.auth.unlock_account(
                        self.principal(self.admin),
                        target.id,
                        context(),
                        session_id=session_id,
                    )
        self.assertTrue(await self.is_locked("admin-two", "admin passphrase"))
        self.assertTrue(await self.is_locked("boss", "owner passphrase"))
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.unlock", "deny", "role_not_allowed")], 3)
        self.assertNotIn(("auth.unlock", "allow", "unlocked"), summary)

    async def test_a_user_cannot_unlock_anyone_not_even_themselves(self):
        await self.lock("alice")
        for target in (self.alice, self.admin):
            with self.assertRaises(AuthPermissionError):
                await self.auth.unlock_account(
                    self.principal(self.alice),
                    target.id,
                    context(),
                    session_id=uuid.uuid4(),
                )
        self.assertTrue(await self.is_locked("alice", PASSWORD))
        self.assertEqual(
            (await self.audit_summary())[("auth.unlock", "deny", "role_not_allowed")], 2
        )

    async def test_an_unknown_account_or_one_that_is_closed_is_not_found(self):
        gone = await self.make_user("gone-user", status="deleted")
        pending = await self.make_user("pending-user", status="pending_deletion")
        session_id = await self.open_session(self.admin, "admin passphrase")
        for target in (uuid.uuid4(), gone.id, pending.id):
            with self.subTest(target=str(target)[:8]):
                with self.assertRaises(AccountNotFoundError):
                    await self.auth.unlock_account(
                        self.principal(self.admin),
                        target,
                        context(),
                        session_id=session_id,
                    )

    async def test_unlocking_clears_only_the_accounts_counter_not_the_sources(self):
        session_id = await self.open_session(self.admin, "admin passphrase")
        for index in range(20):
            with self.assertRaises(InvalidCredentialsError):
                await self.auth.login(
                    f"guess-{index:03d}", "wrong wrong wrong", context(SOURCE)
                )
        with self.assertRaises(ThrottledError):
            await self.auth.login("alice", PASSWORD, context(SOURCE))
        await self.auth.unlock_account(
            self.principal(self.admin), self.alice.id, context(), session_id=session_id
        )
        # The account is free, but this source is still locked.
        with self.assertRaises(ThrottledError):
            await self.auth.login("alice", PASSWORD, context(SOURCE))
        result = await self.auth.login("alice", PASSWORD, context(OTHER))
        self.assertEqual(result.session.login_name, "alice")

    async def test_the_arguments_are_checked_first(self):
        admin = self.principal(self.admin)
        for args in (
            ("admin", self.alice.id, context()),
            (None, self.alice.id, context()),
            (admin, str(self.alice.id), context()),
            (admin, None, context()),
            (admin, self.alice.id, None),
            (admin, self.alice.id, "context"),
        ):
            with self.subTest(args=repr(args)[:40]):
                with self.assertRaises(InvalidAuthInputError):
                    await self.auth.unlock_account(*args, session_id=uuid.uuid4())
        for bad in (None, str(uuid.uuid4()), 5, b"x"):
            with self.subTest(session_id=repr(bad)[:20]):
                with self.assertRaises(InvalidAuthInputError):
                    await self.auth.unlock_account(
                        admin, self.alice.id, context(), session_id=bad
                    )
        self.assertEqual(await self.audit_rows(), [])


@requires_postgres
class PolicyTest(AdminTestCase):
    async def owner_session(self, step_up="passkey"):
        """A signed-in Owner session.

        ``step_up``: ``"passkey"`` (a passkey step-up written by the fixture: there
        is no Passkey to produce one yet), ``"password"`` (the real password
        step-up, which rotates the session) or ``False`` (none).
        """
        logged = await self.auth.login("boss", "owner passphrase", context())
        if step_up == "password":
            stepped = await self.auth.step_up(
                logged.session,
                StepUpEvidence(AuthMethod.PASSWORD, "owner passphrase"),
                context(),
            )
            return stepped.session
        if step_up == "passkey":
            await self.fake_passkey_step_up(logged.session.record.id)
        return logged.session

    async def update(self, session, version=1, **changes):
        values = {
            "passkey_owner": "required",
            "passkey_admin": "required",
            "passkey_user": "optional",
            "recommend_passkey_to_users": True,
            "stepup_window_minutes": 30,
        }
        values.update(changes)
        return await self.auth.policy.update(
            self.principal(self.owner),
            session.record.id,
            context(),
            expected_version=version,
            **values,
        )

    async def test_the_default_is_the_requirements_policy(self):
        policy = await self.auth.policy.get()
        self.assertEqual(
            (
                policy.version,
                policy.passkey_owner,
                policy.passkey_admin,
                policy.passkey_user,
                policy.recommend_passkey_to_users,
                policy.stepup_window_minutes,
                policy.updated_by,
            ),
            (
                1,
                PasskeyRequirement.REQUIRED,
                PasskeyRequirement.REQUIRED,
                PasskeyRequirement.OPTIONAL,
                True,
                30,
                None,
            ),
        )
        self.assertEqual(
            policy.requirement_for(SystemRole.OWNER), PasskeyRequirement.REQUIRED
        )
        self.assertEqual(
            policy.requirement_for(SystemRole.ADMIN), PasskeyRequirement.REQUIRED
        )
        self.assertEqual(
            policy.requirement_for(SystemRole.USER), PasskeyRequirement.OPTIONAL
        )
        self.assertEqual(
            policy.requirement_for(SystemRole.SYSTEM), PasskeyRequirement.OPTIONAL
        )

    async def test_the_owner_changes_it_and_the_change_is_versioned_and_recorded(self):
        session = await self.owner_session()
        self.clock.advance(minutes=1)
        policy = await self.update(
            session, passkey_user="required", stepup_window_minutes=45
        )
        self.assertEqual(
            (policy.version, policy.passkey_user, policy.stepup_window_minutes),
            (2, PasskeyRequirement.REQUIRED, 45),
        )
        self.assertEqual(
            (policy.updated_by, policy.updated_at),
            (self.owner.id, T0 + timedelta(minutes=1)),
        )
        (change,) = await self.query("SELECT * FROM auth_policy_changes")
        self.assertEqual(
            (
                change.version,
                change.changed_by,
                change.changed_at,
                change.old_passkey_user,
                change.new_passkey_user,
                change.old_stepup_window_minutes,
                change.new_stepup_window_minutes,
                change.old_passkey_owner,
                change.new_passkey_owner,
            ),
            (
                2,
                self.owner.id,
                T0 + timedelta(minutes=1),
                "optional",
                "required",
                30,
                45,
                "required",
                "required",
            ),
        )
        audit = [r for r in await self.audit_rows() if r.action == "auth.policy.update"]
        self.assertEqual(len(audit), 1)
        self.assertEqual(
            (
                audit[0].decision,
                audit[0].reason,
                audit[0].actor_id,
                audit[0].actor_role,
                audit[0].resource_kind,
                audit[0].resource_id,
            ),
            (
                "allow",
                "updated",
                self.owner.id,
                "owner",
                "auth_policy_change",
                change.id,
            ),
        )

    async def test_relaxing_and_tightening_are_both_the_owners_to_do(self):
        session = await self.owner_session()
        relaxed = await self.update(
            session, 1, passkey_owner="optional", passkey_admin="optional"
        )
        tightened = await self.update(session, 2, passkey_user="required")
        self.assertEqual(
            (
                relaxed.passkey_owner,
                relaxed.passkey_admin,
                tightened.passkey_user,
                tightened.version,
            ),
            (
                PasskeyRequirement.OPTIONAL,
                PasskeyRequirement.OPTIONAL,
                PasskeyRequirement.REQUIRED,
                3,
            ),
        )
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM auth_policy_changes"), 2
        )

    async def test_setting_the_values_it_already_has_changes_nothing(self):
        session = await self.owner_session()
        policy = await self.update(session)
        self.assertEqual(policy.version, 1)
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM auth_policy_changes"), 0
        )
        self.assertNotIn(
            ("auth.policy.update", "allow", "updated"), await self.audit_summary()
        )

    async def test_an_admin_and_a_user_cannot_change_it(self):
        for who in (self.admin, self.alice):
            with self.subTest(role=who.role):
                with self.assertRaises(AuthPermissionError):
                    await self.auth.policy.update(
                        self.principal(who),
                        uuid.uuid4(),
                        context(),
                        expected_version=1,
                        passkey_owner="optional",
                        passkey_admin="required",
                        passkey_user="optional",
                        recommend_passkey_to_users=True,
                        stepup_window_minutes=30,
                    )
        self.assertEqual((await self.auth.policy.get()).version, 1)
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.policy.update", "deny", "role_not_allowed")], 2)

    async def test_a_change_needs_a_recent_step_up_of_the_owners_own_session(self):
        session = await self.owner_session(step_up=False)
        with self.assertRaises(StepUpRequiredError):
            await self.update(session, passkey_user="required")
        self.assertEqual((await self.auth.policy.get()).version, 1)
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.policy.update", "deny", "step_up_required")
            ],
            1,
        )

    async def test_a_change_needs_a_step_up_that_is_actually_missing_not_insufficient(
        self,
    ):
        session = await self.owner_session(step_up=False)
        with self.assertRaises(StepUpRequiredError) as caught:
            await self.update(session, passkey_user="required")
        # "no step-up at all" is the base error, not the "wrong method" one.
        self.assertIs(type(caught.exception), StepUpRequiredError)

    async def test_a_password_step_up_is_not_enough_to_change_the_policy(self):
        # REQUIREMENTS.md: the Owner's sensitive operations need a Passkey
        # step-up. A stolen Owner password must not be able to relax the policy.
        session = await self.owner_session("password")
        step = (await self.auth.view(session)).auth.step_up
        self.assertEqual((step.method, step.satisfied), (AuthMethod.PASSWORD, True))
        for changes in (
            {"passkey_owner": "optional"},
            {"passkey_admin": "optional"},
            {"passkey_user": "required"},
            {"stepup_window_minutes": 240},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(StepUpMethodInsufficientError):
                    await self.update(session, **changes)
        policy = await self.auth.policy.get()
        self.assertEqual(policy.version, 1)
        self.assertEqual(policy.passkey_owner, PasskeyRequirement.REQUIRED)
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM auth_policy_changes"), 0
        )
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.policy.update", "deny", "step_up_method_insufficient")], 4
        )
        self.assertNotIn(("auth.policy.update", "allow", "updated"), summary)

    async def test_the_insufficient_method_error_is_a_step_up_error(self):
        # Callers that handle "a step-up is needed" also handle this one.
        self.assertTrue(issubclass(StepUpMethodInsufficientError, StepUpRequiredError))

    async def test_a_passkey_step_up_changes_the_policy_and_the_change_is_recorded(
        self,
    ):
        session = await self.owner_session("passkey")
        policy = await self.update(session, passkey_user="required")
        self.assertEqual(
            (policy.version, policy.passkey_user), (2, PasskeyRequirement.REQUIRED)
        )

    async def test_a_password_step_up_cannot_be_turned_into_a_passkey_one(self):
        session = await self.owner_session("password")
        # 1. Asking for the passkey method with a password: no verifier is
        #    registered for it, so it is refused before anything is written.
        for password in ("owner passphrase", None):
            with self.subTest(password=password):
                with self.assertRaises(InvalidAuthInputError):
                    await self.auth.step_up(
                        session, StepUpEvidence(AuthMethod.PASSKEY, password), context()
                    )
        row = (
            await self.query(
                "SELECT stepup_method FROM auth_sessions WHERE id = :id",
                id=session.record.id,
            )
        )[0]
        self.assertEqual(row.stepup_method, "password")
        # 2. The password verifier refuses evidence that claims another method.
        verifier = self.auth._verifiers[AuthMethod.PASSWORD]
        self.assertFalse(
            await verifier.verify(
                self.owner.id,
                "boss",
                StepUpEvidence(AuthMethod.PASSKEY, "owner passphrase"),
            )
        )
        # 3. Repeating the password step-up (refresh, replay) stays a password one.
        again = await self.auth.step_up(
            session, StepUpEvidence(AuthMethod.PASSWORD, "owner passphrase"), context()
        )
        self.assertEqual(
            (await self.auth.view(again.session)).auth.step_up.method,
            AuthMethod.PASSWORD,
        )
        with self.assertRaises(StepUpMethodInsufficientError):
            await self.update(again.session, passkey_user="required")

    async def test_a_verifier_cannot_be_registered_for_a_method_it_does_not_verify(
        self,
    ):
        # A password verifier under the passkey key would let a password
        # step-up be recorded as a passkey one: the registration is refused.
        service = self.services.service
        for key, verifier in (
            (AuthMethod.PASSKEY, self.auth._verifiers[AuthMethod.PASSWORD]),
            (AuthMethod.PASSWORD, FakePasskeyVerifier()),
        ):
            with self.subTest(key=key):
                with self.assertRaises(TypeError):
                    AuthService(
                        self.service_database,
                        hasher=service._hasher,
                        sessions=service._sessions,
                        throttle=service._throttle,
                        audit=service._audit,
                        policy=service._policy,
                        step_up_verifiers={key: verifier},
                    )

    async def test_a_later_password_step_up_replaces_a_passkey_one(self):
        # The recorded method is the LAST one: a password step-up cannot inherit
        # the strength of an earlier passkey one (it lowers, never raises).
        logged = await self.auth.login("boss", "owner passphrase", context())
        await self.fake_passkey_step_up(logged.session.record.id)
        await self.update(logged.session, passkey_user="required")  # accepted
        stepped = await self.auth.step_up(
            logged.session,
            StepUpEvidence(AuthMethod.PASSWORD, "owner passphrase"),
            context(),
        )
        with self.assertRaises(StepUpMethodInsufficientError):
            await self.update(stepped.session, 2, passkey_user="optional")

    async def test_a_real_passkey_step_up_through_the_seam_is_accepted(self):
        # What PAW-023 will do: register a verifier for the passkey method; the
        # step-up then records the method and rotates the session as usual.
        seen = []

        class Verifier(FakePasskeyVerifier):
            async def verify(self, user_id, login_name, evidence):
                seen.append((user_id, login_name))
                return True

        service = self.services.service
        extended = AuthService(
            self.service_database,
            hasher=service._hasher,
            sessions=service._sessions,
            throttle=service._throttle,
            audit=service._audit,
            policy=service._policy,
            step_up_verifiers={AuthMethod.PASSKEY: Verifier()},
        )
        logged = await self.auth.login("boss", "owner passphrase", context())
        stepped = await extended.step_up(
            logged.session, StepUpEvidence(AuthMethod.PASSKEY), context()
        )
        self.assertEqual(seen, [(self.owner.id, "boss")])
        self.assertNotEqual(stepped.token, logged.token)
        policy = await self.update(stepped.session, passkey_user="required")
        self.assertEqual(policy.version, 2)

    async def test_a_step_up_of_another_session_does_not_count(self):
        stale = await self.owner_session(step_up=False)
        await self.owner_session()  # a second session that did step up
        with self.assertRaises(StepUpRequiredError):
            await self.update(stale, passkey_user="required")

    async def test_the_step_up_window_is_exact_and_uses_the_policys_own_window(self):
        session = await self.owner_session()
        self.clock.advance(minutes=29, seconds=59)
        await self.update(session, passkey_user="required")
        self.clock.advance(minutes=1)
        with self.assertRaises(StepUpRequiredError):
            await self.update(session, 2, passkey_user="optional")

    async def test_a_session_that_ended_cannot_change_the_policy(self):
        session = await self.owner_session()
        await self.auth.logout(session, context())
        with self.assertRaises(StepUpRequiredError):
            await self.update(session, passkey_user="required")

    async def test_a_stale_version_is_refused_and_nothing_is_lost(self):
        session = await self.owner_session()
        await self.update(session, 1, passkey_user="required")
        with self.assertRaises(PolicyVersionConflictError):
            await self.update(session, 1, passkey_admin="optional")
        self.assertEqual(
            (await self.auth.policy.get()).passkey_admin, PasskeyRequirement.REQUIRED
        )
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.policy.update", "deny", "version_conflict")
            ],
            1,
        )

    async def test_of_two_concurrent_edits_from_the_same_version_exactly_one_wins(self):
        session = await self.owner_session()

        async def edit(services, index):
            return await services.service.policy.update(
                self.principal(self.owner),
                session.record.id,
                context(),
                expected_version=1,
                passkey_owner="required",
                passkey_admin="required",
                passkey_user="optional",
                recommend_passkey_to_users=index % 2 == 0,
                stepup_window_minutes=31 + index,  # never the current 30
            )

        results = await self.gather_on_own_engines(6, edit)
        winners = [r for r in results if not isinstance(r, Exception)]
        losers = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(winners), 1, results)
        self.assertTrue(
            all(isinstance(e, PolicyVersionConflictError) for e in losers), results
        )
        current = await self.auth.policy.get()
        self.assertEqual(current.version, 2)
        self.assertEqual(
            current.stepup_window_minutes, winners[0].stepup_window_minutes
        )
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM auth_policy_changes"), 1
        )

    async def test_a_change_takes_effect_for_new_sign_ins_and_never_ends_a_session(
        self,
    ):
        alice = await self.auth.login("alice", PASSWORD, context())
        admin = await self.auth.login("admin-one", "admin passphrase", context())
        session = await self.owner_session()
        await self.update(session, passkey_user="required", passkey_admin="required")
        # Nobody was signed out by the tightening ...
        for logged in (alice, admin):
            async with self.service_database.session() as db:
                found = await self.services.sessions.authenticate(db, logged.token)
                await db.commit()
            self.assertIsNotNone(found)
        # ... and what a client is told is the new requirement.
        view = await self.auth.view(alice.session)
        self.assertEqual(view.auth.passkey.requirement, PasskeyRequirement.REQUIRED)
        self.assertTrue(view.auth.passkey.enrollment_required)
        # ... while the password sign-in of the Owner still works (nothing here
        # enforces a Passkey: PAW-023 does, and must keep this path open).
        again = await self.auth.login("boss", "owner passphrase", context())
        self.assertEqual(again.session.system_role, SystemRole.OWNER)

    async def test_what_a_session_reports_follows_the_policy_and_the_role(self):
        alice = await self.auth.login("alice", PASSWORD, context())
        view = await self.auth.view(alice.session)
        self.assertEqual(
            (
                view.auth.passkey.requirement,
                view.auth.passkey.enrolled,
                view.auth.passkey.enrollment_required,
                view.auth.passkey.recommended,
            ),
            (PasskeyRequirement.OPTIONAL, False, False, True),
        )
        owner = await self.auth.login("boss", "owner passphrase", context())
        owner_view = await self.auth.view(owner.session)
        self.assertEqual(
            (
                owner_view.auth.passkey.requirement,
                owner_view.auth.passkey.enrollment_required,
                owner_view.auth.passkey.recommended,
            ),
            (PasskeyRequirement.REQUIRED, True, False),
        )
        session = await self.owner_session()
        await self.update(session, recommend_passkey_to_users=False)
        self.assertFalse((await self.auth.view(alice.session)).auth.passkey.recommended)

    async def test_every_field_is_checked_before_the_database(self):
        session = await self.owner_session()
        owner = self.principal(self.owner)
        good = dict(
            expected_version=1,
            passkey_owner="required",
            passkey_admin="required",
            passkey_user="optional",
            recommend_passkey_to_users=True,
            stepup_window_minutes=30,
        )
        bad_values = {
            "expected_version": [0, -1, True, "1", None, 1.5, 2**31],
            "passkey_owner": ["always", "", "REQUIRED", None, 1, True],
            "passkey_admin": ["always", "", " required", None, 1],
            "passkey_user": ["never", "", "Optional", None, b"optional"],
            "recommend_passkey_to_users": [1, 0, "true", None, "yes"],
            "stepup_window_minutes": [4, 241, 0, -30, True, "30", None, 30.5],
        }
        before = await self.audit_rows()
        for field, values in bad_values.items():
            for bad in values:
                with self.subTest(field=field, value=repr(bad)):
                    with self.assertRaises(InvalidAuthInputError):
                        await self.auth.policy.update(
                            owner, session.record.id, context(), **{**good, field: bad}
                        )
        for args in (
            ("owner", session.record.id, context()),
            (owner, str(session.record.id), context()),
            (owner, session.record.id, "context"),
        ):
            with self.subTest(args=repr(args)[:30]):
                with self.assertRaises(InvalidAuthInputError):
                    await self.auth.policy.update(*args, **good)
        self.assertEqual((await self.auth.policy.get()).version, 1)
        self.assertEqual(len(await self.audit_rows()), len(before))

    async def test_the_boundary_values_of_the_window_are_accepted(self):
        session = await self.owner_session()
        low = await self.update(session, 1, stepup_window_minutes=5)
        self.assertEqual(low.stepup_window_minutes, 5)
        # The window that is judged is the one in force when the change is made
        # (30 minutes: the step-up is fresh), and 240 is the upper bound.
        high = await self.update(session, 2, stepup_window_minutes=240)
        self.assertEqual((high.version, high.stepup_window_minutes), (3, 240))

    async def test_no_secret_reaches_the_policy_history_or_the_audit_trail(self):
        session = await self.owner_session()
        await self.update(session, passkey_user="required")
        stored = await self.everything_stored()
        self.assertNotIn("owner passphrase", stored)
        self.assertNotIn("admin passphrase", stored)


if __name__ == "__main__":
    import unittest

    unittest.main()
