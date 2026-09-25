"""``validate_provider`` and ``ProviderRegistry``: up-front validation, order."""

import asyncio
import inspect
import logging
import re
import traceback
import unicodedata
import unittest
from enum import StrEnum
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
    ResearchBroker,
    ResearchRequest,
    StaticProvider,
    UnknownProviderError,
    validate_provider,
)
from paw_backend.research.providers.contract import PROVIDER_NAME_PATTERN

from .research_support import (
    BASE_EXCEPTIONS,
    FORGED,
    GUARD_SECONDS,
    NOW,
    SECRET,
    cancel_current_task,
    fixed_clock,
    hit,
)


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


class IdentitySnapshotTest(unittest.TestCase):
    """The name and kind that are validated are the ones that are registered."""

    def flipping_provider(self, names, kinds=None):
        reads = {"name": 0, "kind": 0}
        kinds = kinds or [ProviderKind.WEB] * 8

        class Flipping(Good):
            @property
            def name(self):
                reads["name"] += 1
                return names[min(reads["name"], len(names)) - 1]

            @property
            def kind(self):
                reads["kind"] += 1
                return kinds[min(reads["kind"], len(kinds)) - 1]

        return Flipping(), reads

    def test_a_name_that_changes_between_reads_registers_the_validated_one(self):
        provider, reads = self.flipping_provider(["ok", "BAD!", "BAD!"])
        registry = ProviderRegistry()

        entry = registry.register(provider, timeout_seconds=5)

        self.assertEqual(entry.name, "ok")  # never the unvalidated second read
        self.assertEqual(registry.names(), ("ok",))
        self.assertEqual(reads["name"], 1)  # read exactly once

    def test_a_kind_that_changes_between_reads_registers_the_validated_one(self):
        provider, reads = self.flipping_provider(
            ["flip"], [ProviderKind.DOCS, "web", ProviderKind.GITHUB]
        )
        registry = ProviderRegistry()

        entry = registry.register(provider, timeout_seconds=5)

        self.assertEqual(entry.kind, ProviderKind.DOCS)
        self.assertEqual(reads["kind"], 1)

    def test_an_invalid_first_read_is_rejected_whatever_follows(self):
        provider, _ = self.flipping_provider(["BAD!", "ok"])
        with self.assertRaises(ProviderInterfaceError) as caught:
            ProviderRegistry().register(provider, timeout_seconds=5)
        self.assertEqual(caught.exception.member, "name")


def hostile_name(content: str, **hooks) -> str:
    """A ``str`` subclass instance holding ``content`` whose hooks are overridden.

    Every hook that the registry, the broker or a log line could reach by
    accident is replaced (by ``hooks`` or by one that raises or lies).
    """

    def boom(name):
        def hook(self, *args, **kwargs):
            raise RuntimeError(f"adapter code ran: {name}")

        return hook

    def secret(self, *args, **kwargs):
        return SECRET

    attributes = {
        "__hash__": boom("__hash__"),
        "__lt__": boom("__lt__"),
        "__gt__": boom("__gt__"),
        "__le__": boom("__le__"),
        "__ge__": boom("__ge__"),
        "__str__": secret,
        "__repr__": secret,
        "__format__": secret,
        "encode": boom("encode"),
        "__getitem__": boom("__getitem__"),
        "__iter__": boom("__iter__"),
        "__len__": boom("__len__"),
        "__add__": boom("__add__"),
        "__radd__": boom("__radd__"),
    }
    attributes.update(hooks)
    return type("HostileName", (str,), attributes)(content)


def named(name, kind=ProviderKind.WEB):
    return adapter(name=name, kind=kind)


class ProviderNameSpellingTest(unittest.TestCase):
    """The name pattern leaves no room for look-alikes, so nothing is normalised.

    ``PROVIDER_NAME_PATTERN`` is ASCII only and is applied with ``fullmatch`` to
    the ORIGINAL string: a name that is not already in its final form is
    rejected, never rewritten (NFKC, case folding, stripping) into a form that
    could equal another provider's name. ``$`` would accept a trailing newline;
    ``fullmatch`` does not (and the pattern has no ``$``).
    """

    LOOKALIKES = {
        "full-width letters": "\uff57\uff45\uff42",  # ｗｅｂ, NFKC -> "web"
        "one full-width letter": "w\uff45b",
        "full-width digits": "docs\uff11",  # docs１
        "full-width hyphen": "a\uff0db",
        "full-width low line": "a\uff3fb",
        "ligature (NFKC: fi)": "\ufb01le",  # ﬁle
        "circled letters": "\u24e6eb",  # ⓦeb
        "superscript digit": "web\u00b2",
        "roman numeral": "web\u2160",
        "Kelvin sign (casefold: k)": "\u212aey",
        "long s (casefold: s)": "\u017ftatic",
        "dotless i": "d\u0131rect",
        "Turkish dotted I": "\u0130ssues",
        "Cyrillic a": "w\u0435b",  # looks like "web"
        "Greek omicron": "d\u03bfcs",
        "combining accent": "e\u0301",
        "precomposed accent": "\u00e9",
        "upper case": "Web",
        "upper case only": "WEB",
        "trailing newline": "web\n",
        "trailing CRLF": "web\r\n",
        "leading newline": "\nweb",
        "trailing space": "web ",
        "leading space": " web",
        "inner space": "we b",
        "tab": "we\tb",
        "vertical tab": "web\x0b",
        "form feed": "web\x0c",
        "no-break space": "we\u00a0b",
        "ideographic space": "web\u3000",
        "line separator": "web\u2028",
        "next line": "web\x85",
        "zero-width space": "we\u200bb",
        "zero-width non-joiner": "web\u200c",
        "zero-width joiner": "\u200dweb",
        "word joiner": "web\u2060",
        "byte order mark": "\ufeffweb",
        "soft hyphen": "we\u00adb",
        "right-to-left mark": "web\u200f",
        "NUL": "web\x00",
        "NUL first": "\x00web",
        "DEL": "web\x7f",
        "lone surrogate": "web\ud800",
        "astral letter (math bold)": "\U0001d420eb",
        "empty": "",
        "only a hyphen": "-",
        "leading underscore": "_web",
        "dot": "web.a",
        "65 characters": "a" * 65,
    }

    def test_the_pattern_admits_nothing_but_plain_ascii_id_characters(self):
        # Every code point, at the first and at a later position.
        first_ok = {chr(c) for c in [*range(0x30, 0x3A), *range(0x61, 0x7B)]}
        later_ok = first_ok | {"_", "-"}
        wrong = []
        for code in range(0x110000):
            char = chr(code)
            if (PROVIDER_NAME_PATTERN.fullmatch(char) is not None) != (
                char in first_ok
            ):
                wrong.append(("first", hex(code)))
            if (PROVIDER_NAME_PATTERN.fullmatch("a" + char) is not None) != (
                char in later_ok
            ):
                wrong.append(("later", hex(code)))
        self.assertEqual(wrong, [])

    def test_the_pattern_has_no_flag_that_widens_it(self):
        # IGNORECASE would let the Kelvin sign and the long s in.
        self.assertEqual(PROVIDER_NAME_PATTERN.flags, re.UNICODE)
        self.assertNotIn("$", PROVIDER_NAME_PATTERN.pattern)

    def test_an_accepted_name_is_already_in_every_normal_form(self):
        for name in ("web", "a", "docs-2", "x_y", "0", "a" * 64, "9" + "-_" * 31):
            with self.subTest(name=name):
                self.assertIsNotNone(PROVIDER_NAME_PATTERN.fullmatch(name))
                for form in ("NFC", "NFD", "NFKC", "NFKD"):
                    self.assertEqual(unicodedata.normalize(form, name), name)
                self.assertEqual(name.casefold(), name)
                self.assertEqual(name.strip(), name)
                self.assertTrue(name.isascii())

    def test_look_alikes_are_rejected_not_normalised(self):
        for label, name in self.LOOKALIKES.items():
            with self.subTest(label):
                with self.assertRaises(ProviderInterfaceError) as caught:
                    validate_provider(named(name))
                self.assertEqual(caught.exception.member, "name")
                registry = ProviderRegistry()
                with self.assertRaises(ProviderInterfaceError):
                    registry.register(named(name))
                self.assertEqual(len(registry), 0)
                self.assertEqual(registry.names(), ())

    def test_a_look_alike_of_a_registered_name_does_not_join_it(self):
        registry = ProviderRegistry()
        registry.register(named("web"))
        self.assertEqual(unicodedata.normalize("NFKC", "\uff57\uff45\uff42"), "web")
        for name in ("\uff57\uff45\uff42", "WEB", "web\n", "web ", "we\u200bb"):
            with self.subTest(name=name):
                # Not a DuplicateProviderError (that would mean it was
                # normalised into "web"), and never stored.
                with self.assertRaises(ProviderInterfaceError):
                    registry.register(named(name))
        self.assertEqual(registry.names(), ("web",))
        self.assertEqual(len(registry), 1)

    def test_lookups_do_not_normalise_either(self):
        registry = ProviderRegistry()
        registry.register(named("web"))
        for name in ("\uff57\uff45\uff42", "WEB", "web\n", " web"):
            with self.subTest(name=name):
                self.assertNotIn(name, registry)
                with self.assertRaises(UnknownProviderError):
                    registry.get(name)


class ProviderNameTypeTest(unittest.TestCase):
    """The registry stores an exact ``str`` copy; a ``str`` subclass cannot act."""

    def test_a_subclass_is_stored_as_a_plain_copy(self):
        registry = ProviderRegistry()

        entry = registry.register(named(hostile_name("web-a")))

        self.assertIs(type(entry.name), str)
        self.assertEqual(entry.name, "web-a")
        self.assertIs(type(registry.names()[0]), str)
        self.assertIs(type(registry.get("web-a").name), str)
        self.assertIs(type(registry.select()[0].name), str)
        self.assertEqual(registry.names(), ("web-a",))
        # Formatting the stored name cannot yield credential text.
        for text in (str(entry.name), f"{entry.name}", "%s" % entry.name, repr(entry)):  # noqa: UP031
            self.assertNotIn(SECRET, text)
        self.assertIn("web-a", f"{entry.name}")

    def test_a_subclass_with_an_overridden_hash_or_order_does_not_break_the_registry(
        self,
    ):
        registry = ProviderRegistry()
        for name, kind in (
            ("b", ProviderKind.DOCS),
            ("a", ProviderKind.DOCS),
            ("c", ProviderKind.WEB),
        ):
            registry.register(named(hostile_name(name), kind))

        self.assertEqual(registry.names(), ("c", "a", "b"))  # kind, then name
        self.assertEqual(
            [e.name for e in registry.select({ProviderKind.DOCS})], list("ab")
        )
        self.assertIn("a", registry)
        self.assertEqual(registry.get("b").kind, ProviderKind.DOCS)

    def test_a_subclass_that_denies_being_equal_cannot_bypass_uniqueness(self):
        for first, second in (
            (
                hostile_name(
                    "dup", __eq__=lambda self, other: False, __hash__=str.__hash__
                ),
                "dup",
            ),
            (
                "dup",
                hostile_name(
                    "dup", __eq__=lambda self, other: False, __hash__=str.__hash__
                ),
            ),
            (
                hostile_name("dup", __eq__=lambda s, o: False, __hash__=str.__hash__),
                hostile_name("dup", __eq__=lambda s, o: False, __hash__=str.__hash__),
            ),
        ):
            with self.subTest(first=type(first).__name__, second=type(second).__name__):
                registry = ProviderRegistry()
                registry.register(named(first))
                with self.assertRaises(DuplicateProviderError):
                    registry.register(named(second))
                self.assertEqual(len(registry), 1)

    def test_a_subclass_is_validated_by_its_real_content(self):
        """Overridden methods cannot make a bad content pass, or a good one fail."""
        claim_ok = {"__len__": lambda self: 2, "__getitem__": lambda self, i: "o"}
        for content in ("Bad", "web\n", "\uff57eb", "", "a" * 65):
            with self.subTest(content=content):
                self.assertRaisesInterface(
                    named(
                        hostile_name(content, __eq__=lambda s, o: o == "ok", **claim_ok)
                    )
                )
        liar = hostile_name("ok", __len__=lambda self: 500, __eq__=lambda s, o: False)
        self.assertEqual(ProviderRegistry().register(named(liar)).name, "ok")

    def assertRaisesInterface(self, provider):
        with self.assertRaises(ProviderInterfaceError) as caught:
            ProviderRegistry().register(provider)
        self.assertEqual(caught.exception.member, "name")

    def test_an_object_that_only_claims_to_be_a_str_is_not_a_name(self):
        class ClaimsToBeStr:
            @property
            def __class__(self):
                return str

            def __hash__(self):
                raise RuntimeError("adapter code ran: __hash__")

        claimant = ClaimsToBeStr()
        self.assertTrue(isinstance(claimant, str))  # what isinstance would believe
        registry = ProviderRegistry()
        with self.assertRaises(ProviderInterfaceError) as caught:
            registry.register(named(claimant))
        self.assertEqual(caught.exception.member, "name")
        with self.assertRaises(ProviderInterfaceError):
            validate_provider(named(claimant))
        self.assertEqual(len(registry), 0)

    def test_an_object_that_only_claims_to_be_a_kind_is_not_a_kind(self):
        class ClaimsToBeKind:
            @property
            def __class__(self):
                return ProviderKind

            def __hash__(self):
                raise RuntimeError("adapter code ran: __hash__")

        claimant = ClaimsToBeKind()
        self.assertTrue(isinstance(claimant, ProviderKind))
        registry = ProviderRegistry()
        with self.assertRaises(ProviderInterfaceError) as caught:
            registry.register(named("ok", claimant))
        self.assertEqual(caught.exception.member, "kind")
        self.assertEqual(len(registry), 0)

    def test_a_str_enum_member_is_a_fine_name(self):
        class Names(StrEnum):
            DOCS = "docs-x"

        entry = ProviderRegistry().register(named(Names.DOCS))

        self.assertIs(type(entry.name), str)
        self.assertEqual(entry.name, "docs-x")

    def test_the_stored_name_is_not_the_object_the_provider_returned(self):
        original = hostile_name("kept")
        entry = ProviderRegistry().register(named(original))
        self.assertIsNot(entry.name, original)


class SubclassedNameThroughTheBrokerTest(unittest.IsolatedAsyncioTestCase):
    """Results and errors carry plain names; a hostile name cannot fail a call."""

    async def test_gather_runs_with_hostile_names_and_reports_plain_ones(self):
        registry = ProviderRegistry()
        for name, kind, provider in (
            ("b-docs", ProviderKind.DOCS, StaticProvider("x", hits=[hit()])),
            ("a-docs", ProviderKind.DOCS, StaticProvider("x", raw_search_response=5)),
            (
                "c-web",
                ProviderKind.WEB,
                StaticProvider("x", hits=[hit("https://e.com/w")]),
            ),
        ):
            provider.name = hostile_name(name)
            provider.kind = kind
            registry.register(provider)
        # No privacy gate here: the explicit opt-out (PAW-053 fails closed without it).
        broker = ResearchBroker(registry, clock=fixed_clock(NOW), unfiltered=True)

        result = await asyncio.wait_for(
            broker.gather(ResearchRequest("python asyncio")), GUARD_SECONDS
        )

        self.assertEqual(
            [(item.source.provider_id) for item in result.items],
            ["c-web", "b-docs"],
        )
        self.assertEqual([error.provider_id for error in result.errors], ["a-docs"])
        for name in (
            *[item.source.provider_id for item in result.items],
            *[error.provider_id for error in result.errors],
        ):
            self.assertIs(type(name), str)
            self.assertNotIn(SECRET, f"{name}")


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


# Text that must never leave the adapter: a credential, and a newline followed by a
# line that would forge a log record.
HOSTILE_TEXT = f"{SECRET}\nWARNING forged log line {FORGED}"
MEMBERS = ("name", "kind", "search", "fetch")


class WeirdError(Exception):
    """An adapter exception whose own hooks (text, repr, class) are hostile."""

    def __str__(self):
        return HOSTILE_TEXT

    def __repr__(self):
        return HOSTILE_TEXT


# Label -> an exception class raised as ``cls(HOSTILE_TEXT)``.
HOOK_EXCEPTIONS: dict[str, type[BaseException]] = {
    "RuntimeError": RuntimeError,
    "RecursionError": RecursionError,
    "an exception with hostile __str__ / __repr__": WeirdError,
    **BASE_EXCEPTIONS,
}


async def good_search(self, query, *, limit):
    return ()


async def good_fetch(self, locator):
    return None


GOOD_MEMBERS = {
    "name": "hostile-ok",
    "kind": ProviderKind.WEB,
    "search": good_search,
    "fetch": good_fetch,
}


def raising_property(member, make, calls):
    def getter(self):
        calls.append(member)
        raise make(HOSTILE_TEXT)

    return type("RaisingProperty", (Good,), {member: property(getter)})()


def raising_getattribute(member, make, calls):
    def hook(self, attr):
        if attr == member:
            calls.append(member)
            raise make(HOSTILE_TEXT)
        return object.__getattribute__(self, attr)

    return type("RaisingGetattribute", (Good,), {"__getattribute__": hook})()


def raising_getattr(member, make, calls):
    """The member is missing, so Python asks ``__getattr__``, which raises."""
    members = {key: value for key, value in GOOD_MEMBERS.items() if key != member}

    def hook(self, attr):
        calls.append(attr)
        raise make(HOSTILE_TEXT)

    members["__getattr__"] = hook
    return type("RaisingGetattr", (), members)()


def raising_descriptor(member, make, calls):
    class Raising:
        def __get__(self, instance, owner):
            calls.append(member)
            raise make(HOSTILE_TEXT)

    return type("RaisingDescriptor", (Good,), {member: Raising()})()


READ_STYLES = {
    "a property": raising_property,
    "__getattribute__": raising_getattribute,
    "__getattr__": raising_getattr,
    "a descriptor": raising_descriptor,
}


def hostile_instance(make, calls):
    """An object (callable) whose every attribute read raises, ``__class__`` included.

    ``inspect.iscoroutinefunction`` asks an object for its ``__class__`` (through
    ``isinstance``), so what ``search`` / ``fetch`` return is adapter code too.
    """

    def hook(self, attr):
        calls.append(attr)
        raise make(HOSTILE_TEXT)

    async def call(self, *args, **kwargs):
        return None

    return type("HostileCallable", (), {"__getattribute__": hook, "__call__": call})()


def hostile_metaclass_class(make, calls):
    """A class (so callable) whose metaclass raises on every attribute read."""

    class Meta(type):
        def __getattribute__(cls, attr):
            calls.append(attr)
            raise make(HOSTILE_TEXT)

    return Meta("HostileClass", (), {})


def hostile_signature_function(make, calls):
    """An ``async def`` whose ``__signature__`` is a ``Signature`` that raises."""

    class Raising(inspect.Signature):
        def bind(self, *args, **kwargs):
            calls.append("bind")
            raise make(HOSTILE_TEXT)

    async def method(*args, **kwargs):
        return None

    method.__signature__ = Raising(
        [
            inspect.Parameter("query", inspect.Parameter.POSITIONAL_OR_KEYWORD),
            inspect.Parameter("limit", inspect.Parameter.KEYWORD_ONLY),
        ]
    )
    return method


PROBE_STYLES = {
    "an object that raises on any attribute read": hostile_instance,
    "a class with a metaclass that raises on any attribute read": (
        hostile_metaclass_class
    ),
    "a Signature whose bind raises": hostile_signature_function,
}


class HostileAdapterTestCase(unittest.TestCase):
    def assertFixedRejection(self, member, function, *args, **kwargs):
        """``function(...)`` raises exactly ``ProviderInterfaceError(member)``.

        The error must be self-contained: no text of the adapter in ``str`` /
        ``repr`` / ``args`` / the formatted traceback (what ``logger.exception``
        would write), and no adapter exception as ``__context__`` or ``__cause__``.
        """
        caught = escaped = None
        try:
            function(*args, **kwargs)
        except ProviderInterfaceError as error:
            caught = error
        except BaseException as error:  # the adapter's own exception escaped
            escaped = type(error).__name__
        if escaped is not None:
            self.fail(f"{escaped} of the adapter escaped ({member})")
        if caught is None:
            self.fail(f"no error was raised ({member})")
        self.assertIs(type(caught), ProviderInterfaceError)
        self.assertEqual(caught.member, member)
        message = f"Provider does not satisfy ResearchProvider: {member}"
        self.assertEqual(caught.args, (message,))
        self.assertEqual(str(caught), message)
        self.assertEqual(repr(caught), f"ProviderInterfaceError({message!r})")
        self.assertIsNone(caught.__cause__)
        self.assertIsNone(caught.__context__)
        record = logging.LogRecord(
            "paw_backend.research.providers",
            logging.ERROR,
            __file__,
            0,
            "registration failed",
            (),
            (type(caught), caught, caught.__traceback__),
        )
        logged = logging.Formatter().format(record)
        rendered = "".join(traceback.format_exception(caught)) + logged
        for text in (SECRET, FORGED, "forged log line", "WeirdError"):
            self.assertNotIn(text, rendered)


class HostileMemberReadTest(HostileAdapterTestCase):
    """Reading a member is adapter code: whatever it raises is a fixed error.

    A property, ``__getattribute__``, ``__getattr__`` or descriptor of the adapter
    can raise anything, ``BaseException`` included. Neither ``validate_provider``
    nor ``register`` may let it (or its text) out: the caller gets the documented
    ``ProviderInterfaceError`` for that member, with nothing chained to it.
    """

    def test_a_raising_read_of_any_member_is_the_fixed_interface_error(self):
        for member in MEMBERS:
            for style, build in READ_STYLES.items():
                for label, make in HOOK_EXCEPTIONS.items():
                    with self.subTest(member=member, style=style, raises=label):
                        calls = []
                        provider = build(member, make, calls)
                        self.assertFixedRejection(member, validate_provider, provider)
                        self.assertEqual(len(calls), 1)  # the hook did run

                        calls = []
                        provider = build(member, make, calls)
                        registry = ProviderRegistry()
                        self.assertFixedRejection(
                            member, registry.register, provider, timeout_seconds=5
                        )
                        self.assertEqual(len(calls), 1)
                        self.assertEqual(len(registry), 0)
                        self.assertEqual(registry.names(), ())

    def test_the_first_failing_member_is_the_one_reported(self):
        calls = []
        provider = raising_property("kind", RuntimeError, calls)
        provider.search = None  # also wrong, but kind comes first
        self.assertFixedRejection("kind", validate_provider, provider)
        # A hostile name wins over a hostile kind.
        both = type(
            "Both",
            (Good,),
            {
                "name": property(lambda self: 1 / 0),
                "kind": property(lambda self: 1 / 0),
            },
        )()
        self.assertFixedRejection("name", validate_provider, both)

    def test_a_registry_that_failed_on_a_hostile_read_stays_usable(self):
        registry = ProviderRegistry()
        registry.register(adapter(name="first"), timeout_seconds=5)
        provider = raising_property("fetch", RuntimeError, [])
        self.assertFixedRejection("fetch", registry.register, provider)
        registry.register(adapter(name="second"), timeout_seconds=5)
        self.assertEqual(registry.names(), ("first", "second"))

    def test_a_raising_member_is_rejected_when_nothing_else_is_wrong(self):
        # The same adapter without the hook registers, so it was the read that failed.
        self.assertEqual(ProviderRegistry().register(Good()).name, "good")


class HostileMemberProbeTest(HostileAdapterTestCase):
    """What ``search`` / ``fetch`` return is inspected: that is adapter code too.

    ``inspect.iscoroutinefunction`` and ``inspect.signature`` read attributes of
    the object they are given (``__class__``, ``__signature__``, ``__wrapped__``)
    and call its ``Signature.bind``. An object returned by the adapter can raise
    there; that is a wrong adapter, not an error of the registry.
    """

    def test_a_search_or_fetch_that_raises_when_inspected_is_rejected(self):
        for member in ("search", "fetch"):
            for style, build in PROBE_STYLES.items():
                for label, make in HOOK_EXCEPTIONS.items():
                    with self.subTest(member=member, style=style, raises=label):
                        calls = []
                        provider = adapter(**{member: build(make, calls)})
                        self.assertFixedRejection(member, validate_provider, provider)
                        self.assertGreaterEqual(len(calls), 1)  # the hook ran

                        calls = []
                        provider = adapter(**{member: build(make, calls)})
                        registry = ProviderRegistry()
                        self.assertFixedRejection(member, registry.register, provider)
                        self.assertGreaterEqual(len(calls), 1)
                        self.assertEqual(len(registry), 0)

    def test_name_and_kind_objects_with_hostile_classes_are_rejected(self):
        """``name`` / ``kind`` values whose metaclass and instance hooks all raise."""

        def hostile_value():
            calls = []

            class Meta(type):
                def __getattribute__(cls, attr):
                    calls.append(attr)
                    raise RuntimeError(HOSTILE_TEXT)

                def __eq__(cls, other):
                    raise RuntimeError(HOSTILE_TEXT)

                def __hash__(cls):
                    raise RuntimeError(HOSTILE_TEXT)

                def __instancecheck__(cls, instance):
                    raise RuntimeError(HOSTILE_TEXT)

                def __subclasscheck__(cls, subclass):
                    raise RuntimeError(HOSTILE_TEXT)

            def boom(self, *args, **kwargs):
                raise RuntimeError(HOSTILE_TEXT)

            hooks = {
                name: boom
                for name in (
                    "__getattribute__",
                    "__eq__",
                    "__hash__",
                    "__str__",
                    "__repr__",
                    "__len__",
                    "__iter__",
                )
            }
            return Meta("HostileValue", (), hooks)()

        for member in ("name", "kind"):
            with self.subTest(member=member):
                provider = adapter(**{member: hostile_value()})
                self.assertFixedRejection(member, validate_provider, provider)
                self.assertFixedRejection(member, ProviderRegistry().register, provider)


def hostile_cancellers(member):
    """Providers whose read of ``member`` cancels the running task and returns fine.

    Nothing is raised, and the value is a valid one: without a guard the adapter
    would register, and the request would stay on the task.
    """
    values = dict(GOOD_MEMBERS)

    def getter(self):
        cancel_current_task()
        return (
            values[member]
            if member in ("name", "kind")
            else values[member].__get__(self)
        )

    def hook(self, attr):
        if attr == member:
            cancel_current_task()
        return object.__getattribute__(self, attr)

    return {
        "a property": type("CancellingProperty", (Good,), {member: property(getter)})(),
        "__getattribute__": type(
            "CancellingGetattribute", (Good,), {"__getattribute__": hook}
        )(),
    }


class RegistrationCancellationTest(
    HostileAdapterTestCase, unittest.IsolatedAsyncioTestCase
):
    """Registration is synchronous, so the cancellation guard is the broker's.

    ``register`` and ``validate_provider`` never await: a real ``Task.cancel()``
    (from the caller, or the loop) can only be delivered at the caller's next
    ``await``, never in the middle of a read. A ``CancelledError`` raised by an
    adapter's hook is therefore the adapter's own failure. A hook that asks for
    the cancellation of the running task and then returns normally is retracted
    (``CancelGuard``, the broker's) and rejected; a request that was there before
    the call is left alone and still delivered.
    """

    async def test_a_hook_that_cancels_the_task_and_returns_is_retracted(self):
        task = asyncio.current_task()
        for member in MEMBERS:
            for style, provider in hostile_cancellers(member).items():
                for call in ("validate_provider", "register"):
                    with self.subTest(member=member, style=style, call=call):
                        registry = ProviderRegistry()
                        run = (
                            validate_provider
                            if call == "validate_provider"
                            else registry.register
                        )
                        self.assertFixedRejection(member, run, provider)
                        self.assertEqual(task.cancelling(), 0)
                        self.assertEqual(len(registry), 0)
                        await asyncio.sleep(0)  # nothing is delivered

    async def test_a_hook_that_cancels_and_raises_is_also_retracted(self):
        task = asyncio.current_task()

        def hook(self, attr):
            if attr == "name":
                cancel_current_task(2)
                raise asyncio.CancelledError(HOSTILE_TEXT)
            return object.__getattribute__(self, attr)

        provider = type("Both", (Good,), {"__getattribute__": hook})()
        self.assertFixedRejection("name", validate_provider, provider)
        self.assertEqual(task.cancelling(), 0)
        await asyncio.sleep(0)

    async def test_a_cancel_requested_before_the_call_is_kept_and_delivered(self):
        reached = []
        registered = []

        async def body():
            task = asyncio.current_task()
            registry = ProviderRegistry()
            task.cancel()  # a real request: cancelling() == 1
            registry.register(StaticProvider("p", ProviderKind.WEB))
            registered.append(len(registry))
            validate_provider(Good())
            self.assertEqual(task.cancelling(), 1)  # neither call uncancelled it
            await asyncio.sleep(0)
            reached.append("not cancelled")

        task = asyncio.create_task(body())
        await asyncio.wait({task}, timeout=GUARD_SECONDS)

        self.assertTrue(task.cancelled())  # delivered at the first await
        self.assertEqual(registered, [1])  # registration itself was not interrupted
        self.assertEqual(reached, [])

    async def test_a_hostile_hook_does_not_retract_a_request_from_before(self):
        outcome = []

        async def body():
            task = asyncio.current_task()
            task.cancel()  # the caller's own request
            provider = hostile_cancellers("name")["a property"]  # asks twice over
            self.assertFixedRejection("name", validate_provider, provider)
            outcome.append(task.cancelling())  # only the hook's request is retracted
            await asyncio.sleep(0)
            outcome.append("not cancelled")

        task = asyncio.create_task(body())
        await asyncio.wait({task}, timeout=GUARD_SECONDS)

        self.assertTrue(task.cancelled())
        self.assertEqual(outcome, [1])

    async def test_a_raised_cancelled_error_does_not_cancel_the_caller(self):
        task = asyncio.current_task()
        for label, make in BASE_EXCEPTIONS.items():
            with self.subTest(raises=label):
                provider = raising_property("kind", make, [])
                self.assertFixedRejection("kind", ProviderRegistry().register, provider)
                self.assertEqual(task.cancelling(), 0)
        await asyncio.sleep(0)  # the awaiting task was never cancelled


if __name__ == "__main__":
    unittest.main()
