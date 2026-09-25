"""The project lifecycle through ``ProjectService`` (real PostgreSQL).

Acceptance criteria covered here: **Active / Archived / Pending deletion /
Deleted** (``ArchiveTest`` ... ``PurgeTest``), **Pending deletion lasts 30 days**
(``BeginDeletionTest``, ``RestoreTest.test_the_window_...``, ``PurgeTest``) and an
**Archived project can be restored** (``UnarchiveTest``; a Pending deletion one
comes back as Archived: ``RestoreTest``).
"""

import asyncio
import unittest
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from paw_backend.authz import SystemRole
from paw_backend.authz.policy import Reason
from paw_backend.authz.roles import ProjectRole
from paw_backend.projects import (
    ConfirmationMismatchError,
    DeletionWindowClosedError,
    IllegalTransitionError,
    InvalidProjectInputError,
    LifecycleAction,
    MemberStatus,
    NoManagerError,
    ProjectNotFoundError,
    ProjectPermissionDeniedError,
    ProjectStatus,
    PurgeResult,
)

from .projects_support import T0, requires_postgres
from .test_projects_service_access import AccessTestCase

MANAGER, CONTRIBUTOR, VIEWER = (
    ProjectRole.MANAGER,
    ProjectRole.CONTRIBUTOR,
    ProjectRole.VIEWER,
)
ACTIVE, ARCHIVED, PENDING, DELETED = (
    ProjectStatus.ACTIVE,
    ProjectStatus.ARCHIVED,
    ProjectStatus.PENDING_DELETION,
    ProjectStatus.DELETED,
)
INVITED = MemberStatus.INVITED
US = timedelta(microseconds=1)
DAYS_30 = timedelta(days=30)


class LifecycleTestCase(AccessTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha")
        self.team = self.seed_team(self.project_id)
        self.manager = self.actor(self.team.manager)

    def status(self, project_id=None) -> str:
        return self.project_row(project_id or self.project_id)["status"]

    async def forbidden_for_non_managers(self, call):
        """``call(actor)`` is refused for a Contributor, a Viewer and an outsider."""
        before = self.snapshot()
        for user in (self.team.contributor, self.team.viewer):
            with self.subTest(user=user):
                await self.assertDenied(
                    call(self.actor(user)), Reason.CAPABILITY_NOT_GRANTED
                )
        await self.assertNotFound(call(self.actor(self.seed_user())))
        self.assertEqual(self.snapshot(), before)

    def start_deletion_in_the_database(self, started_at=T0):
        self.set_project(
            self.project_id,
            status="pending_deletion",
            deletion_started_at=started_at,
            deletion_scheduled_at=started_at + DAYS_30,
        )


@requires_postgres
class ArchiveTest(LifecycleTestCase):
    async def test_a_manager_archives_an_active_project(self):
        self.clock.advance(hours=3)

        project = await self.service.archive(self.manager, self.project_id)

        self.assertIs(project.status, ARCHIVED)
        self.assertEqual(project.updated_at, T0 + timedelta(hours=3))
        row = self.project_row(self.project_id)
        self.assertEqual(
            (row["status"], row["name"], row["deletion_started_at"], row["deleted_at"]),
            ("archived", "Alpha", None, None),
        )
        self.assertEqual(
            self.audit(),
            [("project.lifecycle.manage", "allow", "granted_by_project_role")],
        )

    async def test_owner_and_admin_may_archive_without_being_members(self):
        for count, role in enumerate((SystemRole.OWNER, SystemRole.ADMIN), 1):
            with self.subTest(role=role.value):
                self.set_project(self.project_id, status="active")
                project = await self.service.archive(
                    self.actor(self.seed_user(), role), self.project_id
                )
                self.assertIs(project.status, ARCHIVED)
                self.assertEqual(
                    self.audit(),
                    [("project.lifecycle.manage", "allow", "granted_by_system_role")]
                    * count,
                )

    async def test_an_ordinary_user_who_is_not_a_member_may_not(self):
        await self.assertNotFound(
            self.service.archive(self.actor(self.seed_user()), self.project_id)
        )
        self.assertEqual(self.status(), "active")

    async def test_contributors_and_viewers_may_not(self):
        await self.forbidden_for_non_managers(
            lambda actor: self.service.archive(actor, self.project_id)
        )

    async def test_archiving_again_is_a_repeat_that_writes_nothing(self):
        await self.service.archive(self.manager, self.project_id)
        first = self.project_row(self.project_id)
        self.clock.advance(days=4)

        project = await self.service.archive(self.manager, self.project_id)

        self.assertIs(project.status, ARCHIVED)
        self.assertEqual(project.updated_at, first["updated_at"])
        self.assertEqual(self.project_row(self.project_id), first)

    async def test_a_pending_deletion_project_cannot_be_archived(self):
        self.start_deletion_in_the_database()
        with self.assertRaises(IllegalTransitionError) as caught:
            await self.service.archive(self.manager, self.project_id)
        self.assertEqual(
            (caught.exception.status, caught.exception.action),
            (PENDING, LifecycleAction.ARCHIVE),
        )
        self.assertEqual(self.status(), "pending_deletion")

    async def test_a_missing_and_a_deleted_project_are_not_found(self):
        await self.assertNotFound(self.service.archive(self.manager, uuid4()))
        deleted = self.seed_project(DELETED)
        self.seed_member(deleted, self.team.manager, MANAGER)
        await self.assertNotFound(self.service.archive(self.manager, deleted))

    async def test_an_archived_project_is_read_only_but_still_readable(self):
        await self.service.archive(self.manager, self.project_id)

        project = await self.service.get_project(self.manager, self.project_id)
        self.assertIs(project.status, ARCHIVED)
        viewer = await self.service.get_project(
            self.actor(self.team.viewer), self.project_id
        )
        self.assertEqual(viewer.id, self.project_id)
        await self.assertStateForbids(
            self.service.rename_project(self.manager, self.project_id, "X"), ARCHIVED
        )
        await self.assertStateForbids(
            self.service.invite_member(
                self.manager, self.project_id, self.seed_user(), VIEWER
            ),
            ARCHIVED,
        )

    async def test_an_archived_project_moves_to_the_archived_list(self):
        await self.service.archive(self.manager, self.project_id)
        me = self.manager
        self.assertEqual(await self.service.list_projects(me), ())
        listed = await self.service.list_projects(me, status=ARCHIVED)
        self.assertEqual([p.id for p in listed], [self.project_id])


@requires_postgres
class UnarchiveTest(LifecycleTestCase):
    async def test_an_archived_project_is_restored_to_active_by_a_manager(self):
        self.set_project(self.project_id, status="archived")
        self.clock.advance(hours=1)

        project = await self.service.unarchive(self.manager, self.project_id)

        self.assertIs(project.status, ACTIVE)
        self.assertEqual(project.updated_at, T0 + timedelta(hours=1))
        self.assertEqual(self.status(), "active")

    async def test_owner_and_admin_may_unarchive(self):
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            with self.subTest(role=role.value):
                self.set_project(self.project_id, status="archived")
                project = await self.service.unarchive(
                    self.actor(self.seed_user(), role), self.project_id
                )
                self.assertIs(project.status, ACTIVE)

    async def test_contributors_viewers_and_outsiders_may_not(self):
        self.set_project(self.project_id, status="archived")
        await self.forbidden_for_non_managers(
            lambda actor: self.service.unarchive(actor, self.project_id)
        )

    async def test_unarchiving_an_active_project_is_a_repeat_that_writes_nothing(self):
        before = self.project_row(self.project_id)
        self.clock.advance(days=2)
        project = await self.service.unarchive(self.manager, self.project_id)
        self.assertIs(project.status, ACTIVE)
        self.assertEqual(self.project_row(self.project_id), before)

    async def test_a_pending_deletion_project_must_be_restored_not_unarchived(self):
        self.start_deletion_in_the_database()
        with self.assertRaises(IllegalTransitionError):
            await self.service.unarchive(self.manager, self.project_id)
        self.assertEqual(self.status(), "pending_deletion")

    async def test_an_unarchived_project_can_be_changed_again(self):
        self.set_project(self.project_id, status="archived")
        await self.service.unarchive(self.manager, self.project_id)
        renamed = await self.service.rename_project(
            self.manager, self.project_id, "Back"
        )
        self.assertEqual(renamed.name, "Back")

    async def test_archive_and_unarchive_can_alternate(self):
        for _ in range(3):
            self.assertIs(
                (await self.service.archive(self.manager, self.project_id)).status,
                ARCHIVED,
            )
            self.assertIs(
                (await self.service.unarchive(self.manager, self.project_id)).status,
                ACTIVE,
            )


@requires_postgres
class BeginDeletionTest(LifecycleTestCase):
    async def test_a_manager_starts_a_deletion_that_lasts_30_days(self):
        self.clock.advance(days=2, hours=3, microseconds=5)
        now = self.clock.now

        project = await self.service.begin_deletion(
            self.manager, self.project_id, "Alpha"
        )

        self.assertIs(project.status, PENDING)
        self.assertEqual(project.deletion_started_at, now)
        self.assertEqual(project.deletion_scheduled_at, now + timedelta(days=30))
        self.assertEqual(
            project.deletion_scheduled_at - project.deletion_started_at,
            timedelta(hours=720),
        )
        self.assertEqual(project.updated_at, now)
        self.assertIsNone(project.deleted_at)
        row = self.project_row(self.project_id)
        self.assertEqual(
            (row["status"], row["deletion_started_at"], row["deletion_scheduled_at"]),
            ("pending_deletion", now, now + DAYS_30),
        )
        self.assertEqual(
            self.audit(),
            [("project.lifecycle.manage", "allow", "granted_by_project_role")],
        )

    async def test_an_archived_project_can_be_deleted_too(self):
        self.set_project(self.project_id, status="archived")
        project = await self.service.begin_deletion(
            self.manager, self.project_id, "Alpha"
        )
        self.assertIs(project.status, PENDING)

    async def test_owner_and_admin_may_start_it_without_being_members(self):
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            with self.subTest(role=role.value):
                self.set_project(
                    self.project_id,
                    status="active",
                    deletion_started_at=None,
                    deletion_scheduled_at=None,
                )
                project = await self.service.begin_deletion(
                    self.actor(self.seed_user(), role), self.project_id, "Alpha"
                )
                self.assertIs(project.status, PENDING)

    async def test_the_confirmation_must_be_exactly_the_project_name(self):
        for wrong in ("alpha", "ALPHA", "Alpha ", " Alpha", "Alph", "", "Alpha2"):
            with self.subTest(wrong=wrong):
                with self.assertRaises(ConfirmationMismatchError) as caught:
                    await self.service.begin_deletion(
                        self.manager, self.project_id, wrong
                    )
                self.assertEqual(
                    str(caught.exception),
                    "The confirmation does not match the project name",
                )
        self.assertEqual(self.status(), "active")
        self.assertIsNone(self.project_row(self.project_id)["deletion_started_at"])

    async def test_the_confirmation_is_compared_with_the_stored_name(self):
        self.set_project(self.project_id, name="Two  Spaces 日本語")
        project = await self.service.begin_deletion(
            self.manager, self.project_id, "Two  Spaces 日本語"
        )
        self.assertIs(project.status, PENDING)

    async def test_the_confirmation_is_checked_after_the_authorization(self):
        # An actor who may not delete learns nothing about the name.
        for user in (self.team.viewer, self.seed_user()):
            with self.subTest(user=user):
                with self.assertRaises(
                    (ProjectPermissionDeniedError, ProjectNotFoundError)
                ):
                    await self.service.begin_deletion(
                        self.actor(user), self.project_id, "wrong"
                    )

    async def test_contributors_viewers_and_outsiders_may_not(self):
        await self.forbidden_for_non_managers(
            lambda actor: self.service.begin_deletion(actor, self.project_id, "Alpha")
        )

    async def test_starting_again_does_not_restart_the_30_days(self):
        first = await self.service.begin_deletion(
            self.manager, self.project_id, "Alpha"
        )
        before = self.project_row(self.project_id)
        self.clock.advance(days=10)

        again = await self.service.begin_deletion(
            self.manager, self.project_id, "Alpha"
        )

        self.assertEqual(again, first)
        self.assertEqual(self.project_row(self.project_id), before)
        self.assertEqual(again.deletion_scheduled_at, T0 + DAYS_30)

    async def test_a_repeat_still_needs_the_confirmation(self):
        await self.service.begin_deletion(self.manager, self.project_id, "Alpha")
        with self.assertRaises(ConfirmationMismatchError):
            await self.service.begin_deletion(self.manager, self.project_id, "nope")

    async def test_access_stops_when_the_deletion_starts(self):
        await self.service.begin_deletion(self.manager, self.project_id, "Alpha")

        for user in self.team:
            await self.assertStateForbids(
                self.service.get_project(self.actor(user), self.project_id), PENDING
            )
        await self.assertStateForbids(
            self.service.rename_project(self.manager, self.project_id, "X"), PENDING
        )
        await self.assertStateForbids(
            self.service.invite_member(
                self.manager, self.project_id, self.seed_user(), VIEWER
            ),
            PENDING,
        )
        self.assertEqual(await self.service.list_projects(self.manager), ())
        pending = await self.service.list_projects(self.manager, status=PENDING)
        self.assertEqual([p.id for p in pending], [self.project_id])

    async def test_the_members_are_kept_while_the_deletion_is_pending(self):
        await self.service.begin_deletion(self.manager, self.project_id, "Alpha")
        self.assertEqual(set(self.member_rows(self.project_id)), set(self.team))

    async def test_a_deleted_or_missing_project_is_not_found(self):
        await self.assertNotFound(
            self.service.begin_deletion(self.manager, uuid4(), "Alpha")
        )
        deleted = self.seed_project(DELETED)
        self.seed_member(deleted, self.team.manager, MANAGER)
        await self.assertNotFound(
            self.service.begin_deletion(self.manager, deleted, "Deleted Project")
        )

    async def test_the_confirmation_is_validated(self):
        for bad in (None, 5, "a" * 101):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidProjectInputError):
                    await self.service.begin_deletion(
                        self.manager, self.project_id, bad
                    )


@requires_postgres
class RestoreTest(LifecycleTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.start_deletion_in_the_database(T0)
        self.deadline = T0 + DAYS_30

    async def test_a_pending_deletion_project_is_restored_to_archived(self):
        self.clock.advance(days=10)

        project = await self.service.restore(self.manager, self.project_id)

        self.assertIs(project.status, ARCHIVED)
        self.assertIsNone(project.deletion_started_at)
        self.assertIsNone(project.deletion_scheduled_at)
        self.assertEqual(project.updated_at, T0 + timedelta(days=10))
        row = self.project_row(self.project_id)
        self.assertEqual(
            (row["status"], row["deletion_started_at"], row["deletion_scheduled_at"]),
            ("archived", None, None),
        )
        self.assertEqual(
            self.audit(),
            [("project.lifecycle.manage", "allow", "granted_by_project_role")],
        )

    async def test_owner_and_admin_may_restore_without_being_members(self):
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            with self.subTest(role=role.value):
                self.start_deletion_in_the_database(T0)
                project = await self.service.restore(
                    self.actor(self.seed_user(), role), self.project_id
                )
                self.assertIs(project.status, ARCHIVED)

    async def test_contributors_viewers_and_outsiders_may_not(self):
        await self.forbidden_for_non_managers(
            lambda actor: self.service.restore(actor, self.project_id)
        )

    async def test_the_window_closes_at_the_instant_the_30_days_end(self):
        self.clock.now = self.deadline - US
        project = await self.service.restore(self.manager, self.project_id)
        self.assertIs(project.status, ARCHIVED)

        self.start_deletion_in_the_database(T0)
        for now in (
            self.deadline,
            self.deadline + US,
            self.deadline + timedelta(days=60),
        ):
            with self.subTest(now=now.isoformat()):
                self.clock.now = now
                with self.assertRaises(DeletionWindowClosedError) as caught:
                    await self.service.restore(self.manager, self.project_id)
                self.assertEqual(str(caught.exception), "The restore window has closed")
                self.assertEqual(self.status(), "pending_deletion")

    async def test_the_window_is_30_days_from_the_moment_the_deletion_started(self):
        # A deletion that started later has a later deadline.
        self.start_deletion_in_the_database(T0 + timedelta(days=5))
        self.clock.now = T0 + timedelta(days=34)
        self.assertIs(
            (await self.service.restore(self.manager, self.project_id)).status, ARCHIVED
        )

    async def test_restoring_an_archived_project_is_a_repeat_that_writes_nothing(self):
        await self.service.restore(self.manager, self.project_id)
        before = self.project_row(self.project_id)
        self.clock.advance(days=100)
        project = await self.service.restore(self.manager, self.project_id)
        self.assertIs(project.status, ARCHIVED)
        self.assertEqual(self.project_row(self.project_id), before)

    async def test_an_active_project_was_never_deleted_so_it_cannot_be_restored(self):
        self.set_project(
            self.project_id,
            status="active",
            deletion_started_at=None,
            deletion_scheduled_at=None,
        )
        with self.assertRaises(IllegalTransitionError):
            await self.service.restore(self.manager, self.project_id)

    async def test_a_project_without_a_manager_is_not_restored(self):
        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM project_members WHERE user_id = :u"),
                {"u": self.team.manager},
            )
        for actor in (
            self.actor(self.seed_user(), SystemRole.OWNER),
            self.actor(self.seed_user(), SystemRole.ADMIN),
        ):
            with self.assertRaises(NoManagerError) as caught:
                await self.service.restore(actor, self.project_id)
            self.assertEqual(str(caught.exception), "The project has no Manager")
        self.assertEqual(self.status(), "pending_deletion")

    async def test_an_invited_manager_does_not_make_a_project_restorable(self):
        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM project_members WHERE role = 'manager'")
            )
        self.seed_member(self.project_id, role=MANAGER, status=INVITED)
        with self.assertRaises(NoManagerError):
            await self.service.restore(
                self.actor(self.seed_user(), SystemRole.OWNER), self.project_id
            )

    async def test_the_last_manager_leaving_a_deleted_project_blocks_its_restore(self):
        await self.service.leave_project(self.manager, self.project_id)
        with self.assertRaises(NoManagerError):
            await self.service.restore(
                self.actor(self.seed_user(), SystemRole.ADMIN), self.project_id
            )

    async def test_a_deleted_or_missing_project_is_not_found(self):
        await self.assertNotFound(self.service.restore(self.manager, uuid4()))
        deleted = self.seed_project(DELETED)
        self.seed_member(deleted, self.team.manager, MANAGER)
        await self.assertNotFound(self.service.restore(self.manager, deleted))

    async def test_a_restored_project_can_be_unarchived_and_deleted_again(self):
        await self.service.restore(self.manager, self.project_id)
        self.assertIs(
            (await self.service.unarchive(self.manager, self.project_id)).status, ACTIVE
        )
        self.clock.advance(days=50)
        again = await self.service.begin_deletion(
            self.manager, self.project_id, "Alpha"
        )
        self.assertEqual(again.deletion_started_at, T0 + timedelta(days=50))
        self.assertEqual(again.deletion_scheduled_at, T0 + timedelta(days=80))


@requires_postgres
class FullLifecycleTest(LifecycleTestCase):
    async def test_active_archived_pending_deleted_and_back(self):
        creator = self.seed_user()
        me = self.actor(creator)
        project = await self.service.create_project(me, "Journey")
        self.assertIs(project.status, ACTIVE)

        self.clock.advance(days=1)
        self.assertIs((await self.service.archive(me, project.id)).status, ARCHIVED)
        self.clock.advance(days=1)
        self.assertIs((await self.service.unarchive(me, project.id)).status, ACTIVE)
        self.clock.advance(days=1)
        pending = await self.service.begin_deletion(me, project.id, "Journey")
        self.assertIs(pending.status, PENDING)
        self.assertEqual(pending.deletion_scheduled_at, self.clock.now + DAYS_30)
        self.clock.advance(days=29)
        self.assertIs((await self.service.restore(me, project.id)).status, ARCHIVED)
        self.clock.advance(days=1)
        await self.service.begin_deletion(me, project.id, "Journey")
        self.clock.now += DAYS_30
        self.assertEqual(self.project_row(project.id)["status"], "pending_deletion")

        result = await self.service.purge_expired()

        self.assertEqual(result, PurgeResult((project.id,), False))
        self.assertEqual(self.project_row(project.id)["status"], "deleted")
        await self.assertNotFound(self.service.get_project(me, project.id))


@requires_postgres
class PurgeTest(LifecycleTestCase):
    def due(self, name="Due", *, scheduled_at=None, members=True):
        scheduled_at = scheduled_at or T0
        project_id = self.seed_project(
            PENDING,
            name=name,
            description="Secret description",
            deletion_started_at=scheduled_at - DAYS_30,
        )
        if members:
            self.seed_team(project_id)
            self.seed_member(project_id, status=MemberStatus.INVITED)
        return project_id

    async def test_a_due_project_becomes_a_tombstone_and_loses_its_members(self):
        creator = self.seed_user()
        project_id = self.seed_project(
            PENDING,
            name="Secret name",
            description="Secret description",
            created_by=creator,
            created_at=T0 - timedelta(days=40),
            deletion_started_at=T0 - DAYS_30,
        )
        self.seed_team(project_id)
        self.seed_member(project_id, status=MemberStatus.INVITED)

        result = await self.service.purge_expired()

        self.assertEqual(result, PurgeResult((project_id,), False))
        row = self.project_row(project_id)
        self.assertEqual(
            (row["status"], row["name"], row["description"]),
            ("deleted", "Deleted Project", None),
        )
        self.assertEqual(row["deleted_at"], T0)
        self.assertEqual(row["updated_at"], T0)
        self.assertEqual(row["created_by"], creator)
        self.assertEqual(row["created_at"], T0 - timedelta(days=40))
        self.assertEqual(row["deletion_started_at"], T0 - DAYS_30)
        self.assertEqual(row["deletion_scheduled_at"], T0)
        self.assertEqual(self.member_rows(project_id), {})
        self.assertNotIn("Secret", " ".join(str(v) for v in row.values()))

    async def test_the_purge_starts_at_the_instant_the_30_days_end(self):
        exactly = self.due("Exactly", scheduled_at=T0)
        later = self.due("Later", scheduled_at=T0 + US)

        result = await self.service.purge_expired()

        self.assertEqual(result.purged, (exactly,))
        self.assertEqual(self.status(later), "pending_deletion")
        self.assertEqual(len(self.member_rows(later)), 4)

    async def test_the_default_clock_and_an_explicit_now(self):
        project_id = self.due(scheduled_at=T0 + timedelta(days=1))
        self.assertEqual((await self.service.purge_expired()).purged, ())
        self.assertEqual(
            (await self.service.purge_expired(T0 + timedelta(days=1) - US)).purged, ()
        )
        self.assertEqual(self.status(project_id), "pending_deletion")
        result = await self.service.purge_expired(T0 + timedelta(days=1))
        self.assertEqual(result.purged, (project_id,))
        self.assertEqual(
            self.project_row(project_id)["deleted_at"], T0 + timedelta(days=1)
        )

    async def test_only_due_pending_deletion_projects_are_touched(self):
        active = self.seed_project(name="Active")
        self.seed_team(active)
        archived = self.seed_project(ARCHIVED, name="Archived")
        self.seed_team(archived)
        deleted = self.seed_project(
            DELETED, deletion_started_at=T0 - timedelta(days=90)
        )
        before = {p: self.project_row(p) for p in (active, archived, deleted)}
        due = self.due()

        result = await self.service.purge_expired()

        self.assertEqual(result.purged, (due,))
        for project_id, row in before.items():
            self.assertEqual(self.project_row(project_id), row)
        self.assertEqual(len(self.member_rows(active)), 3)
        self.assertEqual(len(self.member_rows(archived)), 3)

    async def test_the_members_of_other_projects_are_kept(self):
        due = self.due()
        keep = self.seed_project(name="Keep")
        team = self.seed_team(keep)
        await self.service.purge_expired()
        self.assertEqual(self.member_rows(due), {})
        self.assertEqual(set(self.member_rows(keep)), set(team))

    async def test_a_batch_takes_the_oldest_and_says_whether_more_are_due(self):
        oldest = self.due("A", scheduled_at=T0 - timedelta(days=3))
        middle = self.due("B", scheduled_at=T0 - timedelta(days=2))
        newest = self.due("C", scheduled_at=T0 - timedelta(days=1))

        first = await self.service.purge_expired(batch_size=2)
        self.assertEqual(first, PurgeResult((oldest, middle), True))
        self.assertEqual(self.status(newest), "pending_deletion")

        second = await self.service.purge_expired(batch_size=2)
        self.assertEqual(second, PurgeResult((newest,), False))

        third = await self.service.purge_expired(batch_size=2)
        self.assertEqual(third, PurgeResult((), False))

    async def test_a_batch_that_exactly_empties_the_queue_has_no_more(self):
        self.due("A", scheduled_at=T0 - timedelta(days=2))
        self.due("B", scheduled_at=T0 - timedelta(days=1))
        result = await self.service.purge_expired(batch_size=2)
        self.assertEqual(len(result.purged), 2)
        self.assertIs(result.has_more, False)

    async def test_purging_is_idempotent(self):
        project_id = self.due()
        first = await self.service.purge_expired()
        row = self.project_row(project_id)
        second = await self.service.purge_expired()
        self.assertEqual(first.purged, (project_id,))
        self.assertEqual(second, PurgeResult((), False))
        self.assertEqual(self.project_row(project_id), row)

    async def test_a_project_someone_else_has_locked_is_skipped_without_waiting(self):
        locked = self.due("Locked", scheduled_at=T0 - timedelta(days=2))
        free = self.due("Free", scheduled_at=T0 - timedelta(days=1))
        _, transaction = self.lock_project(locked)

        async with asyncio.timeout(20):
            result = await self.service.purge_expired()

        self.assertEqual(result.purged, (free,))
        self.assertIs(result.has_more, True)
        self.assertEqual(self.status(locked), "pending_deletion")
        transaction.rollback()
        again = await self.service.purge_expired()
        self.assertEqual(again, PurgeResult((locked,), False))

    async def test_nothing_is_purged_after_a_restore(self):
        project_id = self.due()
        self.clock.now = T0 - US
        await self.service.restore(self.manager_of(project_id), project_id)
        self.clock.now = T0 + DAYS_30
        self.assertEqual((await self.service.purge_expired()).purged, ())
        self.assertEqual(self.status(project_id), "archived")

    def manager_of(self, project_id):
        with self.engine.connect() as connection:
            user = connection.execute(
                text(
                    "SELECT user_id FROM project_members WHERE project_id = :p"
                    " AND role = 'manager' AND status = 'active'"
                ),
                {"p": project_id},
            ).scalar_one()
        return self.actor(user)

    async def test_a_purged_project_is_gone_for_everyone(self):
        project_id = self.due()
        manager = self.manager_of(project_id)
        await self.service.purge_expired()

        await self.assertNotFound(self.service.get_project(manager, project_id))
        await self.assertNotFound(self.service.restore(manager, project_id))
        await self.assertNotFound(
            self.service.archive(
                self.actor(self.seed_user(), SystemRole.OWNER), project_id
            )
        )
        await self.assertNotFound(self.service.leave_project(manager, project_id))
        self.assertEqual(await self.service.list_projects(manager, status=PENDING), ())
        self.assertEqual(dict(await self.service.roles_of(manager.user_id)), {})

    async def test_the_purge_leaves_the_data_of_other_areas_alone(self):
        project_id = self.due()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tasks (id, project_id, created_by, title, input,"
                    " state, attempt, retry_count, version, created_at, updated_at)"
                    " VALUES (gen_random_uuid(), :p, gen_random_uuid(), 'Task',"
                    " CAST('{}' AS jsonb), 'queued', 1, 0, 1, :now, :now)"
                ),
                {"p": project_id, "now": T0},
            )
        self.addCleanup(self.delete_tasks)

        await self.service.purge_expired()

        self.assertEqual(self.status(project_id), "deleted")
        with self.engine.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM tasks WHERE project_id = :p"),
                {"p": project_id},
            ).scalar_one()
        self.assertEqual(count, 1)

    def delete_tasks(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM tasks"))

    async def test_the_users_are_not_touched(self):
        self.due()
        users_before = self.table_count("users")
        await self.service.purge_expired()
        self.assertEqual(self.table_count("users"), users_before)

    async def test_the_purge_is_the_janitor_and_writes_no_audit_event(self):
        self.due()
        await self.service.purge_expired()
        self.assertEqual(self.sink.events, [])


if __name__ == "__main__":
    unittest.main()
