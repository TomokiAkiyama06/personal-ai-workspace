"""Typed errors of the Research Scratch Store.

Messages are fixed strings built from closed vocabularies (field names and
:class:`InputProblem` values written in this code base). They never contain
caller-supplied content (queries, titles, summaries, metadata, ids), driver
messages or SQL, so they are safe to log and to map to an API response. ``code``
is the stable machine-readable identifier. Database errors that the store does
not handle (connection loss, timeouts) propagate unchanged.
"""

from enum import StrEnum
from typing import ClassVar


class InputProblem(StrEnum):
    """Why an argument was rejected. Closed set; never a caller's own text."""

    REQUIRED = "required"  # None where a value is required
    WRONG_TYPE = "wrong_type"  # a value of the wrong Python / JSON type
    BLANK = "blank"  # an empty or whitespace-only string
    TOO_LONG = "too_long"  # more characters than allowed
    TOO_LARGE = "too_large"  # more bytes than allowed
    TOO_DEEP = "too_deep"  # nested deeper than allowed
    OUT_OF_RANGE = "out_of_range"  # a number outside its allowed range
    INVALID_CHARACTERS = "invalid_characters"  # NUL or not encodable as UTF-8
    NAIVE_DATETIME = "naive_datetime"  # a datetime without a timezone
    UNKNOWN_REFERENCE = (
        "unknown_reference"  # a task that is missing or not the project's
    )


class ScratchError(Exception):
    """Base class of every error raised by the Research Scratch Store."""

    code: ClassVar[str] = "scratch_error"


class InvalidScratchInputError(ScratchError):
    """An argument was rejected. ``field`` names it, ``problem`` says why."""

    code = "invalid_scratch_input"

    def __init__(self, field: str, problem: InputProblem) -> None:
        self.field = field
        self.problem = problem
        super().__init__(f"Invalid {field}: {problem.value}")


class ScratchItemNotFoundError(ScratchError):
    """No visible item with this id in this project.

    Raised for a missing id, an id that belongs to another project, and an item
    whose TTL ended without an exemption (it is gone even if the purge has not
    run yet). The three cases are deliberately indistinguishable.
    """

    code = "scratch_item_not_found"

    def __init__(self) -> None:
        super().__init__("Scratch item not found")


class ScratchStateError(ScratchError):
    """The promotion request or resolution is not allowed in the item's state."""

    code = "scratch_state_conflict"

    def __init__(self) -> None:
        super().__init__("Scratch item is in a state that does not allow this")


class ScratchLeaseLimitError(ScratchError):
    """Too many holders use the item at the same time."""

    code = "scratch_lease_limit"

    def __init__(self) -> None:
        super().__init__("Scratch item has too many active leases")


class ScratchBusyError(ScratchError):
    """A row lock could not be obtained within the store's lock timeout.

    Another transaction holds the item (or task) row for too long. Nothing was
    changed; the caller may retry.
    """

    code = "scratch_busy"

    def __init__(self) -> None:
        super().__init__("Scratch item is busy; retry later")
