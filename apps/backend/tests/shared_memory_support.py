"""Helpers for the Shared Memory tests (PAW-046).

Rows are **seeded with SQL**, not through the service, and asserted with SQL, so
that a test of one service method does not depend on another method being right
(the tests that go through several methods say so). The service under test runs
on an engine of its own, as a second backend process would; the seeding engine
is separate and always commits. Nothing here depends on the real clock.
"""

import asyncio
import json
import unittest
from collections.abc import Coroutine, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text

from paw_backend.authz import (
    ALL_PROJECTS,
    AgentGrant,
    Authorizer,
    Capability,
    InMemoryAuditSink,
    Principal,
    SystemRole,
)
from paw_backend.db import Database
from paw_backend.memory.metadata import metadata_change_actor
from paw_backend.memory.models import ActorType
from paw_backend.memory.shared import (
    AgentActor,
    CandidateState,
    OriginScope,
    SharedMemory,
    SharedMemoryCandidate,
    SharedMemoryDraft,
    SharedMemoryService,
    SharedMemoryStatus,
    StaticPolicySource,
    SystemPolicyItem,
)

from .authz_support import StaticDirectory, uid
from .memory_support import (
    TEST_DATABASE_URL,
    migrate,
    requires_postgres,
    sync_database_url,
)
from .support import make_settings

__all__ = [
    "T0",
    "AsyncPostgresSharedTestCase",
    "SharedClock",
    "make_candidate",
    "make_memory",
    "policy",
    "raise_unexpected",
    "requires_postgres",
]

# The instant every test "starts" at.
T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)

OWNER_ID, ADMIN_ID, USER_ID, OTHER_USER_ID, SYSTEM_ID = (uid(n) for n in range(11, 16))
AGENT_ID = uid(21)
NIL = UUID(int=0)


def raise_unexpected(results: Iterable[Any], *allowed: type[BaseException]) -> None:
    """Re-raise the first exception in ``results`` that is not of an ``allowed`` type.

    For ``asyncio.gather(..., return_exceptions=True)``: an error the test does
    not expect (a stub that is not implemented, a database error) surfaces as
    itself instead of as a wrong count of outcomes.
    """
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, allowed):
            raise result


class SharedClock:
    """An injectable clock that only moves when the test says so."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def policy(
    policy_id: str, subject: str, statement: str = "Mandatory rule."
) -> SystemPolicyItem:
    return SystemPolicyItem(policy_id, subject, statement)


def make_memory(**overrides: Any) -> SharedMemory:
    """A ``SharedMemory`` record for the pure rule tests."""
    values: dict[str, Any] = {
        "memory_id": uuid4(),
        "version_id": uuid4(),
        "version_number": 1,
        "memory_type": "rule",
        "title": "Title A",
        "content": "Content A",
        "importance": 50,
        "policy_subjects": (),
        "status": SharedMemoryStatus.ACTIVE,
        "created_at": T0,
        "updated_at": T0,
    }
    values.update(overrides)
    return SharedMemory(**values)


def make_candidate(**overrides: Any) -> SharedMemoryCandidate:
    """A pending ``SharedMemoryCandidate`` record for the pure rule tests."""
    values: dict[str, Any] = {
        "candidate_id": uuid4(),
        "state": CandidateState.PENDING,
        "proposer_user_id": USER_ID,
        "proposer_agent_id": None,
        "origin_scope": OriginScope.USER,
        "origin_version_id": None,
        "memory_type": "rule",
        "title": "Candidate title",
        "content": "Candidate content",
        "importance": 50,
        "policy_subjects": (),
        "reason": None,
        "created_at": T0,
        "decided_by": None,
        "decided_at": None,
        "decision_reason": None,
        "memory_id": None,
    }
    values.update(overrides)
    return SharedMemoryCandidate(**values)


def draft(**overrides: Any) -> SharedMemoryDraft:
    values: dict[str, Any] = {
        "memory_type": "rule",
        "title": "Draft title",
        "content": "Draft content",
    }
    values.update(overrides)
    return SharedMemoryDraft(**values)


def agent_for(
    delegator: Principal,
    *capabilities: Capability,
    agent_id: UUID = AGENT_ID,
    projects: Any = ALL_PROJECTS,
) -> AgentActor:
    """An agent of ``delegator`` whose grant lists ``capabilities``."""
    return AgentActor(
        delegator.user_id, AgentGrant(agent_id, frozenset(capabilities), projects)
    )


class AsyncPostgresSharedTestCase(unittest.IsolatedAsyncioTestCase):
    """A service on a real PostgreSQL, a SQL seeding engine and an injected clock.

    ``self.service`` is built with the real ``Authorizer`` (an in-memory audit
    sink, a directory that knows the four users), a static policy source and the
    fake clock. Subclasses that run the same tests as another database role
    override :meth:`new_database`.
    """

    engine: Any

    @classmethod
    def setUpClass(cls) -> None:
        migrate("upgrade", "head")
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.clean_tables()
        cls.engine.dispose()

    @classmethod
    def clean_tables(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(
                text("TRUNCATE memories, shared_memory_candidates CASCADE")
            )

    async def asyncSetUp(self) -> None:
        self.clean_tables()
        self.clock = SharedClock()
        self.owner = Principal(OWNER_ID, SystemRole.OWNER)
        self.admin = Principal(ADMIN_ID, SystemRole.ADMIN)
        self.user = Principal(USER_ID, SystemRole.USER)
        self.other_user = Principal(OTHER_USER_ID, SystemRole.USER)
        # The backend's own identity (a background worker): holds no capability.
        self.system = Principal(SYSTEM_ID, SystemRole.SYSTEM)
        self.directory = StaticDirectory(
            self.owner, self.admin, self.user, self.other_user
        )
        self.sink = InMemoryAuditSink()
        self.authorizer = Authorizer(self.sink, directory=self.directory)
        self.policy_source = StaticPolicySource(())
        self.service = self.new_service()

    def new_database(self) -> Database:
        return Database(make_settings(database_url=TEST_DATABASE_URL))

    def new_service(self, **options: Any) -> SharedMemoryService:
        """A service on a connection pool of its own, closed when the test ends."""
        database = self.new_database()
        self.addAsyncCleanup(database.dispose)
        options.setdefault("authorizer", self.authorizer)
        options.setdefault("policies", self.policy_source)
        options.setdefault("clock", self.clock)
        return SharedMemoryService(database, **options)

    def spawn(self, coroutine: Coroutine) -> asyncio.Task:
        """Run ``coroutine`` as a task that is cancelled if the test ends early."""
        task = asyncio.ensure_future(coroutine)

        async def stop() -> None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(stop)
        return task

    # -- audit -------------------------------------------------------------------

    def events(self) -> list[Any]:
        return list(self.sink.events)

    def only_event(self) -> Any:
        events = self.events()
        self.assertEqual(len(events), 1, [e.action for e in events])
        return events[0]

    # -- seeding (SQL) -------------------------------------------------------------

    def seed_memory(
        self,
        *,
        title: str = "Seeded title",
        content: str = "Seeded content",
        memory_type: str = "rule",
        importance: int = 50,
        status: str = "active",
        subjects: Iterable[str] | None = None,
        attributes: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        scope: str = "shared",
        versions: int = 1,
    ) -> UUID:
        """Insert a memory with ``versions`` versions; only the last has ``status``.

        Earlier versions are ``superseded`` with content "Old content <n>". The
        scope columns follow ``scope`` (a ``user`` memory gets a random owner).
        """
        created_at = created_at or self.clock.now
        if attributes is None:
            attributes = {"policy_subjects": sorted(subjects)} if subjects else {}
        with self.engine.begin() as connection:
            memory_id = connection.execute(
                text("INSERT INTO memories (created_at) VALUES (:t) RETURNING id"),
                {"t": created_at},
            ).scalar_one()
            for number in range(1, versions + 1):
                last = number == versions
                self._insert_version(
                    connection,
                    memory_id,
                    number,
                    status=status if last else "superseded",
                    title=title if last else f"Old title {number}",
                    content=content if last else f"Old content {number}",
                    memory_type=memory_type,
                    importance=importance,
                    attributes=attributes if last else {},
                    scope=scope,
                    created_at=created_at + timedelta(seconds=number - 1),
                )
        return memory_id

    def _insert_version(
        self,
        connection: Any,
        memory_id: UUID,
        number: int,
        *,
        status: str,
        title: str,
        content: str,
        memory_type: str,
        importance: int,
        attributes: dict[str, Any],
        scope: str,
        created_at: datetime,
    ) -> None:
        owner = uuid4() if scope == "user" else None
        project = uuid4() if scope == "project" else None
        repo = uuid4() if scope == "repo" else None
        group = uuid4() if scope == "project_group" else None
        connection.execute(
            text(
                "INSERT INTO memory_versions (memory_id, version_number, scope,"
                " owner_user_id, project_id, project_group_id, repo_id, memory_type,"
                " title, content, importance, status, confirmation_state,"
                " freshness_policy, attributes, actor_type, created_at)"
                " VALUES (:m, :n, :scope, :owner, :project, :group, :repo, :type,"
                " :title, :content, :importance, :status, 'confirmed', 'permanent',"
                " CAST(:attributes AS jsonb), 'system', :created_at)"
            ),
            {
                "m": memory_id,
                "n": number,
                "scope": scope,
                "owner": owner,
                "project": project,
                "group": group,
                "repo": repo,
                "type": memory_type,
                "title": title,
                "content": content,
                "importance": importance,
                "status": status,
                "attributes": json.dumps(attributes),
                "created_at": created_at,
            },
        )

    def seed_version(
        self,
        memory_id: UUID,
        number: int,
        *,
        status: str = "active",
        scope: str = "shared",
        title: str = "Seeded title",
        content: str = "Seeded content",
        memory_type: str = "rule",
        importance: int = 50,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Add a version; a new ``active`` one first supersedes the active one."""
        with self.engine.begin() as connection:
            if status == "active":
                # The database records a status change and refuses one that names
                # no actor (revision 0071); the seeding names the system.
                connection.execute(metadata_change_actor(ActorType.SYSTEM))
                connection.execute(
                    text(
                        "UPDATE memory_versions SET status = 'superseded'"
                        " WHERE memory_id = :m AND status = 'active'"
                    ),
                    {"m": memory_id},
                )
            self._insert_version(
                connection,
                memory_id,
                number,
                status=status,
                title=title,
                content=content,
                memory_type=memory_type,
                importance=importance,
                attributes=attributes or {},
                scope=scope,
                created_at=self.clock.now + timedelta(seconds=number),
            )

    def seed_candidate(self, **values: Any) -> UUID:
        """Insert a candidate (pending unless ``state`` says otherwise)."""
        row: dict[str, Any] = {
            "state": "pending",
            "proposer_user_id": USER_ID,
            "origin_scope": "user",
            "memory_type": "rule",
            "title": "Candidate title",
            "content": "Candidate content",
            "importance": 50,
            "policy_subjects": [],
            "created_at": self.clock.now,
        }
        row.update(values)
        if row["state"] != "pending":
            row.setdefault("decided_by", ADMIN_ID)
            row.setdefault("decided_at", self.clock.now)
        columns = ", ".join(row)
        placeholders = ", ".join(f":{name}" for name in row)
        with self.engine.begin() as connection:
            return connection.execute(
                text(
                    f"INSERT INTO shared_memory_candidates ({columns})"
                    f" VALUES ({placeholders}) RETURNING id"
                ),
                row,
            ).scalar_one()

    # -- reading (SQL) ---------------------------------------------------------------

    def rows(self, sql: str, **parameters: Any) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            result = connection.execute(text(sql), parameters).mappings()
            return [dict(row) for row in result]

    def versions(self, memory_id: UUID) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM memory_versions WHERE memory_id = :m"
            " ORDER BY version_number",
            m=memory_id,
        )

    def relations(self, memory_id: UUID) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT r.*, f.version_number AS from_number, t.version_number AS to_number"
            " FROM memory_relations r"
            " JOIN memory_versions f ON f.id = r.from_version_id"
            " JOIN memory_versions t ON t.id = r.to_version_id"
            " WHERE f.memory_id = :m ORDER BY f.version_number",
            m=memory_id,
        )

    def sources(self, memory_id: UUID) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT s.* FROM memory_sources s"
            " JOIN memory_versions v ON v.id = s.memory_version_id"
            " WHERE v.memory_id = :m",
            m=memory_id,
        )

    def candidate_row(self, candidate_id: UUID) -> dict[str, Any]:
        (row,) = self.rows(
            "SELECT * FROM shared_memory_candidates WHERE id = :c", c=candidate_id
        )
        return row

    def count(self, table: str) -> int:
        return self.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"]

    def memory_ids(self) -> set[UUID]:
        return {row["id"] for row in self.rows("SELECT id FROM memories")}

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """Every row of every table the service may touch, for "nothing changed"."""
        return {
            table: self.rows(f"SELECT * FROM {table} ORDER BY 1")
            for table in (
                "memories",
                "memory_versions",
                "memory_relations",
                "memory_sources",
                "shared_memory_candidates",
            )
        }

    def hold_advisory_lock(self, key: str):
        """Hold the advisory lock ``key`` from a separate connection.

        Returns ``(connection, transaction)``; roll the transaction back to
        release. It is rolled back and the connection closed when the test ends.
        """
        connection = self.engine.connect()
        self.addCleanup(connection.close)
        transaction = connection.begin()
        self.addCleanup(
            lambda: transaction.rollback() if transaction.is_active else None
        )
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )
        return connection, transaction
