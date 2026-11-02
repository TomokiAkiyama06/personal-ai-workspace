"""The health probes over HTTP (issue #306, Decision 0100, 6 and 7): the probe
that follows an attach / replace, the Admin's recheck, what the list shows, and
that the backend's shutdown stops the running probes before its database goes.

The probes' own rules are tested in ``test_connections_probe`` and the Claude
adapter in ``test_agents_claude_probe``; here the routes. The real CLI is never
run: fake adapters, and the real adapter with the fake ``claude``.
"""

import asyncio
import json
import logging
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient

from paw_backend.agents.claude_probe import ClaudeProbeAdapter
from paw_backend.agents.cli_session import SubprocessLauncher
from paw_backend.api.v1.connections import _errors, _probe_after_change
from paw_backend.app import create_app, stop_connection_probes
from paw_backend.connections import (
    AdapterFailure,
    ConnectionKind,
    FailureCode,
    ProbeRefusal,
    ProbeRefusedError,
    ProbeTrigger,
)
from paw_backend.connections.wiring import build_connection_service
from paw_backend.errors import ApiError

from .auth_http_support import error_code, requires_postgres
from .connections_fakes import FakeAdapter
from .support import FakeDatabase, make_settings
from .test_agents_claude_probe import FAKE, GOOD
from .test_connections_api import CLAUDE, CODEX, LIST, OTHER, ConnectionsHttpCase
from .test_scratch_janitor_lifespan import configured

CHECK = f"{LIST}/claude/health-check"
_LOGGER = "paw_backend.api.v1.connections"


class HeldProbe(FakeAdapter):
    """A probe adapter that stays inside ``check_health`` until released."""

    def __init__(self, kind: ConnectionKind = CLAUDE, **options) -> None:
        super().__init__(kind, **options)
        self.release = asyncio.Event()

    async def check_health(self, secret):
        self.started.set()
        await self.release.wait()
        return await super().check_health(secret)


class ProbeHttpCase(ConnectionsHttpCase):
    def setUp(self) -> None:
        super().setUp()
        # The history outlives the users and the connections (no foreign key).
        with self.engine.begin() as connection:
            connection.exec_driver_sql("TRUNCATE connection_probes")
        self.fake_passkey_step_up(self.admin)

    def use_probes(self, *probes):
        service = build_connection_service(
            self.settings,
            self.database,
            self.app.state.authorizer,
            self.services.audit_sink,
            secret_store=self.store,
            probes=probes,
        )
        self.app.state.connections = service
        return service

    def probes(self) -> list:
        return self.rows(
            "SELECT id, kind, secret_handle, trigger, state, outcome, failure_code,"
            " actor_id FROM connection_probes ORDER BY started_at, id"
        )

    def settle(self) -> None:
        """Wait for the probes this application started."""
        for row in self.probes():
            self.client.portal.call(self.app.state.connections.wait_probe, row.id)

    def release(self, adapter: HeldProbe) -> None:
        self.client.portal.call(adapter.release.set)
        self.settle()

    def attach_claude(self, value: str | None = None) -> dict:
        handle = self.put(CLAUDE, value) if value else self.put(CLAUDE)
        response = self.attach("claude", handle)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def age_probes(self, seconds: int = 3600) -> None:
        """The spacing passed: the probes ended ``seconds`` earlier."""
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE connection_probes SET"
                f" started_at = started_at - interval '{seconds} seconds',"
                f" finished_at = finished_at - interval '{seconds} seconds'"
            )

    def disable_claude(self) -> None:
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE shared_connections SET enabled = false WHERE kind = 'claude'"
            )

    def recheck(self, token: str | None = None):
        return self.call("POST", CHECK, token=token or self.admin)


@requires_postgres
class AutomaticProbeTest(ProbeHttpCase):
    def test_an_attach_starts_one_probe_and_does_not_wait_for_it(self):
        adapter = HeldProbe()
        self.use_probes(adapter)

        body = self.attach_claude()

        # The answer came while the probe is held inside the adapter.
        self.assertEqual(
            (body["status"], body["available"], body["last_probe"]),
            ("unavailable", False, None),
        )
        (running,) = self.probes()
        self.assertEqual(
            (running.kind, running.trigger, running.state, running.outcome),
            ("claude", "attach", "running", None),
        )
        self.assertEqual(running.actor_id, self.admin_id)
        self.assertEqual(self.status_of("claude")["last_probe"]["state"], "running")

        self.release(adapter)

        (finished,) = self.probes()
        self.assertEqual((finished.state, finished.outcome), ("finished", "connected"))
        self.assertEqual(adapter.health_checks, 1)
        status = self.status_of("claude")
        self.assertEqual(status["status"], "connected")
        summary = self.audit_summary()
        self.assertEqual(summary[("connection.probe", "allow", "attach")], 1)
        self.assertEqual(summary[("connection.probe_result", "allow", "connected")], 1)
        self.assertEqual(summary[("connection.status", "allow", "connected")], 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM connection_usage"), 0)

    def test_a_replacement_probes_the_new_credential(self):
        first = FakeAdapter(CLAUDE)
        self.use_probes(first)
        body = self.attach_claude()
        self.settle()
        second = self.put(CLAUDE, OTHER)

        # At once after the first probe: an automatic probe is not spaced.
        replaced = self.replace("claude", second, body["revision"])

        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(replaced.json()["status"], "unavailable")
        self.settle()
        rows = self.probes()
        self.assertEqual(
            [(r.trigger, r.outcome) for r in rows],
            [("attach", "connected"), ("replace", "connected")],
        )
        self.assertEqual(rows[1].secret_handle, second)
        self.assertEqual(first.revealed[-1], OTHER)
        self.assertEqual(self.status_of("claude")["status"], "connected")

    def test_a_refused_change_starts_nothing(self):
        adapter = FakeAdapter(CLAUDE)
        self.use_probes(adapter)
        missing = "cred_" + "1" * 32

        unknown = self.attach("claude", missing)
        self.assertEqual(unknown.status_code, 422, unknown.text)
        self.assertEqual(self.probes(), [])

        body = self.attach_claude()
        self.settle()
        stale = self.replace("claude", self.put(CLAUDE, OTHER), "0" * 32)
        again = self.attach("claude", self.put(CLAUDE, OTHER))
        self.assertEqual(
            (stale.status_code, error_code(stale)),
            (409, "stale_connection_credential"),
        )
        self.assertEqual(
            (again.status_code, error_code(again)), (409, "connection_exists")
        )
        self.assertEqual(len(self.probes()), 1)
        self.assertEqual(adapter.health_checks, 1)
        self.assertTrue(body["revision"])

    def test_without_a_probe_adapter_nothing_is_recorded_and_it_stays_unavailable(self):
        with self.assertLogs("paw_backend.connections", logging.WARNING) as logs:
            body = self.attach_claude()

        self.assertEqual(body["status"], "unavailable")
        self.assertEqual(self.probes(), [])
        self.assertIn("reason=probe_not_configured", "\n".join(logs.output))
        self.assertEqual(self.status_of("claude")["status"], "unavailable")
        summary = self.audit_summary()
        self.assertEqual(summary[("connection.connect", "allow", "succeeded")], 1)
        self.assertEqual(
            sum(
                count for key, count in summary.items() if key[0] == "connection.probe"
            ),
            0,
        )

    def test_a_disabled_connection_is_not_probed_after_a_replacement(self):
        adapter = FakeAdapter(CLAUDE)
        self.use_probes(adapter)
        body = self.attach_claude()
        self.settle()
        self.disable_claude()

        replaced = self.replace("claude", self.put(CLAUDE, OTHER), body["revision"])

        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(adapter.health_checks, 1)
        self.assertEqual(len(self.probes()), 1)
        # The earlier credential's verdict says nothing about this one.
        status = self.status_of("claude")
        self.assertIsNone(status["last_probe"])
        self.assertIsNone(status["probe_available_at"])

    def test_a_failure_to_start_does_not_undo_the_committed_change(self):
        self.use_probes(FakeAdapter(CLAUDE))

        async def fail(*_):
            raise psycopg.OperationalError("the database went away")

        self.app.state.connections.start_probe = fail

        with self.assertLogs(_LOGGER, logging.WARNING) as logs:
            body = self.attach_claude()

        self.assertEqual(body["status"], "unavailable")
        self.assertEqual(self.attached()[0][0], "claude")
        self.assertIn("connection probe not started", "\n".join(logs.output))
        self.assertNotIn("the database went away", "\n".join(logs.output))


@requires_postgres
class RecheckTest(ProbeHttpCase):
    def test_a_recheck_answers_at_once_and_its_end_is_read_from_the_list(self):
        adapter = HeldProbe()
        self.use_probes(adapter)
        self.attach_claude()
        self.release(adapter)
        self.age_probes()
        self.client.portal.call(adapter.release.clear)

        response = self.recheck()

        self.assertEqual(response.status_code, 202, response.text)
        probe = response.json()["probe"]
        self.assertEqual(set(probe), {"id", "trigger", "state", "started_at"})
        self.assertEqual((probe["trigger"], probe["state"]), ("manual", "running"))
        running = self.status_of("claude")["last_probe"]
        self.assertEqual(
            (running["trigger"], running["state"], running["outcome"]),
            ("manual", "running", None),
        )
        self.assertIsNone(running["finished_at"])

        self.release(adapter)

        status = self.status_of("claude")
        self.assertEqual(set(status["last_probe"]), {
            "trigger", "state", "outcome", "started_at", "finished_at",
        })  # fmt: skip
        self.assertEqual(
            (status["last_probe"]["state"], status["last_probe"]["outcome"]),
            ("finished", "connected"),
        )
        self.assertEqual(status["status"], "connected")
        self.assertEqual(str(self.probes()[-1].id), probe["id"])
        self.assertEqual(
            self.audit_summary()[("connection.probe", "allow", "manual")], 1
        )

    def test_a_missing_connection_is_not_found(self):
        self.use_probes(FakeAdapter(CLAUDE))

        response = self.recheck()

        self.assertEqual(
            (response.status_code, error_code(response)), (404, "not_found")
        )
        self.assertEqual(self.probes(), [])

    def test_a_disabled_connection_is_a_conflict_and_is_audited(self):
        adapter = FakeAdapter(CLAUDE)
        self.use_probes(adapter)
        self.attach_claude()
        self.settle()
        self.age_probes()
        self.disable_claude()

        response = self.recheck()

        self.assertEqual(
            (response.status_code, error_code(response)), (409, "connection_disabled")
        )
        self.assertEqual(adapter.health_checks, 1)
        self.assertEqual(
            self.audit_summary()[("connection.probe", "deny", "connection_disabled")], 1
        )

    def test_a_kind_without_a_probe_adapter_is_a_conflict(self):
        # Codex's adapter comes after #305; the connection itself may exist.
        self.use_probes(FakeAdapter(CLAUDE))
        created = self.attach("codex", self.put(CODEX))
        self.assertEqual(created.status_code, 201, created.text)

        response = self.call("POST", f"{LIST}/codex/health-check", token=self.admin)

        self.assertEqual(
            (response.status_code, error_code(response)), (409, "probe_not_configured")
        )
        self.assertEqual(self.probes(), [])
        self.assertEqual(
            self.audit_summary()[("connection.probe", "deny", "probe_not_configured")],
            1,
        )

    def test_a_running_probe_of_the_credential_is_a_conflict(self):
        adapter = HeldProbe()
        self.use_probes(adapter)
        self.attach_claude()

        response = self.recheck()

        self.assertEqual(
            (response.status_code, error_code(response)), (409, "probe_running")
        )
        self.assertEqual(len(self.probes()), 1)
        self.release(adapter)

    def test_rechecks_are_spaced_doubling_after_failures_until_a_replacement(self):
        adapter = FakeAdapter(
            CLAUDE, health_error=AdapterFailure(FailureCode.INTERNAL_ERROR)
        )
        self.use_probes(adapter)
        body = self.attach_claude()
        self.settle()

        # One failure: 60 s from its end.
        early = self.recheck()
        self.assertEqual(
            (early.status_code, error_code(early)), (429, "probe_cooldown")
        )
        self.assertTrue(0 < int(early.headers["Retry-After"]) <= 60, early.headers)
        self.assertEqual(len(self.probes()), 1)
        self.assertEqual(
            self.audit_summary()[("connection.probe", "deny", "probe_cooldown")], 1
        )
        self.assert_available_within(60)

        # The spacing passed: nothing to wait for, and a second failure doubles it.
        self.age_probes(61)
        self.assertIsNone(self.status_of("claude")["probe_available_at"])
        self.assertEqual(self.recheck().status_code, 202)
        self.settle()
        doubled = self.recheck()
        self.assertEqual(doubled.status_code, 429)
        self.assertTrue(60 < int(doubled.headers["Retry-After"]) <= 120)
        self.assert_available_within(120)

        # A new credential starts again.
        replaced = self.replace("claude", self.put(CLAUDE, OTHER), body["revision"])
        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.settle()
        again = self.recheck()
        self.assertEqual(again.status_code, 429)
        self.assertTrue(0 < int(again.headers["Retry-After"]) <= 60)

    def assert_available_within(self, seconds: int) -> None:
        available = datetime.fromisoformat(
            self.status_of("claude")["probe_available_at"]
        )
        now = self.scalar("SELECT clock_timestamp()")
        self.assertLessEqual(available - now, timedelta(seconds=seconds))
        self.assertGreater(available, now)

    def test_a_recheck_needs_a_recent_passkey_step_up(self):
        adapter = FakeAdapter(CLAUDE)
        self.use_probes(adapter)
        self.attach_claude()
        self.settle()
        self.age_probes()

        # The owner has no step-up.
        response = self.recheck(self.owner)

        self.assertEqual(
            (response.status_code, error_code(response)), (403, "step_up_required")
        )
        self.assertEqual(adapter.health_checks, 1)
        self.assertEqual(
            self.audit_summary()[("connection.probe", "deny", "step_up_required")], 1
        )

    def test_a_recheck_needs_admin_config_manage(self):
        adapter = FakeAdapter(CLAUDE)
        self.use_probes(adapter)
        self.attach_claude()
        self.settle()
        self.age_probes()
        self.fake_passkey_step_up(self.user)

        response = self.recheck(self.user)

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(adapter.health_checks, 1)
        self.assertEqual(self.call("POST", CHECK).status_code, 401, "no session at all")

    def test_without_a_secret_store_or_with_a_bad_kind_nothing_runs(self):
        self.use_store(None)
        response = self.recheck()
        self.assertEqual(
            (response.status_code, error_code(response)),
            (503, "secret_store_not_configured"),
        )
        unknown = self.call("POST", f"{LIST}/gemini/health-check", token=self.admin)
        self.assertEqual(unknown.status_code, 422, unknown.text)

    def test_a_driver_failure_is_service_unavailable(self):
        self.use_probes(FakeAdapter(CLAUDE))

        async def fail(*_):
            raise psycopg.OperationalError("connection lost")

        self.app.state.connections.start_probe = fail

        response = self.recheck()

        self.assertEqual(
            (response.status_code, error_code(response)), (503, "service_unavailable")
        )


@requires_postgres
class ListTest(ProbeHttpCase):
    def test_a_connection_never_probed_shows_nulls(self):
        self.attach_claude()
        status = self.status_of("claude")
        self.assertIsNone(status["last_probe"])
        self.assertIsNone(status["probe_available_at"])
        self.assertIsNone(self.status_of("codex")["last_probe"])

    def test_the_failure_code_tokens_and_handle_are_not_shown(self):
        adapter = FakeAdapter(CLAUDE, health_error=AdapterFailure(FailureCode.EXPIRED))
        self.use_probes(adapter)
        handle = self.put(CLAUDE)
        self.assertEqual(self.attach("claude", handle).status_code, 201)
        self.settle()

        response = self.call("GET", LIST, token=self.admin)

        self.assertEqual(response.status_code, 200, response.text)
        (found,) = [c for c in response.json()["connections"] if c["kind"] == "claude"]
        self.assertEqual(found["status"], "expired")
        self.assertEqual(found["last_probe"]["outcome"], "expired")
        self.assertEqual(self.probes()[0].failure_code, "expired")
        self.assertNotIn("failure", response.text)
        self.assertNotIn("token", response.text)
        self.assertNotIn(handle, response.text)
        for row in self.audit_rows():
            self.assertNotIn(handle, str(row))

    def test_a_probe_abandoned_by_its_process_has_no_outcome(self):
        adapter = HeldProbe()
        self.use_probes(adapter)
        self.attach_claude()
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE connection_probes SET state = 'abandoned',"
                " finished_at = clock_timestamp(), duration_ms = 0"
            )

        last = self.status_of("claude")["last_probe"]

        self.assertEqual((last["state"], last["outcome"]), ("abandoned", None))
        self.client.portal.call(adapter.release.set)
        self.settle()


@requires_postgres
class RealAdapterTest(ProbeHttpCase):
    """The real adapter and launcher behind the routes, with the fake ``claude``:
    the token is the attached credential's, and it reaches only the CLI."""

    def setUp(self) -> None:
        super().setUp()
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.scenario = self.base / "scenario.json"
        self.report = self.base / "report.json"
        sessions = self.base / "sessions"
        sessions.mkdir()
        self.use_probes(
            ClaudeProbeAdapter(
                SubprocessLauncher(),
                model="claude-test-model",
                session_base=sessions,
                executable=(
                    sys.executable,
                    "-I",
                    str(FAKE),
                    "--scenario",
                    str(self.scenario),
                    "--report",
                    str(self.report),
                ),
            )
        )

    def test_an_attach_with_a_working_cli_is_connected(self):
        self.scenario.write_text(json.dumps({"actions": GOOD}))

        self.attach_claude()
        self.settle()

        self.assertEqual(self.status_of("claude")["status"], "connected")
        (row,) = self.rows(
            "SELECT outcome, failure_code, input_tokens, output_tokens, model"
            " FROM connection_probes"
        )
        self.assertEqual(tuple(row), ("connected", None, 120, 4, "claude-test-model"))
        self.assertNotIn("PAW_PROBE_OK", self.everything_stored())

    def test_a_refused_credential_is_expired(self):
        self.scenario.write_text(
            json.dumps({"actions": [{"retry": "authentication_failed"}, {"sleep": 30}]})
        )

        self.attach_claude()
        self.settle()

        self.assertEqual(self.status_of("claude")["status"], "expired")

    def test_the_shutdown_stops_the_cli_and_closes_the_row(self):
        self.scenario.write_text(json.dumps({"actions": [{"sleep": 30}, *GOOD]}))
        self.attach_claude()
        started = time.monotonic()

        self.client.portal.call(
            stop_connection_probes, self.app.state.connections, 20.0
        )

        self.assertLess(time.monotonic() - started, 20)
        (row,) = self.probes()
        self.assertEqual((row.state, row.outcome), ("finished", "cancelled"))
        status = self.status_of("claude")
        self.assertEqual(
            (status["status"], status["checked_at"]), ("unavailable", None)
        )
        self.assertEqual(
            self.audit_summary()[("connection.probe_result", "allow", "cancelled")], 1
        )


class FakeProbes:
    """Stands in for the connection service at the shutdown."""

    def __init__(self, *, error: Exception | None = None, hang: bool = False) -> None:
        self.error = error
        self.hang = hang
        self.stopped = False

    async def stop_probes(self) -> None:
        if self.hang:
            await asyncio.Event().wait()
        self.stopped = True
        if self.error is not None:
            raise self.error


class ShutdownTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_probes_stop_before_the_database_is_disposed(self):
        settings, database = configured()
        app = create_app(settings, database=database)
        fake = FakeProbes()
        app.state.connections = fake
        seen = []
        database.on_dispose = lambda: seen.append(fake.stopped)

        async with app.router.lifespan_context(app):
            self.assertFalse(fake.stopped)

        self.assertEqual(seen, [True])

    async def test_a_probe_that_does_not_stop_does_not_hold_the_shutdown(self):
        settings, database = configured(shutdown_timeout_seconds=1)
        app = create_app(settings, database=database)
        app.state.connections = FakeProbes(hang=True)

        with (
            patch("paw_backend.app.PROBE_SHUTDOWN_WAIT_SECONDS", 0),
            self.assertLogs("paw_backend.app", logging.WARNING) as logs,
        ):
            async with asyncio.timeout(10):
                async with app.router.lifespan_context(app):
                    pass

        self.assertTrue(database.disposed)
        self.assertIn("did not stop", "\n".join(logs.output))

    async def test_a_failing_stop_is_logged_by_its_type_only(self):
        fake = FakeProbes(error=RuntimeError("do-not-print"))

        with self.assertLogs("paw_backend.app", logging.WARNING) as logs:
            await stop_connection_probes(fake, 1.0)

        self.assertTrue(fake.stopped)
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("do-not-print", "\n".join(logs.output))

    async def test_no_service_no_stop(self):
        await stop_connection_probes(None, 1.0)  # no database: no service
        app = create_app(make_settings(), database=FakeDatabase())
        self.assertIsNone(app.state.connections)
        async with app.router.lifespan_context(app):
            pass


class ProbeAfterChangeTest(unittest.IsolatedAsyncioTestCase):
    """What a committed attach / replace does with the probe's start."""

    class Service:
        def __init__(self, error: BaseException | None = None) -> None:
            self.error = error
            self.calls = []

        async def start_probe(self, principal, kind, trigger):
            self.calls.append((principal, kind, trigger))
            if self.error is not None:
                raise self.error

    async def after(self, error: BaseException | None = None):
        service = self.Service(error)
        await _probe_after_change(service, "admin", CLAUDE, ProbeTrigger.REPLACE)
        self.assertEqual(service.calls, [("admin", CLAUDE, ProbeTrigger.REPLACE)])

    async def test_it_starts_the_probe(self):
        await self.after()

    async def test_a_refusal_is_quiet_here(self):
        for reason in (
            ProbeRefusal.PROBE_NOT_CONFIGURED,
            ProbeRefusal.CONNECTION_DISABLED,
            ProbeRefusal.PROBE_RUNNING,
        ):
            await self.after(ProbeRefusedError(reason))

    async def test_a_failure_is_logged_by_type_only(self):
        with self.assertLogs(_LOGGER, logging.WARNING) as logs:
            await self.after(psycopg.OperationalError("do-not-print"))
        self.assertNotIn("do-not-print", "\n".join(logs.output))

    async def test_a_cancellation_goes_through(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.after(asyncio.CancelledError())


class ErrorMappingTest(unittest.TestCase):
    def mapped(self, error: Exception) -> ApiError:
        with self.assertRaises(ApiError) as caught, _errors():
            raise error
        return caught.exception

    def test_a_refused_recheck_is_a_conflict(self):
        for reason in (
            ProbeRefusal.CONNECTION_DISABLED,
            ProbeRefusal.PROBE_NOT_CONFIGURED,
            ProbeRefusal.PROBE_RUNNING,
        ):
            with self.subTest(reason):
                error = self.mapped(ProbeRefusedError(reason))
                self.assertEqual(
                    (error.status_code, error.code, error.headers),
                    (409, reason.value, None),
                )

    def test_the_spacing_is_too_many_requests_with_the_wait(self):
        error = self.mapped(ProbeRefusedError(ProbeRefusal.PROBE_COOLDOWN, 37))
        self.assertEqual(
            (error.status_code, error.code, error.headers),
            (429, "probe_cooldown", {"Retry-After": "37"}),
        )

    def test_the_header_of_an_api_error_is_sent(self):
        app = create_app(make_settings(), database=FakeDatabase())

        @app.get("/raises")
        async def raises():
            raise self.mapped(ProbeRefusedError(ProbeRefusal.PROBE_COOLDOWN, 37))

        response = TestClient(app, base_url="https://localhost").get("/raises")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "37")
        self.assertEqual(error_code(response), "probe_cooldown")


if __name__ == "__main__":
    unittest.main()
