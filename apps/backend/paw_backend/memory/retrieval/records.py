"""What a caller asks (:class:`RetrievalQuery`) and what it gets back.

Nothing in a result is derived from a memory the caller may not read: every field
is about a returned memory, and there are no totals, no "n more matches" and no
ids of anything that was filtered out. A result that contained such a number would
be a side channel (the count of hidden memories), which the retrieval tests rule
out by comparing results with and without the memories a caller cannot see.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from paw_backend.memory.models import ConfirmationState, MemoryScope
from paw_backend.memory.retrieval import limits
from paw_backend.memory.retrieval.validation import (
    validate_commit_heads,
    validate_enum,
    validate_enum_set,
    validate_int,
    validate_text,
    validate_uuid_set,
)


class StalePolicy(StrEnum):
    """What to do with a memory that may be out of date (``on_stale``)."""

    # Return it, marked ``stale``, with a lower score (``on_stale: lower_priority``).
    FLAG = "flag"
    # Do not return it at all.
    EXCLUDE = "exclude"


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"


class StaleReason(StrEnum):
    """Why a memory is a stale candidate (MEMORY_ARCHITECTURE.md section 11)."""

    # ``revalidate``: ``verified_at + revalidate_after`` has passed.
    REVALIDATE_DUE = "revalidate_due"
    # ``stale_since`` is set: something already marked it.
    MARKED_STALE = "marked_stale"
    # ``repo_commit``: the repository's head is not the commit the memory is from.
    REPO_COMMIT_CHANGED = "repo_commit_changed"


class MatchSource(StrEnum):
    KEYWORD = "keyword"
    VECTOR = "vector"
    # Pulled in because a ``conflicts_with`` relation ties it to another result.
    CONFLICT = "conflict"


class DegradedStage(StrEnum):
    """A quality stage that did not run. Permissions never depend on these."""

    VECTOR = "vector"  # the Embedder failed: keyword candidates only
    RERANK = "rerank"  # the Reranker failed: the fused order was used


ALL_SCOPES: frozenset[MemoryScope] = frozenset(MemoryScope)


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """One retrieval request. Validated when it is built.

    * ``text``: the query, 1 to ``MAX_QUERY_CHARS`` characters, not blank.
    * ``scopes``: which memory scopes to search; ``None`` is all of them. A scope
      the caller may not read contributes nothing whatever is asked.
    * ``project_ids`` / ``repo_ids``: narrow Project and Repo Memory to these
      (the Working Set of a task: REQUIREMENTS.md "Memory Retrieval"). ``None``
      means every project / repository the caller may read; an empty set means
      none. An id the caller may not read is ignored without a word.
    * ``limit``: at most this many memories come back (1..``MAX_LIMIT``); a
      conflict group is returned whole or not at all.
    * ``stale_policy``: what to do with stale candidates.
    * ``repo_heads``: ``{repo_id: current head commit}`` from the repository layer,
      so a ``repo_commit`` memory of another commit is a stale candidate. Without
      an entry for its repository such a memory is not judged stale by commit.
    """

    text: str
    scopes: frozenset[MemoryScope] | None = None
    project_ids: frozenset[UUID] | None = None
    repo_ids: frozenset[UUID] | None = None
    limit: int = limits.DEFAULT_LIMIT
    stale_policy: StalePolicy = StalePolicy.FLAG
    repo_heads: Mapping[UUID, str] | None = None

    def __post_init__(self) -> None:
        # Frozen dataclass: normalise through object.__setattr__, from a checked
        # snapshot (a caller's mutable set is copied, never kept).
        text = validate_text("text", self.text, max_chars=limits.MAX_QUERY_CHARS)
        scopes = (
            None
            if self.scopes is None
            else validate_enum_set(
                "scopes", self.scopes, MemoryScope, max_items=len(MemoryScope)
            )
        )
        project_ids = (
            None
            if self.project_ids is None
            else validate_uuid_set(
                "project_ids",
                self.project_ids,
                max_items=limits.MAX_REQUESTED_PROJECTS,
            )
        )
        repo_ids = (
            None
            if self.repo_ids is None
            else validate_uuid_set(
                "repo_ids", self.repo_ids, max_items=limits.MAX_REQUESTED_REPOS
            )
        )
        result_limit = validate_int("limit", self.limit, low=1, high=limits.MAX_LIMIT)
        stale_policy = validate_enum("stale_policy", self.stale_policy, StalePolicy)
        repo_heads = (
            None
            if self.repo_heads is None
            else MappingProxyType(validate_commit_heads("repo_heads", self.repo_heads))
        )
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "scopes", scopes)
        object.__setattr__(self, "project_ids", project_ids)
        object.__setattr__(self, "repo_ids", repo_ids)
        object.__setattr__(self, "limit", result_limit)
        object.__setattr__(self, "stale_policy", stale_policy)
        object.__setattr__(self, "repo_heads", repo_heads)

    @property
    def wanted_scopes(self) -> frozenset[MemoryScope]:
        return ALL_SCOPES if self.scopes is None else self.scopes


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    """One returned memory (its current, active version) and why it ranks here.

    ``score`` is what the order is by. ``relevance`` is the text match (fused
    keyword and vector rank, blended with the Reranker's score when there is one),
    before the structured rules multiply it: confirmation state, freshness,
    importance, pin and scope. ``keyword_rank`` / ``vector_rank`` are 1-based
    positions in the candidate lists of the two legs (``None``: not a candidate
    there). ``duplicates`` are the versions of near-identical memories that were
    merged into this one. ``conflict_group`` is the ``group_id`` of a
    :class:`ConflictGroup` this memory belongs to.
    """

    memory_id: UUID
    version_id: UUID
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
    freshness: Freshness
    stale_reason: StaleReason | None
    score: float
    relevance: float
    fused: float
    keyword_rank: int | None
    vector_rank: int | None
    keyword_score: float | None
    vector_similarity: float | None
    rerank_score: float | None
    sources: tuple[MatchSource, ...]
    conflict_group: int | None
    duplicates: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ConflictGroup:
    """Memories tied by ``conflicts_with`` relations. Nothing is chosen for you.

    Every member is returned as a result as well; the group only says they
    contradict each other, so that whoever assembles the context asks the user
    (MEMORY_ARCHITECTURE.md section 10: an ambiguous conflict is confirmed with
    the user) instead of taking the highest score. ``version_ids`` are ordered
    by the general precedence (confirmed before inferred, more specific scope
    first, then score).
    """

    group_id: int
    version_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """The answer of one retrieval.

    ``hits`` are ordered best first, the members of a conflict group next to each
    other. ``dropped_conflict_groups`` is the number of visible conflict groups
    that did not fit in ``limit``; ``conflicts_incomplete`` says that more
    conflicting memories than the retrieval reads per call exist among the
    readable ones. ``degraded`` lists the quality stages that failed (their
    result is still the best available); an empty tuple is the normal case.
    """

    hits: tuple[RetrievedMemory, ...] = ()
    conflicts: tuple[ConflictGroup, ...] = ()
    dropped_conflict_groups: int = 0
    conflicts_incomplete: bool = False
    degraded: tuple[DegradedStage, ...] = ()
