"""Dataset loader and benchmark runner for retrieval (embedding / reranker) benchmarks."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

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


def load_dataset(path: str) -> RetrievalDataset:
    """Load a retrieval dataset from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Validate memories
    memories_data = data.get("memories", [])
    if not memories_data:
        raise ValueError("Dataset must contain at least one memory")

    memory_map = {}
    for memory_data in memories_data:
        memory_id = memory_data["id"]
        if not memory_id:
            raise ValueError("Memory ID must be non-empty")
        if memory_id in memory_map:
            raise ValueError(f"Duplicate memory ID: {memory_id}")
        if memory_data["status"] not in {"active", "superseded", "deprecated"}:
            raise ValueError(
                f"Invalid status for memory {memory_id}: {memory_data['status']}"
            )
        memory = RetrievalMemory(
            id=memory_data["id"],
            text=memory_data["text"],
            acl=frozenset(memory_data["acl"]),
            status=memory_data["status"],
            fresh=bool(memory_data["fresh"]),
            scope=memory_data["scope"],
        )
        memory_map[memory_id] = memory

    # Validate queries
    queries_data = data.get("queries", [])
    if not queries_data:
        raise ValueError("Dataset must contain at least one query")

    queries = []
    for query_data in queries_data:
        query_id = query_data["id"]
        if not query_id:
            raise ValueError("Query ID must be non-empty")
        if query_id in (q.id for q in queries):
            raise ValueError(f"Duplicate query ID: {query_id}")
        if not query_data["relevant_ids"]:
            raise ValueError(f"Query {query_id} must have at least one relevant ID")
        relevant_ids = frozenset(query_data["relevant_ids"])
        # Check that all relevant IDs exist
        for relevant_id in relevant_ids:
            if relevant_id not in memory_map:
                raise ValueError(
                    f"Query {query_id} references non-existent memory {relevant_id}"
                )
            # Check visibility
            memory = memory_map[relevant_id]
            if not (memory.acl & frozenset(query_data["requester_principals"])):
                raise ValueError(
                    f"Query {query_id} requests memory {relevant_id} that is not visible "
                    f"to requester principals {query_data['requester_principals']}"
                )
        query = RetrievalQuery(
            id=query_data["id"],
            text=query_data["text"],
            requester_principals=frozenset(query_data["requester_principals"]),
            scope=query_data["scope"],
            relevant_ids=relevant_ids,
        )
        queries.append(query)

    return RetrievalDataset(
        memories=tuple(memory_map.values()),
        queries=tuple(queries),
    )


class Retriever(Protocol):
    """Protocol for a retrieval system to benchmark."""

    def retrieve(
        self, query_text: str, requester_principals: Sequence[str], k: int
    ) -> Sequence[str]:
        """Retrieve top-k memory IDs for a query."""


def run_benchmark(
    retriever: Retriever,
    dataset: RetrievalDataset,
    *,
    k: int,
    clock: Callable[[], float] = time.monotonic,
    metrics_collector: MetricsCollector | None = None,
) -> RetrievalReport:
    """Run a benchmark on a retrieval system."""
    if metrics_collector is not None:
        metrics_collector.start()

    query_results = []
    total_latencies = []

    for query in dataset.queries:
        visible_ids = visible_memory_ids(dataset, query)
        started = clock()
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
        total_latencies.append(latency_ms)

        ranked = list(dict.fromkeys(retrieved))
        stale_ids = {m.id for m in dataset.memories if not m.fresh}
        superseded_ids = {m.id for m in dataset.memories if m.status != "active"}
        scope_mismatch_ids = {m.id for m in dataset.memories if m.scope != query.scope}
        query_results.append(
            {
                "id": query.id,
                "latency_ms": latency_ms,
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
        "k": k,
    }

    if metrics_collector is not None:
        metrics_collector.stop()
        resources = metrics_collector.metrics()
    else:
        resources = None

    return RetrievalReport(
        metrics=metrics,
        queries=query_results,
        resources=resources,
    )
