"""The backend's own identity can never propose a Shared Memory candidate.

Decision 0009 section 2: "Backend 自身の ID（``system`` role、Background Worker）は
提案できない". With the default policy the Authorizer denies ``memory.use`` to the
``system`` role, so that rule used to hold only as long as no policy granted it.
``propose_candidate`` now refuses the ``system`` role itself, whatever the
Authorizer answers, exactly as the managing methods already do
(``test_shared_memory_promotion_refused``); the Authorizer is still asked first,
so the decision is audited. Issue #90, finding on #77. Skipped unless
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
    CandidateProposal,
    OriginScope,
    SharedMemoryPermissionError,
)

from .shared_memory_support import (
    USER_ID,
    AsyncPostgresSharedTestCase,
    requires_postgres,
)


def proposal():
    return CandidateProposal(
        memory_type="rule",
        title="Share this",
        content="Everyone should know.",
        origin_scope=OriginScope.PROJECT,
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


@requires_postgres
class SystemRoleMayNotProposeTest(AsyncPostgresSharedTestCase):
    def granting_memory_use_to_system(self) -> Authorizer:
        grants = dict(DEFAULT_POLICY.system_grants)
        grants[SystemRole.SYSTEM] = frozenset({Capability.MEMORY_USE})
        return Authorizer(
            self.sink,
            policy=Policy(
                system_grants=grants, project_grants=DEFAULT_POLICY.project_grants
            ),
            directory=self.directory,
        )

    async def test_a_policy_that_grants_memory_use_to_the_system_role_changes_nothing(
        self,
    ):
        service = self.new_service(authorizer=self.granting_memory_use_to_system())
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await service.propose_candidate(self.system, proposal())
        self.assertEqual(caught.exception.reason, "system_role_may_not_propose")
        self.assertEqual(self.count("shared_memory_candidates"), 0)
        # The Authorizer was asked first: its decision is audited, as for the
        # managing methods.
        event = self.only_event()
        self.assertEqual(
            (event.action, event.decision, event.actor_id),
            ("memory.use", "allow", self.system.user_id),
        )

    async def test_an_authorizer_that_allows_everything_changes_nothing(self):
        allow = AllowEverything()
        service = self.new_service(authorizer=allow)
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await service.propose_candidate(self.system, proposal())
        self.assertEqual(caught.exception.reason, "system_role_may_not_propose")
        self.assertEqual(allow.asked, 1)
        self.assertEqual(self.count("shared_memory_candidates"), 0)

    async def test_the_same_policy_still_lets_a_user_propose(self):
        service = self.new_service(authorizer=self.granting_memory_use_to_system())
        candidate = await service.propose_candidate(self.user, proposal())
        self.assertEqual(candidate.proposer_user_id, USER_ID)
        self.assertEqual(self.count("shared_memory_candidates"), 1)

    async def test_the_default_policy_still_denies_the_system_role_by_the_authorizer(
        self,
    ):
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await self.service.propose_candidate(self.system, proposal())
        self.assertEqual(caught.exception.reason, "capability_not_granted")
        self.assertEqual(self.count("shared_memory_candidates"), 0)
