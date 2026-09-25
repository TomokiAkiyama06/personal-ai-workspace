"""Typed errors of the provenance store.

Messages are fixed strings built from closed vocabularies (field names and
:class:`InputProblem` values written in this code base). They never contain
caller-supplied content (claim texts, locators, titles, ids), driver messages or
SQL, so they are safe to log and to map to an API response. ``code`` is the
stable machine-readable identifier. Database errors that the store does not
handle (connection loss, timeouts) propagate unchanged; their text can contain
SQL parameters, so a caller must never show ``str(error)`` to a user.
"""

from enum import StrEnum
from typing import ClassVar


class InputProblem(StrEnum):
    """Why an argument was rejected. Closed set; never a caller's own text."""

    REQUIRED = "required"  # None where a value is required
    WRONG_TYPE = "wrong_type"  # a value of the wrong Python type
    BLANK = "blank"  # an empty or whitespace-only string
    TOO_LONG = "too_long"  # more characters than allowed
    TOO_MANY = "too_many"  # more elements than allowed
    EMPTY = "empty"  # a sequence without elements where one is required
    OUT_OF_RANGE = "out_of_range"  # a number or an instant outside its range
    INVALID_CHARACTERS = "invalid_characters"  # NUL or not encodable as UTF-8
    NAIVE_DATETIME = "naive_datetime"  # a datetime without a timezone
    INVALID_FORMAT = "invalid_format"  # a locator or hash that is malformed
    UNKNOWN_REFERENCE = (
        "unknown_reference"  # a task that is missing or not the project's
    )
    SELF_REFERENCE = "self_reference"  # a relation between something and itself
    CONFLICT = "conflict"  # contradictory values inside one argument


class ProvenanceError(Exception):
    """Base class of every error raised by the provenance store."""

    code: ClassVar[str] = "provenance_error"


class InvalidProvenanceInputError(ProvenanceError):
    """An argument was rejected. ``field`` names it, ``problem`` says why."""

    code = "invalid_provenance_input"

    def __init__(self, field: str, problem: InputProblem) -> None:
        self.field = field
        self.problem = problem
        super().__init__(f"Invalid {field}: {problem.value}")


class ClaimNotFoundError(ProvenanceError):
    """No claim with this id in this project.

    Raised for a missing id and for an id that belongs to another project; the
    two are deliberately indistinguishable.
    """

    code = "claim_not_found"

    def __init__(self) -> None:
        super().__init__("Claim not found")


class SourceNotFoundError(ProvenanceError):
    """No source with this id in this project (same rule as claims)."""

    code = "source_not_found"

    def __init__(self) -> None:
        super().__init__("Source not found")


class ProvenanceConflictError(ProvenanceError):
    """The change contradicts what is already recorded.

    The same claim and source are already linked with the other stance, or the
    same two claims (or sources) are already related in the other way.
    Recorded evidence is never overwritten, so nothing was changed.
    """

    code = "provenance_conflict"

    def __init__(self) -> None:
        super().__init__("The record conflicts with existing provenance")


class ProvenanceLimitError(ProvenanceError):
    """The claim would have more source links than ``MAX_SOURCES_PER_CLAIM``.

    Nothing was changed (the whole call is rolled back).
    """

    code = "provenance_limit"

    def __init__(self) -> None:
        super().__init__("The claim would have too many sources")


class ProvenanceBusyError(ProvenanceError):
    """A lock could not be obtained within the store's lock timeout, or the
    database resolved a deadlock against this call.

    Nothing was changed; the caller may retry.
    """

    code = "provenance_busy"

    def __init__(self) -> None:
        super().__init__("Provenance store is busy; retry later")
