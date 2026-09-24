"""Helpers for the Memory schema tests that run against a real PostgreSQL."""

import io
import os
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from alembic import command
from sqlalchemy import create_engine, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from paw_backend.memory.models import (
    Conversation,
    Memory,
    MemoryVersion,
    Message,
)

from .support import make_settings, paw_environment
from .test_migrations import offline_config

TEST_DATABASE_URL = os.environ.get("PAW_TEST_DATABASE_URL")

MEMORY_TABLES = (
    "conversations",
    "memories",
    "memory_embeddings",
    "memory_relations",
    "memory_sources",
    "memory_versions",
    "messages",
    "session_states",
)

requires_postgres = unittest.skipUnless(
    TEST_DATABASE_URL, "PAW_TEST_DATABASE_URL is not set"
)


def migrate(action: str, revision: str) -> None:
    """Run ``alembic <action> <revision>`` against the test database."""
    with paw_environment(PAW_DATABASE_URL=TEST_DATABASE_URL):
        getattr(command, action)(offline_config(io.StringIO()), revision)


def sync_database_url() -> str:
    """The test database URL with the psycopg driver, for a synchronous engine."""
    url = make_settings(database_url=TEST_DATABASE_URL).database_url
    assert url is not None
    return url.get_secret_value()


class MemoryDatabaseTestCase(unittest.TestCase):
    """Migrated database per class, one rolled-back transaction per test.

    ``self.session`` shares the transaction with ``self.connection`` and turns
    its own ``commit()`` into a savepoint release, so nothing a test writes
    survives it. A constraint violation is asserted with ``violation()``, which
    runs the statement in a savepoint so that the transaction stays usable.
    """

    engine: Any

    @classmethod
    def setUpClass(cls) -> None:
        migrate("downgrade", "base")  # a clean start, even after a crashed run
        migrate("upgrade", "head")
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()
        migrate("downgrade", "base")

    def setUp(self) -> None:
        self.connection = self.engine.connect()
        transaction = self.connection.begin()
        self.session = Session(
            bind=self.connection, join_transaction_mode="create_savepoint"
        )
        self.addCleanup(self.connection.close)
        self.addCleanup(transaction.rollback)
        self.addCleanup(self.session.close)

    def violation(self, action: Callable[[], object]) -> str | None:
        """Run ``action``; return the name of the constraint it violated, or None."""
        try:
            with self.session.begin_nested():
                action()
        except IntegrityError as error:
            return error.orig.diag.constraint_name
        return None

    # -- rows ---------------------------------------------------------------

    def add_conversation(self, **values: Any) -> UUID:
        values.setdefault("owner_user_id", uuid4())
        return self.session.execute(
            insert(Conversation).values(**values).returning(Conversation.id)
        ).scalar_one()

    def add_message(self, conversation_id: UUID, sequence: int, **values: Any) -> UUID:
        values.setdefault("turn_id", uuid4())
        values.setdefault("role", "user")
        values.setdefault("content", "hello")
        return self.session.execute(
            insert(Message)
            .values(conversation_id=conversation_id, event_sequence=sequence, **values)
            .returning(Message.id)
        ).scalar_one()

    def add_memory(self) -> UUID:
        return self.session.execute(
            insert(Memory).values().returning(Memory.id)
        ).scalar_one()

    def add_version(self, memory_id: UUID, **overrides: Any) -> UUID:
        return self.session.execute(
            insert(MemoryVersion)
            .values(**version_values(memory_id, **overrides))
            .returning(MemoryVersion.id)
        ).scalar_one()


def version_values(memory_id: UUID, **overrides: Any) -> dict[str, Any]:
    """A valid ``memory_versions`` row (user scope) with ``overrides`` applied.

    Overriding ``scope`` drops the default owner, so the caller states the
    matching ``project_id`` / ``repo_id`` (or nothing for ``shared``).
    """
    values: dict[str, Any] = {
        "memory_id": memory_id,
        "version_number": 1,
        "scope": "user",
        "owner_user_id": uuid4(),
        "memory_type": "preference",
        "title": "Prefers tabs",
        "content": "Use tabs for indentation.",
        "status": "active",
        "confirmation_state": "confirmed",
        "freshness_policy": "permanent",
        "actor_type": "system",
    }
    if "scope" in overrides and "owner_user_id" not in overrides:
        del values["owner_user_id"]
    values.update(overrides)
    return values


def utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)
