"""Helpers for the repository tests (PAW-027).

Nothing here needs a network or another Linux user. A **world** is a temporary
directory that holds the "homes" of the test users, and a directory of local bare
repositories that stand in for GitHub: the service is built with a git runner whose
configuration rewrites ``https://github.com/`` to that directory
(``url.<base>.insteadOf``), so the production clone command runs unchanged against a
local repository. Every account maps to the uid of the test process, so the ownership
checks are real; "another user" is an account with another uid.

Rows are seeded with SQL and asserted with SQL (see ``projects_support``). The clock
is injected.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text

from paw_backend.authz import Authorizer
from paw_backend.authz.roles import ProjectRole
from paw_backend.db import Database
from paw_backend.repositories import (
    GitClient,
    LinuxAccount,
    LinuxAccountUnavailableError,
    RepositoryPolicy,
    RepositoryService,
    SubprocessGitRunner,
)

from .memory_support import TEST_DATABASE_URL
from .projects_support import (
    T0,
    FakeClock,
    PostgresProjectTestCase,
    Team,
    requires_postgres,
)
from .support import make_settings

__all__ = [
    "T0",
    "fs",
    "FakeAccounts",
    "PostgresRepositoryTestCase",
    "Team",
    "World",
    "git",
    "requires_postgres",
    "requires_git",
]

_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@example.invalid",
}

requires_git = unittest.skipUnless(shutil.which("git"), "git is not installed")


class fs:  # noqa: N801  (a namespace, used like a module)
    """Blocking file-system helpers for ``async def`` tests (ruff ASYNC240).

    The calls are quick reads of a temporary directory; going through a helper
    keeps the async test bodies free of blocking-call lint noise without a
    blanket ``noqa``.
    """

    exists = staticmethod(os.path.exists)
    lexists = staticmethod(os.path.lexists)
    isdir = staticmethod(os.path.isdir)

    @staticmethod
    def listdir(path: str) -> list[str]:
        return sorted(os.listdir(path))

    @staticmethod
    def read(*parts: str) -> str:
        return Path(*parts).read_text()

    @staticmethod
    def write(*parts_and_content: str) -> None:
        *parts, content = parts_and_content
        Path(*parts).write_text(content)


def git(*args: str, cwd: str | Path | None = None, check: bool = True) -> str:
    """Run the real ``git`` the way a person would (own identity, no global config)."""
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd or tempfile.gettempdir()),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        **_IDENTITY,
    }
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {args[0]} failed: {result.stderr}")
    return result.stdout.strip()


class World:
    """A temporary directory tree: homes of users and "GitHub" bare repositories."""

    def __init__(self) -> None:
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="paw-repos-"))
        self.homes = f"{self.root}/home"
        self.bare_root = f"{self.root}/github"
        os.makedirs(self.homes)
        os.makedirs(self.bare_root)

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def make_home(self, username: str) -> str:
        home = f"{self.homes}/{username}"
        os.makedirs(home, mode=0o755, exist_ok=True)
        return home

    def make_bare(
        self,
        owner: str,
        repo: str,
        files: dict[str, str] | None = None,
        *,
        branch: str = "main",
    ) -> str:
        """A bare repository ``github/<owner>/<repo>.git`` with one commit."""
        bare = f"{self.bare_root}/{owner}/{repo}.git"
        os.makedirs(os.path.dirname(bare), exist_ok=True)
        git("init", "--bare", "--quiet", f"--initial-branch={branch}", bare)
        work = f"{self.root}/seed-{owner}-{repo}"
        git("clone", "--quiet", bare, work, check=False)
        for name, content in (files or {"README.md": "hello\n"}).items():
            target = Path(work, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git("add", "-A", cwd=work)
        git("commit", "--quiet", "-m", "first", cwd=work)
        git("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", cwd=work)
        shutil.rmtree(work)
        return bare

    def make_repository(
        self, path: str, *, origin: str | None = None, branch: str = "main"
    ) -> str:
        """A normal repository (with one commit) at ``path``; the head commit id."""
        os.makedirs(path)
        git("init", "--quiet", f"--initial-branch={branch}", path)
        Path(path, "code.txt").write_text("code\n")
        git("add", "-A", cwd=path)
        git("commit", "--quiet", "-m", "first", cwd=path)
        if origin is not None:
            git("remote", "add", "origin", origin, cwd=path)
        return git("rev-parse", "HEAD", cwd=path)

    def runner_options(self) -> dict[str, Any]:
        """Options sending ``https://github.com/`` to ``github/``, allowing ``file``."""
        return {
            "allowed_protocols": ("https", "file"),
            "extra_config": [
                (f"url.file://{self.bare_root}/.insteadOf", "https://github.com/")
            ],
        }

    def runner(self) -> SubprocessGitRunner:
        return SubprocessGitRunner(**self.runner_options())


class FakeAccounts:
    """An account directory of the test world: every user acts as the test process."""

    def __init__(self, world: World) -> None:
        self._world = world
        self.accounts: dict[UUID, LinuxAccount] = {}

    def add(
        self, user_id: UUID, username: str | None = None, *, uid: int | None = None
    ) -> LinuxAccount:
        name = username or f"user-{user_id.hex[:8]}"
        account = LinuxAccount(
            user_id,
            name,
            os.geteuid() if uid is None else uid,
            self._world.make_home(name),
        )
        self.accounts[user_id] = account
        return account

    async def account_of(self, user_id: UUID) -> LinuxAccount:
        try:
            return self.accounts[user_id]
        except KeyError:
            raise LinuxAccountUnavailableError() from None


class PostgresRepositoryTestCase(PostgresProjectTestCase):
    """A repository service with a real Authorizer, real git and a real PostgreSQL."""

    engine: Any

    @classmethod
    def clean_tables(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE repository_checkouts, repository_remotes, repositories,"
                    " project_members, projects CASCADE"
                )
            )
            connection.execute(text("TRUNCATE users CASCADE"))

    async def asyncSetUp(self) -> None:
        self.world = World()
        self.addCleanup(self.world.close)
        self.accounts = FakeAccounts(self.world)
        self.policy = RepositoryPolicy()
        await super().asyncSetUp()

    def new_service(
        self, clock: FakeClock | None = None, **options: Any
    ) -> RepositoryService:
        """A service on an engine of its own, closed when the test ends."""
        clock = clock or self.clock
        database = self.service_database()
        self.addAsyncCleanup(database.dispose)
        policy = options.pop("policy", self.policy)
        runner = options.pop("runner", None) or self.world.runner()
        return RepositoryService(
            database,
            Authorizer(self.sink, clock=clock),
            options.pop("accounts", self.accounts),
            GitClient(runner, policy),
            policy=policy,
            clock=clock,
            **options,
        )

    def service_database(self) -> Database:
        return Database(make_settings(database_url=TEST_DATABASE_URL))

    # -- seeding ----------------------------------------

    def seed_user_with_account(
        self, username: str | None = None, *, uid: int | None = None
    ) -> UUID:
        user_id = self.seed_user()
        self.accounts.add(user_id, username, uid=uid)
        return user_id

    def seed_manager(self, project_id: UUID, user_id: UUID | None = None, **values):
        return self.seed_member(project_id, user_id, ProjectRole.MANAGER, **values)

    def account(self, user_id: UUID) -> LinuxAccount:
        return self.accounts.accounts[user_id]

    def seed_repository(
        self,
        project_id: UUID,
        *,
        name: str = "repo",
        default_branch: str = "main",
        source: str = "github_clone",
        acl_allowed: Sequence[str] | None = None,
        created_by: UUID | None = None,
        remotes: Sequence[str] = (),
    ) -> UUID:
        repository_id = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repositories (id, project_id, name, default_branch,"
                    " source, acl_allowed, created_by, created_at, updated_at)"
                    " VALUES (:id, :project, :name, :branch, :source, :acl, :by,"
                    " :now, :now)"
                ),
                {
                    "id": repository_id,
                    "project": project_id,
                    "name": name,
                    "branch": default_branch,
                    "source": source,
                    "acl": None if acl_allowed is None else list(acl_allowed),
                    "by": created_by,
                    "now": T0,
                },
            )
            for url in remotes:
                connection.execute(
                    text(
                        "INSERT INTO repository_remotes (repository_id, url,"
                        " project_id, created_at) VALUES (:r, :u, :p, :now)"
                    ),
                    {"r": repository_id, "u": url, "p": project_id, "now": T0},
                )
        return repository_id

    def seed_many_repositories(self, project_id: UUID, count: int) -> None:
        """``count`` repositories in one statement (to reach a limit quickly)."""
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repositories (project_id, name, default_branch,"
                    " source, created_at, updated_at) SELECT :p, 'seeded' || n,"
                    " 'main', 'github_clone', :now, :now FROM generate_series(1, :n)"
                    " AS n"
                ),
                {"p": project_id, "now": T0, "n": count},
            )

    def seed_checkout(
        self,
        repository_id: UUID,
        project_id: UUID,
        user_id: UUID,
        path: str,
        *,
        state: str = "ready",
        created_at: Any = T0,
        identity: tuple[int, int] | None = None,
    ) -> UUID:
        """Insert a checkout row. A ``ready`` one records the identity of ``path``.

        The identity is that of the directory when it exists, else a fake one
        ``(1, 1)`` (for rows whose path is never looked at).
        """
        if state == "ready" and identity is None:
            try:
                info = os.lstat(path)
                identity = (info.st_dev, info.st_ino)
            except OSError:
                identity = (1, 1)
        checkout_id = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repository_checkouts (id, repository_id, project_id,"
                    " user_id, path, state, root_device, root_inode, created_at,"
                    " updated_at) VALUES (:id, :r, :p, :u, :path, :state, :dev, :ino,"
                    " :now, :now)"
                ),
                {
                    "id": checkout_id,
                    "r": repository_id,
                    "p": project_id,
                    "u": user_id,
                    "path": path,
                    "state": state,
                    "dev": None if identity is None else identity[0],
                    "ino": None if identity is None else identity[1],
                    "now": created_at,
                },
            )
        return checkout_id

    # -- reading (SQL) ----------------------------------------

    def rows(self, sql: str, **parameters: Any) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(text(sql), parameters).mappings()
            ]

    def repository_rows(self, project_id: UUID) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM repositories WHERE project_id = :p ORDER BY name",
            p=project_id,
        )

    def remote_urls(self, repository_id: UUID) -> list[str]:
        return [
            row["url"]
            for row in self.rows(
                "SELECT url FROM repository_remotes WHERE repository_id = :r"
                " ORDER BY url",
                r=repository_id,
            )
        ]

    def checkout_rows(self, repository_id: UUID | None = None) -> list[dict[str, Any]]:
        if repository_id is None:
            return self.rows("SELECT * FROM repository_checkouts ORDER BY path")
        return self.rows(
            "SELECT * FROM repository_checkouts WHERE repository_id = :r ORDER BY path",
            r=repository_id,
        )

    def audit_actions(self) -> list[tuple[str, str, str]]:
        """``(action, decision, resource_kind)`` of every event the sink recorded."""
        return [
            (event.action, event.decision, event.resource_kind)
            for event in self.sink.events
        ]
