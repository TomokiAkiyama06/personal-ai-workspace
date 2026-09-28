"""A real-PostgreSQL fixture for the Memory versioning tests (PAW-042).

Users, projects and memories are seeded with SQL (``PostgresRetrievalTestCase``);
the service under test and the freshness jobs run on connection pools of their own,
and the retriever of PAW-043 checks what a normal retrieval gets afterwards.
Results are read back with SQL. Nothing depends on the real clock.
"""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy import event, text

from paw_backend.db import Database
from paw_backend.memory.versioning import (
    FreshnessMaintenance,
    MemoryVersioningService,
)

from .retrieval_pg_support import T0, PostgresRetrievalTestCase, requires_postgres
from .support import make_settings

__all__ = ["T0", "PostgresVersioningTestCase", "requires_postgres"]


class PostgresVersioningTestCase(PostgresRetrievalTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.versioning = self.new_versioning()
        self.freshness = self.new_freshness()

    def _database(self) -> Database:
        database = Database(make_settings(database_url=self.database_url()))
        self.addAsyncCleanup(database.dispose)
        return database

    def new_versioning(self, **options: Any) -> MemoryVersioningService:
        options.setdefault("clock", self.clock)
        return MemoryVersioningService(
            self._database(), options.pop("authorizer", self.authorizer), **options
        )

    def new_freshness(self, **options: Any) -> FreshnessMaintenance:
        options.setdefault("clock", self.clock)
        return FreshnessMaintenance(self._database(), **options)

    # -- reading back (SQL) ------------------------------------------------------------

    def rows(self, sql: str, **parameters: Any) -> list[Any]:
        with self.engine.connect() as connection:
            return list(connection.execute(text(sql), parameters))

    def versions(self, memory_id: UUID) -> list[Any]:
        return self.rows(
            "SELECT * FROM memory_versions WHERE memory_id = :m"
            " ORDER BY version_number",
            m=memory_id,
        )

    def relations(self) -> list[tuple[UUID, UUID, str, str | None]]:
        return [
            (row.from_version_id, row.to_version_id, row.relation_type, row.reason)
            for row in self.rows(
                "SELECT * FROM memory_relations ORDER BY created_at, relation_type"
            )
        ]

    def changes(self, version_id: UUID) -> list[Any]:
        return self.rows(
            "SELECT * FROM memory_metadata_changes WHERE memory_version_id = :v"
            " ORDER BY created_at",
            v=version_id,
        )

    def audit_actions(self) -> list[tuple[str, str]]:
        return [(event.action, event.decision) for event in self.sink.events]

    # -- what leaves the database ------------------------------------------------------

    async def returned_values(
        self, call: Callable[[MemoryVersioningService], Awaitable[Any]]
    ) -> tuple[Any, set[Any]]:
        """Run ``call`` and collect every value its reads of memories returned.

        Each SELECT that names ``memory_versions`` is captured with its parameters
        and executed again, afterwards, on a connection of the test (the database
        has not changed in between for a read). The values of the rows it returns
        are what the backend received. ``call``'s exception is returned, not raised.
        """
        database = self._database()
        service = MemoryVersioningService(database, self.authorizer, clock=self.clock)
        captured: list[tuple[str, Any]] = []

        def record(connection, cursor, statement, parameters, *_):
            head = statement.lstrip().upper()
            if "memory_versions" in statement and head.startswith(("SELECT", "WITH")):
                captured.append((statement, parameters))

        engine = database.engine.sync_engine
        event.listen(engine, "before_cursor_execute", record)
        try:
            try:
                outcome: Any = await call(service)
            except Exception as error:  # noqa: BLE001 - returned to the test
                outcome = error
        finally:
            event.remove(engine, "before_cursor_execute", record)
        values: set[Any] = set()
        raw = self.engine.raw_connection()
        try:
            cursor = raw.cursor()
            for statement, parameters in captured:
                cursor.execute(statement, parameters)
                for row in cursor.fetchall():
                    values.update(
                        value for value in row if isinstance(value, str | UUID)
                    )
            raw.rollback()
        finally:
            raw.close()
        return outcome, values
