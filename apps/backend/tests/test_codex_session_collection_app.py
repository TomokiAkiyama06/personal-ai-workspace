"""The application runs the collection of Codex sessions (issue #280,
Decision 0096, 1 D and 7).

``PAW_CODEX_COLLECT_INTERVAL_SECONDS`` (0 off, default 60) and the lifespan:
with a database and a secret store the collection loop runs on the HTTP
layer's connection service (every process: one wins each slot), is stopped
at shutdown, and System Health reads the egress units left over through the
same session units; without a secret store or when switched off, neither.
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paw_backend.agents.codex_collect import SystemdSessionUnits
from paw_backend.app import create_app
from paw_backend.connections import FileSecretStore
from paw_backend.health.domain import Component

from .support import make_settings, paw_environment, wait_until
from .test_orchestrator_project_sweep_app import RecordingLoop
from .test_scratch_janitor_lifespan import configured

KEY = bytes(range(32))


class RecordingCollection(RecordingLoop):
    instances: list["RecordingCollection"] = []


class SettingTest(unittest.TestCase):
    def test_the_default_and_the_environment(self):
        settings = make_settings()
        self.assertEqual(settings.codex_collect_interval_seconds, 60)
        self.assertEqual(settings.cli_session_dir, Path("/run/paw-cli"))
        with paw_environment(PAW_CODEX_COLLECT_INTERVAL_SECONDS="0"):
            from paw_backend.config import Settings

            self.assertEqual(Settings().codex_collect_interval_seconds, 0)


class LifespanTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        RecordingCollection.instances = []
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.chmod(self.root, 0o700)
        self.store = FileSecretStore(self.root, KEY)
        patcher = patch("paw_backend.app.build_codex_collection", RecordingCollection)
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, *, store=True, **overrides):
        settings, database = configured(**overrides)
        with patch(
            "paw_backend.app.build_secret_store",
            lambda settings: self.store if store else None,
        ):
            return create_app(settings, database=database)

    async def test_it_runs_with_a_database_and_a_secret_store(self):
        app = self.app()
        units = app.state.codex_session_units
        self.assertIsInstance(units, SystemdSessionUnits)
        health = app.state.system_health
        (connections,) = [
            s for s in health.monitor.sources if s.component is Component.CONNECTIONS
        ]
        self.assertIs(connections._egress, units)
        async with app.router.lifespan_context(app):
            (collection,) = RecordingCollection.instances
            self.assertTrue(await wait_until(collection.started.is_set))
            self.assertIs(collection.database, app.state.connections)
            self.assertEqual(
                collection.options,
                {
                    "session_base": Path("/run/paw-cli"),
                    "interval_seconds": 60,
                    "units": units,
                },
            )
        self.assertTrue(collection.stopped)
        self.assertTrue(collection.cancelled)

    async def test_it_does_not_run_without_a_store_or_when_switched_off(self):
        for app in (self.app(store=False), self.app(codex_collect_interval_seconds=0)):
            self.assertIsNone(app.state.codex_session_units)
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0.05)
        self.assertEqual(RecordingCollection.instances, [])


if __name__ == "__main__":
    unittest.main()
