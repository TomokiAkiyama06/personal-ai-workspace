"""Which sandbox a call gets, from the node's scope (Decision 0083, 6).

A call runs in the sandbox of ONE repository: the repository whose root holds
the call's path (its ``workdir`` or file; the innermost one when repositories
nest). That is the repository the broker decided the call on (its ACL, its
Working Set role), so nothing else is mounted:

* a writing tool gets the root read-write, and only when it is the node's own
  worktree (:class:`~.gitread.RepositoryGit` ``pinned``); its ``.git`` file is
  mounted again read-only (Decision 0083, 2);
* a read-only tool gets the root read-only (a node without a worktree reads the
  user's checkout, Decision 0036); the checkout's ``.git`` and the git
  directory and common directory it leads to, when they lie in the root, are
  masked: git is only reached through the shim;
* in both, every excluded path of the scope (``TaskScope.excluded_paths``, which
  holds ``NodeWorktree.protected``), every excluded repository and every other
  repository of the scope that lies inside the root is masked (an empty
  read-only tmpfs or file over it), so that a command never reads what the
  broker's scope keeps out.

The root is mounted at ``/workspace`` when the scope has one repository with a
root, else at ``/workspace/<repository id>`` (Decision 0083, 10).
"""

from dataclasses import dataclass

from paw_backend.sandbox.gitread import RepositoryGit
from paw_backend.sandbox.spec import (
    WORKSPACE,
    SandboxMount,
    SandboxSpec,
    SandboxUnavailableError,
)
from paw_backend.tools.scope import ScopedRepository, TaskScope, path_within


def repository_of(scope: TaskScope, path: str) -> ScopedRepository | None:
    """The scope's repository whose root holds the canonical ``path`` (the
    innermost one when roots nest; ``None``: in none)."""
    found: ScopedRepository | None = None
    for repository in scope.repositories:
        root = repository.root
        if root is None or not (path == root or path_within(path, root)):
            continue
        if found is None or len(root) > len(found.root or ""):
            found = repository
    return found


def mount_target(scope: TaskScope, repository: ScopedRepository) -> str:
    rooted = [r for r in scope.repositories if r.root is not None]
    if len(rooted) == 1:
        return WORKSPACE
    return f"{WORKSPACE}/{repository.repo_id}"


@dataclass(frozen=True, slots=True)
class SandboxPlan:
    """The sandbox of a call, and the host paths it hides (the git shim's
    pathspec excludes the same ones)."""

    spec: SandboxSpec
    hidden: tuple[str, ...]


def _outermost(paths: set[str]) -> list[str]:
    """``paths`` without the ones inside another (a mask over a mask cannot be
    placed: the outer one is an empty read-only tmpfs)."""
    ordered = sorted(paths)
    kept: list[str] = []
    for path in ordered:
        if not any(path_within(path, outer) for outer in kept):
            kept.append(path)
    return kept


def plan_sandbox(
    scope: TaskScope,
    repository: ScopedRepository,
    git: RepositoryGit,
    *,
    writable: bool,
    shim: bool,
) -> SandboxPlan:
    """The sandbox of a call on ``repository`` (see the module), or
    ``SandboxUnavailableError`` (``not_a_worktree``: a write outside the node's
    own worktree)."""
    root = repository.root
    if root is None or git.root != root:
        raise SandboxUnavailableError("no_repository")
    if writable and git.pinned is None:
        raise SandboxUnavailableError("not_a_worktree")
    own_git = f"{root}/.git"
    hidden: set[str] = set()
    for path in scope.excluded_paths:
        if path != root and path_within(path, root):
            if not (writable and path == own_git):
                hidden.add(path)
        elif path == root or (path_within(root, path) and root not in scope.path_roots):
            # The root is excluded, or lies in an excluded path it is not carved
            # out of as a path root of its own (a node's worktree in the
            # account's worktree area is): never mounted.
            raise SandboxUnavailableError("excluded_root")
    for other in (*scope.repositories, *scope.excluded_repositories):
        if other.root is not None and other.root != root:
            if path_within(other.root, root):
                hidden.add(other.root)
    if not writable:
        hidden.add(own_git)
        for place in (git.git_dir, git.common_dir):
            if place != root and path_within(place, root):
                hidden.add(place)
    masks = _outermost(hidden)
    readonly: list[str] = []
    if writable and not any(own_git == m or path_within(own_git, m) for m in masks):
        readonly.append(own_git)
    spec = SandboxSpec(
        mounts=(
            SandboxMount(root, mount_target(scope, repository), writable=writable),
        ),
        masks=tuple(masks),
        readonly=tuple(readonly),
        shim=shim,
    )
    return SandboxPlan(spec, tuple(sorted(hidden)))
