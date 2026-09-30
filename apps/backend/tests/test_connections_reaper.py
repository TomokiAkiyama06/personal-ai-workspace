"""Settling connection calls that a crashed process left ``in_flight`` (PAW-034).

Decision 0016: a usage row that is ``in_flight`` for much longer than the longest
call belongs to no live call (its process died between the admission and the
settlement) and is settled as ``failed`` / ``internal_error``. What is unknown
stays unknown (the tokens); the duration is capped at the longest a call may run;
a live call, and a row that was settled, is never touched; every reaped row is
audited.
"""

import asyncio
import unittest
import uuid
from datetime import timedelta

from paw_backend.connections.errors import InvalidConnectionInputError
from paw_backend.connections.limits import (
    ABANDONED_CALL_AGE_SECONDS,
    MAX_CALL_TIMEOUT_SECONDS,
    MAX_REAPED_PER_CYCLE,
)
from paw_backend.connections.store import ConnectionStore
from paw_backend.db import Database
from paw_backend.orchestrator.connection_reaper import (
    ACTION_ABANDON,
    REASON_ABANDONED,
    AbandonedCallReaper,
)
from paw_backend.orchestrator.errors import InvalidOrchestratorArgumentError
from paw_backend.orchestrator.limits import DEFAULT_REAP_INTERVAL_SECONDS

from .connections_support import (
    T0,
    FailingSink,
    FakeClock,
    PostgresConnectionTestCase,
    requires_postgres,
)
from .orchestrator_support import ManualClock
from .support import make_settings
from .task_support import TEST_DATABASE_URL

AGE = timedelta(seconds=ABANDONED_CALL_AGE_SECONDS)


@requires_postgres
class ReapTest(PostgresConnectionTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.clock = FakeClock(T0 + AGE + timedelta(hours=5))
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        self.store = ConnectionStore(
            database, clock=self.clock, allow_explicit_clock=True
        )
        self.task = self.seed_task(self.user)

    def row(self, usage_id: uuid.UUID) -> dict:
        (row,) = self.rows("SELECT * FROM connection_usage WHERE id = :i", i=usage_id)
        return row

    async def test_an_abandoned_call_is_settled_as_failed_with_what_is_known(self):
        old = self.seed_usage(self.user, self.task, status="in_flight", started_at=T0)

        (reaped,) = await self.store.reap_abandoned()

        self.assertEqual(reaped.id, old)
        row = self.row(old)
        self.assertEqual(
            (row["status"], row["failure_code"]), ("failed", "internal_error")
        )
        # Unknown stays unknown; the end is when it was found; the duration is at
        # most what a call can run.
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (None, None))
        self.assertEqual(row["finished_at"], self.clock.now)
        self.assertEqual(row["duration_ms"], int(MAX_CALL_TIMEOUT_SECONDS * 1000))

    async def test_a_call_that_may_still_run_and_a_settled_call_are_left_alone(self):
        young = self.seed_usage(
            self.user,
            self.task,
            status="in_flight",
            started_at=self.clock.now - AGE + timedelta(minutes=1),
        )
        settled = self.seed_usage(self.user, self.task, started_at=T0, tokens=7)
        before = {i: self.row(i) for i in (young, settled)}

        self.assertEqual(await self.store.reap_abandoned(), ())

        self.assertEqual({i: self.row(i) for i in (young, settled)}, before)

    async def test_a_late_settlement_after_the_reaper_changes_nothing(self):
        from paw_backend.connections.domain import UsageStatus

        usage = self.seed_usage(self.user, self.task, status="in_flight", started_at=T0)
        await self.store.reap_abandoned()
        reaped = self.row(usage)

        late = await self.store.settle(usage, UsageStatus.SUCCEEDED, None, 10, 20)

        self.assertIsNone(late)
        self.assertEqual(self.row(usage), reaped)
        self.assertEqual(await self.store.reap_abandoned(), ())  # once only

    async def test_the_oldest_are_taken_first_and_a_batch_is_bounded(self):
        rows = [
            self.seed_usage(
                self.user,
                self.task,
                status="in_flight",
                started_at=T0 + timedelta(minutes=i),
            )
            for i in range(3)
        ]
        first = await self.store.reap_abandoned(limit=2)
        self.assertEqual([r.id for r in first], rows[:2])
        self.assertEqual(self.row(rows[2])["status"], "in_flight")
        (last,) = await self.store.reap_abandoned(limit=2)
        self.assertEqual(last.id, rows[2])
        for bad in (0, -1, MAX_REAPED_PER_CYCLE + 1, True, 1.5, "2", None):
            with self.subTest(bad=bad), self.assertRaises(InvalidConnectionInputError):
                await self.store.reap_abandoned(limit=bad)

    async def test_concurrent_reapers_settle_each_row_once(self):
        rows = [
            self.seed_usage(self.user, self.task, status="in_flight", started_at=T0)
            for _ in range(20)
        ]
        other_db = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(other_db.dispose)
        other = ConnectionStore(other_db, clock=self.clock, allow_explicit_clock=True)

        first, second = await asyncio.gather(
            self.store.reap_abandoned(), other.reap_abandoned()
        )

        ids = [r.id for r in first] + [r.id for r in second]
        self.assertEqual(sorted(ids), sorted(rows))
        self.assertEqual(len(ids), len(set(ids)))

    async def test_a_cycle_audits_every_reaped_row(self):
        usage = self.seed_usage(self.user, self.task, status="in_flight", started_at=T0)
        reaper = AbandonedCallReaper(self.store, self.sink, clock=ManualClock())

        self.assertEqual(await reaper.run_cycle(), 1)

        (event,) = [e for e in self.sink.events if e.action == ACTION_ABANDON]
        self.assertEqual(
            (event.actor_id, event.actor_role, event.decision, event.reason),
            (None, "system", "allow", REASON_ABANDONED),
        )
        self.assertEqual(
            (event.resource_kind, event.resource_id, event.project_id),
            ("connection_usage", usage, self.project_of(self.task)),
        )
        self.assertEqual(await reaper.run_cycle(), 0)
        # What System Health reads (PAW-066): two cycles, one row settled.
        stats = reaper.stats
        self.assertEqual(
            (stats.cycles, stats.last_reaped, stats.total_reaped), (2, 0, 1)
        )
        self.assertIsNotNone(stats.last_success_at)

    async def test_a_failed_audit_does_not_undo_the_settlement(self):
        usage = self.seed_usage(self.user, self.task, status="in_flight", started_at=T0)
        reaper = AbandonedCallReaper(self.store, FailingSink(), clock=ManualClock())

        with self.assertLogs(
            "paw_backend.orchestrator.connection_reaper", "ERROR"
        ) as logs:
            self.assertEqual(await reaper.run_cycle(), 1)

        self.assertEqual(self.row(usage)["status"], "failed")
        self.assertNotIn("audit store down", "\n".join(logs.output))

    async def test_the_loop_waits_cycles_and_stops(self):
        usage = self.seed_usage(self.user, self.task, status="in_flight", started_at=T0)
        clock = ManualClock()
        reaper = AbandonedCallReaper(self.store, self.sink, clock=clock)
        loop = asyncio.create_task(reaper.run())
        await self.wait_for(lambda: clock.sleeping == 1, "the first wait")
        self.assertEqual(self.row(usage)["status"], "in_flight")

        await clock.advance(DEFAULT_REAP_INTERVAL_SECONDS)
        await self.wait_for(
            lambda: any(e.action == ACTION_ABANDON for e in self.sink.events),
            "the first cycle",
        )
        reaper.stop()
        await asyncio.wait_for(loop, 10)
        self.assertEqual(self.row(usage)["status"], "failed")


class ScriptedStore(ConnectionStore):
    """``reap_abandoned`` answers with the next prepared outcome."""

    def __init__(self, *outcomes) -> None:
        super().__init__(Database(make_settings()))
        self.outcomes = list(outcomes)

    async def reap_abandoned(self, *, limit=MAX_REAPED_PER_CYCLE):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class StatsTest(unittest.IsolatedAsyncioTestCase):
    """What the cycles did, for System Health (PAW-066, issue #52's note)."""

    async def test_successes_and_failures_are_counted(self):
        store = ScriptedStore((), OSError("password=hunter2"), OSError("x"), ())
        reaper = AbandonedCallReaper(store, FailingSink(), clock=ManualClock())
        self.assertEqual(reaper.stats.cycles, 0)
        self.assertIsNone(reaper.stats.last_cycle_at)
        await reaper.run_cycle()
        for _ in range(2):
            with self.assertRaises(OSError):
                await reaper.run_cycle()
        stats = reaper.stats
        self.assertEqual(
            (stats.cycles, stats.consecutive_failures, stats.total_failures),
            (3, 2, 2),
        )
        # The type, as the logs name it; never the message.
        self.assertNotIn("hunter2", repr(stats))
        self.assertIsNotNone(stats.last_error)
        self.assertLessEqual(stats.last_success_at, stats.last_cycle_at)
        await reaper.run_cycle()
        stats = reaper.stats
        self.assertEqual((stats.consecutive_failures, stats.total_failures), (0, 2))
        self.assertEqual(stats.last_success_at, stats.last_cycle_at)


class ArgumentTest(unittest.TestCase):
    def test_the_reaper_refuses_what_it_cannot_use(self):
        store = ConnectionStore(Database(make_settings()))
        with self.assertRaises(TypeError):
            AbandonedCallReaper(object(), FailingSink())
        with self.assertRaises(TypeError):
            AbandonedCallReaper(store, object())
        for bad in (0, 59, 86_401, True, "600", float("nan")):
            with (
                self.subTest(bad=bad),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                AbandonedCallReaper(store, FailingSink(), interval_seconds=bad)

    def test_a_live_call_is_never_old_enough(self):
        self.assertGreater(ABANDONED_CALL_AGE_SECONDS, MAX_CALL_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
