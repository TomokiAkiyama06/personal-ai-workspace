"""The production construction path and the sink's argument checks (issue #87).

No database server: a ``Database`` subclass records the statement the sink hands to
``execute_abortable`` (or fails in a way the test chooses). The real statement is
run in ``test_privacy_audit_postgres.py``.
"""

import asyncio
import inspect
import logging
import unittest
import uuid
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from paw_backend.authz.audit import AuditEvent
from paw_backend.db import Database, DatabaseDisposedError, DatabaseNotConfiguredError
from paw_backend.research.privacy import (
    DEFAULT_AUDIT_TIMEOUT_SECONDS,
    MAX_AUDIT_TIMEOUT_SECONDS,
    ContextLabel,
    InMemoryExternalSendAudit,
    PostgresExternalSendAudit,
    PrivacyGate,
    PrivacyInput,
    PrivacyRefusal,
    RefusalReason,
    build_privacy_gate,
    build_research_broker,
)
from paw_backend.research.privacy import audit as audit_module
from paw_backend.research.providers import (
    PreflightRequiredError,
    ProviderKind,
    ProviderRegistry,
    ResearchBroker,
    ResearchRequest,
)

from .privacy_audit_support import NOW, fingerprint_of, make_record, tampered_record
from .privacy_support import guarded, piece
from .research_support import fixed_clock, hit, registry_of, web
from .support import make_settings

SECRET = "hunter2-CANARY-77aa"
PROJECT = uuid.UUID("11111111-2222-3333-4444-555555555555")


class ScriptedDatabase(Database):
    """Records ``execute_abortable`` calls; raises ``error`` or waits if told to."""

    def __init__(self, *, error: BaseException | None = None, hang: bool = False):
        super().__init__(make_settings(database_url="postgresql://u:p@127.0.0.1:1/d"))
        self.error = error
        self.hang = hang
        self.calls: list[tuple[str, dict, float | None]] = []
        self.events: list[str] = []

    async def execute_abortable(self, sql, params=None, *, timeout_seconds=None):
        self.calls.append((sql, dict(params), timeout_seconds))
        self.events.append("write")
        if self.hang:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error


class UnconfiguredDatabase(Database):
    def __init__(self) -> None:
        super().__init__(make_settings())

    async def execute_abortable(self, *args, **kwargs):
        raise AssertionError("an unconfigured database is never written to")


class SinkArgumentsTest(unittest.TestCase):
    def test_the_database_must_be_a_database(self):
        for value in (None, "postgresql://u:p@h/d", object(), Database, 5):
            with self.subTest(value=repr(value)[:30]):
                with self.assertRaises(TypeError):
                    PostgresExternalSendAudit(value)

    def test_the_timeout_must_be_a_number_above_0_and_at_most_60(self):
        database = ScriptedDatabase()
        for label, value, expected in (
            ("None", None, TypeError),
            ("a str", "5", TypeError),
            ("bytes", b"5", TypeError),
            ("True", True, TypeError),
            ("False", False, TypeError),
            ("a list", [5], TypeError),
            ("zero", 0, ValueError),
            ("zero float", 0.0, ValueError),
            ("negative", -1, ValueError),
            ("negative float", -0.001, ValueError),
            ("just above the maximum", MAX_AUDIT_TIMEOUT_SECONDS + 0.001, ValueError),
            ("a huge int", 10**9, ValueError),
            ("nan", float("nan"), ValueError),
            ("inf", float("inf"), ValueError),
            ("-inf", float("-inf"), ValueError),
        ):
            with self.subTest(timeout=label):
                with self.assertRaises(expected):
                    PostgresExternalSendAudit(database, timeout_seconds=value)

    def test_a_timeout_in_range_is_accepted(self):
        database = ScriptedDatabase()
        for value in (0.001, 0.5, 1, 5, 5.0, 59.999, 60, 60.0):
            with self.subTest(timeout=value):
                PostgresExternalSendAudit(database, timeout_seconds=value)

    def test_the_timeout_is_a_keyword_only_argument(self):
        with self.assertRaises(TypeError):
            PostgresExternalSendAudit(ScriptedDatabase(), 5)  # type: ignore[misc]

    def test_the_sink_implements_the_audit_protocol_of_the_gate(self):
        sink = PostgresExternalSendAudit(ScriptedDatabase())
        self.assertTrue(inspect.iscoroutinefunction(sink.record))
        # The gate checks this at construction: it must accept the sink.
        PrivacyGate(sink)

    def test_unknown_arguments_are_refused(self):
        with self.assertRaises(TypeError):
            PostgresExternalSendAudit(ScriptedDatabase(), timeout=5)  # type: ignore[call-arg]


class SinkRecordTest(unittest.IsolatedAsyncioTestCase):
    async def test_it_writes_one_bound_statement_with_the_checked_row(self):
        database = ScriptedDatabase()
        sink = PostgresExternalSendAudit(database, timeout_seconds=2.5)
        record = make_record(PROJECT, query="python asyncio")
        self.assertIsNone(await sink.record(record))
        ((sql, params, timeout),) = database.calls
        self.assertEqual(timeout, 2.5)
        self.assertTrue(sql.startswith("INSERT INTO audit_events ("))
        # Every column is a bound parameter: no value is formatted into the text.
        columns = sql[sql.index("(") + 1 : sql.index(") VALUES")].split(", ")
        placeholders = sql[sql.index("VALUES (") + 8 : -1].split(", ")
        self.assertEqual(placeholders, [f"%({column})s" for column in columns])
        self.assertEqual(set(columns), set(params))
        self.assertEqual(
            set(columns),
            {"id", *(n for n in AuditEvent.model_fields if n != "event_id")}
            | {"details"},
        )
        for value in ("python asyncio", str(PROJECT), fingerprint_of("python asyncio")):
            self.assertNotIn(value, sql)
        self.assertEqual(params["project_id"], PROJECT)
        self.assertEqual(params["action"], "research.external_send")
        self.assertEqual(params["decision"], "allow")
        self.assertEqual(params["reason"], "send_authorized")
        self.assertEqual(params["actor_role"], "system")
        self.assertEqual(params["occurred_at"], NOW)
        self.assertIsInstance(params["id"], uuid.UUID)
        self.assertIsInstance(params["details"], Jsonb)
        self.assertEqual(
            params["details"].obj["query_fingerprint"], fingerprint_of("python asyncio")
        )

    async def test_it_never_writes_the_recorded_at_column(self):
        database = ScriptedDatabase()
        await PostgresExternalSendAudit(database).record(make_record(PROJECT))
        ((sql, params, _),) = database.calls
        self.assertNotIn("recorded_at", sql)
        self.assertNotIn("recorded_at", params)

    async def test_the_default_deadline_is_the_gates_default(self):
        database = ScriptedDatabase()
        await PostgresExternalSendAudit(database).record(make_record(PROJECT))
        self.assertEqual(database.calls[0][2], DEFAULT_AUDIT_TIMEOUT_SECONDS)
        self.assertEqual(DEFAULT_AUDIT_TIMEOUT_SECONDS, 5.0)

    async def test_every_send_is_a_row_of_its_own(self):
        database = ScriptedDatabase()
        sink = PostgresExternalSendAudit(database)
        record = make_record(PROJECT)
        for _ in range(3):
            await sink.record(record)
        ids = {params["id"] for _, params, _ in database.calls}
        correlations = {params["correlation_id"] for _, params, _ in database.calls}
        self.assertEqual((len(ids), len(correlations)), (3, 3))

    async def test_a_bad_record_is_refused_before_the_database_is_touched(self):
        database = ScriptedDatabase()
        sink = PostgresExternalSendAudit(database)
        bad = [
            None,
            "sha256:" + "a" * 64,
            {"query": "python"},
            tampered_record(query_fingerprint="python asyncio"),
            tampered_record(query_chars=0),
            tampered_record(provider_kinds=()),
            tampered_record(truncated=1),
            tampered_record(project_id=str(PROJECT)),
            tampered_record(recorded_at=datetime(2026, 9, 24, 12, 0)),
        ]
        for index, record in enumerate(bad):
            with self.subTest(record=index):
                with self.assertRaises((TypeError, ValueError)) as caught:
                    await sink.record(record)
                self.assertNotIn("python", str(caught.exception))
        self.assertEqual(database.calls, [])

    async def test_the_error_of_the_database_is_not_replaced_or_swallowed(self):
        for error in (
            psycopg.OperationalError(SECRET),
            TimeoutError(),
            DatabaseDisposedError("disposed"),
            RuntimeError(SECRET),
        ):
            with self.subTest(error=type(error).__name__):
                sink = PostgresExternalSendAudit(ScriptedDatabase(error=error))
                with self.assertRaises(type(error)) as caught:
                    await sink.record(make_record(PROJECT))
                self.assertIs(caught.exception, error)


class FactoryArgumentsTest(unittest.TestCase):
    def test_the_database_must_be_a_configured_database(self):
        for value in (None, "postgresql://u:p@h/d", object(), Database):
            with self.subTest(value=repr(value)[:30]):
                with self.assertRaises(TypeError):
                    build_privacy_gate(value)
                with self.assertRaises(TypeError):
                    build_research_broker(ProviderRegistry(), value)
        # A gate that could never record would refuse every send: not built at all.
        with self.assertRaises(DatabaseNotConfiguredError):
            build_privacy_gate(UnconfiguredDatabase())
        with self.assertRaises(DatabaseNotConfiguredError):
            build_research_broker(ProviderRegistry(), UnconfiguredDatabase())

    def test_the_registry_must_be_a_registry(self):
        for value in (None, [], {}, object(), "registry"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(TypeError):
                    build_research_broker(value, ScriptedDatabase())

    def test_the_deadline_and_the_clock_are_checked(self):
        database = ScriptedDatabase()
        for value, expected in (
            (None, TypeError),
            ("5", TypeError),
            (True, TypeError),
            (0, ValueError),
            (-1, ValueError),
            (61, ValueError),
            (float("nan"), ValueError),
        ):
            with self.subTest(deadline=value):
                with self.assertRaises(expected):
                    build_privacy_gate(database, audit_timeout_seconds=value)
                with self.assertRaises(expected):
                    build_research_broker(
                        ProviderRegistry(), database, audit_timeout_seconds=value
                    )
        for clock in ("now", 5, object()):
            with self.subTest(clock=repr(clock)):
                with self.assertRaises(TypeError):
                    build_privacy_gate(database, clock=clock)

    def test_the_arguments_after_the_database_are_keyword_only(self):
        with self.assertRaises(TypeError):
            build_privacy_gate(ScriptedDatabase(), 5)  # type: ignore[misc]
        with self.assertRaises(TypeError):
            build_research_broker(ProviderRegistry(), ScriptedDatabase(), 5)  # type: ignore[misc]

    def test_the_defaults_are_the_gates_own_default_deadline(self):
        for function in (build_privacy_gate, build_research_broker):
            with self.subTest(function=function.__name__):
                parameter = inspect.signature(function).parameters[
                    "audit_timeout_seconds"
                ]
                self.assertEqual(parameter.default, DEFAULT_AUDIT_TIMEOUT_SECONDS)
        parameter = inspect.signature(PostgresExternalSendAudit).parameters[
            "timeout_seconds"
        ]
        self.assertEqual(parameter.default, DEFAULT_AUDIT_TIMEOUT_SECONDS)

    def test_nothing_connects_while_building(self):
        database = ScriptedDatabase()
        build_privacy_gate(database)
        build_research_broker(ProviderRegistry(), database)
        self.assertEqual(database.calls, [])
        self.assertIsNone(database._engine)

    def test_the_products_have_the_expected_types(self):
        database = ScriptedDatabase()
        self.assertIsInstance(build_privacy_gate(database), PrivacyGate)
        self.assertIsInstance(
            build_research_broker(ProviderRegistry(), database), ResearchBroker
        )


class ProductionBrokerTest(unittest.IsolatedAsyncioTestCase):
    """What the built broker does with a scripted database."""

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def build(self, database, provider, **options):
        return build_research_broker(
            registry_of(provider), database, clock=fixed_clock(), **options
        )

    async def gather(self, broker, draft="python asyncio", context=()):
        return await guarded(
            broker.gather(
                ResearchRequest(draft),
                preflight_input=PrivacyInput(list(context), PROJECT),
            )
        )

    async def test_the_write_happens_before_the_provider_is_called(self):
        database = ScriptedDatabase()

        async def note() -> None:
            database.events.append("search")

        provider = web(hits=[hit()], before_search=note)
        await self.gather(self.build(database, provider))
        self.assertEqual(database.events, ["write", "search"])

    async def test_the_provider_gets_the_minimised_query_that_was_recorded(self):
        database = ScriptedDatabase()
        provider = web(hits=[hit()])
        private = "the billing service retries payment three times"
        await self.gather(
            self.build(database, provider),
            f"asyncio {private}",
            [piece(ContextLabel.PRIVATE_SOURCE, private)],
        )
        ((query, _),) = provider.search_calls
        self.assertEqual(query, "asyncio")
        details = database.calls[0][1]["details"].obj
        self.assertEqual(details["query_fingerprint"], fingerprint_of(query))
        self.assertNotIn(private, repr(database.calls[0][1]))

    async def test_every_failure_of_the_write_refuses_and_sends_nothing(self):
        for error in (
            psycopg.OperationalError(SECRET),
            psycopg.errors.InsufficientPrivilege(SECRET),
            psycopg.errors.CheckViolation(SECRET),
            TimeoutError(SECRET),
            DatabaseDisposedError(SECRET),
            DatabaseNotConfiguredError(SECRET),
            ConnectionResetError(SECRET),
            OSError(SECRET),
            RuntimeError(SECRET),
            ExceptionGroup(SECRET, [ValueError(SECRET)]),
        ):
            with self.subTest(error=type(error).__name__):
                database = ScriptedDatabase(error=error)
                provider = web(hits=[hit()])
                with self.assertRaises(PrivacyRefusal) as caught:
                    await self.gather(self.build(database, provider))
                self.assertIs(caught.exception.reason, RefusalReason.AUDIT_FAILED)
                for text in (str(caught.exception), repr(caught.exception)):
                    self.assertNotIn(SECRET, text)
                self.assertIsNone(caught.exception.__cause__)
                self.assertEqual(provider.search_calls, [])
                self.assertEqual(len(database.calls), 1)

    async def test_a_write_that_never_returns_is_a_refusal_at_the_gates_deadline(self):
        database = ScriptedDatabase(hang=True)
        provider = web(hits=[hit()])
        broker = self.build(database, provider, audit_timeout_seconds=0.2)
        with self.assertRaises(PrivacyRefusal) as caught:
            await self.gather(broker)
        self.assertIs(caught.exception.reason, RefusalReason.AUDIT_FAILED)
        self.assertEqual(provider.search_calls, [])
        # The deadline given to the database is the same one.
        self.assertEqual(database.calls[0][2], 0.2)

    async def test_a_refusal_before_the_audit_writes_nothing(self):
        database = ScriptedDatabase()
        provider = web(hits=[hit()])
        broker = self.build(database, provider)
        with self.assertRaises(PrivacyRefusal) as caught:
            await guarded(broker.gather(ResearchRequest("python asyncio")))
        self.assertIs(caught.exception.reason, RefusalReason.UNCLASSIFIED_CONTEXT)
        self.assertEqual(database.calls, [])
        self.assertEqual(provider.search_calls, [])

    async def test_the_built_broker_is_never_unfiltered(self):
        database = ScriptedDatabase()
        provider = web(hits=[hit()])
        broker = self.build(database, provider)
        self.assertFalse(broker._unfiltered)
        self.assertIsNotNone(broker._preflight)
        with self.assertRaises(ValueError):  # the opt-out cannot be added on top
            ResearchBroker(
                registry_of(provider), preflight=broker._preflight, unfiltered=True
            )

    async def test_a_broker_without_the_gate_still_refuses_to_search(self):
        provider = web(hits=[hit()])
        broker = ResearchBroker(registry_of(provider), clock=fixed_clock())
        with self.assertRaises(PreflightRequiredError):
            await guarded(broker.gather(ResearchRequest("python asyncio")))
        self.assertEqual(provider.search_calls, [])

    async def test_the_in_memory_sink_stays_available_for_tests(self):
        sink = InMemoryExternalSendAudit()
        gate = PrivacyGate(sink, clock=lambda: NOW)
        minimized = await guarded(
            gate.authorize(
                "python asyncio",
                [],
                project_id=PROJECT,
                provider_kinds=frozenset({ProviderKind.WEB}),
            )
        )
        self.assertEqual(len(sink.records), 1)
        self.assertEqual(sink.records[0].query_fingerprint, minimized.fingerprint)


class ModuleShapeTest(unittest.TestCase):
    def test_the_columns_are_the_event_fields_and_the_details(self):
        self.assertEqual(
            audit_module._COLUMNS,
            (
                "id",
                "correlation_id",
                "occurred_at",
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
                "details",
            ),
        )


if __name__ == "__main__":
    unittest.main()
