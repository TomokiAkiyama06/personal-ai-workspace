"""``ResearchBroker.gather`` with the pre-flight, and the privacy gate.

A broker without a pre-flight refuses to search unless it was built with the
explicit opt-out ``unfiltered=True`` (then it behaves as in PAW-051); with a
``PrivacyGate`` no provider sees anything but the minimised, recorded query.
Most of these tests need ``rules.py``; the ones about the pre-flight hook
itself, and about the refusal of an unclassified context, do not.
"""

import asyncio
import logging
import unittest

from paw_backend.research.privacy import (
    ContextLabel,
    ContextPiece,
    InMemoryExternalSendAudit,
    PrivacyGate,
    PrivacyInput,
    PrivacyRefusal,
    RefusalReason,
    WithheldCounts,
    context_pieces_from_items,
)
from paw_backend.research.providers import (
    PreflightNotConfiguredError,
    PreflightRequiredError,
    ProviderKind,
    ProviderRegistry,
    ResearchBroker,
    ResearchRequest,
    SearchPreflight,
    SourceMetadata,
    SourceType,
    validate_preflight,
)

from .privacy_support import NOW, PROJECT_ID, guarded, private_source, secret
from .research_support import (
    SECRET,
    broker_of,
    docs,
    document,
    fixed_clock,
    github,
    hit,
    registry_of,
    web,
)

Reason = RefusalReason
PRIVATE_TEXT = "the billing service retries payment three times before alerting finance"


def gate_of(sink=None, **kwargs) -> tuple[PrivacyGate, InMemoryExternalSendAudit]:
    sink = sink if sink is not None else InMemoryExternalSendAudit()
    return PrivacyGate(sink, clock=lambda: NOW, **kwargs), sink


def broker_with(gate, *providers, **kwargs) -> ResearchBroker:
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return ResearchBroker(registry, clock=fixed_clock(), preflight=gate, **kwargs)


class RecordingPreflight:
    """A pre-flight that records what it was called with and returns ``result``."""

    def __init__(self, result=None, error: BaseException | None = None) -> None:
        self.calls: list[tuple] = []
        self.result = result
        self.error = error

    async def preflight(self, request, kinds, subject):
        self.calls.append((request, kinds, subject))
        if self.error is not None:
            raise self.error
        return request if self.result is None else self.result


class PreflightConstructionTest(unittest.TestCase):
    def test_a_valid_pre_flight_is_accepted(self):
        ResearchBroker(ProviderRegistry(), preflight=RecordingPreflight())
        ResearchBroker(ProviderRegistry(), preflight=gate_of()[0])
        ResearchBroker(ProviderRegistry(), preflight=None)

    def test_an_invalid_pre_flight_is_rejected(self):
        class Sync:
            def preflight(self, request, kinds, subject):
                return request

        class TwoArguments:
            async def preflight(self, request, kinds):
                return request

        class FourArguments:
            async def preflight(self, request, kinds, subject, extra):
                return request

        for value in (object(), "gate", Sync(), TwoArguments(), FourArguments(), 5):
            with self.subTest(value=type(value).__name__), self.assertRaises(TypeError):
                ResearchBroker(ProviderRegistry(), preflight=value)

    def test_validate_preflight_and_the_protocol(self):
        validate_preflight(RecordingPreflight())
        self.assertIsInstance(RecordingPreflight(), SearchPreflight)
        self.assertIsInstance(gate_of()[0], SearchPreflight)
        with self.assertRaises(TypeError):
            validate_preflight(object())

    def test_the_not_configured_error_has_a_fixed_message(self):
        self.assertEqual(
            str(PreflightNotConfiguredError()),
            "a pre-flight input was given but the broker has no pre-flight",
        )


class UnfilteredBrokerTest(unittest.IsolatedAsyncioTestCase):
    """``unfiltered=True``: the explicit opt-out, the behaviour of PAW-051."""

    async def test_gather_sends_the_query_as_it_is(self):
        provider = web(hits=[hit("https://example.com/a")])
        broker = broker_of(provider)
        request = ResearchRequest("python  asyncio /etc/passwd 1234567", max_results=4)
        result = await guarded(broker.gather(request))
        self.assertEqual(
            provider.search_calls, [("python  asyncio /etc/passwd 1234567", 4)]
        )
        self.assertEqual(
            [i.source.locator for i in result.items], ["https://example.com/a"]
        )
        self.assertEqual(result.providers_queried, 1)

    async def test_a_pre_flight_input_without_a_pre_flight_is_refused(self):
        provider = web(hits=[hit()])
        broker = broker_of(provider)
        subject = PrivacyInput([], PROJECT_ID)
        with self.assertRaises(PreflightNotConfiguredError):
            await guarded(
                broker.gather(ResearchRequest("python"), preflight_input=subject)
            )
        self.assertEqual(provider.search_calls, [])

    async def test_any_non_none_input_is_refused_without_a_pre_flight(self):
        provider = web()
        broker = broker_of(provider)
        for value in ("context", 0, [], {}, False):
            with (
                self.subTest(value=repr(value)),
                self.assertRaises(PreflightNotConfiguredError),
            ):
                await guarded(
                    broker.gather(ResearchRequest("python"), preflight_input=value)
                )
        self.assertEqual(provider.search_calls, [])

    async def test_the_error_is_raised_even_when_no_provider_matches(self):
        broker = broker_of(web())
        request = ResearchRequest("python", kinds=frozenset({ProviderKind.DOCS}))
        with self.assertRaises(PreflightNotConfiguredError):
            await guarded(broker.gather(request, preflight_input=object()))


class FailClosedWithoutAPreflightTest(unittest.IsolatedAsyncioTestCase):
    """A broker that is neither configured nor ``unfiltered`` sends nothing."""

    SECRET_QUERY = f"why does {SECRET} fail in /srv/billing/app.py"

    def unconfigured(self, *providers, **kwargs) -> ResearchBroker:
        return ResearchBroker(registry_of(*providers), clock=fixed_clock(), **kwargs)

    async def test_the_default_broker_refuses_and_calls_no_provider(self):
        provider = web(hits=[hit()])
        with self.assertRaises(PreflightRequiredError):
            await guarded(
                self.unconfigured(provider).gather(ResearchRequest(self.SECRET_QUERY))
            )
        self.assertEqual(provider.search_calls, [])

    async def test_every_spelling_of_no_pre_flight_refuses(self):
        for kwargs in ({}, {"preflight": None}, {"unfiltered": False}):
            provider_web, provider_docs = web(), docs()
            request = ResearchRequest(self.SECRET_QUERY)
            with self.subTest(kwargs=kwargs), self.assertRaises(PreflightRequiredError):
                await guarded(
                    self.unconfigured(provider_web, provider_docs, **kwargs).gather(
                        request
                    )
                )
            self.assertEqual(provider_web.search_calls, [])
            self.assertEqual(provider_docs.search_calls, [])

    async def test_it_refuses_whatever_the_registry_holds(self):
        # The refusal does not depend on which providers happen to match: a
        # misconfigured call site is found on its first call, not on the first
        # call that matches a provider.
        for providers, kinds in (
            ((), frozenset(ProviderKind)),
            ((web(),), frozenset({ProviderKind.DOCS})),
        ):
            with self.subTest(providers=len(providers)):
                request = ResearchRequest(self.SECRET_QUERY, kinds=kinds)
                with self.assertRaises(PreflightRequiredError):
                    await guarded(self.unconfigured(*providers).gather(request))

    async def test_a_pre_flight_input_does_not_make_it_pass(self):
        provider = web()
        subject = PrivacyInput([], PROJECT_ID)
        with self.assertRaises(PreflightNotConfiguredError):
            await guarded(
                self.unconfigured(provider).gather(
                    ResearchRequest(self.SECRET_QUERY), preflight_input=subject
                )
            )
        self.assertEqual(provider.search_calls, [])

    async def test_the_error_is_a_not_configured_error_with_a_fixed_message(self):
        with self.assertRaises(PreflightRequiredError) as caught:
            await guarded(
                self.unconfigured(web()).gather(ResearchRequest(self.SECRET_QUERY))
            )
        self.assertIsInstance(caught.exception, PreflightNotConfiguredError)
        message = str(caught.exception)
        self.assertEqual(message, str(PreflightRequiredError()))
        self.assertIn("unfiltered=True", message)
        self.assertNotIn(SECRET, message)
        self.assertNotIn("web-a", message)
        self.assertEqual(caught.exception.args, (message,))

    async def test_a_request_of_the_wrong_type_is_still_a_type_error(self):
        with self.assertRaises(TypeError) as caught:
            await guarded(self.unconfigured(web()).gather("python"))
        self.assertNotIsInstance(caught.exception, PreflightRequiredError)

    async def test_the_opt_out_sends_the_query_as_it_is(self):
        provider = web(hits=[hit()])
        broker = self.unconfigured(provider, unfiltered=True)
        await guarded(broker.gather(ResearchRequest(self.SECRET_QUERY)))
        self.assertEqual(provider.search_calls, [(self.SECRET_QUERY, 10)])

    async def test_a_pre_flight_input_is_still_refused_when_unfiltered(self):
        provider = web()
        with self.assertRaises(PreflightNotConfiguredError) as caught:
            await guarded(
                self.unconfigured(provider, unfiltered=True).gather(
                    ResearchRequest("python"), preflight_input=object()
                )
            )
        self.assertNotIsInstance(caught.exception, PreflightRequiredError)
        self.assertEqual(provider.search_calls, [])

    async def test_a_configured_broker_needs_no_opt_out(self):
        provider = web(hits=[hit()])
        gate, _ = gate_of()
        broker = broker_with(gate, provider)
        await guarded(
            broker.gather(
                ResearchRequest("python asyncio"),
                preflight_input=PrivacyInput([], PROJECT_ID),
            )
        )
        self.assertEqual(provider.search_calls, [("python asyncio", 10)])

    async def test_fetch_carries_no_query_and_works_on_any_broker(self):
        # Decision 0010: ``fetch`` is not gated. It passes the canonical locator
        # of an earlier result to the provider that returned it, so it neither
        # needs a pre-flight nor ``unfiltered=True``.
        provider = web(documents={"https://example.com/a": document(text="Body")})
        source = SourceMetadata(
            provider_kind=ProviderKind.WEB,
            provider_id="web-a",
            locator="https://example.com/a",
            title="Old",
            retrieved_at=NOW,
            content_hash="sha256:" + "0" * 64,
            source_type=SourceType.UNKNOWN,
        )
        result = await guarded(self.unconfigured(provider).fetch(source))
        self.assertEqual([item.text for item in result.items], ["Body"])
        self.assertEqual(provider.fetch_calls, ["https://example.com/a"])
        self.assertEqual(provider.search_calls, [])

    def test_the_opt_out_is_validated(self):
        registry = registry_of(web())
        gate, _ = gate_of()
        for value in (1, 0, "yes", None, [], "True"):
            with self.subTest(value=repr(value)), self.assertRaises(TypeError):
                ResearchBroker(registry, unfiltered=value)
        # A filter and the opt-out contradict each other.
        with self.assertRaises(ValueError):
            ResearchBroker(registry, preflight=gate, unfiltered=True)
        ResearchBroker(registry, unfiltered=True)
        ResearchBroker(registry, unfiltered=False)

    def test_the_opt_out_is_keyword_only(self):
        with self.assertRaises(TypeError):
            ResearchBroker(registry_of(), None, None, True)  # type: ignore[misc]


class PreflightHookTest(unittest.IsolatedAsyncioTestCase):
    """The broker's side of the contract, with a hand-written pre-flight."""

    async def test_it_gets_the_request_the_kinds_and_the_input_as_they_are(self):
        provider_web, provider_docs = web(), docs()
        preflight = RecordingPreflight()
        broker = broker_with(preflight, provider_web, provider_docs, github())
        request = ResearchRequest(
            "python", kinds=frozenset({ProviderKind.WEB, ProviderKind.DOCS})
        )
        subject = object()
        await guarded(broker.gather(request, preflight_input=subject))
        ((seen_request, kinds, seen_subject),) = preflight.calls
        self.assertIs(seen_request, request)
        self.assertEqual(kinds, frozenset({ProviderKind.WEB, ProviderKind.DOCS}))
        self.assertIsInstance(kinds, frozenset)
        self.assertIs(seen_subject, subject)

    async def test_none_is_passed_on_too(self):
        preflight = RecordingPreflight()
        broker = broker_with(preflight, web())
        await guarded(broker.gather(ResearchRequest("python")))
        self.assertEqual(preflight.calls[0][2], None)

    async def test_the_providers_get_the_query_it_returns(self):
        provider = web()
        request = ResearchRequest("draft query", max_results=3)
        rewritten = ResearchRequest("clean query", max_results=3)
        broker = broker_with(RecordingPreflight(result=rewritten), provider)
        await guarded(broker.gather(request))
        self.assertEqual(provider.search_calls, [("clean query", 3)])

    async def test_the_rewritten_request_is_used_for_everything_else_too(self):
        provider = web(
            hits=[hit("https://example.com/a"), hit("https://example.com/b")]
        )
        broker = broker_with(
            RecordingPreflight(result=ResearchRequest("clean", max_results=1)), provider
        )
        result = await guarded(broker.gather(ResearchRequest("draft", max_results=5)))
        self.assertEqual(provider.search_calls, [("clean", 1)])
        self.assertEqual(len(result.items), 1)

    async def test_a_refusal_stops_everything_and_reaches_the_caller(self):
        provider = web(hits=[hit()])
        error = PrivacyRefusal(Reason.EMPTY_QUERY)
        broker = broker_with(RecordingPreflight(error=error), provider)
        with self.assertRaises(PrivacyRefusal) as caught:
            await guarded(broker.gather(ResearchRequest("python")))
        self.assertIs(caught.exception, error)
        self.assertEqual(provider.search_calls, [])

    async def test_any_pre_flight_exception_stops_everything(self):
        provider = web()
        broker = broker_with(RecordingPreflight(error=RuntimeError(SECRET)), provider)
        with self.assertRaises(RuntimeError):
            await guarded(broker.gather(ResearchRequest("python")))
        self.assertEqual(provider.search_calls, [])

    async def test_a_pre_flight_must_return_a_request(self):
        provider = web()
        for value in ("clean query", {"query": "x"}, 5):
            with self.subTest(value=repr(value)):
                broker = broker_with(RecordingPreflight(result=value), provider)
                with self.assertRaises(TypeError):
                    await guarded(broker.gather(ResearchRequest("python")))
        self.assertEqual(provider.search_calls, [])

    async def test_no_selected_provider_means_no_pre_flight_call(self):
        preflight = RecordingPreflight(error=RuntimeError("must not run"))
        broker = broker_with(preflight, web())
        request = ResearchRequest("python", kinds=frozenset({ProviderKind.GITHUB}))
        result = await guarded(broker.gather(request))
        self.assertEqual(preflight.calls, [])
        self.assertEqual(result.providers_queried, 0)
        self.assertEqual(result.items, ())

    async def test_the_pre_flight_runs_before_any_provider_is_called(self):
        order = []

        class Ordered:
            async def preflight(self, request, kinds, subject):
                order.append("preflight")
                await asyncio.sleep(0)
                return request

        async def before_search():
            order.append("search")

        broker = broker_with(Ordered(), web(before_search=before_search))
        await guarded(broker.gather(ResearchRequest("python")))
        self.assertEqual(order, ["preflight", "search"])

    async def test_cancelling_during_the_pre_flight_propagates(self):
        started = asyncio.Event()

        class Slow:
            async def preflight(self, request, kinds, subject):
                started.set()
                await asyncio.Event().wait()

        provider = web()
        broker = broker_with(Slow(), provider)
        task = asyncio.ensure_future(broker.gather(ResearchRequest("python")))
        await guarded(started.wait())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await guarded(task)
        self.assertEqual(provider.search_calls, [])

    async def test_the_time_budget_starts_after_the_pre_flight(self):
        class Slow:
            async def preflight(self, request, kinds, subject):
                await asyncio.sleep(0.3)
                return request

        async def yield_to_the_loop():
            await asyncio.sleep(0.05)

        provider = web(hits=[hit()], before_search=yield_to_the_loop)
        broker = broker_with(Slow(), provider)
        result = await guarded(
            broker.gather(ResearchRequest("python", time_budget_seconds=0.2))
        )
        # The pre-flight took longer than the whole budget, yet the provider ran.
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.errors, ())


class GateThroughTheBrokerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    async def test_providers_only_see_the_minimised_query(self):
        gate, sink = gate_of()
        provider = web(hits=[hit("https://example.com/a")])
        broker = broker_with(gate, provider)
        draft = f"explain {PRIVATE_TEXT} in stripe at /srv/billing/app.py"
        context = [private_source(PRIVATE_TEXT), secret("hunter2!")]
        result = await guarded(
            broker.gather(
                ResearchRequest(draft, max_results=6),
                preflight_input=PrivacyInput(context, PROJECT_ID),
            )
        )
        self.assertEqual(provider.search_calls, [("explain in stripe at", 6)])
        self.assertEqual(len(result.items), 1)
        (recorded,) = sink.records
        self.assertEqual(recorded.provider_kinds, (ProviderKind.WEB,))
        self.assertEqual(recorded.project_id, PROJECT_ID)
        self.assertEqual(recorded.query_chars, len("explain in stripe at"))
        self.assertEqual(recorded.pieces_matched, 1)
        self.assertEqual(recorded.abstractions, 1)
        self.assertEqual(recorded.withheld, WithheldCounts(private_source=1, secret=1))

    async def test_the_send_is_recorded_before_a_provider_is_called(self):
        gate, sink = gate_of()
        seen = []

        async def before_search():
            seen.append(len(sink.records))

        broker = broker_with(
            gate, web(before_search=before_search), docs(before_search=before_search)
        )
        await guarded(
            broker.gather(
                ResearchRequest("python asyncio"),
                preflight_input=PrivacyInput([], PROJECT_ID),
            )
        )
        self.assertEqual(seen, [1, 1])
        self.assertEqual(len(sink.records), 1)

    async def test_one_record_per_gather_lists_the_kinds_that_are_queried(self):
        gate, sink = gate_of()
        broker = broker_with(gate, web(), docs(), github())
        subject = PrivacyInput([], PROJECT_ID)
        await guarded(broker.gather(ResearchRequest("python"), preflight_input=subject))
        await guarded(
            broker.gather(
                ResearchRequest("python", kinds=frozenset({ProviderKind.DOCS})),
                preflight_input=subject,
            )
        )
        await guarded(
            broker.gather(
                ResearchRequest(
                    "python", kinds=frozenset({ProviderKind.GITHUB, ProviderKind.WEB})
                ),
                preflight_input=subject,
            )
        )
        self.assertEqual(
            [r.provider_kinds for r in sink.records],
            [
                (ProviderKind.WEB, ProviderKind.DOCS, ProviderKind.GITHUB),
                (ProviderKind.DOCS,),
                (ProviderKind.WEB, ProviderKind.GITHUB),
            ],
        )

    async def test_kinds_without_a_registered_provider_are_not_listed(self):
        gate, sink = gate_of()
        broker = broker_with(gate, web())
        await guarded(
            broker.gather(
                ResearchRequest("python"),  # all kinds allowed, only web registered
                preflight_input=PrivacyInput([], PROJECT_ID),
            )
        )
        self.assertEqual(sink.records[0].provider_kinds, (ProviderKind.WEB,))

    async def test_a_missing_input_is_refused_and_nothing_is_sent(self):
        gate, sink = gate_of()
        provider = web(hits=[hit()])
        broker = broker_with(gate, provider)
        with self.assertRaises(PrivacyRefusal) as caught:
            await guarded(broker.gather(ResearchRequest("python")))
        self.assertIs(caught.exception.reason, Reason.UNCLASSIFIED_CONTEXT)
        self.assertEqual(provider.search_calls, [])
        self.assertEqual(sink.records, ())

    async def test_unclassified_context_is_refused_and_nothing_is_sent(self):
        gate, sink = gate_of()
        provider = web()
        broker = broker_with(gate, provider)
        for context in (
            ["raw text"],
            [None],
            ["raw", ContextPiece(ContextLabel.PUBLIC, "ok")],
        ):
            with self.subTest(context=repr(context)):
                with self.assertRaises(PrivacyRefusal) as caught:
                    await guarded(
                        broker.gather(
                            ResearchRequest("python"),
                            preflight_input=PrivacyInput(context, PROJECT_ID),
                        )
                    )
                self.assertIs(caught.exception.reason, Reason.UNCLASSIFIED_CONTEXT)
        self.assertEqual(provider.search_calls, [])
        self.assertEqual(sink.records, ())

    async def test_a_wrong_kind_of_input_is_refused(self):
        gate, sink = gate_of()
        provider = web()
        broker = broker_with(gate, provider)
        for value in ("public", [], {"context": []}, PROJECT_ID):
            with self.subTest(value=repr(value)):
                with self.assertRaises(PrivacyRefusal) as caught:
                    await guarded(
                        broker.gather(ResearchRequest("python"), preflight_input=value)
                    )
                self.assertIs(caught.exception.reason, Reason.UNCLASSIFIED_CONTEXT)
        self.assertEqual(provider.search_calls, [])

    async def test_an_empty_query_is_refused_and_nothing_is_sent(self):
        gate, sink = gate_of()
        provider = web()
        broker = broker_with(gate, provider)
        with self.assertRaises(PrivacyRefusal) as caught:
            await guarded(
                broker.gather(
                    ResearchRequest("/srv/billing/app.py"),
                    preflight_input=PrivacyInput([], PROJECT_ID),
                )
            )
        self.assertIs(caught.exception.reason, Reason.EMPTY_QUERY)
        self.assertEqual(provider.search_calls, [])
        self.assertEqual(sink.records, ())

    async def test_a_failing_audit_sink_means_nothing_is_sent(self):
        class Failing:
            async def record(self, record):
                raise RuntimeError(SECRET)

        gate = PrivacyGate(Failing(), clock=lambda: NOW)
        provider = web(hits=[hit()])
        broker = broker_with(gate, provider)
        with self.assertRaises(PrivacyRefusal) as caught:
            await guarded(
                broker.gather(
                    ResearchRequest("python asyncio"),
                    preflight_input=PrivacyInput([], PROJECT_ID),
                )
            )
        self.assertIs(caught.exception.reason, Reason.AUDIT_FAILED)
        self.assertEqual(provider.search_calls, [])
        self.assertNotIn(SECRET, str(caught.exception))

    async def test_with_no_provider_selected_nothing_is_gated_or_recorded(self):
        gate, sink = gate_of()
        broker = broker_with(gate, web())
        result = await guarded(
            broker.gather(
                ResearchRequest("python", kinds=frozenset({ProviderKind.DOCS}))
            )
        )
        self.assertEqual(result.providers_queried, 0)
        self.assertEqual(sink.records, ())

    async def test_provider_failures_still_work_as_before(self):
        gate, sink = gate_of()
        failing = web("web-bad", search_error=RuntimeError(SECRET))
        working = docs(hits=[hit("https://example.com/d")])
        broker = broker_with(gate, failing, working)
        result = await guarded(
            broker.gather(
                ResearchRequest("python"),
                preflight_input=PrivacyInput([], PROJECT_ID),
            )
        )
        self.assertEqual(len(result.items), 1)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(len(sink.records), 1)

    async def test_the_draft_never_reaches_a_provider(self):
        gate, _ = gate_of()
        providers = [web(), docs()]
        broker = broker_with(gate, *providers)
        draft = "pricing sheet for hunter2! customers"
        await guarded(
            broker.gather(
                ResearchRequest(draft),
                preflight_input=PrivacyInput([secret("hunter2!")], PROJECT_ID),
            )
        )
        for provider in providers:
            self.assertEqual(
                provider.search_calls, [("pricing sheet for customers", 10)]
            )


class PrivateItemsAsContextTest(unittest.IsolatedAsyncioTestCase):
    """A private hit found earlier protects its text in the next query."""

    async def test_text_of_a_private_result_is_not_sent_in_the_next_search(self):
        gate, sink = gate_of()
        private_hit = hit(
            "https://git.example.com/acme/billing/blob/main/NOTES.md",
            title="Billing internals",
            text=f"Internal design: {PRIVATE_TEXT}",
            private_source=True,
        )
        first_provider = web(hits=[private_hit])
        broker = broker_with(gate, first_provider)
        subject = PrivacyInput([], PROJECT_ID)
        first = await guarded(
            broker.gather(ResearchRequest("billing notes"), preflight_input=subject)
        )
        (item,) = first.items
        self.assertTrue(item.source.private_source)

        second_provider = docs()
        second_broker = broker_with(gate, second_provider)
        context = context_pieces_from_items(first.items)
        draft = f"how do {PRIVATE_TEXT} work in stripe about Billing internals"
        await guarded(
            second_broker.gather(
                ResearchRequest(draft),
                preflight_input=PrivacyInput(context, PROJECT_ID),
            )
        )
        ((query, _),) = second_provider.search_calls
        self.assertEqual(query, "how do work in stripe about")
        self.assertEqual(len(sink.records), 2)
        self.assertEqual(sink.records[1].pieces_matched, 2)
        self.assertEqual(sink.records[1].withheld, WithheldCounts(private_source=2))


if __name__ == "__main__":
    unittest.main()
