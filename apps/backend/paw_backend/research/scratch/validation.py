"""Argument validation of the Research Scratch Store (pure functions, no I/O).

Every value that comes from a caller passes through one of these functions
before the store touches the database. All of them:

* raise :class:`InvalidScratchInputError` with the argument's ``field`` name and
  an :class:`InputProblem`; the message never contains the rejected value;
* never coerce: a ``str`` is not a UUID, ``"5"`` and ``5.0`` are not integers,
  ``bool`` is not an ``int``, a plain ``"promoted"`` is not a
  :class:`PromotionOutcome`;
* catch only the exceptions they handle themselves (for example
  ``UnicodeEncodeError`` while testing whether a string can be encoded).

Problem mapping (used by every function): ``None`` where a value is required is
``REQUIRED``; a value of the wrong type is ``WRONG_TYPE``.
"""

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from paw_backend.research.scratch import limits
from paw_backend.research.scratch.errors import InputProblem, InvalidScratchInputError
from paw_backend.research.scratch.records import PromotionOutcome


@dataclass(frozen=True, slots=True)
class NewItem:
    """The validated fields of an item that is about to be added."""

    project_id: UUID
    created_by: UUID
    task_id: UUID | None
    query: str | None
    title: str | None
    summary: str | None
    content: str | None
    source_metadata: dict[str, Any]


def validate_uuid(field: str, value: object) -> UUID:
    """Return ``value`` when it is a :class:`uuid.UUID`.

    ``None`` is ``REQUIRED``. Anything else, including the string form of a
    UUID, ``bytes`` and ``int``, is ``WRONG_TYPE``.
    """
    if value is None:
        raise InvalidScratchInputError(field, InputProblem.REQUIRED)
    if not isinstance(value, UUID):
        raise InvalidScratchInputError(field, InputProblem.WRONG_TYPE)
    return value


def validate_optional_uuid(field: str, value: object) -> UUID | None:
    """``None`` stays ``None``; anything else must satisfy :func:`validate_uuid`."""
    if value is None:
        return None
    return validate_uuid(field, value)


def validate_optional_text(field: str, value: object, *, max_chars: int) -> str | None:
    """Validate an optional free-text field and return it unchanged.

    ``None`` returns ``None`` (the field is absent). Otherwise the checks run in
    this order and the first failure is reported:

    1. not a ``str``: ``WRONG_TYPE`` (``bytes``, ``int``, ``bool``, ...);
    2. contains ``"\\x00"`` or cannot be encoded as UTF-8 (a lone surrogate):
       ``INVALID_CHARACTERS`` (PostgreSQL text and jsonb reject both);
    3. empty or whitespace only (``str.strip() == ""``, so ``"　"`` counts):
       ``BLANK`` (an absent value is ``None``, never ``""``);
    4. more than ``max_chars`` characters (``len(value)``, Unicode code points):
       ``TOO_LONG``. Exactly ``max_chars`` is accepted.

    The returned string is the argument itself: it is not stripped, folded or
    truncated.
    """
    if value is None:
        return None
    _check_text(field, value, max_chars)
    return value


def validate_source_metadata(value: object) -> dict[str, Any]:
    """Validate a JSON object of source metadata and return a deep copy.

    ``None`` returns a new empty dict. Anything that is not a ``dict`` is
    ``WRONG_TYPE``. Otherwise the object is walked depth first, in document
    order (dict insertion order), and the first problem is reported:

    * depth: the top-level object is depth 1 and every nested ``dict`` or
      ``list`` is one deeper (scalars add nothing). A container at depth more
      than ``MAX_SOURCE_METADATA_DEPTH`` (6) is ``TOO_DEEP``. The depth is
      checked before descending, so a self-referencing container is ``TOO_DEEP``
      and never a ``RecursionError``;
    * keys: not a ``str`` is ``WRONG_TYPE``; then the text rules 2, 3 and 4 of
      :func:`validate_optional_text` with ``MAX_SOURCE_METADATA_KEY_CHARS`` (128)
      as the limit (``INVALID_CHARACTERS``, ``BLANK``, ``TOO_LONG``);
    * values: ``None``, ``bool``, ``int``, ``float``, ``str``, ``list`` and
      ``dict`` are JSON. Everything else (``tuple``, ``set``, ``bytes``,
      ``Decimal``, ``datetime``, ``UUID``, any object) is ``WRONG_TYPE``.
      ``bool`` is checked before ``int``;
    * ``int``: ``abs(value) > MAX_JSON_INT`` (``2**53 - 1``) is ``OUT_OF_RANGE``;
    * ``float``: ``nan`` and ``inf`` are ``OUT_OF_RANGE``; so is any non-zero
      value with ``abs(value) < 1e-6`` or ``abs(value) >= 1e15`` (``0.0`` and
      ``-0.0`` are accepted);
    * ``str`` values: ``INVALID_CHARACTERS`` for ``"\\x00"`` or a string that
      cannot be encoded as UTF-8. Empty strings and any length are fine here
      (only the total size is bounded).

    After the walk, the compact UTF-8 JSON text
    (``json.dumps(value, ensure_ascii=False, separators=(",", ":"),
    allow_nan=False).encode("utf-8")``) larger than
    ``MAX_SOURCE_METADATA_BYTES`` (16384) is ``TOO_LARGE``; exactly 16384 bytes
    is accepted.

    The result is a new structure of plain ``dict`` / ``list`` / scalars that
    shares nothing with the argument, so a later change of the argument cannot
    change what is stored.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidScratchInputError(_METADATA, InputProblem.WRONG_TYPE)
    copy = _copy_object(value, depth=1)
    compact = json.dumps(
        copy, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    if len(compact.encode("utf-8")) > limits.MAX_SOURCE_METADATA_BYTES:
        raise InvalidScratchInputError(_METADATA, InputProblem.TOO_LARGE)
    return copy


def validate_new_item(
    *,
    project_id: object,
    created_by: object,
    task_id: object,
    query: object,
    title: object,
    summary: object,
    content: object,
    source_metadata: object,
) -> NewItem:
    """Validate every field of a new item; report the first problem.

    Fields are checked in this order, each with the field name of its own
    argument: ``project_id`` (:func:`validate_uuid`), ``created_by``
    (:func:`validate_uuid`), ``task_id`` (:func:`validate_optional_uuid`),
    ``query`` (``MAX_QUERY_CHARS``), ``title`` (``MAX_TITLE_CHARS``), ``summary``
    (``MAX_SUMMARY_CHARS``), ``content`` (``MAX_CONTENT_CHARS``; all four via
    :func:`validate_optional_text`), ``source_metadata``
    (:func:`validate_source_metadata`). After that, an item needs a body: when
    both ``summary`` and ``content`` are ``None`` the result is
    ``InvalidScratchInputError("summary", REQUIRED)``.
    """
    item = NewItem(
        project_id=validate_uuid("project_id", project_id),
        created_by=validate_uuid("created_by", created_by),
        task_id=validate_optional_uuid("task_id", task_id),
        query=validate_optional_text("query", query, max_chars=limits.MAX_QUERY_CHARS),
        title=validate_optional_text("title", title, max_chars=limits.MAX_TITLE_CHARS),
        summary=validate_optional_text(
            "summary", summary, max_chars=limits.MAX_SUMMARY_CHARS
        ),
        content=validate_optional_text(
            "content", content, max_chars=limits.MAX_CONTENT_CHARS
        ),
        source_metadata=validate_source_metadata(source_metadata),
    )
    if item.summary is None and item.content is None:
        raise InvalidScratchInputError("summary", InputProblem.REQUIRED)
    return item


def validate_datetime(field: str, value: object) -> datetime:
    """Return ``value`` converted to UTC (the instant is unchanged).

    ``None`` is ``REQUIRED``; a non-``datetime`` is ``WRONG_TYPE``; a datetime
    without a timezone (``tzinfo`` is ``None`` or ``utcoffset()`` is ``None``)
    is ``NAIVE_DATETIME``.
    """
    if value is None:
        raise InvalidScratchInputError(field, InputProblem.REQUIRED)
    if not isinstance(value, datetime):
        raise InvalidScratchInputError(field, InputProblem.WRONG_TYPE)
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidScratchInputError(field, InputProblem.NAIVE_DATETIME)
    return value.astimezone(UTC)


def validate_bounded_int(
    field: str, value: object, *, minimum: int, maximum: int
) -> int:
    """Return ``value`` when it is an ``int`` with ``minimum <= value <= maximum``.

    ``None`` is ``REQUIRED``. ``bool``, ``float`` (even ``5.0``), ``str`` and
    everything else that is not an ``int`` is ``WRONG_TYPE``. Both bounds are
    inclusive; a value outside them is ``OUT_OF_RANGE``.
    """
    if value is None:
        raise InvalidScratchInputError(field, InputProblem.REQUIRED)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidScratchInputError(field, InputProblem.WRONG_TYPE)
    if not minimum <= value <= maximum:
        raise InvalidScratchInputError(field, InputProblem.OUT_OF_RANGE)
    return value


def validate_bool(field: str, value: object) -> bool:
    """Return ``value`` when it is a ``bool``.

    ``None`` is ``REQUIRED``. ``0``, ``1``, ``"true"`` and everything else is
    ``WRONG_TYPE``.
    """
    if value is None:
        raise InvalidScratchInputError(field, InputProblem.REQUIRED)
    if not isinstance(value, bool):
        raise InvalidScratchInputError(field, InputProblem.WRONG_TYPE)
    return value


def validate_outcome(value: object) -> PromotionOutcome:
    """Return ``value`` when it is a :class:`PromotionOutcome` member.

    The field name is ``"outcome"``. ``None`` is ``REQUIRED``. Anything else,
    including the plain string ``"promoted"`` and a member of another enum such
    as ``PromotionState.PROMOTED``, is ``WRONG_TYPE``.
    """
    if value is None:
        raise InvalidScratchInputError("outcome", InputProblem.REQUIRED)
    if not isinstance(value, PromotionOutcome):
        raise InvalidScratchInputError("outcome", InputProblem.WRONG_TYPE)
    return value


_METADATA = "source_metadata"


def _check_characters(field: str, text: str) -> None:
    """Reject text that PostgreSQL text / jsonb cannot store."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidScratchInputError(field, InputProblem.INVALID_CHARACTERS) from None
    if "\x00" in text:
        raise InvalidScratchInputError(field, InputProblem.INVALID_CHARACTERS)


def _check_text(field: str, value: object, max_chars: int) -> None:
    """The four checks of :func:`validate_optional_text`, in their order."""
    if not isinstance(value, str):
        raise InvalidScratchInputError(field, InputProblem.WRONG_TYPE)
    _check_characters(field, value)
    if not value.strip():
        raise InvalidScratchInputError(field, InputProblem.BLANK)
    if len(value) > max_chars:
        raise InvalidScratchInputError(field, InputProblem.TOO_LONG)


def _check_container_depth(depth: int) -> None:
    if depth > limits.MAX_SOURCE_METADATA_DEPTH:
        raise InvalidScratchInputError(_METADATA, InputProblem.TOO_DEEP)


def _copy_object(node: dict[Any, Any], depth: int) -> dict[str, Any]:
    _check_container_depth(depth)
    copy: dict[str, Any] = {}
    for key, child in node.items():
        _check_text(_METADATA, key, limits.MAX_SOURCE_METADATA_KEY_CHARS)
        copy[key] = _copy_value(child, depth + 1)
    return copy


def _copy_value(node: object, depth: int) -> Any:
    """Validate one JSON value and return a copy that shares nothing with it."""
    if isinstance(node, dict):
        return _copy_object(node, depth)
    if isinstance(node, list):
        _check_container_depth(depth)
        return [_copy_value(child, depth + 1) for child in node]
    if node is None or isinstance(node, bool):
        return node
    if isinstance(node, int):
        if abs(node) > limits.MAX_JSON_INT:
            raise InvalidScratchInputError(_METADATA, InputProblem.OUT_OF_RANGE)
        return node
    if isinstance(node, float):
        if not math.isfinite(node) or (
            node != 0
            and not (
                limits.MIN_JSON_FLOAT_MAGNITUDE
                <= abs(node)
                < limits.MAX_JSON_FLOAT_MAGNITUDE
            )
        ):
            raise InvalidScratchInputError(_METADATA, InputProblem.OUT_OF_RANGE)
        return node
    if isinstance(node, str):
        _check_characters(_METADATA, node)
        return node
    raise InvalidScratchInputError(_METADATA, InputProblem.WRONG_TYPE)
