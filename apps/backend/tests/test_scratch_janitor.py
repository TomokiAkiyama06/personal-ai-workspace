"""ScratchJanitor: the loop that calls ``purge_expired`` (fakes, no database)."""

import asyncio
import math
import unittest
from datetime import timedelta

from paw_backend.db import Database
from paw_backend.research.scratch import (
    DEFAULT_PURGE_BATCH_SIZE,
    MAX_PURGE_BATCH_SIZE,
    PurgeResult,
    PurgeRun,
    ScratchJanitor,
    ScratchStore,
)
from paw_backend.research.scratch import janitor as janitor_module

from .support import make_settings

IDLE = PurgeResult(purged=0, deferred=0, has_more=False)
LOGGER = "paw_backend.research.scratch.janitor"
# A generous limit for things that must happen "at once": never machine speed.
DEADLINE = 30


class ScriptedStore:
    """Answers ``purge_expired`` from a script; idle once the script is used up."""

    def __init__(self, *outcomes: PurgeResult | BaseException) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[int] = []

    async def purge_expired(self, *, batch_size: int) -> PurgeResult:
        self.calls.append(batch_size)
        outcome = self.outcomes.pop(0) if self.outcomes else IDLE
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class StoppingSleep:
    """An injected ``sleep`` that records the delays and ends the loop.

    The loop is stopped the way production stops it, by ``CancelledError``,
    raised from the ``stop_after``-th sleep on.
    """

    def __init__(self, stop_after: int) -> None:
        self.delays: list[float] = []
        self.stop_after = stop_after

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        if len(self.delays) >= self.stop_after:
            raise asyncio.CancelledError
        await asyncio.sleep(0)


class JanitorTestCase(unittest.IsolatedAsyncioTestCase):
    async def run_janitor(
        self, store: ScriptedStore, sleep: StoppingSleep, **options
    ) -> None:
        options.setdefault("interval_seconds", 3600)
        janitor = ScratchJanitor(store, sleep=sleep, **options)
        with self.assertRaises(asyncio.CancelledError):
            await janitor.run()


class ConstructionTest(unittest.TestCase):
    def build(self, **options):
        options.setdefault("interval_seconds", 3600)
        return ScratchJanitor(ScriptedStore(), **options)

    def test_a_real_store_is_accepted(self):
        store = ScratchStore(Database(make_settings()))

        ScratchJanitor(store, interval_seconds=60)

    def test_the_store_must_have_a_purge_expired_that_takes_batch_size(self):
        class Wrong:
            async def purge_expired(self):
                return IDLE

        for store in (object(), None, "store", Wrong()):
            with self.subTest(store=type(store).__name__):
                with self.assertRaises(TypeError):
                    ScratchJanitor(store, interval_seconds=60)

    def test_the_interval_must_be_a_finite_number_above_zero_and_at_most_a_day(self):
        for interval in (0.5, 1, 60, 3600, 86_400, 86_400.0):
            with self.subTest(interval=interval):
                self.build(interval_seconds=interval)
        for interval in (0, 0.0, -1, -0.001, 86_401, math.inf, -math.inf, math.nan):
            with self.subTest(interval=interval):
                with self.assertRaises(ValueError):
                    self.build(interval_seconds=interval)
        for interval in (True, False, "60", None, [60], timedelta(seconds=60)):
            with self.subTest(interval=interval):
                with self.assertRaises(TypeError):
                    self.build(interval_seconds=interval)

    def test_batch_size_and_max_batches_are_bounded_ints(self):
        for name, low, high in (
            ("batch_size", 1, MAX_PURGE_BATCH_SIZE),
            ("max_batches", 1, janitor_module.MAX_MAX_BATCHES),
        ):
            with self.subTest(name):
                self.build(**{name: low})
                self.build(**{name: high})
                for outside in (0, -1, high + 1):
                    with self.assertRaises(ValueError):
                        self.build(**{name: outside})
                for wrong in (True, 5.0, "5", None):
                    with self.assertRaises(TypeError):
                        self.build(**{name: wrong})

    def test_the_sleep_must_be_callable_with_the_seconds(self):
        for sleep in (None, "sleep", 5, lambda: None):
            with self.subTest(sleep=repr(sleep)):
                with self.assertRaises(TypeError):
                    self.build(sleep=sleep)

    def test_an_error_never_contains_the_bad_value(self):
        with self.assertRaises(TypeError) as caught:
            self.build(interval_seconds="hunter2")
        self.assertNotIn("hunter2", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            self.build(interval_seconds=987654321)
        self.assertNotIn("987654321", str(caught.exception))

    def test_the_defaults(self):
        self.assertEqual(DEFAULT_PURGE_BATCH_SIZE, 500)
        self.assertEqual(janitor_module.DEFAULT_MAX_BATCHES, 100)
        self.assertEqual(janitor_module.MAX_INTERVAL_SECONDS, 86_400)
        self.assertEqual(janitor_module.FIRST_TICK_DELAY_SECONDS, 30.0)
        self.assertEqual(janitor_module.RETRY_BASE_SECONDS, 30.0)
        self.assertEqual(janitor_module.CATCH_UP_DELAY_SECONDS, 5.0)


class TickTest(JanitorTestCase):
    async def test_a_tick_works_until_nothing_more_is_due(self):
        store = ScriptedStore(
            PurgeResult(500, 0, True),
            PurgeResult(500, 1, True),
            PurgeResult(37, 3, False),
            PurgeResult(9, 9, False),  # never asked for
        )
        janitor = ScratchJanitor(store, interval_seconds=60, batch_size=500)

        run = await janitor.tick()

        self.assertEqual(
            run, PurgeRun(purged=1037, deferred=3, batches=3, has_more=False)
        )
        self.assertEqual(store.calls, [500, 500, 500])

    async def test_an_idle_tick_is_one_call(self):
        store = ScriptedStore()
        janitor = ScratchJanitor(store, interval_seconds=60, batch_size=7)

        run = await janitor.tick()

        self.assertEqual(run, PurgeRun(purged=0, deferred=0, batches=1, has_more=False))
        self.assertEqual(store.calls, [7])

    async def test_a_tick_stops_at_the_batch_bound_and_reports_the_rest(self):
        store = ScriptedStore(*[PurgeResult(10, 2, True)] * 50)
        janitor = ScratchJanitor(store, interval_seconds=60, max_batches=4)

        run = await janitor.tick()

        self.assertEqual(run, PurgeRun(purged=40, deferred=2, batches=4, has_more=True))
        self.assertEqual(len(store.calls), 4)

    async def test_the_bound_is_exact_when_the_last_allowed_batch_ends_the_work(self):
        store = ScriptedStore(PurgeResult(10, 0, True), PurgeResult(3, 0, False))
        janitor = ScratchJanitor(store, interval_seconds=60, max_batches=2)

        run = await janitor.tick()

        self.assertEqual(
            run, PurgeRun(purged=13, deferred=0, batches=2, has_more=False)
        )

    async def test_the_default_bound_is_a_hundred_batches(self):
        store = ScriptedStore(*[PurgeResult(1, 0, True)] * 1000)
        janitor = ScratchJanitor(store, interval_seconds=60)

        run = await janitor.tick()

        self.assertEqual(run.batches, 100)
        self.assertEqual(len(store.calls), 100)
        self.assertEqual(store.calls[0], DEFAULT_PURGE_BATCH_SIZE)
        self.assertTrue(run.has_more)

    async def test_the_store_error_propagates_out_of_a_tick(self):
        store = ScriptedStore(PurgeResult(5, 0, True), ConnectionError("down"))
        janitor = ScratchJanitor(store, interval_seconds=60)

        with self.assertRaises(ConnectionError):
            await janitor.tick()


class LoopTest(JanitorTestCase):
    async def test_the_first_tick_comes_after_the_start_delay_then_every_interval(self):
        store = ScriptedStore()
        sleep = StoppingSleep(stop_after=4)

        await self.run_janitor(store, sleep, interval_seconds=3600)

        self.assertEqual(sleep.delays, [30.0, 3600.0, 3600.0, 3600.0])
        # A tick ran after each of the first three sleeps, none after the last.
        self.assertEqual(len(store.calls), 3)

    async def test_nothing_is_purged_before_the_first_delay_is_over(self):
        store = ScriptedStore()

        await self.run_janitor(store, StoppingSleep(stop_after=1))

        self.assertEqual(store.calls, [])

    async def test_a_short_interval_shortens_the_start_delay(self):
        sleep = StoppingSleep(stop_after=2)

        await self.run_janitor(ScriptedStore(), sleep, interval_seconds=10)

        self.assertEqual(sleep.delays, [10.0, 10.0])

    async def test_batch_size_is_passed_to_every_call(self):
        store = ScriptedStore(PurgeResult(2, 0, True))

        await self.run_janitor(
            store, StoppingSleep(stop_after=2), batch_size=2, interval_seconds=60
        )

        self.assertEqual(store.calls, [2, 2])

    async def test_a_tick_that_stopped_at_its_bound_is_followed_at_once_by_the_next(
        self,
    ):
        store = ScriptedStore(*[PurgeResult(1, 0, True)] * 3)
        sleep = StoppingSleep(stop_after=4)

        await self.run_janitor(store, sleep, interval_seconds=3600, max_batches=3)

        # After the start delay: the tick hit its bound with work left, so the
        # next one follows after the short catch-up delay; then it is idle.
        self.assertEqual(sleep.delays, [30.0, 5.0, 3600.0, 3600.0])

    async def test_the_catch_up_delay_is_never_longer_than_the_interval(self):
        store = ScriptedStore(PurgeResult(1, 0, True))
        sleep = StoppingSleep(stop_after=2)

        await self.run_janitor(store, sleep, interval_seconds=3, max_batches=1)

        self.assertEqual(sleep.delays, [3.0, 3.0])

    async def test_a_successful_purge_is_logged_with_counts_only(self):
        store = ScriptedStore(PurgeResult(7, 1, False))

        with self.assertLogs(LOGGER, level="INFO") as logs:
            await self.run_janitor(store, StoppingSleep(stop_after=2))

        (line,) = logs.output
        self.assertEqual(
            line,
            f"INFO:{LOGGER}:Scratch purge removed 7 expired item(s) in 1 batch(es)",
        )

    async def test_an_idle_tick_logs_nothing(self):
        with self.assertNoLogs(LOGGER, level="DEBUG"):
            await self.run_janitor(ScriptedStore(), StoppingSleep(stop_after=3))


class FailureIsolationTest(JanitorTestCase):
    async def test_a_failing_tick_does_not_stop_the_loop(self):
        store = ScriptedStore(
            RuntimeError("SELECT ... 'a research secret'"),
            PurgeResult(4, 0, False),
            RuntimeError("again"),
            PurgeResult(2, 0, False),
        )
        sleep = StoppingSleep(stop_after=5)

        with self.assertLogs(LOGGER, level="WARNING"):
            await self.run_janitor(store, sleep, interval_seconds=3600)

        self.assertEqual(len(store.calls), 4)  # every tick ran, the loop lived on
        # Start delay, retry after the failure, the normal interval after the
        # success, the retry delay starts over after the next failure.
        self.assertEqual(sleep.delays, [30.0, 30.0, 3600.0, 30.0, 3600.0])

    async def test_the_log_names_the_exception_type_and_never_its_text(self):
        secret = "postgresql://paw:hunter2@db/paw research secret"
        store = ScriptedStore(ValueError(secret), OSError(secret))

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            await self.run_janitor(store, StoppingSleep(stop_after=3))

        self.assertEqual(
            [record.getMessage() for record in logs.records],
            [
                "Scratch purge failed (ValueError); retrying in 30 s",
                "Scratch purge failed (OSError); retrying in 60 s",
            ],
        )
        for record in logs.records:
            self.assertIsNone(record.exc_info)  # no traceback (it holds the text)
            self.assertNotIn("hunter2", record.getMessage())
            self.assertNotIn("hunter2", str(record.args))

    async def test_the_retry_backs_off_up_to_the_interval_and_resets_on_success(self):
        failures = [RuntimeError("x")] * 6
        store = ScriptedStore(*failures, IDLE, RuntimeError("x"))
        sleep = StoppingSleep(stop_after=9)

        with self.assertLogs(LOGGER, level="WARNING"):
            await self.run_janitor(store, sleep, interval_seconds=300)

        self.assertEqual(
            sleep.delays,
            # start delay, 30 60 120 240, then capped at the interval (300) ...
            [30.0, 30.0, 60.0, 120.0, 240.0, 300.0, 300.0]
            # ... the success returns to the interval, the next failure restarts.
            + [300.0, 30.0],
        )

    async def test_a_retry_is_never_longer_than_a_short_interval(self):
        store = ScriptedStore(RuntimeError("x"), RuntimeError("x"))
        sleep = StoppingSleep(stop_after=3)

        with self.assertLogs(LOGGER, level="WARNING"):
            await self.run_janitor(store, sleep, interval_seconds=7)

        self.assertEqual(sleep.delays, [7.0, 7.0, 7.0])

    async def test_endless_failures_keep_the_loop_alive_with_a_bounded_delay(self):
        store = ScriptedStore(*[OSError("x")] * 1500)
        sleep = StoppingSleep(stop_after=1501)

        with self.assertNoLogs("asyncio", level="ERROR"):
            with self.assertLogs(LOGGER, level="WARNING"):
                await self.run_janitor(store, sleep, interval_seconds=3600)

        self.assertEqual(len(store.calls), 1500)
        self.assertEqual(max(sleep.delays), 3600.0)
        self.assertEqual(sleep.delays[-1], 3600.0)

    async def test_a_partial_tick_keeps_what_it_purged(self):
        # The failure comes on the second batch: the first one is committed
        # (a batch is a transaction), and the next tick continues.
        store = ScriptedStore(
            PurgeResult(5, 0, True), OSError("x"), PurgeResult(3, 0, False)
        )
        sleep = StoppingSleep(stop_after=3)

        with self.assertLogs(LOGGER, level="WARNING"):
            await self.run_janitor(store, sleep, interval_seconds=3600)

        self.assertEqual(len(store.calls), 3)

    async def test_a_bug_in_the_store_is_logged_as_its_type_and_the_loop_lives(
        self,
    ):
        class Broken:
            async def purge_expired(self, *, batch_size):
                raise TypeError("a bug")

        sleep = StoppingSleep(stop_after=2)
        janitor = ScratchJanitor(Broken(), interval_seconds=3600, sleep=sleep)

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            with self.assertRaises(asyncio.CancelledError):
                await janitor.run()

        self.assertEqual(
            logs.output,
            [f"WARNING:{LOGGER}:Scratch purge failed (TypeError); retrying in 30 s"],
        )


class CancellationTest(unittest.IsolatedAsyncioTestCase):
    async def stop(self, task: asyncio.Task) -> None:
        task.cancel()
        done, pending = await asyncio.wait({task}, timeout=DEADLINE)
        self.assertEqual(pending, set(), "the janitor did not stop when cancelled")
        self.assertTrue(task.cancelled())

    async def test_cancelling_the_wait_stops_the_loop(self):
        waiting = asyncio.Event()
        never = asyncio.Event()

        async def sleep(seconds: float) -> None:
            waiting.set()
            await never.wait()

        store = ScriptedStore()
        task = asyncio.create_task(
            ScratchJanitor(store, interval_seconds=60, sleep=sleep).run()
        )
        await asyncio.wait_for(waiting.wait(), DEADLINE)

        await self.stop(task)

        self.assertEqual(store.calls, [])

    async def test_cancelling_a_purge_that_is_running_stops_the_loop(self):
        inside = asyncio.Event()
        released: list[str] = []

        class StuckStore:
            async def purge_expired(self, *, batch_size):
                inside.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    released.append("cleaned up")

        async def no_wait(seconds: float) -> None:
            await asyncio.sleep(0)

        task = asyncio.create_task(
            ScratchJanitor(StuckStore(), interval_seconds=60, sleep=no_wait).run()
        )
        await asyncio.wait_for(inside.wait(), DEADLINE)

        with self.assertNoLogs(LOGGER, level="DEBUG"):
            await self.stop(task)  # it is not a failure to log and go on

        self.assertEqual(released, ["cleaned up"])

    async def test_a_cancellation_raised_by_the_store_is_not_swallowed(self):
        store = ScriptedStore(asyncio.CancelledError())
        sleep = StoppingSleep(stop_after=10)
        janitor = ScratchJanitor(store, interval_seconds=60, sleep=sleep)

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(janitor.run(), DEADLINE)

        self.assertEqual(len(store.calls), 1)
        self.assertEqual(len(sleep.delays), 1)  # only the start delay

    async def test_a_stopped_janitor_leaves_no_task_behind(self):
        before = asyncio.all_tasks()
        never = asyncio.Event()
        waiting = asyncio.Event()

        async def sleep(seconds: float) -> None:
            waiting.set()
            await never.wait()

        task = asyncio.create_task(
            ScratchJanitor(ScriptedStore(), interval_seconds=60, sleep=sleep).run()
        )
        await asyncio.wait_for(waiting.wait(), DEADLINE)
        await self.stop(task)

        self.assertEqual(asyncio.all_tasks() - before, set())


if __name__ == "__main__":
    unittest.main()
