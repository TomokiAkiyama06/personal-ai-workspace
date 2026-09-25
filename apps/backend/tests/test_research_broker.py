"""``ResearchBroker``: fan-out, failure isolation, timeouts, unified results.

Timing: providers that must fail by timeout hang forever (they wait on an event
that is never set), the timeout that cuts them off is 0.3 s, and every provider
that must succeed answers immediately. Nothing asserts an elapsed time except
"much less than the 60 s that an unbounded wait would take"; a hung
implementation is stopped by a 30 s guard instead of blocking CI.
"""

import asyncio
import json
import time
import unittest
from datetime import UTC, datetime, timedelta, timezone

from paw_backend.research.providers import (
    InvalidLocatorError,
    InvalidProviderResponseError,
    ProviderFailure,
    ProviderInterfaceError,
    ProviderKind,
    ProviderRegistry,
    ResearchBroker,
    ResearchError,
    ResearchErrorCode,
    ResearchItem,
    ResearchRequest,
    ResearchResult,
    SourceMetadata,
    SourceType,
    StaticProvider,
    classify_failure,
    compute_content_hash,
)
from paw_backend.research.providers.broker import (
    ADAPTER_ERROR,
    LOGGED_EXCEPTION_TYPES,
    log_type_name,
)

from .research_support import (
    BASE_EXCEPTIONS,
    FORGED,
    GUARD_SECONDS,
    NOW,
    SECRET,
    SHORT_TIMEOUT,
    Tripwire,
    broker_of,
    docs,
    document,
    fixed_clock,
    github,
    guarded,
    hit,
    hostile_containers,
    hostile_failures,
    hostile_named_exceptions,
    malformed_documents,
    malformed_hits,
    other_tasks,
    raising_timezone,
    registry_of,
    web,
)

LOGGER = "paw_backend.research.providers"
JST = timezone(timedelta(hours=9))
Code = ResearchErrorCode


def request(query: str = "python asyncio", **kwargs) -> ResearchRequest:
    return ResearchRequest(query, **kwargs)


def urls(result: ResearchResult) -> list[str]:
    return [item.source.locator for item in result.items]


def error(provider_id: str, kind: ProviderKind, code: ResearchErrorCode):
    return ResearchError(provider_id, kind, code)


class EvilError(Exception):
    """An exception whose text cannot be read (and would be a secret if it could)."""

    def __str__(self):
        raise AssertionError("the exception text must never be read")

    def __repr__(self):
        raise AssertionError("the exception text must never be read")


class ClassifyFailureTest(unittest.TestCase):
    def test_provider_failure_keeps_its_code(self):
        for code in ResearchErrorCode:
            with self.subTest(code=code):
                self.assertIs(classify_failure(ProviderFailure(code)), code)

    def test_timeout_errors_are_timeouts(self):
        self.assertIs(classify_failure(TimeoutError()), Code.TIMEOUT)
        self.assertIs(classify_failure(TimeoutError(SECRET)), Code.TIMEOUT)

    def test_everything_else_is_internal_error(self):
        for exc in (
            RuntimeError(SECRET),
            ValueError(SECRET),
            KeyError(SECRET),
            ConnectionRefusedError(SECRET),
            OSError(SECRET),
            ExceptionGroup(SECRET, [ValueError("x")]),
            Exception(),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertIs(classify_failure(exc), Code.INTERNAL_ERROR)

    def test_a_forged_code_never_becomes_the_error_code(self):
        failure = ProviderFailure(Code.RATE_LIMITED)
        failure.code = f"leaked {SECRET}"
        self.assertIs(classify_failure(failure), Code.INTERNAL_ERROR)

    def test_the_exception_text_is_never_read(self):
        self.assertIs(classify_failure(EvilError()), Code.INTERNAL_ERROR)

    def test_only_the_enum_is_ever_returned(self):
        for exc in (RuntimeError(), TimeoutError(), ProviderFailure(Code.NOT_FOUND)):
            self.assertIsInstance(classify_failure(exc), ResearchErrorCode)

    def test_an_object_that_claims_to_be_a_code_is_not_a_code(self):
        class Impostor:
            value = "rate_limited"

            @property
            def __class__(self):
                return ResearchErrorCode

        self.assertIsInstance(Impostor(), ResearchErrorCode)  # what isinstance says
        with self.assertRaises(TypeError):
            ProviderFailure(Impostor())
        failure = ProviderFailure(Code.RATE_LIMITED)
        failure.code = Impostor()
        self.assertIs(classify_failure(failure), Code.INTERNAL_ERROR)

    def test_hooks_of_a_failure_subclass_are_never_run(self):
        # The code is read from ``ProviderFailure``'s own slot, once: a property,
        # ``__getattribute__`` or ``__class__`` of a subclass can neither raise
        # nor lie. A failure whose slot is unset or forged is a generic failure.
        calls = Tripwire()
        for label, (failure, expected) in hostile_failures(calls).items():
            with self.subTest(failure=label), calls.armed():
                self.assertIs(classify_failure(failure), expected)
        self.assertEqual(calls, [])


class ConstructionTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_registry_must_be_a_provider_registry(self):
        for registry in (None, [], {}, "registry"):
            with self.subTest(registry=registry), self.assertRaises(TypeError):
                ResearchBroker(registry)

    async def test_gather_needs_a_research_request(self):
        broker = broker_of(web())
        for value in ("python", None, {"query": "python"}):
            with self.subTest(value=value), self.assertRaises(TypeError):
                await guarded(broker.gather(value))

    async def test_fetch_needs_a_source_and_a_valid_budget(self):
        broker = broker_of(web())
        for value in ("https://example.com/", None, {}):
            with self.subTest(value=value), self.assertRaises(TypeError):
                await guarded(broker.fetch(value))
        source = SourceMetadata(
            ProviderKind.WEB,
            "web-a",
            "https://example.com/",
            "t",
            NOW,
            "sha256:" + "0" * 64,
        )
        for budget in (0, -1, 120.5, float("nan"), float("inf")):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                await guarded(broker.fetch(source, time_budget_seconds=budget))
        for budget in (True, "5", None):
            with self.subTest(budget=budget), self.assertRaises(TypeError):
                await guarded(broker.fetch(source, time_budget_seconds=budget))


class UnconvertibleTimestampTest(unittest.IsolatedAsyncioTestCase):
    """A time beyond UTC's range is the provider's invalid response, not a crash."""

    @staticmethod
    def far():
        return datetime.max.replace(tzinfo=timezone(timedelta(hours=-1)))

    async def test_gather_reports_invalid_response_instead_of_raising(self):
        bad = hit()
        object.__setattr__(bad, "published_at", self.far())
        provider = web("web-bad", hits=[bad])
        good = docs("docs-ok", hits=[hit("https://example.com/ok")])

        result = await guarded(broker_of(provider, good).gather(request()))

        self.assertEqual(
            [(e.provider_id, e.code) for e in result.errors],
            [("web-bad", ResearchErrorCode.INVALID_RESPONSE)],
        )
        self.assertEqual([i.source.provider_id for i in result.items], ["docs-ok"])

    async def test_fetch_reports_invalid_response_instead_of_raising(self):
        bad = document()
        object.__setattr__(bad, "published_at", self.far())
        provider = web(documents={"https://example.com/a": bad})

        result = await guarded(broker_of(provider).fetch(source_of()))

        self.assertEqual(result.items, ())
        self.assertEqual(
            [(e.provider_id, e.code) for e in result.errors],
            [("web-a", ResearchErrorCode.INVALID_RESPONSE)],
        )


class SynchronousHookBaseExceptionTest(unittest.IsolatedAsyncioTestCase):
    """A ``BaseException`` out of a hook that runs synchronously is the adapter's.

    ``CancelledError`` (and ``KeyboardInterrupt``, ``SystemExit``,
    ``GeneratorExit``) that a ``tzinfo.utcoffset`` or the lookup of a provider's
    method raises cannot be the task's cancellation: ``Task.cancel()`` is only
    delivered at an ``await``. It is classified like any adapter failure and
    must neither cancel ``gather()`` / ``fetch()`` nor cost the other providers
    their answers. A cancellation that IS delivered at an ``await`` still
    propagates.
    """

    @staticmethod
    def stamped(error, *, fail_on_call=1):
        zone = raising_timezone(error, fail_on_call=fail_on_call)
        return datetime(2026, 9, 1, tzinfo=zone)

    async def test_gather_reports_a_timezone_that_raises_as_invalid_response(self):
        for label, error in BASE_EXCEPTIONS.items():
            for fail_on_call in (1, 2):
                with self.subTest(error=label, fail_on_call=fail_on_call):
                    bad = hit()
                    stamp = self.stamped(error, fail_on_call=fail_on_call)
                    object.__setattr__(bad, "published_at", stamp)
                    provider = web("web-bad", hits=[bad])
                    good = docs("docs-ok", hits=[hit("https://example.com/ok")])

                    with self.assertLogs(LOGGER, level="WARNING") as logs:
                        result = await guarded(
                            broker_of(provider, good).gather(request())
                        )

                    self.assertEqual(
                        [(e.provider_id, e.code) for e in result.errors],
                        [("web-bad", Code.INVALID_RESPONSE)],
                    )
                    self.assertEqual(urls(result), ["https://example.com/ok"])
                    self.assertEqual(len(logs.records), 1)
                    self.assertNotIn(SECRET, "\n".join(logs.output))
                    self.assertEqual(asyncio.current_task().cancelling(), 0)
                    self.assertEqual(other_tasks(), set())

    async def test_fetch_reports_a_timezone_that_raises_as_invalid_response(self):
        for label, error in BASE_EXCEPTIONS.items():
            with self.subTest(error=label):
                bad = document()
                object.__setattr__(bad, "published_at", self.stamped(error))
                provider = web(documents={"https://example.com/a": bad})

                with self.assertLogs(LOGGER, level="WARNING") as logs:
                    result = await guarded(broker_of(provider).fetch(source_of()))

                self.assertEqual(result.items, ())
                self.assertEqual(
                    [(e.provider_id, e.code) for e in result.errors],
                    [("web-a", Code.INVALID_RESPONSE)],
                )
                self.assertNotIn(SECRET, "\n".join(logs.output))
                self.assertEqual(asyncio.current_task().cancelling(), 0)

    async def test_gather_isolates_a_provider_method_that_raises_synchronously(self):
        # The broker reads ``provider.search`` / ``provider.fetch`` and calls it
        # before it awaits anything: that is adapter code too (a property, a
        # ``__getattribute__``, a marked plain function).
        for label, error in BASE_EXCEPTIONS.items():
            with self.subTest(error=label):
                bad = ArmedProvider("web-bad", error)
                good = docs("docs-ok", hits=[hit("https://example.com/ok")])
                broker = broker_of(bad, good)
                bad.armed = True

                with self.assertLogs(LOGGER, level="WARNING") as logs:
                    found = await guarded(broker.gather(request()))

                self.assertEqual(
                    [(e.provider_id, e.code) for e in found.errors],
                    [("web-bad", Code.INTERNAL_ERROR)],
                )
                self.assertEqual(urls(found), ["https://example.com/ok"])
                self.assertEqual(len(logs.records), 1)
                self.assertNotIn(SECRET, "\n".join(logs.output))
                self.assertIn(f"exception_type={ADAPTER_ERROR}", logs.output[0])
                self.assertEqual(asyncio.current_task().cancelling(), 0)
                self.assertEqual(other_tasks(), set())

    async def test_fetch_isolates_a_provider_method_that_raises_synchronously(self):
        for label, error in BASE_EXCEPTIONS.items():
            with self.subTest(error=label):
                bad = ArmedProvider("web-a", error)
                broker = broker_of(bad)
                bad.armed = True

                with self.assertLogs(LOGGER, level="WARNING") as logs:
                    result = await guarded(broker.fetch(source_of()))

                self.assertEqual(result.items, ())
                self.assertEqual(
                    [(e.provider_id, e.code) for e in result.errors],
                    [("web-a", Code.INTERNAL_ERROR)],
                )
                self.assertNotIn(SECRET, "\n".join(logs.output))

    async def test_cancelling_gather_still_propagates_next_to_such_a_failure(self):
        hanging = web("web-hang", hang=True)
        bad = ArmedProvider("docs-bad", asyncio.CancelledError, kind=ProviderKind.DOCS)
        broker = broker_of(bad, hanging, timeout_seconds=60)
        bad.armed = True
        task = asyncio.ensure_future(broker.gather(request(time_budget_seconds=60)))
        for _ in range(1000):
            if hanging.search_calls:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(hanging.search_calls), 1)
        self.assertFalse(task.done())  # the failure of "docs-bad" did not end it

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await guarded(task)

        self.assertTrue(task.cancelled())
        self.assertTrue(hanging.cancelled)
        self.assertEqual(other_tasks(), set())

    async def test_cancelling_fetch_still_propagates_after_a_failed_fetch(self):
        bad = ArmedProvider("web-a", asyncio.CancelledError)
        broker = broker_of(bad)
        bad.armed = True
        with self.assertLogs(LOGGER, level="WARNING"):
            first = await guarded(broker.fetch(source_of()))
        self.assertEqual(first.errors[0].code, Code.INTERNAL_ERROR)

        hanging = web("web-a", hang_fetch=True)
        task = asyncio.ensure_future(
            broker_of(hanging, timeout_seconds=60).fetch(
                source_of(), time_budget_seconds=60
            )
        )
        for _ in range(1000):
            if hanging.fetch_calls:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(hanging.fetch_calls), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await guarded(task)
        self.assertTrue(task.cancelled())
        self.assertTrue(hanging.cancelled)


class ArmedProvider(StaticProvider):
    """Reading ``search`` / ``fetch`` raises ``error(SECRET)`` once ``armed``.

    Registration reads the attributes while not armed, as a real provider that
    breaks later would.
    """

    def __init__(self, name, error, kind=ProviderKind.WEB, **kwargs):
        super().__init__(name, kind, **kwargs)
        self.error = error
        self.armed = False

    def __getattribute__(self, name):
        if name in ("search", "fetch") and object.__getattribute__(self, "armed"):
            raise object.__getattribute__(self, "error")(SECRET)
        return super().__getattribute__(name)


class MalformedTypedResponseTest(unittest.IsolatedAsyncioTestCase):
    """A typed hit or document built around its constructor is invalid, not a crash.

    ``isinstance`` succeeds for an object whose slots were never set or whose
    values have the wrong type or size; one such response must neither raise out
    of ``gather`` / ``fetch`` nor cost the other providers their answers.
    """

    async def test_gather_isolates_every_malformed_hit(self):
        for label, bad in malformed_hits().items():
            with self.subTest(malformed=label):
                first = hit("https://example.com/first-of-the-bad-provider")
                provider = web("web-bad", raw_search_response=[first, bad])
                good = docs("docs-ok", hits=[hit("https://example.com/ok")])

                with self.assertLogs(LOGGER, level="WARNING") as logs:
                    result = await guarded(broker_of(provider, good).gather(request()))

                self.assertEqual(len(logs.records), 1)
                self.assertEqual(
                    [(e.provider_id, e.code) for e in result.errors],
                    [("web-bad", Code.INVALID_RESPONSE)],
                )
                self.assertEqual(
                    urls(result), ["https://example.com/ok"], msg="all or nothing"
                )
                self.assertEqual(result.providers_queried, 2)

    async def test_fetch_reports_every_malformed_document(self):
        for label, bad in malformed_documents().items():
            with self.subTest(malformed=label):
                provider = web(raw_fetch_response=bad)

                with self.assertLogs(LOGGER, level="WARNING") as logs:
                    result = await guarded(broker_of(provider).fetch(source_of()))

                self.assertEqual(len(logs.records), 1)
                self.assertEqual(result.items, ())
                self.assertEqual(
                    result.errors,
                    (error("web-a", ProviderKind.WEB, Code.INVALID_RESPONSE),),
                )
                self.assertEqual(result.providers_queried, 1)

    async def test_the_log_has_the_type_of_the_failure_and_not_the_content(self):
        bad = hit()
        object.__setattr__(bad, "title", SECRET * 200)  # far over the limit
        doc = document()
        object.__setattr__(doc, "text", SECRET)
        object.__setattr__(doc, "private_source", SECRET)
        provider = web(raw_search_response=[bad], raw_fetch_response=doc)

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            gathered = await guarded(broker_of(provider).gather(request()))
            fetched = await guarded(broker_of(provider).fetch(source_of()))

        for result in (gathered, fetched):
            self.assertEqual([e.code for e in result.errors], [Code.INVALID_RESPONSE])
            self.assertNotIn(SECRET, json.dumps(result.to_dict()))
        self.assertEqual(len(logs.records), 2)
        for line in logs.output:
            self.assertIn("code=invalid_response", line)
            self.assertIn("exception_type=InvalidProviderResponseError", line)
            self.assertNotIn(SECRET, line)

    async def test_a_valid_object_of_the_same_kind_is_still_accepted(self):
        # The malformed table is built from these very objects.
        provider = web(
            hits=[hit("https://example.com/a")],
            documents={"https://example.com/a": document()},
        )
        broker = broker_of(provider)

        gathered = await guarded(broker.gather(request()))
        fetched = await guarded(broker.fetch(source_of()))

        self.assertEqual(
            (gathered.errors, urls(gathered)), ((), ["https://example.com/a"])
        )
        self.assertEqual(
            (fetched.errors, urls(fetched)), ((), ["https://example.com/a"])
        )


class GatherBasicsTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_providers_gives_an_empty_result(self):
        result = await guarded(broker_of().gather(request()))
        self.assertEqual(result, ResearchResult())
        self.assertEqual(result.providers_queried, 0)
        self.assertFalse(result.all_failed)

    async def test_one_hit_becomes_one_unified_item(self):
        published = datetime(2026, 9, 1, 9, 0, tzinfo=JST)
        provider = web(
            "web-main",
            hits=[
                hit(
                    "HTTPS://Example.com:443/Guide?utm_source=x&b=2&a=1#top",
                    title="  The  Guide\n",
                    text="An excerpt",
                    published_at=published,
                    source_type=SourceType.OFFICIAL_DOCS,
                )
            ],
        )
        result = await guarded(broker_of(provider).gather(request()))
        expected = ResearchItem(
            SourceMetadata(
                provider_kind=ProviderKind.WEB,
                provider_id="web-main",
                locator="https://example.com/Guide?a=1&b=2",
                title="The Guide",
                retrieved_at=NOW,
                content_hash=compute_content_hash("An excerpt"),
                source_type=SourceType.OFFICIAL_DOCS,
                published_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
                private_source=False,
            ),
            "An excerpt",
        )
        self.assertEqual(
            result,
            ResearchResult(items=(expected,), errors=(), providers_queried=1),
        )

    async def test_the_provider_gets_the_query_and_the_limit_unchanged(self):
        provider = web(hits=[hit()])
        query = "最新の  Python 3.13 changes"
        await guarded(broker_of(provider).gather(request(query, max_results=7)))
        self.assertEqual(provider.search_calls, [(query, 7)])

    async def test_only_the_requested_kinds_are_queried(self):
        w, d, g = web(), docs(), github()
        broker = broker_of(w, d, g)
        result = await guarded(
            broker.gather(request(kinds=frozenset({ProviderKind.DOCS})))
        )
        self.assertEqual((w.search_calls, g.search_calls), ([], []))
        self.assertEqual(len(d.search_calls), 1)
        self.assertEqual(result.providers_queried, 1)
        result = await guarded(
            broker.gather(
                request(kinds=frozenset({ProviderKind.WEB, ProviderKind.GITHUB}))
            )
        )
        self.assertEqual(result.providers_queried, 2)
        self.assertEqual(len(d.search_calls), 1)

    async def test_a_kind_without_provider_is_not_an_error(self):
        provider = web(hits=[hit()])
        result = await guarded(
            broker_of(provider).gather(request(kinds=frozenset({ProviderKind.GITHUB})))
        )
        self.assertEqual(result, ResearchResult())
        self.assertEqual(provider.search_calls, [])

    async def test_every_kind_yields_the_same_shape(self):
        w = web("web-a", hits=[hit("https://w.example/1", title="W")])
        d = docs(
            "docs-a",
            hits=[
                hit("https://d.example/1", title="D", source_type=SourceType.PRIMARY)
            ],
        )
        g = github(
            "gh-a",
            hits=[hit("https://github.com/o/r", title="G", private_source=True)],
        )
        result = await guarded(broker_of(w, d, g).gather(request()))
        self.assertEqual(
            [item.source.provider_kind for item in result.items],
            [ProviderKind.WEB, ProviderKind.DOCS, ProviderKind.GITHUB],
        )
        self.assertEqual(
            [item.source.provider_id for item in result.items],
            ["web-a", "docs-a", "gh-a"],
        )
        key_sets = {tuple(item.to_dict()["source"]) for item in result.items}
        self.assertEqual(len(key_sets), 1)
        for item in result.items:
            self.assertIs(type(item), ResearchItem)
            self.assertIs(type(item.source), SourceMetadata)
        self.assertEqual(
            [item.source.private_source for item in result.items], [False, False, True]
        )

    async def test_the_result_exposes_no_provider_objects_or_payloads(self):
        provider = web(hits=[hit(title="T", text="x")])
        result = await guarded(broker_of(provider).gather(request()))
        payload = json.dumps(result.to_dict())
        self.assertNotIn("StaticProvider", payload)
        self.assertEqual(
            set(result.to_dict()), {"items", "errors", "providers_queried", "truncated"}
        )
        self.assertEqual(set(result.to_dict()["items"][0]), {"source", "text"})
        self.assertEqual(
            set(result.to_dict()["items"][0]["source"]),
            {
                "provider_kind",
                "provider_id",
                "locator",
                "title",
                "retrieved_at",
                "content_hash",
                "source_type",
                "published_at",
                "private_source",
            },
        )

    async def test_identity_comes_from_the_registry_snapshot(self):
        provider = web("original", hits=[hit()])
        broker = broker_of(provider)
        provider.name = "impostor"
        provider.kind = ProviderKind.GITHUB
        result = await guarded(broker.gather(request()))
        self.assertEqual(result.items[0].source.provider_id, "original")
        self.assertEqual(result.items[0].source.provider_kind, ProviderKind.WEB)

    async def test_the_result_does_not_depend_on_registration_order(self):
        def providers():
            return [
                github("gh-a", hits=[hit("https://g.example/1")]),
                web("web-b", hits=[hit("https://w.example/2")]),
                docs("docs-a", hits=[hit("https://d.example/1")]),
                web("web-a", hits=[hit("https://w.example/1")]),
            ]

        forward = await guarded(broker_of(*providers()).gather(request()))
        backward = await guarded(broker_of(*reversed(providers())).gather(request()))
        self.assertEqual(forward, backward)
        self.assertEqual(
            urls(forward),
            [
                "https://w.example/1",
                "https://w.example/2",
                "https://d.example/1",
                "https://g.example/1",
            ],
        )

    async def test_results_are_interleaved_and_truncated_at_max_results(self):
        w = web("web-a", hits=[hit(f"https://w.example/{n}") for n in (1, 2, 3)])
        d = docs("docs-a", hits=[hit(f"https://d.example/{n}") for n in (1, 2)])
        result = await guarded(broker_of(w, d).gather(request(max_results=4)))
        self.assertEqual(
            urls(result),
            [
                "https://w.example/1",
                "https://d.example/1",
                "https://w.example/2",
                "https://d.example/2",
            ],
        )
        self.assertIs(result.truncated, True)
        self.assertEqual(w.search_calls[0][1], 4)
        self.assertEqual(d.search_calls[0][1], 4)

    async def test_exactly_max_results_items_are_not_truncated(self):
        w = web("web-a", hits=[hit(f"https://w.example/{n}") for n in (1, 2)])
        d = docs("docs-a", hits=[hit(f"https://d.example/{n}") for n in (1, 2)])
        result = await guarded(broker_of(w, d).gather(request(max_results=4)))
        self.assertEqual(len(result.items), 4)
        self.assertIs(result.truncated, False)

    async def test_duplicates_across_providers_are_merged_by_canonical_locator(self):
        w = web(
            "web-a",
            hits=[
                hit("https://x.example/page?b=2&a=1", title="from web"),
                hit("https://w.example/only"),
            ],
        )
        d = docs(
            "docs-a",
            hits=[
                hit(
                    "HTTPS://X.example:443/page?a=1&utm_source=n&b=2#intro",
                    title="from docs",
                    private_source=True,
                ),
                hit("https://d.example/only"),
            ],
        )
        result = await guarded(broker_of(w, d).gather(request()))
        self.assertEqual(
            urls(result),
            [
                "https://x.example/page?a=1&b=2",
                "https://w.example/only",
                "https://d.example/only",
            ],
        )
        merged = result.items[0].source
        self.assertEqual((merged.provider_id, merged.title), ("web-a", "from web"))
        self.assertIs(merged.private_source, True)
        self.assertIs(result.truncated, False)

    async def test_duplicates_inside_one_provider_are_merged(self):
        provider = web(hits=[hit("https://a.example/1"), hit("https://a.example/1#x")])
        result = await guarded(broker_of(provider).gather(request()))
        self.assertEqual(urls(result), ["https://a.example/1"])

    async def test_an_empty_answer_is_not_an_error(self):
        result = await guarded(broker_of(web(hits=[])).gather(request()))
        self.assertEqual(result, ResearchResult(providers_queried=1))
        self.assertFalse(result.all_failed)


class GatherClockTest(unittest.IsolatedAsyncioTestCase):
    async def test_retrieved_at_is_read_once_and_shared(self):
        readings = []

        def clock():
            readings.append(NOW + timedelta(seconds=len(readings)))
            return readings[-1]

        w = web("web-a", hits=[hit("https://w.example/1"), hit("https://w.example/2")])
        d = docs("docs-a", hits=[hit("https://d.example/1")])
        result = await guarded(broker_of(w, d, clock=clock).gather(request()))
        self.assertEqual(len(readings), 1)
        self.assertEqual(
            {item.source.retrieved_at for item in result.items}, {readings[0]}
        )
        self.assertEqual(len(result.items), 3)

    async def test_the_clock_is_read_once_even_without_items(self):
        readings = []

        def clock():
            readings.append(1)
            return NOW

        await guarded(broker_of(clock=clock).gather(request()))
        await guarded(
            broker_of(web(search_error=RuntimeError()), clock=clock).gather(request())
        )
        self.assertEqual(len(readings), 2)

    async def test_the_clock_is_read_after_the_providers_finished(self):
        class Marking(StaticProvider):
            finished = False

            async def search(self, query, *, limit):
                try:
                    return await super().search(query, limit=limit)
                finally:
                    self.finished = True

        provider = Marking("web-a", ProviderKind.WEB, hits=[hit()])
        seen = []

        def clock():
            seen.append(provider.finished)
            return NOW

        await guarded(broker_of(provider, clock=clock).gather(request()))
        self.assertEqual(seen, [True])

    async def test_the_default_clock_is_utc_now(self):
        broker = ResearchBroker(registry_of(web(hits=[hit()])))
        before = datetime.now(UTC)
        result = await guarded(broker.gather(request()))
        after = datetime.now(UTC)
        retrieved = result.items[0].source.retrieved_at
        self.assertEqual(retrieved.utcoffset(), timedelta(0))
        self.assertLessEqual(before, retrieved)
        self.assertLessEqual(retrieved, after)


class GatherFailureIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_provider_does_not_fail_the_others(self):
        broken = web("web-a", search_error=RuntimeError(SECRET))
        healthy = docs("docs-a", hits=[hit("https://d.example/1")])
        result = await guarded(broker_of(broken, healthy).gather(request()))
        self.assertEqual(urls(result), ["https://d.example/1"])
        self.assertEqual(
            result.errors,
            (error("web-a", ProviderKind.WEB, Code.INTERNAL_ERROR),),
        )
        self.assertEqual(result.providers_queried, 2)
        self.assertFalse(result.all_failed)

    async def test_the_exception_text_never_reaches_the_result(self):
        provider = web(search_error=RuntimeError(f"connect failed: {SECRET}"))
        result = await guarded(broker_of(provider).gather(request()))
        for text in (repr(result), json.dumps(result.to_dict())):
            self.assertNotIn(SECRET, text)
            self.assertNotIn("connect failed", text)
        self.assertEqual(result.errors[0].code, Code.INTERNAL_ERROR)

    async def test_provider_failure_codes_are_reported_as_they_are(self):
        for code in ResearchErrorCode:
            with self.subTest(code=code):
                provider = web(search_error=ProviderFailure(code))
                result = await guarded(broker_of(provider).gather(request()))
                self.assertEqual(
                    result.errors, (error("web-a", ProviderKind.WEB, code),)
                )

    async def test_a_forged_failure_code_becomes_internal_error(self):
        failure = ProviderFailure(Code.RATE_LIMITED)
        failure.code = f"leaked {SECRET}"
        result = await guarded(broker_of(web(search_error=failure)).gather(request()))
        self.assertEqual(result.errors[0].code, Code.INTERNAL_ERROR)
        self.assertNotIn(SECRET, repr(result))

    async def test_a_timeout_error_raised_by_the_provider_is_a_timeout(self):
        result = await guarded(
            broker_of(web(search_error=TimeoutError())).gather(request())
        )
        self.assertEqual(result.errors[0].code, Code.TIMEOUT)

    async def test_a_failure_with_hostile_hooks_costs_only_its_own_provider(self):
        calls = Tripwire()
        for label, (failure, expected) in hostile_failures(calls).items():
            with self.subTest(failure=label):
                with calls.armed():
                    result = await guarded(
                        broker_of(
                            web(search_error=failure),
                            docs(hits=[hit("https://d.example/1")]),
                        ).gather(request())
                    )
                self.assertEqual(urls(result), ["https://d.example/1"])
                self.assertEqual(
                    result.errors, (error("web-a", ProviderKind.WEB, expected),)
                )
        self.assertEqual(calls, [])

    async def test_an_adapter_defined_type_is_logged_as_a_fixed_token(self):
        # The class name of an exception that an adapter defines is adapter data:
        # it is never logged, not even a harmless-looking one.
        calls = Tripwire()
        failure, _ = hostile_failures(calls)["a metaclass whose __name__ raises"]
        with self.assertLogs(LOGGER, level="WARNING") as logs, calls.armed():
            await guarded(broker_of(web(search_error=failure)).gather(request()))
        self.assertEqual(len(logs.records), 1)
        self.assertTrue(
            logs.output[0].endswith("code=internal_error exception_type=adapter_error")
        )
        self.assertNotIn("RaisingMetaclass", "\n".join(logs.output))
        self.assertEqual(calls, [])

    async def test_an_exception_that_cannot_be_printed_is_handled(self):
        result = await guarded(
            broker_of(web(search_error=EvilError()), docs(hits=[hit()])).gather(
                request()
            )
        )
        self.assertEqual(result.errors[0].code, Code.INTERNAL_ERROR)
        self.assertEqual(len(result.items), 1)

    async def test_when_every_provider_fails_the_result_says_so(self):
        result = await guarded(
            broker_of(
                web(search_error=RuntimeError()),
                docs(search_error=ProviderFailure(Code.UNAVAILABLE)),
            ).gather(request())
        )
        self.assertEqual(result.items, ())
        self.assertTrue(result.all_failed)
        self.assertEqual(result.providers_queried, 2)
        self.assertEqual(
            [e.code for e in result.errors], [Code.INTERNAL_ERROR, Code.UNAVAILABLE]
        )

    async def test_errors_follow_registry_order_not_completion_order(self):
        docs_failed = asyncio.Event()

        async def docs_hook():
            docs_failed.set()

        async def web_hook():
            await docs_failed.wait()

        w = web("web-a", search_error=RuntimeError(), before_search=web_hook)
        d = docs("docs-a", search_error=RuntimeError(), before_search=docs_hook)
        result = await guarded(broker_of(w, d).gather(request()))
        self.assertEqual(
            [(e.provider_id, e.kind) for e in result.errors],
            [("web-a", ProviderKind.WEB), ("docs-a", ProviderKind.DOCS)],
        )

    async def test_providers_run_concurrently(self):
        b_started = asyncio.Event()

        async def a_hook():
            await b_started.wait()

        async def b_hook():
            b_started.set()

        a = web("web-a", hits=[hit("https://a.example/1")], before_search=a_hook)
        b = docs("docs-a", hits=[hit("https://b.example/1")], before_search=b_hook)
        result = await guarded(broker_of(a, b, timeout_seconds=10).gather(request()))
        self.assertEqual(result.errors, ())
        self.assertEqual(urls(result), ["https://a.example/1", "https://b.example/1"])

    async def test_invalid_responses_are_reported_and_isolated(self):
        good = hit("https://ok.example/1")
        responses = {
            "none": None,
            "string": "https://a.example/1",
            "bytes": b"bytes",
            "dict": {"locator": "https://a.example/1"},
            "set": {good},
            "generator": (h for h in [good]),
            "single hit": good,
            "non-hit element": [good, "https://a.example/2"],
            "bad scheme": [hit("ftp://a.example/1")],
            "user info": [hit("https://user:pw@a.example/1")],
            "good then bad": [good, hit("javascript:alert(1)")],
            "bad then good": [hit("https://exa mple.com/"), good],
        }
        for label, response in responses.items():
            with self.subTest(response=label):
                broken = web("web-a", raw_search_response=response)
                healthy = docs("docs-a", hits=[hit("https://d.example/1")])
                result = await guarded(broker_of(broken, healthy).gather(request()))
                self.assertEqual(urls(result), ["https://d.example/1"])
                self.assertEqual(
                    result.errors,
                    (error("web-a", ProviderKind.WEB, Code.INVALID_RESPONSE),),
                )

    async def test_a_response_container_with_hostile_hooks_is_invalid(self):
        # Only an exact list or tuple is a response: a subclass (or an object
        # that claims to be one) could run the adapter's code inside the broker.
        calls = Tripwire()
        for count in (2, 5):  # within the limit, and over it
            hits = [hit(f"https://a.example/{n}") for n in range(count)]
            for label, response in hostile_containers(hits, calls).items():
                with self.subTest(response=label, hits=count):
                    broken = web("web-a", raw_search_response=response)
                    healthy = docs("docs-a", hits=[hit("https://d.example/1")])
                    with calls.armed():
                        result = await guarded(
                            broker_of(broken, healthy).gather(request(max_results=2))
                        )
                    self.assertEqual(urls(result), ["https://d.example/1"])
                    self.assertEqual(
                        result.errors,
                        (error("web-a", ProviderKind.WEB, Code.INVALID_RESPONSE),),
                    )
        self.assertEqual(calls, [])

    async def test_returning_more_hits_than_the_limit_is_invalid(self):
        hits = [hit(f"https://a.example/{n}") for n in range(3)]
        over = web("web-a", hits=hits, ignore_limit=True)
        exact = docs("docs-a", hits=hits, ignore_limit=True)
        result = await guarded(broker_of(over, exact).gather(request(max_results=3)))
        self.assertEqual(result.errors, ())
        self.assertEqual(len(result.items), 3)
        result = await guarded(broker_of(over).gather(request(max_results=2)))
        self.assertEqual(result.items, ())
        self.assertEqual(
            result.errors, (error("web-a", ProviderKind.WEB, Code.INVALID_RESPONSE),)
        )

    async def test_failures_are_logged_by_type_and_code_only(self):
        query = "very-private-question"
        locator = "https://leak.example/never-log-this"
        broken = web("web-a", search_error=RuntimeError(f"boom {SECRET}"))
        invalid = docs("docs-a", raw_search_response=[hit(locator), "not a hit"])
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            await guarded(broker_of(broken, invalid).gather(request(query)))
        text = "\n".join(logs.output)
        for forbidden in (SECRET, "boom", query, locator, "not a hit"):
            self.assertNotIn(forbidden, text)
        for expected in ("web-a", "RuntimeError", "internal_error", "docs-a"):
            self.assertIn(expected, text)
        self.assertIn("invalid_response", text)
        self.assertTrue(all(record.exc_info is None for record in logs.records))

    async def test_a_fully_successful_gather_logs_nothing(self):
        with self.assertNoLogs(LOGGER, level="WARNING"):
            await guarded(broker_of(web(hits=[hit()]), docs(hits=[])).gather(request()))


class LoggedExceptionTypeTest(unittest.IsolatedAsyncioTestCase):
    """The log line names an allowlisted exception type or says ``adapter_error``.

    The class name of an exception that an adapter raises is adapter data: it can
    hold a credential, a newline that forges a second log record, 100,000
    characters, or the name of an allowed class. Only a class that IS one of a
    fixed list (matched by identity, never by name) is named.
    """

    def assert_one_line(self, logs, *, code: str, exception_type: str) -> None:
        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        self.assertEqual(
            record.getMessage(),
            "research provider failed: provider=web-a kind=web "
            f"code={code} exception_type={exception_type}",
        )
        self.assertEqual([type(arg) for arg in record.args], [str] * 4)
        self.assertEqual(logs.output[0].count("\n"), 0)

    @staticmethod
    def expected_code(label: str) -> str:
        unavailable = label == "a subclass of ProviderFailure"
        return Code.UNAVAILABLE.value if unavailable else Code.INTERNAL_ERROR.value

    async def test_no_hostile_class_name_reaches_the_log_of_gather(self):
        calls = Tripwire()
        for label, failure in hostile_named_exceptions(calls).items():
            with self.subTest(exception=label):
                broken = web("web-a", search_error=failure)
                healthy = docs("docs-a", hits=[hit("https://d.example/1")])
                with self.assertLogs(LOGGER, level="WARNING") as logs, calls.armed():
                    result = await guarded(broker_of(broken, healthy).gather(request()))
                self.assert_one_line(
                    logs,
                    code=self.expected_code(label),
                    exception_type=ADAPTER_ERROR,
                )
                self.assertEqual(urls(result), ["https://d.example/1"])
                self.assertEqual(
                    [e.code.value for e in result.errors], [self.expected_code(label)]
                )
        self.assertEqual(calls, [])

    async def test_no_hostile_class_name_reaches_the_log_of_fetch(self):
        calls = Tripwire()
        for label, failure in hostile_named_exceptions(calls).items():
            with self.subTest(exception=label):
                provider = web("web-a", fetch_error=failure)
                with self.assertLogs(LOGGER, level="WARNING") as logs, calls.armed():
                    result = await guarded(broker_of(provider).fetch(source_of()))
                self.assert_one_line(
                    logs,
                    code=self.expected_code(label),
                    exception_type=ADAPTER_ERROR,
                )
                self.assertEqual(
                    [e.code.value for e in result.errors], [self.expected_code(label)]
                )
        self.assertEqual(calls, [])

    async def test_the_token_replaces_every_name_of_the_hostile_classes(self):
        # The log holds none of the text that the class names, or their other
        # attributes, carry (the exact-line tests above would also fail on it).
        calls = Tripwire()
        forbidden = (SECRET, FORGED, "access_token", "Innocent", "Runtim", "例外")
        for label, failure in hostile_named_exceptions(calls).items():
            with self.subTest(exception=label):
                with self.assertLogs(LOGGER, level="WARNING") as logs, calls.armed():
                    await guarded(
                        broker_of(web("web-a", search_error=failure)).gather(request())
                    )
                text = "\n".join(logs.output)
                self.assertLess(len(text), 200)
                for word in forbidden:
                    self.assertNotIn(word, text)

    async def test_allowlisted_types_are_named(self):
        cases = [
            (RuntimeError(SECRET), "internal_error", "RuntimeError"),
            (ValueError(SECRET), "internal_error", "ValueError"),
            (KeyError(SECRET), "internal_error", "KeyError"),
            (OSError(SECRET), "internal_error", "OSError"),
            (
                ConnectionRefusedError(SECRET),
                "internal_error",
                "ConnectionRefusedError",
            ),
            (TimeoutError(SECRET), "timeout", "TimeoutError"),
            (
                ExceptionGroup(SECRET, [ValueError()]),
                "internal_error",
                "ExceptionGroup",
            ),
            (Exception(SECRET), "internal_error", "Exception"),
            (ProviderFailure(Code.RATE_LIMITED), "rate_limited", "ProviderFailure"),
            (
                InvalidProviderResponseError(),
                "internal_error",
                "InvalidProviderResponseError",
            ),
        ]
        for failure, code, expected in cases:
            with self.subTest(exception=expected):
                with self.assertLogs(LOGGER, level="WARNING") as logs:
                    await guarded(
                        broker_of(web("web-a", search_error=failure)).gather(request())
                    )
                self.assert_one_line(logs, code=code, exception_type=expected)
                self.assertNotIn(SECRET, logs.output[0])

    async def test_a_subclass_of_an_allowlisted_type_is_not_named(self):
        # The class is the adapter's own, so is its name (``RateLimitedError``).
        class SecretRuntimeError(RuntimeError):
            pass

        class RateLimitedError(ProviderFailure):
            pass

        cases = [
            (SecretRuntimeError(), "internal_error"),
            (RateLimitedError(Code.RATE_LIMITED), "rate_limited"),
        ]
        for failure, code in cases:
            with self.subTest(exception=type(failure).__name__):
                with self.assertLogs(LOGGER, level="WARNING") as logs:
                    await guarded(
                        broker_of(web("web-a", search_error=failure)).gather(request())
                    )
                self.assert_one_line(logs, code=code, exception_type=ADAPTER_ERROR)

    def test_the_allowlist_holds_only_short_plain_names(self):
        names = [cls.__name__ for cls in LOGGED_EXCEPTION_TYPES]
        self.assertEqual(len(set(names)), len(names), msg="unique names")
        self.assertNotIn(ADAPTER_ERROR, names)
        for cls in LOGGED_EXCEPTION_TYPES:
            with self.subTest(exception=cls.__name__):
                self.assertTrue(issubclass(cls, Exception))
                self.assertRegex(cls.__name__, r"\A[A-Za-z]{1,40}\Z")
                self.assertEqual(log_type_name(instance_of(cls)), cls.__name__)
        self.assertEqual(ADAPTER_ERROR, "adapter_error")

    def test_the_package_and_common_builtin_errors_are_on_the_list(self):
        for cls in (
            ProviderFailure,
            InvalidProviderResponseError,
            InvalidLocatorError,
            Exception,
            RuntimeError,
            ValueError,
            TypeError,
            OSError,
            TimeoutError,
            ConnectionError,
            ExceptionGroup,
        ):
            with self.subTest(exception=cls.__name__):
                self.assertIn(cls, LOGGED_EXCEPTION_TYPES)

    def test_reading_the_token_never_runs_adapter_code(self):
        calls = Tripwire()
        failures = [f for f, _ in hostile_failures(calls).values()]
        failures += list(hostile_named_exceptions(calls).values())
        names = []
        for failure in failures:
            with calls.armed():
                names.append(log_type_name(failure))
        self.assertEqual(calls, [])
        # ``ProviderFailure`` itself (a forged code) is on the list; nothing else is.
        self.assertEqual(set(names), {ADAPTER_ERROR, "ProviderFailure"})
        self.assertEqual(names.count("ProviderFailure"), 1)


def instance_of(cls: type[Exception]) -> Exception:
    """An instance of a class of ``LOGGED_EXCEPTION_TYPES`` (some need arguments)."""
    if cls is ExceptionGroup:
        return ExceptionGroup("group", [ValueError()])
    if cls is UnicodeDecodeError:
        return UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
    if cls is UnicodeEncodeError:
        return UnicodeEncodeError("ascii", "\u00e9", 0, 1, "invalid")
    if cls is UnicodeTranslateError:
        return UnicodeTranslateError("\u00e9", 0, 1, "invalid")
    if cls is ProviderFailure:
        return ProviderFailure(Code.UNAVAILABLE)
    if cls is ProviderInterfaceError:
        return ProviderInterfaceError("name")
    return cls()


class GatherTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_slow_provider_times_out_and_the_others_still_answer(self):
        hanging = web("web-a", hang=True)
        healthy = docs("docs-a", hits=[hit("https://d.example/1")])
        broker = broker_of(hanging, healthy, timeout_seconds=SHORT_TIMEOUT)
        result = await guarded(broker.gather(request()))
        self.assertEqual(urls(result), ["https://d.example/1"])
        self.assertEqual(
            result.errors, (error("web-a", ProviderKind.WEB, Code.TIMEOUT),)
        )
        self.assertEqual(result.providers_queried, 2)
        self.assertTrue(hanging.cancelled)
        self.assertEqual(other_tasks(), set())

    async def test_each_provider_has_its_own_timeout(self):
        async def answer_late():
            await asyncio.sleep(SHORT_TIMEOUT * 3)

        registry = ProviderRegistry()
        impatient = web("web-a", hang=True)
        patient = docs(
            "docs-a", hits=[hit("https://d.example/1")], before_search=answer_late
        )
        registry.register(impatient, timeout_seconds=SHORT_TIMEOUT)
        registry.register(patient, timeout_seconds=10)
        broker = ResearchBroker(registry, clock=fixed_clock())
        result = await guarded(broker.gather(request()))
        self.assertEqual(urls(result), ["https://d.example/1"])
        self.assertEqual(
            result.errors, (error("web-a", ProviderKind.WEB, Code.TIMEOUT),)
        )
        self.assertTrue(impatient.cancelled)
        self.assertFalse(patient.cancelled)

    async def test_the_global_budget_caps_every_provider(self):
        hanging = [web("web-a", hang=True), docs("docs-a", hang=True)]
        broker = broker_of(*hanging, timeout_seconds=60)
        started = time.monotonic()
        result = await guarded(
            broker.gather(request(time_budget_seconds=SHORT_TIMEOUT))
        )
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(result.all_failed)
        self.assertEqual(
            result.errors,
            (
                error("web-a", ProviderKind.WEB, Code.TIMEOUT),
                error("docs-a", ProviderKind.DOCS, Code.TIMEOUT),
            ),
        )
        self.assertTrue(all(provider.cancelled for provider in hanging))
        self.assertEqual(other_tasks(), set())

    async def test_a_timed_out_provider_contributes_nothing_and_is_logged(self):
        hanging = web("web-a", hang=True)
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            result = await guarded(
                broker_of(hanging, timeout_seconds=SHORT_TIMEOUT).gather(request())
            )
        self.assertEqual(result.items, ())
        text = "\n".join(logs.output)
        self.assertIn("web-a", text)
        self.assertIn("timeout", text)

    async def test_cancelling_gather_cancels_every_provider_call(self):
        started = []
        both_started = asyncio.Event()

        async def hook():
            started.append(1)
            if len(started) == 2:
                both_started.set()

        providers = [
            web("web-a", hang=True, before_search=hook),
            docs("docs-a", hang=True, before_search=hook),
        ]
        broker = broker_of(*providers, timeout_seconds=60)
        task = asyncio.ensure_future(broker.gather(request(time_budget_seconds=60)))
        await asyncio.wait_for(both_started.wait(), GUARD_SECONDS)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, GUARD_SECONDS)
        self.assertTrue(all(provider.cancelled for provider in providers))
        self.assertEqual(other_tasks(), set())

    async def test_no_task_is_left_behind_after_a_normal_gather(self):
        await guarded(
            broker_of(web(hits=[hit()]), docs(search_error=RuntimeError())).gather(
                request()
            )
        )
        self.assertEqual(other_tasks(), set())


def source_of(
    locator: str = "https://example.com/a",
    *,
    provider_id: str = "web-a",
    kind: ProviderKind = ProviderKind.WEB,
    private_source: bool = False,
) -> SourceMetadata:
    return SourceMetadata(
        provider_kind=kind,
        provider_id=provider_id,
        locator=locator,
        title="Old title",
        retrieved_at=NOW - timedelta(days=1),
        content_hash="sha256:" + "0" * 64,
        source_type=SourceType.SECONDARY,
        published_at=datetime(2020, 1, 1, tzinfo=UTC),
        private_source=private_source,
    )


class FetchTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_returns_the_document_in_the_unified_shape(self):
        doc = document(
            title="  New \n Title ",
            text="Full text",
            published_at=datetime(2026, 9, 1, 9, 0, tzinfo=JST),
            source_type=SourceType.OFFICIAL_DOCS,
        )
        provider = web(documents={"https://example.com/a": doc})
        result = await guarded(broker_of(provider).fetch(source_of()))
        expected = ResearchItem(
            SourceMetadata(
                provider_kind=ProviderKind.WEB,
                provider_id="web-a",
                locator="https://example.com/a",
                title="New Title",
                retrieved_at=NOW,
                content_hash=compute_content_hash("Full text"),
                source_type=SourceType.OFFICIAL_DOCS,
                published_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
                private_source=False,
            ),
            "Full text",
        )
        self.assertEqual(
            result,
            ResearchResult(items=(expected,), errors=(), providers_queried=1),
        )
        self.assertEqual(provider.fetch_calls, ["https://example.com/a"])

    async def test_document_fields_replace_the_old_ones_even_when_unknown(self):
        provider = web(documents={"https://example.com/a": document(title="")})
        result = await guarded(broker_of(provider).fetch(source_of()))
        source = result.items[0].source
        self.assertEqual(source.title, "")
        self.assertEqual(source.source_type, SourceType.UNKNOWN)
        self.assertIsNone(source.published_at)

    async def test_the_provider_gets_the_canonical_locator(self):
        provider = web(documents={"https://example.com/a?x=1": document()})
        source = source_of("https://Example.com:443/a?x=1&utm_source=n")
        result = await guarded(broker_of(provider).fetch(source))
        self.assertEqual(provider.fetch_calls, ["https://example.com/a?x=1"])
        self.assertEqual(result.items[0].source.locator, "https://example.com/a?x=1")

    async def test_a_private_source_stays_private(self):
        cases = [
            (False, False, False),
            (True, False, True),
            (False, True, True),
            (True, True, True),
        ]
        for source_private, doc_private, expected in cases:
            with self.subTest(source=source_private, document=doc_private):
                provider = web(
                    documents={
                        "https://example.com/a": document(private_source=doc_private)
                    }
                )
                result = await guarded(
                    broker_of(provider).fetch(source_of(private_source=source_private))
                )
                self.assertIs(result.items[0].source.private_source, expected)

    async def test_an_unregistered_provider_is_unavailable(self):
        result = await guarded(broker_of(docs("docs-a")).fetch(source_of()))
        self.assertEqual(
            result,
            ResearchResult(
                errors=(error("web-a", ProviderKind.WEB, Code.UNAVAILABLE),),
                providers_queried=1,
            ),
        )

    async def test_a_provider_registered_with_another_kind_is_unavailable(self):
        provider = StaticProvider("web-a", ProviderKind.DOCS)
        result = await guarded(broker_of(provider).fetch(source_of()))
        self.assertEqual(
            result.errors, (error("web-a", ProviderKind.WEB, Code.UNAVAILABLE),)
        )
        self.assertEqual(provider.fetch_calls, [])

    async def test_a_missing_document_is_not_found(self):
        result = await guarded(broker_of(web()).fetch(source_of()))
        self.assertEqual(result.items, ())
        self.assertEqual(
            result.errors, (error("web-a", ProviderKind.WEB, Code.NOT_FOUND),)
        )
        self.assertTrue(result.all_failed)

    async def test_provider_failures_are_codes_only(self):
        for code in ResearchErrorCode:
            with self.subTest(code=code):
                provider = web(fetch_error=ProviderFailure(code))
                result = await guarded(broker_of(provider).fetch(source_of()))
                self.assertEqual(
                    result.errors, (error("web-a", ProviderKind.WEB, code),)
                )
        provider = web(fetch_error=RuntimeError(SECRET))
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            result = await guarded(broker_of(provider).fetch(source_of()))
        self.assertEqual(result.errors[0].code, Code.INTERNAL_ERROR)
        self.assertNotIn(SECRET, repr(result))
        text = "\n".join(logs.output)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("https://example.com/a", text)
        self.assertIn("RuntimeError", text)
        self.assertIn("web-a", text)

    async def test_a_timeout_error_from_the_provider_is_a_timeout(self):
        provider = web(fetch_error=TimeoutError())
        result = await guarded(broker_of(provider).fetch(source_of()))
        self.assertEqual(result.errors[0].code, Code.TIMEOUT)

    async def test_wrong_response_types_are_invalid(self):
        for label, response in {
            "none": None,
            "string": "text",
            "dict": {"text": "x"},
            "a search hit": hit("https://example.com/a"),
            "bytes": b"text",
        }.items():
            with self.subTest(response=label):
                provider = web(raw_fetch_response=response)
                result = await guarded(broker_of(provider).fetch(source_of()))
                self.assertEqual(result.items, ())
                self.assertEqual(
                    result.errors,
                    (error("web-a", ProviderKind.WEB, Code.INVALID_RESPONSE),),
                )

    async def test_a_hanging_fetch_times_out_within_the_budget(self):
        provider = web(hang_fetch=True)
        started = time.monotonic()
        result = await guarded(
            broker_of(provider, timeout_seconds=60).fetch(
                source_of(), time_budget_seconds=SHORT_TIMEOUT
            )
        )
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(
            result.errors, (error("web-a", ProviderKind.WEB, Code.TIMEOUT),)
        )
        self.assertTrue(provider.cancelled)
        self.assertEqual(other_tasks(), set())

    async def test_the_provider_timeout_also_applies_to_fetch(self):
        provider = web(hang_fetch=True)
        result = await guarded(
            broker_of(provider, timeout_seconds=SHORT_TIMEOUT).fetch(
                source_of(), time_budget_seconds=60
            )
        )
        self.assertEqual(result.errors[0].code, Code.TIMEOUT)
        self.assertTrue(provider.cancelled)

    async def test_cancelling_fetch_cancels_the_provider_call(self):
        provider = web(hang_fetch=True)
        broker = broker_of(provider, timeout_seconds=60)
        task = asyncio.ensure_future(broker.fetch(source_of(), time_budget_seconds=60))
        for _ in range(1000):
            if provider.fetch_calls:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(provider.fetch_calls), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, GUARD_SECONDS)
        self.assertTrue(provider.cancelled)
        self.assertEqual(other_tasks(), set())

    async def test_a_locator_that_cannot_be_canonicalised_is_the_callers_bug(self):
        provider = web(documents={})
        source = source_of("https://exa_mple.com/x")
        with self.assertRaises(InvalidLocatorError):
            await guarded(broker_of(provider).fetch(source))
        self.assertEqual(provider.fetch_calls, [])


if __name__ == "__main__":
    unittest.main()
