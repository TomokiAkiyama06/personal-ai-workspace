"""Validation of everything a caller passes to the connection module.

Strict on purpose: nothing is coerced (a ``bool`` is not an ``int``, an enum
accepts the member or its exact ``str`` value and nothing else, an id is a
``uuid.UUID`` or its canonical string, a naive datetime is not an instant) and
nothing that fails is echoed: the error names the field and a closed
:class:`InputProblem`, never the value.
"""

import math
import unicodedata
import uuid
from datetime import UTC, tzinfo
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from paw_backend.authz.subjects import to_uuid
from paw_backend.connections.domain import UNLIMITED, Unlimited
from paw_backend.connections.errors import InputProblem, InvalidConnectionInputError
from paw_backend.connections.limits import (
    MAX_LIST_LIMIT,
    MAX_LIST_OFFSET,
    MAX_MODEL_CHARS,
    MAX_PROMPT_CHARS,
    MAX_QUOTA_LIMIT,
    MODEL_PATTERN,
)
from paw_backend.tools.credentials import is_credential_handle


def fail(field: str, problem: InputProblem) -> InvalidConnectionInputError:
    return InvalidConnectionInputError(field, problem)


def validate_uuid(field: str, value: object) -> uuid.UUID:
    """``value`` as a ``uuid.UUID``: a ``UUID`` or its canonical string only."""
    try:
        return to_uuid(value, field)
    except ValueError:
        raise fail(field, InputProblem.NOT_A_UUID) from None


def validate_enum[E: StrEnum](field: str, value: object, kind: type[E]) -> E:
    """The member of ``kind`` that ``value`` is, or whose exact ``str`` value it is.

    A member of another enum, a ``str`` subclass, different case or surrounding
    space, and every other type are refused.
    """
    if type(value) is kind:
        return value  # type: ignore[return-value]
    if type(value) is not str:
        raise fail(field, InputProblem.NOT_ONE_OF)
    try:
        return kind(value)
    except ValueError:
        raise fail(field, InputProblem.NOT_ONE_OF) from None


def validate_bool(field: str, value: object) -> bool:
    if type(value) is not bool:
        raise fail(field, InputProblem.NOT_A_BOOL)
    return value


def validate_handle(field: str, value: object) -> str:
    """A credential handle exactly as the secret store issues it (``cred_`` + 32 hex).

    Nothing is trimmed or case-folded: a look-alike is not a handle. A credential
    plaintext is not one either, and is refused here without being looked at any
    further.
    """
    if type(value) is not str:
        raise fail(field, InputProblem.NOT_A_STRING)
    if not is_credential_handle(value):
        raise fail(field, InputProblem.NOT_A_HANDLE)
    return value


def validate_model(field: str, value: object) -> str:
    """A model name: 1 to 100 characters of ``[A-Za-z0-9._:/-]``, starting with an
    alphanumeric character. Nothing is stripped."""
    if type(value) is not str:
        raise fail(field, InputProblem.NOT_A_STRING)
    if not value:
        raise fail(field, InputProblem.EMPTY)
    if len(value) > MAX_MODEL_CHARS:
        raise fail(field, InputProblem.TOO_LONG)
    if MODEL_PATTERN.fullmatch(value) is None:
        raise fail(field, InputProblem.INVALID_CHARACTERS)
    return value


def _has_forbidden_character(text: str) -> bool:
    return any(char == "\x00" or unicodedata.category(char) == "Cs" for char in text)


def validate_prompt(field: str, value: object) -> str:
    """The text a call sends to a provider: 1 to 500,000 characters, without NUL
    or a lone surrogate (line breaks and tabs are text). It is never stored."""
    if type(value) is not str:
        raise fail(field, InputProblem.NOT_A_STRING)
    if not value:
        raise fail(field, InputProblem.EMPTY)
    if len(value) > MAX_PROMPT_CHARS:
        raise fail(field, InputProblem.TOO_LONG)
    if _has_forbidden_character(value):
        raise fail(field, InputProblem.INVALID_CHARACTERS)
    return value


def validate_result_text(field: str, value: object, limit: int) -> str:
    """What an adapter answered: any ``str`` up to ``limit`` characters (it may be
    empty; NUL and surrogates are refused so the text can be stored and sent)."""
    if type(value) is not str:
        raise fail(field, InputProblem.NOT_A_STRING)
    if len(value) > limit:
        raise fail(field, InputProblem.TOO_LONG)
    if _has_forbidden_character(value):
        raise fail(field, InputProblem.INVALID_CHARACTERS)
    return value


def validate_int(field: str, value: object, low: int, high: int) -> int:
    if type(value) is not int:  # a bool, an int subclass, a float, a str: none
        raise fail(field, InputProblem.NOT_AN_INTEGER)
    if not low <= value <= high:
        raise fail(field, InputProblem.OUT_OF_RANGE)
    return value


def validate_optional_tokens(field: str, value: object, high: int) -> int | None:
    return None if value is None else validate_int(field, value, 0, high)


def validate_seconds(field: str, value: object, maximum: float) -> float:
    """A positive, finite duration of at most ``maximum`` seconds (a ``bool`` is
    not a number)."""
    if type(value) not in (int, float):
        raise fail(field, InputProblem.NOT_A_NUMBER)
    if not math.isfinite(value) or not 0 < value <= maximum:
        raise fail(field, InputProblem.OUT_OF_RANGE)
    return float(value)


def validate_quota_limit(field: str, value: object) -> int | Unlimited:
    """A quota limit: an ``int`` from 0 to 10**12, or ``UNLIMITED`` (the member or
    the string ``"unlimited"``). ``None`` is not "unlimited": it is refused."""
    if type(value) is Unlimited or (type(value) is str and value == UNLIMITED.value):
        return UNLIMITED
    return validate_int(field, value, 0, MAX_QUOTA_LIMIT)


def validate_page(limit: object, offset: object) -> tuple[int, int]:
    return (
        validate_int("limit", limit, 1, MAX_LIST_LIMIT),
        validate_int("offset", offset, 0, MAX_LIST_OFFSET),
    )


def zone_of(name: object) -> tzinfo:
    """The time zone of the calendar periods, by its IANA name (``"UTC"`` needs no
    database of zones). An unknown name, a wrong type or a path-like name is refused.

    A named zone (the default ``Asia/Tokyo`` too) needs the time zone database of the
    system (``tzdata``); a host without it fails here, when the service is built.
    """
    if type(name) is not str:
        raise fail("period_timezone", InputProblem.NOT_A_STRING)
    if name == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except (ValueError, LookupError, OSError):  # ZoneInfoNotFoundError is a KeyError
        raise fail("period_timezone", InputProblem.NOT_ONE_OF) from None


def require_type(field: str, value: object, expected: type[Any]) -> None:
    """``value`` is exactly an instance of ``expected`` (a subclass is refused: its
    methods could lie about what it holds)."""
    if type(value) is not expected:
        raise fail(field, InputProblem.WRONG_TYPE)
