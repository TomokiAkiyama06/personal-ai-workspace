"""Value objects, errors, limits and the store's constructor (no database needed)."""

import dataclasses
import unittest
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

from paw_backend.db import Database
from paw_backend.research import scratch
from paw_backend.research.scratch import (
    DeferralReason,
    InputProblem,
    InvalidScratchInputError,
    Lease,
    PromotionOutcome,
    PromotionState,
    PurgeResult,
    ScratchBusyError,
    ScratchError,
    ScratchItem,
    ScratchItemNotFoundError,
    ScratchLeaseLimitError,
    ScratchStateError,
    ScratchStore,
    limits,
)

from .scratch_support import T0
from .support import make_settings


def item(**overrides) -> ScratchItem:
    values = {
        "id": uuid4(),
        "project_id": uuid4(),
        "task_id": None,
        "created_by": uuid4(),
        "query": None,
        "title": None,
        "summary": "s",
        "content": None,
        "source_metadata": {},
        "created_at": T0,
        "expires_at": T0 + timedelta(hours=24),
        "expired": False,
        "pinned": False,
        "saved": False,
        "in_use": False,
        "promotion_state": PromotionState.NONE,
        "promotion_requested_at": None,
    }
    values.update(overrides)
    return ScratchItem(**values)


class ScratchItemTest(unittest.TestCase):
    def test_an_item_that_nothing_defers_has_no_reason(self):
        self.assertEqual(item().deferral_reasons, ())

    def test_each_reason_alone(self):
        self.assertEqual(item(pinned=True).deferral_reasons, (DeferralReason.PINNED,))
        self.assertEqual(item(saved=True).deferral_reasons, (DeferralReason.SAVED,))
        self.assertEqual(item(in_use=True).deferral_reasons, (DeferralReason.IN_USE,))
        self.assertEqual(
            item(
                promotion_state=PromotionState.PENDING,
                promotion_requested_at=T0,
            ).deferral_reasons,
            (DeferralReason.PROMOTION_PENDING,),
        )

    def test_finished_promotions_do_not_defer_deletion(self):
        for state in (
            PromotionState.NONE,
            PromotionState.PROMOTED,
            PromotionState.REJECTED,
        ):
            with self.subTest(state):
                self.assertEqual(item(promotion_state=state).deferral_reasons, ())

    def test_all_reasons_come_in_a_fixed_order(self):
        both = item(
            promotion_state=PromotionState.PENDING,
            promotion_requested_at=T0,
            in_use=True,
            pinned=True,
            saved=True,
        )
        self.assertEqual(
            both.deferral_reasons,
            (
                DeferralReason.PINNED,
                DeferralReason.SAVED,
                DeferralReason.IN_USE,
                DeferralReason.PROMOTION_PENDING,
            ),
        )

    def test_records_are_immutable(self):
        snapshot = item()
        lease = Lease(uuid4(), uuid4(), T0, T0 + timedelta(seconds=5))
        result = PurgeResult(1, 2, False)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.pinned = True
        with self.assertRaises(dataclasses.FrozenInstanceError):
            lease.expires_at = T0
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.purged = 5
        self.assertEqual(
            (result.purged, result.deferred, result.has_more), (1, 2, False)
        )

    def test_the_enum_values_are_the_stored_strings(self):
        self.assertEqual(
            [state.value for state in PromotionState],
            ["none", "pending", "promoted", "rejected"],
        )
        self.assertEqual(
            [outcome.value for outcome in PromotionOutcome], ["promoted", "rejected"]
        )
        self.assertEqual(
            [reason.value for reason in DeferralReason],
            ["pinned", "saved", "in_use", "promotion_pending"],
        )


class ErrorsTest(unittest.TestCase):
    def test_every_error_is_a_scratch_error_with_a_distinct_code(self):
        errors = [
            InvalidScratchInputError("summary", InputProblem.TOO_LONG),
            ScratchItemNotFoundError(),
            ScratchStateError(),
            ScratchLeaseLimitError(),
            ScratchBusyError(),
        ]
        for error in errors:
            self.assertIsInstance(error, ScratchError)
        self.assertEqual(
            [error.code for error in errors],
            [
                "invalid_scratch_input",
                "scratch_item_not_found",
                "scratch_state_conflict",
                "scratch_lease_limit",
                "scratch_busy",
            ],
        )

    def test_the_messages_are_fixed(self):
        self.assertEqual(str(ScratchItemNotFoundError()), "Scratch item not found")
        self.assertEqual(
            str(InvalidScratchInputError("summary", InputProblem.TOO_LONG)),
            "Invalid summary: too_long",
        )
        self.assertEqual(
            str(ScratchStateError()),
            "Scratch item is in a state that does not allow this",
        )

    def test_invalid_input_names_the_field_and_the_problem(self):
        error = InvalidScratchInputError("lease_seconds", InputProblem.OUT_OF_RANGE)

        self.assertEqual(error.field, "lease_seconds")
        self.assertIs(error.problem, InputProblem.OUT_OF_RANGE)

    def test_the_problem_vocabulary_is_closed(self):
        self.assertEqual(
            sorted(problem.value for problem in InputProblem),
            [
                "blank",
                "invalid_characters",
                "naive_datetime",
                "out_of_range",
                "required",
                "too_deep",
                "too_large",
                "too_long",
                "unknown_reference",
                "wrong_type",
            ],
        )


class LimitsTest(unittest.TestCase):
    def test_the_ttl_is_24_hours(self):
        self.assertEqual(limits.SCRATCH_TTL, timedelta(hours=24))
        self.assertEqual(limits.expiry_of(T0), datetime(2026, 9, 25, 12, 0, tzinfo=UTC))

    def test_expiry_keeps_the_instant_of_a_non_utc_time(self):
        jst = timezone(timedelta(hours=9))
        created = datetime(2026, 9, 24, 21, 0, tzinfo=jst)

        self.assertEqual(
            limits.expiry_of(created), datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
        )

    def test_the_default_clock_is_aware_utc(self):
        now = limits.utc_now()

        self.assertEqual(now.utcoffset(), timedelta(0))
        self.assertLess(abs(datetime.now(UTC) - now), timedelta(seconds=5))

    def test_the_documented_limits(self):
        self.assertEqual(
            (
                limits.DEFAULT_LEASE_SECONDS,
                limits.MIN_LEASE_SECONDS,
                limits.MAX_LEASE_SECONDS,
                limits.MAX_ACTIVE_LEASES_PER_ITEM,
                limits.DEFAULT_LIST_LIMIT,
                limits.MAX_LIST_LIMIT,
                limits.DEFAULT_PURGE_BATCH_SIZE,
                limits.MAX_PURGE_BATCH_SIZE,
            ),
            (300, 1, 3600, 16, 100, 200, 500, 5000),
        )
        self.assertEqual(
            (
                limits.MAX_QUERY_CHARS,
                limits.MAX_TITLE_CHARS,
                limits.MAX_SUMMARY_CHARS,
                limits.MAX_CONTENT_CHARS,
                limits.MAX_SOURCE_METADATA_BYTES,
                limits.MAX_SOURCE_METADATA_DEPTH,
                limits.MAX_SOURCE_METADATA_KEY_CHARS,
            ),
            (1000, 500, 8000, 100_000, 16_384, 6, 128),
        )

    def test_the_public_package_exports_the_contract(self):
        for name in scratch.__all__:
            with self.subTest(name):
                self.assertTrue(hasattr(scratch, name))


class StoreConstructorTest(unittest.TestCase):
    def database(self) -> Database:
        # Never connects: nothing in the constructor may touch the database.
        return Database(make_settings())

    def test_a_valid_store_is_built_without_connecting(self):
        self.assertIsInstance(ScratchStore(self.database()), ScratchStore)
        self.assertIsInstance(
            ScratchStore(self.database(), clock=lambda: T0, lock_timeout_ms=50),
            ScratchStore,
        )
        self.assertIsInstance(
            ScratchStore(self.database(), lock_timeout_ms=60_000), ScratchStore
        )

    def test_the_database_must_be_a_database(self):
        for bad in (None, object(), "postgresql://x"):
            with self.subTest(bad):
                with self.assertRaises(TypeError):
                    ScratchStore(bad)

    def test_the_clock_must_be_callable_without_arguments(self):
        for bad in ("now", 5, T0):
            with self.subTest(bad):
                with self.assertRaises(TypeError):
                    ScratchStore(self.database(), clock=bad)
        with self.assertRaises(TypeError):
            ScratchStore(self.database(), clock=lambda now: now)

    def test_the_purge_probe_must_be_callable_or_none(self):
        async def probe(session, ids):
            return None

        self.assertIsInstance(
            ScratchStore(self.database(), purge_probe=probe), ScratchStore
        )
        self.assertIsInstance(
            ScratchStore(self.database(), purge_probe=None), ScratchStore
        )
        for bad in ("probe", 5, True):
            with self.subTest(bad):
                with self.assertRaises(TypeError):
                    ScratchStore(self.database(), purge_probe=bad)

    def test_the_lock_timeout_is_an_int_in_range(self):
        for bad in (True, "5000", 5000.0, None):
            with self.subTest(bad):
                with self.assertRaises(TypeError):
                    ScratchStore(self.database(), lock_timeout_ms=bad)
        for bad in (49, 0, -1, 60_001):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    ScratchStore(self.database(), lock_timeout_ms=bad)


if __name__ == "__main__":
    unittest.main()
