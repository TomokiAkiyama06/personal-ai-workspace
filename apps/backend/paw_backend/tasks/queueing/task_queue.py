"""The task queue: priority order, leases and safe concurrent claiming (PAW-033).

The database is the only source of truth (``queue_entries``): nothing is kept in
process memory, so any number of workers in any number of processes can call
the same methods concurrently. The queue does not read or change ``tasks.state``;
the orchestrator (PAW-034) pairs ``claim_next`` with the PAW-032 ``start``
command. It performs no authorisation and offers no HTTP endpoint.

Time. Every method takes ``now`` explicitly (a timezone-aware ``datetime``) so
that behaviour is deterministic and testable; the queue never reads the clock.

Order. Among the claimable entries the next one is chosen by, in this order:

1. ``priority_rank`` ascending (HIGH before NORMAL before LOW),
2. ``enqueued_at`` ascending (first in, first out),
3. ``id`` ascending (a tie of ``enqueued_at``).

The requirements state no aging or starvation rule, so there is none: a LOW
entry waits as long as HIGH / NORMAL entries keep arriving. A HIGH entry never
interrupts a running (claimed) entry; priority only decides the start order.

Claimable. An entry is claimable when its status is ``queued`` OR when its
status is ``claimed`` and ``lease_expires_at <= now`` (an expired lease: the
worker is presumed dead). A reclaimed entry keeps its ``priority`` and
``enqueued_at``, so it sorts where it always did.

Lease. A claim leases the entry to one worker until ``now + lease_seconds``. The
worker holds a VALID lease while ``status = claimed``, ``claimed_by`` is its id
and ``lease_expires_at > now`` (strictly: at the exact expiry instant the lease
is already lost). Only the holder of a valid lease may ``heartbeat``,
``release`` or ``complete``; anyone else gets ``LeaseLostError``. So at any
instant at most one worker holds a valid lease on an entry.

Row-lock semantics. ``claim_next`` must run in ONE transaction that selects the
best claimable row with ``FOR UPDATE SKIP LOCKED`` (``ORDER BY ... LIMIT 1``) and
then updates that row. A row that another transaction has locked is SKIPPED, not
waited for. Hence two racing claimers never receive the same entry, a claimer
never blocks behind another one, and if the only claimable entry is locked by
somebody else the call returns ``None`` immediately. ``heartbeat`` / ``release``
/ ``complete`` / ``cancel`` are single conditional ``UPDATE ... RETURNING``
statements (atomic by themselves).

Errors. Invalid arguments raise ``InvalidQueueingArgumentError(parameter)`` before
any database access. Messages never contain the argument values.
"""

import uuid
from datetime import datetime

from paw_backend.db import Database
from paw_backend.tasks.queueing.domain import Priority, QueueEntry
from paw_backend.tasks.queueing.validation import (
    DEFAULT_LEASE_SECONDS,
    MAX_LEASE_SECONDS,
    check_int,
)


class TaskQueue:
    def __init__(
        self, database: Database, *, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> None:
        """``lease_seconds``: 1 to ``MAX_LEASE_SECONDS`` (``int``, not ``bool``)."""
        self._database = database
        self._lease_seconds = check_int(
            "lease_seconds", lease_seconds, minimum=1, maximum=MAX_LEASE_SECONDS
        )

    @property
    def lease_seconds(self) -> int:
        return self._lease_seconds

    async def enqueue(
        self,
        task_id: uuid.UUID,
        *,
        now: datetime,
        priority: Priority = Priority.NORMAL,
    ) -> QueueEntry:
        """Put a task in the queue and return the new entry.

        The entry is ``queued`` with ``enqueued_at = now``, ``claim_count = 0``
        and no worker. ``priority_rank`` is ``priority.rank``.

        Raises ``TaskNotFoundError`` when no task has this id (foreign key
        violation, SQLSTATE 23503), ``TaskAlreadyQueuedError`` when the task
        already has a ``queued`` or ``claimed`` entry (violation of the unique
        index ``uq_queue_entries_one_active_per_task``, SQLSTATE 23505) and
        ``InvalidQueueingArgumentError`` for a ``task_id`` that is not a
        ``uuid.UUID``, a naive ``now`` or a ``priority`` that is not a
        ``Priority`` member (the string ``"high"`` is rejected). Any other
        ``IntegrityError`` must propagate unchanged.

        A task whose previous entry is ``completed`` or ``cancelled`` can be
        enqueued again (after Retry / Restart); that creates a NEW entry.
        """
        raise NotImplementedError("PAW-033 stub")

    async def claim_next(self, worker_id: str, now: datetime) -> QueueEntry | None:
        """Lease the next claimable entry to ``worker_id`` and return it, or ``None``.

        See the module docstring for the order, "claimable" and the row locks.
        The chosen row is updated to: ``status = claimed``,
        ``claimed_by = worker_id``, ``claimed_at = now``,
        ``lease_expires_at = now + lease_seconds``, ``claim_count + 1``.
        The returned ``QueueEntry`` shows the row after the update.

        ``worker_id``: see ``validation.check_worker_id``. A worker may hold
        several entries at once (limiting concurrency is not the queue's job).
        """
        raise NotImplementedError("PAW-033 stub")

    async def heartbeat(
        self, entry_id: int, worker_id: str, now: datetime
    ) -> QueueEntry:
        """Extend the lease of an entry the worker holds and return it.

        The new ``lease_expires_at`` is ``max(current, now + lease_seconds)``: a
        heartbeat never shortens a lease. Everything else stays unchanged.
        Raises ``LeaseLostError`` unless the worker holds a valid lease (see the
        module docstring).
        """
        raise NotImplementedError("PAW-033 stub")

    async def release(self, entry_id: int, worker_id: str, now: datetime) -> QueueEntry:
        """Give an entry back so that another (or the same) worker can claim it.

        The entry becomes ``queued`` again with ``claimed_by``, ``claimed_at`` and
        ``lease_expires_at`` cleared. ``priority``, ``enqueued_at`` (its place in
        the FIFO order) and ``claim_count`` are kept. Raises ``LeaseLostError``
        unless the worker holds a valid lease.
        """
        raise NotImplementedError("PAW-033 stub")

    async def complete(
        self, entry_id: int, worker_id: str, now: datetime
    ) -> QueueEntry:
        """Finish an entry: ``status = completed``, ``finished_at = now``.

        ``lease_expires_at`` is cleared; ``claimed_by`` and ``claimed_at`` are
        kept as history. A completed entry is never claimable again. Raises
        ``LeaseLostError`` unless the worker holds a valid lease.
        """
        raise NotImplementedError("PAW-033 stub")

    async def cancel(self, task_id: uuid.UUID, now: datetime) -> bool:
        """Cancel the task's active entry (``queued`` or ``claimed``), if any.

        Sets ``status = cancelled``, ``finished_at = now`` and clears
        ``lease_expires_at`` (``claimed_by`` / ``claimed_at`` are kept). A worker
        that held the entry loses its lease: its next ``heartbeat`` / ``release`` /
        ``complete`` raises ``LeaseLostError``. Returns ``True`` when an entry was
        cancelled, ``False`` when the task had no active entry (including an
        unknown task; that is not an error). Idempotent.
        """
        raise NotImplementedError("PAW-033 stub")
