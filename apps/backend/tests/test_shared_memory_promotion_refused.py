"""No automatic promotion: Shared Memory is managed by a human Owner or Admin only.

REQUIREMENTS.md: "Agent: Shared Memoryへ自動昇格しない"; "AgentによるWorkspace-wide
auto-promotionはBackend Policyで拒否する". Every method that creates, changes or
decides Shared Memory is tried with every kind of actor that must not manage it,
against the real ``Authorizer`` and against authorizers that would allow it, and
each attempt must leave the database exactly as it was. Skipped unless
``PAW_TEST_DATABASE_URL`` is set.
"""

from paw_backend.authz import (
    DEFAULT_POLICY,
    Authorizer,
    Capability,
    Decision,
    Policy,
    Reason,
    SystemRole,
)
from paw_backend.memory.shared import (
    AutomaticPromotionRefusedError,
    CandidateProposal,
    CandidateState,
    OriginScope,
    SharedMemoryChanges,
    SharedMemoryPermissionError,
)

from .shared_memory_support import (
    ADMIN_ID,
    AGENT_ID,
    AsyncPostgresSharedTestCase,
    agent_for,
    draft,
    requires_postgres,
)

MANAGE_METHODS = (
    "create_memory",
    "edit_memory",
    "delete_memory",
    "restore_memory",
    "approve_candidate",
    "reject_candidate",
    "list_candidates",
    "get_candidate",
)


class AllowEverything:
    """An authorizer that says yes to everything (and counts how often it is asked)."""

    def __init__(self) -> None:
        self.asked = 0

    async def authorize(self, principal, capability, resource, **kwargs):
        self.asked += 1
        return Decision.allow(Reason.GRANTED_BY_SYSTEM_ROLE, capability)

    async def authorize_agent_action(
        self, delegator, grant, capability, resource, **kwargs
    ):
        self.asked += 1
        return Decision.allow(Reason.GRANTED_BY_SYSTEM_ROLE, capability)


class AnswersTrue:
    """A broken authorizer: it returns a bare ``True``, not a ``Decision``."""

    async def authorize(self, *args, **kwargs):
        return True

    async def authorize_agent_action(self, *args, **kwargs):
        return True


class ManageCallsMixin:
    """The eight managing calls, each with valid arguments for the current rows."""

    def reseed(self):
        self.live = self.seed_memory(title="Live")
        self.gone = self.seed_memory(title="Gone", status="deprecated")
        self.pending = self.seed_candidate()

    def call(self, name, service, actor):
        arguments = {
            "create_memory": (draft(),),
            "edit_memory": (self.live, 1, SharedMemoryChanges(title="Edited")),
            "delete_memory": (self.live,),
            "restore_memory": (self.gone,),
            "approve_candidate": (self.pending,),
            "reject_candidate": (self.pending,),
            "list_candidates": (),
            "get_candidate": (self.pending,),
        }[name]
        return getattr(service, name)(actor, *arguments)


@requires_postgres
class AgentsNeverManageTest(ManageCallsMixin, AsyncPostgresSharedTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.reseed()

    def agents(self):
        every = (
            Capability.SHARED_MEMORY_MANAGE,
            Capability.SHARED_MEMORY_READ,
            Capability.MEMORY_USE,
        )
        return {
            "agent of the owner": (
                agent_for(self.owner, *every),
                "agent_capability_forbidden",
            ),
            "agent of an admin": (
                agent_for(self.admin, *every),
                "agent_capability_forbidden",
            ),
            "agent of a user": (
                agent_for(self.user, *every),
                "capability_not_granted",
            ),
            "agent with an empty grant": (
                agent_for(self.owner),
                "agent_capability_forbidden",
            ),
        }

    async def test_every_managing_call_is_refused_for_every_agent(self):
        for name in MANAGE_METHODS:
            for label, (agent, reason) in self.agents().items():
                with self.subTest(method=name, actor=label):
                    self.sink.events.clear()
                    before = self.snapshot()
                    with self.assertRaises(AutomaticPromotionRefusedError) as caught:
                        await self.call(name, self.service, agent)
                    self.assertEqual(caught.exception.reason, reason)
                    self.assertEqual(self.snapshot(), before)
                    event = self.only_event()
                    self.assertEqual(
                        (event.action, event.decision, event.reason, event.agent_id),
                        ("shared_memory.manage", "deny", reason, AGENT_ID),
                    )

    async def test_an_agent_can_propose_but_the_candidate_stays_pending(self):
        agent = agent_for(
            self.owner, Capability.MEMORY_USE, Capability.SHARED_MEMORY_MANAGE
        )
        candidate = await self.service.propose_candidate(
            agent,
            CandidateProposal(
                memory_type="rule",
                title="Proposed by an agent",
                content="Body",
                origin_scope=OriginScope.USER,
            ),
        )
        with self.assertRaises(AutomaticPromotionRefusedError):
            await self.service.approve_candidate(agent, candidate.candidate_id)
        row = self.candidate_row(candidate.candidate_id)
        self.assertEqual(
            (
                row["state"],
                row["decided_by"],
                row["memory_id"],
                row["proposer_agent_id"],
            ),
            ("pending", None, None, AGENT_ID),
        )
        self.assertEqual(self.memory_ids(), {self.live, self.gone})  # nothing new

    async def test_the_shared_memory_exists_only_after_a_human_approves(self):
        agent = agent_for(self.owner, Capability.MEMORY_USE)
        candidate = await self.service.propose_candidate(
            agent,
            CandidateProposal(
                memory_type="rule",
                title="Promote me",
                content="Body",
                origin_scope=OriginScope.PROJECT,
            ),
        )
        before = self.memory_ids()

        decision = await self.service.approve_candidate(
            self.admin, candidate.candidate_id
        )

        (version,) = self.versions(decision.memory.memory_id)
        self.assertEqual(self.memory_ids() - before, {decision.memory.memory_id})
        self.assertEqual(
            version["actor_user_id"], ADMIN_ID
        )  # the decider, not the agent
        self.assertEqual(decision.candidate.proposer_agent_id, AGENT_ID)
        self.assertEqual(decision.candidate.decided_by, ADMIN_ID)


@requires_postgres
class TheBackendIdentityNeverManagesTest(ManageCallsMixin, AsyncPostgresSharedTestCase):
    async def test_every_managing_call_is_refused_for_the_system_role(self):
        for name in MANAGE_METHODS:
            with self.subTest(method=name):
                self.reseed()
                self.sink.events.clear()
                before = self.snapshot()
                with self.assertRaises(AutomaticPromotionRefusedError) as caught:
                    await self.call(name, self.service, self.system)
                self.assertEqual(caught.exception.reason, "capability_not_granted")
                self.assertEqual(self.snapshot(), before)
                event = self.only_event()
                self.assertEqual(
                    (event.decision, event.actor_role, event.action),
                    ("deny", "system", "shared_memory.manage"),
                )


@requires_postgres
class OrdinaryUsersNeverManageTest(ManageCallsMixin, AsyncPostgresSharedTestCase):
    async def test_every_managing_call_is_refused_for_a_user(self):
        for name in MANAGE_METHODS:
            with self.subTest(method=name):
                self.reseed()
                before = self.snapshot()
                with self.assertRaises(SharedMemoryPermissionError) as caught:
                    await self.call(name, self.service, self.user)
                self.assertNotIsInstance(
                    caught.exception, AutomaticPromotionRefusedError
                )
                self.assertEqual(caught.exception.reason, "capability_not_granted")
                self.assertEqual(self.snapshot(), before)

    async def test_owner_and_admin_can_make_every_managing_call(self):
        for who in (self.owner, self.admin):
            for name in MANAGE_METHODS:
                with self.subTest(role=who.system_role.value, method=name):
                    self.reseed()
                    await self.call(name, self.service, who)


@requires_postgres
class TheServiceDoesNotTrustTheAuthorizerAloneTest(
    ManageCallsMixin, AsyncPostgresSharedTestCase
):
    """Even an authorizer that allows everything cannot make an agent promote."""

    async def test_an_agent_is_refused_although_the_authorizer_allows(self):
        allow = AllowEverything()
        service = self.new_service(authorizer=allow)
        agent = agent_for(self.owner, Capability.SHARED_MEMORY_MANAGE)
        for name in MANAGE_METHODS:
            with self.subTest(method=name):
                self.reseed()
                before = self.snapshot()
                asked = allow.asked
                with self.assertRaises(AutomaticPromotionRefusedError) as caught:
                    await self.call(name, service, agent)
                self.assertEqual(caught.exception.reason, "not_a_human_owner_or_admin")
                self.assertEqual(allow.asked, asked + 1)  # asked exactly once
                self.assertEqual(self.snapshot(), before)

    async def test_the_system_role_is_refused_although_the_authorizer_allows(self):
        service = self.new_service(authorizer=AllowEverything())
        for name in MANAGE_METHODS:
            with self.subTest(method=name):
                self.reseed()
                before = self.snapshot()
                with self.assertRaises(AutomaticPromotionRefusedError):
                    await self.call(name, service, self.system)
                self.assertEqual(self.snapshot(), before)

    async def test_a_user_is_refused_although_the_authorizer_allows(self):
        service = self.new_service(authorizer=AllowEverything())
        for name in MANAGE_METHODS:
            with self.subTest(method=name):
                self.reseed()
                before = self.snapshot()
                with self.assertRaises(SharedMemoryPermissionError) as caught:
                    await self.call(name, service, self.user)
                self.assertNotIsInstance(
                    caught.exception, AutomaticPromotionRefusedError
                )
                self.assertEqual(caught.exception.reason, "not_a_human_owner_or_admin")
                self.assertEqual(self.snapshot(), before)

    async def test_owner_and_admin_still_work_with_that_authorizer(self):
        service = self.new_service(authorizer=AllowEverything())
        self.reseed()
        created = await service.create_memory(self.owner, draft(title="Fine"))
        self.assertEqual(created.title, "Fine")

    async def test_a_policy_that_grants_management_to_the_system_role_changes_nothing(
        self,
    ):
        grants = dict(DEFAULT_POLICY.system_grants)
        grants[SystemRole.SYSTEM] = frozenset({Capability.SHARED_MEMORY_MANAGE})
        grants[SystemRole.USER] = grants[SystemRole.USER] | {
            Capability.SHARED_MEMORY_MANAGE
        }
        permissive = Authorizer(
            self.sink,
            policy=Policy(
                system_grants=grants, project_grants=DEFAULT_POLICY.project_grants
            ),
            directory=self.directory,
        )
        service = self.new_service(authorizer=permissive)
        for name in MANAGE_METHODS:
            with self.subTest(method=name):
                self.reseed()
                before = self.snapshot()
                with self.assertRaises(AutomaticPromotionRefusedError):
                    await self.call(name, service, self.system)
                with self.assertRaises(SharedMemoryPermissionError) as caught:
                    await self.call(name, service, self.user)
                self.assertEqual(caught.exception.reason, "not_a_human_owner_or_admin")
                self.assertEqual(self.snapshot(), before)

    async def test_an_answer_that_is_not_a_decision_is_a_refusal(self):
        service = self.new_service(authorizer=AnswersTrue())
        self.reseed()
        before = self.snapshot()
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await service.create_memory(self.owner, draft())
        self.assertEqual(caught.exception.reason, "invalid_decision")
        with self.assertRaises(SharedMemoryPermissionError):
            await service.list_memories(self.user)
        with self.assertRaises(SharedMemoryPermissionError):
            await service.propose_candidate(
                self.user,
                CandidateProposal(
                    memory_type="rule",
                    title="T",
                    content="C",
                    origin_scope=OriginScope.USER,
                ),
            )
        self.assertEqual(self.snapshot(), before)

    async def test_no_candidate_is_ever_decided_by_anyone_but_a_human_manager(self):
        self.reseed()
        actors = [
            agent_for(self.owner, Capability.SHARED_MEMORY_MANAGE),
            self.system,
            self.user,
            self.other_user,
        ]
        service = self.new_service(authorizer=AllowEverything())
        for actor in actors:
            for method in ("approve_candidate", "reject_candidate"):
                with self.assertRaises(SharedMemoryPermissionError):
                    await getattr(service, method)(actor, self.pending)
        row = self.candidate_row(self.pending)
        self.assertEqual(
            (row["state"], row["decided_by"], row["decided_at"]),
            (CandidateState.PENDING.value, None, None),
        )
        self.assertEqual(self.memory_ids(), {self.live, self.gone})
