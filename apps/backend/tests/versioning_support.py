"""A real-PostgreSQL fixture for the Memory versioning tests (PAW-042).

Users, projects and memories are seeded with SQL (``PostgresRetrievalTestCase``);
the service under test and the freshness jobs run on connection pools of their own,
and the retriever of PAW-043 checks what a normal retrieval gets afterwards.
Results are read back with SQL. Nothing depends on the real clock.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import text

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
