"""The sensitive operations of the Owner and the Admin need a Passkey step-up.

Unlocking an account, changing the policy (PAW-022) and the Tool Broker's strong
approval (Decision 0006) all ask the same question of the session: a Passkey
step-up inside the policy's window, judged at the database's clock.
"""

import uuid

from paw_backend.auth.errors import (
    AccountNotFoundError,
    AuthPermissionError,
    StepUpMethodInsufficientError,
    StepUpRequiredError,
)
from paw_backend.auth.models import AuthMethod
from paw_backend.auth.state import StepUpEvidence
from paw_backend.authz import Principal, ProjectRole, SystemRole
from paw_backend.tools import ApprovalOutcome, ApprovalService, FailClosedStepUp

from .auth_support import PASSWORD, requires_postgres
from .authz_support import principal
from .passkey_pg_support import (
    ADMIN_PASSWORD,
    OWNER_PASSWORD,
    PasskeyTestCase,
    context,
)
from .test_tools_approvals import MERGE, R
from .tools_support import P1, U1, Harness, make_call


class SensitiveCase(PasskeyTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.owner = await self.make_owner()
        self.admin = await self.make_admin()
        self.alice = await self.make_user("alice")

    def actor(self, user):
        return Principal(user.id, SystemRole(user.role))

    async def locked(self, name, password) -> bool:
        from paw_backend.auth.errors import ThrottledError

        try:
            await self.auth.login(name, password, context("198.51.100.9"))
        except ThrottledError:
            return True
        return False

    async def lock_alice(self):
        from paw_backend.auth.errors import InvalidCredentialsError

        for _ in range(5):
            with self.assertRaises(InvalidCredentialsError):
                await self.auth.login(
                    "alice", "wrong wrong wrong", context("198.51.100.9")
                )
        self.assertTrue(await self.locked("alice", PASSWORD))


@requires_postgres
class UnlockTest(SensitiveCase):
    async def unlock(self, actor_user, session, target=None, *, ctx=None):
        return await self.auth.unlock_account(
            self.actor(actor_user),
            (target or self.alice).id,
            ctx or context(),
            session_id=session.record.id,
        )

    async def test_a_fresh_passkey_step_up_allows_the_unlock(self):
        await self.lock_alice()
        session, _ = await self.fully_stepped_up(self.admin)
        await self.unlock(self.admin, session)
        self.assertFalse(await self.locked("alice", PASSWORD))
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.unlock", "allow", "unlocked")], 1)

    async def test_no_step_up_is_refused_and_nothing_is_unlocked(self):
        await self.lock_alice()
        auth, _ = await self.enrolled_session(self.admin)  # enrolling is not a step-up
        with self.assertRaises(StepUpRequiredError):
            await self.unlock(self.admin, auth)
        self.assertTrue(await self.locked("alice", PASSWORD))
        self.assertEqual(
            (await self.audit_summary())[("auth.unlock", "deny", "step_up_required")], 1
        )

    async def test_a_password_step_up_is_refused(self):
        await self.lock_alice()
        auth, _ = await self.enrolled_session(self.admin)
        stepped = await self.auth.step_up(
            auth, StepUpEvidence(AuthMethod.PASSWORD, ADMIN_PASSWORD), context()
        )
        with self.assertRaises(StepUpMethodInsufficientError):
            await self.unlock(self.admin, stepped.session)
        self.assertTrue(await self.locked("alice", PASSWORD))
        self.assertEqual(
            (await self.audit_summary())[
                ("auth.unlock", "deny", "step_up_method_insufficient")
            ],
            1,
        )

    async def test_the_window_is_the_policys_and_exact(self):
        await self.lock_alice()
        session, _ = await self.fully_stepped_up(self.admin)
        for seconds, allowed in ((1799, True), (1800, False)):
            with self.subTest(seconds=seconds):
                self.clock.advance(seconds=seconds)
                if allowed:
                    await self.unlock(self.admin, session)
                else:
                    with self.assertRaises(StepUpRequiredError):
                        await self.unlock(self.admin, session)
                self.clock.advance(seconds=-seconds)
        # A shorter window of the policy is honoured at once.
        await self.execute(
            "UPDATE auth_policy SET version = version + 1, stepup_window_minutes = 5"
        )
        self.clock.advance(minutes=6)
        with self.assertRaises(StepUpRequiredError):
            await self.unlock(self.admin, session)

    async def test_another_sessions_step_up_does_not_count(self):
        stepped, _ = await self.fully_stepped_up(self.admin)
        # A second session of the same admin has none of its own.
        other = await self.sign_in(self.admin, password=ADMIN_PASSWORD)
        with self.assertRaises(StepUpRequiredError):
            await self.unlock(self.admin, other)
        # And an admin cannot borrow a step-up of somebody else's session.
        owner_session, _ = await self.fully_stepped_up(self.owner)
        with self.assertRaises(StepUpRequiredError):
            await self.unlock(self.admin, owner_session)
        # An id that is no session at all.
        with self.assertRaises(StepUpRequiredError):
            await self.auth.unlock_account(
                self.actor(self.admin),
                self.alice.id,
                context(),
                session_id=uuid.uuid4(),
            )
        # The session that has one still works after all these refusals.
        await self.unlock(self.admin, stepped)

    async def test_a_session_that_ended_has_no_step_up(self):
        session, _ = await self.fully_stepped_up(self.admin)
        await self.execute(
            "UPDATE auth_sessions SET revoked_at = now(), revoked_reason = 'logout'"
        )
        with self.assertRaises(StepUpRequiredError):
            await self.unlock(self.admin, session)

    async def test_the_step_up_is_judged_before_the_target_is_looked_at(self):
        stepped, _ = await self.fully_stepped_up(self.admin)
        without = await self.sign_in(self.admin, password=ADMIN_PASSWORD)
        unknown = uuid.uuid4()
        actor = self.actor(self.admin)
        # No step-up: nothing is said about whether the account exists ...
        with self.assertRaises(StepUpRequiredError):
            await self.auth.unlock_account(
                actor, unknown, context(), session_id=without.record.id
            )
        # ... with one, an unknown account is simply not found.
        with self.assertRaises(AccountNotFoundError):
            await self.auth.unlock_account(
                actor, unknown, context(), session_id=stepped.record.id
            )

    async def test_the_role_rules_come_after_the_step_up(self):
        await self.lock_alice()
        session, _ = await self.fully_stepped_up(self.admin)
        boss_locked = self.owner
        with self.assertRaises(AuthPermissionError):
            await self.unlock(self.admin, session, boss_locked)
        self.assertEqual(
            (await self.audit_summary())[("auth.unlock", "deny", "role_not_allowed")], 1
        )

    async def test_the_owner_needs_one_too(self):
        await self.lock_alice()
        auth, authenticator = await self.enrolled_session(self.owner)
        with self.assertRaises(StepUpRequiredError):
            await self.unlock(self.owner, auth)
        stepped = await self.authenticate(auth, authenticator)
        await self.unlock(self.owner, stepped.session)
        self.assertFalse(await self.locked("alice", PASSWORD))


@requires_postgres
class PolicyTest(SensitiveCase):
    async def change(self, session, **changes):
        values = {
            "passkey_owner": "required",
            "passkey_admin": "required",
            "passkey_user": "optional",
            "recommend_passkey_to_users": True,
            "stepup_window_minutes": 30,
            **changes,
        }
        return await self.auth.policy.update(
            self.actor(self.owner),
            session.record.id,
            context(),
            expected_version=1,
            **values,
        )

    async def test_the_policy_changes_with_a_real_passkey_step_up_only(self):
        auth, authenticator = await self.enrolled_session(self.owner)
        with self.assertRaises(StepUpRequiredError):
            await self.change(auth, passkey_admin="optional")
        password = await self.auth.step_up(
            auth, StepUpEvidence(AuthMethod.PASSWORD, OWNER_PASSWORD), context()
        )
        with self.assertRaises(StepUpMethodInsufficientError):
            await self.change(password.session, passkey_admin="optional")
        stepped = await self.authenticate(password.session, authenticator)
        changed = await self.change(stepped.session, passkey_admin="optional")
        self.assertEqual(
            (changed.version, changed.passkey_admin.value), (2, "optional")
        )
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.policy.update", "allow", "updated")], 1)
        self.assertEqual(summary[("auth.policy.update", "deny", "step_up_required")], 1)
        self.assertEqual(
            summary[("auth.policy.update", "deny", "step_up_method_insufficient")], 1
        )

    async def test_the_new_window_applies_to_the_next_operation(self):
        auth, authenticator = await self.enrolled_session(self.owner)
        stepped = await self.authenticate(auth, authenticator)
        await self.change(stepped.session, stepup_window_minutes=5)
        self.clock.advance(minutes=6)
        with self.assertRaises(StepUpRequiredError):
            await self.auth.policy.update(
                self.actor(self.owner),
                stepped.session.record.id,
                context(),
                expected_version=2,
                passkey_owner="required",
                passkey_admin="required",
                passkey_user="optional",
                recommend_passkey_to_users=True,
                stepup_window_minutes=30,
            )


@requires_postgres
class StrongApprovalTest(PasskeyTestCase):
    """``PasskeyApprovalStepUp`` is what ``ApprovalService(step_up=...)`` is given."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.verifier = self.services.approval_step_up
        self.alice = await self.make_user("alice")

    async def make_user_with_id(self, user_id, name="approver"):
        await self.execute(
            "INSERT INTO users (id, login_name, system_role, status, "
            "passkey_required, created_at, updated_at) VALUES "
            "(:id, :n, 'user', 'active', false, :t, :t)",
            id=user_id,
            n=name,
            t=self.clock.now,
        )
        encoded = await self.services.hasher.hash(PASSWORD)
        await self.execute(
            "INSERT INTO password_credentials (user_id, hash, created_at, changed_at)"
            " VALUES (:id, :h, :t, :t)",
            id=user_id,
            h=encoded,
            t=self.clock.now,
        )

    async def test_only_a_fresh_passkey_step_up_of_the_user_counts(self):
        approval = uuid.uuid4()
        self.assertFalse(await self.verifier.verify(self.alice.id, approval))
        auth, authenticator = await self.enrolled_session(self.alice)
        self.assertFalse(
            await self.verifier.verify(self.alice.id, approval)
        )  # enrolling
        password = await self.auth.step_up(
            auth, StepUpEvidence(AuthMethod.PASSWORD, PASSWORD), context()
        )
        self.assertFalse(await self.verifier.verify(self.alice.id, approval))
        stepped = await self.authenticate(password.session, authenticator)
        self.assertIs(await self.verifier.verify(self.alice.id, approval), True)
        # Nobody else is stepped up by it.
        bob = await self.make_user("bob")
        self.assertFalse(await self.verifier.verify(bob.id, approval))
        self.assertEqual(stepped.session.record.stepup_method, AuthMethod.PASSKEY)

    async def test_the_window_and_the_session_are_judged_at_the_database_clock(self):
        approval = uuid.uuid4()
        auth, authenticator = await self.enrolled_session(self.alice)
        await self.authenticate(auth, authenticator)
        for seconds, expected in ((1799, True), (1800, False)):
            with self.subTest(seconds=seconds):
                self.clock.advance(seconds=seconds)
                self.assertEqual(
                    await self.verifier.verify(self.alice.id, approval), expected
                )
                self.clock.advance(seconds=-seconds)
        await self.execute(
            "UPDATE auth_policy SET version = version + 1, stepup_window_minutes = 5"
        )
        self.clock.advance(minutes=6)
        self.assertFalse(await self.verifier.verify(self.alice.id, approval))
        self.clock.advance(minutes=-6)
        self.assertTrue(await self.verifier.verify(self.alice.id, approval))
        # Ended session, closed account: no step-up.
        await self.execute(
            "UPDATE auth_sessions SET revoked_at = now(), revoked_reason = 'logout'"
        )
        self.assertFalse(await self.verifier.verify(self.alice.id, approval))

    async def test_a_closed_account_is_not_stepped_up(self):
        auth, authenticator = await self.enrolled_session(self.alice)
        await self.authenticate(auth, authenticator)
        await self.execute("UPDATE users SET status = 'pending_deletion'")
        self.assertFalse(await self.verifier.verify(self.alice.id, uuid.uuid4()))

    async def test_the_arguments_are_uuids(self):
        from paw_backend.auth.errors import InvalidAuthInputError

        for user, approval in ((None, uuid.uuid4()), (uuid.uuid4(), "x"), ("u", "a")):
            with self.subTest(user=repr(user)[:10]):
                with self.assertRaises(InvalidAuthInputError):
                    await self.verifier.verify(user, approval)

    async def test_the_tool_brokers_strong_approval_end_to_end(self):
        await self.make_user_with_id(U1)
        harness = Harness()
        approver = principal(SystemRole.USER, U1, {P1: ProjectRole.CONTRIBUTOR})
        pending = await harness.broker.request(make_call("git.merge", MERGE))
        self.assertEqual(pending.reason, R.STRONG_APPROVAL_REQUIRED)
        # The service's own default fails closed, whatever the user has done.
        closed = ApprovalService(harness.approvals, harness.sink, clock=harness.clock)
        self.assertIsInstance(closed._step_up, FailClosedStepUp)
        # With the Passkey verifier installed, but no Passkey step-up yet: pending.
        service = ApprovalService(
            harness.approvals,
            harness.sink,
            step_up=self.verifier,
            clock=harness.clock,
        )
        result = await service.approve(pending.approval_id, approver)
        self.assertEqual(result.outcome, ApprovalOutcome.STEP_UP_REQUIRED)
        # The user registers a Passkey and steps up with it: approved.
        user = type("U", (), {"id": U1, "login_name": "approver", "password": PASSWORD})
        await self.fully_stepped_up(user)
        result = await service.approve(pending.approval_id, approver)
        self.assertEqual(result.outcome, ApprovalOutcome.APPROVED)

    async def test_a_database_that_cannot_answer_is_no_step_up(self):
        from unittest.mock import patch

        harness = Harness()
        pending = await harness.broker.request(make_call("git.merge", MERGE))
        service = ApprovalService(
            harness.approvals, harness.sink, step_up=self.verifier, clock=harness.clock
        )
        with (
            patch.object(
                self.verifier._database, "run_abortable", side_effect=OSError("secret")
            ),
            self.assertLogs(level="WARNING") as logs,
        ):
            result = await service.approve(
                pending.approval_id, principal(SystemRole.USER, U1)
            )
        self.assertEqual(result.outcome, ApprovalOutcome.STEP_UP_REQUIRED)
        self.assertNotIn("secret", "\n".join(logs.output))
