"""Memory worker benchmark runner."""

from __future__ import annotations

import inspect
import json
import math
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from functools import lru_cache
from numbers import Real
from pathlib import Path
from typing import Protocol

from jsonschema import Draft202012Validator

from benchmarks.candidate_adapter import CandidateErrorCode
from benchmarks.json_input import decode_json
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


def _reject_unknown_fields(item: dict, allowed: frozenset[str], where: str) -> None:
    """Reject misspelled or unsupported fields instead of silently ignoring them.

    A typo such as ``supercedes`` would otherwise default to "no relation" and skew
    a score without any warning.
    """
    unknown = sorted(str(name) for name in item if name not in allowed)
    if unknown:
        raise ValueError(f"{where} has unknown field(s): {', '.join(unknown)}")


_TOP_FIELDS = frozenset({"cases"})
_CASE_FIELDS = frozenset({"id", "input", "gold"})
_GOLD_FIELDS = frozenset(
    {"key", "scope", "state", "supersedes", "content", "conflicts_with"}
)


def _parse_gold_record(record: object, case_id: str, index: int) -> MemoryRecord:
    where = f"gold record at index {index} in case '{case_id}'"
    record = _require_object(record, where)
    _reject_unknown_fields(record, _GOLD_FIELDS, f"Invalid {where}:")
    for name in ("key", "scope", "state"):
        if name not in record:
            raise ValueError(f"Invalid {where}: missing '{name}'")
    # An absent label (None) is not an empty one: it is not scored on conflicts.
    conflicts_with = None
    if "conflicts_with" in record:
        conflicts = record["conflicts_with"]
        if not isinstance(conflicts, list):
            raise ValueError(f"Invalid {where}: 'conflicts_with' must be a list")
        conflicts_with = tuple(conflicts)
    try:
        return MemoryRecord(
            key=record["key"],
            scope=record["scope"],
            state=record["state"],
            supersedes=record.get("supersedes"),
            content=record.get("content"),
            conflicts_with=conflicts_with,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid {where}: {error}") from None


def load_cases(path: str) -> list[MemoryWorkerCase]:
    """Load memory worker test cases from a JSON file.

    Every malformed input raises ``ValueError`` naming only the case id and field.
    """
    with open(path, encoding="utf-8") as f:
        data = decode_json(f.read())

    data = _require_object(data, "JSON document")
    _reject_unknown_fields(data, _TOP_FIELDS, "JSON document")
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
        _reject_unknown_fields(case_data, _CASE_FIELDS, f"case at index {index}")
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
        parsed = decode_json(raw)
    except ValueError:  # malformed, duplicate keys, or non-standard constants
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
                conflicts_with=tuple(item["conflicts_with"])
                if "conflicts_with" in item
                else None,
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
    timeout_seconds: float | None = None

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
        if self.timeout_seconds is not None:
            result["timeout_seconds"] = self.timeout_seconds
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


# Every case gets the same deadline so candidates are compared under one timeout
# (docs/BENCHMARK_EVALUATOR.md "Fair comparison rules"). The harness deliberately
# has no default: the fixed requirements call for a common timeout but do not choose
# its value, and a default that is too short would record a slow but correct
# candidate as a failure and could change model selection. The operator decides.

# The public failure code the Candidate adapter interface uses for a missed
# deadline; it is recorded as the case's ``error_type``.
DEADLINE_EXCEEDED = CandidateErrorCode.DEADLINE_EXCEEDED.value


def validate_timeout_seconds(value: object) -> float:
    """Return ``value`` as a float, or raise ``ValueError`` unless it is a positive, finite number.

    Same rule as ``WorktreeRunner.execute`` and ``AttemptControl``: a bool, NaN,
    infinity or zero would otherwise mean "no deadline" or an instant failure.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("timeout_seconds must be a finite number above zero")
    return float(value)


class WorkerStuckError(RuntimeError):
    """A timed-out ``extract`` call had still not ended one more deadline later.

    Python cannot stop a running thread, so the runner can neither cancel the call
    nor start the next case without two calls overlapping on the same worker
    (shared state, GPU / VRAM use that distorts the measurements, wrong
    attribution of a late result). It stops the run instead. The message carries
    only the case id and the number of seconds, never worker output.
    """


class _ExtractCall:
    """One ``worker.extract`` call in a daemon thread with a deadline.

    Python cannot interrupt a thread, so a call that misses its deadline is not
    cancelled: it keeps running until it returns on its own, and its output is
    never read. The runner therefore waits for it to end (``wait_until_ended``)
    before it does anything else with the worker. The thread is a daemon so that a
    call that never ends cannot keep the interpreter from exiting, and it catches
    everything itself so no traceback (which can carry exception text) reaches
    stderr.
    """

    def __init__(self, worker: MemoryWorker, input_text: str) -> None:
        self._worker = worker
        self._input_text = input_text
        self._finished = threading.Event()
        self._output: str = ""
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, name="memory-worker-extract", daemon=True
        )

    def _run(self) -> None:
        try:
            self._output = self._worker.extract(self._input_text)
        except BaseException as error:  # noqa: BLE001 - handed to the runner's thread, which decides.
            self._error = error
        finally:
            self._finished.set()

    def result(self, timeout_seconds: float) -> tuple[str, str | None]:
        """Return ``(raw_output, error_type)``; ``error_type`` is None on success."""
        self._thread.start()
        if not self._finished.wait(timeout_seconds):
            return "", DEADLINE_EXCEEDED
        error = self._error
        if error is None:
            return self._output, None
        if not isinstance(error, Exception):
            raise error  # SystemExit and the like are not "a failed case".
        return "", type(error).__name__

    def wait_until_ended(self, timeout_seconds: float) -> bool:
        """Wait up to ``timeout_seconds`` for the call's thread to end; return whether it did."""
        self._thread.join(timeout_seconds)
        return not self._thread.is_alive()


def run_benchmark(
    worker: MemoryWorker,
    cases: Sequence[MemoryWorkerCase],
    *,
    timeout_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    metrics_collector: MetricsCollector | None = None,
) -> BenchmarkReport:
    """Run the Memory Worker benchmark over ``cases``.

    ``timeout_seconds`` is required and has no default: the deadline is the
    operator's decision, and it must be the same for every candidate compared.
    Omitting it is a ``TypeError``; a value that is not a finite number above zero
    is a ``ValueError``.

    Latency covers only ``worker.extract``. A worker that raises is recorded as a
    failed case with empty predictions and the run continues. Each call runs in a
    helper thread with a deadline of ``timeout_seconds``: a call that has not
    returned by then is recorded as a failed case with ``error_type``
    ``"deadline_exceeded"`` (its latency is the time waited until the deadline).

    The timed-out call cannot be stopped, and two calls must never overlap on one
    worker, so before the next case (or the report) the runner waits up to another
    ``timeout_seconds`` for that call to end; the wait is not part of any case's
    latency. If it still has not ended, the run is stopped with
    ``WorkerStuckError`` and no report is built, since a partial one would change
    what every metric is averaged over. A stalled worker therefore costs at most
    ``2 * timeout_seconds`` per case, and a run that returns has never had two
    ``extract`` calls running at once.
    """
    timeout_seconds = validate_timeout_seconds(timeout_seconds)
    validate_worker(worker)
    results: list[BenchmarkResult] = []
    raw_outputs: list[str] = []

    if metrics_collector is not None:
        metrics_collector.start()
    try:
        for case in cases:
            call = _ExtractCall(worker, case.input_text)
            started = clock()
            raw_output, error_type = call.result(timeout_seconds)
            latency_ms = (clock() - started) * 1000
            if error_type == DEADLINE_EXCEEDED and not call.wait_until_ended(
                timeout_seconds
            ):
                raise WorkerStuckError(
                    f"extract() had not ended {timeout_seconds:g} seconds after its "
                    f"deadline in case '{case.id}'; the run was stopped instead of "
                    "starting the next case while it is still running"
                )

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
        timeout_seconds=timeout_seconds,
    )
