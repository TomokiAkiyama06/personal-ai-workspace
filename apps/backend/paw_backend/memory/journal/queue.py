"""The background consolidation queue: priority, leases, retry, dead letter (PAW-041).

The database is the only source of truth (``memory_consolidation_queue``): nothing
is kept in process memory, so any number of workers in any number of processes can
call the same methods concurrently. The pattern is the task queue's (PAW-033,
Decision 0007); what differs is the retry and the dead letter. The queue performs
no authorisation and offers no endpoint: it is called by the consolidator (an
internal component) and, to put an observation back after a dead letter, by an
operator.

Order. Among the claimable jobs the next one is chosen by ``priority_rank``
(HIGH before NORMAL before LOW), then ``enqueued_at`` (first in, first out), then
``id``. A job that was deferred or retried keeps its ``priority`` and
``enqueued_at``, so it sorts where it always did once its delay has passed. There
is no aging: a LOW job waits as long as HIGH and NORMAL jobs keep arriving (the
requirements state no starvation rule). A HIGH job never interrupts a running one.

Claimable. A job is claimable when it is ``queued`` and ``available_at <= now``
(a retry delay has passed), or ``claimed`` and ``lease_expires_at <= now`` (an
expired lease: the worker is presumed dead). ``now`` is the database clock (see
``sql.py``).

Lease and fencing. A claim leases the job to one worker until ``now +
lease_seconds``. ``claim_count``, which every claim (also a reclaim) increases by
one and nothing decreases, is the FENCING TOKEN of the lease: ``claim_next``
returns it in the :class:`QueueJob`, and ``heartbeat`` / ``fail`` /
``dead_letter`` (and the consolidator's completion) REQUIRE it. A worker whose
lease expired and whose job was claimed again, even by the same worker id, holds
a dead generation: its calls raise :class:`LeaseLostError` and change nothing.
The holder of a valid lease is judged after the row lock is held (a statement that
waited for the lock must not let an expired lease pass): the row is locked first
and the lease is judged in the next statement (``lock_held``).

Row-lock semantics. ``claim_next`` selects the best claimable row with ``FOR
UPDATE SKIP LOCKED`` and updates it in one transaction: two racing claimers never
receive the same job, and a claimer never waits behind another.

Retry and dead letter. ``fail`` puts the job back in the queue with a delay
(``Backoff``), or dead-letters it. A failure that counts (``FailureKind.
counts_toward_dead_letter``: a timeout, an invalid output, an error of the worker
or of the write) increases ``attempts``; the ``max_attempts``-th such failure sets
``status = 'dead'``. A worker that is unavailable (the GPU is stopped) increases
``deferrals`` instead, which only lengthens the delay and never dead-letters: the
job waits for the worker to return. A claim of an EXPIRED lease counts as a failed
attempt too (``attempts + 1``, ``last_failure = 'worker_error'``): a job that keeps
killing its worker must end in the dead letter, not loop for ever. A dead job
leaves its journal entry ``pending``; ``enqueue`` puts it back.

Idempotent enqueue. An entry has at most one active (``queued`` or ``claimed``) job
(a partial unique index): ``enqueue`` of an entry that has one returns it.

Indexes. Finished and dead jobs are kept, and the claim index and the uniqueness
index are partial (``WHERE status IN ('queued', 'claimed')``); the statuses are
written into the SQL text (``sql.inlined``) so that a cached generic plan still
uses them (``tests/test_journal_queue.py`` plans every query in both modes).
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Row,
    and_,
    case,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.db import Database
from paw_backend.memory.journal import limits
from paw_backend.memory.journal.domain import (
    ACTIVE_JOB_STATUSES,
    EntryState,
    FailureKind,
    JobStatus,
    Priority,
    QueueJob,
)
from paw_backend.memory.journal.errors import (
    EntryNotFoundError,
    EntryNotPendingError,
    InputProblem,
    InvalidJournalInputError,
    LeaseLostError,
)
from paw_backend.memory.journal.models import ConsolidationJob, JournalEntry
from paw_backend.memory.journal.rules import Backoff
from paw_backend.memory.journal.sql import inlined, transaction
from paw_backend.memory.journal.validation import (
    validate_claim_count,
    validate_enum,
    validate_int,
    validate_job_id,
    validate_uuid,
    validate_worker_id,
)

_JOB = ConsolidationJob.__table__
_ENTRY = JournalEntry.__table__


def _queued() -> ColumnElement:
    return inlined("status_queued", JobStatus.QUEUED.value)


def _claimed() -> ColumnElement:
    return inlined("status_claimed", JobStatus.CLAIMED.value)


def _active() -> ColumnElement[bool]:
    """``status IN ('queued', 'claimed')``: the condition of both partial indexes."""
    return _JOB.c.status.in_(
        [inlined(f"status_{s.value}", s.value) for s in sorted(ACTIVE_JOB_STATUSES)]
    )


def _job(row: Row) -> QueueJob:
    return QueueJob(
        id=row.id,
        entry_id=row.entry_id,
        priority=Priority(row.priority),
        status=JobStatus(row.status),
        enqueued_at=row.enqueued_at,
        available_at=row.available_at,
        attempts=row.attempts,
        deferrals=row.deferrals,
        claim_count=row.claim_count,
        claimed_by=row.claimed_by,
        claimed_at=row.claimed_at,
        lease_expires_at=row.lease_expires_at,
        last_failure=FailureKind(row.last_failure) if row.last_failure else None,
        finished_at=row.finished_at,
    )


def _database_now() -> ColumnElement[datetime]:
    """The database clock as a scalar subquery of a CTE the statement then carries.

    ONE reading per statement: a CTE with a volatile function is evaluated once
    however often it is referenced, so the instants of one statement agree (for
    example ``claimed_at`` and the lease end are exactly ``lease_seconds`` apart).
    """
    sample = select(func.clock_timestamp().label("ts")).cte("clock")
    return select(sample.c.ts).scalar_subquery()


async def insert_job(session: AsyncSession, entry_id: UUID, priority: Priority) -> Row:
    """Insert the entry's job, or return its active one. The caller's transaction.

    Raises :class:`EntryNotPendingError` if the entry's job finished between the
    conflict and the read (the entry was consolidated meanwhile).
    """
    insert = (
        pg_insert(_JOB)
        .values(entry_id=entry_id, priority=priority.value, priority_rank=priority.rank)
        .on_conflict_do_nothing(
            index_elements=[_JOB.c.entry_id],
            index_where=text("status IN ('queued', 'claimed')"),
        )
        .returning(*_JOB.c)
    )
    row = (await session.execute(insert)).first()
    if row is None:  # the entry has an active job already: enqueueing is idempotent
        row = (
            await session.execute(
                select(*_JOB.c).where(_JOB.c.entry_id == entry_id, _active())
            )
        ).first()
    if row is None:
        raise EntryNotPendingError
    return row


async def lock_held(
    session: AsyncSession, job_id: int, worker_id: str, claim_count: int
) -> Row:
    """Lock the job and prove that ``worker_id`` holds a valid lease of this generation.

    Returns the locked row's ``attempts`` and ``deferrals``. Raises
    :class:`LeaseLostError` when the job does not exist, is not claimed, is claimed
    by another worker, was claimed again since (another ``claim_count``) or its
    lease has expired. The row is locked FIRST, in its own statement, and the lease
    is judged in the next one: an ``UPDATE`` judges its ``WHERE`` before it waits
    for a row lock, so a single statement would accept a lease that ran out while
    it waited. The transaction keeps the lock, so nobody else can claim the job
    until it ends.
    """
    locked = (
        await session.execute(
            select(_JOB.c.attempts, _JOB.c.deferrals)
            .where(_JOB.c.id == job_id)
            .with_for_update()
        )
    ).first()
    if locked is None:
        raise LeaseLostError
    valid = (
        await session.execute(
            select(
                and_(
                    _JOB.c.status == _claimed(),
                    _JOB.c.claimed_by == worker_id,
                    _JOB.c.claim_count == claim_count,
                    _JOB.c.lease_expires_at > func.clock_timestamp(),
                )
            ).where(_JOB.c.id == job_id)
        )
    ).scalar_one()
    if not valid:
        raise LeaseLostError
    return locked


async def complete_job(session: AsyncSession, job_id: int) -> None:
    """Mark a job whose lease ``lock_held`` proved in this transaction as completed."""
    await session.execute(
        update(_JOB)
        .where(_JOB.c.id == job_id)
        .values(
            status=JobStatus.COMPLETED.value,
            finished_at=func.clock_timestamp(),
            lease_expires_at=None,
        )
    )


class ConsolidationQueue:
    def __init__(
        self,
        database: Database,
        *,
        lease_seconds: int = limits.DEFAULT_LEASE_SECONDS,
        max_attempts: int = limits.DEFAULT_MAX_ATTEMPTS,
        backoff: Backoff | None = None,
        lock_timeout_ms: int = limits.DEFAULT_LOCK_TIMEOUT_MS,
    ) -> None:
        """Validate the arguments up front; a wrong one fails here, loudly.

        ``lease_seconds``: 1 to ``MAX_LEASE_SECONDS``. ``max_attempts``: 1 to
        ``MAX_MAX_ATTEMPTS``. ``backoff``: a :class:`Backoff` (default numbers if
        ``None``). ``lock_timeout_ms``: 1 to ``MAX_LOCK_TIMEOUT_MS``.
        """
        if not isinstance(database, Database):
            raise InvalidJournalInputError("database", InputProblem.WRONG_TYPE)
        backoff = Backoff() if backoff is None else backoff
        if not isinstance(backoff, Backoff):
            raise InvalidJournalInputError("backoff", InputProblem.WRONG_TYPE)
        self._database = database
        self._lease_seconds = validate_int(
            "lease_seconds", lease_seconds, low=1, high=limits.MAX_LEASE_SECONDS
        )
        self._max_attempts = validate_int(
            "max_attempts", max_attempts, low=1, high=limits.MAX_MAX_ATTEMPTS
        )
        self._backoff = backoff
        self._lock_timeout_ms = validate_int(
            "lock_timeout_ms",
            lock_timeout_ms,
            low=1,
            high=limits.MAX_LOCK_TIMEOUT_MS,
        )

    @property
    def lease_seconds(self) -> int:
        return self._lease_seconds

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def _transaction(self):
        return transaction(self._database, self._lock_timeout_ms)

    async def enqueue(
        self, entry_id: UUID, priority: Priority = Priority.NORMAL
    ) -> QueueJob:
        """Queue the consolidation of a pending entry and return its active job.

        Idempotent: an entry that has a ``queued`` or ``claimed`` job gets that one
        (its priority is not changed). An entry whose earlier job is ``dead`` or
        ``completed`` and that is still ``pending`` gets a NEW job (this is how a
        dead-lettered observation is put back). Raises :class:`EntryNotFoundError`
        for an unknown entry and :class:`EntryNotPendingError` for a consolidated
        one.
        """
        entry_id = validate_uuid("entry_id", entry_id)
        priority = validate_enum("priority", priority, Priority)
        async with self._transaction() as session:
            # ``FOR SHARE`` waits for a consolidation that has the entry locked, so
            # the state read here is the state the job will meet.
            state = (
                await session.execute(
                    select(_ENTRY.c.state)
                    .where(_ENTRY.c.id == entry_id)
                    .with_for_update(read=True)
                )
            ).scalar_one_or_none()
            if state is None:
                raise EntryNotFoundError
            if state != EntryState.PENDING.value:
                raise EntryNotPendingError
            return _job(await insert_job(session, entry_id, priority))

    async def claim_next(self, worker_id: str) -> QueueJob | None:
        """Lease the next claimable job to ``worker_id`` and return it, or ``None``.

        The chosen row becomes ``claimed`` by ``worker_id`` with ``claimed_at`` the
        current instant and the lease ending ``lease_seconds`` later, and its
        ``claim_count`` grows by one: the returned snapshot's ``claim_count`` is the
        generation the worker presents from then on. Reclaiming an expired lease
        also counts one failed attempt (see the module docstring). A worker may
        hold several jobs at once (limiting concurrency is not the queue's job).
        """
        worker_id = validate_worker_id("worker_id", worker_id)
        current = _database_now()
        lease_end = current + timedelta(seconds=self._lease_seconds)
        claimable = or_(
            and_(_JOB.c.status == _queued(), _JOB.c.available_at <= current),
            and_(_JOB.c.status == _claimed(), _JOB.c.lease_expires_at <= current),
        )
        best_first = (
            select(_JOB.c.id)
            .where(claimable)
            .order_by(_JOB.c.priority_rank, _JOB.c.enqueued_at, _JOB.c.id)
            .limit(1)
            # A row another claimer has locked is skipped, never waited for.
            .with_for_update(skip_locked=True)
        )
        async with self._transaction() as session:
            job_id = (await session.execute(best_first)).scalar_one_or_none()
            if job_id is None:
                return None
            was_claimed = _JOB.c.status == _claimed()
            claim = (
                update(_JOB)
                .where(_JOB.c.id == job_id)
                .values(
                    status=JobStatus.CLAIMED.value,
                    claimed_by=worker_id,
                    claimed_at=current,
                    lease_expires_at=lease_end,
                    claim_count=_JOB.c.claim_count + 1,
                    # Expired lease: the previous worker never reported back.
                    attempts=_JOB.c.attempts + case((was_claimed, 1), else_=0),
                    last_failure=case(
                        (was_claimed, FailureKind.WORKER_ERROR.value),
                        else_=_JOB.c.last_failure,
                    ),
                )
                .returning(*_JOB.c)
            )
            return _job((await session.execute(claim)).one())

    async def heartbeat(
        self, job_id: int, worker_id: str, claim_count: int
    ) -> QueueJob:
        """Extend the lease of a job the worker holds; a lease is never shortened.

        Raises :class:`LeaseLostError` unless the worker holds a valid lease of
        that generation.
        """
        job_id = validate_job_id("job_id", job_id)
        worker_id = validate_worker_id("worker_id", worker_id)
        claim_count = validate_claim_count("claim_count", claim_count)
        current = _database_now()
        async with self._transaction() as session:
            await lock_held(session, job_id, worker_id, claim_count)
            row = (
                await session.execute(
                    update(_JOB)
                    .where(_JOB.c.id == job_id)
                    .values(
                        lease_expires_at=func.greatest(
                            _JOB.c.lease_expires_at,
                            current + timedelta(seconds=self._lease_seconds),
                        )
                    )
                    .returning(*_JOB.c)
                )
            ).one()
            return _job(row)

    async def fail(
        self, job_id: int, worker_id: str, claim_count: int, failure: FailureKind
    ) -> QueueJob:
        """Record a failed attempt: retry later, or dead-letter.

        The job returns to ``queued`` (its worker and lease cleared) with
        ``available_at`` the current instant plus the retry delay, or becomes
        ``dead`` when this failure counts and is the ``max_attempts``-th. The delay
        is ``Backoff.delay_seconds(n)``, where ``n`` counts the failures of this kind
        (``attempts``, or ``deferrals`` for an unavailable worker). Priority and
        place in the queue are kept. Raises :class:`LeaseLostError` unless the worker
        holds a valid lease of that generation.
        """
        job_id = validate_job_id("job_id", job_id)
        worker_id = validate_worker_id("worker_id", worker_id)
        claim_count = validate_claim_count("claim_count", claim_count)
        failure = validate_enum("failure", failure, FailureKind)
        current = _database_now()
        async with self._transaction() as session:
            held = await lock_held(session, job_id, worker_id, claim_count)
            counts = failure.counts_toward_dead_letter
            attempts = held.attempts + 1 if counts else held.attempts
            deferrals = held.deferrals if counts else held.deferrals + 1
            dead = counts and attempts >= self._max_attempts
            delay = self._backoff.delay_seconds(attempts if counts else deferrals)
            values: dict[str, object] = {
                "attempts": attempts,
                "deferrals": deferrals,
                "last_failure": failure.value,
                "lease_expires_at": None,
            }
            if dead:
                values |= {"status": JobStatus.DEAD.value, "finished_at": current}
            else:
                values |= {
                    "status": JobStatus.QUEUED.value,
                    "claimed_by": None,
                    "claimed_at": None,
                    "available_at": current + timedelta(seconds=delay),
                }
            row = (
                await session.execute(
                    update(_JOB)
                    .where(_JOB.c.id == job_id)
                    .values(**values)
                    .returning(*_JOB.c)
                )
            ).one()
            return _job(row)

    async def dead_letter(
        self, job_id: int, worker_id: str, claim_count: int
    ) -> QueueJob:
        """Dead-letter a job now, without another attempt.

        For a job whose earlier workers died (``attempts`` already at the limit):
        running it again would only kill another worker. The entry stays
        ``pending``. Raises :class:`LeaseLostError` unless the worker holds a valid
        lease of that generation.
        """
        job_id = validate_job_id("job_id", job_id)
        worker_id = validate_worker_id("worker_id", worker_id)
        claim_count = validate_claim_count("claim_count", claim_count)
        current = _database_now()
        async with self._transaction() as session:
            await lock_held(session, job_id, worker_id, claim_count)
            row = (
                await session.execute(
                    update(_JOB)
                    .where(_JOB.c.id == job_id)
                    .values(
                        status=JobStatus.DEAD.value,
                        finished_at=current,
                        lease_expires_at=None,
                        # A dead job has failed at least once (a CHECK says so).
                        attempts=func.greatest(_JOB.c.attempts, 1),
                        last_failure=func.coalesce(
                            _JOB.c.last_failure, FailureKind.WORKER_ERROR.value
                        ),
                    )
                    .returning(*_JOB.c)
                )
            ).one()
            return _job(row)
