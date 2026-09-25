"""Enums and value objects of the Immediate Journal (PAW-041). No database, no I/O.

Sources (REQUIREMENTS.md "Immediate Journal / Background Consolidation",
docs/MEMORY_ARCHITECTURE.md section 18): a user message is saved at once as Raw
Conversation and as a Pending Observation, and a background worker turns it into
Memory candidates later. The order of events is the ``event_sequence`` of the
conversation, never the time a worker finished. The worker queue has the three
priorities HIGH / NORMAL / LOW.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID


class Priority(StrEnum):
    """Order in which the queue hands out work; it never interrupts a running job."""

    HIGH = "high"  # an explicit preference / decision, a setting the next turn needs
    NORMAL = "normal"  # ordinary consolidation
    LOW = "low"  # re-processing, re-embedding, repo refresh, history compression

    @property
    def rank(self) -> int:
        """0 for HIGH, 1 for NORMAL, 2 for LOW: a smaller rank is claimed first."""
        return PRIORITY_RANKS[self]


PRIORITY_RANKS = MappingProxyType(
    {Priority.HIGH: 0, Priority.NORMAL: 1, Priority.LOW: 2}
)


class EntryState(StrEnum):
    """Processing state of a journal entry (the Pending Observation)."""

    PENDING = "pending"  # saved; not turned into memory candidates yet
    CONSOLIDATED = "consolidated"  # a worker's output was applied (or found empty)


class JobStatus(StrEnum):
    QUEUED = "queued"  # waiting for a worker (or for its retry time)
    CLAIMED = "claimed"  # leased to a worker
    COMPLETED = "completed"  # the output was applied (terminal)
    DEAD = "dead"  # dead letter: too many failed attempts (terminal)


ACTIVE_JOB_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.CLAIMED})


class FailureKind(StrEnum):
    """Why the last attempt of a job failed (a closed set; never a message)."""

    WORKER_UNAVAILABLE = "worker_unavailable"  # GPU / worker service not reachable
    WORKER_TIMEOUT = "worker_timeout"  # the call did not end in time
    WORKER_ERROR = "worker_error"  # the worker raised something else
    WORKER_OUTPUT_INVALID = "worker_output_invalid"  # breaks the output contract
    APPLY_FAILED = "apply_failed"  # writing the candidates failed (rolled back)

    @property
    def counts_toward_dead_letter(self) -> bool:
        """An unavailable worker is not the job's fault, so it never dead-letters."""
        return self is not FailureKind.WORKER_UNAVAILABLE

    @property
    def waits_for_worker(self) -> bool:
        """Shown as "waiting for the GPU" rather than as a failure."""
        return self is FailureKind.WORKER_UNAVAILABLE


class WorkerScope(StrEnum):
    """``scope`` of ``memory-worker-output-v1``: what the worker recommends."""

    USER = "user"
    PROJECT = "project"
    REPO = "repo"
    SHARED = "shared"


class WorkerState(StrEnum):
    """``state`` of ``memory-worker-output-v1``: what the worker claims."""

    CONFIRMED = "confirmed"
    INFERRED = "inferred"


class ItemResult(StrEnum):
    """What the consolidator did with one memory of a worker's output."""

    CREATED = "created"  # a new memory, version 1
    UPDATED = "updated"  # a new version; the previous one is superseded
    DUPLICATE = "duplicate"  # the current version already says this
    STALE = "stale"  # a newer turn already produced the current version
    # Would replace a confirmed memory: the user decides.
    HELD_CONFIRMED = "held_confirmed"
    HELD_HIGH_RISK = "held_high_risk"  # a high-risk area: explicit confirmation first
    BLOCKED = "blocked_by_user"  # the user rejected or deleted this memory
    REFUSED_SHARED = "refused_shared"  # Shared Memory is never written automatically
    NO_CONTENT = "no_content"  # the worker gave no text to store
    DUPLICATE_KEY = "duplicate_key"  # the same key twice in one output: first wins

    @property
    def wrote_memory(self) -> bool:
        return self in (ItemResult.CREATED, ItemResult.UPDATED)


class RunOutcome(StrEnum):
    """What one ``Consolidator.run_once`` did."""

    IDLE = "idle"  # nothing claimable
    COMPLETED = "completed"  # a worker's output was applied
    ALREADY_DONE = "already_done"  # the observation was consolidated already
    WORKER_UNAVAILABLE = "worker_unavailable"  # deferred; the batch stops here
    RETRY_SCHEDULED = "retry_scheduled"  # failed; will be claimed again
    DEAD_LETTERED = "dead_lettered"  # failed for the last time
    LEASE_LOST = "lease_lost"  # another worker owns the job now; nothing was written


@dataclass(frozen=True, slots=True)
class JournalReceipt:
    """What ``record_user_message`` saved. Ids and numbers only."""

    entry_id: UUID
    message_id: UUID
    conversation_id: UUID
    turn_id: UUID
    event_sequence: int
    priority: Priority


@dataclass(frozen=True, slots=True)
class AppendedMessage:
    """What ``append_message`` saved (Raw Conversation only, no observation)."""

    message_id: UUID
    conversation_id: UUID
    turn_id: UUID
    event_sequence: int


@dataclass(frozen=True, slots=True)
class PendingObservation:
    """A user message not yet consolidated: what the next turn must not lose.

    ``content`` is the raw text of the message. It is left out of ``repr`` so
    that an accidental log line or traceback never prints a conversation.
    """

    entry_id: UUID
    conversation_id: UUID
    turn_id: UUID
    event_sequence: int
    recorded_at: datetime
    content: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SyncStatus:
    """The memory sync state of one conversation (the UI's four labels).

    ``synced``: nothing pending. ``consolidating``: queued or being worked on,
    nothing has failed. ``waiting_for_worker``: the worker (GPU) was not
    reachable; the job is queued for its next try. ``retrying``: an attempt failed
    and will be repeated. ``failed``: dead letter; it needs an operator or a new
    ``enqueue``, but the observation is kept.
    """

    consolidating: int
    waiting_for_worker: int
    retrying: int
    failed: int

    @property
    def pending(self) -> int:
        return (
            self.consolidating + self.waiting_for_worker + self.retrying + self.failed
        )

    @property
    def synced(self) -> bool:
        return self.pending == 0


@dataclass(frozen=True, slots=True)
class QueueJob:
    """A snapshot of one ``memory_consolidation_queue`` row.

    ``claim_count`` only ever grows: it is the claim generation (the fencing
    token) of a lease. The worker that received this snapshot from ``claim_next``
    passes it back, and every later call refuses another generation, also from
    the same worker id.
    """

    id: int
    entry_id: UUID
    priority: Priority
    status: JobStatus
    enqueued_at: datetime
    available_at: datetime
    attempts: int
    deferrals: int
    claim_count: int
    claimed_by: str | None
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    last_failure: FailureKind | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class RunResult:
    """The outcome of one ``run_once``: ids, closed codes and counts only."""

    outcome: RunOutcome
    job_id: int | None = None
    failure: FailureKind | None = None
    items: tuple[ItemResult, ...] = ()
