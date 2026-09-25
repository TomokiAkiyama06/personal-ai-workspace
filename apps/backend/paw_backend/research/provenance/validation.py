"""Argument validation of the provenance store (pure functions, no I/O).

Every value that comes from a caller passes through one of these functions
before the store touches the database. All of them:

* raise :class:`InvalidProvenanceInputError` with the argument's ``field`` name
  and an :class:`InputProblem`; the message never contains the rejected value;
* never coerce: a ``str`` is not a UUID, ``bool`` is not an ``int``, a plain
  ``"supports"`` is not a :class:`Stance`, a naive datetime is not a UTC one.

Problem mapping (used by every function): ``None`` where a value is required is
``REQUIRED``; a value of the wrong type is ``WRONG_TYPE``.
"""

import re
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from paw_backend.research.provenance.errors import (
    InputProblem,
    InvalidProvenanceInputError,
)
from paw_backend.research.providers.errors import InvalidLocatorError
from paw_backend.research.providers.locator import canonicalize_locator

_CONTENT_HASH = re.compile(r"sha256:[0-9a-f]{64}")


def validate_uuid(field: str, value: object) -> UUID:
    """Return ``value`` (the same object) when it is a :class:`uuid.UUID`."""
    if value is None:
        raise InvalidProvenanceInputError(field, InputProblem.REQUIRED)
    if not isinstance(value, UUID):
        raise InvalidProvenanceInputError(field, InputProblem.WRONG_TYPE)
    return value


def validate_optional_uuid(field: str, value: object) -> UUID | None:
    """``None`` stays ``None``; anything else must satisfy :func:`validate_uuid`."""
    if value is None:
        return None
    return validate_uuid(field, value)


def validate_enum[E: Enum](field: str, value: object, enum_type: type[E]) -> E:
    """Return ``value`` when it is a member of ``enum_type`` (no string coercion)."""
    if value is None:
        raise InvalidProvenanceInputError(field, InputProblem.REQUIRED)
    if not isinstance(value, enum_type):
        raise InvalidProvenanceInputError(field, InputProblem.WRONG_TYPE)
    return value


def validate_bounded_int(
    field: str, value: object, *, minimum: int, maximum: int
) -> int:
    """Return ``value`` when it is an ``int`` (not a ``bool``) in the range."""
    if value is None:
        raise InvalidProvenanceInputError(field, InputProblem.REQUIRED)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidProvenanceInputError(field, InputProblem.WRONG_TYPE)
    if not minimum <= value <= maximum:
        raise InvalidProvenanceInputError(field, InputProblem.OUT_OF_RANGE)
    return value


def validate_text(
    field: str, value: object, *, max_chars: int, allow_blank: bool = False
) -> str:
    """Return ``value`` unchanged when it is an acceptable text.

    Checks, in this order (the first failure is reported): ``str`` (else
    ``WRONG_TYPE``); no ``"\\x00"`` and encodable as UTF-8 (else
    ``INVALID_CHARACTERS``: PostgreSQL rejects both); not empty or whitespace
    only unless ``allow_blank`` (else ``BLANK``); at most ``max_chars``
    characters, counted as Unicode code points (else ``TOO_LONG``). The text is
    never stripped or altered.
    """
    if value is None:
        raise InvalidProvenanceInputError(field, InputProblem.REQUIRED)
    if not isinstance(value, str):
        raise InvalidProvenanceInputError(field, InputProblem.WRONG_TYPE)
    if "\x00" in value:
        raise InvalidProvenanceInputError(field, InputProblem.INVALID_CHARACTERS)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidProvenanceInputError(
            field, InputProblem.INVALID_CHARACTERS
        ) from None
    if not allow_blank and not value.strip():
        raise InvalidProvenanceInputError(field, InputProblem.BLANK)
    if len(value) > max_chars:
        raise InvalidProvenanceInputError(field, InputProblem.TOO_LONG)
    return value


def validate_datetime(field: str, value: object) -> datetime:
    """Return ``value`` as an aware UTC ``datetime`` (same instant).

    A naive datetime (no ``tzinfo``, or a ``tzinfo`` whose ``utcoffset()`` is
    ``None``) is ``NAIVE_DATETIME``; a value that cannot be expressed in UTC
    (year 1 with a positive offset, year 9999 with a negative one) is
    ``OUT_OF_RANGE``.
    """
    if value is None:
        raise InvalidProvenanceInputError(field, InputProblem.REQUIRED)
    if not isinstance(value, datetime):
        raise InvalidProvenanceInputError(field, InputProblem.WRONG_TYPE)
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidProvenanceInputError(field, InputProblem.NAIVE_DATETIME)
    try:
        return value.astimezone(UTC)
    except OverflowError:
        raise InvalidProvenanceInputError(field, InputProblem.OUT_OF_RANGE) from None


def validate_optional_datetime(field: str, value: object) -> datetime | None:
    """``None`` stays ``None``; anything else must satisfy :func:`validate_datetime`."""
    if value is None:
        return None
    return validate_datetime(field, value)


def validate_locator(field: str, value: object) -> str:
    """Return the canonical form of a source locator (PAW-051 rules).

    Not a ``str``: ``WRONG_TYPE``. Anything ``canonicalize_locator`` refuses
    (not http(s), user info, whitespace, an IPv6 host, more than 2048
    characters, ...) is ``INVALID_FORMAT``; the parser's own message is dropped.
    """
    if value is None:
        raise InvalidProvenanceInputError(field, InputProblem.REQUIRED)
    if not isinstance(value, str):
        raise InvalidProvenanceInputError(field, InputProblem.WRONG_TYPE)
    try:
        return canonicalize_locator(value)
    except InvalidLocatorError:
        raise InvalidProvenanceInputError(field, InputProblem.INVALID_FORMAT) from None


def validate_content_hash(field: str, value: object) -> str:
    """Return ``value`` when it is ``sha256:`` plus 64 lowercase hex digits."""
    if value is None:
        raise InvalidProvenanceInputError(field, InputProblem.REQUIRED)
    if not isinstance(value, str):
        raise InvalidProvenanceInputError(field, InputProblem.WRONG_TYPE)
    if _CONTENT_HASH.fullmatch(value) is None:
        raise InvalidProvenanceInputError(field, InputProblem.INVALID_FORMAT)
    return value
