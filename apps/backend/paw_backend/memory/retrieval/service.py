"""Hybrid Retrieval (PAW-043): permission, metadata, keyword + vector, rerank, dedup.

``HybridRetriever.retrieve(actor, query)`` follows MEMORY_ARCHITECTURE.md section 12:

::

    ACL / Permission  ->  Metadata  ->  Keyword + Vector  ->  Rerank
        ->  Deduplicate / Conflict handling  ->  Top-N

1. **Permission, in the backend, first** (``resolver.py``). The Authorizer decides
   each scope; project membership is read from the database. Nothing of
   ``memory_versions`` has been touched yet, and the outcome (a
   :class:`~paw_backend.memory.retrieval.scopes.ResolvedScopes`) is the only thing
   the SQL is built from.
2. **Metadata and text in one statement each** (``queries.py``, ``pool.py``): the
   keyword query (PostgreSQL full-text) and the vector query (pgvector cosine
   distance) both carry the permission condition, ``status = 'active'`` and the
   freshness filter in their own WHERE, so the ranking sees readable rows only.
   One read-only ``REPEATABLE READ`` transaction on a connection that is shut
   down at the deadline (``Database.run_abortable``).
3. **Fuse** (Reciprocal Rank Fusion), keep the best ``rerank_candidates``, fetch
   the memories that conflict with them.
4. **System Policy** (``shared`` memories only): a shared memory that a System
   Security Policy covers is not returned (Decision 0009); an unavailable policy
   source fails the call.
5. **Rerank** (the Reranker Protocol) on memories the caller may read, then the
   structured rules: confirmed above inferred, freshness, importance, pin, scope
   (``ranking.py``).
6. **Deduplicate, group conflicts, Top-N** (``grouping.py``).

The Embedder and the Reranker are quality stages: when one fails, times out or
answers nonsense the call still succeeds and says so in ``RetrievalResult.degraded``.
The permission step never degrades: it either decides or raises.

Audit (Decision 0004): this class writes no audit event of its own. The
Authorizer records what its capabilities' modes say: ``shared_memory.read`` and
``project.read`` are ``DENIED_ONLY`` (an allowed read writes nothing) and
``memory.use`` is ``REQUIRED`` (one decision per call that asks for user scope).
"""

import asyncio
import logging
import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz import Authorizer, Principal
from paw_backend.db import Database
from paw_backend.memory.fulltext import keyword_terms, tsquery_text
from paw_backend.memory.models import MemoryScope
from paw_backend.memory.retrieval import limits
from paw_backend.memory.retrieval.candidates import Candidate, Ranked
from paw_backend.memory.retrieval.errors import (
    Component,
    RetrievalSourceError,
    RetrievalTimeoutError,
)
from paw_backend.memory.retrieval.grouping import (
    conflict_groups,
    deduplicate,
    select,
)
from paw_backend.memory.retrieval.pool import Pool, read_pool
from paw_backend.memory.retrieval.protocols import (
    Embedder,
    ProjectGroupSource,
    RepoAclSource,
    RerankCandidate,
    Reranker,
)
from paw_backend.memory.retrieval.ranking import (
    DEFAULT_RANKING,
    Fused,
    RankingPolicy,
    final_score,
    freshness_of,
    relevance,
)
from paw_backend.memory.retrieval.records import (
    DegradedStage,
    Freshness,
    MatchSource,
    RetrievalQuery,
    RetrievalResult,
    RetrievedMemory,
    StalePolicy,
)
from paw_backend.memory.retrieval.resolver import ScopeResolver
from paw_backend.memory.retrieval.stages import bounded
from paw_backend.memory.retrieval.validation import (
    reject,
    require_callable,
    validate_aware_datetime,
    validate_int,
    validate_number,
    validate_text,
)
from paw_backend.memory.shared import limits as shared_limits
from paw_backend.memory.shared.errors import (
    InputProblem,
    InvalidSharedMemoryInputError,
    PolicySourceError,
)
from paw_backend.memory.shared.policy import SystemPolicySource, load_policies
from paw_backend.memory.shared.precedence import overriding_policy_ids
from paw_backend.memory.shared.validation import validate_subject

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HybridRetriever:
    """Retrieves the memories a user may read that best answer a query."""

    def __init__(
        self,
        database: Database,
        authorizer: Authorizer,
        embedder: Embedder,
        policies: SystemPolicySource,
        *,
        reranker: Reranker | None = None,
        repo_acls: RepoAclSource | None = None,
        project_groups: ProjectGroupSource | None = None,
        ranking: RankingPolicy = DEFAULT_RANKING,
        keyword_candidates: int = limits.DEFAULT_KEYWORD_CANDIDATES,
        vector_candidates: int = limits.DEFAULT_VECTOR_CANDIDATES,
        rerank_candidates: int = limits.DEFAULT_RERANK_CANDIDATES,
        clock: Clock = _utc_now,
        timeout_seconds: float = limits.DEFAULT_TIMEOUT_SECONDS,
        stage_timeout_seconds: float = limits.DEFAULT_STAGE_TIMEOUT_SECONDS,
    ) -> None:
        """Validate every collaborator up front; a wrong one fails here, loudly.

        ``authorizer`` needs a callable ``authorize``; ``embedder`` a ``model_id``
        (1 to 200 characters), a ``dimensions`` (1 to 16000) and a callable
        ``embed``; ``policies`` a callable ``items``; ``reranker``, ``repo_acls``
        and ``project_groups`` are ``None`` or have a callable ``rerank`` /
        ``repo_acls`` / ``project_group_ids``. The candidate counts are ints (1 to
        ``MAX_CANDIDATES``, and ``MAX_RERANK_CANDIDATES`` for the rerank count);
        ``clock`` is callable and returns an aware ``datetime`` (checked at every
        use); the timeouts are numbers above 0 and at most 60 seconds. Otherwise
        :class:`InvalidRetrievalInputError`.
        """
        if not isinstance(database, Database):
            raise reject("database", InputProblem.WRONG_TYPE)
        require_callable("authorizer", authorizer, "authorize")
        require_callable("embedder", embedder, "embed")
        model_id = validate_text(
            "embedder", getattr(embedder, "model_id", None), max_chars=200
        )
        dimensions = validate_int(
            "embedder", getattr(embedder, "dimensions", None), low=1, high=16000
        )
        require_callable("policies", policies, "items")
        if reranker is not None:
            require_callable("reranker", reranker, "rerank")
        if repo_acls is not None:
            require_callable("repo_acls", repo_acls, "repo_acls")
        if project_groups is not None:
            require_callable("project_groups", project_groups, "project_group_ids")
        if not isinstance(ranking, RankingPolicy):
            raise reject("ranking", InputProblem.WRONG_TYPE)
        if not callable(clock):
            raise reject("clock", InputProblem.WRONG_TYPE)
        self._keyword_candidates = validate_int(
            "keyword_candidates", keyword_candidates, low=1, high=limits.MAX_CANDIDATES
        )
        self._vector_candidates = validate_int(
            "vector_candidates", vector_candidates, low=1, high=limits.MAX_CANDIDATES
        )
        self._rerank_candidates = validate_int(
            "rerank_candidates",
            rerank_candidates,
            low=1,
            high=limits.MAX_RERANK_CANDIDATES,
        )
        self._timeout = validate_number(
            "timeout_seconds",
            timeout_seconds,
            low=0,
            high=limits.MAX_TIMEOUT_SECONDS,
            low_open=True,
        )
        self._stage_timeout = validate_number(
            "stage_timeout_seconds",
            stage_timeout_seconds,
            low=0,
            high=limits.MAX_TIMEOUT_SECONDS,
            low_open=True,
        )
        self._database = database
        self._embedder = embedder
        self._model_id = model_id
        self._dimensions = dimensions
        self._policies = policies
        self._reranker = reranker
        self._ranking = ranking
        self._clock = clock
        self._resolver = ScopeResolver(
            database,
            authorizer,
            repo_acls=repo_acls,
            project_groups=project_groups,
            stage_timeout_seconds=self._stage_timeout,
        )

    # -- entry point -------------------------------------------------------------

    async def retrieve(
        self, actor: Principal, query: RetrievalQuery
    ) -> RetrievalResult:
        """The memories ``actor`` may read that best answer ``query``.

        ``actor`` is the authenticated user (a ``paw_backend.authz.Principal``); the
        project roles it carries are ignored, the database decides membership.
        Raises :class:`InvalidRetrievalInputError` for a wrong ``actor`` or
        ``query``, :class:`RetrievalPermissionError` when a decision could not be
        recorded, :class:`RetrievalSourceError` when the System Policy or a scope
        source failed, :class:`RetrievalScopeLimitError` for a user in too many
        projects, and :class:`RetrievalTimeoutError` after ``timeout_seconds``.
        """
        if not isinstance(actor, Principal):
            raise reject("actor", InputProblem.WRONG_TYPE)
        if not isinstance(query, RetrievalQuery):
            raise reject("query", InputProblem.WRONG_TYPE)
        now = validate_aware_datetime("clock", self._clock())
        deadline = asyncio.timeout(self._timeout)
        try:
            async with deadline:
                return await self._retrieve(actor, query, now)
        except TimeoutError:
            if deadline.expired():
                raise RetrievalTimeoutError from None
            raise

    # -- the pipeline ------------------------------------------------------------

    async def _retrieve(
        self, actor: Principal, query: RetrievalQuery, now: datetime
    ) -> RetrievalResult:
        scopes = await self._resolver.resolve(actor, query)
        if scopes.is_empty:
            return RetrievalResult()

        degraded: list[DegradedStage] = []
        tsquery = tsquery_text(keyword_terms(query.text))
        vector = await self._embed(query.text)
        if vector is None:
            degraded.append(DegradedStage.VECTOR)
        if tsquery is None and vector is None:
            return RetrievalResult(degraded=tuple(degraded))

        async def work(session: AsyncSession) -> Pool:
            return await read_pool(
                session,
                scopes,
                now,
                tsquery=tsquery,
                vector=vector,
                model_id=self._model_id,
                dimensions=self._dimensions,
                keyword_candidates=self._keyword_candidates,
                vector_candidates=self._vector_candidates,
                rerank_candidates=self._rerank_candidates,
                policy=self._ranking,
            )

        pool = await self._database.run_abortable(work)
        candidates = await self._apply_system_policy(pool.candidates)
        candidates = self._drop_excluded_stale(candidates, query, now)

        rerank_scores = await self._rerank(query.text, candidates, pool)
        if rerank_scores is None:
            degraded.append(DegradedStage.RERANK)
            rerank_scores = {}

        ranked = [
            self._rank(candidate, pool, rerank_scores, query, now)
            for candidate in candidates.values()
        ]
        edges = [
            edge
            for edge in pool.conflicts
            if edge[0] in candidates and edge[1] in candidates
        ]
        survivors, merged_into = deduplicate(
            ranked, edges, self._ranking.near_duplicate_similarity
        )
        groups = conflict_groups(survivors, edges, merged_into)
        selection = select(survivors, groups, query.limit)
        return RetrievalResult(
            hits=tuple(_hit(item, group) for item, group in selection.hits),
            conflicts=selection.groups,
            dropped_conflict_groups=selection.dropped_groups,
            conflicts_incomplete=pool.conflicts_incomplete,
            degraded=tuple(degraded),
        )

    # -- stages ---------------------------------------------------------------

    async def _embed(self, text: str) -> list[float] | None:
        """The query vector, or ``None`` when the Embedder cannot give a good one."""
        try:
            raw = await bounded(
                lambda: self._embedder.embed([text]),
                Component.EMBEDDER,
                self._stage_timeout,
            )
        except RetrievalSourceError:
            return None
        vector = self._checked_vector(raw)
        if vector is None:
            logger.warning("retrieval component answered badly: component=embedder")
        return vector

    def _checked_vector(self, raw: object) -> list[float] | None:
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, str | bytes)
            or len(raw) != 1
        ):
            return None
        row = raw[0]
        if (
            not isinstance(row, Sequence)
            or isinstance(row, str | bytes)
            or len(row) != self._dimensions
        ):
            return None
        numbers: list[float] = []
        for value in row:
            if isinstance(value, bool) or not isinstance(value, int | float):
                return None
            number = float(value)
            if not math.isfinite(number):
                return None
            numbers.append(number)
        # The zero vector has no direction: its cosine distance is undefined.
        return numbers if any(numbers) else None

    async def _apply_system_policy(
        self, candidates: dict[UUID, Candidate]
    ) -> dict[UUID, Candidate]:
        """Drop the shared memories a System Security Policy covers (Decision 0009).

        The policy is loaded only when a shared memory is among the candidates; if
        it cannot be loaded the call fails (no memory is returned without it). A
        shared memory whose declared subjects are malformed cannot be judged and is
        left out.
        """
        shared = [c for c in candidates.values() if c.scope is MemoryScope.SHARED]
        if not shared:
            return candidates
        try:
            items = await load_policies(
                self._policies, timeout_seconds=self._stage_timeout
            )
        except PolicySourceError:
            raise RetrievalSourceError(Component.POLICY_SOURCE) from None
        dropped: set[UUID] = set()
        for candidate in shared:
            subjects = _subjects_of(candidate.policy_subjects)
            if subjects is None or overriding_policy_ids(subjects, items):
                dropped.add(candidate.version_id)
        return {k: v for k, v in candidates.items() if k not in dropped}

    def _drop_excluded_stale(
        self, candidates: dict[UUID, Candidate], query: RetrievalQuery, now: datetime
    ) -> dict[UUID, Candidate]:
        if query.stale_policy is not StalePolicy.EXCLUDE:
            return candidates
        return {
            version_id: candidate
            for version_id, candidate in candidates.items()
            if freshness_of(candidate, now, query.repo_heads)[0] is Freshness.FRESH
        }

    async def _rerank(
        self, text: str, candidates: dict[UUID, Candidate], pool: Pool
    ) -> dict[UUID, float] | None:
        """Reranker scores by version id; ``None`` when the Reranker failed.

        With no Reranker (or nothing to rerank) an empty mapping: not a failure.
        The Reranker sees the query and the text of memories the caller may read,
        without ids, in the order of the fused ranking (partners last).
        """
        reranker = self._reranker
        if reranker is None or not candidates:
            return {}
        order = sorted(
            candidates,
            key=lambda version_id: (
                -pool.fused[version_id].fused if version_id in pool.fused else 1.0,
                str(version_id),
            ),
        )
        shown = [
            RerankCandidate(
                index,
                candidates[version_id].title,
                candidates[version_id].content[: limits.MAX_RERANK_CONTENT_CHARS],
            )
            for index, version_id in enumerate(order)
        ]
        try:
            raw = await bounded(
                lambda: reranker.rerank(text, shown),
                Component.RERANKER,
                self._stage_timeout,
            )
        except RetrievalSourceError:
            return None
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, str | bytes)
            or len(raw) != len(order)
        ):
            logger.warning("retrieval component answered badly: component=reranker")
            return None
        scores: dict[UUID, float] = {}
        for version_id, value in zip(order, raw, strict=True):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                logger.warning("retrieval component answered badly: component=reranker")
                return None
            scores[version_id] = float(value)
        return scores

    def _rank(
        self,
        candidate: Candidate,
        pool: Pool,
        rerank_scores: dict[UUID, float],
        query: RetrievalQuery,
        now: datetime,
    ) -> Ranked:
        version_id = candidate.version_id
        fused = pool.fused.get(version_id, Fused(0.0, None, None))
        rerank = rerank_scores.get(version_id)
        match = relevance(fused.fused, rerank, self._ranking)
        freshness, reason = freshness_of(candidate, now, query.repo_heads)
        sources: list[MatchSource] = []
        if fused.keyword_rank is not None:
            sources.append(MatchSource.KEYWORD)
        if fused.vector_rank is not None:
            sources.append(MatchSource.VECTOR)
        if version_id in pool.partners:
            sources.append(MatchSource.CONFLICT)
        return Ranked(
            candidate=candidate,
            fused=fused.fused,
            keyword_rank=fused.keyword_rank,
            vector_rank=fused.vector_rank,
            keyword_score=pool.keyword_scores.get(version_id),
            vector_similarity=pool.vector_similarities.get(version_id),
            rerank_score=rerank,
            relevance=match,
            score=final_score(match, candidate, freshness, self._ranking),
            freshness=freshness,
            stale_reason=reason,
            sources=tuple(sources),
        )


def _subjects_of(raw: object) -> tuple[str, ...] | None:
    """The ``policy_subjects`` of a shared memory; ``None`` when malformed."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > shared_limits.MAX_POLICY_SUBJECTS:
        return None
    try:
        return tuple(sorted({validate_subject(item) for item in raw}))
    except InvalidSharedMemoryInputError:
        return None


def _hit(item: Ranked, group: int | None) -> RetrievedMemory:
    candidate = item.candidate
    return RetrievedMemory(
        memory_id=candidate.memory_id,
        version_id=candidate.version_id,
        version_number=candidate.version_number,
        scope=candidate.scope,
        project_id=candidate.project_id,
        repo_id=candidate.repo_id,
        project_group_id=candidate.project_group_id,
        memory_type=candidate.memory_type,
        title=candidate.title,
        content=candidate.content,
        importance=candidate.importance,
        pinned=candidate.pinned,
        confirmation_state=candidate.confirmation_state,
        freshness=item.freshness,
        stale_reason=item.stale_reason,
        score=item.score,
        relevance=item.relevance,
        fused=item.fused,
        keyword_rank=item.keyword_rank,
        vector_rank=item.vector_rank,
        keyword_score=item.keyword_score,
        vector_similarity=item.vector_similarity,
        rerank_score=item.rerank_score,
        sources=item.sources,
        conflict_group=group,
        duplicates=item.duplicates,
    )
