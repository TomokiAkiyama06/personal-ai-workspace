"""Typed errors of Memory versioning and freshness (PAW-042).

Messages are fixed strings built from closed vocabularies (field names,
:class:`InputProblem` and :class:`StateProblem` values, authorization reasons of
``paw_backend.authz``). They never contain caller content (titles, memory text,
ids), driver messages or SQL, so they are safe to log and to map to an API
response. ``code`` is the stable machine-readable identifier. Database errors the
service does not handle propagate unchanged; their text can contain SQL
parameters, so a caller must never show ``str(error)`` of those to a user.
"""

from enum import StrEnum
from typing import ClassVar


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
