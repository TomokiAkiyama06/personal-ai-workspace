"""The research provider contract: enums, value objects, errors, StaticProvider.

These tests cover code that is implemented (no stub is involved); they pass on
their own and pin the shapes that the stubbed modules must produce.
"""

import asyncio
import dataclasses
import json
import unittest
from datetime import UTC, datetime, timedelta, timezone

from paw_backend.research import providers
from paw_backend.research.providers import (
    KIND_ORDER,
    MAX_DOCUMENT_CHARS,
    MAX_EXCERPT_CHARS,
    MAX_LOCATOR_CHARS,
    MAX_QUERY_CHARS,
    MAX_RESULTS_LIMIT,
    MAX_TITLE_CHARS,
    DuplicateProviderError,
    InvalidLocatorError,
    InvalidProviderResponseError,
    ProviderDocument,
    ProviderFailure,
    ProviderHit,
    ProviderInterfaceError,
    ProviderKind,
    ProviderRegistryError,
    RegistryFullError,
    ResearchError,
    ResearchErrorCode,
    ResearchItem,
    ResearchProvider,
    ResearchRequest,
    ResearchResult,
    SourceMetadata,
    SourceType,
    StaticProvider,
    UnknownProviderError,
)

from .research_support import NOW, SECRET, document, hit

HASH = "sha256:" + "0" * 64
JST = timezone(timedelta(hours=9))


def source(**overrides) -> SourceMetadata:
    values = {
        "provider_kind": ProviderKind.WEB,
        "provider_id": "web-a",
        "locator": "https://example.com/a",
        "title": "Title",
        "retrieved_at": NOW,
        "content_hash": HASH,
    }
    values.update(overrides)
    return SourceMetadata(**values)


class EnumTest(unittest.TestCase):
    def test_provider_kind_members_and_order(self):
        self.assertEqual(
            [kind.value for kind in ProviderKind],
            ["web", "docs", "github", "opencode"],
        )
        self.assertEqual(
            dict(KIND_ORDER),
            {
                ProviderKind.WEB: 0,
                ProviderKind.DOCS: 1,
                ProviderKind.GITHUB: 2,
                ProviderKind.OPENCODE: 3,
            },
        )

    def test_source_type_members(self):
        self.assertEqual(
            {source_type.value for source_type in SourceType},
            {
                "official_docs",
                "official_github",
                "primary",
                "secondary",
                "community",
                "unknown",
            },
        )

    def test_error_code_enum_is_closed(self):
        self.assertEqual(
            {code.value for code in ResearchErrorCode},
            {
                "timeout",
                "rate_limited",
                "unavailable",
                "not_found",
                "invalid_response",
                "internal_error",
            },
        )
        with self.assertRaises(ValueError):
            ResearchErrorCode("boom")

    def test_public_names_exist(self):
        for name in providers.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(providers, name))


class ResearchRequestTest(unittest.TestCase):
    def test_defaults(self):
        request = ResearchRequest("python 3.13")
        self.assertEqual(request.query, "python 3.13")
        self.assertEqual(request.max_results, 10)
        self.assertEqual(request.kinds, frozenset(ProviderKind))
        self.assertEqual(request.time_budget_seconds, 30.0)

    def test_is_immutable(self):
        request = ResearchRequest("q")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.query = "other"

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(TypeError):
            ResearchRequest("q", provider="web-a")

    def test_query_length_boundaries(self):
        self.assertEqual(
            len(ResearchRequest("q" * MAX_QUERY_CHARS).query), MAX_QUERY_CHARS
        )
        with self.assertRaises(ValueError):
            ResearchRequest("q" * (MAX_QUERY_CHARS + 1))
        with self.assertRaises(ValueError):
            ResearchRequest("")

    def test_query_must_not_be_blank_or_multiline(self):
        for query in ("   ", "\u3000", "a\nb", "a\tb", "a\x00b", "a\rb", "\ud800"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                ResearchRequest(query)

    def test_query_may_be_non_ascii(self):
        self.assertEqual(ResearchRequest("最新の Python").query, "最新の Python")

    def test_query_must_be_str(self):
        for query in (None, 5, b"q", ["q"]):
            with self.subTest(query=query), self.assertRaises(TypeError):
                ResearchRequest(query)

    def test_max_results_boundaries(self):
        self.assertEqual(ResearchRequest("q", max_results=1).max_results, 1)
        self.assertEqual(
            ResearchRequest("q", max_results=MAX_RESULTS_LIMIT).max_results, 50
        )
        for value in (0, -1, MAX_RESULTS_LIMIT + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ResearchRequest("q", max_results=value)

    def test_max_results_type_is_not_coerced(self):
        for value in (True, 5.0, "5", None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                ResearchRequest("q", max_results=value)

    def test_kinds_must_be_a_non_empty_frozenset_of_kinds(self):
        request = ResearchRequest("q", kinds=frozenset({ProviderKind.DOCS}))
        self.assertEqual(request.kinds, frozenset({ProviderKind.DOCS}))
        with self.assertRaises(ValueError):
            ResearchRequest("q", kinds=frozenset())
        for kinds in ({ProviderKind.WEB}, [ProviderKind.WEB], "web", None):
            with self.subTest(kinds=kinds), self.assertRaises(TypeError):
                ResearchRequest("q", kinds=kinds)
        with self.assertRaises(TypeError):
            ResearchRequest("q", kinds=frozenset({"web"}))

    def test_time_budget_boundaries(self):
        self.assertEqual(
            ResearchRequest("q", time_budget_seconds=120).time_budget_seconds, 120
        )
        self.assertEqual(
            ResearchRequest("q", time_budget_seconds=0.001).time_budget_seconds, 0.001
        )
        for value in (0, -1, 120.01, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ResearchRequest("q", time_budget_seconds=value)
        for value in (True, "5", None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                ResearchRequest("q", time_budget_seconds=value)


class ProviderHitTest(unittest.TestCase):
    def test_minimal_hit_has_safe_defaults(self):
        value = ProviderHit("https://a.example/", private_source=False)
        self.assertEqual(
            (value.title, value.text, value.published_at, value.source_type),
            ("", "", None, SourceType.UNKNOWN),
        )
        self.assertIs(value.private_source, False)

    def test_private_source_is_required_and_keyword_only(self):
        with self.assertRaises(TypeError):
            ProviderHit("https://a.example/")
        with self.assertRaises(TypeError):
            ProviderHit("u", "t", "x", None, SourceType.UNKNOWN, False)

    def test_private_source_must_be_bool(self):
        for value in (0, 1, "no", None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                ProviderHit("https://a.example/", private_source=value)

    def test_has_no_provider_or_payload_field(self):
        self.assertEqual(
            [field.name for field in dataclasses.fields(ProviderHit)],
            [
                "locator",
                "title",
                "text",
                "published_at",
                "source_type",
                "private_source",
            ],
        )

    def test_length_boundaries(self):
        ProviderHit("h" * MAX_LOCATOR_CHARS, private_source=False)
        ProviderHit("u", "t" * MAX_TITLE_CHARS, private_source=False)
        ProviderHit("u", "", "x" * MAX_EXCERPT_CHARS, private_source=False)
        for kwargs in (
            {"locator": "h" * (MAX_LOCATOR_CHARS + 1)},
            {"locator": ""},
            {"locator": "u", "title": "t" * (MAX_TITLE_CHARS + 1)},
            {"locator": "u", "text": "x" * (MAX_EXCERPT_CHARS + 1)},
            {"locator": "u", "text": "\ud800"},
        ):
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(ValueError):
                ProviderHit(private_source=False, **kwargs)

    def test_types_are_checked(self):
        for kwargs in (
            {"locator": None},
            {"locator": "u", "title": None},
            {"locator": "u", "text": b"x"},
            {"locator": "u", "source_type": "primary"},
            {"locator": "u", "published_at": "2026-01-01"},
        ):
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(TypeError):
                ProviderHit(private_source=False, **kwargs)

    def test_published_at_must_be_timezone_aware(self):
        with self.assertRaises(ValueError):
            ProviderHit("u", published_at=datetime(2026, 1, 1), private_source=False)
        aware = datetime(2026, 1, 1, 9, tzinfo=JST)
        self.assertEqual(
            ProviderHit("u", published_at=aware, private_source=False).published_at,
            aware,
        )

    def test_is_immutable(self):
        value = hit()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            value.title = "x"


class ProviderDocumentTest(unittest.TestCase):
    def test_document_text_bound_is_larger_than_an_excerpt(self):
        self.assertGreater(MAX_DOCUMENT_CHARS, MAX_EXCERPT_CHARS)
        ProviderDocument("t", "x" * MAX_DOCUMENT_CHARS, private_source=False)
        with self.assertRaises(ValueError):
            ProviderDocument("t", "x" * (MAX_DOCUMENT_CHARS + 1), private_source=False)

    def test_private_source_is_required_and_there_is_no_locator(self):
        with self.assertRaises(TypeError):
            ProviderDocument("t", "x")
        self.assertEqual(
            [field.name for field in dataclasses.fields(ProviderDocument)],
            ["title", "text", "published_at", "source_type", "private_source"],
        )

    def test_naive_datetime_and_bad_types_are_rejected(self):
        with self.assertRaises(ValueError):
            ProviderDocument(published_at=datetime(2026, 1, 1), private_source=False)
        with self.assertRaises(TypeError):
            ProviderDocument(source_type="docs", private_source=False)
        with self.assertRaises(TypeError):
            ProviderDocument(private_source=None)


class SourceMetadataTest(unittest.TestCase):
    def test_valid_metadata_and_defaults(self):
        value = source()
        self.assertEqual(value.source_type, SourceType.UNKNOWN)
        self.assertIsNone(value.published_at)
        self.assertIs(value.private_source, False)

    def test_field_set_is_exactly_the_unified_shape(self):
        self.assertEqual(
            [field.name for field in dataclasses.fields(SourceMetadata)],
            [
                "provider_kind",
                "provider_id",
                "locator",
                "title",
                "retrieved_at",
                "content_hash",
                "source_type",
                "published_at",
                "private_source",
            ],
        )

    def test_to_dict_is_json_ready_and_exact(self):
        value = source(
            source_type=SourceType.OFFICIAL_DOCS,
            published_at=datetime(2026, 9, 1, tzinfo=UTC),
            private_source=True,
        )
        expected = {
            "provider_kind": "web",
            "provider_id": "web-a",
            "locator": "https://example.com/a",
            "title": "Title",
            "retrieved_at": "2026-09-24T12:00:00+00:00",
            "content_hash": HASH,
            "source_type": "official_docs",
            "published_at": "2026-09-01T00:00:00+00:00",
            "private_source": True,
        }
        self.assertEqual(value.to_dict(), expected)
        self.assertEqual(json.loads(json.dumps(value.to_dict())), expected)
        self.assertIsNone(source().to_dict()["published_at"])

    def test_provider_id_pattern(self):
        for provider_id in ("a", "0", "web-a", "a_b", "a" * 64):
            with self.subTest(provider_id=provider_id):
                self.assertEqual(
                    source(provider_id=provider_id).provider_id, provider_id
                )
        for provider_id in ("", "Web", "a b", "-a", "_a", "a" * 65, "a\n", "é"):
            with self.subTest(provider_id=provider_id), self.assertRaises(ValueError):
                source(provider_id=provider_id)
        with self.assertRaises(TypeError):
            source(provider_id=None)

    def test_provider_kind_must_be_a_member(self):
        with self.assertRaises(TypeError):
            source(provider_kind="web")

    def test_locator_must_be_a_sanitised_http_url(self):
        for locator in (
            "",
            "ftp://example.com/",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "example.com/a",
            "HTTPS://example.com/",
            "https:///path",
            "https://user@example.com/",
            "https://user:pw@example.com/",
            "https://example.com/a#frag",
            "https://example.com/a b",
            "https://example.com/a\n",
            "https://example.com/a\x00",
        ):
            with self.subTest(locator=locator), self.assertRaises(ValueError):
                source(locator=locator)
        with self.assertRaises(TypeError):
            source(locator=None)

    def test_locator_length_boundary(self):
        prefix = "https://a.example/"
        self.assertEqual(
            len(
                source(locator=prefix + "x" * (MAX_LOCATOR_CHARS - len(prefix))).locator
            ),
            MAX_LOCATOR_CHARS,
        )
        with self.assertRaises(ValueError):
            source(locator=prefix + "x" * (MAX_LOCATOR_CHARS - len(prefix) + 1))

    def test_title_bound_and_type(self):
        self.assertEqual(source(title="").title, "")
        self.assertEqual(
            len(source(title="t" * MAX_TITLE_CHARS).title), MAX_TITLE_CHARS
        )
        with self.assertRaises(ValueError):
            source(title="t" * (MAX_TITLE_CHARS + 1))
        with self.assertRaises(TypeError):
            source(title=None)

    def test_timestamps_must_be_utc(self):
        for name in ("retrieved_at", "published_at"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    source(**{name: datetime(2026, 1, 1)})
                with self.assertRaises(ValueError):
                    source(**{name: datetime(2026, 1, 1, 9, tzinfo=JST)})
                with self.assertRaises(TypeError):
                    source(**{name: "2026-01-01T00:00:00+00:00"})

    def test_content_hash_format(self):
        for value in (
            "sha256:" + "A" * 64,
            "sha256:" + "0" * 63,
            "sha256:" + "0" * 65,
            "0" * 64,
            "sha1:" + "0" * 64,
            "sha256:" + "0" * 64 + "\n",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                source(content_hash=value)
        with self.assertRaises(TypeError):
            source(content_hash=None)

    def test_type_checks_of_flags(self):
        with self.assertRaises(TypeError):
            source(private_source=1)
        with self.assertRaises(TypeError):
            source(source_type="primary")

    def test_is_immutable_and_hashable(self):
        value = source()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            value.title = "x"
        self.assertEqual(hash(value), hash(source()))


class ResearchItemAndErrorTest(unittest.TestCase):
    def test_item_holds_source_and_text_only(self):
        item = ResearchItem(source(), "text")
        self.assertEqual(
            [field.name for field in dataclasses.fields(ResearchItem)],
            ["source", "text"],
        )
        self.assertEqual(item.to_dict(), {"source": source().to_dict(), "text": "text"})

    def test_item_validation(self):
        with self.assertRaises(TypeError):
            ResearchItem({"locator": "x"}, "text")
        with self.assertRaises(TypeError):
            ResearchItem(source(), None)
        with self.assertRaises(ValueError):
            ResearchItem(source(), "x" * (MAX_DOCUMENT_CHARS + 1))

    def test_error_is_a_code_only(self):
        error = ResearchError("web-a", ProviderKind.WEB, ResearchErrorCode.TIMEOUT)
        self.assertEqual(
            [field.name for field in dataclasses.fields(ResearchError)],
            ["provider_id", "kind", "code"],
        )
        self.assertEqual(
            error.to_dict(),
            {"provider_id": "web-a", "kind": "web", "code": "timeout"},
        )

    def test_error_code_must_be_an_enum_member_never_text(self):
        with self.assertRaises(TypeError):
            ResearchError("web-a", ProviderKind.WEB, "timeout")
        with self.assertRaises(TypeError):
            ResearchError("web-a", ProviderKind.WEB, f"failed: {SECRET}")
        with self.assertRaises(TypeError):
            ResearchError("web-a", "web", ResearchErrorCode.TIMEOUT)
        with self.assertRaises(ValueError):
            ResearchError("Web A", ProviderKind.WEB, ResearchErrorCode.TIMEOUT)


class ResearchResultTest(unittest.TestCase):
    def item(self, locator: str) -> ResearchItem:
        return ResearchItem(source(locator=locator), "t")

    def error(self, provider_id: str = "web-a") -> ResearchError:
        return ResearchError(provider_id, ProviderKind.WEB, ResearchErrorCode.TIMEOUT)

    def test_empty_result(self):
        result = ResearchResult()
        self.assertEqual(
            (result.items, result.errors, result.providers_queried, result.truncated),
            ((), (), 0, False),
        )
        self.assertFalse(result.all_failed)

    def test_all_failed_needs_queried_providers_and_only_failures(self):
        self.assertFalse(ResearchResult(providers_queried=2).all_failed)
        self.assertFalse(
            ResearchResult(errors=(self.error(),), providers_queried=2).all_failed
        )
        self.assertTrue(
            ResearchResult(errors=(self.error(),), providers_queried=1).all_failed
        )

    def test_items_must_have_unique_locators(self):
        with self.assertRaises(ValueError):
            ResearchResult(
                items=(
                    self.item("https://a.example/"),
                    self.item("https://a.example/"),
                ),
                providers_queried=1,
            )

    def test_containers_must_be_tuples_of_the_right_types(self):
        with self.assertRaises(TypeError):
            ResearchResult(items=[self.item("https://a.example/")])
        with self.assertRaises(TypeError):
            ResearchResult(items=("https://a.example/",))
        with self.assertRaises(TypeError):
            ResearchResult(errors=[self.error()], providers_queried=1)
        with self.assertRaises(TypeError):
            ResearchResult(errors=("timeout",), providers_queried=1)

    def test_counts_are_validated(self):
        with self.assertRaises(ValueError):
            ResearchResult(
                errors=(self.error(), self.error("web-b")), providers_queried=1
            )
        with self.assertRaises(ValueError):
            ResearchResult(providers_queried=-1)
        with self.assertRaises(TypeError):
            ResearchResult(providers_queried=True)
        with self.assertRaises(TypeError):
            ResearchResult(truncated=1)

    def test_item_count_is_bounded(self):
        items = tuple(
            self.item(f"https://a.example/{n}") for n in range(MAX_RESULTS_LIMIT + 1)
        )
        with self.assertRaises(ValueError):
            ResearchResult(items=items, providers_queried=1)

    def test_to_dict_is_json_ready(self):
        result = ResearchResult(
            items=(self.item("https://a.example/"),),
            errors=(self.error(),),
            providers_queried=2,
            truncated=True,
        )
        self.assertEqual(
            json.loads(json.dumps(result.to_dict())),
            {
                "items": [self.item("https://a.example/").to_dict()],
                "errors": [{"provider_id": "web-a", "kind": "web", "code": "timeout"}],
                "providers_queried": 2,
                "truncated": True,
            },
        )


class ErrorClassesTest(unittest.TestCase):
    def test_provider_failure_carries_a_code_and_no_text(self):
        failure = ProviderFailure(ResearchErrorCode.RATE_LIMITED)
        self.assertIs(failure.code, ResearchErrorCode.RATE_LIMITED)
        self.assertEqual(str(failure), "rate_limited")

    def test_provider_failure_rejects_text_codes(self):
        for code in ("rate_limited", f"boom {SECRET}", None, 7):
            with self.subTest(code=code), self.assertRaises(TypeError):
                ProviderFailure(code)

    def test_fixed_messages_and_bases(self):
        self.assertEqual(str(InvalidLocatorError()), "Invalid source locator")
        self.assertIsInstance(InvalidLocatorError(), ValueError)
        self.assertEqual(
            str(InvalidProviderResponseError()), "Invalid provider response"
        )
        for error in (
            ProviderInterfaceError("name"),
            DuplicateProviderError(),
            RegistryFullError(),
            UnknownProviderError(),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertIsInstance(error, ProviderRegistryError)
        self.assertIsInstance(ProviderInterfaceError("name"), TypeError)
        self.assertIsInstance(DuplicateProviderError(), ValueError)
        self.assertIsInstance(UnknownProviderError(), LookupError)

    def test_interface_error_member_is_a_closed_set(self):
        for member in ("name", "kind", "search", "fetch"):
            with self.subTest(member=member):
                error = ProviderInterfaceError(member)
                self.assertEqual(error.member, member)
                self.assertEqual(
                    str(error), f"Provider does not satisfy ResearchProvider: {member}"
                )
        with self.assertRaises(ValueError):
            ProviderInterfaceError(SECRET)


class StaticProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_satisfies_the_protocol_structurally(self):
        self.assertIsInstance(StaticProvider("static"), ResearchProvider)
        self.assertEqual(StaticProvider("static").kind, ProviderKind.WEB)

    async def test_search_honours_the_limit_and_records_calls(self):
        hits = [hit(f"https://a.example/{n}") for n in range(3)]
        provider = StaticProvider("static", hits=hits)
        self.assertEqual(await provider.search("q", limit=2), tuple(hits[:2]))
        self.assertEqual(provider.search_calls, [("q", 2)])

    async def test_ignore_limit_returns_everything(self):
        hits = [hit(f"https://a.example/{n}") for n in range(3)]
        provider = StaticProvider("static", hits=hits, ignore_limit=True)
        self.assertEqual(len(await provider.search("q", limit=1)), 3)

    async def test_search_error_and_raw_response(self):
        boom = RuntimeError(SECRET)
        with self.assertRaises(RuntimeError) as caught:
            await StaticProvider("static", search_error=boom).search("q", limit=1)
        self.assertIs(caught.exception, boom)
        provider = StaticProvider("static", raw_search_response=None)
        self.assertIsNone(await provider.search("q", limit=1))

    async def test_hook_runs_before_the_answer(self):
        seen = []

        async def hook():
            seen.append("hook")

        provider = StaticProvider("static", hits=[hit()], before_search=hook)
        self.assertEqual(len(await provider.search("q", limit=5)), 1)
        self.assertEqual(seen, ["hook"])

    async def test_hang_waits_until_cancelled(self):
        provider = StaticProvider("static", hang=True)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(provider.search("q", limit=1), 0.05)
        self.assertTrue(provider.cancelled)

    async def test_fetch_returns_documents_or_not_found(self):
        doc = document()
        provider = StaticProvider("static", documents={"https://a.example/": doc})
        self.assertIs(await provider.fetch("https://a.example/"), doc)
        with self.assertRaises(ProviderFailure) as caught:
            await provider.fetch("https://a.example/missing")
        self.assertIs(caught.exception.code, ResearchErrorCode.NOT_FOUND)
        self.assertEqual(
            provider.fetch_calls, ["https://a.example/", "https://a.example/missing"]
        )

    async def test_fetch_error_raw_response_and_hang(self):
        with self.assertRaises(ValueError):
            await StaticProvider("s", fetch_error=ValueError("x")).fetch("u")
        self.assertEqual(
            await StaticProvider("s", raw_fetch_response="raw").fetch("u"), "raw"
        )
        hanging = StaticProvider("s", hang_fetch=True)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(hanging.fetch("u"), 0.05)
        self.assertTrue(hanging.cancelled)


if __name__ == "__main__":
    unittest.main()
