"""Create, read, list and settings through ``ProjectService`` (real PostgreSQL).

Acceptance criteria covered here: a project can be created without a repository
(``CreateProjectTest``); membership is required for everything (``GetProjectTest``,
``ListProjectsTest``); the role of the actor is read from the database, never
from the Principal the caller passes (``StalePrincipalTest``).
"""

import unittest
from datetime import timedelta
from unittest import mock
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from paw_backend.authz import Authorizer, Principal, SystemRole
from paw_backend.authz.policy import Reason
from paw_backend.authz.roles import ProjectRole
from paw_backend.projects import (
    InvalidProjectInputError,
    MemberStatus,
    Project,
    ProjectNotFoundError,
    ProjectPermissionDeniedError,
    ProjectService,
    ProjectStateError,
    ProjectStatus,
    store,
)

from .projects_support import T0, PostgresProjectTestCase, requires_postgres

MANAGER, CONTRIBUTOR, VIEWER = (
    ProjectRole.MANAGER,
    ProjectRole.CONTRIBUTOR,
    ProjectRole.VIEWER,
)
ACTIVE, INVITED = MemberStatus.ACTIVE, MemberStatus.INVITED
PENDING, ARCHIVED = ProjectStatus.PENDING_DELETION, ProjectStatus.ARCHIVED


class AccessTestCase(PostgresProjectTestCase):
    async def assertNotFound(self, awaitable):
        with self.assertRaises(ProjectNotFoundError) as caught:
            await awaitable
        self.assertEqual(str(caught.exception), "Project not found")

    async def assertDenied(self, awaitable, reason: Reason):
        with self.assertRaises(ProjectPermissionDeniedError) as caught:
            await awaitable
        self.assertIs(caught.exception.reason, reason)
        self.assertEqual(str(caught.exception), "Permission denied")

    async def assertStateForbids(self, awaitable, status: ProjectStatus):
        with self.assertRaises(ProjectStateError) as caught:
            await awaitable
        self.assertIs(caught.exception.status, status)


@requires_postgres
class CreateProjectTest(AccessTestCase):
    async def test_a_project_is_created_without_a_repository(self):
        creator = self.seed_user()

        project = await self.service.create_project(self.actor(creator), "Alpha")

        self.assertEqual(
            project,
            Project(
                id=project.id,
                name="Alpha",
                description=None,
                status=ProjectStatus.ACTIVE,
                created_by=creator,
                created_at=T0,
                updated_at=T0,
                deletion_started_at=None,
                deletion_scheduled_at=None,
                deleted_at=None,
            ),
        )
        row = self.project_row(project.id)
        self.assertEqual((row["name"], row["status"]), ("Alpha", "active"))
        # Nothing but the project and its creator was needed: no repository.
        self.assertEqual(self.table_count("projects"), 1)

    async def test_the_creator_is_the_first_manager(self):
        creator = self.seed_user()

        project = await self.service.create_project(self.actor(creator), "Alpha")

        rows = self.member_rows(project.id)
        self.assertEqual(set(rows), {creator})
        row = rows[creator]
        self.assertEqual(
            (row["role"], row["status"], row["invited_at"], row["joined_at"]),
            ("manager", "active", T0, T0),
        )
        self.assertIsNone(row["invite_expires_at"])
        self.assertEqual(await self.service.roles_of(creator), {project.id: MANAGER})

    async def test_every_human_system_role_can_create(self):
        for role in (SystemRole.OWNER, SystemRole.ADMIN, SystemRole.USER):
            with self.subTest(role=role.value):
                user = self.seed_user()
                project = await self.service.create_project(
                    self.actor(user, role), f"By {role.value}"
                )
                self.assertEqual(self.project_row(project.id)["created_by"], user)

    async def test_description_and_stripping(self):
        user = self.seed_user()
        project = await self.service.create_project(
            self.actor(user), "  Alpha  ", "  About\nalpha  "
        )
        self.assertEqual((project.name, project.description), ("Alpha", "About\nalpha"))
        blank = await self.service.create_project(self.actor(user), "Beta", "   ")
        self.assertIsNone(blank.description)
        self.assertIsNone(self.project_row(blank.id)["description"])

    async def test_names_are_not_unique(self):
        user = self.seed_user()
        first = await self.service.create_project(self.actor(user), "Same")
        second = await self.service.create_project(self.actor(user), "Same")
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.table_count("projects"), 2)

    async def test_the_clock_gives_the_timestamps(self):
        user = self.seed_user()
        self.clock.advance(days=3, microseconds=7)
        project = await self.service.create_project(self.actor(user), "Alpha")
        self.assertEqual(project.created_at, T0 + timedelta(days=3, microseconds=7))
        self.assertEqual(project.updated_at, project.created_at)

    async def test_project_roles_in_the_principal_are_ignored(self):
        user = self.seed_user()
        other = uuid4()
        principal = Principal(user, SystemRole.USER, {other: MANAGER})
        project = await self.service.create_project(principal, "Alpha")
        self.assertEqual(set(self.member_rows(project.id)), {user})
        self.assertEqual(self.table_count("project_members"), 1)

    async def test_creating_is_audited_as_one_allowed_project_create(self):
        user = self.seed_user()
        await self.service.create_project(self.actor(user), "Alpha")
        (event,) = self.sink.events
        self.assertEqual(
            (event.action, event.decision, event.reason, event.actor_id),
            ("project.create", "allow", "granted_by_system_role", user),
        )

    async def test_invalid_names_create_nothing(self):
        user = self.seed_user()
        for bad in ("", "  ", "a" * 101, "a\nb", None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidProjectInputError):
                    await self.service.create_project(self.actor(user), bad)
        self.assertEqual(self.table_count("projects"), 0)

    async def test_the_system_identity_cannot_create(self):
        user = self.seed_user()
        await self.assertDenied(
            self.service.create_project(self.actor(user, SystemRole.SYSTEM), "Alpha"),
            Reason.CAPABILITY_NOT_GRANTED,
        )
        self.assertEqual(self.table_count("projects"), 0)

    async def test_an_unknown_creator_leaves_nothing_behind(self):
        with self.assertRaises(IntegrityError):
            await self.service.create_project(self.actor(uuid4()), "Alpha")
        self.assertEqual(self.table_count("projects"), 0)
        self.assertEqual(self.table_count("project_members"), 0)

    async def test_the_project_and_its_manager_are_created_atomically(self):
        user = self.seed_user()

        class Boom(Exception):
            """Not a RuntimeError: an unfinished stub must not satisfy the test."""

        with mock.patch.object(store, "insert_member", side_effect=Boom("boom")):
            with self.assertRaises(Boom):
                await self.service.create_project(self.actor(user), "Alpha")
        self.assertEqual(self.table_count("projects"), 0)
        self.assertEqual(self.table_count("project_members"), 0)


@requires_postgres
class GetProjectTest(AccessTestCase):
    async def test_every_member_reads_an_active_and_an_archived_project(self):
        for status in (ProjectStatus.ACTIVE, ARCHIVED):
            project_id = self.seed_project(status, name="Alpha", description="Text")
            team = self.seed_team(project_id)
            for user in team:
                with self.subTest(status=status.value, user=user):
                    project = await self.service.get_project(
                        self.actor(user), project_id
                    )
                    self.assertEqual(
                        (project.id, project.name, project.description, project.status),
                        (project_id, "Alpha", "Text", status),
                    )

    async def test_a_string_id_is_accepted(self):
        project_id = self.seed_project()
        user = self.seed_member(project_id)
        project = await self.service.get_project(self.actor(user), str(project_id))
        self.assertEqual(project.id, project_id)

    async def test_a_non_member_and_a_missing_project_are_indistinguishable(self):
        project_id = self.seed_project()
        self.seed_team(project_id)
        outsider = self.actor(self.seed_user())
        await self.assertNotFound(self.service.get_project(outsider, project_id))
        await self.assertNotFound(self.service.get_project(outsider, uuid4()))

    async def test_a_deleted_project_is_not_found_even_for_its_former_manager(self):
        project_id = self.seed_project(ProjectStatus.DELETED)
        user = self.seed_manager(project_id)  # a leftover row must not open it
        await self.assertNotFound(
            self.service.get_project(self.actor(user), project_id)
        )

    async def test_a_system_role_does_not_open_a_project(self):
        project_id = self.seed_project()
        self.seed_team(project_id)
        for role in (SystemRole.OWNER, SystemRole.ADMIN, SystemRole.USER):
            with self.subTest(role=role.value):
                await self.assertNotFound(
                    self.service.get_project(
                        self.actor(self.seed_user(), role), project_id
                    )
                )

    async def test_an_invitation_is_not_membership(self):
        project_id = self.seed_project()
        invitee = self.seed_member(project_id, role=MANAGER, status=INVITED)
        await self.assertNotFound(
            self.service.get_project(self.actor(invitee), project_id)
        )

    async def test_a_pending_deletion_project_stops_access_for_members(self):
        project_id = self.seed_project(PENDING)
        team = self.seed_team(project_id)
        for user in team:
            with self.subTest(user=user):
                await self.assertStateForbids(
                    self.service.get_project(self.actor(user), project_id), PENDING
                )

    async def test_a_pending_deletion_project_is_still_not_found_for_outsiders(self):
        project_id = self.seed_project(PENDING)
        await self.assertNotFound(
            self.service.get_project(self.actor(self.seed_user()), project_id)
        )

    async def test_an_allowed_read_is_not_audited_but_a_denied_one_is(self):
        project_id = self.seed_project()
        member = self.seed_member(project_id)
        outsider = self.seed_user()

        await self.service.get_project(self.actor(member), project_id)
        self.assertEqual(self.audit(), [])

        with self.assertRaises(ProjectNotFoundError):
            await self.service.get_project(self.actor(outsider), project_id)
        self.assertEqual(self.audit(), [("project.read", "deny", "not_project_member")])
        event = self.sink.events[0]
        self.assertEqual((event.actor_id, event.project_id), (outsider, project_id))
        self.assertEqual(event.resource_kind, "project")

    async def test_a_project_that_does_not_exist_is_not_audited_as_a_decision(self):
        await self.assertNotFound(
            self.service.get_project(self.actor(self.seed_user()), uuid4())
        )
        self.assertEqual(self.sink.events, [])


@requires_postgres
class StalePrincipalTest(AccessTestCase):
    """The role in the Principal the caller passes is never trusted."""

    async def test_a_role_the_principal_claims_but_the_database_lacks_is_ignored(self):
        project_id = self.seed_project()
        self.seed_team(project_id)
        user = self.seed_user()
        claimed = Principal(user, SystemRole.USER, {project_id: MANAGER})

        await self.assertNotFound(self.service.get_project(claimed, project_id))
        await self.assertNotFound(self.service.rename_project(claimed, project_id, "X"))
        await self.assertNotFound(self.service.archive(claimed, project_id))
        self.assertEqual(self.project_row(project_id)["name"], "Alpha")

    async def test_a_member_who_was_removed_loses_the_role_immediately(self):
        project_id = self.seed_project()
        user = self.seed_manager(project_id)
        stale = Principal(user, SystemRole.USER, {project_id: MANAGER})
        await self.service.rename_project(stale, project_id, "First")

        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM project_members WHERE user_id = :u"), {"u": user}
            )

        await self.assertNotFound(
            self.service.rename_project(stale, project_id, "Second")
        )
        self.assertEqual(self.project_row(project_id)["name"], "First")

    async def test_a_lower_role_in_the_database_wins_over_a_higher_claimed_role(self):
        project_id = self.seed_project()
        viewer = self.seed_member(project_id, role=VIEWER)
        claimed = Principal(viewer, SystemRole.USER, {project_id: MANAGER})
        await self.assertDenied(
            self.service.rename_project(claimed, project_id, "X"),
            Reason.CAPABILITY_NOT_GRANTED,
        )

    async def test_an_invitation_gives_no_role_even_if_the_principal_claims_one(self):
        project_id = self.seed_project()
        invitee = self.seed_member(project_id, role=MANAGER, status=INVITED)
        claimed = Principal(invitee, SystemRole.USER, {project_id: MANAGER})
        await self.assertNotFound(self.service.get_project(claimed, project_id))


class BrokenAuditSink:
    """An audit store that is down: every write fails."""

    async def record(self, event) -> None:
        raise RuntimeError("audit store is down")


@requires_postgres
class AuditFailureTest(AccessTestCase):
    """An action that cannot be audited does not happen (fail-closed, 503)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha")
        self.team = self.seed_team(self.project_id)
        self.broken = ProjectService(
            self.database,
            Authorizer(BrokenAuditSink(), clock=self.clock),
            clock=self.clock,
        )

    async def test_a_manager_change_that_cannot_be_audited_is_refused(self):
        before = self.snapshot()
        with self.assertLogs("paw_backend.authz.authorizer", level="ERROR"):
            await self.assertDenied(
                self.broken.rename_project(
                    self.actor(self.team.manager), self.project_id, "New"
                ),
                Reason.AUDIT_UNAVAILABLE,
            )
        self.assertEqual(self.snapshot(), before)

    async def test_an_owner_who_is_not_a_member_gets_the_audit_error_not_not_found(
        self,
    ):
        before = self.snapshot()
        owner = self.actor(self.seed_user(), SystemRole.OWNER)
        with self.assertLogs("paw_backend.authz.authorizer", level="ERROR"):
            await self.assertDenied(
                self.broken.archive(owner, self.project_id),
                Reason.AUDIT_UNAVAILABLE,
            )
        self.assertEqual(self.snapshot(), before)

    async def test_every_audited_method_is_refused_and_changes_nothing(self):
        manager = self.actor(self.team.manager)
        invitee = self.seed_user()
        before = self.snapshot()
        calls = [
            self.broken.set_description(manager, self.project_id, "x"),
            self.broken.invite_member(manager, self.project_id, invitee, VIEWER),
            self.broken.remove_member(manager, self.project_id, self.team.viewer),
            self.broken.change_role(
                manager, self.project_id, self.team.viewer, MANAGER
            ),
            self.broken.archive(manager, self.project_id),
            self.broken.unarchive(manager, self.project_id),
            self.broken.begin_deletion(manager, self.project_id, "Alpha"),
            self.broken.restore(manager, self.project_id),
            self.broken.list_invites(manager, self.project_id),
        ]
        for awaitable in calls:
            with self.assertLogs("paw_backend.authz.authorizer", level="ERROR"):
                await self.assertDenied(awaitable, Reason.AUDIT_UNAVAILABLE)
        self.assertEqual(self.snapshot(), before)

    async def test_an_allowed_read_does_not_need_the_audit_store(self):
        project = await self.broken.get_project(
            self.actor(self.team.viewer), self.project_id
        )
        members = await self.broken.list_members(
            self.actor(self.team.viewer), self.project_id
        )
        self.assertEqual(project.id, self.project_id)
        self.assertEqual(len(members), 3)

    async def test_a_denied_read_is_still_not_found_when_the_audit_store_is_down(self):
        with self.assertLogs("paw_backend.authz.authorizer", level="WARNING"):
            await self.assertNotFound(
                self.broken.get_project(self.actor(self.seed_user()), self.project_id)
            )

    async def test_creating_and_leaving_that_cannot_be_audited_are_refused(self):
        user = self.seed_user()
        before = self.snapshot()
        with self.assertLogs("paw_backend.authz.authorizer", level="ERROR"):
            await self.assertDenied(
                self.broken.create_project(self.actor(user), "Beta"),
                Reason.AUDIT_UNAVAILABLE,
            )
        with self.assertLogs("paw_backend.authz.authorizer", level="ERROR"):
            await self.assertDenied(
                self.broken.leave_project(
                    self.actor(self.team.viewer), self.project_id
                ),
                Reason.AUDIT_UNAVAILABLE,
            )
        self.assertEqual(self.snapshot(), before)

    async def test_the_own_data_reads_do_not_use_the_audit_store(self):
        # list_projects / list_my_invites only read the actor's own rows; they
        # keep the identity check and write no event (Decision 0022).
        listed = await self.broken.list_projects(self.actor(self.team.viewer))
        self.assertEqual([p.id for p in listed], [self.project_id])
        self.assertEqual(
            await self.broken.list_my_invites(self.actor(self.team.viewer)), ()
        )


@requires_postgres
class ListProjectsTest(AccessTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.user = self.seed_user()
        self.me = self.actor(self.user)

    def join(self, status=ProjectStatus.ACTIVE, role=CONTRIBUTOR, **values):
        project_id = self.seed_project(status, **values)
        self.seed_member(project_id, self.user, role)
        return project_id

    async def test_the_normal_list_holds_the_active_projects_of_the_actor(self):
        mine = self.join(name="Mine")
        self.join(ARCHIVED, name="Archived")
        self.join(PENDING, role=MANAGER, name="Pending")
        self.seed_project(name="Someone else's")

        projects = await self.service.list_projects(self.me)

        self.assertEqual([p.id for p in projects], [mine])
        self.assertIsInstance(projects, tuple)
        self.assertIsInstance(projects[0], Project)

    async def test_archived_projects_have_their_own_list(self):
        self.join(name="Active")
        archived = self.join(ARCHIVED, name="Archived")
        listed = await self.service.list_projects(self.me, status=ARCHIVED)
        self.assertEqual([p.id for p in listed], [archived])

    async def test_pending_deletion_is_listed_for_managers_only(self):
        managed = self.join(PENDING, role=MANAGER, name="Managed")
        self.join(PENDING, role=CONTRIBUTOR, name="Contributed")
        listed = await self.service.list_projects(self.me, status=PENDING)
        self.assertEqual([p.id for p in listed], [managed])
        self.assertEqual(listed[0].deletion_scheduled_at, T0 + timedelta(days=30))

    async def test_system_roles_see_only_their_own_memberships(self):
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            with self.subTest(role=role.value):
                self.seed_project(name="Not theirs")
                privileged = self.actor(self.seed_user(), role)
                self.assertEqual(await self.service.list_projects(privileged), ())

    async def test_an_invitation_shows_nothing_until_it_is_accepted(self):
        project_id = self.seed_project()
        self.seed_member(project_id, self.user, MANAGER, INVITED)
        self.assertEqual(await self.service.list_projects(self.me), ())
        await self.service.accept_invite(self.me, project_id)
        listed = await self.service.list_projects(self.me)
        self.assertEqual([p.id for p in listed], [project_id])

    async def test_a_deleted_project_is_never_listed(self):
        deleted = self.seed_project(ProjectStatus.DELETED)
        self.seed_member(deleted, self.user, MANAGER)
        for status in (ProjectStatus.ACTIVE, ARCHIVED, PENDING):
            self.assertEqual(
                await self.service.list_projects(self.me, status=status), ()
            )

    async def test_order_limit_and_offset(self):
        ids = [self.join(created_at=T0 - timedelta(days=n)) for n in range(4)]
        self.assertEqual([p.id for p in await self.service.list_projects(self.me)], ids)
        self.assertEqual(
            [
                p.id
                for p in await self.service.list_projects(self.me, limit=2, offset=1)
            ],
            ids[1:3],
        )

    async def test_listing_is_self_service_and_writes_no_audit_event(self):
        self.join()
        await self.service.list_projects(self.me)
        self.assertEqual(self.sink.events, [])


@requires_postgres
class RolesOfTest(AccessTestCase):
    async def test_it_returns_a_read_only_mapping_of_accepted_roles(self):
        user = self.seed_user()
        a, b = self.seed_project(), self.seed_project(PENDING)
        self.seed_member(a, user, MANAGER)
        self.seed_member(b, user, VIEWER)
        self.seed_member(self.seed_project(), user, MANAGER, INVITED)

        roles = await self.service.roles_of(user)

        self.assertEqual(dict(roles), {a: MANAGER, b: VIEWER})
        with self.assertRaises(TypeError):
            roles[uuid4()] = MANAGER  # type: ignore[index]

    async def test_a_user_without_membership_has_no_roles(self):
        self.assertEqual(dict(await self.service.roles_of(self.seed_user())), {})

    async def test_a_string_id_is_accepted(self):
        user = self.seed_user()
        project_id = self.seed_project()
        self.seed_member(project_id, user, VIEWER)
        self.assertEqual(
            dict(await self.service.roles_of(str(user))), {project_id: VIEWER}
        )


@requires_postgres
class MemberReadsTest(AccessTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project()
        self.team = self.seed_team(self.project_id)

    async def test_every_member_can_list_the_accepted_members(self):
        self.seed_member(self.project_id, status=INVITED)
        for user in self.team:
            with self.subTest(user=user):
                members = await self.service.list_members(
                    self.actor(user), self.project_id
                )
                self.assertEqual({m.user_id for m in members}, set(self.team))
                self.assertTrue(all(m.status is ACTIVE for m in members))

    async def test_members_are_listed_oldest_join_first(self):
        late = self.seed_member(
            self.project_id, joined_at=T0 + timedelta(days=1), invited_at=T0
        )
        members = await self.service.list_members(
            self.actor(self.team.manager), self.project_id
        )
        self.assertEqual(members[-1].user_id, late)
        self.assertEqual(len(members), 4)

    async def test_an_outsider_cannot_list_members(self):
        await self.assertNotFound(
            self.service.list_members(self.actor(self.seed_user()), self.project_id)
        )
        await self.assertNotFound(
            self.service.list_members(
                self.actor(self.seed_user(), SystemRole.OWNER), self.project_id
            )
        )

    async def test_members_of_a_pending_deletion_project_are_not_listed(self):
        self.set_project(
            self.project_id,
            status="pending_deletion",
            deletion_started_at=T0,
            deletion_scheduled_at=T0 + timedelta(days=30),
        )
        await self.assertStateForbids(
            self.service.list_members(self.actor(self.team.manager), self.project_id),
            PENDING,
        )

    async def test_only_a_manager_lists_the_open_invitations(self):
        open_invite = self.seed_member(
            self.project_id,
            status=INVITED,
            invited_at=T0,
            expires_at=T0 + timedelta(days=1),
        )
        expired = self.seed_member(
            self.project_id,
            status=INVITED,
            invited_at=T0 - timedelta(days=9),
            expires_at=T0,
        )
        invites = await self.service.list_invites(
            self.actor(self.team.manager), self.project_id
        )
        self.assertEqual([m.user_id for m in invites], [open_invite])
        self.assertNotIn(expired, [m.user_id for m in invites])
        for user in (self.team.contributor, self.team.viewer):
            with self.subTest(user=user):
                await self.assertDenied(
                    self.service.list_invites(self.actor(user), self.project_id),
                    Reason.CAPABILITY_NOT_GRANTED,
                )
        await self.assertNotFound(
            self.service.list_invites(self.actor(self.seed_user()), self.project_id)
        )

    async def test_the_expiry_boundary_uses_the_clock(self):
        self.seed_member(
            self.project_id,
            status=INVITED,
            invited_at=T0,
            expires_at=T0 + timedelta(hours=1),
        )
        manager = self.actor(self.team.manager)
        self.clock.advance(minutes=59, seconds=59, microseconds=999_999)
        self.assertEqual(
            len(await self.service.list_invites(manager, self.project_id)), 1
        )
        self.clock.advance(microseconds=1)
        self.assertEqual(await self.service.list_invites(manager, self.project_id), ())

    async def test_a_member_lists_only_their_own_open_invitations(self):
        invitee = self.seed_user()
        other = self.seed_user()
        mine = self.seed_project(name="Invited to")
        self.seed_member(mine, invitee, VIEWER, INVITED, invited_at=T0)
        self.seed_member(self.seed_project(), other, VIEWER, INVITED)
        self.seed_member(self.seed_project(PENDING), invitee, VIEWER, INVITED)

        found = await self.service.list_my_invites(self.actor(invitee))

        self.assertEqual(
            [(i.project_id, i.project_name, i.role) for i in found],
            [(mine, "Invited to", VIEWER)],
        )
        self.assertEqual(found[0].expires_at, T0 + timedelta(days=14))
        self.assertEqual(
            await self.service.list_my_invites(self.actor(self.seed_user())), ()
        )
        self.assertEqual(self.sink.events, [])


@requires_postgres
class RenameProjectTest(AccessTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Old", description="Text")
        self.team = self.seed_team(self.project_id)

    async def test_a_manager_renames_the_project(self):
        self.clock.advance(hours=2)

        project = await self.service.rename_project(
            self.actor(self.team.manager), self.project_id, "  New  "
        )

        self.assertEqual((project.name, project.description), ("New", "Text"))
        self.assertEqual(project.updated_at, T0 + timedelta(hours=2))
        self.assertEqual(project.created_at, T0)
        row = self.project_row(self.project_id)
        self.assertEqual(
            (row["name"], row["updated_at"]), ("New", T0 + timedelta(hours=2))
        )
        self.assertEqual(
            self.audit(),
            [("project.settings.manage", "allow", "granted_by_project_role")],
        )

    async def test_the_same_name_changes_nothing(self):
        self.clock.advance(hours=2)
        project = await self.service.rename_project(
            self.actor(self.team.manager), self.project_id, "Old"
        )
        self.assertEqual(project.updated_at, T0)
        self.assertEqual(self.project_row(self.project_id)["updated_at"], T0)

    async def test_contributors_and_viewers_cannot_rename(self):
        before = self.snapshot()
        for count, user in enumerate((self.team.contributor, self.team.viewer), 1):
            with self.subTest(user=user):
                await self.assertDenied(
                    self.service.rename_project(self.actor(user), self.project_id, "X"),
                    Reason.CAPABILITY_NOT_GRANTED,
                )
                self.assertEqual(
                    self.audit(),
                    [("project.settings.manage", "deny", "capability_not_granted")]
                    * count,
                )
                self.assertEqual(self.snapshot(), before)

    async def test_outsiders_and_system_roles_get_not_found(self):
        before = self.snapshot()
        for role in (SystemRole.USER, SystemRole.ADMIN, SystemRole.OWNER):
            with self.subTest(role=role.value):
                await self.assertNotFound(
                    self.service.rename_project(
                        self.actor(self.seed_user(), role), self.project_id, "X"
                    )
                )
        self.assertEqual(self.snapshot(), before)

    async def test_an_archived_or_pending_project_cannot_be_renamed(self):
        manager = self.actor(self.team.manager)
        self.set_project(self.project_id, status="archived")
        await self.assertStateForbids(
            self.service.rename_project(manager, self.project_id, "X"), ARCHIVED
        )
        self.set_project(
            self.project_id,
            status="pending_deletion",
            deletion_started_at=T0,
            deletion_scheduled_at=T0 + timedelta(days=30),
        )
        await self.assertStateForbids(
            self.service.rename_project(manager, self.project_id, "X"), PENDING
        )
        self.assertEqual(self.project_row(self.project_id)["name"], "Old")

    async def test_a_missing_and_a_deleted_project_are_not_found(self):
        manager = self.actor(self.team.manager)
        await self.assertNotFound(self.service.rename_project(manager, uuid4(), "X"))
        deleted = self.seed_project(ProjectStatus.DELETED)
        self.seed_member(deleted, self.team.manager, MANAGER)
        await self.assertNotFound(self.service.rename_project(manager, deleted, "X"))

    async def test_other_projects_are_not_renamed(self):
        other = self.seed_project(name="Other")
        await self.service.rename_project(
            self.actor(self.team.manager), self.project_id, "New"
        )
        self.assertEqual(self.project_row(other)["name"], "Other")

    async def test_an_invalid_name_changes_nothing(self):
        with self.assertRaises(InvalidProjectInputError):
            await self.service.rename_project(
                self.actor(self.team.manager), self.project_id, " "
            )
        self.assertEqual(self.project_row(self.project_id)["name"], "Old")


@requires_postgres
class SetDescriptionTest(AccessTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha", description="Old text")
        self.team = self.seed_team(self.project_id)
        self.manager = self.actor(self.team.manager)

    async def test_a_manager_sets_the_description(self):
        self.clock.advance(minutes=5)
        project = await self.service.set_description(
            self.manager, self.project_id, "  New\ntext "
        )
        self.assertEqual((project.name, project.description), ("Alpha", "New\ntext"))
        self.assertEqual(project.updated_at, T0 + timedelta(minutes=5))
        self.assertEqual(self.project_row(self.project_id)["description"], "New\ntext")

    async def test_none_and_blank_clear_the_description(self):
        for empty in (None, "", "  \n "):
            with self.subTest(empty=repr(empty)):
                self.set_project(self.project_id, description="Old text")
                project = await self.service.set_description(
                    self.manager, self.project_id, empty
                )
                self.assertIsNone(project.description)
                self.assertIsNone(self.project_row(self.project_id)["description"])

    async def test_the_same_description_changes_nothing(self):
        self.clock.advance(hours=1)
        await self.service.set_description(self.manager, self.project_id, "Old text")
        self.assertEqual(self.project_row(self.project_id)["updated_at"], T0)
        self.set_project(self.project_id, description=None)
        await self.service.set_description(self.manager, self.project_id, None)
        self.assertEqual(self.project_row(self.project_id)["updated_at"], T0)

    async def test_only_managers_may_set_it(self):
        for user in (self.team.contributor, self.team.viewer):
            with self.subTest(user=user):
                await self.assertDenied(
                    self.service.set_description(
                        self.actor(user), self.project_id, "x"
                    ),
                    Reason.CAPABILITY_NOT_GRANTED,
                )
        await self.assertNotFound(
            self.service.set_description(
                self.actor(self.seed_user()), self.project_id, "x"
            )
        )
        self.assertEqual(self.project_row(self.project_id)["description"], "Old text")

    async def test_an_archived_project_cannot_be_changed(self):
        self.set_project(self.project_id, status="archived")
        await self.assertStateForbids(
            self.service.set_description(self.manager, self.project_id, "x"), ARCHIVED
        )

    async def test_too_long_or_invalid_text_changes_nothing(self):
        for bad in ("d" * 2001, "a\x00b", 5):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidProjectInputError):
                    await self.service.set_description(
                        self.manager, self.project_id, bad
                    )
        self.assertEqual(self.project_row(self.project_id)["description"], "Old text")


if __name__ == "__main__":
    unittest.main()
