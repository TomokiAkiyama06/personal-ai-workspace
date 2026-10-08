"""A real-PostgreSQL fixture for the Inferred Preference tests (PAW-044).

Users and projects come from ``PostgresVersioningTestCase``; the journal's
observations (consolidated entries with their outcome items), the candidates and
repositories are seeded with SQL, the way the consolidator (PAW-041) leaves them,
so a test of the confirmation flow does not depend on the consolidator (the one
end-to-end test that goes through it says so).
"""

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from paw_backend.memory.preferences import PreferenceConfirmationService

from .versioning_support import T0, PostgresVersioningTestCase, requires_postgres

__all__ = ["T0", "PostgresPreferenceTestCase", "requires_postgres"]


class PostgresPreferenceTestCase(PostgresVersioningTestCase):
    @classmethod
    def clean_tables(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(text("TRUNCATE conversations CASCADE"))
        super().clean_tables()

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.preferences = self.new_preferences()
        self._tick = 0

    def new_preferences(self, **options: Any) -> PreferenceConfirmationService:
        options.setdefault("clock", self.clock)
        return PreferenceConfirmationService(
            self._database(), options.pop("authorizer", self.authorizer), **options
        )

    # -- seeding (SQL, as the schema's owner) -----------------------------------

    def execute(self, sql: str, **parameters: Any) -> Any:
        with self.engine.begin() as connection:
            return connection.execute(text(sql), parameters)

    def seed_repo(
        self, project_id: UUID, *, acl: list[str] | None = None, name: str = "repo"
    ) -> UUID:
        repo_id = uuid4()
        self.execute(
            "INSERT INTO repositories (id, project_id, name, default_branch, source,"
            " acl_allowed, created_at, updated_at) VALUES (:id, :p, :name, 'main',"
            " 'new_local', :acl, :t, :t)",
            id=repo_id,
            p=project_id,
            name=f"{name}-{repo_id.hex[:6]}",
            acl=acl,
            t=T0,
        )
        return repo_id

    def seed_candidate(
        self,
        owner: UUID,
        key: str,
        content: str,
        *,
        state: str = "inferred",
        status: str = "active",
    ) -> UUID:
        """Version 1 of a private candidate memory, registered for ``key``."""
        memory_id = self.execute(
            "INSERT INTO memories (created_at) VALUES (:t) RETURNING id", t=T0
        ).scalar_one()
        self.execute(
            "INSERT INTO memory_versions (memory_id, version_number, scope,"
            " owner_user_id, memory_type, title, content, status, confirmation_state,"
            " freshness_policy, actor_type, attributes, created_at) VALUES (:m, 1,"
            " 'user', :o, 'worker_candidate', :k, :c, :s, :cs, 'permanent', 'system',"
            " CAST(:a AS jsonb), :t)",
            m=memory_id,
            o=owner,
            k=key,
            c=content,
            s=status,
            cs=state,
            a=json.dumps({"key": key}),
            t=T0,
        )
        self.execute(
            "INSERT INTO memory_consolidation_keys (owner_user_id, key, memory_id,"
            " applied_conversation_id, applied_event_sequence, applied_recorded_at)"
            " VALUES (:o, :k, :m, :c, 0, :t)",
            o=owner,
            k=key,
            m=memory_id,
            c=uuid4(),
            t=T0 - timedelta(days=1),
        )
        return memory_id

    def observe(
        self,
        owner: UUID,
        key: str,
        *,
        result: str = "duplicate",
        project: UUID | None = None,
        repo: UUID | None = None,
        message: str = "tabs please",
        content: str | None = None,
        index: int = 0,
        conversation: UUID | None = None,
        sequence: int = 0,
        at: datetime | None = None,
    ) -> UUID:
        """One consolidated journal entry whose outcome names ``key`` (a new
        conversation unless one is given, one second after the previous observation
        unless ``at`` is given)."""
        self._tick += 1
        at = at or T0 + timedelta(seconds=self._tick)
        if conversation is None:
            conversation = self.execute(
                "INSERT INTO conversations (owner_user_id, project_id, repo_id)"
                " VALUES (:o, :p, :r) RETURNING id",
                o=owner,
                p=project,
                r=repo,
            ).scalar_one()
        message_id = self.execute(
            "INSERT INTO messages (conversation_id, turn_id, event_sequence, role,"
            " content) VALUES (:c, :t, :n, 'user', :m) RETURNING id",
            c=conversation,
            t=uuid4(),
            n=sequence,
            m=message,
        ).scalar_one()
        item: dict[str, Any] = {"index": index, "result": result, "key": key}
        if result.startswith("held_"):
            item["candidate"] = {
                "scope": "user",
                "state": "inferred",
                "content": content or f"content of {key}",
                "supersedes": None,
                "conflicts_with": [],
            }
        outcome = {"contract": "memory-worker-output-v1", "items": [item]}
        return self.execute(
            "INSERT INTO memory_journal_entries (conversation_id, message_id, turn_id,"
            " event_sequence, owner_user_id, project_id, repo_id, recorded_at, state,"
            " consolidated_at, outcome) SELECT :c, :m, turn_id, :n, :o, :p, :r, :at,"
            " 'consolidated', :at, CAST(:outcome AS jsonb) FROM messages WHERE id = :m"
            " RETURNING id",
            c=conversation,
            m=message_id,
            n=sequence,
            o=owner,
            p=project,
            r=repo,
            at=at,
            outcome=json.dumps(outcome),
        ).scalar_one()

    # -- reading back ------------------------------------------------------------------

    def sources(self, version_id: UUID) -> list[tuple[str, str | None]]:
        return sorted(
            (row.source_type, row.source_ref)
            for row in self.rows(
                "SELECT source_type, source_ref FROM memory_sources"
                " WHERE memory_version_id = :v",
                v=version_id,
            )
        )

    def resolutions(self) -> list[tuple[UUID, int, str, UUID | None]]:
        return [
            (row.entry_id, row.item_index, row.resolution, row.memory_id)
            for row in self.rows(
                "SELECT * FROM memory_preference_resolutions ORDER BY resolved_at,"
                " entry_id"
            )
        ]
