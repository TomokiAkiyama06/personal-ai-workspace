"""The service keeps its own guards around the rule functions.

The rules of ``lifecycle.py`` and ``precedence.py`` are written apart from the
service (possibly by a weaker model). The service never persists what a rule
returns without checking it against the contract, and it changes a version's
status only when the version still has the status the rule assumed. These tests
replace the rule functions by wrong ones and check that nothing is written. They
patch the functions, so they pass while the real ones are still stubs.
"""

from unittest.mock import patch

from paw_backend.memory.shared import (
    CandidateState,
    EditPlan,
    RulesContractError,
    SharedMemoryBusyError,
    SharedMemoryChanges,
    SharedMemoryDraft,
)

from .shared_memory_support import (
    AsyncPostgresSharedTestCase,
    draft,
    requires_postgres,
)

SERVICE = "paw_backend.memory.shared.service"


def edit_plan(fields=("title",), **draft_overrides):
    return EditPlan(
        draft=draft(title="Planned", **draft_overrides), changed_fields=fields
    )


@requires_postgres
class ApproveGuardsTest(AsyncPostgresSharedTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.candidate_id = self.seed_candidate(
            title="Candidate title",
            content="Candidate content",
            memory_type="rule",
            importance=50,
            policy_subjects=["a"],
        )
        self.before = self.snapshot()

    async def approve_with_state(self, state):
        with patch(f"{SERVICE}.next_candidate_state", return_value=state):
            with self.assertRaises(RulesContractError) as caught:
                await self.service.approve_candidate(self.admin, self.candidate_id)
        self.assertEqual(caught.exception.rule, "next_candidate_state")

    async def test_approval_needs_the_approved_state_from_the_rule(self):
        for state in (
            CandidateState.REJECTED,
            CandidateState.PENDING,
            "approved",
            None,
        ):
            with self.subTest(state=state):
                await self.approve_with_state(state)
                self.assertEqual(self.snapshot(), self.before)

    async def test_a_rejection_needs_the_rejected_state_from_the_rule(self):
        for state in (CandidateState.APPROVED, CandidateState.PENDING, "rejected"):
            with self.subTest(state=state):
                with patch(f"{SERVICE}.next_candidate_state", return_value=state):
                    with self.assertRaises(RulesContractError):
                        await self.service.reject_candidate(
                            self.admin, self.candidate_id
                        )
                self.assertEqual(self.snapshot(), self.before)

    async def test_the_draft_must_be_exactly_the_candidates_content(self):
        wrong = [
            None,
            {"title": "Candidate title"},
            draft(
                title="Other title", content="Candidate content", policy_subjects=["a"]
            ),
            draft(
                title="Candidate title", content="Other content", policy_subjects=["a"]
            ),
            draft(
                title="Candidate title",
                content="Candidate content",
                memory_type="other",
                policy_subjects=["a"],
            ),
            draft(
                title="Candidate title",
                content="Candidate content",
                importance=51,
                policy_subjects=["a"],
            ),
            draft(
                title="Candidate title", content="Candidate content", policy_subjects=[]
            ),
            draft(
                title="Candidate title",
                content="Candidate content",
                policy_subjects=["a", "b"],
            ),
        ]
        for candidate_draft in wrong:
            with self.subTest(draft=str(candidate_draft)[:60]):
                with (
                    patch(
                        f"{SERVICE}.next_candidate_state",
                        return_value=CandidateState.APPROVED,
                    ),
                    patch(
                        f"{SERVICE}.draft_from_candidate", return_value=candidate_draft
                    ),
                ):
                    with self.assertRaises(RulesContractError) as caught:
                        await self.service.approve_candidate(
                            self.admin, self.candidate_id
                        )
                self.assertEqual(caught.exception.rule, "draft_from_candidate")
                self.assertEqual(self.snapshot(), self.before)

    async def test_a_correct_draft_is_written_even_if_the_rule_is_replaced(self):
        right = SharedMemoryDraft(
            memory_type="rule",
            title="Candidate title",
            content="Candidate content",
            importance=50,
            policy_subjects=("a",),
            reason="from the stub",
        )
        with (
            patch(
                f"{SERVICE}.next_candidate_state", return_value=CandidateState.APPROVED
            ),
            patch(f"{SERVICE}.draft_from_candidate", return_value=right),
        ):
            decision = await self.service.approve_candidate(
                self.admin, self.candidate_id
            )
        (version,) = self.versions(decision.memory.memory_id)
        self.assertEqual(version["change_reason"], "from the stub")
        self.assertEqual(decision.candidate.state, CandidateState.APPROVED)


@requires_postgres
class EditGuardsTest(AsyncPostgresSharedTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.memory_id = self.seed_memory(title="Old", versions=2)
        self.before = self.snapshot()

    async def edit_with(self, plan, *, expected=2, memory_id=None):
        with patch(f"{SERVICE}.plan_edit", return_value=plan):
            return await self.service.edit_memory(
                self.admin,
                memory_id or self.memory_id,
                expected,
                SharedMemoryChanges(title="Anything"),
            )

    async def test_a_plan_that_breaks_the_contract_writes_nothing(self):
        bad_plans = [
            {"draft": draft(), "changed_fields": ("title",)},
            "plan",
            edit_plan(fields=()),
            edit_plan(fields=("title", "content")),  # not sorted
            edit_plan(fields=("title", "title")),  # not unique
            edit_plan(fields=("bogus",)),
            edit_plan(fields=("reason",)),
            edit_plan(fields=["title"]),  # a list, not a tuple
        ]
        for plan in bad_plans:
            with self.subTest(plan=str(plan)[:50]):
                with self.assertRaises(RulesContractError) as caught:
                    await self.edit_with(plan)
                self.assertEqual(caught.exception.rule, "plan_edit")
                self.assertEqual(self.snapshot(), self.before)

    async def test_a_plan_for_the_wrong_version_writes_nothing(self):
        with self.assertRaises(RulesContractError):
            await self.edit_with(edit_plan(), expected=1)
        self.assertEqual(self.snapshot(), self.before)

    async def test_a_plan_for_a_deleted_memory_writes_nothing(self):
        gone = self.seed_memory(status="deprecated")
        before = self.snapshot()
        with self.assertRaises(RulesContractError):
            await self.edit_with(edit_plan(), expected=1, memory_id=gone)
        self.assertEqual(self.snapshot(), before)

    async def test_a_valid_plan_is_written_as_the_next_version(self):
        plan = edit_plan(
            fields=("content", "title"), content="Planned body", importance=9
        )
        edited = await self.edit_with(plan)
        versions = self.versions(self.memory_id)
        self.assertEqual(
            [v["status"] for v in versions], ["superseded", "superseded", "active"]
        )
        new = versions[2]
        self.assertEqual(
            (new["version_number"], new["title"], new["content"], new["importance"]),
            (3, "Planned", "Planned body", 9),
        )
        self.assertEqual(edited.version_number, 3)
        (relation,) = self.relations(self.memory_id)
        self.assertEqual(
            (
                relation["from_number"],
                relation["to_number"],
                relation["relation_type"],
                relation["reason"],
            ),
            (3, 2, "supersedes", "content, title"),
        )

    async def test_a_no_change_answer_writes_nothing(self):
        current = await self.edit_with(None)
        self.assertEqual(current.version_number, 2)
        self.assertEqual(self.snapshot(), self.before)

    async def test_a_status_change_only_happens_when_the_status_is_as_assumed(self):
        gone = self.seed_memory(status="deprecated")
        live = self.seed_memory(status="active")
        before = self.snapshot()
        with patch(f"{SERVICE}.check_deletable", return_value=None):
            with self.assertRaises(SharedMemoryBusyError):
                await self.service.delete_memory(self.admin, gone)
        with patch(f"{SERVICE}.check_restorable", return_value=None):
            with self.assertRaises(SharedMemoryBusyError):
                await self.service.restore_memory(self.admin, live)
        self.assertEqual(self.snapshot(), before)


@requires_postgres
class EffectiveViewGuardTest(AsyncPostgresSharedTestCase):
    async def test_the_result_of_the_precedence_rule_must_be_a_view(self):
        self.seed_memory()
        for value in (None, {"memories": []}, (), [1]):
            with self.subTest(value=str(value)):
                with patch(f"{SERVICE}.resolve_effective_view", return_value=value):
                    with self.assertRaises(RulesContractError) as caught:
                        await self.service.effective_view(self.user)
                self.assertEqual(caught.exception.rule, "resolve_effective_view")
