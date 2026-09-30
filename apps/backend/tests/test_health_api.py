"""``/api/v1/system/health*`` (PAW-066, Decision 0059).

Who may read what: the compact summary is every human role's
(``system_health.summary.read``), the detail, the series and the events the
Owner's and Admins' (``admin.system_health.view``); nobody's without a session.
The detail holds the components' codes and numbers, the summary only the
overall severity and the availability of Codex / Claude. The history answers
503 without a database, and validates the metric name and the time range.
"""

import unittest
from datetime import UTC, datetime, timedelta

from paw_backend.app import create_app
from paw_backend.authz import SystemRole
from paw_backend.authz.deps import install_authz
from paw_backend.health.domain import Component, ComponentHealth, Severity, Status
from paw_backend.health.monitor import HealthMonitor
from paw_backend.health.sources import ComputeSource, ReaperSource
from paw_backend.health.store import HealthEvent, SeriesPoint
from paw_backend.health.wiring import SystemHealth

from .authz_support import InMemoryAuditSink, StaticProvider, principal
from .support import FakeDatabase, make_client, make_settings

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


class FixedSource:
    max_age_seconds = 0.0

    def __init__(self, health: ComponentHealth) -> None:
        self.component = health.component
        self.health = health

    async def check(self) -> ComponentHealth:
        return self.health


class FakeStore:
    def __init__(self) -> None:
        self.series_calls: list[dict] = []
        self.fail = False

    async def series(self, metric, *, since, until, step_seconds, limit):
        if self.fail:
            raise TimeoutError()
        self.series_calls.append(
            {"metric": metric, "since": since, "until": until, "step": step_seconds}
        )
        return (SeriesPoint(T0, 2, 1.5, 1.0, 2.0),)

    async def events(self, *, since, limit):
        return (
            HealthEvent(
                7,
                T0,
                "database",
                "critical",
                "info",
                "unavailable",
                ("database_unavailable",),
            ),
        )


COMPONENTS = (
    ComponentHealth(Component.DATABASE, Severity.INFO, Status.OK, metrics={"up": 1}),
    ComponentHealth(
        Component.CONNECTIONS,
        Severity.WARNING,
        Status.DEGRADED,
        ("unavailable:codex",),
        {"codex_available": 0},
        (
            {"kind": "codex", "available": False, "status": "unavailable"},
            {"kind": "claude", "available": True, "status": "connected"},
        ),
    ),
)


def make_app(role: SystemRole | None, *, store=None):
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
    app.state.system_health = SystemHealth(
        HealthMonitor([FixedSource(h) for h in COMPONENTS]),
        ComputeSource(),
        ReaperSource(),
        store,
        False,
    )
    return app


class AccessTest(unittest.TestCase):
    PATHS = (
        "/api/v1/system/health",
        "/api/v1/system/health/metrics/database.up",
        "/api/v1/system/health/events",
    )

    def test_nobody_gets_nothing(self):
        client = make_client(make_app(None, store=FakeStore()))
        for path in (*self.PATHS, "/api/v1/system/health/summary"):
            with self.subTest(path):
                self.assertEqual(client.get(path).status_code, 401)

    def test_a_user_gets_the_summary_only(self):
        client = make_client(make_app(SystemRole.USER, store=FakeStore()))
        for path in self.PATHS:
            with self.subTest(path):
                self.assertEqual(client.get(path).status_code, 403)
        response = client.get("/api/v1/system/health/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {k: v for k, v in response.json().items() if k != "checked_at"},
            {
                "severity": "warning",
                "connections": {"codex": "unavailable", "claude": "available"},
            },
        )

    def test_owner_and_admin_get_everything(self):
        for role in (SystemRole.ADMIN, SystemRole.OWNER):
            with self.subTest(role):
                client = make_client(make_app(role, store=FakeStore()))
                for path in (*self.PATHS, "/api/v1/system/health/summary"):
                    self.assertEqual(client.get(path).status_code, 200, path)

    def test_the_system_role_gets_nothing(self):
        client = make_client(make_app(SystemRole.SYSTEM, store=FakeStore()))
        self.assertEqual(client.get("/api/v1/system/health/summary").status_code, 403)


class DetailTest(unittest.TestCase):
    def test_the_report(self):
        client = make_client(make_app(SystemRole.ADMIN))
        body = client.get("/api/v1/system/health").json()
        self.assertEqual(body["severity"], "warning")
        database, connections = body["components"]
        self.assertEqual(
            database,
            {
                "component": "database",
                "severity": "info",
                "status": "ok",
                "reasons": [],
                "metrics": {"up": 1},
                "parts": [],
            },
        )
        self.assertEqual(connections["reasons"], ["unavailable:codex"])
        self.assertEqual(connections["parts"][1]["kind"], "claude")

    def test_the_history_needs_a_database(self):
        client = make_client(make_app(SystemRole.ADMIN, store=None))
        for path in (
            "/api/v1/system/health/metrics/database.up",
            "/api/v1/system/health/events",
        ):
            with self.subTest(path):
                self.assertEqual(client.get(path).status_code, 503)


class SeriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeStore()
        self.client = make_client(make_app(SystemRole.ADMIN, store=self.store))

    def test_the_default_is_the_last_day(self):
        body = self.client.get(
            "/api/v1/system/health/metrics/compute.vram_used_bytes"
        ).json()
        (call,) = self.store.series_calls
        self.assertEqual(call["until"] - call["since"], timedelta(days=1))
        # 1,000 points at most: 86,400 / 1,000 rounded up.
        self.assertEqual(body["step_seconds"], 87)
        self.assertEqual(
            body["points"],
            [
                {
                    "bucket_start": "2026-09-30T12:00:00Z",
                    "count": 2,
                    "mean": 1.5,
                    "min": 1.0,
                    "max": 2.0,
                }
            ],
        )

    def test_a_short_range_keeps_the_requested_step(self):
        since = "2026-09-30T11:00:00Z"
        until = "2026-09-30T12:00:00Z"
        body = self.client.get(
            "/api/v1/system/health/metrics/database.up",
            params={"since": since, "until": until, "step_seconds": 60},
        ).json()
        self.assertEqual(body["step_seconds"], 60)

    def test_refusals(self):
        for path, params in (
            ("/api/v1/system/health/metrics/NOT_A_METRIC", {}),
            ("/api/v1/system/health/metrics/nodot", {}),
            (
                "/api/v1/system/health/metrics/database.up",
                {"since": "2026-09-30T12:00:00", "until": "2026-09-30T13:00:00Z"},
            ),
            (
                "/api/v1/system/health/metrics/database.up",
                {"since": "2026-09-30T13:00:00Z", "until": "2026-09-30T12:00:00Z"},
            ),
            (
                "/api/v1/system/health/metrics/database.up",
                {"since": "2020-01-01T00:00:00Z", "until": "2026-09-30T12:00:00Z"},
            ),
            ("/api/v1/system/health/metrics/database.up", {"step_seconds": 1}),
        ):
            with self.subTest(path=path, params=params):
                self.assertEqual(self.client.get(path, params=params).status_code, 422)
        self.assertEqual(self.store.series_calls, [])

    def test_a_failing_read_is_503_without_details(self):
        self.store.fail = True
        response = self.client.get("/api/v1/system/health/metrics/database.up")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "service_unavailable")


class EventsTest(unittest.TestCase):
    def test_the_events(self):
        client = make_client(make_app(SystemRole.OWNER, store=FakeStore()))
        body = client.get("/api/v1/system/health/events", params={"limit": 5}).json()
        self.assertEqual(
            body["events"],
            [
                {
                    "id": 7,
                    "occurred_at": "2026-09-30T12:00:00Z",
                    "component": "database",
                    "severity": "critical",
                    "previous_severity": "info",
                    "status": "unavailable",
                    "reasons": ["database_unavailable"],
                }
            ],
        )
        self.assertEqual(
            client.get("/api/v1/system/health/events", params={"limit": 0}).status_code,
            422,
        )


class WiringTest(unittest.TestCase):
    def test_create_app_builds_system_health(self):
        app = create_app(make_settings(), database=FakeDatabase())
        health = app.state.system_health
        # No database: no store, no sampling, and only the sources that need none.
        self.assertIsNone(health.store)
        self.assertFalse(health.sampling)
        self.assertEqual(
            [s.component for s in health.monitor.sources],
            [Component.DATABASE, Component.COMPUTE, Component.CONNECTION_REAPER],
        )
