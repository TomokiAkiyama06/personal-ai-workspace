"""How candidates are fused, judged and scored: pure rules, no database, no clock.

All numbers live in :class:`RankingPolicy`. They are **provisional** (no document
fixes them; Decision 0019 asks the Human to approve them and the benchmark of
PAW-019 / ``benchmarks/retrieval_runner.py`` is where they are tuned), so they are
data with validation, not constants spread over the code.

The order of the pipeline (MEMORY_ARCHITECTURE.md section 12) is: permission and
metadata filter, keyword + vector, rerank, deduplicate and handle conflicts, Top-N.
This module covers the arithmetic between them:

1. :func:`fuse`: Reciprocal Rank Fusion of the two candidate lists. Only the
   *ranks* of the two lists are used (never the raw ``ts_rank`` or distance,
   whose scales differ), and each is normalised by the best possible value, so
   the result is in 0..1 and depends on the readable candidates alone.
2. :func:`relevance`: the fused value, blended with the Reranker's score.
3. :func:`freshness_of`: fresh or stale (never "excluded": the database filter
   removed expired and session-only memories before the candidates existed).
4. :func:`final_score`: relevance times the structured rules. Vector similarity
   alone never adopts a memory: a candidate with no relevance has no score, and
   a high importance cannot lift it above one that matches.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from uuid import UUID

from paw_backend.memory.models import (
    ConfirmationState,
    FreshnessPolicy,
    MemoryScope,
)
from paw_backend.memory.retrieval.candidates import Candidate
from paw_backend.memory.retrieval.records import Freshness, StaleReason
from paw_backend.memory.retrieval.validation import (
    reject,
    validate_int,
    validate_number,
)
from paw_backend.memory.shared.errors import InputProblem

# More specific scopes first (REQUIREMENTS.md "Memory Conflict / Versioning /
# Retrieval": Repo > Project > User > Shared). ``project_group`` is not in that
# list (the requirements do not define a group): it is placed between Project and
# User because it applies to several projects but is derived from a user's own
# preference. A guess, listed in Decision 0019.
SCOPE_SPECIFICITY: Mapping[MemoryScope, int] = MappingProxyType(
    {
        MemoryScope.SHARED: 0,
        MemoryScope.USER: 1,
        MemoryScope.PROJECT_GROUP: 2,
        MemoryScope.PROJECT: 3,
        MemoryScope.REPO: 4,
    }
)

# confirmed > inferred > observed (rejected cannot be active).
CONFIRMATION_RANK: Mapping[ConfirmationState, int] = MappingProxyType(
    {
        ConfirmationState.REJECTED: 0,
        ConfirmationState.OBSERVED: 1,
        ConfirmationState.INFERRED: 2,
        ConfirmationState.CONFIRMED: 3,
    }
)


@dataclass(frozen=True, slots=True)
class RankingPolicy:
    """The provisional numbers of the ranking (validated when built).

    * ``rrf_k``: the constant of Reciprocal Rank Fusion, ``1 / (k + rank)``; 60 is
      the value of the original paper.
    * ``keyword_weight`` / ``vector_weight``: the weight of each leg (not both 0).
    * ``rerank_weight``: 0 keeps the fused value, 1 uses the Reranker's score alone.
    * ``confirmed_factor`` / ``inferred_factor`` / ``observed_factor``: multiply the
      relevance by the trust in the memory (confirmed above inferred above observed).
    * ``stale_factor``: multiplies a stale candidate (``on_stale: lower_priority``).
    * ``importance_span``: importance 0..100 moves the factor from
      ``1 - span`` to ``1 + span`` (50 is neutral).
    * ``pinned_factor``: multiplies a pinned memory.
    * ``scope_step``: each step of scope specificity adds this to the factor.
    * ``min_vector_similarity``: a memory whose cosine similarity to the query is
      below this is not a vector candidate (``None``: no floor, the nearest ones
      are candidates however far they are). Similarity is model specific (an
      unrelated text is near 0 for one model and near 0.4 for another), so there
      is no default until the PAW-019 benchmark has chosen the model.
    * ``near_duplicate_similarity``: the overlap of two memories' words and
      character pairs (0..1, Jaccard) from which they count as the same memory.
    """

    rrf_k: int = 60
    keyword_weight: float = 1.0
    vector_weight: float = 1.0
    rerank_weight: float = 0.7
    confirmed_factor: float = 1.0
    inferred_factor: float = 0.85
    observed_factor: float = 0.7
    stale_factor: float = 0.5
    importance_span: float = 0.2
    pinned_factor: float = 1.1
    scope_step: float = 0.02
    min_vector_similarity: float | None = None
    near_duplicate_similarity: float = 0.9

    def __post_init__(self) -> None:
        values = {
            "rrf_k": validate_int("rrf_k", self.rrf_k, low=1, high=1000),
            "keyword_weight": validate_number(
                "keyword_weight", self.keyword_weight, low=0, high=10
            ),
            "vector_weight": validate_number(
                "vector_weight", self.vector_weight, low=0, high=10
            ),
            "rerank_weight": validate_number(
                "rerank_weight", self.rerank_weight, low=0, high=1
            ),
            "confirmed_factor": validate_number(
                "confirmed_factor", self.confirmed_factor, low=0, high=2, low_open=True
            ),
            "inferred_factor": validate_number(
                "inferred_factor", self.inferred_factor, low=0, high=2, low_open=True
            ),
            "observed_factor": validate_number(
                "observed_factor", self.observed_factor, low=0, high=2, low_open=True
            ),
            "stale_factor": validate_number(
                "stale_factor", self.stale_factor, low=0, high=1, low_open=True
            ),
            "importance_span": validate_number(
                "importance_span", self.importance_span, low=0, high=0.9
            ),
            "pinned_factor": validate_number(
                "pinned_factor", self.pinned_factor, low=1, high=2
            ),
            "scope_step": validate_number(
                "scope_step", self.scope_step, low=0, high=0.2
            ),
            "min_vector_similarity": (
                None
                if self.min_vector_similarity is None
                else validate_number(
                    "min_vector_similarity", self.min_vector_similarity, low=-1, high=1
                )
            ),
            "near_duplicate_similarity": validate_number(
                "near_duplicate_similarity",
                self.near_duplicate_similarity,
                low=0,
                high=1,
                low_open=True,
            ),
        }
        if values["keyword_weight"] + values["vector_weight"] <= 0:
            raise reject("keyword_weight", InputProblem.OUT_OF_RANGE)
        for name, value in values.items():
            object.__setattr__(self, name, value)
        # The confirmation order is a rule of the requirements, not a tunable.
        if not (self.confirmed_factor >= self.inferred_factor >= self.observed_factor):
            raise reject("confirmed_factor", InputProblem.OUT_OF_RANGE)

    def confirmation_factor(self, state: ConfirmationState) -> float:
        if state is ConfirmationState.CONFIRMED:
            return self.confirmed_factor
        if state is ConfirmationState.INFERRED:
            return self.inferred_factor
        # ``observed``. ``rejected`` never reaches a result (the database refuses
        # an active rejected version); it would get the lowest factor, not an error.
        return self.observed_factor


DEFAULT_RANKING = RankingPolicy()


@dataclass(frozen=True, slots=True)
class Fused:
    """The position of one candidate in the two lists and their fused value."""

    fused: float
    keyword_rank: int | None
    vector_rank: int | None


def fuse(
    keyword_ranked: Sequence[UUID],
    vector_ranked: Sequence[UUID],
    policy: RankingPolicy = DEFAULT_RANKING,
) -> dict[UUID, Fused]:
    """Reciprocal Rank Fusion of two ranked lists of version ids (best first).

    ``fused = (wk / (k + rank_k) + wv / (k + rank_v)) / ((wk + wv) / (k + 1))``: a
    leg that did not list the candidate adds nothing, and the divisor is the value
    of a candidate that is first in both lists, so the result is in 0..1. A list
    may not name an id twice (the first position counts).
    """
    keyword_rank = _positions(keyword_ranked)
    vector_rank = _positions(vector_ranked)
    best = (policy.keyword_weight + policy.vector_weight) / (policy.rrf_k + 1)
    fused: dict[UUID, Fused] = {}
    for version_id in dict.fromkeys([*keyword_ranked, *vector_ranked]):
        k_rank = keyword_rank.get(version_id)
        v_rank = vector_rank.get(version_id)
        total = 0.0
        if k_rank is not None:
            total += policy.keyword_weight / (policy.rrf_k + k_rank)
        if v_rank is not None:
            total += policy.vector_weight / (policy.rrf_k + v_rank)
        fused[version_id] = Fused(total / best, k_rank, v_rank)
    return fused


def _positions(ranked: Sequence[UUID]) -> dict[UUID, int]:
    positions: dict[UUID, int] = {}
    for position, version_id in enumerate(ranked, start=1):
        positions.setdefault(version_id, position)
    return positions


def relevance(
    fused: float, rerank_score: float | None, policy: RankingPolicy = DEFAULT_RANKING
) -> float:
    """The text match: the fused value, blended with the Reranker's when it ran."""
    if rerank_score is None:
        return fused
    return (1 - policy.rerank_weight) * fused + policy.rerank_weight * rerank_score


def freshness_of(
    candidate: Candidate,
    now: datetime,
    repo_heads: Mapping[UUID, str] | None,
) -> tuple[Freshness, StaleReason | None]:
    """Fresh or stale at ``now`` (MEMORY_ARCHITECTURE.md section 11).

    * A memory that something already marked (``stale_since``) is stale.
    * ``revalidate``: stale once ``verified_at + revalidate_after`` has passed
      (the moment itself is stale). The columns are required by a CHECK, but a
      missing one is judged stale rather than fresh.
    * ``repo_commit``: stale when the caller gave the repository's current head
      and it is not the memory's commit. Without a head there is no evidence, so
      it is fresh; the requirement's "diff / change significance" is not judged.
    * ``permanent``, ``expiring`` (the database removed the expired ones),
      ``session_only``: fresh.
    """
    if candidate.stale_since is not None:
        return Freshness.STALE, StaleReason.MARKED_STALE
    if candidate.freshness_policy is FreshnessPolicy.REVALIDATE:
        if candidate.verified_at is None or candidate.revalidate_after is None:
            return Freshness.STALE, StaleReason.REVALIDATE_DUE
        if now >= candidate.verified_at + candidate.revalidate_after:
            return Freshness.STALE, StaleReason.REVALIDATE_DUE
    if (
        candidate.freshness_policy is FreshnessPolicy.REPO_COMMIT
        and repo_heads is not None
        and candidate.repo_id is not None
    ):
        head = repo_heads.get(candidate.repo_id)
        if head is not None and head != candidate.commit_sha:
            return Freshness.STALE, StaleReason.REPO_COMMIT_CHANGED
    return Freshness.FRESH, None


def final_score(
    relevance_value: float,
    candidate: Candidate,
    freshness: Freshness,
    policy: RankingPolicy = DEFAULT_RANKING,
) -> float:
    """``relevance`` times the structured factors (each above 0, so never a sign flip).

    ``confirmation * stale * (1 + span * (importance - 50) / 50) * pinned *
    (1 + step * scope specificity)``.
    """
    score = relevance_value * policy.confirmation_factor(candidate.confirmation_state)
    if freshness is Freshness.STALE:
        score *= policy.stale_factor
    score *= 1 + policy.importance_span * (candidate.importance - 50) / 50
    if candidate.pinned:
        score *= policy.pinned_factor
    score *= 1 + policy.scope_step * SCOPE_SPECIFICITY[candidate.scope]
    return score
