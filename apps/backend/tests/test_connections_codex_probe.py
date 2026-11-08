"""The Codex connection's health probe in the service (Decision 0100, 5, 10
and "Test の方針"; Decision 0083, 6; Decision 0096, 2): real PostgreSQL, a real
``FileSecretStore``, the connection's real session slot.

* the probe waits for the connection's session slot and, when it stays taken,
  is ``busy``: nothing is sent and the status is kept;
* held, the adapter gets a ``ProbeSession``: the binding puts the probe's
  session, the handle and the version on the slot's row; the write-back makes
  a refreshed login the credential's next version (``connection.rotate``)
  under the compare-and-swap; the slot is given back afterwards and the
  adapter told so;
* an ``unavailable`` the adapter says keeps the status, a slot lost meanwhile,
  a probe stopped by the backend's shutdown: none changes the status; a CLI
  that could not be stopped keeps the slot (``connection.session_kept``);
* without a credential store (no write-back) nothing is sent;
* end to end, the real ``CodexProbeAdapter`` with a fake ``codex`` marks the
  connection ``connected`` and writes the refreshed login back.
"""

import asyncio
import json
import shutil
import sys
import uuid

from sqlalchemy import text

from paw_backend.agents.cli_session import UnitNotStoppedError
from paw_backend.agents.codex_cli import InteractiveSubprocessLauncher
from paw_backend.agents.codex_credentials import CodexAuthFile
from paw_backend.agents.codex_probe import CodexProbeAdapter
from paw_backend.connections import (
    AdapterFailure,
    AdapterRegistry,
    ConnectionStatus,
    FailureCode,
    ProbeOutcome,
    ProbeReport,
    ProbeSession,
    ProbeTrigger,
    WriteBack,
)
from paw_backend.connections.store import ConnectionStore
from paw_backend.db import Database

from .support import make_settings
from .task_support import TEST_DATABASE_URL
from .test_agents_codex_probe import FAKE, GOOD
from .test_connections_session_slot import CODEX, LOGIN, REFRESHED, SlotCase

MANUAL = ProbeTrigger.MANUAL


class ProbeInSlot:
    """A Codex probe adapter double: uses the slot's hooks as the real one."""

    kind = CODEX
    exclusive_sessions = True

    def __init__(self, *, refresh=None, report=None, hold=None, keep=False):
        self.refresh = refresh
        self.report = report or ProbeReport(ProbeOutcome.CONNECTED, None, 10, 2, "m")
        self.hold = hold
        self.keep = keep
        self.calls = []
        self.sessions = []
        self.released = []
        self.bound = None
        self.written = []
        self.cancelled = False

    async def run(self, secret, request):
        raise AdapterFailure(FailureCode.INTERNAL_ERROR)

    async def check_health(self, secret, session=None):
        self.calls.append(secret.reveal())
        self.sessions.append(session)
        if session is None:
            return ProbeReport(ProbeOutcome.UNAVAILABLE, FailureCode.UNAVAILABLE)
        session.on_released(self.released.append)
        self.bound = await session.bind_session(uuid.uuid4())
        try:
            if self.hold is not None:
                await self.hold.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            if self.refresh is not None and await session.still_held():
                self.written.append(await session.write_back(self.refresh))
            if self.keep:
                session.cli_running()
        return self.report


class CodexProbeCase(SlotCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.set_status("unavailable")

    def set_status(self, status):
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE shared_connections SET status = :s WHERE kind = 'codex'"),
                {"s": status},
            )

    def probing_service(self, adapter, **options):
        self.adapters = AdapterRegistry()
        self.adapters.register(adapter)
        return self.slot_service(adapter=None, **options)

    def slot_service(self, adapter, **options):
        # The probe's service: the probe adapters, the credential store, no
        # session adapter (the HTTP layer's).
        options.setdefault("credential_store", self.store)
        return self.new_service(
            **{
                "session_slot_wait_seconds": 1.0,
                "session_slot_ttl_seconds": 1.2,
                "session_slot_heartbeat_seconds": 0.1,
                "session_slot_margin_seconds": 0.6,
                **options,
            }
        )

    async def probe(self, service):
        ticket = await service.start_probe(self.principal(self.admin), CODEX, MANUAL)
        return ticket, await service.wait_probe(ticket.id)

    def status(self):
        return self.connection_row(CODEX)["status"]

    def probe_row(self):
        (row,) = self.rows("SELECT * FROM connection_probes")
        return row

    def actions(self):
        return [(e.action, e.reason) for e in self.sink.events]


class SlotTest(CodexProbeCase):
    async def test_a_probe_runs_in_the_slot_and_writes_the_refresh_back(self):
        adapter = ProbeInSlot(refresh=REFRESHED)
        service = self.probing_service(adapter)
        _, result = await self.probe(service)
        self.assertEqual(result.outcome, ProbeOutcome.CONNECTED)
        self.assertEqual(self.status(), "connected")
        self.assertEqual(adapter.calls, [LOGIN])
        (session,) = adapter.sessions
        self.assertIsInstance(session, ProbeSession)
        self.assertEqual(session.connection_id, self.connection_row(CODEX)["id"])
        self.assertTrue(adapter.bound)
        self.assertEqual(adapter.written, [WriteBack.WRITTEN])
        stored = await self.store.read(self.handle)
        self.assertEqual((stored.version, stored.secret.reveal()), (2, REFRESHED))
        self.assertIn(("connection.rotate", "allow"), [
            (e.action, e.decision) for e in self.sink.events
        ])  # fmt: skip
        # Given back afterwards, and the adapter told so.
        self.assertEqual(self.leases(), [])
        self.assertEqual(adapter.released, [True])
        self.assertEqual(self.probe_row()["credential_version"], 1)

    async def test_the_binding_is_on_the_slots_row_while_it_runs(self):
        hold = asyncio.Event()
        adapter = ProbeInSlot(hold=hold)
        service = self.probing_service(adapter)
        ticket = await service.start_probe(self.principal(self.admin), CODEX, MANUAL)
        for _ in range(200):
            if adapter.bound:
                break
            await asyncio.sleep(0.02)
        (row,) = self.rows(
            "SELECT session, secret_handle, expected_version"
            " FROM connection_session_leases"
        )
        self.assertIsNotNone(row["session"])
        self.assertEqual(
            (row["secret_handle"], row["expected_version"]), (self.handle, 1)
        )
        hold.set()
        await service.wait_probe(ticket.id)
        self.assertEqual(self.leases(), [])

    async def test_a_slot_that_stays_taken_is_busy_and_sends_nothing(self):
        other = ConnectionStore(Database(make_settings(database_url=TEST_DATABASE_URL)))
        self.assertTrue(await other.acquire_session_slot(CODEX, uuid.uuid4(), 60))
        self.set_status("connected")
        adapter = ProbeInSlot()
        _, result = await self.probe(self.probing_service(adapter))
        self.assertEqual((result.outcome, result.failure), (ProbeOutcome.BUSY, None))
        self.assertFalse(result.status_written)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(self.status(), "connected")
        row = self.probe_row()
        self.assertEqual((row["outcome"], row["failure_code"]), ("busy", None))

    async def test_an_unavailable_that_keeps_the_status_keeps_it(self):
        self.set_status("connected")
        report = ProbeReport(
            ProbeOutcome.UNAVAILABLE, FailureCode.INTERNAL_ERROR, keeps_status=True
        )
        _, result = await self.probe(self.probing_service(ProbeInSlot(report=report)))
        self.assertEqual(result.outcome, ProbeOutcome.UNAVAILABLE)
        self.assertFalse(result.status_written)
        self.assertEqual(self.status(), "connected")
        row = self.probe_row()
        self.assertEqual(
            (row["outcome"], row["failure_code"]), ("unavailable", "internal_error")
        )

    async def test_an_unavailable_about_the_credential_is_written(self):
        self.set_status("connected")
        report = ProbeReport(ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE)
        _, result = await self.probe(self.probing_service(ProbeInSlot(report=report)))
        self.assertTrue(result.status_written)
        self.assertEqual(self.status(), "unavailable")

    async def test_a_slot_lost_meanwhile_stops_the_probe_and_keeps_the_status(self):
        self.set_status("connected")
        hold = asyncio.Event()
        adapter = ProbeInSlot(hold=hold, refresh=REFRESHED)
        service = self.probing_service(adapter)
        ticket = await service.start_probe(self.principal(self.admin), CODEX, MANUAL)
        for _ in range(200):
            if adapter.bound:
                break
            await asyncio.sleep(0.02)
        self.clean_leases()  # the heartbeat's renewal fails from now on
        result = await service.wait_probe(ticket.id)
        self.assertTrue(adapter.cancelled)
        self.assertEqual(
            (result.outcome, result.failure),
            (ProbeOutcome.UNAVAILABLE, FailureCode.UNAVAILABLE),
        )
        self.assertFalse(result.status_written)
        self.assertEqual(self.status(), "connected")
        # The check before the write-back failed: nothing was written.
        self.assertEqual(adapter.written, [])
        self.assertEqual((await self.store.read(self.handle)).version, 1)

    async def test_a_cli_that_could_not_be_stopped_keeps_the_slot(self):
        report = ProbeReport(
            ProbeOutcome.UNAVAILABLE, FailureCode.INTERNAL_ERROR, keeps_status=True
        )
        adapter = ProbeInSlot(keep=True, report=report)
        await self.probe(self.probing_service(adapter))
        self.addAsyncCleanup(self.let_the_hold_go)
        self.assertEqual(len(self.leases()), 1)
        self.assertIn(("connection.session_kept", "unit_running"), self.actions())
        self.assertEqual(adapter.released, [False])

    async def let_the_hold_go(self):
        # The kept slot is renewed until its row is gone: before the loop ends.
        await asyncio.to_thread(self.clean_leases)
        await asyncio.sleep(0.3)

    async def test_without_a_credential_store_nothing_is_sent(self):
        adapter = ProbeInSlot()
        service = self.probing_service(adapter, credential_store=None)
        _, result = await self.probe(service)
        self.assertEqual(
            (result.outcome, result.failure),
            (ProbeOutcome.UNAVAILABLE, FailureCode.UNAVAILABLE),
        )
        self.assertEqual(adapter.calls, [])
        self.assertEqual(self.leases(), [])

    async def test_a_stopped_probe_is_cancelled_and_gives_the_slot_back(self):
        self.set_status("connected")
        hold = asyncio.Event()
        adapter = ProbeInSlot(hold=hold, refresh=REFRESHED)
        service = self.probing_service(adapter)
        ticket = await service.start_probe(self.principal(self.admin), CODEX, MANUAL)
        for _ in range(200):
            if adapter.bound:
                break
            await asyncio.sleep(0.02)
        await service.stop_probes()
        self.assertTrue(adapter.cancelled)
        self.assertEqual(self.probe_row()["outcome"], "cancelled")
        self.assertEqual(self.status(), "connected")
        # The refresh was written back before the slot went back.
        self.assertEqual(adapter.written, [WriteBack.WRITTEN])
        self.assertEqual(self.leases(), [])
        self.assertEqual(adapter.released, [True])
        result = await service.wait_probe(ticket.id)
        self.assertEqual(result.outcome, ProbeOutcome.CANCELLED)
        self.assertFalse(result.status_written)

    async def test_the_health_check_without_an_actor_starts_nothing_in_codex(self):
        adapter = ProbeInSlot()
        service = self.probing_service(adapter)
        status = await service.check_health(CODEX)
        self.assertIs(status, ConnectionStatus.UNAVAILABLE)
        self.assertEqual(adapter.sessions, [None])  # refused by the adapter


class EndToEndTest(CodexProbeCase):
    def real_adapter(self, actions, launcher=None):
        directory = self.root.parent / f"sessions-{uuid.uuid4().hex[:8]}"
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        scenario, report = directory / "scenario.json", directory / "report.json"
        scenario.write_text(json.dumps({"actions": actions}))
        self.sessions = directory / "s"
        self.sessions.mkdir()
        return CodexProbeAdapter(
            launcher or InteractiveSubprocessLauncher(),
            model="gpt-test-codex",
            session_base=self.sessions,
            credentials=CodexAuthFile(),
            executable=(
                sys.executable,
                "-I",
                str(FAKE),
                "--scenario",
                str(scenario),
                "--report",
                str(report),
            ),
            verified_versions=frozenset({"0.162.1"}),
        )

    async def test_a_deadline_during_a_stop_that_fails_keeps_the_status(self):
        # Codex review of #336 (P2): the deadline comes while the CLI is
        # being stopped and the stop then fails: the probe is unavailable,
        # but the connection's status is kept (Decision 0100, 10).
        self.set_status("connected")

        class SlowStuckLauncher(InteractiveSubprocessLauncher):
            async def start(self, launch):
                process = await super().start(launch)
                if "app-server" in launch.argv:
                    real_stop = process.stop

                    async def stop():
                        await real_stop()
                        await asyncio.sleep(4)  # the deadline passes here
                        raise UnitNotStoppedError("the unit could not be stopped")

                    process.stop = stop
                return process

        adapter = self.real_adapter(GOOD, SlowStuckLauncher())
        service = self.probing_service(adapter, health_timeout_seconds=3.0)
        with self.assertLogs("paw_backend", "ERROR"):
            _, result = await self.probe(service)
        self.addAsyncCleanup(self.let_the_hold_go)
        self.assertEqual(
            (result.outcome, result.failure),
            (ProbeOutcome.UNAVAILABLE, FailureCode.TIMEOUT),
        )
        self.assertFalse(result.status_written)
        self.assertEqual(self.status(), "connected")
        row = self.probe_row()
        self.assertEqual(
            (row["outcome"], row["failure_code"]), ("unavailable", "timeout")
        )
        # The CLI may still run with the login: the slot and the files stay.
        self.assertEqual(len(self.leases()), 1)
        self.assertEqual(len(list(self.sessions.iterdir())), 1)

    async def let_the_hold_go(self):
        await asyncio.to_thread(self.clean_leases)
        await asyncio.sleep(0.3)

    async def test_the_real_probe_marks_the_connection_connected(self):
        directory = self.root.parent / f"sessions-{uuid.uuid4().hex[:8]}"
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        scenario, report = directory / "scenario.json", directory / "report.json"
        scenario.write_text(json.dumps({"actions": [{"refresh": REFRESHED}, *GOOD]}))
        sessions = directory / "s"
        sessions.mkdir()
        adapter = CodexProbeAdapter(
            InteractiveSubprocessLauncher(),
            model="gpt-test-codex",
            session_base=sessions,
            credentials=CodexAuthFile(),
            executable=(
                sys.executable,
                "-I",
                str(FAKE),
                "--scenario",
                str(scenario),
                "--report",
                str(report),
            ),
            verified_versions=frozenset({"0.162.1"}),
        )
        _, result = await self.probe(self.probing_service(adapter))
        self.assertEqual(result.outcome, ProbeOutcome.CONNECTED)
        self.assertEqual(self.status(), "connected")
        stored = await self.store.read(self.handle)
        self.assertEqual((stored.version, stored.secret.reveal()), (2, REFRESHED))
        row = self.probe_row()
        self.assertEqual(
            (row["input_tokens"], row["output_tokens"], row["model"]),
            (120, 4, "gpt-test-codex"),
        )
        self.assertEqual(list(sessions.iterdir()), [])
        self.assertEqual(self.leases(), [])
        self.assertNotIn(LOGIN, self.everything_stored())
        self.assertNotIn(REFRESHED, self.everything_stored())
