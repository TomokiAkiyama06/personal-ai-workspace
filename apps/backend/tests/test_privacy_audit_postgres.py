"""The persistent audit sink and the production gate on a real PostgreSQL.

``build_research_broker`` (the production construction path) is used throughout:
what a provider receives, what ``audit_events`` holds, and in which order. Every
test has its own project id (``audit_events`` is append-only and shared), and none
expects an empty table. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import unittest
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import psycopg
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from paw_backend.authz.models import EXTERNAL_SEND_WITHHELD_KEYS
from paw_backend.research.privacy import (
    PostgresExternalSendAudit,
    PrivacyGate,
    PrivacyInput,
    PrivacyRefusal,
    RefusalReason,
    build_privacy_gate,
    build_research_broker,
)
from paw_backend.research.providers import (
    ProviderKind,
    ResearchBroker,
    ResearchRequest,
)

from .privacy_audit_support import (
    NOW,
    PostgresAuditTestCase,
    TrackingDatabase,
    canary_never_stored,
    fingerprint_of,
    make_record,
)
from .privacy_support import guarded, private_source, public, secret
from .research_support import SECRET, docs, fixed_clock, github, hit, registry_of, web
from .support import make_settings

PRIVATE_TEXT = "the billing service retries payment three times before alerting finance"
SECRET_VALUE = "hunter2-CANARY-5d3e"


class GateTestCase(PostgresAuditTestCase):
    def broker_of(self, *providers, **options) -> ResearchBroker:
        return build_research_broker(
            registry_of(*providers), self.database, clock=fixed_clock(), **options
        )

    async def search(
        self, broker: ResearchBroker, draft: str, context=(), project_id=None
    ):
        return await guarded(
            broker.gather(
                ResearchRequest(draft),
                preflight_input=PrivacyInput(
                    list(context), project_id or self.project_id
                ),
            )
        )


class SendFlowTest(GateTestCase):
    async def test_the_row_exists_before_the_provider_is_called(self):
        seen: list[int] = []

        async def look() -> None:
            seen.append(len(await self.rows()))

        provider = web(hits=[hit()], before_search=look)
        await self.search(self.broker_of(provider), "python asyncio timeout")
        self.assertEqual(seen, [1])  # audit first, then send
        self.assertEqual(len(provider.search_calls), 1)
        self.assertEqual(len(await self.rows()), 1)

    async def test_the_row_holds_the_record_and_never_the_query(self):
        provider = web(hits=[hit()])
        context = [
            private_source(PRIVATE_TEXT),
            secret(SECRET_VALUE),
            public("asyncio taskgroup documentation"),
        ]
        draft = f"retry payments {PRIVATE_TEXT} stripe {SECRET_VALUE} asyncio taskgroup"
        await self.search(self.broker_of(provider), draft, context)
        ((query, _),) = provider.search_calls
        (row,) = await self.rows()
        self.assertEqual(
            {
                key: row[key]
                for key in (
                    "actor_id",
                    "actor_role",
                    "agent_id",
                    "action",
                    "resource_kind",
                    "resource_id",
                    "project_id",
                    "repo_id",
                    "repo_acl",
                    "decision",
                    "reason",
                    "old_role",
                    "new_role",
                    "client_request_id",
                )
            },
            {
                "actor_id": None,
                "actor_role": "system",
                "agent_id": None,
                "action": "research.external_send",
                "resource_kind": "research_query",
                "resource_id": None,
                "project_id": self.project_id,
                "repo_id": None,
                "repo_acl": None,
                "decision": "allow",
                "reason": "send_authorized",
                "old_role": None,
                "new_role": None,
                "client_request_id": None,
            },
        )
        self.assertEqual(
            row["details"],
            {
                "query_fingerprint": fingerprint_of(query),
                "query_chars": len(query),
                "provider_kinds": ["web"],
                "withheld": {
                    "private_source": 1,
                    "private_memory": 0,
                    "raw_conversation": 0,
                    "secret": 1,
                },
                "credentials_removed": 0,
                "pieces_matched": 2,
                "abstractions": 0,
                "truncated": False,
            },
        )
        # No column of the row holds the query, the draft or any context text.
        self.assertEqual(
            canary_never_stored(
                [row],
                query,
                draft,
                PRIVATE_TEXT,
                SECRET_VALUE,
                "billing service",
                "retry payments",
                "asyncio taskgroup",
            ),
            [],
        )

    async def test_the_fingerprint_is_the_sha256_of_the_query_the_provider_got(self):
        provider = web(hits=[hit()])
        await self.search(
            self.broker_of(provider),
            "how does asyncio.TaskGroup cancel siblings  in 3.13.15",
        )
        ((query, _),) = provider.search_calls
        self.assertEqual(query, "how does asyncio.TaskGroup cancel siblings in 3.13")
        (row,) = await self.rows()
        self.assertEqual(row["details"]["query_fingerprint"], fingerprint_of(query))
        self.assertEqual(row["details"]["query_chars"], len(query))
        # ... and the database can recompute it: it is exactly sha256 of the text.
        self.assertEqual(
            row["details"]["query_fingerprint"],
            "sha256:"
            + await self.owner_scalar(
                "SELECT encode(sha256(convert_to(:q, 'UTF8')), 'hex')", q=query
            ),
        )

    async def test_occurred_at_is_the_gate_clock_and_recorded_at_the_database_clock(
        self,
    ):
        provider = web(hits=[hit()])
        await self.search(self.broker_of(provider), "python asyncio")
        (row,) = await self.rows()
        self.assertEqual(row["occurred_at"], NOW)
        skew = await self.owner_scalar(
            "SELECT abs(extract(epoch FROM now() - recorded_at)) FROM audit_events "
            "WHERE project_id = :p",
            p=self.project_id,
        )
        self.assertLess(skew, 120)
        self.assertGreater(row["recorded_at"], datetime(2026, 9, 25, tzinfo=UTC))

    async def test_every_provider_kind_that_is_queried_is_recorded_in_kind_order(self):
        providers = (github(hits=[hit()]), web(hits=[hit()]), docs(hits=[hit()]))
        await self.search(self.broker_of(*providers), "python asyncio")
        (row,) = await self.rows()
        self.assertEqual(row["details"]["provider_kinds"], ["web", "docs", "github"])
        for provider in providers:
            self.assertEqual(len(provider.search_calls), 1)

    async def test_only_the_kinds_that_are_asked_for_are_recorded(self):
        web_provider, docs_provider = web(hits=[hit()]), docs(hits=[hit()])
        broker = self.broker_of(web_provider, docs_provider)
        await guarded(
            broker.gather(
                ResearchRequest("python asyncio", kinds=frozenset({ProviderKind.DOCS})),
                preflight_input=PrivacyInput([], self.project_id),
            )
        )
        (row,) = await self.rows()
        self.assertEqual(row["details"]["provider_kinds"], ["docs"])
        self.assertEqual(web_provider.search_calls, [])

    async def test_the_removed_counts_are_recorded(self):
        provider = web(hits=[hit()])
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        draft = f"asyncio token {token} at /srv/app/main.py and 3.13.15"
        await self.search(self.broker_of(provider), draft)
        (row,) = await self.rows()
        details = row["details"]
        self.assertEqual(details["credentials_removed"], 1)
        self.assertEqual(details["abstractions"], 2)  # the path and the version
        self.assertEqual(
            details["withheld"], dict.fromkeys(EXTERNAL_SEND_WITHHELD_KEYS, 0)
        )
        self.assertEqual(canary_never_stored([row], token, "/srv/app"), [])

    async def test_a_refused_request_writes_no_row_and_calls_no_provider(self):
        provider = web(hits=[hit()])
        broker = self.broker_of(provider)
        for label, request in {
            "no context object": lambda: broker.gather(
                ResearchRequest("python asyncio")
            ),
            "an unclassified piece": lambda: broker.gather(
                ResearchRequest("python asyncio"),
                preflight_input=PrivacyInput(["plain text"], self.project_id),
            ),
            "nothing left of the query": lambda: broker.gather(
                ResearchRequest("/srv/app/main.py"),
                preflight_input=PrivacyInput([], self.project_id),
            ),
        }.items():
            with self.subTest(case=label):
                with self.assertRaises(PrivacyRefusal):
                    await guarded(request())
        self.assertEqual(await self.rows(), [])
        self.assertEqual(provider.search_calls, [])

    async def test_two_sends_of_the_same_query_are_two_rows(self):
        broker = self.broker_of(web(hits=[hit()]))
        for _ in range(2):
            await self.search(broker, "python asyncio")
        first, second = await self.rows()
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["correlation_id"], second["correlation_id"])
        self.assertEqual(first["details"], second["details"])

    async def test_a_hostile_draft_and_context_are_handled_as_data(self):
        provider = web(hits=[hit()])
        hostile = "'); DROP TABLE audit_events; -- \u202e bidi \U0001f600"
        context = [
            private_source("'); DELETE FROM audit_events; --" + PRIVATE_TEXT),
            secret("Robert'); DROP TABLE students;--"),
        ]
        await self.search(
            self.broker_of(provider), f"python asyncio {hostile}", context
        )
        (row,) = await self.rows()
        self.assertEqual(row["action"], "research.external_send")
        # The table is intact and the statement text is nowhere in the row.
        self.assertEqual(
            await self.owner_scalar("SELECT to_regclass('audit_events')::text"),
            "audit_events",
        )
        self.assertEqual(
            canary_never_stored([row], "DROP TABLE", "DELETE FROM", PRIVATE_TEXT), []
        )
        self.assertEqual(
            row["details"]["query_fingerprint"],
            fingerprint_of(provider.search_calls[0][0]),
        )

    async def test_a_project_without_a_projects_row_is_still_recorded(self):
        # audit_events has no foreign key: an audit row must not depend on a table
        # that can lose its row (Decision 0008 keeps tombstones, but the rule holds).
        provider = web(hits=[hit()])
        await self.search(self.broker_of(provider), "python asyncio")
        self.assertEqual(len(await self.rows()), 1)

    async def test_the_gate_of_the_factory_records_an_authorised_send_directly(self):
        gate = build_privacy_gate(self.database, clock=lambda: NOW)
        minimized = await guarded(
            gate.authorize(
                "python asyncio",
                [public("asyncio docs")],
                project_id=self.project_id,
                provider_kinds=frozenset({ProviderKind.WEB}),
            )
        )
        (row,) = await self.rows()
        self.assertEqual(row["details"]["query_fingerprint"], minimized.fingerprint)
        self.assertEqual(row["occurred_at"], NOW)


class ProductionSinkTest(GateTestCase):
    """The production path uses the persistent sink and the abortable write."""

    async def test_the_default_deadline_is_the_gates_five_seconds(self):
        await self.search(self.broker_of(web(hits=[hit()])), "python asyncio")
        self.assertEqual(self.database.timeouts, [5.0])

    async def test_an_explicit_deadline_reaches_the_abortable_write(self):
        broker = self.broker_of(web(hits=[hit()]), audit_timeout_seconds=2.5)
        await self.search(broker, "python asyncio")
        self.assertEqual(self.database.timeouts, [2.5])

    async def test_the_write_is_one_statement_on_a_dedicated_connection(self):
        await self.search(self.broker_of(web(hits=[hit()])), "python asyncio")
        self.assertEqual(self.database.started, 1)
        self.assertIsNone(self.database._engine)  # the pool was never involved
        self.assertEqual(self.database._probe_connections, {})

    async def test_no_slot_or_connection_is_left_behind(self):
        broker = self.broker_of(web(hits=[hit()]))
        for _ in range(3):
            await self.search(broker, "python asyncio")
        self.assertEqual(self.database.running, 0)
        self.assertEqual(self.database._probes, set())
        self.assertFalse(self.database._abortable_slots.locked())


class AppendOnlyTest(GateTestCase):
    async def test_the_written_row_cannot_be_changed_or_removed(self):
        await self.search(self.broker_of(web(hits=[hit()])), "python asyncio")
        (before,) = await self.rows()
        for sql in (
            "UPDATE audit_events SET details = '{}'::jsonb WHERE project_id = :p",
            "UPDATE audit_events SET reason = 'x' WHERE project_id = :p",
            "DELETE FROM audit_events WHERE project_id = :p",
            "TRUNCATE audit_events",
        ):
            with self.subTest(sql=sql):
                async with self.reader.session() as session:
                    with self.assertRaises(DBAPIError) as caught:
                        await session.execute(text(sql), {"p": self.project_id})
                        await session.commit()
                self.assertEqual(caught.exception.orig.sqlstate, "23001")
        self.assertEqual(await self.rows(), [before])

    async def test_the_triggers_of_the_table_are_all_still_enabled_always(self):
        await self.search(self.broker_of(web(hits=[hit()])), "python asyncio")
        triggers = await self.owner_scalar(
            "SELECT string_agg(tgname || ':' || tgenabled::text, ',' ORDER BY tgname) "
            "FROM pg_trigger WHERE tgrelid = 'audit_events'::regclass "
            "AND NOT tgisinternal"
        )
        self.assertEqual(
            triggers,
            "tr_audit_events_force_recorded_at:A,tr_audit_events_reject_truncate:A,"
            "tr_audit_events_reject_update_delete:A",
        )


class FailClosedTest(GateTestCase):
    """A write that is not confirmed means nothing is sent."""

    async def refused_send(self, broker, provider, expected=RefusalReason.AUDIT_FAILED):
        with self.assertLogs("paw_backend.research.privacy", level="WARNING") as logs:
            with self.assertRaises(PrivacyRefusal) as caught:
                await self.search(broker, "python asyncio")
        self.assertIs(caught.exception.reason, expected)
        self.assertEqual(provider.search_calls, [])  # nothing was sent
        (line,) = logs.output
        for secret_text in (SECRET, "python", "password", "5432"):
            self.assertNotIn(secret_text, line)
        return line

    async def test_an_unreachable_database_refuses_the_send(self):
        provider = web(hits=[hit()])
        # Nothing listens on port 1.
        database = TrackingDatabase(
            make_settings(
                database_url="postgresql://paw:hunter2-not-logged@127.0.0.1:1/paw"
            )
        )
        self.addAsyncCleanup(database.dispose)
        broker = build_research_broker(
            registry_of(provider), database, clock=fixed_clock()
        )
        line = await self.refused_send(broker, provider)
        self.assertNotIn("hunter2", line)
        # The driver's error is not one of the fixed, loggable types.
        self.assertIn("exception_type=adapter_error", line)
        self.assertEqual(database.running, 0)

    async def test_a_database_that_rejects_the_row_refuses_the_send_and_stores_nothing(
        self,
    ):
        # A bug in this module must not be able to store text: the CHECK of
        # migration 0087 refuses a row of this action with a key of its own.
        provider = web(hits=[hit()])
        broker = self.broker_of(provider)
        good = external_send_details_for("python asyncio")

        def with_query_text(_read):
            return {**good, "query": "python asyncio"}

        with patch(
            "paw_backend.research.privacy.audit._checked_details", with_query_text
        ):
            await self.refused_send(broker, provider)
        self.assertEqual(await self.rows(), [])

    async def test_the_schema_check_is_what_refuses_a_row_with_text_in_it(self):
        record = make_record(self.project_id)
        sink = PostgresExternalSendAudit(self.database)
        good = external_send_details_for("python asyncio")
        for label, details in {
            "the query as a key": {**good, "query": "python asyncio"},
            "the query as the fingerprint": {**good, "query_fingerprint": "python"},
            "the query as a kind": {**good, "provider_kinds": ["python asyncio"]},
        }.items():
            with self.subTest(case=label):
                with patch(
                    "paw_backend.research.privacy.audit._checked_details",
                    lambda _read, details=details: details,
                ):
                    with self.assertRaises(psycopg.errors.CheckViolation) as caught:
                        await sink.record(record)
                self.assertEqual(
                    caught.exception.diag.constraint_name,
                    "ck_audit_events_external_send_details",
                )
        self.assertEqual(await self.rows(), [])

    async def test_a_late_write_is_a_refusal_not_a_late_send(self):
        # The sink is given more time than the gate: the gate's deadline decides.
        provider = web(hits=[hit()])

        class Slow(PostgresExternalSendAudit):
            async def record(self, record):
                await asyncio.sleep(30)

        gate = PrivacyGate(
            Slow(self.database), clock=fixed_clock(), audit_timeout_seconds=0.2
        )
        broker = ResearchBroker(
            registry_of(provider), clock=fixed_clock(), preflight=gate
        )
        await self.refused_send(broker, provider)


def external_send_details_for(query: str) -> dict:
    """A valid ``details`` object for ``query`` (the shape of migration 0087)."""
    return {
        "query_fingerprint": fingerprint_of(query),
        "query_chars": len(query),
        "provider_kinds": ["web"],
        "withheld": dict.fromkeys(EXTERNAL_SEND_WITHHELD_KEYS, 0),
        "credentials_removed": 0,
        "pieces_matched": 0,
        "abstractions": 0,
        "truncated": False,
    }


class ConcurrencyTest(GateTestCase):
    async def test_concurrent_sends_are_all_recorded_within_the_connection_cap(self):
        database = self.new_database(database_pool_size=3)
        provider = web(hits=[hit()])
        broker = build_research_broker(
            registry_of(provider), database, clock=fixed_clock()
        )
        queries = [f"python asyncio topic{number}" for number in range(24)]
        results = await guarded(
            asyncio.gather(*(self.search(broker, query) for query in queries))
        )
        self.assertEqual(len(results), 24)
        rows = await self.rows()
        self.assertEqual(len(rows), 24)
        self.assertEqual(
            sorted(row["details"]["query_fingerprint"] for row in rows),
            sorted(fingerprint_of(query) for query in queries),
        )
        self.assertEqual(len({row["id"] for row in rows}), 24)
        self.assertEqual(len({row["correlation_id"] for row in rows}), 24)
        self.assertEqual(
            sorted(query for query, _ in provider.search_calls), sorted(queries)
        )
        # The dedicated connections never exceeded the pool size ...
        self.assertEqual(database.started, 24)
        self.assertEqual(database.peak, 3)  # all three slots were in use at once
        # ... and every slot was given back.
        self.assertEqual(database.running, 0)
        self.assertFalse(database._abortable_slots.locked())

    async def test_concurrent_sends_of_one_query_keep_every_row(self):
        broker = self.broker_of(web(hits=[hit()]))
        await guarded(
            asyncio.gather(*(self.search(broker, "python asyncio") for _ in range(12)))
        )
        rows = await self.rows()
        self.assertEqual(len(rows), 12)
        self.assertEqual(
            {row["details"]["query_fingerprint"] for row in rows},
            {fingerprint_of("python asyncio")},
        )
        self.assertEqual(len({row["id"] for row in rows}), 12)

    async def test_sends_of_different_projects_stay_apart(self):
        broker = self.broker_of(web(hits=[hit()]))
        projects = [uuid.uuid4() for _ in range(6)]
        await guarded(
            asyncio.gather(
                *(
                    self.search(broker, f"python asyncio v{index}", project_id=project)
                    for index, project in enumerate(projects)
                )
            )
        )
        for index, project in enumerate(projects):
            with self.subTest(project=index):
                (row,) = await self.rows(project)
                self.assertEqual(
                    row["details"]["query_fingerprint"],
                    fingerprint_of(f"python asyncio v{index}"),
                )

    async def test_a_cancelled_send_leaves_no_connection_and_no_slot(self):
        database = self.new_database(database_pool_size=1)
        broker = build_research_broker(
            registry_of(web(hits=[hit()])), database, clock=fixed_clock()
        )
        task = asyncio.ensure_future(self.search(broker, "python asyncio"))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        # The only slot comes back (the query is aborted or has finished) ...
        for _ in range(100):
            if not database._abortable_slots.locked():
                break
            await asyncio.sleep(0.02)
        self.assertFalse(database._abortable_slots.locked())
        # ... and the next send works.
        await self.search(broker, "python asyncio again")


class SinkDirectTest(GateTestCase):
    async def test_record_appends_a_row_and_returns_none(self):
        sink = PostgresExternalSendAudit(self.database, timeout_seconds=10)
        record = make_record(self.project_id, query="asyncio gather")
        self.assertIsNone(await sink.record(record))
        (row,) = await self.rows()
        self.assertEqual(
            row["details"]["query_fingerprint"], fingerprint_of("asyncio gather")
        )

    async def test_a_bad_record_reaches_no_database(self):
        sink = PostgresExternalSendAudit(self.database)
        record = make_record(self.project_id)
        object.__setattr__(record, "query_fingerprint", "python asyncio")
        with self.assertRaises(ValueError):
            await sink.record(record)
        self.assertEqual(self.database.started, 0)
        self.assertEqual(await self.rows(), [])

    async def test_the_sink_does_not_swallow_a_cancellation(self):
        sink = PostgresExternalSendAudit(self.database)
        task = asyncio.ensure_future(sink.record(make_record(self.project_id)))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
