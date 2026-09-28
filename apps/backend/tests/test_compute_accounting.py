"""VRAM accounting and KV-based concurrency: pure functions (PAW-036).

Actual (what the probe sees) and reserved (what the scheduler promised) are kept
apart; the scheduler admits against the larger of the two for its own models,
adds what other workloads use, and keeps a safety headroom free.
"""

import math
import random
import unittest

from paw_backend.compute import ResourceClass
from paw_backend.compute.accounting import (
    DeploymentUsage,
    account,
    headroom_bytes,
)
from paw_backend.compute.concurrency import KvState, kv_refusal, parallelism
from paw_backend.compute.domain import Refusal
from paw_backend.compute.limits import DEFAULT_CLASS_KV_CEILINGS
from paw_backend.compute.probe import GpuDevice, GpuProcess
from tests.compute_support import GIB

UUID = "GPU-a"


def device(total=96 * GIB, used=0):
    return GpuDevice(0, UUID, "GPU", total, used, 0)


def proc(pid, used):
    return GpuProcess(UUID, pid, used)


class HeadroomTest(unittest.TestCase):
    def test_the_larger_of_the_minimum_and_the_fraction(self):
        self.assertEqual(
            headroom_bytes(96 * GIB, minimum_bytes=4 * GIB, fraction=0.05),
            math.floor(96 * GIB * 0.05),
        )
        self.assertEqual(
            headroom_bytes(40 * GIB, minimum_bytes=4 * GIB, fraction=0.05), 4 * GIB
        )
        self.assertEqual(headroom_bytes(96 * GIB, minimum_bytes=0, fraction=0.0), 0)

    def test_never_more_than_the_gpu(self):
        self.assertEqual(headroom_bytes(GIB, minimum_bytes=4 * GIB, fraction=0.05), GIB)


class AccountTest(unittest.TestCase):
    def test_nothing_used_nothing_reserved(self):
        view = account(device(), (), (), headroom=4 * GIB)
        self.assertEqual(view.committed, 0)
        self.assertEqual(view.available, 92 * GIB)
        self.assertFalse(view.under_pressure)

    def test_reserved_but_not_yet_used_counts_as_committed(self):
        # A model that is loading: its memory is promised before it shows up.
        view = account(
            device(),
            (),
            (DeploymentUsage(reserved_bytes=30 * GIB, pids=frozenset({1})),),
            headroom=4 * GIB,
        )
        self.assertEqual(view.reserved, 30 * GIB)
        self.assertEqual(view.actual, 0)
        self.assertEqual(view.committed, 30 * GIB)
        self.assertEqual(view.available, 62 * GIB)

    def test_own_model_that_uses_more_than_reserved_counts_what_it_uses(self):
        view = account(
            device(used=40 * GIB),
            (proc(1, 40 * GIB),),
            (DeploymentUsage(reserved_bytes=30 * GIB, pids=frozenset({1})),),
            headroom=4 * GIB,
        )
        self.assertEqual(view.own_actual, 40 * GIB)
        self.assertEqual(view.external, 0)
        self.assertEqual(view.committed, 40 * GIB)

    def test_other_workloads_are_external_and_counted(self):
        view = account(
            device(used=70 * GIB),
            (proc(1, 30 * GIB), proc(77, 40 * GIB)),
            (DeploymentUsage(reserved_bytes=30 * GIB, pids=frozenset({1})),),
            headroom=4 * GIB,
        )
        self.assertEqual(view.external, 40 * GIB)
        self.assertEqual(view.committed, 70 * GIB)
        self.assertEqual(view.available, 22 * GIB)

    def test_a_model_without_known_pids_is_assumed_to_hold_its_reservation(self):
        # Observe-only: the probe sees 30 GiB but cannot say whose; the 20 GiB
        # model is assumed to be part of it, the rest is external.
        view = account(
            device(used=30 * GIB),
            (proc(5, 30 * GIB),),
            (DeploymentUsage(reserved_bytes=20 * GIB, pids=None),),
            headroom=0,
        )
        self.assertEqual(view.external, 10 * GIB)
        self.assertEqual(view.committed, 30 * GIB)

    def test_memory_of_an_unloaded_model_that_lingers_is_external(self):
        view = account(
            device(used=30 * GIB),
            (proc(1, 30 * GIB),),
            (),  # the model was unloaded: nothing is reserved any more
            headroom=4 * GIB,
        )
        self.assertEqual(view.external, 30 * GIB)
        self.assertEqual(view.committed, 30 * GIB)

    def test_a_model_whose_pids_hold_nothing_is_not_counted_twice(self):
        # The pids command named only the parent (MainPID) while the GPU memory
        # sits on a child: the model is assumed to hold its reservation out of
        # what the probe sees, not reserved and external both.
        view = account(
            device(used=60 * GIB),
            (proc(43, 60 * GIB),),
            (DeploymentUsage(reserved_bytes=66 * GIB, pids=frozenset({42})),),
            headroom=4 * GIB,
        )
        self.assertEqual(view.own_actual, 0)
        self.assertEqual(view.external, 0)
        self.assertEqual(view.committed, 66 * GIB)
        # Another workload beyond the reservation still counts.
        view = account(
            device(used=76 * GIB),
            (proc(43, 60 * GIB), proc(77, 16 * GIB)),
            (DeploymentUsage(reserved_bytes=66 * GIB, pids=frozenset({42})),),
            headroom=4 * GIB,
        )
        self.assertEqual(view.external, 10 * GIB)
        self.assertEqual(view.committed, 76 * GIB)

    def test_extra_reservations(self):
        view = account(device(), (), (), headroom=4 * GIB, extra_reserved=50 * GIB)
        self.assertEqual(view.reserved, 50 * GIB)
        self.assertEqual(view.available, 42 * GIB)

    def test_an_exclusive_jobs_use_counts_against_its_reservation(self):
        # An 80 GiB Exclusive job that uses its 80 GiB: its processes are not a
        # model's, so what they use is absorbed by its reservation, not counted
        # again as external (that would say ~160 GiB committed).
        view = account(
            device(used=80 * GIB),
            (proc(77, 80 * GIB),),
            (),
            headroom=4 * GIB,
            extra_reserved=80 * GIB,
        )
        self.assertEqual(view.external, 0)
        self.assertEqual(view.committed, 80 * GIB)
        self.assertEqual(view.available, 12 * GIB)
        # Beyond its reservation the rest is still counted.
        view = account(
            device(used=94 * GIB),
            (proc(77, 94 * GIB),),
            (),
            headroom=4 * GIB,
            extra_reserved=80 * GIB,
        )
        self.assertEqual(view.committed, 94 * GIB)
        self.assertTrue(view.under_pressure)

    def test_external_memory_from_before_the_exclusive_lease_stays_external(self):
        # 10 GiB of another workload were there before the 80 GiB lease: the
        # reservation absorbs only what grew over that baseline.
        idle = account(
            device(used=10 * GIB),
            (proc(9, 10 * GIB),),
            (),
            headroom=4 * GIB,
            extra_reserved=80 * GIB,
            extra_baseline=10 * GIB,
        )
        self.assertEqual(idle.external, 10 * GIB)
        self.assertEqual(idle.committed, 90 * GIB)
        busy = account(
            device(used=90 * GIB),
            (proc(9, 10 * GIB), proc(77, 80 * GIB)),
            (),
            headroom=4 * GIB,
            extra_reserved=80 * GIB,
            extra_baseline=10 * GIB,
        )
        self.assertEqual(busy.external, 10 * GIB)
        self.assertEqual(busy.committed, 90 * GIB)
        over = account(
            device(used=95 * GIB),
            (proc(9, 10 * GIB), proc(77, 85 * GIB)),
            (),
            headroom=4 * GIB,
            extra_reserved=80 * GIB,
            extra_baseline=10 * GIB,
        )
        self.assertEqual(over.committed, 95 * GIB)

    def test_a_model_with_a_process_of_unknown_usage_holds_its_reservation(self):
        # 80 GiB reserved; one of its pids reports 1 GiB, the other "[N/A]" (the
        # device shows 80 GiB used): the unknown part is the model's, not external.
        view = account(
            device(used=80 * GIB),
            (proc(1, 1 * GIB), GpuProcess(UUID, 2, None)),
            (DeploymentUsage(80 * GIB, frozenset({1, 2})),),
            headroom=4 * GIB,
        )
        self.assertEqual(view.external, 0)
        self.assertEqual(view.committed, 80 * GIB)
        # What is beyond its reservation is still counted.
        view = account(
            device(used=90 * GIB),
            (proc(1, 1 * GIB), GpuProcess(UUID, 2, None)),
            (DeploymentUsage(80 * GIB, frozenset({1, 2})),),
            headroom=4 * GIB,
        )
        self.assertEqual(view.committed, 90 * GIB)

    def test_pressure_when_the_headroom_is_eaten(self):
        view = account(
            device(used=93 * GIB), (proc(9, 93 * GIB),), (), headroom=4 * GIB
        )
        self.assertLess(view.available, 0)
        self.assertTrue(view.under_pressure)

    def test_committed_is_never_below_actual_nor_below_reserved(self):
        rng = random.Random(36)
        for _ in range(500):
            total = 96 * GIB
            pids = list(range(1, 6))
            used_by = {pid: rng.randrange(0, 12) * GIB for pid in pids}
            used = min(total, sum(used_by.values()))
            usages = []
            for pid in pids[:3]:
                known = rng.random() < 0.7
                usages.append(
                    DeploymentUsage(
                        reserved_bytes=rng.randrange(0, 15) * GIB,
                        pids=frozenset({pid}) if known else None,
                    )
                )
            view = account(
                device(total=total, used=used),
                tuple(proc(pid, amount) for pid, amount in used_by.items()),
                tuple(usages),
                headroom=4 * GIB,
                extra_reserved=rng.randrange(0, 20) * GIB,
                extra_baseline=rng.randrange(0, 20) * GIB,
            )
            self.assertGreaterEqual(view.committed, used)
            self.assertGreaterEqual(view.committed, view.reserved)
            self.assertEqual(view.available, total - 4 * GIB - view.committed)


class KvTest(unittest.TestCase):
    CAPACITY = 100_000

    def state(self, reserved=0, sequences=0, observed=None, max_sequences=16):
        return KvState(
            capacity_tokens=self.CAPACITY,
            reserved_tokens=reserved,
            sequences=sequences,
            max_sequences=max_sequences,
            observed_fraction=observed,
        )

    def refusal(self, state, tokens, cls=ResourceClass.INTERACTIVE):
        return kv_refusal(
            state, tokens, cls, safety=0.9, ceilings=DEFAULT_CLASS_KV_CEILINGS
        )

    def test_fits_until_the_safe_part_of_the_pool_is_used(self):
        self.assertIsNone(self.refusal(self.state(), 90_000))
        self.assertEqual(self.refusal(self.state(), 90_001), Refusal.KV_FULL)
        self.assertIsNone(self.refusal(self.state(reserved=60_000), 30_000))
        self.assertEqual(
            self.refusal(self.state(reserved=60_000), 30_001), Refusal.KV_FULL
        )

    def test_the_runtime_observation_is_used_when_it_is_higher(self):
        self.assertEqual(
            self.refusal(self.state(reserved=10_000, observed=0.8), 20_000),
            Refusal.KV_FULL,
        )
        self.assertIsNone(
            self.refusal(self.state(reserved=10_000, observed=0.05), 80_000)
        )

    def test_lower_classes_leave_room_for_higher_ones(self):
        state = self.state(reserved=60_000)
        # Interactive may use 90% of the pool, Background only 0.9 * 0.7 = 63%.
        self.assertIsNone(self.refusal(state, 20_000, ResourceClass.INTERACTIVE))
        self.assertIsNone(self.refusal(state, 20_000, ResourceClass.CODING))
        self.assertEqual(
            self.refusal(state, 20_000, ResourceClass.SUPPORT), Refusal.KV_FULL
        )
        self.assertEqual(
            self.refusal(state, 20_000, ResourceClass.BACKGROUND), Refusal.KV_FULL
        )

    def test_the_number_of_sequences_is_bounded(self):
        self.assertEqual(
            self.refusal(self.state(sequences=16), 1), Refusal.SEQUENCES_FULL
        )

    def test_a_deployment_without_a_pool_only_counts_sequences(self):
        state = KvState(0, 0, 3, 4, None)
        self.assertIsNone(self.refusal(state, 10**9))
        self.assertEqual(
            self.refusal(KvState(0, 0, 4, 4, None), 1), Refusal.SEQUENCES_FULL
        )

    def test_parallelism_follows_the_context_length(self):
        # Long contexts: few at once; short ones: as many as the runtime allows.
        state = self.state()
        self.assertEqual(
            parallelism(
                state,
                30_000,
                ResourceClass.INTERACTIVE,
                safety=0.9,
                ceilings=DEFAULT_CLASS_KV_CEILINGS,
            ),
            3,
        )
        self.assertEqual(
            parallelism(
                state,
                2_000,
                ResourceClass.INTERACTIVE,
                safety=0.9,
                ceilings=DEFAULT_CLASS_KV_CEILINGS,
            ),
            16,
        )
        self.assertEqual(
            parallelism(
                state,
                100_000,
                ResourceClass.INTERACTIVE,
                safety=0.9,
                ceilings=DEFAULT_CLASS_KV_CEILINGS,
            ),
            0,
        )
        busy = self.state(reserved=45_000, sequences=2)
        self.assertEqual(
            parallelism(
                busy,
                15_000,
                ResourceClass.INTERACTIVE,
                safety=0.9,
                ceilings=DEFAULT_CLASS_KV_CEILINGS,
            ),
            3,
        )

    def test_parallelism_agrees_with_admitting_one_after_another(self):
        rng = random.Random(7)
        for _ in range(300):
            tokens = rng.randrange(1, 60_000)
            cls = rng.choice(
                [
                    ResourceClass.INTERACTIVE,
                    ResourceClass.CODING,
                    ResourceClass.SUPPORT,
                    ResourceClass.BACKGROUND,
                ]
            )
            state = self.state(
                reserved=rng.randrange(0, 50_000), sequences=rng.randrange(0, 16)
            )
            expected = parallelism(
                state, tokens, cls, safety=0.9, ceilings=DEFAULT_CLASS_KV_CEILINGS
            )
            admitted = 0
            while self.refusal(state, tokens, cls) is None:
                admitted += 1
                state = KvState(
                    state.capacity_tokens,
                    state.reserved_tokens + tokens,
                    state.sequences + 1,
                    state.max_sequences,
                    None,
                )
            self.assertEqual(admitted, expected)


if __name__ == "__main__":
    unittest.main()
