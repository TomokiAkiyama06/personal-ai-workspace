"""Stored notifications on PostgreSQL (issue #188, Decision 0070).

* a user sees their own notifications and those of the audiences their role
  holds now, not another user's, not a resolved one;
* read / dismissed are per user; marking reads only what the user sees; a
  dismissal covers the entry (the earlier notifications of its key) and a newer
  notification of the key is listed again;
* another user's notification cannot be marked read or dismissed (``False`` / 0);
* the purge removes old notifications with their receipts;
* System Health's severity changes become notifications of the Owner / Admin
  audience in the same transaction (with ``notify=True`` only), announced after
  the commit;
* the HTTP routes on a real database (another user's id answers 404).

Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import unittest
import uuid
from datetime import UTC, datetime

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from paw_backend.app import create_app
from paw_backend.authz import Capability, Principal, SystemRole
from paw_backend.authz.deps import install_authz
from paw_backend.db import Database
from paw_backend.health.domain import Component, ComponentHealth, Severity, Status
from paw_backend.health.store import HealthStore
from paw_backend.notifications import (
    Category,
    NewNotification,
    NotificationStore,
    add_in,
    audience_capabilities,
    resolve_in,
)

from .authz_support import InMemoryAuditSink, StaticProvider
from .memory_support import sync_database_url
from .support import make_client, make_settings
from .task_support import TEST_DATABASE_URL, migrate, requires_postgres

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
ADMIN_AUDIENCE = Capability.ADMIN_SYSTEM_HEALTH_VIEW


class NotificationsTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate("head")
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.clean()
        cls.engine.dispose()

    @classmethod
    def clean(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(
                text("TRUNCATE notifications, notification_receipts, health_events")
            )
            connection.execute(text("DELETE FROM users WHERE login_name LIKE 'nt_%'"))

    def database_url(self) -> str:
        return TEST_DATABASE_URL

    async def asyncSetUp(self) -> None:
        self.clean()
        self.database = Database(make_settings(database_url=self.database_url()))
        self.addAsyncCleanup(self.database.dispose)
        self.store = NotificationStore(self.database, timeout_seconds=10)
        self.alice = self.user("user")
        self.bob = self.user("user")
        self.admin = self.user("admin")

    def sql(self, statement: str, **params):
        with self.engine.begin() as connection:
            result = connection.execute(text(statement), params)
            return result.fetchall() if result.returns_rows else []

    def user(self, role: str) -> Principal:
        user_id = uuid.uuid4()
        self.sql(
            "INSERT INTO users (id, login_name, system_role, status,"
            " passkey_required, created_at, updated_at) VALUES (:id, :login,"
            " :role, 'active', :role <> 'user', :now, :now)",
            id=user_id,
            login="nt_" + user_id.hex[:12],
            role=role,
            now=T0,
        )
        return Principal(user_id, SystemRole(role))

    async def add(self, **fields) -> uuid.UUID:
        fields.setdefault("kind", "test.happened")
        fields.setdefault("severity", "warning")
        fields.setdefault("category", Category.SYSTEM)
        notification = NewNotification(**fields)

        async def work(session):
            return await add_in(session, notification)

        return await self.database.run_abortable(work)

    async def page(self, who: Principal):
        return await self.store.page(who.user_id, audience_capabilities(who))

    async def ids(self, who: Principal) -> list[uuid.UUID]:
        return [item.id for item in (await self.page(who)).items]


@requires_postgres
class NotificationStoreTest(NotificationsTestCase):
    async def test_each_user_sees_their_own_and_their_audiences(self):
        mine = await self.add(key="k:a", recipient_user_id=self.alice.user_id)
        theirs = await self.add(key="k:b", recipient_user_id=self.bob.user_id)
        admins = await self.add(key="k:h", audience_capability=ADMIN_AUDIENCE)
        self.assertEqual(await self.ids(self.alice), [mine])
        self.assertEqual(await self.ids(self.bob), [theirs])
        self.assertEqual(await self.ids(self.admin), [admins])
        page = await self.page(self.alice)
        self.assertEqual(page.unread, 1)
        item = page.items[0]
        self.assertEqual(
            (item.key, item.kind, item.read), ("k:a", "test.happened", False)
        )
        self.assertEqual(item.category, Category.SYSTEM)

    async def test_a_demoted_admin_no_longer_sees_the_audience(self):
        await self.add(key="k:h", audience_capability=ADMIN_AUDIENCE)
        demoted = Principal(self.admin.user_id, SystemRole.USER)
        self.assertEqual(await self.ids(demoted), [])

    async def test_params_round_trip_and_newest_first(self):
        first = await self.add(
            key="k:1",
            recipient_user_id=self.alice.user_id,
            params={"component": "database", "count": 3, "reasons": ["a", "b"]},
        )
        second = await self.add(key="k:2", recipient_user_id=self.alice.user_id)
        page = await self.page(self.alice)
        self.assertEqual([item.id for item in page.items], [second, first])
        self.assertEqual(
            dict(page.items[1].params),
            {"component": "database", "count": 3, "reasons": ["a", "b"]},
        )
        limited = await self.store.page(self.alice.user_id, (), limit=1)
        self.assertEqual([item.id for item in limited.items], [second])
        self.assertEqual(limited.unread, 2)

    async def test_mark_read_is_per_user_and_ignores_what_the_user_cannot_see(self):
        shared = await self.add(key="k:h", audience_capability=ADMIN_AUDIENCE)
        bobs = await self.add(key="k:b", recipient_user_id=self.bob.user_id)
        other_admin = self.user("admin")
        admin_audiences = audience_capabilities(self.admin)
        marked = await self.store.mark_read(
            self.admin.user_id, admin_audiences, [shared, bobs]
        )
        self.assertEqual(marked, 1)  # Bob's notification is not the admin's
        self.assertTrue((await self.page(self.admin)).items[0].read)
        self.assertFalse((await self.page(other_admin)).items[0].read)
        self.assertFalse((await self.page(self.bob)).items[0].read)
        # Marking again changes nothing.
        self.assertEqual(
            await self.store.mark_read(self.admin.user_id, admin_audiences, [shared]),
            0,
        )

    async def test_mark_all_read(self):
        await self.add(key="k:1", recipient_user_id=self.alice.user_id)
        await self.add(key="k:2", recipient_user_id=self.alice.user_id)
        await self.add(key="k:3", recipient_user_id=self.bob.user_id)
        self.assertEqual(await self.store.mark_read(self.alice.user_id, (), None), 2)
        self.assertEqual(await self.store.unread(self.alice.user_id, ()), 0)
        self.assertEqual(await self.store.unread(self.bob.user_id, ()), 1)

    async def test_dismiss_covers_the_entry_and_a_newer_one_comes_back(self):
        first = await self.add(key="k:a", recipient_user_id=self.alice.user_id)
        second = await self.add(key="k:a", recipient_user_id=self.alice.user_id)
        other = await self.add(key="k:z", recipient_user_id=self.alice.user_id)
        self.assertTrue(await self.store.dismiss(self.alice.user_id, (), second))
        self.assertEqual(await self.ids(self.alice), [other])
        self.assertEqual(await self.store.unread(self.alice.user_id, ()), 1)
        receipts = self.sql(
            "SELECT notification_id FROM notification_receipts"
            " WHERE dismissed_at IS NOT NULL AND read_at IS NOT NULL"
        )
        self.assertEqual({row[0] for row in receipts}, {first, second})
        newer = await self.add(key="k:a", recipient_user_id=self.alice.user_id)
        self.assertEqual(await self.ids(self.alice), [newer, other])

    async def test_another_users_notification_cannot_be_dismissed(self):
        bobs = await self.add(key="k:b", recipient_user_id=self.bob.user_id)
        admins = await self.add(key="k:h", audience_capability=ADMIN_AUDIENCE)
        self.assertFalse(await self.store.dismiss(self.alice.user_id, (), bobs))
        self.assertFalse(
            await self.store.dismiss(
                self.alice.user_id, audience_capabilities(self.alice), admins
            )
        )
        self.assertFalse(await self.store.dismiss(self.alice.user_id, (), uuid.uuid4()))
        self.assertEqual(
            self.sql("SELECT count(*) FROM notification_receipts")[0][0], 0
        )
        self.assertEqual(await self.ids(self.bob), [bobs])

    async def test_a_resolved_notification_is_not_listed(self):
        await self.add(key="pairing:1", recipient_user_id=self.alice.user_id)
        kept = await self.add(key="pairing:2", recipient_user_id=self.alice.user_id)

        async def work(session):
            return await resolve_in(session, "pairing:1")

        self.assertEqual(await self.database.run_abortable(work), 1)
        self.assertEqual(await self.ids(self.alice), [kept])

    async def test_the_purge_removes_old_notifications_and_their_receipts(self):
        old = await self.add(key="k:old", recipient_user_id=self.alice.user_id)
        new = await self.add(key="k:new", recipient_user_id=self.alice.user_id)
        await self.store.mark_read(self.alice.user_id, (), None)
        self.sql(
            "UPDATE notifications SET created_at = now() - interval '91 days'"
            " WHERE id = :id",
            id=old,
        )
        self.assertEqual(await self.store.purge(), 1)
        self.assertEqual(await self.ids(self.alice), [new])
        self.assertEqual(
            self.sql("SELECT count(*) FROM notification_receipts")[0][0], 1
        )

    async def test_the_schema_refuses_two_audiences_or_none(self):
        for recipient, audience in ((self.alice.user_id, "a.b"), (None, None)):
            with self.subTest(recipient=recipient), self.assertRaises(IntegrityError):
                self.sql(
                    "INSERT INTO notifications (key, kind, severity, category,"
                    " recipient_user_id, audience_capability)"
                    " VALUES ('k', 'a.b', 'info', 'system', :r, :a)",
                    r=recipient,
                    a=audience,
                )


def health(component: Component, severity: Severity, *reasons: str):
    status = Status.OK if severity is Severity.INFO else Status.FAILING
    return ComponentHealth(component, severity, status, reasons)


@requires_postgres
class HealthNotificationTest(NotificationsTestCase):
    async def test_severity_changes_notify_the_admin_audience(self):
        announced = []
        store = HealthStore(
            self.database, notify=True, on_notified=lambda: announced.append(1)
        )
        # A fresh start at info is not a notification.
        await store.record_changes([health(Component.DATABASE, Severity.INFO)])
        self.assertEqual(announced, [])
        self.assertEqual(await self.ids(self.admin), [])
        await store.record_changes(
            [health(Component.DATABASE, Severity.CRITICAL, "database_unavailable")]
        )
        self.assertEqual(announced, [1])
        await store.record_changes([health(Component.DATABASE, Severity.INFO)])
        self.assertEqual(announced, [1, 1])
        page = await self.page(self.admin)
        self.assertEqual(
            [(item.key, item.severity.value) for item in page.items],
            [
                ("system_health:database", "info"),
                ("system_health:database", "critical"),
            ],
        )
        self.assertEqual(
            dict(page.items[1].params),
            {
                "component": "database",
                "status": "failing",
                "previous_severity": "info",
                "reasons": ["database_unavailable"],
            },
        )
        self.assertEqual(page.items[1].kind, "system_health.component_changed")
        # Nobody else receives them.
        self.assertEqual(await self.ids(self.alice), [])

    async def test_a_first_event_above_info_is_a_notification(self):
        store = HealthStore(self.database, notify=True)
        await store.record_changes([health(Component.RECOVERY_BACKUP, Severity.ERROR)])
        page = await self.page(self.admin)
        self.assertEqual([item.severity.value for item in page.items], ["error"])
        self.assertIsNone(page.items[0].params["previous_severity"])

    async def test_without_notify_nothing_is_added(self):
        await HealthStore(self.database).record_changes(
            [health(Component.DATABASE, Severity.CRITICAL)]
        )
        self.assertEqual(self.sql("SELECT count(*) FROM notifications")[0][0], 0)

    async def test_the_roll_up_purges_old_notifications(self):
        store = HealthStore(self.database, notify=True)
        old = await self.add(key="k:old", recipient_user_id=self.alice.user_id)
        self.sql(
            "UPDATE notifications SET created_at = now() - interval '91 days'"
            " WHERE id = :id",
            id=old,
        )
        result = await store.roll_up(retention_days=400)
        self.assertEqual(result.purged_notifications, 1)


@requires_postgres
class NotificationHttpTest(NotificationsTestCase):
    def client(self, who: Principal):
        settings = make_settings(database_url=self.database_url())
        app = create_app(settings, database=self.database)
        install_authz(
            app,
            settings=settings,
            database=self.database,
            principal_provider=StaticProvider(who),
            audit_sink=InMemoryAuditSink(),
        )
        return make_client(app)

    async def test_a_user_reads_marks_and_dismisses_their_own_only(self):
        mine = await self.add(key="k:a", recipient_user_id=self.alice.user_id)
        bobs = await self.add(key="k:b", recipient_user_id=self.bob.user_id)
        alice = self.client(self.alice)
        listed = alice.get("/api/v1/notifications").json()
        self.assertEqual([n["id"] for n in listed["notifications"]], [str(mine)])
        self.assertEqual(listed["unread"], 1)
        response = alice.post(
            "/api/v1/notifications/read", json={"ids": [str(mine), str(bobs)]}
        )
        self.assertEqual(response.json(), {"updated": 1, "unread": 0})
        self.assertEqual(
            alice.post(f"/api/v1/notifications/{bobs}/dismiss").status_code, 404
        )
        self.assertEqual(
            alice.post(f"/api/v1/notifications/{mine}/dismiss").status_code, 204
        )
        self.assertEqual(alice.get("/api/v1/notifications").json()["notifications"], [])
        bob = self.client(self.bob).get("/api/v1/notifications").json()
        self.assertEqual(
            [(n["id"], n["read"]) for n in bob["notifications"]], [(str(bobs), False)]
        )
