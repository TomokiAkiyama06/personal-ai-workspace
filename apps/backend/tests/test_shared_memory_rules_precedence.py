"""The precedence rule of Shared Memory (``memory.shared.precedence``): pure tests."""

import dataclasses
import json
import unittest
from uuid import uuid4

from paw_backend.memory.shared import (
    EffectiveSharedMemory,
    InputProblem,
    InternalEffectiveView,
    InvalidSharedMemoryInputError,
    OverriddenMemory,
    SharedMemoryStatus,
)
from paw_backend.memory.shared.precedence import (
    overriding_policy_ids,
    resolve_effective_view,
    subject_covers,
)

from .shared_memory_support import make_memory, policy


class SubjectCoversTest(unittest.TestCase):
    def test_the_table_of_examples(self):
        cases = [
            ("merge", "merge", True),
            ("merge", "merge.permission", True),
            ("merge", "merge.permission.admin", True),
            ("merge.permission", "merge", False),
            ("merge", "mergeable", False),
            ("merge", "merge_x", False),
            ("a.b", "a.bc", False),
            ("a.b", "a.b.c", True),
            ("a.b", "a", False),
            ("a.b", "a.c", False),
            ("a.b", "b.a", False),
            ("docs", "merge", False),
            ("a", "a.b.c.d.e", True),
            ("a.b.c.d.e", "a.b.c.d.e", True),
        ]
        for policy_subject, memory_subject, expected in cases:
            with self.subTest(policy=policy_subject, memory=memory_subject):
                self.assertIs(subject_covers(policy_subject, memory_subject), expected)

    def test_an_invalid_policy_subject_is_rejected_first(self):
        with self.assertRaises(InvalidSharedMemoryInputError) as caught:
            subject_covers("Merge", "also bad")
        self.assertEqual(caught.exception.field, "policy_subject")
        self.assertIs(caught.exception.problem, InputProblem.INVALID_FORMAT)

    def test_an_invalid_memory_subject_is_rejected(self):
        with self.assertRaises(InvalidSharedMemoryInputError) as caught:
            subject_covers("merge", "merge..x")
        self.assertEqual(caught.exception.field, "memory_subject")
        self.assertIs(caught.exception.problem, InputProblem.INVALID_FORMAT)

    def test_bad_subjects_of_every_kind(self):
        bad = ["", "Merge", "merge.", ".merge", "1merge", "merge-x", "a.b.c.d.e.f"]
        for value in bad:
            with self.subTest(policy=value):
                with self.assertRaises(InvalidSharedMemoryInputError):
                    subject_covers(value, "merge")
            with self.subTest(memory=value):
                with self.assertRaises(InvalidSharedMemoryInputError):
                    subject_covers("merge", value)

    def test_a_non_string_is_a_wrong_type(self):
        for value in (None, 5, b"merge", ("merge",)):
            with self.subTest(value=value):
                with self.assertRaises(InvalidSharedMemoryInputError) as caught:
                    subject_covers(value, "merge")
                self.assertIn(
                    caught.exception.problem,
                    (InputProblem.WRONG_TYPE, InputProblem.REQUIRED),
                )

    def test_the_error_does_not_echo_the_subject(self):
        with self.assertRaises(InvalidSharedMemoryInputError) as caught:
            subject_covers("SECRET-hunter2", "merge")
        self.assertNotIn("hunter2", str(caught.exception))


class OverridingPolicyIdsTest(unittest.TestCase):
    def test_the_docstring_example(self):
        policies = [
            policy("p2", "merge"),
            policy("p1", "merge.permission"),
            policy("p3", "deploy"),
        ]
        self.assertEqual(
            overriding_policy_ids(("merge.permission", "docs"), policies), ("p1", "p2")
        )

    def test_no_subjects_means_no_policy(self):
        self.assertEqual(overriding_policy_ids((), [policy("p1", "merge")]), ())

    def test_no_policies_means_no_policy(self):
        self.assertEqual(overriding_policy_ids(("merge",), []), ())

    def test_nothing_covered(self):
        self.assertEqual(overriding_policy_ids(("docs",), [policy("p1", "merge")]), ())

    def test_a_policy_is_listed_once_however_many_subjects_it_covers(self):
        result = overriding_policy_ids(
            ("merge.a", "merge.b", "merge"), [policy("p1", "merge")]
        )
        self.assertEqual(result, ("p1",))

    def test_the_result_is_sorted_by_id_not_by_input_order(self):
        policies = [policy("zz", "merge"), policy("aa", "merge"), policy("mm", "merge")]
        self.assertEqual(
            overriding_policy_ids(("merge",), policies), ("aa", "mm", "zz")
        )

    def test_a_policy_below_the_memory_subject_does_not_cover_it(self):
        self.assertEqual(
            overriding_policy_ids(("merge",), [policy("p1", "merge.permission")]), ()
        )

    def test_a_similar_name_is_not_covered(self):
        self.assertEqual(
            overriding_policy_ids(("mergeable",), [policy("p1", "merge")]), ()
        )

    def test_the_arguments_are_not_modified(self):
        subjects = ["merge"]
        policies = [policy("p2", "merge"), policy("p1", "merge")]
        overriding_policy_ids(subjects, policies)
        self.assertEqual(subjects, ["merge"])
        self.assertEqual([p.policy_id for p in policies], ["p2", "p1"])

    def test_the_result_is_a_tuple(self):
        self.assertIsInstance(
            overriding_policy_ids(("merge",), [policy("p", "merge")]), tuple
        )


class ResolveEffectiveViewTest(unittest.TestCase):
    def setUp(self):
        self.m1 = make_memory(title="m1", policy_subjects=("merge.permission",))
        self.m2 = make_memory(title="m2", policy_subjects=())
        self.m3 = make_memory(title="m3", policy_subjects=("docs",))
        self.p1 = policy("p1", "merge")
        self.p2 = policy("p2", "deploy")

    def test_the_docstring_example(self):
        view = resolve_effective_view([self.m1, self.m2, self.m3], [self.p1, self.p2])
        # Decision 0009, section 10: the rule returns the internal view (it
        # carries the policy items); only its ``public()`` may reach a user.
        self.assertIsInstance(view, InternalEffectiveView)
        self.assertEqual(view.memories, (self.m2, self.m3))
        self.assertEqual(
            view.overridden,
            (OverriddenMemory(self.m1.memory_id, self.m1.version_id, ("p1",)),),
        )
        self.assertEqual(view.applied_policies, (self.p1,))

    def test_the_policy_wins_the_overridden_content_is_not_in_the_result(self):
        view = resolve_effective_view([self.m1], [self.p1])
        self.assertEqual(view.memories, ())
        self.assertEqual(len(view.overridden), 1)
        self.assertNotIn(self.m1, view.memories)

    def test_without_policies_every_active_memory_is_kept_in_order(self):
        view = resolve_effective_view([self.m3, self.m1, self.m2], [])
        self.assertEqual(view.memories, (self.m3, self.m1, self.m2))
        self.assertEqual(view.overridden, ())
        self.assertEqual(view.applied_policies, ())

    def test_without_memories_the_view_is_empty(self):
        view = resolve_effective_view([], [self.p1])
        self.assertEqual(
            (view.memories, view.overridden, view.applied_policies), ((), (), ())
        )

    def test_a_memory_without_subjects_is_never_overridden(self):
        view = resolve_effective_view([self.m2], [self.p1, self.p2])
        self.assertEqual(view.memories, (self.m2,))

    def test_deleted_memories_appear_nowhere(self):
        deleted = make_memory(
            title="d",
            policy_subjects=("merge",),
            status=SharedMemoryStatus.DELETED,
        )
        deleted_plain = make_memory(title="d2", status=SharedMemoryStatus.DELETED)
        view = resolve_effective_view([deleted, deleted_plain, self.m2], [self.p1])
        self.assertEqual(view.memories, (self.m2,))
        self.assertEqual(view.overridden, ())
        self.assertEqual(view.applied_policies, ())

    def test_overridden_keeps_the_input_order(self):
        a = make_memory(policy_subjects=("merge",))
        b = make_memory(policy_subjects=("merge.x",))
        c = make_memory(policy_subjects=("merge.y",))
        view = resolve_effective_view([b, a, c], [self.p1])
        self.assertEqual(
            [item.memory_id for item in view.overridden],
            [b.memory_id, a.memory_id, c.memory_id],
        )

    def test_several_policies_on_one_memory_are_sorted_and_all_applied(self):
        memory = make_memory(policy_subjects=("merge", "deploy.prod"))
        policies = [
            policy("zz", "deploy"),
            policy("aa", "merge"),
            policy("mm", "other"),
        ]
        view = resolve_effective_view([memory], policies)
        self.assertEqual(
            view.overridden,
            (OverriddenMemory(memory.memory_id, memory.version_id, ("aa", "zz")),),
        )
        self.assertEqual([p.policy_id for p in view.applied_policies], ["aa", "zz"])

    def test_a_policy_that_overrides_nothing_is_not_listed(self):
        view = resolve_effective_view([self.m1], [self.p1, self.p2])
        self.assertEqual(view.applied_policies, (self.p1,))

    def test_an_applied_policy_is_listed_once_for_many_memories(self):
        a = make_memory(policy_subjects=("merge",))
        b = make_memory(policy_subjects=("merge.x",))
        view = resolve_effective_view([a, b], [self.p1])
        self.assertEqual(view.applied_policies, (self.p1,))

    def test_applied_policies_are_sorted_by_id(self):
        a = make_memory(policy_subjects=("merge",))
        b = make_memory(policy_subjects=("deploy",))
        policies = [policy("z", "merge"), policy("a", "deploy")]
        view = resolve_effective_view([a, b], policies)
        self.assertEqual([p.policy_id for p in view.applied_policies], ["a", "z"])

    def test_a_policy_below_the_memory_subject_does_not_override_it(self):
        memory = make_memory(policy_subjects=("merge",))
        view = resolve_effective_view([memory], [policy("p", "merge.permission")])
        self.assertEqual(view.memories, (memory,))

    def test_every_collection_of_the_result_is_a_tuple(self):
        view = resolve_effective_view([self.m1, self.m2], [self.p1])
        for value in (view.memories, view.overridden, view.applied_policies):
            self.assertIsInstance(value, tuple)
        self.assertIsInstance(view.overridden[0].policy_ids, tuple)

    def test_the_inputs_are_not_modified(self):
        memories = [self.m1, self.m2]
        policies = [self.p2, self.p1]
        resolve_effective_view(memories, policies)
        self.assertEqual(memories, [self.m1, self.m2])
        self.assertEqual(policies, [self.p2, self.p1])

    def test_the_same_memory_twice_is_resolved_twice(self):
        view = resolve_effective_view([self.m2, self.m2], [])
        self.assertEqual(view.memories, (self.m2, self.m2))

    def test_ids_are_carried_from_the_memory(self):
        memory = make_memory(
            memory_id=uuid4(), version_id=uuid4(), policy_subjects=("merge",)
        )
        view = resolve_effective_view([memory], [self.p1])
        self.assertEqual(view.overridden[0].memory_id, memory.memory_id)
        self.assertEqual(view.overridden[0].version_id, memory.version_id)


if __name__ == "__main__":
    unittest.main()


class PublicViewCarriesNoPolicyTextTest(unittest.TestCase):
    """The view a user or an agent may get has no policy wording anywhere.

    Decision 0009, section 10: ``applied_policies`` (the ``statement`` of the
    System Policies that overrode a memory) is for the backend's own context
    assembly. The public ``EffectiveSharedMemory`` has no such field, so the
    wording cannot appear in it however it is read: attribute, ``repr``, ``str``,
    ``dataclasses.asdict`` or JSON.
    """

    WORDING = "POLICY-WORDING-Merging-needs-a-human-approval"

    def setUp(self):
        self.memory = make_memory(title="m1", policy_subjects=("merge.permission",))
        self.plain = make_memory(title="m2")
        self.item = policy("no-auto-merge", "merge", self.WORDING)
        self.internal = resolve_effective_view([self.memory, self.plain], [self.item])
        self.public = self.internal.public()

    def test_the_rule_result_holds_the_wording_for_the_internal_use(self):
        self.assertEqual(self.internal.applied_policies, (self.item,))
        self.assertEqual(self.internal.applied_policies[0].statement, self.WORDING)

    def test_the_public_view_has_no_field_for_the_policies(self):
        self.assertEqual(
            [f.name for f in dataclasses.fields(EffectiveSharedMemory)],
            ["memories", "overridden"],
        )
        self.assertFalse(hasattr(self.public, "applied_policies"))
        with self.assertRaises(AttributeError):
            self.public.applied_policies  # noqa: B018

    def test_the_public_view_keeps_everything_but_the_wording(self):
        self.assertIsInstance(self.public, EffectiveSharedMemory)
        self.assertEqual(self.public.memories, (self.plain,))
        self.assertEqual(self.public.overridden, self.internal.overridden)
        # The ids of the winning policies stay (Decision 0009, section 9 4).
        self.assertEqual(self.public.overridden[0].policy_ids, ("no-auto-merge",))

    def test_no_reading_of_the_public_view_shows_the_wording(self):
        readings = {
            "repr": repr(self.public),
            "str": str(self.public),
            "asdict": repr(dataclasses.asdict(self.public)),
            "json": json.dumps(dataclasses.asdict(self.public), default=str),
            "astuple": repr(dataclasses.astuple(self.public)),
        }
        for how, text in readings.items():
            with self.subTest(reading=how):
                self.assertNotIn(self.WORDING, text)
                self.assertNotIn("statement", text)

    def test_the_internal_view_does_not_put_the_wording_in_its_repr(self):
        # A log line made by accident from the internal view stays clean too.
        self.assertNotIn(self.WORDING, repr(self.internal))
        self.assertNotIn(self.WORDING, str(self.internal))
        # It is still there for the code that needs it.
        self.assertIn(self.WORDING, repr(dataclasses.asdict(self.internal)))

    def test_the_public_view_of_an_empty_view_is_empty(self):
        public = resolve_effective_view([], [self.item]).public()
        self.assertEqual((public.memories, public.overridden), ((), ()))
