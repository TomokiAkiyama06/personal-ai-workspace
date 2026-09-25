"""Per-task execution budgets (PAW-033).

A task has six budget items (``BudgetKind``): runtime, agent steps, retries, tool
calls, tokens and GPU time. The limits come from a preset (``PRESET_LIMITS``:
Standard / Long / Unlimited); the tracker stores a copy per task in
``budget_usages`` (one row per task and kind) together with the consumption.

Units. Runtime and GPU time are whole seconds. Every amount is a non-negative
``int``; floats (finite or not), ``bool``, ``str`` and ``None`` are rejected with
``InvalidQueueingArgumentError`` (see ``validation.check_amount``).

Exceeded. An item is EXCEEDED when ``consumed + planned > limit`` (``planned``
is 0 unless the caller asks "what if"); a ``None`` limit is never exceeded.
Using up a limit exactly is NOT exceeded: "max 50 steps" allows 50. To ask
"may I do one more?" call ``check(task_id, planned={kind: 1})``. There is no
warning threshold: the requirements define none.

Atomicity. ``record`` is one ``UPDATE ... SET consumed = LEAST(consumed +
:amount, MAX_CONSUMED) ... RETURNING`` statement: concurrent recorders (any
number of processes) never lose an increment, and consumed never exceeds
``MAX_CONSUMED`` (saturating, no overflow).

Runtime is measured with ONE clock, the DATABASE's (Decision 0007, 10, Proposed):
every persisted runtime instant (``running_since``, ``settled_through``) and every
elapsed time is PostgreSQL's ``clock_timestamp()``, read INSIDE the SQL statement
(``WITH clock AS (SELECT clock_timestamp() AS ts)``: one reading per statement, as
in the queue, see ``task_queue``). No process's clock is read, so workers on hosts
whose clocks disagree cannot charge a session too little (a budget bypass: a host
that is ahead writes a cutoff that puts the replacement's timer into the future of
a host that is behind) or too much. ``start_runtime`` stores ``running_since`` on
the ``runtime_seconds`` row; ``stop_runtime`` adds the elapsed whole seconds and
clears it. While a run is in progress ``usage`` / ``check`` report ``consumed +
elapsed`` (the database's reading of the statement that read the row) WITHOUT
writing. Elapsed whole seconds are ``floor((now - running_since).total_seconds())``,
and never negative (a clock that went backwards counts as 0).

Test clock. ``BudgetTracker(database, clock=..., allow_explicit_clock=True)`` is a
TEST SEAM that stands in for the database clock so that tests move time
deterministically (no sleeping): ``clock`` is a zero-argument callable that returns
a timezone-aware ``datetime``, read in Python and bound into the statement in place
of ``clock_timestamp()``. It is accepted only with ``allow_explicit_clock=True``
(the same opt-in as ``TaskQueue(allow_explicit_now=True)``); production code builds
``BudgetTracker(database)``, which uses the database clock and cannot be given a
process time. Every persisted instant of one deployment must come from the same
authority: never mix a tracker with a test clock and one without on one database.

Ordering of the timer times. The database clock is read by the statement BEFORE it
waits for a row lock (as for the queue), and a wall clock can step backwards (NTP
step, a failover to another server). So ``stop_runtime`` still records the cutoff
it settled through (``budget_usages.settled_through`` = ``greatest(now,
running_since)``) and ``start_runtime`` starts a timer at ``greatest(now,
settled_through)``, inside the (row-locked) statements: a start that took its
reading before a concurrent stop's cutoff cannot make the interval in between count
twice, and the cutoff only moves forward, so the charged intervals never overlap.
The cutoff no longer protects against clocks of different HOSTS (there is only one
authority now); it is kept because it costs nothing (the column, its CHECK and the
grant exist) and it closes the reading-before-waiting and backwards-step races.

Runtime sessions (fencing). Every ``start_runtime`` begins a new runtime session
and returns its generation (``budget_usages.runtime_generation``: it only grows
and is kept when the timer stops). ``stop_runtime`` must present that generation:
a stop of any other generation raises ``StaleRuntimeSessionError`` and changes
nothing, so a delayed stop of a superseded execution (the worker whose lease
expired and whose entry was reclaimed, or the run of a task that has been
restarted) can neither clear nor settle the newer session's timer. This is the
same idea as the ``claim_count`` of the queue lease; a dedicated counter is used
because ``claim_count`` starts again at 1 for every new queue entry (Decision
0007, 10).

A task without budget rows raises ``BudgetNotConfiguredError`` (from every method
except ``set_preset``, and whether or not the task exists); it is never treated
as unlimited. Only ``set_preset`` raises ``TaskNotFoundError`` for an unknown
task.
"""

import math
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    cast,
    extract,
    func,
    literal,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from paw_backend.db import Database
from paw_backend.tasks.errors import TaskNotFoundError
from paw_backend.tasks.queueing.domain import (
    PRESET_LIMITS,
    BudgetKind,
    BudgetPreset,
    BudgetStatus,
    BudgetUsage,
    BudgetVerdict,
)
from paw_backend.tasks.queueing.errors import (
    BudgetNotConfiguredError,
    InvalidQueueingArgumentError,
    StaleRuntimeSessionError,
)
from paw_backend.tasks.queueing.models import BudgetUsageRow
from paw_backend.tasks.queueing.sql import FOREIGN_KEY_VIOLATION, sqlstate
from paw_backend.tasks.queueing.validation import (
    MAX_CONSUMED,
    check_amount,
    check_bool,
    check_member,
    check_runtime_generation,
    check_uuid,
)


def _elapsed_seconds(now: datetime, since: datetime) -> int:
    """Whole seconds from ``since`` to ``now``, rounded down, never negative."""
    return max(math.floor((now - since).total_seconds()), 0)


class BudgetTracker:
    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
        allow_explicit_clock: bool = False,
    ) -> None:
        """Production code passes only ``database``: time is the database clock.

        ``clock`` and ``allow_explicit_clock`` (a ``bool``) are the TEST SEAM of
        the module docstring. A ``clock`` is accepted only with
        ``allow_explicit_clock=True`` and must then be callable; otherwise
        ``InvalidQueueingArgumentError("clock")`` (so a production tracker cannot
        be given a process clock). A clock that returns something other than a
        timezone-aware ``datetime`` makes the calling method raise
        ``InvalidQueueingArgumentError("clock")``.
        """
        allow = check_bool("allow_explicit_clock", allow_explicit_clock)
        if clock is not None and not (allow and callable(clock)):
            raise InvalidQueueingArgumentError("clock")
        self._database = database
        self._clock = clock

    async def set_preset(
        self, task_id: uuid.UUID, preset: BudgetPreset
    ) -> tuple[BudgetUsage, ...]:
        """Create or change the task's budget and return its usage.

        Creates the six rows (consumed 0) or, if they exist, sets ``preset`` and
        ``limit_value`` of each from ``PRESET_LIMITS[preset]`` and KEEPS
        ``consumed``, ``running_since``, ``runtime_generation`` and
        ``settled_through`` (a task that is already over the new limits is simply
        EXCEEDED afterwards). One transaction; concurrent calls and ``record``
        calls never lose consumption. Returns what ``usage`` returns.
        ``TaskNotFoundError`` for an unknown task (foreign key violation SQLSTATE
        23503; other ``IntegrityError`` propagate).
        """
        check_uuid("task_id", task_id)
        check_member("preset", preset, BudgetPreset)
        limits = PRESET_LIMITS[preset]
        upsert = insert(BudgetUsageRow).values(
            [
                {
                    "task_id": task_id,
                    "kind": kind,
                    "preset": preset,
                    "consumed": 0,
                    "limit_value": limits[kind],
                }
                for kind in BudgetKind
            ]
        )
        upsert = upsert.on_conflict_do_update(
            index_elements=[BudgetUsageRow.task_id, BudgetUsageRow.kind],
            # Consumption and a runtime session in progress are kept.
            set_={
                "preset": upsert.excluded.preset,
                "limit_value": upsert.excluded.limit_value,
            },
        )
        try:
            async with self._database.engine.begin() as connection:
                await connection.execute(upsert)
                return await self._read_usage(connection, task_id)
        except IntegrityError as error:
            if sqlstate(error) == FOREIGN_KEY_VIOLATION:
                raise TaskNotFoundError() from None
            raise

    async def record(
        self, task_id: uuid.UUID, kind: BudgetKind, amount: int
    ) -> BudgetUsage:
        """Atomically add ``amount`` to the consumption of ``kind``; return it.

        ``kind`` must be a ``BudgetKind`` member other than RUNTIME_SECONDS
        (``InvalidQueueingArgumentError("kind")``; runtime is measured, not
        reported). ``amount``: ``validation.check_amount("amount", ...)``; 0 is
        accepted and changes nothing. Nothing is written when validation fails.
        Recording is allowed even when the budget is already exceeded (the work
        happened); the returned ``BudgetUsage`` is the state after the increment.
        Raises ``BudgetNotConfiguredError`` if the task has no budget.
        """
        check_uuid("task_id", task_id)
        check_member("kind", kind, BudgetKind)
        if kind is BudgetKind.RUNTIME_SECONDS:
            raise InvalidQueueingArgumentError("kind")
        check_amount("amount", amount)
        add = (
            update(BudgetUsageRow)
            .where(BudgetUsageRow.task_id == task_id, BudgetUsageRow.kind == kind)
            .values(consumed=func.least(BudgetUsageRow.consumed + amount, MAX_CONSUMED))
            .returning(BudgetUsageRow.consumed, BudgetUsageRow.limit_value)
        )
        async with self._database.engine.begin() as connection:
            row = (await connection.execute(add)).one_or_none()
        if row is None:
            raise BudgetNotConfiguredError()
        return BudgetUsage(kind, row.consumed, row.limit_value)

    async def start_runtime(self, task_id: uuid.UUID) -> int:
        """Begin a new runtime session and return its generation (>= 1).

        The generation grows by 1 on every call and must be passed to
        ``stop_runtime``. When no run is in progress ``running_since`` is set to
        ``greatest(now, settled_through)`` where ``now`` is the database clock read
        by this statement: never before the cutoff of the last ``stop_runtime`` (a
        start whose reading precedes a concurrent stop's cutoff, or a clock that
        stepped backwards, would otherwise charge that interval twice), and plain
        ``now`` when nothing was stopped yet. When a run is already in progress (a
        worker that died without stopping, whose lease expired) the new session
        TAKES OVER: the original ``running_since`` is kept, so the time since then
        is neither lost nor counted twice, and the previous session's generation is
        superseded (its ``stop_runtime`` raises ``StaleRuntimeSessionError``). Only
        the worker that holds the queue lease may call this: the tracker does not
        read the queue, so a superseded worker that starts again would take the
        session back (Decision 0007, 10). Raises ``BudgetNotConfiguredError`` if
        there is no budget.
        """
        check_uuid("task_id", task_id)
        now = self._instant()
        start = (
            update(BudgetUsageRow)
            .where(
                BudgetUsageRow.task_id == task_id,
                BudgetUsageRow.kind == BudgetKind.RUNTIME_SECONDS,
            )
            .values(
                runtime_generation=BudgetUsageRow.runtime_generation + 1,
                # ``greatest`` ignores a NULL ``settled_through`` (nothing stopped).
                running_since=func.coalesce(
                    BudgetUsageRow.running_since,
                    func.greatest(now, BudgetUsageRow.settled_through),
                ),
            )
            .returning(BudgetUsageRow.runtime_generation)
        )
        async with self._database.engine.begin() as connection:
            row = (await connection.execute(start)).one_or_none()
        if row is None:
            raise BudgetNotConfiguredError()
        return row.runtime_generation

    async def stop_runtime(self, task_id: uuid.UUID, generation: int) -> BudgetUsage:
        """End the run of session ``generation``: add the elapsed seconds, clear it.

        ``generation`` is what ``start_runtime`` returned (an ``int`` >= 1;
        otherwise ``InvalidQueueingArgumentError("generation")``). Adds
        ``max(floor((now - running_since).total_seconds()), 0)`` to the runtime
        consumption (saturating at ``MAX_CONSUMED``; ``now`` is the database clock
        read by this statement), sets ``running_since`` to ``NULL``, records the
        cutoff ``settled_through = greatest(now, running_since)`` (the next start
        begins no earlier; the cutoff only moves forward) and returns the runtime
        usage. The read of
        ``running_since`` and the write are one atomic statement (a second
        concurrent stop must not add the time twice).

        When the session has no run in progress (it was already stopped) nothing
        changes and the current runtime usage is returned. Any OTHER generation
        (a newer session has begun, or the generation was never issued) raises
        ``StaleRuntimeSessionError`` and changes nothing: the newer session's
        ``running_since`` and the accumulated runtime are untouched.
        ``BudgetNotConfiguredError`` is raised first if the task has no budget.
        """
        check_uuid("task_id", task_id)
        check_runtime_generation(generation)
        stopped_at = self._instant()
        elapsed = func.greatest(
            cast(
                func.floor(extract("epoch", stopped_at - BudgetUsageRow.running_since)),
                BigInteger,
            ),
            0,
        )
        stop = (
            update(BudgetUsageRow)
            .where(
                BudgetUsageRow.task_id == task_id,
                BudgetUsageRow.kind == BudgetKind.RUNTIME_SECONDS,
                BudgetUsageRow.runtime_generation == generation,
                BudgetUsageRow.running_since.is_not(None),
            )
            .values(
                consumed=func.least(BudgetUsageRow.consumed + elapsed, MAX_CONSUMED),
                running_since=None,
                # Both expressions see the OLD ``running_since`` (a non-NULL, per
                # the WHERE): the cutoff is never before the run's own start.
                settled_through=func.greatest(stopped_at, BudgetUsageRow.running_since),
            )
            .returning(BudgetUsageRow.consumed, BudgetUsageRow.limit_value)
        )
        async with self._database.engine.begin() as connection:
            row = (await connection.execute(stop)).one_or_none()
            if row is not None:
                return BudgetUsage(
                    BudgetKind.RUNTIME_SECONDS, row.consumed, row.limit_value
                )
            # Nothing was stopped: no budget, another session, or no run in
            # progress (this session was stopped already).
            current = (
                await connection.execute(
                    select(BudgetUsageRow.runtime_generation).where(
                        BudgetUsageRow.task_id == task_id,
                        BudgetUsageRow.kind == BudgetKind.RUNTIME_SECONDS,
                    )
                )
            ).scalar_one_or_none()
            if current is None:
                raise BudgetNotConfiguredError()
            if current != generation:
                raise StaleRuntimeSessionError()
            # This session has already been stopped: report the runtime so far.
            usage = await self._read_usage(connection, task_id)
        return next(item for item in usage if item.kind is BudgetKind.RUNTIME_SECONDS)

    async def usage(self, task_id: uuid.UUID) -> tuple[BudgetUsage, ...]:
        """The six ``BudgetUsage`` in ``BudgetKind`` declaration order.

        The runtime entry includes the run in progress (``consumed + elapsed`` at
        the database clock read by the statement that read the row). Read-only.
        ``BudgetNotConfiguredError`` if none.
        """
        check_uuid("task_id", task_id)
        async with self._database.engine.connect() as connection:
            return await self._read_usage(connection, task_id)

    async def check(
        self,
        task_id: uuid.UUID,
        *,
        planned: Mapping[BudgetKind, int] | None = None,
    ) -> BudgetVerdict:
        """Judge the task's budget. Read-only and idempotent.

        ``usage`` of the verdict is exactly what ``usage`` returns (WITHOUT
        ``planned``). A kind is in ``exceeded`` when ``consumed + planned[kind]
        > limit`` (limit not ``None``); ``exceeded`` is in ``BudgetKind``
        declaration order. ``status`` is EXCEEDED iff ``exceeded`` is not empty.
        ``planned`` (``None`` = nothing): a mapping whose keys are ``BudgetKind``
        members (RUNTIME_SECONDS is allowed here) and whose values are valid
        amounts; otherwise ``InvalidQueueingArgumentError("planned")`` for the
        mapping / keys and ``InvalidQueueingArgumentError("amount")`` for a value.
        """
        check_uuid("task_id", task_id)
        extra: Mapping[BudgetKind, int] = {}
        if planned is not None:
            if not isinstance(planned, Mapping):
                raise InvalidQueueingArgumentError("planned")
            for kind, amount in planned.items():
                check_member("planned", kind, BudgetKind)
                check_amount("amount", amount)
            extra = planned
        usage = await self.usage(task_id)
        exceeded = tuple(
            item.kind
            for item in usage
            if item.limit is not None
            and item.consumed + extra.get(item.kind, 0) > item.limit
        )
        status = BudgetStatus.EXCEEDED if exceeded else BudgetStatus.OK
        return BudgetVerdict(status, exceeded, usage)

    def _instant(self) -> ColumnElement[datetime]:
        """The current instant as SQL: the database clock (the test clock, if any).

        Without a test clock: a scalar subquery of the CTE ``clock`` =
        ``SELECT clock_timestamp()``. ``clock_timestamp()`` (the wall clock when it
        is evaluated; ``now()`` would be the start of the transaction) is read ONCE
        per statement however often the returned expression is used in it: a CTE
        that contains a volatile function is evaluated once. The statement using it
        starts with the ``WITH`` clause SQLAlchemy adds. With a test clock: the
        clock's reading, checked to be a timezone-aware ``datetime``
        (``InvalidQueueingArgumentError("clock")`` otherwise), bound as a value.
        """
        if self._clock is None:
            sample = select(func.clock_timestamp().label("ts")).cte("clock")
            return select(sample.c.ts).scalar_subquery()
        now = self._clock()
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise InvalidQueueingArgumentError("clock")
        return literal(now, DateTime(timezone=True))

    async def _read_usage(
        self, connection: AsyncConnection, task_id: uuid.UUID
    ) -> tuple[BudgetUsage, ...]:
        """The task's six usages in ``BudgetKind`` order, a run in progress included.

        The instant is read by the same statement as the rows (``_instant``), so a
        running timer's elapsed time is measured by the database's clock.
        """
        rows = await connection.execute(
            select(
                BudgetUsageRow.kind,
                BudgetUsageRow.consumed,
                BudgetUsageRow.limit_value,
                BudgetUsageRow.running_since,
                self._instant().label("now"),
            ).where(BudgetUsageRow.task_id == task_id)
        )
        by_kind = {row.kind: row for row in rows}
        if not by_kind:
            raise BudgetNotConfiguredError()
        usage = []
        for kind in BudgetKind:
            row = by_kind[kind]
            consumed = row.consumed
            if row.running_since is not None:
                consumed += _elapsed_seconds(row.now, row.running_since)
            usage.append(BudgetUsage(kind, consumed, row.limit_value))
        return tuple(usage)
