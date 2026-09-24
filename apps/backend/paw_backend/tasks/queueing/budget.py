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

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from paw_backend.db import Database
from paw_backend.tasks.queueing.domain import (
    BudgetKind,
    BudgetPreset,
    BudgetUsage,
    BudgetVerdict,
)
from paw_backend.tasks.queueing.errors import InvalidQueueingArgumentError


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
        raise NotImplementedError("PAW-033 stub")

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
        raise NotImplementedError("PAW-033 stub")

    async def start_runtime(self, task_id: uuid.UUID) -> None:
        """Mark the task as running now (``running_since = clock()``).

        Idempotent: when a run is already in progress nothing changes (the
        original ``running_since`` is kept). Raises ``BudgetNotConfiguredError``
        if there is no budget.
        """
        raise NotImplementedError("PAW-033 stub")

    async def stop_runtime(self, task_id: uuid.UUID) -> BudgetUsage:
        """End the run in progress: add the elapsed seconds and clear it.

        Adds ``max(floor((clock() - running_since).total_seconds()), 0)`` to the
        runtime consumption (saturating at ``MAX_CONSUMED``), sets
        ``running_since`` to ``NULL`` and returns the runtime usage. When no run
        is in progress it changes nothing and returns the current runtime usage.
        The read of ``running_since`` and the write must be atomic (a second
        concurrent stop must not add the time twice).
        """
        raise NotImplementedError("PAW-033 stub")

    async def usage(self, task_id: uuid.UUID) -> tuple[BudgetUsage, ...]:
        """The six ``BudgetUsage`` in ``BudgetKind`` declaration order.

        The runtime entry includes the run in progress (``consumed + elapsed``
        at ``clock()``). Read-only. ``BudgetNotConfiguredError`` if none.
        """
        raise NotImplementedError("PAW-033 stub")

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
        raise NotImplementedError("PAW-033 stub")
