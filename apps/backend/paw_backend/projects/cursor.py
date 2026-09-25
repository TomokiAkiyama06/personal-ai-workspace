"""The opaque keyset cursor of the administrator's project list (Issue #84).

``ProjectService.list_all_projects`` pages by **keyset**, not by offset: the next
page is "the rows after this ``(created_at, id)``" in the order newest first, ties
by id descending. A row that is created, changed or purged while somebody pages
through the list can therefore never make a row appear twice or be skipped
(which an offset does); a row inserted *behind* the cursor is simply seen when
the traversal reaches it, one inserted *ahead* of it is not part of that
traversal.

The cursor is text the caller only hands back. It is **opaque** (a caller must
not build or parse one) and it is **validated as hostile input**: it is never
trusted, never echoed and never reaches SQL as anything but two typed
parameters. It is not signed: it names only a position in a list that its
holder may read in full anyway, so a forged cursor gains nothing (a position the
caller could have got from the list itself, or one that selects an empty page).

Format (version 1): the ASCII text ``1.<filter>.<microseconds>.<id>`` written as
unpadded URL-safe base64, where ``<filter>`` is ``all`` or the status the list was
asked for, ``<microseconds>`` is ``created_at`` in whole microseconds since the
Unix epoch (canonical decimal) and ``<id>`` is the canonical lower-case UUID. A
cursor is refused (``InvalidProjectInputError``, field ``cursor``) unless it
decodes to exactly this shape, is the canonical encoding of it (one spelling per
value), names the **same filter** as the call (a cursor of the archived list is
not a position in the list of everything), and holds an instant that a
``datetime`` can represent. The longest valid cursor is ``MAX_CURSOR_CHARS``
characters, so longer text is refused before it is looked at.
"""

import base64
import binascii
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from paw_backend.projects.errors import InputProblem, InvalidProjectInputError
from paw_backend.projects.limits import MAX_CURSOR_CHARS
from paw_backend.projects.records import ProjectStatus

_VERSION = "1"
_ALL = "all"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
# The instants a ``datetime`` (and PostgreSQL's ``timestamptz``) can hold.
MIN_MICROS = (datetime.min.replace(tzinfo=UTC) - _EPOCH) // _MICROSECOND
MAX_MICROS = (datetime.max.replace(tzinfo=UTC) - _EPOCH) // _MICROSECOND

_BASE64_TEXT = re.compile(r"[A-Za-z0-9_-]+")
_TOKEN = re.compile(
    rf"{_VERSION}\.(?P<filter>{_ALL}|active|archived|pending_deletion)"
    r"\.(?P<micros>0|-?[1-9][0-9]{0,17})"
    r"\.(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.ASCII,
)


@dataclass(frozen=True, slots=True)
class Keyset:
    """The position a cursor stands for: the last row of the previous page."""

    created_at: datetime  # aware, UTC
    id: uuid.UUID


def filter_token(status: ProjectStatus | None) -> str:
    """``all`` for no filter, else the status value (a closed vocabulary)."""
    return _ALL if status is None else status.value


def encode_cursor(
    status: ProjectStatus | None, created_at: datetime, project_id: uuid.UUID
) -> str:
    """The cursor that continues the list ``status`` after this row."""
    micros = (created_at.astimezone(UTC) - _EPOCH) // _MICROSECOND
    token = f"{_VERSION}.{filter_token(status)}.{micros}.{project_id}"
    return base64.urlsafe_b64encode(token.encode("ascii")).decode("ascii").rstrip("=")


def _invalid(
    problem: InputProblem = InputProblem.INVALID_CURSOR,
) -> InvalidProjectInputError:
    return InvalidProjectInputError("cursor", problem)


def decode_cursor(value: object, status: ProjectStatus | None) -> Keyset:
    """The position of a cursor for the list ``status``, or an input error.

    Order of the checks: the type (an exact ``str``), the length, the alphabet,
    the canonical base64, the shape of the text, the filter, the instant. The
    error names the field and a closed problem, never the value.
    """
    if type(value) is not str:
        raise _invalid(InputProblem.NOT_A_STRING)
    if len(value) > MAX_CURSOR_CHARS:
        raise _invalid(InputProblem.TOO_LONG)
    if _BASE64_TEXT.fullmatch(value) is None or len(value) % 4 == 1:
        raise _invalid()
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise _invalid() from None
    # One spelling per value: base64 text with stray low bits decodes but is
    # not what ``encode_cursor`` writes.
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != value:
        raise _invalid()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise _invalid() from None
    match = _TOKEN.fullmatch(text)
    if match is None or match["filter"] != filter_token(status):
        raise _invalid()
    micros = int(match["micros"])
    if not MIN_MICROS <= micros <= MAX_MICROS:
        raise _invalid()
    return Keyset(_EPOCH + timedelta(microseconds=micros), uuid.UUID(match["id"]))
