"""Helpers for the Research Scratch Store tests.

Rows are **seeded with SQL**, not through the service, and asserted with SQL, so
that a test of one service method does not depend on another method being right.
The service under test runs on its own async engine (as a second backend process
would); the seeding connection is separate and always commits.
"""

import asyncio
import unittest
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, insert, text

from paw_backend.db import Database
from paw_backend.memory.models import Memory, MemoryVersion
from paw_backend.research.scratch import (
    PromotionState,
    ScratchItem,
    ScratchStore,
)
from paw_backend.research.scratch.limits import SCRATCH_TTL
from paw_backend.research.scratch.models import ScratchItemRow, ScratchLeaseRow

from .memory_support import (
    TEST_DATABASE_URL,
    migrate,
    requires_postgres,
    sync_database_url,
    version_values,
)
from .support import make_settings

__all__ = [
    "T0",
    "FakeClock",
    "PostgresScratchTestCase",
    "requires_postgres",
]

# The instant every test "starts" at. Nothing here depends on the real clock.
T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """An injectable clock that only moves when the test says so."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class PostgresScratchTestCase(unittest.IsolatedAsyncioTestCase):
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
            connection.execute(text("TRUNCATE research_scratch_items CASCADE"))

    async def asyncSetUp(self) -> None:
        self.clean_tables()
        self.clock = FakeClock()
        self.project_id = uuid4()
        self.user_id = uuid4()
        self.store = self.new_store(self.clock)

    def new_store(self, clock: FakeClock | None = None, **options: Any) -> ScratchStore:
        """A store on an engine of its own, closed when the test ends."""
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        return ScratchStore(database, clock=clock or self.clock, **options)

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

    def seed_item(
        self,
        *,
        expires_at: datetime | None = None,
        created_at: datetime | None = None,
        **values: Any,
    ) -> UUID:
        """Insert an item and return its id.

        Give ``expires_at`` (then ``created_at`` is 24 hours earlier) or
        ``created_at`` (default: the clock's current instant). Everything else
        has a valid default and can be overridden by keyword.
        """
        if expires_at is not None:
            created_at = expires_at - SCRATCH_TTL
        elif created_at is None:
            created_at = self.clock.now
        row: dict[str, Any] = {
            "project_id": self.project_id,
            "created_by": self.user_id,
            "summary": "A summary",
        }
        row.update(values)
        row["created_at"] = created_at
        row["expires_at"] = created_at + SCRATCH_TTL
        with self.engine.begin() as connection:
            return connection.execute(
                insert(ScratchItemRow).values(**row).returning(ScratchItemRow.id)
            ).scalar_one()

    def seed_pending(self, **values: Any) -> UUID:
        values.setdefault("promotion_requested_at", T0 - timedelta(hours=1))
        return self.seed_item(promotion_state="pending", **values)

    def seed_lease(
        self,
        item_id: UUID,
        expires_at: datetime,
        *,
        holder_id: UUID | None = None,
        leased_at: datetime | None = None,
    ) -> UUID:
        holder_id = holder_id or uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                insert(ScratchLeaseRow).values(
                    item_id=item_id,
                    holder_id=holder_id,
                    leased_at=leased_at or expires_at - timedelta(seconds=60),
                    expires_at=expires_at,
                )
            )
        return holder_id

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

    def seed_memory(self) -> UUID:
        """A Long-term Memory row (with a version), to see that it is left alone."""
        with self.engine.begin() as connection:
            memory_id = connection.execute(
                insert(Memory).values().returning(Memory.id)
            ).scalar_one()
            connection.execute(
                insert(MemoryVersion).values(**version_values(memory_id))
            )
        self.addCleanup(self.delete_memory, memory_id)
        return memory_id

    def delete_memory(self, memory_id: UUID) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM memory_versions WHERE memory_id = :id"),
                {"id": memory_id},
            )
            connection.execute(
                text("DELETE FROM memories WHERE id = :id"), {"id": memory_id}
            )

    def set_item(self, item_id: UUID, **values: Any) -> None:
        """Change columns of an item directly (to set up a state)."""
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE research_scratch_items SET "
                    + ", ".join(f"{name} = :{name}" for name in values)
                    + " WHERE id = :item_id"
                ),
                {**values, "item_id": item_id},
            )

    def delete_leases(self, item_id: UUID) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM research_scratch_leases WHERE item_id = :id"),
                {"id": item_id},
            )

    # -- reading (SQL) ---------------------------------------------------------

    def row(self, item_id: UUID) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            result = connection.execute(
                text("SELECT * FROM research_scratch_items WHERE id = :id"),
                {"id": item_id},
            ).mappings()
            found = result.first()
        return dict(found) if found is not None else None

    def exists(self, item_id: UUID) -> bool:
        return self.row(item_id) is not None

    def item_ids(self) -> set[UUID]:
        with self.engine.connect() as connection:
            return set(
                connection.execute(
                    text("SELECT id FROM research_scratch_items")
                ).scalars()
            )

    def lease_rows(self, item_id: UUID) -> dict[UUID, dict[str, Any]]:
        """The lease rows of an item, keyed by holder."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT * FROM research_scratch_leases WHERE item_id = :id"),
                {"id": item_id},
            ).mappings()
            return {row["holder_id"]: dict(row) for row in rows}

    def table_count(self, table: str) -> int:
        with self.engine.connect() as connection:
            return connection.execute(
                text(f"SELECT count(*) FROM {table}")
            ).scalar_one()

    def hold_row_lock(self, sql: str, **parameters: Any):
        """Lock rows from a separate connection until ``release`` is called.

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

    def lock_item(self, item_id: UUID):
        return self.hold_row_lock(
            "SELECT id FROM research_scratch_items WHERE id = :id FOR UPDATE",
            id=item_id,
        )

    def snapshot(
        self,
        item_id: UUID,
        *,
        now: datetime | None = None,
        in_use: bool = False,
        content: bool = True,
    ) -> ScratchItem:
        """The snapshot the store must return for a row, built from SQL."""
        now = now or self.clock.now
        row = self.row(item_id)
        assert row is not None
        return ScratchItem(
            id=row["id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            created_by=row["created_by"],
            query=row["query"],
            title=row["title"],
            summary=row["summary"],
            content=row["content"] if content else None,
            source_metadata=row["source_metadata"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            expired=now >= row["expires_at"],
            pinned=row["pinned"],
            in_use=in_use,
            promotion_state=PromotionState(row["promotion_state"]),
            promotion_requested_at=row["promotion_requested_at"],
        )
