"""``ConsolidationQueue``: priority, leases with fencing, retry, dead letter, enqueue.

Real PostgreSQL. Acceptance criteria "background consolidation queue" and
"HIGH/NORMAL/LOW優先度". The queue trusts only the database clock and cannot be told
the time, so a test moves the database-side rows (``expire_lease``, ``make_due``).
"""

import asyncio
import json
import unittest
from uuid import uuid4

from sqlalchemy import event, text

from paw_backend.memory.journal import (
    ConsolidationQueue,
    EntryNotFoundError,
    EntryNotPendingError,
    FailureKind,
    JobStatus,
    LeaseLostError,
    Priority,
)
from paw_backend.memory.journal.rules import Backoff

from .journal_support import (
    AsyncPostgresJournalTestCase,
    raise_unexpected,
    requires_postgres,
)
from .task_support import PostgresTaskTestCase


class QueueTestCase(AsyncPostgresJournalTestCase):
    async def enqueue_entries(self, *priorities: Priority) -> list:
        """One entry per priority, in one conversation; returns the receipts."""
        conversation = self.seed_conversation()
        return [
            await self.record(f"message {n}", conversation=conversation, priority=p)
            for n, p in enumerate(priorities)
        ]

    def seconds_until_available(self, job_id: int) -> float:
        return float(
            self.scalar(
                "SELECT EXTRACT(EPOCH FROM (available_at - clock_timestamp()))"
                " FROM memory_consolidation_queue WHERE id = :i",
                i=job_id,
            )
        )


@requires_postgres
class PriorityOrderTest(QueueTestCase):
    async def test_high_before_normal_before_low_then_first_in_first_out(self):
        receipts = await self.enqueue_entries(
            Priority.LOW,
            Priority.NORMAL,
            Priority.HIGH,
            Priority.NORMAL,
            Priority.HIGH,
            Priority.LOW,
        )
        claimed = []
        while (job := await self.queue.claim_next("worker-1")) is not None:
            claimed.append(job.entry_id)
        by_entry = {r.entry_id: n for n, r in enumerate(receipts)}
        self.assertEqual(
            [by_entry[entry] for entry in claimed],
            [2, 4, 1, 3, 0, 5],  # HIGH (2, 4), NORMAL (1, 3), LOW (0, 5), each FIFO
        )

    async def test_a_high_job_does_not_interrupt_a_claimed_one(self):
        (low,) = await self.enqueue_entries(Priority.LOW)
        running = await self.queue.claim_next("worker-1")
        (high,) = await self.enqueue_entries(Priority.HIGH)
        self.assertEqual(running.entry_id, low.entry_id)
        self.assertEqual(self.job_row(running.id)["status"], "claimed")
        self.assertEqual(
            (await self.queue.claim_next("worker-2")).entry_id, high.entry_id
        )

    async def test_a_retried_job_keeps_its_place_in_line(self):
        first, second = await self.enqueue_entries(Priority.NORMAL, Priority.NORMAL)
        job = await self.queue.claim_next("worker-1")
        self.assertEqual(job.entry_id, first.entry_id)
        await self.queue.fail(
            job.id, "worker-1", job.claim_count, FailureKind.WORKER_ERROR
        )
        self.make_due()
        again = await self.queue.claim_next("worker-1")
        self.assertEqual(again.entry_id, first.entry_id)  # not behind the second
        self.assertEqual(again.enqueued_at, job.enqueued_at)
        self.assertIsNotNone(second)

    async def test_an_empty_queue_returns_none(self):
        self.assertIsNone(await self.queue.claim_next("worker-1"))


@requires_postgres
class LeaseTest(QueueTestCase):
    async def test_a_claim_leases_the_job_to_one_worker(self):
        (receipt,) = await self.enqueue_entries(Priority.NORMAL)
        job = await self.queue.claim_next("worker-1")
        self.assertEqual(
            (job.status, job.claimed_by, job.claim_count, job.attempts),
            (JobStatus.CLAIMED, "worker-1", 1, 0),
        )
        self.assertEqual(
            self.scalar(
                "SELECT EXTRACT(EPOCH FROM (lease_expires_at - claimed_at))"
                " FROM memory_consolidation_queue WHERE id = :i",
                i=job.id,
            ),
            self.queue.lease_seconds,
        )
        self.assertIsNone(await self.queue.claim_next("worker-2"))
        self.assertEqual(job.entry_id, receipt.entry_id)

    async def test_an_expired_lease_is_reclaimed_and_counts_a_failed_attempt(self):
        await self.enqueue_entries(Priority.NORMAL)
        first = await self.queue.claim_next("worker-1")
        self.expire_lease(first.id)

        second = await self.queue.claim_next("worker-2")

        self.assertEqual(second.id, first.id)
        self.assertEqual(
            (
                second.claimed_by,
                second.claim_count,
                second.attempts,
                second.last_failure,
            ),
            ("worker-2", 2, 1, FailureKind.WORKER_ERROR),
        )

    async def test_the_new_holder_of_a_reclaimed_job_can_finish_it_the_old_one_cannot(
        self,
    ):
        await self.enqueue_entries(Priority.NORMAL)
        old = await self.queue.claim_next("worker-1")
        self.expire_lease(old.id)
        new = await self.queue.claim_next("worker-1")  # the SAME worker id, restarted
        self.assertEqual(new.claim_count, old.claim_count + 1)

        for call in (
            lambda: self.queue.heartbeat(old.id, "worker-1", old.claim_count),
            lambda: self.queue.fail(
                old.id, "worker-1", old.claim_count, FailureKind.WORKER_ERROR
            ),
            lambda: self.queue.dead_letter(old.id, "worker-1", old.claim_count),
        ):
            with self.subTest(call):
                with self.assertRaises(LeaseLostError):
                    await call()
        after = self.job_row(new.id)
        self.assertEqual(
            (
                after["status"],
                after["claimed_by"],
                after["claim_count"],
                after["attempts"],
            ),
            ("claimed", "worker-1", 2, 1),
        )
        await self.queue.heartbeat(new.id, "worker-1", new.claim_count)

    async def test_another_worker_cannot_use_the_lease(self):
        await self.enqueue_entries(Priority.NORMAL)
        job = await self.queue.claim_next("worker-1")
        with self.assertRaises(LeaseLostError):
            await self.queue.heartbeat(job.id, "worker-2", job.claim_count)
        self.assertEqual(self.job_row(job.id)["claimed_by"], "worker-1")

    async def test_an_expired_lease_of_the_right_generation_is_refused(self):
        await self.enqueue_entries(Priority.NORMAL)
        job = await self.queue.claim_next("worker-1")
        self.expire_lease(job.id)
        with self.assertRaises(LeaseLostError):
            await self.queue.fail(
                job.id, "worker-1", job.claim_count, FailureKind.WORKER_ERROR
            )
        self.assertEqual(self.job_row(job.id)["attempts"], 0)

    async def test_an_unknown_job_is_a_lost_lease(self):
        with self.assertRaises(LeaseLostError):
            await self.queue.heartbeat(999_999, "worker-1", 1)

    async def test_a_heartbeat_extends_the_lease_and_never_shortens_it(self):
        await self.enqueue_entries(Priority.NORMAL)
        job = await self.queue.claim_next("worker-1")
        self.execute(
            "UPDATE memory_consolidation_queue"
            " SET lease_expires_at = clock_timestamp() + interval '10 seconds'"
            " WHERE id = :i",
            i=job.id,
        )
        extended = await self.queue.heartbeat(job.id, "worker-1", job.claim_count)
        remaining = self.scalar(
            "SELECT EXTRACT(EPOCH FROM (lease_expires_at - clock_timestamp()))"
            " FROM memory_consolidation_queue WHERE id = :i",
            i=job.id,
        )
        self.assertGreater(remaining, self.queue.lease_seconds - 10)
        # A lease that is already longer is left alone.
        self.execute(
            "UPDATE memory_consolidation_queue"
            " SET lease_expires_at = clock_timestamp() + interval '2 days'"
            " WHERE id = :i",
            i=job.id,
        )
        longer = await self.queue.heartbeat(job.id, "worker-1", job.claim_count)
        self.assertGreater(longer.lease_expires_at, extended.lease_expires_at)
        self.assertGreater(
            self.scalar(
                "SELECT EXTRACT(EPOCH FROM (lease_expires_at - clock_timestamp()))"
                " FROM memory_consolidation_queue WHERE id = :i",
                i=job.id,
            ),
            86_400,
        )


@requires_postgres
class RetryAndDeadLetterTest(QueueTestCase):
    async def claim_one(self, worker="worker-1"):
        return await self.queue.claim_next(worker)

    async def test_a_failure_puts_the_job_back_with_a_delay(self):
        (receipt,) = await self.enqueue_entries(Priority.HIGH)
        job = await self.claim_one()

        failed = await self.queue.fail(
            job.id, "worker-1", job.claim_count, FailureKind.WORKER_OUTPUT_INVALID
        )

        self.assertEqual(
            (failed.status, failed.attempts, failed.deferrals, failed.last_failure),
            (JobStatus.QUEUED, 1, 0, FailureKind.WORKER_OUTPUT_INVALID),
        )
        self.assertEqual(
            (failed.claimed_by, failed.claimed_at, failed.lease_expires_at),
            (None, None, None),
        )
        self.assertEqual(failed.priority, Priority.HIGH)
        # Not claimable until the delay has passed: 30 s, the first step.
        self.assertTrue(20 < self.seconds_until_available(job.id) <= 30)
        self.assertIsNone(await self.claim_one())
        self.make_due()
        self.assertEqual((await self.claim_one()).entry_id, receipt.entry_id)

    async def test_the_delay_doubles_with_every_failure(self):
        await self.enqueue_entries(Priority.NORMAL)
        delays = []
        for _ in range(5):
            job = await self.claim_one()
            await self.queue.fail(
                job.id, "worker-1", job.claim_count, FailureKind.WORKER_ERROR
            )
            delays.append(round(self.seconds_until_available(job.id) / 10) * 10)
            self.make_due()
        # 30, 60, 120, 240 seconds (rounded to 10 s); the fifth failure is the last
        # (``max_attempts`` is 5), so the job is dead and has no delay to wait.
        self.assertEqual(delays[:4], [30, 60, 120, 240])
        self.assertEqual(self.job_row(job.id)["status"], "dead")

    async def test_the_maximum_delay_caps_the_growth(self):
        queue = self.new_queue(
            backoff=Backoff(base_seconds=100, factor=3, max_seconds=250)
        )
        await self.enqueue_entries(Priority.NORMAL)
        delays = []
        for _ in range(3):
            job = await queue.claim_next("worker-1")
            await queue.fail(
                job.id, "worker-1", job.claim_count, FailureKind.WORKER_ERROR
            )
            delays.append(round(self.seconds_until_available(job.id) / 10) * 10)
            self.make_due()
        self.assertEqual(delays, [100, 250, 250])

    async def test_the_last_counted_failure_dead_letters_and_keeps_the_observation(
        self,
    ):
        queue = self.new_queue(max_attempts=3)
        (receipt,) = await self.enqueue_entries(Priority.NORMAL)
        statuses = []
        for _ in range(3):
            job = await queue.claim_next("worker-1")
            failed = await queue.fail(
                job.id, "worker-1", job.claim_count, FailureKind.WORKER_TIMEOUT
            )
            statuses.append((failed.status, failed.attempts))
            self.make_due()
        self.assertEqual(
            statuses,
            [(JobStatus.QUEUED, 1), (JobStatus.QUEUED, 2), (JobStatus.DEAD, 3)],
        )
        dead = self.jobs_of(receipt.entry_id)[0]
        self.assertEqual(
            (dead["status"], dead["last_failure"], dead["lease_expires_at"]),
            ("dead", "worker_timeout", None),
        )
        self.assertIsNotNone(dead["finished_at"])
        self.assertIsNone(await queue.claim_next("worker-1"))
        # The observation is NOT lost: still pending, still readable.
        self.assertEqual(self.entry_row(receipt.entry_id)["state"], "pending")
        pending = await self.journal.pending_observations(
            self.user, receipt.conversation_id
        )
        self.assertEqual([p.entry_id for p in pending], [receipt.entry_id])

    async def test_an_unavailable_worker_defers_without_counting_toward_the_dead_letter(
        self,
    ):
        queue = self.new_queue(max_attempts=2)
        await self.enqueue_entries(Priority.NORMAL)
        for expected_deferrals in range(1, 8):
            job = await queue.claim_next("worker-1")
            deferred = await queue.fail(
                job.id, "worker-1", job.claim_count, FailureKind.WORKER_UNAVAILABLE
            )
            self.assertEqual(
                (deferred.status, deferred.attempts, deferred.deferrals),
                (JobStatus.QUEUED, 0, expected_deferrals),
            )
            self.make_due()

    async def test_deferrals_grow_the_delay_like_failures_do(self):
        await self.enqueue_entries(Priority.NORMAL)
        delays = []
        for _ in range(3):
            job = await self.claim_one()
            await self.queue.fail(
                job.id, "worker-1", job.claim_count, FailureKind.WORKER_UNAVAILABLE
            )
            delays.append(round(self.seconds_until_available(job.id) / 10) * 10)
            self.make_due()
        self.assertEqual(delays, [30, 60, 120])

    async def test_a_job_that_kills_its_workers_ends_in_the_dead_letter(self):
        queue = self.new_queue(max_attempts=2)
        (receipt,) = await self.enqueue_entries(Priority.NORMAL)
        for _ in range(2):  # two workers claim it and die: the lease just expires
            job = await queue.claim_next("worker-1")
            self.expire_lease(job.id)
        third = await queue.claim_next("worker-1")
        self.assertEqual(third.attempts, 2)  # the runner sees this and dead-letters
        dead = await queue.dead_letter(third.id, "worker-1", third.claim_count)
        self.assertEqual((dead.status, dead.attempts), (JobStatus.DEAD, 2))
        self.assertEqual(self.entry_row(receipt.entry_id)["state"], "pending")

    async def test_dead_lettering_a_job_with_no_failed_attempt_counts_one(self):
        await self.enqueue_entries(Priority.NORMAL)
        job = await self.claim_one()
        dead = await self.queue.dead_letter(job.id, "worker-1", job.claim_count)
        self.assertEqual(
            (dead.status, dead.attempts, dead.last_failure),
            (JobStatus.DEAD, 1, FailureKind.WORKER_ERROR),
        )


@requires_postgres
class EnqueueTest(QueueTestCase):
    async def test_enqueueing_twice_gives_the_same_job(self):
        (receipt,) = await self.enqueue_entries(Priority.NORMAL)
        first = await self.queue.enqueue(receipt.entry_id, Priority.HIGH)
        second = await self.queue.enqueue(receipt.entry_id)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.jobs_of(receipt.entry_id)), 1)
        # The priority of the existing job is not changed by a later enqueue.
        self.assertEqual(second.priority, Priority.NORMAL)

    async def test_ten_concurrent_enqueues_make_one_job(self):
        (receipt,) = await self.enqueue_entries(Priority.NORMAL)
        queues = [self.new_queue() for _ in range(10)]
        results = await asyncio.gather(
            *(q.enqueue(receipt.entry_id) for q in queues), return_exceptions=True
        )
        raise_unexpected(results)
        self.assertEqual(
            {job.id for job in results}, {self.jobs_of(receipt.entry_id)[0]["id"]}
        )
        self.assertEqual(len(self.jobs_of(receipt.entry_id)), 1)

    async def test_a_dead_letter_can_be_put_back_with_a_new_job(self):
        (receipt,) = await self.enqueue_entries(Priority.NORMAL)
        job = await self.queue.claim_next("worker-1")
        await self.queue.dead_letter(job.id, "worker-1", job.claim_count)

        again = await self.queue.enqueue(receipt.entry_id, Priority.LOW)

        self.assertNotEqual(again.id, job.id)
        self.assertEqual(
            (again.status, again.priority, again.attempts),
            (JobStatus.QUEUED, Priority.LOW, 0),
        )
        self.assertEqual(
            [j["status"] for j in self.jobs_of(receipt.entry_id)], ["dead", "queued"]
        )

    async def test_a_consolidated_entry_is_not_enqueued(self):
        (receipt,) = await self.enqueue_entries(Priority.NORMAL)
        self.execute(
            "UPDATE memory_journal_entries SET state = 'consolidated',"
            " consolidated_at = now(), outcome = '{}'::jsonb WHERE id = :e",
            e=receipt.entry_id,
        )
        job = self.jobs_of(receipt.entry_id)[0]
        self.execute(
            "UPDATE memory_consolidation_queue SET status = 'completed',"
            " finished_at = now() WHERE id = :i",
            i=job["id"],
        )
        with self.assertRaises(EntryNotPendingError):
            await self.queue.enqueue(receipt.entry_id)
        self.assertEqual(len(self.jobs_of(receipt.entry_id)), 1)

    async def test_an_unknown_entry_is_not_found(self):
        with self.assertRaises(EntryNotFoundError):
            await self.queue.enqueue(uuid4())


@requires_postgres
class ConcurrentClaimTest(QueueTestCase):
    async def test_racing_claimers_never_receive_the_same_job(self):
        receipts = await self.enqueue_entries(*([Priority.NORMAL] * 12))
        queues = [self.new_queue() for _ in range(6)]

        async def drain(number: int, queue: ConsolidationQueue):
            claimed = []
            while (job := await queue.claim_next(f"worker-{number}")) is not None:
                claimed.append(job)
            return claimed

        results = await asyncio.gather(
            *(drain(n, q) for n, q in enumerate(queues)), return_exceptions=True
        )
        raise_unexpected(results)

        jobs = [job for claimed in results for job in claimed]
        self.assertEqual(len(jobs), 12)
        self.assertEqual({job.entry_id for job in jobs}, {r.entry_id for r in receipts})
        self.assertEqual({job.claim_count for job in jobs}, {1})
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM memory_consolidation_queue WHERE claim_count <> 1"
            ),
            0,
        )

    async def test_a_claimer_does_not_wait_behind_a_locked_job(self):
        await self.enqueue_entries(Priority.NORMAL)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "SELECT id FROM memory_consolidation_queue"
                    " WHERE status = 'queued' FOR UPDATE"
                )
            )
            # Another transaction holds the only claimable row: the claim returns
            # at once with nothing, instead of waiting for it.
            job = await asyncio.wait_for(self.queue.claim_next("worker-1"), 5)
        self.assertIsNone(job)
        self.assertIsNotNone(await self.queue.claim_next("worker-1"))


@requires_postgres
class QueryPlanTest(QueueTestCase):
    """The claim and the enqueue use the partial indexes, also in a cached generic
    plan, although finished and dead jobs are kept for ever."""

    plan = PostgresTaskTestCase.plan
    generic_plan = PostgresTaskTestCase.generic_plan

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.database = self.new_database()

    async def captured(self, action):
        """Run ``action``; return the (SQL, parameters) of its queue statements."""
        captured: list[tuple[str, dict]] = []
        engine = self.queue._database.engine.sync_engine

        def before(connection, cursor, statement, parameters, context, executemany):
            if "memory_consolidation_queue" in statement:
                captured.append((statement, dict(parameters or {})))

        event.listen(engine, "before_cursor_execute", before)
        try:
            await action()
        finally:
            event.remove(engine, "before_cursor_execute", before)
        return captured

    async def test_the_claim_query_uses_a_partial_index_not_a_full_scan(
        self,
    ):
        # Many finished jobs, a few queued ones.
        (receipt,) = await self.enqueue_entries(Priority.NORMAL)
        self.execute(
            "INSERT INTO memory_consolidation_queue"
            " (entry_id, priority, priority_rank, status, finished_at)"
            " SELECT :e, 'normal', 1, 'completed', now() FROM generate_series(1, 400)",
            e=receipt.entry_id,
        )
        statements = await self.captured(lambda: self.queue.claim_next("worker-1"))
        (select_sql, parameters) = next(
            item for item in statements if "FOR UPDATE SKIP LOCKED" in item[0]
        )
        self.assertNotIn("status", parameters)  # the statuses are written into the SQL
        for mode in ("force_generic_plan", "force_custom_plan"):
            with self.subTest(mode):
                plan = json.dumps(await self.plan(select_sql, parameters, mode))
                # Whichever partial index the planner prefers (it sorts by the
                # ordering index or filters the uniqueness index), the 400 finished
                # jobs are never scanned: that needs the statuses in the SQL text.
                self.assertNotIn("Seq Scan", plan)
                self.assertTrue(
                    "ix_memory_consolidation_queue_claim_order" in plan
                    or "uq_memory_consolidation_queue_one_active_per_entry" in plan,
                    plan,
                )

    async def test_enqueue_uses_the_uniqueness_index_not_a_scan(self):
        (receipt,) = await self.enqueue_entries(Priority.NORMAL)
        statements = await self.captured(lambda: self.queue.enqueue(receipt.entry_id))
        inserts = [item for item in statements if item[0].lstrip().startswith("INSERT")]
        self.assertEqual(len(inserts), 1)
        self.assertIn(
            "ON CONFLICT (entry_id) WHERE status IN ('queued', 'claimed') DO NOTHING",
            inserts[0][0],
        )


if __name__ == "__main__":
    unittest.main()
