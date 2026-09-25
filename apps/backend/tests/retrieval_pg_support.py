"""A real-PostgreSQL fixture for the Hybrid Retrieval tests (PAW-043).

Rows are **seeded with SQL** and read back with SQL, so a retrieval test does not
depend on another service being right. The retriever runs on its own async engine
(``Database.run_abortable`` uses connections of its own), the seeding engine is
separate and always commits, and nothing depends on the real clock.
"""

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, NamedTuple
from uuid import UUID, uuid4

from sqlalchemy import text

from paw_backend.authz import (
    Authorizer,
    InMemoryAuditSink,
    Principal,
    ProjectRole,
    SystemRole,
)
from paw_backend.db import Database
from paw_backend.memory.retrieval import (
    HashingEmbedder,
    HybridRetriever,
    OverlapReranker,
    RetrievalQuery,
    RetrievalResult,
)
from paw_backend.memory.retrieval.protocols import RerankCandidate
from paw_backend.memory.shared import StaticPolicySource
from paw_backend.projects import MemberStatus

from .memory_support import requires_postgres
from .projects_support import T0, PostgresProjectTestCase
from .support import make_settings
from .task_support import TEST_DATABASE_URL

__all__ = [
    "T0",
    "requires_postgres",
    "CountingPolicies",
    "FailingEmbedder",
    "FixedEmbedder",
    "PostgresRetrievalTestCase",
    "RecordingReranker",
    "Seeded",
    "StaticGroups",
    "StaticRepoAcls",
    "titles",
]


class StaticRepoAcls:
    """A ``RepoAclSource`` with a fixed answer (a callable answers per call)."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[UUID, frozenset[UUID]]] = []

    async def repo_acls(self, user_id: UUID, project_ids: Any) -> Any:
        self.calls.append((user_id, frozenset(project_ids)))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return (
            self.answer(user_id, project_ids) if callable(self.answer) else self.answer
        )


class StaticGroups:
    """A ``ProjectGroupSource`` with a fixed answer."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[UUID] = []

    async def project_group_ids(self, user_id: UUID) -> Any:
        self.calls.append(user_id)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class CountingPolicies:
    """A policy source that counts how often it is asked."""

    def __init__(self, source: Any) -> None:
        self.source = source
        self.calls = 0

    async def items(self) -> Any:
        self.calls += 1
        if isinstance(self.source, BaseException):
            raise self.source
        return await self.source.items()


class Seeded(NamedTuple):
    memory_id: UUID
    version_id: UUID


def titles(result: RetrievalResult) -> list[str]:
    return [hit.title for hit in result.hits]


class FixedEmbedder:
    """Maps chosen texts to chosen vectors (any other text: a fixed fallback)."""

    def __init__(
        self,
        vectors: dict[str, list[float]] | None = None,
        *,
        dimensions: int = 3,
        model_id: str = "fixed-test-model",
    ) -> None:
        self.model_id = model_id
        self.dimensions = dimensions
        self.vectors = vectors or {}
        self.texts: list[str] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        fallback = [1.0] + [0.0] * (self.dimensions - 1)
        return [self.vectors.get(text, fallback) for text in texts]


class FailingEmbedder:
    """An Embedder that fails in the way the test asks for."""

    def __init__(self, behaviour: Any, *, dimensions: int = 64) -> None:
        self.model_id = "fake-hashing-embedder-v1"
        self.dimensions = dimensions
        self.behaviour = behaviour
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> Any:
        self.calls += 1
        behaviour = self.behaviour
        if isinstance(behaviour, BaseException):
            raise behaviour
        if callable(behaviour):
            return await behaviour(texts)
        return behaviour


class RecordingReranker:
    """A Reranker that records everything it is shown and answers as told."""

    def __init__(self, answer: Any = None) -> None:
        self.answer = answer
        self.calls: list[tuple[str, list[RerankCandidate]]] = []
        self._fallback = OverlapReranker()

    @property
    def seen_texts(self) -> list[str]:
        return [
            text
            for _, candidates in self.calls
            for candidate in candidates
            for text in (candidate.title, candidate.content)
        ]

    async def rerank(self, query: str, candidates: Sequence[RerankCandidate]) -> Any:
        self.calls.append((query, list(candidates)))
        answer = self.answer
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return await answer(query, candidates)
        if answer is None:
            return await self._fallback.rerank(query, candidates)
        return answer


class PostgresRetrievalTestCase(PostgresProjectTestCase):
    """Users, projects, memories and embeddings by SQL; a retriever on its own pool."""

    @classmethod
    def clean_tables(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(text("TRUNCATE memories, embedding_models CASCADE"))
        super().clean_tables()

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.embedder = HashingEmbedder()
        self.policy_source = StaticPolicySource(())
        self.sink = InMemoryAuditSink()
        self.authorizer = Authorizer(self.sink, clock=self.clock)
        self.retriever = self.new_retriever()

    def new_retriever(self, **options: Any) -> HybridRetriever:
        database = Database(make_settings(database_url=self.database_url()))
        self.addAsyncCleanup(database.dispose)
        options.setdefault("clock", self.clock)
        return HybridRetriever(
            database,
            options.pop("authorizer", self.authorizer),
            options.pop("embedder", self.embedder),
            options.pop("policies", self.policy_source),
            **options,
        )

    @staticmethod
    def new_repo_id() -> UUID:
        return uuid4()

    def database_url(self) -> str:
        return TEST_DATABASE_URL

    # -- callers ------------------------------------------------------------------

    def user(self, system_role: SystemRole = SystemRole.USER) -> Principal:
        """A new active user as a Principal WITHOUT project roles."""
        return Principal(self.seed_user(system_role=system_role.value), system_role)

    def member_of(
        self,
        project_id: UUID,
        role: ProjectRole = ProjectRole.CONTRIBUTOR,
        status: MemberStatus = MemberStatus.ACTIVE,
    ) -> Principal:
        user_id = self.seed_member(project_id, role=role, status=status)
        return Principal(user_id, SystemRole.USER)

    async def retrieve(
        self,
        actor: Principal,
        text: str,
        *,
        retriever: HybridRetriever | None = None,
        **query: Any,
    ) -> RetrievalResult:
        return await (retriever or self.retriever).retrieve(
            actor, RetrievalQuery(text, **query)
        )

    # -- seeding (SQL) ---------------------------------------------------------------

    def register_model(self, model_id: str, dimensions: int) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO embedding_models (id, dimensions)"
                    " VALUES (:id, :dimensions) ON CONFLICT (id) DO NOTHING"
                ),
                {"id": model_id, "dimensions": dimensions},
            )

    def seed(
        self,
        title: str,
        content: str = "",
        *,
        scope: str = "user",
        owner: UUID | None = None,
        project: UUID | None = None,
        repo: UUID | None = None,
        group: UUID | None = None,
        status: str = "active",
        confirmation: str = "confirmed",
        freshness: str = "permanent",
        importance: int = 50,
        pinned: bool = False,
        verified_at: datetime | None = None,
        revalidate_after: timedelta | None = None,
        expires_at: datetime | None = None,
        commit_sha: str | None = None,
        stale_since: datetime | None = None,
        subjects: Sequence[str] | None = None,
        attributes: Any = None,
        memory_id: UUID | None = None,
        version_number: int = 1,
        embedding: Sequence[float] | None = None,
        embed: bool = True,
        model_id: str | None = None,
    ) -> Seeded:
        """Insert one version (a new memory unless ``memory_id`` is given).

        The scope columns follow ``scope`` (state exactly the one id it needs).
        The embedding is the embedder's vector of ``title + content`` unless
        ``embedding`` is given or ``embed`` is false.
        """
        if attributes is None:
            attributes = {"policy_subjects": list(subjects)} if subjects else {}
        with self.engine.begin() as connection:
            if memory_id is None:
                memory_id = connection.execute(
                    text("INSERT INTO memories (created_at) VALUES (:t) RETURNING id"),
                    {"t": T0},
                ).scalar_one()
            version_id = connection.execute(
                text(
                    "INSERT INTO memory_versions (memory_id, version_number, scope,"
                    " owner_user_id, project_id, project_group_id, repo_id,"
                    " memory_type, title, content, importance, pinned, status,"
                    " confirmation_state, freshness_policy, verified_at,"
                    " revalidate_after, expires_at, commit_sha, stale_since,"
                    " attributes, actor_type, created_at) VALUES (:m, :n, :scope,"
                    " :owner, :project, :group, :repo, 'note', :title, :content,"
                    " :importance, :pinned, :status, :confirmation, :freshness,"
                    " :verified, :revalidate, :expires, :sha, :stale,"
                    " CAST(:attributes AS jsonb), 'system', :t) RETURNING id"
                ),
                {
                    "m": memory_id,
                    "n": version_number,
                    "scope": scope,
                    "owner": owner,
                    "project": project,
                    "group": group,
                    "repo": repo,
                    "title": title,
                    "content": content or title,
                    "importance": importance,
                    "pinned": pinned,
                    "status": status,
                    "confirmation": confirmation,
                    "freshness": freshness,
                    "verified": verified_at,
                    "revalidate": revalidate_after,
                    "expires": expires_at,
                    "sha": commit_sha,
                    "stale": stale_since,
                    "attributes": json.dumps(attributes),
                    "t": T0,
                },
            ).scalar_one()
        if embed or embedding is not None:
            vector = list(
                embedding
                if embedding is not None
                else self.embedder.vector(f"{title} {content or title}")
            )
            self.seed_embedding(version_id, vector, model_id=model_id)
        return Seeded(memory_id, version_id)

    def seed_embedding(
        self, version_id: UUID, vector: Sequence[float], *, model_id: str | None = None
    ) -> None:
        model = model_id or self.embedder.model_id
        self.register_model(model, len(vector))
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO memory_embeddings (memory_version_id,"
                    " embedding_model_id, dimensions, embedding)"
                    " VALUES (:v, :model, :dims, CAST(:vector AS vector))"
                ),
                {
                    "v": version_id,
                    "model": model,
                    "dims": len(vector),
                    "vector": "[" + ",".join(repr(float(x)) for x in vector) + "]",
                },
            )

    def seed_relation(
        self, newer: UUID, older: UUID, relation: str = "conflicts_with"
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO memory_relations (from_version_id, to_version_id,"
                    " relation_type) VALUES (:f, :t, :r)"
                ),
                {"f": newer, "t": older, "r": relation},
            )
