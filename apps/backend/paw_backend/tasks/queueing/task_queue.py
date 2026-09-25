"""The task queue: priority order, leases and safe concurrent claiming (PAW-033).

The database is the only source of truth (``queue_entries``): nothing is kept in
process memory, so any number of workers in any number of processes can call
the same methods concurrently. The queue never CHANGES ``tasks.state`` (the
orchestrator (PAW-034) pairs ``claim_next`` with the PAW-032 ``start`` command). It
READS ``tasks`` in exactly two places, both from Issue #83 (Decision 0008, section
8) and both optional: ``enqueue`` reads the task's ``project_id`` for the Project
state gate, and ``cancel(..., only_if_task_terminal=True)`` reads (and share-locks)
the task's state. It performs no authorisation and offers no HTTP endpoint.

Project state gate. A queue built with a ``ProjectGate`` (``tasks.project_gate``)
makes ``enqueue`` lock the task's project row ``FOR SHARE`` in the transaction of
the insert and refuse (``ProjectNotActiveError``, nothing written) unless the project
is Active, so a Delete that begins meanwhile is serialised with the enqueue. It
needs ``SELECT`` on ``tasks`` (already granted) and whatever the gate needs on the
project table (see ``projects.task_gate``: ``SELECT`` and a column ``UPDATE``, which
``FOR SHARE`` requires; both are already granted). Every other method ignores the
project: a lease, a heartbeat, a completion and a cancel must work in any state.

Time. The DATABASE clock is the only clock the queue trusts (Decision 0007,
section 6, Approved 2026-09-25): every instant it compares or stores (``enqueued_at``,
``claimed_at``, ``lease_expires_at``, ``finished_at`` and "has this lease
expired?") is PostgreSQL's ``clock_timestamp()`` (the wall clock at the moment it is
evaluated) read inside the SQL statement. It is never ``now()``: that is fixed at the
start of the transaction and would judge an expiry by a time that has already
passed after a wait for a row lock. A worker's own clock, however skewed or wrong,
therefore cannot make a live lease look expired (and so cannot start the same task
on a second worker) or put an entry in front of the queue. All processes share one
authority. Exactly how the clock is read:

* ONE reading per statement. Every statement that needs the time starts with
  ``WITH clock AS (SELECT clock_timestamp() AS ts)`` and refers to that CTE (see
  ``_instants``), so "now" and "now + lease" of one statement come from one value
  and ``claimed_at`` and ``lease_expires_at`` are exactly ``lease_seconds`` apart.
* A new reading per statement. Two statements of one transaction read the clock
  separately (``claim_next``: the statement that picks the entry judges expiry with
  its reading, the one that updates it stamps ``claimed_at`` with a later one).
* A lease is judged after the row lock is held. ``heartbeat`` / ``release`` /
  ``complete`` first lock the row with ``SELECT ... FOR UPDATE`` (this is the
  statement that may wait) and judge the lease, with a reading taken afterwards,
  in the next statement (see ``_update_held``).
* A statement that judges no lease keeps the reading it took before a wait:
  ``enqueue`` (``enqueued_at``, while it waits for another uncommitted enqueue of
  the same task) and ``cancel`` (``finished_at``, while it waits for the row lock).
  These stamp ordering and records, and decide no lease.

Every method takes an optional ``now``. ``None`` (the default, and what production
code must always use) means the database clock. An explicit timezone-aware
``datetime`` is a TEST SEAM that makes behaviour deterministic; it is accepted only
by a queue built with ``allow_explicit_now=True``, and any other queue rejects it
with ``InvalidQueueingArgumentError("now")``, so a caller of a production queue
cannot substitute its own time. (The seam was kept, instead of an injected clock,
because it leaves every existing test unchanged.)

Order. Among the claimable entries the next one is chosen by, in this order:

1. ``priority_rank`` ascending (HIGH before NORMAL before LOW),
2. ``enqueued_at`` ascending (first in, first out),
3. ``id`` ascending (a tie of ``enqueued_at``).

The requirements state no aging or starvation rule, so there is none: a LOW
entry waits as long as HIGH / NORMAL entries keep arriving. A HIGH entry never
interrupts a running (claimed) entry; priority only decides the start order.

Claimable. An entry is claimable when its status is ``queued`` OR when its
status is ``claimed`` and ``lease_expires_at <= now`` (an expired lease: the
worker is presumed dead; ``now`` is the trusted clock above). A reclaimed entry
keeps its ``priority`` and ``enqueued_at``, so it sorts where it always did.

Lease. A claim leases the entry to one worker until ``now + lease_seconds``. The
worker holds a VALID lease while ``status = claimed``, ``claimed_by`` is its id,
the entry's ``claim_count`` is the claim generation it was given, and
``lease_expires_at > now`` (strictly: at the exact expiry instant the lease is
already lost). Only the holder of a valid lease may ``heartbeat``, ``release`` or
``complete``; anyone else gets ``LeaseLostError``. So at any instant at most one
worker holds a valid lease on an entry.

Claim generation (Decision 0007, section 7). A worker id alone does not identify a
lease: a worker whose lease expired can be claimed again by a worker with the SAME
id (a restarted process with a stable configured id), and the old execution, still
running, would then satisfy ``claimed_by`` and the new lease and could extend,
release or complete the replacement's claim. So ``claim_count``, which every claim
(also a reclaim) increases by one and nothing ever decreases or resets, is the
FENCING TOKEN of the lease: ``claim_next`` returns it in the ``QueueEntry`` and
``heartbeat`` / ``release`` / ``complete`` REQUIRE it (a required argument, so a
caller cannot forget to fence). A call that presents an older generation raises
``LeaseLostError`` and changes nothing.

Row-lock semantics. ``claim_next`` must run in ONE transaction that selects the
best claimable row with ``FOR UPDATE SKIP LOCKED`` (``ORDER BY ... LIMIT 1``) and
then updates that row. A row that another transaction has locked is SKIPPED, not
waited for. Hence two racing claimers never receive the same entry, a claimer
never blocks behind another one, and if the only claimable entry is locked by
somebody else the call returns ``None`` immediately. ``heartbeat`` / ``release``
/ ``complete`` run in one transaction of two statements: ``SELECT ... FOR UPDATE``
of the entry (they wait here for a competing transaction), then a conditional
``UPDATE ... RETURNING`` that judges worker, claim generation and lease. ``cancel``
is one conditional ``UPDATE`` (atomic by itself; it takes no row lock first). With
``only_if_task_terminal=True`` it first share-locks the TASK row
(``SELECT state FROM tasks ... FOR SHARE``: it waits for a command in flight, and a
Restart that starts afterwards waits for it) and cancels the entry only if the task
is Completed, Failed or Cancelled. That makes "the task is finished, so its entry
is dead" a decision that cannot go stale before the entry is cancelled: a Restart
either committed first (the entry belongs to a running task again and is kept) or
waits until the cancel committed (the task then needs a new entry). ``cancel_in``
does the same inside a transaction of the caller (see there).

Indexes. Completed and cancelled entries are kept for ever, and both indexes of the
queue (``ix_queue_entries_claim_order``, ``uq_queue_entries_one_active_per_task``)
are partial: ``WHERE status IN ('queued', 'claimed')``. A statement whose predicate
does not imply that condition cannot use them, and a status sent as a bind parameter
is not known in the generic plan PostgreSQL may cache for a prepared statement. So
``claim_next`` and ``cancel`` write the statuses into the SQL text (``_inlined``,
``_active``), and ``IndexPlanTest`` plans every query in both ``plan_cache_mode``s.

Errors. Invalid arguments (including an explicit ``now`` that this queue does not
accept) raise ``InvalidQueueingArgumentError(parameter)`` before any database
access. Messages never contain the argument values.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    DateTime,
    and_,
    bindparam,
    func,
    insert,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import BindParameter, ColumnElement

from paw_backend.db import Database
from paw_backend.tasks.domain import TERMINAL_STATES
from paw_backend.tasks.errors import TaskNotFoundError
from paw_backend.tasks.models import TaskRow
from paw_backend.tasks.project_gate import ProjectGate
from paw_backend.tasks.queueing.domain import (
    ACTIVE_QUEUE_STATUSES,
    Priority,
    QueueEntry,
    QueueStatus,
)
from paw_backend.tasks.queueing.errors import (
    InvalidQueueingArgumentError,
    LeaseLostError,
    TaskAlreadyQueuedError,
)
from paw_backend.tasks.queueing.models import QueueEntryRow
from paw_backend.tasks.queueing.sql import (
    FOREIGN_KEY_VIOLATION,
    UNIQUE_VIOLATION,
    constraint_name,
    sqlstate,
)
from paw_backend.tasks.queueing.validation import (
    DEFAULT_LEASE_SECONDS,
    MAX_LEASE_SECONDS,
    check_bool,
    check_claim_count,
    check_entry_id,
    check_int,
    check_member,
    check_now,
    check_project_gate,
    check_session,
    check_uuid,
    check_worker_id,
)

ONE_ACTIVE_ENTRY_PER_TASK = "uq_queue_entries_one_active_per_task"


def _inlined(status: QueueStatus) -> BindParameter[QueueStatus]:
    """``status`` written into the SQL text (``'queued'``), not sent as a parameter.

    ``ix_queue_entries_claim_order`` and ``uq_queue_entries_one_active_per_task``
    are partial indexes over ``status IN ('queued', 'claimed')``. PostgreSQL uses
    such an index only if the statement's own predicate implies that condition, and
    it cannot prove it for a status sent as a bind parameter in the plan it caches
    for a prepared statement (the driver prepares a statement it runs often): the
    claim would then scan and sort every entry ever queued (completed and cancelled
    entries are kept), and ``cancel`` would scan them all. The status is one of two
    constants, so writing it into the statement costs no plan reuse.
    """
    return bindparam(
        f"status_{status.value}",
        status,
        type_=QueueEntryRow.status.type,
        literal_execute=True,
    )


def _active() -> ColumnElement[bool]:
    """``status IN ('queued', 'claimed')`` (the condition of both partial indexes)."""
    return QueueEntryRow.status.in_(
        [_inlined(status) for status in sorted(ACTIVE_QUEUE_STATUSES)]
    )


def _entry(row: QueueEntryRow) -> QueueEntry:
    return QueueEntry(
        id=row.id,
        task_id=row.task_id,
        priority=row.priority,
        status=row.status,
        enqueued_at=row.enqueued_at,
        claimed_by=row.claimed_by,
        claimed_at=row.claimed_at,
        lease_expires_at=row.lease_expires_at,
        claim_count=row.claim_count,
        finished_at=row.finished_at,
    )


class TaskQueue:
    def __init__(
        self,
        database: Database,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        allow_explicit_now: bool = False,
        project_gate: ProjectGate | None = None,
    ) -> None:
        """``lease_seconds``: 1 to ``MAX_LEASE_SECONDS`` (``int``, not ``bool``).

        ``allow_explicit_now`` (a ``bool``): the TEST SEAM of the module docstring.
        Leave it ``False`` in production code: the queue then uses only the
        database clock and rejects a caller-supplied ``now``.

        ``project_gate``: the Project state gate of ``enqueue`` (module docstring);
        ``None`` (the default) enqueues for a task of any project, for tests and tools
        without projects. Production wiring must pass one (Decision 0020).
        """
        self._database = database
        self._lease_seconds = check_int(
            "lease_seconds", lease_seconds, minimum=1, maximum=MAX_LEASE_SECONDS
        )
        self._allow_explicit_now = check_bool("allow_explicit_now", allow_explicit_now)
        self._project_gate = check_project_gate("project_gate", project_gate)

    @property
    def lease_seconds(self) -> int:
        return self._lease_seconds

    @property
    def _lease(self) -> timedelta:
        return timedelta(seconds=self._lease_seconds)

    def _instants(
        self, now: datetime | None
    ) -> tuple[ColumnElement[datetime], ColumnElement[datetime]]:
        """The current instant and the expiry of a lease granted now, as SQL.

        ``now=None``: PostgreSQL's ``clock_timestamp()`` (the wall clock at the
        moment it is evaluated; ``now()`` would be the start of the transaction) and
        that plus the lease. Both are subqueries of the CTE ``clock`` that the
        statement using them starts with, so ONE reading serves the whole statement.
        A statement that waits for a row lock keeps its reading from before the
        wait (which is why ``_update_held`` locks first). An explicit ``now`` (test
        seam) is checked and bound as a value. Raises
        ``InvalidQueueingArgumentError("now")`` for an explicit ``now`` that is
        not a timezone-aware ``datetime`` or when the queue does not allow it.
        """
        if now is None:
            # ONE reading of the clock per statement: ``clock_timestamp()`` written
            # twice would be read twice (the reads of a volatile function may
            # differ even within a statement), so ``claimed_at`` and the lease end
            # would not be exactly ``lease_seconds`` apart. A CTE that contains a
            # volatile function is evaluated (materialised) once however often it
            # is referenced.
            sample = select(func.clock_timestamp().label("ts")).cte("clock")
            current = select(sample.c.ts).scalar_subquery()
            return current, current + self._lease
        if not self._allow_explicit_now:
            raise InvalidQueueingArgumentError("now")
        check_now("now", now)
        return (
            literal(now, DateTime(timezone=True)),
            literal(now + self._lease, DateTime(timezone=True)),
        )

    async def enqueue(
        self,
        task_id: uuid.UUID,
        *,
        now: datetime | None = None,
        priority: Priority = Priority.NORMAL,
    ) -> QueueEntry:
        """Put a task in the queue and return the new entry.

        The entry is ``queued`` with ``enqueued_at`` = the current instant (the
        database clock read by this statement, or ``now`` under the test seam),
        ``claim_count = 0`` and no worker. ``priority_rank`` is ``priority.rank``.

        Raises ``TaskNotFoundError`` when no task has this id (foreign key
        violation, SQLSTATE 23503), ``TaskAlreadyQueuedError`` when the task
        already has a ``queued`` or ``claimed`` entry (violation of the unique
        index ``uq_queue_entries_one_active_per_task``, SQLSTATE 23505) and
        ``InvalidQueueingArgumentError`` for a ``task_id`` that is not a
        ``uuid.UUID``, a ``now`` that is not accepted or is naive, or a
        ``priority`` that is not a ``Priority`` member (the string ``"high"`` is
        rejected). Any other ``IntegrityError`` must propagate unchanged.

        A task whose previous entry is ``completed`` or ``cancelled`` can be
        enqueued again (after Retry / Restart); that creates a NEW entry.

        With a ``project_gate`` the task's project is locked ``FOR SHARE`` in the
        transaction of the insert and must be Active: ``ProjectNotActiveError``
        (nothing written) otherwise. The gate is judged first: for an unknown task
        ``TaskNotFoundError`` is raised by the lookup of its project.
        """
        check_uuid("task_id", task_id)
        check_member("priority", priority, Priority)
        current, _ = self._instants(now)
        insert_entry = (
            insert(QueueEntryRow)
            .values(
                task_id=task_id,
                priority=priority,
                priority_rank=priority.rank,
                status=QueueStatus.QUEUED,
                enqueued_at=current,
                claim_count=0,
            )
            .returning(QueueEntryRow)
            .execution_options(populate_existing=True)
        )
        try:
            async with self._database.session() as session, session.begin():
                if self._project_gate is not None:
                    await self._require_active_project(session, task_id)
                row = (await session.execute(insert_entry)).scalar_one()
                return _entry(row)
        except IntegrityError as error:
            if sqlstate(error) == FOREIGN_KEY_VIOLATION:
                raise TaskNotFoundError() from None
            if (
                sqlstate(error) == UNIQUE_VIOLATION
                and constraint_name(error) == ONE_ACTIVE_ENTRY_PER_TASK
            ):
                raise TaskAlreadyQueuedError() from None
            raise

    async def claim_next(
        self, worker_id: str, now: datetime | None = None
    ) -> QueueEntry | None:
        """Lease the next claimable entry to ``worker_id`` and return it, or ``None``.

        See the module docstring for the order, "claimable", the clock and the row
        locks. The chosen row is updated to: ``status = claimed``,
        ``claimed_by = worker_id``, ``claimed_at`` = the current instant,
        ``lease_expires_at`` = that instant + ``lease_seconds``,
        ``claim_count + 1``. The returned ``QueueEntry`` shows the row after the
        update; its ``claim_count`` is the claim generation the worker must present
        to ``heartbeat`` / ``release`` / ``complete``. Both statements run in one
        transaction but each reads the database clock itself: the selecting one
        judges expiry with its reading, the updating one stamps ``claimed_at`` and
        the lease end (exactly ``lease_seconds`` apart) with a later reading. The
        selected row is locked by this claimer in between, so its lease cannot
        change.

        ``worker_id``: see ``validation.check_worker_id``. A worker may hold
        several entries at once (limiting concurrency is not the queue's job).
        """
        check_worker_id(worker_id)
        current, lease_end = self._instants(now)
        claimable = or_(
            QueueEntryRow.status == _inlined(QueueStatus.QUEUED),
            and_(
                QueueEntryRow.status == _inlined(QueueStatus.CLAIMED),
                QueueEntryRow.lease_expires_at <= current,
            ),
        )
        best_first = (
            select(QueueEntryRow.id)
            .where(claimable)
            .order_by(
                QueueEntryRow.priority_rank, QueueEntryRow.enqueued_at, QueueEntryRow.id
            )
            .limit(1)
            # A row that another claimer has locked is skipped, never waited for.
            .with_for_update(skip_locked=True)
        )
        async with self._database.session() as session, session.begin():
            entry_id = (await session.execute(best_first)).scalar_one_or_none()
            if entry_id is None:
                return None
            claim = (
                update(QueueEntryRow)
                .where(QueueEntryRow.id == entry_id)
                .values(
                    status=QueueStatus.CLAIMED,
                    claimed_by=worker_id,
                    claimed_at=current,
                    lease_expires_at=lease_end,
                    claim_count=QueueEntryRow.claim_count + 1,
                )
                .returning(QueueEntryRow)
                .execution_options(populate_existing=True)
            )
            return _entry((await session.execute(claim)).scalar_one())

    async def heartbeat(
        self,
        entry_id: int,
        worker_id: str,
        claim_count: int,
        now: datetime | None = None,
    ) -> QueueEntry:
        """Extend the lease of an entry the worker holds and return it.

        ``claim_count`` is the claim generation the worker was given by
        ``claim_next`` (``QueueEntry.claim_count``; see the module docstring).

        The new ``lease_expires_at`` is ``max(current, now + lease_seconds)``: a
        heartbeat never shortens a lease. Everything else stays unchanged.
        Raises ``LeaseLostError`` unless the worker holds a valid lease of that
        generation (see the module docstring; ``now`` is the trusted clock).
        """
        check_entry_id(entry_id)
        check_worker_id(worker_id)
        check_claim_count(claim_count)
        current, lease_end = self._instants(now)
        return await self._update_held(
            entry_id,
            worker_id,
            claim_count,
            current,
            lease_expires_at=func.greatest(QueueEntryRow.lease_expires_at, lease_end),
        )

    async def release(
        self,
        entry_id: int,
        worker_id: str,
        claim_count: int,
        now: datetime | None = None,
    ) -> QueueEntry:
        """Give an entry back so that another (or the same) worker can claim it.

        The entry becomes ``queued`` again with ``claimed_by``, ``claimed_at`` and
        ``lease_expires_at`` cleared. ``priority``, ``enqueued_at`` (its place in
        the FIFO order) and ``claim_count`` are kept (so the next claim is a newer
        generation and this ``claim_count`` is dead). ``claim_count`` is the claim
        generation of the module docstring. Raises ``LeaseLostError`` unless the
        worker holds a valid lease of that generation.
        """
        check_entry_id(entry_id)
        check_worker_id(worker_id)
        check_claim_count(claim_count)
        current, _ = self._instants(now)
        return await self._update_held(
            entry_id,
            worker_id,
            claim_count,
            current,
            status=QueueStatus.QUEUED,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
        )

    async def complete(
        self,
        entry_id: int,
        worker_id: str,
        claim_count: int,
        now: datetime | None = None,
    ) -> QueueEntry:
        """Finish an entry: ``status = completed``, ``finished_at`` = now.

        ``lease_expires_at`` is cleared; ``claimed_by`` and ``claimed_at`` are
        kept as history. A completed entry is never claimable again.
        ``claim_count`` is the claim generation of the module docstring. Raises
        ``LeaseLostError`` unless the worker holds a valid lease of that
        generation.
        """
        check_entry_id(entry_id)
        check_worker_id(worker_id)
        check_claim_count(claim_count)
        current, _ = self._instants(now)
        return await self._update_held(
            entry_id,
            worker_id,
            claim_count,
            current,
            status=QueueStatus.COMPLETED,
            finished_at=current,
            lease_expires_at=None,
        )

    async def cancel(
        self,
        task_id: uuid.UUID,
        now: datetime | None = None,
        *,
        only_if_task_terminal: bool = False,
    ) -> bool:
        """Cancel the task's active entry (``queued`` or ``claimed``), if any.

        Sets ``status = cancelled``, ``finished_at`` = now (read by this statement
        before it waits for the row lock, if it has to) and clears
        ``lease_expires_at`` (``claimed_by`` / ``claimed_at`` are kept). A worker
        that held the entry loses its lease: its next ``heartbeat`` / ``release`` /
        ``complete`` raises ``LeaseLostError``. Returns ``True`` when an entry was
        cancelled, ``False`` when the task had no active entry (including an
        unknown task; that is not an error). Idempotent.

        ``only_if_task_terminal`` (a ``bool``): cancel only if the TASK is
        ``completed``, ``failed`` or ``cancelled``, judged with the task row
        share-locked (module docstring). For a task that is active, or unknown, the
        entry is left alone and ``False`` is returned. Use it whenever the decision
        "this entry is dead" comes from a state that was read earlier: a Restart that
        committed in between makes the entry the restarted task's own.
        """
        check_uuid("task_id", task_id)
        check_bool("only_if_task_terminal", only_if_task_terminal)
        current, _ = self._instants(now)
        async with self._database.session() as session, session.begin():
            return await self._cancel(session, task_id, current, only_if_task_terminal)

    async def cancel_in(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        now: datetime | None = None,
        *,
        only_if_task_terminal: bool = False,
    ) -> bool:
        """``cancel`` in the transaction of the caller's ``session``.

        Nothing is committed or rolled back here: the entry is cancelled when the
        caller's transaction commits, together with whatever else it wrote. This is
        how the stop processor of a deleted project cancels a task and its entry in
        ONE transaction (``TaskService.execute(..., in_transaction=...)``), so no
        crash or Restore can fall between the two. ``session`` must be an
        ``AsyncSession`` inside a transaction (``InvalidQueueingArgumentError``
        otherwise); the other arguments are those of ``cancel``.
        """
        check_session("session", session)
        check_uuid("task_id", task_id)
        check_bool("only_if_task_terminal", only_if_task_terminal)
        current, _ = self._instants(now)
        return await self._cancel(session, task_id, current, only_if_task_terminal)

    async def _cancel(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        current: ColumnElement[datetime],
        only_if_task_terminal: bool,
    ) -> bool:
        if only_if_task_terminal:
            # FOR SHARE conflicts with the FOR NO KEY UPDATE every task command takes
            # first, so this waits for a command in flight and holds off the next one
            # until this transaction ends. (It needs UPDATE on some column of
            # ``tasks``, which the application role has; see the module docstring.)
            state = (
                await session.execute(
                    select(TaskRow.state)
                    .where(TaskRow.id == task_id)
                    .with_for_update(read=True)
                )
            ).scalar_one_or_none()
            if state not in TERMINAL_STATES:
                return False
        cancel_active = (
            update(QueueEntryRow)
            .where(QueueEntryRow.task_id == task_id, _active())
            .values(
                status=QueueStatus.CANCELLED, finished_at=current, lease_expires_at=None
            )
        )
        result = await session.execute(cancel_active)
        return result.rowcount > 0

    async def _require_active_project(
        self, session: AsyncSession, task_id: uuid.UUID
    ) -> None:
        """Lock the task's project through the gate, or refuse (``enqueue``).

        ``tasks.project_id`` never changes, so a plain read of it is enough; the
        gate takes the lock. An unknown task is ``TaskNotFoundError``.
        """
        assert self._project_gate is not None
        project_id = (
            await session.execute(
                select(TaskRow.project_id).where(TaskRow.id == task_id)
            )
        ).scalar_one_or_none()
        if project_id is None:
            raise TaskNotFoundError()
        await self._project_gate.require_active(session, project_id)

    async def _update_held(
        self,
        entry_id: int,
        worker_id: str,
        claim_count: int,
        current: ColumnElement[datetime],
        **values: Any,
    ) -> QueueEntry:
        """Apply ``values`` if the worker holds a valid lease of this generation.

        Raises ``LeaseLostError`` when the entry does not exist, is not claimed, is
        claimed by another worker, has been claimed again since (``claim_count``
        differs: a stale claim of the same worker id) or its lease has expired
        (``lease_expires_at <= current``).

        The row is locked FIRST, in its own statement, and the lease is judged in the
        next one. An ``UPDATE`` judges its ``WHERE`` before it waits for a row lock,
        and does not judge it again when the lock holder rolled back, so a single
        statement would accept a lease that ran out while it waited.
        """
        lock_entry = (
            select(QueueEntryRow.id)
            .where(QueueEntryRow.id == entry_id)
            .with_for_update()
        )
        update_held = (
            update(QueueEntryRow)
            .where(
                QueueEntryRow.id == entry_id,
                QueueEntryRow.status == QueueStatus.CLAIMED,
                QueueEntryRow.claimed_by == worker_id,
                QueueEntryRow.claim_count == claim_count,
                QueueEntryRow.lease_expires_at > current,
            )
            .values(**values)
            .returning(QueueEntryRow)
            .execution_options(populate_existing=True)
        )
        async with self._database.session() as session, session.begin():
            if (await session.execute(lock_entry)).scalar_one_or_none() is None:
                raise LeaseLostError()
            row = (await session.execute(update_held)).scalar_one_or_none()
            if row is None:
                raise LeaseLostError()
            return _entry(row)
