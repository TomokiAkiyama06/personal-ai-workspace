"""The pure rules and records of Memory versioning (PAW-042). No database."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from paw_backend.memory.models import (
    ActorType,
    ConfirmationState,
    FreshnessPolicy,
    MemoryScope,
    MemoryStatus,
    RelationType,
)
from paw_backend.memory.versioning import (
    FreshnessSpec,
    InputProblem,
    InvalidMemoryInputError,
    ManualRelation,
    MemoryChanges,
    MemoryDraft,
    MemoryStateError,
    MemoryVersionView,
    RelationClassification,
    RevalidateTrigger,
    StateProblem,
    TriggerTarget,
    plan_relation,
    rules,
)

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
SHA = "a" * 40


def view(**overrides) -> MemoryVersionView:
    values = {
        "memory_id": uuid4(),
        "version_id": uuid4(),
        "version_number": 1,
        "scope": MemoryScope.USER,
        "owner_user_id": uuid4(),
        "project_id": None,
        "project_group_id": None,
        "repo_id": None,
        "memory_type": "preference",
        "title": "Title",
        "content": "Content",
        "importance": 50,
        "pinned": False,
        "status": MemoryStatus.ACTIVE,
        "confirmation_state": ConfirmationState.CONFIRMED,
        "freshness_policy": FreshnessPolicy.PERMANENT,
        "verified_at": None,
        "revalidate_after": None,
        "revalidate_triggers": (),
        "expires_at": None,
        "commit_sha": None,
        "branch": None,
        "stale_since": None,
        "actor_type": ActorType.USER,
        "actor_user_id": None,
        "change_reason": None,
        "created_at": NOW,
    }
    values.update(overrides)
    return MemoryVersionView(**values)


class InputError(unittest.TestCase):
    def assertInvalid(self, field, problem, build):
        with self.assertRaises(InvalidMemoryInputError) as caught:
            build()
        self.assertEqual(
            (caught.exception.field, caught.exception.problem), (field, problem)
        )


class FreshnessSpecTest(InputError):
    def test_each_policy_takes_only_its_own_fields(self):
        self.assertEqual(FreshnessSpec.permanent().policy, FreshnessPolicy.PERMANENT)
        spec = FreshnessSpec.revalidate(
            timedelta(days=90),
            (RevalidateTrigger.MEMBER_CHANGED, RevalidateTrigger.MEMBER_CHANGED),
        )
        self.assertEqual(spec.revalidate_triggers, (RevalidateTrigger.MEMBER_CHANGED,))
        FreshnessSpec.expiring(NOW)
        FreshnessSpec.repo_commit(SHA, "main")
        FreshnessSpec(FreshnessPolicy.SESSION_ONLY)
        cases = [
            (
                "revalidate_after",
                lambda: FreshnessSpec(
                    FreshnessPolicy.PERMANENT, revalidate_after=timedelta(days=1)
                ),
            ),
            (
                "expires_at",
                lambda: FreshnessSpec(
                    FreshnessPolicy.REVALIDATE,
                    revalidate_after=timedelta(days=1),
                    expires_at=NOW,
                ),
            ),
            (
                "commit_sha",
                lambda: FreshnessSpec(
                    FreshnessPolicy.EXPIRING, expires_at=NOW, commit_sha=SHA
                ),
            ),
            (
                "revalidate_triggers",
                lambda: FreshnessSpec(
                    FreshnessPolicy.SESSION_ONLY,
                    revalidate_triggers=(RevalidateTrigger.MODEL_CHANGED,),
                ),
            ),
            ("branch", lambda: FreshnessSpec(FreshnessPolicy.PERMANENT, branch="main")),
        ]
        for field, build in cases:
            with self.subTest(field):
                self.assertInvalid(field, InputProblem.NOT_ALLOWED, build)

    def test_required_fields_and_their_bounds(self):
        self.assertInvalid(
            "revalidate_after",
            InputProblem.REQUIRED,
            lambda: FreshnessSpec(FreshnessPolicy.REVALIDATE),
        )
        self.assertInvalid(
            "revalidate_after",
            InputProblem.OUT_OF_RANGE,
            lambda: FreshnessSpec.revalidate(timedelta(minutes=59)),
        )
        self.assertInvalid(
            "revalidate_after",
            InputProblem.OUT_OF_RANGE,
            lambda: FreshnessSpec.revalidate(timedelta(days=3651)),
        )
        FreshnessSpec.revalidate(timedelta(hours=1))
        self.assertInvalid(
            "expires_at",
            InputProblem.NAIVE_DATETIME,
            lambda: FreshnessSpec.expiring(datetime(2026, 10, 1)),
        )
        self.assertInvalid(
            "commit_sha",
            InputProblem.INVALID_FORMAT,
            lambda: FreshnessSpec.repo_commit("A" * 40),
        )
        self.assertInvalid(
            "commit_sha",
            InputProblem.INVALID_FORMAT,
            lambda: FreshnessSpec.repo_commit("a" * 41),
        )
        FreshnessSpec.repo_commit("b" * 64)

    def test_nothing_is_coerced(self):
        self.assertInvalid(
            "policy", InputProblem.WRONG_TYPE, lambda: FreshnessSpec("permanent")
        )
        self.assertInvalid(
            "revalidate_triggers",
            InputProblem.WRONG_TYPE,
            lambda: FreshnessSpec.revalidate(timedelta(days=1), ("member_changed",)),
        )
        self.assertInvalid(
            "revalidate_triggers",
            InputProblem.WRONG_TYPE,
            lambda: FreshnessSpec(
                FreshnessPolicy.REVALIDATE,
                revalidate_after=timedelta(days=1),
                revalidate_triggers="member_changed",
            ),
        )


class DraftAndChangesTest(InputError):
    def test_a_draft_is_user_or_project_scoped(self):
        MemoryDraft(MemoryScope.USER, "preference", "T", "C")
        MemoryDraft(MemoryScope.PROJECT, "rule", "T", "C", project_id=uuid4())
        for scope in (MemoryScope.SHARED, MemoryScope.REPO, MemoryScope.PROJECT_GROUP):
            with self.subTest(scope):
                self.assertInvalid(
                    "scope",
                    InputProblem.NOT_ALLOWED,
                    lambda scope=scope: MemoryDraft(scope, "rule", "T", "C"),
                )
        self.assertInvalid(
            "project_id",
            InputProblem.REQUIRED,
            lambda: MemoryDraft(MemoryScope.PROJECT, "rule", "T", "C"),
        )
        self.assertInvalid(
            "project_id",
            InputProblem.NOT_ALLOWED,
            lambda: MemoryDraft(MemoryScope.USER, "rule", "T", "C", project_id=uuid4()),
        )
        self.assertInvalid(
            "title",
            InputProblem.TOO_LONG,
            lambda: MemoryDraft(MemoryScope.USER, "rule", "x" * 201, "C"),
        )
        self.assertInvalid(
            "memory_type",
            InputProblem.INVALID_FORMAT,
            lambda: MemoryDraft(MemoryScope.USER, "Rule", "T", "C"),
        )

    def test_changes_need_at_least_one_field(self):
        self.assertInvalid("changes", InputProblem.REQUIRED, lambda: MemoryChanges())
        self.assertInvalid(
            "changes", InputProblem.REQUIRED, lambda: MemoryChanges(reason="why")
        )
        MemoryChanges(freshness=FreshnessSpec.permanent())
        self.assertInvalid(
            "importance",
            InputProblem.OUT_OF_RANGE,
            lambda: MemoryChanges(importance=101),
        )
        self.assertInvalid(
            "content", InputProblem.BLANK, lambda: MemoryChanges(content="  ")
        )

    def test_a_trigger_target_names_one_id_unless_it_is_the_workspace(self):
        TriggerTarget.workspace()
        TriggerTarget.user(uuid4())
        self.assertInvalid(
            "id", InputProblem.REQUIRED, lambda: TriggerTarget.project(None)
        )
        self.assertInvalid(
            "id", InputProblem.WRONG_TYPE, lambda: TriggerTarget.repo(str(uuid4()))
        )


class PlanRelationTest(unittest.TestCase):
    def test_each_classification_of_the_requirements(self):
        same = plan_relation(RelationClassification.SAME)
        self.assertFalse(same.writes_new)
        supersedes = plan_relation(RelationClassification.SUPERSEDES)
        self.assertEqual(supersedes.relation, RelationType.SUPERSEDES)
        self.assertTrue(supersedes.retires_older)
        self.assertFalse(supersedes.needs_confirmation)
        extends = plan_relation(RelationClassification.EXTENDS)
        self.assertEqual(extends.relation, RelationType.EXTENDS)
        self.assertFalse(extends.retires_older)
        conflicts = plan_relation(RelationClassification.CONFLICTS)
        self.assertEqual(conflicts.relation, RelationType.CONFLICTS_WITH)
        self.assertFalse(conflicts.retires_older)
        self.assertTrue(conflicts.needs_confirmation)
        unrelated = plan_relation(RelationClassification.UNRELATED)
        self.assertTrue(unrelated.writes_new)
        self.assertIsNone(unrelated.relation)

    def test_every_classification_has_a_plan_and_only_a_clear_one_retires(self):
        retiring = {c for c in RelationClassification if plan_relation(c).retires_older}
        self.assertEqual(retiring, {RelationClassification.SUPERSEDES})


class FreshnessRuleTest(InputError):
    def test_session_only_and_repo_commit_are_not_manual_long_term_memory(self):
        self.assertInvalid(
            "freshness",
            InputProblem.NOT_ALLOWED,
            lambda: rules.check_manual_freshness(
                FreshnessSpec(FreshnessPolicy.SESSION_ONLY), MemoryScope.USER, NOW
            ),
        )
        for scope in (MemoryScope.USER, MemoryScope.PROJECT):
            self.assertInvalid(
                "freshness",
                InputProblem.NOT_ALLOWED,
                lambda scope=scope: rules.check_manual_freshness(
                    FreshnessSpec.repo_commit(SHA), scope, NOW
                ),
            )
        rules.check_manual_freshness(
            FreshnessSpec.repo_commit(SHA), MemoryScope.REPO, NOW
        )

    def test_an_expiry_must_lie_ahead(self):
        for expires in (NOW, NOW - timedelta(seconds=1), NOW + timedelta(days=3651)):
            with self.subTest(expires):
                self.assertInvalid(
                    "expires_at",
                    InputProblem.OUT_OF_RANGE,
                    lambda expires=expires: rules.check_manual_freshness(
                        FreshnessSpec.expiring(expires), MemoryScope.USER, NOW
                    ),
                )
        rules.check_manual_freshness(
            FreshnessSpec.expiring(NOW + timedelta(seconds=1)), MemoryScope.USER, NOW
        )


class StateRuleTest(unittest.TestCase):
    def assertState(self, problem, call):
        with self.assertRaises(MemoryStateError) as caught:
            call()
        self.assertEqual(caught.exception.problem, problem)

    def test_changed_fields_ignores_values_that_are_already_there(self):
        current = view(title="T", importance=40)
        self.assertEqual(
            rules.changed_fields(current, MemoryChanges(title="T", importance=40)), ()
        )
        self.assertEqual(
            rules.changed_fields(
                current,
                MemoryChanges(
                    title="New", importance=40, freshness=FreshnessSpec.permanent()
                ),
            ),
            ("title",),
        )
        self.assertEqual(
            rules.changed_fields(
                current,
                MemoryChanges(freshness=FreshnessSpec.revalidate(timedelta(days=9))),
            ),
            ("freshness",),
        )

    def test_only_an_active_version_is_edited_or_revalidated(self):
        for status in (
            MemoryStatus.SUPERSEDED,
            MemoryStatus.DEPRECATED,
            MemoryStatus.HISTORY,
        ):
            with self.subTest(status):
                self.assertState(
                    StateProblem.NOT_ACTIVE,
                    lambda status=status: rules.check_active(view(status=status)),
                )
        self.assertState(
            StateProblem.NOT_REVALIDATABLE,
            lambda: rules.check_revalidatable(view()),
        )
        rules.check_revalidatable(
            view(
                freshness_policy=FreshnessPolicy.REVALIDATE,
                verified_at=NOW,
                revalidate_after=timedelta(days=1),
            )
        )

    def test_a_restore_needs_an_active_or_deprecated_current_version(self):
        current = view(version_number=3)
        rules.check_restorable(current, view(version_number=1))
        self.assertState(
            StateProblem.ALREADY_ACTIVE,
            lambda: rules.check_restorable(current, current),
        )
        deprecated = replace(current, status=MemoryStatus.DEPRECATED)
        rules.check_restorable(deprecated, deprecated)
        self.assertState(
            StateProblem.NOT_ACTIVE,
            lambda: rules.check_restorable(
                replace(current, status=MemoryStatus.SUPERSEDED), view()
            ),
        )

    def test_supersedes_needs_the_same_audience(self):
        owner = uuid4()
        newer, older = view(owner_user_id=owner), view(owner_user_id=owner)
        rules.check_relatable(ManualRelation.SUPERSEDES, newer, older)
        project = view(
            scope=MemoryScope.PROJECT, owner_user_id=None, project_id=uuid4()
        )
        self.assertState(
            StateProblem.SCOPE_MISMATCH,
            lambda: rules.check_relatable(ManualRelation.SUPERSEDES, newer, project),
        )
        # Relations that retire nothing may cross audiences.
        rules.check_relatable(ManualRelation.CONFLICTS_WITH, newer, project)
        rules.check_relatable(ManualRelation.EXTENDS, newer, project)
        self.assertState(
            StateProblem.SAME_MEMORY,
            lambda: rules.check_relatable(
                ManualRelation.EXTENDS, newer, replace(older, memory_id=newer.memory_id)
            ),
        )
        self.assertState(
            StateProblem.NOT_ACTIVE,
            lambda: rules.check_relatable(
                ManualRelation.EXTENDS,
                newer,
                replace(older, status=MemoryStatus.DEPRECATED),
            ),
        )

    def test_an_edit_of_an_unconfirmed_version_records_the_promotion(self):
        self.assertEqual(rules.confirmation_after_edit(view()), ())
        for state in (ConfirmationState.OBSERVED, ConfirmationState.INFERRED):
            self.assertEqual(
                rules.confirmation_after_edit(view(confirmation_state=state)),
                (RelationType.CONFIRMED_FROM,),
            )


if __name__ == "__main__":
    unittest.main()
