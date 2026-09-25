"""Argument validation shared by the queue, budget and loop services.

Every check raises ``InvalidQueueingArgumentError(parameter)`` and never
coerces: a ``bool`` is not an ``int``, a ``str`` is not a ``uuid.UUID`` and a
naive ``datetime`` is not a point in time. The services must call these checks
before they touch the database.
"""

import re
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, NoReturn

from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.tasks.queueing.errors import InvalidQueueingArgumentError

# -- bounds (the single place; the database CHECK constraints repeat them) -----
MAX_RECORD_AMOUNT = 10**12  # one ``record`` call
MAX_CONSUMED = 10**15  # consumed is capped here (saturating), never overflows
MAX_APPROACH = 100
MAX_WORKER_ID_LENGTH = 100
MAX_ERROR_CLASS_LENGTH = 200
MAX_STEP_NAME_LENGTH = 100
MAX_SIGNATURE_MESSAGE_CHARS = 2000  # only this prefix of a message is examined
DEFAULT_LEASE_SECONDS = 60
MAX_LEASE_SECONDS = 86_400
MAX_ENTRY_ID = 2**63 - 1
MAX_CLAIM_COUNT = 2**31 - 1  # ``queue_entries.claim_count`` is a 32-bit integer
MAX_ATTEMPT = 2**31 - 1  # ``tasks.attempt`` is a 32-bit integer
MAX_RUNTIME_GENERATION = 2**63 - 1  # ``budget_usages.runtime_generation`` is a BIGINT

_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]*")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_SIGNATURE = re.compile(r"[0-9a-f]{64}")
# A lone surrogate code point (U+D800 to U+DFFF): a Python ``str`` can hold it (for
# example from the JSON text ``"\ud800"`` or ``errors="surrogateescape"``), but it
# is not Unicode text and cannot be encoded as UTF-8.
_SURROGATE = re.compile("[\ud800-\udfff]")


def _reject(parameter: str) -> NoReturn:
    raise InvalidQueueingArgumentError(parameter)


def check_int(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    """``value`` must be an ``int`` (not a ``bool``) within [minimum, maximum]."""
    if isinstance(value, bool) or not isinstance(value, int):
        _reject(name)
    if not minimum <= value <= maximum:
        _reject(name)
    return value


def check_bool(name: str, value: Any) -> bool:
    """Only ``True`` / ``False`` (no truthy values)."""
    if not isinstance(value, bool):
        _reject(name)
    return value


def check_amount(name: str, value: Any) -> int:
    """A non-negative integer amount of at most ``MAX_RECORD_AMOUNT``.

    Floats (finite or not), strings, ``None`` and booleans are rejected.
    """
    return check_int(name, value, minimum=0, maximum=MAX_RECORD_AMOUNT)


def check_entry_id(value: Any) -> int:
    """A queue entry id: an ``int`` from 1 to ``MAX_ENTRY_ID``."""
    return check_int("entry_id", value, minimum=1, maximum=MAX_ENTRY_ID)


def check_claim_count(value: Any) -> int:
    """A claim generation: an ``int`` from 1 (first claim) to ``MAX_CLAIM_COUNT``."""
    return check_int("claim_count", value, minimum=1, maximum=MAX_CLAIM_COUNT)


def check_runtime_generation(value: Any) -> int:
    """A runtime session generation: an ``int`` from 1 (first start) upward."""
    return check_int("generation", value, minimum=1, maximum=MAX_RUNTIME_GENERATION)


def check_attempt(value: Any) -> int:
    """A task attempt number: an ``int`` from 1 to ``MAX_ATTEMPT``."""
    return check_int("attempt", value, minimum=1, maximum=MAX_ATTEMPT)


def check_approach(value: Any) -> int:
    """An approach index: an ``int`` from 0 to ``MAX_APPROACH``."""
    return check_int("approach", value, minimum=0, maximum=MAX_APPROACH)


def check_uuid(name: str, value: Any) -> uuid.UUID:
    """Only a ``uuid.UUID`` instance is accepted (strings are not parsed)."""
    if not isinstance(value, uuid.UUID):
        _reject(name)
    return value


def check_session(name: str, value: Any) -> AsyncSession:
    """An ``AsyncSession`` that is INSIDE a transaction (the caller's own).

    A session that has no transaction (a fresh one, or one whose transaction was
    committed or rolled back) would begin one silently when the statement runs and
    give it up when the session is closed, so a write reported as done would be
    rolled back and a lock reported as held would be released. ``in_transaction()``
    is true inside ``session.begin()`` and after any statement that began the
    transaction by itself; the caller then commits or rolls it back.
    """
    if not isinstance(value, AsyncSession) or not value.in_transaction():
        _reject(name)
    return value


def check_project_gate(name: str, value: Any) -> Any:
    """An object with ``require_active`` and ``active_condition`` (never ``None``)."""
    if not all(
        callable(getattr(value, method, None))
        for method in ("require_active", "active_condition")
    ):
        _reject(name)
    return value


def check_now(name: str, value: Any) -> datetime:
    """A timezone-aware ``datetime`` (naive values are rejected)."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        _reject(name)
    return value


def check_member[E: Enum](name: str, value: Any, enum_class: type[E]) -> E:
    """``value`` must be a member of ``enum_class`` itself, not its string value."""
    if not isinstance(value, enum_class):
        _reject(name)
    return value


def check_worker_id(value: Any) -> str:
    """1 to 100 ASCII characters ``[A-Za-z0-9._:@/-]``, first a letter or digit."""
    if (
        not isinstance(value, str)
        or len(value) > MAX_WORKER_ID_LENGTH
        or _WORKER_ID.fullmatch(value) is None
    ):
        _reject("worker_id")
    return value


def check_label(name: str, value: Any, *, maximum: int) -> str:
    """A non-blank ``str`` of at most ``maximum`` characters, no control characters
    and no surrogate code points (text that UTF-8 cannot encode)."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or _CONTROL_CHARACTER.search(value) is not None
        or _SURROGATE.search(value) is not None
    ):
        _reject(name)
    return value


def check_error_class(value: Any) -> str:
    return check_label("error_class", value, maximum=MAX_ERROR_CLASS_LENGTH)


def check_step_name(value: Any) -> str:
    return check_label("step", value, maximum=MAX_STEP_NAME_LENGTH)


def check_message(value: Any) -> str:
    """Any ``str`` (possibly empty, multi-line) that UTF-8 can encode. Length is not
    limited here: only the first ``MAX_SIGNATURE_MESSAGE_CHARS`` characters are ever
    used, but the whole text is checked for surrogate code points (also the part
    that is cut off, so the result does not depend on where the cut falls). Other
    control characters, NUL included, are accepted: the message is only hashed and
    never stored."""
    if not isinstance(value, str) or _SURROGATE.search(value) is not None:
        _reject("message")
    return value


def check_signature(value: Any) -> str:
    """A failure signature: exactly 64 lowercase hexadecimal characters."""
    if not isinstance(value, str) or _SIGNATURE.fullmatch(value) is None:
        _reject("signature")
    return value
