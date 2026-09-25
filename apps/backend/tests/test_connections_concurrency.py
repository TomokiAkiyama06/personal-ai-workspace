"""The quota under concurrency, on a real PostgreSQL, and the ordering of the locks.

Several services (each with its own engine, as separate backend processes would
have) call at once. The tests order two operations by what PostgreSQL reports
(a backend blocked on a lock), never by sleeping, and use generous deadlines.
"""

import asyncio
import unittest
import uuid

from paw_backend.connections import (
    AdapterResult,
    ConnectionBusyError,
    ConnectionExistsError,
    ConnectionKind,
    ConnectionUnavailableError,
    FailureCode,
    QuotaExceededError,
    RefusalReason,
    TaskNotUsableError,
    UsageStatus,
)

from .connections_fakes import handle
from .connections_support import PostgresConnectionTestCase, requires_postgres

CODEX, CLAUDE = ConnectionKind.CODEX, ConnectionKind.CLAUDE


class ConcurrentCase(PostgresConnectionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.seed_connection(CODEX)
        # Four "processes".
        self.services = [self.new_service() for _ in range(4)]

    async def slow_answer(self, secret, request):
        self.codex.requests.append(request)
        await asyncio.sleep(0.005)  # lets the admissions of the other calls overlap
        return AdapterResult("an answer", 1, 1)

    def prepare_tasks(self, count: int, user=None) -> list[tuple[uuid.UUID, uuid.UUID]]:
        user = user or self.user
        tasks = [self.seed_task(user) for _ in range(count)]
        return [(task, self.project_of(task)) for task in tasks]

    async def execute(self, index: int, task, project, user=None, kind=CODEX):
        user = user or self.user
        service = self.services[index % len(self.services)]
        return await service.execute(
            self.principal(user),
            self.context(task, user, project),
            kind,
            self.request(),
        )

    async def gather(self, jobs):
        """Run the jobs at once; ``(successes, refusals)`` by exception type."""
        results = await asyncio.gather(*jobs, return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        return [r for r in results if not isinstance(r, BaseException)], errors


@requires_postgres
class QuotaIsExactUnderConcurrencyTest(ConcurrentCase):
    async def test_concurrent_new_tasks_never_exceed_the_requests_limit(self):
        self.codex.run = self.slow_answer
        self.seed_quota(self.user, 5)
        tasks = self.prepare_tasks(24)

        done, errors = await self.gather(
            [self.execute(i, task, project) for i, (task, project) in enumerate(tasks)]
        )

        self.assertEqual(len(done), 5)
        self.assertEqual(len(errors), 19)
        for error in errors:
            self.assertIsInstance(error, QuotaExceededError)
            self.assertEqual(
                (error.metric.value, error.period.value), ("requests", "day")
            )
        rows = self.usage_rows()
        self.assertEqual(len(rows), 5)  # exactly the limit, never one more
        self.assertEqual({r["status"] for r in rows}, {"succeeded"})
        self.assertEqual(len({r["task_id"] for r in rows}), 5)
        refusals = [e for e in self.sink.events if e.action == "connection.use"]
        self.assertEqual(len(refusals), 19)
        self.assertEqual({e.reason for e in refusals}, {"quota_exceeded"})
        self.assertEqual(len(self.codex.requests), 5)

    async def test_repeated_rounds_never_exceed_the_limit(self):
        self.codex.run = self.slow_answer
        self.seed_quota(self.user, 3)
        for round_number in range(3):
            done, errors = await self.gather(
                [
                    self.execute(i, task, project)
                    for i, (task, project) in enumerate(self.prepare_tasks(8))
                ]
            )
            self.assertEqual(len(done), 3 if round_number == 0 else 0, round_number)
            self.assertEqual(len(self.usage_rows()), 3)

    async def test_concurrent_new_tasks_never_exceed_the_tasks_limit(self):
        self.seed_quota(self.user, 3, metric="tasks")
        done, errors = await self.gather(
            [
                self.execute(i, task, project)
                for i, (task, project) in enumerate(self.prepare_tasks(12))
            ]
        )
        self.assertEqual((len(done), len(errors)), (3, 9))
        self.assertEqual(len({r["task_id"] for r in self.usage_rows()}), 3)

    async def test_the_tightest_of_several_quotas_holds(self):
        self.seed_quota(self.user, 6, period="day")
        self.seed_quota(self.user, 4, period="rolling_5h")
        self.seed_quota(self.user, 100, period="month")
        done, errors = await self.gather(
            [
                self.execute(i, task, project)
                for i, (task, project) in enumerate(self.prepare_tasks(16))
            ]
        )
        self.assertEqual((len(done), len(errors)), (4, 12))
        self.assertEqual({e.period.value for e in errors}, {"rolling_5h"})

    async def test_unlimited_admits_everyone_at_once(self):
        self.seed_quota(self.user, None)
        done, errors = await self.gather(
            [
                self.execute(i, task, project)
                for i, (task, project) in enumerate(self.prepare_tasks(16))
            ]
        )
        self.assertEqual((len(done), len(errors)), (16, 0))

    async def test_running_tasks_are_not_limited_and_only_new_ones_are(self):
        self.seed_quota(self.user, 3)
        started = self.prepare_tasks(3)
        for i, (task, project) in enumerate(started):
            await self.execute(i, task, project)  # the quota is used up
        extra_new = self.prepare_tasks(6)
        jobs = [
            self.execute(i, task, project)
            for i, (task, project) in enumerate(started * 4 + extra_new)
        ]
        done, errors = await self.gather(jobs)
        self.assertEqual(len(done), 12)  # every call of a task that had started
        self.assertEqual(len(errors), 6)  # every call of a new task
        self.assertTrue(all(isinstance(e, QuotaExceededError) for e in errors))
        self.assertEqual(len(self.usage_rows()), 15)

    async def test_the_users_are_counted_separately(self):
        other = self.seed_user()
        self.seed_quota(self.user, 2)
        self.seed_quota(other, 3)
        mine, theirs = self.prepare_tasks(6), self.prepare_tasks(6, other)
        jobs = [
            self.execute(i, task, project) for i, (task, project) in enumerate(mine)
        ] + [
            self.execute(i, task, project, user=other)
            for i, (task, project) in enumerate(theirs)
        ]
        await self.gather(jobs)
        self.assertEqual(len(self.usage_rows(self.user)), 2)
        self.assertEqual(len(self.usage_rows(other)), 3)


@requires_postgres
class InFlightCallsTest(ConcurrentCase):
    async def test_a_call_in_flight_finishes_when_the_quota_is_reached_meanwhile(self):
        self.seed_quota(self.user, 1)
        gate = asyncio.Event()
        self.codex.gate = gate
        (first, first_project), (second, second_project) = self.prepare_tasks(2)
        running = self.spawn(self.execute(0, first, first_project))
        await asyncio.wait_for(self.codex.started.wait(), 30)

        # The quota is used by the call in flight: a new task is refused ...
        with self.assertRaises(QuotaExceededError):
            await self.execute(1, second, second_project)
        # ... an Admin sets it to zero ...
        await self.services[0].set_quota(
            self.principal(self.admin), self.user, CODEX, "requests", "day", 0
        )
        # ... and the call that had started is neither cancelled nor cut short.
        gate.set()
        result = await running

        self.assertEqual(result.text, "an answer")
        (row,) = self.usage_rows()
        self.assertEqual((row["status"], row["failure_code"]), ("succeeded", None))
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (10, 5))

    async def test_calls_in_flight_count_as_requests(self):
        self.seed_quota(self.user, 2)
        self.codex.gate = asyncio.Event()
        tasks = self.prepare_tasks(3)
        first = self.spawn(self.execute(0, *tasks[0]))
        second = self.spawn(self.execute(1, *tasks[1]))
        await self.wait_for(
            lambda: len(self.usage_rows()) == 2, "both calls to be admitted"
        )
        with self.assertRaises(QuotaExceededError):
            await self.execute(2, *tasks[2])
        self.codex.gate.set()
        await asyncio.gather(first, second)
        self.assertEqual([r["status"] for r in self.usage_rows()], ["succeeded"] * 2)

    async def test_a_call_that_settles_after_the_window_ended_keeps_its_start(self):
        # The row belongs to the window it started in, whenever it settles.
        self.seed_quota(self.user, 5)
        gate = asyncio.Event()
        self.codex.gate = gate
        ((task, project),) = self.prepare_tasks(1)
        running = self.spawn(self.execute(0, task, project))
        await asyncio.wait_for(self.codex.started.wait(), 30)
        before = self.usage_rows()[0]["started_at"]
        gate.set()
        await running
        self.assertEqual(self.usage_rows()[0]["started_at"], before)


@requires_postgres
class LockOrderingTest(ConcurrentCase):
    """An operation and an admission are ordered by the row locks, never crossed."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.seed_quota(self.user, 10)
        ((self.task, self.project),) = self.prepare_tasks(1)

    async def test_a_disable_that_is_in_progress_decides_the_admission_after_it(self):
        _, transaction = self.hold_row_lock(
            "UPDATE shared_connections SET enabled = false WHERE kind = 'codex'"
        )
        call = self.spawn(self.execute(0, self.task, self.project))
        await self.wait_for_lock_waiters(1)
        self.assertFalse(call.done())  # it waits: it cannot see a half-done disable

        transaction.commit()

        with self.assertRaises(ConnectionUnavailableError):
            await call
        self.assertEqual(self.usage_rows(), [])
        self.assertEqual(self.codex.requests, [])

    async def test_a_disable_waits_for_the_admissions_that_are_in_flight(self):
        # An admission holds the connection's row FOR SHARE until it commits.
        _, transaction = self.hold_row_lock(
            "SELECT id FROM shared_connections WHERE kind = 'codex' FOR SHARE"
        )
        disable = self.spawn(
            self.services[0].disable(self.principal(self.admin), CODEX)
        )
        await self.wait_for_lock_waiters(1)
        self.assertFalse(disable.done())
        self.assertTrue(self.connection_row(CODEX)["enabled"])

        transaction.commit()

        info = await disable
        self.assertFalse(info.enabled)

    async def test_a_credential_replacement_waits_for_the_admissions_too(self):
        _, transaction = self.hold_row_lock(
            "SELECT id FROM shared_connections WHERE kind = 'codex' FOR SHARE"
        )
        replace = self.spawn(
            self.services[0].replace_credential(
                self.principal(self.admin), CODEX, handle(2)
            )
        )
        await self.wait_for_lock_waiters(1)
        self.assertEqual(self.connection_row(CODEX)["secret_handle"], handle(1))
        transaction.commit()
        await replace
        self.assertEqual(self.connection_row(CODEX)["secret_handle"], handle(2))

    async def test_the_end_of_the_task_and_an_admission_are_ordered(self):
        _, transaction = self.hold_row_lock(
            "UPDATE tasks SET state = 'completed' WHERE id = :t", t=self.task
        )
        call = self.spawn(self.execute(0, self.task, self.project))
        await self.wait_for_lock_waiters(1)
        self.assertFalse(call.done())

        transaction.commit()

        with self.assertRaises(TaskNotUsableError) as caught:
            await call
        self.assertEqual(caught.exception.reason, RefusalReason.TASK_ENDED)
        self.assertEqual(self.usage_rows(), [])

    async def test_a_retry_that_starts_a_new_run_is_seen_by_the_waiting_admission(self):
        _, transaction = self.hold_row_lock(
            "UPDATE tasks SET retry_count = retry_count + 1 WHERE id = :t",
            t=self.task,
        )
        call = self.spawn(self.execute(0, self.task, self.project))
        await self.wait_for_lock_waiters(1)
        transaction.commit()
        with self.assertRaises(TaskNotUsableError) as caught:
            await call
        self.assertEqual(caught.exception.reason, RefusalReason.TASK_SUPERSEDED)

    async def test_a_quota_change_in_progress_decides_the_admission_after_it(self):
        _, transaction = self.hold_row_lock(
            "UPDATE connection_quotas SET limit_value = 0 WHERE user_id = :u",
            u=self.user,
        )
        call = self.spawn(self.execute(0, self.task, self.project))
        await self.wait_for_lock_waiters(1)
        self.assertFalse(call.done())

        transaction.commit()

        with self.assertRaises(QuotaExceededError):
            await call
        self.assertEqual(self.usage_rows(), [])

    async def test_another_user_does_not_wait_for_a_locked_quota(self):
        other = self.seed_user()
        self.seed_quota(other, 10)
        ((other_task, other_project),) = self.prepare_tasks(1, other)
        _, transaction = self.hold_row_lock(
            "SELECT 1 FROM connection_quotas WHERE user_id = :u FOR UPDATE",
            u=self.user,
        )
        mine = self.spawn(self.execute(0, self.task, self.project))
        await self.wait_for_lock_waiters(1)

        # The other user's admission goes through while mine is blocked.
        result = await asyncio.wait_for(
            self.execute(1, other_task, other_project, user=other), 30
        )
        self.assertEqual(result.text, "an answer")
        self.assertFalse(mine.done())

        transaction.commit()
        await mine
        self.assertEqual(len(self.usage_rows()), 2)

    async def test_the_other_kind_does_not_wait_for_a_locked_quota_either(self):
        self.seed_connection(CLAUDE, secret_handle=handle(2))
        self.seed_quota(self.user, 10, kind=CLAUDE)
        ((claude_task, claude_project),) = self.prepare_tasks(1)
        _, transaction = self.hold_row_lock(
            "SELECT 1 FROM connection_quotas WHERE user_id = :u AND kind = 'codex'"
            " FOR UPDATE",
            u=self.user,
        )
        blocked = self.spawn(self.execute(0, self.task, self.project))
        await self.wait_for_lock_waiters(1)
        result = await asyncio.wait_for(
            self.execute(1, claude_task, claude_project, kind=CLAUDE), 30
        )
        self.assertEqual(result.text, "an answer")
        transaction.commit()
        await blocked

    async def test_a_caller_that_is_cancelled_while_waiting_leaves_nothing_behind(self):
        _, transaction = self.hold_row_lock(
            "SELECT 1 FROM connection_quotas WHERE user_id = :u FOR UPDATE", u=self.user
        )
        # A short database limit: the server gives up on the abandoned transaction
        # shortly after the caller's own deadline (transact_abortable).
        service = self.new_service(database_timeout_seconds=1)
        call = asyncio.ensure_future(
            service.execute(
                self.principal(self.user),
                self.context(self.task, self.user, self.project),
                CODEX,
                self.request(),
            )
        )
        await self.wait_for_lock_waiters(1)

        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call

        # The waiting backend is gone (its socket was shut down), no usage row
        # was written, and the adapter was never reached.
        await self.wait_for(
            lambda: (
                self.scalar(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname ="
                    " current_database() AND wait_event_type = 'Lock'"
                )
                == 0
            ),
            "the waiting backend to end",
        )
        transaction.commit()
        self.assertEqual(self.usage_rows(), [])
        self.assertEqual(self.codex.requests, [])
        # ... and the quota is usable again.
        await self.execute(1, self.task, self.project)
        self.assertEqual(len(self.usage_rows()), 1)


@requires_postgres
class BusyDatabaseTest(ConcurrentCase):
    async def test_a_lock_held_too_long_is_a_busy_error_and_nothing_is_written(self):
        self.seed_quota(self.user, 10)
        ((task, project),) = self.prepare_tasks(1)
        service = self.new_service(database_timeout_seconds=1)
        _, transaction = self.hold_row_lock(
            "SELECT 1 FROM connection_quotas WHERE user_id = :u FOR UPDATE", u=self.user
        )

        with self.assertRaises(ConnectionBusyError) as caught:
            await service.execute(
                self.principal(self.user),
                self.context(task, self.user, project),
                CODEX,
                self.request(),
            )

        self.assertEqual(str(caught.exception), "The database did not answer in time")
        self.assertEqual(self.usage_rows(), [])
        self.assertEqual(self.codex.requests, [])
        transaction.commit()
        await service.execute(  # once the lock is gone the same call works
            self.principal(self.user),
            self.context(task, self.user, project),
            CODEX,
            self.request(),
        )
        self.assertEqual(len(self.usage_rows()), 1)

    async def test_a_locked_row_makes_a_quota_change_busy_not_stuck(self):
        self.seed_quota(self.user, 10)
        service = self.new_service(database_timeout_seconds=1)
        _, transaction = self.hold_row_lock(
            "SELECT 1 FROM connection_quotas WHERE user_id = :u FOR UPDATE", u=self.user
        )
        with self.assertRaises(ConnectionBusyError):
            await service.set_quota(
                self.principal(self.admin), self.user, CODEX, "requests", "day", 3
            )
        transaction.commit()
        # The abandoned change was never committed, even after the lock was gone.
        self.assertEqual(self.scalar("SELECT limit_value FROM connection_quotas"), 10)

    async def test_a_locked_connection_row_makes_a_disable_busy(self):
        service = self.new_service(database_timeout_seconds=1)
        _, transaction = self.hold_row_lock(
            "SELECT 1 FROM shared_connections FOR UPDATE"
        )
        with self.assertRaises(ConnectionBusyError):
            await service.disable(self.principal(self.admin), CODEX)
        transaction.commit()
        self.assertTrue(self.connection_row(CODEX)["enabled"])


@requires_postgres
class SettlementRaceTest(ConcurrentCase):
    async def test_of_many_concurrent_settlements_of_one_call_exactly_one_wins(self):
        ((task, _),) = self.prepare_tasks(1)
        usage_id = self.seed_usage(self.user, task, status="in_flight")
        store = self.services[0]._store
        outcomes = [
            (UsageStatus.SUCCEEDED, None, 1, 1),
            (UsageStatus.FAILED, FailureCode.RATE_LIMITED, None, None),
            (UsageStatus.CANCELLED, None, None, None),
            (UsageStatus.SUCCEEDED, None, 2, 2),
        ] * 3

        results = await asyncio.gather(
            *[store.settle(usage_id, *outcome) for outcome in outcomes]
        )

        winners = [r for r in results if r is not None]
        self.assertEqual(len(winners), 1)
        (row,) = self.usage_rows()
        self.assertEqual(row["status"], winners[0].status.value)
        self.assertEqual(row["input_tokens"], winners[0].input_tokens)
        self.assertEqual(row["output_tokens"], winners[0].output_tokens)

    async def test_a_settled_call_never_changes_again(self):
        ((task, _),) = self.prepare_tasks(1)
        usage_id = self.seed_usage(self.user, task, status="in_flight")
        store = self.services[0]._store
        first = await store.settle(usage_id, UsageStatus.SUCCEEDED, None, 7, 3)
        again = await store.settle(
            usage_id, UsageStatus.FAILED, FailureCode.INTERNAL_ERROR, None, None
        )
        self.assertIsNotNone(first)
        self.assertIsNone(again)
        (row,) = self.usage_rows()
        self.assertEqual(
            (
                row["status"],
                row["input_tokens"],
                row["output_tokens"],
                row["failure_code"],
            ),
            ("succeeded", 7, 3, None),
        )

    async def test_settling_a_call_that_does_not_exist_changes_nothing(self):
        store = self.services[0]._store
        self.assertIsNone(
            await store.settle(uuid.uuid4(), UsageStatus.SUCCEEDED, None, 1, 1)
        )


@requires_postgres
class ConnectRaceTest(ConcurrentCase):
    async def test_of_concurrent_connects_exactly_one_creates_the_connection(self):
        self.clear_connections()
        results = await asyncio.gather(
            *[
                self.services[i % 4].connect(
                    self.principal(self.admin), CLAUDE, handle(10 + i)
                )
                for i in range(8)
            ],
            return_exceptions=True,
        )
        created = [r for r in results if not isinstance(r, BaseException)]
        refused = [r for r in results if isinstance(r, ConnectionExistsError)]
        self.assertEqual((len(created), len(refused)), (1, 7))
        self.assertEqual(
            self.connection_row(CLAUDE)["secret_handle"],
            self.scalar("SELECT secret_handle FROM shared_connections"),
        )

    async def test_concurrent_quota_changes_leave_one_row(self):
        await asyncio.gather(
            *[
                self.services[i % 4].set_quota(
                    self.principal(self.admin), self.user, CODEX, "requests", "day", i
                )
                for i in range(12)
            ]
        )
        (row,) = self.rows("SELECT limit_value FROM connection_quotas")
        self.assertIn(row["limit_value"], range(12))


if __name__ == "__main__":
    unittest.main()
