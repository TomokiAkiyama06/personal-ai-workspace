"""Argument validation of Shared Memory administration (pure functions, no I/O).

Every value that comes from a caller passes through one of these functions
before anything else happens. All of them:

* raise :class:`InvalidSharedMemoryInputError` with the argument's ``field``
  name and an :class:`InputProblem`; the message never contains the rejected
  value;
* never coerce: a ``str`` is not a UUID, ``"5"`` and ``5.0`` are not integers,
  ``bool`` is not an ``int``, a plain ``"user"`` is not an enum member;
* validate every element of a collection before they are de-duplicated.

``None`` where a value is required is ``REQUIRED``; a value of the wrong type is
``WRONG_TYPE``.
"""

import re
from datetime import datetime
from enum import Enum
from uuid import UUID

from paw_backend.memory.shared import limits
from paw_backend.memory.shared.errors import InputProblem, InvalidSharedMemoryInputError

_SUBJECT = re.compile(r"[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){0,4}")
_MEMORY_TYPE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_POLICY_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


def _reject(field: str, problem: InputProblem) -> InvalidSharedMemoryInputError:
    return InvalidSharedMemoryInputError(field, problem)


def validate_uuid(field: str, value: object) -> UUID:
    """``value`` if it is a :class:`uuid.UUID`; a string form is ``WRONG_TYPE``."""
    if value is None:
        raise _reject(field, InputProblem.REQUIRED)
    if not isinstance(value, UUID):
        raise _reject(field, InputProblem.WRONG_TYPE)
    return value


def validate_optional_uuid(field: str, value: object) -> UUID | None:
    return None if value is None else validate_uuid(field, value)


def validate_enum[E: Enum](field: str, value: object, kind: type[E]) -> E:
    """``value`` if it is a member of ``kind``; a plain string is ``WRONG_TYPE``."""
    if value is None:
        raise _reject(field, InputProblem.REQUIRED)
    if not isinstance(value, kind):
        raise _reject(field, InputProblem.WRONG_TYPE)
    return value


def validate_int(field: str, value: object, *, low: int, high: int) -> int:
    """An ``int`` in ``low..high`` (both included). ``bool`` is ``WRONG_TYPE``."""
    if value is None:
        raise _reject(field, InputProblem.REQUIRED)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _reject(field, InputProblem.WRONG_TYPE)
    if not low <= value <= high:
        raise _reject(field, InputProblem.OUT_OF_RANGE)
    return value


def validate_text(field: str, value: object, *, max_chars: int) -> str:
    """Free text, returned unchanged (never stripped, folded or truncated).

    Checks, first failure wins: ``None`` is ``REQUIRED``; not a ``str`` is
    ``WRONG_TYPE``; more than ``max_chars`` characters (``len``, code points;
    exactly ``max_chars`` is accepted) is ``TOO_LONG``; a NUL or a character that
    cannot be encoded as UTF-8 (a lone surrogate) is ``INVALID_CHARACTERS``;
    empty or whitespace only (``value.strip() == ""``) is ``BLANK``.
    """
    if value is None:
        raise _reject(field, InputProblem.REQUIRED)
    if not isinstance(value, str):
        raise _reject(field, InputProblem.WRONG_TYPE)
    if len(value) > max_chars:
        raise _reject(field, InputProblem.TOO_LONG)
    if "\x00" in value:
        raise _reject(field, InputProblem.INVALID_CHARACTERS)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _reject(field, InputProblem.INVALID_CHARACTERS) from None
    if value.strip() == "":
        raise _reject(field, InputProblem.BLANK)
    return value


def validate_optional_text(field: str, value: object, *, max_chars: int) -> str | None:
    return None if value is None else validate_text(field, value, max_chars=max_chars)


def _pattern(
    field: str, value: object, pattern: re.Pattern[str], max_chars: int
) -> str:
    if value is None:
        raise _reject(field, InputProblem.REQUIRED)
    if not isinstance(value, str):
        raise _reject(field, InputProblem.WRONG_TYPE)
    if len(value) > max_chars or pattern.fullmatch(value) is None:
        raise _reject(field, InputProblem.INVALID_FORMAT)
    return value


def validate_subject(value: object, field: str = "subject") -> str:
    """A policy subject: 1 to 5 dot-separated segments, each ``[a-z][a-z0-9_]{0,31}``.

    At most ``MAX_SUBJECT_CHARS`` (100) characters overall. ``"merge"``,
    ``"merge.permission"`` and ``"a.b.c.d.e"`` are valid; ``""``, ``"Merge"``,
    ``"merge."``, ``".merge"``, ``"merge..x"``, ``"a.b.c.d.e.f"`` (6 segments),
    ``"1merge"`` and ``"merge-x"`` are ``INVALID_FORMAT``. A non-string is
    ``WRONG_TYPE``.
    """
    return _pattern(field, value, _SUBJECT, limits.MAX_SUBJECT_CHARS)


def validate_memory_type(field: str, value: object) -> str:
    """``[a-z][a-z0-9_]{0,63}`` (``"preference"``, ``"team_rule"``)."""
    return _pattern(field, value, _MEMORY_TYPE, limits.MAX_MEMORY_TYPE_CHARS)


def validate_policy_id(field: str, value: object) -> str:
    """``[a-z][a-z0-9_.-]{0,63}`` (``"no-force-push"``, ``"security.merge_1"``)."""
    return _pattern(field, value, _POLICY_ID, limits.MAX_POLICY_ID_CHARS)


def normalize_subjects(field: str, values: object) -> tuple[str, ...]:
    """The sorted, de-duplicated tuple of valid subjects.

    ``None`` is the empty tuple. Only ``list``, ``tuple``, ``set`` and
    ``frozenset`` are accepted (a ``str`` or ``bytes`` would be split into
    characters, a ``dict`` is a key set): anything else is ``WRONG_TYPE``. More
    than ``MAX_POLICY_SUBJECTS`` elements (counted before de-duplication, before
    any element is looked at) is ``TOO_MANY``. Every element must satisfy
    :func:`validate_subject` (reported with ``field``), and only then are
    duplicates removed.
    """
    if values is None:
        return ()
    if not isinstance(values, list | tuple | set | frozenset):
        raise _reject(field, InputProblem.WRONG_TYPE)
    if len(values) > limits.MAX_POLICY_SUBJECTS:
        raise _reject(field, InputProblem.TOO_MANY)
    checked = [validate_subject(value, field) for value in values]
    return tuple(sorted(set(checked)))


def validate_aware_datetime(field: str, value: object) -> datetime:
    if not isinstance(value, datetime):
        raise _reject(field, InputProblem.WRONG_TYPE)
    if value.tzinfo is None or value.utcoffset() is None:
        raise _reject(field, InputProblem.NAIVE_DATETIME)
    return value


def validate_page(limit: object, offset: object) -> tuple[int, int]:
    """``(limit, offset)``: ``limit`` 1..200 and ``offset`` 0..100000, both ``int``."""
    return (
        validate_int("limit", limit, low=1, high=limits.MAX_LIST_LIMIT),
        validate_int("offset", offset, low=0, high=limits.MAX_LIST_OFFSET),
    )
