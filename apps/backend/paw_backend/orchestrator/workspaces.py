"""The seam to the worktrees of a task's writing nodes and to their integration
(PAW-035).

The orchestrator does not run git. When it is given a :class:`NodeWorkspaces`
(``Orchestrator(worktrees=...)``; ``paw_backend.integration.GitWorktreeCoordinator``
is the implementation), it asks it for two things and nothing else:

* :meth:`NodeWorkspaces.prepare_node`, before an attempt of a **Worker** node
  whose grant may write to a repository (``project.repo.write``): a dedicated
  worktree and branch for each repository of the node's scope that has a checkout
  (``REQUIREMENTS.md``: "Write可能なSub-Agentは原則それぞれ専用のworktree / branch
  を使用する"). The node's scope then points at the worktree, not at the user's
  own checkout (``scope.derive_child_scope(worktrees=...)``), and the runtime is
  told where it is (``NodeAssignment.worktrees``).
* :meth:`NodeWorkspaces.integrate`, once the DAG succeeded and before the task
  goes to evaluation: the branches of the Worker nodes are merged into a
  task-owned **integration branch / worktree** per repository, never into the
  default branch ("default branchへ直接統合せず、Task専用のintegration branch /
  worktreeへ変更を集約する"). A conflict is reported, not resolved.

Without a ``NodeWorkspaces`` the orchestrator behaves as PAW-034 did (no
worktree, no integration): the seam is optional so that a deployment without
checkouts keeps working. Decision 0036 (Approved) lists the choices.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Protocol

from paw_backend.orchestrator.errors import OrchestratorError
from paw_backend.tasks import TaskRun, TaskSnapshot
from paw_backend.tools import TaskScope

# The fixed names recorded for a node attempt that could not get its worktree
# (``Orchestrator._attempt``). Orchestrator names: a runtime cannot report them.
WORKTREE_UNAVAILABLE = "WorktreeUnavailable"
WORKTREE_CONFLICT = "WorktreeConflict"


class WorktreeProblem(StrEnum):
    """Why a worktree could not be prepared or integrated. A closed set."""

    ACCOUNT_UNAVAILABLE = "account_unavailable"  # no Linux account for the user
    BASE_UNKNOWN = "base_unknown"  # the default branch has no commit to start from
    # The default branch is inside the namespace the workspace owns (``paw/``):
    # integrating could then move the default branch. Refused, never guessed.
    DEFAULT_BRANCH_IN_NAMESPACE = "default_branch_in_namespace"
    # Something is at the worktree's path that is not that worktree on its branch.
    NOT_THE_WORKTREE = "not_the_worktree"
    OVERLAPS_CHECKOUT = "overlaps_checkout"  # the worktree path and the checkout nest
    GIT_FAILED = "git_failed"  # a git command failed (timeout, exit code, output)
    TOO_LONG = "too_long"  # the path or the branch would be too long


class WorktreeUnavailableError(OrchestratorError):
    """A worktree could not be prepared (or inspected). ``reason`` is closed."""

    code: ClassVar[str] = "worktree_unavailable"

    def __init__(self, reason: WorktreeProblem) -> None:
        self.reason = reason
        super().__init__(f"The worktree is not available ({reason.value})")


class WorktreeConflictError(OrchestratorError):
    """A new Worker branch could not take in the branch of a Worker node it
    depends on (a merge conflict): the node fails without a retry, since trying
    again produces the same conflict."""

    code: ClassVar[str] = "worktree_conflict"

    def __init__(self) -> None:
        super().__init__("The branches of the upstream nodes conflict")


@dataclass(frozen=True, slots=True)
class NodeWorktree:
    """The worktree one Worker node works in, for one repository.

    ``path`` is the worktree (it replaces the checkout as the repository's root in
    the node's scope), ``branch`` its branch (``refs/heads/<branch>``).
    ``protected`` are paths the node must not touch although the task's scope
    might reach them: the user's own checkout of the repository, the task's
    integration worktree and the account's whole worktree area (every other
    node's and task's worktree; the node's own worktree is carved out of it as
    a path root inside it), and the worktree's ``.git``. They become
    ``TaskScope.excluded_paths`` of the node (``derive_child_scope`` adds the
    worktree's ``.git`` whatever this says).
    """

    repo_id: uuid.UUID
    path: str
    branch: str
    protected: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NodeWorkspaceRequest:
    """What :meth:`NodeWorkspaces.prepare_node` is asked for."""

    task: TaskSnapshot
    run: TaskRun
    node_key: str
    # The keys of the Worker nodes this node depends on directly, in node order:
    # their branches (where they exist) are merged into a NEW branch of this node.
    upstream_workers: tuple[str, ...]
    # The node's scope as derived from the task's (its repositories, ACLs, remotes).
    scope: TaskScope = field(repr=False)


@dataclass(frozen=True, slots=True)
class IntegrationRequest:
    """What :meth:`NodeWorkspaces.integrate` (and ``status``) is asked for."""

    task: TaskSnapshot
    run: TaskRun
    # The keys of the succeeded Worker nodes, in node order (the merge order).
    workers: tuple[str, ...]
    # The task's scope (its whole working set).
    scope: TaskScope = field(repr=False)


class IntegrationState(StrEnum):
    """The integration state of ONE repository (each has its own)."""

    NOTHING = "nothing"  # no Worker branch in this repository: nothing to integrate
    MERGED = "merged"  # every Worker branch is in the integration branch
    CONFLICT = "conflict"  # a Worker branch conflicts; the merge was not made
    # A Worker (or the integration) worktree has uncommitted changes: only commits
    # are integrated, and uncommitted work is never dropped or committed for it.
    DIRTY = "dirty"


@dataclass(frozen=True, slots=True)
class RepositoryIntegration:
    """How the integration of one repository ended.

    ``merged`` are the Worker node keys whose branch is in the integration branch
    (in merge order). On ``CONFLICT``, ``blocking_node`` is the node whose branch
    could not be merged and ``conflicted_files`` what git reported (repository
    paths, at most :data:`MAX_CONFLICTED_FILES`); nothing after it was merged.
    On ``DIRTY``, ``blocking_node`` is the node whose worktree has changes (or
    ``None`` for the integration worktree itself). ``head`` is the commit the
    integration branch points at (``None`` for ``NOTHING``).
    """

    repo_id: uuid.UUID
    state: IntegrationState
    branch: str | None = None
    path: str | None = None
    head: str | None = None
    merged: tuple[str, ...] = ()
    blocking_node: str | None = None
    conflicted_files: tuple[str, ...] = ()


MAX_CONFLICTED_FILES = 100


@dataclass(frozen=True, slots=True)
class IntegrationReport:
    repositories: tuple[RepositoryIntegration, ...] = ()

    @property
    def clean(self) -> bool:
        """Whether every repository integrated (or had nothing to integrate)."""
        return all(
            r.state in (IntegrationState.NOTHING, IntegrationState.MERGED)
            for r in self.repositories
        )

    def repository(self, repo_id: uuid.UUID) -> RepositoryIntegration | None:
        for repository in self.repositories:
            if repository.repo_id == repo_id:
                return repository
        return None


class NodeWorkspaces(Protocol):
    async def prepare_node(
        self, request: NodeWorkspaceRequest
    ) -> Mapping[uuid.UUID, NodeWorktree]:
        """The worktree of each repository of ``request.scope`` that has a
        checkout (idempotent: a later attempt of the same node gets the same
        worktree back, with its partial work). Raises
        :class:`WorktreeUnavailableError` or :class:`WorktreeConflictError`."""
        ...

    async def integrate(self, request: IntegrationRequest) -> IntegrationReport:
        """Merge the Worker branches into each repository's integration branch
        (idempotent: a branch already merged is not merged again). Raises
        :class:`WorktreeUnavailableError` when git cannot be used at all."""
        ...
