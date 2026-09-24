"""Typed errors of Shared Memory administration.

Messages are fixed strings built from closed vocabularies (field names,
:class:`InputProblem` and :class:`StateProblem` values, authorization reasons
of ``paw_backend.authz``). They never contain caller-supplied content (titles,
memory text, ids, policy text), driver messages or SQL, so they are safe to log
and to map to an API response. ``code`` is the stable machine-readable
identifier. Database errors the service does not handle (connection loss and so
on) propagate unchanged; their text can contain SQL parameters, so a caller must
never show ``str(error)`` of those to a user.
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
    OUT_OF_RANGE = "out_of_range"  # a number outside its allowed range
    INVALID_CHARACTERS = "invalid_characters"  # NUL or not encodable as UTF-8
    INVALID_FORMAT = "invalid_format"  # does not match the required pattern
    NAIVE_DATETIME = "naive_datetime"  # a datetime without a timezone
    DUPLICATE = "duplicate"  # the same key twice (policy ids)


class StateProblem(StrEnum):
    """Which state rule an operation ran into. Closed set."""

    DELETED = "deleted"  # an edit of a deleted shared memory
    ALREADY_DELETED = "already_deleted"  # a delete of a deleted shared memory
    NOT_DELETED = "not_deleted"  # a restore of a shared memory that is not deleted
    CANDIDATE_NOT_PENDING = "candidate_not_pending"  # already approved or rejected


class SharedMemoryError(Exception):
    """Base class of every error raised by Shared Memory administration."""

    code: ClassVar[str] = "shared_memory_error"


class InvalidSharedMemoryInputError(SharedMemoryError):
    """An argument was rejected. ``field`` names it, ``problem`` says why."""

    code = "invalid_shared_memory_input"

    def __init__(self, field: str, problem: InputProblem) -> None:
        self.field = field
        self.problem = problem
        super().__init__(f"Invalid {field}: {problem.value}")


class SharedMemoryPermissionError(SharedMemoryError):
    """The actor may not do this. ``reason`` is the stable authorization reason."""

    code = "shared_memory_forbidden"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Not allowed: {reason}")


class AutomaticPromotionRefusedError(SharedMemoryPermissionError):
    """An agent or the backend's own identity tried to manage Shared Memory.

    Creating, editing, deleting, restoring and approving or rejecting a
    candidate is a decision of a human Owner or Admin. An agent (a delegated
    grant) and the ``system`` role (a background worker) never make it, whatever
    an authorization policy says.
    """

    code = "automatic_promotion_refused"


class SharedMemoryNotFoundError(SharedMemoryError):
    """No visible shared memory with this id.

    Raised for a missing id, an id of a memory that is not Shared Memory, and
    (for a caller who may not see deleted ones) a deleted memory. The cases are
    deliberately indistinguishable.
    """

    code = "shared_memory_not_found"

    def __init__(self) -> None:
        super().__init__("Shared memory not found")


class CandidateNotFoundError(SharedMemoryError):
    """No shared memory candidate with this id."""

    code = "shared_memory_candidate_not_found"

    def __init__(self) -> None:
        super().__init__("Shared memory candidate not found")


class SharedMemoryStateError(SharedMemoryError):
    """The operation is not allowed in the current state (see :class:`StateProblem`)."""

    code = "shared_memory_state_conflict"

    def __init__(self, problem: StateProblem) -> None:
        self.problem = problem
        super().__init__(f"State does not allow this: {problem.value}")


class SharedMemoryVersionConflictError(SharedMemoryError):
    """An edit was made on top of a version that is not the current one."""

    code = "shared_memory_version_conflict"

    def __init__(self, expected_version: int, current_version: int) -> None:
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__("The shared memory changed since the version you edited")


class CandidateLimitError(SharedMemoryError):
    """The proposer already has the maximum number of pending candidates."""

    code = "shared_memory_candidate_limit"

    def __init__(self) -> None:
        super().__init__("Too many pending candidates")


class SharedMemoryBusyError(SharedMemoryError):
    """A lock could not be obtained within the service's lock timeout.

    Nothing was changed; the caller may retry.
    """

    code = "shared_memory_busy"

    def __init__(self) -> None:
        super().__init__("Shared memory is busy; retry later")


class PolicySourceError(SharedMemoryError):
    """The System Security Policy could not be loaded, so nothing is returned.

    The effective view fails closed: without the policy the precedence rule
    cannot be applied, and Shared Memory must never be shown without it.
    """

    code = "policy_source_unavailable"

    def __init__(self) -> None:
        super().__init__("System policy is unavailable")


class SharedMemoryDataError(SharedMemoryError):
    """A stored shared memory does not have the documented format."""

    code = "shared_memory_data_invalid"

    def __init__(self) -> None:
        super().__init__("A stored shared memory is malformed")


class RulesContractError(SharedMemoryError):
    """A rule function returned something its contract does not allow."""

    code = "shared_memory_rules_contract"

    def __init__(self, rule: str) -> None:
        self.rule = rule
        super().__init__(f"Rule {rule} broke its contract")
