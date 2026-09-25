"""Shared fixtures for the PAW-033 tests that need a real PostgreSQL."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from paw_backend.tasks.queueing import BudgetTracker, LoopDetector, TaskQueue

from .gate_support import ALWAYS_ACTIVE
from .task_support import PostgresTaskTestCase, new_database, requires_postgres

__all__ = [
    "T0",
    "FakeClock",
    "PostgresQueueingTestCase",
    "at",
    "raise_unexpected",
    "requires_postgres",
]

# A fixed origin: every test derives its times from it (never from the wall clock).
T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    """``T0`` plus ``seconds``."""
    return T0 + timedelta(seconds=seconds)


def raise_unexpected(results: list, *expected: type[BaseException]) -> None:
    """Re-raise the first exception in ``results`` (from ``gather(return_exceptions=
    True)``) that is not an instance of ``expected``, so that a bug in the code
    under test is reported as itself and not as a wrong count."""
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, expected):
            raise result


class FakeClock:
    """A settable clock for ``BudgetTracker(clock=..., allow_explicit_clock=True)``:
    it stands in for the database clock (the test seam of ``budget``)."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.now

    def set(self, seconds: float) -> None:
        self.now = at(seconds)


class PostgresQueueingTestCase(PostgresTaskTestCase):
    """Real tasks (PAW-032) in a migrated database and empty PAW-033 tables."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Always as the owner of the schema, also when the test's own connections
        # use the unprivileged application role (test_queueing_grants).
        await self.owner_sql(
            "TRUNCATE queue_entries, budget_usages, loop_failure_signatures"
        )
        self.clock = FakeClock()
        # Explicit times are the test seam (production queues use the database clock).
        self.queue = TaskQueue(
            self.database, allow_explicit_now=True, project_gate=ALWAYS_ACTIVE
        )
        # The fake clock is the test seam (production trackers use the database's).
        self.budget = BudgetTracker(
            self.database, clock=self.clock, allow_explicit_clock=True
        )
        self.loop_detector = LoopDetector(self.database)

    async def owner_sql(self, sql: str, **parameters) -> None:
        """Run ``sql`` as the owner of the schema (a test moves database-side time or
        state this way; the services under test may run as the app role)."""
        owner = new_database()
        try:
            async with owner.engine.begin() as connection:
                await connection.execute(text(sql), parameters)
        finally:
            await owner.dispose()

    async def make_tasks(self, count: int) -> list[uuid.UUID]:
        return [await self.create_task(title=f"task {i}") for i in range(count)]

    async def rows(self, sql: str, **parameters) -> list[dict]:
        async with self.database.engine.connect() as connection:
            result = await connection.execute(text(sql), parameters)
            return [dict(row) for row in result.mappings()]

    async def entry_row(self, entry_id: int) -> dict:
        (row,) = await self.rows(
            "SELECT * FROM queue_entries WHERE id = :id", id=entry_id
        )
        return row

    async def budget_row(self, task_id: uuid.UUID, kind: str) -> dict:
        (row,) = await self.rows(
            "SELECT * FROM budget_usages WHERE task_id = :t AND kind = :k",
            t=task_id,
            k=kind,
        )
        return row

    def new_queue(self, **kwargs) -> TaskQueue:
        """A queue on its own engine, as a second worker process would have."""
        kwargs.setdefault("allow_explicit_now", True)
        return TaskQueue(self.new_database(), **kwargs, project_gate=ALWAYS_ACTIVE)

    def new_budget(self, **kwargs) -> BudgetTracker:
        kwargs.setdefault("clock", self.clock)
        kwargs.setdefault("allow_explicit_clock", True)
        return BudgetTracker(self.new_database(), **kwargs)

    def new_loop_detector(self) -> LoopDetector:
        return LoopDetector(self.new_database())
