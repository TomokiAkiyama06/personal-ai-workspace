"""Bounded JSON values (PAW-034): node inputs and node results.

The orchestrator stores what a planner and an agent runtime produce in JSONB
columns and hands it to other agents, so it must be plain JSON data of a bounded
size. This is the one checker: it returns a **deep copy** built while it walks
(the caller's containers are detached), and it stops as soon as a bound is
crossed, so a hostile value that shares one large list many times cannot make it
do unbounded work.

Accepted: ``dict`` (``str`` keys), ``list``, ``tuple`` (as a list), ``str``,
``int``, finite ``float``, ``bool`` and ``None``. Refused: anything else,
``NaN`` / infinities, integers beyond 64 bits, a NUL or a surrogate code point in
a text or a key (PostgreSQL's JSONB cannot hold a NUL and UTF-8 cannot encode a
surrogate), nesting deeper than ``max_depth`` and an encoded size above
``max_bytes``.
"""

import json
import re
from typing import Any

_SURROGATE = re.compile("[\ud800-\udfff]")
_MAX_INT = 2**63 - 1
_MAX_KEY_CHARS = 200


class JsonProblem(Exception):
    """A value is not acceptable. ``kind`` is one of ``type``, ``text``,
    ``number``, ``depth`` or ``size`` (a constant, never part of the value)."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


class _Walker:
    def __init__(self, max_bytes: int, max_depth: int) -> None:
        self._remaining = max_bytes
        self._max_depth = max_depth

    def _spend(self, cost: int) -> None:
        # Every character of the walked value is at least one byte of its
        # encoding, so exceeding the budget proves the encoding is too large.
        self._remaining -= cost
        if self._remaining < 0:
            raise JsonProblem("size")

    def _text(self, value: str, *, key: bool = False) -> str:
        if len(value) > (_MAX_KEY_CHARS if key else self._remaining):
            raise JsonProblem("size" if not key else "text")
        if "\x00" in value or _SURROGATE.search(value) is not None:
            raise JsonProblem("text")
        self._spend(len(value) + 2)
        return value

    def walk(self, value: Any, depth: int) -> Any:
        if value is None:
            self._spend(4)
            return None
        if isinstance(value, bool):
            self._spend(5)
            return value
        if isinstance(value, int):
            if type(value) is not int or not -_MAX_INT <= value <= _MAX_INT:
                raise JsonProblem("number")
            self._spend(len(str(value)))
            return value
        if isinstance(value, float):
            if (
                type(value) is not float
                or value != value
                or value
                in (
                    float("inf"),
                    float("-inf"),
                )
            ):
                raise JsonProblem("number")
            self._spend(len(repr(value)))
            return value
        if isinstance(value, str):
            if type(value) is not str:
                raise JsonProblem("type")
            return self._text(value)
        if isinstance(value, dict | list | tuple):
            if type(value) not in (dict, list, tuple):
                raise JsonProblem("type")
            if depth >= self._max_depth:
                raise JsonProblem("depth")
            self._spend(2)
            if isinstance(value, dict):
                copy: dict[str, Any] = {}
                for key, item in value.items():
                    if type(key) is not str:
                        raise JsonProblem("type")
                    self._text(key, key=True)
                    self._spend(1)
                    copy[key] = self.walk(item, depth + 1)
                return copy
            items = []
            for item in value:
                self._spend(1)
                items.append(self.walk(item, depth + 1))
            return items
        raise JsonProblem("type")


def check_json_object(value: object, *, max_bytes: int, max_depth: int) -> dict:
    """``value`` as a detached JSON object, or :class:`JsonProblem`.

    The top level must be a ``dict``. ``max_depth`` counts the levels of
    containers, the top-level object being the first.
    """
    if type(value) is not dict:
        raise JsonProblem("type")
    clean = _Walker(max_bytes, max_depth).walk(value, 0)
    encoded = json.dumps(
        clean, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        raise JsonProblem("size")
    return clean


def encoded_size(value: object) -> int:
    """The size in bytes of the compact UTF-8 JSON of an already checked value."""
    return len(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    )
