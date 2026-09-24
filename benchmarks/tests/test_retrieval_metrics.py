"""Tests for the pure retrieval ranking metrics."""

import math
import unittest

from benchmarks.retrieval_metrics import (
    mean,
    misselection_rate,
    ndcg_at_k,
    permission_leakage_count,
    recall_at_k,
    reciprocal_rank,
)

RANKED = ["a", "b", "c", "d"]


class RecallAtKTest(unittest.TestCase):
    def test_partial_hit_depends_on_k(self):
        self.assertAlmostEqual(recall_at_k(RANKED, {"a", "c"}, 1), 0.5)
        self.assertAlmostEqual(recall_at_k(RANKED, {"a", "c"}, 2), 0.5)
        self.assertAlmostEqual(recall_at_k(RANKED, {"a", "c"}, 3), 1.0)

    def test_no_hit_and_k_larger_than_list(self):
        self.assertEqual(recall_at_k(RANKED, {"z"}, 4), 0.0)
        self.assertEqual(recall_at_k(RANKED, {"d"}, 100), 1.0)

    def test_relevant_ids_missing_from_ranking_lower_recall(self):
        self.assertAlmostEqual(recall_at_k(RANKED, {"a", "z"}, 4), 0.5)


class ReciprocalRankTest(unittest.TestCase):
    def test_first_relevant_rank_decides_the_value(self):
        self.assertEqual(reciprocal_rank(RANKED, {"a"}), 1.0)
        self.assertAlmostEqual(reciprocal_rank(RANKED, {"c"}), 1 / 3)
        self.assertAlmostEqual(reciprocal_rank(RANKED, {"b", "d"}), 1 / 2)

    def test_no_relevant_result_is_zero(self):
        self.assertEqual(reciprocal_rank(RANKED, {"z"}), 0.0)
        self.assertEqual(reciprocal_rank([], {"z"}), 0.0)


class NdcgAtKTest(unittest.TestCase):
    def test_perfect_ranking_is_one(self):
        self.assertAlmostEqual(ndcg_at_k(["a", "b", "x"], {"a", "b"}, 3), 1.0)

    def test_relevant_items_at_ranks_one_and_three(self):
        dcg = 1 + 1 / math.log2(4)
        ideal = 1 + 1 / math.log2(3)
        self.assertAlmostEqual(ndcg_at_k(["a", "x", "b"], {"a", "b"}, 3), dcg / ideal)

    def test_cutoff_excludes_relevant_items_beyond_k(self):
        self.assertEqual(ndcg_at_k(["x", "a"], {"a"}, 1), 0.0)

    def test_ideal_is_capped_by_k(self):
        self.assertAlmostEqual(ndcg_at_k(["a", "x", "b"], {"a", "b", "c"}, 1), 1.0)


class PermissionLeakageTest(unittest.TestCase):
    def test_counts_disallowed_ids_within_top_k(self):
        allowed = {"a", "b"}
        self.assertEqual(permission_leakage_count(["a", "b", "c"], allowed, 2), 0)
        self.assertEqual(permission_leakage_count(["a", "b", "c"], allowed, 3), 1)
        self.assertEqual(permission_leakage_count(["c", "d"], allowed, 5), 2)


class MisselectionRateTest(unittest.TestCase):
    def test_share_of_disallowed_ids_within_top_k(self):
        self.assertAlmostEqual(misselection_rate(RANKED, {"b", "d"}, 4), 0.5)
        self.assertAlmostEqual(misselection_rate(RANKED, {"b", "d"}, 2), 0.5)
        self.assertEqual(misselection_rate(RANKED, {"z"}, 4), 0.0)
        self.assertAlmostEqual(misselection_rate(RANKED, {"a"}, 4), 0.25)

    def test_empty_ranking_is_zero(self):
        self.assertEqual(misselection_rate([], {"a"}, 3), 0.0)


class MeanTest(unittest.TestCase):
    def test_arithmetic_mean(self):
        self.assertAlmostEqual(mean([1.0, 0.5, 0.0]), 0.5)

    def test_empty_values_are_rejected(self):
        with self.assertRaises(ValueError):
            mean([])


class ValidationTest(unittest.TestCase):
    def test_empty_relevant_ids_are_rejected(self):
        for metric in (
            lambda: recall_at_k(RANKED, set(), 2),
            lambda: reciprocal_rank(RANKED, set()),
            lambda: ndcg_at_k(RANKED, set(), 2),
        ):
            with self.assertRaises(ValueError):
                metric()

    def test_k_must_be_a_positive_integer(self):
        with self.assertRaises(ValueError):
            recall_at_k(RANKED, {"a"}, 0)
        with self.assertRaises(ValueError):
            misselection_rate(RANKED, {"a"}, -1)
        for bad_k in (True, "3", 2.0):
            with self.assertRaises(TypeError):
                ndcg_at_k(RANKED, {"a"}, bad_k)
            with self.assertRaises(TypeError):
                permission_leakage_count(RANKED, {"a"}, bad_k)

    def test_duplicate_ranked_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            recall_at_k(["a", "a"], {"a"}, 2)
        with self.assertRaises(ValueError):
            reciprocal_rank(["a", "b", "a"], {"a"})

    def test_ids_must_be_strings(self):
        with self.assertRaises(TypeError):
            recall_at_k(["a", 1], {"a"}, 2)
        with self.assertRaises(TypeError):
            recall_at_k(RANKED, {"a", 1}, 2)
        with self.assertRaises(TypeError):
            permission_leakage_count(RANKED, {1}, 2)


if __name__ == "__main__":
    unittest.main()
