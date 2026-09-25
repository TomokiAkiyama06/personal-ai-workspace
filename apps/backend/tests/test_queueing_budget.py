"""``BudgetTracker`` on a real PostgreSQL. Skipped unless ``PAW_TEST_DATABASE_URL``
is set (except the constructor checks)."""

import asyncio
import contextlib
import math
import unittest
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from types import MappingProxyType
from unittest import mock

from sqlalchemy import event, text

from paw_backend.tasks import TaskNotFoundError
from paw_backend.tasks.queueing import (
    PRESET_LIMITS,
    RECORDABLE_KINDS,
    BudgetKind,
    BudgetNotConfiguredError,
    BudgetPreset,
    BudgetStatus,
    BudgetTracker,
    BudgetUsage,
    InvalidQueueingArgumentError,
    StaleRuntimeSessionError,
)
from paw_backend.tasks.queueing import budget as budget_module
from paw_backend.tasks.queueing.validation import MAX_CONSUMED

from .queueing_support import (
    FakeClock,
    PostgresQueueingTestCase,
    at,
    requires_postgres,
)

K = BudgetKind
STANDARD = BudgetPreset.STANDARD
LONG = BudgetPreset.LONG
UNLIMITED = BudgetPreset.UNLIMITED


def limit(kind: BudgetKind, preset: BudgetPreset = STANDARD) -> int | None:
    return PRESET_LIMITS[preset][kind]


class ConstructorTest(unittest.TestCase):
    def test_a_test_clock_must_be_callable(self):
        BudgetTracker(
            object(), clock=lambda: datetime.now(UTC), allow_explicit_clock=True
        )
        BudgetTracker(object())
        BudgetTracker(object(), allow_explicit_clock=True)
        for bad in (5, "now", datetime.now(UTC)):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    BudgetTracker(object(), clock=bad, allow_explicit_clock=True)
                self.assertEqual(caught.exception.parameter, "clock")

    def test_a_production_tracker_cannot_be_given_a_process_clock(self):
        # Runtime endpoints come from the database clock: a clock is refused unless
        # the caller says it is a test (allow_explicit_clock, as TaskQueue's now).
        for kwargs in ({}, {"allow_explicit_clock": False}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    BudgetTracker(object(), clock=lambda: datetime.now(UTC), **kwargs)
                self.assertEqual(caught.exception.parameter, "clock")

    def test_allow_explicit_clock_must_be_a_bool(self):
        for bad in (1, 0, "yes", None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    BudgetTracker(object(), allow_explicit_clock=bad)
                self.assertEqual(caught.exception.parameter, "allow_explicit_clock")


class StalledBegin:
    """``engine.begin()`` that first says it was reached, then waits for the gate."""

    def __init__(self, engine, reached: asyncio.Event, gate: asyncio.Event) -> None:
        self._engine = engine
        self._reached = reached
        self._gate = gate
        self._transaction = None

    async def __aenter__(self):
        self._reached.set()
        await self._gate.wait()
        self._transaction = self._engine.begin()
        return await self._transaction.__aenter__()

    async def __aexit__(self, *exc_info):
        return await self._transaction.__aexit__(*exc_info)


class StalledDatabase:
    """A database whose transactions stall until ``gate`` is set. A tracker on it
    reads its clock, then stalls before its statement runs (a paused process, a
    slow connection): the delayed writer of a race, made deterministic."""

    def __init__(self, database) -> None:
        self.reached = asyncio.Event()
        self.gate = asyncio.Event()
        self.engine = self
        self._database = database

    def begin(self) -> StalledBegin:
        return StalledBegin(self._database.engine, self.reached, self.gate)


class BudgetTestCase(PostgresQueueingTestCase):
    async def configured_task(self, preset: BudgetPreset = STANDARD) -> uuid.UUID:
        (task_id,) = await self.make_tasks(1)
        await self.budget.set_preset(task_id, preset)
        return task_id

    async def snapshot(self) -> list[dict]:
        return await self.rows(
            "SELECT task_id, kind, preset, consumed, limit_value, running_since, "
            "runtime_generation, settled_through FROM budget_usages "
            "ORDER BY task_id, kind"
        )


@requires_postgres
class ConfigurationTest(BudgetTestCase):
    async def test_a_task_without_a_budget_is_never_treated_as_unlimited(self):
        (task_id,) = await self.make_tasks(1)
        calls = [
            lambda: self.budget.usage(task_id),
            lambda: self.budget.check(task_id),
            lambda: self.budget.record(task_id, K.STEPS, 1),
            lambda: self.budget.start_runtime(task_id),
            lambda: self.budget.stop_runtime(task_id, 1),
        ]
        for call in calls:
            with self.assertRaises(BudgetNotConfiguredError) as caught:
                await call()
            self.assertEqual(caught.exception.code, "budget_not_configured")
        self.assertEqual(await self.scalar("SELECT count(*) FROM budget_usages"), 0)

    async def test_only_set_preset_reports_an_unknown_task_as_not_found(self):
        with self.assertRaises(BudgetNotConfiguredError):
            await self.budget.usage(uuid.uuid4())
        with self.assertRaises(BudgetNotConfiguredError):
            await self.budget.record(uuid.uuid4(), K.STEPS, 1)

    async def test_an_unknown_task_is_not_found(self):
        with self.assertRaises(TaskNotFoundError):
            await self.budget.set_preset(uuid.uuid4(), STANDARD)
        self.assertEqual(await self.scalar("SELECT count(*) FROM budget_usages"), 0)

    async def test_set_preset_creates_one_row_per_kind_with_the_preset_limits(self):
        (task_id,) = await self.make_tasks(1)
        usage = await self.budget.set_preset(task_id, STANDARD)
        self.assertEqual(
            usage,
            tuple(BudgetUsage(kind, 0, limit(kind)) for kind in BudgetKind),
        )
        rows = await self.snapshot()
        self.assertEqual(
            [row["kind"] for row in rows], sorted(k.value for k in BudgetKind)
        )
        for row in rows:
            self.assertEqual(
                (
                    row["preset"],
                    row["consumed"],
                    row["limit_value"],
                    row["running_since"],
                    row["runtime_generation"],
                ),
                ("standard", 0, limit(BudgetKind(row["kind"])), None, 0),
            )

    async def test_the_unlimited_preset_stores_no_limit(self):
        (task_id,) = await self.make_tasks(1)
        usage = await self.budget.set_preset(task_id, UNLIMITED)
        self.assertEqual(usage, tuple(BudgetUsage(k, 0, None) for k in BudgetKind))
        for row in await self.snapshot():
            self.assertEqual((row["preset"], row["limit_value"]), ("unlimited", None))

    async def test_changing_the_preset_keeps_the_consumption(self):
        task_id = await self.configured_task(STANDARD)
        await self.budget.record(task_id, K.TOKENS, 500)
        await self.budget.record(task_id, K.STEPS, 7)
        generation = await self.budget.start_runtime(task_id)
        usage = await self.budget.set_preset(task_id, LONG)
        by_kind = {u.kind: u for u in usage}
        self.assertEqual(
            by_kind[K.TOKENS], BudgetUsage(K.TOKENS, 500, limit(K.TOKENS, LONG))
        )
        self.assertEqual(
            by_kind[K.STEPS], BudgetUsage(K.STEPS, 7, limit(K.STEPS, LONG))
        )
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual(
            (row["preset"], row["running_since"], row["runtime_generation"]),
            ("long", at(0), generation),
        )
        self.assertEqual(await self.scalar("SELECT count(*) FROM budget_usages"), 6)

    async def test_every_preset_can_be_switched_to_every_other(self):
        task_id = await self.configured_task(STANDARD)
        await self.budget.record(task_id, K.TOOL_CALLS, 11)
        for preset in (LONG, UNLIMITED, STANDARD, UNLIMITED, LONG, LONG):
            with self.subTest(preset=preset):
                usage = await self.budget.set_preset(task_id, preset)
                self.assertEqual(
                    usage[list(K).index(K.TOOL_CALLS)],
                    BudgetUsage(K.TOOL_CALLS, 11, limit(K.TOOL_CALLS, preset)),
                )
        self.assertEqual(await self.scalar("SELECT count(*) FROM budget_usages"), 6)

    async def test_lowering_the_preset_below_the_consumption_exceeds_the_budget(self):
        task_id = await self.configured_task(LONG)
        over_standard = limit(K.STEPS, STANDARD) + 1
        await self.budget.record(task_id, K.STEPS, over_standard)
        self.assertEqual((await self.budget.check(task_id)).status, BudgetStatus.OK)
        await self.budget.set_preset(task_id, STANDARD)
        verdict = await self.budget.check(task_id)
        self.assertEqual(
            (verdict.status, verdict.exceeded), (BudgetStatus.EXCEEDED, (K.STEPS,))
        )

    async def test_set_preset_validates_its_arguments(self):
        (task_id,) = await self.make_tasks(1)
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            await self.budget.set_preset(task_id, "standard")
        self.assertEqual(caught.exception.parameter, "preset")
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            await self.budget.set_preset(str(task_id), STANDARD)
        self.assertEqual(caught.exception.parameter, "task_id")
        self.assertEqual(await self.scalar("SELECT count(*) FROM budget_usages"), 0)

    async def test_budgets_of_different_tasks_are_independent(self):
        first = await self.configured_task(STANDARD)
        second = await self.configured_task(UNLIMITED)
        await self.budget.record(first, K.TOKENS, 40)
        await self.budget.record(second, K.TOKENS, 2)
        self.assertEqual((await self.budget_row(first, "tokens"))["consumed"], 40)
        self.assertEqual((await self.budget_row(second, "tokens"))["consumed"], 2)
        self.assertEqual((await self.budget_row(second, "tokens"))["limit_value"], None)

    async def test_simultaneous_set_preset_calls_create_the_six_rows_once(self):
        (task_id,) = await self.make_tasks(1)
        trackers = [self.new_budget() for _ in range(6)]
        results = await asyncio.gather(
            *(t.set_preset(task_id, STANDARD) for t in trackers)
        )
        self.assertEqual(len({tuple(r) for r in results}), 1)
        self.assertEqual(await self.scalar("SELECT count(*) FROM budget_usages"), 6)


@requires_postgres
class RecordTest(BudgetTestCase):
    async def test_consumption_accumulates_and_the_usage_is_returned(self):
        task_id = await self.configured_task()
        first = await self.budget.record(task_id, K.TOKENS, 100)
        second = await self.budget.record(task_id, K.TOKENS, 250)
        self.assertEqual(first, BudgetUsage(K.TOKENS, 100, limit(K.TOKENS)))
        self.assertEqual(second, BudgetUsage(K.TOKENS, 350, limit(K.TOKENS)))
        self.assertEqual((await self.budget_row(task_id, "tokens"))["consumed"], 350)

    async def test_every_recordable_kind_is_counted_separately(self):
        task_id = await self.configured_task()
        for index, kind in enumerate(RECORDABLE_KINDS, start=1):
            await self.budget.record(task_id, kind, index * 10)
        usage = {u.kind: u.consumed for u in await self.budget.usage(task_id)}
        self.assertEqual(
            usage,
            {
                K.RUNTIME_SECONDS: 0,
                K.STEPS: 10,
                K.RETRIES: 20,
                K.TOOL_CALLS: 30,
                K.TOKENS: 40,
                K.GPU_SECONDS: 50,
            },
        )

    async def test_recording_zero_changes_nothing(self):
        task_id = await self.configured_task()
        usage = await self.budget.record(task_id, K.STEPS, 0)
        self.assertEqual(usage, BudgetUsage(K.STEPS, 0, limit(K.STEPS)))

    async def test_only_non_negative_integers_are_accepted_as_amounts(self):
        task_id = await self.configured_task()
        await self.budget.record(task_id, K.GPU_SECONDS, 3)
        before = await self.snapshot()
        for bad in (
            -1,
            0.5,
            1.0,
            math.nan,
            math.inf,
            -math.inf,
            True,
            "5",
            None,
            b"5",
            10**12 + 1,
        ):
            for kind in (K.TOKENS, K.GPU_SECONDS):
                with self.subTest(kind=kind, bad=repr(bad)):
                    with self.assertRaises(InvalidQueueingArgumentError) as caught:
                        await self.budget.record(task_id, kind, bad)
                    self.assertEqual(caught.exception.parameter, "amount")
        self.assertEqual(await self.snapshot(), before)

    async def test_the_largest_single_amount_is_accepted(self):
        task_id = await self.configured_task(UNLIMITED)
        usage = await self.budget.record(task_id, K.TOKENS, 10**12)
        self.assertEqual(usage, BudgetUsage(K.TOKENS, 10**12, None))

    async def test_the_kind_must_be_a_recordable_member(self):
        task_id = await self.configured_task()
        for bad in ("tokens", None, 4, K.RUNTIME_SECONDS):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    await self.budget.record(task_id, bad, 1)
                self.assertEqual(caught.exception.parameter, "kind")
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            await self.budget.record(str(task_id), K.TOKENS, 1)
        self.assertEqual(caught.exception.parameter, "task_id")

    async def test_the_amount_is_not_echoed_in_errors(self):
        task_id = await self.configured_task()
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            await self.budget.record(task_id, K.TOKENS, "secret-amount")
        self.assertNotIn("secret", str(caught.exception))

    async def test_work_that_already_happened_is_recorded_beyond_the_limit(self):
        task_id = await self.configured_task()
        await self.budget.record(task_id, K.TOKENS, limit(K.TOKENS))
        usage = await self.budget.record(task_id, K.TOKENS, 1_000)
        self.assertEqual(usage.consumed, limit(K.TOKENS) + 1_000)
        self.assertEqual(usage.remaining, 0)

    async def test_consumption_saturates_at_the_cap_instead_of_overflowing(self):
        task_id = await self.configured_task(UNLIMITED)
        async with self.database.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE budget_usages SET consumed = :v "
                    "WHERE task_id = :t AND kind = 'tokens'"
                ),
                {"v": MAX_CONSUMED - 5, "t": task_id},
            )
        usage = await self.budget.record(task_id, K.TOKENS, 10)
        self.assertEqual(usage.consumed, MAX_CONSUMED)
        usage = await self.budget.record(task_id, K.TOKENS, 10**12)
        self.assertEqual(usage.consumed, MAX_CONSUMED)

    async def test_simultaneous_increments_from_many_processes_are_never_lost(self):
        task_id = await self.configured_task(UNLIMITED)
        trackers = [self.new_budget() for _ in range(20)]
        await asyncio.gather(
            *(
                t.record(task_id, K.TOKENS, amount)
                for amount, t in enumerate(trackers, 1)
            )
        )
        self.assertEqual(sum(range(1, 21)), 210)
        self.assertEqual((await self.budget_row(task_id, "tokens"))["consumed"], 210)

    async def test_increments_racing_with_preset_changes_are_not_lost(self):
        task_id = await self.configured_task(STANDARD)
        recorders = [self.new_budget() for _ in range(10)]
        switchers = [self.new_budget() for _ in range(4)]
        presets = [LONG, UNLIMITED, STANDARD, LONG]
        await asyncio.gather(
            *(r.record(task_id, K.STEPS, 5) for r in recorders),
            *(
                s.set_preset(task_id, p)
                for s, p in zip(switchers, presets, strict=True)
            ),
        )
        self.assertEqual((await self.budget_row(task_id, "steps"))["consumed"], 50)


@requires_postgres
class CheckTest(BudgetTestCase):
    async def test_a_fresh_budget_is_ok(self):
        task_id = await self.configured_task()
        verdict = await self.budget.check(task_id)
        self.assertEqual(verdict.status, BudgetStatus.OK)
        self.assertEqual(verdict.exceeded, ())
        self.assertEqual(
            verdict.usage,
            tuple(BudgetUsage(kind, 0, limit(kind)) for kind in BudgetKind),
        )

    async def test_using_a_limit_up_exactly_is_not_yet_exceeded(self):
        for kind in RECORDABLE_KINDS:
            task_id = await self.configured_task()
            await self.budget.record(task_id, kind, limit(kind) - 1)
            with self.subTest(kind=kind, consumed="limit - 1"):
                self.assertEqual(
                    (await self.budget.check(task_id)).status, BudgetStatus.OK
                )
            await self.budget.record(task_id, kind, 1)
            verdict = await self.budget.check(task_id)
            with self.subTest(kind=kind, consumed="limit"):
                self.assertEqual(verdict.status, BudgetStatus.OK)
                self.assertEqual(verdict.usage_of(kind).remaining, 0)

    async def test_one_unit_beyond_the_limit_names_exactly_that_kind(self):
        for kind in RECORDABLE_KINDS:
            task_id = await self.configured_task()
            await self.budget.record(task_id, kind, limit(kind) + 1)
            verdict = await self.budget.check(task_id)
            with self.subTest(kind=kind):
                self.assertEqual(verdict.status, BudgetStatus.EXCEEDED)
                self.assertEqual(verdict.exceeded, (kind,))
                self.assertEqual(verdict.usage_of(kind).consumed, limit(kind) + 1)

    async def test_several_exceeded_kinds_are_listed_in_canonical_order(self):
        task_id = await self.configured_task()
        # Recorded in the reverse of the canonical order.
        for kind in (K.GPU_SECONDS, K.TOOL_CALLS, K.STEPS):
            await self.budget.record(task_id, kind, limit(kind) + 1)
        verdict = await self.budget.check(task_id)
        self.assertEqual(verdict.exceeded, (K.STEPS, K.TOOL_CALLS, K.GPU_SECONDS))
        self.assertEqual(verdict.status, BudgetStatus.EXCEEDED)

    async def test_planned_consumption_asks_whether_one_more_would_fit(self):
        task_id = await self.configured_task()
        retries = limit(K.RETRIES)
        await self.budget.record(task_id, K.RETRIES, retries - 1)
        cases = [
            ({K.RETRIES: 1}, BudgetStatus.OK, ()),
            ({K.RETRIES: 2}, BudgetStatus.EXCEEDED, (K.RETRIES,)),
            ({K.RETRIES: 0}, BudgetStatus.OK, ()),
            ({}, BudgetStatus.OK, ()),
            (None, BudgetStatus.OK, ()),
            ({K.STEPS: limit(K.STEPS)}, BudgetStatus.OK, ()),
            ({K.STEPS: limit(K.STEPS) + 1}, BudgetStatus.EXCEEDED, (K.STEPS,)),
            (
                {K.RETRIES: 2, K.STEPS: limit(K.STEPS) + 1},
                BudgetStatus.EXCEEDED,
                (K.STEPS, K.RETRIES),
            ),
        ]
        for planned, status, exceeded in cases:
            with self.subTest(planned=planned):
                verdict = await self.budget.check(task_id, planned=planned)
                self.assertEqual((verdict.status, verdict.exceeded), (status, exceeded))
                # The usage shown never includes what is only planned.
                self.assertEqual(verdict.usage_of(K.RETRIES).consumed, retries - 1)

    async def test_planned_consumption_at_the_limit(self):
        task_id = await self.configured_task()
        await self.budget.record(task_id, K.RETRIES, limit(K.RETRIES))
        self.assertEqual((await self.budget.check(task_id)).status, BudgetStatus.OK)
        verdict = await self.budget.check(task_id, planned={K.RETRIES: 1})
        self.assertEqual(verdict.exceeded, (K.RETRIES,))

    async def test_planned_may_be_any_mapping(self):
        task_id = await self.configured_task()
        planned = MappingProxyType({K.STEPS: limit(K.STEPS) + 1})
        verdict = await self.budget.check(task_id, planned=planned)
        self.assertEqual(verdict.exceeded, (K.STEPS,))

    async def test_the_task_id_is_validated_by_every_method(self):
        calls = {
            "usage": lambda: self.budget.usage("x"),
            "check": lambda: self.budget.check("x"),
            "start_runtime": lambda: self.budget.start_runtime("x"),
            "stop_runtime": lambda: self.budget.stop_runtime("x", 1),
        }
        for name, call in calls.items():
            with self.subTest(method=name):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    await call()
                self.assertEqual(caught.exception.parameter, "task_id")

    async def test_planned_is_validated(self):
        task_id = await self.configured_task()
        for planned, parameter in (
            ([(K.STEPS, 1)], "planned"),
            ("steps", "planned"),
            (5, "planned"),
            ({"steps": 1}, "planned"),
            ({K.STEPS: -1}, "amount"),
            ({K.STEPS: 1.5}, "amount"),
            ({K.STEPS: True}, "amount"),
            ({K.STEPS: None}, "amount"),
            ({K.STEPS: math.nan}, "amount"),
            ({K.STEPS: 10**12 + 1}, "amount"),
        ):
            with self.subTest(planned=repr(planned)):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    await self.budget.check(task_id, planned=planned)
                self.assertEqual(caught.exception.parameter, parameter)

    async def test_the_unlimited_preset_is_never_exceeded_but_is_still_measured(self):
        task_id = await self.configured_task(UNLIMITED)
        for kind in RECORDABLE_KINDS:
            await self.budget.record(task_id, kind, 10**12)
        verdict = await self.budget.check(task_id, planned={K.TOKENS: 10**12})
        self.assertEqual((verdict.status, verdict.exceeded), (BudgetStatus.OK, ()))
        for kind in RECORDABLE_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(
                    verdict.usage_of(kind), BudgetUsage(kind, 10**12, None)
                )
                self.assertIsNone(verdict.usage_of(kind).remaining)

    async def test_check_is_idempotent_and_writes_nothing(self):
        task_id = await self.configured_task()
        await self.budget.record(task_id, K.STEPS, limit(K.STEPS) + 3)
        await self.budget.start_runtime(task_id)
        self.clock.set(500)
        before = await self.snapshot()
        first = await self.budget.check(task_id)
        second = await self.budget.check(task_id)
        self.assertEqual(first, second)
        self.assertEqual(await self.snapshot(), before)

    async def test_the_verdict_usage_equals_the_usage_call(self):
        task_id = await self.configured_task(LONG)
        await self.budget.record(task_id, K.TOKENS, 12_345)
        await self.budget.start_runtime(task_id)
        self.clock.set(42)
        self.assertEqual(
            (await self.budget.check(task_id)).usage, await self.budget.usage(task_id)
        )

    async def test_usage_lists_the_six_kinds_in_canonical_order(self):
        task_id = await self.configured_task()
        usage = await self.budget.usage(task_id)
        self.assertEqual([u.kind for u in usage], list(BudgetKind))

    async def test_budgets_do_not_leak_between_tasks(self):
        first = await self.configured_task()
        second = await self.configured_task()
        await self.budget.record(first, K.STEPS, limit(K.STEPS) + 1)
        self.assertEqual((await self.budget.check(first)).exceeded, (K.STEPS,))
        self.assertEqual((await self.budget.check(second)).exceeded, ())


@requires_postgres
class RuntimeTest(BudgetTestCase):
    async def test_a_run_in_progress_is_included_without_being_written(self):
        task_id = await self.configured_task()
        await self.budget.start_runtime(task_id)
        self.clock.set(100)
        usage = {u.kind: u for u in await self.budget.usage(task_id)}
        self.assertEqual(usage[K.RUNTIME_SECONDS].consumed, 100)
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual((row["consumed"], row["running_since"]), (0, at(0)))

    async def test_stopping_adds_the_whole_elapsed_seconds(self):
        task_id = await self.configured_task()
        generation = await self.budget.start_runtime(task_id)
        self.clock.set(130.9)
        usage = await self.budget.stop_runtime(task_id, generation)
        self.assertEqual(
            usage, BudgetUsage(K.RUNTIME_SECONDS, 130, limit(K.RUNTIME_SECONDS))
        )
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual((row["consumed"], row["running_since"]), (130, None))

    async def test_stopping_twice_adds_the_time_only_once(self):
        task_id = await self.configured_task()
        generation = await self.budget.start_runtime(task_id)
        self.clock.set(60)
        await self.budget.stop_runtime(task_id, generation)
        self.clock.set(9_999)
        again = await self.budget.stop_runtime(task_id, generation)
        self.assertEqual(again.consumed, 60)
        self.assertEqual(
            (await self.budget_row(task_id, "runtime_seconds"))["consumed"], 60
        )

    async def test_runs_accumulate(self):
        task_id = await self.configured_task()
        first = await self.budget.start_runtime(task_id)
        self.clock.set(130.9)
        await self.budget.stop_runtime(task_id, first)
        self.clock.set(200)
        second = await self.budget.start_runtime(task_id)
        self.clock.set(210)
        usage = await self.budget.stop_runtime(task_id, second)
        self.assertEqual(usage.consumed, 140)

    async def test_time_between_runs_is_not_counted(self):
        task_id = await self.configured_task()
        generation = await self.budget.start_runtime(task_id)
        self.clock.set(10)
        await self.budget.stop_runtime(task_id, generation)
        self.clock.set(100_000)
        usage = {u.kind: u for u in await self.budget.usage(task_id)}
        self.assertEqual(usage[K.RUNTIME_SECONDS].consumed, 10)

    async def test_starting_twice_keeps_the_first_start(self):
        task_id = await self.configured_task()
        first = await self.budget.start_runtime(task_id)
        self.clock.set(50)
        second = await self.budget.start_runtime(task_id)
        # The run in progress is not restarted: its time is neither lost nor
        # counted twice. The second start takes the session over (10).
        self.assertEqual(
            (await self.budget_row(task_id, "runtime_seconds"))["running_since"], at(0)
        )
        self.assertEqual((first, second), (1, 2))
        self.clock.set(100)
        self.assertEqual(
            (await self.budget.stop_runtime(task_id, second)).consumed, 100
        )

    async def test_stopping_a_session_that_was_never_started_changes_nothing(self):
        # Contract change: stop_runtime needs the generation of a session that
        # start_runtime returned. A task that never started one has none, so any
        # generation is stale (before, this was a silent no-op).
        task_id = await self.configured_task()
        before = await self.snapshot()
        for generation in (1, 2, 10**6):
            with self.subTest(generation=generation):
                with self.assertRaises(StaleRuntimeSessionError) as caught:
                    await self.budget.stop_runtime(task_id, generation)
                self.assertEqual(caught.exception.code, "runtime_session_stale")
        self.assertEqual(await self.snapshot(), before)

    async def test_elapsed_time_is_floored_to_whole_seconds(self):
        task_id = await self.configured_task()
        await self.budget.start_runtime(task_id)
        for seconds, expected in ((0, 0), (0.9, 0), (1, 1), (1.999, 1), (2, 2)):
            self.clock.set(seconds)
            usage = (await self.budget.usage(task_id))[0]
            with self.subTest(seconds=seconds):
                self.assertEqual(usage.consumed, expected)

    async def test_the_runtime_limit_is_exceeded_one_second_after_it_is_reached(self):
        task_id = await self.configured_task()
        maximum = limit(K.RUNTIME_SECONDS)
        generation = await self.budget.start_runtime(task_id)
        self.clock.set(maximum - 1)
        self.assertEqual((await self.budget.check(task_id)).status, BudgetStatus.OK)
        self.clock.set(maximum)
        verdict = await self.budget.check(task_id)
        self.assertEqual(verdict.status, BudgetStatus.OK)
        self.assertEqual(verdict.usage_of(K.RUNTIME_SECONDS).remaining, 0)
        self.clock.set(maximum + 1)
        verdict = await self.budget.check(task_id)
        self.assertEqual(
            (verdict.status, verdict.exceeded),
            (BudgetStatus.EXCEEDED, (K.RUNTIME_SECONDS,)),
        )
        # Stopping keeps it exceeded.
        await self.budget.stop_runtime(task_id, generation)
        self.assertEqual(
            (await self.budget.check(task_id)).exceeded, (K.RUNTIME_SECONDS,)
        )

    async def test_planned_runtime_is_checked_against_the_limit(self):
        task_id = await self.configured_task()
        maximum = limit(K.RUNTIME_SECONDS)
        await self.budget.start_runtime(task_id)
        self.clock.set(maximum - 10)
        ok = await self.budget.check(task_id, planned={K.RUNTIME_SECONDS: 10})
        self.assertEqual(ok.status, BudgetStatus.OK)
        over = await self.budget.check(task_id, planned={K.RUNTIME_SECONDS: 11})
        self.assertEqual(over.exceeded, (K.RUNTIME_SECONDS,))

    async def test_a_clock_that_went_backwards_counts_as_zero(self):
        task_id = await self.configured_task()
        self.clock.set(100)
        generation = await self.budget.start_runtime(task_id)
        self.clock.set(50)
        self.assertEqual((await self.budget.usage(task_id))[0].consumed, 0)
        usage = await self.budget.stop_runtime(task_id, generation)
        self.assertEqual(usage.consumed, 0)
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual((row["consumed"], row["running_since"]), (0, None))

    async def test_the_unlimited_preset_has_no_runtime_limit(self):
        task_id = await self.configured_task(UNLIMITED)
        await self.budget.start_runtime(task_id)
        self.clock.set(10**9)
        verdict = await self.budget.check(task_id)
        self.assertEqual(verdict.status, BudgetStatus.OK)
        self.assertEqual(
            verdict.usage_of(K.RUNTIME_SECONDS),
            BudgetUsage(K.RUNTIME_SECONDS, 10**9, None),
        )

    async def test_the_test_clock_replaces_the_database_clock(self):
        task_id = await self.configured_task()
        self.clock.set(7)
        await self.budget.start_runtime(task_id)
        self.assertGreater(self.clock.calls, 0)
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual(row["running_since"], at(7))

    async def test_a_clock_without_a_timezone_is_rejected(self):
        task_id = await self.configured_task()
        naive = self.new_budget(clock=lambda: datetime(2030, 1, 1))
        for call in (
            lambda: naive.start_runtime(task_id),
            lambda: naive.stop_runtime(task_id, 1),
            lambda: naive.usage(task_id),
            lambda: naive.check(task_id),
        ):
            with self.assertRaises(InvalidQueueingArgumentError) as caught:
                await call()
            self.assertEqual(caught.exception.parameter, "clock")
        wrong = self.new_budget(clock=lambda: 1234.5)
        with self.assertRaises(InvalidQueueingArgumentError):
            await wrong.start_runtime(task_id)
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertIsNone(row["running_since"])

    async def test_simultaneous_stops_add_the_time_once(self):
        task_id = await self.configured_task()
        generation = await self.budget.start_runtime(task_id)
        self.clock.set(100)
        trackers = [self.new_budget() for _ in range(6)]
        results = await asyncio.gather(
            *(t.stop_runtime(task_id, generation) for t in trackers)
        )
        self.assertEqual({u.consumed for u in results}, {100})
        self.assertEqual(
            (await self.budget_row(task_id, "runtime_seconds"))["consumed"], 100
        )

    async def test_a_delayed_stop_of_a_reclaimed_execution_keeps_the_new_timer(self):
        task_id = await self.configured_task()
        maximum = limit(K.RUNTIME_SECONDS)
        old = await self.budget.start_runtime(task_id)  # worker A
        self.clock.set(100)
        new = await self.budget.start_runtime(task_id)  # worker B, after A's lease
        self.assertGreater(new, old)  # expired and the entry was reclaimed
        before = await self.snapshot()
        self.clock.set(150)
        with self.assertRaises(StaleRuntimeSessionError):
            await self.budget.stop_runtime(task_id, old)  # A's delayed stop
        # Nothing changed: B's timer still runs from the first start, and the
        # generation is B's.
        self.assertEqual(await self.snapshot(), before)
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual(
            (row["consumed"], row["running_since"], row["runtime_generation"]),
            (0, at(0), new),
        )
        # check() keeps accruing B's runtime, so the limit still bites.
        self.clock.set(200)
        self.assertEqual((await self.budget.usage(task_id))[0].consumed, 200)
        self.clock.set(maximum + 1)
        verdict = await self.budget.check(task_id)
        self.assertEqual(verdict.exceeded, (K.RUNTIME_SECONDS,))
        # B stops its own session normally.
        self.clock.set(250)
        self.assertEqual((await self.budget.stop_runtime(task_id, new)).consumed, 250)

    async def test_a_delayed_stop_of_a_restarted_run_keeps_the_new_timer(self):
        task_id = await self.configured_task()
        old = await self.budget.start_runtime(task_id)
        self.clock.set(10)
        await self.budget.stop_runtime(task_id, old)  # the old run ended normally
        self.clock.set(100)
        new = await self.budget.start_runtime(task_id)  # the restarted task runs
        self.clock.set(120)
        # The old worker (a retry of its stop, or a slow duplicate) arrives late.
        with self.assertRaises(StaleRuntimeSessionError):
            await self.budget.stop_runtime(task_id, old)
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual(
            (row["consumed"], row["running_since"], row["runtime_generation"]),
            (10, at(100), new),
        )
        self.clock.set(150)
        self.assertEqual((await self.budget.usage(task_id))[0].consumed, 60)
        self.assertEqual((await self.budget.stop_runtime(task_id, new)).consumed, 60)

    async def test_the_newer_session_stops_only_with_its_own_generation(self):
        task_id = await self.configured_task()
        old = await self.budget.start_runtime(task_id)
        new = await self.budget.start_runtime(task_id)
        self.assertEqual((old, new), (1, 2))
        self.clock.set(30)
        # Neither an older generation nor one that was never issued stops it.
        for wrong in (old, new + 1, 10**6):
            with self.subTest(generation=wrong):
                with self.assertRaises(StaleRuntimeSessionError):
                    await self.budget.stop_runtime(task_id, wrong)
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual((row["consumed"], row["running_since"]), (0, at(0)))
        self.assertEqual((await self.budget.stop_runtime(task_id, new)).consumed, 30)

    async def test_generations_only_grow_across_stops(self):
        task_id = await self.configured_task()
        seen = []
        for second in (10, 20, 30):
            self.clock.set(second)
            generation = await self.budget.start_runtime(task_id)
            seen.append(generation)
            self.clock.set(second + 5)
            await self.budget.stop_runtime(task_id, generation)
        self.assertEqual(seen, [1, 2, 3])
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual((row["consumed"], row["runtime_generation"]), (15, 3))
        # An old generation cannot stop the run of a later session either.
        self.clock.set(40)
        latest = await self.budget.start_runtime(task_id)
        with self.assertRaises(StaleRuntimeSessionError):
            await self.budget.stop_runtime(task_id, seen[0])
        self.assertEqual(
            (await self.budget_row(task_id, "runtime_seconds"))["running_since"], at(40)
        )
        self.clock.set(41)
        self.assertEqual((await self.budget.stop_runtime(task_id, latest)).consumed, 16)

    async def test_a_stale_stop_and_the_current_stop_at_the_same_time(self):
        task_id = await self.configured_task()
        old = await self.budget.start_runtime(task_id)
        new = await self.budget.start_runtime(task_id)
        self.clock.set(100)
        trackers = [self.new_budget() for _ in range(4)]
        results = await asyncio.gather(
            trackers[0].stop_runtime(task_id, old),
            trackers[1].stop_runtime(task_id, new),
            trackers[2].stop_runtime(task_id, old),
            trackers[3].stop_runtime(task_id, new),
            return_exceptions=True,
        )
        self.assertIsInstance(results[0], StaleRuntimeSessionError)
        self.assertIsInstance(results[2], StaleRuntimeSessionError)
        self.assertEqual([results[1].consumed, results[3].consumed], [100, 100])
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual((row["consumed"], row["running_since"]), (100, None))

    async def test_simultaneous_starts_leave_exactly_one_current_session(self):
        task_id = await self.configured_task()
        trackers = [self.new_budget() for _ in range(5)]
        generations = await asyncio.gather(
            *(t.start_runtime(task_id) for t in trackers)
        )
        self.assertEqual(sorted(generations), [1, 2, 3, 4, 5])
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual((row["running_since"], row["runtime_generation"]), (at(0), 5))
        self.clock.set(10)
        for generation in sorted(generations)[:-1]:
            with self.assertRaises(StaleRuntimeSessionError):
                await self.budget.stop_runtime(task_id, generation)
        self.assertEqual((await self.budget.stop_runtime(task_id, 5)).consumed, 10)

    async def test_a_delayed_replacement_start_cannot_predate_the_old_stop(self):
        task_id = await self.configured_task()
        old = await self.budget.start_runtime(task_id)  # worker A, t=0
        stalled = StalledDatabase(self.database)
        replacement = BudgetTracker(
            stalled, clock=FakeClock(at(100)), allow_explicit_clock=True
        )
        # Worker B reads its clock (t=100) and then stalls before its statement.
        start = asyncio.create_task(replacement.start_runtime(task_id))
        try:
            await asyncio.wait_for(stalled.reached.wait(), timeout=30)
            self.clock.set(150)
            # A's stop settles the run through t=150 first and clears the timer.
            settled = await self.budget.stop_runtime(task_id, old)
            self.assertEqual(settled.consumed, 150)
        finally:
            stalled.gate.set()
        new = await asyncio.wait_for(start, timeout=30)
        self.assertEqual(new, old + 1)
        # B's timer starts at the cutoff of A's stop, not at its own earlier reading
        # (t=100): 100..150 is already charged.
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual(
            (row["consumed"], row["running_since"], row["runtime_generation"]),
            (150, at(150), new),
        )
        self.clock.set(160)
        self.assertEqual((await self.budget.usage(task_id))[0].consumed, 160)
        self.assertEqual((await self.budget.stop_runtime(task_id, new)).consumed, 160)

    async def test_a_start_waiting_on_the_row_lock_starts_after_the_stop_settled(
        self,
    ):
        # The same race with the real interleaving: both statements wait on the row
        # lock, the stop first. The start already holds its earlier reading (t=100)
        # and is re-evaluated against the row the stop wrote.
        task_id = await self.configured_task()
        old = await self.budget.start_runtime(task_id)  # t=0
        replacement = self.new_budget(clock=FakeClock(at(100)))
        stop = start = None
        try:
            async with self.database.engine.connect() as holder:
                await holder.execute(
                    text(
                        "SELECT 1 FROM budget_usages "
                        "WHERE task_id = :t AND kind = 'runtime_seconds' FOR UPDATE"
                    ),
                    {"t": task_id},
                )
                self.clock.set(150)
                stop = asyncio.create_task(self.budget.stop_runtime(task_id, old))
                await self.wait_for_lock_waiters(1)
                start = asyncio.create_task(replacement.start_runtime(task_id))
                await self.wait_for_lock_waiters(2)
                await holder.rollback()
            settled = await asyncio.wait_for(stop, timeout=30)
            new = await asyncio.wait_for(start, timeout=30)
        finally:
            for pending in (stop, start):
                if pending is not None and not pending.done():
                    pending.cancel()
        self.assertEqual(settled.consumed, 150)
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual(
            (row["consumed"], row["running_since"], row["runtime_generation"]),
            (150, at(150), new),
        )
        self.clock.set(160)
        self.assertEqual((await self.budget.stop_runtime(task_id, new)).consumed, 160)

    async def test_a_start_never_predates_the_previous_stops_cutoff(self):
        task_id = await self.configured_task()
        self.clock.set(100)
        first = await self.budget.start_runtime(task_id)
        self.clock.set(50)  # the clock went backwards: nothing is charged
        await self.budget.stop_runtime(task_id, first)
        self.clock.set(60)
        second = await self.budget.start_runtime(task_id)
        # The interval up to t=100 was settled (as an empty one) by the first stop.
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual((row["running_since"], row["consumed"]), (at(100), 0))
        self.clock.set(130)
        self.assertEqual((await self.budget.stop_runtime(task_id, second)).consumed, 30)

    async def test_the_cutoff_only_moves_forward_across_backwards_clocks(self):
        task_id = await self.configured_task()
        self.clock.set(0)
        session = await self.budget.start_runtime(task_id)
        self.clock.set(150)
        await self.budget.stop_runtime(task_id, session)  # settled through t=150
        for reading in (130, 120, 200):
            self.clock.set(reading)
            session = await self.budget.start_runtime(task_id)
            self.clock.set(110)  # a stop from a host whose clock is behind
            await self.budget.stop_runtime(task_id, session)
        # The last stop settled through t=200 (its own running_since), although its
        # clock said 110: a later start cannot go back before that.
        self.clock.set(160)
        session = await self.budget.start_runtime(task_id)
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual((row["consumed"], row["running_since"]), (150, at(200)))
        self.clock.set(230)
        self.assertEqual(
            (await self.budget.stop_runtime(task_id, session)).consumed, 180
        )

    async def test_a_takeover_start_keeps_the_running_timer_and_the_cutoff(self):
        task_id = await self.configured_task()
        self.clock.set(10)
        first = await self.budget.start_runtime(task_id)
        self.clock.set(40)
        await self.budget.stop_runtime(task_id, first)  # settled through t=40
        self.clock.set(50)
        await self.budget.start_runtime(task_id)
        self.clock.set(20)  # a delayed replacement start that sampled t=20
        takeover = await self.budget.start_runtime(task_id)
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertEqual(
            (row["running_since"], row["settled_through"]), (at(50), at(40))
        )
        self.clock.set(70)
        self.assertEqual(
            (await self.budget.stop_runtime(task_id, takeover)).consumed, 50
        )

    async def test_the_generation_is_validated_before_anything_is_written(self):
        task_id = await self.configured_task()
        await self.budget.start_runtime(task_id)
        before = await self.snapshot()
        for bad in (0, -1, True, "1", 1.0, None, 2**63):
            with self.subTest(generation=repr(bad)):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    await self.budget.stop_runtime(task_id, bad)
                self.assertEqual(caught.exception.parameter, "generation")
        self.assertEqual(await self.snapshot(), before)

    async def test_runtime_cannot_be_reported_with_record(self):
        task_id = await self.configured_task()
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            await self.budget.record(task_id, K.RUNTIME_SECONDS, 5)
        self.assertEqual(caught.exception.parameter, "kind")


@contextlib.contextmanager
def process_clock_off_by(offset_seconds: float) -> Iterator[list[float]]:
    """Make this process's clock, as ``budget`` reads it (``datetime.now``), run
    ``offset_seconds`` ahead of (behind, if negative) the real time: one host of a
    fleet whose clocks disagree. Yields the list of readings taken meanwhile."""
    readings: list[float] = []
    real = datetime

    class SkewedDatetime(real):
        @classmethod
        def now(cls, tz=None):
            readings.append(offset_seconds)
            return cls.fromtimestamp(real.now(UTC).timestamp() + offset_seconds, tz)

    with mock.patch.object(budget_module, "datetime", SkewedDatetime):
        yield readings


def runtime_of(usage: tuple[BudgetUsage, ...]) -> BudgetUsage:
    return next(item for item in usage if item.kind is K.RUNTIME_SECONDS)


@requires_postgres
class RuntimeClockAuthorityTest(BudgetTestCase):
    """The persisted runtime endpoints (``running_since``, ``settled_through`` and
    the charged seconds) come from ONE clock, the database's (Decision 0007, 10): a
    ``BudgetTracker`` built without a test clock never reads its process's clock,
    however far apart the clocks of the hosts are. Time passes by moving the stored
    instants back in the database (never by sleeping)."""

    async def shift_timer(self, task_id: uuid.UUID, seconds: float) -> None:
        """As if ``seconds`` more had passed since the timer's stored instants."""
        await self.owner_sql(
            "UPDATE budget_usages SET "
            "running_since = running_since - make_interval(secs => :s), "
            "settled_through = settled_through - make_interval(secs => :s) "
            "WHERE task_id = :t AND kind = 'runtime_seconds'",
            s=seconds,
            t=task_id,
        )

    async def database_now(self) -> datetime:
        (row,) = await self.rows("SELECT clock_timestamp() AS now")
        return row["now"]

    async def test_a_tracker_without_a_test_clock_reads_the_database_clock(self):
        task_id = await self.configured_task()
        tracker = BudgetTracker(self.new_database())
        before = await self.database_now()
        generation = await tracker.start_runtime(task_id)
        after = await self.database_now()
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertLessEqual(before, row["running_since"])
        self.assertLessEqual(row["running_since"], after)
        before = await self.database_now()
        await tracker.stop_runtime(task_id, generation)
        after = await self.database_now()
        row = await self.budget_row(task_id, "runtime_seconds")
        self.assertLessEqual(before, row["settled_through"])
        self.assertLessEqual(row["settled_through"], after)

    async def test_a_replacement_on_a_host_with_another_clock_is_charged_fully(self):
        # The review scenario: the host that stops a session runs ahead of the host
        # that starts the replacement. With process clocks the cutoff written by the
        # first put the second timer into its future, and the whole replacement
        # session was charged 0 (a budget bypass).
        for host_a, host_b in ((3600, -3600), (-3600, 3600), (86_400, -86_400)):
            with self.subTest(host_a=host_a, host_b=host_b):
                task_id = await self.configured_task()
                a = BudgetTracker(self.new_database())
                b = BudgetTracker(self.new_database())
                with process_clock_off_by(host_a) as reads_a:
                    first = await a.start_runtime(task_id)
                    await self.shift_timer(task_id, 200.5)
                    stopped = await a.stop_runtime(task_id, first)
                self.assertGreaterEqual(stopped.consumed, 200)
                self.assertLess(stopped.consumed, 260)
                with process_clock_off_by(host_b) as reads_b:
                    second = await b.start_runtime(task_id)
                    await self.shift_timer(task_id, 100.5)
                    finished = await b.stop_runtime(task_id, second)
                self.assertEqual(second, first + 1)
                # Both sessions are charged, 200 + 100 seconds (plus the real few
                # milliseconds): not 200 (the second one lost) and not hours more.
                self.assertGreaterEqual(finished.consumed, 300)
                self.assertLess(finished.consumed, 360)
                self.assertEqual((reads_a, reads_b), ([], []), "a process clock read")

    async def test_every_host_sees_a_running_timer_at_the_databases_time(self):
        for host_a, host_b in ((3600, -3600), (-3600, 3600)):
            with self.subTest(host_a=host_a, host_b=host_b):
                task_id = await self.configured_task()
                a = BudgetTracker(self.new_database())
                b = BudgetTracker(self.new_database())
                with process_clock_off_by(host_a) as reads_a:
                    first = await a.start_runtime(task_id)
                await self.shift_timer(task_id, 200.5)
                with process_clock_off_by(host_b) as reads_b:
                    seen = runtime_of(await b.usage(task_id))
                    verdict = await b.check(task_id)
                    kept = runtime_of(await b.set_preset(task_id, STANDARD))
                for item in (seen, verdict.usage_of(K.RUNTIME_SECONDS), kept):
                    self.assertGreaterEqual(item.consumed, 200)
                    self.assertLess(item.consumed, 260)
                # The stop reports the same, on either host (the second is a
                # repeated stop of a stopped session: it only reports).
                stopped = await a.stop_runtime(task_id, first)
                self.assertGreaterEqual(stopped.consumed, 200)
                self.assertLess(stopped.consumed, 260)
                with process_clock_off_by(host_b) as reads_c:
                    again = await b.stop_runtime(task_id, first)
                self.assertEqual(again, stopped)
                self.assertEqual((reads_a, reads_b, reads_c), ([], [], []))

    async def test_each_statement_reads_the_database_clock_exactly_once(self):
        # Two readings in one statement would make the charged seconds and the
        # stored cutoff differ by microseconds (and the cutoff no longer bound the
        # elapsed time): every statement takes ONE reading (a materialised CTE).
        task_id = await self.configured_task()
        database = self.new_database()
        tracker = BudgetTracker(database)
        statements: list[str] = []

        def record(connection, cursor, statement, *_):
            statements.append(statement)

        event.listen(database.engine.sync_engine, "before_cursor_execute", record)
        try:
            generation = await tracker.start_runtime(task_id)
            start_statements, statements[:] = list(statements), []
            await tracker.usage(task_id)
            usage_statements, statements[:] = list(statements), []
            await tracker.stop_runtime(task_id, generation)
            stop_statements = list(statements)
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", record)
        for name, executed in (
            ("start", start_statements),
            ("usage", usage_statements),
            ("stop", stop_statements),
        ):
            with self.subTest(statement=name):
                (timed,) = [sql for sql in executed if "clock_timestamp()" in sql]
                self.assertEqual(timed.count("clock_timestamp()"), 1)
                self.assertTrue(timed.startswith("WITH clock AS"))


if __name__ == "__main__":
    unittest.main()
