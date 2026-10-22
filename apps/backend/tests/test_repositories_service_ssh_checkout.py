"""Managed checkouts when the backend is not the account (Issue #285, Decision 0097).

Under ``git: ssh`` the backend's Linux user (``paw``) cannot create a directory in
the account's home: the account makes it, through the forced-command wrapper
(``paw-dir/v1``). There is no second Linux user here, so "the backend cannot
``mkdir`` there" is made literal instead: the backend process's own
``paths.create_checkout_directory`` / ``paths.remove_directory`` are replaced
by functions that fail the test, and everything must still work through
``SshGitRunner`` → fake ``sshd`` → the real wrapper → real git.

Real PostgreSQL; skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import os
import shutil
import stat
import sys
from unittest import mock

from paw_backend.authz import Authorizer
from paw_backend.repositories import (
    CheckoutState,
    GitCommandError,
    PathProblem,
    PathRejectedError,
    RepositoryPolicy,
    RepositoryService,
)
from paw_backend.repositories.directories import LocalCheckoutDirectories
from paw_backend.repositories.ssh import SshCheckoutDirectories, SshGitRunner

from .repositories_support import (
    PostgresRepositoryTestCase,
    fs,
    git,
    requires_git,
    requires_postgres,
)
from .test_ssh_git_wrapper import _FAKE_SSHD, WRAPPER_PATH, _FixedKey


def mode_of(path: str) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _not_in_this_process(*args, **kwargs):
    raise AssertionError("the backend process touched the account's home itself")


@requires_postgres
@requires_git
class SshManagedCheckoutTest(PostgresRepositoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha Project")
        self.manager = self.seed_user_with_account("alice")
        self.seed_manager(self.project_id, self.manager)
        self.alice = self.actor(self.manager)
        self.home = self.account(self.manager).home
        self.root = f"{self.home}/workspaces"
        os.makedirs(self.root)  # the deployment's (Decision 0097, 6)
        os.chmod(self.root, 0o755)
        self.project_dir = f"{self.root}/alpha-project-{self.project_id.hex[:8]}"
        self.bare = self.world.make_bare("acme", "tool", {"README.md": "hello\n"})
        self.runner = self.ssh_runner()
        self.ssh_service = self.new_service(
            runner=self.runner,
            directories=SshCheckoutDirectories(self.runner, timeout_s=60),
        )
        for name in ("create_checkout_directory", "remove_directory"):
            patcher = mock.patch(
                f"paw_backend.repositories.directories.{name}",
                _not_in_this_process,
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def ssh_runner(self) -> SshGitRunner:
        options = self.world.runner_options()
        wrapper_options = [
            f"--root={self.root}",
            f"--home={self.home}",
            f"--git={shutil.which('git')}",
            *(f"--allow-protocol={p}" for p in options["allowed_protocols"]),
            *(f"--config={k}={v}" for k, v in options["extra_config"]),
        ]
        sshd = f"{self.world.root}/fake-sshd.py"
        fs.write(
            sshd,
            _FAKE_SSHD.replace("__PYTHON__", repr(sys.executable))
            .replace("__WRAPPER__", repr(str(WRAPPER_PATH)))
            .replace("__OPTIONS__", repr(wrapper_options)),
        )
        os.chmod(sshd, 0o755)
        key = f"{self.world.root}/alice.key"
        fs.write(key, "not a real key\n")
        os.chmod(key, 0o600)
        return SshGitRunner(_FixedKey(key), ssh_executable=sshd, **options)

    async def test_a_github_clone_is_made_by_the_account(self):
        result = await self.ssh_service.clone_from_github(
            self.alice, self.project_id, "acme/tool"
        )
        path = f"{self.project_dir}/tool"
        self.assertEqual(result.checkout.path, path)
        self.assertEqual(result.checkout.state, CheckoutState.READY)
        self.assertEqual(fs.read(path, "README.md"), "hello\n")
        self.assertEqual(
            git("rev-parse", "HEAD", cwd=path), git("rev-parse", "HEAD", cwd=self.bare)
        )
        # A root of mode ``0755`` without an ACL: the project directory gets
        # its execute bits only (the backend can ``lstat`` the checkout,
        # nobody can list the project directory).
        self.assertEqual(mode_of(self.project_dir), 0o711)
        self.assertEqual(mode_of(path), 0o700)

    async def test_a_new_local_repository_is_made_by_the_account(self):
        result = await self.ssh_service.create_local(self.alice, self.project_id, "new")
        path = f"{self.project_dir}/new"
        self.assertEqual(result.checkout.state, CheckoutState.READY)
        self.assertTrue(fs.isdir(f"{path}/.git"))
        self.assertEqual(mode_of(path), 0o700)

    async def test_an_existing_directory_is_never_reused(self):
        os.makedirs(f"{self.project_dir}/tool")
        with self.assertRaises(PathRejectedError) as caught:
            await self.ssh_service.clone_from_github(
                self.alice, self.project_id, "acme/tool"
            )
        self.assertEqual(caught.exception.problem, PathProblem.EXISTS)
        self.assertEqual(self.checkout_rows(), [])
        self.assertEqual(os.listdir(f"{self.project_dir}/tool"), [])  # not removed

    async def test_a_failed_clone_leaves_no_directory_and_no_row(self):
        with self.assertRaises(GitCommandError):
            await self.ssh_service.clone_from_github(
                self.alice, self.project_id, "acme/missing"
            )
        self.assertFalse(fs.lexists(f"{self.project_dir}/missing"))
        self.assertEqual(self.checkout_rows(), [])
        self.assertEqual(self.repository_rows(self.project_id), [])
        # The same registration works afterwards.
        self.world.make_bare("acme", "missing")
        result = await self.ssh_service.clone_from_github(
            self.alice, self.project_id, "acme/missing"
        )
        self.assertEqual(result.checkout.state, CheckoutState.READY)

    async def test_a_members_own_checkout_is_made_by_the_account_too(self):
        registered = await self.ssh_service.clone_from_github(
            self.alice, self.project_id, "acme/tool"
        )
        shutil.rmtree(registered.checkout.path)
        await self.ssh_service.remove_checkout(
            self.alice, self.project_id, registered.repository.id
        )
        checkout = await self.ssh_service.create_checkout(
            self.alice, self.project_id, registered.repository.id
        )
        self.assertEqual(checkout.state, CheckoutState.READY)
        self.assertEqual(fs.read(checkout.path, "README.md"), "hello\n")


@requires_postgres
class FromPolicyDirectoriesTest(PostgresRepositoryTestCase):
    """``from_policy`` picks who makes the directories from the runner."""

    def build(self, runner, **options) -> RepositoryService:
        service = RepositoryService.from_policy(
            self.service_database(),
            Authorizer(self.sink, clock=self.clock),
            runner,
            RepositoryPolicy(),
            **options,
        )
        self.addAsyncCleanup(service._database.dispose)
        return service

    def test_ssh_means_the_account_makes_them(self):
        runner = SshGitRunner(_FixedKey("/nonexistent"))
        directories = self.build(runner)._directories
        self.assertIsInstance(directories, SshCheckoutDirectories)
        self.assertIs(directories.runner, runner)
        self.assertEqual(directories.timeout_s, RepositoryPolicy().git_timeout_s)

    def test_otherwise_the_backend_process_makes_them(self):
        self.assertIsInstance(
            self.build(self.world.runner())._directories, LocalCheckoutDirectories
        )

    def test_an_explicit_choice_wins_and_is_checked(self):
        chosen = LocalCheckoutDirectories()
        runner = SshGitRunner(_FixedKey("/nonexistent"))
        self.assertIs(self.build(runner, directories=chosen)._directories, chosen)
        with self.assertRaises(TypeError):
            self.build(runner, directories=object())
