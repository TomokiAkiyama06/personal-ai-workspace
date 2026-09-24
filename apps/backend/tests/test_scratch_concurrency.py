"""Row locks, SKIP LOCKED and the purge / lease race (real PostgreSQL).

Blocking is provoked with a second connection that holds a row lock, so nothing
here depends on scheduling luck: "is the operation still waiting" checks use a
short sleep and only ever fail when the operation did *not* wait; every wait
for completion has a generous deadline.
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from paw_backend.research.scratch import (
    PromotionOutcome,
    PurgeResult,
    ScratchBusyError,
    ScratchItemNotFoundError,
)

from .scratch_support import T0, FakeClock, PostgresScratchTestCase, requires_postgres

HOUR = timedelta(hours=1)
SECOND = timedelta(seconds=1)
DEADLINE = 15  # seconds; only a hung implementation ever waits this long
STILL_WAITING = 0.5  # seconds


@requires_postgres
class PurgeSkipsLockedRowsTest(PostgresScratchTestCase):
    async def test_a_locked_item_is_skipped_not_waited_for_and_survives(self):
        locked = self.seed_item(expires_at=T0 - HOUR)
        free = self.seed_item(expires_at=T0 - HOUR)
        _, transaction = self.lock_item(locked)

        result = await asyncio.wait_for(self.store.purge_expired(), DEADLINE)

        self.assertEqual(result, PurgeResult(purged=1, deferred=0, has_more=False))
        self.assertEqual(self.item_ids(), {locked})
        self.assertNotIn(free, self.item_ids())
        transaction.commit()
        self.assertEqual(await self.store.purge_expired(), PurgeResult(1, 0, False))
        self.assertEqual(self.item_ids(), set())

    async def test_an_acquire_that_has_not_committed_yet_keeps_the_item(self):
        item_id = self.seed_item(expires_at=T0 - HOUR)
        connection, transaction = self.lock_item(item_id)
        # What an acquire in flight looks like: the row lock plus an uncommitted lease.
        connection.execute(
            text(
                "INSERT INTO research_scratch_leases (item_id, holder_id, leased_at,"
                " expires_at) VALUES (:item, :holder, :leased, :expires)"
            ),
            {
                "item": item_id,
                "holder": uuid4(),
                "leased": T0,
                "expires": T0 + timedelta(minutes=5),
            },
        )

        during = await self.store.purge_expired()
        transaction.commit()
        after = await self.store.purge_expired()

        self.assertEqual(during, PurgeResult(0, 0, False))
        self.assertEqual(after, PurgeResult(0, 1, False))
        self.assertEqual(self.item_ids(), {item_id})

    async def test_a_locked_item_that_is_exempt_is_still_counted_as_deferred(self):
        item_id = self.seed_item(expires_at=T0 - HOUR, pinned=True)
        self.lock_item(item_id)

        result = await asyncio.wait_for(self.store.purge_expired(), DEADLINE)

        self.assertEqual(result, PurgeResult(0, 1, False))
        self.assertEqual(self.item_ids(), {item_id})

    async def test_two_purges_at_once_delete_every_item_exactly_once(self):
        ids = {self.seed_item(expires_at=T0 - HOUR) for _ in range(30)}
        first, second = self.new_store(), self.new_store()

        results = await asyncio.wait_for(
            asyncio.gather(
                first.purge_expired(batch_size=100),
                second.purge_expired(batch_size=100),
            ),
            DEADLINE,
        )

        self.assertEqual(sum(result.purged for result in results), 30)
        self.assertEqual(sum(result.deferred for result in results), 0)
        self.assertFalse(ids & self.item_ids())


@requires_postgres
class PurgeRechecksBeforeDeletingTest(PostgresScratchTestCase):
    """An exemption that shows up after the candidates were chosen still protects.

    In production that happens when a transaction commits a lease or a pin
    after the purge's ``SELECT`` took its snapshot but before it locked the row.
    The ``purge_probe`` seam makes it happen at exactly that moment.
    """

    async def test_exemptions_that_appear_after_the_selection_protect_the_item(self):
        pinned = self.seed_item(expires_at=T0 - 5 * HOUR)
        leased = self.seed_item(expires_at=T0 - 4 * HOUR)
        pending = self.seed_item(expires_at=T0 - 3 * HOUR)
        plain = [self.seed_item(expires_at=T0 - 2 * HOUR) for _ in range(2)]
        seen = []

        async def probe(session, ids):
            seen.append(list(ids))
            await session.execute(
                text("UPDATE research_scratch_items SET pinned = true WHERE id = :id"),
                {"id": pinned},
            )
            await session.execute(
                text(
                    "INSERT INTO research_scratch_leases (item_id, holder_id,"
                    " leased_at, expires_at) VALUES (:id, gen_random_uuid(), :now,"
                    " :ends)"
                ),
                {"id": leased, "now": T0, "ends": T0 + 5 * SECOND * 60},
            )
            await session.execute(
                text(
                    "UPDATE research_scratch_items SET promotion_state = 'pending',"
                    " promotion_requested_at = :now WHERE id = :id"
                ),
                {"id": pending, "now": T0},
            )

        store = self.new_store(purge_probe=probe)

        result = await store.purge_expired()

        self.assertEqual(result, PurgeResult(purged=2, deferred=3, has_more=False))
        self.assertEqual(self.item_ids(), {pinned, leased, pending})
        self.assertEqual(len(seen), 1)
        self.assertEqual(set(seen[0]), {pinned, leased, pending, *plain})

    async def test_the_probe_sees_only_the_chosen_batch_and_is_skipped_when_empty(self):
        ids = [self.seed_item(expires_at=T0 - (5 - n) * HOUR) for n in range(5)]
        seen = []

        async def probe(session, chosen):
            seen.append(list(chosen))

        store = self.new_store(purge_probe=probe)

        result = await store.purge_expired(batch_size=2)
        nothing = await self.new_store(purge_probe=probe).purge_expired(
            now=T0 - 9 * HOUR
        )

        self.assertEqual(result, PurgeResult(2, 0, True))
        self.assertEqual(nothing, PurgeResult(0, 0, False))
        self.assertEqual(seen, [ids[:2]])


@requires_postgres
class OperationsWaitForTheRowLockTest(PostgresScratchTestCase):
    async def test_every_change_of_an_item_waits_and_then_reports_busy(self):
        busy_store = self.new_store(lock_timeout_ms=100)
        item_id = self.seed_pending(expires_at=T0 + HOUR)
        holder = uuid4()
        self.seed_lease(item_id, T0 + timedelta(minutes=5), holder_id=holder)
        _, transaction = self.lock_item(item_id)
        project = self.project_id
        calls = {
            "pin": lambda: busy_store.pin(project, item_id),
            "unpin": lambda: busy_store.unpin(project, item_id),
            "acquire_use": lambda: busy_store.acquire_use(project, item_id, uuid4()),
            "release_use": lambda: busy_store.release_use(project, item_id, holder),
            "request_promotion": lambda: busy_store.request_promotion(project, item_id),
            "resolve_promotion": lambda: busy_store.resolve_promotion(
                project, item_id, PromotionOutcome.PROMOTED
            ),
        }
        before = self.row(item_id), self.lease_rows(item_id)

        for name, call in calls.items():
            with self.subTest(name):
                with self.assertRaises(ScratchBusyError) as raised:
                    await asyncio.wait_for(call(), DEADLINE)
                self.assertEqual(
                    str(raised.exception), "Scratch item is busy; retry later"
                )

        self.assertEqual((self.row(item_id), self.lease_rows(item_id)), before)
        transaction.rollback()

    async def test_the_operations_work_again_once_the_lock_is_gone(self):
        busy_store = self.new_store(lock_timeout_ms=100)
        item_id = self.seed_item(expires_at=T0 + HOUR)
        _, transaction = self.lock_item(item_id)
        with self.assertRaises(ScratchBusyError):
            await busy_store.pin(self.project_id, item_id)

        transaction.rollback()
        item = await busy_store.pin(self.project_id, item_id)

        self.assertTrue(item.pinned)

    async def test_a_change_waits_for_the_lock_and_then_completes(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        _, transaction = self.lock_item(item_id)
        pending = self.spawn(self.store.pin(self.project_id, item_id))

        await asyncio.sleep(STILL_WAITING)
        self.assertWaiting(pending)
        self.assertFalse(self.row(item_id)["pinned"])
        transaction.commit()
        item = await asyncio.wait_for(pending, DEADLINE)

        self.assertTrue(item.pinned)
        self.assertTrue(self.row(item_id)["pinned"])

    async def test_an_acquire_that_waited_for_a_purge_finds_the_item_gone(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        connection, transaction = self.lock_item(item_id)
        pending = self.spawn(self.store.acquire_use(self.project_id, item_id, uuid4()))

        await asyncio.sleep(STILL_WAITING)
        self.assertWaiting(pending)
        # What a purge that won the lock does before it commits.
        connection.execute(
            text("DELETE FROM research_scratch_items WHERE id = :id"), {"id": item_id}
        )
        transaction.commit()

        with self.assertRaises(ScratchItemNotFoundError):
            await asyncio.wait_for(pending, DEADLINE)
        self.assertEqual(self.table_count("research_scratch_leases"), 0)

    async def test_an_acquire_that_waited_gets_its_lease_when_the_item_is_still_there(
        self,
    ):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        _, transaction = self.lock_item(item_id)
        holder = uuid4()
        pending = self.spawn(self.store.acquire_use(self.project_id, item_id, holder))

        await asyncio.sleep(STILL_WAITING)
        self.assertWaiting(pending)
        transaction.commit()
        lease = await asyncio.wait_for(pending, DEADLINE)

        self.assertEqual(lease.holder_id, holder)
        self.assertIn(holder, self.lease_rows(item_id))

    async def test_add_waits_for_a_locked_task_row_and_then_reports_busy(self):
        busy_store = self.new_store(lock_timeout_ms=100)
        task_id = self.seed_task()
        _, transaction = self.hold_row_lock(
            "SELECT id FROM tasks WHERE id = :id FOR UPDATE", id=task_id
        )

        with self.assertRaises(ScratchBusyError):
            await asyncio.wait_for(
                busy_store.add(
                    self.project_id,
                    created_by=self.user_id,
                    task_id=task_id,
                    summary="s",
                ),
                DEADLINE,
            )

        self.assertEqual(self.item_ids(), set())
        transaction.rollback()

    async def test_a_task_that_is_being_updated_does_not_block_add(self):
        # The task lifecycle updates tasks all the time; that must not stall research.
        task_id = self.seed_task()
        self.hold_row_lock(
            "UPDATE tasks SET updated_at = now() WHERE id = :id RETURNING id",
            id=task_id,
        )

        item = await asyncio.wait_for(
            self.store.add(
                self.project_id, created_by=self.user_id, task_id=task_id, summary="s"
            ),
            DEADLINE,
        )

        self.assertEqual(item.task_id, task_id)

    async def test_reads_do_not_wait_for_row_locks(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        self.lock_item(item_id)

        item = await asyncio.wait_for(
            self.store.get(self.project_id, item_id), DEADLINE
        )
        listed = await asyncio.wait_for(
            self.store.list_items(self.project_id), DEADLINE
        )

        self.assertEqual(item.id, item_id)
        self.assertEqual([entry.id for entry in listed], [item_id])


@requires_postgres
class PurgeRacesLeaseTest(PostgresScratchTestCase):
    """The item's TTL ends at ``T0 + 10 s``. The acquiring side works at ``T0``
    (the item is alive), the purging side at ``T0 + 20 s`` (it is expired)."""

    ITERATIONS = 25

    async def one_round(self, exempting, purging, exempt_call, item_id):
        """One item: ``exempt_call`` and a purge start together; returns both."""
        barrier = asyncio.Barrier(2)

        async def exempt():
            await barrier.wait()
            try:
                await exempt_call(exempting, item_id)
            except ScratchItemNotFoundError:
                return False
            return True

        async def purge():
            await barrier.wait()
            return await purging.purge_expired(now=T0 + 20 * SECOND)

        return await asyncio.wait_for(asyncio.gather(exempt(), purge()), DEADLINE)

    async def race(self, exempt_call):
        """Run ``exempt_call(store, item_id)`` against a purge, repeatedly.

        Whatever the interleaving, exactly one of two outcomes may happen:
        the exemption was granted and the item survived, or the purge got the
        row first and the exemption reports "not found". A granted exemption
        on a deleted item is the bug this test exists for.
        """
        exempting = self.new_store(FakeClock(T0))
        purging = self.new_store(FakeClock(T0 + 20 * SECOND))
        outcomes = {"granted": 0, "purged first": 0}
        for iteration in range(self.ITERATIONS):
            self.clean_tables()
            item_id = self.seed_item(expires_at=T0 + 10 * SECOND)

            granted, result = await self.one_round(
                exempting, purging, exempt_call, item_id
            )

            with self.subTest(iteration=iteration, granted=granted):
                if granted:
                    self.assertTrue(self.exists(item_id))
                    self.assertEqual(result.purged, 0)
                    outcomes["granted"] += 1
                else:
                    self.assertFalse(self.exists(item_id))
                    self.assertEqual(result, PurgeResult(1, 0, False))
                    outcomes["purged first"] += 1
        return outcomes

    async def test_an_acquired_item_is_never_purged(self):
        async def acquire(store, item_id):
            await store.acquire_use(self.project_id, item_id, uuid4())

        outcomes = await self.race(acquire)

        self.assertEqual(sum(outcomes.values()), self.ITERATIONS)

    async def test_a_pinned_item_is_never_purged(self):
        async def pin(store, item_id):
            await store.pin(self.project_id, item_id)

        outcomes = await self.race(pin)

        self.assertEqual(sum(outcomes.values()), self.ITERATIONS)

    async def test_a_requested_promotion_keeps_the_item_from_being_purged(self):
        async def request(store, item_id):
            await store.request_promotion(self.project_id, item_id)

        outcomes = await self.race(request)

        self.assertEqual(sum(outcomes.values()), self.ITERATIONS)

    async def test_an_item_acquired_before_the_purge_starts_survives_it(self):
        item_id = self.seed_item(expires_at=T0 + 10 * SECOND)
        await self.store.acquire_use(self.project_id, item_id, uuid4())  # at T0

        result = await self.store.purge_expired(now=T0 + 20 * SECOND)

        self.assertEqual(result, PurgeResult(0, 1, False))
        self.assertTrue(self.exists(item_id))
        self.assertEqual(len(self.lease_rows(item_id)), 1)
