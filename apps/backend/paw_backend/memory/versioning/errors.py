"""Typed errors of Memory versioning and freshness (PAW-042).

Messages are fixed strings built from closed vocabularies (field names,
:class:`InputProblem` and :class:`StateProblem` values, authorization reasons of
``paw_backend.authz``). They never contain caller content (titles, memory text,
ids), driver messages or SQL, so they are safe to log and to map to an API
response. ``code`` is the stable machine-readable identifier.

A database error is never passed on as it is: the text of SQLAlchemy's
``StatementError`` / ``DBAPIError`` carries the bound parameters (a title, a
content), and PostgreSQL's ``DETAIL`` can quote the failing row. Every public
method of ``MemoryVersioningService`` and ``FreshnessMaintenance`` turns it into
:class:`MemoryBusyError` (a lock timed out), :class:`MemoryVersionConflictError` /
:class:`MemoryStateError` (a known race), or :class:`MemoryDatabaseError`
(anything else), and detaches the original (:func:`raise_detached`): neither
``__cause__`` nor ``__context__`` refers to it, so no traceback, log or ``repr``
of the chain can show it.
"""

import re
from enum import StrEnum
from typing import ClassVar, NoReturn

_SQLSTATE = re.compile(r"[0-9A-Z]{5}")


class InputProblem(StrEnum):
    """Why an argument was rejected. Closed set; never a caller's own text."""

    REQUIRED = "required"  # None (or nothing at all) where a value is required
    WRONG_TYPE = "wrong_type"  # a value of the wrong Python type
    BLANK = "blank"  # empty or whitespace-only text
    TOO_LONG = "too_long"  # more characters than allowed
    TOO_MANY = "too_many"  # more elements than allowed
    OUT_OF_RANGE = "out_of_range"  # a number or a time outside its allowed range
    INVALID_CHARACTERS = "invalid_characters"  # NUL or not encodable as UTF-8
    INVALID_FORMAT = "invalid_format"  # does not match the required pattern
    NAIVE_DATETIME = "naive_datetime"  # a datetime without a timezone
    NOT_ALLOWED = "not_allowed"  # a value this operation or policy does not take


class StateProblem(StrEnum):
    """Which state rule an operation ran into. Closed set."""

    NOT_ACTIVE = "not_active"  # the current version is not ``active``
    ALREADY_ACTIVE = "already_active"  # a restore that would change nothing
    NOT_REVALIDATABLE = "not_revalidatable"  # not a ``revalidate`` memory
    SAME_MEMORY = "same_memory"  # a relation of a memory to itself
    SCOPE_MISMATCH = "scope_mismatch"  # supersedes across different audiences
    WOULD_CYCLE = "would_cycle"  # the relation would make the history graph cyclic
    ALREADY_RELATED = "already_related"  # the same (or the reverse) relation exists
    UNKNOWN_VERSION = "unknown_version"  # a restore from a version that is not there


class MemoryVersioningError(Exception):
    """Base class of every error raised by this package."""

    code: ClassVar[str] = "memory_versioning_error"


class InvalidMemoryInputError(MemoryVersioningError):
    """An argument was rejected. ``field`` names it, ``problem`` says why."""

    code = "invalid_memory_input"

    def __init__(self, field: str, problem: InputProblem) -> None:
        self.field = field
        self.problem = problem
        super().__init__(f"Invalid {field}: {problem.value}")


class MemoryPermissionError(MemoryVersioningError):
    """The actor may not do this. ``reason`` is the stable authorization reason."""

    code = "memory_forbidden"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Not allowed: {reason}")


class MemoryNotFoundError(MemoryVersioningError):
    """No memory with this id that this service lets the actor change.

    Raised for a missing id, somebody else's private memory, a project memory of a
    project the actor is not a member of, and a repository or project-group memory
    (not handled here, see Decision 0034). The cases are deliberately
    indistinguishable.
    """

    code = "memory_not_found"

    def __init__(self) -> None:
        super().__init__("Memory not found")


class MemoryScopeNotSupportedError(MemoryVersioningError):
    """Shared Memory is changed by ``SharedMemoryService`` (PAW-046), never here."""

    code = "memory_scope_not_supported"

    def __init__(self) -> None:
        super().__init__("This memory is managed elsewhere")


class MemoryStateError(MemoryVersioningError):
    """The operation is not allowed in the current state (see :class:`StateProblem`)."""

    code = "memory_state_conflict"

    def __init__(self, problem: StateProblem) -> None:
        self.problem = problem
        super().__init__(f"State does not allow this: {problem.value}")


class MemoryVersionConflictError(MemoryVersioningError):
    """The change was made on top of a version that is not the current one.

    The Optimistic Lock of REQUIREMENTS.md "Concurrent Editing": nothing was
    written; the caller shows the difference and lets the person reload or merge.
    """

    code = "memory_version_conflict"

    def __init__(self, expected_version: int, current_version: int) -> None:
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__("The memory changed since the version you edited")


class MemoryBusyError(MemoryVersioningError):
    """A lock could not be obtained within the lock timeout. Nothing was changed."""

    code = "memory_busy"

    def __init__(self) -> None:
        super().__init__("Memory is busy; retry later")


class MemoryDatabaseError(MemoryVersioningError):
    """The database refused or failed the operation; nothing was changed.

    The transaction was rolled back. ``sqlstate`` is PostgreSQL's five-character
    SQLSTATE code when the driver reported one (``None`` otherwise): a closed code
    (``23514`` a check violation, ``42501`` a missing privilege, ...) without any
    value of the row. The driver's message, the SQL and its parameters are not kept.
    """

    code = "memory_database_error"

    def __init__(self, sqlstate: str | None = None) -> None:
        if not (isinstance(sqlstate, str) and _SQLSTATE.fullmatch(sqlstate)):
            sqlstate = None
        self.sqlstate = sqlstate
        super().__init__(
            "Database error" if sqlstate is None else f"Database error: {sqlstate}"
        )


def raise_detached(error: MemoryVersioningError) -> NoReturn:
    """Raise ``error`` with no link to the exception being handled.

    ``raise ... from None`` clears ``__cause__`` but still sets ``__context__`` to
    the original (only its display is suppressed), so the original, with its SQL
    parameters, would stay reachable from the new error. The context is cleared
    after the raise and the error re-raised as it is (a bare ``raise`` does not
    chain).
    """
    try:
        raise error from None
    except MemoryVersioningError as clean:
        clean.__context__ = None
        raise
