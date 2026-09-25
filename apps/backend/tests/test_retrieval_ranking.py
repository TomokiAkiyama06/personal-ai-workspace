"""Fusion, freshness and the structured score (pure rules, no database)."""

import unittest
from datetime import timedelta
from uuid import uuid4

from paw_backend.memory.models import (
    ConfirmationState,
    FreshnessPolicy,
    MemoryScope,
)
from paw_backend.memory.retrieval.errors import InvalidRetrievalInputError
from paw_backend.memory.retrieval.ranking import (
    DEFAULT_RANKING,
    RankingPolicy,
    final_score,
    freshness_of,
    fuse,
    relevance,
)
from paw_backend.memory.retrieval.records import Freshness, StaleReason

from .retrieval_support import T0, make_candidate

A, B, C, D = (uuid4() for _ in range(4))


class FuseTest(unittest.TestCase):
    def test_reciprocal_rank_fusion_is_normalised_by_the_best_possible_value(self):
        fused = fuse([A, B, C], [B, A, D])
        best = 2 / 61
        self.assertAlmostEqual(fused[A].fused, (1 / 61 + 1 / 62) / best)
        self.assertAlmostEqual(fused[B].fused, (1 / 62 + 1 / 61) / best)
        self.assertAlmostEqual(fused[C].fused, (1 / 63) / best)
        self.assertAlmostEqual(fused[D].fused, (1 / 63) / best)
        self.assertEqual((fused[A].keyword_rank, fused[A].vector_rank), (1, 2))
        self.assertEqual((fused[C].keyword_rank, fused[C].vector_rank), (3, None))
        self.assertEqual((fused[D].keyword_rank, fused[D].vector_rank), (None, 3))

    def test_first_in_both_lists_is_exactly_one(self):
        self.assertAlmostEqual(fuse([A], [A])[A].fused, 1.0)

    def test_a_candidate_in_both_lists_beats_one_that_tops_a_single_list(self):
        fused = fuse([C, A], [A, B])
        self.assertGreater(fused[A].fused, fused[C].fused)
        self.assertGreater(fused[A].fused, fused[B].fused)

    def test_the_weights_scale_each_leg(self):
        policy = RankingPolicy(keyword_weight=3.0, vector_weight=1.0)
        fused = fuse([A], [B], policy)
        self.assertAlmostEqual(fused[A].fused, 3 / 4)
        self.assertAlmostEqual(fused[B].fused, 1 / 4)

    def test_a_leg_with_weight_zero_adds_nothing(self):
        policy = RankingPolicy(keyword_weight=0.0, vector_weight=1.0)
        fused = fuse([A, B], [B], policy)
        self.assertEqual(fused[A].fused, 0.0)
        self.assertAlmostEqual(fused[B].fused, 1.0)

    def test_the_first_position_counts_when_a_list_repeats_an_id(self):
        fused = fuse([A, B, A], [])
        self.assertEqual(fused[A].keyword_rank, 1)
        self.assertEqual(fused[B].keyword_rank, 2)

    def test_empty_lists_give_no_candidates(self):
        self.assertEqual(fuse([], []), {})

    def test_the_result_depends_on_the_lists_only(self):
        # Ranks come from the two lists alone, so the same lists give the same
        # values whatever else exists in the database.
        self.assertEqual(fuse([A, B], [B]), fuse([A, B], [B]))


class RelevanceTest(unittest.TestCase):
    def test_without_a_reranker_the_fused_value_is_the_relevance(self):
        self.assertEqual(relevance(0.4, None), 0.4)

    def test_the_reranker_score_is_blended_by_the_weight(self):
        self.assertAlmostEqual(relevance(0.4, 1.0), 0.3 * 0.4 + 0.7 * 1.0)
        policy = RankingPolicy(rerank_weight=0.0)
        self.assertEqual(relevance(0.4, 1.0, policy), 0.4)
        policy = RankingPolicy(rerank_weight=1.0)
        self.assertEqual(relevance(0.4, 0.25, policy), 0.25)


class FreshnessTest(unittest.TestCase):
    def check(self, expected, **fields):
        heads = fields.pop("heads", None)
        result = freshness_of(make_candidate(**fields), T0, heads)
        self.assertEqual(result, expected, fields)

    def test_permanent_expiring_and_session_only_are_fresh(self):
        for policy in (
            FreshnessPolicy.PERMANENT,
            FreshnessPolicy.EXPIRING,
            FreshnessPolicy.SESSION_ONLY,
        ):
            with self.subTest(policy):
                self.check((Freshness.FRESH, None), freshness_policy=policy)

    def test_revalidate_turns_stale_exactly_at_verified_at_plus_the_interval(self):
        interval = timedelta(days=90)
        verified = T0 - interval
        base = {
            "freshness_policy": FreshnessPolicy.REVALIDATE,
            "revalidate_after": interval,
        }
        self.check(
            (Freshness.STALE, StaleReason.REVALIDATE_DUE),
            verified_at=verified,
            **base,
        )
        self.check(
            (Freshness.FRESH, None),
            verified_at=verified + timedelta(microseconds=1),
            **base,
        )
        self.check(
            (Freshness.STALE, StaleReason.REVALIDATE_DUE),
            verified_at=verified - timedelta(days=1),
            **base,
        )

    def test_revalidate_with_a_missing_column_is_stale_not_fresh(self):
        self.check(
            (Freshness.STALE, StaleReason.REVALIDATE_DUE),
            freshness_policy=FreshnessPolicy.REVALIDATE,
            verified_at=None,
            revalidate_after=timedelta(days=1),
        )

    def test_something_that_marked_the_memory_stale_wins_over_every_policy(self):
        for policy in FreshnessPolicy:
            with self.subTest(policy):
                self.check(
                    (Freshness.STALE, StaleReason.MARKED_STALE),
                    freshness_policy=policy,
                    stale_since=T0,
                )

    def test_repo_commit_is_stale_only_against_a_known_different_head(self):
        repo = uuid4()
        base = {
            "freshness_policy": FreshnessPolicy.REPO_COMMIT,
            "repo_id": repo,
            "scope": MemoryScope.REPO,
            "commit_sha": "a" * 40,
        }
        self.check((Freshness.FRESH, None), heads={repo: "a" * 40}, **base)
        self.check(
            (Freshness.STALE, StaleReason.REPO_COMMIT_CHANGED),
            heads={repo: "b" * 40},
            **base,
        )
        # No evidence: no head given, or none for this repository.
        self.check((Freshness.FRESH, None), heads=None, **base)
        self.check((Freshness.FRESH, None), heads={uuid4(): "b" * 40}, **base)

    def test_a_repo_commit_memory_without_a_repo_is_not_judged(self):
        self.check(
            (Freshness.FRESH, None),
            freshness_policy=FreshnessPolicy.REPO_COMMIT,
            commit_sha="a" * 40,
            heads={uuid4(): "b" * 40},
        )


class FinalScoreTest(unittest.TestCase):
    def score(self, value=1.0, freshness=Freshness.FRESH, **fields):
        return final_score(value, make_candidate(**fields), freshness)

    def test_a_neutral_user_memory_keeps_its_relevance_but_for_the_scope_step(self):
        self.assertAlmostEqual(self.score(0.8), 0.8 * 1.02)

    def test_confirmed_outranks_inferred_outranks_observed(self):
        confirmed = self.score(confirmation_state=ConfirmationState.CONFIRMED)
        inferred = self.score(confirmation_state=ConfirmationState.INFERRED)
        observed = self.score(confirmation_state=ConfirmationState.OBSERVED)
        self.assertGreater(confirmed, inferred)
        self.assertGreater(inferred, observed)
        self.assertAlmostEqual(inferred / confirmed, 0.85)
        self.assertAlmostEqual(observed / confirmed, 0.7)

    def test_a_stale_candidate_is_halved(self):
        fresh = self.score()
        stale = self.score(freshness=Freshness.STALE)
        self.assertAlmostEqual(stale / fresh, 0.5)

    def test_importance_moves_the_score_within_the_span(self):
        low, neutral, high = (self.score(importance=n) for n in (0, 50, 100))
        self.assertAlmostEqual(low / neutral, 0.8)
        self.assertAlmostEqual(high / neutral, 1.2)

    def test_pinned_is_a_boost(self):
        self.assertAlmostEqual(self.score(pinned=True) / self.score(), 1.1)

    def test_more_specific_scopes_score_higher(self):
        scopes = [
            MemoryScope.SHARED,
            MemoryScope.USER,
            MemoryScope.PROJECT_GROUP,
            MemoryScope.PROJECT,
            MemoryScope.REPO,
        ]
        scores = [self.score(scope=scope) for scope in scopes]
        self.assertEqual(scores, sorted(scores))
        self.assertEqual(len(set(scores)), 5)
        self.assertAlmostEqual(scores[-1] / scores[0], 1.08)

    def test_no_relevance_means_no_score_whatever_the_metadata(self):
        best = self.score(
            0.0,
            importance=100,
            pinned=True,
            scope=MemoryScope.REPO,
            confirmation_state=ConfirmationState.CONFIRMED,
        )
        self.assertEqual(best, 0.0)

    def test_importance_cannot_lift_a_weak_match_above_a_strong_one(self):
        weak_but_important = self.score(0.2, importance=100, pinned=True)
        strong = self.score(0.9, importance=0)
        self.assertLess(weak_but_important, strong)


class PolicyValidationTest(unittest.TestCase):
    def test_the_default_policy_is_valid_and_orders_confirmation(self):
        self.assertEqual(DEFAULT_RANKING.rrf_k, 60)
        self.assertGreater(
            DEFAULT_RANKING.confirmed_factor, DEFAULT_RANKING.inferred_factor
        )
        self.assertGreater(
            DEFAULT_RANKING.inferred_factor, DEFAULT_RANKING.observed_factor
        )

    def test_bad_values_are_refused_with_the_typed_error(self):
        bad = [
            {"rrf_k": 0},
            {"rrf_k": 1001},
            {"rrf_k": True},
            {"rrf_k": 60.0},
            {"keyword_weight": -0.1},
            {"keyword_weight": 10.1},
            {"keyword_weight": float("nan")},
            {"keyword_weight": float("inf")},
            {"keyword_weight": "1"},
            {"keyword_weight": 0.0, "vector_weight": 0.0},
            {"rerank_weight": 1.5},
            {"rerank_weight": -0.5},
            {"confirmed_factor": 0.0},
            {"confirmed_factor": 2.5},
            {"stale_factor": 0.0},
            {"stale_factor": 1.5},
            {"importance_span": 1.0},
            {"importance_span": -0.1},
            {"pinned_factor": 0.9},
            {"pinned_factor": 2.1},
            {"scope_step": 0.3},
            {"near_duplicate_similarity": 0.0},
            {"near_duplicate_similarity": 1.1},
            # Confirmed must never rank below inferred or observed.
            {"confirmed_factor": 0.5, "inferred_factor": 0.85},
            {"inferred_factor": 0.5, "observed_factor": 0.7},
        ]
        for values in bad:
            with self.subTest(values), self.assertRaises(InvalidRetrievalInputError):
                RankingPolicy(**values)

    def test_boundary_values_are_accepted(self):
        RankingPolicy(
            rrf_k=1, keyword_weight=0.0, vector_weight=10.0, rerank_weight=1.0
        )
        RankingPolicy(stale_factor=1.0, pinned_factor=1.0, importance_span=0.0)
        RankingPolicy(near_duplicate_similarity=1.0, scope_step=0.0)
        RankingPolicy(confirmed_factor=2.0, inferred_factor=2.0, observed_factor=2.0)


if __name__ == "__main__":
    unittest.main()
