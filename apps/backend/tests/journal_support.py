"""Helpers for the Immediate Journal tests (PAW-041).

Rows are seeded and asserted with SQL on an engine of the schema's owner, so a test
of one service does not depend on another being right (a test that goes through
several says so). The services under test run on engines of their own, as a second
backend process would have. Nothing here depends on the wall clock: a test that
needs the lease of a job to be over, or a retry delay to have passed, moves the
database-side rows (``expire_lease``, ``make_due``), because the queue trusts only
the database clock and has no way to be told the time.
"""

import asyncio
import inspect
import json
import logging
import unittest
from collections import deque
from collections.abc import Coroutine
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text

from paw_backend.authz import (
    Authorizer,
    InMemoryAuditSink,
    Principal,
    SystemRole,
)
from paw_backend.db import Database
from paw_backend.memory.journal import (
    ConsolidationQueue,
    Consolidator,
    MemoryJournal,
)

from .authz_support import StaticDirectory, uid
from .memory_support import (
    TEST_DATABASE_URL,
    migrate,
    requires_postgres,
    sync_database_url,
)
from .support import make_settings

# The consolidator logs job ids and closed codes at INFO / WARNING. A test that
# looks at the log attaches its own handler; the others should not print them.
logging.getLogger("paw_backend.memory.journal").addHandler(logging.NullHandler())

__all__ = [
    "AsyncPostgresJournalTestCase",
    "ScriptedWorker",
    "memory",
    "raise_unexpected",
    "requires_postgres",
    "worker_output",
]

USER_ID, OTHER_USER_ID, ADMIN_ID = (uid(n) for n in (31, 32, 33))


def memory(key: str = "indent_style", **fields: Any) -> dict[str, Any]:
    """One memory of a worker's output (a valid one; ``fields`` override / add)."""
    values: dict[str, Any] = {
        "key": key,
        "scope": "user",
        "state": "inferred",
        "supersedes": None,
        "content": f"content of {key}",
    }
    values.update(fields)
    return values


def worker_output(*memories: dict[str, Any]) -> str:
    """The JSON text of ``memory-worker-output-v1`` with ``memories``."""
    return json.dumps({"memories": list(memories)})


def raise_unexpected(results: list, *allowed: type[BaseException]) -> None:
    """Re-raise the first exception of ``gather(return_exceptions=True)`` results
    that is not of an allowed type, so that a bug in the code under test surfaces
    as itself and not as a wrong count."""
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, allowed):
            raise result


class ScriptedWorker:
    """A Memory Worker without a model: answers from a script, records its inputs.

    Each call takes the next response: a ``str`` is returned, an exception
    instance is raised, a callable is called with the input (its result may be
    awaitable). When the script is used up, ``fallback`` answers (default: an empty
    but valid output). ``inputs`` lists the texts it was given.
    """

    def __init__(self, *responses: Any, fallback: Any = None) -> None:
        self.responses = deque(responses)
        self.fallback = worker_output() if fallback is None else fallback
        self.inputs: list[str] = []

    async def extract(self, input_text: str) -> str:
        self.inputs.append(input_text)
        response = self.responses.popleft() if self.responses else self.fallback
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(input_text)
        if inspect.isawaitable(response):
            response = await response
        return response


class AsyncPostgresJournalTestCase(unittest.IsolatedAsyncioTestCase):
    """The journal, the queue and a consolidator on a real PostgreSQL.

    ``self.journal`` is built with the real ``Authorizer`` (an in-memory audit
    sink, a directory that knows the users). Subclasses that run the same tests as
    another database role override :meth:`new_database`.
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
            connection.execute(text("TRUNCATE conversations, memories CASCADE"))

    async def asyncSetUp(self) -> None:
        self.clean_tables()
        self.user = Principal(USER_ID, SystemRole.USER)
        self.other_user = Principal(OTHER_USER_ID, SystemRole.USER)
        self.admin = Principal(ADMIN_ID, SystemRole.ADMIN)
        self.directory = StaticDirectory(self.user, self.other_user, self.admin)
        self.sink = InMemoryAuditSink()
        self.authorizer = Authorizer(self.sink, directory=self.directory)
        self.journal = self.new_journal()
        self.queue = self.new_queue()

    def new_database(self) -> Database:
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        return database

    def new_journal(self, **options: Any) -> MemoryJournal:
        return MemoryJournal(self.new_database(), self.authorizer, **options)

    def new_queue(self, **options: Any) -> ConsolidationQueue:
        return ConsolidationQueue(self.new_database(), **options)

    def new_consolidator(
        self,
        worker: Any,
        *,
        queue: ConsolidationQueue | None = None,
        worker_id: str = "worker-1",
        **options: Any,
    ) -> Consolidator:
        queue = queue or self.new_queue()
        options.setdefault("worker_timeout_seconds", 5)
        return Consolidator(
            self.new_database(), queue, worker, worker_id=worker_id, **options
        )

    def spawn(self, coroutine: Coroutine) -> asyncio.Task:
        """Run ``coroutine`` as a task that is cancelled if the test ends early."""
        task = asyncio.ensure_future(coroutine)

        async def stop() -> None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(stop)
        return task

    # -- seeding and asserting (SQL, as the schema's owner) ---------------------

    def seed_conversation(
        self,
        owner: UUID | None = None,
        *,
        project_id: UUID | None = None,
        repo_id: UUID | None = None,
    ) -> UUID:
        with self.engine.begin() as connection:
            return connection.execute(
                text(
                    "INSERT INTO conversations (owner_user_id, project_id, repo_id)"
                    " VALUES (:o, :p, :r) RETURNING id"
                ),
                {"o": owner or USER_ID, "p": project_id, "r": repo_id},
            ).scalar_one()

    def rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [
                dict(row) for row in connection.execute(text(sql), params).mappings()
            ]

    def scalar(self, sql: str, **params: Any) -> Any:
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).scalar()

    def execute(self, sql: str, **params: Any) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(sql), params)

    def seed_key_memory(
        self,
        key: str,
        content: str,
        confirmation: str,
        *,
        status: str = "active",
        owner: UUID | None = None,
        register: bool = True,
    ) -> UUID:
        """A private memory of ``owner`` (default: the test user), with a registry row
        unless ``register`` is false, as an earlier run or a manual edit left it.
        Returns the memory id."""
        with self.engine.begin() as connection:
            memory_id = connection.execute(
                text("INSERT INTO memories DEFAULT VALUES RETURNING id")
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO memory_versions (memory_id, version_number, scope,"
                    " owner_user_id, memory_type, title, content, status,"
                    " confirmation_state, freshness_policy, actor_type)"
                    " VALUES (:m, 1, 'user', :o, 'preference', :k, :c, :s, :cs,"
                    " 'permanent', 'system')"
                ),
                {
                    "m": memory_id,
                    "o": owner or USER_ID,
                    "k": key,
                    "c": content,
                    "s": status,
                    "cs": confirmation,
                },
            )
            if register:
                connection.execute(
                    text(
                        "INSERT INTO memory_consolidation_keys (owner_user_id, key,"
                        " memory_id, applied_conversation_id, applied_event_sequence,"
                        " applied_recorded_at) VALUES (:o, :k, :m, :c, 0,"
                        " '2000-01-01T00:00:00+00:00')"
                    ),
                    {"o": owner or USER_ID, "k": key, "m": memory_id, "c": uuid4()},
                )
        return memory_id

    def entry_row(self, entry_id: UUID) -> dict[str, Any]:
        (row,) = self.rows(
            "SELECT * FROM memory_journal_entries WHERE id = :i", i=entry_id
        )
        return row

    def job_row(self, job_id: int) -> dict[str, Any]:
        (row,) = self.rows(
            "SELECT * FROM memory_consolidation_queue WHERE id = :i", i=job_id
        )
        return row

    def jobs_of(self, entry_id: UUID) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM memory_consolidation_queue WHERE entry_id = :e ORDER BY id",
            e=entry_id,
        )

    def versions(self, owner: UUID | None = None) -> list[dict[str, Any]]:
        """Every memory version of ``owner`` (default: the test user), oldest first."""
        return self.rows(
            "SELECT v.*, k.key FROM memory_versions v"
            " JOIN memory_consolidation_keys k ON k.memory_id = v.memory_id"
            " WHERE v.owner_user_id = :o ORDER BY k.key, v.version_number",
            o=owner or USER_ID,
        )

    def active_versions(self, owner: UUID | None = None) -> dict[str, dict[str, Any]]:
        """The active version of each key of ``owner``."""
        return {
            row["key"]: row for row in self.versions(owner) if row["status"] == "active"
        }

    # -- moving the database-side clock of a job ---------------------------------

    def expire_lease(self, job_id: int) -> None:
        """The lease of a claimed job is over (the worker is presumed dead)."""
        self.execute(
            "UPDATE memory_consolidation_queue"
            " SET claimed_at = clock_timestamp() - interval '2 hours',"
            "     lease_expires_at = clock_timestamp() - interval '1 hour'"
            " WHERE id = :i AND status = 'claimed'",
            i=job_id,
        )

    def make_due(self, job_id: int | None = None) -> None:
        """A retry delay has passed: queued jobs (one, or all) may be claimed now."""
        self.execute(
            "UPDATE memory_consolidation_queue"
            " SET available_at = clock_timestamp() - interval '1 second'"
            " WHERE status = 'queued' AND (CAST(:i AS bigint) IS NULL OR id = :i)",
            i=job_id,
        )

    async def wait_until_a_backend_waits_for_a_lock(self, seconds: float = 15) -> None:
        """Block until some backend of the test database waits for a lock.

        A test that must hold a transaction open until a writer has reached the row
        lock cannot sleep a guessed time: it polls the server instead, so it does
        not depend on how fast the machine is.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            waiting = self.scalar(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
            if waiting:
                return
            await asyncio.sleep(0.02)
        self.fail("no backend waited for a lock")

    async def record(
        self, content: str = "I prefer tabs.", *, conversation: UUID | None = None, **kw
    ):
        """``record_user_message`` for the test user; returns the receipt."""
        conversation = conversation or self.seed_conversation()
        return await self.journal.record_user_message(
            self.user, conversation, content, **kw
        )
