"""``validate_provider`` and ``ProviderRegistry``: up-front validation, order."""

import unittest
from unittest.mock import AsyncMock

from paw_backend.research.providers import (
    MAX_PROVIDER_TIMEOUT_SECONDS,
    MAX_PROVIDERS,
    DuplicateProviderError,
    ProviderInterfaceError,
    ProviderKind,
    ProviderRegistry,
    RegisteredProvider,
    RegistryFullError,
    StaticProvider,
    UnknownProviderError,
    validate_provider,
)

from .research_support import SECRET


class Good:
    """A provider that is not a StaticProvider but satisfies the interface."""

    name = "good"
    kind = ProviderKind.DOCS

    async def search(self, query, *, limit):
        return ()

    async def fetch(self, locator):
        raise NotImplementedError


def adapter(**overrides):
    """A ``Good`` instance with some members replaced (a wrong adapter)."""
    instance = Good()
    for key, value in overrides.items():
        setattr(instance, key, value)
    return instance


async def async_any(*args, **kwargs):
    return ()


def sync_search(query, *, limit):
    return ()


async def search_without_limit(query):
    return ()


async def search_with_required_extra(query, *, limit, extra):
    return ()


async def search_keyword_only_query(*, query, limit):
    return ()


async def search_positional_limit(query, limit):
    return ()


async def search_renamed(text, *, limit):
    return ()


async def search_extra_optional(query, *, limit, extra=None):
    return ()


async def fetch_two_required(locator, other):
    return None


async def fetch_no_parameter():
    return None


async def fetch_keyword_only(*, locator):
    return None


def sync_fetch(locator):
    return None


class ValidateProviderTest(unittest.TestCase):
    def assertRejected(self, provider, member: str) -> None:
        with self.assertRaises(ProviderInterfaceError) as caught:
            validate_provider(provider)
        self.assertEqual(caught.exception.member, member)
        self.assertIsInstance(caught.exception, TypeError)

    def test_valid_providers_return_none(self):
        self.assertIsNone(validate_provider(Good()))
        for kind in ProviderKind:
            with self.subTest(kind=kind):
                self.assertIsNone(validate_provider(StaticProvider("p", kind)))

    def test_flexible_but_compatible_signatures_are_accepted(self):
        for search in (
            async_any,
            search_positional_limit,
            search_renamed,
            search_extra_optional,
        ):
            with self.subTest(search=search.__name__):
                self.assertIsNone(validate_provider(adapter(search=search)))
        self.assertIsNone(validate_provider(adapter(fetch=async_any)))

    def test_the_name_must_be_a_valid_id(self):
        for name in ("", "Bad", "has space", "-x", "x" * 65, "é", "a\n", None, 5, b"a"):
            with self.subTest(name=name):
                self.assertRejected(adapter(name=name), "name")
        self.assertIsNone(validate_provider(adapter(name="x" * 64)))

    def test_a_missing_name_is_rejected(self):
        class NoName:
            kind = ProviderKind.WEB

            async def search(self, query, *, limit):
                return ()

            async def fetch(self, locator):
                return None

        self.assertRejected(NoName(), "name")

    def test_the_kind_must_be_a_member_not_a_string(self):
        for kind in ("web", "docs", None, 0, "unknown"):
            with self.subTest(kind=kind):
                self.assertRejected(adapter(kind=kind), "kind")

    def test_search_must_exist_and_be_a_coroutine_function(self):
        self.assertRejected(adapter(search=None), "search")
        self.assertRejected(adapter(search="not callable"), "search")
        self.assertRejected(adapter(search=sync_search), "search")

        class NoSearch:
            name = "x"
            kind = ProviderKind.WEB

            async def fetch(self, locator):
                return None

        self.assertRejected(NoSearch(), "search")

    def test_search_signature_must_accept_query_and_limit(self):
        for search in (
            search_without_limit,
            search_with_required_extra,
            search_keyword_only_query,
        ):
            with self.subTest(search=search.__name__):
                self.assertRejected(adapter(search=search), "search")

    def test_fetch_must_exist_be_async_and_take_one_locator(self):
        self.assertRejected(adapter(fetch=None), "fetch")
        self.assertRejected(adapter(fetch=sync_fetch), "fetch")
        for fetch in (fetch_two_required, fetch_no_parameter, fetch_keyword_only):
            with self.subTest(fetch=fetch.__name__):
                self.assertRejected(adapter(fetch=fetch), "fetch")

        class NoFetch:
            name = "x"
            kind = ProviderKind.WEB

            async def search(self, query, *, limit):
                return ()

        self.assertRejected(NoFetch(), "fetch")

    def test_members_are_checked_in_order(self):
        self.assertRejected(adapter(name="Bad", kind="x", search=None), "name")
        self.assertRejected(adapter(kind="x", search=None, fetch=None), "kind")
        self.assertRejected(adapter(search=None, fetch=None), "search")

    def test_things_that_are_not_providers_are_rejected(self):
        for value in (None, 5, "provider", object(), StaticProvider, AsyncMock()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ProviderInterfaceError):
                    validate_provider(value)

    def test_the_error_never_echoes_the_provider(self):
        with self.assertRaises(ProviderInterfaceError) as caught:
            validate_provider(adapter(name=f"Bad {SECRET}"))
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn(SECRET, repr(caught.exception))

    def test_nothing_is_called_on_the_provider(self):
        calls = []

        class Recording(Good):
            async def search(self, query, *, limit):
                calls.append("search")
                return ()

            async def fetch(self, locator):
                calls.append("fetch")
                return None

        validate_provider(Recording())
        self.assertEqual(calls, [])


class RegistrationTest(unittest.TestCase):
    def test_register_returns_the_entry(self):
        registry = ProviderRegistry()
        provider = StaticProvider("docs-main", ProviderKind.DOCS)
        entry = registry.register(provider, timeout_seconds=5)
        self.assertIsInstance(entry, RegisteredProvider)
        self.assertEqual(
            (entry.name, entry.kind, entry.timeout_seconds),
            ("docs-main", ProviderKind.DOCS, 5),
        )
        self.assertIs(entry.provider, provider)
        self.assertIs(registry.get("docs-main").provider, provider)

    def test_the_default_timeout_is_ten_seconds(self):
        entry = ProviderRegistry().register(StaticProvider("p"))
        self.assertEqual(entry.timeout_seconds, 10.0)

    def test_the_registry_starts_empty_and_instances_are_independent(self):
        first, second = ProviderRegistry(), ProviderRegistry()
        self.assertEqual((len(first), first.names(), first.select()), (0, (), ()))
        first.register(StaticProvider("p"))
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)
        self.assertNotIn("p", second)

    def test_names_are_unique_whatever_the_kind(self):
        registry = ProviderRegistry()
        registry.register(StaticProvider("same", ProviderKind.WEB))
        with self.assertRaises(DuplicateProviderError):
            registry.register(StaticProvider("same", ProviderKind.WEB))
        with self.assertRaises(DuplicateProviderError):
            registry.register(StaticProvider("same", ProviderKind.DOCS))
        self.assertEqual(len(registry), 1)
        self.assertEqual(registry.get("same").kind, ProviderKind.WEB)

    def test_the_same_provider_object_cannot_be_registered_twice(self):
        registry = ProviderRegistry()
        provider = StaticProvider("p")
        registry.register(provider)
        with self.assertRaises(DuplicateProviderError):
            registry.register(provider)

    def test_a_wrong_adapter_is_rejected_and_nothing_is_stored(self):
        registry = ProviderRegistry()
        with self.assertRaises(ProviderInterfaceError) as caught:
            registry.register(adapter(search=sync_search))
        self.assertEqual(caught.exception.member, "search")
        self.assertEqual((len(registry), registry.names()), (0, ()))

    def test_timeout_boundaries(self):
        registry = ProviderRegistry()
        registry.register(StaticProvider("low"), timeout_seconds=0.001)
        registry.register(
            StaticProvider("high"), timeout_seconds=MAX_PROVIDER_TIMEOUT_SECONDS
        )
        self.assertEqual(registry.get("low").timeout_seconds, 0.001)
        self.assertEqual(registry.get("high").timeout_seconds, 120.0)
        for value in (0, -1, 120.01, float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                registry.register(StaticProvider("bad"), timeout_seconds=value)
        for value in (True, "5", None, [1]):
            with self.subTest(value=value), self.assertRaises(TypeError):
                registry.register(StaticProvider("bad"), timeout_seconds=value)
        self.assertEqual(len(registry), 2)

    def test_the_interface_is_checked_before_the_timeout(self):
        with self.assertRaises(ProviderInterfaceError):
            ProviderRegistry().register(adapter(kind="web"), timeout_seconds=-1)

    def test_the_timeout_is_checked_before_the_duplicate(self):
        registry = ProviderRegistry()
        registry.register(StaticProvider("p"))
        with self.assertRaises(ValueError) as caught:
            registry.register(StaticProvider("p"), timeout_seconds=-1)
        self.assertNotIsInstance(caught.exception, DuplicateProviderError)

    def test_capacity(self):
        registry = ProviderRegistry()
        for number in range(MAX_PROVIDERS):
            registry.register(StaticProvider(f"p{number:02d}"))
        self.assertEqual(len(registry), MAX_PROVIDERS)
        with self.assertRaises(RegistryFullError):
            registry.register(StaticProvider("one-too-many"))
        self.assertEqual(len(registry), MAX_PROVIDERS)
        self.assertNotIn("one-too-many", registry)
        with self.assertRaises(DuplicateProviderError):
            registry.register(StaticProvider("p00"))

    def test_identity_is_a_snapshot_taken_at_registration(self):
        registry = ProviderRegistry()
        provider = StaticProvider("original", ProviderKind.WEB)
        registry.register(provider)
        provider.name = "renamed"
        provider.kind = ProviderKind.GITHUB
        self.assertEqual(registry.names(), ("original",))
        entry = registry.get("original")
        self.assertEqual((entry.name, entry.kind), ("original", ProviderKind.WEB))
        self.assertEqual(registry.select({ProviderKind.WEB}), (entry,))
        self.assertEqual(registry.select({ProviderKind.GITHUB}), ())
        self.assertNotIn("renamed", registry)


class LookupTest(unittest.TestCase):
    def build(self) -> ProviderRegistry:
        registry = ProviderRegistry()
        # Deliberately not in kind or name order.
        for name, kind in (
            ("gh-b", ProviderKind.GITHUB),
            ("web-z", ProviderKind.WEB),
            ("oc-c", ProviderKind.OPENCODE),
            ("docs-m", ProviderKind.DOCS),
            ("web-a", ProviderKind.WEB),
            ("gh-a", ProviderKind.GITHUB),
        ):
            registry.register(StaticProvider(name, kind))
        return registry

    def test_order_is_kind_priority_then_name(self):
        expected = ("web-a", "web-z", "docs-m", "gh-a", "gh-b", "oc-c")
        registry = self.build()
        self.assertEqual(registry.names(), expected)
        self.assertEqual(tuple(entry.name for entry in registry.select()), expected)
        self.assertEqual(tuple(entry.name for entry in registry.select(None)), expected)

    def test_order_does_not_depend_on_registration_order(self):
        reverse = ProviderRegistry()
        for entry in reversed(self.build().select()):
            reverse.register(entry.provider)
        self.assertEqual(reverse.names(), self.build().names())

    def test_select_filters_by_kind(self):
        registry = self.build()
        self.assertEqual(
            [e.name for e in registry.select({ProviderKind.WEB})], ["web-a", "web-z"]
        )
        self.assertEqual(
            [e.name for e in registry.select(frozenset({ProviderKind.OPENCODE}))],
            ["oc-c"],
        )
        self.assertEqual(
            [e.name for e in registry.select([ProviderKind.GITHUB, ProviderKind.DOCS])],
            ["docs-m", "gh-a", "gh-b"],
        )
        self.assertEqual(
            [e.name for e in registry.select([ProviderKind.WEB, ProviderKind.WEB])],
            ["web-a", "web-z"],
        )

    def test_select_with_no_kinds_or_an_unused_kind_is_empty(self):
        registry = ProviderRegistry()
        registry.register(StaticProvider("web-a", ProviderKind.WEB))
        self.assertEqual(registry.select([]), ())
        self.assertEqual(registry.select(frozenset()), ())
        self.assertEqual(registry.select({ProviderKind.GITHUB}), ())

    def test_select_rejects_anything_but_provider_kinds(self):
        registry = self.build()
        for kinds in ("web", ["web"], [ProviderKind.WEB, "docs"], [None], 5):
            with self.subTest(kinds=kinds), self.assertRaises(TypeError):
                registry.select(kinds)

    def test_select_returns_a_snapshot_tuple(self):
        registry = ProviderRegistry()
        registry.register(StaticProvider("web-a"))
        snapshot = registry.select()
        registry.register(StaticProvider("web-b"))
        self.assertIsInstance(snapshot, tuple)
        self.assertEqual([entry.name for entry in snapshot], ["web-a"])
        self.assertEqual(
            [entry.name for entry in registry.select()], ["web-a", "web-b"]
        )

    def test_get_and_contains(self):
        registry = self.build()
        self.assertEqual(registry.get("docs-m").kind, ProviderKind.DOCS)
        self.assertIn("web-a", registry)
        self.assertNotIn("nope", registry)
        self.assertEqual(len(registry), 6)
        with self.assertRaises(UnknownProviderError):
            registry.get("nope")
        with self.assertRaises(UnknownProviderError):
            registry.get("WEB-A")
        for name in (None, 5, b"web-a"):
            with self.subTest(name=name):
                with self.assertRaises(TypeError):
                    registry.get(name)
                self.assertNotIn(name, registry)

    def test_the_unknown_provider_error_does_not_echo_the_name(self):
        with self.assertRaises(UnknownProviderError) as caught:
            ProviderRegistry().get(SECRET)
        self.assertNotIn(SECRET, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
