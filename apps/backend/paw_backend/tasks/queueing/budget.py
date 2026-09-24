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

Runtime is measured with the injected ``clock`` (a zero-argument callable that
returns a timezone-aware ``datetime``; the default reads the real UTC time).
``start_runtime`` stores ``running_since`` on the ``runtime_seconds`` row;
``stop_runtime`` adds the elapsed whole seconds and clears it. While a run is in
progress ``usage`` / ``check`` report ``consumed + elapsed`` WITHOUT writing.
Elapsed whole seconds are ``floor((now - running_since).total_seconds())``, and
never negative (a clock that went backwards counts as 0).

A task without budget rows raises ``BudgetNotConfiguredError`` (from every method
except ``set_preset``, and whether or not the task exists); it is never treated
as unlimited. Only ``set_preset`` raises ``TaskNotFoundError`` for an unknown
task.
"""

import math
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from sqlalchemy import BigInteger, cast, extract, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

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
)
from paw_backend.tasks.queueing.models import BudgetUsageRow
from paw_backend.tasks.queueing.sql import FOREIGN_KEY_VIOLATION, sqlstate
from paw_backend.tasks.queueing.validation import (
    MAX_CONSUMED,
    check_amount,
    check_member,
    check_uuid,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _elapsed_seconds(now: datetime, since: datetime) -> int:
    """Whole seconds from ``since`` to ``now``, rounded down, never negative."""
    return max(math.floor((now - since).total_seconds()), 0)


class BudgetTracker:
    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """``clock`` must be callable, else ``InvalidQueueingArgumentError("clock")``.

        A clock that returns something other than a timezone-aware ``datetime``
        makes the calling method raise ``InvalidQueueingArgumentError("clock")``.
        """
        if not callable(clock):
            raise InvalidQueueingArgumentError("clock")
        self._database = database
        self._clock = clock

    async def set_preset(
        self, task_id: uuid.UUID, preset: BudgetPreset
    ) -> tuple[BudgetUsage, ...]:
        """Create or change the task's budget and return its usage.

        Creates the six rows (consumed 0) or, if they exist, sets ``preset`` and
        ``limit_value`` of each from ``PRESET_LIMITS[preset]`` and KEEPS
        ``consumed`` and ``running_since`` (a task that is already over the new
        limits is simply EXCEEDED afterwards). One transaction; concurrent calls
        and ``record`` calls never lose consumption. Returns what ``usage``
        returns. ``TaskNotFoundError`` for an unknown task (foreign key violation
        SQLSTATE 23503; other ``IntegrityError`` propagate).
        """
        check_uuid("task_id", task_id)
        check_member("preset", preset, BudgetPreset)
        now = self._now()
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
            # Consumption and a run in progress are kept.
            set_={
                "preset": upsert.excluded.preset,
                "limit_value": upsert.excluded.limit_value,
            },
        )
        try:
            async with self._database.engine.begin() as connection:
                await connection.execute(upsert)
                return await self._read_usage(connection, task_id, now)
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

    async def start_runtime(self, task_id: uuid.UUID) -> None:
        """Mark the task as running now (``running_since = clock()``).

        Idempotent: when a run is already in progress nothing changes (the
        original ``running_since`` is kept). Raises ``BudgetNotConfiguredError``
        if there is no budget.
        """
        check_uuid("task_id", task_id)
        now = self._now()
        start = (
            update(BudgetUsageRow)
            .where(
                BudgetUsageRow.task_id == task_id,
                BudgetUsageRow.kind == BudgetKind.RUNTIME_SECONDS,
                BudgetUsageRow.running_since.is_(None),
            )
            .values(running_since=now)
        )
        async with self._database.engine.begin() as connection:
            if (await connection.execute(start)).rowcount == 0:
                # A run is already in progress (nothing to do), or there is no
                # budget: reading the usage raises BudgetNotConfiguredError then.
                await self._read_usage(connection, task_id, now)

    async def stop_runtime(self, task_id: uuid.UUID) -> BudgetUsage:
        """End the run in progress: add the elapsed seconds and clear it.

        Adds ``max(floor((clock() - running_since).total_seconds()), 0)`` to the
        runtime consumption (saturating at ``MAX_CONSUMED``), sets
        ``running_since`` to ``NULL`` and returns the runtime usage. When no run
        is in progress it changes nothing and returns the current runtime usage.
        The read of ``running_since`` and the write must be atomic (a second
        concurrent stop must not add the time twice).
        """
        check_uuid("task_id", task_id)
        now = self._now()
        elapsed = func.greatest(
            cast(
                func.floor(
                    extract("epoch", literal(now) - BudgetUsageRow.running_since)
                ),
                BigInteger,
            ),
            0,
        )
        stop = (
            update(BudgetUsageRow)
            .where(
                BudgetUsageRow.task_id == task_id,
                BudgetUsageRow.kind == BudgetKind.RUNTIME_SECONDS,
                BudgetUsageRow.running_since.is_not(None),
            )
            .values(
                consumed=func.least(BudgetUsageRow.consumed + elapsed, MAX_CONSUMED),
                running_since=None,
            )
            .returning(BudgetUsageRow.consumed, BudgetUsageRow.limit_value)
        )
        async with self._database.engine.begin() as connection:
            row = (await connection.execute(stop)).one_or_none()
            if row is not None:
                return BudgetUsage(
                    BudgetKind.RUNTIME_SECONDS, row.consumed, row.limit_value
                )
            # No run in progress: report the runtime so far, or raise
            # BudgetNotConfiguredError when the task has no budget.
            usage = await self._read_usage(connection, task_id, now)
        return next(item for item in usage if item.kind is BudgetKind.RUNTIME_SECONDS)

    async def usage(self, task_id: uuid.UUID) -> tuple[BudgetUsage, ...]:
        """The six ``BudgetUsage`` in ``BudgetKind`` declaration order.

        The runtime entry includes the run in progress (``consumed + elapsed``
        at ``clock()``). Read-only. ``BudgetNotConfiguredError`` if none.
        """
        check_uuid("task_id", task_id)
        now = self._now()
        async with self._database.engine.connect() as connection:
            return await self._read_usage(connection, task_id, now)

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

    def _now(self) -> datetime:
        """The injected clock's reading; it must be a timezone-aware ``datetime``."""
        now = self._clock()
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise InvalidQueueingArgumentError("clock")
        return now

    @staticmethod
    async def _read_usage(
        connection: AsyncConnection, task_id: uuid.UUID, now: datetime
    ) -> tuple[BudgetUsage, ...]:
        """The task's six usages in ``BudgetKind`` order, a run in progress included."""
        rows = await connection.execute(
            select(
                BudgetUsageRow.kind,
                BudgetUsageRow.consumed,
                BudgetUsageRow.limit_value,
                BudgetUsageRow.running_since,
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
                consumed += _elapsed_seconds(now, row.running_since)
            usage.append(BudgetUsage(kind, consumed, row.limit_value))
        return tuple(usage)
