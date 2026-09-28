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
    MAX_EXCLUDED_PATHS,
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
BASE = f"{WORKSPACES}/.paw-worktrees"
AREA = f"{BASE}/task/1"


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
    # As GitWorktreeCoordinator makes it: the checkout, the integration
    # worktree, the whole worktree area and the worktree's own ``.git``.
    path = f"{AREA}/{repo_id}/{key}"
    return NodeWorktree(
        repo_id,
        path,
        f"paw/task/1/{key}",
        protected=(checkout, f"{AREA}/{repo_id}/_integration", BASE, f"{path}/.git"),
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

    async def test_other_worktrees_are_out_of_the_nodes_scope(self):
        # The task's scope reaches the whole workspace directory, which holds
        # the worktree area: a Worker still reaches its own worktree only.
        parent = parent_scope(path_roots=[WORKSPACES])
        own = worktree(R1)
        child = derive_child_scope(
            parent, role=NodeRole.WORKER, repositories=None, worktrees={R1: own}
        )

        for path in (f"{own.path}/src/x.py", f"{WORKSPACES}/notes.txt"):
            with self.subTest(path=path):
                self.assertEqual(
                    (await classify(child, path)).status, ScopeStatus.IN_SCOPE
                )
        for path in (
            f"{AREA}/{R1}/b/src/x.py",  # a sibling node of the same DAG
            f"{AREA}/{R1}/_integration/x",  # the task's integration
            f"{BASE}/other-task/1/{R1}/_integration/x",  # another task's
            f"{BASE}/x",
            BASE,
            f"{own.path}/.git",  # its own git directory / file
            f"{own.path}/.git/config",
            f"{CHECKOUT_1}/x",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    (await classify(child, path)).status, ScopeStatus.OUT_OF_SCOPE
                )

    async def test_the_worktrees_git_is_excluded_whatever_protected_says(self):
        path = f"{AREA}/{R1}/a"
        own = NodeWorktree(R1, path, "paw/task/1/a", protected=(CHECKOUT_1,))
        child = derive_child_scope(
            parent_scope(), role=NodeRole.WORKER, repositories=None, worktrees={R1: own}
        )
        self.assertIn(f"{path}/.git", child.excluded_paths)
        self.assertEqual(
            (await classify(child, f"{path}/.git")).status, ScopeStatus.OUT_OF_SCOPE
        )

    def test_a_child_root_inside_a_parent_excluded_path_is_not_accepted(self):
        # A root below an excluded path would carve it out (the node's own
        # worktree does that inside the worktree area); only the parent may.
        parent = parent_scope(
            path_roots=[WORKSPACES], excluded_paths=[f"{WORKSPACES}/private"]
        )
        child = derive_child_scope(parent, role=NodeRole.WORKER, repositories=None)
        wider = replace(
            child, path_roots=(*child.path_roots, f"{WORKSPACES}/private/sub")
        )
        self.assertFalse(scope_within(wider, parent))

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
    async def test_a_root_inside_an_excluded_path_is_carved_out(self):
        scope = parent_scope(
            path_roots=[WORKSPACES, f"{BASE}/own"], excluded_paths=[BASE]
        )
        cases = {
            f"{BASE}/own/x": ScopeStatus.IN_SCOPE,
            f"{BASE}/own": ScopeStatus.IN_SCOPE,
            f"{BASE}/other/x": ScopeStatus.OUT_OF_SCOPE,
            f"{BASE}/own-not/x": ScopeStatus.OUT_OF_SCOPE,
        }
        for path, status in cases.items():
            with self.subTest(path=path):
                self.assertEqual((await classify(scope, path)).status, status)
        # A root equal to the excluded path carves nothing out.
        same = parent_scope(path_roots=[WORKSPACES, BASE], excluded_paths=[BASE])
        self.assertEqual(
            (await classify(same, f"{BASE}/x")).status, ScopeStatus.OUT_OF_SCOPE
        )
        # An excluded repository is never carved out.
        excluded_repo = parent_scope(
            path_roots=[WORKSPACES, f"{CHECKOUT_2}/sub"],
            repositories=[repository(R1, CHECKOUT_1)],
            excluded_repositories=[repository(R2, CHECKOUT_2)],
        )
        self.assertEqual(
            (await classify(excluded_repo, f"{CHECKOUT_2}/sub/x")).status,
            ScopeStatus.OUT_OF_SCOPE,
        )

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

    def test_a_worker_of_every_repository_fits_the_bound(self):
        repositories = [
            repository(uid(900 + i), f"{WORKSPACES}/project/{i}") for i in range(32)
        ]
        parent = parent_scope(path_roots=[WORKSPACES], repositories=repositories)
        worktrees = {
            r.repo_id: worktree(r.repo_id, checkout=r.root) for r in repositories[:16]
        }
        child = derive_child_scope(
            parent, role=NodeRole.WORKER, repositories=None, worktrees=worktrees
        )
        self.assertLessEqual(len(child.excluded_paths), MAX_EXCLUDED_PATHS)

    def test_excluded_paths_are_normalised_and_bounded(self):
        scope = parent_scope(excluded_paths=["/srv/a/./b", "/srv/a/b"])
        self.assertEqual(scope.excluded_paths, ("/srv/a/b",))
        with self.assertRaises(ValueError):
            parent_scope(excluded_paths=["/"])
        with self.assertRaises(ValueError):
            parent_scope(excluded_paths=["relative"])
        with self.assertRaises(ValueError):
            parent_scope(
                excluded_paths=[f"/srv/{i}" for i in range(MAX_EXCLUDED_PATHS + 1)]
            )
        with self.assertRaises(TypeError):
            parent_scope(excluded_paths="/srv/a")
