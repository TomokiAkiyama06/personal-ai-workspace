"""Settling abandoned connection calls, on a schedule (PAW-034).

A call through a shared connection (PAW-030) is admitted with an ``in_flight``
usage row and settled when it ends. A process that dies in between leaves the row
``in_flight`` for ever: it counts as a request but has no end, tokens or duration.
Decision 0016 (approved) gives the cleanup to the orchestrator's lane: rows that
are ``in_flight`` for much longer than the longest call are settled as ``failed``
(``internal_error``). This module is that reaper; the SQL is the connection
store's (``ConnectionStore.reap_abandoned``).

What a reaped row says (the safe choice where the Decision is silent; README):

* ``failed`` / ``internal_error``: the call's outcome is unknown, and it is not
  claimed to have succeeded;
* the tokens stay unknown (NULL). They are charged to nobody: a guess could only
  be wrong, and the task's own budget was never charged for them either (the
  budget charge is part of the settlement that never ran);
* ``finished_at`` is when the reaper found it; ``duration_ms`` is the time since the
  start, capped at the longest a call may run (its deadline ended it at the
  latest), so a runtime quota counts at most what the call could have used;
* a row is abandoned only when it started more than ``ABANDONED_CALL_AGE_SECONDS``
  ago (the longest call plus an hour for its settlement), by the database's clock,
  so a live call is never reaped.

Every reaped row is audited (``connection.usage.abandon``, the ``system`` role, the
usage row as the resource, its project). The audit is written after the settlement
committed, best effort (a failure is logged by type): the usage row itself is the
durable record, as for the connection module's own refusal events.

The loop: the first cycle comes ``FIRST_CYCLE_DELAY_SECONDS`` after the start (at
most the interval); one statement per cycle settles at most
``MAX_REAPED_PER_CYCLE`` rows (the oldest); a full batch is followed at once by
another cycle, otherwise the loop waits ``interval_seconds``. Errors are logged
by type and retried after the interval; ``stop`` (or a cancellation) ends it.
Several Backend processes may each run one: the rows are locked ``SKIP LOCKED``
and settled only while ``in_flight``.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from paw_backend.authz import AuditEvent, AuditSink, PostgresAuditSink
from paw_backend.connections.limits import MAX_REAPED_PER_CYCLE
from paw_backend.connections.store import ConnectionStore
from paw_backend.db import Database
from paw_backend.orchestrator.config import Clock, SystemClock
from paw_backend.orchestrator.errors import error_class_of
from paw_backend.orchestrator.limits import (
    DEFAULT_REAP_INTERVAL_SECONDS,
    MAX_REAP_INTERVAL_SECONDS,
    MIN_REAP_INTERVAL_SECONDS,
)
from paw_backend.orchestrator.validation import check_seconds
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger(__name__)

ACTION_ABANDON = "connection.usage.abandon"
REASON_ABANDONED = "abandoned_call"
_RESOURCE_USAGE = "connection_usage"
_SYSTEM_ROLE = "system"
AUDIT_TIMEOUT_SECONDS = 3.0
# The wait between the start of the loop and its first cycle (at most the
# interval): nothing is urgent at startup (a row is reaped a day after it began).
FIRST_CYCLE_DELAY_SECONDS = 60.0


class AbandonedCallReaper:
    def __init__(
        self,
        store: ConnectionStore,
        audit_sink: AuditSink,
        *,
        interval_seconds: float = DEFAULT_REAP_INTERVAL_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(store, ConnectionStore):
            raise TypeError("store must be a ConnectionStore")
        require_async_method(audit_sink, "record", 1)
        self._interval = check_seconds(
            "interval_seconds",
            interval_seconds,
            minimum=MIN_REAP_INTERVAL_SECONDS,
            maximum=MAX_REAP_INTERVAL_SECONDS,
        )
        clock = clock or SystemClock()
        require_async_method(clock, "sleep", 1)
        self._store = store
        self._sink = audit_sink
        self._clock = clock
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        """Ask the loop to end at its next suspension point."""
        self._stopping.set()

    async def run_cycle(self) -> int:
        """Settle one batch of abandoned rows and audit each; return how many."""
        reaped = await self._store.reap_abandoned(limit=MAX_REAPED_PER_CYCLE)
        correlation_id = uuid.uuid4()
        for usage in reaped:
            await self._audit(correlation_id, usage.id, usage.project_id)
        if reaped:
            logger.warning("Settled %d abandoned connection call(s)", len(reaped))
        return len(reaped)

    async def _audit(
        self,
        correlation_id: uuid.UUID,
        usage_id: uuid.UUID,
        project_id: uuid.UUID | None,
    ) -> None:
        try:
            event = AuditEvent(
                event_id=uuid.uuid4(),
                correlation_id=correlation_id,
                occurred_at=datetime.now(UTC),
                actor_id=None,
                actor_role=_SYSTEM_ROLE,
                action=ACTION_ABANDON,
                resource_kind=_RESOURCE_USAGE,
                resource_id=usage_id,
                project_id=project_id,
                decision="allow",
                reason=REASON_ABANDONED,
            )
            async with asyncio.timeout(AUDIT_TIMEOUT_SECONDS):
                await self._sink.record(event)
        except Exception as error:
            logger.error(
                "Auditing an abandoned connection call failed (%s)",
                error_class_of(error),
            )

    async def run(self) -> None:
        """Wait, cycle, wait, ... until :meth:`stop` or a cancellation."""
        await self._sleep(min(self._interval, FIRST_CYCLE_DELAY_SECONDS))
        while not self._stopping.is_set():
            delay = self._interval
            try:
                if await self.run_cycle() >= MAX_REAPED_PER_CYCLE:
                    delay = 0.0  # a full batch: there may be more
            except Exception as error:  # a supervisor: one bad cycle must not end it
                logger.warning(
                    "Reaping abandoned connection calls failed (%s)",
                    error_class_of(error),
                )
            await self._sleep(delay)

    async def _sleep(self, seconds: float) -> None:
        timer = asyncio.create_task(self._clock.sleep(seconds))
        waiter = asyncio.create_task(self._stopping.wait())
        try:
            await asyncio.wait({timer, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (timer, waiter):
                task.cancel()
            await asyncio.gather(timer, waiter, return_exceptions=True)


def build_connection_reaper(
    database: Database,
    *,
    interval_seconds: float = DEFAULT_REAP_INTERVAL_SECONDS,
    clock: Clock | None = None,
) -> AbandonedCallReaper:
    """The reaper the application runs, wired the way production wires it."""
    return AbandonedCallReaper(
        ConnectionStore(database),
        PostgresAuditSink(database),
        interval_seconds=interval_seconds,
        clock=clock,
    )
