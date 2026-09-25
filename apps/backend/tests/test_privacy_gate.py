"""``PrivacyGate``: minimisation, refusals, the safety net and the audit.

The gate, its validation and its safety checks are implemented; the text work
is done by ``rules.py``. Tests that reach a rule fail with
``NotImplementedError`` against the stubs; the input-validation and
unclassified-context tests, which are decided before any rule runs, pass.
"""

import asyncio
import json
import logging
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
from paw_backend.research.privacy.gate import merge_spans, replace_spans
from paw_backend.research.providers import (
    KIND_ORDER,
    ProviderKind,
    ResearchRequest,
)
from paw_backend.research.providers.contract import MAX_QUERY_CHARS

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

    def test_a_secret_piece_wins_over_the_credential_rule(self):
        # The copied text is removed first, so the credential rule finds nothing.
        result = self.gate.minimize(f"use {AWS_KEY} now", [secret(AWS_KEY)])
        self.assertEqual(result.query, "use now")
        self.assertEqual(result.pieces_matched, 1)
        self.assertEqual(result.credentials_removed, 0)

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
