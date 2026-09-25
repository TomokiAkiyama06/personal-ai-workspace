"""ScratchStore.purge_expired and the acceptance workflows (real PostgreSQL)."""

from datetime import timedelta, timezone
from uuid import uuid4

from paw_backend.research.scratch import (
    DeferralReason,
    InputProblem,
    InvalidScratchInputError,
    PromotionOutcome,
    PurgeResult,
    ScratchItemNotFoundError,
)

from .scratch_support import T0, PostgresScratchTestCase, requires_postgres

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
SECOND = timedelta(seconds=1)
MICRO = timedelta(microseconds=1)


@requires_postgres
class PurgeCountsTest(PostgresScratchTestCase):
    async def test_an_empty_store_purges_nothing(self):
        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 0, False))

    async def test_exact_counts_for_every_kind_of_item(self):
        purgeable = {
            self.seed_item(expires_at=T0 - SECOND),
            self.seed_item(expires_at=T0),  # expires exactly now: expired
            self.seed_item(expires_at=T0 - 5 * HOUR),
            self.seed_item(expires_at=T0 - HOUR, promotion_state="promoted"),
            self.seed_item(expires_at=T0 - HOUR, promotion_state="rejected"),
        }
        ended_lease = self.seed_item(expires_at=T0 - HOUR)
        self.seed_lease(ended_lease, T0)  # ends exactly now: over
        purgeable.add(ended_lease)

        pinned = self.seed_item(expires_at=T0 - HOUR, pinned=True)
        pending = self.seed_pending(expires_at=T0 - HOUR)
        in_use = self.seed_item(expires_at=T0 - HOUR)
        self.seed_lease(in_use, T0 + MICRO)
        every_reason = self.seed_pending(expires_at=T0 - HOUR, pinned=True)
        self.seed_lease(every_reason, T0 + 5 * MINUTE)
        deferred = {pinned, pending, in_use, every_reason}
        # Unexpired items are never counted as deferred, exempt or not.
        unexpired = {
            self.seed_item(expires_at=T0 + MICRO),
            self.seed_item(expires_at=T0 + HOUR, pinned=True),
            self.seed_pending(expires_at=T0 + HOUR),
        }
        leased = self.seed_item(expires_at=T0 + HOUR)
        self.seed_lease(leased, T0 + MINUTE)
        unexpired.add(leased)

        result = await self.store.purge_expired()

        self.assertEqual(result, PurgeResult(purged=6, deferred=4, has_more=False))
        self.assertEqual(self.item_ids(), deferred | unexpired)
        self.assertFalse(purgeable & self.item_ids())

    async def test_unexpired_items_are_never_touched_whatever_their_state(self):
        ids = {
            self.seed_item(expires_at=T0 + MICRO),
            self.seed_item(expires_at=T0 + HOUR, promotion_state="rejected"),
            self.seed_item(expires_at=T0 + 20 * HOUR),
        }

        result = await self.store.purge_expired()

        self.assertEqual(result, PurgeResult(0, 0, False))
        self.assertEqual(self.item_ids(), ids)

    async def test_a_second_purge_finds_nothing_more_and_repeats_the_deferred_count(
        self,
    ):
        self.seed_item(expires_at=T0 - HOUR)
        keep = self.seed_item(expires_at=T0 - HOUR, pinned=True)

        first = await self.store.purge_expired()
        second = await self.store.purge_expired()

        self.assertEqual(first, PurgeResult(1, 1, False))
        self.assertEqual(second, PurgeResult(0, 1, False))
        self.assertEqual(self.item_ids(), {keep})

    async def test_purge_works_across_projects(self):
        first = self.seed_item(expires_at=T0 - HOUR)
        second = self.seed_item(project_id=uuid4(), expires_at=T0 - HOUR)

        result = await self.store.purge_expired()

        self.assertEqual(result, PurgeResult(2, 0, False))
        self.assertFalse({first, second} & self.item_ids())

    async def test_the_leases_of_purged_items_go_with_them(self):
        item_id = self.seed_item(expires_at=T0 - HOUR)
        self.seed_lease(item_id, T0 - SECOND)
        self.seed_lease(item_id, T0)
        kept = self.seed_item(expires_at=T0 - HOUR, pinned=True)
        kept_holder = self.seed_lease(kept, T0 - SECOND)

        await self.store.purge_expired()

        self.assertEqual(self.table_count("research_scratch_leases"), 1)
        self.assertEqual(set(self.lease_rows(kept)), {kept_holder})

    async def test_explicit_now_decides_what_is_expired(self):
        item_id = self.seed_item(expires_at=T0 + 2 * HOUR)

        early = await self.store.purge_expired(now=T0 + 2 * HOUR - MICRO)
        self.assertEqual(early, PurgeResult(0, 0, False))
        self.assertTrue(self.exists(item_id))

        exact = await self.store.purge_expired(now=T0 + 2 * HOUR)
        self.assertEqual(exact, PurgeResult(1, 0, False))
        self.assertFalse(self.exists(item_id))

    async def test_without_now_the_injected_clock_is_used(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)

        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 0, False))
        self.clock.advance(hours=1)
        self.assertEqual(await self.store.purge_expired(), PurgeResult(1, 0, False))
        self.assertFalse(self.exists(item_id))

    async def test_a_now_in_another_time_zone_means_the_same_instant(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        jst = timezone(timedelta(hours=9))

        early = await self.store.purge_expired(now=(T0 + HOUR - MICRO).astimezone(jst))
        exact = await self.store.purge_expired(now=(T0 + HOUR).astimezone(jst))

        self.assertEqual(early, PurgeResult(0, 0, False))
        self.assertEqual(exact, PurgeResult(1, 0, False))
        self.assertFalse(self.exists(item_id))

    async def test_long_term_memory_and_tasks_are_never_touched(self):
        memory_id = self.seed_memory()
        task_id = self.seed_task()
        self.seed_item(expires_at=T0 - HOUR, task_id=task_id)
        before = {
            table: self.table_count(table)
            for table in ("memories", "memory_versions", "tasks")
        }

        result = await self.store.purge_expired()

        self.assertEqual(result, PurgeResult(1, 0, False))
        after = {table: self.table_count(table) for table in before}
        self.assertEqual(after, before)
        self.assertEqual(before["memories"], 1)
        self.assertIsNotNone(memory_id)


@requires_postgres
class PurgeBatchTest(PostgresScratchTestCase):
    def seed_purgeable(self, count):
        """``count`` purgeable items, the oldest first; returns their ids in order."""
        return [
            self.seed_item(expires_at=T0 - (count - index) * MINUTE)
            for index in range(count)
        ]

    async def test_a_batch_deletes_the_oldest_expired_items_first(self):
        ids = self.seed_purgeable(5)

        result = await self.store.purge_expired(batch_size=2)

        self.assertEqual(result, PurgeResult(2, 0, True))
        self.assertEqual(self.item_ids(), set(ids[2:]))

    async def test_calling_until_has_more_is_false_purges_everything(self):
        ids = self.seed_purgeable(5)
        results = []

        while True:
            result = await self.store.purge_expired(batch_size=2)
            results.append(result)
            if not result.has_more:
                break

        self.assertEqual(
            results,
            [
                PurgeResult(2, 0, True),
                PurgeResult(2, 0, True),
                PurgeResult(1, 0, False),
            ],
        )
        self.assertEqual(self.item_ids(), set())
        self.assertEqual(len(ids), 5)

    async def test_has_more_is_true_only_when_another_purgeable_item_exists(self):
        self.seed_purgeable(3)

        exactly = await self.store.purge_expired(batch_size=3)

        self.assertEqual(exactly, PurgeResult(3, 0, False))

        self.seed_purgeable(4)
        one_more = await self.store.purge_expired(batch_size=3)

        self.assertEqual(one_more, PurgeResult(3, 0, True))
        self.assertEqual(len(self.item_ids()), 1)

    async def test_exempt_items_do_not_use_up_the_batch(self):
        pinned = {
            self.seed_item(expires_at=T0 - 10 * HOUR - index * MINUTE, pinned=True)
            for index in range(5)
        }
        purgeable = [
            self.seed_item(expires_at=T0 - HOUR + index * MINUTE) for index in range(3)
        ]

        first = await self.store.purge_expired(batch_size=2)
        second = await self.store.purge_expired(batch_size=2)

        self.assertEqual(first, PurgeResult(2, 5, True))
        self.assertEqual(second, PurgeResult(1, 5, False))
        self.assertEqual(self.item_ids(), pinned)
        self.assertEqual(len(purgeable), 3)

    async def test_every_kind_of_exempt_item_stays_out_of_the_batch(self):
        exempt = set()
        for index in range(2):
            old = T0 - 10 * HOUR - index * MINUTE
            exempt.add(self.seed_item(expires_at=old, pinned=True))
            exempt.add(self.seed_pending(expires_at=old - 30 * SECOND))
            leased = self.seed_item(expires_at=old - 45 * SECOND)
            self.seed_lease(leased, T0 + 5 * MINUTE)
            exempt.add(leased)
        for index in range(3):
            self.seed_item(expires_at=T0 - HOUR + index * MINUTE)

        first = await self.store.purge_expired(batch_size=2)
        second = await self.store.purge_expired(batch_size=2)

        self.assertEqual(first, PurgeResult(2, 6, True))
        self.assertEqual(second, PurgeResult(1, 6, False))
        self.assertEqual(self.item_ids(), exempt)

    async def test_the_default_batch_size_is_500(self):
        self.assertEqual(
            await self.store.purge_expired(batch_size=5000), PurgeResult(0, 0, False)
        )
        ids = [self.seed_item(expires_at=T0 - HOUR) for _ in range(3)]

        result = await self.store.purge_expired()

        self.assertEqual(result, PurgeResult(3, 0, False))
        self.assertFalse(set(ids) & self.item_ids())

    async def test_batch_size_and_now_are_validated_before_anything_is_deleted(self):
        item_id = self.seed_item(expires_at=T0 - HOUR)
        cases = [
            ({"batch_size": 0}, "batch_size", InputProblem.OUT_OF_RANGE),
            ({"batch_size": -1}, "batch_size", InputProblem.OUT_OF_RANGE),
            ({"batch_size": 5001}, "batch_size", InputProblem.OUT_OF_RANGE),
            ({"batch_size": True}, "batch_size", InputProblem.WRONG_TYPE),
            ({"batch_size": 2.0}, "batch_size", InputProblem.WRONG_TYPE),
            ({"batch_size": "10"}, "batch_size", InputProblem.WRONG_TYPE),
            ({"batch_size": None}, "batch_size", InputProblem.REQUIRED),
            ({"now": T0.replace(tzinfo=None)}, "now", InputProblem.NAIVE_DATETIME),
            ({"now": "2026-09-24T12:00:00Z"}, "now", InputProblem.WRONG_TYPE),
            (
                {"now": T0.replace(tzinfo=None), "batch_size": 0},
                "now",
                InputProblem.NAIVE_DATETIME,
            ),
        ]
        for arguments, field, problem in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(InvalidScratchInputError) as raised:
                    await self.store.purge_expired(**arguments)
                self.assertEqual(
                    (raised.exception.field, raised.exception.problem), (field, problem)
                )
        self.assertTrue(self.exists(item_id))


@requires_postgres
class DeferredDeletionTest(PostgresScratchTestCase):
    """A deferred item is purged as soon as its exemption has ended."""

    async def test_pinned_until_it_is_unpinned(self):
        item_id = self.seed_item(expires_at=T0 - HOUR, pinned=True)

        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 1, False))
        self.set_item(item_id, pinned=False)
        self.assertEqual(await self.store.purge_expired(), PurgeResult(1, 0, False))
        self.assertFalse(self.exists(item_id))

    async def test_saved_until_it_is_unsaved(self):
        item_id = self.seed_item(expires_at=T0 - HOUR, saved=True)

        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 1, False))
        self.set_item(item_id, saved=False)
        self.assertEqual(await self.store.purge_expired(), PurgeResult(1, 0, False))
        self.assertFalse(self.exists(item_id))

    async def test_pinned_and_saved_wait_for_both_to_be_cleared(self):
        item_id = self.seed_item(expires_at=T0 - HOUR, pinned=True, saved=True)

        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 1, False))
        self.set_item(item_id, pinned=False)
        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 1, False))
        self.set_item(item_id, saved=False)
        self.assertEqual(await self.store.purge_expired(), PurgeResult(1, 0, False))

    async def test_promotion_pending_until_it_is_resolved(self):
        item_id = self.seed_pending(expires_at=T0 - HOUR)

        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 1, False))
        self.set_item(item_id, promotion_state="rejected", promotion_requested_at=None)
        self.assertEqual(await self.store.purge_expired(), PurgeResult(1, 0, False))

    async def test_in_use_until_the_lease_is_released(self):
        item_id = self.seed_item(expires_at=T0 - HOUR)
        self.seed_lease(item_id, T0 + 5 * MINUTE)

        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 1, False))
        self.delete_leases(item_id)
        self.assertEqual(await self.store.purge_expired(), PurgeResult(1, 0, False))

    async def test_in_use_until_the_lease_ends_by_itself(self):
        item_id = self.seed_item(expires_at=T0 - HOUR)
        self.seed_lease(item_id, T0 + 5 * MINUTE)

        before_end = await self.store.purge_expired(now=T0 + 5 * MINUTE - MICRO)
        at_end = await self.store.purge_expired(now=T0 + 5 * MINUTE)

        self.assertEqual(before_end, PurgeResult(0, 1, False))
        self.assertEqual(at_end, PurgeResult(1, 0, False))
        self.assertFalse(self.exists(item_id))

    async def test_an_item_with_two_exemptions_waits_for_both(self):
        item_id = self.seed_item(expires_at=T0 - HOUR, pinned=True)
        self.seed_lease(item_id, T0 + 5 * MINUTE)

        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 1, False))
        self.set_item(item_id, pinned=False)
        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 1, False))
        self.delete_leases(item_id)
        self.assertEqual(await self.store.purge_expired(), PurgeResult(1, 0, False))


@requires_postgres
class RetentionWorkflowTest(PostgresScratchTestCase):
    """The acceptance criteria end to end, through the public API only."""

    async def add(self, **overrides):
        arguments = {"created_by": self.user_id, "summary": "Finding"}
        arguments.update(overrides)
        return await self.store.add(self.project_id, **arguments)

    async def test_an_item_expires_24_hours_after_it_was_created(self):
        item = await self.add()

        self.clock.now = T0 + timedelta(hours=24) - MICRO
        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 0, False))
        self.assertEqual((await self.store.get(self.project_id, item.id)).id, item.id)

        self.clock.now = T0 + timedelta(hours=24)
        with self.assertRaises(ScratchItemNotFoundError):
            await self.store.get(self.project_id, item.id)
        self.assertEqual(await self.store.purge_expired(), PurgeResult(1, 0, False))
        self.assertEqual(self.item_ids(), set())

    async def test_pinning_defers_the_deletion_until_the_item_is_unpinned(self):
        item = await self.add()
        await self.store.pin(self.project_id, item.id)
        self.clock.now = T0 + timedelta(hours=25)

        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 1, False))
        kept = await self.store.get(self.project_id, item.id)
        self.assertTrue(kept.expired)
        self.assertEqual(kept.deferral_reasons, (DeferralReason.PINNED,))

        unpinned = await self.store.unpin(self.project_id, item.id)
        self.assertEqual(unpinned.deferral_reasons, ())
        with self.assertRaises(ScratchItemNotFoundError):
            await self.store.get(self.project_id, item.id)
        self.assertEqual(await self.store.purge_expired(), PurgeResult(1, 0, False))

    async def test_use_defers_the_deletion_until_the_lease_is_released_or_ends(self):
        released = await self.add()
        lapsing = await self.add()
        holder = uuid4()
        expiry = T0 + timedelta(hours=24)
        self.clock.now = expiry - MINUTE
        await self.store.acquire_use(self.project_id, released.id, holder)
        await self.store.acquire_use(
            self.project_id, lapsing.id, uuid4(), lease_seconds=120
        )

        self.clock.now = expiry + 30 * SECOND  # both items have expired
        in_use = await self.store.get(self.project_id, released.id)
        self.assertTrue(in_use.expired and in_use.in_use)
        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 2, False))

        await self.store.release_use(self.project_id, released.id, holder)
        self.clock.now = expiry + MINUTE  # the other lease ends exactly now
        self.assertEqual(await self.store.purge_expired(), PurgeResult(2, 0, False))
        self.assertEqual(self.item_ids(), set())

    async def test_a_lease_that_is_not_renewed_cannot_keep_an_item_forever(self):
        item = await self.add()
        expiry = T0 + timedelta(hours=24)
        self.clock.now = expiry - 30 * MINUTE
        # The worker crashes right after this: it never renews or releases.
        await self.store.acquire_use(
            self.project_id, item.id, uuid4(), lease_seconds=3600
        )

        self.clock.now = expiry + 15 * MINUTE
        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 1, False))
        self.clock.now = expiry + 30 * MINUTE  # one hour after the lease began
        self.assertEqual(await self.store.purge_expired(), PurgeResult(1, 0, False))

    async def test_a_pending_promotion_defers_the_deletion_until_it_is_resolved(self):
        item = await self.add()
        await self.store.request_promotion(self.project_id, item.id)
        self.clock.now = T0 + timedelta(days=3)

        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 1, False))
        pending = await self.store.get(self.project_id, item.id)
        self.assertEqual(pending.deferral_reasons, (DeferralReason.PROMOTION_PENDING,))

        await self.store.resolve_promotion(
            self.project_id, item.id, PromotionOutcome.REJECTED
        )
        self.assertEqual(await self.store.purge_expired(), PurgeResult(1, 0, False))

    async def test_a_resolved_promotion_does_not_extend_the_ttl(self):
        item = await self.add()
        await self.store.request_promotion(self.project_id, item.id)
        await self.store.resolve_promotion(
            self.project_id, item.id, PromotionOutcome.PROMOTED
        )
        self.clock.now = T0 + timedelta(hours=24)

        self.assertEqual(await self.store.purge_expired(), PurgeResult(1, 0, False))

    async def test_the_project_and_task_relation_survives_every_operation(self):
        task_id = self.seed_task()
        item = await self.add(task_id=task_id)
        holder = uuid4()

        await self.store.pin(self.project_id, item.id)
        await self.store.acquire_use(self.project_id, item.id, holder)
        await self.store.request_promotion(self.project_id, item.id)
        await self.store.release_use(self.project_id, item.id, holder)
        await self.store.unpin(self.project_id, item.id)
        self.clock.advance(hours=30)
        await self.store.purge_expired()  # deferred: the promotion is pending

        (listed,) = await self.store.list_items(self.project_id, task_id=task_id)
        self.assertEqual(
            (listed.id, listed.project_id, listed.task_id),
            (item.id, self.project_id, task_id),
        )
        row = self.row(item.id)
        self.assertEqual(
            (row["project_id"], row["task_id"]), (self.project_id, task_id)
        )

    async def test_deleting_a_task_keeps_the_relation_of_a_pinned_item(self):
        # Decision 0013: the task id stays as a plain UUID. The deletion of the
        # task is neither blocked by the item nor does it delete the pinned item.
        task_id = self.seed_task()
        item = await self.add(task_id=task_id)
        await self.store.pin(self.project_id, item.id)

        self.delete_task(task_id)
        self.clock.advance(hours=30)

        self.assertEqual(await self.store.purge_expired(), PurgeResult(0, 1, False))
        kept = await self.store.get(self.project_id, item.id)
        self.assertEqual(
            (kept.project_id, kept.task_id, kept.pinned),
            (self.project_id, task_id, True),
        )
        (listed,) = await self.store.list_items(self.project_id, task_id=task_id)
        self.assertEqual(listed.id, item.id)

    async def test_an_unpinned_item_of_a_deleted_task_still_expires(self):
        task_id = self.seed_task()
        item = await self.add(task_id=task_id)
        self.delete_task(task_id)
        self.clock.advance(hours=24)

        self.assertEqual(await self.store.purge_expired(), PurgeResult(1, 0, False))

        self.assertFalse(self.exists(item.id))

    async def test_scratch_items_are_never_written_to_long_term_memory(self):
        self.seed_memory()
        before = self.table_count("memory_versions"), self.table_count("memories")
        item = await self.add()

        await self.store.pin(self.project_id, item.id)
        await self.store.request_promotion(self.project_id, item.id)
        await self.store.resolve_promotion(
            self.project_id, item.id, PromotionOutcome.PROMOTED
        )

        self.assertEqual(
            (self.table_count("memory_versions"), self.table_count("memories")), before
        )
