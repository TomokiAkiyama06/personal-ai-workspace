"""Shared fixtures for the login / session / password tests that need PostgreSQL."""

import asyncio
import contextlib
import unittest
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from paw_backend.auth.wiring import AuthServices, build_auth
from paw_backend.authz.audit import AuditEvent, PostgresAuditSink
from paw_backend.db import Database

from .identity_support import (
    ROLE_PASSWORD,
    TEST_DATABASE_URL,
    FakeClock,
    LogCapture,
    migrate,
    requires_postgres,
    role_name,
    sync_database_url,
    url_for_role,
)
from .support import make_settings

__all__ = [
    "PASSWORD",
    "ROLE_PASSWORD",
    "T0",
    "TEST_DATABASE_URL",
    "FakeClock",
    "LogCapture",
    "PostgresAuthTestCase",
    "TestUser",
    "fast_settings",
    "migrate",
    "requires_postgres",
    "role_name",
    "sync_database_url",
    "url_for_role",
]

T0 = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
# A password that meets the policy and is not on the block list.
PASSWORD = "correct horse battery staple"
DEFAULT_POLICY_ROW = (
    "INSERT INTO auth_policy (id, version, passkey_owner, passkey_admin, "
    "passkey_user, recommend_passkey_to_users, stepup_window_minutes, updated_at, "
    "updated_by) VALUES (1, 1, 'required', 'required', 'optional', true, 30, "
    "now(), NULL)"
)


def fast_settings(**overrides):
    """Settings with the cheapest Argon2id the settings allow (fast tests)."""
    values = {
        "database_url": TEST_DATABASE_URL,
        "password_hash_time_cost": 1,
        "password_hash_memory_kib": 19_456,
        "password_hash_parallelism": 1,
        "password_hash_concurrency": 2,
        # A shared, busy test server must not turn a slow connection into a
        # failure of a test that is about something else.
        "database_timeout_seconds": 30,
    }
    values.update(overrides)
    return make_settings(**values)


@dataclass(frozen=True)
class TestUser:
    __test__ = False  # not a test case

    id: uuid.UUID
    login_name: str
    role: str
    password: str


class PostgresAuthTestCase(unittest.IsolatedAsyncioTestCase):
    """A migrated database per class; every test starts without users.

    ``audit_events`` cannot be emptied (that is its point), so a test reads the
    rows stored after it began (``audit_rows``).
    """

    # Environment of the migration (``grants`` tests set ``PAW_APP_DATABASE_ROLE``).
    migration_environment: dict = {}
    # The role the SERVICES connect as; ``None``: the test database's own user.
    # The test itself (seeding, reading rows) always uses the owner (``database``).
    service_role: str | None = None
    settings_overrides: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        migrate("downgrade", "base")  # a clean start, even after a crashed run
        migrate("upgrade", "head", **cls.migration_environment)

    @classmethod
    def tearDownClass(cls) -> None:
        migrate("downgrade", "base")

    async def asyncSetUp(self) -> None:
        self.settings = fast_settings(**self.settings_overrides)
        # The owner's engine: seeding and reading rows.
        self.database = Database(fast_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(self.database.dispose)
        await self.reset()
        self.started_at = await self.scalar("SELECT clock_timestamp()")
        self.clock = FakeClock(T0)
        # The engine the services use (another role in the grants tests).
        self.service_database = self.new_database()
        self.services = self.build(self.service_database)
        self.auth = self.services.service

    def service_url(self) -> str:
        if self.service_role is None:
            return TEST_DATABASE_URL
        return url_for_role(self.service_role)

    def new_database(self, url: str | None = None) -> Database:
        """Another independent engine for a service, as a second process would have."""
        database = Database(fast_settings(database_url=url or self.service_url()))
        self.addAsyncCleanup(database.dispose)
        return database

    def build(
        self, database: Database, *, sink=None, settings=None, clock=None
    ) -> AuthServices:
        """The authentication services on ``database`` (their own hasher and clock)."""
        services = build_auth(
            settings or self.settings,
            database,
            audit_sink=sink or PostgresAuditSink(database),
            clock=clock or self.clock,
        )
        self.addCleanup(services.close)
        return services

    # -- database access for the test itself ------------------------------------

    async def query(self, sql: str, **params) -> list:
        async with self.database.session() as session:
            return list((await session.execute(text(sql), params)).all())

    async def execute(self, sql: str, **params) -> None:
        async with self.database.session() as session:
            await session.execute(text(sql), params)
            await session.commit()

    async def scalar(self, sql: str, **params):
        rows = await self.query(sql, **params)
        return rows[0][0]

    async def reset(self) -> None:
        async with self.database.session() as session:
            await session.execute(text("TRUNCATE users CASCADE"))
            await session.execute(text("TRUNCATE auth_throttles"))
            await session.execute(text("TRUNCATE auth_policy_changes"))
            await session.execute(text("TRUNCATE auth_policy"))
            await session.execute(text(DEFAULT_POLICY_ROW))
            await session.commit()

    async def make_user(
        self,
        login_name: str = "alice",
        *,
        role: str = "user",
        status: str = "active",
        password: str | None = PASSWORD,
    ) -> TestUser:
        """A user row (and its password, unless ``password=None``), written directly."""
        user_id = uuid.uuid4()
        await self.execute(
            "INSERT INTO users (id, login_name, system_role, status, "
            "passkey_required, created_at, updated_at) VALUES (:id, :name, :role, "
            ":status, :required, :now, :now)",
            id=user_id,
            name=login_name,
            role=role,
            status=status,
            required=role in ("owner", "admin"),
            now=T0,
        )
        if password is not None:
            encoded = await self.services.hasher.hash(password)
            await self.execute(
                "INSERT INTO password_credentials (user_id, hash, created_at, "
                "changed_at) VALUES (:id, :hash, :now, :now)",
                id=user_id,
                hash=encoded,
                now=T0,
            )
        return TestUser(user_id, login_name, role, password or "")

    # -- audit ----------------------------------------------------------------------

    async def audit_rows(self) -> list:
        return await self.query(
            "SELECT * FROM audit_events WHERE recorded_at >= :since "
            "ORDER BY recorded_at, occurred_at",
            since=self.started_at,
        )

    async def audit_summary(self) -> Counter:
        """Counts of (action, decision, reason) stored since the test began."""
        return Counter(
            (row.action, row.decision, row.reason) for row in await self.audit_rows()
        )

    async def everything_stored(self) -> str:
        """Every row of every table this feature writes, as text (a secret scan)."""
        parts = []
        for table in (
            "users",
            "password_credentials",
            "auth_sessions",
            "auth_throttles",
            "auth_policy",
            "auth_policy_changes",
        ):
            parts += [
                str(row[0])
                for row in await self.query(f"SELECT t::text FROM {table} t")
            ]
        parts += [
            str(row[0])
            for row in await self.query(
                "SELECT t::text FROM audit_events t WHERE recorded_at >= :since",
                since=self.started_at,
            )
        ]
        return "\n".join(parts)

    async def gather_on_own_engines(self, count: int, make_call):
        """Run ``make_call(services, index)`` ``count`` times at once.

        Each call has its own engine and its own services (its own connections
        and hasher), as separate processes would. Exceptions are returned.
        """
        services = [self.build(self.new_database()) for _ in range(count)]
        return await asyncio.gather(
            *(make_call(service, index) for index, service in enumerate(services)),
            return_exceptions=True,
        )

    async def advance(self, **delta: float) -> None:
        self.clock.advance(**delta)

    @contextlib.contextmanager
    def no_secret_in_logs(self, *secrets: str, application_only: tuple = ()):
        """Fail if a secret appears in anything logged in the block.

        ``secrets`` (a password, a session id) must not appear anywhere, the SQL
        echo of SQLAlchemy at INFO included. ``application_only`` (a password
        hash, which the SQL echo shows as a bound parameter when an operator
        turns that echo on) must not appear in what the application itself logs.
        """
        with LogCapture() as logs:
            yield logs
        for secret in secrets:
            self.assertNotIn(secret, logs.text)
        for secret in application_only:
            self.assertNotIn(secret, logs.application_text)


def now_plus(**delta: float) -> datetime:
    return T0 + timedelta(**delta)


class RecordingSink:
    """An audit sink that keeps events and can be told to fail."""

    def __init__(self, inner=None) -> None:
        self.events: list[AuditEvent] = []
        self.fail = False
        self._inner = inner

    async def record(self, event: AuditEvent) -> None:
        if self.fail:
            raise RuntimeError("audit-secret-detail-022")
        self.events.append(event)
        if self._inner is not None:
            await self._inner.record(event)
