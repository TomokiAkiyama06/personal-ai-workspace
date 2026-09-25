"""Validation of everything a caller passes to the repository module.

Strict on purpose: nothing is coerced (a ``bool`` is not an ``int``, the string
``"read"`` is not a :class:`RepoPermission`, a relative path is not made
absolute) and nothing that fails is echoed: the error names the field and a
closed :class:`InputProblem`, never the value.

The same functions check what git prints about an *untrusted* repository (a
branch name, a remote URL) before it is stored: a repository can say anything.
"""

import re
import unicodedata
import uuid
from collections.abc import Iterable

from paw_backend.authz.capabilities import RepoPermission
from paw_backend.authz.subjects import to_uuid
from paw_backend.repositories.errors import InputProblem, InvalidRepositoryInputError
from paw_backend.repositories.limits import (
    MAX_BRANCH_CHARS,
    MAX_LIST_LIMIT,
    MAX_LIST_OFFSET,
    MAX_NAME_CHARS,
    MAX_PATH_BYTES,
    MAX_PATH_CHARS,
    MAX_PURGE_PROJECTS,
    MAX_REMOTE_URL_CHARS,
    RAW_TEXT_FACTOR,
)
from paw_backend.repositories.models import REMOTE_SQL_PATTERN
from paw_backend.tools.scope import TargetError, normalise_path, normalise_remote

_REMOTE = re.compile(REMOTE_SQL_PATTERN)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
# What a bare control, format, private-use or unassigned character looks like:
# none of them belongs in a name, a branch or a path that is shown to people.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def _fail(field: str, problem: InputProblem) -> InvalidRepositoryInputError:
    return InvalidRepositoryInputError(field, problem)


def validate_uuid(field: str, value: object) -> uuid.UUID:
    """``value`` as a ``uuid.UUID``: a ``UUID`` or its canonical string only."""
    try:
        return to_uuid(value, field)
    except ValueError:
        raise _fail(field, InputProblem.NOT_A_UUID) from None


def _checked_text(field: str, value: object, limit: int) -> str:
    """``value`` if it is a bounded ``str`` without forbidden characters."""
    if not isinstance(value, str):
        raise _fail(field, InputProblem.NOT_A_STRING)
    if not value:
        raise _fail(field, InputProblem.EMPTY)
    if len(value) > limit * RAW_TEXT_FACTOR:
        raise _fail(field, InputProblem.TOO_LONG)
    for char in value:
        if unicodedata.category(char) in _FORBIDDEN_CATEGORIES:
            raise _fail(field, InputProblem.INVALID_CHARACTERS)
    return value


def validate_name(value: object, field: str = "name") -> str:
    """A repository name: 1 to 100 of ``A-Z a-z 0-9 . _ -``, starting alphanumeric.

    It must not end with ``.git`` (compared without case): the canonical name is
    the name without that suffix. The name becomes a directory name, so this is
    the whole "safe file name" rule: no separator, no leading dot, no ``..``.
    """
    text = _checked_text(field, value, MAX_NAME_CHARS)
    if len(text) > MAX_NAME_CHARS:
        raise _fail(field, InputProblem.TOO_LONG)
    if _NAME.fullmatch(text) is None or text.lower().endswith(".git"):
        raise _fail(field, InputProblem.INVALID_FORMAT)
    return text


def validate_branch(value: object, field: str = "branch") -> str:
    """A branch name: a conservative subset of git's ref-name rules.

    ``A-Z a-z 0-9 . _ - /`` only, starting alphanumeric (so it can never be read
    as an option), at most 200 characters, no ``..``, no empty segment, no segment
    that starts with ``.`` or ends with ``.lock``, no trailing ``.`` or ``/``.
    """
    text = _checked_text(field, value, MAX_BRANCH_CHARS)
    if len(text) > MAX_BRANCH_CHARS:
        raise _fail(field, InputProblem.TOO_LONG)
    if _BRANCH.fullmatch(text) is None or ".." in text or text.endswith((".", "/")):
        raise _fail(field, InputProblem.INVALID_FORMAT)
    for segment in text.split("/"):
        if not segment or segment.startswith(".") or segment.endswith(".lock"):
            raise _fail(field, InputProblem.INVALID_FORMAT)
    return text


def validate_bool(field: str, value: object) -> bool:
    """``True`` or ``False`` itself; ``0`` / ``1`` / strings are refused."""
    if not isinstance(value, bool):
        raise _fail(field, InputProblem.NOT_A_BOOL)
    return value


def validate_path_text(value: object, field: str = "path") -> str:
    """An absolute path in the canonical spelling of ``tools.scope.normalise_path``.

    At most 1024 characters **and** 2048 bytes in UTF-8 (``TOO_LONG`` otherwise).

    The text is not changed: a path that is not already canonical (``//``,
    ``/./``, a trailing ``/``) is refused, so what is checked later is exactly
    what the caller wrote. ``..``, ``~``, backslashes, percent-encoded
    separators, control characters and non-NFKC text are refused by the same
    function that the Tool Broker applies to a path.
    """
    text = _checked_text(field, value, MAX_PATH_CHARS)
    if len(text) > MAX_PATH_CHARS or len(text.encode()) > MAX_PATH_BYTES:
        # The stored path is bounded by its encoded length too (the unique index).
        raise _fail(field, InputProblem.TOO_LONG)
    if not text.startswith("/"):
        raise _fail(field, InputProblem.INVALID_FORMAT)
    try:
        canonical = normalise_path(text)
    except TargetError:
        raise _fail(field, InputProblem.INVALID_FORMAT) from None
    if canonical != text or canonical == "/":
        raise _fail(field, InputProblem.INVALID_FORMAT)
    return text


def validate_remote_url(value: object, field: str = "url") -> str:
    """An ``https`` URL in the canonical form the Tool Broker compares against.

    ``paw_backend.tools.scope.normalise_remote`` decides (no user information,
    no port, no query, a path on the host, nothing that climbs out of it) and the
    result is what is stored. Only ``https``: an ``http`` URL sends everything in
    clear text.
    """
    text = _checked_text(field, value, MAX_REMOTE_URL_CHARS)
    if len(text) > MAX_REMOTE_URL_CHARS:
        raise _fail(field, InputProblem.TOO_LONG)
    if not text.isascii() or not text[:8].lower() == "https://" or "#" in text:
        # A fragment would be dropped silently by the normaliser: refused instead.
        raise _fail(field, InputProblem.INVALID_FORMAT)
    try:
        canonical = normalise_remote(text)
    except TargetError:
        raise _fail(field, InputProblem.INVALID_FORMAT) from None
    if len(canonical) > MAX_REMOTE_URL_CHARS:
        raise _fail(field, InputProblem.TOO_LONG)
    if not is_storable_remote(canonical):
        raise _fail(field, InputProblem.INVALID_FORMAT)
    return canonical


def is_storable_remote(url: str) -> bool:
    """Whether the database accepts this (canonical) remote URL as it is."""
    return len(url) <= MAX_REMOTE_URL_CHARS and _REMOTE.fullmatch(url) is not None


def validate_permissions(
    value: object, field: str = "allowed"
) -> frozenset[RepoPermission] | None:
    """``None`` (inherit) or a collection of :class:`RepoPermission` members.

    A string is refused (also a permission's own value): ``"read"`` would be
    read as the letters ``r``, ``e``, ``a``, ``d`` by an iterating caller. An
    empty collection is an override that denies every permission.
    """
    if value is None:
        return None
    if isinstance(value, str | bytes) or not isinstance(value, Iterable):
        raise _fail(field, InputProblem.NOT_A_COLLECTION)
    members: set[RepoPermission] = set()
    for permission in value:
        if not isinstance(permission, RepoPermission):
            raise _fail(field, InputProblem.NOT_A_PERMISSION)
        members.add(permission)
    return frozenset(members)


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


def validate_project_ids(value: object, field: str = "project_ids") -> tuple:
    """A collection of at most 500 project UUIDs; duplicates collapse (order kept)."""
    if isinstance(value, str | bytes) or not isinstance(value, Iterable):
        raise _fail(field, InputProblem.NOT_A_COLLECTION)
    found: dict[uuid.UUID, None] = {}
    seen = 0
    for item in value:
        seen += 1
        if seen > MAX_PURGE_PROJECTS:  # counts duplicates too: bounds the loop
            raise _fail(field, InputProblem.TOO_MANY)
        found[validate_uuid(field, item)] = None
    return tuple(found)
