"""Normalisation of provider output: hashes, titles, hits, merging."""

import hashlib
import unittest
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from types import SimpleNamespace
from unittest.mock import patch

from paw_backend.research.providers import (
    InvalidProviderResponseError,
    ProviderHit,
    ProviderKind,
    ResearchItem,
    SourceMetadata,
    SourceType,
    compute_content_hash,
    merge_items,
    normalize_hits,
    normalize_title,
)

from .research_support import NOW, SECRET, hit, malformed_hits

JST = timezone(timedelta(hours=9))


def normalize(hits, *, limit=10, kind=ProviderKind.WEB, provider_id="web-a"):
    return normalize_hits(
        provider_id=provider_id,
        kind=kind,
        hits=hits,
        limit=limit,
        retrieved_at=NOW,
    )


def item(
    locator: str, *, private_source: bool = False, title: str = "T"
) -> ResearchItem:
    return ResearchItem(
        SourceMetadata(
            provider_kind=ProviderKind.WEB,
            provider_id="web-a",
            locator=locator,
            title=title,
            retrieved_at=NOW,
            content_hash="sha256:" + "1" * 64,
            private_source=private_source,
        ),
        "text",
    )


def urls(items) -> list[str]:
    return [entry.source.locator for entry in items]


class ContentHashTest(unittest.TestCase):
    def test_known_vectors(self):
        self.assertEqual(
            compute_content_hash(""),
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        self.assertEqual(
            compute_content_hash("abc"),
            "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )
        self.assertEqual(
            compute_content_hash("日本語"),
            "sha256:77710aedc74ecfa33685e33a6c7df5cc83004da1bdcef7fb280f5c2b2e97e0a5",
        )

    def test_text_is_hashed_exactly_as_given(self):
        self.assertEqual(
            compute_content_hash(" abc"),
            "sha256:d92b1cb3a32147b86a4db0647e4bf6eda6cf160fd3b2da264c5b088c9f9ccbfa",
        )
        self.assertEqual(
            compute_content_hash("abc\n"),
            "sha256:edeaaff3f1774ad2888673770c6d64097e391bc362d7d6fb34982ddf0efd18cb",
        )
        self.assertNotEqual(compute_content_hash("abc"), compute_content_hash("ABC"))

    def test_format_and_equivalence_with_hashlib(self):
        text = "Ünïcode ✓ text\nwith lines"
        value = compute_content_hash(text)
        self.assertRegex(value, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(value, "sha256:" + hashlib.sha256(text.encode()).hexdigest())

    def test_non_str_is_a_type_error(self):
        for value in (None, b"abc", 5):
            with self.subTest(value=value), self.assertRaises(TypeError):
                compute_content_hash(value)


class NormalizeTitleTest(unittest.TestCase):
    def test_whitespace_runs_collapse_and_ends_are_trimmed(self):
        cases = {
            "  Hello \n\t world ": "Hello world",
            "Hello": "Hello",
            "a\u3000b\u00a0c": "a b c",
            "line1\r\nline2": "line1 line2",
            "": "",
            "   \n\t": "",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(normalize_title(title), expected)

    def test_nothing_else_changes(self):
        self.assertEqual(
            normalize_title("Ünï-Cödé 日本語 (v2.0)!"), "Ünï-Cödé 日本語 (v2.0)!"
        )

    def test_non_str_is_a_type_error(self):
        for value in (None, 5, b"x"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                normalize_title(value)


class NormalizeHitsTest(unittest.TestCase):
    def test_a_timestamp_that_cannot_be_expressed_in_utc_rejects_the_response(self):
        # Aware, so the hit is constructed; but ``datetime.max`` at UTC-01:00 is
        # beyond UTC's range. Built by hand (the constructor refuses it).
        far = datetime.max.replace(tzinfo=timezone(timedelta(hours=-1)))
        bad = hit()
        object.__setattr__(bad, "published_at", far)
        with self.assertRaises(InvalidProviderResponseError):
            normalize([hit("https://example.com/ok"), bad])

    def test_a_hit_becomes_an_item_with_unified_metadata(self):
        published = datetime(2026, 9, 1, 9, 30, tzinfo=JST)
        result = normalize(
            [
                hit(
                    "HTTPS://Docs.Example.com:443/A?utm_source=x&b=2&a=1#frag",
                    title="  Great \n Docs ",
                    text="The excerpt.",
                    published_at=published,
                    source_type=SourceType.OFFICIAL_DOCS,
                    private_source=True,
                )
            ],
            kind=ProviderKind.DOCS,
            provider_id="docs-main",
        )
        expected = ResearchItem(
            SourceMetadata(
                provider_kind=ProviderKind.DOCS,
                provider_id="docs-main",
                locator="https://docs.example.com/A?a=1&b=2",
                title="Great Docs",
                retrieved_at=NOW,
                content_hash=compute_content_hash("The excerpt."),
                source_type=SourceType.OFFICIAL_DOCS,
                published_at=datetime(2026, 9, 1, 0, 30, tzinfo=UTC),
                private_source=True,
            ),
            "The excerpt.",
        )
        self.assertEqual(result, (expected,))
        self.assertEqual(result[0].source.published_at.utcoffset(), timedelta(0))

    def test_defaults_carry_over(self):
        (result,) = normalize([hit("https://a.example/x", title="", text="")])
        self.assertEqual(result.source.title, "")
        self.assertIsNone(result.source.published_at)
        self.assertEqual(result.source.source_type, SourceType.UNKNOWN)
        self.assertIs(result.source.private_source, False)
        self.assertEqual(result.text, "")
        self.assertEqual(result.source.content_hash, compute_content_hash(""))

    def test_order_is_kept_and_duplicates_inside_one_response_are_not_merged(self):
        hits = [
            hit("https://a.example/2", text="two"),
            hit("https://a.example/1", text="one"),
            hit("https://a.example/2#again", text="two again"),
        ]
        result = normalize(hits)
        self.assertEqual(
            urls(result),
            ["https://a.example/2", "https://a.example/1", "https://a.example/2"],
        )
        self.assertEqual([entry.text for entry in result], ["two", "one", "two again"])

    def test_list_and_tuple_are_accepted_and_the_result_is_a_tuple(self):
        one = hit("https://a.example/1")
        self.assertIsInstance(normalize([one]), tuple)
        self.assertEqual(normalize((one,)), normalize([one]))
        self.assertEqual(normalize([]), ())
        self.assertEqual(normalize(()), ())

    def test_the_limit_is_inclusive(self):
        hits = [hit(f"https://a.example/{n}") for n in range(3)]
        self.assertEqual(len(normalize(hits, limit=3)), 3)
        with self.assertRaises(InvalidProviderResponseError):
            normalize(hits, limit=2)

    def test_the_result_does_not_alias_the_response(self):
        hits = [hit("https://a.example/1")]
        result = normalize(hits)
        hits.append(hit("https://a.example/2"))
        hits.clear()
        self.assertEqual(urls(result), ["https://a.example/1"])

    def test_wrong_response_types_are_rejected(self):
        one = hit("https://a.example/1")
        for response in (
            None,
            "https://a.example/1",
            b"bytes",
            {"locator": "https://a.example/1"},
            {one},
            iter([one]),
            (h for h in [one]),
            5,
            one,
        ):
            with self.subTest(response=type(response).__name__):
                with self.assertRaises(InvalidProviderResponseError):
                    normalize(response)

    def test_every_element_must_be_a_provider_hit_and_nothing_is_partial(self):
        good = hit("https://a.example/1")
        for bad in (None, "https://a.example/2", {"locator": "x"}, 5, object()):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(InvalidProviderResponseError):
                    normalize([good, bad])
                with self.assertRaises(InvalidProviderResponseError):
                    normalize([bad, good])

    def test_one_bad_locator_rejects_the_whole_response(self):
        good = hit("https://a.example/1")
        for locator in (
            "ftp://a.example/",
            "javascript:alert(1)",
            "https://user:pw@a.example/",
            "example.com/x",
            "https://exa mple.com/",
            "https://exa_mple.com/",
        ):
            with self.subTest(locator=locator):
                with self.assertRaises(InvalidProviderResponseError):
                    normalize([good, hit(locator)])

    def test_look_alike_objects_are_not_provider_hits(self):
        # An object with hit-like attributes could lack ``private_source`` and
        # would silently count as public.
        class Lookalike:
            locator = "https://a.example/1"
            title = "t"
            text = "x"
            published_at = None
            source_type = SourceType.UNKNOWN
            private_source = False

        for value in (
            SimpleNamespace(locator="https://a.example/1", title="t", text="x"),
            SimpleNamespace(
                locator="https://a.example/1",
                title="t",
                text="x",
                published_at=None,
                source_type=SourceType.UNKNOWN,
                private_source=True,
            ),
            Lookalike(),
        ):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(InvalidProviderResponseError):
                    normalize([value])

    def test_only_locator_errors_are_converted(self):
        # A bug elsewhere must surface, not turn into "invalid response".
        target = "paw_backend.research.providers.normalize.canonicalize_locator"
        with patch(target, side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                normalize([hit()])
        with self.assertRaises(TypeError):
            normalize([hit()], limit="ten")

    def test_the_error_message_is_fixed_and_never_echoes_the_response(self):
        with self.assertRaises(InvalidProviderResponseError) as caught:
            normalize([hit(f"https://user:{SECRET}@a.example/{SECRET}")])
        self.assertEqual(str(caught.exception), "Invalid provider response")
        self.assertNotIn(SECRET, repr(caught.exception))

    def test_bugs_in_the_trusted_arguments_are_not_hidden(self):
        with self.assertRaises(TypeError):
            normalize([hit()], provider_id=None)
        with self.assertRaises(ValueError):
            normalize([hit()], provider_id="Not A Valid Id")

    def test_hit_subclass_instances_are_accepted(self):
        class MyHit(ProviderHit):
            __slots__ = ()

        (result,) = normalize([MyHit("https://a.example/1", private_source=False)])
        self.assertEqual(result.source.locator, "https://a.example/1")


class MalformedTypedHitTest(unittest.TestCase):
    """A ``ProviderHit`` built around its constructor is validated again.

    ``isinstance`` says nothing about the field values: an object can lack
    slots, hold a value of the wrong type, or be far over the length bounds.
    Each such response is the provider's invalid response and never an
    ``AttributeError`` / ``TypeError`` for the caller.
    """

    def test_every_malformed_hit_rejects_the_whole_response(self):
        good = hit("https://example.com/ok")
        for label, bad in malformed_hits().items():
            for hits in ([good, bad], [bad, good], (bad,)):
                with self.subTest(malformed=label, hits=len(hits)):
                    with self.assertRaises(InvalidProviderResponseError):
                        normalize(hits)

    def test_the_unmodified_hit_of_the_table_is_accepted(self):
        # The table's builder is a valid hit: only the changed field is at fault.
        (result,) = normalize([hit()])
        self.assertEqual(result.source.locator, "https://example.com/a")

    def test_the_rejection_names_neither_the_field_nor_the_value(self):
        bad = hit()
        object.__setattr__(bad, "title", SECRET * 200)
        with self.assertRaises(InvalidProviderResponseError) as caught:
            normalize([bad])
        self.assertEqual(str(caught.exception), "Invalid provider response")
        self.assertNotIn(SECRET, repr(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_a_subclass_that_sets_nothing_is_not_read_through_its_properties(self):
        class Shadow(ProviderHit):
            locator = property(lambda self: "https://a.example/1")
            title = property(lambda self: "t")
            text = property(lambda self: "x")
            published_at = property(lambda self: None)
            source_type = property(lambda self: SourceType.UNKNOWN)
            private_source = property(lambda self: False)

            def __init__(self) -> None:
                pass

        with self.assertRaises(InvalidProviderResponseError):
            normalize([Shadow()])

    def test_the_subclass_is_never_asked_for_a_value(self):
        calls: list[str] = []

        def spy(name):
            def read(self):
                calls.append(name)
                raise RuntimeError(name)

            return property(read)

        class Meddling(ProviderHit):
            locator = spy("locator")
            title = spy("title")
            text = spy("text")
            published_at = spy("published_at")
            source_type = spy("source_type")
            private_source = spy("private_source")

            def __init__(self) -> None:  # the class's own properties block the
                pass  # normal setters, so the slots are filled by hand

            def __getattribute__(self, name):
                calls.append(name)
                return super().__getattribute__(name)

        meddling = Meddling()
        for name, value in {
            "locator": "https://a.example/1",
            "title": "  Real   title ",
            "text": "the text",
            "published_at": None,
            "source_type": SourceType.PRIMARY,
            "private_source": True,
        }.items():
            getattr(ProviderHit, name).__set__(meddling, value)

        (result,) = normalize([meddling])

        self.assertEqual(calls, [])
        self.assertEqual(
            (
                result.source.locator,
                result.source.title,
                result.text,
                result.source.source_type,
                result.source.private_source,
            ),
            (
                "https://a.example/1",
                "Real title",
                "the text",
                SourceType.PRIMARY,
                True,
            ),
        )

    def test_a_class_that_claims_to_be_a_hit_is_rejected(self):
        class Impostor:
            @property
            def __class__(self):
                return ProviderHit

            locator = "https://a.example/1"

        self.assertIsInstance(Impostor(), ProviderHit)  # what isinstance would say
        with self.assertRaises(InvalidProviderResponseError):
            normalize([Impostor()])

    def test_a_value_that_claims_to_be_a_bool_or_a_source_type_is_rejected(self):
        def claiming(cls):
            class Impostor:
                @property
                def __class__(self):
                    return cls

            return Impostor()

        for field, cls in (("private_source", bool), ("source_type", SourceType)):
            bad = hit()
            object.__setattr__(bad, field, claiming(cls))
            with self.subTest(field=field):
                with self.assertRaises(InvalidProviderResponseError):
                    normalize([bad])

    def test_a_string_subclass_cannot_lie_about_its_length(self):
        class Liar(str):
            def __len__(self):
                return 1

        for field, size in (("text", 4_001), ("title", 301), ("locator", 2_049)):
            bad = hit()
            object.__setattr__(bad, field, Liar("x" * size))
            with (
                self.subTest(field=field),
                self.assertRaises(InvalidProviderResponseError),
            ):
                normalize([bad])

    def test_a_string_subclass_becomes_a_plain_str(self):
        class Tagged(str):
            def encode(self, *args, **kwargs):
                raise RuntimeError("must not run")

        sub = hit()
        for field, value in {
            "locator": Tagged("https://a.example/1"),
            "title": Tagged(" A  B "),
            "text": Tagged("excerpt"),
        }.items():
            object.__setattr__(sub, field, value)

        (result,) = normalize([sub])

        self.assertEqual(
            (result.source.locator, result.source.title, result.text),
            ("https://a.example/1", "A B", "excerpt"),
        )
        self.assertIs(type(result.text), str)
        self.assertEqual(result.source.content_hash, compute_content_hash("excerpt"))

    def test_a_datetime_subclass_is_converted_without_running_its_code(self):
        class Meddling(datetime):
            def astimezone(self, tz=None):
                raise RuntimeError("must not run")

            def utcoffset(self):
                raise RuntimeError("must not run")

        stamp = Meddling(2026, 9, 1, 9, 30, tzinfo=JST)
        bad = hit()
        object.__setattr__(bad, "published_at", stamp)

        (result,) = normalize([bad])

        self.assertEqual(
            result.source.published_at, datetime(2026, 9, 1, 0, 30, tzinfo=UTC)
        )
        self.assertIs(type(result.source.published_at), datetime)

    def test_a_timezone_that_fails_is_an_invalid_response(self):
        class Broken(tzinfo):
            def utcoffset(self, moment):
                raise RuntimeError(SECRET)

        bad = hit()
        object.__setattr__(bad, "published_at", datetime(2026, 9, 1, tzinfo=Broken()))
        with self.assertRaises(InvalidProviderResponseError) as caught:
            normalize([bad])
        self.assertNotIn(SECRET, repr(caught.exception))

    def test_the_hits_are_not_modified(self):
        first = hit("https://example.com/a", title="  spaced  title ")
        normalize([first])
        self.assertEqual(first.title, "  spaced  title ")


class MergeItemsTest(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(merge_items([], max_results=5), ((), False))
        self.assertEqual(merge_items([[], []], max_results=5), ((), False))

    def test_a_single_batch_is_kept_in_order(self):
        batch = [item("https://a.example/1"), item("https://a.example/2")]
        merged, truncated = merge_items([batch], max_results=5)
        self.assertEqual(merged, tuple(batch))
        self.assertIsInstance(merged, tuple)
        self.assertIs(truncated, False)

    def test_batches_are_interleaved_round_robin(self):
        a = [item(f"https://a.example/{n}") for n in (1, 2, 3)]
        b = [item(f"https://b.example/{n}") for n in (1, 2)]
        merged, truncated = merge_items([a, b], max_results=10)
        self.assertEqual(
            urls(merged),
            [
                "https://a.example/1",
                "https://b.example/1",
                "https://a.example/2",
                "https://b.example/2",
                "https://a.example/3",
            ],
        )
        self.assertIs(truncated, False)

    def test_three_batches_of_unequal_length(self):
        a = [item("https://a.example/1")]
        b = [item("https://b.example/1"), item("https://b.example/2")]
        c = [
            item("https://c.example/1"),
            item("https://c.example/2"),
            item("https://c.example/3"),
        ]
        merged, _ = merge_items([a, b, c], max_results=10)
        self.assertEqual(
            urls(merged),
            [
                "https://a.example/1",
                "https://b.example/1",
                "https://c.example/1",
                "https://b.example/2",
                "https://c.example/2",
                "https://c.example/3",
            ],
        )

    def test_truncation_flag_at_the_boundary(self):
        a = [item(f"https://a.example/{n}") for n in (1, 2, 3)]
        b = [item(f"https://b.example/{n}") for n in (1, 2)]
        merged, truncated = merge_items([a, b], max_results=4)
        self.assertEqual(
            urls(merged),
            [
                "https://a.example/1",
                "https://b.example/1",
                "https://a.example/2",
                "https://b.example/2",
            ],
        )
        self.assertIs(truncated, True)
        merged, truncated = merge_items([a, b], max_results=5)
        self.assertEqual(len(merged), 5)
        self.assertIs(truncated, False)
        merged, truncated = merge_items([a, b], max_results=1)
        self.assertEqual(urls(merged), ["https://a.example/1"])
        self.assertIs(truncated, True)

    def test_duplicates_keep_the_first_occurrence(self):
        first = item("https://x.example/1", title="from A")
        second = item("https://x.example/1", title="from B")
        merged, truncated = merge_items(
            [
                [first, item("https://a.example/2")],
                [second, item("https://b.example/2")],
            ],
            max_results=10,
        )
        self.assertEqual(
            urls(merged),
            ["https://x.example/1", "https://a.example/2", "https://b.example/2"],
        )
        self.assertEqual(merged[0].source.title, "from A")
        self.assertIs(truncated, False)

    def test_duplicates_inside_one_batch_are_merged_too(self):
        merged, _ = merge_items(
            [[item("https://a.example/1"), item("https://a.example/1")]], max_results=5
        )
        self.assertEqual(urls(merged), ["https://a.example/1"])

    def test_duplicates_are_removed_before_truncating(self):
        a = [item("https://x.example/1"), item("https://a.example/2")]
        b = [item("https://x.example/1"), item("https://b.example/2")]
        merged, truncated = merge_items([a, b], max_results=3)
        self.assertEqual(
            urls(merged),
            ["https://x.example/1", "https://a.example/2", "https://b.example/2"],
        )
        self.assertIs(truncated, False)

    def test_duplicates_do_not_count_towards_truncation(self):
        a = [item("https://a.example/1"), item("https://a.example/2")]
        b = [item("https://a.example/1"), item("https://a.example/2")]
        merged, truncated = merge_items([a, b], max_results=2)
        self.assertEqual(len(merged), 2)
        self.assertIs(truncated, False)

    def test_a_private_duplicate_makes_the_kept_item_private(self):
        public = item("https://x.example/1")
        private = item("https://x.example/1", private_source=True, title="private one")
        merged, _ = merge_items([[public], [private]], max_results=5)
        self.assertIs(merged[0].source.private_source, True)
        self.assertEqual(merged[0].source.title, "T")
        merged, _ = merge_items([[private], [public]], max_results=5)
        self.assertIs(merged[0].source.private_source, True)
        self.assertEqual(merged[0].source.title, "private one")

    def test_private_flag_is_merged_even_when_the_duplicate_is_ranked_last(self):
        x = item("https://x.example/1")
        y = item("https://y.example/1")
        x_private = item("https://x.example/1", private_source=True)
        merged, truncated = merge_items([[x], [y, x_private]], max_results=2)
        self.assertEqual(urls(merged), ["https://x.example/1", "https://y.example/1"])
        self.assertIs(merged[0].source.private_source, True)
        self.assertIs(merged[1].source.private_source, False)
        self.assertIs(truncated, False)

    def test_inputs_are_not_modified(self):
        public = item("https://x.example/1")
        batches = [[public], [item("https://x.example/1", private_source=True)]]
        merge_items(batches, max_results=5)
        self.assertIs(public.source.private_source, False)
        self.assertEqual(len(batches[0]), 1)
        self.assertEqual(len(batches[1]), 1)

    def test_max_results_is_validated(self):
        for value in (0, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                merge_items([], max_results=value)
        for value in (True, 1.5, "3", None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                merge_items([], max_results=value)

    def test_the_hash_of_the_metadata_is_not_used_to_deduplicate(self):
        one = item("https://x.example/1")
        other = item("https://x.example/2")
        self.assertEqual(one.source.content_hash, other.source.content_hash)
        merged, _ = merge_items([[one, other]], max_results=5)
        self.assertEqual(len(merged), 2)


if __name__ == "__main__":
    unittest.main()
