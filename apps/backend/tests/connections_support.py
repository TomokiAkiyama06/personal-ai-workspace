"""Helpers for the shared connection tests (not a test module: no ``test_`` prefix).

Rows are **seeded with SQL**, not through the service, and asserted with SQL, so that
a test of one method does not depend on another method being right. The service
under test runs on its own engine (as a second backend process would). Nothing here
depends on the machine's speed: gates are events, clocks are injected, deadlines are
generous.

The fakes (adapter, resolver, clock, canary credential) are in ``connections_fakes``.
"""

import asyncio
import unittest
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, text

from paw_backend.authz import (
    AgentGrant,
    Authorizer,
    Capability,
    InMemoryAuditSink,
    Principal,
    ProjectState,
    SystemRole,
)
from paw_backend.connections import (
    AdapterRegistry,
    ConnectionKind,
    ConnectionRequest,
    ConnectionService,
    UsagePurpose,
)
from paw_backend.db import Database
from paw_backend.tasks import TaskRun
from paw_backend.tools import TaskContext, TaskScope

from .connections_fakes import (
    CANARY,
    RUN,
    T0,
    FakeAdapter,
    FakeClock,
    FakeResolver,
    handle,
)
from .memory_support import sync_database_url
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate, requires_postgres

__all__ = [
    "CANARY",
    "RUN",
    "T0",
    "FakeAdapter",
    "FakeClock",
    "FakeResolver",
    "FailingSink",
    "PostgresConnectionTestCase",
    "handle",
    "requires_postgres",
]


class FailingSink:
    """An audit sink that is down (its error text holds a canary on purpose)."""

    async def record(self, event: Any) -> None:
        raise RuntimeError("audit store down " + CANARY)


class PostgresConnectionTestCase(unittest.IsolatedAsyncioTestCase):
    """A service with a real Authorizer on a real PostgreSQL and fake adapters."""

    engine: Any

    @classmethod
    def setUpClass(cls) -> None:
        migrate()
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.clean_tables()
        cls.engine.dispose()

    @classmethod
    def clean_tables(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(
                text("TRUNCATE connection_usage, connection_quotas, shared_connections")
            )
            connection.execute(text("TRUNCATE tasks CASCADE"))
            connection.execute(text("TRUNCATE users CASCADE"))

    async def asyncSetUp(self) -> None:
        self.clean_tables()
        self.sink = InMemoryAuditSink()
        self.adapters = AdapterRegistry()
        self.codex = FakeAdapter(ConnectionKind.CODEX)
        self.claude = FakeAdapter(ConnectionKind.CLAUDE)
        self.adapters.register(self.codex)
        self.adapters.register(self.claude)
        self.resolver = FakeResolver({handle(1): CANARY, handle(2): CANARY + "-second"})
        self.service = self.new_service()
        self.user = self.seed_user()
        self.admin = self.seed_user(system_role="admin")
        self.owner = self.seed_user(system_role="owner")

    def new_service(
        self,
        *,
        authorizer_sink: Any = None,
        audit_sink: Any = None,
        **options: Any,
    ) -> ConnectionService:
        """A service on an engine of its own, closed when the test ends.

        ``authorizer_sink`` and ``audit_sink`` replace the sink of the Authorizer
        (the capability decisions) and of the module's own events (default: the
        same in-memory sink, ``self.sink``).
        """
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        return ConnectionService(
            database,
            Authorizer(authorizer_sink or self.sink),
            audit_sink or self.sink,
            self.adapters,
            self.resolver,
            **options,
        )

    # -- actors and requests -------------------------------------------------

    def principal(self, user_id: uuid.UUID) -> Principal:
        """The Principal of a seeded user, with the role stored for it."""
        role = self.scalar("SELECT system_role FROM users WHERE id = :u", u=user_id)
        return Principal(user_id, SystemRole(role))

    @staticmethod
    def context(
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        run: TaskRun = RUN,
    ) -> TaskContext:
        return TaskContext(
            task_id=task_id,
            delegator_id=user_id,
            grant=AgentGrant(
                uuid.uuid4(), frozenset({Capability.PROJECT_READ}), {project_id}
            ),
            scope=TaskScope(
                path_roots=["/srv/paw-test/worktree"],
                hosts=["github.com"],
                projects={project_id: ProjectState.ACTIVE},
            ),
            primary_project_id=project_id,
            run=run,
        )

    @staticmethod
    def request(**overrides: Any) -> ConnectionRequest:
        arguments: dict[str, Any] = {
            "model": "test-model-1",
            "purpose": UsagePurpose.CODING,
            "prompt": "Fix the parser.",
            "timeout_seconds": 30.0,
        }
        arguments.update(overrides)
        return ConnectionRequest(**arguments)

    # -- seeding (SQL) -------------------------------------------------------

    def seed_user(
        self, *, status: str = "active", system_role: str = "user"
    ) -> uuid.UUID:
        user_id = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, login_name, system_role, status,"
                    " passkey_required, created_at, updated_at) VALUES (:id, :login,"
                    " :role, :status, :passkey, :now, :now)"
                ),
                {
                    "id": user_id,
                    "login": "u" + user_id.hex[:12],
                    "role": system_role,
                    "status": status,
                    "passkey": system_role in ("owner", "admin"),
                    "now": T0,
                },
            )
        return user_id

    def seed_task(
        self,
        user_id: uuid.UUID,
        *,
        state: str = "running",
        attempt: int = 1,
        retry_count: int = 0,
        project_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        task_id = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tasks (id, project_id, created_by, title, input,"
                    " state, wait_reason, attempt, retry_count, version, created_at,"
                    " updated_at) VALUES (:id, :project, :user, 'Task',"
                    " CAST('{}' AS jsonb), :state, :wait, :attempt, :retry, 1, :now,"
                    " :now)"
                ),
                {
                    "id": task_id,
                    "project": project_id or uuid.uuid4(),
                    "user": user_id,
                    "state": state,
                    "wait": "user" if state == "waiting" else None,
                    "attempt": attempt,
                    "retry": retry_count,
                    "now": T0,
                },
            )
        return task_id

    def project_of(self, task_id: uuid.UUID) -> uuid.UUID:
        return self.scalar("SELECT project_id FROM tasks WHERE id = :t", t=task_id)

    def seed_connection(
        self,
        kind: ConnectionKind = ConnectionKind.CODEX,
        *,
        status: str = "connected",
        enabled: bool = True,
        secret_handle: str | None = None,
    ) -> uuid.UUID:
        connection_id = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO shared_connections (id, kind, secret_handle, status,"
                    " enabled, created_at, updated_at) VALUES (:id, :kind, :handle,"
                    " :status, :enabled, :now, :now)"
                ),
                {
                    "id": connection_id,
                    "kind": kind.value,
                    "handle": secret_handle or handle(1),
                    "status": status,
                    "enabled": enabled,
                    "now": T0,
                },
            )
        return connection_id

    def clear_connections(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM shared_connections"))

    def seed_quota(
        self,
        user_id: uuid.UUID,
        limit: int | None,
        *,
        kind: ConnectionKind = ConnectionKind.CODEX,
        metric: str = "requests",
        period: str = "day",
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO connection_quotas (user_id, kind, metric, period,"
                    " limit_value, created_at, updated_at) VALUES (:u, :k, :m, :p,"
                    " :limit, :now, :now)"
                ),
                {
                    "u": user_id,
                    "k": kind.value,
                    "m": metric,
                    "p": period,
                    "limit": limit,
                    "now": T0,
                },
            )

    def seed_usage(
        self,
        user_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        kind: ConnectionKind = ConnectionKind.CODEX,
        started_at: datetime = T0,
        tokens: int | None = 0,
        output_tokens: int | None = None,
        duration_ms: int = 0,
        status: str = "succeeded",
    ) -> uuid.UUID:
        """A usage row: settled by default (``tokens`` are input tokens, ``None`` =
        unknown), or ``status="in_flight"`` (no end, duration or tokens yet)."""
        usage_id = uuid.uuid4()
        in_flight = status == "in_flight"
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO connection_usage (id, user_id, task_id, project_id,"
                    " kind, model, purpose, status, failure_code, input_tokens,"
                    " output_tokens, started_at, finished_at, duration_ms) VALUES"
                    " (:id, :u, :t,"
                    " (SELECT project_id FROM tasks WHERE id = :t), :k, 'seeded',"
                    " 'coding', :status, :failure, :tokens, :out, :start, :finish, :ms)"
                ),
                {
                    "id": usage_id,
                    "u": user_id,
                    "t": task_id,
                    "k": kind.value,
                    "status": status,
                    "failure": "internal_error" if status == "failed" else None,
                    "tokens": None if in_flight else tokens,
                    "out": None if in_flight else output_tokens,
                    "start": started_at,
                    "finish": None
                    if in_flight
                    else started_at + timedelta(milliseconds=duration_ms),
                    "ms": None if in_flight else duration_ms,
                },
            )
        return usage_id

    # -- reading (SQL) -------------------------------------------------------

    def scalar(self, sql: str, **parameters: Any) -> Any:
        with self.engine.connect() as connection:
            return connection.execute(text(sql), parameters).scalar()

    def rows(self, sql: str, **parameters: Any) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(text(sql), parameters).mappings()
            ]

    def usage_rows(self, user_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
        if user_id is None:
            return self.rows("SELECT * FROM connection_usage ORDER BY started_at, id")
        return self.rows(
            "SELECT * FROM connection_usage WHERE user_id = :u ORDER BY started_at, id",
            u=user_id,
        )

    def connection_row(self, kind: ConnectionKind) -> dict[str, Any] | None:
        found = self.rows(
            "SELECT * FROM shared_connections WHERE kind = :k", k=kind.value
        )
        return found[0] if found else None

    def everything_stored(self) -> str:
        """Every row of every table of the module, as one text (to look for leaks)."""
        parts = []
        for table in ("shared_connections", "connection_quotas", "connection_usage"):
            parts.append(repr(self.rows(f"SELECT * FROM {table}")))
        return "\n".join(parts)

    # -- helpers -------------------------------------------------------------

    def spawn(self, coroutine: Awaitable) -> asyncio.Task:
        """Run ``coroutine`` as a task that is cancelled if the test ends early."""
        task = asyncio.ensure_future(coroutine)

        async def stop() -> None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(stop)
        return task

    async def wait_for(self, condition: Callable[[], bool], what: str = "condition"):
        """Poll (yielding to the loop) until ``condition()``; a generous deadline."""
        async with asyncio.timeout(30):
            for _ in range(10_000):
                if condition():
                    return
                await asyncio.sleep(0.01)
        self.fail(f"{what} did not happen")

    def hold_row_lock(self, sql: str, **parameters: Any):
        """Run ``sql`` in a transaction of its own and keep it open (its row locks
        stay) until the test releases it; returns ``(connection, transaction)``."""
        connection = self.engine.connect()
        self.addCleanup(connection.close)
        transaction = connection.begin()
        self.addCleanup(
            lambda: transaction.rollback() if transaction.is_active else None
        )
        connection.execute(text(sql), parameters)
        return connection, transaction

    async def wait_for_lock_waiters(self, count: int, limit: float = 20.0) -> None:
        """Wait until ``count`` backends are blocked on a lock held by another one:
        how the race tests order two operations without sleeping."""
        async with asyncio.timeout(limit):
            for _ in range(100_000):
                waiting = self.scalar(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname ="
                    " current_database() AND wait_event_type = 'Lock'"
                )
                if waiting >= count:
                    return
                await asyncio.sleep(0.02)

    def audit_actions(self) -> list[tuple[str, str, str]]:
        """``(action, decision, reason)`` of every audit event, in order."""
        return [(e.action, e.decision, e.reason) for e in self.sink.events]

    def own_audit(self) -> list[tuple[str, str, str]]:
        """The events of this module (not the Authorizer's capability rows)."""
        return [row for row in self.audit_actions() if row[0].startswith("connection.")]
