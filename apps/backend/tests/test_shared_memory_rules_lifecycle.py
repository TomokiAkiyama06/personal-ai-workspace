"""The lifecycle rules of Shared Memory (``memory.shared.lifecycle``): pure tests.

No database, no clock. Every expected value is worked out by hand from the
docstrings of the functions.
"""

import unittest
from uuid import uuid4

from paw_backend.memory.shared import (
    CandidateAction,
    CandidateState,
    EditPlan,
    SharedMemoryChanges,
    SharedMemoryDraft,
    SharedMemoryStateError,
    SharedMemoryStatus,
    SharedMemoryVersionConflictError,
    StateProblem,
)
from paw_backend.memory.shared.lifecycle import (
    apply_changes,
    changed_fields,
    check_deletable,
    check_restorable,
    draft_from_candidate,
    next_candidate_state,
    plan_edit,
)

from .shared_memory_support import make_candidate, make_memory

P = CandidateState.PENDING
A = CandidateState.APPROVED
R = CandidateState.REJECTED
APPROVE = CandidateAction.APPROVE
REJECT = CandidateAction.REJECT


class NextCandidateStateTest(unittest.TestCase):
    def test_a_pending_candidate_that_is_approved_is_approved(self):
        self.assertIs(next_candidate_state(P, APPROVE), A)

    def test_a_pending_candidate_that_is_rejected_is_rejected(self):
        self.assertIs(next_candidate_state(P, REJECT), R)

    def test_a_decided_candidate_is_final_for_every_action(self):
        for state in (A, R):
            for action in (APPROVE, REJECT):
                with self.subTest(state=state.value, action=action.value):
                    with self.assertRaises(SharedMemoryStateError) as caught:
                        next_candidate_state(state, action)
                    self.assertIs(
                        caught.exception.problem, StateProblem.CANDIDATE_NOT_PENDING
                    )

    def test_the_error_message_is_fixed(self):
        with self.assertRaises(SharedMemoryStateError) as caught:
            next_candidate_state(A, APPROVE)
        self.assertEqual(
            str(caught.exception), "State does not allow this: candidate_not_pending"
        )

    def test_arguments_that_are_not_enum_members_raise_type_error(self):
        bad_pairs = [
            ("pending", APPROVE),  # equals the member's value, is not the member
            (P, "approve"),
            (None, APPROVE),
            (P, None),
            (1, APPROVE),
            (P, 1),
            (CandidateAction.APPROVE, P),  # swapped
        ]
        for current, action in bad_pairs:
            with self.subTest(current=current, action=action):
                with self.assertRaises(TypeError):
                    next_candidate_state(current, action)

    def test_the_state_error_is_not_raised_for_a_bad_type(self):
        # "approved" is a final state but is a str here: TypeError comes first.
        with self.assertRaises(TypeError):
            next_candidate_state("approved", APPROVE)


class CheckDeletableTest(unittest.TestCase):
    def test_an_active_memory_may_be_deleted(self):
        memory = make_memory(status=SharedMemoryStatus.ACTIVE)
        self.assertIsNone(check_deletable(memory))

    def test_a_deleted_memory_may_not_be_deleted_again(self):
        memory = make_memory(status=SharedMemoryStatus.DELETED)
        with self.assertRaises(SharedMemoryStateError) as caught:
            check_deletable(memory)
        self.assertIs(caught.exception.problem, StateProblem.ALREADY_DELETED)


class CheckRestorableTest(unittest.TestCase):
    def test_a_deleted_memory_may_be_restored(self):
        memory = make_memory(status=SharedMemoryStatus.DELETED)
        self.assertIsNone(check_restorable(memory))

    def test_an_active_memory_may_not_be_restored(self):
        memory = make_memory(status=SharedMemoryStatus.ACTIVE)
        with self.assertRaises(SharedMemoryStateError) as caught:
            check_restorable(memory)
        self.assertIs(caught.exception.problem, StateProblem.NOT_DELETED)


class ApplyChangesTest(unittest.TestCase):
    def setUp(self):
        self.current = make_memory(
            title="A",
            content="c",
            memory_type="rule",
            importance=50,
            policy_subjects=("x",),
            version_number=7,
        )

    def test_the_docstring_example(self):
        draft = apply_changes(
            self.current, SharedMemoryChanges(title="B", importance=70)
        )
        self.assertEqual(
            draft,
            SharedMemoryDraft(
                memory_type="rule",
                title="B",
                content="c",
                importance=70,
                policy_subjects=("x",),
                reason=None,
            ),
        )

    def test_each_field_can_be_changed_on_its_own(self):
        cases = {
            "title": ("New title", {"title": "New title"}),
            "content": ("New content", {"content": "New content"}),
            "memory_type": ("preference", {"memory_type": "preference"}),
            "importance": (99, {"importance": 99}),
            "policy_subjects": (("a.b", "z"), {"policy_subjects": ("z", "a.b")}),
        }
        for name, (expected, kwargs) in cases.items():
            with self.subTest(field=name):
                draft = apply_changes(self.current, SharedMemoryChanges(**kwargs))
                self.assertEqual(getattr(draft, name), expected)
                for other in cases:
                    if other != name:
                        self.assertEqual(
                            getattr(draft, other), getattr(self.current, other)
                        )

    def test_importance_zero_is_a_value_not_an_absent_field(self):
        draft = apply_changes(self.current, SharedMemoryChanges(importance=0))
        self.assertEqual(draft.importance, 0)

    def test_an_empty_subject_tuple_clears_the_subjects(self):
        draft = apply_changes(self.current, SharedMemoryChanges(policy_subjects=()))
        self.assertEqual(draft.policy_subjects, ())
        self.assertEqual(draft.title, "A")

    def test_absent_subjects_keep_the_current_ones(self):
        draft = apply_changes(self.current, SharedMemoryChanges(title="B"))
        self.assertEqual(draft.policy_subjects, ("x",))

    def test_the_reason_is_the_changes_reason(self):
        draft = apply_changes(
            self.current, SharedMemoryChanges(title="B", reason="fix typo")
        )
        self.assertEqual(draft.reason, "fix typo")

    def test_without_a_reason_the_draft_has_none(self):
        draft = apply_changes(self.current, SharedMemoryChanges(title="B"))
        self.assertIsNone(draft.reason)

    def test_the_status_and_version_of_the_memory_do_not_matter(self):
        deleted = make_memory(status=SharedMemoryStatus.DELETED, title="A")
        draft = apply_changes(deleted, SharedMemoryChanges(content="z"))
        self.assertEqual((draft.title, draft.content), ("A", "z"))

    def test_the_arguments_are_not_modified(self):
        changes = SharedMemoryChanges(title="B", policy_subjects=("q",))
        before = (self.current, changes)
        apply_changes(self.current, changes)
        self.assertEqual((self.current, changes), before)

    def test_the_result_is_a_draft(self):
        draft = apply_changes(self.current, SharedMemoryChanges(title="B"))
        self.assertIsInstance(draft, SharedMemoryDraft)


class ChangedFieldsTest(unittest.TestCase):
    def setUp(self):
        self.current = make_memory(
            title="A",
            content="c",
            memory_type="rule",
            importance=50,
            policy_subjects=("x",),
        )

    def draft(self, **overrides):
        values = {
            "memory_type": "rule",
            "title": "A",
            "content": "c",
            "importance": 50,
            "policy_subjects": ("x",),
        }
        values.update(overrides)
        return SharedMemoryDraft(**values)

    def test_nothing_changed_gives_an_empty_tuple(self):
        self.assertEqual(changed_fields(self.current, self.draft()), ())

    def test_each_field_is_reported_by_its_name(self):
        cases = {
            "title": {"title": "B"},
            "content": {"content": "d"},
            "memory_type": {"memory_type": "preference"},
            "importance": {"importance": 51},
            "policy_subjects": {"policy_subjects": ("x", "y")},
        }
        for name, overrides in cases.items():
            with self.subTest(field=name):
                self.assertEqual(
                    changed_fields(self.current, self.draft(**overrides)), (name,)
                )

    def test_several_fields_are_sorted_alphabetically(self):
        draft = self.draft(title="B", content="d", importance=1)
        self.assertEqual(
            changed_fields(self.current, draft), ("content", "importance", "title")
        )

    def test_all_five_fields_in_order(self):
        draft = self.draft(
            title="B",
            content="d",
            importance=1,
            memory_type="preference",
            policy_subjects=(),
        )
        self.assertEqual(
            changed_fields(self.current, draft),
            ("content", "importance", "memory_type", "policy_subjects", "title"),
        )

    def test_the_reason_is_not_a_field(self):
        self.assertEqual(changed_fields(self.current, self.draft(reason="why")), ())

    def test_subjects_are_compared_by_value(self):
        current = make_memory(policy_subjects=("a", "b"))
        same = SharedMemoryDraft("rule", "Title A", "Content A", 50, ("b", "a"))
        self.assertEqual(changed_fields(current, same), ())
        fewer = SharedMemoryDraft("rule", "Title A", "Content A", 50, ("a",))
        self.assertEqual(changed_fields(current, fewer), ("policy_subjects",))

    def test_importance_zero_differs_from_fifty(self):
        self.assertEqual(
            changed_fields(self.current, self.draft(importance=0)), ("importance",)
        )


class PlanEditTest(unittest.TestCase):
    def setUp(self):
        self.current = make_memory(version_number=3, title="A", content="c")

    def test_a_change_gives_a_plan_with_the_new_field_set(self):
        changes = SharedMemoryChanges(title="B", reason="rename")
        plan = plan_edit(self.current, 3, changes)
        self.assertIsInstance(plan, EditPlan)
        self.assertEqual(
            plan.draft,
            SharedMemoryDraft(
                memory_type="rule",
                title="B",
                content="c",
                importance=50,
                policy_subjects=(),
                reason="rename",
            ),
        )
        self.assertEqual(plan.changed_fields, ("title",))

    def test_several_changed_fields_are_listed_alphabetically(self):
        plan = plan_edit(
            self.current, 3, SharedMemoryChanges(title="B", content="d", importance=9)
        )
        self.assertEqual(plan.changed_fields, ("content", "importance", "title"))

    def test_an_edit_that_changes_nothing_is_none(self):
        self.assertIsNone(
            plan_edit(self.current, 3, SharedMemoryChanges(title="A", content="c"))
        )

    def test_a_reason_alone_does_not_make_a_change(self):
        self.assertIsNone(
            plan_edit(
                self.current, 3, SharedMemoryChanges(title="A", reason="no change")
            )
        )

    def test_an_older_version_is_a_conflict_in_both_directions(self):
        for expected in (2, 1, 4, 99):
            with self.subTest(expected=expected):
                with self.assertRaises(SharedMemoryVersionConflictError) as caught:
                    plan_edit(self.current, expected, SharedMemoryChanges(title="B"))
                self.assertEqual(caught.exception.expected_version, expected)
                self.assertEqual(caught.exception.current_version, 3)

    def test_a_conflict_is_reported_even_when_nothing_would_change(self):
        with self.assertRaises(SharedMemoryVersionConflictError):
            plan_edit(self.current, 2, SharedMemoryChanges(title="A"))

    def test_a_deleted_memory_cannot_be_edited(self):
        deleted = make_memory(version_number=3, status=SharedMemoryStatus.DELETED)
        with self.assertRaises(SharedMemoryStateError) as caught:
            plan_edit(deleted, 3, SharedMemoryChanges(title="B"))
        self.assertIs(caught.exception.problem, StateProblem.DELETED)

    def test_a_deleted_memory_cannot_be_edited_even_without_a_change(self):
        deleted = make_memory(
            version_number=3, title="A", status=SharedMemoryStatus.DELETED
        )
        with self.assertRaises(SharedMemoryStateError):
            plan_edit(deleted, 3, SharedMemoryChanges(title="A"))

    def test_the_conflict_is_reported_before_the_deleted_state(self):
        deleted = make_memory(version_number=3, status=SharedMemoryStatus.DELETED)
        with self.assertRaises(SharedMemoryVersionConflictError):
            plan_edit(deleted, 1, SharedMemoryChanges(title="B"))

    def test_clearing_the_subjects_is_a_change(self):
        current = make_memory(version_number=1, policy_subjects=("x",))
        plan = plan_edit(current, 1, SharedMemoryChanges(policy_subjects=()))
        self.assertEqual(plan.changed_fields, ("policy_subjects",))
        self.assertEqual(plan.draft.policy_subjects, ())

    def test_clearing_already_empty_subjects_is_not_a_change(self):
        current = make_memory(version_number=1, policy_subjects=())
        self.assertIsNone(
            plan_edit(current, 1, SharedMemoryChanges(policy_subjects=()))
        )

    def test_the_memory_is_not_modified(self):
        before = self.current
        plan_edit(self.current, 3, SharedMemoryChanges(title="B"))
        self.assertEqual(self.current, before)


class DraftFromCandidateTest(unittest.TestCase):
    def test_the_content_fields_are_copied(self):
        candidate = make_candidate(
            memory_type="policy_note",
            title="T",
            content="C",
            importance=80,
            policy_subjects=("merge", "merge.permission"),
            reason="useful for everyone",
        )
        self.assertEqual(
            draft_from_candidate(candidate),
            SharedMemoryDraft(
                memory_type="policy_note",
                title="T",
                content="C",
                importance=80,
                policy_subjects=("merge", "merge.permission"),
                reason="useful for everyone",
            ),
        )

    def test_a_missing_reason_stays_none(self):
        self.assertIsNone(draft_from_candidate(make_candidate(reason=None)).reason)

    def test_importance_zero_is_copied(self):
        self.assertEqual(
            draft_from_candidate(make_candidate(importance=0)).importance, 0
        )

    def test_the_state_of_the_candidate_is_not_looked_at(self):
        for state in CandidateState:
            with self.subTest(state=state.value):
                candidate = make_candidate(
                    state=state,
                    decided_by=uuid4() if state is not P else None,
                )
                self.assertEqual(
                    draft_from_candidate(candidate).title, "Candidate title"
                )

    def test_the_result_is_a_draft(self):
        self.assertIsInstance(draft_from_candidate(make_candidate()), SharedMemoryDraft)


if __name__ == "__main__":
    unittest.main()
