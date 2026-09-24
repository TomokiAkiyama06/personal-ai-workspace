"""Memory worker comparison and aggregation metrics."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from jsonschema import Draft202012Validator


@dataclass(frozen=True)
class MemoryRecord:
    """A memory record with key, scope, state, and supersedes information."""

    key: str
    scope: str
    state: str
    supersedes: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise TypeError("key must be a non-empty string")
        if not isinstance(self.scope, str) or not self.scope:
            raise TypeError("scope must be a non-empty string")
        if not isinstance(self.state, str) or not self.state:
            raise TypeError("state must be a non-empty string")
        if self.state not in {"confirmed", "inferred"}:
            raise ValueError("state must be 'confirmed' or 'inferred'")
        if self.supersedes is not None and not isinstance(self.supersedes, str):
            raise TypeError("supersedes must be a string or None")


@dataclass(frozen=True)
class MemoryComparison:
    """Comparison results between gold and predicted memory records."""

    gold_count: int
    predicted_count: int
    matched: int
    unneeded: int
    scope_correct: int
    state_correct: int
    supersedes_correct: int


def compare_memories(
    gold: Sequence[MemoryRecord], predicted: Sequence[MemoryRecord]
) -> MemoryComparison:
    """Compare gold and predicted memory records for one test case.

    A predicted record is needed only when it is the first occurrence of a gold
    key; every other predicted record (unknown key or repeated key) is unneeded.
    """
    gold_by_key = {record.key: record for record in gold}
    if len(gold_by_key) != len(gold):
        raise ValueError("Gold keys must be unique")

    first_predicted: dict[str, MemoryRecord] = {}
    for record in predicted:
        if record.key in gold_by_key:
            first_predicted.setdefault(record.key, record)

    matched = len(first_predicted)
    scope_correct = state_correct = supersedes_correct = 0
    for key, predicted_record in first_predicted.items():
        gold_record = gold_by_key[key]
        scope_correct += gold_record.scope == predicted_record.scope
        state_correct += gold_record.state == predicted_record.state
        supersedes_correct += gold_record.supersedes == predicted_record.supersedes

    return MemoryComparison(
        gold_count=len(gold),
        predicted_count=len(predicted),
        matched=matched,
        unneeded=len(predicted) - matched,
        scope_correct=scope_correct,
        state_correct=state_correct,
        supersedes_correct=supersedes_correct,
    )


def aggregate(comparisons: Sequence[MemoryComparison]) -> dict[str, float | None]:
    """Aggregate memory comparison results across multiple test cases."""
    if not comparisons:
        raise ValueError("Cannot aggregate empty sequence of comparisons")

    # Sum all counts
    total_gold_count = sum(c.gold_count for c in comparisons)
    total_predicted_count = sum(c.predicted_count for c in comparisons)
    total_matched = sum(c.matched for c in comparisons)
    total_unneeded = sum(c.unneeded for c in comparisons)
    total_scope_correct = sum(c.scope_correct for c in comparisons)
    total_state_correct = sum(c.state_correct for c in comparisons)
    total_supersedes_correct = sum(c.supersedes_correct for c in comparisons)

    # Calculate ratios
    def safe_divide(numerator: int, denominator: int) -> float | None:
        return numerator / denominator if denominator != 0 else None

    return {
        "extraction_recall": safe_divide(total_matched, total_gold_count),
        "unneeded_rate": safe_divide(total_unneeded, total_predicted_count),
        "scope_accuracy": safe_divide(total_scope_correct, total_matched),
        "state_accuracy": safe_divide(total_state_correct, total_matched),
        "supersedes_accuracy": safe_divide(total_supersedes_correct, total_matched),
    }


def schema_adherence(
    raw_outputs: Sequence[str], schema: Mapping[str, object]
) -> SchemaAdherence:
    """Check how many raw outputs conform to the given JSON schema."""
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    valid = 0
    for output in raw_outputs:
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            continue
        if validator.is_valid(parsed):
            valid += 1

    return SchemaAdherence(valid=valid, total=len(raw_outputs))


@dataclass(frozen=True)
class SchemaAdherence:
    """Result of schema adherence checking."""

    valid: int
    total: int

    @property
    def rate(self) -> float | None:
        """Return the rate of valid outputs, or None if no outputs."""
        return self.valid / self.total if self.total != 0 else None
