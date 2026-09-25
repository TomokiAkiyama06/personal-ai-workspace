"""Typed errors of the Immediate Journal and the background consolidation (PAW-041).

Messages are fixed strings built from closed vocabularies (argument names,
:class:`InputProblem`, :class:`OutputProblem`, authorization reasons). They never
contain a caller's text, a message, a key or a content of a memory, an id, a
driver message or SQL: a raw conversation must not reach a log or an API response
through an error. ``code`` is the stable machine-readable identifier. Database
errors the services do not handle propagate unchanged; their text can contain SQL
parameters, so a caller must never show ``str(error)`` of those to a user.
"""

from enum import StrEnum
from typing import ClassVar


class InputProblem(StrEnum):
    """Why an argument was rejected. Closed set; never a caller's own text."""

    REQUIRED = "required"  # None where a value is required
    WRONG_TYPE = "wrong_type"
    BLANK = "blank"  # empty or whitespace only
    TOO_LONG = "too_long"
    TOO_MANY = "too_many"
    OUT_OF_RANGE = "out_of_range"
    INVALID_CHARACTERS = "invalid_characters"  # NUL, or not encodable as UTF-8
    INVALID_FORMAT = "invalid_format"
    UNKNOWN_VALUE = "unknown_value"  # not a member of the enum


class OutputProblem(StrEnum):
    """Why a Memory Worker's output broke the contract. Closed set."""

    NOT_TEXT = "not_text"  # the worker returned something other than a str
    TOO_LARGE = "too_large"  # more characters than ``MAX_RAW_OUTPUT_CHARS``
    NOT_JSON = "not_json"  # not JSON, a duplicate member name, NaN, too deep, ...
    NOT_AN_OBJECT = "not_an_object"
    UNKNOWN_FIELD = "unknown_field"  # a misspelled or unsupported member name
    MISSING_FIELD = "missing_field"
    WRONG_TYPE = "wrong_type"
    INVALID_VALUE = "invalid_value"  # an enum or a text that is not allowed
    TOO_MANY = "too_many"
    DUPLICATE = "duplicate"  # the same conflict key twice (``uniqueItems``)


class JournalError(Exception):
    """Base class of every error raised by ``paw_backend.memory.journal``."""

    code: ClassVar[str] = "journal_error"


class InvalidJournalInputError(JournalError):
    """An argument was rejected. ``field`` names it, ``problem`` says why."""

    code = "invalid_journal_input"

    def __init__(self, field: str, problem: InputProblem) -> None:
        self.field = field
        self.problem = problem
        super().__init__(f"Invalid {field}: {problem.value}")


class JournalPermissionError(JournalError):
    """The actor may not do this. ``reason`` is the stable authorization reason."""

    code = "journal_permission_denied"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Not permitted: {reason}")


class ConversationNotFoundError(JournalError):
    """No conversation with this id belongs to the actor.

    A conversation of another user is "not found" too: the two cases are not
    distinguished, so the error reveals nothing about other users.
    """

    code = "conversation_not_found"

    def __init__(self) -> None:
        super().__init__("Conversation not found")


class EntryNotFoundError(JournalError):
    """No journal entry has this id (or its conversation was deleted)."""

    code = "journal_entry_not_found"

    def __init__(self) -> None:
        super().__init__("Journal entry not found")


class EntryNotPendingError(JournalError):
    """The observation is already consolidated: there is nothing to enqueue."""

    code = "journal_entry_not_pending"

    def __init__(self) -> None:
        super().__init__("Journal entry is not pending")


class JournalBusyError(JournalError):
    """A lock was not granted within ``lock_timeout_ms``; nothing changed, try again."""

    code = "journal_busy"

    def __init__(self) -> None:
        super().__init__("Journal is busy; try again")


class LeaseLostError(JournalError):
    """The worker does not hold a valid lease on the job.

    Raised when the job does not exist (its conversation was deleted), is not
    claimed, is claimed by another worker, has been claimed again since the
    caller's claim (another ``claim_count``, also by the same worker id), or the
    worker's lease has expired. The cases are not distinguished, so the error
    reveals nothing about other workers.
    """

    code = "journal_lease_lost"

    def __init__(self) -> None:
        super().__init__("Job is not leased to this worker")


class WorkerUnavailableError(JournalError):
    """Raised by a Memory Worker adapter when the worker cannot be reached.

    The GPU service is stopped, the Kaggle session is gone, the connection is
    refused. It is the contract of ``MemoryWorker.extract`` for "not now": the
    consolidator keeps the job queued, defers it with a growing delay and does
    NOT count it toward the dead letter, so consolidation resumes when the worker
    returns. (The built-in ``ConnectionError`` is read the same way.) Raise no
    text of the failure: the type is what the consolidator uses.
    """

    code = "memory_worker_unavailable"

    def __init__(self) -> None:
        super().__init__("Memory worker is unavailable")


class WorkerOutputError(JournalError):
    """A Memory Worker returned output that breaks ``memory-worker-output-v1``.

    ``problem`` says which rule (closed vocabulary); the output itself is never
    part of the error, because it is derived from a raw conversation.
    """

    code = "memory_worker_output_invalid"

    def __init__(self, problem: OutputProblem) -> None:
        self.problem = problem
        super().__init__(f"Invalid worker output: {problem.value}")
