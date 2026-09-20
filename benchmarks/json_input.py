"""Decode benchmark JSON while rejecting ambiguous JSON syntax."""

from __future__ import annotations

import json
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


def decode_json(content: str) -> Any:
    """Decode standard JSON without silently accepting duplicate object keys."""
    return json.loads(
        content,
        object_pairs_hook=_reject_duplicate_object_keys,
        parse_constant=_reject_nonstandard_constant,
    )
