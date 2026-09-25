"""The administrator's list of all projects on a real PostgreSQL (Issue #84).

``ProjectService.list_all_projects``: an Owner / Admin (``admin.projects.manage``)
finds every project that is not Deleted, with its status and its deletion
deadline, whether or not they are a member, and never sees what is inside a
project. Rows are seeded and read back with SQL, so no test depends on another
service method being right (the lifecycle methods are used only where the point
is that the list follows what they write). The clock is injected.

Needs ``PAW_TEST_DATABASE_URL`` (skipped otherwise).
"""

import asyncio
import base64
import dataclasses
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from paw_backend.authz import (
    Authorizer,
    PostgresAuditSink,
    Principal,
    SystemRole,
)
from paw_backend.authz.roles import ProjectRole
from paw_backend.db import Database
from paw_backend.projects import (
    AdminProjectPage,
    AdminProjectSummary,
    InputProblem,
    InvalidProjectInputError,
    MemberStatus,
    ProjectPermissionDeniedError,
    ProjectService,
    ProjectStatus,
)
from paw_backend.projects.cursor import MAX_MICROS, MIN_MICROS, decode_cursor
from paw_backend.projects.cursor import encode_cursor as encode
from paw_backend.projects.limits import DELETION_RETENTION

from .projects_support import T0, PostgresProjectTestCase, requires_postgres
from .support import make_settings
from .task_support import TEST_DATABASE_URL

S = ProjectStatus
MINUTE = timedelta(minutes=1)
DELETED_NAME = "Deleted Project"
SECRET_NAME = "Quarterly Reorganisation"
SECRET_DESCRIPTION = "DESCRIPTION-CANARY-17c3 confidential plan"


class AdminListTestCase(PostgresProjectTestCase):
    """A service, an Owner and an Admin, and helpers to seed many projects."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.owner = self.actor(self.seed_user(system_role="owner"), SystemRole.OWNER)
        self.admin = self.actor(self.seed_user(system_role="admin"), SystemRole.ADMIN)

    def connect(self) -> Database:
        """A database of its own, closed when the test ends (the app role in
        ``test_projects_admin_grants.py``)."""
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        return database

    # -- seeding ------------------------------------------------------------------

    def seed_rows(
        self, rows: list[tuple[UUID, str, str, datetime]]
    ) -> list[tuple[UUID, datetime]]:
        """Insert ``(id, name, status, created_at)`` rows in one statement.

        Pending deletion / Deleted rows get a deadline 30 days after
        ``created_at`` (a tombstone has the fixed name and no description).
        """
        values = []
        for project_id, name, status, created_at in rows:
            started = created_at if status in ("pending_deletion", "deleted") else None
            values.append(
                {
                    "id": project_id,
                    "name": DELETED_NAME if status == "deleted" else name,
                    "status": status,
                    "created": created_at,
                    "started": started,
                    "scheduled": started and started + DELETION_RETENTION,
                    "deleted": created_at + DELETION_RETENTION
                    if status == "deleted"
                    else None,
                }
            )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects (id, name, status, created_at, updated_at,"
                    " deletion_started_at, deletion_scheduled_at, deleted_at)"
                    " VALUES (:id, :name, :status, :created, :created, :started,"
                    " :scheduled, :deleted)"
                ),
                values,
            )
        return [(v["id"], v["created"]) for v in values]

    def seed_many(
        self,
        count: int,
        *,
        status: str = "active",
        start: datetime = T0,
        step: timedelta = MINUTE,
        name: str = "Project",
    ) -> list[tuple[UUID, datetime]]:
        return self.seed_rows(
            [(uuid4(), f"{name} {n}", status, start + n * step) for n in range(count)]
        )

    @staticmethod
    def newest_first(rows: list[tuple[UUID, datetime]]) -> list[UUID]:
        """The ids in the order of the list: ``created_at`` then ``id``, descending."""
        return [
            project_id
            for project_id, _ in sorted(
                rows, key=lambda row: (row[1], row[0]), reverse=True
            )
        ]

    # -- reading ----------------------------------------------------------------

    async def pages(
        self, service: ProjectService | None = None, actor=None, **arguments
    ) -> list[AdminProjectPage]:
        """Every page of one traversal, following ``next_cursor``."""
        service = service or self.service
        actor = actor or self.admin
        found: list[AdminProjectPage] = []
        cursor = None
        while True:
            page = await service.list_all_projects(actor, cursor=cursor, **arguments)
            found.append(page)
            if page.next_cursor is None:
                return found
            self.assertLess(len(found), 1000, "the traversal does not end")
            cursor = page.next_cursor

    @staticmethod
    def ids(pages: list[AdminProjectPage]) -> list[UUID]:
        return [summary.id for page in pages for summary in page.projects]


@requires_postgres
class AdminListContentTest(AdminListTestCase):
    async def test_an_admin_who_is_no_member_lists_every_project(self):
        creator = self.seed_user()
        active = self.seed_project(name="Active", created_at=T0, created_by=creator)
        archived = self.seed_project(
            S.ARCHIVED, name="Archived", created_at=T0 + MINUTE
        )
        pending = self.seed_project(
            S.PENDING_DELETION,
            name="Pending",
            created_at=T0 + 2 * MINUTE,
            deletion_started_at=T0 + 3 * MINUTE,
        )
        self.seed_project(S.DELETED, created_at=T0 + 4 * MINUTE)
        # Members and invitations exist, none of them is the admin.
        self.seed_team(active)
        self.seed_member(active, status=MemberStatus.INVITED)

        for actor in (self.admin, self.owner):
            with self.subTest(role=actor.system_role):
                page = await self.service.list_all_projects(actor)
                self.assertEqual(
                    page,
                    AdminProjectPage(
                        projects=(
                            AdminProjectSummary(
                                pending,
                                "Pending",
                                S.PENDING_DELETION,
                                T0 + 2 * MINUTE,
                                T0 + 3 * MINUTE + DELETION_RETENTION,
                            ),
                            AdminProjectSummary(
                                archived, "Archived", S.ARCHIVED, T0 + MINUTE, None
                            ),
                            AdminProjectSummary(active, "Active", S.ACTIVE, T0, None),
                        ),
                        next_cursor=None,
                    ),
                )

    async def test_the_summary_holds_no_project_content(self):
        creator = self.seed_user()
        project = self.seed_project(
            name=SECRET_NAME, description=SECRET_DESCRIPTION, created_by=creator
        )
        self.seed_team(project)
        (summary,) = (await self.service.list_all_projects(self.admin)).projects
        self.assertEqual(
            {field.name for field in dataclasses.fields(AdminProjectSummary)},
            {"id", "name", "status", "created_at", "deletion_scheduled_at"},
        )
        rendered = json.dumps(dataclasses.asdict(summary), default=str)
        self.assertIn(SECRET_NAME, rendered)  # the name is what the list is for
        for private in (SECRET_DESCRIPTION, str(creator), "manager"):
            self.assertNotIn(private, rendered)
        # A page holds summaries and a cursor: no other attribute to read from.
        self.assertEqual(
            {field.name for field in dataclasses.fields(AdminProjectPage)},
            {"projects", "next_cursor"},
        )

    async def test_a_deleted_project_is_never_listed(self):
        self.seed_project(S.DELETED)
        self.seed_project(S.DELETED, created_at=T0 + MINUTE)
        alive = self.seed_project(name="Alive", created_at=T0 - MINUTE)
        for status in (None, S.ACTIVE, S.ARCHIVED, S.PENDING_DELETION):
            with self.subTest(status=status):
                pages = await self.pages(status=status)
                expected = [alive] if status in (None, S.ACTIVE) else []
                self.assertEqual(self.ids(pages), expected)
                names = [s.name for page in pages for s in page.projects]
                self.assertNotIn(DELETED_NAME, names)

    async def test_a_status_filter_returns_exactly_that_status(self):
        by_status = {
            S.ACTIVE: self.seed_project(S.ACTIVE, created_at=T0),
            S.ARCHIVED: self.seed_project(S.ARCHIVED, created_at=T0 + MINUTE),
            S.PENDING_DELETION: self.seed_project(
                S.PENDING_DELETION, created_at=T0 + 2 * MINUTE
            ),
        }
        self.seed_project(S.DELETED, created_at=T0 + 3 * MINUTE)
        for status, project in by_status.items():
            for argument in (status, status.value):
                with self.subTest(status=status, argument=argument):
                    page = await self.service.list_all_projects(
                        self.admin, status=argument
                    )
                    self.assertEqual([p.id for p in page.projects], [project])
                    self.assertEqual([p.status for p in page.projects], [status])
        everything = await self.service.list_all_projects(self.admin)
        self.assertEqual(
            [p.id for p in everything.projects],
            [
                by_status[S.PENDING_DELETION],
                by_status[S.ARCHIVED],
                by_status[S.ACTIVE],
            ],
        )

    async def test_the_deletion_deadline_is_only_set_while_pending(self):
        first = self.seed_project(
            S.PENDING_DELETION,
            created_at=T0,
            deletion_started_at=T0 + timedelta(days=1),
        )
        second = self.seed_project(
            S.PENDING_DELETION,
            created_at=T0 + MINUTE,
            deletion_started_at=T0 + timedelta(days=5, hours=3),
        )
        active = self.seed_project(S.ACTIVE, created_at=T0 + 2 * MINUTE)
        archived = self.seed_project(S.ARCHIVED, created_at=T0 + 3 * MINUTE)
        page = await self.service.list_all_projects(self.owner)
        deadlines = {p.id: p.deletion_scheduled_at for p in page.projects}
        self.assertEqual(
            deadlines,
            {
                first: T0 + timedelta(days=31),
                second: T0 + timedelta(days=35, hours=3),
                active: None,
                archived: None,
            },
        )

    async def test_the_list_follows_the_lifecycle_and_finds_the_id_to_restore(self):
        # The reason for the list: an Admin who is no member finds a project that
        # is pending deletion and restores it, without asking a Manager for its id.
        project = self.seed_project(name="Lost", created_at=T0)
        manager = self.seed_manager(project)
        await self.service.begin_deletion(self.actor(manager), project, "Lost")

        pending = await self.service.list_all_projects(
            self.admin, status=S.PENDING_DELETION
        )
        (found,) = pending.projects
        self.assertEqual(
            (found.id, found.name, found.status, found.deletion_scheduled_at),
            (project, "Lost", S.PENDING_DELETION, T0 + DELETION_RETENTION),
        )
        restored = await self.service.restore(self.admin, found.id)
        self.assertEqual(restored.status, S.ARCHIVED)

        after = await self.service.list_all_projects(self.admin)
        self.assertEqual(
            [(p.id, p.status, p.deletion_scheduled_at) for p in after.projects],
            [(project, S.ARCHIVED, None)],
        )
        # Deleted for good (the 30 days are over): it leaves the list.
        await self.service.begin_deletion(self.admin, project, "Lost")
        self.clock.advance(days=30)
        purged = await self.service.purge_expired()
        self.assertEqual(purged.purged, (project,))
        self.assertEqual(
            (await self.service.list_all_projects(self.admin)).projects, ()
        )

    async def test_the_member_only_list_is_unchanged(self):
        mine = self.seed_project(name="Mine", created_at=T0)
        self.seed_project(name="Theirs", created_at=T0 + MINUTE)
        self.seed_manager(mine, user_id=self.admin.user_id)
        # The Admin is a member of one project: list_projects shows only that
        # one; list_all_projects shows both.
        own = await self.service.list_projects(self.admin)
        self.assertEqual([p.id for p in own], [mine])
        every = await self.service.list_all_projects(self.admin)
        self.assertEqual(len(every.projects), 2)
        # An Owner with no membership still sees nothing in the member-only list.
        self.assertEqual(await self.service.list_projects(self.owner), ())
        self.assertEqual(
            len((await self.service.list_all_projects(self.owner)).projects), 2
        )

    async def test_an_empty_workspace_is_an_empty_last_page(self):
        page = await self.service.list_all_projects(self.owner)
        self.assertEqual(page, AdminProjectPage(projects=(), next_cursor=None))
        self.assertEqual(len(self.sink.events), 1)

    async def test_the_read_does_not_change_anything_or_wait_for_a_lock(self):
        project = self.seed_project(name="Busy")
        self.seed_team(project)
        before = self.snapshot()
        connection, transaction = self.lock_project(project)
        # Another transaction holds the row FOR UPDATE: a plain read is not blocked.
        page = await asyncio.wait_for(
            self.service.list_all_projects(self.admin), timeout=20
        )
        self.assertEqual([p.id for p in page.projects], [project])
        transaction.rollback()
        self.assertEqual(self.snapshot(), before)

    async def test_the_result_does_not_depend_on_the_callers_project_roles(self):
        project = self.seed_project()
        with_roles = Principal(
            self.admin.user_id, SystemRole.ADMIN, {uuid4(): ProjectRole.VIEWER}
        )
        page = await self.service.list_all_projects(with_roles)
        self.assertEqual([p.id for p in page.projects], [project])

    async def test_a_user_who_manages_every_project_is_denied(self):
        project = self.seed_project()
        user = self.seed_manager(project)
        with self.assertRaises(ProjectPermissionDeniedError) as raised:
            await self.service.list_all_projects(self.actor(user))
        self.assertEqual(raised.exception.reason.value, "capability_not_granted")
        self.assertEqual(
            self.audit(), [("admin.projects.manage", "deny", "capability_not_granted")]
        )
        self.assertEqual(self.sink.events[0].actor_id, user)


@requires_postgres
class AdminListPagingTest(AdminListTestCase):
    async def test_the_pages_cover_every_project_once_in_order(self):
        rows = self.seed_many(45)
        pages = await self.pages(limit=10)
        self.assertEqual([len(p.projects) for p in pages], [10, 10, 10, 10, 5])
        self.assertEqual(self.ids(pages), self.newest_first(rows))
        self.assertEqual(
            [p.next_cursor is None for p in pages], [False, False, False, False, True]
        )
        # Every page is its own audited call.
        self.assertEqual(len(self.sink.events), 5)

    async def test_the_last_full_page_has_no_cursor_and_there_is_no_empty_page(self):
        rows = self.seed_many(40)
        pages = await self.pages(limit=10)
        self.assertEqual([len(p.projects) for p in pages], [10, 10, 10, 10])
        self.assertIsNone(pages[-1].next_cursor)
        self.assertEqual(self.ids(pages), self.newest_first(rows))
        one = await self.service.list_all_projects(self.admin, limit=40)
        self.assertEqual((len(one.projects), one.next_cursor), (40, None))
        two = await self.service.list_all_projects(self.admin, limit=39)
        self.assertEqual(len(two.projects), 39)
        self.assertIsNotNone(two.next_cursor)

    async def test_one_project_per_page(self):
        rows = self.seed_many(6)
        pages = await self.pages(limit=1)
        self.assertEqual([len(p.projects) for p in pages], [1] * 6)
        self.assertEqual(self.ids(pages), self.newest_first(rows))

    async def test_projects_created_at_the_same_instant_are_ordered_by_id(self):
        rows = self.seed_rows(
            [(uuid4(), f"Same {n}", "active", T0) for n in range(12)]
            + [(uuid4(), "Later", "active", T0 + MINUTE)]
        )
        expected = self.newest_first(rows)
        self.assertEqual(expected[0], rows[-1][0])  # newest first
        self.assertEqual(expected[1:], sorted(expected[1:], reverse=True))
        for limit in (1, 2, 5, 13):
            with self.subTest(limit=limit):
                self.assertEqual(self.ids(await self.pages(limit=limit)), expected)

    async def test_microsecond_precision_is_kept_in_the_position(self):
        base = T0.replace(microsecond=123456)
        rows = self.seed_rows(
            [
                (uuid4(), f"Tick {n}", "active", base + timedelta(microseconds=n))
                for n in range(9)
            ]
        )
        pages = await self.pages(limit=2)
        self.assertEqual(self.ids(pages), self.newest_first(rows))
        self.assertEqual(
            [s.created_at for p in pages for s in p.projects],
            [base + timedelta(microseconds=n) for n in range(8, -1, -1)],
        )

    async def test_the_default_and_the_largest_page_are_bounded(self):
        rows = self.seed_many(260, name="N" * 90)
        default = await self.service.list_all_projects(self.admin)
        self.assertEqual(len(default.projects), 50)
        largest = await self.service.list_all_projects(self.admin, limit=200)
        self.assertEqual(len(largest.projects), 200)
        self.assertIsNotNone(largest.next_cursor)
        rest = await self.service.list_all_projects(
            self.admin, limit=200, cursor=largest.next_cursor
        )
        self.assertEqual(len(rest.projects), 60)
        self.assertIsNone(rest.next_cursor)
        self.assertEqual(
            [s.id for s in largest.projects + rest.projects], self.newest_first(rows)
        )
        with self.assertRaises(InvalidProjectInputError) as raised:
            await self.service.list_all_projects(self.admin, limit=201)
        self.assertEqual(raised.exception.problem, InputProblem.OUT_OF_RANGE)

    async def test_paging_within_a_status_filter(self):
        active = self.seed_many(7, status="active", name="A")
        archived = self.seed_many(6, status="archived", start=T0 + timedelta(hours=1))
        pending = self.seed_many(
            5, status="pending_deletion", start=T0 + timedelta(hours=2)
        )
        self.seed_many(4, status="deleted", start=T0 + timedelta(hours=3))
        for status, rows in (
            (S.ACTIVE, active),
            (S.ARCHIVED, archived),
            (S.PENDING_DELETION, pending),
            (None, active + archived + pending),
        ):
            with self.subTest(status=status):
                pages = await self.pages(status=status, limit=4)
                self.assertEqual(self.ids(pages), self.newest_first(rows))

    async def test_the_cursor_is_stateless_another_service_continues_it(self):
        rows = self.seed_many(15)
        first = await self.service.list_all_projects(self.admin, limit=6)
        other = self.new_service()
        second = await other.list_all_projects(
            self.owner, limit=6, cursor=first.next_cursor
        )
        third = await self.new_service().list_all_projects(
            self.admin, limit=6, cursor=second.next_cursor
        )
        self.assertEqual(
            [s.id for page in (first, second, third) for s in page.projects],
            self.newest_first(rows),
        )
        self.assertIsNone(third.next_cursor)

    async def test_the_cursor_stands_for_the_last_row_of_the_page(self):
        rows = self.seed_many(8)
        first = await self.service.list_all_projects(self.admin, limit=3)
        last = first.projects[-1]
        keyset = decode_cursor(first.next_cursor, None)
        self.assertEqual((keyset.id, keyset.created_at), (last.id, last.created_at))
        self.assertEqual(last.id, self.newest_first(rows)[2])

    async def test_a_position_is_exclusive_and_need_not_belong_to_a_project(self):
        rows = self.seed_many(5)
        order = self.newest_first(rows)
        at = dict(rows)
        # Exactly a row: the rows strictly after it.
        after_second = await self.service.list_all_projects(
            self.admin, cursor=encode(None, at[order[1]], order[1])
        )
        self.assertEqual([s.id for s in after_second.projects], order[2:])
        # A position between two rows (same instant as a row, another id).
        smaller = UUID(int=order[1].int - 1)
        between = await self.service.list_all_projects(
            self.admin, cursor=encode(None, at[order[1]], smaller)
        )
        self.assertEqual(
            [s.id for s in between.projects],
            [i for i in order if (at[i], i) < (at[order[1]], smaller)],
        )
        # Before everything, and after everything.
        newest = at[order[0]]
        top = await self.service.list_all_projects(
            self.admin, cursor=encode(None, newest + MINUTE, uuid4())
        )
        self.assertEqual([s.id for s in top.projects], order)
        bottom = await self.service.list_all_projects(
            self.admin, cursor=encode(None, min(at.values()) - MINUTE, uuid4())
        )
        self.assertEqual(bottom, AdminProjectPage((), None))

    async def test_the_extreme_positions_reach_postgres_without_an_error(self):
        rows = self.seed_many(3)
        nil = UUID(int=0)
        top = await self.service.list_all_projects(
            self.admin, cursor=encode(None, datetime.max.replace(tzinfo=UTC), nil)
        )
        self.assertEqual([s.id for s in top.projects], self.newest_first(rows))
        bottom = await self.service.list_all_projects(
            self.admin, cursor=encode(None, datetime.min.replace(tzinfo=UTC), nil)
        )
        self.assertEqual(bottom.projects, ())
        self.assertEqual(
            (MIN_MICROS, MAX_MICROS), (-62135596800000000, 253402300799999999)
        )

    async def test_a_hostile_cursor_is_refused_and_touches_nothing(self):
        self.seed_many(3)
        before = self.snapshot()
        events = len(self.sink.events)
        hostile = [
            "'; DROP TABLE projects; --",
            "1.all.0.00000000-0000-0000-0000-000000000000",  # not base64 text
            "A" * 500,
            "\x00",
            "%s" * 30,
            "\ud800",
            encode(S.ARCHIVED, T0, uuid4()),  # another list's cursor
        ]
        for payload in (
            "1.all.0.00000000-0000-0000-0000-000000000000'; DROP TABLE projects;--",
            "1.all.1 OR 1=1.00000000-0000-0000-0000-000000000000",
            "1.all.0.00000000-0000-0000-0000-000000000000\x00",
        ):
            hostile.append(
                base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
            )
        for cursor in hostile:
            with self.subTest(cursor=cursor[:30]):
                with self.assertRaises(InvalidProjectInputError) as raised:
                    await self.service.list_all_projects(self.admin, cursor=cursor)
                self.assertEqual(raised.exception.field, "cursor")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.sink.events), events)  # no decision, no event
        self.assertEqual(self.table_count("projects"), 3)

    async def test_a_cursor_survives_the_row_it_names_being_purged(self):
        rows = self.seed_many(8)
        order = self.newest_first(rows)
        first = await self.service.list_all_projects(self.admin, limit=3)
        # The row the cursor stands for becomes a tombstone before the next page.
        self.set_project(
            order[2],
            status="deleted",
            name=DELETED_NAME,
            deleted_at=T0 + DELETION_RETENTION,
            deletion_started_at=T0,
            deletion_scheduled_at=T0 + DELETION_RETENTION,
        )
        second = await self.service.list_all_projects(
            self.admin, limit=10, cursor=first.next_cursor
        )
        self.assertEqual([s.id for s in second.projects], order[3:])


@requires_postgres
class AdminListConcurrentChangesTest(AdminListTestCase):
    """Rows come, change and go between the pages of one traversal."""

    async def test_changes_between_two_pages_never_repeat_or_skip_a_row(self):
        seeded = self.seed_many(30)
        order = self.newest_first(seeded)
        model = {i: ("active", at) for i, at in seeded}
        first = await self.service.list_all_projects(self.admin, limit=10)
        self.assertEqual([s.id for s in first.projects], order[:10])
        cursor_id, cursor_at = order[9], model[order[9]][1]

        # 1. Newer than everything (ahead of the cursor), and two rows at the
        #    cursor's own instant: one with a larger id (ahead), one with a
        #    smaller id (behind).
        (newer,) = self.seed_rows([(uuid4(), "Newer", "active", T0 + 99 * MINUTE)])
        (tie_ahead,) = self.seed_rows(
            [(UUID(int=cursor_id.int + 1), "Tie ahead", "active", cursor_at)]
        )
        (tie_behind,) = self.seed_rows(
            [(UUID(int=cursor_id.int - 1), "Tie behind", "active", cursor_at)]
        )
        # 2. Older than everything, and in the middle of the unseen rows.
        (oldest,) = self.seed_rows([(uuid4(), "Oldest", "active", T0 - 5 * MINUTE)])
        middle_at = model[order[15]][1] + timedelta(seconds=1)
        (middle,) = self.seed_rows([(uuid4(), "Middle", "active", middle_at)])
        for new in (newer, tie_ahead, tie_behind, oldest, middle):
            model[new[0]] = ("active", new[1])
        # 3. One unseen row is purged, one seen row is purged, one unseen row is
        #    archived (still listed, with its new status).
        for victim in (order[20], order[3]):
            self.set_project(
                victim,
                status="deleted",
                name=DELETED_NAME,
                deletion_started_at=T0,
                deletion_scheduled_at=T0 + DELETION_RETENTION,
                deleted_at=T0 + DELETION_RETENTION,
            )
            model[victim] = ("deleted", model[victim][1])
        self.set_project(order[25], status="archived")
        model[order[25]] = ("archived", model[order[25]][1])

        continued: list[AdminProjectPage] = []
        cursor = first.next_cursor
        while cursor is not None:
            page = await self.service.list_all_projects(
                self.admin, limit=10, cursor=cursor
            )
            continued.append(page)
            cursor = page.next_cursor

        expected = [
            i
            for (at, i) in sorted(
                ((at, i) for i, (status, at) in model.items() if status != "deleted"),
                reverse=True,
            )
            if (at, i) < (cursor_at, cursor_id)
        ]
        got = self.ids(continued)
        self.assertEqual(got, expected)
        self.assertEqual(len(got), len(set(got)))  # nothing twice
        # Ahead of the cursor: not part of this traversal.
        for ahead in (newer[0], tie_ahead[0]):
            self.assertNotIn(ahead, got)
        # Behind it (and still there): seen, wherever it was inserted.
        for behind in (tie_behind[0], oldest[0], middle[0]):
            self.assertIn(behind, got)
        # Every unseen row that still exists is seen exactly once.
        unseen = [i for i in order[10:] if model[i][0] != "deleted"]
        self.assertEqual([i for i in got if i in set(unseen)], unseen)
        self.assertNotIn(order[20], got)
        self.assertEqual(
            {s.id: s.status for p in continued for s in p.projects}[order[25]],
            S.ARCHIVED,
        )
        # A fresh traversal starts at the newest and sees the late arrivals.
        fresh = self.ids(await self.pages(limit=10))
        self.assertIn(newer[0], fresh)
        self.assertEqual(fresh[0], newer[0])
        self.assertEqual(len(fresh), len(set(fresh)))

        # The control: the same changes make an OFFSET page repeat a row (a new
        # row ahead of the cursor pushes the last row of page 1 into page 2), so
        # this scenario is one that a paging by offset would fail.
        with self.engine.connect() as connection:
            offset_page = [
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT id FROM projects WHERE status <> 'deleted'"
                        " ORDER BY created_at DESC, id DESC OFFSET 10 LIMIT 10"
                    )
                )
            ]
        self.assertTrue(set(offset_page) & {s.id for s in first.projects})

    async def test_a_status_change_moves_a_row_between_filtered_lists_cleanly(self):
        seeded = self.seed_many(12)
        order = self.newest_first(seeded)
        first = await self.service.list_all_projects(
            self.admin, status=S.ACTIVE, limit=4
        )
        self.assertEqual([s.id for s in first.projects], order[:4])
        # Two unseen active rows are archived while paging: they leave the ACTIVE
        # list; the rest continues without a gap or a repeat.
        for moved in (order[5], order[9]):
            self.set_project(moved, status="archived")
        got = []
        cursor = first.next_cursor
        while cursor is not None:
            page = await self.service.list_all_projects(
                self.admin, status=S.ACTIVE, limit=4, cursor=cursor
            )
            got += [s.id for s in page.projects]
            cursor = page.next_cursor
        self.assertEqual(got, [i for i in order[4:] if i not in (order[5], order[9])])

    async def test_an_uncommitted_insert_is_not_seen_and_does_not_block(self):
        seeded = self.seed_many(6)
        connection = self.engine.connect()
        self.addCleanup(connection.close)
        transaction = connection.begin()
        self.addCleanup(
            lambda: transaction.rollback() if transaction.is_active else None
        )
        pending = uuid4()
        connection.execute(
            text(
                "INSERT INTO projects (id, name, status, created_at, updated_at)"
                " VALUES (:id, 'Uncommitted', 'active', :at, :at)"
            ),
            {"id": pending, "at": T0 + 50 * MINUTE},
        )
        before = self.ids(await self.pages(limit=4))
        self.assertEqual(before, self.newest_first(seeded))
        transaction.commit()
        after = self.ids(await self.pages(limit=4))
        self.assertEqual(after, [pending, *self.newest_first(seeded)])

    async def test_writers_running_during_a_traversal_break_no_invariant(self):
        seeded = self.seed_many(60)
        seeded_ids = {i for i, _ in seeded}
        oldest_first_arrivals: list[UUID] = []
        stop_at = 150

        def write() -> None:
            """Inserts (newer, older and interleaved), archives and purges."""
            for n in range(stop_at):
                project_id = uuid4()
                match n % 3:
                    case 0:  # newer than every seeded row
                        created = T0 + timedelta(days=1, seconds=n)
                    case 1:  # older than every seeded row
                        created = T0 - timedelta(days=1, seconds=n)
                        oldest_first_arrivals.append(project_id)
                    case _:  # inside the seeded range
                        created = T0 + timedelta(seconds=n * 7 + 1)
                self.seed_rows([(project_id, f"Writer {n}", "active", created)])
                if n % 10 == 0:
                    self.set_project(project_id, status="archived")

        writer = self.spawn(asyncio.to_thread(write))
        got: list[AdminProjectSummary] = []
        cursor = None
        pages = 0
        while True:
            page = await self.service.list_all_projects(
                self.admin, limit=7, cursor=cursor
            )
            pages += 1
            got += page.projects
            if pages == 3:
                # From here on the traversal only reaches the end after the writer
                # is done, so every row older than the seeded ones must be seen.
                await asyncio.wait_for(writer, timeout=120)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        await asyncio.wait_for(writer, timeout=120)

        ids = [s.id for s in got]
        self.assertEqual(len(ids), len(set(ids)), "a project was listed twice")
        keys = [(s.created_at, s.id) for s in got]
        self.assertEqual(keys, sorted(keys, reverse=True), "the order broke")
        self.assertTrue(seeded_ids <= set(ids), "a project that was there was skipped")
        self.assertTrue(set(oldest_first_arrivals) <= set(ids))
        self.assertEqual(len(oldest_first_arrivals), stop_at // 3)


@requires_postgres
class AdminListAuditInPostgresTest(AdminListTestCase):
    """The Authorizer's own audit row: one per call, with no result in it."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # The audit row is written on a connection of its own (the Authorizer's
        # sink), the read on another: neither shares a transaction with the other.
        self.pg_service = ProjectService(
            self.connect(),
            Authorizer(PostgresAuditSink(self.connect()), clock=self.clock),
            clock=self.clock,
        )

    def events(self, actor_id: UUID) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM audit_events WHERE actor_id = :actor"
                    " ORDER BY recorded_at, id"
                ),
                {"actor": actor_id},
            ).mappings()
            return [dict(row) for row in rows]

    async def test_a_call_is_one_row_with_the_actor_and_the_filter_and_no_result(self):
        project = self.seed_project(
            name=SECRET_NAME, description=SECRET_DESCRIPTION, created_by=None
        )
        self.seed_team(project)
        page = await self.pg_service.list_all_projects(self.admin, status=S.ACTIVE)
        self.assertEqual([p.name for p in page.projects], [SECRET_NAME])
        (row,) = self.events(self.admin.user_id)
        self.assertEqual(
            (
                row["action"],
                row["decision"],
                row["reason"],
                row["actor_id"],
                row["actor_role"],
                row["resource_kind"],
            ),
            (
                "admin.projects.manage",
                "allow",
                "granted_by_system_role",
                self.admin.user_id,
                "admin",
                "project_list_active",
            ),
        )
        for column in ("resource_id", "project_id", "repo_id", "agent_id"):
            self.assertIsNone(row[column], column)
        rendered = json.dumps(row, default=str)
        for content in (SECRET_NAME, SECRET_DESCRIPTION, str(project)):
            self.assertNotIn(content, rendered)

        # The next page and a differently filtered call are two more events.
        await self.pg_service.list_all_projects(self.admin)
        await self.pg_service.list_all_projects(self.admin, cursor=None, limit=3)
        kinds = [r["resource_kind"] for r in self.events(self.admin.user_id)]
        self.assertEqual(
            kinds, ["project_list_active", "project_list_all", "project_list_all"]
        )

    async def test_a_denial_is_recorded_and_nothing_is_read(self):
        user = self.actor(self.seed_user())
        self.seed_project(name=SECRET_NAME)
        with self.assertRaises(ProjectPermissionDeniedError):
            await self.pg_service.list_all_projects(user, status="archived")
        (row,) = self.events(user.user_id)
        self.assertEqual(
            (row["decision"], row["reason"], row["action"], row["resource_kind"]),
            (
                "deny",
                "capability_not_granted",
                "admin.projects.manage",
                "project_list_archived",
            ),
        )
        self.assertNotIn(SECRET_NAME, json.dumps(row, default=str))

    async def test_an_invalid_call_writes_no_row(self):
        for arguments in ({"limit": 0}, {"status": "deleted"}, {"cursor": "x"}):
            with self.assertRaises(InvalidProjectInputError):
                await self.pg_service.list_all_projects(self.admin, **arguments)
        self.assertEqual(self.events(self.admin.user_id), [])
