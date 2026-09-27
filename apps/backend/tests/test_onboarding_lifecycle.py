"""The user lifecycle (PAW-024): delete, cancel an invitation, restore (PostgreSQL)."""

import uuid
from datetime import timedelta

from paw_backend.auth.errors import (
    AccountNotFoundError,
    AccountStateError,
    AuthPermissionError,
    InvalidCredentialsError,
    OwnershipTransferRequiredError,
    RetentionExpiredError,
    StepUpRequiredError,
    TokenRejectedError,
)
from paw_backend.authz.roles import SystemRole
from paw_backend.identity import UserStatus

from .auth_support import T0, requires_postgres
from .onboarding_support import PASSWORD, OnboardingTestCase


@requires_postgres
class DeleteTest(OnboardingTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        _, self.admin, self.admin_auth = await self.administrator()

    async def delete(self, user_id, actor=None, auth=None):
        return await self.lifecycle.delete_user(
            actor or self.admin,
            user_id,
            self.context(),
            session_id=(auth or self.admin_auth).record.id,
        )

    async def test_deleting_an_active_user_closes_the_account_at_once(self):
        bob = await self.make_user("bob")
        first = await self.sign_in(bob, step_up=False)
        await self.sign_in(bob, step_up=False)
        pairing = await self.pairing.issue(first, self.context())

        status = await self.delete(bob.id)

        self.assertIs(status, UserStatus.PENDING_DELETION)
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")
        self.assertEqual(
            await self.history_of(bob.id), [("active", "pending_deletion")]
        )
        reasons = await self.query(
            "SELECT revoked_reason FROM auth_sessions WHERE user_id = :id", id=bob.id
        )
        self.assertEqual([r.revoked_reason for r in reasons], ["account_closed"] * 2)
        self.assertEqual(
            await self.scalar(
                "SELECT state FROM device_pairings WHERE audit_ref = :r",
                r=pairing.pairing_id,
            ),
            "revoked",
        )
        with self.assertRaises(InvalidCredentialsError):
            await self.auth.login("bob", PASSWORD, self.context())
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.user.delete", "allow", "deletion_pending")], 1)
        self.assertEqual(
            summary[("auth.session.revoke_all", "allow", "revoked_all")], 1
        )
        self.assertEqual(summary[("auth.pairing.revoke", "allow", "account_closed")], 1)

    async def test_deleting_an_invited_user_cancels_the_invitation(self):
        issued = await self.invitations.invite(
            self.admin,
            "bob",
            SystemRole.USER,
            self.context(),
            session_id=self.admin_auth.record.id,
        )

        status = await self.delete(issued.user_id)

        self.assertIs(status, UserStatus.DELETED)
        self.assertEqual(
            await self.history_of(issued.user_id),
            [(None, "invited"), ("invited", "deleted")],
        )
        with self.assertRaises(TokenRejectedError):
            await self.invitations.redeem(
                issued.token, "a brand new passphrase", self.context()
            )
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.user.delete", "allow", "invitation_cancelled")], 1
        )
        self.assertEqual(summary[("auth.invitation.revoke", "allow", "cancelled")], 1)
        # Deleted is final here.
        with self.assertRaises(AccountStateError):
            await self.delete(issued.user_id)

    async def test_the_role_rules(self):
        carol = await self.make_user("carol", role="admin")
        with self.assertRaises(AuthPermissionError):
            await self.delete(carol.id)
        self.assertEqual(await self.status_of(carol.id), "active")
        # Nobody deletes themself.
        with self.assertRaises(AuthPermissionError):
            await self.delete(self.admin.user_id)
        # The Owner deletes an Admin, and is never a target.
        owner_user, owner, owner_auth = await self.administrator("owner")
        await self.delete(carol.id, owner, owner_auth)
        self.assertEqual(await self.status_of(carol.id), "pending_deletion")
        with self.assertRaises(AccountNotFoundError):
            await self.delete(owner_user.id)
        with self.assertRaises(AccountNotFoundError):
            await self.delete(uuid.uuid4())
        # A plain user cannot delete anyone.
        dave = await self.make_user("dave")
        dave_auth = await self.sign_in(dave)
        erin = await self.make_user("erin")
        with self.assertRaises(AuthPermissionError):
            await self.delete(erin.id, self.principal(dave), dave_auth)
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.user.delete", "deny", "role_not_allowed")], 3)

    async def test_a_user_pending_deletion_cannot_be_deleted_again(self):
        bob = await self.make_user("bob")
        await self.delete(bob.id)
        with self.assertRaises(AccountStateError):
            await self.delete(bob.id)
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.user.delete", "deny", "invalid_state")], 1)

    async def test_deleting_needs_a_recent_passkey_step_up(self):
        bob = await self.make_user("bob")
        await self.advance(minutes=31)
        with self.assertRaises(StepUpRequiredError):
            await self.delete(bob.id)
        self.assertEqual(await self.status_of(bob.id), "active")

    async def make_project(self, *managers, status="active"):
        project = uuid.uuid4()
        deleting = status in ("pending_deletion", "deleted")
        await self.execute(
            "INSERT INTO projects (id, name, status, created_by, created_at, "
            "updated_at, deletion_started_at, deletion_scheduled_at) VALUES (:id, "
            "'p', :status, NULL, :now, :now, :started, :scheduled)",
            id=project,
            status=status,
            now=T0,
            started=T0 if deleting else None,
            scheduled=T0 + timedelta(hours=720) if deleting else None,
        )
        for user in managers:
            await self.execute(
                "INSERT INTO project_members (project_id, user_id, role, status, "
                "invited_at, joined_at) VALUES (:p, :u, 'manager', 'active', :now, "
                ":now)",
                p=project,
                u=user.id,
                now=T0,
            )
        return project

    async def test_the_last_manager_of_a_project_must_hand_it_over_first(self):
        bob = await self.make_user("bob")
        carol = await self.make_user("carol")
        project = await self.make_project(bob, status="archived")
        with self.assertRaises(OwnershipTransferRequiredError):
            await self.delete(bob.id)
        self.assertEqual(await self.status_of(bob.id), "active")
        summary = await self.audit_summary()
        self.assertEqual(
            summary[("auth.user.delete", "deny", "ownership_transfer_required")], 1
        )
        # A second accepted Manager is enough.
        await self.execute(
            "INSERT INTO project_members (project_id, user_id, role, status, "
            "invited_at, joined_at) VALUES (:p, :u, 'manager', 'active', :now, :now)",
            p=project,
            u=carol.id,
            now=T0,
        )
        await self.delete(bob.id)

    async def test_a_project_being_deleted_does_not_hold_its_manager(self):
        bob = await self.make_user("bob")
        await self.make_project(bob, status="pending_deletion")
        await self.delete(bob.id)
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")


@requires_postgres
class RestoreTest(OnboardingTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.owner_user, self.owner, self.owner_auth = await self.administrator("owner")
        self.bob = await self.make_user("bob")
        self.bob_session = await self.sign_in(self.bob, step_up=False)
        await self.lifecycle.delete_user(
            self.owner,
            self.bob.id,
            self.context(),
            session_id=self.owner_auth.record.id,
        )

    async def restore(self, actor=None, auth=None):
        return await self.lifecycle.restore_user(
            actor or self.owner,
            self.bob.id,
            self.context(),
            session_id=(auth or self.owner_auth).record.id,
        )

    async def test_the_owner_restores_within_30_days(self):
        await self.advance(days=29)
        await self.fake_passkey_step_up(self.owner_auth.record.id)

        self.assertIs(await self.restore(), UserStatus.ACTIVE)

        self.assertEqual(
            await self.history_of(self.bob.id),
            [("active", "pending_deletion"), ("pending_deletion", "active")],
        )
        # The sessions that ended stay ended; signing in again works.
        self.assertIsNotNone(
            await self.scalar(
                "SELECT revoked_at FROM auth_sessions WHERE id = :id",
                id=self.bob_session.record.id,
            )
        )
        await self.auth.login("bob", PASSWORD, self.context())
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.user.restore", "allow", "restored")], 1)

    async def test_after_30_days_it_is_too_late(self):
        await self.advance(days=30)
        # The Owner's session went idle meanwhile: a new one.
        self.owner_auth = await self.sign_in(self.owner_user)
        with self.assertRaises(RetentionExpiredError):
            await self.restore()
        self.assertEqual(await self.status_of(self.bob.id), "pending_deletion")
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.user.restore", "deny", "retention_expired")], 1)

    async def test_only_the_owner_restores_and_only_a_pending_user(self):
        _, admin, admin_auth = await self.administrator()
        with self.assertRaises(AuthPermissionError):
            await self.restore(admin, admin_auth)
        await self.restore()
        with self.assertRaises(AccountStateError):
            await self.restore()
        summary = await self.audit_summary()
        self.assertEqual(summary[("auth.user.restore", "deny", "role_not_allowed")], 1)
        self.assertEqual(summary[("auth.user.restore", "deny", "invalid_state")], 1)

    async def test_restoring_needs_a_recent_passkey_step_up(self):
        await self.advance(minutes=31)
        with self.assertRaises(StepUpRequiredError):
            await self.restore()
        self.assertEqual(await self.status_of(self.bob.id), "pending_deletion")
