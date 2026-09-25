"""The contract of Shared Memory administration that needs no database.

Value objects validate themselves when they are built; the service validates its
collaborators up front; the public surface is closed. These parts are implemented
in the repository (not left to the implementer of the rules), so these tests pass
before the stubs of ``lifecycle.py`` and ``precedence.py`` are implemented.
"""

import inspect
import unittest
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint

from paw_backend.authz import AgentGrant, Authorizer, Capability, InMemoryAuditSink
from paw_backend.db import Database
from paw_backend.memory.models import MemoryVersion
from paw_backend.memory.shared import (
    AgentActor,
    AutomaticPromotionRefusedError,
    CandidateLimitError,
    CandidateNotFoundError,
    CandidateProposal,
    EffectiveSharedMemory,
    InputProblem,
    InternalEffectiveView,
    InvalidSharedMemoryInputError,
    OriginScope,
    PolicySourceError,
    RulesContractError,
    SharedMemoryBusyError,
    SharedMemoryChanges,
    SharedMemoryDataError,
    SharedMemoryDraft,
    SharedMemoryError,
    SharedMemoryNotFoundError,
    SharedMemoryPermissionError,
    SharedMemoryService,
    SharedMemoryStateError,
    SharedMemoryVersionConflictError,
    StateProblem,
    StaticPolicySource,
    SystemPolicyItem,
    limits,
)
from paw_backend.memory.shared.models import SharedMemoryCandidateRow
from paw_backend.memory.shared.validation import (
    normalize_subjects,
    validate_subject,
)

from .support import make_settings


def problem_of(action, *args, **kwargs) -> tuple[str, InputProblem]:
    with_error = None
    try:
        action(*args, **kwargs)
    except InvalidSharedMemoryInputError as error:
        with_error = error
    assert with_error is not None, "no InvalidSharedMemoryInputError was raised"
    return with_error.field, with_error.problem


class DraftValidationTest(unittest.TestCase):
    def build(self, **overrides):
        values = {"memory_type": "rule", "title": "T", "content": "C"}
        values.update(overrides)
        return SharedMemoryDraft(**values)

    def test_the_defaults(self):
        draft = self.build()
        self.assertEqual(draft.importance, 50)
        self.assertEqual(draft.policy_subjects, ())
        self.assertIsNone(draft.reason)

    def test_the_title_boundary(self):
        self.assertEqual(len(self.build(title="a" * 200).title), 200)
        self.assertEqual(
            problem_of(self.build, title="a" * 201),
            ("title", InputProblem.TOO_LONG),
        )

    def test_the_content_boundary(self):
        self.assertEqual(len(self.build(content="a" * 20000).content), 20000)
        self.assertEqual(
            problem_of(self.build, content="a" * 20001),
            ("content", InputProblem.TOO_LONG),
        )

    def test_the_reason_boundary(self):
        self.assertEqual(len(self.build(reason="a" * 500).reason), 500)
        self.assertEqual(
            problem_of(self.build, reason="a" * 501),
            ("reason", InputProblem.TOO_LONG),
        )

    def test_text_that_is_blank_or_has_bad_characters(self):
        cases = [
            ("title", "", InputProblem.BLANK),
            ("title", "   ", InputProblem.BLANK),
            ("title", "　", InputProblem.BLANK),
            ("content", "\n\t ", InputProblem.BLANK),
            ("title", "a\x00b", InputProblem.INVALID_CHARACTERS),
            ("content", "a\ud800b", InputProblem.INVALID_CHARACTERS),
            ("title", None, InputProblem.REQUIRED),
            ("title", 5, InputProblem.WRONG_TYPE),
            ("content", b"bytes", InputProblem.WRONG_TYPE),
            ("reason", "", InputProblem.BLANK),
            ("reason", 3, InputProblem.WRONG_TYPE),
        ]
        for field, value, problem in cases:
            with self.subTest(field=field, value=value):
                self.assertEqual(
                    problem_of(self.build, **{field: value}), (field, problem)
                )

    def test_text_is_kept_unchanged(self):
        draft = self.build(title="  Padded  ", content="Line 1\nLine 2\n")
        self.assertEqual(draft.title, "  Padded  ")
        self.assertEqual(draft.content, "Line 1\nLine 2\n")

    def test_importance_bounds_and_types(self):
        for value in (0, 1, 99, 100):
            self.assertEqual(self.build(importance=value).importance, value)
        cases = [
            (-1, InputProblem.OUT_OF_RANGE),
            (101, InputProblem.OUT_OF_RANGE),
            (True, InputProblem.WRONG_TYPE),
            (False, InputProblem.WRONG_TYPE),
            (50.0, InputProblem.WRONG_TYPE),
            ("50", InputProblem.WRONG_TYPE),
            (None, InputProblem.REQUIRED),
        ]
        for value, problem in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    problem_of(self.build, importance=value),
                    ("importance", problem),
                )

    def test_memory_type_pattern(self):
        for value in ("rule", "a", "team_rule", "r2", "a" * 64):
            self.assertEqual(self.build(memory_type=value).memory_type, value)
        for value in ("", "Rule", "1rule", "rule-x", "rule x", "a" * 65, "_rule"):
            with self.subTest(value=value):
                self.assertEqual(
                    problem_of(self.build, memory_type=value),
                    ("memory_type", InputProblem.INVALID_FORMAT),
                )
        self.assertEqual(
            problem_of(self.build, memory_type=5),
            ("memory_type", InputProblem.WRONG_TYPE),
        )

    def test_subjects_are_sorted_and_deduplicated(self):
        draft = self.build(policy_subjects=["b", "a.b", "a", "b"])
        self.assertEqual(draft.policy_subjects, ("a", "a.b", "b"))

    def test_subjects_accept_sets_and_reject_strings_and_mappings(self):
        self.assertEqual(
            self.build(policy_subjects={"z", "y"}).policy_subjects, ("y", "z")
        )
        for value in ("merge", b"merge", {"merge": 1}, 5, iter(["merge"])):
            with self.subTest(value=value):
                self.assertEqual(
                    problem_of(self.build, policy_subjects=value),
                    ("policy_subjects", InputProblem.WRONG_TYPE),
                )

    def test_elements_that_cannot_be_hashed_are_a_wrong_type_not_a_crash(self):
        for element in (["merge"], {"merge": 1}, {"merge"}):
            with self.subTest(element=element):
                self.assertEqual(
                    problem_of(self.build, policy_subjects=["ok", element]),
                    ("policy_subjects", InputProblem.WRONG_TYPE),
                )

    def test_at_most_twenty_subjects(self):
        twenty = [f"s{n}" for n in range(20)]
        self.assertEqual(len(self.build(policy_subjects=twenty).policy_subjects), 20)
        self.assertEqual(
            problem_of(self.build, policy_subjects=[*twenty, "extra"]),
            ("policy_subjects", InputProblem.TOO_MANY),
        )

    def test_the_count_limit_applies_before_deduplication(self):
        self.assertEqual(
            problem_of(self.build, policy_subjects=["same"] * 21),
            ("policy_subjects", InputProblem.TOO_MANY),
        )

    def test_every_subject_is_validated_before_duplicates_are_dropped(self):
        self.assertEqual(
            problem_of(self.build, policy_subjects=["ok", "ok", "Not Valid"]),
            ("policy_subjects", InputProblem.INVALID_FORMAT),
        )
        self.assertEqual(
            problem_of(self.build, policy_subjects=["ok", 5]),
            ("policy_subjects", InputProblem.WRONG_TYPE),
        )

    def test_the_draft_is_frozen(self):
        with self.assertRaises(AttributeError):
            self.build().title = "changed"  # type: ignore[misc]


class SubjectValidationTest(unittest.TestCase):
    def test_valid_subjects(self):
        for value in ("merge", "merge.permission", "a.b.c.d.e", "a1_b", "x" * 32):
            self.assertEqual(validate_subject(value), value)

    def test_invalid_subjects(self):
        too_long_segment = "x" * 33
        for value in (
            "",
            "Merge",
            "merge.",
            ".merge",
            "merge..x",
            "a.b.c.d.e.f",
            "1merge",
            "merge-x",
            "merge x",
            too_long_segment,
            "a" * 101,
            "merge\n",
        ):
            with self.subTest(value=value):
                with self.assertRaises(InvalidSharedMemoryInputError) as caught:
                    validate_subject(value)
                self.assertIs(caught.exception.problem, InputProblem.INVALID_FORMAT)

    def test_normalize_returns_a_sorted_tuple(self):
        self.assertEqual(normalize_subjects("f", ["b", "a", "b"]), ("a", "b"))
        self.assertEqual(normalize_subjects("f", None), ())


class ChangesValidationTest(unittest.TestCase):
    def test_at_least_one_field_is_required(self):
        self.assertEqual(
            problem_of(
                SharedMemoryChanges,
            ),
            ("changes", InputProblem.REQUIRED),
        )

    def test_a_reason_alone_is_not_a_change(self):
        self.assertEqual(
            problem_of(SharedMemoryChanges, reason="why"),
            ("changes", InputProblem.REQUIRED),
        )

    def test_clearing_the_subjects_is_a_change(self):
        changes = SharedMemoryChanges(policy_subjects=[])
        self.assertEqual(changes.policy_subjects, ())

    def test_zero_importance_is_a_change(self):
        self.assertEqual(SharedMemoryChanges(importance=0).importance, 0)

    def test_every_given_field_is_validated_like_a_draft(self):
        cases = [
            ({"title": " "}, ("title", InputProblem.BLANK)),
            ({"title": "a" * 201}, ("title", InputProblem.TOO_LONG)),
            ({"content": "a" * 20001}, ("content", InputProblem.TOO_LONG)),
            ({"memory_type": "Bad"}, ("memory_type", InputProblem.INVALID_FORMAT)),
            ({"importance": 101}, ("importance", InputProblem.OUT_OF_RANGE)),
            ({"importance": True}, ("importance", InputProblem.WRONG_TYPE)),
            (
                {"policy_subjects": "merge"},
                ("policy_subjects", InputProblem.WRONG_TYPE),
            ),
            ({"title": "ok", "reason": " "}, ("reason", InputProblem.BLANK)),
        ]
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(problem_of(SharedMemoryChanges, **kwargs), expected)


class ProposalValidationTest(unittest.TestCase):
    def build(self, **overrides):
        values = {
            "memory_type": "rule",
            "title": "T",
            "content": "C",
            "origin_scope": OriginScope.PROJECT,
        }
        values.update(overrides)
        return CandidateProposal(**values)

    def test_a_valid_proposal(self):
        proposal = self.build(origin_version_id=uuid4(), reason="share it")
        self.assertIs(proposal.origin_scope, OriginScope.PROJECT)
        self.assertEqual(proposal.importance, 50)

    def test_the_origin_scope_must_be_an_origin_scope(self):
        from paw_backend.memory.models import MemoryScope

        for value in ("user", MemoryScope.USER, MemoryScope.SHARED, None, 1):
            with self.subTest(value=value):
                field, problem = problem_of(self.build, origin_scope=value)
                self.assertEqual(field, "origin_scope")
                self.assertIn(problem, (InputProblem.WRONG_TYPE, InputProblem.REQUIRED))

    def test_shared_is_not_an_origin_scope(self):
        self.assertEqual(
            {scope.value for scope in OriginScope},
            {"user", "project", "project_group", "repo"},
        )

    def test_the_origin_version_id_is_a_uuid_or_none(self):
        self.assertIsNone(self.build().origin_version_id)
        for value in ("6b2bb4d3-0000-0000-0000-000000000000", 5, b"x"):
            with self.subTest(value=value):
                self.assertEqual(
                    problem_of(self.build, origin_version_id=value),
                    ("origin_version_id", InputProblem.WRONG_TYPE),
                )

    def test_the_content_fields_follow_the_draft_rules(self):
        self.assertEqual(
            problem_of(self.build, title="a" * 201),
            ("title", InputProblem.TOO_LONG),
        )
        self.assertEqual(
            problem_of(self.build, policy_subjects=["Bad"]),
            ("policy_subjects", InputProblem.INVALID_FORMAT),
        )
        self.assertEqual(
            self.build(policy_subjects=["b", "a"]).policy_subjects, ("a", "b")
        )


class AgentActorTest(unittest.TestCase):
    def test_a_valid_actor(self):
        grant = AgentGrant(uuid4(), frozenset({Capability.MEMORY_USE}), frozenset())
        actor = AgentActor(uuid4(), grant)
        self.assertIs(actor.grant, grant)

    def test_the_delegator_must_be_a_uuid_object(self):
        grant = AgentGrant(uuid4(), frozenset(), frozenset())
        for value in (str(uuid4()), None, 5):
            with self.subTest(value=value):
                with self.assertRaises(InvalidSharedMemoryInputError):
                    AgentActor(value, grant)

    def test_the_grant_must_be_an_agent_grant(self):
        for value in (None, {"capabilities": []}, frozenset()):
            with self.subTest(value=value):
                self.assertEqual(
                    problem_of(AgentActor, uuid4(), value),
                    ("grant", InputProblem.WRONG_TYPE),
                )


class PolicyItemValidationTest(unittest.TestCase):
    def test_a_valid_item(self):
        item = SystemPolicyItem("no-force-push", "git.push", "Never force push.")
        self.assertEqual(item.subject, "git.push")

    def test_invalid_items(self):
        cases = [
            (("", "a", "s"), "policy_id"),
            (("Bad", "a", "s"), "policy_id"),
            (("p" * 65, "a", "s"), "policy_id"),
            (("p", "A.b", "s"), "subject"),
            (("p", "a", ""), "statement"),
            (("p", "a", "s" * 2001), "statement"),
            (("p", "a", "x\x00y"), "statement"),
            ((5, "a", "s"), "policy_id"),
        ]
        for args, field in cases:
            with self.subTest(args=args):
                with self.assertRaises(InvalidSharedMemoryInputError) as caught:
                    SystemPolicyItem(*args)
                self.assertEqual(caught.exception.field, field)

    def test_the_statement_boundary(self):
        self.assertEqual(len(SystemPolicyItem("p", "a", "s" * 2000).statement), 2000)


class ErrorMessageTest(unittest.TestCase):
    def test_messages_are_fixed_and_carry_no_input(self):
        cases = [
            (
                InvalidSharedMemoryInputError("title", InputProblem.TOO_LONG),
                "Invalid title: too_long",
            ),
            (
                SharedMemoryPermissionError("capability_not_granted"),
                "Not allowed: capability_not_granted",
            ),
            (SharedMemoryNotFoundError(), "Shared memory not found"),
            (CandidateNotFoundError(), "Shared memory candidate not found"),
            (
                SharedMemoryStateError(StateProblem.DELETED),
                "State does not allow this: deleted",
            ),
            (
                SharedMemoryVersionConflictError(2, 3),
                "The shared memory changed since the version you edited",
            ),
            (CandidateLimitError(), "Too many pending candidates"),
            (SharedMemoryBusyError(), "Shared memory is busy; retry later"),
            (PolicySourceError(), "System policy is unavailable"),
            (SharedMemoryDataError(), "A stored shared memory is malformed"),
            (RulesContractError("plan_edit"), "Rule plan_edit broke its contract"),
        ]
        for error, message in cases:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(str(error), message)
                self.assertIsInstance(error, SharedMemoryError)

    def test_the_promotion_refusal_is_a_permission_error(self):
        error = AutomaticPromotionRefusedError("agent_capability_forbidden")
        self.assertIsInstance(error, SharedMemoryPermissionError)
        self.assertEqual(error.reason, "agent_capability_forbidden")

    def test_every_error_has_a_distinct_stable_code(self):
        classes = [
            cls
            for cls in vars(
                __import__("paw_backend.memory.shared.errors", fromlist=["x"])
            ).values()
            if inspect.isclass(cls) and issubclass(cls, SharedMemoryError)
        ]
        codes = [cls.code for cls in classes]
        self.assertEqual(len(codes), len(set(codes)), codes)
        self.assertEqual(len(classes), 13)


class LimitsMatchTheDatabaseTest(unittest.TestCase):
    """The service limits and the CHECK constraints must say the same numbers."""

    def check_text(self, table, name):
        return str(
            next(
                c.sqltext for c in table.constraints if getattr(c, "name", None) == name
            )
        )

    def test_the_candidate_constraints_use_the_service_limits(self):
        table = SharedMemoryCandidateRow.__table__
        prefix = "ck_shared_memory_candidates_"
        expected = {
            "title_length": limits.MAX_TITLE_CHARS,
            "content_length": limits.MAX_CONTENT_CHARS,
            "memory_type_length": limits.MAX_MEMORY_TYPE_CHARS,
            "reason_length": limits.MAX_REASON_CHARS,
            "decision_reason_length": limits.MAX_REASON_CHARS,
            "policy_subjects_count": limits.MAX_POLICY_SUBJECTS,
        }
        constraints = {
            c.name: str(c.sqltext)
            for c in table.constraints
            if isinstance(c, CheckConstraint)
        }
        for name, number in expected.items():
            with self.subTest(name):
                self.assertIn(str(number), constraints[prefix + name])

    def test_the_memory_version_checks_are_at_least_as_wide_as_the_service_limits(self):
        table = MemoryVersion.__table__
        constraints = {
            c.name: str(c.sqltext)
            for c in table.constraints
            if isinstance(c, CheckConstraint)
        }
        self.assertIn(
            f"BETWEEN 1 AND {limits.MAX_TITLE_CHARS}",
            constraints["ck_memory_versions_title_length"],
        )
        self.assertIn(
            f"BETWEEN 1 AND {limits.MAX_MEMORY_TYPE_CHARS}",
            constraints["ck_memory_versions_memory_type_length"],
        )

    def test_the_documented_numbers(self):
        self.assertEqual(
            (
                limits.MAX_TITLE_CHARS,
                limits.MAX_CONTENT_CHARS,
                limits.MAX_REASON_CHARS,
                limits.MAX_POLICY_SUBJECTS,
                limits.MAX_LIST_LIMIT,
                limits.MAX_PENDING_CANDIDATES_PER_PROPOSER,
            ),
            (200, 20000, 500, 20, 200, 50),
        )


class ServiceConstructionTest(unittest.TestCase):
    def setUp(self):
        self.database = Database(make_settings())
        self.authorizer = Authorizer(InMemoryAuditSink())
        self.policies = StaticPolicySource(())

    def build(self, **overrides):
        values = {
            "database": self.database,
            "authorizer": self.authorizer,
            "policies": self.policies,
        }
        values.update(overrides)
        return SharedMemoryService(**values)

    def test_a_valid_service_builds_without_touching_the_database(self):
        self.assertIsInstance(self.build(), SharedMemoryService)

    def test_wrong_collaborators_fail_loudly_up_front(self):
        class Half:
            async def authorize(self, *args):
                return None

        cases = [
            ({"database": object()}, "database"),
            ({"database": None}, "database"),
            ({"authorizer": object()}, "authorizer"),
            ({"authorizer": Half()}, "authorizer"),
            ({"policies": object()}, "policies"),
            ({"clock": "now"}, "clock"),
        ]
        for overrides, field in cases:
            with self.subTest(field=field, overrides=overrides):
                with self.assertRaises(InvalidSharedMemoryInputError) as caught:
                    self.build(**overrides)
                self.assertEqual(caught.exception.field, field)

    def test_option_bounds(self):
        self.build(lock_timeout_ms=1)
        self.build(lock_timeout_ms=60000)
        self.build(policy_timeout_seconds=0.001)
        self.build(policy_timeout_seconds=60)
        self.build(max_pending_candidates=1)
        self.build(max_pending_candidates=1000)
        bad = [
            {"lock_timeout_ms": 0},
            {"lock_timeout_ms": 60001},
            {"lock_timeout_ms": True},
            {"lock_timeout_ms": "5"},
            {"lock_timeout_ms": 5.0},
            {"policy_timeout_seconds": 0},
            {"policy_timeout_seconds": -1},
            {"policy_timeout_seconds": 60.5},
            {"policy_timeout_seconds": True},
            {"policy_timeout_seconds": "3"},
            {"max_pending_candidates": 0},
            {"max_pending_candidates": 1001},
            {"max_pending_candidates": False},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides):
                with self.assertRaises(InvalidSharedMemoryInputError):
                    self.build(**overrides)


class PublicSurfaceTest(unittest.TestCase):
    """There is no way to write Shared Memory except through the gated methods."""

    MANAGE = {
        "create_memory",
        "edit_memory",
        "delete_memory",
        "restore_memory",
        "approve_candidate",
        "reject_candidate",
        "list_candidates",
        "get_candidate",
    }
    OTHER = {
        "list_memories",
        "get_memory",
        "effective_view",
        "propose_candidate",
    }
    # Backend-internal (Decision 0009, section 10): the one method that returns
    # the wording of the System Policies that overrode a memory. It is named here
    # on purpose so that a new such method cannot appear unnoticed; whoever adds
    # an HTTP surface must not expose it.
    INTERNAL = {"internal_effective_view"}

    def public_members(self):
        return {
            name: member
            for name, member in inspect.getmembers(SharedMemoryService)
            if not name.startswith("_") and callable(member)
        }

    def test_the_public_methods_are_exactly_the_documented_ones(self):
        # ``INTERNAL`` is added to the set (the test used to expect
        # ``MANAGE | OTHER``): Decision 0009 (approved) keeps the policy wording
        # off the public view and gives the backend its own path to it.
        self.assertEqual(
            set(self.public_members()), self.MANAGE | self.OTHER | self.INTERNAL
        )

    def test_only_the_internal_method_returns_the_policy_wording(self):
        hints = {
            name: inspect.signature(member).return_annotation
            for name, member in self.public_members().items()
        }
        self.assertIs(hints["effective_view"], EffectiveSharedMemory)
        self.assertIs(hints["internal_effective_view"], InternalEffectiveView)
        carriers = {
            name for name, hint in hints.items() if hint is InternalEffectiveView
        }
        self.assertEqual(carriers, self.INTERNAL)

    def test_every_public_method_is_async_and_takes_the_actor_first(self):
        for name, member in self.public_members().items():
            with self.subTest(method=name):
                self.assertTrue(inspect.iscoroutinefunction(member))
                parameters = list(inspect.signature(member).parameters)
                self.assertEqual(parameters[:2], ["self", "actor"])

    def test_no_public_attribute_holds_state_or_a_backdoor(self):
        service = SharedMemoryService(
            Database(make_settings()),
            Authorizer(InMemoryAuditSink()),
            StaticPolicySource(()),
        )
        public = {name for name in vars(service) if not name.startswith("_")}
        self.assertEqual(public, set())

    def test_the_module_level_helpers_are_lock_keys_only(self):
        from paw_backend.memory.shared import memory_lock_key, proposer_lock_key

        an_id = UUID(int=7)
        self.assertEqual(
            memory_lock_key(an_id),
            "paw.shared_memory.00000000-0000-0000-0000-000000000007",
        )
        self.assertEqual(
            proposer_lock_key(an_id),
            "paw.shared_memory.proposer.00000000-0000-0000-0000-000000000007",
        )


if __name__ == "__main__":
    unittest.main()
