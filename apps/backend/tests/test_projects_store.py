"""The SQL statements of ``paw_backend.projects.store`` on a real PostgreSQL.

Every test seeds rows with SQL, calls one store function inside a transaction
that this module opens (the store never opens one), and reads the result back
with SQL. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, NoResultFound

from paw_backend.authz.roles import ProjectRole
from paw_backend.projects import (
    Member,
    MemberStatus,
    PendingInvite,
    Project,
    ProjectStatus,
    store,
)
from paw_backend.projects.limits import (
    DELETED_PROJECT_NAME,
    DELETION_RETENTION,
    INVITE_TTL,
)

from .projects_support import T0, PostgresProjectTestCase, member, requires_postgres

US = timedelta(microseconds=1)
MANAGER, CONTRIBUTOR, VIEWER = (
    ProjectRole.MANAGER,
    ProjectRole.CONTRIBUTOR,
    ProjectRole.VIEWER,
)
ACTIVE, INVITED = MemberStatus.ACTIVE, MemberStatus.INVITED
PENDING = ProjectStatus.PENDING_DELETION


class StoreTestCase(PostgresProjectTestCase):
    async def run_store(self, function, *args: Any, **kwargs: Any):
        """Call ``function(session, ...)`` in one transaction that is committed."""
        async with self.database.session() as session, session.begin():
            return await function(session, *args, **kwargs)

    def due_project(self, scheduled_at, **values) -> UUID:
        """A Pending deletion project whose deadline is ``scheduled_at``."""
        return self.seed_project(
            PENDING, deletion_started_at=scheduled_at - DELETION_RETENTION, **values
        )


@requires_postgres
class GetProjectTest(StoreTestCase):
    async def test_it_returns_every_column_of_the_row(self):
        creator = self.seed_user()
        project_id = self.seed_project(
            PENDING,
            name="Alpha",
            description="About alpha",
            created_by=creator,
            created_at=T0 - timedelta(days=3),
            deletion_started_at=T0 - timedelta(days=1),
        )

        project = await self.run_store(store.get_project, project_id)

        self.assertEqual(
            project,
            Project(
                id=project_id,
                name="Alpha",
                description="About alpha",
                status=PENDING,
                created_by=creator,
                created_at=T0 - timedelta(days=3),
                updated_at=T0 - timedelta(days=3),
                deletion_started_at=T0 - timedelta(days=1),
                deletion_scheduled_at=T0 + timedelta(days=29),
                deleted_at=None,
            ),
        )

    async def test_a_missing_project_is_none(self):
        self.assertIsNone(await self.run_store(store.get_project, uuid4()))

    async def test_a_tombstone_is_returned_like_any_other_project(self):
        project_id = self.seed_project(ProjectStatus.DELETED)
        project = await self.run_store(store.get_project, project_id)
        self.assertIs(project.status, ProjectStatus.DELETED)
        self.assertEqual(project.name, DELETED_PROJECT_NAME)
        self.assertIsNone(project.description)
        self.assertEqual(project.deleted_at, T0 + DELETION_RETENTION)

    async def test_it_returns_only_the_project_asked_for(self):
        first = self.seed_project(name="First")
        self.seed_project(name="Second")
        self.assertEqual((await self.run_store(store.get_project, first)).name, "First")

    async def test_without_for_update_it_does_not_wait_for_a_row_lock(self):
        project_id = self.seed_project()
        self.lock_project(project_id)
        async with asyncio.timeout(20):
            project = await self.run_store(store.get_project, project_id)
        self.assertEqual(project.id, project_id)

    async def test_for_update_waits_for_a_row_lock_and_then_returns_the_row(self):
        project_id = self.seed_project()
        _, transaction = self.lock_project(project_id)

        task = self.spawn(
            self.run_store(store.get_project, project_id, for_update=True)
        )
        await asyncio.sleep(0.3)
        self.assertWaiting(task)

        transaction.rollback()
        async with asyncio.timeout(20):
            project = await task
        self.assertEqual(project.id, project_id)

    async def test_for_update_takes_the_lock(self):
        project_id = self.seed_project()
        async with self.database.session() as session, session.begin():
            await store.get_project(session, project_id, for_update=True)
            with self.engine.connect() as other:
                locked = other.execute(
                    text(
                        "SELECT id FROM projects WHERE id = :id FOR UPDATE SKIP LOCKED"
                    ),
                    {"id": project_id},
                ).first()
        self.assertIsNone(locked)


@requires_postgres
class InsertProjectTest(StoreTestCase):
    async def test_it_inserts_an_active_project_and_returns_it(self):
        creator = self.seed_user()

        project = await self.run_store(
            store.insert_project,
            name="Alpha",
            description="About",
            created_by=creator,
            now=T0,
        )

        self.assertIsInstance(project.id, UUID)
        self.assertEqual(
            project,
            Project(
                id=project.id,
                name="Alpha",
                description="About",
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
        self.assertEqual(
            (row["name"], row["description"], row["status"], row["created_by"]),
            ("Alpha", "About", "active", creator),
        )
        self.assertEqual((row["created_at"], row["updated_at"]), (T0, T0))

    async def test_the_description_can_be_none(self):
        creator = self.seed_user()
        project = await self.run_store(
            store.insert_project,
            name="Alpha",
            description=None,
            created_by=creator,
            now=T0,
        )
        self.assertIsNone(project.description)
        self.assertIsNone(self.project_row(project.id)["description"])

    async def test_every_call_creates_a_new_project_with_a_new_id(self):
        creator = self.seed_user()
        ids = set()
        for _ in range(3):
            project = await self.run_store(
                store.insert_project,
                name="Same",
                description=None,
                created_by=creator,
                now=T0,
            )
            ids.add(project.id)
        self.assertEqual(len(ids), 3)
        self.assertEqual(self.table_count("projects"), 3)

    async def test_a_creator_that_is_not_a_user_violates_the_foreign_key(self):
        with self.assertRaises(IntegrityError):
            await self.run_store(
                store.insert_project,
                name="Alpha",
                description=None,
                created_by=uuid4(),
                now=T0,
            )
        self.assertEqual(self.table_count("projects"), 0)

    async def test_the_database_refuses_a_blank_name_and_the_store_does_not_hide_it(
        self,
    ):
        creator = self.seed_user()
        with self.assertRaises(IntegrityError):
            await self.run_store(
                store.insert_project,
                name=" ",
                description=None,
                created_by=creator,
                now=T0,
            )


@requires_postgres
class UpdateSettingsTest(StoreTestCase):
    async def test_it_sets_name_description_and_updated_at_only(self):
        project_id = self.seed_project(name="Old", description="Old text")
        later = T0 + timedelta(hours=5)

        project = await self.run_store(
            store.update_settings,
            project_id,
            name="New",
            description="New text",
            now=later,
        )

        self.assertEqual((project.name, project.description), ("New", "New text"))
        self.assertEqual(project.updated_at, later)
        row = self.project_row(project_id)
        self.assertEqual(
            (row["name"], row["description"], row["updated_at"], row["created_at"]),
            ("New", "New text", later, T0),
        )
        self.assertEqual(row["status"], "active")

    async def test_the_description_can_be_cleared(self):
        project_id = self.seed_project(description="Text")
        project = await self.run_store(
            store.update_settings, project_id, name="Alpha", description=None, now=T0
        )
        self.assertIsNone(project.description)
        self.assertIsNone(self.project_row(project_id)["description"])

    async def test_it_does_not_change_the_status_or_the_deletion_columns(self):
        project_id = self.seed_project(PENDING)
        before = self.project_row(project_id)
        await self.run_store(
            store.update_settings, project_id, name="X", description=None, now=T0
        )
        after = self.project_row(project_id)
        for column in (
            "status",
            "deletion_started_at",
            "deletion_scheduled_at",
            "deleted_at",
            "created_by",
        ):
            self.assertEqual(after[column], before[column], column)

    async def test_other_projects_are_not_touched(self):
        target = self.seed_project(name="Target")
        other = self.seed_project(name="Other", description="keep")
        await self.run_store(
            store.update_settings, target, name="Changed", description=None, now=T0
        )
        row = self.project_row(other)
        self.assertEqual((row["name"], row["description"]), ("Other", "keep"))

    async def test_a_missing_project_raises_no_result_found(self):
        with self.assertRaises(NoResultFound):
            await self.run_store(
                store.update_settings, uuid4(), name="X", description=None, now=T0
            )


@requires_postgres
class SetLifecycleTest(StoreTestCase):
    async def test_it_starts_a_deletion(self):
        project_id = self.seed_project(name="Alpha", description="Text")
        scheduled = T0 + DELETION_RETENTION

        project = await self.run_store(
            store.set_lifecycle,
            project_id,
            status=PENDING,
            deletion_started_at=T0,
            deletion_scheduled_at=scheduled,
            now=T0 + timedelta(minutes=1),
        )

        self.assertIs(project.status, PENDING)
        self.assertEqual(
            (project.deletion_started_at, project.deletion_scheduled_at),
            (T0, scheduled),
        )
        self.assertEqual(project.updated_at, T0 + timedelta(minutes=1))
        row = self.project_row(project_id)
        self.assertEqual(
            (row["status"], row["name"], row["description"], row["deleted_at"]),
            ("pending_deletion", "Alpha", "Text", None),
        )

    async def test_none_clears_both_deletion_timestamps(self):
        project_id = self.seed_project(PENDING)

        project = await self.run_store(
            store.set_lifecycle,
            project_id,
            status=ProjectStatus.ARCHIVED,
            deletion_started_at=None,
            deletion_scheduled_at=None,
            now=T0,
        )

        self.assertIs(project.status, ProjectStatus.ARCHIVED)
        self.assertIsNone(project.deletion_started_at)
        self.assertIsNone(project.deletion_scheduled_at)
        row = self.project_row(project_id)
        self.assertEqual(
            (row["status"], row["deletion_started_at"], row["deletion_scheduled_at"]),
            ("archived", None, None),
        )

    async def test_a_plain_status_change_touches_only_the_status_and_updated_at(self):
        project_id = self.seed_project(name="Alpha", description="Text")
        later = T0 + timedelta(days=2)
        await self.run_store(
            store.set_lifecycle,
            project_id,
            status=ProjectStatus.ARCHIVED,
            deletion_started_at=None,
            deletion_scheduled_at=None,
            now=later,
        )
        row = self.project_row(project_id)
        self.assertEqual(
            (row["status"], row["name"], row["description"], row["updated_at"]),
            ("archived", "Alpha", "Text", later),
        )
        self.assertEqual(row["created_at"], T0)

    async def test_a_deadline_that_is_not_30_days_later_is_refused_by_the_database(
        self,
    ):
        project_id = self.seed_project()
        with self.assertRaises(IntegrityError):
            await self.run_store(
                store.set_lifecycle,
                project_id,
                status=PENDING,
                deletion_started_at=T0,
                deletion_scheduled_at=T0 + timedelta(days=29),
                now=T0,
            )
        self.assertEqual(self.project_row(project_id)["status"], "active")

    async def test_a_missing_project_raises_no_result_found(self):
        with self.assertRaises(NoResultFound):
            await self.run_store(
                store.set_lifecycle,
                uuid4(),
                status=ProjectStatus.ARCHIVED,
                deletion_started_at=None,
                deletion_scheduled_at=None,
                now=T0,
            )


@requires_postgres
class MarkDeletedTest(StoreTestCase):
    async def test_it_turns_the_project_into_its_tombstone(self):
        creator = self.seed_user()
        project_id = self.seed_project(
            PENDING,
            name="Secret name",
            description="Secret text",
            created_by=creator,
            created_at=T0 - timedelta(days=40),
            deletion_started_at=T0 - timedelta(days=31),
        )
        now = T0

        project = await self.run_store(store.mark_deleted, project_id, now=now)

        self.assertEqual(
            project,
            Project(
                id=project_id,
                name=DELETED_PROJECT_NAME,
                description=None,
                status=ProjectStatus.DELETED,
                created_by=creator,
                created_at=T0 - timedelta(days=40),
                updated_at=now,
                deletion_started_at=T0 - timedelta(days=31),
                deletion_scheduled_at=T0 - timedelta(days=1),
                deleted_at=now,
            ),
        )
        row = self.project_row(project_id)
        self.assertEqual(
            (row["status"], row["name"], row["description"], row["deleted_at"]),
            ("deleted", "Deleted Project", None, now),
        )
        self.assertNotIn("Secret", " ".join(str(v) for v in row.values()))

    async def test_other_projects_are_not_touched(self):
        target = self.due_project(T0)
        other = self.due_project(T0, name="Other", description="keep")
        await self.run_store(store.mark_deleted, target, now=T0)
        row = self.project_row(other)
        self.assertEqual((row["status"], row["name"]), ("pending_deletion", "Other"))

    async def test_a_missing_project_raises_no_result_found(self):
        with self.assertRaises(NoResultFound):
            await self.run_store(store.mark_deleted, uuid4(), now=T0)


@requires_postgres
class DueProjectsTest(StoreTestCase):
    async def test_a_project_is_due_from_its_deadline_on(self):
        now = T0
        before = self.due_project(now - US)
        exactly = self.due_project(now)
        after = self.due_project(now + US)

        ids = await self.run_store(store.select_due_project_ids, now, 10)

        self.assertIn(before, ids)
        self.assertIn(exactly, ids)
        self.assertNotIn(after, ids)
        self.assertEqual(await self.run_store(store.count_due_projects, now), 2)

    async def test_only_pending_deletion_projects_are_due(self):
        due = self.due_project(T0 - timedelta(days=1))
        # Archived / active / deleted projects never are, whatever their dates.
        self.seed_project(ProjectStatus.ARCHIVED)
        self.seed_project(ProjectStatus.ACTIVE)
        self.seed_project(
            ProjectStatus.DELETED, deletion_started_at=T0 - timedelta(days=90)
        )

        ids = await self.run_store(store.select_due_project_ids, T0, 10)

        self.assertEqual(ids, [due])
        self.assertEqual(await self.run_store(store.count_due_projects, T0), 1)

    async def test_they_come_oldest_deadline_first_then_by_id(self):
        newest = self.due_project(T0 - timedelta(hours=1))
        oldest = self.due_project(T0 - timedelta(days=3))
        tie = sorted([self.due_project(T0 - timedelta(days=2)) for _ in range(2)])

        ids = await self.run_store(store.select_due_project_ids, T0, 10)

        self.assertEqual(ids, [oldest, *tie, newest])

    async def test_the_limit_takes_the_oldest_ones(self):
        ids = [self.due_project(T0 - timedelta(days=n)) for n in (1, 2, 3, 4)]
        oldest_two = [ids[3], ids[2]]

        self.assertEqual(
            await self.run_store(store.select_due_project_ids, T0, 2), oldest_two
        )
        self.assertEqual(
            await self.run_store(store.select_due_project_ids, T0, 1), [ids[3]]
        )
        self.assertEqual(await self.run_store(store.count_due_projects, T0), 4)

    async def test_nothing_due_gives_an_empty_list_and_zero(self):
        self.due_project(T0 + timedelta(days=1))
        self.assertEqual(await self.run_store(store.select_due_project_ids, T0, 10), [])
        self.assertEqual(await self.run_store(store.count_due_projects, T0), 0)

    async def test_a_locked_project_is_skipped_without_waiting(self):
        locked = self.due_project(T0 - timedelta(days=2))
        free = self.due_project(T0 - timedelta(days=1))
        self.lock_project(locked)

        async with asyncio.timeout(20):
            ids = await self.run_store(store.select_due_project_ids, T0, 10)

        self.assertEqual(ids, [free])

    async def test_the_selected_projects_are_locked_for_other_transactions(self):
        project_id = self.due_project(T0 - timedelta(days=1))
        async with self.database.session() as session, session.begin():
            ids = await store.select_due_project_ids(session, T0, 10)
            with self.engine.connect() as other:
                free = other.execute(
                    text("SELECT id FROM projects FOR UPDATE SKIP LOCKED")
                ).all()
        self.assertEqual(ids, [project_id])
        self.assertEqual(free, [])

    async def test_the_count_does_not_skip_or_wait_for_locked_projects(self):
        locked = self.due_project(T0 - timedelta(days=1))
        self.due_project(T0 - timedelta(days=2))
        self.lock_project(locked)
        async with asyncio.timeout(20):
            count = await self.run_store(store.count_due_projects, T0)
        self.assertEqual(count, 2)


@requires_postgres
class MemberRowTest(StoreTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project()

    async def test_get_member_returns_an_accepted_member(self):
        user = self.seed_member(
            self.project_id,
            role=MANAGER,
            invited_at=T0,
            joined_at=T0 + timedelta(hours=1),
        )
        found = await self.run_store(store.get_member, self.project_id, user)
        self.assertEqual(
            found,
            Member(
                self.project_id,
                user,
                MANAGER,
                ACTIVE,
                T0,
                None,
                T0 + timedelta(hours=1),
            ),
        )

    async def test_get_member_returns_an_invitation_even_when_it_is_expired(self):
        expires = T0 - timedelta(days=1)
        user = self.seed_member(
            self.project_id,
            role=VIEWER,
            status=INVITED,
            invited_at=T0 - timedelta(days=15),
            expires_at=expires,
        )
        found = await self.run_store(store.get_member, self.project_id, user)
        self.assertEqual(
            found,
            Member(
                self.project_id,
                user,
                VIEWER,
                INVITED,
                T0 - timedelta(days=15),
                expires,
                None,
            ),
        )

    async def test_get_member_is_none_for_other_users_and_other_projects(self):
        user = self.seed_member(self.project_id)
        other_project = self.seed_project()
        self.assertIsNone(
            await self.run_store(store.get_member, self.project_id, self.seed_user())
        )
        self.assertIsNone(await self.run_store(store.get_member, other_project, user))
        self.assertIsNone(await self.run_store(store.get_member, uuid4(), uuid4()))

    async def test_insert_member_stores_an_accepted_member_as_given(self):
        user = self.seed_user()
        given = member(
            VIEWER, ACTIVE, user_id=user, project_id=self.project_id, invited_at=T0
        )

        stored = await self.run_store(store.insert_member, given)

        self.assertEqual(stored, given)
        row = self.member_row(self.project_id, user)
        self.assertEqual(
            (row["role"], row["status"], row["invite_expires_at"], row["joined_at"]),
            ("viewer", "active", None, T0),
        )

    async def test_insert_member_stores_an_invitation_as_given(self):
        user = self.seed_user()
        given = member(
            CONTRIBUTOR,
            INVITED,
            user_id=user,
            project_id=self.project_id,
            invited_at=T0,
        )

        stored = await self.run_store(store.insert_member, given)

        self.assertEqual(stored, given)
        row = self.member_row(self.project_id, user)
        self.assertEqual(
            (row["role"], row["status"], row["invite_expires_at"], row["joined_at"]),
            ("contributor", "invited", T0 + INVITE_TTL, None),
        )

    async def test_insert_member_refuses_a_second_row_for_the_same_pair(self):
        user = self.seed_member(self.project_id, role=VIEWER)
        with self.assertRaises(IntegrityError):
            await self.run_store(
                store.insert_member,
                member(MANAGER, ACTIVE, user_id=user, project_id=self.project_id),
            )
        self.assertEqual(self.member_row(self.project_id, user)["role"], "viewer")

    async def test_insert_member_needs_an_existing_user_and_project(self):
        with self.assertRaises(IntegrityError):
            await self.run_store(
                store.insert_member, member(project_id=self.project_id, user_id=uuid4())
            )
        with self.assertRaises(IntegrityError):
            await self.run_store(
                store.insert_member,
                member(project_id=uuid4(), user_id=self.seed_user()),
            )

    async def test_activate_invite_makes_the_member_active(self):
        user = self.seed_member(
            self.project_id, role=CONTRIBUTOR, status=INVITED, invited_at=T0
        )
        joined = T0 + timedelta(days=2)

        activated = await self.run_store(
            store.activate_invite, self.project_id, user, joined_at=joined
        )

        self.assertEqual(
            activated,
            Member(self.project_id, user, CONTRIBUTOR, ACTIVE, T0, None, joined),
        )
        row = self.member_row(self.project_id, user)
        self.assertEqual(
            (row["status"], row["role"], row["invite_expires_at"], row["joined_at"]),
            ("active", "contributor", None, joined),
        )

    async def test_activate_invite_changes_only_an_invitation(self):
        accepted = self.seed_member(self.project_id, joined_at=T0)
        with self.assertRaises(NoResultFound):
            await self.run_store(
                store.activate_invite,
                self.project_id,
                accepted,
                joined_at=T0 + timedelta(days=1),
            )
        self.assertEqual(self.member_row(self.project_id, accepted)["joined_at"], T0)
        with self.assertRaises(NoResultFound):
            await self.run_store(
                store.activate_invite, self.project_id, uuid4(), joined_at=T0
            )

    async def test_activate_invite_leaves_other_rows_alone(self):
        target = self.seed_member(self.project_id, status=INVITED)
        other = self.seed_member(self.project_id, status=INVITED)
        await self.run_store(
            store.activate_invite, self.project_id, target, joined_at=T0
        )
        self.assertEqual(self.member_row(self.project_id, other)["status"], "invited")

    async def test_set_member_role_changes_an_accepted_member(self):
        user = self.seed_member(self.project_id, role=VIEWER)
        other = self.seed_member(self.project_id, role=VIEWER)

        changed = await self.run_store(
            store.set_member_role, self.project_id, user, MANAGER
        )

        self.assertEqual(changed.role, MANAGER)
        self.assertIs(changed.status, ACTIVE)
        self.assertEqual(self.member_row(self.project_id, user)["role"], "manager")
        self.assertEqual(self.member_row(self.project_id, other)["role"], "viewer")

    async def test_set_member_role_never_changes_an_invitation(self):
        user = self.seed_member(self.project_id, role=VIEWER, status=INVITED)
        with self.assertRaises(NoResultFound):
            await self.run_store(store.set_member_role, self.project_id, user, MANAGER)
        self.assertEqual(self.member_row(self.project_id, user)["role"], "viewer")
        with self.assertRaises(NoResultFound):
            await self.run_store(
                store.set_member_role, self.project_id, uuid4(), MANAGER
            )

    async def test_delete_member_deletes_exactly_one_row(self):
        user = self.seed_member(self.project_id)
        keep = self.seed_member(self.project_id)
        other_project = self.seed_project()
        self.seed_member(other_project, user)

        self.assertIs(
            await self.run_store(store.delete_member, self.project_id, user), True
        )

        self.assertEqual(set(self.member_rows(self.project_id)), {keep})
        self.assertEqual(set(self.member_rows(other_project)), {user})

    async def test_delete_member_deletes_an_invitation_too(self):
        user = self.seed_member(self.project_id, status=INVITED)
        self.assertIs(
            await self.run_store(store.delete_member, self.project_id, user), True
        )
        self.assertEqual(self.member_rows(self.project_id), {})

    async def test_delete_member_reports_false_when_there_is_no_row(self):
        self.assertIs(
            await self.run_store(store.delete_member, self.project_id, uuid4()), False
        )

    async def test_delete_project_members_deletes_members_and_invitations(self):
        for _ in range(2):
            self.seed_member(self.project_id)
        self.seed_member(self.project_id, status=INVITED)
        other_project = self.seed_project()
        keep = self.seed_member(other_project)

        deleted = await self.run_store(store.delete_project_members, self.project_id)

        self.assertEqual(deleted, 3)
        self.assertEqual(self.member_rows(self.project_id), {})
        self.assertEqual(set(self.member_rows(other_project)), {keep})
        self.assertEqual(self.table_count("projects"), 2)

    async def test_delete_project_members_of_a_project_without_rows_is_zero(self):
        self.assertEqual(
            await self.run_store(store.delete_project_members, self.project_id), 0
        )
        self.assertEqual(await self.run_store(store.delete_project_members, uuid4()), 0)


@requires_postgres
class MemberListsTest(StoreTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project()

    async def test_active_members_are_ordered_by_join_time_then_user_id(self):
        late = self.seed_member(self.project_id, joined_at=T0 + timedelta(hours=2))
        early = self.seed_member(self.project_id, joined_at=T0)
        tie = [
            self.seed_member(self.project_id, joined_at=T0 + timedelta(hours=1))
            for _ in range(2)
        ]

        members = await self.run_store(store.list_active_members, self.project_id)

        self.assertEqual([m.user_id for m in members], [early, *sorted(tie), late])
        self.assertTrue(all(m.status is ACTIVE for m in members))
        self.assertTrue(all(isinstance(m, Member) for m in members))

    async def test_active_members_exclude_invitations_and_other_projects(self):
        user = self.seed_member(self.project_id, role=MANAGER)
        self.seed_member(self.project_id, status=INVITED)
        self.seed_member(self.seed_project())

        members = await self.run_store(store.list_active_members, self.project_id)

        self.assertEqual([m.user_id for m in members], [user])
        self.assertEqual(members[0].role, MANAGER)

    async def test_active_members_of_an_empty_project_is_an_empty_list(self):
        self.assertEqual(
            await self.run_store(store.list_active_members, self.project_id), []
        )

    async def test_open_invitations_are_open_strictly_before_their_expiry(self):
        now = T0 + timedelta(days=3)
        expired = self.seed_member(
            self.project_id, status=INVITED, expires_at=now - US, invited_at=T0
        )
        boundary = self.seed_member(
            self.project_id, status=INVITED, expires_at=now, invited_at=T0
        )
        last_moment = self.seed_member(
            self.project_id, status=INVITED, expires_at=now + US, invited_at=T0
        )

        invites = await self.run_store(store.list_open_invites, self.project_id, now)

        self.assertEqual([m.user_id for m in invites], [last_moment])
        self.assertNotIn(expired, [m.user_id for m in invites])
        self.assertNotIn(boundary, [m.user_id for m in invites])

    async def test_open_invitations_are_ordered_and_exclude_members(self):
        second = self.seed_member(
            self.project_id, status=INVITED, invited_at=T0 + timedelta(hours=1)
        )
        first = self.seed_member(self.project_id, status=INVITED, invited_at=T0)
        tie = sorted(
            [
                self.seed_member(
                    self.project_id, status=INVITED, invited_at=T0 + timedelta(hours=2)
                )
                for _ in range(2)
            ]
        )
        self.seed_member(self.project_id)  # an accepted member
        self.seed_member(self.seed_project(), status=INVITED)  # another project

        invites = await self.run_store(store.list_open_invites, self.project_id, T0)

        self.assertEqual([m.user_id for m in invites], [first, second, *tie])
        self.assertTrue(all(m.status is INVITED for m in invites))
        self.assertTrue(all(m.joined_at is None for m in invites))

    async def test_count_members_counts_members_and_open_invitations(self):
        now = T0 + timedelta(days=5)
        self.seed_member(self.project_id)
        self.seed_member(self.project_id, role=MANAGER)
        self.seed_member(self.project_id, status=INVITED, expires_at=now + US)
        # Not counted: expired (also exactly at the boundary), other project.
        self.seed_member(self.project_id, status=INVITED, expires_at=now)
        self.seed_member(self.project_id, status=INVITED, expires_at=now - US)
        self.seed_member(self.seed_project())

        self.assertEqual(
            await self.run_store(store.count_members, self.project_id, now), 3
        )

    async def test_count_members_of_an_empty_project_is_zero(self):
        self.assertEqual(
            await self.run_store(store.count_members, self.project_id, T0), 0
        )


@requires_postgres
class ListProjectsOfTest(StoreTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.user = self.seed_user()

    def join(self, status=ProjectStatus.ACTIVE, role=CONTRIBUTOR, **values) -> UUID:
        project_id = self.seed_project(status, **values)
        self.seed_member(project_id, self.user, role)
        return project_id

    async def list(self, status=ProjectStatus.ACTIVE, limit=50, offset=0, user=None):
        return await self.run_store(
            store.list_projects_of, user or self.user, status, limit, offset
        )

    async def test_it_returns_the_projects_of_the_status_the_user_is_a_member_of(self):
        mine = self.join(name="Mine")
        self.join(ProjectStatus.ARCHIVED, name="Archived")
        self.seed_project(name="Not a member")

        projects = await self.list()

        self.assertEqual([p.id for p in projects], [mine])
        self.assertEqual(projects[0].name, "Mine")
        self.assertIsInstance(projects[0], Project)

    async def test_the_status_filter_selects_archived_projects_separately(self):
        self.join(name="Active")
        archived = self.join(ProjectStatus.ARCHIVED, name="Archived")
        self.assertEqual(
            [p.id for p in await self.list(ProjectStatus.ARCHIVED)], [archived]
        )

    async def test_every_role_sees_active_and_archived_projects(self):
        for role in ProjectRole:
            with self.subTest(role=role.value):
                user = self.seed_user()
                a = self.seed_project()
                b = self.seed_project(ProjectStatus.ARCHIVED)
                self.seed_member(a, user, role)
                self.seed_member(b, user, role)
                self.assertEqual([p.id for p in await self.list(user=user)], [a])
                self.assertEqual(
                    [p.id for p in await self.list(ProjectStatus.ARCHIVED, user=user)],
                    [b],
                )

    async def test_pending_deletion_is_listed_for_managers_only(self):
        managed = self.join(PENDING, role=MANAGER, name="Managed")
        self.join(PENDING, role=CONTRIBUTOR, name="Contributed")
        self.join(PENDING, role=VIEWER, name="Viewed")

        projects = await self.list(PENDING)

        self.assertEqual([p.id for p in projects], [managed])
        self.assertIsNotNone(projects[0].deletion_scheduled_at)

    async def test_an_invitation_shows_nothing(self):
        project_id = self.seed_project()
        self.seed_member(project_id, self.user, MANAGER, INVITED)
        self.assertEqual(await self.list(), [])

    async def test_other_users_memberships_are_not_shown(self):
        other = self.seed_user()
        project_id = self.seed_project()
        self.seed_member(project_id, other, MANAGER)
        self.assertEqual(await self.list(), [])
        self.assertEqual([p.id for p in await self.list(user=other)], [project_id])

    async def test_a_deleted_project_is_never_listed(self):
        deleted = self.seed_project(ProjectStatus.DELETED)
        self.seed_member(deleted, self.user, MANAGER)
        for status in (ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED, PENDING):
            self.assertEqual(await self.list(status), [])

    async def test_newest_first_then_by_id(self):
        old = self.join(created_at=T0 - timedelta(days=2))
        new = self.join(created_at=T0)
        tie = sorted(self.join(created_at=T0 - timedelta(days=1)) for _ in range(2))

        self.assertEqual([p.id for p in await self.list()], [new, *tie, old])

    async def test_limit_and_offset_are_applied_after_the_ordering(self):
        ids = [self.join(created_at=T0 - timedelta(days=n)) for n in range(5)]
        # newest first: ids[0] is the newest.
        self.assertEqual([p.id for p in await self.list(limit=2)], ids[:2])
        self.assertEqual([p.id for p in await self.list(limit=2, offset=2)], ids[2:4])
        self.assertEqual([p.id for p in await self.list(limit=2, offset=4)], ids[4:])
        self.assertEqual(await self.list(limit=2, offset=5), [])
        self.assertEqual([p.id for p in await self.list(limit=200)], ids)

    async def test_a_user_without_projects_gets_an_empty_list(self):
        self.assertEqual(await self.list(), [])
        self.assertEqual(await self.list(user=uuid4()), [])


@requires_postgres
class RolesOfTest(StoreTestCase):
    async def test_it_maps_projects_to_the_roles_of_accepted_memberships(self):
        user = self.seed_user()
        a, b, c = (
            self.seed_project(),
            self.seed_project(ProjectStatus.ARCHIVED),
            self.seed_project(PENDING),
        )
        self.seed_member(a, user, MANAGER)
        self.seed_member(b, user, VIEWER)
        self.seed_member(c, user, CONTRIBUTOR)

        roles = await self.run_store(store.roles_of, user)

        self.assertEqual(roles, {a: MANAGER, b: VIEWER, c: CONTRIBUTOR})
        self.assertTrue(all(isinstance(r, ProjectRole) for r in roles.values()))

    async def test_invitations_and_deleted_projects_do_not_count(self):
        user = self.seed_user()
        invited = self.seed_project()
        self.seed_member(invited, user, MANAGER, INVITED)
        deleted = self.seed_project(ProjectStatus.DELETED)
        self.seed_member(deleted, user, MANAGER)
        real = self.seed_project()
        self.seed_member(real, user, VIEWER)

        self.assertEqual(await self.run_store(store.roles_of, user), {real: VIEWER})

    async def test_other_users_roles_are_not_included(self):
        user, other = self.seed_user(), self.seed_user()
        project_id = self.seed_project()
        self.seed_member(project_id, other, MANAGER)
        self.assertEqual(await self.run_store(store.roles_of, user), {})
        self.assertEqual(
            await self.run_store(store.roles_of, other), {project_id: MANAGER}
        )

    async def test_an_unknown_user_has_no_roles(self):
        self.assertEqual(await self.run_store(store.roles_of, uuid4()), {})


@requires_postgres
class OpenInvitesOfTest(StoreTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.user = self.seed_user()

    async def test_it_describes_the_open_invitations_of_the_user(self):
        project_id = self.seed_project(name="Alpha")
        self.seed_member(
            project_id,
            self.user,
            CONTRIBUTOR,
            INVITED,
            invited_at=T0,
            expires_at=T0 + INVITE_TTL,
        )

        invites = await self.run_store(store.list_open_invites_of, self.user, T0)

        self.assertEqual(
            invites,
            [PendingInvite(project_id, "Alpha", CONTRIBUTOR, T0, T0 + INVITE_TTL)],
        )

    async def test_an_invitation_is_open_strictly_before_its_expiry(self):
        now = T0 + timedelta(days=3)
        cases = {"expired": now - US, "boundary": now, "open": now + US}
        projects = {}
        for label, expires in cases.items():
            projects[label] = self.seed_project(name=label)
            self.seed_member(
                projects[label],
                self.user,
                VIEWER,
                INVITED,
                invited_at=T0,
                expires_at=expires,
            )

        invites = await self.run_store(store.list_open_invites_of, self.user, now)

        self.assertEqual([i.project_id for i in invites], [projects["open"]])

    async def test_only_active_and_archived_projects_are_shown(self):
        active = self.seed_project(ProjectStatus.ACTIVE, name="Active")
        archived = self.seed_project(ProjectStatus.ARCHIVED, name="Archived")
        pending = self.seed_project(PENDING, name="Pending")
        deleted = self.seed_project(ProjectStatus.DELETED)
        for project_id in (active, archived, pending, deleted):
            self.seed_member(project_id, self.user, VIEWER, INVITED)

        invites = await self.run_store(store.list_open_invites_of, self.user, T0)

        self.assertEqual({i.project_id for i in invites}, {active, archived})
        self.assertEqual({i.project_name for i in invites}, {"Active", "Archived"})

    async def test_memberships_and_other_users_invitations_are_not_shown(self):
        project_id = self.seed_project()
        self.seed_member(project_id, self.user, MANAGER, ACTIVE)
        other_project = self.seed_project()
        self.seed_member(other_project, self.seed_user(), VIEWER, INVITED)

        self.assertEqual(
            await self.run_store(store.list_open_invites_of, self.user, T0), []
        )

    async def test_they_are_ordered_by_invitation_time_then_project_id(self):
        second = self.seed_project(name="Second")
        first = self.seed_project(name="First")
        ties = sorted(self.seed_project() for _ in range(2))
        self.seed_member(
            second, self.user, VIEWER, INVITED, invited_at=T0 + timedelta(hours=1)
        )
        self.seed_member(first, self.user, VIEWER, INVITED, invited_at=T0)
        for project_id in ties:
            self.seed_member(
                project_id,
                self.user,
                VIEWER,
                INVITED,
                invited_at=T0 + timedelta(hours=2),
            )

        invites = await self.run_store(store.list_open_invites_of, self.user, T0)

        self.assertEqual([i.project_id for i in invites], [first, second, *ties])


@requires_postgres
class UserIsActiveTest(StoreTestCase):
    async def test_only_an_active_user_is_active(self):
        for status, expected in [
            ("active", True),
            ("invited", False),
            ("pending_deletion", False),
            ("deleted", False),
        ]:
            with self.subTest(status=status):
                user = self.seed_user(status=status)
                self.assertIs(
                    await self.run_store(store.user_is_active, user), expected
                )

    async def test_an_unknown_id_is_not_active(self):
        self.assertIs(await self.run_store(store.user_is_active, uuid4()), False)

    async def test_an_admin_and_a_user_are_both_active_users(self):
        for role in ("user", "admin"):
            with self.subTest(role=role):
                user = self.seed_user(system_role=role)
                self.assertIs(await self.run_store(store.user_is_active, user), True)


if __name__ == "__main__":
    import unittest

    unittest.main()
