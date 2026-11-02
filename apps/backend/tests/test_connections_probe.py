"""The health probes of the connections (issue #306, Decision 0100, approved
2026-10-29): recorded with their audit event before anything is sent, run in
the background through the probe adapter, finished with an outcome, and
written to the connection's status only when they say something about the
credential and no newer verdict came meanwhile. Refusals (not configured,
disabled, running, the spacing of rechecks) record and send nothing.
"""

import asyncio
import logging
import unittest
import uuid
from datetime import UTC, datetime, timedelta

from paw_backend.authz.policy import Reason
from paw_backend.connections import (
    AdapterFailure,
    ConnectionKind,
    ConnectionNotFoundError,
    ConnectionPermissionDeniedError,
    ConnectionStatus,
    FailureCode,
    InvalidConnectionInputError,
    ProbeOutcome,
    ProbeRefusal,
    ProbeRefusedError,
    ProbeReport,
    ProbeTrigger,
)
from paw_backend.connections.probe import (
    PROBE_ABANDONED_AGE_SECONDS,
    changes_status,
    cooldown_seconds,
    failures_in_a_row,
    probe_available_at,
)
from paw_backend.connections.store import ConnectionStore
from paw_backend.db import Database

from .connections_fakes import CANARY, handle
from .connections_support import PostgresConnectionTestCase, requires_postgres
from .support import make_settings
from .task_support import TEST_DATABASE_URL

CLAUDE, CODEX = ConnectionKind.CLAUDE, ConnectionKind.CODEX
MANUAL, ATTACH, REPLACE = (
    ProbeTrigger.MANUAL,
    ProbeTrigger.ATTACH,
    ProbeTrigger.REPLACE,
)
T = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


class RulesTest(unittest.TestCase):
    def test_the_spacing_doubles_after_each_failure_up_to_fifteen_minutes(self):
        self.assertEqual(
            [cooldown_seconds(n) for n in range(8)],
            [60, 60, 120, 240, 480, 900, 900, 900],
        )

    def test_failures_are_counted_since_the_last_success(self):
        U, E, C = ProbeOutcome.UNAVAILABLE, ProbeOutcome.EXPIRED, ProbeOutcome.CONNECTED
        B, X = ProbeOutcome.BUSY, ProbeOutcome.CANCELLED
        self.assertEqual(failures_in_a_row([]), 0)
        self.assertEqual(failures_in_a_row([C, U, U]), 0)
        self.assertEqual(failures_in_a_row([U, E, C, U]), 2)
        # busy and cancelled say nothing about the credential: skipped.
        self.assertEqual(failures_in_a_row([B, U, X, U, C]), 2)

    def test_the_next_recheck_is_counted_from_the_latest_end(self):
        U, C = ProbeOutcome.UNAVAILABLE, ProbeOutcome.CONNECTED
        self.assertIsNone(probe_available_at([]))
        self.assertEqual(probe_available_at([(C, T)]), T + timedelta(seconds=60))
        self.assertEqual(
            probe_available_at([(U, T), (U, T - timedelta(minutes=5)), (C, T)]),
            T + timedelta(seconds=120),
        )

    def test_only_a_verdict_about_the_credential_changes_the_status(self):
        self.assertTrue(changes_status(ProbeOutcome.CONNECTED, None))
        self.assertTrue(changes_status(ProbeOutcome.EXPIRED, FailureCode.EXPIRED))
        self.assertTrue(changes_status(ProbeOutcome.UNAVAILABLE, FailureCode.TIMEOUT))
        self.assertTrue(changes_status(ProbeOutcome.UNAVAILABLE, None))
        self.assertFalse(
            changes_status(ProbeOutcome.UNAVAILABLE, FailureCode.RATE_LIMITED)
        )
        self.assertFalse(changes_status(ProbeOutcome.BUSY, None))
        self.assertFalse(changes_status(ProbeOutcome.CANCELLED, None))

    def test_a_report_is_checked(self):
        ProbeReport(ProbeOutcome.CONNECTED, None, 10, 2, "claude-x")
        for bad in (
            {"outcome": "connected"},
            {"outcome": ProbeOutcome.CANCELLED},
            {"outcome": ProbeOutcome.CONNECTED, "failure": FailureCode.TIMEOUT},
            {"outcome": ProbeOutcome.UNAVAILABLE, "failure": "timeout"},
            {"outcome": ProbeOutcome.CONNECTED, "input_tokens": -1},
            {"outcome": ProbeOutcome.CONNECTED, "model": "a model"},
        ):
            with self.subTest(bad), self.assertRaises((ValueError, TypeError)):
                ProbeReport(**bad)

    def test_a_refusal_carries_the_wait_only_for_the_spacing(self):
        error = ProbeRefusedError(ProbeRefusal.PROBE_COOLDOWN, 30)
        self.assertEqual(
            (error.code, error.retry_after_seconds), ("probe_cooldown", 30)
        )
        for reason, wait in (
            (ProbeRefusal.PROBE_COOLDOWN, None),
            (ProbeRefusal.PROBE_COOLDOWN, 0),
            (ProbeRefusal.PROBE_RUNNING, 5),
        ):
            with self.subTest(reason=reason, wait=wait), self.assertRaises(ValueError):
                ProbeRefusedError(reason, wait)


@requires_postgres
class ProbeTest(PostgresConnectionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.connection_id = self.seed_connection(CLAUDE, status="unavailable")
        self.admin_principal = self.principal(self.admin)

    async def probe(self, trigger=MANUAL, service=None):
        service = service or self.service
        ticket = await service.start_probe(self.admin_principal, CLAUDE, trigger)
        return ticket, await service.wait_probe(ticket.id)

    def probe_rows(self):
        return self.rows("SELECT * FROM connection_probes ORDER BY started_at, id")

    def audit_rows(self, correlation_id):
        return self.rows(
            "SELECT action, decision, reason, actor_id, actor_role, resource_id"
            " FROM audit_events WHERE correlation_id = :c ORDER BY occurred_at",
            c=correlation_id,
        )

    def hold_checks(self) -> asyncio.Event:
        gate = asyncio.Event()
        self.claude.started_checks = 0

        async def held(secret):
            self.claude.started_checks += 1
            await gate.wait()
            return ConnectionStatus.CONNECTED

        self.claude.check_health = held
        return gate

    # -- a probe that runs ---------------------------------------------------

    async def test_a_probe_after_an_attach_marks_a_good_credential_connected(self):
        ticket, result = await self.probe(ATTACH)

        self.assertEqual(result.outcome, ProbeOutcome.CONNECTED)
        self.assertTrue(result.status_written)
        row = self.connection_row(CLAUDE)
        self.assertEqual(row["status"], "connected")
        self.assertIsNotNone(row["checked_at"])
        (probe,) = self.probe_rows()
        self.assertEqual(probe["id"], ticket.id)
        self.assertEqual(
            (probe["state"], probe["outcome"], probe["failure_code"]),
            ("finished", "connected", None),
        )
        self.assertEqual(
            (probe["trigger"], probe["actor_id"], probe["secret_handle"]),
            ("attach", self.admin, handle(1)),
        )
        self.assertEqual(probe["connection_id"], self.connection_id)
        self.assertGreaterEqual(probe["finished_at"], probe["started_at"])
        self.assertGreaterEqual(probe["duration_ms"], 0)
        self.assertEqual(self.claude.revealed, [CANARY])
        # Not a call of a user: no usage row, no quota, no admission.
        self.assertEqual(self.usage_rows(), [])

    async def test_the_start_is_audited_with_the_row_and_the_end_after_it(self):
        ticket, _ = await self.probe(ATTACH)

        (start,) = self.audit_rows(ticket.id)
        self.assertEqual(
            (start["action"], start["decision"], start["reason"]),
            ("connection.probe", "allow", "attach"),
        )
        self.assertEqual(start["actor_id"], self.admin)
        self.assertEqual(start["actor_role"], "admin")
        self.assertEqual(start["resource_id"], self.connection_id)
        own = self.own_audit()
        self.assertIn(("connection.probe_result", "allow", "connected"), own)
        self.assertIn(("connection.status", "allow", "connected"), own)
        (result,) = [
            e for e in self.sink.events if e.action == "connection.probe_result"
        ]
        self.assertEqual(result.correlation_id, ticket.id)
        self.assertIsNone(result.actor_id)
        self.assertEqual(result.actor_role, "system")

    async def test_nothing_of_the_credential_is_stored_or_audited(self):
        await self.probe(ATTACH)
        stored = repr(self.probe_rows()) + repr(self.sink.events)
        self.assertNotIn(CANARY, stored)
        audited = repr(
            self.rows("SELECT * FROM audit_events WHERE action LIKE 'connection.%'")
        )
        self.assertNotIn(handle(1), audited)
        self.assertNotIn(CANARY, audited)

    async def test_the_reported_tokens_and_model_are_kept(self):
        self.claude.health = ProbeReport(
            ProbeOutcome.CONNECTED, None, 321, 7, "claude-x"
        )
        await self.probe()
        (probe,) = self.probe_rows()
        self.assertEqual(
            (probe["input_tokens"], probe["output_tokens"], probe["model"]),
            (321, 7, "claude-x"),
        )

    async def test_an_expired_credential_is_marked_expired(self):
        self.claude.health_error = AdapterFailure(FailureCode.EXPIRED)
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            _, result = await self.probe()
        self.assertEqual(
            (result.outcome, result.failure),
            (ProbeOutcome.EXPIRED, FailureCode.EXPIRED),
        )
        self.assertEqual(self.connection_row(CLAUDE)["status"], "expired")

    async def test_any_other_failure_is_unavailable_with_its_type_logged_only(self):
        self.claude.health_error = RuntimeError("the CLI said " + CANARY)
        self.clear_connections()
        self.seed_connection(CLAUDE, status="connected")
        with self.assertLogs("paw_backend.connections", logging.WARNING) as logs:
            _, result = await self.probe()
        self.assertEqual(
            (result.outcome, result.failure),
            (ProbeOutcome.UNAVAILABLE, FailureCode.INTERNAL_ERROR),
        )
        self.assertEqual(self.connection_row(CLAUDE)["status"], "unavailable")
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn(CANARY, text)

    async def test_a_rate_limit_or_a_busy_slot_does_not_change_the_status(self):
        for verdict, error in (
            (None, AdapterFailure(FailureCode.RATE_LIMITED)),
            (ProbeReport(ProbeOutcome.BUSY), None),
            (ProbeReport(ProbeOutcome.UNAVAILABLE, FailureCode.RATE_LIMITED), None),
        ):
            with self.subTest(verdict=verdict, error=error):
                self.clean_tables()
                self.admin = self.seed_user(system_role="admin")
                self.admin_principal = self.principal(self.admin)
                self.seed_connection(CLAUDE, status="connected")
                before = self.connection_row(CLAUDE)
                self.claude.health = verdict
                self.claude.health_error = error
                with self.assertNoLogs("paw_backend.connections", logging.ERROR):
                    _, result = await self.probe()
                self.assertFalse(result.status_written)
                after = self.connection_row(CLAUDE)
                self.assertEqual(
                    (after["status"], after["checked_at"]),
                    (before["status"], before["checked_at"]),
                )
                self.assertNotIn(
                    "connection.status", [a for a, _, _ in self.own_audit()]
                )

    async def test_an_answer_that_is_neither_a_status_nor_a_report_is_unavailable(
        self,
    ):
        for verdict in ("connected", None, ProbeOutcome.CONNECTED):
            with self.subTest(verdict=repr(verdict)):
                self.claude.health = verdict
                with self.assertLogs("paw_backend.connections", logging.WARNING):
                    _, result = await self.probe(REPLACE)
                self.assertEqual(
                    (result.outcome, result.failure),
                    (ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE),
                )

    async def test_a_probe_that_never_answers_is_unavailable_at_the_deadline(self):
        self.hold_checks()
        service = self.new_service(health_timeout_seconds=0.2)
        with self.assertLogs("paw_backend.connections", logging.WARNING):
            _, result = await self.probe(service=service)
        self.assertEqual(
            (result.outcome, result.failure),
            (ProbeOutcome.UNAVAILABLE, FailureCode.TIMEOUT),
        )

    # -- whose verdict is kept -----------------------------------------------

    async def test_a_verdict_recorded_after_the_probe_started_is_kept(self):
        gate = self.hold_checks()
        ticket = await self.service.start_probe(self.admin_principal, CLAUDE, MANUAL)
        await self.wait_for(lambda: self.claude.started_checks, "the probe to start")
        # A session found the credential expired while the probe ran.
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE shared_connections SET status = 'expired',"
                " checked_at = clock_timestamp()"
            )
        gate.set()
        result = await self.service.wait_probe(ticket.id)

        self.assertEqual(result.outcome, ProbeOutcome.CONNECTED)
        self.assertFalse(result.status_written)
        self.assertEqual(self.connection_row(CLAUDE)["status"], "expired")
        (probe,) = self.probe_rows()
        self.assertEqual(probe["outcome"], "connected")

    async def test_a_verdict_about_a_replaced_credential_is_dropped(self):
        gate = self.hold_checks()
        ticket = await self.service.start_probe(self.admin_principal, CLAUDE, MANUAL)
        await self.wait_for(lambda: self.claude.started_checks, "the probe to start")
        await self.service.replace_credential(self.admin_principal, CLAUDE, handle(2))
        gate.set()
        result = await self.service.wait_probe(ticket.id)

        self.assertFalse(result.status_written)
        row = self.connection_row(CLAUDE)
        self.assertEqual(
            (row["secret_handle"], row["status"]), (handle(2), "unavailable")
        )

    async def test_an_older_verdict_does_not_stop_the_probe_from_writing(self):
        self.clear_connections()
        self.seed_connection(CLAUDE, status="expired")
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE shared_connections SET checked_at = clock_timestamp()"
                " - interval '1 hour'"
            )
        _, result = await self.probe()
        self.assertTrue(result.status_written)
        self.assertEqual(self.connection_row(CLAUDE)["status"], "connected")

    # -- refusals: nothing recorded, nothing sent ----------------------------

    async def test_no_probe_adapter_is_not_configured(self):
        self.adapters._adapters.pop(CLAUDE)
        with self.assertRaises(ProbeRefusedError) as caught:
            await self.service.start_probe(self.admin_principal, CLAUDE, MANUAL)
        self.assertIs(caught.exception.reason, ProbeRefusal.PROBE_NOT_CONFIGURED)
        self.assertEqual(self.probe_rows(), [])
        self.assertIn(
            ("connection.probe", "deny", "probe_not_configured"), self.own_audit()
        )

    async def test_an_automatic_probe_that_is_refused_is_logged_not_audited(self):
        self.adapters._adapters.pop(CLAUDE)
        with (
            self.assertLogs("paw_backend.connections", logging.WARNING) as logs,
            self.assertRaises(ProbeRefusedError),
        ):
            await self.service.start_probe(self.admin_principal, CLAUDE, ATTACH)
        self.assertIn("probe_not_configured", "\n".join(logs.output))
        self.assertNotIn("connection.probe", [a for a, _, _ in self.own_audit()])

    async def test_a_disabled_connection_is_not_probed(self):
        self.clear_connections()
        self.seed_connection(CLAUDE, status="connected", enabled=False)
        for trigger in (MANUAL, REPLACE):
            with self.subTest(trigger=trigger):
                with self.assertRaises(ProbeRefusedError) as caught:
                    with self.assertNoLogs("paw_backend.connections", logging.ERROR):
                        await self.service.start_probe(
                            self.admin_principal, CLAUDE, trigger
                        )
                self.assertIs(caught.exception.reason, ProbeRefusal.CONNECTION_DISABLED)
        self.assertEqual(self.probe_rows(), [])
        self.assertEqual(self.claude.health_checks, 0)
        self.assertEqual(self.resolver.resolved, [])

    async def test_no_connection_is_not_found(self):
        self.clear_connections()
        with self.assertRaises(ConnectionNotFoundError):
            await self.service.start_probe(self.admin_principal, CLAUDE, MANUAL)
        self.assertEqual(self.probe_rows(), [])

    async def test_a_second_probe_of_the_same_credential_is_refused_while_one_runs(
        self,
    ):
        gate = self.hold_checks()
        other = self.new_service()  # another backend process
        ticket = await self.service.start_probe(self.admin_principal, CLAUDE, ATTACH)
        for service, trigger in ((self.service, MANUAL), (other, REPLACE)):
            with (
                self.subTest(trigger=trigger),
                self.assertRaises(ProbeRefusedError) as caught,
            ):
                with self.assertLogs("paw_backend.connections", logging.WARNING):
                    await service.start_probe(self.admin_principal, CLAUDE, trigger)
            self.assertIs(caught.exception.reason, ProbeRefusal.PROBE_RUNNING)
        gate.set()
        await self.service.wait_probe(ticket.id)
        self.assertEqual(len(self.probe_rows()), 1)

    async def test_probes_that_start_together_leave_one_running(self):
        gate = self.hold_checks()
        services = [self.new_service() for _ in range(4)]
        outcomes = await asyncio.gather(
            *(s.start_probe(self.admin_principal, CLAUDE, ATTACH) for s in services),
            return_exceptions=True,
        )
        started = [o for o in outcomes if not isinstance(o, BaseException)]
        refused = [o for o in outcomes if isinstance(o, ProbeRefusedError)]
        self.assertEqual((len(started), len(refused)), (1, 3))
        self.assertTrue(all(r.reason is ProbeRefusal.PROBE_RUNNING for r in refused))
        gate.set()
        for service in services:
            await service.stop_probes()
        self.assertEqual(len(self.probe_rows()), 1)

    async def test_a_manual_recheck_is_spaced_and_an_automatic_probe_is_not(self):
        await self.probe(ATTACH)
        with self.assertRaises(ProbeRefusedError) as caught:
            await self.service.start_probe(self.admin_principal, CLAUDE, MANUAL)
        self.assertIs(caught.exception.reason, ProbeRefusal.PROBE_COOLDOWN)
        self.assertTrue(55 <= caught.exception.retry_after_seconds <= 60)
        self.assertIn(("connection.probe", "deny", "probe_cooldown"), self.own_audit())
        _, result = await self.probe(REPLACE)  # not spaced
        self.assertEqual(result.outcome, ProbeOutcome.CONNECTED)

    async def test_the_spacing_grows_with_failures_in_a_row(self):
        now = self.scalar("SELECT clock_timestamp()")
        for minutes_ago, outcome in ((30, "connected"), (20, "unavailable"),
                                     (10, "expired"), (1, "unavailable")):  # fmt: skip
            self.seed_probe(outcome, now - timedelta(minutes=minutes_ago))
        with self.assertRaises(ProbeRefusedError) as caught:
            await self.service.start_probe(self.admin_principal, CLAUDE, MANUAL)
        # Three failures in a row: 240 s after the latest end (a minute ago).
        self.assertTrue(175 <= caught.exception.retry_after_seconds <= 180)

    async def test_a_new_credential_is_not_spaced_by_the_old_ones_probes(self):
        now = self.scalar("SELECT clock_timestamp()")
        self.seed_probe("unavailable", now, secret_handle=handle(2))
        _, result = await self.probe()
        self.assertEqual(result.outcome, ProbeOutcome.CONNECTED)

    async def test_a_user_without_the_capability_may_not_probe(self):
        with self.assertRaises(ConnectionPermissionDeniedError) as caught:
            await self.service.start_probe(self.principal(self.user), CLAUDE, MANUAL)
        self.assertIs(caught.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
        self.assertEqual(self.probe_rows(), [])

    async def test_bad_arguments_are_refused_before_anything_is_read(self):
        for kind, trigger in (("other", MANUAL), (CLAUDE, "loop"), (CLAUDE, None)):
            with self.subTest(kind=kind, trigger=trigger):
                with self.assertRaises(InvalidConnectionInputError):
                    await self.service.start_probe(self.admin_principal, kind, trigger)
        with self.assertRaises(ConnectionPermissionDeniedError):
            await self.service.start_probe(object(), CLAUDE, MANUAL)

    # -- the end of the process ----------------------------------------------

    async def test_a_stopped_probe_is_cancelled_and_changes_nothing(self):
        self.hold_checks()
        ticket = await self.service.start_probe(self.admin_principal, CLAUDE, ATTACH)
        await self.wait_for(lambda: self.claude.started_checks, "the probe to start")
        await self.service.stop_probes()

        (probe,) = self.probe_rows()
        self.assertEqual((probe["state"], probe["outcome"]), ("finished", "cancelled"))
        self.assertEqual(self.connection_row(CLAUDE)["status"], "unavailable")
        result = await self.service.wait_probe(ticket.id)
        self.assertEqual(result.outcome, ProbeOutcome.CANCELLED)

    async def test_a_probe_stopped_before_its_task_ran_is_finished(self):
        # Codex review of #316: no yield between the start and the stop.
        ticket = await self.service.start_probe(self.admin_principal, CLAUDE, ATTACH)
        await self.service.stop_probes()

        (probe,) = self.probe_rows()
        self.assertEqual((probe["state"], probe["outcome"]), ("finished", "cancelled"))
        self.assertEqual(self.claude.health_checks, 0)
        result = await self.service.wait_probe(ticket.id)
        self.assertEqual(result.outcome, ProbeOutcome.CANCELLED)
        # The credential can be probed again at once.
        _, again = await self.probe(REPLACE)
        self.assertEqual(again.outcome, ProbeOutcome.CONNECTED)

    async def test_a_stop_while_the_end_is_written_still_writes_it(self):
        # Codex review of #316: the probe ended, its end waits for the
        # connection's lock, and the shutdown comes then.
        gate = self.hold_checks()
        ticket = await self.service.start_probe(self.admin_principal, CLAUDE, ATTACH)
        await self.wait_for(lambda: self.claude.started_checks, "the probe to start")
        _, transaction = self.hold_row_lock(
            "SELECT 1 FROM shared_connections WHERE id = :id FOR UPDATE",
            id=self.connection_id,
        )
        gate.set()
        await self.wait_for_lock_waiters(1)
        stopping = self.spawn(self.service.stop_probes())
        await asyncio.sleep(0.1)
        self.assertFalse(stopping.done())
        transaction.rollback()
        await stopping

        (probe,) = self.probe_rows()
        self.assertEqual((probe["state"], probe["outcome"]), ("finished", "connected"))
        self.assertEqual(self.connection_row(CLAUDE)["status"], "connected")
        result = await self.service.wait_probe(ticket.id)
        self.assertEqual(result.outcome, ProbeOutcome.CONNECTED)

    async def test_an_abandoned_probe_is_closed_by_the_reaper(self):
        now = self.scalar("SELECT clock_timestamp()")
        old = self.seed_probe(
            None, now - timedelta(seconds=PROBE_ABANDONED_AGE_SECONDS + 5)
        )
        young = self.seed_probe(
            None, now - timedelta(seconds=30), connection=uuid.uuid4()
        )
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        store = ConnectionStore(database)

        self.assertEqual(await store.reap_abandoned_probes(), (old,))
        states = {row["id"]: row["state"] for row in self.probe_rows()}
        self.assertEqual(states, {old: "abandoned", young: "running"})
        self.assertEqual(await store.reap_abandoned_probes(), ())
        self.assertEqual(self.connection_row(CLAUDE)["status"], "unavailable")

    # -- helpers -------------------------------------------------------------

    def seed_probe(
        self,
        outcome: str | None,
        at: datetime,
        *,
        secret_handle: str | None = None,
        connection: uuid.UUID | None = None,
    ) -> uuid.UUID:
        probe_id = uuid.uuid4()
        with self.engine.begin() as db:
            db.exec_driver_sql(
                "INSERT INTO connection_probes (id, connection_id, kind,"
                " secret_handle, trigger, state, outcome, started_at, finished_at,"
                " duration_ms) VALUES (%(id)s, %(connection)s, 'claude', %(handle)s,"
                " 'manual', %(state)s, %(outcome)s, %(at)s, %(end)s, %(ms)s)",
                {
                    "id": probe_id,
                    "connection": connection or self.connection_id,
                    "handle": secret_handle or handle(1),
                    "state": "running" if outcome is None else "finished",
                    "outcome": outcome,
                    "at": at,
                    "end": None if outcome is None else at,
                    "ms": None if outcome is None else 0,
                },
            )
        return probe_id


if __name__ == "__main__":
    unittest.main()
