"""The orchestrator's workers in the application (Decision 0086, Proposed).

The lifespan starts ``PAW_ORCHESTRATOR_WORKERS`` workers (``Orchestrator.serve``)
only when the application composed an orchestrator, stops them through its stop
event at shutdown and cancels the ones still running after the shutdown
timeout. The orchestrator's ``serve`` is replaced by a recorder here (its own
behaviour is ``test_orchestrator_*``'s).
"""

import asyncio
import unittest

from paw_backend.app import create_app, worker_ids
from paw_backend.orchestrator import Orchestrator, OrchestratorConfig
from paw_backend.orchestrator.validation import check_worker_id

from .orchestrator_support import FakeRuntime
from .test_compute_app import QUIET
from .test_scratch_janitor_lifespan import configured


class WorkerIdTest(unittest.TestCase):
    def test_ids_are_valid_and_distinct(self):
        ids = worker_ids(3)
        self.assertEqual(len(set(ids)), 3)
        for value in ids:
            check_worker_id(value)
            self.assertLessEqual(len(value), 100)
        self.assertEqual(worker_ids(0), [])


class LifespanTest(unittest.IsolatedAsyncioTestCase):
    def app(self, *, runtimes=True, **settings):
        values = dict(QUIET, **settings)
        config, database = configured(**values)
        options = {}
        if runtimes:
            options = dict(
                agent_runtimes={"local": FakeRuntime()},
                orchestrator_config=OrchestratorConfig.uniform(("local",)),
            )
        return create_app(config, database=database, **options)

    def record(self, *, stubborn=False, deaf=None):
        served = []

        async def serve(orchestrator, worker_id, stop, *, idle_seconds=1.0):
            served.append(worker_id)
            if deaf is not None:
                # Ignores its cancellation (a runtime that swallows it) until
                # the test lets it go.
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    await deaf.wait()
                return
            if stubborn:
                await asyncio.sleep(3600)
            await stop.wait()
            served.append(f"stopped {worker_id}")

        original = Orchestrator.serve
        Orchestrator.serve = serve
        self.addCleanup(setattr, Orchestrator, "serve", original)
        return served

    async def test_workers_run_while_the_application_does(self):
        served = self.record()
        app = self.app(orchestrator_workers=2)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)
            self.assertEqual(served, worker_ids(2))
        self.assertEqual(served[2:], [f"stopped {w}" for w in worker_ids(2)])

    async def test_none_without_an_orchestrator_or_when_turned_off(self):
        served = self.record()
        for app in (self.app(runtimes=False), self.app(orchestrator_workers=0)):
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0.05)
        self.assertEqual(served, [])

    async def test_a_worker_that_does_not_stop_is_cancelled(self):
        served = self.record(stubborn=True)
        app = self.app(shutdown_timeout_seconds=1)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)
        self.assertEqual(served, worker_ids(1))

    async def test_a_worker_that_ignores_its_cancellation_is_given_up(self):
        release = asyncio.Event()
        self.addCleanup(release.set)
        served = self.record(deaf=release)
        app = self.app(shutdown_timeout_seconds=1)
        started = asyncio.get_running_loop().time()
        with self.assertLogs("paw_backend.app", "WARNING"):
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0.05)
        self.assertLess(asyncio.get_running_loop().time() - started, 10)
        self.assertEqual(served, worker_ids(1))


if __name__ == "__main__":
    unittest.main()
