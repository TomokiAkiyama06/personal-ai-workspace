"""Builders for the Hybrid Retrieval tests (PAW-043); not a test module."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from paw_backend.memory.models import (
    ConfirmationState,
    FreshnessPolicy,
    MemoryScope,
)
from paw_backend.memory.retrieval.candidates import Candidate, Ranked
from paw_backend.memory.retrieval.records import Freshness, MatchSource

# The instant every retrieval test "starts" at. Nothing depends on the real clock.
T0 = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


def make_candidate(**overrides: Any) -> Candidate:
    """A ``Candidate`` (a confirmed, permanent, user-scope memory) with overrides."""
    values: dict[str, Any] = {
        "version_id": uuid4(),
        "memory_id": uuid4(),
        "version_number": 1,
        "scope": MemoryScope.USER,
        "project_id": None,
        "repo_id": None,
        "project_group_id": None,
        "memory_type": "preference",
        "title": "Title",
        "content": "Content",
        "importance": 50,
        "pinned": False,
        "confirmation_state": ConfirmationState.CONFIRMED,
        "freshness_policy": FreshnessPolicy.PERMANENT,
        "verified_at": None,
        "revalidate_after": None,
        "stale_since": None,
        "commit_sha": None,
        "policy_subjects": None,
    }
    values.update(overrides)
    return Candidate(**values)


def make_ranked(*, score: float = 0.5, **overrides: Any) -> Ranked:
    """A ``Ranked`` whose candidate fields can be overridden with ``candidate_`` names.

    ``make_ranked(score=0.9, scope=MemoryScope.REPO)`` overrides the candidate's
    ``scope``; the ranking fields (``freshness`` and so on) are passed as
    ``freshness=...``.
    """
    ranking_fields = {
        "fused",
        "keyword_rank",
        "vector_rank",
        "keyword_score",
        "vector_similarity",
        "rerank_score",
        "relevance",
        "freshness",
        "stale_reason",
        "sources",
        "duplicates",
    }
    ranking = {k: v for k, v in overrides.items() if k in ranking_fields}
    candidate = make_candidate(
        **{k: v for k, v in overrides.items() if k not in ranking_fields}
    )
    values: dict[str, Any] = {
        "fused": score,
        "keyword_rank": 1,
        "vector_rank": None,
        "keyword_score": 0.5,
        "vector_similarity": None,
        "rerank_score": None,
        "relevance": score,
        "freshness": Freshness.FRESH,
        "stale_reason": None,
        "sources": (MatchSource.KEYWORD,),
    }
    values.update(ranking)
    return Ranked(candidate=candidate, score=score, **values)


def with_candidate(item: Ranked, **changes: Any) -> Ranked:
    return replace(item, candidate=replace(item.candidate, **changes))


def ids(items: Any) -> list[UUID]:
    return [item.candidate.version_id for item in items]


def later(**delta: float) -> datetime:
    return T0 + timedelta(**delta)
