"""``rules.py``: the text primitives (normalisation, copy detection, cutting, hash).

Every test calls a stubbed function, so against the stubs each one fails with
``NotImplementedError``. Only behaviour written in the docstrings is asserted.
"""

import hashlib
import random
import re
import time
import unittest

from paw_backend.research.privacy import rules

LOOSE_DEADLINE_SECONDS = 20.0


class NormalizeTextTest(unittest.TestCase):
    def test_docstring_examples(self):
        for text, expected in (
            ("  Hello \n\t World  ", "Hello World"),
            ("pass​word", "password"),
            ("a\x00b", "ab"),
            ("ｆｕｌｌ　ｗｉｄｔｈ", "full width"),
            ("﻿", ""),
            ("ﬁle", "file"),
        ):
            with self.subTest(text=text):
                self.assertEqual(rules.normalize_text(text), expected)

    def test_empty_and_blank_texts(self):
        for text in ("", " ", "\n\n", "\t \r\n", " ", "　　", "​​"):
            with self.subTest(text=text):
                self.assertEqual(rules.normalize_text(text), "")

    def test_case_is_kept(self):
        self.assertEqual(
            rules.normalize_text("Python ASYNCIO Timeout"), "Python ASYNCIO Timeout"
        )

    def test_every_kind_of_whitespace_becomes_one_space(self):
        for separator in (
            " ",
            "\t",
            "\n",
            "\r\n",
            "\x0b",
            "\x0c",
            "\x1c",
            "\x1f",
            "\x85",
            " ",
            " ",
            " ",
            " ",
            "　",
        ):
            with self.subTest(separator=repr(separator)):
                self.assertEqual(rules.normalize_text(f"a{separator}b"), "a b")
                self.assertEqual(rules.normalize_text(f"a{separator * 3}b"), "a b")

    def test_control_characters_that_are_not_space_are_dropped(self):
        self.assertEqual(rules.normalize_text("a\x00b\x07c\x1bd\x7fe"), "abcde")

    def test_format_characters_are_dropped_not_turned_into_spaces(self):
        for char in ("​", "‌", "‍", "⁠", "﻿", "­", "‮"):
            with self.subTest(char=repr(char)):
                self.assertEqual(rules.normalize_text(f"co{char}op"), "coop")

    def test_nfkc_folds_compatibility_forms(self):
        for text, expected in (
            ("ﬁ", "fi"),
            ("①②", "12"),
            ("ｶﾀｶﾅ", "カタカナ"),
            ("Ａｂｃ", "Abc"),
            ("x²", "x2"),
            ("é", "é"),
        ):
            with self.subTest(text=text):
                self.assertEqual(rules.normalize_text(text), expected)

    def test_other_text_is_untouched(self):
        for text in ("検索クエリ", "über café", "a-b_c.d/e", "emoji 🙂 ok", "Ünï"):
            with self.subTest(text=text):
                self.assertEqual(rules.normalize_text(text), text)

    def test_the_result_is_idempotent(self):
        for text in (
            "  a​  b\n",
            "ｆｕｌｌ　width",
            "\x00\x01 x \x02",
            "already normal",
            "",
            "ﬁ ﬁ",
        ):
            with self.subTest(text=text):
                once = rules.normalize_text(text)
                self.assertEqual(rules.normalize_text(once), once)
                self.assertEqual(once, " ".join(once.split()))

    def test_a_long_text_is_handled(self):
        text = "word ​" * 20_000
        self.assertEqual(rules.normalize_text(text), " ".join(["word"] * 20_000))


class FoldForMatchTest(unittest.TestCase):
    def test_docstring_examples(self):
        for text, expected in (
            ("ABC def", "abc def"),
            ("ẞ", "ß"),
            ("İ", "İ"),
            ("", ""),
        ):
            with self.subTest(text=text):
                self.assertEqual(rules.fold_for_match(text), expected)

    def test_the_length_never_changes(self):
        for text in (
            "İstanbul",
            "ǅ",
            "ẞtraße",
            "ΣΑΣ",
            "MIXED case 123 !?",
            "検索 ＡＢＣ",
            "ÀÉÎÕÜ",
            "İİİ",
        ):
            with self.subTest(text=text):
                self.assertEqual(len(rules.fold_for_match(text)), len(text))

    def test_each_character_is_folded_on_its_own(self):
        # ``"ΣΑΣ".lower()`` would end in a final sigma ("σας"); one character at a
        # time gives the plain sigma every time.
        self.assertEqual(rules.fold_for_match("ΣΑΣ"), "σασ")
        self.assertEqual(rules.fold_for_match("İstanbul"), "İstanbul")
        self.assertEqual(rules.fold_for_match("ǅ"), "ǆ")

    def test_uncased_text_is_unchanged(self):
        for text in ("検索クエリ", "12345 !?", "　"):
            with self.subTest(text=text):
                self.assertEqual(rules.fold_for_match(text), text)


class FindCopiedSpansTest(unittest.TestCase):
    def test_docstring_examples(self):
        find = rules.find_copied_spans
        self.assertEqual(find("abcdef", "xxbcdexx", window=3), ((1, 5),))
        self.assertEqual(find("ABC", "abc", window=3), ((0, 3),))
        self.assertEqual(find("abXcd", "abYcd", window=2), ((0, 2), (3, 5)))
        self.assertEqual(find("abc", "abc", window=4), ())
        self.assertEqual(find("abc", "xyz", window=1), ())

    def test_the_whole_text_can_be_a_copy(self):
        self.assertEqual(
            rules.find_copied_spans("hello world", "hello world", window=11), ((0, 11),)
        )
        self.assertEqual(
            rules.find_copied_spans("hello", "say hello now", window=5), ((0, 5),)
        )

    def test_a_window_of_one_marks_every_shared_character(self):
        self.assertEqual(
            rules.find_copied_spans("abcabc", "cxb", window=1),
            ((1, 3), (4, 6)),
        )

    def test_a_run_shorter_than_the_window_is_not_a_copy(self):
        self.assertEqual(rules.find_copied_spans("xxabxx", "abcd", window=3), ())
        self.assertEqual(
            rules.find_copied_spans("xxabcxx", "abcd", window=3), ((2, 5),)
        )

    def test_runs_that_touch_are_one_span(self):
        # "abc" and "def" are found separately but touch in the text: one span.
        self.assertEqual(
            rules.find_copied_spans("abcdef", "abc def", window=3), ((0, 6),)
        )
        self.assertEqual(
            rules.find_copied_spans("abcdef", "abcdef", window=3), ((0, 6),)
        )
        # One uncovered character between two runs keeps them apart.
        self.assertEqual(
            rules.find_copied_spans("abcXdef", "abc def", window=3), ((0, 3), (4, 7))
        )
        # Overlapping windows extend a run: "abcd" is covered by abc and bcd.
        self.assertEqual(rules.find_copied_spans("zabcdz", "abcd", window=3), ((1, 5),))

    def test_every_occurrence_is_found(self):
        self.assertEqual(
            rules.find_copied_spans("--secret--secret--", "secret", window=6),
            ((2, 8), (10, 16)),
        )

    def test_matching_ignores_case_in_both_texts(self):
        self.assertEqual(
            rules.find_copied_spans("Hello WORLD", "hello world", window=8), ((0, 11),)
        )
        self.assertEqual(
            rules.find_copied_spans("hello world", "HELLO World", window=8), ((0, 11),)
        )

    def test_positions_are_positions_in_the_original_text(self):
        # "İ" is not shortened or lengthened by the fold, so later positions stay put.
        text = "İİ secret İİ"
        self.assertEqual(rules.find_copied_spans(text, "secret", window=6), ((3, 9),))

    def test_other_scripts(self):
        source = "顧客データベースの接続情報は社外秘です"
        text = "質問 顧客データベースの接続情報は社外秘です とは"
        start = text.index("顧客")
        self.assertEqual(
            rules.find_copied_spans(text, source, window=8),
            ((start, start + len(source)),),
        )

    def test_nothing_is_found_for_empty_inputs_or_a_long_window(self):
        find = rules.find_copied_spans
        self.assertEqual(find("", "abc", window=1), ())
        self.assertEqual(find("abc", "", window=1), ())
        self.assertEqual(find("ab", "abcdef", window=3), ())
        self.assertEqual(find("abcdef", "ab", window=3), ())

    def test_the_result_is_a_tuple_of_int_pairs(self):
        result = rules.find_copied_spans("xxabcxxabcxx", "abc", window=3)
        self.assertEqual(result, ((2, 5), (7, 10)))
        self.assertIsInstance(result, tuple)
        for span in result:
            self.assertIsInstance(span, tuple)
            self.assertEqual(len(span), 2)

    def test_a_window_below_one_is_a_value_error_without_the_text(self):
        for window in (0, -1):
            with self.subTest(window=window):
                with self.assertRaises(ValueError) as caught:
                    rules.find_copied_spans(
                        "PRIVATE-TEXT", "PRIVATE-TEXT", window=window
                    )
                self.assertNotIn("PRIVATE", str(caught.exception))

    def test_the_cost_is_linear_in_the_size_of_the_source(self):
        generator = random.Random(53)
        source = "".join(
            generator.choice("abcdefghijklmnopqrstuvwxyz ") for _ in range(200_000)
        )
        text = "".join(
            generator.choice("abcdefghijklmnopqrstuvwxyz ") for _ in range(2_000)
        )
        text = text[:1_000] + source[50_000:50_040] + text[1_000:]
        started = time.monotonic()
        spans = rules.find_copied_spans(text, source, window=16)
        elapsed = time.monotonic() - started
        self.assertEqual(spans, ((1_000, 1_040),))
        self.assertLess(elapsed, LOOSE_DEADLINE_SECONDS)


class TruncateQueryTest(unittest.TestCase):
    def test_docstring_examples(self):
        for text, limit, expected in (
            ("aaaa bbbb cccc", 9, "aaaa bbbb"),
            ("aaaa bbbb cccc", 7, "aaaa"),
            ("aaaa bbbb", 3, "aaa"),
            ("aaaa bbbb", 4, "aaaa"),
            ("aaaa", 4, "aaaa"),
            ("ab cd", 100, "ab cd"),
        ):
            with self.subTest(text=text, limit=limit):
                self.assertEqual(rules.truncate_query(text, limit), expected)

    def test_a_text_that_fits_is_returned_as_it_is(self):
        self.assertEqual(rules.truncate_query("abc def", 7), "abc def")
        self.assertEqual(rules.truncate_query("", 5), "")
        self.assertEqual(rules.truncate_query("x", 1), "x")

    def test_one_character_over_the_limit(self):
        self.assertEqual(rules.truncate_query("abc def", 6), "abc")
        self.assertEqual(rules.truncate_query("abcdef g", 7), "abcdef")
        self.assertEqual(rules.truncate_query("abcdefg", 6), "abcdef")

    def test_a_cut_at_a_space_boundary_and_inside_a_word(self):
        # limit 5: head "aaaa " ends with a space, the next character is a letter.
        self.assertEqual(rules.truncate_query("aaaa bbbb", 5), "aaaa")
        # limit 4 of "aaaa bbbb": the next character is a space.
        self.assertEqual(rules.truncate_query("aaaa bbbb", 4), "aaaa")
        # limit 6: head "aaaa b", the next character is a letter: back to the space.
        self.assertEqual(rules.truncate_query("aaaa bbbb", 6), "aaaa")

    def test_a_single_long_word_is_cut_hard(self):
        self.assertEqual(rules.truncate_query("abcdefghij", 4), "abcd")
        self.assertEqual(rules.truncate_query("abcdefghij", 1), "a")

    def test_the_result_is_a_bounded_word_prefix(self):
        text = " ".join(f"w{number}" for number in range(200))
        for limit in (1, 2, 3, 10, 50, 99, 256):
            with self.subTest(limit=limit):
                result = rules.truncate_query(text, limit)
                self.assertLessEqual(len(result), limit)
                self.assertTrue(text.startswith(result))
                self.assertFalse(result.endswith(" "))
                if limit >= 2:  # every word has 2 to 4 characters here
                    # Whole words only.
                    next_char = text[len(result) : len(result) + 1]
                    self.assertIn(next_char, (" ", ""))

    def test_the_real_limit(self):
        text = " ".join(["abcde"] * 50)
        self.assertEqual(len(text), 299)
        result = rules.truncate_query(text, 256)
        self.assertEqual(result, " ".join(["abcde"] * 42))
        self.assertEqual(len(result), 251)

    def test_non_ascii_text_counts_characters(self):
        text = "検索 " * 100
        text = text.strip()
        result = rules.truncate_query(text, 10)
        self.assertEqual(result, "検索 検索 検索")
        self.assertEqual(rules.truncate_query("検索クエリ", 3), "検索ク")


class QueryFingerprintTest(unittest.TestCase):
    def test_known_vectors(self):
        self.assertEqual(
            rules.query_fingerprint("abc"),
            "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )
        self.assertEqual(
            rules.query_fingerprint(""),
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_it_hashes_the_utf8_bytes_exactly(self):
        for query in ("python asyncio", "検索クエリ 🙂", " padded ", "Case", "case"):
            with self.subTest(query=query):
                expected = "sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest()
                self.assertEqual(rules.query_fingerprint(query), expected)

    def test_shape_and_distinctness(self):
        first = rules.query_fingerprint("one")
        second = rules.query_fingerprint("two")
        self.assertRegex(first, r"\Asha256:[0-9a-f]{64}\Z")
        self.assertNotEqual(first, second)
        self.assertNotEqual(
            rules.query_fingerprint("Abc"), rules.query_fingerprint("abc")
        )
        self.assertEqual(first, rules.query_fingerprint("one"))
        self.assertIsNotNone(re.fullmatch(r"sha256:[0-9a-f]{64}", first))


if __name__ == "__main__":
    unittest.main()
