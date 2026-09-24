"""Memory worker comparison and aggregation metrics."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from jsonschema import Draft202012Validator

from benchmarks.json_input import decode_json


def normalize_content(text: str) -> str:
    """Normalize memory content for comparison (NFKC, case-folded, single spaces)."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


@dataclass(frozen=True)
class MemoryRecord:
    """A memory record.

    ``key`` identifies the memory (matching is by key). ``content`` is the extracted
    fact; a gold record without content is not scored on content. ``conflicts_with``
    lists the keys of memories this one conflicts with.
    """

    key: str
    scope: str
    state: str
    supersedes: str | None
    content: str | None = None
    conflicts_with: tuple[str, ...] = ()

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
        if self.content is not None and (
            not isinstance(self.content, str) or not self.content.strip()
        ):
            raise TypeError("content must be a non-empty string or None")
        if not isinstance(self.conflicts_with, tuple) or not all(
            isinstance(key, str) and key for key in self.conflicts_with
        ):
            raise TypeError("conflicts_with must be a tuple of non-empty strings")


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
    # Counts below stay 0 when the gold data carries no content / conflict labels.
    content_evaluated: int = 0
    content_correct: int = 0
    conflicts_evaluated: int = 0
    conflicts_correct: int = 0


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
    content_evaluated = content_correct = conflicts_evaluated = conflicts_correct = 0
    for key, predicted_record in first_predicted.items():
        gold_record = gold_by_key[key]
        scope_correct += gold_record.scope == predicted_record.scope
        state_correct += gold_record.state == predicted_record.state
        supersedes_correct += gold_record.supersedes == predicted_record.supersedes
        if gold_record.content is not None:
            content_evaluated += 1
            content_correct += predicted_record.content is not None and (
                normalize_content(gold_record.content)
                == normalize_content(predicted_record.content)
            )
        if gold_record.conflicts_with or predicted_record.conflicts_with:
            conflicts_evaluated += 1
            conflicts_correct += set(gold_record.conflicts_with) == set(
                predicted_record.conflicts_with
            )

    return MemoryComparison(
        gold_count=len(gold),
        predicted_count=len(predicted),
        matched=matched,
        unneeded=len(predicted) - matched,
        scope_correct=scope_correct,
        state_correct=state_correct,
        supersedes_correct=supersedes_correct,
        content_evaluated=content_evaluated,
        content_correct=content_correct,
        conflicts_evaluated=conflicts_evaluated,
        conflicts_correct=conflicts_correct,
    )


def aggregate(comparisons: Sequence[MemoryComparison]) -> dict[str, float | None]:
    """Aggregate memory comparison results across multiple test cases (micro-average).

    ``extraction_recall`` is key-level: it does not check that the extracted fact is
    right. ``exact_recall`` also requires matching content where the gold record has
    content; ``content_accuracy`` is the share of key-matched, content-labelled gold
    memories whose content matched; ``conflict_accuracy`` is scored over key-matched
    pairs where either side declares a conflict.
    """
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
    total_content_evaluated = sum(c.content_evaluated for c in comparisons)
    total_content_correct = sum(c.content_correct for c in comparisons)
    total_conflicts_evaluated = sum(c.conflicts_evaluated for c in comparisons)
    total_conflicts_correct = sum(c.conflicts_correct for c in comparisons)
    # A gold memory counts as exactly recalled when its key matched AND, if the
    # gold record carries content, the content matched too.
    total_exact = total_matched - (total_content_evaluated - total_content_correct)

    # Calculate ratios
    def safe_divide(numerator: int, denominator: int) -> float | None:
        return numerator / denominator if denominator != 0 else None

    return {
        "extraction_recall": safe_divide(total_matched, total_gold_count),
        "unneeded_rate": safe_divide(total_unneeded, total_predicted_count),
        "scope_accuracy": safe_divide(total_scope_correct, total_matched),
        "state_accuracy": safe_divide(total_state_correct, total_matched),
        "supersedes_accuracy": safe_divide(total_supersedes_correct, total_matched),
        "exact_recall": safe_divide(total_exact, total_gold_count),
        "content_accuracy": safe_divide(total_content_correct, total_content_evaluated),
        "conflict_accuracy": safe_divide(
            total_conflicts_correct, total_conflicts_evaluated
        ),
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
            parsed = decode_json(output)
        except ValueError:  # malformed, duplicate keys, or non-standard constants
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
