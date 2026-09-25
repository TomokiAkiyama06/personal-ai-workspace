"""Helpers for the persistent audit sink tests of issue #87 (not a test module)."""

import hashlib
import json
import unittest
import uuid
from typing import Any

from sqlalchemy import text

from paw_backend.db import Database
from paw_backend.research.privacy import (
    ExternalSendRecord,
    WithheldCounts,
)
from paw_backend.research.providers import ProviderKind

from .privacy_support import NOW
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate, new_database, requires_postgres

__all__ = [
    "COLUMNS",
    "NOW",
    "PostgresAuditTestCase",
    "TrackingDatabase",
    "audit_rows",
    "canary_never_stored",
    "fingerprint_of",
    "make_record",
    "tampered_record",
]

# The columns of an ``audit_events`` row that the tests compare.
COLUMNS = (
    "id, correlation_id, occurred_at, recorded_at, actor_id, actor_role, agent_id, "
    "action, resource_kind, resource_id, project_id, repo_id, repo_acl, decision, "
    "reason, old_role, new_role, client_request_id, details"
)


def fingerprint_of(query: str) -> str:
    """``sha256:`` and the hex digest of ``query`` (the format of the record)."""
    return "sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest()


def make_record(
    project_id: uuid.UUID | None = None, *, query: str = "python asyncio", **overrides
) -> ExternalSendRecord:
    """A valid record; ``overrides`` replace fields (and are validated)."""
    fields: dict[str, Any] = {
        "recorded_at": NOW,
        "project_id": project_id or uuid.uuid4(),
        "query_fingerprint": fingerprint_of(query),
        "query_chars": len(query),
        "provider_kinds": (ProviderKind.WEB,),
        "withheld": WithheldCounts(),
        "credentials_removed": 0,
        "pieces_matched": 0,
        "abstractions": 0,
        "truncated": False,
    }
    fields.update(overrides)
    return ExternalSendRecord(**fields)


def tampered_record(**fields: object) -> ExternalSendRecord:
    """A valid record whose fields were then set around its validation.

    A frozen dataclass can be changed with ``object.__setattr__``; the audit sink
    must not trust a record just because it is an ``ExternalSendRecord``.
    """
    record = make_record()
    for name, value in fields.items():
        object.__setattr__(record, name, value)
    return record


async def audit_rows(database: Database, project_id: uuid.UUID) -> list[dict]:
    """Every ``audit_events`` row of ``project_id``, oldest first, as dicts.

    ``whole_row`` is the row rendered as text, to search for text that must not be
    in it (the ``details`` column is jsonb: its own rendering is in there too).
    """
    async with database.session() as session:
        result = await session.execute(
            text(
                f"SELECT {COLUMNS}, audit_events::text AS whole_row "
                "FROM audit_events WHERE project_id = :project "
                "ORDER BY recorded_at, id"
            ),
            {"project": project_id},
        )
        return [dict(row) for row in result.mappings()]


def canary_never_stored(rows: list[dict], *canaries: str) -> list[str]:
    """The canaries that appear in any column of ``rows`` (should be none)."""
    found: list[str] = []
    for row in rows:
        haystack = row["whole_row"] + json.dumps(row["details"], default=str)
        for canary in canaries:
            if canary and canary in haystack:
                found.append(canary)
    return found


class TrackingDatabase(Database):
    """A Database that counts the abortable statements running at once."""

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.started = 0
        self.running = 0
        self.peak = 0
        self.timeouts: list[float | None] = []

    async def fetch_abortable(self, sql, params=None, *, timeout_seconds=None):
        self.timeouts.append(timeout_seconds)
        return await super().fetch_abortable(
            sql, params, timeout_seconds=timeout_seconds
        )

    async def _query(self, sql, params=None):
        self.started += 1
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            return await super()._query(sql, params)
        finally:
            self.running -= 1


@requires_postgres
class PostgresAuditTestCase(unittest.IsolatedAsyncioTestCase):
    """Real PostgreSQL, migrated to head, one fresh project id per test.

    self.database is the one the code under test writes with (new_database:
    the schema owner here, the unprivileged application role in
    test_privacy_audit_grants.py); self.reader is always the owner and reads
    what was written. audit_events is append-only, so a test never expects an
    empty table: self.project_id is its filter.
    """

    @classmethod
    def setUpClass(cls) -> None:
        migrate()

    def database_url(self) -> str:
        return TEST_DATABASE_URL

    def new_database(self, **settings: Any) -> TrackingDatabase:
        database = TrackingDatabase(
            make_settings(database_url=self.database_url(), **settings)
        )
        self.addAsyncCleanup(database.dispose)
        return database

    async def asyncSetUp(self) -> None:
        self.database = self.new_database()
        self.reader = new_database()
        self.addAsyncCleanup(self.reader.dispose)
        self.project_id = uuid.uuid4()

    async def rows(self, project_id: uuid.UUID | None = None) -> list[dict]:
        return await audit_rows(self.reader, project_id or self.project_id)

    async def owner_scalar(self, sql: str, **parameters: Any) -> Any:
        async with self.reader.session() as session:
            return (await session.execute(text(sql), parameters)).scalar()
