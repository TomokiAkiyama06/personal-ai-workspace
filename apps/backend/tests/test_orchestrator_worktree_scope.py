"""A Worker node's scope points at its own worktree, not the user's checkout
(PAW-035). No database, no git.

``derive_child_scope(worktrees=...)`` makes each repository's root its worktree,
adds the worktree as the first path root and excludes the user's checkout and
the task's integration worktree; ``scope_within`` accepts exactly that widening
and nothing more; the Tool Broker's classification then refuses a path in the
checkout while it accepts one in the worktree (attributed to the repository, so
its ACL is still asked).
"""

import unittest
from dataclasses import replace

from paw_backend.authz import ProjectState, RepoAcl
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.errors import ScopeEscalationError
from paw_backend.orchestrator.scope import derive_child_scope, scope_within
from paw_backend.orchestrator.workspaces import NodeWorktree
from paw_backend.tools import ScopedRepository, TaskScope
from paw_backend.tools.scope import (
    LexicalPathResolver,
    ScopeStatus,
    Target,
    TargetKind,
    classify_targets,
)

from .authz_support import P1, uid

R1, R2 = uid(851), uid(852)
WORKSPACES = "/home/alice/workspaces"
CHECKOUT_1 = f"{WORKSPACES}/project/one"
CHECKOUT_2 = f"{WORKSPACES}/project/two"
AREA = f"{WORKSPACES}/.paw-worktrees/task/1"


def repository(repo_id, root):
    return ScopedRepository(repo_id, P1, root, RepoAcl.inherit(repo_id, P1))


def parent_scope(**overrides) -> TaskScope:
    arguments = {
        "path_roots": [CHECKOUT_1, CHECKOUT_2],
        "hosts": ["github.com"],
        "projects": {P1: ProjectState.ACTIVE},
        "repositories": [repository(R1, CHECKOUT_1), repository(R2, CHECKOUT_2)],
    }
    arguments.update(overrides)
    return TaskScope(**arguments)


def worktree(repo_id, key="a", checkout=CHECKOUT_1) -> NodeWorktree:
    return NodeWorktree(
        repo_id,
        f"{AREA}/{repo_id}/{key}",
        f"paw/task/1/{key}",
        protected=(checkout, f"{AREA}/{repo_id}/_integration"),
    )


async def classify(scope, *paths):
    return await classify_targets(
        [Target(TargetKind.PATH, path) for path in paths], scope, LexicalPathResolver()
    )


class WorktreeScopeTest(unittest.IsolatedAsyncioTestCase):
    def test_the_repository_root_becomes_the_worktree(self):
        parent = parent_scope()
        own = worktree(R1)

        child = derive_child_scope(
            parent, role=NodeRole.WORKER, repositories=None, worktrees={R1: own}
        )

        self.assertEqual(
            child.repository(R1), replace(parent.repository(R1), root=own.path)
        )
        self.assertEqual(child.repository(R2), parent.repository(R2))
        # The worktree is the FIRST root: a relative path resolves inside it.
        self.assertEqual(child.path_roots, (own.path, CHECKOUT_1, CHECKOUT_2))
        self.assertEqual(child.excluded_paths, own.protected)
        self.assertTrue(scope_within(child, parent, worktrees={R1: own}))
        # Without the worktrees it was derived with, the child is wider.
        self.assertFalse(scope_within(child, parent))

    async def test_the_users_checkout_is_out_of_the_nodes_scope(self):
        parent = parent_scope()
        own = worktree(R1)
        child = derive_child_scope(
            parent, role=NodeRole.WORKER, repositories=None, worktrees={R1: own}
        )

        in_worktree = await classify(child, f"{own.path}/src/main.py")
        self.assertEqual(in_worktree.status, ScopeStatus.IN_SCOPE)
        self.assertEqual(in_worktree.repositories, (R1,))  # its ACL is asked

        for path in (f"{CHECKOUT_1}/src/main.py", f"{AREA}/{R1}/_integration/x"):
            with self.subTest(path=path):
                outcome = await classify(child, path)
                self.assertEqual(outcome.status, ScopeStatus.OUT_OF_SCOPE)
        # The other repository, without a worktree of its own, is as it was.
        other = await classify(child, f"{CHECKOUT_2}/x")
        self.assertEqual(other.status, ScopeStatus.IN_SCOPE)
        # The parent still reaches the checkout.
        self.assertEqual(
            (await classify(parent, f"{CHECKOUT_1}/x")).status, ScopeStatus.IN_SCOPE
        )

    def test_a_worktree_outside_the_nodes_repositories_is_an_escalation(self):
        with self.assertRaises(ScopeEscalationError):
            derive_child_scope(
                parent_scope(),
                role=NodeRole.WORKER,
                repositories=[R2],
                worktrees={R1: worktree(R1)},
            )

    def test_a_read_only_role_gets_no_worktree(self):
        for role in (NodeRole.PLANNER, NodeRole.RESEARCHER, NodeRole.REVIEWER):
            with self.subTest(role=role.value), self.assertRaises(ScopeEscalationError):
                derive_child_scope(
                    parent_scope(),
                    role=role,
                    repositories=None,
                    worktrees={R1: worktree(R1)},
                )

    def test_the_worktrees_must_be_what_they_say(self):
        with self.assertRaises(TypeError):
            derive_child_scope(
                parent_scope(),
                role=NodeRole.WORKER,
                repositories=None,
                worktrees={R2: worktree(R1)},  # filed under another repository
            )
        with self.assertRaises(TypeError):
            derive_child_scope(
                parent_scope(),
                role=NodeRole.WORKER,
                repositories=None,
                worktrees={R1: f"{AREA}/x"},
            )

    def test_a_worktree_inside_an_excluded_path_is_not_accepted(self):
        parent = parent_scope(excluded_paths=[f"{WORKSPACES}/.paw-worktrees"])
        with self.assertRaises(ScopeEscalationError):
            derive_child_scope(
                parent,
                role=NodeRole.WORKER,
                repositories=None,
                worktrees={R1: worktree(R1)},
            )

    def test_scope_within_accepts_nothing_but_the_worktree(self):
        parent = parent_scope()
        own = worktree(R1)
        child = derive_child_scope(
            parent, role=NodeRole.WORKER, repositories=None, worktrees={R1: own}
        )
        wider = [
            # Another root than the worktree.
            replace(child, path_roots=(*child.path_roots, "/etc")),
            # The repository changed beyond its root.
            replace(
                child,
                repositories=(
                    replace(child.repository(R1), remotes=("https://github.com/x/y",)),
                    child.repository(R2),
                ),
            ),
            # The other repository moved to the worktree too.
            replace(
                child,
                repositories=(
                    child.repository(R1),
                    replace(child.repository(R2), root=own.path),
                ),
            ),
        ]
        for scope in wider:
            with self.subTest(scope=scope):
                self.assertFalse(scope_within(scope, parent, worktrees={R1: own}))

    def test_excluded_paths_are_inherited(self):
        parent = parent_scope(excluded_paths=["/home/alice/workspaces/private"])
        child = derive_child_scope(parent, role=NodeRole.WORKER, repositories=None)
        self.assertEqual(child.excluded_paths, parent.excluded_paths)
        self.assertFalse(scope_within(replace(child, excluded_paths=()), parent))


class ExcludedPathsTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_path_in_an_excluded_path_is_out_of_scope(self):
        scope = parent_scope(excluded_paths=[f"{CHECKOUT_1}/secrets"])
        self.assertEqual(
            (await classify(scope, f"{CHECKOUT_1}/secrets/key")).status,
            ScopeStatus.OUT_OF_SCOPE,
        )
        self.assertEqual(
            (await classify(scope, f"{CHECKOUT_1}/secrets-not")).status,
            ScopeStatus.IN_SCOPE,
        )

    def test_excluded_paths_are_normalised_and_bounded(self):
        scope = parent_scope(excluded_paths=["/srv/a/./b", "/srv/a/b"])
        self.assertEqual(scope.excluded_paths, ("/srv/a/b",))
        with self.assertRaises(ValueError):
            parent_scope(excluded_paths=["/"])
        with self.assertRaises(ValueError):
            parent_scope(excluded_paths=["relative"])
        with self.assertRaises(ValueError):
            parent_scope(excluded_paths=[f"/srv/{i}" for i in range(33)])
        with self.assertRaises(TypeError):
            parent_scope(excluded_paths="/srv/a")
