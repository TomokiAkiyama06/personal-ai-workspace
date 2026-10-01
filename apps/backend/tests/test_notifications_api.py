"""``/api/v1/notifications`` (issue #188, Decision 0070) without a database.

Who may do what: every human role reads (``notification.read``) and marks /
dismisses (``notification.manage``) their own notifications; nobody without a
session; never the system role. The routes hand the store the user's id and the
audiences their role holds now (an Admin's include the System Health audience,
a User's do not), validate the body, answer 404 for a notification the user does
not receive and 503 without a store or when it fails, and announce a change to
the user's own streams only.
"""

import unittest
import uuid
from datetime import UTC, datetime

from paw_backend.app import create_app
from paw_backend.authz import SystemRole
from paw_backend.authz.deps import install_authz
from paw_backend.events import EventType, Viewer
from paw_backend.notifications import (
    Category,
    NotificationPage,
    NotificationsUnavailableError,
    ReadResult,
    Severity,
    StoredNotification,
)

from .authz_support import U1, InMemoryAuditSink, StaticProvider, principal
from .support import FakeDatabase, make_client, make_settings

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
N1 = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
ADMIN_AUDIENCE = "admin.system_health.view"


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail = False
        self.updated = 1
        self.found = True
        self.unread_fails = False

    def _call(self, *args):
        if self.fail:
            raise NotificationsUnavailableError
        self.calls.append(args)

    async def page(self, user_id, audiences, *, limit):
        self._call("page", user_id, tuple(audiences), limit)
        return NotificationPage(
            items=(
                StoredNotification(
                    id=N1,
                    key="system_health:database",
                    kind="system_health.component_changed",
                    severity=Severity.CRITICAL,
                    category=Category.SYSTEM,
                    project_id=None,
                    params={"component": "database", "reasons": ["a"]},
                    created_at=T0,
                    read=False,
                ),
            ),
            unread=3,
        )

    async def unread(self, user_id, audiences):
        self._call("unread", user_id)
        if self.unread_fails:
            raise NotificationsUnavailableError
        return 2

    async def mark_read(self, user_id, audiences, ids):
        self._call("mark_read", user_id, None if ids is None else tuple(ids))
        return ReadResult(updated=self.updated, unread=2)

    async def dismiss(self, user_id, audiences, notification_id):
        self._call("dismiss", user_id, notification_id)
        return self.found


def make_app(role: SystemRole | None, store=None):
    settings = make_settings()
    database = FakeDatabase()
    app = create_app(settings, database=database)
    install_authz(
        app,
        settings=settings,
        database=database,
        principal_provider=StaticProvider(None if role is None else principal(role)),
        audit_sink=InMemoryAuditSink(),
    )
    app.state.notifications = store
    return app


class AccessTest(unittest.TestCase):
    CALLS = (
        ("get", "/api/v1/notifications", None),
        ("post", "/api/v1/notifications/read", {"all": True}),
        ("post", f"/api/v1/notifications/{N1}/dismiss", None),
    )

    def answers(self, role):
        client = make_client(make_app(role, FakeStore()))
        return [
            getattr(client, method)(path, json=body).status_code
            if body is not None
            else getattr(client, method)(path).status_code
            for method, path, body in self.CALLS
        ]

    def test_nobody_gets_nothing(self):
        self.assertEqual(self.answers(None), [401, 401, 401])

    def test_the_system_role_gets_nothing(self):
        self.assertEqual(self.answers(SystemRole.SYSTEM), [403, 403, 403])

    def test_every_human_role_uses_their_own(self):
        for role in (SystemRole.USER, SystemRole.ADMIN, SystemRole.OWNER):
            with self.subTest(role):
                self.assertEqual(self.answers(role), [200, 200, 204])

    def test_no_store_is_503_after_the_session_check(self):
        client = make_client(make_app(SystemRole.USER, None))
        self.assertEqual(client.get("/api/v1/notifications").status_code, 503)
        anonymous = make_client(make_app(None, None))
        self.assertEqual(anonymous.get("/api/v1/notifications").status_code, 401)


class ListTest(unittest.TestCase):
    def test_the_list_and_what_the_store_is_asked(self):
        store = FakeStore()
        client = make_client(make_app(SystemRole.USER, store))
        response = client.get("/api/v1/notifications", params={"limit": 50})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "notifications": [
                    {
                        "id": str(N1),
                        "key": "system_health:database",
                        "kind": "system_health.component_changed",
                        "severity": "critical",
                        "category": "system",
                        "project_id": None,
                        "params": {"component": "database", "reasons": ["a"]},
                        "created_at": "2026-10-01T09:00:00Z",
                        "read": False,
                    }
                ],
                "unread": 3,
            },
        )
        (_, user_id, audiences, limit) = store.calls[0]
        self.assertEqual((user_id, limit), (U1, 50))
        self.assertNotIn(ADMIN_AUDIENCE, audiences)

    def test_an_admins_audiences_include_system_health(self):
        store = FakeStore()
        make_client(make_app(SystemRole.ADMIN, store)).get("/api/v1/notifications")
        self.assertIn(ADMIN_AUDIENCE, store.calls[0][2])

    def test_the_limit_is_bounded(self):
        client = make_client(make_app(SystemRole.USER, FakeStore()))
        for limit in (0, 201, "x"):
            with self.subTest(limit):
                response = client.get("/api/v1/notifications", params={"limit": limit})
                self.assertEqual(response.status_code, 422)

    def test_a_store_failure_is_503(self):
        store = FakeStore()
        store.fail = True
        client = make_client(make_app(SystemRole.USER, store))
        self.assertEqual(client.get("/api/v1/notifications").status_code, 503)
        self.assertEqual(
            client.post("/api/v1/notifications/read", json={"all": True}).status_code,
            503,
        )
        self.assertEqual(
            client.post(f"/api/v1/notifications/{N1}/dismiss").status_code, 503
        )


class WriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeStore()
        self.app = make_app(SystemRole.USER, self.store)
        self.client = make_client(self.app)
        self.bus = self.app.state.event_bus

    def test_mark_read_by_ids_or_all(self):
        response = self.client.post(
            "/api/v1/notifications/read", json={"ids": [str(N1)]}
        )
        self.assertEqual(response.json(), {"updated": 1, "unread": 2})
        self.assertEqual(self.store.calls[0], ("mark_read", U1, (N1,)))
        self.client.post("/api/v1/notifications/read", json={"all": True})
        self.assertEqual(self.store.calls[1], ("mark_read", U1, None))

    def test_a_read_that_was_saved_is_answered_and_announced(self):
        # The read and the unread count come from one transaction: a saved read
        # is never answered 503 (the Web App would show it unread again) nor
        # left unannounced to the user's other devices.
        self.store.unread_fails = True
        with self.bus.subscribe(Viewer(U1, frozenset())) as mine:
            response = self.client.post(
                "/api/v1/notifications/read", json={"ids": [str(N1)]}
            )
            self.assertFalse(mine._queue.empty())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"updated": 1, "unread": 2})

    def test_the_body_is_validated(self):
        for body in (
            {},
            {"all": False},
            {"ids": [str(N1)], "all": True},
            {"ids": ["not-a-uuid"]},
            {"all": "yes"},
            {"all": True, "extra": 1},
            {"ids": [str(uuid.uuid4()) for _ in range(201)]},
        ):
            with self.subTest(body=body):
                response = self.client.post("/api/v1/notifications/read", json=body)
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.calls, [])

    def test_an_unknown_notification_is_404(self):
        self.store.found = False
        response = self.client.post(f"/api/v1/notifications/{N1}/dismiss")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")
        bad = self.client.post("/api/v1/notifications/not-a-uuid/dismiss")
        self.assertEqual(bad.status_code, 422)

    def test_a_change_is_announced_to_the_users_streams_only(self):
        with self.bus.subscribe() as anonymous:
            with self.bus.subscribe(Viewer(U1, frozenset())) as mine:
                self.client.post(f"/api/v1/notifications/{N1}/dismiss")
                self.client.post("/api/v1/notifications/read", json={"all": True})
                self.store.updated = 0
                self.client.post("/api/v1/notifications/read", json={"all": True})
                received = []
                while not mine._queue.empty():
                    received.append(mine._queue.get_nowait())
            self.assertTrue(anonymous._queue.empty())
        self.assertEqual(
            [event.type for event in received], [EventType.NOTIFICATION_CHANGED] * 2
        )


if __name__ == "__main__":
    unittest.main()
