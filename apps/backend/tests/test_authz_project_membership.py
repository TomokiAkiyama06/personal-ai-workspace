"""The project.create / project.invitation.respond / project.leave capabilities.

Decision 0022 (Approved 2026-09-26) adds them.

The three capabilities give the Authorizer a say in creating a project, answering
one's own invitation and leaving a project (Decision 0008 had no capability for
them). The inventory tests in ``test_authz_policy.py`` pin the literal matrix;
this module pins what is specific to these three: who holds them, that a role in
a project or the project's state never changes the answer, that an agent can
never exercise them, and what the audit rows look like.
"""

import unittest
import uuid
from datetime import UTC, datetime

from paw_backend.authz import (
    ALL_PROJECTS,
    CAPABILITIES,
    AgentGrant,
    AuditMode,
    Authorizer,
    Capability,
    InMemoryAuditSink,
    ProjectRole,
    ProjectState,
    Reason,
    RepoAcl,
    Resource,
    Scope,
    SystemRole,
)
from paw_backend.authz.policy import decide, decide_agent

from .authz_support import AGENT, P1, P2, REPO, U1, U2, FailingSink, StaticDirectory
from .authz_support import principal as make_principal

CREATE = Capability.PROJECT_CREATE
RESPOND = Capability.PROJECT_INVITATION_RESPOND
LEAVE = Capability.PROJECT_LEAVE
THREE = (CREATE, RESPOND, LEAVE)
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
LOGGER = "paw_backend.authz.authorizer"


def own_row(capability: Capability, owner: uuid.UUID, project_id=P1):
    """The resource the service builds for ``capability`` (the actor's own row)."""
    match capability:
        case Capability.PROJECT_CREATE:
            return Resource.system()
        case Capability.PROJECT_INVITATION_RESPOND:
            kind = "project_invitation"
        case _:
            kind = "project_membership"
    return Resource(kind=kind, project_id=project_id, owner_id=owner)


class InventoryTest(unittest.TestCase):
    def test_the_names_are_exactly_the_proposed_ones(self):
        self.assertEqual(
            [c.value for c in THREE],
            ["project.create", "project.invitation.respond", "project.leave"],
        )

    def test_scope_delegation_and_audit_mode_of_each(self):
        expected = {
            CREATE: (Scope.SYSTEM, False, AuditMode.REQUIRED),
            RESPOND: (Scope.SELF, False, AuditMode.REQUIRED),
            LEAVE: (Scope.SELF, False, AuditMode.REQUIRED),
        }
        for capability, values in expected.items():
            with self.subTest(capability=capability.value):
                info = CAPABILITIES[capability]
                self.assertEqual((info.scope, info.delegable, info.audit), values)


class WhoHoldsThemTest(unittest.TestCase):
    """Every human role holds all three; the internal identity holds none."""

    def test_every_role_by_capability(self):
        expected = {
            SystemRole.OWNER: True,
            SystemRole.ADMIN: True,
            SystemRole.USER: True,
            SystemRole.SYSTEM: False,
        }
        for role, allowed in expected.items():
            for capability in THREE:
                with self.subTest(role=role.value, capability=capability.value):
                    who = make_principal(role)
                    decision = decide(who, capability, own_row(capability, who.user_id))
                    self.assertIs(decision.allowed, allowed)
                    self.assertIs(decision.capability, capability)
                    if not allowed:
                        self.assertIs(decision.reason, Reason.CAPABILITY_NOT_GRANTED)

    def test_the_reason_of_an_allowance_names_how_it_was_granted(self):
        who = make_principal(SystemRole.USER)
        self.assertIs(
            decide(who, CREATE, Resource.system()).reason,
            Reason.GRANTED_BY_SYSTEM_ROLE,
        )
        for capability in (RESPOND, LEAVE):
            with self.subTest(capability=capability.value):
                self.assertIs(
                    decide(who, capability, own_row(capability, who.user_id)).reason,
                    Reason.GRANTED_TO_RESOURCE_OWNER,
                )

    def test_a_role_in_a_project_grants_nothing_to_the_internal_identity(self):
        # Holding a Manager role somewhere is not a system grant.
        system = make_principal(SystemRole.SYSTEM, projects={P1: ProjectRole.MANAGER})
        for capability in THREE:
            with self.subTest(capability=capability.value):
                decision = decide(
                    system, capability, own_row(capability, system.user_id)
                )
                self.assertFalse(decision.allowed)
                self.assertIs(decision.reason, Reason.CAPABILITY_NOT_GRANTED)

    def test_the_project_role_and_the_project_state_never_change_the_answer(self):
        # Leaving must work in every state (Decision 0008), for every role, and a
        # user who is not a member is still *authorized*: whether a membership
        # exists is the service's transaction, not the policy.
        for role in (None, *ProjectRole):
            for state in ProjectState:
                for capability in (RESPOND, LEAVE):
                    with self.subTest(
                        role=getattr(role, "value", None),
                        state=state.value,
                        capability=capability.value,
                    ):
                        who = make_principal(
                            SystemRole.USER, projects={} if role is None else {P1: role}
                        )
                        resource = Resource(
                            kind="project_membership",
                            id=P1,
                            project_id=P1,
                            owner_id=who.user_id,
                            project_state=state,
                        )
                        self.assertTrue(decide(who, capability, resource).allowed)


class ResourceShapeTest(unittest.TestCase):
    def test_another_users_row_is_refused_even_for_the_owner_role(self):
        for role in SystemRole:
            for capability in (RESPOND, LEAVE):
                with self.subTest(role=role.value, capability=capability.value):
                    who = make_principal(role, user_id=U1)
                    decision = decide(who, capability, own_row(capability, U2))
                    self.assertFalse(decision.allowed)
                    expected = (
                        Reason.CAPABILITY_NOT_GRANTED
                        if role is SystemRole.SYSTEM
                        else Reason.NOT_RESOURCE_OWNER
                    )
                    self.assertIs(decision.reason, expected)

    def test_a_row_without_an_owner_is_invalid_not_allowed(self):
        who = make_principal(SystemRole.OWNER)
        for capability in (RESPOND, LEAVE):
            with self.subTest(capability=capability.value):
                resource = Resource(kind="project_membership", project_id=P1)
                decision = decide(who, capability, resource)
                self.assertFalse(decision.allowed)
                self.assertIs(decision.reason, Reason.INVALID_RESOURCE)

    def test_a_repository_is_never_the_resource_of_these_capabilities(self):
        who = make_principal(SystemRole.USER)
        acl = RepoAcl.inherit(REPO, P1)
        for capability in THREE:
            with self.subTest(capability=capability.value):
                resource = Resource(
                    kind="repository",
                    id=REPO,
                    project_id=P1,
                    owner_id=who.user_id,
                    repo_id=REPO,
                    project_state=ProjectState.ACTIVE,
                    repo_acl=acl,
                )
                decision = decide(who, capability, resource)
                self.assertFalse(decision.allowed)
                self.assertIs(decision.reason, Reason.INVALID_RESOURCE)

    def test_no_principal_and_no_resource_are_denials(self):
        for capability in THREE:
            with self.subTest(capability=capability.value):
                self.assertIs(
                    decide(None, capability, Resource.system()).reason,
                    Reason.UNAUTHENTICATED,
                )
                self.assertIs(
                    decide(make_principal(SystemRole.USER), capability, None).reason,
                    Reason.INVALID_RESOURCE,
                )


class AgentsCannotDoThemTest(unittest.IsolatedAsyncioTestCase):
    """None of the three is delegable: not even with a grant and a willing user."""

    async def test_the_pure_decision_forbids_the_capability_for_every_grant(self):
        for role in (SystemRole.OWNER, SystemRole.ADMIN, SystemRole.USER):
            who = make_principal(role, projects={P1: ProjectRole.MANAGER})
            for capability in THREE:
                for grant in (
                    AgentGrant(AGENT, frozenset({capability}), ALL_PROJECTS),
                    AgentGrant(AGENT, frozenset(Capability), ALL_PROJECTS),
                    AgentGrant(AGENT, frozenset({capability}), frozenset({P1})),
                ):
                    with self.subTest(role=role.value, capability=capability.value):
                        resource = own_row(capability, who.user_id)
                        self.assertTrue(decide(who, capability, resource).allowed)
                        decision = decide_agent(who, grant, capability, resource)
                        self.assertFalse(decision.allowed)
                        self.assertIs(
                            decision.reason, Reason.AGENT_CAPABILITY_FORBIDDEN
                        )

    async def test_an_agents_attempt_is_denied_and_recorded_with_the_agent(self):
        owner = make_principal(SystemRole.OWNER, user_id=U1)
        sink = InMemoryAuditSink()
        authorizer = Authorizer(
            sink, directory=StaticDirectory(owner), clock=lambda: NOW
        )
        grant = AgentGrant(AGENT, frozenset(THREE), ALL_PROJECTS)
        for capability in THREE:
            with self.subTest(capability=capability.value):
                sink.events.clear()
                decision = await authorizer.authorize_agent_action(
                    U1, grant, capability, own_row(capability, U1)
                )
                self.assertFalse(decision.allowed)
                self.assertIs(decision.reason, Reason.AGENT_CAPABILITY_FORBIDDEN)
                (event,) = sink.events
                self.assertEqual(
                    (
                        event.action,
                        event.decision,
                        event.reason,
                        event.actor_id,
                        event.agent_id,
                    ),
                    (capability.value, "deny", "agent_capability_forbidden", U1, AGENT),
                )

    async def test_an_agents_denial_that_cannot_be_recorded_is_still_a_denial(self):
        owner = make_principal(SystemRole.OWNER, user_id=U1)
        sink = FailingSink()
        authorizer = Authorizer(
            sink, directory=StaticDirectory(owner), clock=lambda: NOW
        )
        grant = AgentGrant(AGENT, frozenset(THREE), ALL_PROJECTS)
        with self.assertLogs(LOGGER, level="WARNING"):
            decision = await authorizer.authorize_agent_action(
                U1, grant, LEAVE, own_row(LEAVE, U1)
            )
        self.assertFalse(decision.allowed)
        self.assertIs(decision.reason, Reason.AGENT_CAPABILITY_FORBIDDEN)
        self.assertEqual(sink.attempts, 1)

    async def test_a_user_who_cannot_do_it_is_reported_before_the_grant(self):
        # The user's own denial is what is reported (as for every capability).
        system = make_principal(SystemRole.SYSTEM, user_id=U1)
        authorizer = Authorizer(
            InMemoryAuditSink(), directory=StaticDirectory(system), clock=lambda: NOW
        )
        grant = AgentGrant(AGENT, frozenset(THREE), ALL_PROJECTS)
        for capability in THREE:
            with self.subTest(capability=capability.value):
                decision = await authorizer.authorize_agent_action(
                    U1, grant, capability, own_row(capability, U1)
                )
                self.assertIs(decision.reason, Reason.CAPABILITY_NOT_GRANTED)


class AuditRowsTest(unittest.IsolatedAsyncioTestCase):
    """REQUIRED: a row per allowed and per denied call; closed when unwritable."""

    def setUp(self):
        self.sink = InMemoryAuditSink()
        self.authorizer = Authorizer(self.sink, clock=lambda: NOW)

    async def test_an_allowed_call_writes_one_row_that_names_the_resource(self):
        user = make_principal(SystemRole.USER, user_id=U1)
        expected = {
            CREATE: ("system", None, None),
            RESPOND: ("project_invitation", None, P2),
            LEAVE: ("project_membership", None, P2),
        }
        for capability in THREE:
            with self.subTest(capability=capability.value):
                self.sink.events.clear()
                decision = await self.authorizer.authorize(
                    user, capability, own_row(capability, U1, project_id=P2)
                )
                self.assertTrue(decision.allowed)
                (event,) = self.sink.events
                self.assertEqual(
                    (event.resource_kind, event.resource_id, event.project_id),
                    expected[capability],
                )
                self.assertEqual(
                    (event.action, event.decision, event.actor_id, event.actor_role),
                    (capability.value, "allow", U1, "user"),
                )
                self.assertIsNone(event.agent_id)

    async def test_every_denied_call_writes_its_own_row(self):
        system = make_principal(SystemRole.SYSTEM, user_id=U1)
        for capability in THREE:
            with self.subTest(capability=capability.value):
                self.sink.events.clear()
                for _ in range(3):
                    decision = await self.authorizer.authorize(
                        system, capability, own_row(capability, U1)
                    )
                    self.assertFalse(decision.allowed)
                self.assertEqual(
                    [(e.action, e.decision, e.reason) for e in self.sink.events],
                    [(capability.value, "deny", "capability_not_granted")] * 3,
                )

    async def test_an_allowance_that_cannot_be_recorded_becomes_a_denial(self):
        sink = FailingSink()
        authorizer = Authorizer(sink, clock=lambda: NOW)
        user = make_principal(SystemRole.OWNER, user_id=U1)
        for capability in THREE:
            with self.subTest(capability=capability.value):
                with self.assertLogs(LOGGER, level="ERROR"):
                    decision = await authorizer.authorize(
                        user, capability, own_row(capability, U1)
                    )
                self.assertFalse(decision.allowed)
                self.assertIs(decision.reason, Reason.AUDIT_UNAVAILABLE)
        self.assertEqual(sink.attempts, 3)

    async def test_a_denial_stays_a_denial_when_it_cannot_be_recorded(self):
        sink = FailingSink()
        authorizer = Authorizer(sink, clock=lambda: NOW)
        system = make_principal(SystemRole.SYSTEM, user_id=U1)
        for capability in THREE:
            with self.subTest(capability=capability.value):
                with self.assertLogs(LOGGER, level="WARNING"):
                    decision = await authorizer.authorize(
                        system, capability, own_row(capability, U1)
                    )
                self.assertIs(decision.reason, Reason.CAPABILITY_NOT_GRANTED)

    async def test_an_unauthenticated_call_is_logged_not_stored(self):
        for capability in THREE:
            with self.subTest(capability=capability.value):
                with self.assertLogs(LOGGER, level="INFO") as logs:
                    decision = await self.authorizer.authorize(
                        None, capability, Resource.system()
                    )
                self.assertIs(decision.reason, Reason.UNAUTHENTICATED)
                self.assertIn(f"action={capability.value}", "\n".join(logs.output))
        self.assertEqual(self.sink.events, [])


if __name__ == "__main__":
    unittest.main()
