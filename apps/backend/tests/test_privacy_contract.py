"""The value objects, enums, limits and the in-memory audit sink of PAW-053.

Everything here is implemented (it is not stubbed logic), so these tests do not
depend on ``rules.py``.
"""

import dataclasses
import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta, timezone

from paw_backend.research.privacy import (
    COPY_WINDOW_CHARS,
    DEFAULT_MAX_MEMORY_RECORDS,
    MAX_AUDIT_TIMEOUT_SECONDS,
    MAX_CONTEXT_PIECES,
    MAX_DRAFT_CHARS,
    MAX_MINIMIZED_QUERY_CHARS,
    MAX_PIECE_CHARS,
    MAX_TOTAL_CONTEXT_CHARS,
    MIN_HEX_HASH_CHARS,
    MIN_ID_DIGITS,
    MIN_OPAQUE_TOKEN_CHARS,
    NON_PUBLIC_LABELS,
    PRIVATE_HOST_SUFFIXES,
    SECRET_WINDOW_CHARS,
    AuditSinkFullError,
    ContextLabel,
    ContextPiece,
    ExternalSendAudit,
    ExternalSendRecord,
    InMemoryExternalSendAudit,
    MinimizedQuery,
    PrivacyInput,
    PrivacyRefusal,
    RefusalReason,
    WithheldCounts,
    context_pieces_from_items,
    copy_window,
)
from paw_backend.research.providers import (
    ProviderKind,
    ResearchItem,
    SourceMetadata,
)

from .privacy_support import NOW, PROJECT_ID, guarded, piece
from .research_support import SECRET

FINGERPRINT = "sha256:" + "ab" * 32
JST = timezone(timedelta(hours=9))


class LimitsTest(unittest.TestCase):
    def test_the_documented_limits(self):
        self.assertEqual(MAX_DRAFT_CHARS, 2_000)
        self.assertEqual(MAX_MINIMIZED_QUERY_CHARS, 256)
        self.assertEqual(MAX_CONTEXT_PIECES, 32)
        self.assertEqual(MAX_PIECE_CHARS, 200_000)
        self.assertEqual(MAX_TOTAL_CONTEXT_CHARS, 400_000)
        self.assertEqual(COPY_WINDOW_CHARS, 16)
        self.assertEqual(SECRET_WINDOW_CHARS, 4)
        self.assertEqual(MIN_ID_DIGITS, 5)
        self.assertEqual(MIN_HEX_HASH_CHARS, 12)
        self.assertEqual(MIN_OPAQUE_TOKEN_CHARS, 40)
        self.assertEqual(MAX_AUDIT_TIMEOUT_SECONDS, 60.0)
        self.assertEqual(DEFAULT_MAX_MEMORY_RECORDS, 1_000)

    def test_the_minimised_query_fits_a_research_request(self):
        from paw_backend.research.providers import MAX_QUERY_CHARS

        self.assertLessEqual(MAX_MINIMIZED_QUERY_CHARS, MAX_QUERY_CHARS)

    def test_private_host_suffixes(self):
        self.assertEqual(
            PRIVATE_HOST_SUFFIXES,
            frozenset(
                {
                    "local",
                    "localhost",
                    "internal",
                    "lan",
                    "home",
                    "corp",
                    "intranet",
                    "localdomain",
                    "private",
                    "arpa",
                }
            ),
        )


class ContextLabelTest(unittest.TestCase):
    def test_the_closed_set_of_labels(self):
        self.assertEqual(
            {label.name: label.value for label in ContextLabel},
            {
                "PUBLIC": "public",
                "PRIVATE_SOURCE": "private_source",
                "PRIVATE_MEMORY": "private_memory",
                "RAW_CONVERSATION": "raw_conversation",
                "SECRET": "secret",
            },
        )

    def test_non_public_labels_are_all_but_public_in_enum_order(self):
        self.assertEqual(
            NON_PUBLIC_LABELS,
            (
                ContextLabel.PRIVATE_SOURCE,
                ContextLabel.PRIVATE_MEMORY,
                ContextLabel.RAW_CONVERSATION,
                ContextLabel.SECRET,
            ),
        )

    def test_copy_window_per_label(self):
        self.assertEqual(copy_window(ContextLabel.SECRET), 4)
        for label in (
            ContextLabel.PRIVATE_SOURCE,
            ContextLabel.PRIVATE_MEMORY,
            ContextLabel.RAW_CONVERSATION,
        ):
            with self.subTest(label=label):
                self.assertEqual(copy_window(label), 16)

    def test_public_text_has_no_copy_window(self):
        with self.assertRaises(ValueError):
            copy_window(ContextLabel.PUBLIC)

    def test_copy_window_needs_a_label(self):
        for value in ("secret", None, 4):
            with self.subTest(value=value), self.assertRaises(TypeError):
                copy_window(value)


class RefusalTest(unittest.TestCase):
    def test_the_closed_set_of_reasons(self):
        self.assertEqual(
            {reason.name: reason.value for reason in RefusalReason},
            {
                "UNCLASSIFIED_CONTEXT": "unclassified_context",
                "DRAFT_TOO_LONG": "draft_too_long",
                "CONTEXT_TOO_LARGE": "context_too_large",
                "EMPTY_QUERY": "empty_query",
                "CREDENTIAL_REMAINS": "credential_remains",
                "PRIVATE_TEXT_REMAINS": "private_text_remains",
                "AUDIT_FAILED": "audit_failed",
            },
        )

    def test_a_refusal_carries_the_reason_and_nothing_else(self):
        error = PrivacyRefusal(RefusalReason.EMPTY_QUERY)
        self.assertIs(error.reason, RefusalReason.EMPTY_QUERY)
        self.assertEqual(str(error), "empty_query")
        self.assertEqual(error.args, ("empty_query",))
        self.assertIsInstance(error, Exception)

    def test_the_reason_must_be_the_enum(self):
        for value in ("empty_query", None, 3):
            with self.subTest(value=value), self.assertRaises(TypeError):
                PrivacyRefusal(value)


class ContextPieceTest(unittest.TestCase):
    def test_a_valid_piece(self):
        item = ContextPiece(ContextLabel.SECRET, "hunter2")
        self.assertIs(item.label, ContextLabel.SECRET)
        self.assertEqual(item.text, "hunter2")

    def test_a_piece_cannot_be_unclassified(self):
        for label in ("public", "PUBLIC", None, 1, ContextLabel):
            with self.subTest(label=label), self.assertRaises(TypeError):
                ContextPiece(label, "text")

    def test_the_text_must_be_a_str(self):
        for text in (None, b"bytes", 12, ["a"]):
            with self.subTest(text=text), self.assertRaises(TypeError):
                ContextPiece(ContextLabel.PUBLIC, text)

    def test_a_blank_text_is_rejected(self):
        for text in ("", " ", "\n\t ", "　"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                ContextPiece(ContextLabel.PUBLIC, text)

    def test_the_length_limit(self):
        ContextPiece(ContextLabel.PUBLIC, "a" * MAX_PIECE_CHARS)
        with self.assertRaises(ValueError):
            ContextPiece(ContextLabel.PUBLIC, "a" * (MAX_PIECE_CHARS + 1))

    def test_the_text_must_be_encodable(self):
        with self.assertRaises(ValueError) as caught:
            ContextPiece(ContextLabel.PUBLIC, "ok \ud800 ok")
        self.assertNotIn("ud800", str(caught.exception))

    def test_the_text_never_appears_in_the_repr(self):
        item = ContextPiece(ContextLabel.SECRET, SECRET)
        self.assertNotIn(SECRET, repr(item))
        self.assertNotIn(SECRET, str(item))
        self.assertIn("SECRET", repr(item).upper())

    def test_a_piece_is_immutable_and_comparable(self):
        item = ContextPiece(ContextLabel.PUBLIC, "a")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            item.text = "b"
        self.assertEqual(item, ContextPiece(ContextLabel.PUBLIC, "a"))
        self.assertNotEqual(item, ContextPiece(ContextLabel.SECRET, "a"))


class PrivacyInputTest(unittest.TestCase):
    def test_a_list_becomes_a_tuple(self):
        first = piece(ContextLabel.PUBLIC, "a")
        value = PrivacyInput([first], PROJECT_ID)
        self.assertEqual(value.context, (first,))
        self.assertIsInstance(value.context, tuple)
        self.assertEqual(PrivacyInput((first,), PROJECT_ID).context, (first,))

    def test_an_empty_context_is_allowed(self):
        self.assertEqual(PrivacyInput((), PROJECT_ID).context, ())

    def test_the_context_must_be_a_list_or_a_tuple(self):
        for context in (None, "text", {1}, {"a": 1}, iter(()), b"x"):
            with self.subTest(context=context), self.assertRaises(TypeError):
                PrivacyInput(context, PROJECT_ID)

    def test_elements_are_not_checked_here(self):
        value = PrivacyInput(["unclassified text", None], PROJECT_ID)
        self.assertEqual(value.context, ("unclassified text", None))

    def test_the_project_id_must_be_a_uuid(self):
        for project_id in (str(PROJECT_ID), None, 5, PROJECT_ID.hex):
            with self.subTest(project_id=project_id), self.assertRaises(TypeError):
                PrivacyInput((), project_id)

    def test_the_repr_hides_the_text_of_the_pieces(self):
        value = PrivacyInput([piece(ContextLabel.SECRET, SECRET)], PROJECT_ID)
        self.assertNotIn(SECRET, repr(value))


def source(
    *, private: bool, title: str = "Title", locator: str = "https://example.com/a"
) -> SourceMetadata:
    return SourceMetadata(
        ProviderKind.WEB,
        "web-a",
        locator,
        title,
        NOW,
        "sha256:" + "0" * 64,
        private_source=private,
    )


class ContextPiecesFromItemsTest(unittest.TestCase):
    def test_a_public_item_gives_one_public_piece(self):
        item = ResearchItem(source(private=False), "public body")
        self.assertEqual(
            context_pieces_from_items([item]),
            (ContextPiece(ContextLabel.PUBLIC, "public body"),),
        )

    def test_a_private_item_gives_its_text_and_its_title(self):
        item = ResearchItem(source(private=True, title="Secret Plan"), "private body")
        self.assertEqual(
            context_pieces_from_items((item,)),
            (
                ContextPiece(ContextLabel.PRIVATE_SOURCE, "private body"),
                ContextPiece(ContextLabel.PRIVATE_SOURCE, "Secret Plan"),
            ),
        )

    def test_a_private_item_without_a_title_gives_only_its_text(self):
        item = ResearchItem(source(private=True, title=""), "private body")
        self.assertEqual(
            context_pieces_from_items([item]),
            (ContextPiece(ContextLabel.PRIVATE_SOURCE, "private body"),),
        )

    def test_a_blank_text_gives_no_text_piece(self):
        public = ResearchItem(source(private=False), "  \n")
        private = ResearchItem(source(private=True, title="Only Title"), "")
        self.assertEqual(context_pieces_from_items([public]), ())
        self.assertEqual(
            context_pieces_from_items([private]),
            (ContextPiece(ContextLabel.PRIVATE_SOURCE, "Only Title"),),
        )

    def test_the_public_title_and_the_locator_are_not_pieces(self):
        item = ResearchItem(
            source(private=False, title="Public Title", locator="https://a.example/x"),
            "body",
        )
        texts = [p.text for p in context_pieces_from_items([item])]
        self.assertEqual(texts, ["body"])

    def test_order_is_kept_over_several_items(self):
        one = ResearchItem(source(private=False, locator="https://a.example/1"), "one")
        two = ResearchItem(
            source(private=True, title="T2", locator="https://a.example/2"), "two"
        )
        pieces = context_pieces_from_items([one, two])
        self.assertEqual(
            [(p.label, p.text) for p in pieces],
            [
                (ContextLabel.PUBLIC, "one"),
                (ContextLabel.PRIVATE_SOURCE, "two"),
                (ContextLabel.PRIVATE_SOURCE, "T2"),
            ],
        )

    def test_an_empty_sequence_gives_an_empty_tuple(self):
        self.assertEqual(context_pieces_from_items([]), ())
        self.assertEqual(context_pieces_from_items(()), ())

    def test_wrong_types_are_rejected(self):
        for items in (None, "items", {1}, iter(())):
            with self.subTest(items=items), self.assertRaises(TypeError):
                context_pieces_from_items(items)
        with self.assertRaises(TypeError):
            context_pieces_from_items(["a string"])


class WithheldCountsTest(unittest.TestCase):
    def test_defaults_are_zero(self):
        counts = WithheldCounts()
        self.assertEqual(counts.to_dict(), dict.fromkeys(_COUNT_NAMES, 0))

    def test_from_pieces_counts_per_label_and_ignores_public(self):
        pieces = [
            piece(ContextLabel.PUBLIC, "p"),
            piece(ContextLabel.PRIVATE_SOURCE, "a"),
            piece(ContextLabel.PRIVATE_SOURCE, "b"),
            piece(ContextLabel.SECRET, "c"),
            piece(ContextLabel.RAW_CONVERSATION, "d"),
            piece(ContextLabel.RAW_CONVERSATION, "e"),
            piece(ContextLabel.RAW_CONVERSATION, "f"),
        ]
        self.assertEqual(
            WithheldCounts.from_pieces(pieces),
            WithheldCounts(
                private_source=2, private_memory=0, raw_conversation=3, secret=1
            ),
        )
        self.assertEqual(WithheldCounts.from_pieces([]), WithheldCounts())

    def test_from_pieces_rejects_non_pieces(self):
        with self.assertRaises(TypeError):
            WithheldCounts.from_pieces(["text"])

    def test_values_are_validated(self):
        for name in _COUNT_NAMES:
            for value in (-1, MAX_CONTEXT_PIECES + 1):
                with (
                    self.subTest(name=name, value=value),
                    self.assertRaises(ValueError),
                ):
                    WithheldCounts(**{name: value})
            for value in (True, 1.0, "1", None):
                with self.subTest(name=name, value=value), self.assertRaises(TypeError):
                    WithheldCounts(**{name: value})
            WithheldCounts(**{name: MAX_CONTEXT_PIECES})

    def test_to_dict_is_json_ready(self):
        counts = WithheldCounts(1, 2, 3, 4)
        self.assertEqual(
            counts.to_dict(),
            {
                "private_source": 1,
                "private_memory": 2,
                "raw_conversation": 3,
                "secret": 4,
            },
        )
        json.dumps(counts.to_dict())


_COUNT_NAMES = ("private_source", "private_memory", "raw_conversation", "secret")


def minimized(**overrides) -> MinimizedQuery:
    values = {
        "query": "python asyncio timeout",
        "fingerprint": FINGERPRINT,
        "truncated": False,
        "credentials_removed": 0,
        "pieces_matched": 0,
        "abstractions": 0,
        "withheld": WithheldCounts(),
    }
    values.update(overrides)
    return MinimizedQuery(**values)


class MinimizedQueryTest(unittest.TestCase):
    def test_a_valid_query(self):
        result = minimized()
        self.assertEqual(result.query, "python asyncio timeout")
        self.assertEqual(result.fingerprint, FINGERPRINT)

    def test_the_length_bounds(self):
        minimized(query="a")
        minimized(query="a" * MAX_MINIMIZED_QUERY_CHARS)
        for query in ("", "a" * (MAX_MINIMIZED_QUERY_CHARS + 1)):
            with self.subTest(length=len(query)), self.assertRaises(ValueError):
                minimized(query=query)

    def test_the_query_must_be_in_normal_form(self):
        for query in (
            " leading",
            "trailing ",
            "double  space",
            "tab\tsep",
            "new\nline",
            "nbsp here",
            "zero​width",
            "bell\x07",
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                minimized(query=query)

    def test_the_query_needs_a_word_character(self):
        for query in ("!!!", "- - -", "..."):
            with self.subTest(query=query), self.assertRaises(ValueError):
                minimized(query=query)
        minimized(query="a!")
        minimized(query="検索")
        minimized(query="_")

    def test_the_query_must_be_a_str(self):
        for query in (None, b"abc", 5):
            with self.subTest(query=query), self.assertRaises(TypeError):
                minimized(query=query)

    def test_the_fingerprint_shape(self):
        for value in (
            "sha256:" + "AB" * 32,
            "sha256:" + "ab" * 31,
            "sha256:" + "ab" * 33,
            "sha1:" + "ab" * 32,
            "ab" * 32,
            "",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                minimized(fingerprint=value)
        with self.assertRaises(TypeError):
            minimized(fingerprint=None)

    def test_counts_and_flags_are_validated(self):
        for name in ("credentials_removed", "pieces_matched", "abstractions"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    minimized(**{name: -1})
                for value in (True, 1.5, "1", None):
                    with self.assertRaises(TypeError):
                        minimized(**{name: value})
        for value in (0, 1, "yes", None):
            with self.subTest(truncated=value), self.assertRaises(TypeError):
                minimized(truncated=value)
        with self.assertRaises(TypeError):
            minimized(withheld={"secret": 1})

    def test_it_is_immutable(self):
        result = minimized()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.query = "other"


def record(**overrides) -> ExternalSendRecord:
    values = {
        "recorded_at": NOW,
        "project_id": PROJECT_ID,
        "query_fingerprint": FINGERPRINT,
        "query_chars": 22,
        "provider_kinds": (ProviderKind.WEB, ProviderKind.DOCS),
        "withheld": WithheldCounts(private_source=1, secret=2),
        "credentials_removed": 1,
        "pieces_matched": 2,
        "abstractions": 3,
        "truncated": True,
    }
    values.update(overrides)
    return ExternalSendRecord(**values)


class ExternalSendRecordTest(unittest.TestCase):
    def test_to_dict_is_exact_and_json_ready(self):
        self.assertEqual(
            record().to_dict(),
            {
                "recorded_at": "2026-09-24T12:00:00+00:00",
                "project_id": "11111111-2222-3333-4444-555555555555",
                "query_fingerprint": FINGERPRINT,
                "query_chars": 22,
                "provider_kinds": ["web", "docs"],
                "withheld": {
                    "private_source": 1,
                    "private_memory": 0,
                    "raw_conversation": 0,
                    "secret": 2,
                },
                "credentials_removed": 1,
                "pieces_matched": 2,
                "abstractions": 3,
                "truncated": True,
            },
        )
        json.dumps(record().to_dict())

    def test_it_holds_no_text_field(self):
        self.assertEqual(
            {field.name for field in dataclasses.fields(ExternalSendRecord)},
            {
                "recorded_at",
                "project_id",
                "query_fingerprint",
                "query_chars",
                "provider_kinds",
                "withheld",
                "credentials_removed",
                "pieces_matched",
                "abstractions",
                "truncated",
            },
        )

    def test_the_time_must_be_aware_and_utc(self):
        with self.assertRaises(ValueError):
            record(recorded_at=datetime(2026, 9, 24, 12, 0, 0))
        with self.assertRaises(ValueError):
            record(recorded_at=datetime(2026, 9, 24, 12, 0, 0, tzinfo=JST))
        with self.assertRaises(TypeError):
            record(recorded_at="2026-09-24T12:00:00+00:00")
        record(recorded_at=datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC))

    def test_the_project_id_must_be_a_uuid(self):
        for value in (str(PROJECT_ID), None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                record(project_id=value)
        record(project_id=uuid.uuid4())

    def test_the_fingerprint_shape(self):
        with self.assertRaises(ValueError):
            record(query_fingerprint="sha256:zz")
        with self.assertRaises(TypeError):
            record(query_fingerprint=None)

    def test_query_chars_bounds(self):
        record(query_chars=1)
        record(query_chars=MAX_MINIMIZED_QUERY_CHARS)
        for value in (0, MAX_MINIMIZED_QUERY_CHARS + 1, -3):
            with self.subTest(value=value), self.assertRaises(ValueError):
                record(query_chars=value)
        for value in (True, 2.0, "22"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                record(query_chars=value)

    def test_provider_kinds_rules(self):
        record(provider_kinds=(ProviderKind.OPENCODE,))
        record(provider_kinds=tuple(ProviderKind))
        with self.assertRaises(ValueError):
            record(provider_kinds=())
        with self.assertRaises(ValueError):
            record(provider_kinds=(ProviderKind.DOCS, ProviderKind.WEB))
        with self.assertRaises(ValueError):
            record(provider_kinds=(ProviderKind.WEB, ProviderKind.WEB))
        with self.assertRaises(TypeError):
            record(provider_kinds=[ProviderKind.WEB])
        with self.assertRaises(TypeError):
            record(provider_kinds=frozenset({ProviderKind.WEB}))
        with self.assertRaises(TypeError):
            record(provider_kinds=("web",))

    def test_counts_and_flags_are_validated(self):
        for name in ("credentials_removed", "pieces_matched", "abstractions"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    record(**{name: -1})
                with self.assertRaises(TypeError):
                    record(**{name: True})
        with self.assertRaises(TypeError):
            record(truncated=1)
        with self.assertRaises(TypeError):
            record(withheld=None)


class InMemoryAuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_it_implements_the_protocol(self):
        self.assertIsInstance(InMemoryExternalSendAudit(), ExternalSendAudit)

    async def test_records_are_kept_in_order(self):
        sink = InMemoryExternalSendAudit()
        first, second = record(query_chars=5), record(query_chars=6)
        await guarded(sink.record(first))
        await guarded(sink.record(second))
        self.assertEqual(sink.records, (first, second))
        self.assertIsInstance(sink.records, tuple)

    async def test_a_full_sink_refuses_and_keeps_what_it_has(self):
        sink = InMemoryExternalSendAudit(max_records=2)
        first, second = record(query_chars=5), record(query_chars=6)
        await guarded(sink.record(first))
        await guarded(sink.record(second))
        with self.assertRaises(AuditSinkFullError):
            await guarded(sink.record(record(query_chars=7)))
        self.assertEqual(sink.records, (first, second))

    async def test_only_records_are_accepted(self):
        sink = InMemoryExternalSendAudit()
        for value in ("record", None, {"a": 1}):
            with self.subTest(value=value), self.assertRaises(TypeError):
                await guarded(sink.record(value))
        self.assertEqual(sink.records, ())

    async def test_the_capacity_is_validated(self):
        for value in (0, -1, 100_001, True, 1.5, None):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                InMemoryExternalSendAudit(max_records=value)
        InMemoryExternalSendAudit(max_records=1)
        InMemoryExternalSendAudit(max_records=100_000)

    async def test_the_full_error_message_is_fixed(self):
        self.assertEqual(str(AuditSinkFullError()), "the in-memory audit sink is full")


if __name__ == "__main__":
    unittest.main()
