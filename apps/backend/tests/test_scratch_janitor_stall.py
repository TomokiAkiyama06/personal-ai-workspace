"""A janitor that is inside a stalled purge cannot hold up shutdown (PAW-050).

The lifespan cancels the janitor and waits for it for at most
``PAW_SHUTDOWN_TIMEOUT_SECONDS``. A purge that ran on a pooled connection was
cancelled the ordinary way when PostgreSQL accepted the connection but never
answered: psycopg asked the server to cancel and waited for the answer (up to
about ten seconds, or, with a libpq older than 17, from a thread that
``asyncio.run`` waits for at exit). So the bounded wait only *gave up* on the
task; the task and its connection lived on and the process was held up when the
event loop closed. The purge now runs on a dedicated connection whose socket is
shut down instead (``Database.run_abortable``), so the janitor really ends.

Here the ``HangingPostgres`` fake plays the stalled server: it authenticates and
then answers no query at all, so every statement of the purge (also the first
ones that SQLAlchemy runs on a new connection) stays unanswered.
"""

import asyncio
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from paw_backend.app import create_app
from paw_backend.db import Database
from paw_backend.research.scratch import ScratchJanitor, ScratchStore
from paw_backend.research.scratch import janitor as janitor_module

from .fake_postgres import HangingPostgres
from .support import make_settings, wait_until


def settings_for(server: HangingPostgres, **overrides):
    return make_settings(
        database_url=f"postgresql://paw:pw@127.0.0.1:{server.port}/paw", **overrides
    )


class RecordingJanitor(ScratchJanitor):
    """The real janitor, that remembers the task its ``run`` is executing in."""

    tasks: list[asyncio.Task] = []

    async def run(self) -> None:
        self.tasks.append(asyncio.current_task())
        await super().run()


class LifespanWithAStalledPurgeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        RecordingJanitor.tasks = []
        for patcher in (
            patch("paw_backend.app.ScratchJanitor", RecordingJanitor),
            # The first purge starts at once instead of after 30 seconds.
            patch.object(janitor_module, "FIRST_TICK_DELAY_SECONDS", 0.0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_shutdown_ends_the_janitor_inside_a_stalled_purge_on_time(self):
        async with HangingPostgres() as server:
            settings = settings_for(
                # Long, so that no database timeout can end anything first.
                server,
                database_timeout_seconds=30,
                shutdown_timeout_seconds=2,
            )
            database = Database(settings)
            app = create_app(settings, database=database)
            async with asyncio.timeout(8):  # without the fix: 2 s wait, then a hang
                started = None
                async with app.router.lifespan_context(app):
                    # The audit and the Owner-token diagnostics and the purge:
                    # one connection each, all of them inside their query.
                    self.assertTrue(await wait_until(lambda: server.logins == 3))
                    await asyncio.sleep(0.3)
                    (task,) = RecordingJanitor.tasks
                    self.assertFalse(task.done())
                    started = time.monotonic()
                elapsed = time.monotonic() - started

        # Well inside the shutdown budget (2 s): nothing waited for the server ...
        self.assertLess(elapsed, 1.0)
        # ... and the janitor did end (it was not just given up on), with the
        # transaction it was in and the connection of that transaction.
        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())
        self.assertEqual(database._probes, set())
        self.assertEqual(database._probe_connections, {})

    async def test_cancelling_a_stalled_purge_ends_it_and_its_connection_at_once(self):
        async with HangingPostgres() as server:
            database = Database(
                settings_for(server, database_timeout_seconds=30),
            )
            store = ScratchStore(database)
            purge = asyncio.create_task(store.purge_expired())
            self.assertTrue(await wait_until(lambda: server.logins == 1))
            await asyncio.sleep(0.2)
            self.assertFalse(purge.done())

            started = time.monotonic()
            purge.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await purge
            await database.dispose()

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(database._probes, set())
        self.assertEqual(database._probe_connections, {})


class ProcessShutdownWithAStalledPurgeTest(unittest.TestCase):
    """The whole process exits promptly, not only the lifespan.

    A child process, because a blocked thread or task shows up in
    ``asyncio.run``'s shutdown, which cannot be observed from inside the loop
    (same pattern as ``test_audit_diagnostic_stall.ProcessShutdownTest``).
    """

    def run_child(self, mode: str) -> tuple[float, float]:
        environment = {k: v for k, v in os.environ.items() if not k.startswith("PAW_")}
        started = time.monotonic()
        try:
            result = subprocess.run(
                [sys.executable, "-m", "tests.teardown_child", mode],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            self.fail(f"the process ({mode}) did not exit after shutdown")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("done", result.stdout)
        shutdown = next(
            float(line.split()[1])
            for line in result.stdout.splitlines()
            if line.startswith("shutdown ")
        )
        return shutdown, time.monotonic() - started

    def test_shutdown_is_prompt_with_query_cancellation_via_libpq_17(self):
        shutdown, total = self.run_child("janitor_cancel_safe")
        self.assertLess(shutdown, 1.0)  # without the fix: the whole budget (2 s)
        self.assertLess(total, 8)

    def test_shutdown_is_prompt_when_libpq_cancels_from_a_thread(self):
        shutdown, total = self.run_child("janitor_libpq_fallback")
        self.assertLess(shutdown, 1.0)  # without the fix: never returns
        self.assertLess(total, 8)


if __name__ == "__main__":
    unittest.main()
