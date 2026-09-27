"""The background consolidator: leases a job, asks the worker, applies the answer.

REQUIREMENTS.md "Background Consolidation": the heavy memory work runs in an
asynchronous worker, and "GPU service stopped" must never lose the observation.
``Consolidator.run_once`` is one turn of that loop; ``run_batch`` repeats it up to
``batch_size`` times and stops early when there is nothing to do or the worker is
unavailable (so a stopped GPU is asked once per batch, not once per job).

One turn, and where a failure leaves the job:

1. ``queue.claim_next``: the job is leased (a transaction of its own; from now on
   its ``claim_count`` is the worker's fencing token).
2. The observation's text is read (a short transaction). No database connection is
   held while the worker thinks.
3. ``worker.extract(text)`` under ``worker_timeout_seconds``. ``WorkerUnavailable
   Error`` or ``ConnectionError``: the job is DEFERRED (``fail`` with
   ``WORKER_UNAVAILABLE``): back in the queue with a growing delay, no attempt
   counted, so it never dead-letters because the GPU was off. A timeout, any other
   exception of the worker, or an output that breaks ``memory-worker-output-v1``
   (validated in full before anything is written, ``worker.parse_worker_output``)
   is a failed attempt: retried with a delay, and dead-lettered at ``max_attempts``.
4. One transaction applies the output: ``queue.lock_held`` (the lease is proven
   after the row lock; an expired or superseded lease is :class:`LeaseLostError`
   and NOTHING is written), the candidates (``applier.apply_items``), the entry
   ``pending -> consolidated`` with its outcome, and the job ``completed``. If the
   write fails, the transaction rolls back (no half-applied output) and the job is
   retried (``APPLY_FAILED``).

In every failure the observation stays ``pending``: it is still returned by
``MemoryJournal.pending_observations`` and counted by ``sync_status``. Nothing here
deletes it. A dead-lettered one is put back with ``ConsolidationQueue.enqueue``.

Nothing in this module logs or raises message text, keys or contents: log lines
carry job ids and closed codes, and a worker's exception is never read (only its
type decides the path).
"""

import asyncio
import logging
from collections import Counter

from sqlalchemy import select

from paw_backend.db import Database
from paw_backend.memory.journal import limits
from paw_backend.memory.journal.applier import (
    apply_items,
    build_outcome,
    lock_entry,
    mark_consolidated,
)
from paw_backend.memory.journal.domain import (
    EntryState,
    FailureKind,
    JobStatus,
    QueueJob,
    RunOutcome,
    RunResult,
)
from paw_backend.memory.journal.errors import (
    InputProblem,
    InvalidJournalInputError,
    LeaseLostError,
    WorkerOutputError,
    WorkerUnavailableError,
)
from paw_backend.memory.journal.models import JournalEntry
from paw_backend.memory.journal.queue import (
    ConsolidationQueue,
    complete_job,
    lock_held,
)
from paw_backend.memory.journal.sql import transaction
from paw_backend.memory.journal.validation import (
    validate_int,
    validate_seconds,
    validate_worker_id,
)
from paw_backend.memory.journal.worker import (
    MemoryWorker,
    WorkerMemory,
    check_worker,
    parse_worker_output,
)
from paw_backend.memory.models import Message

logger = logging.getLogger(__name__)

_ENTRY = JournalEntry.__table__
_MESSAGE = Message.__table__


class _Failed(Exception):  # noqa: N818 - an internal signal, never leaves this module
    def __init__(self, kind: FailureKind) -> None:
        self.kind = kind
        super().__init__(kind.value)


class Consolidator:
    def __init__(
        self,
        database: Database,
        queue: ConsolidationQueue,
        worker: MemoryWorker,
        *,
        worker_id: str,
        worker_timeout_seconds: float = limits.DEFAULT_WORKER_TIMEOUT_SECONDS,
        batch_size: int = limits.DEFAULT_BATCH_SIZE,
        lock_timeout_ms: int = limits.DEFAULT_LOCK_TIMEOUT_MS,
    ) -> None:
        """Validate the collaborators up front; a wrong one fails here, loudly.

        ``worker.extract`` must be an ``async`` function. ``worker_id``: see
        ``validate_worker_id``. ``worker_timeout_seconds`` must leave the lease at
        least twice as long (the runner does not heartbeat while it waits).
        ``batch_size``: 1 to ``MAX_BATCH_SIZE``.
        """
        if not isinstance(database, Database):
            raise InvalidJournalInputError("database", InputProblem.WRONG_TYPE)
        if not isinstance(queue, ConsolidationQueue):
            raise InvalidJournalInputError("queue", InputProblem.WRONG_TYPE)
        try:
            check_worker(worker)
        except TypeError:
            raise InvalidJournalInputError("worker", InputProblem.WRONG_TYPE) from None
        self._worker_id = validate_worker_id("worker_id", worker_id)
        self._timeout = validate_seconds(
            "worker_timeout_seconds",
            worker_timeout_seconds,
            high=limits.MAX_WORKER_TIMEOUT_SECONDS,
        )
        if queue.lease_seconds < limits.MIN_LEASE_TO_TIMEOUT_RATIO * self._timeout:
            raise InvalidJournalInputError(
                "worker_timeout_seconds", InputProblem.OUT_OF_RANGE
            )
        self._batch_size = validate_int(
            "batch_size", batch_size, low=1, high=limits.MAX_BATCH_SIZE
        )
        self._lock_timeout_ms = validate_int(
            "lock_timeout_ms", lock_timeout_ms, low=1, high=limits.MAX_LOCK_TIMEOUT_MS
        )
        self._database = database
        self._queue = queue
        self._worker = worker

    # -- one turn ------------------------------------------------------------------

    async def run_once(self) -> RunResult:
        """Claim one job and work it to its end. See the module docstring."""
        job = await self._queue.claim_next(self._worker_id)
        if job is None:
            return RunResult(RunOutcome.IDLE)
        try:
            if job.attempts >= self._queue.max_attempts:
                # Earlier workers died on this job (each reclaim of an expired lease
                # counted an attempt): do not feed it to another one.
                return await self._dead_letter(job)
            state, text = await self._read(job)
            if state is None:
                return RunResult(RunOutcome.LEASE_LOST, job.id)  # the entry is gone
            if state is EntryState.CONSOLIDATED:
                return await self._apply(job, ())
            try:
                items = await self._extract(text)
            except _Failed as failed:
                return await self._fail(job, failed.kind)
            return await self._apply(job, items)
        except LeaseLostError:
            logger.info("memory consolidation: lease lost job=%s", job.id)
            return RunResult(RunOutcome.LEASE_LOST, job.id)

    async def run_batch(self) -> tuple[RunResult, ...]:
        """Run up to ``batch_size`` turns; stop at the first idle or unavailable one."""
        results: list[RunResult] = []
        for _ in range(self._batch_size):
            result = await self.run_once()
            results.append(result)
            if result.outcome in (RunOutcome.IDLE, RunOutcome.WORKER_UNAVAILABLE):
                break
        return tuple(results)

    # -- steps -----------------------------------------------------------------------

    async def _read(self, job: QueueJob) -> tuple[EntryState | None, str]:
        """The entry's state and the text of its message (short transaction)."""
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(_ENTRY.c.state, _MESSAGE.c.content)
                    .join(_MESSAGE, _MESSAGE.c.id == _ENTRY.c.message_id)
                    .where(_ENTRY.c.id == job.entry_id)
                )
            ).first()
        if row is None:
            return None, ""
        return EntryState(row.state), row.content

    async def _extract(self, text: str) -> tuple[WorkerMemory, ...]:
        """The worker's validated memories, or ``_Failed`` with why not."""
        try:
            async with asyncio.timeout(self._timeout):
                raw = await self._worker.extract(text)
        except (WorkerUnavailableError, ConnectionError):
            raise _Failed(FailureKind.WORKER_UNAVAILABLE) from None
        except TimeoutError:
            raise _Failed(FailureKind.WORKER_TIMEOUT) from None
        except Exception:
            # The exception is never read: its text may hold the conversation.
            raise _Failed(FailureKind.WORKER_ERROR) from None
        try:
            return parse_worker_output(raw)
        except WorkerOutputError:
            raise _Failed(FailureKind.WORKER_OUTPUT_INVALID) from None

    async def _apply(self, job: QueueJob, items: tuple[WorkerMemory, ...]) -> RunResult:
        """One transaction: prove the lease, write the candidates, complete the job."""
        try:
            async with transaction(self._database, self._lock_timeout_ms) as session:
                await lock_held(session, job.id, self._worker_id, job.claim_count)
                entry = await lock_entry(session, job.entry_id)
                if entry is None:
                    raise LeaseLostError
                if entry.state is EntryState.CONSOLIDATED:
                    await complete_job(session, job.id)
                    return RunResult(RunOutcome.ALREADY_DONE, job.id)
                outcomes = await apply_items(session, entry, items)
                await mark_consolidated(session, entry, build_outcome(outcomes))
                await complete_job(session, job.id)
        except LeaseLostError:
            raise
        except Exception:
            # Rolled back: nothing was written. The exception is never read.
            return await self._fail(job, FailureKind.APPLY_FAILED)
        results = tuple(outcome.result for outcome in outcomes)
        logger.info(
            "memory consolidation: completed job=%s items=%s",
            job.id,
            dict(Counter(result.value for result in results)),
        )
        return RunResult(RunOutcome.COMPLETED, job.id, items=results)

    async def _fail(self, job: QueueJob, kind: FailureKind) -> RunResult:
        """Record the failure; the job is retried later or dead-lettered."""
        failed = await self._queue.fail(job.id, self._worker_id, job.claim_count, kind)
        logger.warning(
            "memory consolidation: failed job=%s failure=%s status=%s",
            job.id,
            kind.value,
            failed.status.value,
        )
        if kind is FailureKind.WORKER_UNAVAILABLE:
            outcome = RunOutcome.WORKER_UNAVAILABLE
        elif failed.status is JobStatus.DEAD:
            outcome = RunOutcome.DEAD_LETTERED
        else:
            outcome = RunOutcome.RETRY_SCHEDULED
        return RunResult(outcome, job.id, failure=kind)

    async def _dead_letter(self, job: QueueJob) -> RunResult:
        await self._queue.dead_letter(job.id, self._worker_id, job.claim_count)
        logger.warning("memory consolidation: dead letter job=%s", job.id)
        return RunResult(
            RunOutcome.DEAD_LETTERED, job.id, failure=FailureKind.WORKER_ERROR
        )
