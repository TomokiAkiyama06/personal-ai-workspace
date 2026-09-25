"""Immediate Journal and background consolidation of Memory (PAW-041).

The synchronous half, ``MemoryJournal``: a user message is saved as Raw
Conversation and as a Pending Observation in one transaction, with an event
sequence assigned under a row lock, and its consolidation is queued. The
asynchronous half, ``Consolidator`` with ``ConsolidationQueue``: a background
Memory Worker (a ``MemoryWorker``: the contract of the PAW-018 benchmark) turns
the observation into candidate memory versions, through the rules of the Memory
schema (PAW-040), in priority order (HIGH / NORMAL / LOW) and in event order, and
an unavailable GPU never loses an observation. See ``apps/backend/README.md``
("Immediate Journal / Background Consolidation") and Decision 0018.

There are no HTTP endpoints: the chat backend calls ``MemoryJournal``, and a worker
process runs ``Consolidator.run_batch`` on a schedule (the process itself is not
part of this issue).
"""

from paw_backend.memory.journal import models
from paw_backend.memory.journal.consolidator import Consolidator
from paw_backend.memory.journal.domain import (
    AppendedMessage,
    EntryState,
    FailureKind,
    ItemResult,
    JobStatus,
    JournalReceipt,
    PendingObservation,
    Priority,
    QueueJob,
    RunOutcome,
    RunResult,
    SyncStatus,
    WorkerScope,
    WorkerState,
)
from paw_backend.memory.journal.errors import (
    ConversationNotFoundError,
    EntryNotFoundError,
    EntryNotPendingError,
    InputProblem,
    InvalidJournalInputError,
    JournalBusyError,
    JournalError,
    JournalPermissionError,
    LeaseLostError,
    OutputProblem,
    WorkerOutputError,
    WorkerUnavailableError,
)
from paw_backend.memory.journal.queue import ConsolidationQueue
from paw_backend.memory.journal.rules import Backoff
from paw_backend.memory.journal.service import MemoryJournal
from paw_backend.memory.journal.worker import (
    MemoryWorker,
    WorkerMemory,
    parse_worker_output,
)

__all__ = [
    "AppendedMessage",
    "Backoff",
    "ConsolidationQueue",
    "Consolidator",
    "ConversationNotFoundError",
    "EntryNotFoundError",
    "EntryNotPendingError",
    "EntryState",
    "FailureKind",
    "InputProblem",
    "InvalidJournalInputError",
    "ItemResult",
    "JobStatus",
    "JournalBusyError",
    "JournalError",
    "JournalPermissionError",
    "JournalReceipt",
    "LeaseLostError",
    "MemoryJournal",
    "MemoryWorker",
    "OutputProblem",
    "PendingObservation",
    "Priority",
    "QueueJob",
    "RunOutcome",
    "RunResult",
    "SyncStatus",
    "WorkerMemory",
    "WorkerOutputError",
    "WorkerScope",
    "WorkerState",
    "WorkerUnavailableError",
    "models",
    "parse_worker_output",
]
