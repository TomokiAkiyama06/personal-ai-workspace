"""``TaskQueue`` on a real PostgreSQL: order, leases, release / complete / cancel
and concurrent claimers. Skipped unless ``PAW_TEST_DATABASE_URL`` is set."""

import asyncio
import unittest
import uuid
from datetime import timedelta, timezone

from sqlalchemy import text

from paw_backend.tasks import TaskNotFoundError
from paw_backend.tasks.queueing import (
    InvalidQueueingArgumentError,
    LeaseLostError,
    Priority,
    QueueEntry,
    QueueStatus,
    TaskAlreadyQueuedError,
    TaskQueue,
)

from .queueing_support import (
    T0,
    PostgresQueueingTestCase,
    at,
    raise_unexpected,
    requires_postgres,
)

P = Priority


class QueueTestCase(PostgresQueueingTestCase):
    async def enqueue(
        self, priority: Priority = P.NORMAL, seconds: float = 0, queue=None
    ) -> QueueEntry:
        """A new task, enqueued at ``at(seconds)``."""
        (task_id,) = await self.make_tasks(1)
        return await (queue or self.queue).enqueue(
            task_id, now=at(seconds), priority=priority
        )

    async def claim_all(
        self, worker: str = "w1", seconds: float = 0
    ) -> list[QueueEntry]:
        claimed = []
        while (entry := await self.queue.claim_next(worker, at(seconds))) is not None:
            claimed.append(entry)
        return claimed


class ConstructorTest(unittest.TestCase):
    def test_the_lease_length_is_validated(self):
        self.assertEqual(TaskQueue(object()).lease_seconds, 60)
        self.assertEqual(TaskQueue(object(), lease_seconds=1).lease_seconds, 1)
        self.assertEqual(
            TaskQueue(object(), lease_seconds=86_400).lease_seconds, 86_400
        )
        for bad in (0, -1, 86_401, True, 1.5, "60", None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    TaskQueue(object(), lease_seconds=bad)
                self.assertEqual(caught.exception.parameter, "lease_seconds")

    def test_the_explicit_time_switch_is_a_real_bool(self):
        TaskQueue(object(), allow_explicit_now=True)
        TaskQueue(object(), allow_explicit_now=False)
        for bad in (1, 0, "yes", "", None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    TaskQueue(object(), allow_explicit_now=bad)
                self.assertEqual(caught.exception.parameter, "allow_explicit_now")


@requires_postgres
class EnqueueTest(QueueTestCase):
    async def test_a_new_entry_is_queued_with_the_default_priority(self):
        (task_id,) = await self.make_tasks(1)
        entry = await self.queue.enqueue(task_id, now=at(5))
        self.assertEqual(
            entry,
            QueueEntry(
                id=entry.id,
                task_id=task_id,
                priority=P.NORMAL,
                status=QueueStatus.QUEUED,
                enqueued_at=at(5),
                claimed_by=None,
                claimed_at=None,
                lease_expires_at=None,
                claim_count=0,
                finished_at=None,
            ),
        )
        row = await self.entry_row(entry.id)
        self.assertEqual(
            (row["task_id"], row["priority"], row["priority_rank"], row["status"]),
            (task_id, "normal", 1, "queued"),
        )
        self.assertEqual(row["enqueued_at"], at(5))

    async def test_each_priority_is_stored_with_its_rank(self):
        for priority, rank in ((P.HIGH, 0), (P.NORMAL, 1), (P.LOW, 2)):
            entry = await self.enqueue(priority)
            row = await self.entry_row(entry.id)
            with self.subTest(priority=priority):
                self.assertEqual(entry.priority, priority)
                self.assertEqual(
                    (row["priority"], row["priority_rank"]), (priority.value, rank)
                )

    async def test_times_are_compared_as_instants_not_as_local_clock_readings(self):
        tokyo = timezone(timedelta(hours=9))
        later = await self.enqueue(seconds=20)
        earlier_in_tokyo = await self.enqueue(seconds=10)
        (task_id,) = await self.make_tasks(1)
        entry = await self.queue.enqueue(task_id, now=at(5).astimezone(tokyo))
        self.assertEqual(entry.enqueued_at, at(5))
        claimed = await self.claim_all(seconds=100)
        self.assertEqual(
            [e.id for e in claimed], [entry.id, earlier_in_tokyo.id, later.id]
        )

    async def test_a_priority_must_be_a_member_not_a_string(self):
        (task_id,) = await self.make_tasks(1)
        for bad in ("high", "HIGH", None, 0, True):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    await self.queue.enqueue(task_id, now=at(0), priority=bad)
                self.assertEqual(caught.exception.parameter, "priority")
        self.assertEqual(await self.scalar("SELECT count(*) FROM queue_entries"), 0)

    async def test_invalid_task_id_and_now_are_rejected_before_the_database(self):
        (task_id,) = await self.make_tasks(1)
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            await self.queue.enqueue(str(task_id), now=at(0))
        self.assertEqual(caught.exception.parameter, "task_id")
        naive = at(0).replace(tzinfo=None)
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            await self.queue.enqueue(task_id, now=naive)
        self.assertEqual(caught.exception.parameter, "now")
        self.assertEqual(await self.scalar("SELECT count(*) FROM queue_entries"), 0)

    async def test_an_unknown_task_is_not_found(self):
        with self.assertRaises(TaskNotFoundError) as caught:
            await self.queue.enqueue(uuid.uuid4(), now=at(0))
        self.assertEqual(caught.exception.code, "task_not_found")
        self.assertEqual(await self.scalar("SELECT count(*) FROM queue_entries"), 0)

    async def test_a_task_with_a_queued_entry_cannot_be_enqueued_again(self):
        (task_id,) = await self.make_tasks(1)
        await self.queue.enqueue(task_id, now=at(0))
        with self.assertRaises(TaskAlreadyQueuedError) as caught:
            await self.queue.enqueue(task_id, now=at(1), priority=P.HIGH)
        self.assertEqual(caught.exception.code, "task_already_queued")
        self.assertEqual(await self.scalar("SELECT count(*) FROM queue_entries"), 1)

    async def test_a_task_that_is_being_worked_on_cannot_be_enqueued_again(self):
        (task_id,) = await self.make_tasks(1)
        await self.queue.enqueue(task_id, now=at(0))
        await self.queue.claim_next("w1", at(1))
        with self.assertRaises(TaskAlreadyQueuedError):
            await self.queue.enqueue(task_id, now=at(2))

    async def test_a_finished_entry_does_not_prevent_a_new_one(self):
        (task_id,) = await self.make_tasks(1)
        first = await self.queue.enqueue(task_id, now=at(0))
        await self.queue.claim_next("w1", at(1))
        await self.queue.complete(first.id, "w1", 1, at(2))
        second = await self.queue.enqueue(task_id, now=at(3))
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(await self.queue.cancel(task_id, at(4)), True)
        third = await self.queue.enqueue(task_id, now=at(5))
        self.assertEqual(len({first.id, second.id, third.id}), 3)
        self.assertEqual(await self.scalar("SELECT count(*) FROM queue_entries"), 3)

    async def test_of_simultaneous_enqueues_of_one_task_exactly_one_wins(self):
        (task_id,) = await self.make_tasks(1)
        queues = [self.new_queue() for _ in range(6)]
        results = await asyncio.gather(
            *(q.enqueue(task_id, now=at(i)) for i, q in enumerate(queues)),
            return_exceptions=True,
        )
        raise_unexpected(results, TaskAlreadyQueuedError)
        winners = [r for r in results if isinstance(r, QueueEntry)]
        losers = [r for r in results if isinstance(r, TaskAlreadyQueuedError)]
        self.assertEqual((len(winners), len(losers)), (1, 5))
        self.assertEqual(await self.scalar("SELECT count(*) FROM queue_entries"), 1)


@requires_postgres
class ClaimOrderTest(QueueTestCase):
    async def test_an_empty_queue_yields_nothing(self):
        self.assertIsNone(await self.queue.claim_next("w1", at(0)))

    async def test_a_claim_leases_the_entry_to_the_worker(self):
        queued = await self.enqueue(P.HIGH, seconds=3)
        claimed = await self.queue.claim_next("worker-1", at(10))
        self.assertEqual(
            claimed,
            QueueEntry(
                id=queued.id,
                task_id=queued.task_id,
                priority=P.HIGH,
                status=QueueStatus.CLAIMED,
                enqueued_at=at(3),
                claimed_by="worker-1",
                claimed_at=at(10),
                lease_expires_at=at(70),
                claim_count=1,
                finished_at=None,
            ),
        )
        row = await self.entry_row(queued.id)
        self.assertEqual(
            (row["status"], row["claimed_by"], row["claim_count"]),
            ("claimed", "worker-1", 1),
        )
        self.assertEqual((row["claimed_at"], row["lease_expires_at"]), (at(10), at(70)))

    async def test_higher_priority_starts_first_then_first_in_first_out(self):
        low = await self.enqueue(P.LOW, seconds=1)
        normal_early = await self.enqueue(P.NORMAL, seconds=2)
        high_early = await self.enqueue(P.HIGH, seconds=3)
        normal_late = await self.enqueue(P.NORMAL, seconds=4)
        high_late = await self.enqueue(P.HIGH, seconds=5)
        claimed = await self.claim_all(seconds=100)
        self.assertEqual(
            [e.id for e in claimed],
            [
                high_early.id,
                high_late.id,
                normal_early.id,
                normal_late.id,
                low.id,
            ],
        )

    async def test_fifo_follows_the_enqueue_time_not_the_insertion_order(self):
        later = await self.enqueue(seconds=10)
        earlier = await self.enqueue(seconds=5)
        claimed = await self.claim_all(seconds=100)
        self.assertEqual([e.id for e in claimed], [earlier.id, later.id])

    async def test_the_same_enqueue_time_falls_back_to_the_insertion_order(self):
        first = await self.enqueue(seconds=7)
        second = await self.enqueue(seconds=7)
        third = await self.enqueue(seconds=7)
        claimed = await self.claim_all(seconds=100)
        self.assertEqual([e.id for e in claimed], [first.id, second.id, third.id])

    async def test_there_is_no_aging_a_low_entry_never_overtakes_a_high_one(self):
        old_low = await self.enqueue(P.LOW, seconds=0)
        new_high = await self.enqueue(P.HIGH, seconds=10**7)
        claimed = await self.claim_all(seconds=10**8)
        self.assertEqual([e.id for e in claimed], [new_high.id, old_low.id])

    async def test_each_call_claims_exactly_one_entry(self):
        entries = [await self.enqueue(seconds=i) for i in range(3)]
        first = await self.queue.claim_next("w1", at(50))
        self.assertEqual(first.id, entries[0].id)
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM queue_entries WHERE status = 'claimed'"
            ),
            1,
        )
        second = await self.queue.claim_next("w1", at(50))
        third = await self.queue.claim_next("w2", at(50))
        self.assertEqual([second.id, third.id], [entries[1].id, entries[2].id])
        self.assertIsNone(await self.queue.claim_next("w3", at(50)))

    async def test_a_high_entry_does_not_interrupt_a_running_one(self):
        running = await self.enqueue(P.LOW, seconds=0)
        await self.queue.claim_next("w1", at(1))
        high = await self.enqueue(P.HIGH, seconds=2)
        row = await self.entry_row(running.id)
        self.assertEqual((row["status"], row["claimed_by"]), ("claimed", "w1"))
        # The next free worker gets the HIGH entry; the claimed one is untouched.
        claimed = await self.queue.claim_next("w2", at(3))
        self.assertEqual(claimed.id, high.id)
        row = await self.entry_row(running.id)
        self.assertEqual((row["status"], row["claimed_by"]), ("claimed", "w1"))

    async def test_a_claimed_entry_is_not_claimed_again_while_its_lease_is_valid(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(0))
        self.assertIsNone(await self.queue.claim_next("w2", at(30)))
        self.assertIsNone(await self.queue.claim_next("w1", at(30)))
        self.assertEqual((await self.entry_row(entry.id))["claim_count"], 1)

    async def test_finished_entries_are_never_claimed(self):
        completed = await self.enqueue(seconds=0)
        cancelled = await self.enqueue(seconds=1)
        await self.queue.claim_next("w1", at(2))
        await self.queue.complete(completed.id, "w1", 1, at(3))
        await self.queue.cancel(cancelled.task_id, at(4))
        self.assertIsNone(await self.queue.claim_next("w1", at(10**6)))

    async def test_one_worker_may_hold_several_entries(self):
        await self.enqueue(seconds=0)
        await self.enqueue(seconds=1)
        claimed = await self.claim_all("w1", seconds=5)
        self.assertEqual(len(claimed), 2)
        self.assertEqual({e.claimed_by for e in claimed}, {"w1"})

    async def test_claim_validation_touches_nothing(self):
        entry = await self.enqueue()
        for parameter, worker, now in (
            ("worker_id", "", at(0)),
            ("worker_id", "a b", at(0)),
            ("worker_id", None, at(0)),
            ("worker_id", "x" * 101, at(0)),
            ("now", "w1", at(0).replace(tzinfo=None)),
            ("now", "w1", "2030-01-01T00:00:00Z"),
            # (``now=None`` is not invalid: it means the database clock, see
            # TrustedClockTest.)
        ):
            with self.subTest(parameter=parameter, worker=worker):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    await self.queue.claim_next(worker, now)
                self.assertEqual(caught.exception.parameter, parameter)
        row = await self.entry_row(entry.id)
        self.assertEqual((row["status"], row["claim_count"]), ("queued", 0))

    async def test_the_worker_id_is_not_echoed_in_errors(self):
        secret = "top secret worker"
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            await self.queue.claim_next(secret, at(0))
        self.assertNotIn("secret", str(caught.exception))


@requires_postgres
class LeaseTest(QueueTestCase):
    async def test_the_lease_lasts_lease_seconds(self):
        entry = await self.enqueue()
        claimed = await self.queue.claim_next("w1", at(0))
        self.assertEqual(claimed.lease_expires_at, at(60))
        short = self.new_queue(lease_seconds=5)
        other = await self.enqueue(seconds=1)
        claimed = await short.claim_next("w1", at(30))
        self.assertEqual((claimed.id, claimed.lease_expires_at), (other.id, at(35)))
        self.assertNotEqual(entry.id, other.id)

    async def test_an_expired_lease_can_be_reclaimed_exactly_at_the_expiry(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(0))
        just_before = T0 + timedelta(seconds=59, microseconds=999_999)
        self.assertIsNone(await self.queue.claim_next("w2", just_before))
        reclaimed = await self.queue.claim_next("w2", at(60))
        self.assertEqual(
            (
                reclaimed.id,
                reclaimed.claimed_by,
                reclaimed.claimed_at,
                reclaimed.lease_expires_at,
                reclaimed.claim_count,
                reclaimed.status,
            ),
            (entry.id, "w2", at(60), at(120), 2, QueueStatus.CLAIMED),
        )

    async def test_a_worker_can_reclaim_its_own_expired_entry(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(0))
        again = await self.queue.claim_next("w1", at(1000))
        self.assertEqual((again.id, again.claim_count), (entry.id, 2))

    async def test_a_reclaimed_entry_keeps_its_place_in_the_order(self):
        old = await self.enqueue(seconds=0)
        newer = await self.enqueue(seconds=1)
        await self.queue.claim_next("w1", at(10))  # takes `old`
        # `old` has expired, `newer` still waits: `old` is first again.
        first = await self.queue.claim_next("w2", at(100))
        self.assertEqual(first.id, old.id)
        second = await self.queue.claim_next("w2", at(100))
        self.assertEqual(second.id, newer.id)

    async def test_an_expired_low_entry_waits_behind_a_queued_normal_entry(self):
        low = await self.enqueue(P.LOW, seconds=0)
        await self.queue.claim_next("w1", at(1))
        normal = await self.enqueue(P.NORMAL, seconds=50)
        claimed = await self.claim_all("w2", seconds=200)
        self.assertEqual([e.id for e in claimed], [normal.id, low.id])

    async def test_the_previous_holder_loses_every_right_after_a_reclaim(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(0))
        await self.queue.claim_next("w2", at(61))
        for operation in (
            self.queue.heartbeat,
            self.queue.release,
            self.queue.complete,
        ):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(LeaseLostError) as caught:
                    await operation(entry.id, "w1", 1, at(62))
                self.assertEqual(caught.exception.code, "queue_lease_lost")
        row = await self.entry_row(entry.id)
        self.assertEqual(
            (row["status"], row["claimed_by"], row["lease_expires_at"]),
            ("claimed", "w2", at(121)),
        )

    async def test_a_stale_claim_of_the_same_worker_id_cannot_act_on_the_new_claim(
        self,
    ):
        # A worker whose lease expired is claimed again by a worker with the SAME id
        # (a restarted process with a stable configured id). The old execution can
        # not be told apart by the worker id: only the claim it was given can.
        entry = await self.enqueue()
        first = await self.queue.claim_next("w1", at(0))
        second = await self.queue.claim_next("w1", at(61))
        self.assertEqual((first.claim_count, second.claim_count), (1, 2))
        for operation in (
            self.queue.heartbeat,
            self.queue.release,
            self.queue.complete,
        ):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(LeaseLostError):
                    await operation(entry.id, "w1", 1, at(62))
        row = await self.entry_row(entry.id)
        self.assertEqual(
            (row["status"], row["claimed_by"], row["claim_count"]),
            ("claimed", "w1", 2),
        )
        self.assertEqual(row["lease_expires_at"], at(121))
        # The refused calls changed nothing, and the new claim works with its own
        # generation.
        beat = await self.queue.heartbeat(entry.id, "w1", 2, at(90))
        self.assertEqual(beat.lease_expires_at, at(150))
        done = await self.queue.complete(entry.id, "w1", second.claim_count, at(91))
        self.assertEqual(
            (done.status, done.finished_at), (QueueStatus.COMPLETED, at(91))
        )

    async def test_a_released_and_claimed_again_entry_fences_the_old_generation(self):
        # No expiry involved: the same worker id releases and claims the entry again
        # (a retry loop). Its delayed call from the first execution is refused.
        entry = await self.enqueue()
        first = await self.queue.claim_next("w1", at(0))
        await self.queue.release(entry.id, "w1", first.claim_count, at(1))
        second = await self.queue.claim_next("w1", at(2))
        self.assertEqual((first.claim_count, second.claim_count), (1, 2))
        with self.assertRaises(LeaseLostError):
            await self.queue.complete(entry.id, "w1", first.claim_count, at(3))
        row = await self.entry_row(entry.id)
        self.assertEqual((row["status"], row["claim_count"]), ("claimed", 2))

    async def test_a_heartbeat_extends_the_lease(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(0))
        beat = await self.queue.heartbeat(entry.id, "w1", 1, at(30))
        self.assertEqual(
            (beat.status, beat.claimed_by, beat.claimed_at, beat.claim_count),
            (QueueStatus.CLAIMED, "w1", at(0), 1),
        )
        self.assertEqual(beat.lease_expires_at, at(90))
        self.assertEqual((await self.entry_row(entry.id))["lease_expires_at"], at(90))
        # Still leased at the old expiry.
        self.assertIsNone(await self.queue.claim_next("w2", at(60)))
        self.assertIsNone(
            await self.queue.claim_next(
                "w2", T0 + timedelta(seconds=89, microseconds=999_999)
            )
        )
        self.assertIsNotNone(await self.queue.claim_next("w2", at(90)))

    async def test_a_heartbeat_never_shortens_the_lease(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(0))
        await self.queue.heartbeat(entry.id, "w1", 1, at(30))
        beat = await self.queue.heartbeat(entry.id, "w1", 1, at(10))
        self.assertEqual(beat.lease_expires_at, at(90))

    async def test_the_lease_is_lost_at_the_expiry_instant(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(0))
        with self.assertRaises(LeaseLostError):
            await self.queue.heartbeat(entry.id, "w1", 1, at(60))
        just_before = T0 + timedelta(seconds=59, microseconds=999_999)
        beat = await self.queue.heartbeat(entry.id, "w1", 1, just_before)
        self.assertEqual(beat.lease_expires_at, just_before + timedelta(seconds=60))

    async def test_only_the_holder_of_a_valid_lease_may_act(self):
        entry = await self.enqueue()
        queued_id = entry.id
        # Not claimed yet.
        for operation in (
            self.queue.heartbeat,
            self.queue.release,
            self.queue.complete,
        ):
            with self.assertRaises(LeaseLostError):
                await operation(queued_id, "w1", 1, at(1))
        await self.queue.claim_next("w1", at(2))
        # Another worker, an unknown entry.
        for operation in (
            self.queue.heartbeat,
            self.queue.release,
            self.queue.complete,
        ):
            with self.assertRaises(LeaseLostError):
                await operation(queued_id, "w2", 1, at(3))
            with self.assertRaises(LeaseLostError):
                await operation(queued_id + 10_000, "w1", 1, at(3))
        # The right worker with another claim generation (a dead one or a future
        # one) is refused as well.
        for operation in (
            self.queue.heartbeat,
            self.queue.release,
            self.queue.complete,
        ):
            for wrong in (2, 3, 2**31 - 1):
                with self.subTest(operation=operation.__name__, claim_count=wrong):
                    with self.assertRaises(LeaseLostError):
                        await operation(queued_id, "w1", wrong, at(3))
        row = await self.entry_row(queued_id)
        self.assertEqual((row["status"], row["claimed_by"]), ("claimed", "w1"))
        self.assertEqual((row["claim_count"], row["lease_expires_at"]), (1, at(62)))

    async def test_the_lease_error_reveals_neither_worker_nor_entry(self):
        with self.assertRaises(LeaseLostError) as caught:
            await self.queue.heartbeat(987654, "secret-worker", 1, at(0))
        message = str(caught.exception)
        self.assertEqual(message, "Queue entry is not leased to this worker")

    async def test_operation_arguments_are_validated(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(0))
        for operation in (
            self.queue.heartbeat,
            self.queue.release,
            self.queue.complete,
        ):
            for parameter, entry_id, worker, claim_count, now in (
                ("entry_id", 0, "w1", 1, at(1)),
                ("entry_id", -3, "w1", 1, at(1)),
                ("entry_id", True, "w1", 1, at(1)),
                ("entry_id", str(entry.id), "w1", 1, at(1)),
                ("entry_id", 1.0, "w1", 1, at(1)),
                ("worker_id", entry.id, "bad worker", 1, at(1)),
                ("worker_id", entry.id, None, 1, at(1)),
                ("claim_count", entry.id, "w1", 0, at(1)),
                ("claim_count", entry.id, "w1", -1, at(1)),
                ("claim_count", entry.id, "w1", 2**31, at(1)),
                ("claim_count", entry.id, "w1", True, at(1)),
                ("claim_count", entry.id, "w1", "1", at(1)),
                ("claim_count", entry.id, "w1", 1.0, at(1)),
                ("claim_count", entry.id, "w1", None, at(1)),
                # The token is required: the old ``(entry, worker, now)`` call shape
                # is rejected instead of being taken for an unfenced call.
                ("claim_count", entry.id, "w1", at(1), None),
                ("now", entry.id, "w1", 1, at(1).replace(tzinfo=None)),
            ):
                with self.subTest(op=operation.__name__, parameter=parameter):
                    with self.assertRaises(InvalidQueueingArgumentError) as caught:
                        await operation(entry_id, worker, claim_count, now)
                    self.assertEqual(caught.exception.parameter, parameter)
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            await self.queue.cancel("not-a-uuid", at(1))
        self.assertEqual(caught.exception.parameter, "task_id")
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            await self.queue.cancel(entry.task_id, at(1).replace(tzinfo=None))
        self.assertEqual(caught.exception.parameter, "now")
        row = await self.entry_row(entry.id)
        self.assertEqual((row["status"], row["claimed_by"]), ("claimed", "w1"))


@requires_postgres
class ReleaseCompleteCancelTest(QueueTestCase):
    async def test_release_returns_the_entry_to_the_queue(self):
        entry = await self.enqueue(P.HIGH, seconds=4)
        await self.queue.claim_next("w1", at(10))
        released = await self.queue.release(entry.id, "w1", 1, at(20))
        self.assertEqual(
            released,
            QueueEntry(
                id=entry.id,
                task_id=entry.task_id,
                priority=P.HIGH,
                status=QueueStatus.QUEUED,
                enqueued_at=at(4),
                claimed_by=None,
                claimed_at=None,
                lease_expires_at=None,
                claim_count=1,
                finished_at=None,
            ),
        )
        row = await self.entry_row(entry.id)
        self.assertEqual(
            (row["status"], row["claimed_by"], row["lease_expires_at"]),
            ("queued", None, None),
        )

    async def test_a_released_entry_keeps_its_place_and_counts_the_claims(self):
        first = await self.enqueue(seconds=1)
        second = await self.enqueue(seconds=2)
        await self.queue.claim_next("w1", at(10))
        await self.queue.release(first.id, "w1", 1, at(11))
        again = await self.queue.claim_next("w2", at(12))
        self.assertEqual(
            (again.id, again.claim_count, again.claimed_by), (first.id, 2, "w2")
        )
        after = await self.queue.claim_next("w2", at(13))
        self.assertEqual(after.id, second.id)

    async def test_release_by_another_worker_changes_nothing(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(0))
        with self.assertRaises(LeaseLostError):
            await self.queue.release(entry.id, "w2", 1, at(1))
        self.assertEqual((await self.entry_row(entry.id))["status"], "claimed")

    async def test_complete_finishes_the_entry(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(10))
        done = await self.queue.complete(entry.id, "w1", 1, at(25))
        self.assertEqual(
            done,
            QueueEntry(
                id=entry.id,
                task_id=entry.task_id,
                priority=P.NORMAL,
                status=QueueStatus.COMPLETED,
                enqueued_at=at(0),
                claimed_by="w1",
                claimed_at=at(10),
                lease_expires_at=None,
                claim_count=1,
                finished_at=at(25),
            ),
        )
        self.assertEqual((await self.entry_row(entry.id))["status"], "completed")
        self.assertIsNone(await self.queue.claim_next("w2", at(10**6)))

    async def test_an_entry_cannot_be_completed_twice_or_after_release(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(0))
        await self.queue.complete(entry.id, "w1", 1, at(1))
        with self.assertRaises(LeaseLostError):
            await self.queue.complete(entry.id, "w1", 1, at(2))
        with self.assertRaises(LeaseLostError):
            await self.queue.heartbeat(entry.id, "w1", 1, at(2))
        with self.assertRaises(LeaseLostError):
            await self.queue.release(entry.id, "w1", 1, at(2))
        self.assertEqual((await self.entry_row(entry.id))["finished_at"], at(1))

    async def test_a_released_entry_cannot_be_used_by_its_former_holder(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(0))
        await self.queue.release(entry.id, "w1", 1, at(1))
        for operation in (
            self.queue.heartbeat,
            self.queue.release,
            self.queue.complete,
        ):
            with self.assertRaises(LeaseLostError):
                await operation(entry.id, "w1", 1, at(2))
        self.assertEqual((await self.entry_row(entry.id))["status"], "queued")

    async def test_completing_after_the_lease_expired_is_refused(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(0))
        with self.assertRaises(LeaseLostError):
            await self.queue.complete(entry.id, "w1", 1, at(60))
        row = await self.entry_row(entry.id)
        self.assertEqual((row["status"], row["finished_at"]), ("claimed", None))

    async def test_cancel_removes_a_queued_entry(self):
        entry = await self.enqueue()
        self.assertIs(await self.queue.cancel(entry.task_id, at(9)), True)
        row = await self.entry_row(entry.id)
        self.assertEqual(
            (row["status"], row["finished_at"], row["claimed_by"]),
            ("cancelled", at(9), None),
        )
        self.assertIsNone(await self.queue.claim_next("w1", at(10)))

    async def test_cancel_takes_the_entry_away_from_the_worker(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(0))
        self.assertIs(await self.queue.cancel(entry.task_id, at(5)), True)
        row = await self.entry_row(entry.id)
        self.assertEqual(
            (
                row["status"],
                row["lease_expires_at"],
                row["claimed_by"],
                row["finished_at"],
            ),
            ("cancelled", None, "w1", at(5)),
        )
        for operation in (
            self.queue.heartbeat,
            self.queue.release,
            self.queue.complete,
        ):
            with self.assertRaises(LeaseLostError):
                await operation(entry.id, "w1", 1, at(6))
        self.assertIsNone(await self.queue.claim_next("w2", at(10**6)))

    async def test_cancel_reports_false_when_there_is_nothing_to_cancel(self):
        self.assertIs(await self.queue.cancel(uuid.uuid4(), at(0)), False)
        (task_id,) = await self.make_tasks(1)
        self.assertIs(await self.queue.cancel(task_id, at(0)), False)
        entry = await self.queue.enqueue(task_id, now=at(1))
        self.assertIs(await self.queue.cancel(task_id, at(2)), True)
        self.assertIs(await self.queue.cancel(task_id, at(3)), False)
        self.assertEqual((await self.entry_row(entry.id))["finished_at"], at(2))

    async def test_cancel_does_not_touch_a_completed_entry_or_other_tasks(self):
        done = await self.enqueue()
        other = await self.enqueue(seconds=1)
        await self.queue.claim_next("w1", at(2))
        await self.queue.complete(done.id, "w1", 1, at(3))
        self.assertIs(await self.queue.cancel(done.task_id, at(4)), False)
        self.assertEqual((await self.entry_row(done.id))["status"], "completed")
        self.assertEqual((await self.entry_row(other.id))["status"], "queued")


@requires_postgres
class ConcurrencyTest(QueueTestCase):
    async def test_racing_claimers_never_receive_the_same_entry(self):
        for round_number in range(5):
            entry = await self.enqueue(seconds=round_number)
            claimers = [(f"w{i}", self.new_queue()) for i in range(8)]
            results = await asyncio.gather(
                *(
                    q.claim_next(worker, at(1000 + round_number))
                    for worker, q in claimers
                )
            )
            winners = [r for r in results if r is not None]
            with self.subTest(round=round_number):
                self.assertEqual(len(winners), 1)
                self.assertEqual(winners[0].id, entry.id)
                row = await self.entry_row(entry.id)
                self.assertEqual(row["claim_count"], 1)
                self.assertEqual(row["claimed_by"], winners[0].claimed_by)
            await self.queue.complete(
                entry.id,
                winners[0].claimed_by,
                winners[0].claim_count,
                at(1000 + round_number),
            )

    async def test_many_claimers_share_many_entries_without_overlap(self):
        entries = [await self.enqueue(seconds=i) for i in range(6)]
        claimers = [(f"w{i}", self.new_queue()) for i in range(12)]
        results = await asyncio.gather(
            *(q.claim_next(worker, at(100)) for worker, q in claimers)
        )
        winners = [r for r in results if r is not None]
        self.assertEqual(len(winners), 6)
        self.assertEqual({w.id for w in winners}, {e.id for e in entries})
        self.assertEqual(len({w.claimed_by for w in winners}), 6)
        rows = await self.rows("SELECT claim_count, status FROM queue_entries")
        self.assertEqual(
            {(r["claim_count"], r["status"]) for r in rows}, {(1, "claimed")}
        )

    async def test_racing_claimers_take_the_most_urgent_entries(self):
        high = await self.enqueue(P.HIGH, seconds=3)
        normal = await self.enqueue(P.NORMAL, seconds=2)
        low = await self.enqueue(P.LOW, seconds=1)
        claimers = [(f"w{i}", self.new_queue()) for i in range(2)]
        results = await asyncio.gather(
            *(q.claim_next(worker, at(100)) for worker, q in claimers)
        )
        self.assertEqual({r.id for r in results}, {high.id, normal.id})
        self.assertEqual((await self.entry_row(low.id))["status"], "queued")

    async def test_racing_claimers_of_an_expired_lease_have_one_winner(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w0", at(0))
        claimers = [(f"w{i}", self.new_queue()) for i in range(1, 9)]
        results = await asyncio.gather(
            *(q.claim_next(worker, at(61)) for worker, q in claimers)
        )
        winners = [r for r in results if r is not None]
        self.assertEqual(len(winners), 1)
        row = await self.entry_row(entry.id)
        self.assertEqual(
            (row["claim_count"], row["claimed_by"]), (2, winners[0].claimed_by)
        )
        self.assertEqual(row["lease_expires_at"], at(121))

    async def test_an_entry_locked_by_another_transaction_is_skipped_not_waited_for(
        self,
    ):
        top = await self.enqueue(P.HIGH, seconds=0)
        second = await self.enqueue(P.NORMAL, seconds=1)
        async with self.database.engine.connect() as holder:
            await holder.execute(
                text("SELECT id FROM queue_entries WHERE id = :id FOR UPDATE"),
                {"id": top.id},
            )
            async with asyncio.timeout(15):
                claimed = await self.new_queue().claim_next("w1", at(10))
            self.assertEqual(claimed.id, second.id)
            # Only the locked entry is left: nothing to claim, and no waiting.
            async with asyncio.timeout(15):
                self.assertIsNone(await self.new_queue().claim_next("w2", at(10)))
            await holder.rollback()
        claimed = await self.queue.claim_next("w3", at(11))
        self.assertEqual(claimed.id, top.id)

    async def test_a_claim_and_a_cancel_racing_leave_a_consistent_entry(self):
        entry = await self.enqueue()
        claimer = self.new_queue()
        canceller = self.new_queue()
        claimed, cancelled = await asyncio.gather(
            claimer.claim_next("w1", at(5)), canceller.cancel(entry.task_id, at(5))
        )
        row = await self.entry_row(entry.id)
        self.assertIs(cancelled, True)
        # Whatever the order, the entry ends cancelled and nobody may act on it.
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["lease_expires_at"])
        if claimed is not None:
            with self.assertRaises(LeaseLostError):
                await self.queue.complete(entry.id, "w1", 1, at(6))
        self.assertIsNone(await self.queue.claim_next("w2", at(10**6)))


@requires_postgres
class TrustedClockTest(QueueTestCase):
    """A production queue never takes a time from its caller: the database clock
    decides what is expired, and stamps every time it stores."""

    def production_queue(self, **kwargs) -> TaskQueue:
        """A queue as production builds it: no explicit time is accepted."""
        return TaskQueue(self.new_database(), **kwargs)

    async def database_time(self):
        return await self.scalar("SELECT clock_timestamp()")

    async def claimed_entry(self, queue: TaskQueue, worker: str = "w1") -> QueueEntry:
        (task_id,) = await self.make_tasks(1)
        await queue.enqueue(task_id)
        claimed = await queue.claim_next(worker)
        self.assertEqual((claimed.task_id, claimed.claimed_by), (task_id, worker))
        return claimed

    async def until(self, sql: str, **parameters) -> None:
        """Poll a query that returns a boolean until it is true (30 s at most)."""
        for _ in range(600):
            if await self.scalar(sql, **parameters):
                return
            await asyncio.sleep(0.05)
        self.fail("the condition was not reached in time")

    async def expire_lease(self, entry_id: int) -> None:
        """The lease ran out on the database clock (moved, never slept)."""
        await self.owner_sql(
            "UPDATE queue_entries SET claimed_at = now() - interval '2 minutes', "
            "lease_expires_at = now() - interval '1 second' WHERE id = :id",
            id=entry_id,
        )

    async def test_a_caller_supplied_future_time_cannot_steal_a_live_lease(self):
        entry = await self.enqueue()
        await self.queue.claim_next("w1", at(0))  # w1 holds the lease until at(60)
        # A queue as production builds it, called by a worker whose clock is far ahead.
        production = TaskQueue(self.database)
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            await production.claim_next("w2", at(10**6))
        self.assertEqual(caught.exception.parameter, "now")
        row = await self.entry_row(entry.id)
        self.assertEqual((row["claimed_by"], row["claim_count"]), ("w1", 1))
        # The database clock (which is before at(60)) says the lease is alive.
        self.assertIsNone(await production.claim_next("w2"))
        row = await self.entry_row(entry.id)
        self.assertEqual((row["claimed_by"], row["claim_count"]), ("w1", 1))

    async def test_expiry_is_judged_after_the_wait_for_the_row_lock(self):
        # The heartbeat starts while the lease is alive and then waits for a row
        # lock past the lease end. The transaction start time (``now()``) would say
        # "alive"; the time of the decision (``clock_timestamp()``) says expired.
        production = self.production_queue()
        claimed = await self.claimed_entry(production)
        await self.owner_sql(
            "UPDATE queue_entries SET claimed_at = clock_timestamp() - interval "
            "'1 minute', lease_expires_at = clock_timestamp() + interval '3 seconds' "
            "WHERE id = :id",
            id=claimed.id,
        )
        async with self.database.engine.connect() as holder:
            await holder.execute(
                text("SELECT id FROM queue_entries WHERE id = :id FOR UPDATE"),
                {"id": claimed.id},
            )
            heartbeat = asyncio.create_task(
                production.heartbeat(claimed.id, "w1", claimed.claim_count)
            )
            # It is blocked on the row, and then the lease runs out while it waits.
            await self.until(
                "SELECT count(*) > 0 FROM pg_locks "
                "WHERE locktype = 'transactionid' AND NOT granted"
            )
            await self.until(
                "SELECT clock_timestamp() > lease_expires_at FROM queue_entries "
                "WHERE id = :id",
                id=claimed.id,
            )
            await holder.rollback()
            with self.assertRaises(LeaseLostError):
                async with asyncio.timeout(30):
                    await heartbeat

    async def test_a_lease_is_granted_from_one_reading_of_the_clock(self):
        # ``claimed_at`` and the end of the lease come from the same reading, so
        # they are exactly ``lease_seconds`` apart on every claim.
        production = self.production_queue(lease_seconds=60)
        for task_id in await self.make_tasks(60):
            await production.enqueue(task_id)
        claimed = []
        for index in range(60):
            claimed.append(await production.claim_next(f"w{index}"))
        for entry in claimed:
            self.assertEqual(
                entry.lease_expires_at - entry.claimed_at, timedelta(seconds=60)
            )

    async def test_a_production_queue_rejects_every_caller_supplied_time(self):
        production = self.production_queue()
        claimed = await self.claimed_entry(production)
        (other_task,) = await self.make_tasks(1)
        for name, call in (
            ("enqueue", lambda now: production.enqueue(other_task, now=now)),
            ("claim_next", lambda now: production.claim_next("w2", now)),
            (
                "heartbeat",
                lambda now: production.heartbeat(
                    claimed.id, "w1", claimed.claim_count, now
                ),
            ),
            (
                "release",
                lambda now: production.release(
                    claimed.id, "w1", claimed.claim_count, now
                ),
            ),
            (
                "complete",
                lambda now: production.complete(
                    claimed.id, "w1", claimed.claim_count, now
                ),
            ),
            ("cancel", lambda now: production.cancel(claimed.task_id, now)),
        ):
            # A valid time, a far-future time, a naive time and a non-time: none of
            # them is accepted, so none can be used to influence the outcome.
            for now in (at(0), at(10**6), at(0).replace(tzinfo=None), "2030-01-01"):
                with self.subTest(operation=name, now=repr(now)):
                    with self.assertRaises(InvalidQueueingArgumentError) as caught:
                        await call(now)
                    self.assertEqual(caught.exception.parameter, "now")
        row = await self.entry_row(claimed.id)
        self.assertEqual(
            (row["status"], row["claimed_by"], row["claim_count"]), ("claimed", "w1", 1)
        )
        self.assertEqual(await self.scalar("SELECT count(*) FROM queue_entries"), 1)

    async def test_the_database_clock_decides_that_a_lease_expired(self):
        production = self.production_queue()
        claimed = await self.claimed_entry(production, "w1")
        self.assertEqual(
            claimed.lease_expires_at - claimed.claimed_at, timedelta(seconds=60)
        )
        self.assertIsNone(await production.claim_next("w2"))

        await self.expire_lease(claimed.id)
        with self.assertRaises(LeaseLostError):
            await production.heartbeat(claimed.id, "w1", claimed.claim_count)
        reclaimed = await production.claim_next("w2")
        self.assertEqual(
            (reclaimed.id, reclaimed.claimed_by, reclaimed.claim_count),
            (claimed.id, "w2", 2),
        )
        with self.assertRaises(LeaseLostError):
            await production.complete(claimed.id, "w1", claimed.claim_count)

    async def test_release_and_complete_are_refused_after_expiry_on_the_database_clock(
        self,
    ):
        production = self.production_queue()
        for operation in (
            production.heartbeat,
            production.release,
            production.complete,
        ):
            with self.subTest(operation=operation.__name__):
                await self.owner_sql("TRUNCATE queue_entries")
                claimed = await self.claimed_entry(production)
                await operation(claimed.id, "w1", claimed.claim_count)  # alive: allowed
                await self.owner_sql("TRUNCATE queue_entries")
                claimed = await self.claimed_entry(production)
                await self.expire_lease(claimed.id)
                with self.assertRaises(LeaseLostError):
                    await operation(claimed.id, "w1", claimed.claim_count)

    async def test_a_lease_that_is_still_live_on_the_database_clock_is_kept(self):
        production = self.production_queue()
        claimed = await self.claimed_entry(production, "w1")
        await self.owner_sql(
            "UPDATE queue_entries SET lease_expires_at = now() + interval '1 hour' "
            "WHERE id = :id",
            id=claimed.id,
        )
        self.assertIsNone(await production.claim_next("w2"))
        beat = await production.heartbeat(claimed.id, "w1", claimed.claim_count)
        # a heartbeat never shortens a lease
        self.assertGreater(
            beat.lease_expires_at - beat.claimed_at, timedelta(minutes=59)
        )

    async def test_heartbeat_extends_a_short_lease_to_the_database_time_plus_the_lease(
        self,
    ):
        production = self.production_queue()
        claimed = await self.claimed_entry(production)
        await self.owner_sql(
            "UPDATE queue_entries SET lease_expires_at = now() + interval '1 second' "
            "WHERE id = :id",
            id=claimed.id,
        )
        before = await self.database_time()
        beat = await production.heartbeat(claimed.id, "w1", claimed.claim_count)
        after = await self.database_time()
        self.assertGreaterEqual(beat.lease_expires_at, before + timedelta(seconds=60))
        self.assertLessEqual(beat.lease_expires_at, after + timedelta(seconds=60))

    async def test_the_database_clock_stamps_every_stored_time(self):
        production = self.production_queue(lease_seconds=90)
        (first, second) = await self.make_tasks(2)
        before = await self.database_time()
        entry = await production.enqueue(first)
        claimed = await production.claim_next("w1")
        completed = await production.complete(claimed.id, "w1", claimed.claim_count)
        await production.enqueue(second)
        cancelled = await production.cancel(second)
        after = await self.database_time()

        self.assertTrue(cancelled)
        self.assertLessEqual(before, entry.enqueued_at)
        self.assertLessEqual(entry.enqueued_at, claimed.claimed_at)
        self.assertEqual(
            claimed.lease_expires_at - claimed.claimed_at, timedelta(seconds=90)
        )
        self.assertLessEqual(claimed.claimed_at, completed.finished_at)
        self.assertLessEqual(completed.finished_at, after)
        self.assertIsNone(completed.lease_expires_at)
        (row,) = await self.rows(
            "SELECT * FROM queue_entries WHERE task_id = :t", t=second
        )
        self.assertEqual(row["status"], "cancelled")
        self.assertLessEqual(completed.finished_at, row["finished_at"])
        self.assertLessEqual(row["finished_at"], after)

    async def test_the_order_still_follows_priority_then_arrival_on_the_database_clock(
        self,
    ):
        production = self.production_queue()
        tasks = await self.make_tasks(4)
        normal_first = await production.enqueue(tasks[0])
        low = await production.enqueue(tasks[1], priority=P.LOW)
        high = await production.enqueue(tasks[2], priority=P.HIGH)
        normal_second = await production.enqueue(tasks[3])
        self.assertLessEqual(normal_first.enqueued_at, normal_second.enqueued_at)
        claimed = [await production.claim_next("w1") for _ in range(4)]
        self.assertEqual(
            [entry.id for entry in claimed],
            [high.id, normal_first.id, normal_second.id, low.id],
        )
        self.assertIsNone(await production.claim_next("w1"))

    async def test_none_means_the_database_clock_also_for_a_queue_with_the_test_seam(
        self,
    ):
        (task_id,) = await self.make_tasks(1)
        before = await self.database_time()
        entry = await self.queue.enqueue(task_id, now=None)
        claimed = await self.queue.claim_next("w1", None)
        after = await self.database_time()
        self.assertEqual(claimed.id, entry.id)
        self.assertLessEqual(before, entry.enqueued_at)
        self.assertLessEqual(claimed.claimed_at, after)
        self.assertEqual(
            claimed.lease_expires_at - claimed.claimed_at, timedelta(seconds=60)
        )


if __name__ == "__main__":
    unittest.main()
