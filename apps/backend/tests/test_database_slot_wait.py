"""Abortable calls that wait for a slot (PAW-050 review fix).

``fetch_abortable`` / ``run_abortable`` use at most ``database_pool_size``
connections at once; a further call waits for a slot. The waiting call is part
of the abortable work: cancelling it ends the wait, and ``dispose()`` fails every
waiter with ``DatabaseDisposedError`` instead of letting it start a transaction
once a running one has released its slot (after the engine is disposed).

The integration tests use the ``HangingPostgres`` fake as the stalled holder of
the only slot (``database_pool_size=1``); the last class tests the slot gate
itself.
"""

import asyncio
import time
import unittest

from sqlalchemy import text

from paw_backend.db import Database, DatabaseDisposedError, _Slots

from .fake_postgres import HangingPostgres
from .memory_support import TEST_DATABASE_URL, requires_postgres
from .support import make_settings, wait_until

DEADLINE = 30  # generous: only a hang can reach it


async def select_one(session) -> int:
    return (await session.execute(text("SELECT 1"))).scalar_one()


class SlotWaitTests:
    """The behaviour every kind of abortable call shares (mixed into TestCases)."""

    def call(self, database: Database):
        """One call of the kind under test, as a coroutine."""
        raise NotImplementedError

    async def start(self, server: HangingPostgres):
        database = Database(
            make_settings(
                database_url=f"postgresql://paw:pw@127.0.0.1:{server.port}/paw",
                database_timeout_seconds=30,
                shutdown_timeout_seconds=4,
                database_pool_size=1,
            )
        )
        self.addAsyncCleanup(database.dispose)
        holder = asyncio.create_task(self.call(database))
        self.addCleanup(holder.cancel)
        self.assertTrue(await wait_until(lambda: server.logins == 1))
        await asyncio.sleep(0.2)  # the holder is inside its first statement
        self.assertFalse(holder.done())
        return database, holder

    async def test_dispose_fails_a_call_that_waits_for_a_slot(self):
        async with HangingPostgres() as server:
            database, holder = await self.start(server)
            waiter = asyncio.create_task(self.call(database))
            await asyncio.sleep(0.2)  # queued behind the holder
            self.assertFalse(waiter.done())

            started = time.monotonic()
            await database.dispose()

            done, pending = await asyncio.wait({waiter, holder}, timeout=DEADLINE)
            self.assertEqual(pending, set())
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertIsInstance(waiter.exception(), DatabaseDisposedError)
            # The holder was aborted (an error of its connection, never a result).
            self.assertIsNotNone(holder.exception())
            # The waiter never got a slot: it opened no connection and no engine
            # was created after the disposal.
            self.assertEqual(server.logins, 1)
            self.assertIsNone(database._abortable_engine)
            self.assertEqual(database._probes, set())
            self.assertEqual(database._probe_connections, {})
            # Every slot is back, exactly once.
            self.assertEqual(database._abortable_slots.free, 1)
            self.assertEqual(database._abortable_slots.waiting, 0)

    async def test_dispose_fails_every_waiter(self):
        async with HangingPostgres() as server:
            database, holder = await self.start(server)
            waiters = [asyncio.create_task(self.call(database)) for _ in range(3)]
            await asyncio.sleep(0.2)
            self.assertEqual(database._abortable_slots.waiting, 3)

            await database.dispose()

            done, pending = await asyncio.wait(waiters, timeout=DEADLINE)
            self.assertEqual(pending, set())
            for waiter in waiters:
                self.assertIsInstance(waiter.exception(), DatabaseDisposedError)
            self.assertEqual(server.logins, 1)
            self.assertEqual(database._abortable_slots.free, 1)

    async def test_a_call_started_while_dispose_runs_is_refused(self):
        async with HangingPostgres() as server:
            database, holder = await self.start(server)
            disposal = asyncio.create_task(database.dispose())
            await asyncio.sleep(0)  # dispose() has aborted the holder and waits
            self.assertFalse(disposal.done())

            with self.assertRaises(DatabaseDisposedError):
                await asyncio.wait_for(self.call(database), DEADLINE)

            await asyncio.wait_for(disposal, DEADLINE)
            self.assertEqual(server.logins, 1)
            self.assertIsNone(database._abortable_engine)

    async def test_cancelling_a_waiting_call_ends_the_wait_and_leaks_no_slot(self):
        async with HangingPostgres() as server:
            database, holder = await self.start(server)
            waiter = asyncio.create_task(self.call(database))
            await asyncio.sleep(0.2)
            self.assertEqual(database._abortable_slots.waiting, 1)

            started = time.monotonic()
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertLess(time.monotonic() - started, 1.0)

            # The holder still has the only slot; the waiter neither took nor
            # returned one.
            self.assertEqual(database._abortable_slots.waiting, 0)
            self.assertEqual(database._abortable_slots.free, 0)
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)
            self.assertTrue(await wait_until(lambda: not database._probes, limit=5))
            self.assertEqual(database._abortable_slots.free, 1)
            self.assertEqual(server.logins, 1)  # the waiter never connected


class FetchAbortableSlotWaitTest(SlotWaitTests, unittest.IsolatedAsyncioTestCase):
    def call(self, database):
        return database.fetch_abortable("SELECT 1", timeout_seconds=30)


class RunAbortableSlotWaitTest(SlotWaitTests, unittest.IsolatedAsyncioTestCase):
    def call(self, database):
        return database.run_abortable(select_one)


@requires_postgres
class AfterDisposeTest(unittest.IsolatedAsyncioTestCase):
    async def test_both_kinds_of_call_work_again_once_dispose_has_finished(self):
        # dispose() ends the current work; the object is not closed for good (the
        # engines are created again on first use, as they always were).
        database = Database(
            make_settings(database_url=TEST_DATABASE_URL, database_pool_size=1)
        )
        self.addAsyncCleanup(database.dispose)
        self.assertEqual(await database.run_abortable(select_one), 1)

        await database.dispose()

        self.assertEqual(await database.run_abortable(select_one), 1)
        self.assertEqual(await database.fetch_abortable("SELECT 2"), [(2,)])
        self.assertEqual(database._abortable_slots.free, 1)


class SlotHandedOverBeforeDisposeTest(unittest.IsolatedAsyncioTestCase):
    """A waiter that was given a slot but has not resumed yet when dispose() runs."""

    async def test_it_gives_the_slot_back_and_does_not_start(self):
        database = Database(make_settings(database_pool_size=1))
        await database._acquire_slot()  # the running call's slot
        waiter = asyncio.create_task(database._acquire_slot())
        self.assertTrue(await wait_until(lambda: database._abortable_slots.waiting))

        # The running call finishes: the slot is handed to the waiter, which is
        # scheduled to resume. dispose() (nothing to stop, so it does not yield)
        # completes before it does.
        database._abortable_slots.release()
        await database.dispose()

        with self.assertRaises(DatabaseDisposedError):
            await waiter
        self.assertEqual(database._abortable_slots.free, 1)  # returned once
        await database._acquire_slot()  # and it is usable again
        self.assertEqual(database._abortable_slots.free, 0)


class DisposeDoesNotWaitForTheHolderTest(unittest.IsolatedAsyncioTestCase):
    """The waiters fail at once, whether or not the running call ever lets go."""

    async def test_waiters_fail_while_the_slot_is_still_held(self):
        database = Database(make_settings(database_pool_size=1))
        await database._acquire_slot()  # a running call that dispose() cannot stop
        waiters = [asyncio.create_task(database._acquire_slot()) for _ in range(2)]
        self.assertTrue(
            await wait_until(lambda: database._abortable_slots.waiting == 2)
        )

        await database.dispose()

        results = await asyncio.wait_for(
            asyncio.gather(*waiters, return_exceptions=True), DEADLINE
        )
        self.assertEqual(
            [type(result) for result in results],
            [DatabaseDisposedError, DatabaseDisposedError],
        )
        # The waiters never held a slot; the running call still does.
        self.assertEqual(database._abortable_slots.free, 0)
        self.assertEqual(database._abortable_slots.waiting, 0)


class SlotsTest(unittest.IsolatedAsyncioTestCase):
    async def test_locked_says_whether_no_slot_is_free(self):
        slots = _Slots(2)
        self.assertFalse(slots.locked())
        await slots.acquire()
        self.assertFalse(slots.locked())
        await slots.acquire()
        self.assertTrue(slots.locked())
        slots.release()
        self.assertFalse(slots.locked())

    async def test_it_hands_slots_over_in_order(self):
        slots = _Slots(1)
        await slots.acquire()
        order = []

        async def wait(name):
            await slots.acquire()
            order.append(name)

        first = asyncio.create_task(wait("first"))
        await wait_until(lambda: slots.waiting == 1)
        second = asyncio.create_task(wait("second"))
        await wait_until(lambda: slots.waiting == 2)

        slots.release()
        await asyncio.wait_for(first, DEADLINE)
        self.assertEqual((order, slots.free, slots.waiting), (["first"], 0, 1))
        slots.release()
        await asyncio.wait_for(second, DEADLINE)
        self.assertEqual(order, ["first", "second"])
        self.assertEqual(slots.free, 0)

    async def test_a_new_call_does_not_overtake_a_waiter(self):
        slots = _Slots(1)
        await slots.acquire()
        waiter = asyncio.create_task(slots.acquire())
        await wait_until(lambda: slots.waiting == 1)
        slots.release()  # handed to the waiter, which has not resumed yet
        newcomer = asyncio.create_task(slots.acquire())
        await asyncio.wait_for(waiter, DEADLINE)

        await asyncio.sleep(0.05)
        self.assertFalse(newcomer.done())
        newcomer.cancel()
        await asyncio.gather(newcomer, return_exceptions=True)

    async def test_cancelling_a_waiter_removes_it(self):
        slots = _Slots(1)
        await slots.acquire()
        waiter = asyncio.create_task(slots.acquire())
        await wait_until(lambda: slots.waiting == 1)

        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        self.assertEqual((slots.free, slots.waiting), (0, 0))
        slots.release()
        self.assertEqual(slots.free, 1)  # nobody left to hand it to

    async def test_a_waiter_cancelled_after_it_was_given_a_slot_returns_it(self):
        slots = _Slots(1)
        await slots.acquire()
        waiter = asyncio.create_task(slots.acquire())
        await wait_until(lambda: slots.waiting == 1)

        slots.release()  # handed to the waiter, which has not resumed yet
        waiter.cancel()  # ... and is cancelled before it does
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        self.assertEqual((slots.free, slots.waiting), (1, 0))  # once, not twice
        with self.assertRaises(ValueError):
            slots.release()  # a slot nobody holds cannot be released

    async def test_a_slot_returned_by_a_cancelled_waiter_goes_to_the_next(self):
        slots = _Slots(1)
        await slots.acquire()
        first = asyncio.create_task(slots.acquire())
        await wait_until(lambda: slots.waiting == 1)
        second = asyncio.create_task(slots.acquire())
        await wait_until(lambda: slots.waiting == 2)

        slots.release()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

        await asyncio.wait_for(second, DEADLINE)
        self.assertEqual((slots.free, slots.waiting), (0, 0))

    async def test_a_slot_that_was_never_taken_cannot_be_released(self):
        slots = _Slots(2)
        with self.assertRaises(ValueError):
            slots.release()
        await slots.acquire()
        slots.release()
        with self.assertRaises(ValueError):
            slots.release()
        self.assertEqual(slots.free, 2)

    async def test_fail_waiters_wakes_them_all_and_keeps_the_slots(self):
        slots = _Slots(1)
        await slots.acquire()
        waiters = [asyncio.create_task(slots.acquire()) for _ in range(2)]
        await wait_until(lambda: slots.waiting == 2)

        slots.fail_waiters(lambda: KeyError("closed"))

        results = await asyncio.gather(*waiters, return_exceptions=True)
        self.assertEqual([type(r) for r in results], [KeyError, KeyError])
        self.assertIsNot(results[0], results[1])  # one error per waiter
        self.assertEqual((slots.free, slots.waiting), (0, 0))  # the holder keeps its
        slots.release()
        self.assertEqual(slots.free, 1)

    async def test_a_waiter_failed_and_cancelled_at_once_returns_no_slot(self):
        slots = _Slots(1)
        await slots.acquire()  # the holder
        waiter = asyncio.create_task(slots.acquire())
        await wait_until(lambda: slots.waiting == 1)

        slots.fail_waiters(lambda: KeyError("closed"))
        waiter.cancel()  # before the waiter has seen its error
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        # It never held a slot, so it gave none back: the holder still has it.
        self.assertEqual((slots.free, slots.waiting), (0, 0))
        slots.release()
        self.assertEqual(slots.free, 1)

    async def test_a_slot_is_not_handed_to_a_waiter_that_was_just_cancelled(self):
        slots = _Slots(1)
        await slots.acquire()
        first = asyncio.create_task(slots.acquire())
        await wait_until(lambda: slots.waiting == 1)
        second = asyncio.create_task(slots.acquire())
        await wait_until(lambda: slots.waiting == 2)

        first.cancel()  # its future is cancelled; the task has not run yet
        slots.release()  # must skip it, not fail on it, and serve the second

        with self.assertRaises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(second, DEADLINE)
        self.assertEqual((slots.free, slots.waiting), (0, 0))

    async def test_releasing_after_the_only_waiter_was_just_cancelled_frees_it(self):
        slots = _Slots(1)
        await slots.acquire()
        waiter = asyncio.create_task(slots.acquire())
        await wait_until(lambda: slots.waiting == 1)

        waiter.cancel()
        slots.release()

        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual((slots.free, slots.waiting), (1, 0))


if __name__ == "__main__":
    unittest.main()
