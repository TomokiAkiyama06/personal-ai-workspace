"""``TaskQueue.cancel(only_if_task_terminal=True)`` and ``cancel_in`` (Issue #83).

Decision 0008, section 8, item 8: the stop processor cancels a queue entry because
its task is finished. That was a decision from a state that could go stale before
the entry was cancelled: another caller could Restart the task in between, and the
entry (now the restarted task's own) was cancelled anyway. The conditional cancel
share-locks the TASK row and cancels only if the task is Completed, Failed or
Cancelled; ``cancel_in`` does the same in a transaction of the caller. Real
PostgreSQL; the races are interleaved with a step that holds a Restart's transaction
open and with the lock waits PostgreSQL reports, never with sleeps.
"""

import asyncio
import unittest
import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from paw_backend.tasks import TaskCommand, TaskService, TaskState
from paw_backend.tasks.queueing import InvalidQueueingArgumentError, LeaseLostError

from .queueing_support import PostgresQueueingTestCase, requires_postgres

FINISHED = (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED)
ACTIVE = (
    TaskState.QUEUED,
    TaskState.RUNNING,
    TaskState.WAITING,
    TaskState.PAUSED,
    TaskState.EVALUATING,
)
DEADLINE = 30


class HoldOpen:
    """A step for ``execute`` that keeps the command's transaction open."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, session, task_id, project_id) -> None:
        self.reached.set()
        await self.release.wait()


class CancelTestCase(PostgresQueueingTestCase):
    async def statuses(self, task_id: uuid.UUID) -> list[str]:
        rows = await self.rows(
            "SELECT status FROM queue_entries WHERE task_id = :t ORDER BY id",
            t=task_id,
        )
        return [row["status"] for row in rows]

    async def task_state(self, task_id: uuid.UUID) -> str:
        (row,) = await self.rows("SELECT state FROM tasks WHERE id = :t", t=task_id)
        return row["state"]

    def spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.ensure_future(coroutine)

        async def stop() -> None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(stop)
        return task


@requires_postgres
class ConditionalCancelTest(CancelTestCase):
    async def test_the_entry_of_a_finished_task_is_cancelled(self):
        for state in FINISHED:
            with self.subTest(state=state.value):
                task_id = await self.task_in_state(state)
                entry = await self.queue.enqueue(task_id)

                cancelled = await self.queue.cancel(task_id, only_if_task_terminal=True)

                self.assertIs(cancelled, True)
                self.assertEqual(await self.statuses(task_id), ["cancelled"])
                row = await self.entry_row(entry.id)
                self.assertIsNotNone(row["finished_at"])
                self.assertIsNone(row["lease_expires_at"])

    async def test_a_claimed_entry_of_a_finished_task_loses_its_lease(self):
        task_id = await self.task_in_state(TaskState.CANCELLED)
        await self.queue.enqueue(task_id)
        claimed = await self.queue.claim_next("worker-1")
        assert claimed is not None

        cancelled = await self.queue.cancel(task_id, only_if_task_terminal=True)

        self.assertIs(cancelled, True)
        self.assertEqual(await self.statuses(task_id), ["cancelled"])
        with self.assertRaises(LeaseLostError):
            await self.queue.heartbeat(claimed.id, "worker-1", claimed.claim_count)

    async def test_the_entry_of_an_active_task_is_kept(self):
        for state in ACTIVE:
            with self.subTest(state=state.value):
                task_id = await self.task_in_state(state)
                await self.queue.enqueue(task_id)

                cancelled = await self.queue.cancel(task_id, only_if_task_terminal=True)

                self.assertIs(cancelled, False)
                self.assertEqual(await self.statuses(task_id), ["queued"])
                self.assertEqual(await self.task_state(task_id), state.value)

    async def test_the_claimed_entry_of_a_running_task_keeps_its_lease(self):
        task_id = await self.task_in_state(TaskState.RUNNING)
        await self.queue.enqueue(task_id)
        claimed = await self.queue.claim_next("worker-1")
        assert claimed is not None and claimed.task_id == task_id

        cancelled = await self.queue.cancel(task_id, only_if_task_terminal=True)

        self.assertIs(cancelled, False)
        self.assertEqual(await self.statuses(task_id), ["claimed"])
        heartbeat = await self.queue.heartbeat(
            claimed.id, "worker-1", claimed.claim_count
        )
        self.assertEqual(heartbeat.status.value, "claimed")

    async def test_nothing_happens_for_an_unknown_task_or_a_task_without_an_entry(
        self,
    ):
        finished = await self.task_in_state(TaskState.FAILED)

        self.assertIs(
            await self.queue.cancel(uuid.uuid4(), only_if_task_terminal=True), False
        )
        self.assertIs(
            await self.queue.cancel(finished, only_if_task_terminal=True), False
        )
        self.assertEqual(await self.statuses(finished), [])

    async def test_it_is_idempotent(self):
        task_id = await self.task_in_state(TaskState.CANCELLED)
        await self.queue.enqueue(task_id)

        first = await self.queue.cancel(task_id, only_if_task_terminal=True)
        second = await self.queue.cancel(task_id, only_if_task_terminal=True)

        self.assertEqual((first, second), (True, False))

    async def test_the_plain_cancel_still_cancels_the_entry_of_an_active_task(self):
        task_id = await self.task_in_state(TaskState.QUEUED)
        await self.queue.enqueue(task_id)

        self.assertIs(await self.queue.cancel(task_id), True)
        self.assertEqual(await self.statuses(task_id), ["cancelled"])
        self.assertEqual(await self.task_state(task_id), "queued")

    async def test_the_task_row_is_share_locked_while_the_entry_is_cancelled(self):
        task_id = await self.task_in_state(TaskState.CANCELLED)
        await self.queue.enqueue(task_id)

        async with self.database.session() as session, session.begin():
            self.assertTrue(
                await self.queue.cancel_in(session, task_id, only_if_task_terminal=True)
            )
            # Another connection cannot take FOR NO KEY UPDATE (what every task
            # command takes first) until this transaction ends.
            with self.assertRaises(DBAPIError) as caught:
                async with self.database.engine.begin() as other:
                    await other.execute(text("SET LOCAL lock_timeout = '50ms'"))
                    await other.execute(
                        text("SELECT id FROM tasks WHERE id = :t FOR NO KEY UPDATE"),
                        {"t": task_id},
                    )
            self.assertEqual(caught.exception.orig.sqlstate, "55P03")  # lock timeout

    async def test_the_arguments_are_checked_before_the_database_is_used(self):
        task_id = uuid.uuid4()
        with self.captured_statements() as statements:
            for bad in (None, 1, 0, "yes", "true", object(), [True]):
                with self.subTest(only_if_task_terminal=repr(bad)):
                    with self.assertRaises(InvalidQueueingArgumentError) as caught:
                        await self.queue.cancel(task_id, only_if_task_terminal=bad)
                    self.assertEqual(
                        caught.exception.parameter, "only_if_task_terminal"
                    )
            for bad in (None, "id", 5, task_id.hex, object()):
                with self.subTest(task_id=repr(bad)):
                    with self.assertRaises(InvalidQueueingArgumentError) as caught:
                        await self.queue.cancel(bad, only_if_task_terminal=True)
                    self.assertEqual(caught.exception.parameter, "task_id")
            async with self.database.session() as session:
                for bad in (None, object(), "session", self.database, 5):
                    with self.subTest(session=repr(bad)):
                        with self.assertRaises(InvalidQueueingArgumentError) as caught:
                            await self.queue.cancel_in(bad, task_id)
                        self.assertEqual(caught.exception.parameter, "session")
                for bad in (None, task_id.hex):
                    with self.assertRaises(InvalidQueueingArgumentError) as caught:
                        await self.queue.cancel_in(session, bad)
                    self.assertEqual(caught.exception.parameter, "task_id")
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    await self.queue.cancel_in(session, task_id, now=1)
                self.assertEqual(caught.exception.parameter, "now")
        self.assertEqual(statements, [])


@requires_postgres
class CancelInTest(CancelTestCase):
    """``cancel_in`` writes in the caller's transaction and commits nothing itself."""

    async def test_the_entry_is_cancelled_when_the_callers_transaction_commits(self):
        task_id = await self.task_in_state(TaskState.QUEUED)
        await self.queue.enqueue(task_id)

        async with self.database.session() as session, session.begin():
            self.assertIs(await self.queue.cancel_in(session, task_id), True)
            # Not committed: another connection still sees a queued entry.
            self.assertEqual(await self.statuses(task_id), ["queued"])

        self.assertEqual(await self.statuses(task_id), ["cancelled"])

    async def test_the_entry_stays_when_the_callers_transaction_rolls_back(self):
        task_id = await self.task_in_state(TaskState.QUEUED)
        await self.queue.enqueue(task_id)

        async with self.database.session() as session:
            transaction = await session.begin()
            self.assertIs(await self.queue.cancel_in(session, task_id), True)
            await transaction.rollback()

        self.assertEqual(await self.statuses(task_id), ["queued"])

    async def test_the_conditional_form_works_in_the_callers_transaction(self):
        active = await self.task_in_state(TaskState.RUNNING)
        finished = await self.task_in_state(TaskState.CANCELLED)
        await self.queue.enqueue(active)
        await self.queue.enqueue(finished)

        async with self.database.session() as session, session.begin():
            kept = await self.queue.cancel_in(
                session, active, only_if_task_terminal=True
            )
            cancelled = await self.queue.cancel_in(
                session, finished, only_if_task_terminal=True
            )

        self.assertEqual((kept, cancelled), (False, True))
        self.assertEqual(await self.statuses(active), ["queued"])
        self.assertEqual(await self.statuses(finished), ["cancelled"])


@requires_postgres
class RestartRaceTest(CancelTestCase):
    """A Restart and the conditional cancel are serialised by the task row."""

    async def test_a_restart_in_flight_keeps_the_entry_for_the_restarted_task(self):
        # The task was Cancelled; its old entry is still active. A Restart is
        # in flight (uncommitted). The stopper's conditional cancel must not
        # cancel the entry on the strength of the OLD state: it waits for the
        # Restart, sees the task queued again and leaves the entry alone.
        task_id = await self.task_in_state(TaskState.CANCELLED)
        await self.queue.enqueue(task_id)
        hold = HoldOpen()
        restarting = self.spawn(
            TaskService(self.new_database()).execute(
                task_id, TaskCommand.RESTART, actor=self.user, in_transaction=hold
            )
        )
        async with asyncio.timeout(DEADLINE):
            await hold.reached.wait()

        cancelling = self.spawn(
            self.new_queue().cancel(task_id, only_if_task_terminal=True)
        )
        await self.wait_for_lock_waiters(1)
        self.assertFalse(cancelling.done())
        hold.release.set()
        async with asyncio.timeout(DEADLINE):
            await restarting
            cancelled = await cancelling

        self.assertIs(cancelled, False)
        self.assertEqual(await self.task_state(task_id), "queued")
        self.assertEqual(await self.statuses(task_id), ["queued"])

    async def test_a_restart_after_the_cancel_waits_and_the_task_needs_a_new_entry(
        self,
    ):
        task_id = await self.task_in_state(TaskState.CANCELLED)
        await self.queue.enqueue(task_id)

        async with self.database.session() as session, session.begin():
            self.assertTrue(
                await self.queue.cancel_in(session, task_id, only_if_task_terminal=True)
            )
            restarting = self.spawn(
                TaskService(self.new_database()).execute(
                    task_id, TaskCommand.RESTART, actor=self.user
                )
            )
            await self.wait_for_lock_waiters(1)
            self.assertFalse(restarting.done())
            self.assertEqual(await self.task_state(task_id), "cancelled")
        async with asyncio.timeout(DEADLINE):
            await restarting

        # The order is Cancel, then Restart: the dead entry is gone and the
        # restarted task is queued without an entry, ready to be enqueued.
        self.assertEqual(await self.task_state(task_id), "queued")
        self.assertEqual(await self.statuses(task_id), ["cancelled"])
        await self.queue.enqueue(task_id)
        self.assertEqual(await self.statuses(task_id), ["cancelled", "queued"])


if __name__ == "__main__":
    unittest.main()
