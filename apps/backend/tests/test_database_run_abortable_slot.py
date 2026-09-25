"""``Database.run_abortable`` keeps its slot until the transaction task has exited.

The slot bounds how many dedicated connections exist (at most
``database_pool_size``). A caller that is cancelled returns at once, but the
transaction task it abandoned is still alive until the driver has cleaned up
(and the server-side statement of an aborted connection may run on). Giving the
slot back when the CALLER returns therefore let the next call open a connection
while the previous one still existed, and repeated cancellations could exceed the
cap (issue #90, finding on #75). Like ``fetch_abortable`` / ``transact_abortable``
(``Database._abortable``), the slot is returned by a done callback of the
transaction task. Needs ``PAW_TEST_DATABASE_URL`` (skipped otherwise).
"""

import asyncio
import unittest

from sqlalchemy import text

from paw_backend.db import Database

from .memory_support import TEST_DATABASE_URL, requires_postgres
from .support import make_settings, wait_until

DEADLINE = 30  # generous: only a hang can reach it


async def select_one(session) -> int:
    return (await session.execute(text("SELECT 1"))).scalar_one()


@requires_postgres
class SlotHeldUntilTheTransactionEndsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database = Database(
            make_settings(database_url=TEST_DATABASE_URL, database_pool_size=1)
        )
        self.addAsyncCleanup(self.database.dispose)
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def linger(self, session) -> None:
        """A transaction on a live connection that is still busy when abandoned."""
        await session.execute(text("SELECT 1"))  # the connection now exists
        self.entered.set()
        await self.release.wait()

    async def cancel_the_caller(self) -> asyncio.Task:
        call = asyncio.create_task(self.database.run_abortable(self.linger))
        await asyncio.wait_for(self.entered.wait(), DEADLINE)
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        return call

    async def test_the_slot_is_not_free_while_the_abandoned_transaction_lives(self):
        await self.cancel_the_caller()

        # The caller is gone; its transaction task is not.
        self.assertEqual(len(self.database._probes), 1)
        self.assertEqual(self.database._abortable_slots.free, 0)

        self.release.set()
        self.assertTrue(await wait_until(lambda: not self.database._probes))
        self.assertTrue(
            await wait_until(lambda: self.database._abortable_slots.free == 1)
        )

    async def test_the_next_call_waits_for_the_abandoned_transaction_to_exit(self):
        await self.cancel_the_caller()

        following = asyncio.create_task(self.database.run_abortable(select_one))
        await asyncio.sleep(0.3)  # generous: it must not get a connection meanwhile
        self.assertFalse(following.done())
        self.assertEqual(self.database._abortable_slots.waiting, 1)

        self.release.set()
        self.assertEqual(await asyncio.wait_for(following, DEADLINE), 1)

    async def test_repeated_cancellations_never_exceed_the_pool_size(self):
        # Two cancelled callers whose transactions are still alive hold both
        # slots of a pool of two: a third call gets no connection until one ends.
        database = Database(
            make_settings(database_url=TEST_DATABASE_URL, database_pool_size=2)
        )
        self.addAsyncCleanup(database.dispose)
        started = [asyncio.Event(), asyncio.Event()]
        releases = [asyncio.Event(), asyncio.Event()]

        def lingering(index: int):
            async def work(session) -> None:
                await session.execute(text("SELECT 1"))
                started[index].set()
                await releases[index].wait()

            return work

        try:
            for index in range(2):
                call = asyncio.create_task(database.run_abortable(lingering(index)))
                await asyncio.wait_for(started[index].wait(), DEADLINE)
                call.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await call
            self.assertEqual(len(database._probes), 2)

            third = asyncio.create_task(database.run_abortable(select_one))
            await asyncio.sleep(0.3)  # generous: it must not get a connection
            self.assertFalse(third.done())
            self.assertEqual(len(database._probes), 2)  # still only the two

            releases[1].set()  # one abandoned transaction ends: the third runs
            self.assertEqual(await asyncio.wait_for(third, DEADLINE), 1)
        finally:
            for release in releases:
                release.set()
        self.assertTrue(await wait_until(lambda: not database._probes))
        self.assertTrue(await wait_until(lambda: database._abortable_slots.free == 2))

    async def test_a_normal_call_still_returns_its_slot(self):
        for _ in range(3):
            self.assertEqual(await self.database.run_abortable(select_one), 1)
        self.assertTrue(
            await wait_until(lambda: self.database._abortable_slots.free == 1)
        )

    async def test_a_failing_transaction_still_returns_its_slot(self):
        async def boom(session):
            await session.execute(text("SELECT 1"))
            raise KeyError

        with self.assertRaises(KeyError):
            await self.database.run_abortable(boom)
        self.assertTrue(
            await wait_until(lambda: self.database._abortable_slots.free == 1)
        )
        self.assertEqual(await self.database.run_abortable(select_one), 1)


if __name__ == "__main__":
    unittest.main()
