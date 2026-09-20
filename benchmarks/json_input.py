"""Decode benchmark JSON while rejecting ambiguous JSON syntax."""

from __future__ import annotations

import json
import math
from typing import Any


def _reject_nonstandard_constant(_constant: str) -> None:
    raise ValueError("non-standard numeric constant")


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON object key")
        document[key] = value
    return document


def _reject_nonfinite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def decode_json(content: str) -> Any:
    """Decode standard JSON without silently accepting duplicate object keys."""
    return json.loads(
        content,
        object_pairs_hook=_reject_duplicate_object_keys,
        parse_constant=_reject_nonstandard_constant,
        parse_float=_reject_nonfinite_float,
    )
