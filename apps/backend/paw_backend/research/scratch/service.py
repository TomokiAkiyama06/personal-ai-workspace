"""The Research Scratch Store service (PAW-050).

Temporary research results, separate from Long-term Memory, that live for
``created_at + 24 hours`` unless something defers their deletion.

Who may call it
---------------
The store performs **no authorisation**, like ``TaskService``: the API layer
(a later issue) decides first and passes only ids it has checked. It is not
exposed over HTTP by this issue. The intended mapping to the capabilities of
``paw_backend.authz`` (a proposal for that issue, not enforced here):

* ``get``, ``list_items``: ``project.read`` on ``project_id``;
* ``add``, ``acquire_use``, ``release_use``, ``pin``, ``unpin``:
  ``project.task.run`` on ``project_id``;
* ``request_promotion``: ``project.memory.use``; ``resolve_promotion``:
  ``project.memory.manage`` (not delegable to an agent: research is never saved
  to Long-term Memory on an agent's own say-so);
* ``purge_expired``: the Backend's own janitor only (no user or agent).

Every per-item method takes ``project_id`` and finds the item only inside that
project: an id from another project is "not found", exactly like a missing id.

Visibility, exemptions and TTL
------------------------------
An item is *exempt* from deletion at instant ``now`` when any of these holds:

* it is pinned (``pinned``);
* its promotion is pending (``promotion_state == 'pending'``);
* it is in use: a lease with ``expires_at > now`` exists.

An item is *visible* at ``now`` when ``now < expires_at`` or it is exempt.
Everything that is not visible is gone for every method of the store
("not found") even if ``purge_expired`` has not removed the row yet, so
behaviour never depends on when the janitor happens to run. ``expires_at`` is
never changed: exemptions defer the deletion, they do not extend the TTL. When
the last exemption ends (unpin, release / lease end, promotion resolved) an
expired item is not visible any more and the next ``purge_expired`` deletes it;
no extra state or queue is needed for that.

Time
----
All instants come from the injected clock (``Clock``: a callable without
arguments returning an aware ``datetime``). Every operation reads the clock
once, at its start, validates it with ``validate_datetime("clock", ...)`` and
uses that single instant for visibility, ``created_at``, leases and
``promotion_requested_at``. ``purge_expired`` may be given ``now`` explicitly.

Concurrency contract
--------------------
The database runs at the default READ COMMITTED level. Rules that make the
purge / lease race safe:

1. Every operation that changes an item or its leases (``pin``, ``unpin``,
   ``acquire_use``, ``release_use``, ``request_promotion``,
   ``resolve_promotion``) starts a transaction and first locks the item row:
   ``SELECT ... FOR UPDATE`` (it waits). Only then does it evaluate visibility
   and change anything. The item row is always locked before its lease rows.
2. ``add`` locks the task row ``FOR KEY SHARE`` while it checks that the task
   exists and belongs to ``project_id`` (a concurrent delete of the task waits
   until the item is inserted). There is no foreign key (decision 0013): this
   check is the only one, and once the item is stored a later delete of the task
   is neither blocked nor changes the item, which keeps its ``task_id``.
3. ``purge_expired`` never waits for a locked row: it selects candidates with
   ``FOR UPDATE ... SKIP LOCKED``, and after locking them it re-checks the
   exemptions in a **new statement** before it deletes (see its docstring). So
   an item whose lease or pin was committed before the purge's delete statement
   started survives, and an item another transaction is working on is skipped.
   If the purge locks an item first, the waiting operation finds no row and
   reports "not found".
4. Every transaction that changes something (``add``, the operations of rule 1
   and ``purge_expired``) begins with ``SET LOCAL lock_timeout`` set to
   ``lock_timeout_ms`` (milliseconds); a lock wait that exceeds it raises
   :class:`ScratchBusyError` (the transaction is rolled back). ``get`` and
   ``list_items`` read without locking and never wait for a row lock.

Errors
------
Validation problems are :class:`InvalidScratchInputError` and are raised before
the database is touched (a store on an unconfigured ``Database`` still reports
them). Messages never contain caller content. Database errors the store does not
handle (connection loss and so on) propagate unchanged; their text can contain
SQL parameters, so a caller must never show ``str(error)`` to a user. The store
logs nothing that contains caller content.
"""

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg.errors
from sqlalchemy import (
    ColumnElement,
    and_,
    delete,
    exists,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.db import Database
from paw_backend.research.scratch.errors import (
    InputProblem,
    InvalidScratchInputError,
    ScratchBusyError,
    ScratchItemNotFoundError,
    ScratchLeaseLimitError,
    ScratchStateError,
)
from paw_backend.research.scratch.limits import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_LIST_LIMIT,
    DEFAULT_PURGE_BATCH_SIZE,
    MAX_ACTIVE_LEASES_PER_ITEM,
    MAX_LEASE_SECONDS,
    MAX_LIST_LIMIT,
    MAX_PURGE_BATCH_SIZE,
    MIN_LEASE_SECONDS,
    expiry_of,
    utc_now,
)
from paw_backend.research.scratch.models import ScratchItemRow, ScratchLeaseRow
from paw_backend.research.scratch.records import (
    Lease,
    PromotionOutcome,
    PromotionState,
    PurgeResult,
    ScratchItem,
)
from paw_backend.research.scratch.validation import (
    validate_bool,
    validate_bounded_int,
    validate_datetime,
    validate_new_item,
    validate_optional_uuid,
    validate_outcome,
    validate_uuid,
)
from paw_backend.tasks.models import TaskRow

Clock = Callable[[], datetime]
# Test seam of ``purge_expired`` (see its docstring): called with the session of
# the purge transaction and the ids that are about to be deleted.
PurgeProbe = Callable[[AsyncSession, list[UUID]], Awaitable[None]]

DEFAULT_LOCK_TIMEOUT_MS = 5000
MIN_LOCK_TIMEOUT_MS = 50
MAX_LOCK_TIMEOUT_MS = 60_000


_ITEMS = ScratchItemRow.__table__
_LEASES = ScratchLeaseRow.__table__
_TASKS = TaskRow.__table__


def _has_active_lease(now: datetime) -> ColumnElement[bool]:
    """A lease of the enclosing statement's item is still running at ``now``."""
    return exists().where(_LEASES.c.item_id == _ITEMS.c.id, _LEASES.c.expires_at > now)


def _exempt(now: datetime) -> ColumnElement[bool]:
    """Deletion of the item is deferred at ``now`` (see the module docstring)."""
    return or_(
        _ITEMS.c.pinned,
        _ITEMS.c.promotion_state == PromotionState.PENDING.value,
        _has_active_lease(now),
    )


def _is_visible(item: ScratchItem) -> bool:
    """The module docstring's visibility rule, on a snapshot."""
    return not item.expired or bool(item.deferral_reasons)


def _snapshot(row: Mapping[str, Any], now: datetime, *, in_use: bool) -> ScratchItem:
    """A snapshot of an item row; ``content`` is None when the row lacks it."""
    return ScratchItem(
        id=row["id"],
        project_id=row["project_id"],
        task_id=row["task_id"],
        created_by=row["created_by"],
        query=row["query"],
        title=row["title"],
        summary=row["summary"],
        content=row.get("content"),
        source_metadata=row["source_metadata"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        expired=now >= row["expires_at"],
        pinned=row["pinned"],
        in_use=in_use,
        promotion_state=PromotionState(row["promotion_state"]),
        promotion_requested_at=row["promotion_requested_at"],
    )


class ScratchStore:
    """Persistence and rules of the Research Scratch Store (see the module doc)."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Clock | None = None,
        lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
        purge_probe: PurgeProbe | None = None,
    ) -> None:
        """Validate the collaborators up front; a wrong one fails here, loudly.

        ``database`` must be a :class:`Database` (``TypeError`` otherwise).
        ``clock`` defaults to ``utc_now``; it must be callable with no
        arguments (``TypeError`` otherwise). ``lock_timeout_ms`` must be an
        ``int`` (not a ``bool``) from 50 to 60000 (``TypeError`` for another
        type, ``ValueError`` when out of range). ``purge_probe`` is a test seam
        (see ``purge_expired``); it must be ``None`` or callable (``TypeError``
        otherwise). Nothing connects here.
        """
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if clock is None:
            clock = utc_now
        elif not callable(clock):
            raise TypeError("clock must be callable")
        else:
            try:
                inspect.signature(clock).bind()
            except TypeError:
                raise TypeError("clock must be callable without arguments") from None
            except ValueError:
                pass  # a callable without an introspectable signature
        if isinstance(lock_timeout_ms, bool) or not isinstance(lock_timeout_ms, int):
            raise TypeError("lock_timeout_ms must be an int")
        if not MIN_LOCK_TIMEOUT_MS <= lock_timeout_ms <= MAX_LOCK_TIMEOUT_MS:
            raise ValueError("lock_timeout_ms is out of range")
        if purge_probe is not None and not callable(purge_probe):
            raise TypeError("purge_probe must be callable")
        self._database = database
        self._clock: Clock = clock
        self._lock_timeout_ms = lock_timeout_ms
        self._purge_probe = purge_probe

    # -- helpers ---------------------------------------------------------------

    def _now(self) -> datetime:
        return validate_datetime("clock", self._clock())

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        """One transaction with the store's lock timeout (module docstring, rule 4)."""
        try:
            async with self._database.session() as session, session.begin():
                await session.execute(
                    select(
                        func.set_config(
                            "lock_timeout", str(self._lock_timeout_ms), True
                        )
                    )
                )
                yield session
        except DBAPIError as error:
            # Only the type of the driver's error is read, never its text.
            if isinstance(error.orig, psycopg.errors.LockNotAvailable):
                raise ScratchBusyError() from None
            raise

    @staticmethod
    async def _read(
        session: AsyncSession,
        project_id: UUID,
        item_id: UUID,
        now: datetime,
    ) -> ScratchItem | None:
        """The item's snapshot at ``now``, visible or not; one consistent statement."""
        statement = select(_ITEMS, _has_active_lease(now).label("in_use")).where(
            _ITEMS.c.id == item_id, _ITEMS.c.project_id == project_id
        )
        row = (await session.execute(statement)).mappings().first()
        return None if row is None else _snapshot(row, now, in_use=row["in_use"])

    @staticmethod
    async def _lock(session: AsyncSession, project_id: UUID, item_id: UUID) -> bool:
        """Lock the item's row (waiting); False when there is no such item."""
        statement = (
            select(_ITEMS.c.id)
            .where(_ITEMS.c.id == item_id, _ITEMS.c.project_id == project_id)
            .with_for_update()
        )
        return (await session.execute(statement)).first() is not None

    async def _lock_visible(
        self, session: AsyncSession, project_id: UUID, item_id: UUID, now: datetime
    ) -> ScratchItem:
        """Lock the item's row, then return its snapshot if it is visible.

        The snapshot is read by a new statement after the lock is granted, so it
        includes every lease committed before that moment. Reading the lease in
        the locking statement would not: PostgreSQL re-checks only the locked
        row itself, not the other tables of the query, after waiting for a lock.
        """
        if await self._lock(session, project_id, item_id):
            item = await self._read(session, project_id, item_id, now)
            if item is not None and _is_visible(item):
                return item
        raise ScratchItemNotFoundError()

    async def _set_pinned(
        self, project_id: object, item_id: object, pinned: bool
    ) -> ScratchItem:
        project = validate_uuid("project_id", project_id)
        item_uuid = validate_uuid("item_id", item_id)
        now = self._now()
        async with self._transaction() as session:
            item = await self._lock_visible(session, project, item_uuid, now)
            if item.pinned != pinned:
                await session.execute(
                    update(_ITEMS).where(_ITEMS.c.id == item_uuid).values(pinned=pinned)
                )
            return replace(item, pinned=pinned)

    # -- create and read ----------------------------------------------------

    async def add(
        self,
        project_id: UUID,
        *,
        created_by: UUID,
        task_id: UUID | None = None,
        query: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        content: str | None = None,
        source_metadata: dict[str, Any] | None = None,
    ) -> ScratchItem:
        """Store a new research result and return its snapshot.

        1. ``validate_new_item`` (errors as documented there), then the clock is
           read once: ``created_at`` is that instant and ``expires_at`` is
           ``expiry_of(created_at)``.
        2. When ``task_id`` is given, one transaction locks the task row
           (``SELECT ... FROM tasks WHERE id = :task_id AND project_id =
           :project_id FOR KEY SHARE``). No row (the task does not exist, or it
           belongs to another project: the two are not distinguished) raises
           ``InvalidScratchInputError("task_id", UNKNOWN_REFERENCE)`` and stores
           nothing. The relation to the project and the task is kept as given,
           also after the task is deleted (decision 0013).
        3. The item is inserted with a new random id (generated by the database),
           ``pinned`` false, ``promotion_state`` ``none`` and no lease.

        ``add`` is not idempotent: two calls with the same arguments store two
        items with different ids (retry policy belongs to the caller).

        The snapshot has ``expired`` False, ``in_use`` False, ``pinned`` False,
        ``promotion_state`` ``PromotionState.NONE``, ``promotion_requested_at``
        None and ``source_metadata`` equal to the validated copy.
        """
        fields = validate_new_item(
            project_id=project_id,
            created_by=created_by,
            task_id=task_id,
            query=query,
            title=title,
            summary=summary,
            content=content,
            source_metadata=source_metadata,
        )
        now = self._now()
        async with self._transaction() as session:
            if fields.task_id is not None:
                task = (
                    select(_TASKS.c.id)
                    .where(
                        _TASKS.c.id == fields.task_id,
                        _TASKS.c.project_id == fields.project_id,
                    )
                    .with_for_update(read=True, key_share=True)
                )
                if (await session.execute(task)).first() is None:
                    raise InvalidScratchInputError(
                        "task_id", InputProblem.UNKNOWN_REFERENCE
                    )
            inserted = insert(_ITEMS).values(
                project_id=fields.project_id,
                task_id=fields.task_id,
                created_by=fields.created_by,
                query=fields.query,
                title=fields.title,
                summary=fields.summary,
                content=fields.content,
                source_metadata=fields.source_metadata,
                created_at=now,
                expires_at=expiry_of(now),
            )
            row = (
                (await session.execute(inserted.returning(*_ITEMS.c))).mappings().one()
            )
        return _snapshot(row, now, in_use=False)

    async def get(self, project_id: UUID, item_id: UUID) -> ScratchItem:
        """Return the visible item (see the module doc), with its full content.

        ``ScratchItemNotFoundError`` when there is no row with this ``item_id``
        in ``project_id``, or the item is not visible at the clock's instant
        (its TTL ended and nothing exempts it). One statement reads the row and
        computes ``in_use`` (an active lease exists at that instant), so the
        snapshot is consistent. Invalid ids: ``InvalidScratchInputError`` with
        field ``project_id`` / ``item_id`` (checked in that order).
        """
        project = validate_uuid("project_id", project_id)
        item_uuid = validate_uuid("item_id", item_id)
        now = self._now()
        async with self._database.session() as session:
            item = await self._read(session, project, item_uuid, now)
        if item is None or not _is_visible(item):
            raise ScratchItemNotFoundError()
        return item

    async def list_items(
        self,
        project_id: UUID,
        *,
        task_id: UUID | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        include_content: bool = False,
    ) -> list[ScratchItem]:
        """Return visible items of the project, newest first.

        Order: ``created_at`` descending, ties by ``id`` descending. With
        ``task_id`` only the items whose ``task_id`` equals it are returned
        (an unknown task is simply an empty list); without it every visible
        item of the project is returned, including those with no task. Items of
        other projects never appear. Exempt items whose TTL has ended are
        included (``expired`` True); items that are not visible are not.
        ``limit`` is 1 to ``MAX_LIST_LIMIT`` (``validate_bounded_int``, field
        ``limit``); there is no paging: callers that need more narrow by task.
        ``include_content`` (a strict ``bool``, field ``include_content``): when
        False every returned snapshot has ``content=None`` (the summary is always
        included); when True the full content is returned. One statement
        computes visibility and ``in_use`` at the clock's instant. Validation
        order: ``project_id``, ``task_id``, ``limit``, ``include_content``.
        """
        project = validate_uuid("project_id", project_id)
        task = validate_optional_uuid("task_id", task_id)
        row_limit = validate_bounded_int(
            "limit", limit, minimum=1, maximum=MAX_LIST_LIMIT
        )
        with_content = validate_bool("include_content", include_content)
        now = self._now()
        columns = [c for c in _ITEMS.c if with_content or c.name != "content"]
        statement = (
            select(*columns, _has_active_lease(now).label("in_use"))
            .where(
                _ITEMS.c.project_id == project,
                or_(_ITEMS.c.expires_at > now, _exempt(now)),
            )
            .order_by(_ITEMS.c.created_at.desc(), _ITEMS.c.id.desc())
            .limit(row_limit)
        )
        if task is not None:
            statement = statement.where(_ITEMS.c.task_id == task)
        async with self._database.session() as session:
            rows = (await session.execute(statement)).mappings().all()
        return [_snapshot(row, now, in_use=row["in_use"]) for row in rows]

    # -- pin ------------------------------------------------------------------

    async def pin(self, project_id: UUID, item_id: UUID) -> ScratchItem:
        """Keep the item beyond its TTL until it is unpinned.

        Locks the item row (concurrency rule 1). ``ScratchItemNotFoundError``
        when it is missing, belongs to another project, or is not visible (a
        pin cannot bring an expired, unexempt item back). Idempotent: pinning a
        pinned item changes nothing and returns its snapshot. Returns the
        snapshot after the change (``pinned`` True).
        """
        return await self._set_pinned(project_id, item_id, True)

    async def unpin(self, project_id: UUID, item_id: UUID) -> ScratchItem:
        """Remove the pin. Idempotent; same lookup rules as :meth:`pin`.

        Returns the snapshot after the change. If the pin was the last
        exemption of an expired item, that snapshot has ``expired`` True and
        the item is gone for later calls (``get`` raises
        ``ScratchItemNotFoundError``) and is deleted by the next purge.
        """
        return await self._set_pinned(project_id, item_id, False)

    # -- use (leases) ------------------------------------------------------------

    async def acquire_use(
        self,
        project_id: UUID,
        item_id: UUID,
        holder_id: UUID,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> Lease:
        """Mark the item as in use by ``holder_id`` for ``lease_seconds``.

        ``lease_seconds`` is 1 to ``MAX_LEASE_SECONDS`` (field
        ``lease_seconds``). Validation order: ``project_id``, ``item_id``,
        ``holder_id``, ``lease_seconds``. In one transaction, after locking the
        item row (rule 1):

        1. ``ScratchItemNotFoundError`` when it is missing, in another project
           or not visible at the clock's instant ``now`` (an expired, unexempt
           item cannot be acquired; an expired item that is pinned or has a
           pending promotion can).
        2. Delete this item's lease rows with ``expires_at <= now`` (expired
           leases must not accumulate).
        3. If ``MAX_ACTIVE_LEASES_PER_ITEM`` holders other than ``holder_id``
           have an active lease, raise ``ScratchLeaseLimitError``. At most
           ``MAX_ACTIVE_LEASES_PER_ITEM`` holders, ``holder_id`` included, are
           active at once, so a holder that already has a lease can always renew.
        4. Upsert the lease of ``(item_id, holder_id)`` with ``leased_at = now``
           and ``expires_at = now + lease_seconds``. A holder that already has a
           lease is renewed: the new ``expires_at`` replaces the old one (it may
           be earlier). Leases of other holders are untouched.

        Returns the stored lease. The item is *in use* on ``[leased_at,
        expires_at)``: at ``expires_at`` the lease is over. A holder that
        crashes therefore keeps the item alive for at most ``lease_seconds``.
        Acquiring does not change ``expires_at`` of the item.
        """
        project = validate_uuid("project_id", project_id)
        item_uuid = validate_uuid("item_id", item_id)
        holder = validate_uuid("holder_id", holder_id)
        seconds = validate_bounded_int(
            "lease_seconds",
            lease_seconds,
            minimum=MIN_LEASE_SECONDS,
            maximum=MAX_LEASE_SECONDS,
        )
        now = self._now()
        lease = Lease(item_uuid, holder, now, now + timedelta(seconds=seconds))
        async with self._transaction() as session:
            await self._lock_visible(session, project, item_uuid, now)
            await session.execute(
                delete(_LEASES).where(
                    _LEASES.c.item_id == item_uuid, _LEASES.c.expires_at <= now
                )
            )
            other_holders = (
                await session.execute(
                    select(func.count())
                    .select_from(_LEASES)
                    .where(
                        _LEASES.c.item_id == item_uuid, _LEASES.c.holder_id != holder
                    )
                )
            ).scalar_one()
            if other_holders >= MAX_ACTIVE_LEASES_PER_ITEM:
                raise ScratchLeaseLimitError()
            upsert = postgresql_insert(_LEASES).values(
                item_id=lease.item_id,
                holder_id=lease.holder_id,
                leased_at=lease.leased_at,
                expires_at=lease.expires_at,
            )
            await session.execute(
                upsert.on_conflict_do_update(
                    index_elements=[_LEASES.c.item_id, _LEASES.c.holder_id],
                    set_={
                        "leased_at": upsert.excluded.leased_at,
                        "expires_at": upsert.excluded.expires_at,
                    },
                )
            )
        return lease

    async def release_use(
        self, project_id: UUID, item_id: UUID, holder_id: UUID
    ) -> None:
        """End ``holder_id``'s lease on the item. Idempotent; returns ``None``.

        After locking the item row (rule 1) the lease row of ``(item_id,
        holder_id)`` is deleted, whether it is still active or already over, and
        whether or not the item is still visible (a lease of an item that has
        already expired is removed too). Nothing is raised when the item or the
        lease does not exist, or the item belongs to another project (then
        nothing is deleted): a worker that finishes after a purge must not fail.
        Other holders' leases are untouched. Validation order: ``project_id``,
        ``item_id``, ``holder_id``.
        """
        project = validate_uuid("project_id", project_id)
        item_uuid = validate_uuid("item_id", item_id)
        holder = validate_uuid("holder_id", holder_id)
        async with self._transaction() as session:
            if await self._lock(session, project, item_uuid):
                await session.execute(
                    delete(_LEASES).where(
                        _LEASES.c.item_id == item_uuid, _LEASES.c.holder_id == holder
                    )
                )

    # -- promotion ------------------------------------------------------------

    async def request_promotion(self, project_id: UUID, item_id: UUID) -> ScratchItem:
        """Mark the item as awaiting a promotion decision (deletion is deferred).

        Locks the item row, then ``ScratchItemNotFoundError`` when missing, in
        another project or not visible. By ``promotion_state``:

        * ``none`` or ``rejected``: becomes ``pending`` and
          ``promotion_requested_at`` is the clock's instant;
        * ``pending``: unchanged (idempotent; the first request time is kept);
        * ``promoted``: ``ScratchStateError`` (already promoted).

        Returns the snapshot after the change. The store does not create a
        Memory Candidate: that is the promotion flow's job.
        """
        project = validate_uuid("project_id", project_id)
        item_uuid = validate_uuid("item_id", item_id)
        now = self._now()
        async with self._transaction() as session:
            item = await self._lock_visible(session, project, item_uuid, now)
            if item.promotion_state is PromotionState.PENDING:
                return item
            if item.promotion_state is PromotionState.PROMOTED:
                raise ScratchStateError()
            await session.execute(
                update(_ITEMS)
                .where(_ITEMS.c.id == item_uuid)
                .values(
                    promotion_state=PromotionState.PENDING.value,
                    promotion_requested_at=now,
                )
            )
            return replace(
                item,
                promotion_state=PromotionState.PENDING,
                promotion_requested_at=now,
            )

    async def resolve_promotion(
        self, project_id: UUID, item_id: UUID, outcome: PromotionOutcome
    ) -> ScratchItem:
        """Record how a pending promotion ended; the exemption ends with it.

        ``outcome`` is validated by ``validate_outcome`` (validation order:
        ``project_id``, ``item_id``, ``outcome``). Locks the item row, then
        ``ScratchItemNotFoundError`` when missing, in another project or not
        visible (a pending item is always visible). By ``promotion_state``:

        * ``pending``: becomes ``outcome.value`` and ``promotion_requested_at``
          becomes None;
        * already equal to ``outcome.value``: unchanged (idempotent);
        * any other state (``none``, or the opposite outcome):
          ``ScratchStateError``.

        Returns the snapshot after the change. If the item is expired it is not
        visible afterwards and the next purge deletes it.
        """
        project = validate_uuid("project_id", project_id)
        item_uuid = validate_uuid("item_id", item_id)
        result = PromotionState(validate_outcome(outcome).value)
        now = self._now()
        async with self._transaction() as session:
            item = await self._lock_visible(session, project, item_uuid, now)
            if item.promotion_state is result:
                return item
            if item.promotion_state is not PromotionState.PENDING:
                raise ScratchStateError()
            await session.execute(
                update(_ITEMS)
                .where(_ITEMS.c.id == item_uuid)
                .values(promotion_state=result.value, promotion_requested_at=None)
            )
            return replace(item, promotion_state=result, promotion_requested_at=None)

    # -- purge ---------------------------------------------------------------------

    async def purge_expired(
        self,
        *,
        now: datetime | None = None,
        batch_size: int = DEFAULT_PURGE_BATCH_SIZE,
    ) -> PurgeResult:
        """Delete expired items that are not exempt; report exact counts.

        For the Backend's janitor: it works across all projects and does not
        take a project id. ``now`` (an aware datetime, field ``now``; the clock
        when omitted) and ``batch_size`` (1 to ``MAX_PURGE_BATCH_SIZE``, field
        ``batch_size``) are validated first (order: ``now``, ``batch_size``).

        One call is one transaction that deletes at most ``batch_size`` rows.
        An item is *purgeable* at ``now`` when ``expires_at <= now`` (at exactly
        ``expires_at`` it is expired) and it is not exempt: not pinned, promotion
        not ``pending``, and no lease with ``expires_at > now`` (a lease that
        ends exactly at ``now`` is over). Required procedure:

        1. Select up to ``batch_size + 1`` purgeable ids ordered by
           ``(expires_at, id)`` with ``FOR UPDATE OF <items> SKIP LOCKED``.
           Exempt rows are excluded by the ``WHERE`` clause (otherwise a run of
           old exempt rows would fill every batch and starve the purgeable
           ones); rows another transaction has locked are skipped, not waited
           for. ``has_more`` is True exactly when the extra
           ``(batch_size + 1)``-th id was found. Only the first ``batch_size``
           ids continue.
        2. In a **new statement**, ``DELETE`` those ids again with the same
           purgeable condition (so an exemption committed after step 1's
           snapshot but before the lock still protects the item), counting the
           rows the ``DELETE`` reports. Leases of deleted items go with them (the
           foreign key cascades). Nothing outside the two scratch tables is
           touched.
        3. ``deferred`` = the number of rows with ``expires_at <= now`` that are
           exempt at ``now``, counted after the delete in a new statement (a row
           with several reasons counts once; rows skipped only because another
           transaction had them locked are not counted anywhere: the next call
           handles them).

        Test seam: when the store was built with ``purge_probe``, call
        ``await purge_probe(session, chosen)`` exactly once, between step 1 and
        step 2, inside the same transaction, with the transaction's
        ``AsyncSession`` and the list of the ids chosen in step 1 (at most
        ``batch_size``). It is skipped when nothing was chosen. Production code
        never sets it; the tests use it to make an exemption appear at the one
        moment a concurrent transaction could, which proves that step 2 really
        re-checks. The probe's own writes on the session count for steps 2 and
        3 (same transaction).

        ``PurgeResult(purged, deferred, has_more)``: with no concurrent writers
        ``purged`` is exactly the number of rows removed, ``deferred`` exactly
        the number of expired-but-exempt rows left. Calling again is safe and
        repeats no work (idempotent). Unexpired rows are never deleted.
        """
        instant = None if now is None else validate_datetime("now", now)
        size = validate_bounded_int(
            "batch_size", batch_size, minimum=1, maximum=MAX_PURGE_BATCH_SIZE
        )
        if instant is None:
            instant = self._now()
        purgeable = and_(_ITEMS.c.expires_at <= instant, ~_exempt(instant))
        async with self._transaction() as session:
            candidates = (
                (
                    await session.execute(
                        select(_ITEMS.c.id)
                        .where(purgeable)
                        .order_by(_ITEMS.c.expires_at, _ITEMS.c.id)
                        .limit(size + 1)
                        .with_for_update(skip_locked=True, of=_ITEMS)
                    )
                )
                .scalars()
                .all()
            )
            chosen = list(candidates[:size])
            purged = 0
            if chosen:
                if self._purge_probe is not None:
                    await self._purge_probe(session, chosen)
                # A new statement: it sees exemptions committed after the SELECT.
                deleted = await session.execute(
                    delete(_ITEMS).where(_ITEMS.c.id.in_(chosen), purgeable)
                )
                purged = deleted.rowcount
            deferred = (
                await session.execute(
                    select(func.count())
                    .select_from(_ITEMS)
                    .where(_ITEMS.c.expires_at <= instant, _exempt(instant))
                )
            ).scalar_one()
        return PurgeResult(
            purged=purged, deferred=deferred, has_more=len(candidates) > size
        )
