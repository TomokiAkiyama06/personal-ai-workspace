"""Hybrid Retrieval (PAW-043): permission-first search over Long-term Memory.

``HybridRetriever`` answers "which memories may this user see that fit this
query" from the PAW-040 tables: the permission filter comes first and is part of
every SQL statement that ranks, keyword and vector search are fused, a Reranker
(a Protocol) reorders, near-duplicates are merged, conflicts are surfaced (never
resolved), and scope, freshness, confirmation state and importance are weighed.
There is no HTTP surface yet. See ``service.py`` for the pipeline and
``apps/backend/README.md`` ("Hybrid Retrieval") for the rules and their limits.
"""

from paw_backend.memory.retrieval.errors import (
    Component,
    InvalidRetrievalInputError,
    RetrievalDataError,
    RetrievalError,
    RetrievalPermissionError,
    RetrievalScopeLimitError,
    RetrievalSourceError,
    RetrievalTimeoutError,
)
from paw_backend.memory.retrieval.fakes import HashingEmbedder, OverlapReranker
from paw_backend.memory.retrieval.protocols import (
    Embedder,
    ProjectGroupSource,
    RepoAclSource,
    RerankCandidate,
    Reranker,
)
from paw_backend.memory.retrieval.ranking import DEFAULT_RANKING, RankingPolicy
from paw_backend.memory.retrieval.records import (
    ConflictGroup,
    DegradedStage,
    Freshness,
    MatchSource,
    RetrievalQuery,
    RetrievalResult,
    RetrievedMemory,
    StalePolicy,
    StaleReason,
)
from paw_backend.memory.retrieval.service import HybridRetriever

__all__ = [
    "DEFAULT_RANKING",
    "Component",
    "ConflictGroup",
    "DegradedStage",
    "Embedder",
    "Freshness",
    "HashingEmbedder",
    "HybridRetriever",
    "InvalidRetrievalInputError",
    "MatchSource",
    "OverlapReranker",
    "ProjectGroupSource",
    "RankingPolicy",
    "RepoAclSource",
    "RerankCandidate",
    "Reranker",
    "RetrievalDataError",
    "RetrievalError",
    "RetrievalPermissionError",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrievalScopeLimitError",
    "RetrievalSourceError",
    "RetrievalTimeoutError",
    "RetrievedMemory",
    "StalePolicy",
    "StaleReason",
]
