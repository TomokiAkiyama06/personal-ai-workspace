"""Shared fixtures for the Owner setup tests that need a real PostgreSQL."""

import asyncio
import contextlib
import io
import logging
import os
import unittest
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from alembic import command as alembic_command
from sqlalchemy import text
from sqlalchemy.engine import make_url

from paw_backend.authz import PostgresAuditSink
from paw_backend.authz.audit import AuditEvent
from paw_backend.db import Database
from paw_backend.identity import TokenRedeemer
from paw_backend.identity.operator import ROOT_UID, OwnerOperator

from .support import make_settings, paw_environment
from .test_migrations import offline_config


@contextlib.contextmanager
def running_as(uid: int, sudo_uid: int | None = None):
    """This process as the service sees it: effective uid ``uid`` and ``SUDO_UID``.

    The service reads who runs it from the process itself (``os.geteuid()`` and
    the environment) and takes it from no caller, so this is the only way for a
    test to be root, or somebody else: it replaces exactly what the service reads.
    """
    with patch("os.geteuid", return_value=uid), patch.dict(os.environ):
        os.environ.pop("SUDO_UID", None)
        if sudo_uid is not None:
            os.environ["SUDO_UID"] = str(sudo_uid)
        yield


TEST_DATABASE_URL = os.environ.get("PAW_TEST_DATABASE_URL")

requires_postgres = unittest.skipUnless(
    TEST_DATABASE_URL, "PAW_TEST_DATABASE_URL is not set"
)

# Database roles are cluster-wide, so a fixed name collides when two runs share
# a server. The names of the throw-away roles carry a random per-run suffix.
RUN_ID = uuid.uuid4().hex[:10]


def role_name(kind: str) -> str:
    """A role name for ``kind`` that is unique to this run (a valid role name)."""
    return f"paw_owner_{kind}_{RUN_ID}"


# Dummy credentials of the throw-away, non-superuser database roles.
ROLE_PASSWORD = "dummy-test-password-021"
SECRET_DETAIL = "hunter2-connection-detail-021"
T0 = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


def migrate(action: str, revision: str, **environment: str) -> None:
    """Run Alembic against the test database (synchronously; no event loop)."""
    variables = {"PAW_DATABASE_URL": TEST_DATABASE_URL, **environment}
    with paw_environment(**variables):
        config = offline_config(io.StringIO())
        getattr(alembic_command, action)(config, revision)


def sync_database_url() -> str:
    """The test database URL with the psycopg driver, for a synchronous engine."""
    url = make_settings(database_url=TEST_DATABASE_URL).database_url
    assert url is not None
    return url.get_secret_value()


def url_for_role(role: str) -> str:
    url = make_url(TEST_DATABASE_URL).set(username=role, password=ROLE_PASSWORD)
    return url.render_as_string(hide_password=False)


def secret_of(token: str) -> str:
    """The secret part of ``pawst1.<id>.<secret>``."""
    return token.split(".")[2]


def wrong_secret_for(token: str) -> str:
    """A well-formed token for the same lookup id with another secret."""
    prefix, token_id, _ = token.split(".")
    return f"{prefix}.{token_id}.{'A' * 43}"


def lookup_id_of(token: str) -> str:
    """The lookup id (32 hex digits) of ``pawst1.<id>.<secret>``."""
    return token.split(".")[1]


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class FailingSink:
    """An audit sink whose write fails with a message that must never leak."""

    def __init__(self) -> None:
        self.attempts = 0

    async def record(self, event: AuditEvent) -> None:
        self.attempts += 1
        raise RuntimeError(SECRET_DETAIL)


class FlakySink:
    """Stores the first ``allow`` events, then fails."""

    def __init__(self, database: Database, allow: int) -> None:
        self._inner = PostgresAuditSink(database)
        self._allow = allow

    async def record(self, event: AuditEvent) -> None:
        if self._allow <= 0:
            raise RuntimeError(SECRET_DETAIL)
        self._allow -= 1
        await self._inner.record(event)


class LogCapture:
    """Collects everything logged, SQL statements and their parameters included."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        capture = self

        class Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                capture.messages.append(self.format(record))

        self._handler = Handler(logging.DEBUG)
        self._handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
        self._saved: list[tuple[logging.Logger, int]] = []

    def __enter__(self) -> "LogCapture":
        for name in ("", "sqlalchemy.engine"):
            logger = logging.getLogger(name)
            self._saved.append((logger, logger.level))
            logger.setLevel(logging.DEBUG if name == "" else logging.INFO)
        logging.getLogger().addHandler(self._handler)
        return self

    def __exit__(self, *exc_info) -> None:
        logging.getLogger().removeHandler(self._handler)
        for logger, level in self._saved:
            logger.setLevel(level)

    @property
    def text(self) -> str:
        return "\n".join(self.messages)

    @property
    def application_text(self) -> str:
        """Only the lines the application itself logged (not the SQL echo)."""
        return "\n".join(m for m in self.messages if m.startswith("paw_backend"))


class PostgresIdentityTestCase(unittest.IsolatedAsyncioTestCase):
    """Migrated database per class; every test starts without users.

    ``audit_events`` cannot be emptied (that is the point of it), so a test looks
    at the audit rows stored after it began (``audit_rows``).
    """

    @classmethod
    def setUpClass(cls) -> None:
        migrate("downgrade", "base")  # a clean start, even after a crashed run
        migrate("upgrade", "head")

    @classmethod
    def tearDownClass(cls) -> None:
        migrate("downgrade", "base")

    async def asyncSetUp(self) -> None:
        # Recovery needs root, as under sudo: a test about who may recover, or
        # about what is recorded, says who it runs as with ``running_as``.
        self.enterContext(running_as(ROOT_UID))
        self.database = self.new_database()
        await self.reset_users()
        self.started_at = await self.scalar("SELECT clock_timestamp()")
        self.clock = FakeClock()
        self.operator = self.make_operator()
        self.redeemer = self.make_redeemer()

    def new_database(self, url: str | None = None) -> Database:
        """Another independent engine, as a second process would have."""
        database = Database(make_settings(database_url=url or TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        return database

    def make_operator(
        self, database: Database | None = None, sink=None, **options
    ) -> OwnerOperator:
        """The operator side (creates the Owner, issues tokens)."""
        database = database or self.database
        options.setdefault("clock", self.clock)
        return OwnerOperator(database, sink or PostgresAuditSink(database), **options)

    def make_redeemer(
        self, database: Database | None = None, sink=None, **options
    ) -> TokenRedeemer:
        """The web-facing side (spends tokens)."""
        database = database or self.database
        options.setdefault("clock", self.clock)
        return TokenRedeemer(database, sink or PostgresAuditSink(database), **options)

    @contextlib.contextmanager
    def audit_failure_is_logged_by_type_only(self):
        """The block must log an audit write failure (its type, never its text)."""
        with self.assertLogs("paw_backend.identity.audit", "ERROR") as logs:
            yield
        text = "\n".join(logs.output)
        self.assertIn("Audit write failed (RuntimeError)", text)
        self.assertNotIn(SECRET_DETAIL, text)

    async def query(self, sql: str, **params) -> list:
        async with self.database.session() as session:
            return list((await session.execute(text(sql), params)).all())

    async def execute(self, sql: str, **params) -> None:
        """Run one statement and commit it."""
        async with self.database.session() as session:
            await session.execute(text(sql), params)
            await session.commit()

    async def scalar(self, sql: str, **params):
        rows = await self.query(sql, **params)
        return rows[0][0]

    async def wait_until_a_statement_waits_for_a_lock(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30
        while loop.time() < deadline:
            waiting = await self.scalar(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
            if waiting:
                return
            await asyncio.sleep(0.02)
        self.fail("no statement is waiting for a lock")

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
        """Every row of every table this feature writes, as text."""
        parts = []
        for table in ("users", "setup_tokens"):
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

    async def owner_count(self) -> int:
        return await self.scalar(
            "SELECT count(*) FROM users WHERE system_role = 'owner'"
        )

    async def gather_on_own_engines(
        self, count: int, make_call, *, redeemer: bool = False, **options
    ):
        """Run ``make_call(service, index)`` ``count`` times at once.

        Each call has its own engine (its own connections), as separate
        processes would. ``service`` is an operator, or a redeemer with
        ``redeemer=True``. Exceptions are returned, not raised.
        """
        make = self.make_redeemer if redeemer else self.make_operator
        services = [make(self.new_database(), **options) for _ in range(count)]
        return await asyncio.gather(
            *(make_call(service, index) for index, service in enumerate(services)),
            return_exceptions=True,
        )

    async def reset_users(self) -> None:
        async with self.database.session() as session:
            await session.execute(text("TRUNCATE users CASCADE"))
            await session.commit()
