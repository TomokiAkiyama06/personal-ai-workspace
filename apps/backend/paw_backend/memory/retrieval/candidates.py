"""A candidate memory as the pipeline handles it (an internal record)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from paw_backend.memory.models import (
    ConfirmationState,
    FreshnessPolicy,
    MemoryScope,
)
from paw_backend.memory.retrieval.errors import RetrievalDataError
from paw_backend.memory.retrieval.records import Freshness, MatchSource, StaleReason


@dataclass(frozen=True, slots=True)
class Candidate:
    """The columns of one readable, active ``memory_versions`` row the pipeline uses.

    ``policy_subjects`` is the raw ``attributes.policy_subjects`` value (only the
    Shared Memory precedence rule reads it); ``None`` when absent.
    """

    version_id: UUID
    memory_id: UUID
    version_number: int
    scope: MemoryScope
    project_id: UUID | None
    repo_id: UUID | None
    project_group_id: UUID | None
    memory_type: str
    title: str
    content: str
    importance: int
    pinned: bool
    confirmation_state: ConfirmationState
    freshness_policy: FreshnessPolicy
    verified_at: datetime | None
    revalidate_after: timedelta | None
    stale_since: datetime | None
    commit_sha: str | None
    policy_subjects: object = None


def candidate_from_row(row: Any) -> Candidate:
    """A :class:`Candidate` from a result row; an unknown enum value fails closed."""
    try:
        return Candidate(
            version_id=row.id,
            memory_id=row.memory_id,
            version_number=row.version_number,
            scope=MemoryScope(row.scope),
            project_id=row.project_id,
            repo_id=row.repo_id,
            project_group_id=row.project_group_id,
            memory_type=row.memory_type,
            title=row.title,
            content=row.content,
            importance=row.importance,
            pinned=row.pinned,
            confirmation_state=ConfirmationState(row.confirmation_state),
            freshness_policy=FreshnessPolicy(row.freshness_policy),
            verified_at=row.verified_at,
            revalidate_after=row.revalidate_after,
            stale_since=row.stale_since,
            commit_sha=row.commit_sha,
            policy_subjects=row.policy_subjects,
        )
    except ValueError:
        # The value is not echoed: it is stored data, not a caller's input.
        raise RetrievalDataError from None


@dataclass(frozen=True, slots=True)
class Ranked:
    """A candidate with everything the ranking decided about it."""

    candidate: Candidate
    fused: float
    keyword_rank: int | None
    vector_rank: int | None
    keyword_score: float | None
    vector_similarity: float | None
    rerank_score: float | None
    relevance: float
    score: float
    freshness: Freshness
    stale_reason: StaleReason | None
    sources: tuple[MatchSource, ...]
    duplicates: tuple[UUID, ...] = ()
