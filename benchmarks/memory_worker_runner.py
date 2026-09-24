"""Memory worker benchmark runner."""

from __future__ import annotations

import inspect
import json
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from jsonschema import Draft202012Validator

from benchmarks.memory_worker_metrics import (
    MemoryComparison,
    MemoryRecord,
    aggregate,
    compare_memories,
    schema_adherence,
)
from benchmarks.metrics_collector import MetricsCollector


@dataclass(frozen=True)
class MemoryWorkerCase:
    """A test case for the memory worker benchmark."""

    id: str
    input_text: str
    gold: tuple[MemoryRecord, ...]


def _require_object(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")  # noqa: TRY004 - dataset problems are reported uniformly as ValueError.
    return value


def _parse_gold_record(record: object, case_id: str, index: int) -> MemoryRecord:
    where = f"gold record at index {index} in case '{case_id}'"
    record = _require_object(record, where)
    for name in ("key", "scope", "state"):
        if name not in record:
            raise ValueError(f"Invalid {where}: missing '{name}'")
    conflicts = record.get("conflicts_with", [])
    if not isinstance(conflicts, list):
        raise ValueError(f"Invalid {where}: 'conflicts_with' must be a list")  # noqa: TRY004 - dataset problems are reported uniformly as ValueError.
    try:
        return MemoryRecord(
            key=record["key"],
            scope=record["scope"],
            state=record["state"],
            supersedes=record.get("supersedes"),
            content=record.get("content"),
            conflicts_with=tuple(conflicts),
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid {where}: {error}") from None


def load_cases(path: str) -> list[MemoryWorkerCase]:
    """Load memory worker test cases from a JSON file.

    Every malformed input raises ``ValueError`` naming only the case id and field.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    data = _require_object(data, "JSON document")
    if "cases" not in data:
        raise ValueError("JSON must have a 'cases' key")
    cases = data["cases"]
    if not isinstance(cases, list):
        raise ValueError("'cases' must be a list")  # noqa: TRY004 - dataset problems are reported uniformly as ValueError.
    if not cases:
        raise ValueError("Cases list cannot be empty")

    result = []
    seen_ids: set[str] = set()
    for index, case_data in enumerate(cases):
        case_data = _require_object(case_data, f"case at index {index}")
        if "id" not in case_data:
            raise ValueError("Each case must have an 'id' field")
        case_id = case_data["id"]
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"case at index {index} has an invalid 'id'")
        if case_id in seen_ids:
            raise ValueError("Duplicate case ID found")
        seen_ids.add(case_id)
        if "input" not in case_data:
            raise ValueError("Each case must have an 'input' field")
        if "gold" not in case_data:
            raise ValueError("Each case must have a 'gold' key")
        if not isinstance(case_data["input"], str) or not case_data["input"]:
            raise ValueError("Input text cannot be empty")
        if not isinstance(case_data["gold"], list):
            raise ValueError(f"'gold' must be a list in case '{case_id}'")  # noqa: TRY004 - dataset problems are reported uniformly as ValueError.

        gold_records = [
            _parse_gold_record(record, case_id, i)
            for i, record in enumerate(case_data["gold"])
        ]
        gold_keys = [record.key for record in gold_records]
        if len(gold_keys) != len(set(gold_keys)):
            raise ValueError(f"Duplicate gold key found in case '{case_id}'")

        result.append(
            MemoryWorkerCase(
                id=case_id, input_text=case_data["input"], gold=tuple(gold_records)
            )
        )

    return result


class MemoryWorker(Protocol):
    """Protocol for memory worker that extracts text from input."""

    def extract(self, input_text: str) -> str:
        """Extract memory records from input text."""
        ...


_SCHEMA_PATH = Path(__file__).parent / "schemas" / "memory-worker-output-v1.schema.json"


@lru_cache(maxsize=1)
def _output_validator() -> Draft202012Validator:
    with _SCHEMA_PATH.open(encoding="utf-8") as schema_file:
        return Draft202012Validator(json.load(schema_file))


def _output_schema() -> dict[str, object]:
    return dict(_output_validator().schema)


def parse_worker_output(raw: str) -> list[MemoryRecord] | None:
    """Parse raw worker output; return ``None`` unless it is valid JSON matching the schema."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not _output_validator().is_valid(parsed):
        return None

    # Extract memory records
    try:
        records = []
        for item in parsed["memories"]:
            record = MemoryRecord(
                key=item["key"],
                scope=item["scope"],
                state=item["state"],
                supersedes=item["supersedes"],
                content=item.get("content"),
                conflicts_with=tuple(item.get("conflicts_with", ())),
            )
            records.append(record)
        return records
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class BenchmarkResult:
    """Result for a single benchmark case."""

    id: str
    latency_ms: float
    schema_valid: bool
    comparison: MemoryComparison
    error_type: str | None = None


@dataclass(frozen=True)
class BenchmarkReport:
    """Complete benchmark report."""

    cases: tuple[BenchmarkResult, ...]
    metrics: dict[str, float | None]
    resources: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        """Convert report to a JSON-serializable dictionary."""
        result = {
            "cases": [
                {
                    "id": case.id,
                    "latency_ms": case.latency_ms,
                    "schema_valid": case.schema_valid,
                    "error_type": case.error_type,
                    "comparison": asdict(case.comparison),
                }
                for case in self.cases
            ],
            "metrics": self.metrics,
        }
        if self.resources is not None:
            result["resources"] = self.resources
        return result


def _percentile(sorted_values: list[float], percent: float) -> float:
    """Nearest-rank percentile: the value at rank ceil(percent / 100 * n)."""
    return sorted_values[max(math.ceil(percent / 100 * len(sorted_values)), 1) - 1]


def validate_worker(worker: object) -> None:
    """Raise ``TypeError`` unless ``worker.extract`` can take the benchmark's call.

    Without this check a missing or mis-declared method would be swallowed as a
    failure of every case and produce an all-zero report.
    """
    method = getattr(worker, "extract", None)
    if not callable(method):
        raise TypeError("worker must provide a callable extract()")
    try:
        inspect.signature(method).bind("input text")
    except TypeError:
        raise TypeError("extract() must accept (input_text)") from None
    except ValueError:
        raise TypeError(
            "extract() has no inspectable signature; wrap it in a plain Python method"
        ) from None


def run_benchmark(
    worker: MemoryWorker,
    cases: Sequence[MemoryWorkerCase],
    *,
    clock: Callable[[], float] = time.monotonic,
    metrics_collector: MetricsCollector | None = None,
) -> BenchmarkReport:
    """Run the Memory Worker benchmark over ``cases``.

    Latency covers only ``worker.extract``. A worker that raises is recorded as a
    failed case with empty predictions and the run continues.
    """
    validate_worker(worker)
    results: list[BenchmarkResult] = []
    raw_outputs: list[str] = []

    if metrics_collector is not None:
        metrics_collector.start()
    try:
        for case in cases:
            error_type = None
            started = clock()
            try:
                raw_output = worker.extract(case.input_text)
            except Exception as error:  # noqa: BLE001 - one failing worker call must not end the run.
                raw_output, error_type = "", type(error).__name__
            latency_ms = (clock() - started) * 1000

            raw_outputs.append(raw_output)
            predicted = parse_worker_output(raw_output)
            results.append(
                BenchmarkResult(
                    id=case.id,
                    latency_ms=latency_ms,
                    schema_valid=predicted is not None,
                    comparison=compare_memories(case.gold, predicted or []),
                    error_type=error_type,
                )
            )
    finally:
        if metrics_collector is not None:
            metrics_collector.stop()

    metrics = aggregate([result.comparison for result in results])
    metrics["schema_adherence_rate"] = schema_adherence(
        raw_outputs, _output_schema()
    ).rate
    latencies = sorted(result.latency_ms for result in results)
    metrics["latency_ms_mean"] = sum(latencies) / len(latencies)
    metrics["latency_ms_p50"] = _percentile(latencies, 50)
    metrics["latency_ms_p95"] = _percentile(latencies, 95)

    return BenchmarkReport(
        cases=tuple(results),
        metrics=metrics,
        resources=metrics_collector.metrics()
        if metrics_collector is not None
        else None,
    )
