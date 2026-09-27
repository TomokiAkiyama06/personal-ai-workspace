"""Argument validation of Hybrid Retrieval (pure functions, no I/O).

Every value that comes from a caller passes through one of these functions
before anything else happens. All of them raise
:class:`~paw_backend.memory.retrieval.errors.InvalidRetrievalInputError` with the
argument's ``field`` name and an ``InputProblem``; the message never contains the
rejected value. Nothing is coerced: a ``str`` is not a UUID, ``bool`` is not an
``int``, ``5.0`` is not an integer, and an enum accepts its member or its exact
serialised string (which is normalised to the member). Caller collections are
copied into immutable ones only after every element was checked.
"""

import math
import re
from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from paw_backend.memory.retrieval import limits
from paw_backend.memory.retrieval.errors import InvalidRetrievalInputError
from paw_backend.memory.shared.errors import InputProblem

_COMMIT_SHA = re.compile(r"[0-9a-f]+")


def reject(field: str, problem: InputProblem) -> InvalidRetrievalInputError:
    return InvalidRetrievalInputError(field, problem)


def validate_uuid(field: str, value: object) -> UUID:
    """``value`` if it is a :class:`uuid.UUID`; a string form is ``WRONG_TYPE``."""
    if value is None:
        raise reject(field, InputProblem.REQUIRED)
    if not isinstance(value, UUID):
        raise reject(field, InputProblem.WRONG_TYPE)
    return value


def validate_enum[E: Enum](field: str, value: object, kind: type[E]) -> E:
    """The member of ``kind`` for ``value`` (the member, or its exact value).

    ``"project"`` is accepted for ``MemoryScope.PROJECT``; ``"Project"``,
    ``" project"`` and ``"PROJECT"`` are not. Anything that is not a member and
    not a ``str`` is ``WRONG_TYPE``; an unknown string is ``INVALID_FORMAT``.
    """
    if value is None:
        raise reject(field, InputProblem.REQUIRED)
    if isinstance(value, kind):
        return value
    if isinstance(value, Enum) or not isinstance(value, str):
        raise reject(field, InputProblem.WRONG_TYPE)
    for member in kind:
        if member.value == value:
            return member
    raise reject(field, InputProblem.INVALID_FORMAT)


def validate_int(field: str, value: object, *, low: int, high: int) -> int:
    """An ``int`` in ``low..high`` (both included). ``bool`` is ``WRONG_TYPE``."""
    if value is None:
        raise reject(field, InputProblem.REQUIRED)
    if isinstance(value, bool) or not isinstance(value, int):
        raise reject(field, InputProblem.WRONG_TYPE)
    if not low <= value <= high:
        raise reject(field, InputProblem.OUT_OF_RANGE)
    return value


def validate_number(
    field: str,
    value: object,
    *,
    low: float,
    high: float,
    low_open: bool = False,
) -> float:
    """A finite ``int`` or ``float`` in ``low..high`` (``bool`` is ``WRONG_TYPE``)."""
    if value is None:
        raise reject(field, InputProblem.REQUIRED)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise reject(field, InputProblem.WRONG_TYPE)
    number = float(value)
    if not math.isfinite(number) or number > high:
        raise reject(field, InputProblem.OUT_OF_RANGE)
    if number < low or (low_open and number == low):
        raise reject(field, InputProblem.OUT_OF_RANGE)
    return number


def validate_text(field: str, value: object, *, max_chars: int) -> str:
    """Free text, returned unchanged (never stripped, folded or truncated).

    Checks, first failure wins: ``None`` is ``REQUIRED``; not a ``str`` is
    ``WRONG_TYPE``; more than ``max_chars`` characters (exactly ``max_chars`` is
    accepted) is ``TOO_LONG``; a NUL or a character that cannot be encoded as
    UTF-8 (a lone surrogate) is ``INVALID_CHARACTERS``; empty or whitespace only
    is ``BLANK``.
    """
    if value is None:
        raise reject(field, InputProblem.REQUIRED)
    if not isinstance(value, str):
        raise reject(field, InputProblem.WRONG_TYPE)
    if len(value) > max_chars:
        raise reject(field, InputProblem.TOO_LONG)
    if "\x00" in value:
        raise reject(field, InputProblem.INVALID_CHARACTERS)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise reject(field, InputProblem.INVALID_CHARACTERS) from None
    if value.strip() == "":
        raise reject(field, InputProblem.BLANK)
    return value


def validate_collection(field: str, value: object, *, max_items: int) -> list[Any]:
    """The elements of a ``list``, ``tuple``, ``set`` or ``frozenset``, bounded.

    A ``str`` / ``bytes`` (which would be split into characters), a mapping and
    any other type are ``WRONG_TYPE``. More than ``max_items`` elements is
    ``TOO_MANY``, decided from the length before any element is looked at.
    """
    if value is None:
        raise reject(field, InputProblem.REQUIRED)
    if not isinstance(value, list | tuple | set | frozenset):
        raise reject(field, InputProblem.WRONG_TYPE)
    if len(value) > max_items:
        raise reject(field, InputProblem.TOO_MANY)
    return list(value)


def validate_uuid_set(field: str, value: object, *, max_items: int) -> frozenset[UUID]:
    """A set of UUIDs; every element is checked before duplicates are removed."""
    items = validate_collection(field, value, max_items=max_items)
    return frozenset(validate_uuid(field, item) for item in items)


def validate_enum_set[E: Enum](
    field: str, value: object, kind: type[E], *, max_items: int
) -> frozenset[E]:
    items = validate_collection(field, value, max_items=max_items)
    return frozenset(validate_enum(field, item, kind) for item in items)


def validate_commit_heads(field: str, value: object) -> Mapping[UUID, str]:
    """``{repo_id: commit sha}``: lower-case hex of 40 or 64 characters, bounded."""
    if value is None:
        raise reject(field, InputProblem.REQUIRED)
    if not isinstance(value, dict):
        raise reject(field, InputProblem.WRONG_TYPE)
    if len(value) > limits.MAX_REPO_HEADS:
        raise reject(field, InputProblem.TOO_MANY)
    checked: dict[UUID, str] = {}
    for repo_id, sha in value.items():
        key = validate_uuid(field, repo_id)
        if not isinstance(sha, str):
            raise reject(field, InputProblem.WRONG_TYPE)
        if len(sha) not in limits.COMMIT_SHA_LENGTHS or not _COMMIT_SHA.fullmatch(sha):
            raise reject(field, InputProblem.INVALID_FORMAT)
        checked[key] = sha
    return checked


def validate_aware_datetime(field: str, value: object) -> datetime:
    if not isinstance(value, datetime):
        raise reject(field, InputProblem.WRONG_TYPE)
    if value.tzinfo is None or value.utcoffset() is None:
        raise reject(field, InputProblem.NAIVE_DATETIME)
    return value


def require_callable(field: str, value: object, *names: str) -> None:
    """``value`` has a callable attribute for every name in ``names``."""
    for name in names:
        if not callable(getattr(value, name, None)):
            raise reject(field, InputProblem.WRONG_TYPE)


def require_clock(field: str, value: object) -> Callable[[], datetime]:
    if not callable(value):
        raise reject(field, InputProblem.WRONG_TYPE)
    return value  # type: ignore[return-value]


def validate_members(field: str, value: Collection[Any], kind: type) -> None:
    if not all(isinstance(item, kind) for item in value):
        raise reject(field, InputProblem.WRONG_TYPE)
