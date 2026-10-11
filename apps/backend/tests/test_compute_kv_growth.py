"""A running request's reservation follows its conversation (Decision 0076, 3;
#200).

* ``ComputeLease.resize(context_tokens)`` changes the reservation and never
  waits: growth is granted past the class's share and ``kv_safety``; a new
  request then waits at the limit (the queue of Decision 0037, 4) and a shrink
  admits it;
* a new request counts the lower classes' reservations each up to its share and
  in sum up to the highest lower share, so growth below never shuts out a higher
  class (Interactive keeps 1 − 0.95 of the safe pool);
* above the model's ``max_context_tokens`` the resize is refused
  (``CONTEXT_TOO_LONG``) and the reservation stays.

Fakes only: no GPU, no model.
"""

import asyncio
import random
import unittest

from paw_backend.compute import (
    ComputeRequest,
    ComputeUnavailableError,
    DeploymentState,
    InvalidComputeArgumentError,
    Placement,
    Refusal,
    ResourceClass,
)
from paw_backend.compute.concurrency import (
    KvState,
    counted_tokens,
    kv_refusal,
    parallelism,
)
from paw_backend.compute.config import DEFAULT_CLASS_KV_CEILINGS
from tests.compute_support import build, embedding_spec, main_spec, settle

IC = ResourceClass.INTERACTIVE
CO = ResourceClass.CODING
SU = ResourceClass.SUPPORT
BG = ResourceClass.BACKGROUND

# The fake main model: a pool of 131,072 tokens, 90% of it usable.
SAFE = 117_964  # floor(131,072 * 0.9)
CODING_SHARE = 112_066  # floor(131,072 * 0.9 * 0.95)


def request(cls=CO, deployment="main", tokens=1_000, **options):
    return ComputeRequest(cls, deployment=deployment, context_tokens=tokens, **options)


class CountingTest(unittest.TestCase):
    """The pure rule (``concurrency.counted_tokens``)."""

    CAPACITY = 100_000  # shares of the safe 90,000: 90,000 / 85,500 / 76,500 / 63,000

    def state(self, observed=None, **by_class):
        reserved = {cls: by_class.get(cls.value, 0) for cls in ResourceClass}
        reserved.pop(ResourceClass.EXCLUSIVE)
        return KvState(
            capacity_tokens=self.CAPACITY,
            reserved_tokens=sum(reserved.values()),
            sequences=0,
            max_sequences=64,
            observed_fraction=observed,
            reserved_by_class=reserved,
        )

    def counted(self, state, cls):
        return counted_tokens(
            state, cls, safety=0.9, ceilings=DEFAULT_CLASS_KV_CEILINGS
        )

    def refusal(self, state, tokens, cls):
        return kv_refusal(
            state, tokens, cls, safety=0.9, ceilings=DEFAULT_CLASS_KV_CEILINGS
        )

    def test_lower_classes_grown_past_their_shares_leave_the_upper_room(self):
        # Support and Background grew past their shares (76,500 and 63,000):
        # each counts up to its share, their sum up to the highest lower share.
        state = self.state(support=80_000, background=70_000)
        # Interactive: min(76,500 + 63,000, 85,500 (Coding's share)) = 85,500.
        self.assertEqual(self.counted(state, IC), 85_500)
        self.assertIsNone(self.refusal(state, 4_500, IC))
        self.assertEqual(self.refusal(state, 4_501, IC), Refusal.KV_FULL)
        # Coding: min(76,500 + 63,000, 76,500 (Support's share)) = 76,500.
        self.assertEqual(self.counted(state, CO), 76_500)
        self.assertIsNone(self.refusal(state, 9_000, CO))
        self.assertEqual(self.refusal(state, 9_001, CO), Refusal.KV_FULL)
        # Support counts itself as it is: past its share, nothing more.
        self.assertEqual(self.counted(state, SU), 80_000 + 63_000)
        self.assertEqual(self.refusal(state, 1, SU), Refusal.KV_FULL)
        self.assertEqual(self.counted(state, BG), 150_000)

    def test_the_same_class_and_the_classes_above_count_as_they_are(self):
        state = self.state(interactive=10_000, coding=90_000)
        self.assertEqual(self.counted(state, CO), 100_000)
        self.assertEqual(self.refusal(state, 1, CO), Refusal.KV_FULL)
        # Interactive: its own 10,000 and Coding up to its share (85,500).
        self.assertEqual(self.counted(state, IC), 95_500)
        self.assertEqual(self.refusal(state, 1, IC), Refusal.KV_FULL)
        alone = self.state(coding=90_000)
        self.assertEqual(self.counted(alone, IC), 85_500)
        self.assertIsNone(self.refusal(alone, 4_500, IC))  # the 5% of Interactive

    def test_reservations_within_their_shares_count_as_before(self):
        # Without growth no lower class is past its share: the same answers as
        # counting every reservation as it is.
        rng = random.Random(76)
        for _ in range(500):
            shares = {IC: 90_000, CO: 85_500, SU: 76_500, BG: 63_000}
            by_class = {cls: rng.randrange(0, shares[cls] // 3) for cls in shares}
            state = self.state(**{cls.value: n for cls, n in by_class.items()})
            plain = KvState(self.CAPACITY, state.reserved_tokens, 0, 64, None)
            for cls in shares:
                tokens = rng.randrange(1, 60_000)
                with self.subTest(by_class=by_class, cls=cls, tokens=tokens):
                    self.assertEqual(
                        self.refusal(state, tokens, cls),
                        self.refusal(plain, tokens, cls),
                    )

    def test_the_observed_usage_still_counts_when_it_is_higher(self):
        state = self.state(background=70_000, observed=0.95)
        self.assertEqual(self.refusal(state, 1, IC), Refusal.KV_FULL)

    def test_parallelism_counts_the_same_way(self):
        state = self.state(coding=90_000)
        self.assertEqual(
            parallelism(
                state, 1_500, IC, safety=0.9, ceilings=DEFAULT_CLASS_KV_CEILINGS
            ),
            3,  # 4,500 / 1,500
        )
        self.assertEqual(
            parallelism(
                state, 1_500, CO, safety=0.9, ceilings=DEFAULT_CLASS_KV_CEILINGS
            ),
            0,
        )


class ResizeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler, self.probe, self.control, self.clock = build()
        await self.scheduler.refresh()

    async def lease(self, cls=CO, tokens=1_000):
        admission = await self.scheduler.try_acquire(request(cls, tokens=tokens))
        self.assertIsNone(admission.refusal)
        return admission.lease

    def reserved(self):
        return self.scheduler.status().deployment("main").reserved_tokens

    async def test_a_running_request_grows_and_shrinks_its_reservation(self):
        lease = await self.lease(tokens=20_000)
        lease.resize(45_000)
        self.assertEqual((lease.tokens, self.reserved()), (45_000, 45_000))
        lease.resize(30_000)
        self.assertEqual((lease.tokens, self.reserved()), (30_000, 30_000))
        await lease.release()
        self.assertEqual(self.reserved(), 0)

    async def test_growth_never_waits_even_past_the_share_and_kv_safety(self):
        leases = [await self.lease(tokens=50_000) for _ in range(2)]
        for lease in leases:
            lease.resize(65_536)  # the model's longest context
        # 131,072 reserved: past Coding's share (112,066) and the safe 117,964.
        self.assertEqual(self.reserved(), 131_072)
        self.assertGreater(self.reserved(), SAFE)
        # A new Coding request waits at the limit; it counts the growth.
        admission = await self.scheduler.try_acquire(request(CO, tokens=1))
        self.assertEqual(admission.refusal, Refusal.KV_FULL)
        for lease in leases:
            await lease.release()

    async def test_a_new_request_waits_until_a_shrink_makes_room(self):
        leases = [await self.lease(tokens=40_000) for _ in range(2)]
        leases[0].resize(65_000)
        leases[1].resize(50_000)  # 115,000: over Coding's share
        waiter = asyncio.create_task(
            self.scheduler.acquire(request(CO, tokens=10_000), wait_seconds=60)
        )
        await settle()
        self.assertFalse(waiter.done())
        self.assertEqual(self.scheduler.status().waiting[CO], 1)
        leases[0].resize(60_000)  # 110,000: still no room for 10,000
        await settle()
        self.assertFalse(waiter.done())
        leases[0].resize(50_000)  # 100,000 + 10,000 <= 112,066
        new = await asyncio.wait_for(waiter, 5)
        self.assertEqual(new.placement, Placement.LOCAL_GPU)
        self.assertEqual(self.reserved(), 110_000)
        for lease in (*leases, new):
            await lease.release()

    async def test_interactive_keeps_its_share_while_coding_has_grown(self):
        leases = [await self.lease(tokens=25_000) for _ in range(4)]
        for lease in leases:
            lease.resize(32_768)  # 131,072
        # Coding counts up to its share (112,066): 5,898 of the safe 117,964
        # are left for Interactive.
        self.assertEqual(self.scheduler.parallelism("main", 5_898, IC), 1)
        refused = await self.scheduler.try_acquire(request(IC, tokens=5_899))
        self.assertEqual(refused.refusal, Refusal.KV_FULL)
        chat = await self.lease(IC, tokens=5_898)
        self.assertEqual(chat.placement, Placement.LOCAL_GPU)
        # The interactive lease is counted as it is (no more room for chat).
        refused = await self.scheduler.try_acquire(request(IC, tokens=1))
        self.assertEqual(refused.refusal, Refusal.KV_FULL)
        for lease in (*leases, chat):
            await lease.release()
        self.assertEqual(self.reserved(), 0)

    async def test_interactive_goes_past_a_waiting_coding_request(self):
        leases = [await self.lease(tokens=50_000) for _ in range(2)]
        for lease in leases:
            lease.resize(65_536)
        coding = asyncio.create_task(
            self.scheduler.acquire(request(CO, tokens=10_000), wait_seconds=60)
        )
        await settle()
        self.assertFalse(coding.done())
        # A chat request arriving later is not queued behind it (Decision 0037,
        # 4) and fits in Interactive's own share.
        chat = await self.scheduler.acquire(request(IC, tokens=5_000), wait_seconds=0)
        self.assertEqual(chat.placement, Placement.LOCAL_GPU)
        await chat.release()
        for lease in leases:
            await lease.release()
        await (await asyncio.wait_for(coding, 5)).release()

    async def test_above_the_models_context_the_resize_is_refused(self):
        lease = await self.lease(tokens=20_000)
        with self.assertRaises(ComputeUnavailableError) as raised:
            lease.resize(65_537)
        self.assertEqual(raised.exception.reason, Refusal.CONTEXT_TOO_LONG)
        self.assertEqual((lease.tokens, self.reserved()), (20_000, 20_000))
        lease.resize(65_536)
        self.assertEqual(self.reserved(), 65_536)
        await lease.release()

    async def test_wrong_values_and_leases_are_refused(self):
        lease = await self.lease(tokens=1_000)
        for value in (-1, True, 1.5, "10", None, 1 << 25):
            with self.subTest(value), self.assertRaises(InvalidComputeArgumentError):
                lease.resize(value)
        self.assertEqual(self.reserved(), 1_000)
        await lease.release()
        with self.assertRaises(InvalidComputeArgumentError):
            lease.resize(2_000)
        self.assertEqual(self.reserved(), 0)

    async def test_a_cpu_lease_reserves_nothing_but_keeps_the_limit(self):
        scheduler, *_ = build(
            (main_spec(), embedding_spec(initial=DeploymentState.CPU)),
            control=False,
        )
        await scheduler.refresh()
        lease = (
            await scheduler.try_acquire(request(SU, deployment="embed", tokens=100))
        ).lease
        self.assertEqual(lease.placement, Placement.LOCAL_CPU)
        lease.resize(8_000)
        self.assertEqual(lease.tokens, 0)
        with self.assertRaises(ComputeUnavailableError):
            lease.resize(8_193)
        await lease.release()

    async def test_a_cloud_lease_does_not_change(self):
        self.probe.fail = True
        await self.scheduler.refresh()
        lease = await self.scheduler.acquire(
            request(CO, tokens=1_000, allow_cloud=True), wait_seconds=0
        )
        self.assertEqual(lease.placement, Placement.CLOUD)
        lease.resize(200_000)
        self.assertEqual(lease.tokens, 0)
        await lease.release()

    async def test_coding_capacity_counts_the_growth(self):
        lease = await self.lease(tokens=10_000)
        before = self.scheduler.coding_capacity()
        lease.resize(65_536)
        after = self.scheduler.coding_capacity()
        self.assertEqual(before.running, after.running)
        self.assertLessEqual(after.limit, before.limit)
        await lease.release()


if __name__ == "__main__":
    unittest.main()
