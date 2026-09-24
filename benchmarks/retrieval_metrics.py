"""Pure ranking metrics for the retrieval (embedding / reranker) benchmark.

Every function takes a ranked list of memory ids (best first) and treats relevance
as binary. The functions have no knowledge of a retriever, dataset, or model; the
benchmark runner supplies the ranked ids and the ground-truth id sets.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence


def _validate_ranked(ranked_ids: Sequence[str]) -> None:
    for memory_id in ranked_ids:
        if not isinstance(memory_id, str):
            raise TypeError("memory ids must be strings")
    if len(set(ranked_ids)) != len(ranked_ids):
        raise ValueError("ranked_ids must not contain duplicates")


def _validate_ids(name: str, ids: Collection[str]) -> None:
    for memory_id in ids:
        if not isinstance(memory_id, str):
            raise TypeError(f"{name} must contain only strings")


def _validate_k(k: int) -> None:
    if isinstance(k, bool) or not isinstance(k, int):
        raise TypeError("k must be an integer")
    if k < 1:
        raise ValueError("k must be at least 1")


def _validate_relevant(relevant_ids: Collection[str]) -> None:
    _validate_ids("relevant_ids", relevant_ids)
    if not relevant_ids:
        raise ValueError("relevant_ids must not be empty")


def recall_at_k(
    ranked_ids: Sequence[str], relevant_ids: Collection[str], k: int
) -> float:
    """Return the fraction of relevant ids found in the first ``k`` results."""
    _validate_ranked(ranked_ids)
    _validate_relevant(relevant_ids)
    _validate_k(k)
    relevant = set(relevant_ids)
    return len(relevant.intersection(ranked_ids[:k])) / len(relevant)


def reciprocal_rank(ranked_ids: Sequence[str], relevant_ids: Collection[str]) -> float:
    """Return ``1 / rank`` of the first relevant id, or ``0.0`` when none is ranked."""
    _validate_ranked(ranked_ids)
    _validate_relevant(relevant_ids)
    relevant = set(relevant_ids)
    for position, memory_id in enumerate(ranked_ids, start=1):
        if memory_id in relevant:
            return 1 / position
    return 0.0


def ndcg_at_k(
    ranked_ids: Sequence[str], relevant_ids: Collection[str], k: int
) -> float:
    """Return binary-relevance nDCG over the first ``k`` results.

    The ideal ranking places ``min(len(relevant_ids), k)`` relevant ids first.
    """
    _validate_ranked(ranked_ids)
    _validate_relevant(relevant_ids)
    _validate_k(k)
    relevant = set(relevant_ids)
    dcg = sum(
        1 / math.log2(position + 1)
        for position, memory_id in enumerate(ranked_ids[:k], start=1)
        if memory_id in relevant
    )
    ideal = sum(
        1 / math.log2(position + 1) for position in range(1, min(len(relevant), k) + 1)
    )
    return dcg / ideal


def permission_leakage_count(
    ranked_ids: Sequence[str], allowed_ids: Collection[str], k: int
) -> int:
    """Count ids among the first ``k`` results that the requester may not see.

    The benchmark requires this to be ``0`` for every query.
    """
    _validate_ranked(ranked_ids)
    _validate_ids("allowed_ids", allowed_ids)
    _validate_k(k)
    allowed = set(allowed_ids)
    return sum(1 for memory_id in ranked_ids[:k] if memory_id not in allowed)


def misselection_rate(
    ranked_ids: Sequence[str], disallowed_ids: Collection[str], k: int
) -> float:
    """Return the share of the first ``k`` results that are in ``disallowed_ids``.

    Used for the stale, superseded, and wrong-scope rates. An empty result list
    yields ``0.0``.
    """
    _validate_ranked(ranked_ids)
    _validate_ids("disallowed_ids", disallowed_ids)
    _validate_k(k)
    top = ranked_ids[:k]
    if not top:
        return 0.0
    disallowed = set(disallowed_ids)
    return sum(1 for memory_id in top if memory_id in disallowed) / len(top)


def mean(values: Sequence[float]) -> float:
    """Return the arithmetic mean of ``values``."""
    if not values:
        raise ValueError("values must not be empty")
    return sum(values) / len(values)
