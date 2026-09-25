"""A sub-agent's grant and scope never exceed its parent's (PAW-034). No database.

``derive_child_scope`` / ``node_grant`` derive; ``scope_within`` and
``is_subgrant`` judge. The derivation is checked on every role and on random
parents; the judges are checked on hand-made children that are too wide.
"""

import random
import unittest
import uuid

from paw_backend.authz import (
    ALL_PROJECTS,
    CAPABILITIES,
    AgentGrant,
    Capability,
    GrantEscalationError,
    ProjectState,
    RepoAcl,
    RepoPermission,
    is_subgrant,
)
from paw_backend.orchestrator.domain import (
    ROLE_CEILING,
    ROLES_WITH_CREDENTIALS,
    NodeRole,
    NodeState,
)
from paw_backend.orchestrator.errors import ScopeEscalationError
from paw_backend.orchestrator.plan import PlanNode
from paw_backend.orchestrator.records import NodeRecord
from paw_backend.orchestrator.scope import (
    agent_id_of,
    derive_child_scope,
    node_grant,
    scope_within,
)
from paw_backend.tools import ScopedRepository, TaskScope

from .authz_support import AGENT, P1, P2, uid

C = Capability
R1, R2, R3 = uid(801), uid(802), uid(803)
HANDLE = "cred_" + "a1" * 16
OTHER_HANDLE = "cred_" + "b2" * 16
ROOT = "/srv/paw/task"


def repository(repo_id, project=P1, root=None, acl=None, remotes=()):
    return ScopedRepository(
        repo_id,
        project,
        root or f"{ROOT}/{repo_id.int}",
        acl if acl is not None else RepoAcl.inherit(repo_id, project),
        remotes=remotes,
    )


def parent_scope(**overrides) -> TaskScope:
    arguments = {
        "path_roots": [ROOT],
        "hosts": ["github.com", "api.github.com"],
        "projects": {P1: ProjectState.ACTIVE},
        "credential_handles": {HANDLE: ["github.com"]},
        "repositories": [
            repository(R1, remotes=["https://github.com/org/one"]),
            repository(R2, acl=RepoAcl.override(R2, P1, {RepoPermission.READ})),
        ],
    }
    arguments.update(overrides)
    return TaskScope(**arguments)


def record(**overrides) -> NodeRecord:
    node = PlanNode(key="n", role=NodeRole.WORKER, title="t", goal="g")
    data = {
        "key": node.key,
        "ordinal": 0,
        "role": node.role,
        "title": node.title,
        "goal": node.goal,
        "input": {},
        "required": True,
        "capabilities": None,
        "repositories": None,
        "depends_on": (),
        "state": NodeState.READY,
        "agent_index": 0,
        "approach": 0,
        "attempt_count": 1,
        "rung_attempts": 1,
        "result": None,
        "error_class": None,
    }
    data.update(overrides)
    return NodeRecord(**data)


PARENT_GRANT = AgentGrant(
    AGENT,
    frozenset(
        {
            C.PROJECT_READ,
            C.PROJECT_MEMORY_USE,
            C.PROJECT_TASK_RUN,
            C.PROJECT_REPO_WRITE,
            C.PROJECT_PR_CREATE,
            C.PROJECT_AGENT_USE,
        }
    ),
    {P1},
)


class DeriveScopeTest(unittest.TestCase):
    def test_a_child_scope_is_the_parents_narrowed(self):
        parent = parent_scope()

        for role in NodeRole:
            with self.subTest(role=role.value):
                child = derive_child_scope(parent, role=role, repositories=None)
                self.assertTrue(scope_within(child, parent))
                self.assertEqual(child.path_roots, parent.path_roots)
                self.assertEqual(child.hosts, parent.hosts)
                self.assertEqual(dict(child.projects), dict(parent.projects))
                self.assertEqual(child.repositories, parent.repositories)
                expected = (
                    dict(parent.credential_handles)
                    if role in ROLES_WITH_CREDENTIALS
                    else {}
                )
                self.assertEqual(dict(child.credential_handles), expected)

    def test_only_the_worker_gets_credential_handles(self):
        self.assertEqual(ROLES_WITH_CREDENTIALS, {NodeRole.WORKER})

    def test_a_node_can_ask_for_some_of_the_repositories_and_they_stay_as_they_are(
        self,
    ):
        parent = parent_scope()

        child = derive_child_scope(parent, role=NodeRole.WORKER, repositories=[R2])

        self.assertEqual([r.repo_id for r in child.repositories], [R2])
        # The ACL (an override that only allows reading) is the parent's, unchanged.
        self.assertEqual(child.repositories[0], parent.repository(R2))
        self.assertEqual(child.repositories[0].acl.allowed, {RepoPermission.READ})
        self.assertEqual(
            derive_child_scope(
                parent, role=NodeRole.WORKER, repositories=[]
            ).repositories,
            (),
        )

    def test_the_remotes_of_a_repository_reach_the_node_unchanged(self):
        child = derive_child_scope(
            parent_scope(), role=NodeRole.WORKER, repositories=[R1]
        )
        self.assertEqual(child.repositories[0].remotes, ("https://github.com/org/one",))

    def test_a_repository_outside_the_working_set_is_refused(self):
        for requested in ([R3], [R1, R3], [uuid.uuid4()]):
            with (
                self.subTest(requested=requested),
                self.assertRaises(ScopeEscalationError),
            ):
                derive_child_scope(
                    parent_scope(), role=NodeRole.WORKER, repositories=requested
                )

    def test_a_wrong_parent_is_a_type_error(self):
        with self.assertRaises(TypeError):
            derive_child_scope(object(), role=NodeRole.WORKER, repositories=None)


class ScopeWithinTest(unittest.TestCase):
    def test_a_scope_is_within_itself(self):
        scope = parent_scope()
        self.assertTrue(scope_within(scope, scope))

    def test_every_way_of_being_wider_is_noticed(self):
        parent = parent_scope()
        wider_repo = repository(
            R1, remotes=["https://github.com/org/one", "https://github.com/x/y"]
        )
        cases = {
            "another path root": parent_scope(path_roots=[ROOT, "/srv/other"]),
            "the root above": parent_scope(path_roots=["/srv/paw"]),
            "another host": parent_scope(hosts=["github.com", "evil.example"]),
            "another project": parent_scope(
                projects={P1: ProjectState.ACTIVE, P2: ProjectState.ACTIVE},
                repositories=[],
            ),
            "the project's state changed": parent_scope(
                projects={P1: ProjectState.ARCHIVED}, repositories=[]
            ),
            "another credential": parent_scope(
                credential_handles={
                    HANDLE: ["github.com"],
                    OTHER_HANDLE: ["github.com"],
                }
            ),
            "a credential for more hosts": parent_scope(
                credential_handles={HANDLE: ["github.com", "api.github.com"]}
            ),
            "another repository": parent_scope(
                repositories=[*parent.repositories, repository(R3)]
            ),
            "a repository with more remotes": parent_scope(repositories=[wider_repo]),
            "a repository with a wider ACL": parent_scope(
                repositories=[repository(R2, acl=RepoAcl.inherit(R2, P1))]
            ),
            "a repository somewhere else": parent_scope(
                repositories=[repository(R1, root=f"{ROOT}/elsewhere")]
            ),
        }
        for label, child in cases.items():
            with self.subTest(label):
                self.assertFalse(scope_within(child, parent))

    def test_a_narrower_scope_is_within(self):
        parent = parent_scope()
        cases = {
            "a sub-directory": parent_scope(path_roots=[f"{ROOT}/sub"]),
            "fewer hosts": parent_scope(hosts=["github.com"]),
            "no credentials": parent_scope(credential_handles={}),
            "a credential for fewer hosts": parent_scope(
                credential_handles={HANDLE: []}
            ),
            "no repository": parent_scope(repositories=[]),
        }
        for label, child in cases.items():
            with self.subTest(label):
                self.assertTrue(scope_within(child, parent))


class NodeGrantTest(unittest.TestCase):
    def grant(self, node=None, role=NodeRole.WORKER, agent=None):
        return node_grant(
            PARENT_GRANT, node, role, agent or agent_id_of(uid(1), "n", 1)
        )

    def test_a_node_without_a_request_gets_its_roles_ceiling_narrowed_to_the_parent(
        self,
    ):
        expected = {
            NodeRole.PLANNER: {C.PROJECT_READ, C.PROJECT_MEMORY_USE},
            NodeRole.RESEARCHER: {C.PROJECT_READ, C.PROJECT_MEMORY_USE},
            NodeRole.REVIEWER: {C.PROJECT_READ, C.PROJECT_MEMORY_USE},
            NodeRole.WORKER: {
                C.PROJECT_READ,
                C.PROJECT_MEMORY_USE,
                C.PROJECT_TASK_RUN,
                C.PROJECT_REPO_WRITE,
            },
        }
        for role, capabilities in expected.items():
            with self.subTest(role=role.value):
                grant = self.grant(role=role)
                self.assertEqual(grant.capabilities, capabilities)
                self.assertTrue(is_subgrant(grant, PARENT_GRANT))
                self.assertEqual(grant.project_ids, PARENT_GRANT.project_ids)
                # Nothing an agent cannot exercise, nothing to start agents.
                self.assertTrue(
                    all(CAPABILITIES[c].delegable for c in grant.capabilities)
                )
                self.assertNotIn(C.PROJECT_PR_CREATE, grant.capabilities)

    def test_a_plan_can_narrow_the_grant(self):
        narrowed = record(capabilities=(C.PROJECT_READ,))
        self.assertEqual(self.grant(narrowed).capabilities, {C.PROJECT_READ})
        self.assertEqual(self.grant(record(capabilities=())).capabilities, frozenset())

    def test_a_plan_cannot_widen_the_grant_beyond_the_parent(self):
        parent = AgentGrant(AGENT, {C.PROJECT_READ}, {P1})
        wide = record(capabilities=(C.PROJECT_READ, C.PROJECT_REPO_WRITE))
        with self.assertRaises(GrantEscalationError):
            node_grant(parent, wide, NodeRole.WORKER, uid(9))

    def test_the_agent_of_a_node_attempt_is_derived_and_never_the_parent(self):
        seen = {
            agent_id_of(uid(1), "a", 1),
            agent_id_of(uid(1), "a", 2),
            agent_id_of(uid(1), "b", 1),
            agent_id_of(uid(2), "a", 1),
        }
        self.assertEqual(len(seen), 4)
        self.assertEqual(agent_id_of(uid(1), "a", 1), agent_id_of(uid(1), "a", 1))
        self.assertNotIn(AGENT, seen)
        self.assertEqual(self.grant().agent_id, agent_id_of(uid(1), "n", 1))

    def test_the_child_can_never_be_the_parent(self):
        with self.assertRaises(GrantEscalationError):
            node_grant(PARENT_GRANT, None, NodeRole.PLANNER, AGENT)


class RandomParentTest(unittest.TestCase):
    def test_every_derived_grant_and_scope_is_within_a_random_parent(self):
        pool = sorted(
            (c for c in Capability if CAPABILITIES[c].delegable), key=lambda c: c.value
        )
        checked = 0
        for seed in range(300):
            rng = random.Random(seed)
            held = frozenset(rng.sample(pool, rng.randint(0, len(pool))))
            projects = rng.choice([{P1}, {P1, P2}, ALL_PROJECTS])
            grant = AgentGrant(AGENT, held, projects)
            repos = [repository(r) for r in rng.sample([R1, R2, R3], rng.randint(0, 3))]
            scope = TaskScope(
                path_roots=[ROOT],
                hosts=rng.sample(
                    ["github.com", "gitlab.com", "pypi.org"], rng.randint(0, 3)
                ),
                projects={P1: ProjectState.ACTIVE},
                credential_handles={HANDLE: ["github.com"]}
                if rng.random() < 0.5
                else {},
                repositories=repos,
            )
            for role in NodeRole:
                wanted = tuple(
                    rng.sample(
                        sorted(ROLE_CEILING[role], key=lambda c: c.value),
                        rng.randint(0, len(ROLE_CEILING[role])),
                    )
                )
                request = rng.choice([None, wanted])
                node = (
                    None if request is None else record(role=role, capabilities=request)
                )
                with self.subTest(seed=seed, role=role.value):
                    try:
                        child = node_grant(grant, node, role, uid(50))
                    except GrantEscalationError:
                        self.assertIsNotNone(request)
                        self.assertFalse(set(request) <= grant.capabilities)
                    else:
                        self.assertTrue(is_subgrant(child, grant))
                        checked += 1
                    wanted_repos = rng.sample(
                        [r.repo_id for r in repos], rng.randint(0, len(repos))
                    )
                    derived = derive_child_scope(
                        scope, role=role, repositories=wanted_repos
                    )
                    self.assertTrue(scope_within(derived, scope))
        self.assertGreater(checked, 400)


if __name__ == "__main__":
    unittest.main()
