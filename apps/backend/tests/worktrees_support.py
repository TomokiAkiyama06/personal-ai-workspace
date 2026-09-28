"""Helpers for the PAW-035 tests (not a test module: no ``test_`` prefix).

Everything runs the real ``git`` on temporary repositories that belong to the
user running the tests, through ``SubprocessGitRunner`` (the backend's own
Linux user): no SSH, no other Linux user, no other user's home or key is ever
read. A "checkout" is a normal repository in ``<home>/workspaces/...`` of a test
home inside a temporary directory (``repositories_support.World``).
"""

import os
import uuid
from pathlib import Path
from types import SimpleNamespace

from paw_backend.authz import ProjectState, RepoAcl
from paw_backend.integration import GitWorktreeCoordinator
from paw_backend.orchestrator.workspaces import (
    IntegrationRequest,
    NodeWorkspaceRequest,
)
from paw_backend.repositories import RepositoryPolicy, SubprocessGitRunner
from paw_backend.repositories.git import command_name
from paw_backend.tasks import TaskRun
from paw_backend.tools import ScopedRepository, TaskScope

from .repositories_support import FakeAccounts, World, git

__all__ = [
    "RUN",
    "RecordingRunner",
    "Workspace",
    "commit_file",
    "git",
]

RUN = TaskRun(1, 0)


def commit_file(path: str, name: str, content: str, message: str = "work") -> str:
    """Write ``name`` in the work tree at ``path`` and commit it; the new commit."""
    target = Path(path, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git("add", "-A", cwd=path)
    git("commit", "--quiet", "-m", message, cwd=path)
    return git("rev-parse", "HEAD", cwd=path)


class RecordingRunner:
    """``SubprocessGitRunner`` that remembers every argument list it ran."""

    def __init__(self, inner: SubprocessGitRunner | None = None) -> None:
        self.inner = inner or SubprocessGitRunner()
        self.calls: list[tuple[str, ...]] = []

    async def run(self, args, *, account, cwd, timeout_s, ceiling=None):
        self.calls.append(tuple(args))
        return await self.inner.run(
            args, account=account, cwd=cwd, timeout_s=timeout_s, ceiling=ceiling
        )

    def subcommands(self) -> list[str]:
        return [command_name(args) for args in self.calls]


class Workspace:
    """One test user with checkouts, a coordinator and request builders."""

    def __init__(
        self,
        *,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
    ) -> None:
        self.world = World()
        self.accounts = FakeAccounts(self.world)
        self.user_id = user_id or uuid.uuid4()
        self.account = self.accounts.add(self.user_id, "alice")
        self.home = self.account.home
        self.project_id = project_id or uuid.uuid4()
        self.task = SimpleNamespace(id=uuid.uuid4(), created_by=self.user_id)
        self.runner = RecordingRunner()
        self.repositories: list[ScopedRepository] = []

    def close(self) -> None:
        self.world.close()

    @property
    def base(self) -> str:
        return f"{self.home}/workspaces/.paw-worktrees"

    def add_checkout(self, name: str = "repo", *, branch: str = "main") -> uuid.UUID:
        """A checkout ``<home>/workspaces/project/<name>`` with one commit."""
        path = f"{self.home}/workspaces/project/{name}"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.world.make_repository(path, branch=branch)
        return self.add_checkout_at(path)

    def add_checkout_at(self, path: str) -> uuid.UUID:
        """Register the existing repository at ``path`` as a checkout."""
        repo_id = uuid.uuid4()
        self.repositories.append(
            ScopedRepository(
                repo_id,
                self.project_id,
                path,
                RepoAcl.inherit(repo_id, self.project_id),
            )
        )
        return repo_id

    def checkout(self, repo_id: uuid.UUID) -> str:
        for repository in self.repositories:
            if repository.repo_id == repo_id:
                assert repository.root is not None
                return repository.root
        raise KeyError(repo_id)

    def scope(self, **overrides) -> TaskScope:
        arguments = {
            "path_roots": [f"{self.home}/workspaces"],
            "hosts": [],
            "projects": {self.project_id: ProjectState.ACTIVE},
            "repositories": self.repositories,
        }
        arguments.update(overrides)
        return TaskScope(**arguments)

    def coordinator(self, **options) -> GitWorktreeCoordinator:
        return GitWorktreeCoordinator(
            runner=options.pop("runner", self.runner),
            accounts=options.pop("accounts", self.accounts),
            policy=options.pop("policy", RepositoryPolicy()),
        )

    def node_request(
        self, key: str, *upstream: str, run: TaskRun = RUN, scope=None
    ) -> NodeWorkspaceRequest:
        return NodeWorkspaceRequest(
            task=self.task,
            run=run,
            node_key=key,
            upstream_workers=tuple(upstream),
            scope=scope or self.scope(),
        )

    def integration_request(
        self, *workers: str, run: TaskRun = RUN, scope=None
    ) -> IntegrationRequest:
        return IntegrationRequest(
            task=self.task, run=run, workers=tuple(workers), scope=scope or self.scope()
        )

    def branch(self, key: str, run: TaskRun = RUN) -> str:
        return f"paw/{self.task.id}/{run.attempt}/{key}"

    def worktree(self, repo_id: uuid.UUID, key: str, run: TaskRun = RUN) -> str:
        return f"{self.base}/{self.task.id}/{run.attempt}/{repo_id}/{key}"
