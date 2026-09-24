"""Helpers for the provenance tests.

Rows are **seeded with SQL**, not through the service, and asserted with SQL, so
that a test of one service method does not depend on another method being right.
The service under test runs on its own async engine (as a second backend process
would); the seeding connection is separate and always commits. The expected
values (fingerprints, hashes) are computed here with the standard library, never
with the code under test.
"""

import asyncio
import hashlib
import unicodedata
import unittest
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, insert, text

from paw_backend.db import Database, DatabaseNotConfiguredError
from paw_backend.research.provenance import (
    Claim,
    InvalidProvenanceInputError,
    ProvenanceError,
    ProvenanceStore,
    Source,
    SourceInput,
    SourceLinkInput,
    Stance,
)
from paw_backend.research.provenance.models import (
    ClaimRelationRow,
    ClaimRow,
    ClaimSourceRow,
    ClaimUseRow,
    SourceRelationRow,
    SourceRow,
)
from paw_backend.research.providers.contract import SourceType

from .memory_support import (
    TEST_DATABASE_URL,
    migrate,
    requires_postgres,
    sync_database_url,
)
from .support import make_settings

__all__ = [
    "GUARD_SECONDS",
    "T0",
    "FakeClock",
    "PostgresProvenanceTestCase",
    "content_hash",
    "expected_fingerprint",
    "link",
    "StoreValidationTestCase",
    "requires_postgres",
    "source_input",
    "unconfigured_store",
    "together",
]

# The instant every test "starts" at. Nothing here depends on the real clock.
T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
TABLES = (
    "research_sources",
    "research_claims",
    "research_claim_sources",
    "research_claim_uses",
    "research_claim_relations",
    "research_source_relations",
)


GUARD_SECONDS = 60.0


async def together(*coroutines: Coroutine, return_exceptions: bool = False) -> list:
    """Run the coroutines concurrently and return their results in order.

    When one fails (or the deadline passes) the others are cancelled and awaited
    before the error is raised, so no operation keeps a transaction open behind
    the test's back. Waiting is bounded by ``GUARD_SECONDS``.
    """
    tasks = [asyncio.ensure_future(coroutine) for coroutine in coroutines]
    try:
        async with asyncio.timeout(GUARD_SECONDS):
            results = await asyncio.gather(*tasks, return_exceptions=return_exceptions)
        # With ``return_exceptions`` a test looks at the *expected* store errors
        # (``ProvenanceError``); anything else is a bug and must surface as itself.
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, ProvenanceError
            ):
                raise result
        return results
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class FakeClock:
    """An injectable clock that only moves when the test says so."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def content_hash(content: str = "v1") -> str:
    """The ``sha256:`` hash of ``content`` (what a source's hash looks like)."""
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def expected_fingerprint(claim_text: str) -> str:
    """The fingerprint of a claim text, computed independently of the rules."""
    value = unicodedata.normalize("NFKC", claim_text).casefold()
    value = unicodedata.normalize("NFKC", value)
    return hashlib.sha256(" ".join(value.split()).encode("utf-8")).hexdigest()


def source_input(
    locator: str = "https://example.com/a",
    *,
    content: str = "v1",
    source_type: SourceType = SourceType.OFFICIAL_DOCS,
    fetched_at: datetime = T0,
    published_at: datetime | None = None,
    title: str = "",
) -> SourceInput:
    return SourceInput(
        locator=locator,
        source_type=source_type,
        fetched_at=fetched_at,
        content_hash=content_hash(content),
        published_at=published_at,
        title=title,
    )


def link(
    locator: str = "https://example.com/a",
    stance: Stance = Stance.SUPPORTS,
    **source_options: Any,
) -> SourceLinkInput:
    return SourceLinkInput(
        source=source_input(locator, **source_options), stance=stance
    )


def unconfigured_store(**options) -> ProvenanceStore:
    options.setdefault("clock", FakeClock())
    return ProvenanceStore(Database(make_settings()), **options)


class StoreValidationTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = unconfigured_store()

    async def rejected(self, call):
        """(field, problem) of the input error that ``call`` raises."""
        with self.assertRaises(InvalidProvenanceInputError) as raised:
            await call
        return raised.exception.field, raised.exception.problem

    async def reaches_the_database(self, call):
        with self.assertRaises(DatabaseNotConfiguredError):
            await call


class PostgresProvenanceTestCase(unittest.IsolatedAsyncioTestCase):
    """A store on a real PostgreSQL, a SQL seeding engine and an injected clock."""

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
            # A lock that a broken implementation left behind must fail the
            # test (after 10 seconds), not hang the whole run.
            connection.execute(text("SET LOCAL lock_timeout = '10s'"))
            connection.execute(
                text("TRUNCATE research_claims, research_sources CASCADE")
            )

    async def asyncSetUp(self) -> None:
        self.clean_tables()
        self.clock = FakeClock()
        self.project_id = uuid4()
        self.other_project_id = uuid4()
        self.user_id = uuid4()
        self.store = self.new_store(self.clock)

    def new_store(
        self, clock: FakeClock | None = None, **options: Any
    ) -> ProvenanceStore:
        """A store on an engine of its own, closed when the test ends."""
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        return ProvenanceStore(database, clock=clock or self.clock, **options)

    def assertWaiting(self, task: asyncio.Task) -> None:
        """The task is still running, i.e. it is waiting for a lock.

        A task that already ended raised or returned without waiting: its own
        error is re-raised (so a broken operation is reported as itself), and a
        normal return fails the test.
        """
        if task.done():
            task.result()
            self.fail("the operation did not wait for the row lock")

    def spawn(self, coroutine: Coroutine) -> asyncio.Task:
        """Run ``coroutine`` as a task that is cancelled if the test ends early."""
        task = asyncio.ensure_future(coroutine)

        async def stop() -> None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(stop)
        return task

    # -- seeding (SQL) ---------------------------------------------------------

    def seed_source(
        self,
        *,
        project_id: UUID | None = None,
        locator: str = "https://example.com/a",
        content: str = "v1",
        source_type: str = "official_docs",
        title: str = "",
        fetched_at: datetime = T0,
        published_at: datetime | None = None,
        created_at: datetime = T0,
    ) -> UUID:
        with self.engine.begin() as connection:
            return connection.execute(
                insert(SourceRow)
                .values(
                    project_id=project_id or self.project_id,
                    locator=locator,
                    source_type=source_type,
                    title=title,
                    content_hash=content_hash(content),
                    fetched_at=fetched_at,
                    published_at=published_at,
                    created_at=created_at,
                )
                .returning(SourceRow.id)
            ).scalar_one()

    def seed_claim(
        self,
        claim_text: str = "The sky is blue.",
        *,
        project_id: UUID | None = None,
        task_id: UUID | None = None,
        created_by: UUID | None = None,
        created_at: datetime = T0,
    ) -> UUID:
        with self.engine.begin() as connection:
            return connection.execute(
                insert(ClaimRow)
                .values(
                    project_id=project_id or self.project_id,
                    task_id=task_id,
                    created_by=created_by or self.user_id,
                    claim_text=claim_text,
                    text_fingerprint=expected_fingerprint(claim_text),
                    created_at=created_at,
                )
                .returning(ClaimRow.id)
            ).scalar_one()

    def seed_link(
        self,
        claim_id: UUID,
        source_id: UUID,
        stance: str = "supports",
        *,
        project_id: UUID | None = None,
        created_at: datetime = T0,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(ClaimSourceRow).values(
                    claim_id=claim_id,
                    source_id=source_id,
                    project_id=project_id or self.project_id,
                    stance=stance,
                    created_at=created_at,
                )
            )

    def seed_use(
        self,
        claim_id: UUID,
        ref_kind: str,
        ref_id: UUID,
        *,
        project_id: UUID | None = None,
        created_by: UUID | None = None,
        created_at: datetime = T0,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(ClaimUseRow).values(
                    claim_id=claim_id,
                    ref_kind=ref_kind,
                    ref_id=ref_id,
                    project_id=project_id or self.project_id,
                    created_by=created_by or self.user_id,
                    created_at=created_at,
                )
            )

    def seed_relation(
        self,
        entity: str,
        first: UUID,
        second: UUID,
        kind: str = "duplicate",
        *,
        project_id: UUID | None = None,
        created_by: UUID | None = None,
        created_at: datetime = T0,
    ) -> None:
        """A relation between two claims or two sources (any order of the ids)."""
        low, high = sorted((first, second), key=lambda value: value.int)
        model = ClaimRelationRow if entity == "claim" else SourceRelationRow
        with self.engine.begin() as connection:
            connection.execute(
                insert(model).values(
                    low_id=low,
                    high_id=high,
                    project_id=project_id or self.project_id,
                    kind=kind,
                    created_by=created_by or self.user_id,
                    created_at=created_at,
                )
            )

    def seed_task(self, project_id: UUID | None = None) -> UUID:
        task_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tasks (id, project_id, created_by, title, input,"
                    " state, attempt, retry_count, version, created_at, updated_at)"
                    " VALUES (:id, :project, :user, 'Task', CAST('{}' AS jsonb),"
                    " 'queued', 1, 0, 1, :now, :now)"
                ),
                {
                    "id": task_id,
                    "project": project_id or self.project_id,
                    "user": self.user_id,
                    "now": T0,
                },
            )
        self.addCleanup(self.delete_task, task_id)
        return task_id

    def delete_task(self, task_id: UUID) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM tasks WHERE id = :id"), {"id": task_id}
            )

    # -- reading (SQL) ---------------------------------------------------------

    def rows(self, sql: str, **parameters: Any) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            result = connection.execute(text(sql), parameters).mappings()
            return [dict(row) for row in result]

    def table_count(self, table: str) -> int:
        with self.engine.connect() as connection:
            return connection.execute(
                text(f"SELECT count(*) FROM {table}")
            ).scalar_one()

    def counts(self) -> dict[str, int]:
        """The row count of every provenance table."""
        return {table: self.table_count(table) for table in TABLES}

    def claim_rows(self) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM research_claims ORDER BY created_at, id")

    def source_rows(self) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM research_sources ORDER BY locator, id")

    def link_rows(self, claim_id: UUID) -> dict[UUID, dict[str, Any]]:
        """The links of a claim, keyed by source id."""
        return {
            row["source_id"]: row
            for row in self.rows(
                "SELECT * FROM research_claim_sources WHERE claim_id = :id",
                id=claim_id,
            )
        }

    def use_rows(self, claim_id: UUID) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM research_claim_uses WHERE claim_id = :id"
            " ORDER BY ref_kind, ref_id",
            id=claim_id,
        )

    def source_from_sql(self, source_id: UUID) -> Source:
        (row,) = self.rows("SELECT * FROM research_sources WHERE id = :i", i=source_id)
        return Source(
            id=row["id"],
            project_id=row["project_id"],
            locator=row["locator"],
            source_type=SourceType(row["source_type"]),
            title=row["title"],
            content_hash=row["content_hash"],
            fetched_at=row["fetched_at"],
            published_at=row["published_at"],
            created_at=row["created_at"],
        )

    def claim_from_sql(self, claim_id: UUID) -> Claim:
        (row,) = self.rows("SELECT * FROM research_claims WHERE id = :i", i=claim_id)
        return Claim(
            id=row["id"],
            project_id=row["project_id"],
            text=row["claim_text"],
            task_id=row["task_id"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    def hold_row_lock(self, sql: str, **parameters: Any):
        """Lock rows from a separate connection until the test ends.

        ``sql`` is a ``SELECT ... FOR UPDATE``. Returns ``(connection,
        transaction)``; commit or roll back the transaction to release. The
        transaction is rolled back and the connection closed when the test ends.
        """
        connection = self.engine.connect()
        self.addCleanup(connection.close)
        transaction = connection.begin()
        self.addCleanup(
            lambda: transaction.rollback() if transaction.is_active else None
        )
        connection.execute(text(sql), parameters)
        return connection, transaction

    def lock_claim(self, claim_id: UUID):
        return self.hold_row_lock(
            "SELECT id FROM research_claims WHERE id = :id FOR UPDATE", id=claim_id
        )
