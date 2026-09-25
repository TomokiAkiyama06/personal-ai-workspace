"""The database part of a retrieval: candidates, fusion, relations, in ONE snapshot.

:func:`read_pool` runs inside a single read-only ``REPEATABLE READ`` transaction, so
the keyword list, the vector list, the relations and the conflict partners all see
the same state. Every statement it runs carries the permission prefilter
(``queries.visible``), so everything in a :class:`Pool` is a memory the caller may
read. It reads nothing else, and returns no count of what it did not return.

What it does, in order:

1. the keyword candidates and the vector candidates (each at most its own limit,
   each ordered by its own score, ties by id);
2. Reciprocal Rank Fusion; only the best ``rerank_candidates`` go on;
3. the ``conflicts_with`` and ``supersedes`` relations of those candidates whose two
   ends are both readable and active; memories on the other end of a conflict are
   fetched as *partners* (at most ``MAX_CONFLICT_PARTNERS``);
4. a candidate that a readable, active version supersedes is dropped (its status
   should be ``superseded`` already; a row that is not is inconsistent data and must
   not be offered as current).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.memory.retrieval import limits
from paw_backend.memory.retrieval.candidates import Candidate, candidate_from_row
from paw_backend.memory.retrieval.queries import (
    CONFLICT,
    SUPERSEDES,
    keyword_statement,
    relations_statement,
    vector_statement,
    versions_statement,
)
from paw_backend.memory.retrieval.ranking import Fused, RankingPolicy, fuse
from paw_backend.memory.retrieval.scopes import ResolvedScopes


@dataclass(frozen=True, slots=True)
class Pool:
    """Everything the ranking stages work on; every member is readable by the caller."""

    candidates: dict[UUID, Candidate]
    fused: dict[UUID, Fused]
    keyword_scores: dict[UUID, float]
    vector_similarities: dict[UUID, float]
    # Present because of a conflict relation only (they may also be in ``fused``).
    partners: frozenset[UUID]
    # ``conflicts_with`` relations between members of ``candidates``.
    conflicts: tuple[tuple[UUID, UUID], ...]
    conflicts_incomplete: bool


async def read_pool(
    session: AsyncSession,
    scopes: ResolvedScopes,
    now: datetime,
    *,
    tsquery: str | None,
    vector: Sequence[float] | None,
    model_id: str,
    dimensions: int,
    keyword_candidates: int,
    vector_candidates: int,
    rerank_candidates: int,
    policy: RankingPolicy,
) -> Pool:
    await session.execute(
        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    )
    found: dict[UUID, Candidate] = {}
    keyword_ids: list[UUID] = []
    keyword_scores: dict[UUID, float] = {}
    vector_ids: list[UUID] = []
    vector_similarities: dict[UUID, float] = {}

    if tsquery is not None:
        statement = keyword_statement(scopes, now, tsquery, keyword_candidates)
        for row in (await session.execute(statement)).all():
            candidate = candidate_from_row(row)
            found.setdefault(candidate.version_id, candidate)
            keyword_ids.append(candidate.version_id)
            keyword_scores[candidate.version_id] = float(row.keyword_score)
    if vector is not None:
        floor = policy.min_vector_similarity
        statement = vector_statement(
            scopes,
            now,
            model_id,
            dimensions,
            vector,
            vector_candidates,
            None if floor is None else 1.0 - floor,
        )
        for row in (await session.execute(statement)).all():
            candidate = candidate_from_row(row)
            found.setdefault(candidate.version_id, candidate)
            vector_ids.append(candidate.version_id)
            vector_similarities[candidate.version_id] = 1.0 - float(row.distance)

    fused = fuse(keyword_ids, vector_ids, policy)
    best = sorted(
        fused, key=lambda version_id: (-fused[version_id].fused, str(version_id))
    )
    best = best[:rerank_candidates]
    order = {version_id: position for position, version_id in enumerate(best)}
    members = {version_id: found[version_id] for version_id in best}

    conflicts: list[tuple[UUID, UUID]] = []
    superseded: set[UUID] = set()
    incomplete = False
    partner_rows: dict[UUID, Candidate] = {}
    if best:
        statement = relations_statement(
            scopes, now, best, limits.MAX_CONFLICT_EDGES + 1
        )
        rows = (await session.execute(statement)).all()
        if len(rows) > limits.MAX_CONFLICT_EDGES:
            incomplete = True
            rows = rows[: limits.MAX_CONFLICT_EDGES]
        edges: list[tuple[UUID, UUID]] = []
        for row in rows:
            if row.relation_type == SUPERSEDES:
                if row.to_version_id in order:
                    superseded.add(row.to_version_id)
            elif row.relation_type == CONFLICT:
                edges.append((row.from_version_id, row.to_version_id))

        # Partners: the other end of a conflict, nearest to the best candidate first.
        def by_position(edge: tuple[UUID, UUID]) -> tuple[int, str]:
            inside = edge[0] if edge[0] in order else edge[1]
            other = edge[1] if inside == edge[0] else edge[0]
            return order[inside], str(other)

        wanted: list[UUID] = []
        for edge in sorted(edges, key=by_position):
            for version_id in edge:
                if version_id not in order and version_id not in wanted:
                    wanted.append(version_id)
        if len(wanted) > limits.MAX_CONFLICT_PARTNERS:
            incomplete = True
            wanted = wanted[: limits.MAX_CONFLICT_PARTNERS]
        if wanted:
            statement = versions_statement(scopes, now, wanted)
            for row in (await session.execute(statement)).all():
                partner = candidate_from_row(row)
                partner_rows[partner.version_id] = partner
        kept = set(order) | set(partner_rows)
        conflicts = [edge for edge in edges if edge[0] in kept and edge[1] in kept]

    candidates = {
        version_id: candidate
        for version_id, candidate in {**members, **partner_rows}.items()
        if version_id not in superseded
    }
    return Pool(
        candidates=candidates,
        fused=fused,
        keyword_scores=keyword_scores,
        vector_similarities=vector_similarities,
        partners=frozenset(partner_rows) - superseded,
        conflicts=tuple(edge for edge in conflicts if set(edge) <= set(candidates)),
        conflicts_incomplete=incomplete,
    )
