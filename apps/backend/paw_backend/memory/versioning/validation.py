"""Argument validation of Memory versioning (pure functions, no I/O).

The same contract as ``memory/shared/validation.py``: every function raises
:class:`InvalidMemoryInputError` with the argument's ``field`` and an
:class:`InputProblem`, the message never contains the rejected value, and nothing
is coerced (a ``str`` is not a UUID, ``bool`` is not an ``int``, a plain string is
not an enum member).
"""

import re
from datetime import datetime, timedelta
from enum import Enum
from uuid import UUID

from paw_backend.memory.versioning import limits
from paw_backend.memory.versioning.errors import InputProblem, InvalidMemoryInputError

_MEMORY_TYPE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def reject(field: str, problem: InputProblem) -> InvalidMemoryInputError:
    return InvalidMemoryInputError(field, problem)


def validate_uuid(field: str, value: object) -> UUID:
    if value is None:
        raise reject(field, InputProblem.REQUIRED)
    if not isinstance(value, UUID):
        raise reject(field, InputProblem.WRONG_TYPE)
    return value


def validate_enum[E: Enum](field: str, value: object, kind: type[E]) -> E:
    if value is None:
        raise reject(field, InputProblem.REQUIRED)
    if not isinstance(value, kind):
        raise reject(field, InputProblem.WRONG_TYPE)
    return value


def validate_int(field: str, value: object, *, low: int, high: int) -> int:
    if value is None:
        raise reject(field, InputProblem.REQUIRED)
    if isinstance(value, bool) or not isinstance(value, int):
        raise reject(field, InputProblem.WRONG_TYPE)
    if not low <= value <= high:
        raise reject(field, InputProblem.OUT_OF_RANGE)
    return value


def validate_version_number(field: str, value: object) -> int:
    return validate_int(field, value, low=1, high=limits.MAX_VERSION_NUMBER)


def validate_text(field: str, value: object, *, max_chars: int) -> str:
    """Free text, returned unchanged (never stripped, folded or truncated)."""
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


def validate_optional_text(field: str, value: object, *, max_chars: int) -> str | None:
    return None if value is None else validate_text(field, value, max_chars=max_chars)


def validate_memory_type(field: str, value: object) -> str:
    """``[a-z][a-z0-9_]{0,63}`` (``"preference"``, ``"team_rule"``)."""
    if value is None:
        raise reject(field, InputProblem.REQUIRED)
    if not isinstance(value, str):
        raise reject(field, InputProblem.WRONG_TYPE)
    if len(value) > limits.MAX_MEMORY_TYPE_CHARS or not _MEMORY_TYPE.fullmatch(value):
        raise reject(field, InputProblem.INVALID_FORMAT)
    return value


def validate_commit_sha(field: str, value: object) -> str:
    """A full object name in lower-case hex: 40 (SHA-1) or 64 (SHA-256) digits."""
    if value is None:
        raise reject(field, InputProblem.REQUIRED)
    if not isinstance(value, str):
        raise reject(field, InputProblem.WRONG_TYPE)
    if len(value) not in limits.COMMIT_SHA_LENGTHS or not _COMMIT_SHA.fullmatch(value):
        raise reject(field, InputProblem.INVALID_FORMAT)
    return value


def validate_aware_datetime(field: str, value: object) -> datetime:
    if value is None:
        raise reject(field, InputProblem.REQUIRED)
    if not isinstance(value, datetime):
        raise reject(field, InputProblem.WRONG_TYPE)
    if value.tzinfo is None or value.utcoffset() is None:
        raise reject(field, InputProblem.NAIVE_DATETIME)
    return value


def validate_timedelta(
    field: str, value: object, *, low: timedelta, high: timedelta
) -> timedelta:
    if value is None:
        raise reject(field, InputProblem.REQUIRED)
    if not isinstance(value, timedelta):
        raise reject(field, InputProblem.WRONG_TYPE)
    if not low <= value <= high:
        raise reject(field, InputProblem.OUT_OF_RANGE)
    return value
