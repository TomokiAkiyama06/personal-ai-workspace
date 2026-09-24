"""Shared Memory Candidates: propose, review, approve, reject (real PostgreSQL).

Approving and rejecting go through ``lifecycle.next_candidate_state`` and
``draft_from_candidate``, so those tests fail while they are stubs. Proposing,
listing and the permission checks are implemented in the service. Skipped unless
``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

from paw_backend.authz import Capability
from paw_backend.memory.shared import (
    AutomaticPromotionRefusedError,
    CandidateDecision,
    CandidateLimitError,
    CandidateNotFoundError,
    CandidateProposal,
    CandidateState,
    InputProblem,
    InvalidSharedMemoryInputError,
    OriginScope,
    SharedMemoryCandidate,
    SharedMemoryNotFoundError,
    SharedMemoryPermissionError,
    SharedMemoryStateError,
    SharedMemoryStatus,
    StateProblem,
)

from .shared_memory_support import (
    ADMIN_ID,
    AGENT_ID,
    T0,
    USER_ID,
    AsyncPostgresSharedTestCase,
    agent_for,
    raise_unexpected,
    requires_postgres,
)


def proposal(**overrides):
    values = {
        "memory_type": "rule",
        "title": "Share this",
        "content": "Everyone should know.",
        "origin_scope": OriginScope.PROJECT,
    }
    values.update(overrides)
    return CandidateProposal(**values)


@requires_postgres
class ProposeTest(AsyncPostgresSharedTestCase):
    async def test_a_user_proposes_and_gets_a_pending_candidate(self):
        origin = uuid4()
        candidate = await self.service.propose_candidate(
            self.user,
            proposal(
                importance=65,
                policy_subjects=["b", "a"],
                origin_version_id=origin,
                reason="Useful",
            ),
        )
        self.assertEqual(
            candidate,
            SharedMemoryCandidate(
                candidate_id=candidate.candidate_id,
                state=CandidateState.PENDING,
                proposer_user_id=USER_ID,
                proposer_agent_id=None,
                origin_scope=OriginScope.PROJECT,
                origin_version_id=origin,
                memory_type="rule",
                title="Share this",
                content="Everyone should know.",
                importance=65,
                policy_subjects=("a", "b"),
                reason="Useful",
                created_at=T0,
                decided_by=None,
                decided_at=None,
                decision_reason=None,
                memory_id=None,
            ),
        )

    async def test_the_row_holds_the_proposal_and_no_decision(self):
        candidate = await self.service.propose_candidate(
            self.user, proposal(policy_subjects=["b", "a"], importance=0)
        )
        row = self.candidate_row(candidate.candidate_id)
        self.assertEqual(
            {k: row[k] for k in row if k != "id"},
            {
                "state": "pending",
                "proposer_user_id": USER_ID,
                "proposer_agent_id": None,
                "origin_scope": "project",
                "origin_version_id": None,
                "memory_type": "rule",
                "title": "Share this",
                "content": "Everyone should know.",
                "importance": 0,
                "policy_subjects": ["a", "b"],
                "reason": None,
                "created_at": T0,
                "decided_by": None,
                "decided_at": None,
                "decision_reason": None,
                "memory_id": None,
            },
        )

    async def test_every_active_user_role_may_propose(self):
        for who in (self.user, self.other_user, self.admin, self.owner):
            with self.subTest(role=who.system_role.value):
                candidate = await self.service.propose_candidate(who, proposal())
                self.assertEqual(candidate.proposer_user_id, who.user_id)

    async def test_proposing_creates_no_shared_memory(self):
        await self.service.propose_candidate(self.user, proposal())
        self.assertEqual(self.count("memories"), 0)
        self.assertEqual(self.count("memory_versions"), 0)
        self.assertEqual(
            await self.service.list_memories(self.owner, include_deleted=True), []
        )

    async def test_the_proposal_is_audited_as_the_users_own_memory_use(self):
        await self.service.propose_candidate(self.user, proposal())
        event = self.only_event()
        self.assertEqual(
            (
                event.action,
                event.decision,
                event.actor_id,
                event.resource_kind,
                event.agent_id,
            ),
            ("memory.use", "allow", USER_ID, "shared_memory_candidate", None),
        )

    async def test_the_backend_own_identity_may_not_propose(self):
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await self.service.propose_candidate(self.system, proposal())
        self.assertEqual(caught.exception.reason, "capability_not_granted")
        self.assertNotIsInstance(caught.exception, AutomaticPromotionRefusedError)
        self.assertEqual(self.count("shared_memory_candidates"), 0)
        event = self.only_event()
        self.assertEqual((event.action, event.decision), ("memory.use", "deny"))

    async def test_an_agent_proposes_for_its_delegating_user(self):
        agent = agent_for(self.user, Capability.MEMORY_USE)
        candidate = await self.service.propose_candidate(agent, proposal())
        self.assertEqual(
            (candidate.proposer_user_id, candidate.proposer_agent_id, candidate.state),
            (USER_ID, AGENT_ID, CandidateState.PENDING),
        )
        row = self.candidate_row(candidate.candidate_id)
        self.assertEqual(
            (row["proposer_user_id"], row["proposer_agent_id"]), (USER_ID, AGENT_ID)
        )
        event = self.only_event()
        self.assertEqual(
            (event.action, event.decision, event.agent_id, event.actor_id),
            ("memory.use", "allow", AGENT_ID, USER_ID),
        )

    async def test_an_agent_without_the_grant_may_not_propose(self):
        agent = agent_for(self.user, Capability.SHARED_MEMORY_READ)
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await self.service.propose_candidate(agent, proposal())
        self.assertEqual(caught.exception.reason, "agent_capability_not_granted")
        self.assertEqual(self.count("shared_memory_candidates"), 0)

    async def test_an_agent_of_an_inactive_user_may_not_propose(self):
        agent = agent_for(self.user, Capability.MEMORY_USE)
        del self.directory.principals[self.user.user_id]
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await self.service.propose_candidate(agent, proposal())
        self.assertEqual(caught.exception.reason, "delegator_not_active")

    async def test_a_candidate_is_not_visible_as_shared_memory_to_anyone(self):
        candidate = await self.service.propose_candidate(self.user, proposal())
        for who in (self.user, self.other_user, self.admin):
            self.assertEqual(await self.service.list_memories(who), [])
        with self.assertRaises(SharedMemoryNotFoundError):
            await self.service.get_memory(self.owner, candidate.candidate_id)

    async def test_the_proposal_must_be_a_proposal(self):
        for value in (None, {"title": "x"}, "x"):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(InvalidSharedMemoryInputError) as caught:
                    await self.service.propose_candidate(self.user, value)
                self.assertEqual(caught.exception.field, "proposal")
        self.assertEqual(self.events(), [])

    async def test_the_time_comes_from_the_clock(self):
        self.clock.advance(days=2)
        candidate = await self.service.propose_candidate(self.user, proposal())
        self.assertEqual(candidate.created_at, T0 + timedelta(days=2))


@requires_postgres
class PendingLimitTest(AsyncPostgresSharedTestCase):
    async def test_the_default_limit_is_fifty(self):
        for _ in range(50):
            self.seed_candidate()
        with self.assertRaises(CandidateLimitError):
            await self.service.propose_candidate(self.user, proposal())
        self.assertEqual(self.count("shared_memory_candidates"), 50)

    async def test_forty_nine_pending_still_allow_one_more(self):
        for _ in range(49):
            self.seed_candidate()
        await self.service.propose_candidate(self.user, proposal())
        self.assertEqual(self.count("shared_memory_candidates"), 50)

    async def test_the_limit_is_per_proposer(self):
        service = self.new_service(max_pending_candidates=2)
        for _ in range(2):
            await service.propose_candidate(self.user, proposal())
        with self.assertRaises(CandidateLimitError):
            await service.propose_candidate(self.user, proposal())
        await service.propose_candidate(self.other_user, proposal())

    async def test_an_agent_counts_against_its_delegating_user(self):
        service = self.new_service(max_pending_candidates=2)
        agent = agent_for(self.user, Capability.MEMORY_USE)
        await service.propose_candidate(agent, proposal())
        await service.propose_candidate(self.user, proposal())
        with self.assertRaises(CandidateLimitError):
            await service.propose_candidate(agent, proposal())

    async def test_decided_candidates_do_not_count(self):
        service = self.new_service(max_pending_candidates=2)
        self.seed_candidate(state="approved")
        self.seed_candidate(state="rejected")
        self.seed_candidate(state="rejected")
        await service.propose_candidate(self.user, proposal())
        await service.propose_candidate(self.user, proposal())
        with self.assertRaises(CandidateLimitError):
            await service.propose_candidate(self.user, proposal())

    async def test_concurrent_proposals_never_exceed_the_limit(self):
        services = [self.new_service(max_pending_candidates=3) for _ in range(3)]
        results = await asyncio.gather(
            *(
                services[n % 3].propose_candidate(self.user, proposal(title=f"P{n}"))
                for n in range(8)
            ),
            return_exceptions=True,
        )
        raise_unexpected(results, SharedMemoryCandidate, CandidateLimitError)
        accepted = [r for r in results if isinstance(r, SharedMemoryCandidate)]
        refused = [r for r in results if isinstance(r, CandidateLimitError)]
        self.assertEqual((len(accepted), len(refused)), (3, 5), results)
        self.assertEqual(self.count("shared_memory_candidates"), 3)


@requires_postgres
class ReviewTest(AsyncPostgresSharedTestCase):
    async def test_only_owner_and_admin_list_and_get_candidates(self):
        first = self.seed_candidate(title="First", created_at=T0)
        self.seed_candidate(title="Second", created_at=T0 + timedelta(seconds=1))
        for who in (self.owner, self.admin):
            with self.subTest(role=who.system_role.value):
                listed = await self.service.list_candidates(who)
                self.assertEqual([c.title for c in listed], ["First", "Second"])
                one = await self.service.get_candidate(who, first)
                self.assertEqual((one.candidate_id, one.title), (first, "First"))

    async def test_a_normal_user_sees_no_candidate_not_even_their_own(self):
        own = self.seed_candidate(proposer_user_id=USER_ID)
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await self.service.list_candidates(self.user)
        self.assertEqual(caught.exception.reason, "capability_not_granted")
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.get_candidate(self.user, own)
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.get_candidate(self.user, uuid4())

    async def test_a_listed_candidate_holds_every_field(self):
        origin = uuid4()
        memory_id = self.seed_memory()
        candidate_id = self.seed_candidate(
            state="approved",
            proposer_agent_id=AGENT_ID,
            origin_scope="repo",
            origin_version_id=origin,
            memory_type="team_rule",
            title="T",
            content="C",
            importance=12,
            policy_subjects=["a", "b"],
            reason="why",
            decided_by=ADMIN_ID,
            decided_at=T0 + timedelta(hours=1),
            decision_reason="ok",
            memory_id=memory_id,
        )
        (found,) = await self.service.list_candidates(self.owner)
        self.assertEqual(
            found,
            SharedMemoryCandidate(
                candidate_id=candidate_id,
                state=CandidateState.APPROVED,
                proposer_user_id=USER_ID,
                proposer_agent_id=AGENT_ID,
                origin_scope=OriginScope.REPO,
                origin_version_id=origin,
                memory_type="team_rule",
                title="T",
                content="C",
                importance=12,
                policy_subjects=("a", "b"),
                reason="why",
                created_at=T0,
                decided_by=ADMIN_ID,
                decided_at=T0 + timedelta(hours=1),
                decision_reason="ok",
                memory_id=memory_id,
            ),
        )

    async def test_the_state_filter(self):
        self.seed_candidate(title="P")
        self.seed_candidate(title="A", state="approved")
        self.seed_candidate(title="R", state="rejected")
        for state, titles in (
            (CandidateState.PENDING, ["P"]),
            (CandidateState.APPROVED, ["A"]),
            (CandidateState.REJECTED, ["R"]),
        ):
            with self.subTest(state=state.value):
                listed = await self.service.list_candidates(self.owner, state=state)
                self.assertEqual([c.title for c in listed], titles)
        self.assertEqual(len(await self.service.list_candidates(self.owner)), 3)

    async def test_a_state_given_as_a_string_is_refused(self):
        with self.assertRaises(InvalidSharedMemoryInputError) as caught:
            await self.service.list_candidates(self.owner, state="pending")
        self.assertEqual(
            (caught.exception.field, caught.exception.problem),
            ("state", InputProblem.WRONG_TYPE),
        )

    async def test_order_and_pages(self):
        for n in range(5):
            self.seed_candidate(title=f"c{n}", created_at=T0 + timedelta(seconds=n))
        page = await self.service.list_candidates(self.owner, limit=2, offset=2)
        self.assertEqual([c.title for c in page], ["c2", "c3"])
        same_instant = [
            self.seed_candidate(title="s", created_at=T0 - timedelta(days=1))
            for _ in range(12)
        ]
        first = await self.service.list_candidates(self.owner, limit=12)
        self.assertEqual([c.candidate_id for c in first], sorted(same_instant))

    async def test_the_page_arguments_are_validated(self):
        for kwargs in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"limit": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(InvalidSharedMemoryInputError):
                    await self.service.list_candidates(self.owner, **kwargs)

    async def test_a_missing_candidate_is_not_found(self):
        with self.assertRaises(CandidateNotFoundError):
            await self.service.get_candidate(self.owner, uuid4())

    async def test_the_id_must_be_a_uuid_object(self):
        for value in (str(uuid4()), None, 4):
            with self.subTest(value=value):
                with self.assertRaises(InvalidSharedMemoryInputError) as caught:
                    await self.service.get_candidate(self.owner, value)
                self.assertEqual(caught.exception.field, "candidate_id")

    async def test_the_review_is_audited(self):
        candidate_id = self.seed_candidate()
        await self.service.get_candidate(self.admin, candidate_id)
        event = self.only_event()
        self.assertEqual(
            (event.action, event.decision, event.resource_kind, event.resource_id),
            ("shared_memory.manage", "allow", "shared_memory_candidate", candidate_id),
        )


@requires_postgres
class ApproveTest(AsyncPostgresSharedTestCase):
    def seed(self, **overrides):
        values = {
            "title": "Share this",
            "content": "Everyone should know.",
            "memory_type": "rule",
            "importance": 65,
            "policy_subjects": ["a", "b"],
            "reason": "Useful",
            "origin_scope": "project",
        }
        values.update(overrides)
        return self.seed_candidate(**values)

    async def test_approval_creates_the_shared_memory_and_marks_the_candidate(self):
        candidate_id = self.seed()
        self.clock.advance(hours=1)

        decision = await self.service.approve_candidate(
            self.admin, candidate_id, reason="Good"
        )

        self.assertIsInstance(decision, CandidateDecision)
        memory = decision.memory
        self.assertEqual(
            (memory.title, memory.content, memory.memory_type, memory.importance),
            ("Share this", "Everyone should know.", "rule", 65),
        )
        self.assertEqual(memory.policy_subjects, ("a", "b"))
        self.assertEqual(
            (
                memory.version_number,
                memory.status,
                memory.created_at,
                memory.updated_at,
            ),
            (
                1,
                SharedMemoryStatus.ACTIVE,
                T0 + timedelta(hours=1),
                T0 + timedelta(hours=1),
            ),
        )
        approved = decision.candidate
        self.assertEqual(
            (
                approved.state,
                approved.decided_by,
                approved.decided_at,
                approved.decision_reason,
                approved.memory_id,
            ),
            (
                CandidateState.APPROVED,
                ADMIN_ID,
                T0 + timedelta(hours=1),
                "Good",
                memory.memory_id,
            ),
        )
        self.assertEqual(
            (
                approved.candidate_id,
                approved.title,
                approved.proposer_user_id,
                approved.reason,
            ),
            (candidate_id, "Share this", USER_ID, "Useful"),
        )
        row = self.candidate_row(candidate_id)
        self.assertEqual(
            (row["state"], row["decided_by"], row["decision_reason"], row["memory_id"]),
            ("approved", ADMIN_ID, "Good", memory.memory_id),
        )

    async def test_the_new_version_is_a_confirmed_shared_version_by_the_approver(self):
        candidate_id = self.seed()
        decision = await self.service.approve_candidate(self.owner, candidate_id)
        (version,) = self.versions(decision.memory.memory_id)
        self.assertEqual(
            {
                k: version[k]
                for k in (
                    "scope",
                    "owner_user_id",
                    "project_id",
                    "status",
                    "confirmation_state",
                    "freshness_policy",
                    "actor_type",
                    "actor_user_id",
                    "change_reason",
                    "attributes",
                    "version_number",
                )
            },
            {
                "scope": "shared",
                "owner_user_id": None,
                "project_id": None,
                "status": "active",
                "confirmation_state": "confirmed",
                "freshness_policy": "permanent",
                "actor_type": "user",
                "actor_user_id": self.owner.user_id,
                "change_reason": "Useful",
                "attributes": {"policy_subjects": ["a", "b"]},
                "version_number": 1,
            },
        )

    async def test_the_new_memory_records_the_approval_as_its_source(self):
        candidate_id = self.seed()
        decision = await self.service.approve_candidate(self.admin, candidate_id)
        (source,) = self.sources(decision.memory.memory_id)
        self.assertEqual(
            (
                source["source_type"],
                source["source_ref"],
                source["conversation_id"],
                source["message_id"],
                source["source_deleted_at"],
            ),
            (
                "user_confirmation",
                f"shared_memory_candidate:{candidate_id}",
                None,
                None,
                None,
            ),
        )

    async def test_the_approved_memory_is_readable_by_every_user(self):
        candidate_id = self.seed()
        decision = await self.service.approve_candidate(self.admin, candidate_id)
        for who in (self.user, self.other_user):
            found = await self.service.get_memory(who, decision.memory.memory_id)
            self.assertEqual(found, decision.memory)

    async def test_without_a_reason_the_decision_has_none(self):
        candidate_id = self.seed(reason=None, policy_subjects=[])
        decision = await self.service.approve_candidate(self.admin, candidate_id)
        self.assertIsNone(decision.candidate.decision_reason)
        (version,) = self.versions(decision.memory.memory_id)
        self.assertEqual((version["change_reason"], version["attributes"]), (None, {}))

    async def test_approving_twice_is_a_state_error_and_creates_one_memory(self):
        candidate_id = self.seed()
        await self.service.approve_candidate(self.admin, candidate_id)
        before = self.snapshot()
        with self.assertRaises(SharedMemoryStateError) as caught:
            await self.service.approve_candidate(self.owner, candidate_id)
        self.assertIs(caught.exception.problem, StateProblem.CANDIDATE_NOT_PENDING)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.count("memories"), 1)

    async def test_a_rejected_candidate_cannot_be_approved(self):
        candidate_id = self.seed(state="rejected")
        with self.assertRaises(SharedMemoryStateError) as caught:
            await self.service.approve_candidate(self.admin, candidate_id)
        self.assertIs(caught.exception.problem, StateProblem.CANDIDATE_NOT_PENDING)
        self.assertEqual(self.count("memories"), 0)
        self.assertEqual(self.candidate_row(candidate_id)["state"], "rejected")

    async def test_an_unknown_candidate_is_not_found_and_nothing_is_written(self):
        with self.assertRaises(CandidateNotFoundError):
            await self.service.approve_candidate(self.admin, uuid4())
        self.assertEqual(self.count("memories"), 0)

    async def test_the_reason_is_validated_before_anything_happens(self):
        candidate_id = self.seed()
        cases = [
            ("a" * 501, InputProblem.TOO_LONG),
            ("", InputProblem.BLANK),
            ("   ", InputProblem.BLANK),
            (5, InputProblem.WRONG_TYPE),
        ]
        for reason, problem in cases:
            with self.subTest(reason=str(reason)[:5]):
                with self.assertRaises(InvalidSharedMemoryInputError) as caught:
                    await self.service.approve_candidate(
                        self.admin, candidate_id, reason=reason
                    )
                self.assertEqual(
                    (caught.exception.field, caught.exception.problem),
                    ("reason", problem),
                )
        self.assertEqual(self.events(), [])
        self.assertEqual(self.candidate_row(candidate_id)["state"], "pending")
        await self.service.approve_candidate(self.admin, candidate_id, reason="a" * 500)

    async def test_the_approval_is_audited_with_the_candidate_id(self):
        candidate_id = self.seed()
        await self.service.approve_candidate(self.admin, candidate_id)
        event = self.only_event()
        self.assertEqual(
            (
                event.action,
                event.decision,
                event.resource_kind,
                event.resource_id,
                event.actor_role,
            ),
            (
                "shared_memory.candidate.approve",
                "allow",
                "shared_memory_candidate",
                candidate_id,
                "admin",
            ),
        )

    async def test_a_normal_user_cannot_approve_and_the_candidate_stays_pending(self):
        candidate_id = self.seed(proposer_user_id=USER_ID)
        before = self.snapshot()
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await self.service.approve_candidate(self.user, candidate_id)
        self.assertEqual(caught.exception.reason, "capability_not_granted")
        self.assertEqual(self.snapshot(), before)


@requires_postgres
class RejectTest(AsyncPostgresSharedTestCase):
    async def test_a_rejection_marks_the_candidate_and_creates_nothing(self):
        candidate_id = self.seed_candidate()
        self.clock.advance(minutes=30)

        decision = await self.service.reject_candidate(
            self.admin, candidate_id, reason="Too specific"
        )

        self.assertIsNone(decision.memory)
        rejected = decision.candidate
        self.assertEqual(
            (
                rejected.state,
                rejected.decided_by,
                rejected.decided_at,
                rejected.decision_reason,
                rejected.memory_id,
            ),
            (
                CandidateState.REJECTED,
                ADMIN_ID,
                T0 + timedelta(minutes=30),
                "Too specific",
                None,
            ),
        )
        row = self.candidate_row(candidate_id)
        self.assertEqual((row["state"], row["memory_id"]), ("rejected", None))
        self.assertEqual(self.count("memories"), 0)
        self.assertEqual(self.count("memory_sources"), 0)

    async def test_a_rejection_without_a_reason(self):
        candidate_id = self.seed_candidate()
        decision = await self.service.reject_candidate(self.owner, candidate_id)
        self.assertIsNone(decision.candidate.decision_reason)
        self.assertEqual(decision.candidate.decided_by, self.owner.user_id)

    async def test_rejecting_twice_or_after_approval_is_a_state_error(self):
        for state in ("rejected", "approved"):
            with self.subTest(state=state):
                candidate_id = self.seed_candidate(state=state)
                before = self.candidate_row(candidate_id)
                with self.assertRaises(SharedMemoryStateError) as caught:
                    await self.service.reject_candidate(self.admin, candidate_id)
                self.assertIs(
                    caught.exception.problem, StateProblem.CANDIDATE_NOT_PENDING
                )
                self.assertEqual(self.candidate_row(candidate_id), before)

    async def test_an_unknown_candidate_is_not_found(self):
        with self.assertRaises(CandidateNotFoundError):
            await self.service.reject_candidate(self.admin, uuid4())

    async def test_a_normal_user_cannot_reject(self):
        candidate_id = self.seed_candidate(proposer_user_id=USER_ID)
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.reject_candidate(self.user, candidate_id)
        self.assertEqual(self.candidate_row(candidate_id)["state"], "pending")

    async def test_the_reason_is_validated(self):
        candidate_id = self.seed_candidate()
        with self.assertRaises(InvalidSharedMemoryInputError):
            await self.service.reject_candidate(
                self.admin, candidate_id, reason="x" * 501
            )
        self.assertEqual(self.candidate_row(candidate_id)["state"], "pending")


@requires_postgres
class DecisionRaceTest(AsyncPostgresSharedTestCase):
    async def test_two_approvals_create_exactly_one_memory(self):
        candidate_id = self.seed_candidate()
        services = [self.new_service() for _ in range(4)]
        results = await asyncio.gather(
            *(s.approve_candidate(self.admin, candidate_id) for s in services),
            return_exceptions=True,
        )
        raise_unexpected(results, CandidateDecision, SharedMemoryStateError)
        winners = [r for r in results if isinstance(r, CandidateDecision)]
        losers = [r for r in results if isinstance(r, SharedMemoryStateError)]
        self.assertEqual((len(winners), len(losers)), (1, 3), results)
        self.assertEqual(self.count("memories"), 1)
        self.assertEqual(self.count("memory_versions"), 1)
        self.assertEqual(self.count("memory_sources"), 1)
        self.assertEqual(
            self.candidate_row(candidate_id)["memory_id"], winners[0].memory.memory_id
        )

    async def test_an_approval_racing_a_rejection_has_one_outcome(self):
        for _ in range(3):
            candidate_id = self.seed_candidate()
            other = self.new_service()
            results = await asyncio.gather(
                self.service.approve_candidate(self.admin, candidate_id),
                other.reject_candidate(self.owner, candidate_id),
                return_exceptions=True,
            )
            raise_unexpected(results, CandidateDecision, SharedMemoryStateError)
            decisions = [r for r in results if isinstance(r, CandidateDecision)]
            errors = [r for r in results if isinstance(r, SharedMemoryStateError)]
            self.assertEqual((len(decisions), len(errors)), (1, 1), results)
            row = self.candidate_row(candidate_id)
            if row["state"] == "approved":
                self.assertIsNotNone(row["memory_id"])
                self.assertEqual(len(self.versions(row["memory_id"])), 1)
            else:
                self.assertEqual((row["state"], row["memory_id"]), ("rejected", None))
        approved = self.count_where("approved")
        self.assertEqual(self.count("memories"), approved)

    def count_where(self, state):
        return self.rows(
            "SELECT count(*) AS n FROM shared_memory_candidates WHERE state = :s",
            s=state,
        )[0]["n"]

    async def test_a_decided_candidate_never_changes_again(self):
        candidate_id = self.seed_candidate()
        first = await self.service.reject_candidate(
            self.admin, candidate_id, reason="No"
        )
        before = self.candidate_row(candidate_id)
        with self.assertRaises(SharedMemoryStateError):
            await self.service.approve_candidate(self.owner, candidate_id)
        with self.assertRaises(SharedMemoryStateError):
            await self.service.reject_candidate(
                self.owner, candidate_id, reason="Again"
            )
        self.assertEqual(self.candidate_row(candidate_id), before)
        self.assertEqual(first.candidate.decided_by, ADMIN_ID)


@requires_postgres
class AuditFailureTest(AsyncPostgresSharedTestCase):
    async def test_when_the_audit_cannot_be_written_the_candidate_stays_pending(self):
        from paw_backend.authz import Authorizer

        from .authz_support import FailingSink

        candidate_id = self.seed_candidate()
        service = self.new_service(
            authorizer=Authorizer(FailingSink(), directory=self.directory)
        )
        before = self.snapshot()
        with self.assertLogs("paw_backend.authz.authorizer", level="ERROR"):
            with self.assertRaises(SharedMemoryPermissionError) as caught:
                await service.approve_candidate(self.admin, candidate_id)
        self.assertEqual(caught.exception.reason, "audit_unavailable")
        self.assertEqual(self.snapshot(), before)
