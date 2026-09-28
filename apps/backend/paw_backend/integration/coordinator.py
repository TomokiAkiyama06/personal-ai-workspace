"""Dedicated worktrees for Worker nodes and their integration (PAW-035).

:class:`GitWorktreeCoordinator` implements the orchestrator's
:class:`~paw_backend.orchestrator.workspaces.NodeWorkspaces` seam with git, as the
Linux account of the user who created the task (``AccountDirectory``: the
per-user checkout of PAW-027 is that user's, and so are the worktrees made from
it). It never reads another account's files itself: every question about a
worktree is asked of git through the ``GitRunner`` (``SshGitRunner`` reaches
another Linux user, Decision 0029).

For each repository of a task attempt, the layout is (``layout.py``; the
worktrees lie in ``<home>/<workspace_subdir>/.paw-worktrees``)::

    <checkout>                             the user's own checkout (untouched)
    <task>/<attempt>/<repo>/_integration   branch paw/<task>/<attempt>/_integration
    <task>/<attempt>/<repo>/<node>         branch paw/<task>/<attempt>/<node>

* The **integration branch** starts at the tip of the checkout's default branch
  (its local branch, else ``origin/<default>``) the first time the attempt needs
  it; the base of the attempt is fixed from then on.
* A **Worker branch** starts at the integration branch, then takes in the
  branches of the Worker nodes it depends on directly (in node order), so that
  a node sees the work it builds on. A conflict there fails the node without a
  retry (``WorktreeConflictError``).
* **Integration** (after the DAG succeeded) merges every Worker branch into the
  integration branch in node order, ``--no-ff``, inside the integration worktree.
  ``merge-tree`` judges each merge first, so a conflict never leaves the
  worktree half merged; the first conflict stops that repository (the later
  branches are not merged) and is reported with the conflicted files. A Worker
  worktree with uncommitted changes stops the repository before any merge
  (``DIRTY``): only commits are integrated. Each repository has its own state.
* Everything is **idempotent**: a worktree that exists on its branch is reused
  (a later attempt of a node continues its work), a branch already in the
  integration branch is not merged again, a merge left unfinished by a crash is
  aborted first. A human who resolved a conflict by merging the branch in the
  integration worktree lets the next integration pass it.

Nothing here pushes, and no branch outside ``paw/`` is ever written: the default
branch cannot be integrated into (``layout.py``). Worktrees and branches are kept
after the task (``REQUIREMENTS.md``: stopping a task keeps its branch / worktree;
deleting them is a separate operation). Decision 0036 (Proposed) lists the
choices.
"""

import asyncio
import uuid
import weakref
from dataclasses import dataclass

from paw_backend.integration.git import WorktreeGit
from paw_backend.integration.layout import (
    INTEGRATION_KEY,
    branch_name,
    in_namespace,
    worktree_base,
    worktree_path,
)
from paw_backend.orchestrator.workspaces import (
    MAX_CONFLICTED_FILES,
    IntegrationReport,
    IntegrationRequest,
    IntegrationState,
    NodeWorkspaceRequest,
    NodeWorktree,
    RepositoryIntegration,
    WorktreeConflictError,
    WorktreeProblem,
    WorktreeUnavailableError,
)
from paw_backend.repositories.accounts import AccountDirectory
from paw_backend.repositories.errors import (
    GitCommandError,
    InvalidRepositoryInputError,
    LinuxAccountUnavailableError,
)
from paw_backend.repositories.git import GitRunner
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.repositories.policy import RepositoryPolicy
from paw_backend.repositories.validation import validate_branch
from paw_backend.tasks import TaskRun, TaskSnapshot
from paw_backend.tools import ScopedRepository
from paw_backend.tools.interfaces import require_async_method
from paw_backend.tools.scope import path_within


@dataclass(frozen=True, slots=True)
class _Place:
    path: str
    branch: str


@dataclass(frozen=True, slots=True)
class IntegrationTarget:
    """Where the integrated result of one repository is, for the checks that run
    after the integration (``gate.py``): the integration worktree, its branch and
    the commit it pointed at when it was read."""

    repo_id: uuid.UUID
    path: str
    branch: str
    head: str


class GitWorktreeCoordinator:
    """See the module docstring. ``runner`` is the ``GitRunner`` of the
    deployment (the same one the repository service uses), ``accounts`` maps the
    task's creator to a Linux account, ``policy`` gives ``workspace_subdir`` and
    the git timeout."""

    def __init__(
        self,
        *,
        runner: GitRunner,
        accounts: AccountDirectory,
        policy: RepositoryPolicy,
    ) -> None:
        require_async_method(runner, "run", 1)
        require_async_method(accounts, "account_of", 1)
        if not isinstance(policy, RepositoryPolicy):
            raise TypeError("policy must be a RepositoryPolicy")
        self._git = WorktreeGit(runner, timeout_s=policy.git_timeout_s)
        self._accounts = accounts
        self._subdir = policy.workspace_subdir
        # One lock per (task, attempt, repository): the Worker nodes of one DAG
        # run in parallel in this process and may create the same integration
        # branch at the same moment. (Two worker processes never run one DAG:
        # the queue lease and the DAG epoch see to that.)
        self._locks: weakref.WeakValueDictionary[tuple, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    # -- the seam -------------------------------------------------------------------

    async def prepare_node(
        self, request: NodeWorkspaceRequest
    ) -> dict[uuid.UUID, NodeWorktree]:
        if not isinstance(request, NodeWorkspaceRequest):
            raise TypeError("request must be a NodeWorkspaceRequest")
        account = await self._account(request.task)
        base = worktree_base(account, self._subdir)
        prepared: dict[uuid.UUID, NodeWorktree] = {}
        for repository in request.scope.repositories:
            if repository.root is None:
                continue
            async with self._lock(request.task.id, request.run, repository.repo_id):
                prepared[repository.repo_id] = await self._git_errors(
                    self._prepare(account, base, request, repository)
                )
        return prepared

    async def integrate(self, request: IntegrationRequest) -> IntegrationReport:
        if not isinstance(request, IntegrationRequest):
            raise TypeError("request must be a IntegrationRequest")
        account = await self._account(request.task)
        base = worktree_base(account, self._subdir)
        results = []
        for repository in request.scope.repositories:
            if repository.root is None:
                continue
            async with self._lock(request.task.id, request.run, repository.repo_id):
                results.append(
                    await self._git_errors(
                        self._integrate(account, base, request, repository)
                    )
                )
        return IntegrationReport(tuple(results))

    async def targets(
        self, request: IntegrationRequest
    ) -> tuple[IntegrationTarget, ...]:
        """The integration worktree of every repository of the task attempt that
        has one (read only: nothing is created or merged)."""
        if not isinstance(request, IntegrationRequest):
            raise TypeError("request must be a IntegrationRequest")
        account = await self._account(request.task)
        base = worktree_base(account, self._subdir)
        found = []
        for repository in request.scope.repositories:
            if repository.root is None:
                continue
            place = self._place(base, request.task.id, request.run, repository, None)
            head = await self._git_errors(
                self._git.branch_commit(repository.root, place.branch, account)
            )
            if head is not None:
                found.append(
                    IntegrationTarget(
                        repository.repo_id, place.path, place.branch, head
                    )
                )
        return tuple(found)

    # -- internals ----------------------------------------------------------------

    def _lock(self, task_id: uuid.UUID, run: TaskRun, repo_id: uuid.UUID):
        key = (task_id, run.attempt, repo_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def _account(self, task: TaskSnapshot) -> LinuxAccount:
        try:
            return await self._accounts.account_of(task.created_by)
        except LinuxAccountUnavailableError:
            raise WorktreeUnavailableError(
                WorktreeProblem.ACCOUNT_UNAVAILABLE
            ) from None

    @staticmethod
    async def _git_errors(awaitable):
        """A git failure is ``WorktreeUnavailableError(GIT_FAILED)`` (its closed
        reason stays in the log of the runner, never a git text)."""
        try:
            return await awaitable
        except GitCommandError:
            raise WorktreeUnavailableError(WorktreeProblem.GIT_FAILED) from None
        except InvalidRepositoryInputError:
            raise WorktreeUnavailableError(WorktreeProblem.TOO_LONG) from None

    @staticmethod
    def _place(
        base: str,
        task_id: uuid.UUID,
        run: TaskRun,
        repository: ScopedRepository,
        key: str | None,
    ) -> _Place:
        key = INTEGRATION_KEY if key is None else key
        path = worktree_path(base, task_id, run.attempt, repository.repo_id, key)
        assert repository.root is not None
        if path_within(path, repository.root) or path_within(repository.root, path):
            # A checkout inside the worktree area (or the other way round) would
            # make the user's checkout and an agent's worktree one directory.
            raise WorktreeUnavailableError(WorktreeProblem.OVERLAPS_CHECKOUT)
        return _Place(path, branch_name(task_id, run.attempt, key))

    async def _default_branch(self, checkout: str, account: LinuxAccount) -> str:
        """``origin/HEAD``'s branch, else the branch checked out in the checkout."""
        default = await self._git.origin_head(checkout, account)
        if default is None:
            default = await self._git.current_branch(checkout, account)
        if default is None:
            raise WorktreeUnavailableError(WorktreeProblem.BASE_UNKNOWN)
        try:
            default = validate_branch(default)
        except InvalidRepositoryInputError:
            raise WorktreeUnavailableError(WorktreeProblem.BASE_UNKNOWN) from None
        if in_namespace(default):
            raise WorktreeUnavailableError(WorktreeProblem.DEFAULT_BRANCH_IN_NAMESPACE)
        return default

    async def _base_commit(self, checkout: str, account: LinuxAccount) -> str:
        default = await self._default_branch(checkout, account)
        for ref in (f"refs/heads/{default}", f"refs/remotes/origin/{default}"):
            commit = await self._git.commit_of(checkout, ref, account)
            if commit is not None:
                return commit
        raise WorktreeUnavailableError(WorktreeProblem.BASE_UNKNOWN)

    async def _ensure(
        self,
        checkout: str,
        place: _Place,
        account: LinuxAccount,
        *,
        start: str | None,
    ) -> bool:
        """The worktree ``place`` on its branch; ``True`` when the branch is new.

        A branch that does not exist is created at ``start`` (``None``: the caller
        found none) with its worktree. A branch that exists keeps its commits:
        its worktree is reused when git says it is that worktree on that branch,
        and made again (on the same branch) when it was removed."""
        exists = await self._git.branch_commit(checkout, place.branch, account)
        if exists is None:
            if start is None:
                raise WorktreeUnavailableError(WorktreeProblem.BASE_UNKNOWN)
            await self._git.add_worktree(
                checkout, place.path, place.branch, start, account
            )
            await self._verify(place, account)
            return True
        listed = await self._git.worktree_branch(checkout, place.path, account)
        if listed is None:
            # Removed by hand (or its directory is gone): the branch keeps the
            # work, and the worktree is made again on it.
            await self._git.prune_worktrees(checkout, account)
            await self._git.attach_worktree(checkout, place.path, place.branch, account)
        elif listed != place.branch:
            raise WorktreeUnavailableError(WorktreeProblem.NOT_THE_WORKTREE)
        await self._verify(place, account)
        return False

    async def _verify(self, place: _Place, account: LinuxAccount) -> None:
        """git itself must say that ``place.path`` is a work tree whose top is
        ``place.path`` (no symbolic link, no other repository) and that it has
        ``place.branch`` checked out."""
        top = await self._git.toplevel(place.path, account)
        branch = await self._git.current_branch(place.path, account)
        if top != place.path or branch != place.branch:
            raise WorktreeUnavailableError(WorktreeProblem.NOT_THE_WORKTREE)

    async def _integration(
        self,
        account: LinuxAccount,
        base: str,
        task_id: uuid.UUID,
        run: TaskRun,
        repository: ScopedRepository,
    ) -> _Place:
        assert repository.root is not None
        place = self._place(base, task_id, run, repository, None)
        exists = await self._git.branch_commit(repository.root, place.branch, account)
        start = None if exists else await self._base_commit(repository.root, account)
        if exists:
            # The namespace rule holds for a branch made earlier, too.
            await self._default_branch(repository.root, account)
        await self._ensure(repository.root, place, account, start=start)
        return place

    async def _prepare(
        self,
        account: LinuxAccount,
        base: str,
        request: NodeWorkspaceRequest,
        repository: ScopedRepository,
    ) -> NodeWorktree:
        checkout = repository.root
        assert checkout is not None
        task_id, run = request.task.id, request.run
        integration = await self._integration(account, base, task_id, run, repository)
        place = self._place(base, task_id, run, repository, request.node_key)
        start = await self._git.branch_commit(checkout, integration.branch, account)
        await self._ensure(checkout, place, account, start=start)
        # The work of the Worker nodes this one builds on (idempotent: a branch
        # already contained is not merged again, so a later attempt changes
        # nothing it already has).
        for upstream in request.upstream_workers:
            branch = branch_name(task_id, run.attempt, upstream)
            if await self._git.branch_commit(checkout, branch, account) is None:
                continue  # the upstream node wrote nothing to this repository
            if await self._git.is_ancestor(checkout, branch, place.branch, account):
                continue
            if not await self._git.merge(place.path, branch, account):
                raise WorktreeConflictError()
        return NodeWorktree(
            repository.repo_id,
            place.path,
            place.branch,
            protected=(checkout, integration.path),
        )

    async def _integrate(
        self,
        account: LinuxAccount,
        base: str,
        request: IntegrationRequest,
        repository: ScopedRepository,
    ) -> RepositoryIntegration:
        checkout = repository.root
        assert checkout is not None
        task_id, run = request.task.id, request.run
        workers = []
        for key in request.workers:
            place = self._place(base, task_id, run, repository, key)
            if await self._git.branch_commit(checkout, place.branch, account):
                workers.append((key, place))
        if not workers:
            return RepositoryIntegration(repository.repo_id, IntegrationState.NOTHING)
        integration = await self._integration(account, base, task_id, run, repository)

        def ended(state: IntegrationState, **fields) -> RepositoryIntegration:
            return RepositoryIntegration(
                repository.repo_id,
                state,
                branch=integration.branch,
                path=integration.path,
                **fields,
            )

        # Uncommitted work is never integrated, dropped or committed for a node.
        for key, place in workers:
            listed = await self._git.worktree_branch(checkout, place.path, account)
            if listed is not None and not await self._git.is_clean(place.path, account):
                return ended(IntegrationState.DIRTY, blocking_node=key)
        await self._git.abort_merge(integration.path, account)
        if not await self._git.is_clean(integration.path, account):
            return ended(IntegrationState.DIRTY)

        merged: list[str] = []
        for key, place in workers:
            if await self._git.is_ancestor(
                checkout, place.branch, integration.branch, account
            ):
                merged.append(key)
                continue
            check = await self._git.check_merge(
                integration.path, integration.branch, place.branch, account
            )
            if not check.clean or not await self._git.merge(
                integration.path, place.branch, account
            ):
                return ended(
                    IntegrationState.CONFLICT,
                    merged=tuple(merged),
                    blocking_node=key,
                    conflicted_files=check.conflicted_files[:MAX_CONFLICTED_FILES],
                    head=await self._git.branch_commit(
                        checkout, integration.branch, account
                    ),
                )
            merged.append(key)
        head = await self._git.branch_commit(checkout, integration.branch, account)
        return ended(IntegrationState.MERGED, merged=tuple(merged), head=head)
