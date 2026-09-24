"""ScratchStore leases (acquire_use / release_use) and promotion requests."""

import asyncio
from datetime import timedelta
from uuid import uuid4

from paw_backend.research.scratch import (
    DeferralReason,
    InputProblem,
    InvalidScratchInputError,
    PromotionOutcome,
    PromotionState,
    ScratchItemNotFoundError,
    ScratchLeaseLimitError,
    ScratchStateError,
)

from .scratch_support import T0, PostgresScratchTestCase, requires_postgres

HOUR = timedelta(hours=1)
MICRO = timedelta(microseconds=1)
SECOND = timedelta(seconds=1)


@requires_postgres
class AcquireUseTest(PostgresScratchTestCase):
    async def test_a_lease_lasts_300_seconds_by_default(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        holder = uuid4()

        lease = await self.store.acquire_use(self.project_id, item_id, holder)

        self.assertEqual(lease.item_id, item_id)
        self.assertEqual(lease.holder_id, holder)
        self.assertEqual(lease.leased_at, T0)
        self.assertEqual(lease.expires_at, T0 + timedelta(seconds=300))
        stored = self.lease_rows(item_id)[holder]
        self.assertEqual(stored["leased_at"], T0)
        self.assertEqual(stored["expires_at"], T0 + timedelta(seconds=300))

    async def test_the_lease_length_can_be_chosen_between_one_second_and_one_hour(self):
        for seconds in (1, 60, 3600):
            with self.subTest(seconds=seconds):
                item_id = self.seed_item(expires_at=T0 + HOUR)
                holder = uuid4()

                lease = await self.store.acquire_use(
                    self.project_id, item_id, holder, lease_seconds=seconds
                )

                self.assertEqual(lease.expires_at, T0 + timedelta(seconds=seconds))
                self.assertEqual(
                    self.lease_rows(item_id)[holder]["expires_at"],
                    T0 + timedelta(seconds=seconds),
                )

    async def test_an_item_in_use_says_so_and_is_kept(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)

        await self.store.acquire_use(self.project_id, item_id, uuid4())

        item = await self.store.get(self.project_id, item_id)
        self.assertTrue(item.in_use)
        self.assertEqual(item.deferral_reasons, (DeferralReason.IN_USE,))

    async def test_the_item_row_is_not_changed(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        before = self.row(item_id)

        await self.store.acquire_use(self.project_id, item_id, uuid4())

        self.assertEqual(self.row(item_id), before)

    async def test_two_holders_have_separate_leases(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        first, second = uuid4(), uuid4()

        await self.store.acquire_use(self.project_id, item_id, first, lease_seconds=60)
        await self.store.acquire_use(
            self.project_id, item_id, second, lease_seconds=120
        )

        rows = self.lease_rows(item_id)
        self.assertEqual(set(rows), {first, second})
        self.assertEqual(rows[first]["expires_at"], T0 + timedelta(seconds=60))
        self.assertEqual(rows[second]["expires_at"], T0 + timedelta(seconds=120))

    async def test_a_holder_that_acquires_again_renews_its_lease_even_to_an_earlier_end(
        self,
    ):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        holder, other = uuid4(), uuid4()
        await self.store.acquire_use(
            self.project_id, item_id, holder, lease_seconds=300
        )
        await self.store.acquire_use(self.project_id, item_id, other, lease_seconds=200)
        self.clock.advance(seconds=100)

        lease = await self.store.acquire_use(
            self.project_id, item_id, holder, lease_seconds=60
        )

        self.assertEqual(lease.leased_at, T0 + timedelta(seconds=100))
        self.assertEqual(lease.expires_at, T0 + timedelta(seconds=160))
        rows = self.lease_rows(item_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[holder]["expires_at"], T0 + timedelta(seconds=160))
        self.assertEqual(rows[other]["expires_at"], T0 + timedelta(seconds=200))

    async def test_expired_leases_of_the_item_are_removed_when_someone_acquires(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        stale = [
            self.seed_lease(item_id, T0 - seconds * SECOND) for seconds in (1, 2, 3)
        ]
        boundary = self.seed_lease(item_id, T0)  # ends exactly now: over
        active = self.seed_lease(item_id, T0 + SECOND)
        other_item = self.seed_item(expires_at=T0 + HOUR)
        untouched = self.seed_lease(other_item, T0 - SECOND)
        holder = uuid4()

        await self.store.acquire_use(self.project_id, item_id, holder)

        self.assertEqual(set(self.lease_rows(item_id)), {active, holder})
        self.assertFalse(({*stale, boundary}) & set(self.lease_rows(item_id)))
        self.assertEqual(set(self.lease_rows(other_item)), {untouched})

    async def test_at_most_16_holders_can_use_an_item_at_the_same_time(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        holders = [
            self.seed_lease(item_id, T0 + timedelta(minutes=5)) for _ in range(16)
        ]

        with self.assertRaises(ScratchLeaseLimitError):
            await self.store.acquire_use(self.project_id, item_id, uuid4())

        self.assertEqual(set(self.lease_rows(item_id)), set(holders))

    async def test_a_holder_at_the_limit_can_still_renew(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        holders = [
            self.seed_lease(item_id, T0 + timedelta(minutes=5)) for _ in range(16)
        ]

        lease = await self.store.acquire_use(
            self.project_id, item_id, holders[3], lease_seconds=600
        )

        self.assertEqual(lease.expires_at, T0 + timedelta(seconds=600))
        self.assertEqual(len(self.lease_rows(item_id)), 16)

    async def test_ended_leases_do_not_count_towards_the_limit(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        for _ in range(11):
            self.seed_lease(item_id, T0 - SECOND)
        for _ in range(15):
            self.seed_lease(item_id, T0 + timedelta(minutes=5))
        holder = uuid4()

        await self.store.acquire_use(self.project_id, item_id, holder)

        self.assertEqual(len(self.lease_rows(item_id)), 16)
        self.assertIn(holder, self.lease_rows(item_id))

    async def test_the_limit_is_per_item(self):
        full = self.seed_item(expires_at=T0 + HOUR)
        for _ in range(16):
            self.seed_lease(full, T0 + timedelta(minutes=5))
        other = self.seed_item(expires_at=T0 + HOUR)
        holder = uuid4()

        lease = await self.store.acquire_use(self.project_id, other, holder)

        self.assertEqual(lease.item_id, other)

    async def test_a_missing_foreign_or_expired_item_cannot_be_acquired(self):
        foreign = self.seed_item(project_id=uuid4(), expires_at=T0 + HOUR)
        expired = self.seed_item(expires_at=T0)  # expires exactly now
        long_gone = self.seed_item(expires_at=T0 - HOUR)
        ended = self.seed_item(expires_at=T0 - HOUR)
        self.seed_lease(ended, T0)  # an ended lease exempts nothing

        for item_id in (uuid4(), foreign, expired, long_gone, ended):
            with self.subTest(item_id=item_id):
                with self.assertRaises(ScratchItemNotFoundError):
                    await self.store.acquire_use(self.project_id, item_id, uuid4())

        self.assertEqual(self.lease_rows(foreign), {})
        self.assertEqual(self.lease_rows(expired), {})
        self.assertEqual(self.lease_rows(long_gone), {})

    async def test_an_expired_item_that_is_still_exempt_can_be_acquired(self):
        pinned = self.seed_item(expires_at=T0 - HOUR, pinned=True)
        pending = self.seed_pending(expires_at=T0 - HOUR)
        leased = self.seed_item(expires_at=T0 - HOUR)
        self.seed_lease(leased, T0 + timedelta(minutes=1))

        for item_id in (pinned, pending, leased):
            with self.subTest(item_id=item_id):
                holder = uuid4()
                lease = await self.store.acquire_use(self.project_id, item_id, holder)
                self.assertEqual(lease.expires_at, T0 + timedelta(seconds=300))
                self.assertIn(holder, self.lease_rows(item_id))

    async def test_a_lease_ends_by_itself(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        await self.store.acquire_use(
            self.project_id, item_id, uuid4(), lease_seconds=30
        )

        self.clock.now = T0 + timedelta(seconds=29)
        self.assertTrue((await self.store.get(self.project_id, item_id)).in_use)
        self.clock.now = T0 + timedelta(seconds=30)
        self.assertFalse((await self.store.get(self.project_id, item_id)).in_use)

    async def test_bad_arguments_are_reported_in_the_documented_order(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        cases = [
            (
                (self.project_id, item_id, "nope"),
                {},
                "holder_id",
                InputProblem.WRONG_TYPE,
            ),
            ((self.project_id, item_id, None), {}, "holder_id", InputProblem.REQUIRED),
            ((self.project_id, "x", uuid4()), {}, "item_id", InputProblem.WRONG_TYPE),
            (("x", item_id, uuid4()), {}, "project_id", InputProblem.WRONG_TYPE),
            (
                (self.project_id, item_id, uuid4()),
                {"lease_seconds": 0},
                "lease_seconds",
                InputProblem.OUT_OF_RANGE,
            ),
            (
                (self.project_id, item_id, uuid4()),
                {"lease_seconds": 3601},
                "lease_seconds",
                InputProblem.OUT_OF_RANGE,
            ),
            (
                (self.project_id, item_id, uuid4()),
                {"lease_seconds": -5},
                "lease_seconds",
                InputProblem.OUT_OF_RANGE,
            ),
            (
                (self.project_id, item_id, uuid4()),
                {"lease_seconds": True},
                "lease_seconds",
                InputProblem.WRONG_TYPE,
            ),
            (
                (self.project_id, item_id, uuid4()),
                {"lease_seconds": 5.0},
                "lease_seconds",
                InputProblem.WRONG_TYPE,
            ),
            (
                (self.project_id, item_id, uuid4()),
                {"lease_seconds": "60"},
                "lease_seconds",
                InputProblem.WRONG_TYPE,
            ),
            (
                (self.project_id, item_id, uuid4()),
                {"lease_seconds": None},
                "lease_seconds",
                InputProblem.REQUIRED,
            ),
            (
                (self.project_id, "x", uuid4()),
                {"lease_seconds": 0},
                "item_id",
                InputProblem.WRONG_TYPE,
            ),
        ]
        for args, kwargs, field, problem in cases:
            with self.subTest(field=field, kwargs=kwargs):
                with self.assertRaises(InvalidScratchInputError) as raised:
                    await self.store.acquire_use(*args, **kwargs)
                self.assertEqual(
                    (raised.exception.field, raised.exception.problem), (field, problem)
                )
        self.assertEqual(self.lease_rows(item_id), {})

    async def test_twenty_holders_at_once_get_exactly_sixteen_leases(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        stores = [self.new_store() for _ in range(4)]

        results = await asyncio.gather(
            *(
                stores[index % 4].acquire_use(self.project_id, item_id, uuid4())
                for index in range(20)
            ),
            return_exceptions=True,
        )

        for result in results:  # anything but the limit error is a real failure
            if isinstance(result, BaseException) and not isinstance(
                result, ScratchLeaseLimitError
            ):
                raise result
        limit_errors = [r for r in results if isinstance(r, ScratchLeaseLimitError)]
        leases = [r for r in results if not isinstance(r, BaseException)]
        self.assertEqual((len(leases), len(limit_errors)), (16, 4))
        self.assertEqual(len(self.lease_rows(item_id)), 16)


@requires_postgres
class ReleaseUseTest(PostgresScratchTestCase):
    async def test_release_ends_only_that_holders_lease(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        first = self.seed_lease(item_id, T0 + timedelta(minutes=5))
        second = self.seed_lease(item_id, T0 + timedelta(minutes=5))

        result = await self.store.release_use(self.project_id, item_id, first)

        self.assertIsNone(result)
        self.assertEqual(set(self.lease_rows(item_id)), {second})
        self.assertTrue((await self.store.get(self.project_id, item_id)).in_use)

    async def test_releasing_the_last_lease_makes_the_item_not_in_use(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        holder = self.seed_lease(item_id, T0 + timedelta(minutes=5))

        await self.store.release_use(self.project_id, item_id, holder)

        item = await self.store.get(self.project_id, item_id)
        self.assertFalse(item.in_use)
        self.assertEqual(item.deferral_reasons, ())

    async def test_release_is_idempotent(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        holder = self.seed_lease(item_id, T0 + timedelta(minutes=5))

        await self.store.release_use(self.project_id, item_id, holder)
        await self.store.release_use(self.project_id, item_id, holder)

        self.assertEqual(self.lease_rows(item_id), {})

    async def test_releasing_an_unknown_holder_or_item_is_not_an_error(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        holder = self.seed_lease(item_id, T0 + timedelta(minutes=5))

        await self.store.release_use(self.project_id, item_id, uuid4())
        await self.store.release_use(self.project_id, uuid4(), holder)

        self.assertEqual(set(self.lease_rows(item_id)), {holder})

    async def test_an_item_of_another_project_is_left_alone(self):
        item_id = self.seed_item(project_id=uuid4(), expires_at=T0 + HOUR)
        holder = self.seed_lease(item_id, T0 + timedelta(minutes=5))

        await self.store.release_use(self.project_id, item_id, holder)

        self.assertEqual(set(self.lease_rows(item_id)), {holder})

    async def test_a_lease_of_an_item_that_has_already_expired_is_removed_too(self):
        item_id = self.seed_item(expires_at=T0 - HOUR)
        holder = self.seed_lease(item_id, T0 - SECOND)

        await self.store.release_use(self.project_id, item_id, holder)

        self.assertEqual(self.lease_rows(item_id), {})
        self.assertTrue(self.exists(item_id))

    async def test_releasing_the_last_lease_of_an_expired_item_ends_it(self):
        item_id = self.seed_item(expires_at=T0 - HOUR)
        holder = self.seed_lease(item_id, T0 + timedelta(minutes=5))
        await self.store.get(self.project_id, item_id)  # still visible

        await self.store.release_use(self.project_id, item_id, holder)

        with self.assertRaises(ScratchItemNotFoundError):
            await self.store.get(self.project_id, item_id)

    async def test_bad_arguments_are_reported_with_their_field(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        for args, field in [
            ((self.project_id, item_id, "x"), "holder_id"),
            ((self.project_id, "x", uuid4()), "item_id"),
            (("x", "y", "z"), "project_id"),
        ]:
            with self.subTest(field=field):
                with self.assertRaises(InvalidScratchInputError) as raised:
                    await self.store.release_use(*args)
                self.assertEqual(raised.exception.field, field)


@requires_postgres
class RequestPromotionTest(PostgresScratchTestCase):
    async def test_a_request_marks_the_promotion_pending_and_defers_deletion(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        self.clock.advance(minutes=5)

        item = await self.store.request_promotion(self.project_id, item_id)

        self.assertIs(item.promotion_state, PromotionState.PENDING)
        self.assertEqual(item.promotion_requested_at, T0 + timedelta(minutes=5))
        self.assertEqual(item.deferral_reasons, (DeferralReason.PROMOTION_PENDING,))
        self.assertEqual(item, self.snapshot(item_id))
        row = self.row(item_id)
        self.assertEqual(row["promotion_state"], "pending")
        self.assertEqual(row["promotion_requested_at"], T0 + timedelta(minutes=5))

    async def test_a_second_request_keeps_the_first_request_time(self):
        item_id = self.seed_item(expires_at=T0 + HOUR)
        first = await self.store.request_promotion(self.project_id, item_id)
        self.clock.advance(minutes=5)

        second = await self.store.request_promotion(self.project_id, item_id)

        self.assertEqual(second, first)
        self.assertEqual(second.promotion_requested_at, T0)
        self.assertEqual(self.row(item_id)["promotion_requested_at"], T0)

    async def test_a_rejected_promotion_can_be_requested_again(self):
        item_id = self.seed_item(expires_at=T0 + HOUR, promotion_state="rejected")

        item = await self.store.request_promotion(self.project_id, item_id)

        self.assertIs(item.promotion_state, PromotionState.PENDING)
        self.assertEqual(item.promotion_requested_at, T0)

    async def test_a_promoted_item_cannot_be_requested_again(self):
        item_id = self.seed_item(expires_at=T0 + HOUR, promotion_state="promoted")

        with self.assertRaises(ScratchStateError):
            await self.store.request_promotion(self.project_id, item_id)

        self.assertEqual(self.row(item_id)["promotion_state"], "promoted")

    async def test_a_missing_foreign_or_invisible_item_is_not_found(self):
        foreign = self.seed_item(project_id=uuid4(), expires_at=T0 + HOUR)
        expired = self.seed_item(expires_at=T0)
        for item_id in (uuid4(), foreign, expired):
            with self.subTest(item_id=item_id):
                with self.assertRaises(ScratchItemNotFoundError):
                    await self.store.request_promotion(self.project_id, item_id)
        self.assertEqual(self.row(foreign)["promotion_state"], "none")
        self.assertEqual(self.row(expired)["promotion_state"], "none")

    async def test_an_expired_item_that_is_still_exempt_can_be_requested(self):
        item_id = self.seed_item(expires_at=T0 - HOUR, pinned=True)

        item = await self.store.request_promotion(self.project_id, item_id)

        self.assertTrue(item.expired)
        self.assertEqual(
            item.deferral_reasons,
            (DeferralReason.PINNED, DeferralReason.PROMOTION_PENDING),
        )

    async def test_bad_arguments_are_reported_with_their_field(self):
        with self.assertRaises(InvalidScratchInputError) as raised:
            await self.store.request_promotion(self.project_id, "x")
        self.assertEqual(raised.exception.field, "item_id")


@requires_postgres
class ResolvePromotionTest(PostgresScratchTestCase):
    async def test_a_pending_promotion_can_end_either_way(self):
        for outcome, state in (
            (PromotionOutcome.PROMOTED, PromotionState.PROMOTED),
            (PromotionOutcome.REJECTED, PromotionState.REJECTED),
        ):
            with self.subTest(outcome=outcome):
                item_id = self.seed_pending(expires_at=T0 + HOUR)

                item = await self.store.resolve_promotion(
                    self.project_id, item_id, outcome
                )

                self.assertIs(item.promotion_state, state)
                self.assertIsNone(item.promotion_requested_at)
                self.assertEqual(item.deferral_reasons, ())
                self.assertEqual(item, self.snapshot(item_id))
                row = self.row(item_id)
                self.assertEqual(row["promotion_state"], state.value)
                self.assertIsNone(row["promotion_requested_at"])

    async def test_resolving_twice_with_the_same_outcome_changes_nothing(self):
        item_id = self.seed_pending(expires_at=T0 + HOUR)
        first = await self.store.resolve_promotion(
            self.project_id, item_id, PromotionOutcome.PROMOTED
        )

        second = await self.store.resolve_promotion(
            self.project_id, item_id, PromotionOutcome.PROMOTED
        )

        self.assertEqual(second, first)

    async def test_any_other_state_is_a_state_error_and_changes_nothing(self):
        cases = [
            ("none", PromotionOutcome.PROMOTED),
            ("none", PromotionOutcome.REJECTED),
            ("promoted", PromotionOutcome.REJECTED),
            ("rejected", PromotionOutcome.PROMOTED),
        ]
        for state, outcome in cases:
            with self.subTest(state=state, outcome=outcome):
                item_id = self.seed_item(expires_at=T0 + HOUR, promotion_state=state)

                with self.assertRaises(ScratchStateError):
                    await self.store.resolve_promotion(
                        self.project_id, item_id, outcome
                    )

                self.assertEqual(self.row(item_id)["promotion_state"], state)

    async def test_resolving_the_promotion_of_an_expired_item_ends_the_item(self):
        item_id = self.seed_pending(expires_at=T0 - HOUR)
        await self.store.get(self.project_id, item_id)  # pending keeps it visible

        item = await self.store.resolve_promotion(
            self.project_id, item_id, PromotionOutcome.REJECTED
        )

        self.assertTrue(item.expired)
        self.assertEqual(item.deferral_reasons, ())
        with self.assertRaises(ScratchItemNotFoundError):
            await self.store.get(self.project_id, item_id)

    async def test_a_pinned_item_stays_after_its_promotion_is_resolved(self):
        item_id = self.seed_pending(expires_at=T0 - HOUR, pinned=True)

        await self.store.resolve_promotion(
            self.project_id, item_id, PromotionOutcome.PROMOTED
        )

        item = await self.store.get(self.project_id, item_id)
        self.assertEqual(item.deferral_reasons, (DeferralReason.PINNED,))

    async def test_a_missing_or_foreign_item_is_not_found(self):
        foreign = self.seed_pending(project_id=uuid4(), expires_at=T0 + HOUR)
        for item_id in (uuid4(), foreign):
            with self.subTest(item_id=item_id):
                with self.assertRaises(ScratchItemNotFoundError):
                    await self.store.resolve_promotion(
                        self.project_id, item_id, PromotionOutcome.PROMOTED
                    )
        self.assertEqual(self.row(foreign)["promotion_state"], "pending")

    async def test_the_outcome_must_be_a_promotion_outcome(self):
        item_id = self.seed_pending(expires_at=T0 + HOUR)
        for bad in ("promoted", PromotionState.PROMOTED, None, True):
            with self.subTest(repr(bad)):
                with self.assertRaises(InvalidScratchInputError) as raised:
                    await self.store.resolve_promotion(self.project_id, item_id, bad)
                self.assertEqual(raised.exception.field, "outcome")
        self.assertEqual(self.row(item_id)["promotion_state"], "pending")

    async def test_the_arguments_are_validated_in_the_documented_order(self):
        for args, field in [
            (("x", "y", "z"), "project_id"),
            ((self.project_id, "y", "z"), "item_id"),
            ((self.project_id, uuid4(), "z"), "outcome"),
        ]:
            with self.subTest(field=field):
                with self.assertRaises(InvalidScratchInputError) as raised:
                    await self.store.resolve_promotion(*args)
                self.assertEqual(raised.exception.field, field)
