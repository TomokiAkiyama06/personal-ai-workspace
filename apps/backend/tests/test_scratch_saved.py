"""Explicit save vs. pin: two independent deferrals of the TTL deletion.

REQUIREMENTS.md lists "Pin済み" and "Userが明示保存" as separate reasons to defer
deletion (decision 0013, the section on pin and save). A single boolean let the
later ``unpin`` of an ordinary pin clear a user's explicit save as well and so
let an expired item be purged. These tests hold the two markers apart: each
API call clears only its own marker, and the purge keeps an item while either
is set. Everything runs on a real PostgreSQL; rows are seeded and read with SQL.
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from paw_backend.research.scratch import (
    DeferralReason,
    InvalidScratchInputError,
    PurgeResult,
    ScratchItemNotFoundError,
)

from .scratch_support import T0, PostgresScratchTestCase, requires_postgres

HOUR = timedelta(hours=1)
MICRO = timedelta(microseconds=1)
DEADLINE = 15  # seconds; only a hung implementation ever waits this long
STILL_WAITING = 0.5  # seconds


@requires_postgres
class SaveTest(PostgresScratchTestCase):
    async def test_save_keeps_the_item_and_returns_the_new_state(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        before = self.row(item_id)

        item = await self.store.save(self.project_id, item_id)

        self.assertTrue(item.saved)
        self.assertFalse(item.pinned)
        self.assertEqual(item.deferral_reasons, (DeferralReason.SAVED,))
        self.assertEqual(item, self.snapshot(item_id))
        after = self.row(item_id)
        self.assertTrue(after.pop("saved"))
        self.assertFalse(before.pop("saved"))
        self.assertEqual(after, before)  # expires_at and everything else unchanged

    async def test_save_is_idempotent(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)

        first = await self.store.save(self.project_id, item_id)
        second = await self.store.save(self.project_id, item_id)

        self.assertEqual(first, second)
        self.assertTrue(self.row(item_id)["saved"])

    async def test_unsave_removes_only_the_save_and_is_idempotent(self):
        item_id = self.seed_item(expires_at=T0 + HOUR, saved=True)

        first = await self.store.unsave(self.project_id, item_id)
        second = await self.store.unsave(self.project_id, item_id)

        self.assertFalse(first.saved)
        self.assertEqual(first, second)
        self.assertEqual(first.deferral_reasons, ())
        self.assertFalse(self.row(item_id)["saved"])

    async def test_a_new_item_is_neither_pinned_nor_saved(self):
        item = await self.store.add(
            self.project_id, created_by=self.user_id, summary="s"
        )

        self.assertEqual((item.pinned, item.saved), (False, False))
        row = self.row(item.id)
        self.assertEqual((row["pinned"], row["saved"]), (False, False))

    async def test_a_missing_or_foreign_item_is_not_found(self):
        foreign = self.seed_item(project_id=uuid4(), expires_at=T0 + HOUR)
        saved_foreign = self.seed_item(
            project_id=uuid4(), expires_at=T0 + HOUR, saved=True
        )

        for call, item_id in [
            (self.store.save, foreign),
            (self.store.save, uuid4()),
            (self.store.unsave, saved_foreign),
            (self.store.unsave, uuid4()),
        ]:
            with self.subTest(call=call.__name__):
                with self.assertRaises(ScratchItemNotFoundError):
                    await call(self.project_id, item_id)
        self.assertFalse(self.row(foreign)["saved"])
        self.assertTrue(self.row(saved_foreign)["saved"])

    async def test_an_expired_item_without_an_exemption_cannot_be_saved(self):
        item_id = self.seed_item(expires_at=T0)  # expired exactly now

        with self.assertRaises(ScratchItemNotFoundError):
            await self.store.save(self.project_id, item_id)

        self.assertFalse(self.row(item_id)["saved"])

    async def test_an_expired_item_kept_by_another_exemption_can_be_saved(self):
        pending = self.seed_pending(expires_at=T0 - HOUR)
        in_use = self.seed_item(expires_at=T0 - HOUR)
        self.seed_lease(in_use, T0 + timedelta(minutes=1))
        pinned = self.seed_item(expires_at=T0 - HOUR, pinned=True)

        for item_id in (pending, in_use, pinned):
            with self.subTest(item_id=item_id):
                item = await self.store.save(self.project_id, item_id)
                self.assertTrue(item.saved)
                self.assertTrue(item.expired)

    async def test_save_works_up_to_the_last_microsecond_before_expiry(self):
        item_id = self.seed_item(expires_at=T0 + MICRO)

        item = await self.store.save(self.project_id, item_id)

        self.assertTrue(item.saved)
        self.assertFalse(item.expired)

    async def test_unsaving_the_last_exemption_of_an_expired_item_ends_it(self):
        item_id = self.seed_item(expires_at=T0 - HOUR, saved=True)

        item = await self.store.unsave(self.project_id, item_id)

        self.assertFalse(item.saved)
        self.assertTrue(item.expired)
        self.assertEqual(item.deferral_reasons, ())
        with self.assertRaises(ScratchItemNotFoundError):
            await self.store.get(self.project_id, item_id)
        self.assertTrue(self.exists(item_id))  # the row waits for the next purge

    async def test_bad_arguments_are_reported_with_their_field(self):
        for call in (self.store.save, self.store.unsave):
            with self.subTest(call=call.__name__):
                with self.assertRaises(InvalidScratchInputError) as raised:
                    await call(self.project_id, "nope")
                self.assertEqual(raised.exception.field, "item_id")
                with self.assertRaises(InvalidScratchInputError) as raised:
                    await call("nope", "nope")
                self.assertEqual(raised.exception.field, "project_id")


@requires_postgres
class SavedItemVisibilityTest(PostgresScratchTestCase):
    async def test_an_expired_saved_item_is_visible_and_listed(self):
        saved = self.seed_item(expires_at=T0 - HOUR, saved=True)
        plain = self.seed_item(expires_at=T0 - HOUR)

        item = await self.store.get(self.project_id, saved)
        listed = await self.store.list_items(self.project_id)

        self.assertTrue(item.expired)
        self.assertEqual(item.deferral_reasons, (DeferralReason.SAVED,))
        self.assertEqual([entry.id for entry in listed], [saved])
        with self.assertRaises(ScratchItemNotFoundError):
            await self.store.get(self.project_id, plain)

    async def test_the_reasons_of_an_item_with_both_markers_come_in_a_fixed_order(self):
        item_id = self.seed_pending(expires_at=T0 - HOUR, pinned=True, saved=True)
        self.seed_lease(item_id, T0 + timedelta(minutes=1))

        item = await self.store.get(self.project_id, item_id)

        self.assertEqual(
            item.deferral_reasons,
            (
                DeferralReason.PINNED,
                DeferralReason.SAVED,
                DeferralReason.IN_USE,
                DeferralReason.PROMOTION_PENDING,
            ),
        )


@requires_postgres
class PinAndSaveAreIndependentTest(PostgresScratchTestCase):
    """The reviewed defect: ``unpin`` must not clear a user's explicit save."""

    async def test_pin_and_save_then_unpin_keeps_the_item_through_the_purge(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        await self.store.pin(self.project_id, item_id)
        await self.store.save(self.project_id, item_id)
        self.clock.now = T0 + 2 * HOUR  # the TTL is over; both markers defer

        unpinned = await self.store.unpin(self.project_id, item_id)
        purge = await self.store.purge_expired()

        self.assertEqual(
            (unpinned.pinned, unpinned.saved, unpinned.expired), (False, True, True)
        )
        self.assertEqual(unpinned.deferral_reasons, (DeferralReason.SAVED,))
        self.assertEqual(purge, PurgeResult(purged=0, deferred=1, has_more=False))
        self.assertTrue(self.exists(item_id))
        self.assertEqual((await self.store.get(self.project_id, item_id)).id, item_id)

    async def test_pin_and_save_then_unsave_keeps_the_pin(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        await self.store.save(self.project_id, item_id)
        await self.store.pin(self.project_id, item_id)
        self.clock.now = T0 + 2 * HOUR

        unsaved = await self.store.unsave(self.project_id, item_id)
        purge = await self.store.purge_expired()

        self.assertEqual((unsaved.pinned, unsaved.saved), (True, False))
        self.assertEqual(unsaved.deferral_reasons, (DeferralReason.PINNED,))
        self.assertEqual(purge, PurgeResult(purged=0, deferred=1, has_more=False))
        self.assertTrue(self.exists(item_id))

    async def test_saved_only_survives_the_purge_until_it_is_unsaved(self):
        item_id = self.seed_item(expires_at=T0 - HOUR, saved=True)

        first = await self.store.purge_expired()
        await self.store.unsave(self.project_id, item_id)
        second = await self.store.purge_expired()

        self.assertEqual(first, PurgeResult(purged=0, deferred=1, has_more=False))
        self.assertEqual(second, PurgeResult(purged=1, deferred=0, has_more=False))
        self.assertFalse(self.exists(item_id))

    async def test_pinned_only_survives_the_purge_until_it_is_unpinned(self):
        item_id = self.seed_item(expires_at=T0 - HOUR, pinned=True)

        first = await self.store.purge_expired()
        await self.store.unpin(self.project_id, item_id)
        second = await self.store.purge_expired()

        self.assertEqual(first, PurgeResult(purged=0, deferred=1, has_more=False))
        self.assertEqual(second, PurgeResult(purged=1, deferred=0, has_more=False))
        self.assertFalse(self.exists(item_id))

    async def test_both_cleared_the_item_is_purged_only_after_its_ttl(self):
        for label, clear in {
            "unpin first": ("unpin", "unsave"),
            "unsave first": ("unsave", "unpin"),
        }.items():
            with self.subTest(label):
                self.clock.now = T0
                item_id = self.seed_item()  # created_at = T0, expires T0 + 24 h
                await self.store.pin(self.project_id, item_id)
                await self.store.save(self.project_id, item_id)
                for name in clear:
                    await getattr(self.store, name)(self.project_id, item_id)

                # The markers only defer: the TTL was never extended.
                before_ttl = await self.store.purge_expired(now=T0 + 23 * HOUR)
                self.assertEqual(before_ttl, PurgeResult(0, 0, False))
                self.assertTrue(self.exists(item_id))
                self.clock.now = T0 + 24 * HOUR
                with self.assertRaises(ScratchItemNotFoundError):
                    await self.store.get(self.project_id, item_id)
                after_ttl = await self.store.purge_expired()

                self.assertEqual(after_ttl, PurgeResult(1, 0, False))
                self.assertFalse(self.exists(item_id))

    async def test_each_call_changes_only_its_own_column(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        seen = []

        for name in ("pin", "save", "unpin", "pin", "unsave", "unpin"):
            await getattr(self.store, name)(self.project_id, item_id)
            row = self.row(item_id)
            seen.append((name, row["pinned"], row["saved"]))

        self.assertEqual(
            seen,
            [
                ("pin", True, False),
                ("save", True, True),
                ("unpin", False, True),
                ("pin", True, True),
                ("unsave", True, False),
                ("unpin", False, False),
            ],
        )

    async def test_a_row_with_both_markers_counts_once_as_deferred(self):
        self.seed_item(expires_at=T0 - HOUR, pinned=True, saved=True)
        self.seed_item(expires_at=T0 - HOUR, saved=True)
        self.seed_item(expires_at=T0 - HOUR)

        result = await self.store.purge_expired()

        self.assertEqual(result, PurgeResult(purged=1, deferred=2, has_more=False))

    async def test_saved_rows_do_not_starve_the_purgeable_ones(self):
        saved = [self.seed_item(expires_at=T0 - 9 * HOUR, saved=True) for _ in range(3)]
        purgeable = self.seed_item(expires_at=T0 - HOUR)

        result = await self.store.purge_expired(batch_size=1)

        self.assertEqual(result, PurgeResult(purged=1, deferred=3, has_more=False))
        self.assertEqual(self.item_ids(), set(saved))
        self.assertNotIn(purgeable, self.item_ids())

    async def test_a_save_committed_after_the_purge_chose_its_rows_still_protects(self):
        item_id = self.seed_item(expires_at=T0 - HOUR)

        async def probe(session, chosen):
            # What a concurrent ``save`` committed at the last moment looks like
            # (the probe's writes are part of the purge's transaction).
            await session.execute(
                text("UPDATE research_scratch_items SET saved = true WHERE id = :id"),
                {"id": item_id},
            )

        store = self.new_store(purge_probe=probe)

        result = await store.purge_expired()

        self.assertEqual(result, PurgeResult(purged=0, deferred=1, has_more=False))
        self.assertTrue(self.exists(item_id))


@requires_postgres
class ConcurrentMarkersTest(PostgresScratchTestCase):
    async def test_unpin_and_unsave_at_the_same_time_clear_both(self):
        for _ in range(5):
            item_id = self.seed_item(expires_at=T0 + HOUR, pinned=True, saved=True)

            unpinned, unsaved = await asyncio.wait_for(
                asyncio.gather(
                    self.store.unpin(self.project_id, item_id),
                    self.new_store().unsave(self.project_id, item_id),
                ),
                DEADLINE,
            )

            self.assertFalse(unpinned.pinned)
            self.assertFalse(unsaved.saved)
            row = self.row(item_id)
            self.assertEqual((row["pinned"], row["saved"]), (False, False))

    async def test_pin_and_save_at_the_same_time_set_both(self):
        for _ in range(5):
            item_id = self.seed_item(expires_at=T0 + HOUR)

            await asyncio.wait_for(
                asyncio.gather(
                    self.store.pin(self.project_id, item_id),
                    self.new_store().save(self.project_id, item_id),
                ),
                DEADLINE,
            )

            row = self.row(item_id)
            self.assertEqual((row["pinned"], row["saved"]), (True, True))

    async def test_unpin_and_save_at_the_same_time_never_lose_an_acknowledged_save(
        self,
    ):
        for _ in range(5):
            item_id = self.seed_item(expires_at=T0 - HOUR, pinned=True)

            unpinned, saved = await asyncio.wait_for(
                asyncio.gather(
                    self.store.unpin(self.project_id, item_id),
                    self.new_store().save(self.project_id, item_id),
                    return_exceptions=True,
                ),
                DEADLINE,
            )

            # The pin is the only thing that keeps the expired item visible, so
            # either the save ran first (it succeeds and stays) or the unpin
            # did (the item is gone for the save). The row never disagrees with
            # what the caller was told.
            self.assertFalse(unpinned.pinned)
            row = self.row(item_id)
            self.assertFalse(row["pinned"])
            if isinstance(saved, ScratchItemNotFoundError):
                self.assertFalse(row["saved"])
            else:
                self.assertTrue(saved.saved)
                self.assertTrue(row["saved"])

    async def test_an_unpin_that_waited_for_a_lock_does_not_undo_a_save_made_meanwhile(
        self,
    ):
        item_id = self.seed_item(expires_at=T0 - HOUR, pinned=True)
        connection, transaction = self.lock_item(item_id)
        pending = self.spawn(self.store.unpin(self.project_id, item_id))
        await asyncio.sleep(STILL_WAITING)
        self.assertWaiting(pending)
        # A save that commits while the unpin is waiting for the row lock.
        connection.execute(
            text("UPDATE research_scratch_items SET saved = true WHERE id = :id"),
            {"id": item_id},
        )
        transaction.commit()

        item = await asyncio.wait_for(pending, DEADLINE)

        self.assertEqual((item.pinned, item.saved), (False, True))
        self.assertEqual(item.deferral_reasons, (DeferralReason.SAVED,))
        row = self.row(item_id)
        self.assertEqual((row["pinned"], row["saved"]), (False, True))
        self.assertEqual((await self.store.get(self.project_id, item_id)).id, item_id)

    async def test_an_unsave_that_waited_for_a_lock_does_not_undo_a_pin_made_meanwhile(
        self,
    ):
        item_id = self.seed_item(expires_at=T0 - HOUR, saved=True)
        connection, transaction = self.lock_item(item_id)
        pending = self.spawn(self.store.unsave(self.project_id, item_id))
        await asyncio.sleep(STILL_WAITING)
        self.assertWaiting(pending)
        connection.execute(
            text("UPDATE research_scratch_items SET pinned = true WHERE id = :id"),
            {"id": item_id},
        )
        transaction.commit()

        item = await asyncio.wait_for(pending, DEADLINE)

        self.assertEqual((item.pinned, item.saved), (True, False))
        row = self.row(item_id)
        self.assertEqual((row["pinned"], row["saved"]), (True, False))
        self.assertEqual((await self.store.get(self.project_id, item_id)).id, item_id)
