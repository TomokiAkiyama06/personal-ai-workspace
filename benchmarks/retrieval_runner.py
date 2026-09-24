"""Dataset loader and benchmark runner for retrieval (embedding / reranker) benchmarks."""

from __future__ import annotations

import inspect
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from benchmarks.json_input import decode_json
from benchmarks.metrics_collector import MetricsCollector
from benchmarks.retrieval_metrics import (
    mean,
    misselection_rate,
    ndcg_at_k,
    permission_leakage_count,
    recall_at_k,
    reciprocal_rank,
)


@dataclass(frozen=True)
class RetrievalMemory:
    """A piece of memory that can be retrieved."""

    id: str
    text: str
    acl: frozenset[str]
    status: str
    fresh: bool
    scope: str


@dataclass(frozen=True)
class RetrievalQuery:
    """A query that can be answered by retrieving memories."""

    id: str
    text: str
    requester_principals: frozenset[str]
    scope: str
    relevant_ids: frozenset[str]
    # Further scopes whose memories legitimately apply to this query (for example a
    # Repo query may use User or Shared memories). ``scope`` is always applicable.
    allowed_scopes: frozenset[str] = frozenset()

    @property
    def effective_scopes(self) -> frozenset[str]:
        """Every scope whose memories are not a scope mix-up for this query."""
        return self.allowed_scopes | {self.scope}


@dataclass(frozen=True)
class RetrievalDataset:
    """A collection of memories and queries for retrieval benchmarking."""

    memories: tuple[RetrievalMemory, ...]
    queries: tuple[RetrievalQuery, ...]


@dataclass(frozen=True)
class RetrievalReport:
    """Results of a retrieval benchmark run."""

    metrics: dict[str, int | float]
    queries: list[dict[str, int | float | str | None]]
    resources: dict[str, int | float] | None = None

    def to_dict(self) -> dict[str, object]:
        """Return plain JSON-serializable data without memory or query text."""
        data: dict[str, object] = {
            "metrics": dict(self.metrics),
            "queries": [dict(query) for query in self.queries],
        }
        if self.resources is not None:
            data["resources"] = dict(self.resources)
        return data


def visible_memory_ids(
    dataset: RetrievalDataset, query: RetrievalQuery
) -> frozenset[str]:
    """Return the set of memory IDs visible to the query requester."""
    return frozenset(
        memory.id
        for memory in dataset.memories
        if memory.acl & query.requester_principals
    )


# The memory states defined in REQUIREMENTS.md (Memory Conflict / Versioning).
_STATUSES = frozenset({"active", "superseded", "deprecated", "history"})


def _object(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")  # noqa: TRY004 - dataset problems are reported uniformly as ValueError.
    return value


def _reject_unknown_fields(item: dict, allowed: frozenset[str], where: str) -> None:
    """Reject misspelled or unsupported fields instead of silently ignoring them."""
    unknown = sorted(str(name) for name in item if name not in allowed)
    if unknown:
        raise ValueError(f"{where} has unknown field(s): {', '.join(unknown)}")


_DATASET_FIELDS = frozenset({"memories", "queries"})
_MEMORY_FIELDS = frozenset({"id", "text", "acl", "status", "fresh", "scope"})
_QUERY_FIELDS = frozenset(
    {"id", "text", "requester_principals", "scope", "allowed_scopes", "relevant_ids"}
)


def _string(item: dict, name: str, where: str) -> str:
    value = item.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: '{name}' must be a non-empty string")
    return value


def _string_list(item: dict, name: str, where: str, *, allow_empty: bool) -> list[str]:
    value = item.get(name)
    if not isinstance(value, list) or not all(
        isinstance(entry, str) and entry for entry in value
    ):
        raise ValueError(f"{where}: '{name}' must be a list of non-empty strings")
    if not value and not allow_empty:
        raise ValueError(f"{where}: '{name}' must not be empty")
    if len(set(value)) != len(value):
        raise ValueError(f"{where}: '{name}' must not contain duplicates")
    return value


def _parse_memory(item: object, index: int) -> RetrievalMemory:
    where = f"memory at index {index}"
    item = _object(item, where)
    _reject_unknown_fields(item, _MEMORY_FIELDS, where)
    memory_id = _string(item, "id", where)
    where = f"memory {memory_id}"
    status = _string(item, "status", where)
    if status not in _STATUSES:
        raise ValueError(f"Invalid status for memory {memory_id}")
    if not isinstance(item.get("fresh"), bool):
        raise ValueError(f"{where}: 'fresh' must be a boolean")  # noqa: TRY004 - dataset problems are reported uniformly as ValueError.
    return RetrievalMemory(
        id=memory_id,
        text=_string(item, "text", where),
        acl=frozenset(_string_list(item, "acl", where, allow_empty=True)),
        status=status,
        fresh=item["fresh"],
        scope=_string(item, "scope", where),
    )


def _parse_query(
    item: object, index: int, memories: dict[str, RetrievalMemory]
) -> RetrievalQuery:
    where = f"query at index {index}"
    item = _object(item, where)
    _reject_unknown_fields(item, _QUERY_FIELDS, where)
    query_id = _string(item, "id", where)
    where = f"query {query_id}"
    principals = frozenset(
        _string_list(item, "requester_principals", where, allow_empty=False)
    )
    relevant_ids = frozenset(
        _string_list(item, "relevant_ids", where, allow_empty=False)
    )
    for relevant_id in sorted(relevant_ids):
        if relevant_id not in memories:
            raise ValueError(f"{where} references non-existent memory {relevant_id}")
        if not memories[relevant_id].acl & principals:
            raise ValueError(
                f"{where} requests memory {relevant_id} that is not visible to its requester"
            )
    scope = _string(item, "scope", where)
    allowed_scopes = (
        frozenset(_string_list(item, "allowed_scopes", where, allow_empty=True))
        if "allowed_scopes" in item
        else frozenset()
    )
    effective_scopes = allowed_scopes | {scope}
    for relevant_id in sorted(relevant_ids):
        memory = memories[relevant_id]
        if (
            memory.status != "active"
            or not memory.fresh
            or memory.scope not in effective_scopes
        ):
            raise ValueError(
                f"{where}: relevant memory {relevant_id} must be active, fresh and in the "
                "query's scope or allowed scopes, or a perfect result would also count as "
                "stale, superseded or wrong-scope"
            )
    return RetrievalQuery(
        id=query_id,
        text=_string(item, "text", where),
        requester_principals=principals,
        scope=scope,
        relevant_ids=relevant_ids,
        allowed_scopes=allowed_scopes,
    )


def load_dataset(path: str) -> RetrievalDataset:
    """Load a retrieval dataset from a JSON file.

    Every malformed input raises ``ValueError`` naming only the memory or query id
    and the field; memory text is never echoed.
    """
    with open(path, encoding="utf-8") as f:
        data = _object(decode_json(f.read()), "dataset")
    _reject_unknown_fields(data, _DATASET_FIELDS, "dataset")

    memories_data = data.get("memories")
    if not isinstance(memories_data, list) or not memories_data:
        raise ValueError("Dataset must contain at least one memory")
    memories: dict[str, RetrievalMemory] = {}
    for index, item in enumerate(memories_data):
        memory = _parse_memory(item, index)
        if memory.id in memories:
            raise ValueError(f"Duplicate memory ID: {memory.id}")
        memories[memory.id] = memory

    queries_data = data.get("queries")
    if not isinstance(queries_data, list) or not queries_data:
        raise ValueError("Dataset must contain at least one query")
    queries: dict[str, RetrievalQuery] = {}
    for index, item in enumerate(queries_data):
        query = _parse_query(item, index, memories)
        if query.id in queries:
            raise ValueError(f"Duplicate query ID: {query.id}")
        queries[query.id] = query

    return RetrievalDataset(
        memories=tuple(memories.values()), queries=tuple(queries.values())
    )


class Retriever(Protocol):
    """Protocol for a retrieval system to benchmark."""

    def retrieve(
        self, query_text: str, requester_principals: Sequence[str], k: int
    ) -> Sequence[str]:
        """Retrieve top-k memory IDs for a query."""


def validate_retriever(retriever: object) -> None:
    """Raise ``TypeError`` unless ``retriever.retrieve`` can take the benchmark's call.

    Without this check a missing or mis-declared method would be swallowed as a
    failure of every single query and produce an all-zero report.
    """
    method = getattr(retriever, "retrieve", None)
    if not callable(method):
        raise TypeError("retriever must provide a callable retrieve()")
    try:
        inspect.signature(method).bind("query", (), 1)
    except TypeError:
        raise TypeError("retrieve() must accept (query_text, principals, k)") from None
    except ValueError:
        raise TypeError(
            "retrieve() has no inspectable signature; wrap it in a plain Python method"
        ) from None


def _score_queries(
    retriever: Retriever,
    dataset: RetrievalDataset,
    k: int,
    clock: Callable[[], float],
    cpu_clock: Callable[[], float],
) -> tuple[list[dict], list[float]]:
    """Run every query once and return its result records and latencies (ms).

    Only the first ``k`` distinct ids a retriever returns are scored, so a retriever
    cannot inflate a metric by appending results beyond the requested cutoff.
    """
    query_results = []
    total_latencies = []

    for query in dataset.queries:
        visible_ids = visible_memory_ids(dataset, query)
        started = clock()
        cpu_started = cpu_clock()
        try:
            retrieved = retriever.retrieve(
                query.text, tuple(sorted(query.requester_principals)), k
            )
        except Exception as error:  # noqa: BLE001 - one failing retriever call must not end the run.
            latency_ms = (clock() - started) * 1000
            total_latencies.append(latency_ms)
            query_results.append(
                {
                    "id": query.id,
                    "latency_ms": latency_ms,
                    "cpu_ms": (cpu_clock() - cpu_started) * 1000,
                    "recall_at_k": 0.0,
                    "mrr": 0.0,
                    "ndcg_at_k": 0.0,
                    "permission_leakage_count": 0,
                    "stale_rate": 0.0,
                    "superseded_rate": 0.0,
                    "scope_mismatch_rate": 0.0,
                    "error_type": type(error).__name__,
                }
            )
            continue
        latency_ms = (clock() - started) * 1000
        cpu_ms = (cpu_clock() - cpu_started) * 1000
        total_latencies.append(latency_ms)

        if isinstance(retrieved, (str, bytes)) or not isinstance(retrieved, Sequence):
            raise TypeError("retrieve() must return a sequence of memory ids")
        # Validate EVERY returned id before truncating to k, so a malformed adapter
        # cannot hide an invalid value beyond the cutoff.
        if not all(isinstance(memory_id, str) for memory_id in retrieved):
            raise TypeError("retrieve() must return only string memory ids")
        ranked = list(dict.fromkeys(retrieved))[:k]
        stale_ids = {m.id for m in dataset.memories if not m.fresh}
        superseded_ids = {m.id for m in dataset.memories if m.status != "active"}
        applicable_scopes = query.effective_scopes
        scope_mismatch_ids = {
            m.id for m in dataset.memories if m.scope not in applicable_scopes
        }
        query_results.append(
            {
                "id": query.id,
                "latency_ms": latency_ms,
                "cpu_ms": cpu_ms,
                "recall_at_k": recall_at_k(ranked, query.relevant_ids, k),
                "mrr": reciprocal_rank(ranked, query.relevant_ids),
                "ndcg_at_k": ndcg_at_k(ranked, query.relevant_ids, k),
                "permission_leakage_count": permission_leakage_count(
                    ranked, visible_ids, k
                ),
                "stale_rate": misselection_rate(ranked, stale_ids, k),
                "superseded_rate": misselection_rate(ranked, superseded_ids, k),
                "scope_mismatch_rate": misselection_rate(ranked, scope_mismatch_ids, k),
                "error_type": None,
            }
        )

    return query_results, total_latencies


def run_benchmark(
    retriever: Retriever,
    dataset: RetrievalDataset,
    *,
    k: int,
    clock: Callable[[], float] = time.monotonic,
    cpu_clock: Callable[[], float] = time.process_time,
    metrics_collector: MetricsCollector | None = None,
) -> RetrievalReport:
    """Run a benchmark on a retrieval system.

    ``cpu_ms`` is the CPU time of this process while ``retrieve`` ran (it includes
    the retriever only when it runs in-process; an external service's CPU time is
    not visible here). GPU / VRAM come from ``metrics_collector``.

    The collector is always stopped, also when scoring raises, so a periodic GPU
    sampling thread is never leaked.
    """
    validate_retriever(retriever)
    if metrics_collector is not None:
        metrics_collector.start()
    try:
        query_results, total_latencies = _score_queries(
            retriever, dataset, k, clock, cpu_clock
        )
    finally:
        if metrics_collector is not None:
            metrics_collector.stop()

    # Compute overall metrics
    if not query_results:
        raise ValueError("No queries processed")

    # Calculate percentiles for latency
    def percentile(values: list[float], p: float) -> float:
        """Nearest-rank percentile: the value at rank ceil(p / 100 * n)."""
        ordered = sorted(values)
        return ordered[max(math.ceil(p / 100 * len(ordered)), 1) - 1]

    latency_p50 = percentile(total_latencies, 50)
    latency_p95 = percentile(total_latencies, 95)
    latency_mean = mean(total_latencies) if total_latencies else 0.0

    # Aggregate metrics
    metrics = {
        "recall_at_k": mean([qr["recall_at_k"] for qr in query_results]),
        "mrr": mean([qr["mrr"] for qr in query_results]),
        "ndcg_at_k": mean([qr["ndcg_at_k"] for qr in query_results]),
        "stale_rate": mean([qr["stale_rate"] for qr in query_results]),
        "superseded_rate": mean([qr["superseded_rate"] for qr in query_results]),
        "scope_mismatch_rate": mean(
            [qr["scope_mismatch_rate"] for qr in query_results]
        ),
        "permission_leakage_total": sum(
            qr["permission_leakage_count"] for qr in query_results
        ),
        "latency_ms_mean": latency_mean,
        "latency_ms_p50": latency_p50,
        "latency_ms_p95": latency_p95,
        "cpu_ms_mean": mean([qr["cpu_ms"] for qr in query_results]),
        "cpu_ms_total": sum(qr["cpu_ms"] for qr in query_results),
        "failed_queries": sum(1 for qr in query_results if qr["error_type"]),
        "k": k,
    }

    resources = metrics_collector.metrics() if metrics_collector is not None else None

    return RetrievalReport(
        metrics=metrics,
        queries=query_results,
        resources=resources,
    )
