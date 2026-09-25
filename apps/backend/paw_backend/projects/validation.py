"""Validation of everything a caller passes to the project module.

Strict on purpose: nothing is coerced (a ``bool`` is not an ``int``, the string
``"manager"`` is not a :class:`ProjectRole`, a naive datetime is not an
instant) and nothing that fails is echoed: the error names the field and a
closed :class:`InputProblem`, never the value.
"""

import unicodedata
import uuid
from datetime import UTC, datetime

from paw_backend.authz.roles import ProjectRole
from paw_backend.authz.subjects import to_uuid
from paw_backend.projects.errors import InputProblem, InvalidProjectInputError
from paw_backend.projects.limits import (
    MAX_DESCRIPTION_CHARS,
    MAX_LIST_LIMIT,
    MAX_LIST_OFFSET,
    MAX_NAME_CHARS,
    MAX_PURGE_BATCH_SIZE,
    RAW_TEXT_FACTOR,
)
from paw_backend.projects.records import ProjectStatus

# Text that may be shown in a UI must not be able to reorder or hide what is
# around it: the Unicode bidirectional formatting controls are refused.
_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩")
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cs", "Zl", "Zp"})


def _fail(field: str, problem: InputProblem) -> InvalidProjectInputError:
    return InvalidProjectInputError(field, problem)


def validate_uuid(field: str, value: object) -> uuid.UUID:
    """``value`` as a ``uuid.UUID``: a ``UUID`` or its canonical string only.

    Canonical means lower-case and hyphenated (``str(uuid.UUID(...))``): braces,
    ``urn:`` prefixes, upper case and bare hex are refused, so an id has one
    spelling. The same ``UUID`` object is returned when one was given.
    """
    try:
        return to_uuid(value, field)
    except ValueError:
        raise _fail(field, InputProblem.NOT_A_UUID) from None


def _text(field: str, value: object, *, limit: int, allow_line_breaks: bool) -> str:
    """The stripped text: type, size, characters, then the caller checks blank."""
    if not isinstance(value, str):
        raise _fail(field, InputProblem.NOT_A_STRING)
    if len(value) > limit * RAW_TEXT_FACTOR:
        raise _fail(field, InputProblem.TOO_LONG)
    for char in value:
        if allow_line_breaks and char in "\n\t":
            continue
        if (
            char in _BIDI_CONTROLS
            or unicodedata.category(char) in _FORBIDDEN_CATEGORIES
        ):
            raise _fail(field, InputProblem.INVALID_CHARACTERS)
    return value.strip()


def validate_name(value: object, field: str = "name") -> str:
    """A project name: 1 to 100 characters after stripping.

    Order of the checks: type, characters, blank, length. Leading and trailing
    whitespace is removed and the stripped text is returned (``"  Alpha "`` is
    ``"Alpha"``); inner spaces are kept. A control character (tab, newline,
    NUL, ...), a surrogate, a line / paragraph separator and a bidirectional
    formatting control are refused, so a name is always a single visible line.
    """
    text = _text(field, value, limit=MAX_NAME_CHARS, allow_line_breaks=False)
    if not text:
        raise _fail(field, InputProblem.EMPTY)
    if len(text) > MAX_NAME_CHARS:
        raise _fail(field, InputProblem.TOO_LONG)
    return text


def validate_description(value: object, field: str = "description") -> str | None:
    """A project description: ``None`` or up to 2000 characters after stripping.

    ``None``, ``""`` and whitespace-only text all mean "no description" and are
    returned as ``None``. Line feed and tab are allowed inside the text; every
    other control character is refused like in a name.
    """
    if value is None:
        return None
    text = _text(field, value, limit=MAX_DESCRIPTION_CHARS, allow_line_breaks=True)
    if not text:
        return None
    if len(text) > MAX_DESCRIPTION_CHARS:
        raise _fail(field, InputProblem.TOO_LONG)
    return text


def validate_confirmation(value: object, field: str = "confirm_name") -> str:
    """The text a user typed to confirm a deletion, unchanged (not stripped).

    Only its type and size are checked (a ``str`` of at most 100 characters,
    the longest possible name): whether it equals the project's name is decided
    by the service. It may be empty (which then never matches).
    """
    if not isinstance(value, str):
        raise _fail(field, InputProblem.NOT_A_STRING)
    if len(value) > MAX_NAME_CHARS:
        raise _fail(field, InputProblem.TOO_LONG)
    return value


def validate_project_role(value: object, field: str = "role") -> ProjectRole:
    """``value`` itself if it is a :class:`ProjectRole` member; a string is refused."""
    if not isinstance(value, ProjectRole):
        raise _fail(field, InputProblem.NOT_A_ROLE)
    return value


def validate_status_filter(value: object, field: str = "status") -> ProjectStatus:
    """A status to list by: ``ACTIVE``, ``ARCHIVED`` or ``PENDING_DELETION``.

    ``DELETED`` is refused (a tombstone is never listed) and so is a string.
    """
    if not isinstance(value, ProjectStatus):
        raise _fail(field, InputProblem.NOT_A_STATUS)
    if value is ProjectStatus.DELETED:
        raise _fail(field, InputProblem.OUT_OF_RANGE)
    return value


def validate_admin_status_filter(
    value: object, field: str = "status"
) -> ProjectStatus | None:
    """The status filter of the administrator's list: ``None`` (every status that is
    not Deleted), or ``ACTIVE`` / ``ARCHIVED`` / ``PENDING_DELETION``.

    A :class:`ProjectStatus` member, or its exact serialised value (``"archived"``:
    an exact ``str``, no case folding or trimming), is accepted and normalised to
    the member. ``DELETED`` (either spelling) is refused: a tombstone is never
    listed (Decision 0008: a Deleted project is "not found" for every operation).
    Anything else (a ``bool``, a number, another ``str`` subclass) is not a status.
    """
    if value is None:
        return None
    if isinstance(value, ProjectStatus):
        status = value
    elif type(value) is str:
        try:
            status = ProjectStatus(value)
        except ValueError:
            raise _fail(field, InputProblem.NOT_A_STATUS) from None
    else:
        raise _fail(field, InputProblem.NOT_A_STATUS)
    if status is ProjectStatus.DELETED:
        raise _fail(field, InputProblem.OUT_OF_RANGE)
    return status


def _bounded_int(field: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(field, InputProblem.NOT_AN_INTEGER)
    if not low <= value <= high:
        raise _fail(field, InputProblem.OUT_OF_RANGE)
    return value


def validate_limit(value: object, field: str = "limit") -> int:
    """A page size: an ``int`` (not a ``bool``) from 1 to 200."""
    return _bounded_int(field, value, 1, MAX_LIST_LIMIT)


def validate_offset(value: object, field: str = "offset") -> int:
    """A page offset: an ``int`` (not a ``bool``) from 0 to 100000."""
    return _bounded_int(field, value, 0, MAX_LIST_OFFSET)


def validate_batch_size(value: object, field: str = "batch_size") -> int:
    """A batch size (purge, task stop): an ``int`` (not a ``bool``) from 1 to 500."""
    return _bounded_int(field, value, 1, MAX_PURGE_BATCH_SIZE)


def validate_instant(field: str, value: object) -> datetime:
    """An aware ``datetime`` converted to UTC. Naive (or a non-datetime) is refused.

    A datetime whose ``tzinfo`` cannot give an offset counts as naive.
    """
    if not isinstance(value, datetime):
        raise _fail(field, InputProblem.NOT_A_DATETIME)
    if value.tzinfo is None or value.utcoffset() is None:
        raise _fail(field, InputProblem.NAIVE_DATETIME)
    return value.astimezone(UTC)
