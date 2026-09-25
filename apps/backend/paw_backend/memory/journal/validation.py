"""Argument validation of the Immediate Journal (pure functions, no I/O).

Every value that comes from a caller passes through one of these functions before
anything else happens (before the Authorizer, before the database). All of them:

* raise :class:`InvalidJournalInputError` with the argument's ``field`` name and
  an :class:`InputProblem`; the message never contains the rejected value;
* never coerce: a ``str`` is not a UUID, ``bool`` is not an ``int``, ``5.0`` is not
  an ``int``, ``"HIGH"`` is not ``"high"``;
* accept an enum as the member or as its exact serialised ``str`` (a plain ``str``
  only when its type is exactly ``str``, so a subclass with its own ``__eq__``
  cannot pose as a member) and return the member.

``None`` where a value is required is ``REQUIRED``; a value of the wrong type is
``WRONG_TYPE``.
"""

import math
import re
from enum import Enum
from uuid import UUID

from paw_backend.memory.journal import limits
from paw_backend.memory.journal.errors import InputProblem, InvalidJournalInputError

_SURROGATE = re.compile("[\ud800-\udfff]")
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]*")


def _reject(field: str, problem: InputProblem) -> InvalidJournalInputError:
    return InvalidJournalInputError(field, problem)


def validate_uuid(field: str, value: object) -> UUID:
    """``value`` if it is a :class:`uuid.UUID`; the string form is ``WRONG_TYPE``."""
    if value is None:
        raise _reject(field, InputProblem.REQUIRED)
    if not isinstance(value, UUID):
        raise _reject(field, InputProblem.WRONG_TYPE)
    return value


def validate_optional_uuid(field: str, value: object) -> UUID | None:
    return None if value is None else validate_uuid(field, value)


def validate_enum[E: Enum](field: str, value: object, kind: type[E]) -> E:
    """The member of ``kind`` named by ``value`` (the member or its exact value)."""
    if value is None:
        raise _reject(field, InputProblem.REQUIRED)
    if isinstance(value, kind):
        return value
    if type(value) is not str:
        raise _reject(field, InputProblem.WRONG_TYPE)
    try:
        return kind(value)
    except ValueError:
        raise _reject(field, InputProblem.UNKNOWN_VALUE) from None


def validate_bool(field: str, value: object) -> bool:
    if value is None:
        raise _reject(field, InputProblem.REQUIRED)
    if not isinstance(value, bool):
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


def validate_seconds(field: str, value: object, *, high: float) -> float:
    """A finite ``int`` or ``float`` above 0 and at most ``high`` (not a ``bool``)."""
    if value is None:
        raise _reject(field, InputProblem.REQUIRED)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _reject(field, InputProblem.WRONG_TYPE)
    if not math.isfinite(value) or not 0 < value <= high:
        raise _reject(field, InputProblem.OUT_OF_RANGE)
    return float(value)


def has_forbidden_characters(text: str) -> bool:
    """NUL (PostgreSQL ``text`` cannot store it) or a lone surrogate (not UTF-8)."""
    return "\x00" in text or _SURROGATE.search(text) is not None


def validate_text(field: str, value: object, *, max_chars: int) -> str:
    """Free text, returned unchanged (never stripped, folded or truncated).

    First failure wins: ``None`` is ``REQUIRED``; not a ``str`` is ``WRONG_TYPE``;
    more than ``max_chars`` characters (``len``, code points; exactly ``max_chars``
    is accepted) is ``TOO_LONG``; a NUL or a lone surrogate is
    ``INVALID_CHARACTERS``; empty or whitespace only is ``BLANK``.
    """
    if value is None:
        raise _reject(field, InputProblem.REQUIRED)
    if not isinstance(value, str):
        raise _reject(field, InputProblem.WRONG_TYPE)
    if len(value) > max_chars:
        raise _reject(field, InputProblem.TOO_LONG)
    if has_forbidden_characters(value):
        raise _reject(field, InputProblem.INVALID_CHARACTERS)
    if not value.strip():
        raise _reject(field, InputProblem.BLANK)
    return value


def validate_worker_id(field: str, value: object) -> str:
    """1 to 100 ASCII characters ``[A-Za-z0-9._:@/-]``, first a letter or digit."""
    if value is None:
        raise _reject(field, InputProblem.REQUIRED)
    if not isinstance(value, str):
        raise _reject(field, InputProblem.WRONG_TYPE)
    if len(value) > limits.MAX_WORKER_ID_CHARS:
        raise _reject(field, InputProblem.TOO_LONG)
    if _WORKER_ID.fullmatch(value) is None:
        raise _reject(field, InputProblem.INVALID_FORMAT)
    return value


def validate_job_id(field: str, value: object) -> int:
    return validate_int(field, value, low=1, high=limits.MAX_JOB_ID)


def validate_claim_count(field: str, value: object) -> int:
    """A claim generation: from 1 (the first claim) to the largest 32-bit integer."""
    return validate_int(field, value, low=1, high=limits.MAX_CLAIM_COUNT)
