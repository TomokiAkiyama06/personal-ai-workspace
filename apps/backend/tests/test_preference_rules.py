"""The pure decisions of the Inferred Preference flow (PAW-044, Decision 0081).

No database: evidence, the scope recommendation, when to ask, and the buttons.
"""

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from paw_backend.memory.journal.domain import ItemResult
from paw_backend.memory.preferences.rules import (
    MIN_REPEATS,
    Consistency,
    LanguageStrength,
    Observation,
    Recommendation,
    RiskLevel,
    TargetScope,
    evidence,
    is_ready,
    language_strength,
    options,
    overall_strength,
    recommend_scope,
)

T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
PROJECT_A, PROJECT_B = uuid4(), uuid4()
REPO_1, REPO_2 = uuid4(), uuid4()


def seen(
    project=None,
    repo=None,
    *,
    minutes=0,
    result=ItemResult.CREATED,
    strength=LanguageStrength.NEUTRAL,
):
    return Observation(
        entry_id=uuid4(),
        project_id=project,
        repo_id=repo,
        recorded_at=T0 + timedelta(minutes=minutes),
        result=result,
        strength=strength,
    )


class LanguageStrengthTest(unittest.TestCase):
    def test_the_requirements_words(self):
        self.assertIs(language_strength("今回はtabで"), LanguageStrength.ONCE)
        self.assertIs(language_strength("今後はtabで"), LanguageStrength.STANDING)
        self.assertIs(language_strength("基本的にtabにして"), LanguageStrength.STANDING)
        self.assertIs(language_strength("tabにして"), LanguageStrength.NEUTRAL)
        self.assertIs(language_strength(None), LanguageStrength.NEUTRAL)

    def test_english_words_need_their_boundaries(self):
        self.assertIs(
            language_strength("From now on use tabs"), LanguageStrength.STANDING
        )
        self.assertIs(language_strength("just this time"), LanguageStrength.ONCE)
        # "always" inside another word is not the word.
        self.assertIs(language_strength("alwaysx"), LanguageStrength.NEUTRAL)

    def test_full_width_text_is_folded(self):
        self.assertIs(
            language_strength("ＡＬＷＡＹＳ use tabs"), LanguageStrength.STANDING
        )

    def test_standing_wins_over_once(self):
        self.assertIs(language_strength("今回も今後も"), LanguageStrength.STANDING)

    def test_overall(self):
        once, neutral, standing = (
            LanguageStrength.ONCE,
            LanguageStrength.NEUTRAL,
            LanguageStrength.STANDING,
        )
        self.assertIs(overall_strength([once, standing]), standing)
        self.assertIs(overall_strength([once, once]), once)
        self.assertIs(overall_strength([once, neutral]), neutral)
        self.assertIs(overall_strength([]), neutral)


class RecommendScopeTest(unittest.TestCase):
    """REQUIREMENTS.md: one repo -> Repo, one project -> Project, several -> User."""

    def test_repeated_in_one_repository_is_a_repo_preference(self):
        found = recommend_scope(
            [seen(PROJECT_A, REPO_1), seen(PROJECT_A, REPO_1, minutes=1)]
        )
        self.assertEqual(found, Recommendation(TargetScope.REPO, PROJECT_A, REPO_1))

    def test_several_repositories_of_one_project_is_a_project_preference(self):
        found = recommend_scope([seen(PROJECT_A, REPO_1), seen(PROJECT_A, REPO_2)])
        self.assertEqual(found, Recommendation(TargetScope.PROJECT, PROJECT_A))

    def test_a_project_conversation_without_a_repository_is_the_project(self):
        found = recommend_scope([seen(PROJECT_A, REPO_1), seen(PROJECT_A)])
        self.assertEqual(found, Recommendation(TargetScope.PROJECT, PROJECT_A))

    def test_several_projects_is_a_user_preference(self):
        found = recommend_scope([seen(PROJECT_A, REPO_1), seen(PROJECT_B, REPO_2)])
        self.assertEqual(found, Recommendation(TargetScope.USER))

    def test_outside_any_project_is_a_user_preference(self):
        self.assertEqual(
            recommend_scope([seen(PROJECT_A, REPO_1), seen()]),
            Recommendation(TargetScope.USER),
        )
        self.assertEqual(recommend_scope([]), Recommendation(TargetScope.USER))


class EvidenceTest(unittest.TestCase):
    def test_the_five_factors(self):
        observations = [
            seen(PROJECT_A, REPO_1, result=ItemResult.CREATED),
            seen(PROJECT_A, REPO_2, minutes=3, result=ItemResult.DUPLICATE),
            seen(minutes=1, strength=LanguageStrength.STANDING),
        ]
        found = evidence(observations, texts=("indent_style", "use tabs"))
        self.assertEqual(found.frequency, 3)
        self.assertEqual((found.project_count, found.repo_count), (1, 2))
        self.assertEqual(found.outside_projects, 1)
        self.assertEqual(found.last_observed_at, T0 + timedelta(minutes=3))
        self.assertIs(found.language_strength, LanguageStrength.STANDING)
        self.assertIs(found.consistency, Consistency.CONSISTENT)
        self.assertIs(found.risk_level, RiskLevel.LOW)

    def test_one_entry_counts_once(self):
        entry = seen()
        self.assertEqual(evidence([entry, entry], texts=()).frequency, 1)

    def test_a_revised_candidate_changed_and_a_contradiction_conflicts(self):
        changed = evidence([seen(), seen(result=ItemResult.UPDATED)], texts=())
        self.assertIs(changed.consistency, Consistency.CHANGED)
        held = evidence([seen(result=ItemResult.HELD_CONFIRMED)], texts=())
        self.assertIs(held.consistency, Consistency.CONFLICTING)
        related = evidence([seen()], texts=(), conflicting=True)
        self.assertIs(related.consistency, Consistency.CONFLICTING)

    def test_risk_comes_from_the_text_or_a_held_high_risk_observation(self):
        self.assertIs(
            evidence(
                [seen()], texts=("merge_policy", "merge without asking")
            ).risk_level,
            RiskLevel.HIGH,
        )
        self.assertIs(
            evidence([seen()], texts=("x", "mainへのマージ")).risk_level, RiskLevel.HIGH
        )
        self.assertIs(
            evidence([seen(result=ItemResult.HELD_HIGH_RISK)], texts=("x",)).risk_level,
            RiskLevel.HIGH,
        )


class ReadyTest(unittest.TestCase):
    def facts(self, observations, **kw):
        return evidence(observations, texts=("indent", "tabs"), **kw)

    def test_repeated_enough(self):
        self.assertFalse(is_ready(self.facts([seen() for _ in range(MIN_REPEATS - 1)])))
        self.assertTrue(is_ready(self.facts([seen() for _ in range(MIN_REPEATS)])))

    def test_one_standing_statement_is_enough(self):
        self.assertTrue(
            is_ready(self.facts([seen(strength=LanguageStrength.STANDING)]))
        )

    def test_only_this_time_never_asks(self):
        once = [seen(strength=LanguageStrength.ONCE) for _ in range(MIN_REPEATS + 2)]
        self.assertFalse(is_ready(self.facts(once)))

    def test_a_contradiction_is_shown_not_asked(self):
        many = [seen() for _ in range(MIN_REPEATS)]
        self.assertFalse(is_ready(self.facts(many, conflicting=True)))

    def test_a_high_risk_candidate_follows_the_same_rule(self):
        risky = evidence([seen(result=ItemResult.HELD_HIGH_RISK)], texts=("merge",))
        self.assertFalse(is_ready(risky))


class OptionsTest(unittest.TestCase):
    def test_a_repo_recommendation_offers_all_three_narrowest_first(self):
        observations = [seen(PROJECT_A, REPO_1), seen(PROJECT_A, REPO_1, minutes=1)]
        found = options(observations, recommend_scope(observations))
        self.assertEqual(
            [(o.scope, o.project_id, o.repo_id, o.recommended) for o in found],
            [
                (TargetScope.REPO, PROJECT_A, REPO_1, True),
                (TargetScope.PROJECT, PROJECT_A, None, False),
                (TargetScope.USER, None, None, False),
            ],
        )

    def test_a_user_recommendation_offers_the_most_observed_project_and_repo(self):
        observations = [
            seen(PROJECT_A, REPO_1),
            seen(PROJECT_B, REPO_2, minutes=1),
            seen(PROJECT_B, REPO_2, minutes=2),
        ]
        found = options(observations, recommend_scope(observations))
        self.assertEqual(
            [(o.scope, o.project_id, o.repo_id, o.recommended) for o in found],
            [
                (TargetScope.REPO, PROJECT_B, REPO_2, False),
                (TargetScope.PROJECT, PROJECT_B, None, False),
                (TargetScope.USER, None, None, True),
            ],
        )

    def test_a_tie_goes_to_the_latest(self):
        observations = [seen(PROJECT_A, REPO_1), seen(PROJECT_B, REPO_2, minutes=5)]
        found = options(observations, recommend_scope(observations))
        self.assertEqual(found[0].repo_id, REPO_2)

    def test_never_observed_in_a_project_offers_only_the_user(self):
        found = options([seen()], recommend_scope([seen()]))
        self.assertEqual([o.scope for o in found], [TargetScope.USER])
        self.assertTrue(found[0].recommended)


if __name__ == "__main__":
    unittest.main()
