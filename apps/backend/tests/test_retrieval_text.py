"""Words, character pairs and the keyword query text (pure, no database)."""

import unittest

from paw_backend.memory import fulltext
from paw_backend.memory.fulltext import (
    CJK_CLASS,
    features,
    keyword_terms,
    normalize_text,
    search_document_sql,
    tsquery_text,
)


class FeaturesTest(unittest.TestCase):
    def test_words_are_lowered_and_kept_whole(self):
        self.assertEqual(
            features("Deploy the Backend v2"), ("deploy", "the", "backend", "v2")
        )

    def test_japanese_runs_become_overlapping_pairs(self):
        self.assertEqual(features("検索する"), ("検索", "索す", "する"))

    def test_a_single_japanese_character_is_a_feature_of_its_own(self):
        self.assertEqual(features("猫"), ("猫",))
        self.assertEqual(features("a 猫 b"), ("a", "猫", "b"))

    def test_mixed_words_split_at_the_script_change(self):
        self.assertEqual(features("Python3の設計"), ("python3", "の設", "設計"))

    def test_full_width_and_half_width_forms_are_folded_by_nfkc(self):
        self.assertEqual(features("ＡＢＣ　１２３"), ("abc", "123"))
        # Half-width Katakana becomes the ordinary Katakana before pairing.
        self.assertEqual(features("ﾃｽﾄ"), ("テス", "スト"))

    def test_punctuation_only_and_underscore_only_tokens_are_dropped(self):
        self.assertEqual(features("!!! ___ ... foo_bar"), ("foo_bar",))
        self.assertEqual(features(""), ())

    def test_the_hiragana_katakana_and_ideograph_blocks_are_the_japanese_ones(self):
        self.assertEqual(features("ぁ"), ("ぁ",))
        self.assertEqual(features("ヾ"), ("ヾ",))
        self.assertEqual(features("ー"), ("ー",))
        self.assertEqual(features("㐀"), ("㐀",))
        self.assertEqual(features("鿿"), ("鿿",))
        # Just outside: Hangul and the block after the ideographs are words.
        self.assertEqual(features("한국어"), ("한국어",))

    def test_normalisation_is_nfkc_and_lower_case(self):
        self.assertEqual(normalize_text("ＡＢＣ ｶﾀｶﾅ"), "abc カタカナ")


class KeywordTermsTest(unittest.TestCase):
    def test_terms_are_distinct_and_keep_their_first_position(self):
        self.assertEqual(keyword_terms("b a b c a"), ("b", "a", "c"))

    def test_at_most_max_query_terms_remain_in_order(self):
        text = " ".join(f"w{n}" for n in range(fulltext.MAX_QUERY_TERMS + 10))
        terms = keyword_terms(text)
        self.assertEqual(len(terms), fulltext.MAX_QUERY_TERMS)
        self.assertEqual(terms[0], "w0")
        self.assertEqual(terms[-1], f"w{fulltext.MAX_QUERY_TERMS - 1}")

    def test_a_word_longer_than_the_bound_is_dropped(self):
        long = "x" * (fulltext.MAX_TERM_CHARS + 1)
        self.assertEqual(keyword_terms(f"{long} ok"), ("ok",))
        self.assertEqual(keyword_terms("x" * fulltext.MAX_TERM_CHARS), ("x" * 64,))


class TsqueryTextTest(unittest.TestCase):
    def test_words_are_quoted_pairs_are_phrases_and_terms_are_or_ed(self):
        self.assertEqual(
            tsquery_text(keyword_terms("検索 deploy")),
            "'検' <-> '索' | 'deploy'",
        )

    def test_a_single_japanese_character_is_one_lexeme(self):
        self.assertEqual(tsquery_text(("猫",)), "'猫'")

    def test_no_terms_give_none(self):
        self.assertIsNone(tsquery_text(()))
        self.assertIsNone(tsquery_text(keyword_terms("!!! ...")))

    def test_tsquery_operators_in_the_text_never_reach_the_query(self):
        # Everything that is not a word character is a separator, so an attempt
        # to write tsquery syntax yields plain words only.
        text = "a & !b | (c) <-> d:* 'e' \\ f"
        query = tsquery_text(keyword_terms(text))
        self.assertEqual(query, "'a' | 'b' | 'c' | 'd' | 'e' | 'f'")

    def test_a_term_with_a_non_word_character_is_dropped_not_quoted(self):
        self.assertEqual(tsquery_text(("ok", "a'b", "c d")), "'ok'")


class SearchDocumentSqlTest(unittest.TestCase):
    def test_the_default_columns_are_title_and_content(self):
        sql = search_document_sql()
        self.assertIn("(title || ' '::text) || content", sql)
        self.assertIn("'simple'::regconfig", sql)
        self.assertIn(f"[{CJK_CLASS}]", sql)

    def test_columns_can_be_qualified_for_a_query(self):
        self.assertIn(
            "(mv.title || ' '::text) || mv.content",
            search_document_sql("mv.title", "mv.content"),
        )


if __name__ == "__main__":
    unittest.main()
