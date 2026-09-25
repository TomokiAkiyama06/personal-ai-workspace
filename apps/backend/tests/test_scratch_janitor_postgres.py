"""The scratch janitor deletes expired rows for real (real PostgreSQL).

Rows are seeded and read with SQL (``scratch_support``), the store runs on its
own engine with the injected clock, and the janitor is the code under test.
Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from functools import partial
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.engine import make_url

from paw_backend.app import create_app
from paw_backend.db import Database
from paw_backend.research.scratch import (
    PurgeRun,
    ScratchJanitor,
    ScratchStore,
)

from .fake_postgres import FreezableProxy
from .memory_support import TEST_DATABASE_URL
from .scratch_support import T0, PostgresScratchTestCase, requires_postgres
from .support import make_settings

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
DEADLINE = 60  # generous: only a hang can reach it


async def quiet(database, timeout_seconds) -> None:
    """Replaces a startup diagnostic."""


class Gate:
    """An injected ``sleep``: the first call returns, the next one blocks.

    So a running janitor does exactly one tick, and the test can tell when it is
    finished (``second`` is set once the loop is asleep again).
    """

    def __init__(self) -> None:
        self.calls: list[float] = []
        self.second = asyncio.Event()
        self._forever = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if len(self.calls) >= 2:
            self.second.set()
            await self._forever.wait()


@requires_postgres
class TickTest(PostgresScratchTestCase):
    def new_janitor(
        self, store: ScratchStore | None = None, **options
    ) -> ScratchJanitor:
        options.setdefault("interval_seconds", 3600)
        return ScratchJanitor(store or self.store, **options)

    async def test_an_expired_row_is_deleted_and_every_exempt_row_stays(self):
        expired = [
            self.seed_item(expires_at=T0 - MINUTE),
            self.seed_item(expires_at=T0),  # expires exactly now
            self.seed_item(expires_at=T0 - 30 * HOUR, promotion_state="promoted"),
            self.seed_item(expires_at=T0 - HOUR, promotion_state="rejected"),
        ]
        ended_lease = self.seed_item(expires_at=T0 - HOUR)
        self.seed_lease(ended_lease, T0)  # over exactly now
        expired.append(ended_lease)

        pinned = self.seed_item(expires_at=T0 - HOUR, pinned=True)
        pending = self.seed_pending(expires_at=T0 - HOUR)
        in_use = self.seed_item(expires_at=T0 - HOUR)
        holder = self.seed_lease(in_use, T0 + MINUTE)
        kept_expired = {pinned, pending, in_use}
        fresh = {
            self.seed_item(expires_at=T0 + timedelta(microseconds=1)),
            self.seed_item(expires_at=T0 + 23 * HOUR),
        }
        self.seed_memory()

        run = await self.new_janitor().tick()

        self.assertEqual(run, PurgeRun(purged=5, deferred=3, batches=1, has_more=False))
        self.assertEqual(self.item_ids(), kept_expired | fresh)
        for item_id in expired:
            self.assertFalse(self.exists(item_id))
            self.assertEqual(self.lease_rows(item_id), {})
        self.assertEqual(set(self.lease_rows(in_use)), {holder})  # its lease stays
        # Long-term Memory is another system: never touched.
        self.assertEqual(self.table_count("memories"), 1)

    async def test_the_content_itself_is_gone_from_the_database(self):
        item_id = self.seed_item(
            expires_at=T0 - HOUR,
            content="research content that must not outlive its 24 hours",
            source_metadata={"url": "https://example.test/paper"},
        )
        self.assertIn("research content", self.row(item_id)["content"])

        await self.new_janitor().tick()

        self.assertIsNone(self.row(item_id))
        self.assertEqual(self.table_count("research_scratch_items"), 0)

    async def test_the_ttl_is_enforced_as_the_clock_moves(self):
        item_id = self.seed_item()  # created now: expires in 24 hours
        janitor = self.new_janitor()

        self.clock.advance(hours=23, minutes=59, seconds=59)
        self.assertEqual((await janitor.tick()).purged, 0)
        self.assertTrue(self.exists(item_id))

        self.clock.advance(seconds=1)  # exactly created_at + 24 hours
        self.assertEqual((await janitor.tick()).purged, 1)
        self.assertFalse(self.exists(item_id))

    async def test_an_exempt_row_goes_with_the_first_tick_after_its_exemption_ends(
        self,
    ):
        pinned = self.seed_item(expires_at=T0 - HOUR, pinned=True)
        janitor = self.new_janitor()

        self.assertEqual((await janitor.tick()).purged, 0)
        self.assertTrue(self.exists(pinned))

        self.set_item(pinned, pinned=False)
        self.assertEqual((await janitor.tick()).purged, 1)
        self.assertFalse(self.exists(pinned))

    async def test_a_saved_row_survives_the_ticks_until_it_is_unsaved(self):
        saved = self.seed_item(expires_at=T0 - HOUR, pinned=True, saved=True)
        janitor = self.new_janitor()

        self.assertEqual((await janitor.tick()).purged, 0)
        self.set_item(saved, pinned=False)
        self.assertEqual((await janitor.tick()).purged, 0)
        self.assertTrue(self.exists(saved))

        self.set_item(saved, saved=False)
        self.assertEqual((await janitor.tick()).purged, 1)
        self.assertFalse(self.exists(saved))

    async def test_a_lease_that_ends_lets_the_row_go(self):
        item_id = self.seed_item(expires_at=T0 - HOUR)
        self.seed_lease(item_id, T0 + 5 * MINUTE)
        janitor = self.new_janitor()

        self.assertEqual((await janitor.tick()).purged, 0)
        self.clock.advance(minutes=5)  # the lease ends exactly now
        self.assertEqual((await janitor.tick()).purged, 1)

        self.assertFalse(self.exists(item_id))

    async def test_a_backlog_is_worked_off_in_batches(self):
        expired = {self.seed_item(expires_at=T0 - HOUR) for _ in range(5)}
        pinned = self.seed_item(expires_at=T0 - HOUR, pinned=True)
        janitor = self.new_janitor(batch_size=2)

        run = await janitor.tick()

        # 2 + 2 + 1 rows; the last batch sees that nothing more is due.
        self.assertEqual(run, PurgeRun(purged=5, deferred=1, batches=3, has_more=False))
        self.assertEqual(self.item_ids(), {pinned})
        self.assertTrue(expired.isdisjoint(self.item_ids()))

    async def test_a_tick_stops_at_its_batch_bound_and_the_next_one_continues(self):
        for _ in range(5):
            self.seed_item(expires_at=T0 - HOUR)
        janitor = self.new_janitor(batch_size=2, max_batches=2)

        first = await janitor.tick()
        second = await janitor.tick()

        self.assertEqual(
            first, PurgeRun(purged=4, deferred=0, batches=2, has_more=True)
        )
        self.assertEqual(
            second, PurgeRun(purged=1, deferred=0, batches=1, has_more=False)
        )
        self.assertEqual(self.table_count("research_scratch_items"), 0)

    async def test_two_janitors_at_once_delete_every_row_exactly_once(self):
        # Two Backend processes: each has its own engine (``new_store``).
        for _ in range(60):
            self.seed_item(expires_at=T0 - HOUR)
        keep = self.seed_item(expires_at=T0 - HOUR, pinned=True)
        first = self.new_janitor(self.new_store(), batch_size=7)
        second = self.new_janitor(self.new_store(), batch_size=7)

        runs = await asyncio.gather(first.tick(), second.tick())

        self.assertEqual(sum(run.purged for run in runs), 60)
        self.assertEqual(self.item_ids(), {keep})

    async def test_a_failed_purge_leaves_the_rows_and_the_next_tick_deletes_them(self):
        item_id = self.seed_item(expires_at=T0 - HOUR)
        broken = self.new_store()
        real_purge = broken.purge_expired
        failures = []

        async def flaky(**options):
            if not failures:
                failures.append(True)
                raise ConnectionError("the connection was lost")
            return await real_purge(**options)

        broken.purge_expired = flaky
        janitor = self.new_janitor(broken)

        with self.assertRaises(ConnectionError):
            await janitor.tick()
        self.assertTrue(self.exists(item_id))
        self.assertEqual((await janitor.tick()).purged, 1)
        self.assertFalse(self.exists(item_id))


@requires_postgres
class LoopTest(PostgresScratchTestCase):
    async def test_the_running_loop_deletes_what_is_due_and_stops_when_cancelled(self):
        expired = self.seed_item(expires_at=T0 - HOUR)
        pinned = self.seed_item(expires_at=T0 - HOUR, pinned=True)
        gate = Gate()
        task = asyncio.create_task(
            ScratchJanitor(self.store, interval_seconds=120, sleep=gate).run()
        )

        await asyncio.wait_for(gate.second.wait(), DEADLINE)
        task.cancel()
        done, pending = await asyncio.wait({task}, timeout=DEADLINE)

        self.assertEqual(pending, set())
        self.assertTrue(task.cancelled())
        self.assertEqual(gate.calls, [30.0, 120.0])  # start delay, then the interval
        self.assertEqual(self.item_ids(), {pinned})
        self.assertFalse(self.exists(expired))


@requires_postgres
class ApplicationTest(PostgresScratchTestCase):
    """``create_app`` really purges, on its real clock, with its own database."""

    async def test_the_application_deletes_an_expired_row_in_the_background(self):
        # Created two days ago: expired for the application's own (real) clock.
        created_at = datetime.now(UTC) - timedelta(days=2)
        expired = self.seed_item(created_at=created_at)
        exempt = self.seed_item(created_at=created_at, pinned=True)
        gate = Gate()
        settings = make_settings(
            database_url=TEST_DATABASE_URL, scratch_purge_interval_seconds=60
        )
        app = create_app(settings)
        # The application's janitor, but with a sleep that does not wait; the
        # privilege diagnostics (a warning each here: the test user owns
        # everything) are not what this test is about.
        with (
            patch("paw_backend.app.warn_about_loose_privileges", quiet),
            patch("paw_backend.app.warn_if_tokens_can_be_minted", quiet),
            patch(
                "paw_backend.app.ScratchJanitor", partial(ScratchJanitor, sleep=gate)
            ),
            self.assertLogs(
                "paw_backend.research.scratch.janitor", level="INFO"
            ) as logs,
        ):
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(gate.second.wait(), DEADLINE)
                self.assertFalse(self.exists(expired))
                self.assertTrue(self.exists(exempt))

        self.assertEqual(gate.calls, [30.0, 60.0])
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            ["Scratch purge removed 1 expired item(s) in 1 batch(es)"],
        )


@requires_postgres
class StalledPurgeTest(PostgresScratchTestCase):
    """A real server that stops answering in the middle of a purge transaction.

    A ``FreezableProxy`` sits in front of the test database. The ``purge_probe``
    seam freezes it after the batch was selected (and locked), then runs one more
    statement, which is never answered: the janitor is inside its purge, with
    its rows locked, exactly when the application shuts down.
    """

    async def stalled_purge(self, proxy: FreezableProxy) -> asyncio.Task:
        """A running ``purge_expired`` that is stuck in a frozen statement."""
        url = make_url(TEST_DATABASE_URL).set(host="127.0.0.1", port=proxy.port)
        database = Database(make_settings(database_url=url.render_as_string(False)))
        self.addAsyncCleanup(database.dispose)
        stuck = asyncio.Event()

        async def probe(session, chosen):
            proxy.freeze()
            stuck.set()
            await session.execute(text("SELECT 1"))  # never answered

        store = ScratchStore(database, clock=self.clock, purge_probe=probe)
        purge = self.spawn(store.purge_expired())
        await asyncio.wait_for(stuck.wait(), DEADLINE)
        await asyncio.sleep(0.2)  # inside the statement
        self.assertFalse(purge.done())
        self.database = database
        return purge

    async def test_cancelling_the_purge_stops_it_at_once_without_a_server_cancel(self):
        first = self.seed_item(expires_at=T0 - HOUR)
        second = self.seed_item(expires_at=T0 - 2 * HOUR)
        async with FreezableProxy(*self.upstream()) as proxy:
            purge = await self.stalled_purge(proxy)

            started = time.monotonic()
            purge.cancel()
            done, pending = await asyncio.wait({purge}, timeout=DEADLINE)

            # psycopg's own server-side cancellation would take about ten
            # seconds here: the cancel request goes through the frozen proxy too.
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(pending, set())
            self.assertTrue(purge.cancelled())
            await self.database.dispose()
            self.assertEqual(self.database._probes, set())
            self.assertEqual(self.database._probe_connections, {})
        # Nothing was deleted: the transaction did not commit.
        self.assertEqual(self.item_ids(), {first, second})

    async def test_disposing_the_database_stops_the_purge_too(self):
        self.seed_item(expires_at=T0 - HOUR)
        async with FreezableProxy(*self.upstream()) as proxy:
            purge = await self.stalled_purge(proxy)

            started = time.monotonic()
            await self.database.dispose()
            done, pending = await asyncio.wait({purge}, timeout=DEADLINE)

            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(pending, set())
            # The purge ends with an error of the aborted connection, or, when it
            # was aborted before it had a connection, as cancelled; never a result.
            self.assertTrue(purge.cancelled() or purge.exception() is not None)

    def upstream(self) -> tuple[str, int]:
        url = make_url(TEST_DATABASE_URL)
        return url.host or "127.0.0.1", url.port or 5432
