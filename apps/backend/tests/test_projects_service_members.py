"""Invite-only membership through ``ProjectService`` (real PostgreSQL).

Acceptance criterion covered here: **invite-only membership**. A member is added
only by a Manager's invitation that the invitee accepts; nobody joins by
themselves, not even an Owner or Admin; the last accepted Manager cannot leave
or be removed (except by leaving a project that is being deleted).
"""

import unittest
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from paw_backend.authz import Principal, SystemRole
from paw_backend.authz.policy import Reason
from paw_backend.authz.roles import ProjectRole
from paw_backend.projects import (
    AlreadyInvitedError,
    AlreadyMemberError,
    InvalidProjectInputError,
    InviteeUnavailableError,
    InviteExpiredError,
    InviteNotFoundError,
    LastManagerError,
    Member,
    MemberLimitError,
    MemberNotFoundError,
    MemberStatus,
    ProjectNotFoundError,
    ProjectStatus,
)
from paw_backend.projects.limits import INVITE_TTL, MAX_MEMBERS_PER_PROJECT

from .projects_support import T0, requires_postgres
from .test_projects_service_access import AccessTestCase

MANAGER, CONTRIBUTOR, VIEWER = (
    ProjectRole.MANAGER,
    ProjectRole.CONTRIBUTOR,
    ProjectRole.VIEWER,
)
ACTIVE, INVITED = MemberStatus.ACTIVE, MemberStatus.INVITED
PENDING, ARCHIVED = ProjectStatus.PENDING_DELETION, ProjectStatus.ARCHIVED
US = timedelta(microseconds=1)


class MembersTestCase(AccessTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha")
        self.team = self.seed_team(self.project_id)
        self.manager = self.actor(self.team.manager)

    def make_pending_deletion(self):
        self.set_project(
            self.project_id,
            status="pending_deletion",
            deletion_started_at=T0,
            deletion_scheduled_at=T0 + timedelta(days=30),
        )

    async def assertUnchanged(self, before):
        self.assertEqual(self.snapshot(), before)


@requires_postgres
class InviteMemberTest(MembersTestCase):
    async def test_a_manager_invites_an_active_user_for_every_role(self):
        for role in ProjectRole:
            with self.subTest(role=role.value):
                invitee = self.seed_user()
                self.clock.advance(hours=1)
                now = self.clock.now

                invite = await self.service.invite_member(
                    self.manager, self.project_id, invitee, role
                )

                self.assertEqual(
                    invite,
                    Member(
                        self.project_id,
                        invitee,
                        role,
                        INVITED,
                        now,
                        now + INVITE_TTL,
                        None,
                    ),
                )
                row = self.member_row(self.project_id, invitee)
                self.assertEqual(
                    (row["role"], row["status"], row["invited_at"], row["joined_at"]),
                    (role.value, "invited", now, None),
                )
                self.assertEqual(row["invite_expires_at"], now + timedelta(days=14))

    async def test_an_invitation_lasts_14_days(self):
        invitee = self.seed_user()
        invite = await self.service.invite_member(
            self.manager, self.project_id, invitee, VIEWER
        )
        self.assertEqual(
            invite.invite_expires_at - invite.invited_at, timedelta(days=14)
        )

    async def test_the_invitation_is_audited_as_a_members_manage_decision(self):
        invitee = self.seed_user()
        await self.service.invite_member(self.manager, self.project_id, invitee, VIEWER)
        self.assertEqual(
            self.audit(),
            [("project.members.manage", "allow", "granted_by_project_role")],
        )
        event = self.sink.events[0]
        self.assertEqual(
            (event.actor_id, event.project_id, event.actor_role),
            (self.team.manager, self.project_id, "user"),
        )

    async def test_an_invited_user_is_not_a_member_until_they_accept(self):
        invitee = self.seed_user()
        await self.service.invite_member(
            self.manager, self.project_id, invitee, MANAGER
        )

        self.assertEqual(dict(await self.service.roles_of(invitee)), {})
        await self.assertNotFound(
            self.service.get_project(self.actor(invitee), self.project_id)
        )
        await self.assertNotFound(
            self.service.rename_project(self.actor(invitee), self.project_id, "X")
        )
        members = await self.service.list_members(self.manager, self.project_id)
        self.assertNotIn(invitee, [m.user_id for m in members])

    async def test_only_a_manager_may_invite(self):
        invitee = self.seed_user()
        before = self.snapshot()
        for user in (self.team.contributor, self.team.viewer):
            with self.subTest(user=user):
                await self.assertDenied(
                    self.service.invite_member(
                        self.actor(user), self.project_id, invitee, VIEWER
                    ),
                    Reason.CAPABILITY_NOT_GRANTED,
                )
        await self.assertUnchanged(before)

    async def test_owner_and_admin_who_are_not_members_cannot_invite(self):
        invitee = self.seed_user()
        before = self.snapshot()
        for role in (SystemRole.OWNER, SystemRole.ADMIN, SystemRole.USER):
            with self.subTest(role=role.value):
                await self.assertNotFound(
                    self.service.invite_member(
                        self.actor(self.seed_user(), role),
                        self.project_id,
                        invitee,
                        VIEWER,
                    )
                )
        await self.assertUnchanged(before)

    async def test_the_role_comes_from_the_database_not_from_the_principal(self):
        invitee = self.seed_user()
        claimed = Principal(
            self.team.viewer, SystemRole.USER, {self.project_id: MANAGER}
        )
        await self.assertDenied(
            self.service.invite_member(claimed, self.project_id, invitee, VIEWER),
            Reason.CAPABILITY_NOT_GRANTED,
        )
        self.assertIsNone(self.member_row(self.project_id, invitee))

    async def test_an_archived_or_pending_deletion_project_takes_no_invitations(self):
        invitee = self.seed_user()
        self.set_project(self.project_id, status="archived")
        await self.assertStateForbids(
            self.service.invite_member(self.manager, self.project_id, invitee, VIEWER),
            ARCHIVED,
        )
        self.make_pending_deletion()
        await self.assertStateForbids(
            self.service.invite_member(self.manager, self.project_id, invitee, VIEWER),
            PENDING,
        )
        self.assertIsNone(self.member_row(self.project_id, invitee))

    async def test_a_missing_or_deleted_project_is_not_found(self):
        invitee = self.seed_user()
        await self.assertNotFound(
            self.service.invite_member(self.manager, uuid4(), invitee, VIEWER)
        )
        deleted = self.seed_project(ProjectStatus.DELETED)
        self.seed_member(deleted, self.team.manager, MANAGER)
        await self.assertNotFound(
            self.service.invite_member(self.manager, deleted, invitee, VIEWER)
        )

    async def test_the_invitee_must_be_an_existing_active_user(self):
        cases = [uuid4()]
        for status in ("invited", "pending_deletion", "deleted"):
            cases.append(self.seed_user(status=status))
        before = self.snapshot()
        for invitee in cases:
            with self.subTest(invitee=str(invitee)):
                with self.assertRaises(InviteeUnavailableError) as caught:
                    await self.service.invite_member(
                        self.manager, self.project_id, invitee, VIEWER
                    )
                self.assertEqual(str(caught.exception), "The user cannot be invited")
        await self.assertUnchanged(before)

    async def test_an_accepted_member_cannot_be_invited_again(self):
        before = self.snapshot()
        for user in self.team:
            with self.subTest(user=user):
                with self.assertRaises(AlreadyMemberError):
                    await self.service.invite_member(
                        self.manager, self.project_id, user, MANAGER
                    )
        await self.assertUnchanged(before)

    async def test_a_manager_cannot_invite_themselves(self):
        with self.assertRaises(AlreadyMemberError):
            await self.service.invite_member(
                self.manager, self.project_id, self.team.manager, MANAGER
            )

    async def test_an_open_invitation_is_not_refreshed_or_changed(self):
        invitee = self.seed_user()
        first = await self.service.invite_member(
            self.manager, self.project_id, invitee, VIEWER
        )
        self.clock.advance(days=13)
        before = self.snapshot()

        with self.assertRaises(AlreadyInvitedError):
            await self.service.invite_member(
                self.manager, self.project_id, invitee, MANAGER
            )

        await self.assertUnchanged(before)
        row = self.member_row(self.project_id, invitee)
        self.assertEqual(
            (row["role"], row["invite_expires_at"]), ("viewer", first.invite_expires_at)
        )

    async def test_an_expired_invitation_is_replaced_by_a_new_one(self):
        invitee = self.seed_user()
        self.seed_member(
            self.project_id,
            invitee,
            VIEWER,
            INVITED,
            invited_at=T0 - timedelta(days=20),
            expires_at=T0 - timedelta(days=6),
        )

        invite = await self.service.invite_member(
            self.manager, self.project_id, invitee, CONTRIBUTOR
        )

        self.assertEqual(
            (invite.role, invite.invited_at, invite.invite_expires_at),
            (CONTRIBUTOR, T0, T0 + INVITE_TTL),
        )
        rows = self.member_rows(self.project_id)
        self.assertEqual(rows[invitee]["role"], "contributor")
        self.assertEqual(rows[invitee]["invited_at"], T0)
        self.assertEqual(len(rows), 4)

    async def test_the_invitation_is_open_until_the_instant_it_expires(self):
        invitee = self.seed_user()
        expires = T0 + timedelta(hours=1)
        self.seed_member(
            self.project_id, invitee, VIEWER, INVITED, invited_at=T0, expires_at=expires
        )
        self.clock.now = expires - US
        with self.assertRaises(AlreadyInvitedError):
            await self.service.invite_member(
                self.manager, self.project_id, invitee, MANAGER
            )
        self.clock.now = expires
        invite = await self.service.invite_member(
            self.manager, self.project_id, invitee, MANAGER
        )
        self.assertEqual((invite.role, invite.invited_at), (MANAGER, expires))

    async def test_the_role_must_be_a_project_role(self):
        with self.assertRaises(InvalidProjectInputError):
            await self.service.invite_member(
                self.manager, self.project_id, self.seed_user(), "viewer"
            )

    async def test_the_member_limit_counts_members_and_open_invitations(self):
        # The project holds 3 members. Fill it up to the limit minus one.
        self.bulk_seed_members(MAX_MEMBERS_PER_PROJECT - 3 - 1, status=INVITED)
        # Expired invitations do not count.
        for _ in range(5):
            self.seed_member(
                self.project_id,
                role=VIEWER,
                status=INVITED,
                invited_at=T0 - timedelta(days=30),
                expires_at=T0 - timedelta(days=16),
            )
        last = self.seed_user()
        await self.service.invite_member(self.manager, self.project_id, last, VIEWER)
        self.assertEqual(
            self.table_count("project_members"), MAX_MEMBERS_PER_PROJECT + 5
        )

        with self.assertRaises(MemberLimitError):
            await self.service.invite_member(
                self.manager, self.project_id, self.seed_user(), VIEWER
            )
        self.assertEqual(
            self.table_count("project_members"), MAX_MEMBERS_PER_PROJECT + 5
        )

    def bulk_seed_members(self, count: int, *, status: MemberStatus) -> None:
        users = [uuid4() for _ in range(count)]
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, login_name, system_role, status,"
                    " passkey_required, created_at, updated_at) VALUES (:id, :login,"
                    " 'user', 'active', false, :now, :now)"
                ),
                [{"id": u, "login": "u" + u.hex[:12], "now": T0} for u in users],
            )
            connection.execute(
                text(
                    "INSERT INTO project_members (project_id, user_id, role, status,"
                    " invited_at, invite_expires_at, joined_at) VALUES (:p, :u,"
                    " 'viewer', :s, :now, :expires, :joined)"
                ),
                [
                    {
                        "p": self.project_id,
                        "u": u,
                        "s": status.value,
                        "now": T0,
                        "expires": T0 + INVITE_TTL if status is INVITED else None,
                        "joined": T0 if status is ACTIVE else None,
                    }
                    for u in users
                ],
            )


@requires_postgres
class AcceptInviteTest(MembersTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.invitee = self.seed_user()
        self.seed_member(
            self.project_id,
            self.invitee,
            CONTRIBUTOR,
            INVITED,
            invited_at=T0,
            expires_at=T0 + INVITE_TTL,
        )
        self.me = self.actor(self.invitee)

    async def test_the_invitee_becomes_an_active_member_with_the_invited_role(self):
        self.clock.advance(days=2)

        accepted = await self.service.accept_invite(self.me, self.project_id)

        self.assertEqual(
            accepted,
            Member(
                self.project_id,
                self.invitee,
                CONTRIBUTOR,
                ACTIVE,
                T0,
                None,
                T0 + timedelta(days=2),
            ),
        )
        row = self.member_row(self.project_id, self.invitee)
        self.assertEqual(
            (row["status"], row["role"], row["invite_expires_at"], row["joined_at"]),
            ("active", "contributor", None, T0 + timedelta(days=2)),
        )

    async def test_after_accepting_the_user_has_the_role_and_access(self):
        await self.service.accept_invite(self.me, self.project_id)
        self.assertEqual(
            dict(await self.service.roles_of(self.invitee)),
            {self.project_id: CONTRIBUTOR},
        )
        project = await self.service.get_project(self.me, self.project_id)
        self.assertEqual(project.id, self.project_id)
        members = await self.service.list_members(self.me, self.project_id)
        self.assertIn(self.invitee, [m.user_id for m in members])

    async def test_nobody_joins_without_an_invitation(self):
        before = self.snapshot()
        for role in (SystemRole.USER, SystemRole.ADMIN, SystemRole.OWNER):
            with self.subTest(role=role.value):
                stranger = self.seed_user()
                with self.assertRaises(InviteNotFoundError) as caught:
                    await self.service.accept_invite(
                        self.actor(stranger, role), self.project_id
                    )
                self.assertEqual(str(caught.exception), "Invitation not found")
                self.assertIsNone(self.member_row(self.project_id, stranger))
        await self.assertUnchanged(before)
        self.assertEqual(self.sink.events, [])

    async def test_a_manager_of_another_project_cannot_join_this_one(self):
        other_project = self.seed_project()
        outsider = self.seed_manager(other_project)
        with self.assertRaises(InviteNotFoundError):
            await self.service.accept_invite(self.actor(outsider), self.project_id)
        self.assertIsNone(self.member_row(self.project_id, outsider))

    async def test_someone_else_cannot_accept_the_invitation_of_another_user(self):
        other = self.seed_user()
        with self.assertRaises(InviteNotFoundError):
            await self.service.accept_invite(self.actor(other), self.project_id)
        self.assertEqual(
            self.member_row(self.project_id, self.invitee)["status"], "invited"
        )

    async def test_an_expired_invitation_cannot_be_accepted(self):
        expires = T0 + INVITE_TTL
        self.clock.now = expires
        with self.assertRaises(InviteExpiredError) as caught:
            await self.service.accept_invite(self.me, self.project_id)
        self.assertEqual(str(caught.exception), "The invitation has expired")
        row = self.member_row(self.project_id, self.invitee)
        self.assertEqual((row["status"], row["joined_at"]), ("invited", None))

    async def test_an_invitation_can_be_accepted_until_the_last_microsecond(self):
        self.clock.now = T0 + INVITE_TTL - US
        accepted = await self.service.accept_invite(self.me, self.project_id)
        self.assertIs(accepted.status, ACTIVE)
        self.assertEqual(accepted.joined_at, T0 + INVITE_TTL - US)

    async def test_accepting_twice_returns_the_membership_unchanged(self):
        first = await self.service.accept_invite(self.me, self.project_id)
        self.clock.advance(days=5)
        second = await self.service.accept_invite(self.me, self.project_id)
        self.assertEqual(second, first)
        self.assertEqual(
            self.member_row(self.project_id, self.invitee)["joined_at"], T0
        )

    async def test_an_existing_member_who_accepts_keeps_their_own_role(self):
        again = await self.service.accept_invite(
            self.actor(self.team.manager), self.project_id
        )
        self.assertEqual((again.role, again.status), (MANAGER, ACTIVE))

    async def test_an_archived_project_can_still_be_joined(self):
        self.set_project(self.project_id, status="archived")
        accepted = await self.service.accept_invite(self.me, self.project_id)
        self.assertIs(accepted.status, ACTIVE)

    async def test_a_pending_deletion_or_deleted_or_missing_project_cannot_be_joined(
        self,
    ):
        self.make_pending_deletion()
        with self.assertRaises(InviteNotFoundError):
            await self.service.accept_invite(self.me, self.project_id)
        self.assertEqual(
            self.member_row(self.project_id, self.invitee)["status"], "invited"
        )
        deleted = self.seed_project(ProjectStatus.DELETED)
        self.seed_member(deleted, self.invitee, VIEWER, INVITED)
        for project_id in (deleted, uuid4()):
            with self.subTest(project=str(project_id)):
                with self.assertRaises(InviteNotFoundError):
                    await self.service.accept_invite(self.me, project_id)

    async def test_the_system_identity_cannot_accept(self):
        await self.assertDenied(
            self.service.accept_invite(
                self.actor(self.invitee, SystemRole.SYSTEM), self.project_id
            ),
            Reason.CAPABILITY_NOT_GRANTED,
        )
        self.assertEqual(
            self.member_row(self.project_id, self.invitee)["status"], "invited"
        )

    async def test_the_list_of_own_invitations_empties_when_accepted(self):
        self.assertEqual(len(await self.service.list_my_invites(self.me)), 1)
        await self.service.accept_invite(self.me, self.project_id)
        self.assertEqual(await self.service.list_my_invites(self.me), ())


@requires_postgres
class DeclineInviteTest(MembersTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.invitee = self.seed_user()
        self.seed_member(self.project_id, self.invitee, VIEWER, INVITED, invited_at=T0)
        self.me = self.actor(self.invitee)

    async def test_declining_deletes_the_invitation(self):
        self.assertIsNone(await self.service.decline_invite(self.me, self.project_id))
        self.assertIsNone(self.member_row(self.project_id, self.invitee))
        self.assertEqual(len(self.member_rows(self.project_id)), 3)
        self.assertEqual(self.sink.events, [])

    async def test_an_expired_invitation_can_be_declined_too(self):
        self.clock.now = T0 + INVITE_TTL
        await self.service.decline_invite(self.me, self.project_id)
        self.assertIsNone(self.member_row(self.project_id, self.invitee))

    async def test_declining_twice_is_not_found(self):
        await self.service.decline_invite(self.me, self.project_id)
        with self.assertRaises(InviteNotFoundError):
            await self.service.decline_invite(self.me, self.project_id)

    async def test_a_member_cannot_decline_and_must_leave_instead(self):
        with self.assertRaises(InviteNotFoundError):
            await self.service.decline_invite(
                self.actor(self.team.contributor), self.project_id
            )
        self.assertIsNotNone(self.member_row(self.project_id, self.team.contributor))

    async def test_a_stranger_cannot_decline_someone_elses_invitation(self):
        with self.assertRaises(InviteNotFoundError):
            await self.service.decline_invite(
                self.actor(self.seed_user()), self.project_id
            )
        self.assertIsNotNone(self.member_row(self.project_id, self.invitee))

    async def test_an_invitation_to_a_pending_deletion_project_stays(self):
        self.make_pending_deletion()
        with self.assertRaises(InviteNotFoundError):
            await self.service.decline_invite(self.me, self.project_id)
        self.assertIsNotNone(self.member_row(self.project_id, self.invitee))

    async def test_after_declining_the_user_can_be_invited_again(self):
        await self.service.decline_invite(self.me, self.project_id)
        again = await self.service.invite_member(
            self.manager, self.project_id, self.invitee, CONTRIBUTOR
        )
        self.assertEqual(again.role, CONTRIBUTOR)

    async def test_the_system_identity_cannot_decline(self):
        await self.assertDenied(
            self.service.decline_invite(
                self.actor(self.invitee, SystemRole.SYSTEM), self.project_id
            ),
            Reason.CAPABILITY_NOT_GRANTED,
        )
        self.assertIsNotNone(self.member_row(self.project_id, self.invitee))


@requires_postgres
class RemoveMemberTest(MembersTestCase):
    async def test_a_manager_removes_a_member(self):
        users = (self.team.contributor, self.team.viewer)
        for count, user in enumerate(users, 1):
            with self.subTest(user=user):
                self.assertIsNone(
                    await self.service.remove_member(
                        self.manager, self.project_id, user
                    )
                )
                self.assertIsNone(self.member_row(self.project_id, user))
                self.assertEqual(
                    self.audit(),
                    [("project.members.manage", "allow", "granted_by_project_role")]
                    * count,
                )

    async def test_a_removed_member_loses_access_immediately(self):
        await self.service.remove_member(
            self.manager, self.project_id, self.team.contributor
        )
        await self.assertNotFound(
            self.service.get_project(self.actor(self.team.contributor), self.project_id)
        )
        self.assertEqual(dict(await self.service.roles_of(self.team.contributor)), {})

    async def test_a_manager_withdraws_an_invitation_open_or_expired(self):
        open_invite = self.seed_member(self.project_id, status=INVITED)
        expired = self.seed_member(
            self.project_id,
            status=INVITED,
            invited_at=T0 - timedelta(days=20),
            expires_at=T0 - timedelta(days=6),
        )
        for user in (open_invite, expired):
            await self.service.remove_member(self.manager, self.project_id, user)
            self.assertIsNone(self.member_row(self.project_id, user))

    async def test_an_unknown_user_is_not_a_member(self):
        with self.assertRaises(MemberNotFoundError) as caught:
            await self.service.remove_member(self.manager, self.project_id, uuid4())
        self.assertEqual(str(caught.exception), "Member not found")

    async def test_only_a_manager_may_remove(self):
        before = self.snapshot()
        for user in (self.team.contributor, self.team.viewer):
            with self.subTest(user=user):
                await self.assertDenied(
                    self.service.remove_member(
                        self.actor(user), self.project_id, self.team.viewer
                    ),
                    Reason.CAPABILITY_NOT_GRANTED,
                )
        await self.assertNotFound(
            self.service.remove_member(
                self.actor(self.seed_user(), SystemRole.OWNER),
                self.project_id,
                self.team.viewer,
            )
        )
        await self.assertUnchanged(before)

    async def test_the_last_manager_cannot_be_removed(self):
        before = self.snapshot()
        with self.assertRaises(LastManagerError) as caught:
            await self.service.remove_member(
                self.manager, self.project_id, self.team.manager
            )
        self.assertEqual(
            str(caught.exception),
            "The last Manager cannot leave, be removed or be demoted",
        )
        await self.assertUnchanged(before)

    async def test_an_invited_manager_does_not_count_as_a_manager(self):
        self.seed_member(self.project_id, role=MANAGER, status=INVITED)
        with self.assertRaises(LastManagerError):
            await self.service.remove_member(
                self.manager, self.project_id, self.team.manager
            )

    async def test_with_a_second_manager_either_can_be_removed(self):
        second = self.seed_manager(self.project_id)
        await self.service.remove_member(self.manager, self.project_id, second)
        self.assertIsNone(self.member_row(self.project_id, second))
        # The remaining Manager is the last one again.
        with self.assertRaises(LastManagerError):
            await self.service.remove_member(
                self.manager, self.project_id, self.team.manager
            )

    async def test_a_manager_can_remove_themselves_when_another_manager_exists(self):
        self.seed_manager(self.project_id)
        await self.service.remove_member(
            self.manager, self.project_id, self.team.manager
        )
        self.assertIsNone(self.member_row(self.project_id, self.team.manager))

    async def test_an_archived_or_pending_project_takes_no_membership_changes(self):
        self.set_project(self.project_id, status="archived")
        await self.assertStateForbids(
            self.service.remove_member(self.manager, self.project_id, self.team.viewer),
            ARCHIVED,
        )
        self.make_pending_deletion()
        await self.assertStateForbids(
            self.service.remove_member(self.manager, self.project_id, self.team.viewer),
            PENDING,
        )
        self.assertIsNotNone(self.member_row(self.project_id, self.team.viewer))

    async def test_other_projects_are_untouched(self):
        other = self.seed_project()
        self.seed_member(other, self.team.viewer, VIEWER)
        await self.service.remove_member(
            self.manager, self.project_id, self.team.viewer
        )
        self.assertIsNotNone(self.member_row(other, self.team.viewer))


@requires_postgres
class LeaveProjectTest(MembersTestCase):
    async def test_a_member_leaves_without_an_audit_event(self):
        for user in (self.team.contributor, self.team.viewer):
            with self.subTest(user=user):
                self.assertIsNone(
                    await self.service.leave_project(self.actor(user), self.project_id)
                )
                self.assertIsNone(self.member_row(self.project_id, user))
        self.assertEqual(self.sink.events, [])

    async def test_a_manager_leaves_when_another_manager_remains(self):
        self.seed_manager(self.project_id)
        await self.service.leave_project(self.manager, self.project_id)
        self.assertIsNone(self.member_row(self.project_id, self.team.manager))

    async def test_the_last_manager_cannot_leave(self):
        before = self.snapshot()
        with self.assertRaises(LastManagerError):
            await self.service.leave_project(self.manager, self.project_id)
        await self.assertUnchanged(before)

    async def test_the_last_manager_cannot_leave_an_archived_project(self):
        self.set_project(self.project_id, status="archived")
        with self.assertRaises(LastManagerError):
            await self.service.leave_project(self.manager, self.project_id)

    async def test_a_member_can_leave_an_archived_project(self):
        self.set_project(self.project_id, status="archived")
        await self.service.leave_project(self.actor(self.team.viewer), self.project_id)
        self.assertIsNone(self.member_row(self.project_id, self.team.viewer))

    async def test_the_last_manager_may_leave_a_project_that_is_being_deleted(self):
        self.make_pending_deletion()
        await self.service.leave_project(self.manager, self.project_id)
        self.assertIsNone(self.member_row(self.project_id, self.team.manager))
        # Everyone else may leave it too.
        await self.service.leave_project(self.actor(self.team.viewer), self.project_id)

    async def test_an_invitee_a_stranger_and_a_missing_project_are_not_found(self):
        invitee = self.seed_member(self.project_id, status=INVITED)
        for user, project_id in [
            (invitee, self.project_id),
            (self.seed_user(), self.project_id),
            (self.team.viewer, uuid4()),
        ]:
            with self.subTest(user=user):
                await self.assertNotFound(
                    self.service.leave_project(self.actor(user), project_id)
                )
        self.assertIsNotNone(self.member_row(self.project_id, invitee))

    async def test_a_deleted_project_cannot_be_left(self):
        deleted = self.seed_project(ProjectStatus.DELETED)
        user = self.seed_member(deleted)
        await self.assertNotFound(self.service.leave_project(self.actor(user), deleted))

    async def test_owner_and_admin_can_leave_only_as_members(self):
        admin = self.seed_member(self.project_id, role=VIEWER)
        await self.service.leave_project(
            self.actor(admin, SystemRole.ADMIN), self.project_id
        )
        self.assertIsNone(self.member_row(self.project_id, admin))

    async def test_the_system_identity_cannot_leave(self):
        await self.assertDenied(
            self.service.leave_project(
                self.actor(self.team.viewer, SystemRole.SYSTEM), self.project_id
            ),
            Reason.CAPABILITY_NOT_GRANTED,
        )
        self.assertIsNotNone(self.member_row(self.project_id, self.team.viewer))


@requires_postgres
class ChangeRoleTest(MembersTestCase):
    async def test_a_manager_changes_the_role_of_a_member(self):
        changed = await self.service.change_role(
            self.manager, self.project_id, self.team.viewer, CONTRIBUTOR
        )
        self.assertEqual(
            (changed.user_id, changed.role, changed.status),
            (self.team.viewer, CONTRIBUTOR, ACTIVE),
        )
        self.assertEqual(
            self.member_row(self.project_id, self.team.viewer)["role"], "contributor"
        )
        self.assertEqual(
            self.audit(),
            [("project.members.manage", "allow", "granted_by_project_role")],
        )

    async def test_every_role_can_be_assigned(self):
        for role in ProjectRole:
            with self.subTest(role=role.value):
                user = self.seed_member(self.project_id, role=VIEWER)
                changed = await self.service.change_role(
                    self.manager, self.project_id, user, role
                )
                self.assertEqual(changed.role, role)
                self.assertEqual(
                    self.member_row(self.project_id, user)["role"], role.value
                )

    async def test_a_promoted_member_gets_the_new_capabilities(self):
        await self.service.change_role(
            self.manager, self.project_id, self.team.viewer, MANAGER
        )
        renamed = await self.service.rename_project(
            self.actor(self.team.viewer), self.project_id, "By the new manager"
        )
        self.assertEqual(renamed.name, "By the new manager")

    async def test_the_same_role_changes_nothing(self):
        changed = await self.service.change_role(
            self.manager, self.project_id, self.team.contributor, CONTRIBUTOR
        )
        self.assertEqual(changed.role, CONTRIBUTOR)

    async def test_the_last_manager_cannot_be_demoted(self):
        before = self.snapshot()
        for role in (CONTRIBUTOR, VIEWER):
            with self.subTest(role=role.value):
                with self.assertRaises(LastManagerError):
                    await self.service.change_role(
                        self.manager, self.project_id, self.team.manager, role
                    )
        await self.assertUnchanged(before)

    async def test_a_manager_can_be_demoted_when_another_manager_exists(self):
        second = self.seed_manager(self.project_id)
        await self.service.change_role(self.manager, self.project_id, second, VIEWER)
        self.assertEqual(self.member_row(self.project_id, second)["role"], "viewer")
        with self.assertRaises(LastManagerError):
            await self.service.change_role(
                self.manager, self.project_id, self.team.manager, VIEWER
            )

    async def test_the_last_manager_keeps_the_manager_role_without_error(self):
        changed = await self.service.change_role(
            self.manager, self.project_id, self.team.manager, MANAGER
        )
        self.assertEqual(changed.role, MANAGER)

    async def test_an_invitation_or_a_stranger_is_not_a_member_to_change(self):
        invitee = self.seed_member(self.project_id, role=VIEWER, status=INVITED)
        for user in (invitee, uuid4()):
            with self.subTest(user=str(user)):
                with self.assertRaises(MemberNotFoundError):
                    await self.service.change_role(
                        self.manager, self.project_id, user, MANAGER
                    )
        self.assertEqual(self.member_row(self.project_id, invitee)["role"], "viewer")

    async def test_only_a_manager_may_change_roles(self):
        before = self.snapshot()
        for user in (self.team.contributor, self.team.viewer):
            with self.subTest(user=user):
                await self.assertDenied(
                    self.service.change_role(
                        self.actor(user), self.project_id, user, MANAGER
                    ),
                    Reason.CAPABILITY_NOT_GRANTED,
                )
        await self.assertNotFound(
            self.service.change_role(
                self.actor(self.seed_user()), self.project_id, self.team.viewer, MANAGER
            )
        )
        await self.assertUnchanged(before)

    async def test_an_archived_project_takes_no_role_changes(self):
        self.set_project(self.project_id, status="archived")
        await self.assertStateForbids(
            self.service.change_role(
                self.manager, self.project_id, self.team.viewer, MANAGER
            ),
            ARCHIVED,
        )

    async def test_the_role_must_be_a_project_role(self):
        with self.assertRaises(InvalidProjectInputError):
            await self.service.change_role(
                self.manager, self.project_id, self.team.viewer, "manager"
            )

    async def test_a_missing_project_is_not_found(self):
        await self.assertNotFound(
            self.service.change_role(self.manager, uuid4(), self.team.viewer, MANAGER)
        )
        with self.assertRaises(ProjectNotFoundError):
            await self.service.remove_member(self.manager, uuid4(), self.team.viewer)


if __name__ == "__main__":
    unittest.main()
