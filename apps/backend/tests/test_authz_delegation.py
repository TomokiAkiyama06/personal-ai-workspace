"""The grant of a child agent is a subset of its parent's (PAW-034).

Pure functions: no database. The tests check the rules one by one (a table of
requests and their outcomes), then exhaustively over every subset of a small
capability pool, and finally that a derived grant never decides ``allow`` where
the parent's grant decides ``deny`` (the rights of the child are bounded by the
user at every link and by the parent at the link before).
"""

import itertools
import unittest
import uuid

from paw_backend.authz import (
    ALL_PROJECTS,
    CAPABILITIES,
    AgentGrant,
    Capability,
    EscalationReason,
    GrantEscalationError,
    ProjectRole,
    Scope,
    SystemRole,
    derive_child_grant,
    is_subgrant,
)
from paw_backend.authz.policy import decide_agent

from .authz_support import AGENT, P1, P2, P3, principal, project, uid
from .test_authz_policy import resource_for

CHILD = uid(202)
C = Capability

DELEGABLE = {c for c, info in CAPABILITIES.items() if info.delegable}
NON_DELEGABLE = set(Capability) - DELEGABLE


def parent_grant(*capabilities: Capability, projects=(P1, P2)) -> AgentGrant:
    return AgentGrant(AGENT, frozenset(capabilities), projects)


class DerivationRulesTest(unittest.TestCase):
    def test_a_child_holds_the_delegable_capabilities_of_its_parent_by_default(self):
        parent = parent_grant(
            C.PROJECT_READ, C.PROJECT_TASK_RUN, C.AGENT_USE, C.ADMIN_AUDIT_VIEW
        )

        child = derive_child_grant(parent, child_agent_id=CHILD)

        self.assertEqual(child.agent_id, CHILD)
        # Non-delegable capabilities never pass to a child, even when the parent
        # holds them (they are ineffective for the parent as well).
        self.assertEqual(child.capabilities, {C.PROJECT_READ, C.PROJECT_TASK_RUN})
        self.assertEqual(child.project_ids, frozenset({P1, P2}))

    def test_a_child_can_ask_for_less_capabilities_and_projects(self):
        parent = parent_grant(C.PROJECT_READ, C.PROJECT_REPO_WRITE)

        child = derive_child_grant(
            parent,
            child_agent_id=str(CHILD),
            capabilities=[C.PROJECT_READ],
            project_ids=[str(P1)],
        )

        self.assertEqual(child.agent_id, CHILD)
        self.assertEqual(child.capabilities, {C.PROJECT_READ})
        self.assertEqual(child.project_ids, frozenset({P1}))

    def test_a_child_of_an_all_projects_parent_can_be_narrowed_or_stay_wide(self):
        parent = parent_grant(C.PROJECT_READ, projects=ALL_PROJECTS)

        self.assertIs(
            derive_child_grant(parent, child_agent_id=CHILD).project_ids, ALL_PROJECTS
        )
        self.assertEqual(
            derive_child_grant(
                parent, child_agent_id=CHILD, project_ids={P3}
            ).project_ids,
            frozenset({P3}),
        )
        self.assertIs(
            derive_child_grant(
                parent, child_agent_id=CHILD, project_ids=ALL_PROJECTS
            ).project_ids,
            ALL_PROJECTS,
        )

    def test_an_empty_request_is_a_child_that_can_do_nothing(self):
        parent = parent_grant(C.PROJECT_READ)

        child = derive_child_grant(
            parent, child_agent_id=CHILD, capabilities=(), project_ids=()
        )

        self.assertEqual(child.capabilities, frozenset())
        self.assertEqual(child.project_ids, frozenset())

    def test_every_refused_request_names_its_reason(self):
        parent = parent_grant(C.PROJECT_READ, C.AGENT_USE, projects=(P1,))
        cases = [
            (
                "the same agent",
                {"child_agent_id": AGENT},
                EscalationReason.SAME_AGENT,
            ),
            (
                "the same agent as text",
                {"child_agent_id": str(AGENT)},
                EscalationReason.SAME_AGENT,
            ),
            (
                "a capability the parent lacks",
                {"capabilities": [C.PROJECT_READ, C.PROJECT_REPO_WRITE]},
                EscalationReason.CAPABILITY,
            ),
            (
                "a capability nobody holds that the parent lacks too",
                {"capabilities": [C.OWNER_BACKUP_MANAGE]},
                EscalationReason.CAPABILITY,
            ),
            (
                "a non-delegable capability the parent holds",
                {"capabilities": [C.AGENT_USE]},
                EscalationReason.NOT_DELEGABLE,
            ),
            (
                "another project",
                {"project_ids": [P1, P2]},
                EscalationReason.PROJECT,
            ),
            (
                "all projects for a parent of one",
                {"project_ids": ALL_PROJECTS},
                EscalationReason.PROJECT,
            ),
        ]
        for label, arguments, reason in cases:
            with self.subTest(label):
                arguments = {"child_agent_id": CHILD, **arguments}
                with self.assertRaises(GrantEscalationError) as caught:
                    derive_child_grant(parent, **arguments)
                self.assertEqual(caught.exception.reason, reason)

    def test_an_escalation_error_holds_no_id_or_capability_name(self):
        parent = parent_grant(C.PROJECT_READ, projects=(P1,))
        with self.assertRaises(GrantEscalationError) as caught:
            derive_child_grant(
                parent,
                child_agent_id=CHILD,
                capabilities=[C.PROJECT_REPO_WRITE],
                project_ids=[P2],
            )
        text = str(caught.exception)
        for forbidden in (str(P1), str(P2), str(CHILD), "project.repo.write"):
            self.assertNotIn(forbidden, text)

    def test_wrong_types_are_refused_with_the_usual_errors(self):
        parent = parent_grant(C.PROJECT_READ)
        cases = [
            (
                "parent is not a grant",
                (object(),),
                {"child_agent_id": CHILD},
                TypeError,
            ),
            (
                "child id is not an id",
                (parent,),
                {"child_agent_id": 5},
                ValueError,
            ),
            (
                "child id is not canonical",
                (parent,),
                {"child_agent_id": str(CHILD).upper()},
                ValueError,
            ),
            (
                "capabilities as one string",
                (parent,),
                {"child_agent_id": CHILD, "capabilities": "project.read"},
                TypeError,
            ),
            (
                "capabilities as names",
                (parent,),
                {"child_agent_id": CHILD, "capabilities": ["project.read"]},
                ValueError,
            ),
            (
                "capabilities as a number",
                (parent,),
                {"child_agent_id": CHILD, "capabilities": 3},
                TypeError,
            ),
            (
                "projects as one string",
                (parent,),
                {"child_agent_id": CHILD, "project_ids": str(P1)},
                TypeError,
            ),
            (
                "projects that are not ids",
                (parent,),
                {"child_agent_id": CHILD, "project_ids": ["p1"]},
                ValueError,
            ),
        ]
        for label, args, kwargs, error in cases:
            with self.subTest(label), self.assertRaises(error):
                derive_child_grant(*args, **kwargs)

    def test_is_subgrant_compares_capabilities_and_projects(self):
        parent = parent_grant(C.PROJECT_READ, C.PROJECT_TASK_RUN)
        cases = [
            (parent_grant(C.PROJECT_READ, projects=(P1,)), True),
            (parent, True),
            (parent_grant(C.PROJECT_READ, C.CHAT_USE), False),
            (parent_grant(C.PROJECT_READ, projects=(P1, P3)), False),
            (parent_grant(C.PROJECT_READ, projects=ALL_PROJECTS), False),
        ]
        for child, expected in cases:
            with self.subTest(child=child):
                self.assertEqual(is_subgrant(child, parent), expected)
        self.assertTrue(
            is_subgrant(
                parent_grant(C.PROJECT_READ, projects=ALL_PROJECTS),
                parent_grant(C.PROJECT_READ, projects=ALL_PROJECTS),
            )
        )
        with self.assertRaises(TypeError):
            is_subgrant(object(), parent)


class ExhaustiveDerivationTest(unittest.TestCase):
    POOL = (
        C.PROJECT_READ,
        C.PROJECT_TASK_RUN,
        C.PROJECT_REPO_WRITE,
        C.PROJECT_AGENT_USE,  # the one non-delegable member
        C.MEMORY_USE,
    )

    def subsets(self):
        for size in range(len(self.POOL) + 1):
            yield from (frozenset(s) for s in itertools.combinations(self.POOL, size))

    def test_the_child_exists_exactly_when_it_asks_for_a_subset_of_the_parent(self):
        checked = 0
        for held in self.subsets():
            parent = AgentGrant(AGENT, held, {P1})
            for asked in self.subsets():
                with self.subTest(held=sorted(held), asked=sorted(asked)):
                    permitted = asked <= (held & DELEGABLE)
                    if permitted:
                        child = derive_child_grant(
                            parent, child_agent_id=CHILD, capabilities=asked
                        )
                        self.assertEqual(child.capabilities, asked)
                        self.assertTrue(is_subgrant(child, parent))
                    else:
                        with self.assertRaises(GrantEscalationError):
                            derive_child_grant(
                                parent, child_agent_id=CHILD, capabilities=asked
                            )
                    checked += 1
        self.assertEqual(checked, 32 * 32)

    def test_a_grandchild_is_a_subgrant_of_the_grandparent(self):
        grandparent = parent_grant(
            C.PROJECT_READ, C.PROJECT_TASK_RUN, C.PROJECT_REPO_WRITE
        )
        child = derive_child_grant(
            grandparent,
            child_agent_id=CHILD,
            capabilities=[C.PROJECT_READ, C.PROJECT_TASK_RUN],
        )
        grandchild = derive_child_grant(
            child, child_agent_id=uid(203), capabilities=[C.PROJECT_TASK_RUN]
        )
        self.assertTrue(is_subgrant(grandchild, child))
        self.assertTrue(is_subgrant(grandchild, grandparent))
        with self.assertRaises(GrantEscalationError):
            # The right the child never had cannot come back through it.
            derive_child_grant(
                child, child_agent_id=uid(204), capabilities=[C.PROJECT_REPO_WRITE]
            )


class EffectiveRightsTest(unittest.TestCase):
    """A derived grant decides ``allow`` only where the parent's grant does."""

    def test_a_child_never_gets_an_allow_that_its_parent_would_not_get(self):
        users = [
            principal(SystemRole.OWNER),
            principal(SystemRole.ADMIN),
            principal(SystemRole.USER, projects={P1: ProjectRole.MANAGER}),
            principal(SystemRole.USER, projects={P1: ProjectRole.CONTRIBUTOR}),
            principal(SystemRole.USER, projects={P1: ProjectRole.VIEWER}),
            principal(SystemRole.USER),
        ]
        parents = [
            parent_grant(*DELEGABLE, projects=(P1,)),
            parent_grant(*DELEGABLE, *NON_DELEGABLE, projects=ALL_PROJECTS),
            parent_grant(C.PROJECT_READ, C.PROJECT_TASK_RUN, projects=(P1, P2)),
        ]
        requests = [
            {},
            {"capabilities": [C.PROJECT_READ]},
            {"capabilities": [C.PROJECT_READ, C.PROJECT_TASK_RUN], "project_ids": [P1]},
        ]
        checked = allowed_by_child = 0
        for who in users:
            for parent in parents:
                for request in requests:
                    try:
                        child = derive_child_grant(
                            parent, child_agent_id=CHILD, **request
                        )
                    except GrantEscalationError:
                        continue  # a request the parent cannot cover: no child
                    for capability in Capability:
                        for resource in (resource_for(capability, who), project(P2)):
                            child_says = decide_agent(
                                who, child, capability, resource
                            ).allowed
                            parent_says = decide_agent(
                                who, parent, capability, resource
                            ).allowed
                            checked += 1
                            allowed_by_child += child_says
                            with self.subTest(
                                who=who.system_role.value,
                                capability=capability.value,
                            ):
                                self.assertTrue(parent_says or not child_says)
        # The test is not vacuous: many decisions were made and some were allowed.
        self.assertGreater(checked, 500)
        self.assertGreater(allowed_by_child, 20)

    def test_no_child_can_exercise_starting_an_agent(self):
        owner = principal(SystemRole.OWNER)
        parent = parent_grant(
            C.AGENT_USE, C.PROJECT_AGENT_USE, C.PROJECT_READ, projects=ALL_PROJECTS
        )
        child = derive_child_grant(parent, child_agent_id=CHILD)
        for capability in (C.AGENT_USE, C.PROJECT_AGENT_USE):
            with self.subTest(capability=capability.value):
                resource = (
                    project(P1)
                    if CAPABILITIES[capability].scope is Scope.PROJECT
                    else resource_for(capability, owner)
                )
                self.assertFalse(decide_agent(owner, child, capability, resource))

    def test_the_child_id_is_what_an_audit_row_names(self):
        self.assertNotEqual(uuid.UUID(int=201), uuid.UUID(int=202))
        child = derive_child_grant(parent_grant(C.PROJECT_READ), child_agent_id=CHILD)
        self.assertEqual(child.agent_id, CHILD)
        self.assertNotEqual(child.agent_id, AGENT)


if __name__ == "__main__":
    unittest.main()
