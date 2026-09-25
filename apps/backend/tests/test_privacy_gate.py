"""``PrivacyGate``: minimisation, refusals, the safety net and the audit.

The gate, its validation and its safety checks are implemented; the text work
is done by ``rules.py``. Tests that reach a rule fail with
``NotImplementedError`` against the stubs; the input-validation and
unclassified-context tests, which are decided before any rule runs, pass.
"""

import asyncio
import json
import logging
import time
import unicodedata
import unittest
from unittest import mock

from paw_backend.research.privacy import (
    MAX_CONTEXT_PIECES,
    MAX_DRAFT_CHARS,
    MAX_MINIMIZED_QUERY_CHARS,
    MAX_PIECE_CHARS,
    ContextLabel,
    ContextPiece,
    InMemoryExternalSendAudit,
    MinimizedQuery,
    PrivacyGate,
    PrivacyInput,
    PrivacyRefusal,
    RefusalReason,
    WithheldCounts,
    rules,
)
from paw_backend.research.privacy.gate import (
    merge_spans,
    replace_spans,
    widen_to_whole_words,
)
from paw_backend.research.providers import (
    KIND_ORDER,
    ProviderKind,
    ResearchRequest,
)
from paw_backend.research.providers.contract import MAX_QUERY_CHARS
from paw_backend.tools.credentials import redact_text

from .privacy_support import (
    NOW,
    OTHER_PROJECT_ID,
    PROJECT_ID,
    guarded,
    make_gate,
    memory,
    piece,
    private_source,
    public,
    raw_conversation,
    secret,
    wait_for_event_or_task_error,
)
from .research_support import SECRET

Reason = RefusalReason
WEB = frozenset({ProviderKind.WEB})
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
DOTTED_I = "\u0130"  # İ
DECOMPOSED_I = "i\u0307"  # what İ folds to
# Pairs of texts that only FULL Unicode case folding sees as equal; ``lower()``
# keeps them apart. Both directions of each pair are used.
FOLD_PAIRS = (
    ("straßeabcdefghijkl", "STRASSEabcdefghijkl"),
    ("STRAẞEabcdefghijkl", "strasseabcdefghijkl"),
    (f"{DOTTED_I}stanbul office secret", f"{DECOMPOSED_I}stanbul office secret"),
    ("ΟΔΟΣ ΑΘΗΝΩΝ ΜΥΣΤΙΚΟ", "οδος αθηνων μυστικο"),
    ("σας σας σας σας σας", "ΣΑΣ ΣΑΣ ΣΑΣ ΣΑΣ ΣΑΣ"),
)


def refusal(test: unittest.TestCase, function, *args, **kwargs) -> PrivacyRefusal:
    with test.assertRaises(PrivacyRefusal) as caught:
        function(*args, **kwargs)
    return caught.exception


class SpanHelpersTest(unittest.TestCase):
    def test_merge_spans_sorts_and_merges(self):
        self.assertEqual(merge_spans([]), ())
        self.assertEqual(merge_spans([(5, 9), (0, 3), (2, 4)]), ((0, 4), (5, 9)))
        self.assertEqual(merge_spans([(0, 10), (2, 4)]), ((0, 10),))
        self.assertEqual(merge_spans([(2, 4), (0, 10)]), ((0, 10),))
        self.assertEqual(merge_spans([(0, 5), (5, 9)]), ((0, 9),))
        self.assertEqual(merge_spans([(0, 3), (4, 9)]), ((0, 3), (4, 9)))
        self.assertEqual(merge_spans([(1, 2), (1, 2)]), ((1, 2),))

    def test_replace_spans_puts_one_space_for_each_span(self):
        self.assertEqual(replace_spans("abcdef", []), "abcdef")
        self.assertEqual(replace_spans("abcdef", [(1, 3)]), "a def")
        self.assertEqual(replace_spans("abcdef", [(0, 2), (4, 6)]), " cd ")
        self.assertEqual(replace_spans("abcdef", [(0, 6)]), " ")
        self.assertEqual(replace_spans("abcdef", [(0, 3), (3, 6)]), "  ")

    def test_replace_spans_rejects_bad_spans_without_the_text(self):
        for spans in (
            [(3, 5), (0, 2)],  # not sorted
            [(0, 3), (2, 5)],  # overlapping
            [(2, 2)],  # empty
            [(0, 99)],  # outside the text
            [(-1, 2)],
        ):
            with self.subTest(spans=spans):
                with self.assertRaises(ValueError) as caught:
                    replace_spans("PRIVATE", spans)
                self.assertNotIn("PRIVATE", str(caught.exception))


class WidenToWholeWordsTest(unittest.TestCase):
    TEXT = "ab cdef gh"  # the words are (0, 2), (3, 7) and (8, 10)

    def widen(self, spans, rewritten):
        return widen_to_whole_words(self.TEXT, spans, rewritten)

    def test_a_span_that_touches_a_rewritten_word_becomes_the_word(self):
        self.assertEqual(self.widen(((4, 5),), lambda word: word == "cdef"), ((3, 7),))
        self.assertEqual(self.widen(((3, 4),), lambda word: word == "cdef"), ((3, 7),))
        self.assertEqual(self.widen(((6, 7),), lambda word: word == "cdef"), ((3, 7),))
        self.assertEqual(self.widen(((6, 9),), lambda word: word == "cdef"), ((3, 9),))

    def test_other_words_and_spaces_are_left_alone(self):
        self.assertEqual(self.widen(((4, 5),), lambda word: False), ((4, 5),))
        self.assertEqual(self.widen(((4, 5),), lambda word: word == "gh"), ((4, 5),))
        # A span on the spaces between the words touches no word.
        self.assertEqual(self.widen(((2, 3),), lambda word: True), ((2, 3),))
        self.assertEqual(self.widen(((7, 8),), lambda word: True), ((7, 8),))

    def test_a_span_that_covers_a_word_whole_needs_no_widening(self):
        self.assertEqual(self.widen(((3, 7),), lambda word: True), ((3, 7),))
        self.assertEqual(self.widen(((2, 8),), lambda word: True), ((2, 8),))

    def test_widened_spans_are_merged_with_their_neighbours(self):
        self.assertEqual(self.widen(((1, 4),), lambda word: True), ((0, 7),))
        self.assertEqual(
            self.widen(((1, 2), (9, 10)), lambda word: True), ((0, 2), (8, 10))
        )
        self.assertEqual(self.widen((), lambda word: True), ())

    def test_only_the_touched_words_are_judged_and_each_one_once(self):
        judged = []

        def rewritten(word):
            judged.append(word)
            return False

        self.widen(((1, 2), (4, 6)), rewritten)
        self.assertEqual(judged, ["ab", "cdef"])


class ConstructionTest(unittest.TestCase):
    def test_the_audit_sink_is_validated(self):
        class SyncSink:
            def record(self, record):
                return None

        class NoArguments:
            async def record(self):
                return None

        class TwoArguments:
            async def record(self, record, extra):
                return None

        class NotCallable:
            record = None

        for sink in (
            None,
            object(),
            SyncSink(),
            NoArguments(),
            TwoArguments(),
            NotCallable(),
        ):
            with self.subTest(sink=type(sink).__name__), self.assertRaises(TypeError):
                PrivacyGate(sink)

    def test_a_valid_sink_and_defaults(self):
        PrivacyGate(InMemoryExternalSendAudit())

    def test_the_audit_timeout_is_validated(self):
        sink = InMemoryExternalSendAudit()
        for value in (0, -1, 60.5, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PrivacyGate(sink, audit_timeout_seconds=value)
        for value in (True, "5", None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                PrivacyGate(sink, audit_timeout_seconds=value)
        PrivacyGate(sink, audit_timeout_seconds=0.001)
        PrivacyGate(sink, audit_timeout_seconds=60)

    def test_the_clock_must_be_callable(self):
        with self.assertRaises(TypeError):
            PrivacyGate(InMemoryExternalSendAudit(), clock="now")


class InputValidationTest(unittest.TestCase):
    def setUp(self):
        self.gate, self.sink = make_gate()

    def test_the_draft_must_be_a_str(self):
        for draft in (None, b"bytes", 5, ["a"]):
            with self.subTest(draft=draft), self.assertRaises(TypeError):
                self.gate.minimize(draft, [])

    def test_the_context_must_be_a_list_or_a_tuple(self):
        for context in (None, "text", {1}, iter(()), {"a": 1}):
            with self.subTest(context=context), self.assertRaises(TypeError):
                self.gate.minimize("python", context)

    def test_a_draft_that_cannot_be_encoded_is_a_value_error_without_the_text(self):
        with self.assertRaises(ValueError) as caught:
            self.gate.minimize("bad \ud800 draft", [])
        self.assertNotIn("ud800", str(caught.exception))
        self.assertNotIn("bad", str(caught.exception))

    def test_input_errors_never_record_anything(self):
        for draft, context in ((None, []), ("x", None), ("bad \ud800", [])):
            with self.subTest(draft=draft), self.assertRaises((TypeError, ValueError)):
                self.gate.minimize(draft, context)
        self.assertEqual(self.sink.records, ())


class DefaultDenyTest(unittest.TestCase):
    """Unclassified context is refused before any text is processed."""

    def setUp(self):
        self.gate, self.sink = make_gate()

    def test_an_element_that_is_not_a_piece_is_refused(self):
        for context in (
            ["some raw text"],
            [None],
            [{"label": "public", "text": "x"}],
            [("public", "x")],
            [piece(ContextLabel.PUBLIC, "ok"), "unlabelled"],
            ["unlabelled", piece(ContextLabel.PUBLIC, "ok")],
            [b"bytes"],
            [ContextLabel.PUBLIC],
        ):
            with self.subTest(context=repr(context)):
                error = refusal(self, self.gate.minimize, "python asyncio", context)
                self.assertIs(error.reason, Reason.UNCLASSIFIED_CONTEXT)

    def test_it_is_decided_before_the_draft_is_looked_at(self):
        error = refusal(self, self.gate.minimize, "x" * (MAX_DRAFT_CHARS + 1), ["raw"])
        self.assertIs(error.reason, Reason.UNCLASSIFIED_CONTEXT)

    def test_a_tuple_is_checked_like_a_list(self):
        error = refusal(self, self.gate.minimize, "python", ("raw",))
        self.assertIs(error.reason, Reason.UNCLASSIFIED_CONTEXT)

    def test_the_refusal_names_nothing_from_the_input(self):
        error = refusal(self, self.gate.minimize, "python", [SECRET])
        for text in (str(error), repr(error), repr(error.args)):
            self.assertNotIn(SECRET, text)
        self.assertIsNone(error.__cause__)


class SizeLimitTest(unittest.TestCase):
    def setUp(self):
        self.gate, self.sink = make_gate()

    def test_a_draft_over_the_limit_is_refused_and_the_limit_itself_is_not(self):
        error = refusal(self, self.gate.minimize, "a" * (MAX_DRAFT_CHARS + 1), [])
        self.assertIs(error.reason, Reason.DRAFT_TOO_LONG)
        # Exactly the limit is accepted (the words are cut to the query limit).
        result = self.gate.minimize("word " * 400, [])
        self.assertTrue(result.truncated)
        text = " ".join(["word"] * 400)
        self.assertLessEqual(len(text), MAX_DRAFT_CHARS)
        self.assertEqual(len(self.gate.minimize(text, []).query), 254)

    def test_more_than_the_maximum_number_of_pieces_is_refused(self):
        pieces = [public(f"piece {number}") for number in range(MAX_CONTEXT_PIECES + 1)]
        error = refusal(self, self.gate.minimize, "python", pieces)
        self.assertIs(error.reason, Reason.CONTEXT_TOO_LARGE)
        self.gate.minimize("python", pieces[:MAX_CONTEXT_PIECES])

    def test_too_much_context_text_is_refused(self):
        big = "a" * MAX_PIECE_CHARS
        error = refusal(
            self,
            self.gate.minimize,
            "python",
            [public(big), public(big), public("b")],
        )
        self.assertIs(error.reason, Reason.CONTEXT_TOO_LARGE)
        self.gate.minimize("python", [public(big), public(big)])

    def test_the_context_limit_counts_characters_of_every_label(self):
        big = "a" * MAX_PIECE_CHARS
        error = refusal(
            self,
            self.gate.minimize,
            "python",
            [secret(big), private_source(big), memory("b")],
        )
        self.assertIs(error.reason, Reason.CONTEXT_TOO_LARGE)

    def test_a_refusal_is_never_recorded(self):
        refusal(self, self.gate.minimize, "a" * (MAX_DRAFT_CHARS + 1), [])
        self.assertEqual(self.sink.records, ())


class NormalisedSizeLimitTest(unittest.TestCase):
    """The limits also hold AFTER NFKC and full case folding (Decision 0010).

    U+FDFA is one code point that NFKC turns into 18, so a context that is within
    the limits as written can become millions of characters, and the copy
    detection would build one window per character on the event loop. The gate
    counts the size of the text in the form the filter compares: the length of
    ``unicodedata.normalize("NFKC", text).casefold()``.
    """

    EXPANDING = "\ufdfa"  # 1 code point -> 18 after NFKC
    LIGATURE = "\ufb01"  # "ﬁ": 1 code point -> "fi" (2) after NFKC

    def setUp(self):
        self.gate, self.sink = make_gate()

    def test_the_test_characters_expand_as_assumed(self):
        self.assertEqual(len(self.EXPANDING), 1)
        self.assertEqual(len(unicodedata.normalize("NFKC", self.EXPANDING)), 18)
        self.assertEqual(unicodedata.normalize("NFKC", self.LIGATURE), "fi")

    def test_a_context_that_expands_past_the_limit_is_refused_before_any_text_work(
        self,
    ):
        for make in (private_source, memory, raw_conversation, secret, public):
            # Two pieces of the maximum raw size: 400,000 characters as written,
            # 7,200,000 after NFKC.
            context = [make(self.EXPANDING * MAX_PIECE_CHARS)] * 2
            with (
                self.subTest(label=make.__name__),
                mock.patch.object(
                    rules, "find_copied_spans", side_effect=AssertionError("windows")
                ) as windows,
                mock.patch.object(
                    rules, "normalize_text", side_effect=rules.normalize_text
                ) as normalise,
                mock.patch.object(
                    rules, "fold_for_match", side_effect=rules.fold_for_match
                ) as fold,
            ):
                started = time.perf_counter()
                error = refusal(self, self.gate.minimize, "python asyncio", context)
                elapsed = time.perf_counter() - started
                self.assertIs(error.reason, Reason.CONTEXT_TOO_LARGE)
                self.assertLess(elapsed, 1.0)
                # No window set is built and no text is normalised or folded by
                # the rules: the refusal comes first.
                self.assertEqual(windows.call_count, 0)
                self.assertEqual(normalise.call_count, 0)
                self.assertEqual(fold.call_count, 0)

    def test_a_piece_within_the_raw_limit_but_over_it_after_folding_is_refused(self):
        error = refusal(
            self,
            self.gate.minimize,
            "python",
            [private_source(self.EXPANDING * 12_000)],  # 216,000 > 200,000
        )
        self.assertIs(error.reason, Reason.CONTEXT_TOO_LARGE)
        # Pieces that are each within the limit but add up past the total.
        pieces = [private_source(self.EXPANDING * 11_000)] * 3  # 3 x 198,000
        error = refusal(self, self.gate.minimize, "python", pieces)
        self.assertIs(error.reason, Reason.CONTEXT_TOO_LARGE)

    def test_a_piece_of_exactly_the_limit_after_normalisation_is_accepted(self):
        exact = private_source(self.LIGATURE * (MAX_PIECE_CHARS // 2))
        self.assertEqual(len(exact.text), 100_000)  # far within the raw limit
        result = self.gate.minimize("python", [exact])  # 200,000 after NFKC
        self.assertEqual(result.query, "python")
        error = refusal(
            self,
            self.gate.minimize,
            "python",
            [private_source(self.LIGATURE * (MAX_PIECE_CHARS // 2) + "a")],
        )
        self.assertIs(error.reason, Reason.CONTEXT_TOO_LARGE)

    def test_the_total_of_exactly_the_limit_after_normalisation_is_accepted(self):
        half = private_source(self.LIGATURE * (MAX_PIECE_CHARS // 2))  # 200,000
        result = self.gate.minimize("python", [half, half])  # 400,000 in all
        self.assertEqual(result.query, "python")
        # One more character: 400,001 after NFKC, but only 200,001 as written.
        error = refusal(self, self.gate.minimize, "python", [half, half, memory("b")])
        self.assertIs(error.reason, Reason.CONTEXT_TOO_LARGE)

    def test_case_folding_counts_too(self):
        # "ß" folds to "ss": 100,000 of them are 200,000 folded characters.
        result = self.gate.minimize(
            "python", [private_source("\u00df" * (MAX_PIECE_CHARS // 2))]
        )
        self.assertEqual(result.query, "python")
        error = refusal(
            self,
            self.gate.minimize,
            "python",
            [private_source("\u00df" * (MAX_PIECE_CHARS // 2) + "a")],
        )
        self.assertIs(error.reason, Reason.CONTEXT_TOO_LARGE)

    def test_a_normal_large_ascii_context_still_works(self):
        big = ("lorem ipsum dolor sit amet " * 8_000)[:MAX_PIECE_CHARS]
        self.assertEqual(len(big), MAX_PIECE_CHARS)
        result = self.gate.minimize(
            "python asyncio", [private_source(big), raw_conversation(big)]
        )
        self.assertEqual(result.query, "python asyncio")
        self.assertEqual(result.pieces_matched, 0)

    def test_a_draft_that_expands_past_the_limit_is_refused(self):
        # 201 characters as written, 3,618 after NFKC.
        with mock.patch.object(
            rules, "normalize_text", side_effect=AssertionError("text work")
        ):
            error = refusal(self, self.gate.minimize, self.EXPANDING * 201, [])
        self.assertIs(error.reason, Reason.DRAFT_TOO_LONG)
        # "ß" folds to "ss": 1,001 of them are 2,002 characters.
        error = refusal(self, self.gate.minimize, "\u00df" * 1_001, [])
        self.assertIs(error.reason, Reason.DRAFT_TOO_LONG)

    def test_a_draft_of_exactly_the_limit_after_normalisation_is_accepted(self):
        # "ﬁ " is 2 characters as written and 3 after NFKC: 666 of them and "ab"
        # are 2,000; a third letter makes 2,001.
        draft = (self.LIGATURE + " ") * 666 + "ab"
        result = self.gate.minimize(draft, [])
        self.assertTrue(result.query.startswith("fi fi fi"))
        self.assertTrue(result.truncated)
        error = refusal(self, self.gate.minimize, draft + "c", [])
        self.assertIs(error.reason, Reason.DRAFT_TOO_LONG)

    def test_an_unclassified_context_is_still_decided_first(self):
        error = refusal(self, self.gate.minimize, self.EXPANDING * 201, ["raw"])
        self.assertIs(error.reason, Reason.UNCLASSIFIED_CONTEXT)


class MinimizeBasicsTest(unittest.TestCase):
    def setUp(self):
        self.gate, self.sink = make_gate()

    def test_a_clean_draft_passes_unchanged_with_zero_counts(self):
        result = self.gate.minimize("python asyncio timeout", [])
        self.assertIsInstance(result, MinimizedQuery)
        self.assertEqual(result.query, "python asyncio timeout")
        self.assertEqual(
            result.fingerprint,
            "sha256:"
            + __import__("hashlib").sha256(b"python asyncio timeout").hexdigest(),
        )
        self.assertFalse(result.truncated)
        self.assertEqual(
            (result.credentials_removed, result.pieces_matched, result.abstractions),
            (0, 0, 0),
        )
        self.assertEqual(result.withheld, WithheldCounts())

    def test_the_draft_is_normalised(self):
        result = self.gate.minimize("  Python\n\tasyncio​  timeout  ", [])
        self.assertEqual(result.query, "Python asyncio timeout")

    def test_the_result_can_be_used_as_a_research_request(self):
        result = self.gate.minimize("python asyncio timeout", [])
        self.assertEqual(ResearchRequest(result.query).query, result.query)

    def test_public_pieces_never_change_the_draft(self):
        text = "the quick brown fox jumps over the lazy dog"
        result = self.gate.minimize(f"explain {text} please", [public(text)])
        self.assertEqual(result.query, f"explain {text} please")
        self.assertEqual(result.pieces_matched, 0)
        self.assertEqual(result.withheld, WithheldCounts())

    def test_minimising_does_not_record_anything(self):
        self.gate.minimize("python asyncio timeout", [])
        self.assertEqual(self.sink.records, ())

    def test_it_is_deterministic(self):
        context = [private_source("the billing service retries payment three times")]
        draft = "how does the billing service retries payment three times work"
        self.assertEqual(
            self.gate.minimize(draft, context), self.gate.minimize(draft, context)
        )


class CopiedTextTest(unittest.TestCase):
    """Text of a non-public piece never reaches the query."""

    def setUp(self):
        self.gate, _ = make_gate()

    def test_private_source_text_is_removed(self):
        source = "the quick brown fox jumps over the lazy dog"
        result = self.gate.minimize(
            f"explain {source} in python", [private_source(source)]
        )
        self.assertEqual(result.query, "explain in python")
        self.assertEqual(result.pieces_matched, 1)
        self.assertEqual(result.withheld, WithheldCounts(private_source=1))

    def test_every_non_public_label_is_protected_alike(self):
        source = "the quick brown fox jumps over the lazy dog"
        for label in (
            ContextLabel.PRIVATE_SOURCE,
            ContextLabel.PRIVATE_MEMORY,
            ContextLabel.RAW_CONVERSATION,
        ):
            with self.subTest(label=label):
                result = self.gate.minimize(
                    f"explain {source} in python", [ContextPiece(label, source)]
                )
                self.assertEqual(result.query, "explain in python")
                self.assertEqual(result.pieces_matched, 1)

    def test_withheld_counts_come_from_the_context_not_from_the_match(self):
        context = [
            private_source("alpha alpha alpha alpha alpha"),
            private_source("beta beta beta beta beta beta"),
            memory("gamma gamma gamma gamma gamma"),
            raw_conversation("delta delta delta delta"),
            secret("epsilon-secret-value"),
            public("public text"),
        ]
        result = self.gate.minimize("python asyncio", context)
        self.assertEqual(result.pieces_matched, 0)
        self.assertEqual(
            result.withheld,
            WithheldCounts(
                private_source=2, private_memory=1, raw_conversation=1, secret=1
            ),
        )

    def test_a_secret_is_removed_with_a_short_window(self):
        result = self.gate.minimize(
            "why does login fail with hunter2!", [secret("hunter2!")]
        )
        self.assertEqual(result.query, "why does login fail with")
        self.assertEqual(result.pieces_matched, 1)
        self.assertEqual(result.withheld, WithheldCounts(secret=1))

    def test_a_part_of_a_long_secret_is_removed(self):
        # "horse" is 5 characters: far below the 16-character window of the
        # other labels, but a secret is protected part by part.
        result = self.gate.minimize(
            "try horse now", [secret("correct-horse-battery-staple")]
        )
        self.assertEqual(result.query, "try now")
        self.assertEqual(result.pieces_matched, 1)

    def test_a_secret_is_removed_whatever_its_case(self):
        result = self.gate.minimize("why does HUNTER2! fail here", [secret("hunter2!")])
        self.assertEqual(result.query, "why does fail here")

    def test_a_secret_shorter_than_the_window_is_still_removed(self):
        result = self.gate.minimize("reset the pin now", [secret("pin")])
        self.assertEqual(result.query, "reset the now")
        self.assertEqual(result.pieces_matched, 1)

    def test_a_piece_shorter_than_the_copy_window_is_removed_when_copied_whole(self):
        result = self.gate.minimize(
            "what does salary is 500k mean", [memory("salary is 500k")]
        )
        self.assertEqual(result.query, "what does mean")
        self.assertEqual(result.pieces_matched, 1)

    def test_case_spacing_and_unicode_form_do_not_hide_a_copy(self):
        piece_text = "Secret  Project\nAtlas Launch Plan"
        result = self.gate.minimize(
            "status of SECRET PROJECT ATLAS launch plan please",
            [private_source(piece_text)],
        )
        self.assertEqual(result.query, "status of please")

    def test_zero_width_characters_do_not_hide_a_copy(self):
        source = "alpha beta gamma delta epsilon"
        result = self.gate.minimize(
            "explain alp​ha beta gamma delta eps​ilon today", [memory(source)]
        )
        self.assertEqual(result.query, "explain today")

    def test_full_width_text_does_not_hide_a_copy(self):
        source = "alpha beta gamma delta epsilon"
        result = self.gate.minimize(
            "explain ａｌｐｈａ　ｂｅｔａ　ｇａｍｍａ　ｄｅｌｔａ　"
            "ｅｐｓｉｌｏｎ today",
            [memory(source)],
        )
        self.assertEqual(result.query, "explain today")

    def test_japanese_text_is_protected(self):
        source = "顧客データベースの接続情報は社外秘です"
        result = self.gate.minimize(f"{source} とは", [raw_conversation(source)])
        self.assertEqual(result.query, "とは")
        self.assertEqual(result.pieces_matched, 1)

    def test_several_pieces_and_several_copies(self):
        result = self.gate.minimize(
            "alpha needs zzz-secret-zzz and the sixteen char text here ok",
            [
                secret("zzz-secret-zzz"),
                private_source("the sixteen char text here"),
                memory("nothing shared with the draft at all"),
            ],
        )
        self.assertEqual(result.query, "alpha needs and ok")
        self.assertEqual(result.pieces_matched, 2)
        self.assertEqual(
            result.withheld,
            WithheldCounts(private_source=1, private_memory=1, secret=1),
        )

    def test_a_part_of_a_word_is_never_glued_back_together(self):
        result = self.gate.minimize("prefixhunter2!suffix now", [secret("hunter2!")])
        self.assertEqual(result.query, "prefix suffix now")

    def test_a_copy_inside_another_copy_is_removed_with_the_bigger_one(self):
        source = "alpha beta gamma delta epsilon"
        for context in (
            [private_source(source), secret("gamma")],
            [secret("gamma"), private_source(source)],
        ):
            with self.subTest(order=[p.label.value for p in context]):
                result = self.gate.minimize(f"x {source} y", context)
                self.assertEqual(result.query, "x y")
                self.assertEqual(result.pieces_matched, 2)

    def test_overlapping_copies_from_two_pieces_are_removed_together(self):
        result = self.gate.minimize(
            "x alpha beta gamma delta epsilon y",
            [
                private_source("alpha beta gamma delta"),
                memory("gamma delta epsilon zeta eta"),
            ],
        )
        self.assertEqual(result.query, "x y")
        self.assertEqual(result.pieces_matched, 2)

    def test_the_same_secret_twice_counts_the_piece_once(self):
        result = self.gate.minimize("x hunter2! y hunter2! z", [secret("hunter2!")])
        self.assertEqual(result.query, "x y z")
        self.assertEqual(result.pieces_matched, 1)

    def test_a_draft_that_is_only_copied_text_is_refused(self):
        source = "alpha beta gamma delta epsilon"
        error = refusal(self, self.gate.minimize, source, [private_source(source)])
        self.assertIs(error.reason, Reason.EMPTY_QUERY)

    def test_a_copy_is_found_under_full_unicode_case_folding(self):
        # The case of the review: "straße" and "STRASSE" are equal after full case
        # folding but no 16-character window of the lower-cased texts matches.
        for label in (
            ContextLabel.PRIVATE_SOURCE,
            ContextLabel.PRIVATE_MEMORY,
            ContextLabel.RAW_CONVERSATION,
        ):
            for one, other in FOLD_PAIRS:
                for draft_part, piece_text in ((one, other), (other, one)):
                    with self.subTest(label=label, draft=draft_part, piece=piece_text):
                        result = self.gate.minimize(
                            f"explain {draft_part} please",
                            [ContextPiece(label, piece_text)],
                        )
                        self.assertEqual(result.query, "explain please")
                        self.assertEqual(result.pieces_matched, 1)

    def test_the_review_case_end_to_end(self):
        # A private source that contains "STRASSEabcdefghijkl" between other
        # text, and a draft that spells it "straße...": the copy is removed.
        result = self.gate.minimize(
            "explain straßeabcdefghijkl please",
            [private_source("xxSTRASSEabcdefghijklyy")],
        )
        self.assertEqual(result.query, "explain please")
        self.assertEqual(result.pieces_matched, 1)
        # And the other way round; the "xx" and "yy" are not in the private piece.
        result = self.gate.minimize(
            "explain xxSTRASSEabcdefghijklyy please",
            [private_source("straßeabcdefghijkl")],
        )
        self.assertEqual(result.query, "explain xx yy please")
        self.assertEqual(result.pieces_matched, 1)

    def test_a_secret_is_found_under_full_unicode_case_folding(self):
        for secret_text, draft in (
            ("Straße", "the STRASSE road"),
            ("STRASSE", "the straße road"),
            ("ΟΔΟΣ", "the οδος road"),
            ("οδος", "the ΟΔΟΣ road"),
        ):
            with self.subTest(secret=secret_text, draft=draft):
                result = self.gate.minimize(draft, [secret(secret_text)])
                self.assertEqual(result.query, "the road")
                self.assertEqual(result.pieces_matched, 1)

    def test_the_copy_window_is_counted_in_folded_characters(self):
        # The piece "ß" is "ss" for the comparison: a lone "s" is not a copy of
        # it, and the copy is removed as a whole.
        result = self.gate.minimize("sea level", [secret("ß")])
        self.assertEqual(result.query, "sea level")
        self.assertEqual(result.pieces_matched, 0)
        result = self.gate.minimize("the class", [secret("ß")])
        self.assertEqual(result.query, "the cla")
        self.assertEqual(result.pieces_matched, 1)

    def test_a_short_copy_below_the_window_stays(self):
        result = self.gate.minimize(
            "the quick brown fox", [private_source("a story about the quick brown dog")]
        )
        # "the quick brown " is 16 characters: it is a copy; "fox" stays.
        self.assertEqual(result.query, "fox")
        result = self.gate.minimize(
            "the quick brown", [private_source("a story about the quick brown dog")]
        )
        # 15 characters are shorter than the window: nothing is copied.
        self.assertEqual(result.query, "the quick brown")


class CredentialAndAbstractionTest(unittest.TestCase):
    def setUp(self):
        self.gate, _ = make_gate()

    def test_a_credential_in_the_draft_is_removed_and_counted(self):
        result = self.gate.minimize(f"use {AWS_KEY} for the bucket", [])
        self.assertEqual(result.query, "use for the bucket")
        self.assertEqual(result.credentials_removed, 1)
        self.assertEqual(result.pieces_matched, 0)

    def test_the_credential_rule_runs_before_the_copy_rule(self):
        # A credential is removed whole from the draft as written, before a copy
        # removal can cut it into pieces that no rule recognises (Decision 0010).
        # It is counted as a credential; the copy rule finds nothing left of it.
        result = self.gate.minimize(f"use {AWS_KEY} now", [secret(AWS_KEY)])
        self.assertEqual(result.query, "use now")
        self.assertEqual(result.pieces_matched, 0)
        self.assertEqual(result.credentials_removed, 1)

    def test_a_draft_that_is_only_a_credential_is_refused(self):
        error = refusal(self, self.gate.minimize, AWS_KEY, [])
        self.assertIs(error.reason, Reason.EMPTY_QUERY)

    def test_the_abstraction_rules_run_in_the_documented_order(self):
        draft = (
            "see https://docs.python.org/3/library/asyncio.html and email a@b.org "
            "at /home/me/x.py v3.13.15 build 1234567"
        )
        result = self.gate.minimize(draft, [])
        self.assertEqual(result.query, "see docs.python.org and email at v3.13 build")
        self.assertEqual(result.abstractions, 5)

    def test_a_private_host_is_removed_before_its_number_is(self):
        # Hosts run before IDs: the whole token goes, not just its digits.
        result = self.gate.minimize("check 1234567.internal now", [])
        self.assertEqual(result.query, "check now")
        self.assertEqual(result.abstractions, 1)

    def test_private_details_are_all_removed(self):
        result = self.gate.minimize(
            "error on db.internal:5432 from 10.0.0.7 user bob@corp.example.com "
            "in /srv/app/main.py id 987654321 "
            "uuid 123e4567-e89b-12d3-a456-426614174000",
            [],
        )
        self.assertEqual(result.query, "error on from user in id uuid")
        self.assertEqual(result.abstractions, 6)

    def test_scoped_and_absolute_private_hosts_are_removed(self):
        # A zone identifier, a trailing dot before the port and user information
        # must not keep an internal endpoint in the query that is sent out.
        result = self.gate.minimize(
            "retry [fe80::1%eth0]:8080 then db.internal.:5432 and ssh admin@10.0.0.5 "
            "plus fe80::2%25eth1 or LOCALHOST.:3000 for python 3.13",
            [],
        )
        self.assertEqual(result.query, "retry then and ssh plus or for python 3.13")
        self.assertEqual(result.abstractions, 5)
        for private in ("fe80", "eth0", "internal", "10.0.0.5", "LOCALHOST"):
            self.assertNotIn(private, result.query)

    def test_a_private_ipv6_network_is_removed(self):
        # The review case: an unbracketed IPv6 network in CIDR form.
        for network in (
            "fd12:3456:789a::/48",
            "fd00::/8",
            "fe80::1%eth0/64",
            "[fd12::/48]",
            "[fd12::]/48",
        ):
            with self.subTest(network=network):
                result = self.gate.minimize(f"allow {network} through the firewall", [])
                self.assertEqual(result.query, "allow through the firewall")
                self.assertEqual(result.abstractions, 1)

    def test_a_time_range_or_a_ratio_is_not_taken_for_a_network(self):
        for draft in ("meet 10:30/12:00 sharp", "ratio 16:9/2 wide", "a::b/c stays"):
            with self.subTest(draft=draft):
                result = self.gate.minimize(draft, [])
                self.assertEqual(result.query, draft)
                self.assertEqual(result.abstractions, 0)

    def test_a_public_query_is_left_alone(self):
        for draft in (
            "python asyncio TaskGroup exception handling",
            "fastapi 0.141 dependency injection",
            "How do I use SQLAlchemy 2.0 async sessions?",
            "検索クエリ の 書き方",
        ):
            with self.subTest(draft=draft):
                result = self.gate.minimize(draft, [])
                self.assertEqual(result.query, draft)
                self.assertEqual(result.abstractions, 0)

    def test_minimising_twice_changes_nothing_more(self):
        for draft in (
            "see https://docs.python.org/3/library/asyncio.html v3.13.15 build",
            "error on db.internal:5432 in /srv/app/main.py id 987654321",
            "explain the quick brown fox jumps over the lazy dog in python",
        ):
            with self.subTest(draft=draft):
                first = self.gate.minimize(
                    draft,
                    [private_source("the quick brown fox jumps over the lazy dog")],
                )
                second = self.gate.minimize(first.query, [])
                self.assertEqual(second.query, first.query)
                self.assertEqual(second.abstractions, 0)


def eight_character_windows(text: str) -> set[str]:
    return {text[i : i + 8] for i in range(len(text) - 7)}


# (name, the credential as it is written in a draft, the part of it that must not
# reach the query in ANY 8-character piece). Every one is a shape that
# ``redact_text`` recognises (checked in the test).
CREDENTIAL_SHAPES = (
    ("github token", "ghp_Zx9qW4tRb7Lm2PhKvN5cD8fG1jH3sA6yUeXo", None),
    ("github fine-grained", "github_pat_11ABCDEFG0Zx9qW4tRb7Lm2PhKvN5cD8fG1jH3s", None),
    ("gitlab token", "glpat-Zx9qW4tRb7Lm2PhKvN5cD8fG", None),
    ("openai style key", "sk-Zx9qW4tRb7Lm2PhKvN5cD8fG1jH3sA6yUeXo", None),
    # Written in two pieces so that no complete secret-shaped literal is in the
    # source (GitHub push protection rejects them, even as test data).
    ("stripe key", "sk_" + "live_Zx9qW4tRb7Lm2PhKvN5cD8fG", None),
    ("aws access key", "AKIAIOSFODNN7EXAMPLE", None),
    ("google api key", "AIzaSyZx9qW4tRb7Lm2PhKvN5cD8fG1jH3sA6yU", None),
    ("slack token", "xoxb-" + "1234567890-Zx9qW4tRb7Lm2PhKvN", None),
    ("npm token", "npm_Zx9qW4tRb7Lm2PhKvN5cD8fG1jH3sA6yUeXo", None),
    (
        "jwt",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        None,
    ),
    (
        "bearer header",
        "Bearer Zx9qW4tRb7Lm2PhKvN5cD8fG1jH3sA6yUeXo",
        "Zx9qW4tRb7Lm2PhKvN5cD8fG1jH3sA6yUeXo",
    ),
    (
        "private key",
        "-----BEGIN RSA PRIVATE KEY----- MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgO "
        "-----END RSA PRIVATE KEY-----",
        "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgO",
    ),
    (
        "url with user information",
        "https://svc:Zx9qW4tRb7Lm2PhKvN5c@example.com/x.git",
        "Zx9qW4tRb7Lm2PhKvN5c",
    ),
    (
        "password assignment",
        "DB_PASSWORD=Zx9qW4tRb7Lm2PhKvN5cD8fG",
        "Zx9qW4tRb7Lm2PhKvN5cD8fG",
    ),
    (
        "password flag",
        "--password Zx9qW4tRb7Lm2PhKvN5cD8fG",
        "Zx9qW4tRb7Lm2PhKvN5cD8fG",
    ),
)


class CredentialBeforeCopyTest(unittest.TestCase):
    """A copy removal must not cut a credential into pieces nothing recognises.

    Found in review: ``_remove_copied_text`` ran first and replaced any 16
    characters shared with a private piece by a space, so a credential that shared
    them became fragments (``ghp_ABCDEF abcdefghij``) that neither
    ``strip_credentials`` nor the final ``redact_text`` recognised.
    """

    def setUp(self):
        self.gate, _ = make_gate()

    def test_the_shapes_are_recognised_credentials(self):
        for name, credential, _ in CREDENTIAL_SHAPES:
            with self.subTest(shape=name):
                self.assertGreater(redact_text(credential)[1], 0)

    def test_a_credential_that_shares_text_with_a_piece_is_removed_whole(self):
        for name, credential, secret_part in CREDENTIAL_SHAPES:
            core = secret_part or credential
            third = len(core) // 3
            # What the piece shares with the credential: the secret part in four
            # ways (the query must then be exactly the one without any context: the
            # credential is gone before the piece is looked at), and the first and
            # last 24 characters of the credential as written (with its name, its
            # header or its footer).
            slices = {
                "head": (core[:20], True),
                "middle": (core[third : third + 20], True),
                "tail": (core[-20:], True),
                "all": (core, True),
                "written head": (credential[:24], False),
                "written tail": (credential[-24:], False),
            }
            draft = f"why does {credential} fail with psycopg"
            baseline = self.gate.minimize(draft, [])
            for where, (shared, exact) in slices.items():
                for label in (ContextLabel.PRIVATE_SOURCE, ContextLabel.SECRET):
                    with self.subTest(shape=name, shared=where, label=label):
                        result = self.gate.minimize(
                            draft, [ContextPiece(label, shared)]
                        )
                        if exact:
                            self.assertEqual(result.query, baseline.query)
                            self.assertEqual(
                                result.credentials_removed, baseline.credentials_removed
                            )
                        self.assertGreaterEqual(result.credentials_removed, 1)
                        for window in eight_character_windows(core):
                            self.assertNotIn(window, result.query)

    def test_the_review_case(self):
        token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
        result = self.gate.minimize(
            f"why does {token} fail", [private_source(f"deploy note {token[:22]}")]
        )
        self.assertEqual(result.query, "why does fail")
        self.assertEqual(result.credentials_removed, 1)
        self.assertNotIn("ghp_", result.query)
        self.assertNotIn("abcdefghij", result.query)

    def test_a_credential_in_a_long_copied_run_is_removed_whole(self):
        # The credential is inside text that the piece also holds: the words around
        # it are a copy, the credential is not a part of the query in either case.
        token = "ghp_Zx9qW4tRb7Lm2PhKvN5cD8fG1jH3sA6yUeXo"
        note = "the billing service reads its deploy key from the vault every night"
        result = self.gate.minimize(
            f"explain {note} {token} please",
            [private_source(f"{note} {token}")],
        )
        self.assertEqual(result.query, "explain please")
        for window in eight_character_windows(token):
            self.assertNotIn(window, result.query)

    def test_a_credential_that_a_copy_removal_uncovers_is_removed_too(self):
        # The letters in front of the key hide it from the rule (it needs a
        # character that is not a letter or digit before it); once the copy is gone
        # it shows, and the credential rule looks again.
        key = "sk-Zx9qW4tRb7Lm2PhKvN5c"
        prefix = "abcdefghijklmnop"
        self.assertEqual(redact_text(f"{prefix}{key}")[1], 0)
        result = self.gate.minimize(f"why {prefix}{key} fail", [memory(prefix)])
        self.assertEqual(result.query, "why fail")
        self.assertEqual(result.credentials_removed, 1)
        self.assertEqual(result.pieces_matched, 1)

    def test_a_long_word_that_holds_a_credential_is_removed_whole(self):
        # The same draft with a word that the opaque-token rule rewrites: the copy
        # then removes the whole word (no fragment of the key is left).
        token = "ghp_Zx9qW4tRb7Lm2PhKvN5cD8fG1jH3sA6yUeXo"
        prefix = "abcdefghijklmnop"
        result = self.gate.minimize(f"why {prefix}{token} fail", [memory(prefix)])
        self.assertEqual(result.query, "why fail")
        self.assertEqual(result.pieces_matched, 1)

    def test_a_copy_only_removes_more_never_less(self):
        token = "ghp_Zx9qW4tRb7Lm2PhKvN5cD8fG1jH3sA6yUeXo"
        note = "our staging cluster lives behind the office vpn gateway"
        draft = f"{note} with {token} see"
        for context in ([], [private_source(note)], [private_source(token)]):
            with self.subTest(pieces=len(context)):
                result = self.gate.minimize(draft, context)
                self.assertNotIn("ghp_", result.query)
                self.assertNotIn("Zx9qW4tR", result.query)


class WholeTokenCopyTest(unittest.TestCase):
    """A copy removal must not cut a token that an abstraction rule would remove.

    The abstraction rules take out whole words (a path, an address, an identifier,
    a URL). Cutting such a word first left the fragments (``an-2026.md`` of a
    private path, ``123e4567-`` of a UUID) that no rule recognises any more. A word
    that one of the rules rewrites is therefore removed WHOLE when a copy touches
    it (Decision 0010).
    """

    def setUp(self):
        self.gate, _ = make_gate()

    def test_a_private_path_touched_by_a_copy_leaves_no_fragment(self):
        result = self.gate.minimize(
            "open /srv/billing/secret-plan-2026.md now",
            [private_source("see billing/secret-pl ok")],
        )
        self.assertEqual(result.query, "open now")
        self.assertEqual(result.pieces_matched, 1)

    def test_a_uuid_touched_by_a_copy_leaves_no_fragment(self):
        result = self.gate.minimize(
            "user 123e4567-e89b-12d3-a456-426614174000 failed",
            [memory("x e89b-12d3-a456-4266 y")],
        )
        self.assertEqual(result.query, "user failed")

    def test_a_private_host_touched_by_a_copy_leaves_no_fragment(self):
        result = self.gate.minimize(
            "ping printer-floor3.corp.local:9100 now",
            [private_source("zz floor3.corp.loca zz")],
        )
        self.assertEqual(result.query, "ping now")

    def test_an_address_touched_by_a_copy_leaves_no_fragment(self):
        result = self.gate.minimize(
            "route fd12:3456:789a:1::/64 now",
            [private_source("zz 3456:789a:1::/64 zz")],
        )
        self.assertEqual(result.query, "route now")

    def test_an_id_touched_by_a_copy_leaves_no_fragment(self):
        result = self.gate.minimize(
            "ticket 9876543210 now", [private_source("ticket 98765432")]
        )
        self.assertEqual(result.query, "now")

    def test_a_hash_touched_by_a_copy_leaves_no_fragment(self):
        digest = "0123456789abcdef0123456789abcdef"
        result = self.gate.minimize(
            f"commit {digest} broke", [raw_conversation(f"a {digest[8:26]} b")]
        )
        self.assertEqual(result.query, "commit broke")

    def test_a_long_opaque_token_touched_by_a_copy_leaves_no_fragment(self):
        blob = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2"
        result = self.gate.minimize(
            f"use {blob} now", [private_source(f"zz {blob[10:30]} zz")]
        )
        self.assertEqual(result.query, "use now")

    def test_a_version_touched_by_a_copy_is_removed_whole(self):
        result = self.gate.minimize(
            "run build-3.13.15-final now", [private_source("ld-3.13.15-fin")]
        )
        self.assertEqual(result.query, "run now")

    def test_a_url_touched_by_a_copy_leaves_no_host_fragment(self):
        result = self.gate.minimize(
            "see https://wiki.acme-internal.example.com now",
            [private_source("acme-internal.ex")],
        )
        self.assertEqual(result.query, "see now")

    def test_an_email_touched_by_a_copy_leaves_no_fragment(self):
        result = self.gate.minimize(
            "mail alice.smith@corp-mail.example.org today",
            [memory("smith@corp-mail.exa")],
        )
        self.assertEqual(result.query, "mail today")

    def test_a_word_no_rule_rewrites_is_still_cut_only_where_it_was_copied(self):
        # The existing behaviour: the fragments of an ordinary word stay apart.
        result = self.gate.minimize("prefixhunter2!suffix now", [secret("hunter2!")])
        self.assertEqual(result.query, "prefix suffix now")

    def test_a_word_that_no_copy_touches_is_left_to_the_rules(self):
        result = self.gate.minimize(
            "open /srv/app/main.py and explain the quick brown fox jumps over",
            [private_source("the quick brown fox jumps over")],
        )
        self.assertEqual(result.query, "open and explain")
        self.assertEqual(result.abstractions, 1)
        self.assertEqual(result.pieces_matched, 1)

    def test_a_copy_that_covers_the_whole_word_needs_no_widening(self):
        result = self.gate.minimize(
            "open /srv/billing/plan.md now", [private_source("/srv/billing/plan.md")]
        )
        self.assertEqual(result.query, "open now")
        self.assertEqual(result.abstractions, 0)
        self.assertEqual(result.pieces_matched, 1)

    def test_a_copy_that_only_borders_a_word_does_not_widen_to_it(self):
        # The copied run ends with the space in front of the path, or starts with
        # the space behind it: the path itself is not touched, so the path rule
        # removes it (and counts it).
        note = "the quick brown fox jumps over the lazy dog"
        for draft, context_text in (
            (f"explain {note} /srv/app/plan.md now", f"a {note} z"),
            (f"open /srv/app/plan.md {note} now", f"a {note} z"),
        ):
            with self.subTest(draft=draft):
                result = self.gate.minimize(draft, [private_source(context_text)])
                self.assertEqual(result.abstractions, 1)
                self.assertEqual(result.pieces_matched, 1)
                self.assertNotIn("plan", result.query)

    def test_the_widening_never_reaches_a_neighbouring_word(self):
        result = self.gate.minimize(
            "before /srv/billing/secret-plan-2026.md after",
            [private_source("billing/secret-pl")],
        )
        self.assertEqual(result.query, "before after")


class TruncationTest(unittest.TestCase):
    def setUp(self):
        self.gate, _ = make_gate()

    def test_a_long_draft_is_cut_at_a_word(self):
        result = self.gate.minimize(" ".join(["abcde"] * 50), [])
        self.assertTrue(result.truncated)
        self.assertEqual(result.query, " ".join(["abcde"] * 42))
        self.assertLessEqual(len(result.query), MAX_MINIMIZED_QUERY_CHARS)
        self.assertLessEqual(len(result.query), MAX_QUERY_CHARS)

    def test_exactly_the_limit_is_not_truncated(self):
        draft = " ".join(["abcde"] * 42) + " abcd"
        self.assertEqual(len(draft), MAX_MINIMIZED_QUERY_CHARS)
        result = self.gate.minimize(draft, [])
        self.assertFalse(result.truncated)
        self.assertEqual(result.query, draft)

    def test_one_over_the_limit_is_truncated(self):
        draft = " ".join(["abcde"] * 42) + " abcde"
        self.assertEqual(len(draft), MAX_MINIMIZED_QUERY_CHARS + 1)
        result = self.gate.minimize(draft, [])
        self.assertTrue(result.truncated)
        self.assertEqual(result.query, " ".join(["abcde"] * 42))

    def test_the_cut_happens_after_the_removals(self):
        source = "the quick brown fox jumps over the lazy dog"
        draft = f"{' '.join(['abcde'] * 30)} {source} tail"
        result = self.gate.minimize(draft, [private_source(source)])
        # 30 words of 5 characters and a tail: 30 * 5 + 29 + 1 + 4 = 184 characters.
        self.assertFalse(result.truncated)
        self.assertEqual(result.query, f"{' '.join(['abcde'] * 30)} tail")


class SafetyNetTest(unittest.TestCase):
    """A faulty rule must not let private text or a credential through."""

    def setUp(self):
        self.gate, self.sink = make_gate()

    def test_a_secret_left_by_a_broken_copy_rule_is_refused(self):
        with mock.patch.object(rules, "find_copied_spans", return_value=()):
            error = refusal(
                self, self.gate.minimize, "use hunter2! now", [secret("hunter2!")]
            )
        self.assertIs(error.reason, Reason.PRIVATE_TEXT_REMAINS)

    def test_one_word_of_a_secret_left_behind_is_refused(self):
        with mock.patch.object(rules, "find_copied_spans", return_value=()):
            error = refusal(
                self,
                self.gate.minimize,
                "try correct now",
                [secret("correct horse battery staple")],
            )
        self.assertIs(error.reason, Reason.PRIVATE_TEXT_REMAINS)

    def test_a_four_character_word_of_a_secret_is_enough_to_refuse(self):
        with mock.patch.object(rules, "find_copied_spans", return_value=()):
            error = refusal(
                self, self.gate.minimize, "use wxyz here", [secret("wxyz longer-word")]
            )
        self.assertIs(error.reason, Reason.PRIVATE_TEXT_REMAINS)

    def test_a_short_word_of_a_secret_is_not_enough_to_refuse(self):
        with mock.patch.object(rules, "find_copied_spans", return_value=()):
            result = self.gate.minimize("try the cat", [secret("the cat horse")])
        self.assertEqual(result.query, "try the cat")

    def test_a_whole_private_piece_left_by_a_broken_copy_rule_is_refused(self):
        for label in (
            ContextLabel.PRIVATE_SOURCE,
            ContextLabel.PRIVATE_MEMORY,
            ContextLabel.RAW_CONVERSATION,
        ):
            with self.subTest(label=label):
                with mock.patch.object(rules, "find_copied_spans", return_value=()):
                    error = refusal(
                        self,
                        self.gate.minimize,
                        "explain Salary Is 500k please",
                        [ContextPiece(label, "salary is 500k")],
                    )
                self.assertIs(error.reason, Reason.PRIVATE_TEXT_REMAINS)

    def test_the_check_uses_full_unicode_case_folding(self):
        # With the copy rule broken, the whole-piece check alone must refuse text
        # that is equal to a private piece only after full case folding.
        for piece_text, draft in (
            ("Straße Lagerbestand geheim", "explain STRASSE LAGERBESTAND GEHEIM now"),
            ("STRASSE LAGERBESTAND GEHEIM", "explain straße lagerbestand geheim now"),
            ("ΟΔΟΣ ΑΘΗΝΩΝ", "explain οδος αθηνων now"),
            ("οδος αθηνων", "explain ΟΔΟΣ ΑΘΗΝΩΝ now"),
            (f"{DOTTED_I}stanbul", f"explain {DECOMPOSED_I.upper()}STANBUL now"),
            (f"{DECOMPOSED_I}stanbul", f"explain {DOTTED_I}STANBUL now"),
        ):
            with self.subTest(piece=piece_text, draft=draft):
                with mock.patch.object(rules, "find_copied_spans", return_value=()):
                    error = refusal(
                        self, self.gate.minimize, draft, [private_source(piece_text)]
                    )
                self.assertIs(error.reason, Reason.PRIVATE_TEXT_REMAINS)

    def test_a_secret_word_is_found_under_full_unicode_case_folding(self):
        for secret_text, draft in (
            ("Straße-Nummer", "use STRASSE-NUMMER now"),
            ("STRASSE-NUMMER", "use straße-nummer now"),
        ):
            with self.subTest(secret=secret_text):
                with mock.patch.object(rules, "find_copied_spans", return_value=()):
                    error = refusal(
                        self, self.gate.minimize, draft, [secret(secret_text)]
                    )
                self.assertIs(error.reason, Reason.PRIVATE_TEXT_REMAINS)

    def test_the_check_ignores_case_spacing_and_format_characters(self):
        with mock.patch.object(rules, "find_copied_spans", return_value=()):
            error = refusal(
                self,
                self.gate.minimize,
                "explain SALARY​ IS 500k please",
                [memory("salary   is\n500k")],
            )
        self.assertIs(error.reason, Reason.PRIVATE_TEXT_REMAINS)

    def test_public_pieces_are_not_part_of_the_check(self):
        with mock.patch.object(rules, "find_copied_spans", return_value=()):
            result = self.gate.minimize(
                "explain salary is 500k", [public("salary is 500k")]
            )
        self.assertEqual(result.query, "explain salary is 500k")

    def test_a_credential_left_by_a_broken_rule_is_refused(self):
        with mock.patch.object(
            rules, "strip_credentials", side_effect=lambda t: (t, 0)
        ):
            error = refusal(self, self.gate.minimize, f"key {AWS_KEY} here", [])
        self.assertIs(error.reason, Reason.CREDENTIAL_REMAINS)

    def test_a_rule_that_creates_a_credential_is_refused_at_once(self):
        # The credential must be noticed right after the rule that produced it: a
        # later rule that cuts it into pieces would hide it from the final check.
        token = "ghp_Zx9qW4tRb7Lm2PhKvN5cD8fG1jH3sA6yUeXo"

        def create(text):
            return f"{text} {token}", 1

        def cut(text):
            return text.replace(token[10:22], " "), 1

        with (
            mock.patch.object(rules, "abstract_urls", side_effect=create),
            mock.patch.object(rules, "abstract_emails", side_effect=cut),
        ):
            error = refusal(self, self.gate.minimize, "python", [])
        self.assertIs(error.reason, Reason.CREDENTIAL_REMAINS)
        self.assertEqual(self.sink.records, ())

    def test_a_query_with_no_word_character_is_refused(self):
        with mock.patch.object(
            rules, "abstract_urls", side_effect=lambda t: ("!!! ---", 0)
        ):
            error = refusal(self, self.gate.minimize, "python", [])
        self.assertIs(error.reason, Reason.EMPTY_QUERY)

    def test_the_word_check_accepts_any_script_and_underscore(self):
        for query in ("検索", "_", "a"):
            with self.subTest(query=query):
                with mock.patch.object(
                    rules, "abstract_urls", side_effect=lambda t, q=query: (q, 0)
                ):
                    self.assertEqual(self.gate.minimize("python", []).query, query)

    def test_a_faulty_rule_result_never_reaches_the_audit(self):
        with mock.patch.object(rules, "find_copied_spans", return_value=()):
            refusal(self, self.gate.minimize, "use hunter2! now", [secret("hunter2!")])
        self.assertEqual(self.sink.records, ())


class AuthorizeTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_send_is_recorded_before_the_query_is_returned(self):
        gate, sink = make_gate()
        result = await guarded(
            gate.authorize(
                "python asyncio timeout",
                [],
                project_id=PROJECT_ID,
                provider_kinds=WEB,
            )
        )
        self.assertEqual(result.query, "python asyncio timeout")
        self.assertEqual(len(sink.records), 1)
        recorded = sink.records[0]
        self.assertEqual(recorded.recorded_at, NOW)
        self.assertEqual(recorded.project_id, PROJECT_ID)
        self.assertEqual(recorded.query_fingerprint, result.fingerprint)
        self.assertEqual(recorded.query_chars, len("python asyncio timeout"))
        self.assertEqual(recorded.provider_kinds, (ProviderKind.WEB,))
        self.assertEqual(recorded.withheld, WithheldCounts())
        self.assertEqual(
            (
                recorded.credentials_removed,
                recorded.pieces_matched,
                recorded.abstractions,
            ),
            (0, 0, 0),
        )
        self.assertFalse(recorded.truncated)

    async def test_the_record_carries_the_counts_of_what_was_removed(self):
        gate, sink = make_gate()
        source = "the quick brown fox jumps over the lazy dog"
        await guarded(
            gate.authorize(
                f"explain {source} with {AWS_KEY} at /srv/x.py v1.2.3 " + "word " * 80,
                [
                    private_source(source),
                    secret("hunter2!"),
                    memory("other memory text"),
                ],
                project_id=OTHER_PROJECT_ID,
                provider_kinds=frozenset({ProviderKind.GITHUB, ProviderKind.WEB}),
            )
        )
        (recorded,) = sink.records
        self.assertEqual(recorded.project_id, OTHER_PROJECT_ID)
        self.assertEqual(
            recorded.provider_kinds, (ProviderKind.WEB, ProviderKind.GITHUB)
        )
        self.assertEqual(recorded.credentials_removed, 1)
        self.assertEqual(recorded.pieces_matched, 1)
        self.assertEqual(recorded.abstractions, 2)
        self.assertTrue(recorded.truncated)
        self.assertEqual(
            recorded.withheld,
            WithheldCounts(private_source=1, private_memory=1, secret=1),
        )

    async def test_provider_kinds_are_in_kind_order(self):
        gate, sink = make_gate()
        await guarded(
            gate.authorize(
                "python",
                [],
                project_id=PROJECT_ID,
                provider_kinds=frozenset(ProviderKind),
            )
        )
        kinds = sink.records[0].provider_kinds
        self.assertEqual(kinds, tuple(sorted(ProviderKind, key=KIND_ORDER.__getitem__)))
        self.assertEqual(kinds[0], ProviderKind.WEB)

    async def test_the_record_never_holds_the_query_or_removed_text(self):
        gate, sink = make_gate()
        private = "the marker zebra quokka wombat text from a private source"
        await guarded(
            gate.authorize(
                f"explain {private} in python",
                [private_source(private), secret("hunter2!")],
                project_id=PROJECT_ID,
                provider_kinds=WEB,
            )
        )
        (recorded,) = sink.records
        for text in (repr(recorded), json.dumps(recorded.to_dict()), str(recorded)):
            for word in ("zebra", "quokka", "wombat", "explain", "python", "hunter2"):
                self.assertNotIn(word, text)

    async def test_a_timezone_aware_clock_is_converted_to_utc(self):
        from datetime import datetime, timedelta, timezone

        jst = timezone(timedelta(hours=9))
        gate, sink = make_gate(clock=lambda: datetime(2026, 9, 24, 21, 0, tzinfo=jst))
        await guarded(
            gate.authorize("python", [], project_id=PROJECT_ID, provider_kinds=WEB)
        )
        self.assertEqual(sink.records[0].recorded_at, NOW)

    async def test_every_send_is_recorded_once(self):
        gate, sink = make_gate()
        for number in range(3):
            await guarded(
                gate.authorize(
                    f"python topic{number}",
                    [],
                    project_id=PROJECT_ID,
                    provider_kinds=WEB,
                )
            )
        self.assertEqual(len(sink.records), 3)
        self.assertEqual(len({r.query_fingerprint for r in sink.records}), 3)

    async def test_arguments_are_validated_before_anything_happens(self):
        gate, sink = make_gate()
        cases = (
            ({"project_id": str(PROJECT_ID), "provider_kinds": WEB}, TypeError),
            ({"project_id": None, "provider_kinds": WEB}, TypeError),
            (
                {"project_id": PROJECT_ID, "provider_kinds": {ProviderKind.WEB}},
                TypeError,
            ),
            ({"project_id": PROJECT_ID, "provider_kinds": ["web"]}, TypeError),
            (
                {"project_id": PROJECT_ID, "provider_kinds": frozenset({"web"})},
                TypeError,
            ),
            ({"project_id": PROJECT_ID, "provider_kinds": frozenset()}, ValueError),
        )
        for kwargs, error in cases:
            with self.subTest(kwargs=repr(kwargs)), self.assertRaises(error):
                await guarded(gate.authorize("python", [], **kwargs))
            # Argument errors win over a refusal: they are checked first.
            with self.subTest(kwargs=repr(kwargs), context="unclassified"):
                with self.assertRaises(error):
                    await guarded(gate.authorize("python", ["raw"], **kwargs))
        self.assertEqual(sink.records, ())

    async def test_a_refusal_does_not_reach_the_sink(self):
        gate, sink = make_gate()
        for draft, context, reason in (
            ("python", ["raw"], Reason.UNCLASSIFIED_CONTEXT),
            ("x" * (MAX_DRAFT_CHARS + 1), [], Reason.DRAFT_TOO_LONG),
            ("\ufdfa" * 201, [], Reason.DRAFT_TOO_LONG),
            (
                "python",
                [private_source("\ufdfa" * 12_000)],  # 216,000 after NFKC
                Reason.CONTEXT_TOO_LARGE,
            ),
            ("/etc/passwd", [], Reason.EMPTY_QUERY),
            (AWS_KEY, [], Reason.EMPTY_QUERY),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(PrivacyRefusal) as caught:
                    await guarded(
                        gate.authorize(
                            draft, context, project_id=PROJECT_ID, provider_kinds=WEB
                        )
                    )
                self.assertIs(caught.exception.reason, reason)
        self.assertEqual(sink.records, ())


class RaisingNameMeta(type):
    """A metaclass whose ``__name__`` (a property here) raises."""

    @property
    def __name__(cls):
        raise RuntimeError("metaclass __name__")


class RaisingGetattributeMeta(type):
    """A metaclass that raises on every attribute read of its classes."""

    def __getattribute__(cls, name):
        raise RuntimeError("metaclass __getattribute__ " + name)


class RaisingHashMeta(type):
    """A metaclass whose classes can be neither hashed nor compared."""

    def __hash__(cls):
        raise RuntimeError("metaclass __hash__")

    def __eq__(cls, other):
        raise RuntimeError("metaclass __eq__")


def hostile_error_instances():
    """``(label, exception)`` pairs: the classes a sink can make up (adapter data)."""
    secret_name = type(f"access_token={SECRET}", (Exception,), {})
    forged_name = type("Boom\nWARNING forged log line", (Exception,), {})
    lookalike = type("RuntimeError", (Exception,), {})

    class Sub(RuntimeError):
        pass

    class RenamedClass(RuntimeError):
        __name__ = "ValueError"
        __qualname__ = f"access_token={SECRET}"
        __module__ = "builtins"

    class RaisingName(Exception, metaclass=RaisingNameMeta):
        pass

    class RaisingGetattribute(Exception, metaclass=RaisingGetattributeMeta):
        pass

    class RaisingHash(Exception, metaclass=RaisingHashMeta):
        pass

    class Both(RuntimeError, metaclass=RaisingHashMeta):
        pass

    class Group(ExceptionGroup):
        pass

    return [
        ("credential in the name", secret_name(SECRET)),
        ("newline in the name", forged_name(SECRET)),
        ("named like a builtin", lookalike(SECRET)),
        ("subclass of a builtin", Sub(SECRET)),
        ("renamed class", RenamedClass(SECRET)),
        ("raising metaclass __name__", RaisingName(SECRET)),
        ("raising metaclass __getattribute__", RaisingGetattribute(SECRET)),
        ("raising metaclass __hash__ and __eq__", RaisingHash(SECRET)),
        ("builtin subclass with raising hash", Both(SECRET)),
        ("subclass of ExceptionGroup", Group(SECRET, [ValueError(SECRET)])),
    ]


class RaisingSink:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def record(self, record):
        self.calls += 1
        raise self.error


class HangingSink:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def record(self, record):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class AuditFailureTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The gate logs each audit failure; keep the test output clean.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    async def authorize(self, gate):
        return await guarded(
            gate.authorize(
                "python asyncio", [], project_id=PROJECT_ID, provider_kinds=WEB
            )
        )

    async def refused(self, gate):
        """The ``PrivacyRefusal`` of ``authorize``; anything else fails the test.

        A hostile sink error must not stay chained to the failure (unittest would
        format its class and run the class's own hooks while reporting), so the
        test fails from outside the ``except`` block.
        """
        problem = False
        try:
            await self.authorize(gate)
        except PrivacyRefusal as refusal:
            return refusal
        except Exception:
            problem = True
        self.fail(
            "authorize raised something other than a PrivacyRefusal"
            if problem
            else "authorize did not refuse"
        )

    async def test_any_sink_exception_refuses_the_send(self):
        for error in (
            RuntimeError(SECRET),
            ValueError(SECRET),
            OSError(SECRET),
            ConnectionError(SECRET),
            KeyError(SECRET),
            TimeoutError(SECRET),
            ExceptionGroup(SECRET, [ValueError(SECRET)]),
        ):
            with self.subTest(error=type(error).__name__):
                sink = RaisingSink(error)
                gate = PrivacyGate(sink, clock=lambda: NOW)
                with self.assertRaises(PrivacyRefusal) as caught:
                    await self.authorize(gate)
                self.assertIs(caught.exception.reason, Reason.AUDIT_FAILED)
                self.assertEqual(sink.calls, 1)

    async def test_the_refusal_never_carries_the_sink_error(self):
        gate = PrivacyGate(RaisingSink(RuntimeError(SECRET)), clock=lambda: NOW)
        with self.assertRaises(PrivacyRefusal) as caught:
            await self.authorize(gate)
        error = caught.exception
        for text in (str(error), repr(error), repr(error.args)):
            self.assertNotIn(SECRET, text)
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)

    async def test_the_failure_is_logged_once_with_the_type_only(self):
        logging.disable(logging.NOTSET)
        gate = PrivacyGate(RaisingSink(RuntimeError(SECRET)), clock=lambda: NOW)
        with self.assertLogs("paw_backend.research.privacy", level="WARNING") as logs:
            with self.assertRaises(PrivacyRefusal):
                await self.authorize(gate)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelno, logging.WARNING)
        message = logs.output[0]
        self.assertIn("RuntimeError", message)
        self.assertNotIn(SECRET, message)
        self.assertNotIn("python", message)

    async def test_the_logged_type_is_a_fixed_value_the_sink_cannot_choose(self):
        # ``type(error).__name__`` is the sink's data: a credential, a newline that
        # forges the next log line, or a metaclass hook that raises. The gate logs
        # a name only for a builtin or ``paw_backend.research`` exception class
        # (the broker's ``log_type_name``) and ``adapter_error`` for all the rest.
        logging.disable(logging.NOTSET)
        for label, error in hostile_error_instances():
            with self.subTest(sink_error=label):
                gate = PrivacyGate(RaisingSink(error), clock=lambda: NOW)
                with self.assertLogs(
                    "paw_backend.research.privacy", level="WARNING"
                ) as logs:
                    refusal = await self.refused(gate)
                self.assertIs(refusal.reason, Reason.AUDIT_FAILED)
                self.assertEqual(len(logs.records), 1)
                self.assertEqual(
                    logs.records[0].getMessage(),
                    "external send audit failed: exception_type=adapter_error",
                )
                self.assertEqual(logs.output[0].count("\n"), 0)
                self.assertNotIn(SECRET, logs.output[0])
                self.assertIsNone(refusal.__cause__)
                self.assertTrue(refusal.__suppress_context__)

    async def test_the_type_of_a_builtin_or_package_exception_is_still_named(self):
        logging.disable(logging.NOTSET)
        for error, name in (
            (RuntimeError(SECRET), "RuntimeError"),
            (ConnectionRefusedError(SECRET), "ConnectionRefusedError"),
            (ExceptionGroup(SECRET, [ValueError(SECRET)]), "ExceptionGroup"),
            (TimeoutError(SECRET), "TimeoutError"),
        ):
            with self.subTest(name=name):
                gate = PrivacyGate(RaisingSink(error), clock=lambda: NOW)
                with self.assertLogs(
                    "paw_backend.research.privacy", level="WARNING"
                ) as logs:
                    with self.assertRaises(PrivacyRefusal):
                        await self.authorize(gate)
                self.assertEqual(
                    logs.records[0].getMessage(),
                    f"external send audit failed: exception_type={name}",
                )

    async def test_a_sink_that_is_too_slow_is_logged_as_a_timeout(self):
        logging.disable(logging.NOTSET)
        gate = PrivacyGate(HangingSink(), clock=lambda: NOW, audit_timeout_seconds=0.2)
        with self.assertLogs("paw_backend.research.privacy", level="WARNING") as logs:
            with self.assertRaises(PrivacyRefusal) as caught:
                await self.authorize(gate)
        self.assertIs(caught.exception.reason, Reason.AUDIT_FAILED)
        self.assertEqual(
            logs.records[0].getMessage(),
            "external send audit failed: exception_type=TimeoutError",
        )

    async def test_a_sink_that_is_too_slow_refuses_the_send_and_is_cancelled(self):
        sink = HangingSink()
        gate = PrivacyGate(sink, clock=lambda: NOW, audit_timeout_seconds=0.2)
        with self.assertRaises(PrivacyRefusal) as caught:
            await self.authorize(gate)
        self.assertIs(caught.exception.reason, Reason.AUDIT_FAILED)
        self.assertTrue(sink.cancelled)

    async def test_a_full_sink_refuses_and_keeps_the_first_record(self):
        sink = InMemoryExternalSendAudit(max_records=1)
        gate = PrivacyGate(sink, clock=lambda: NOW)
        await self.authorize(gate)
        with self.assertRaises(PrivacyRefusal) as caught:
            await self.authorize(gate)
        self.assertIs(caught.exception.reason, Reason.AUDIT_FAILED)
        self.assertEqual(len(sink.records), 1)

    async def test_cancelling_the_caller_is_never_turned_into_a_refusal(self):
        sink = HangingSink()
        gate = PrivacyGate(sink, clock=lambda: NOW, audit_timeout_seconds=30)
        task = asyncio.ensure_future(self.authorize(gate))
        await wait_for_event_or_task_error(sink.started, task)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await guarded(task)
        self.assertTrue(sink.cancelled)

    async def test_the_sink_is_called_exactly_once_and_before_the_return(self):
        events = []

        class OrderedSink:
            async def record(self, record):
                events.append("record start")
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                events.append("record end")

        gate = PrivacyGate(OrderedSink(), clock=lambda: NOW)
        await self.authorize(gate)
        events.append("returned")
        self.assertEqual(events, ["record start", "record end", "returned"])


class PreflightHookTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_query_is_replaced_and_everything_else_is_kept(self):
        gate, sink = make_gate()
        request = ResearchRequest(
            "explain  https://docs.python.org/3/library/asyncio.html v3.13.15",
            max_results=7,
            kinds=frozenset({ProviderKind.DOCS, ProviderKind.WEB}),
            time_budget_seconds=12.5,
        )
        subject = PrivacyInput([], PROJECT_ID)
        changed = await guarded(
            gate.preflight(request, frozenset({ProviderKind.DOCS}), subject)
        )
        self.assertEqual(changed.query, "explain docs.python.org v3.13")
        self.assertEqual(changed.max_results, 7)
        self.assertEqual(changed.kinds, request.kinds)
        self.assertEqual(changed.time_budget_seconds, 12.5)
        self.assertEqual(
            request.query,
            "explain  https://docs.python.org/3/library/asyncio.html v3.13.15",
        )
        (recorded,) = sink.records
        self.assertEqual(recorded.provider_kinds, (ProviderKind.DOCS,))
        self.assertEqual(recorded.project_id, PROJECT_ID)

    async def test_a_missing_or_foreign_subject_is_refused(self):
        gate, sink = make_gate()
        request = ResearchRequest("python asyncio")
        for subject in (None, "context", [], {"context": []}, (), object(), 5):
            with self.subTest(subject=repr(subject)):
                with self.assertRaises(PrivacyRefusal) as caught:
                    await guarded(gate.preflight(request, WEB, subject))
                self.assertIs(caught.exception.reason, Reason.UNCLASSIFIED_CONTEXT)
        self.assertEqual(sink.records, ())

    async def test_the_request_must_be_a_research_request(self):
        gate, _ = make_gate()
        for request in ("python", None, {"query": "python"}):
            with self.subTest(request=repr(request)), self.assertRaises(TypeError):
                await guarded(
                    gate.preflight(request, WEB, PrivacyInput([], PROJECT_ID))
                )

    async def test_unclassified_context_inside_the_subject_is_refused(self):
        gate, sink = make_gate()
        subject = PrivacyInput(["raw text"], PROJECT_ID)
        with self.assertRaises(PrivacyRefusal) as caught:
            await guarded(gate.preflight(ResearchRequest("python"), WEB, subject))
        self.assertIs(caught.exception.reason, Reason.UNCLASSIFIED_CONTEXT)
        self.assertEqual(sink.records, ())


if __name__ == "__main__":
    unittest.main()
