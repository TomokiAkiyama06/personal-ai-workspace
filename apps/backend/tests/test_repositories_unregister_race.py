"""An unregistration during a running clone or GitHub creation (PAW-027).

The removal operations promise to unregister **without touching files**. When one
of them runs while a clone is in flight, the clone must not delete the directory
afterwards (it may hold work the user added after the unregistration returned); a
failure or a cancellation of the operation's *own* work still cleans up what the
operation made. The unregistrations here are real service calls from a second
service (as a second backend process), not SQL. Real PostgreSQL, real git on local
temporary repositories. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import os
import unittest

from sqlalchemy.exc import IntegrityError

from paw_backend.authz.roles import ProjectRole
from paw_backend.repositories import (
    CheckoutGoneError,
    GitCommandError,
    GitFailure,
    GitHubRepo,
    SubprocessGitRunner,
)
from paw_backend.repositories.service import _violation

from .repositories_support import (
    PostgresRepositoryTestCase,
    fs,
    requires_git,
    requires_postgres,
)


class Gate:
    """Holds one git sub-command after it ran, until the test lets it go."""

    def __init__(self, command: str, *, then_fail: bool = False) -> None:
        self.command = command
        self.then_fail = then_fail
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def runner(self, **options) -> SubprocessGitRunner:
        gate = self

        class Gated(SubprocessGitRunner):
            async def run(self, args, **run_options):
                result = await super().run(args, **run_options)
                if args[0] == gate.command:
                    gate.started.set()
                    await gate.release.wait()
                    if gate.then_fail:
                        raise GitCommandError(args[0], GitFailure.NONZERO_EXIT)
                return result

        return Gated(**options)


class HeldGateway:
    """A GitHub gateway that answers only when the test lets it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def create_repository(self, *, user_id, name, private):
        self.started.set()
        await self.release.wait()
        return GitHubRepo("github.com", "alice-gh", name)


class ImmediateGateway:
    """A GitHub gateway that answers at once."""

    async def create_repository(self, *, user_id, name, private):
        return GitHubRepo("github.com", "alice-gh", name)


class RaceTestCase(PostgresRepositoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha Project")
        self.manager = self.seed_user_with_account("alice")
        self.member = self.seed_user_with_account("carol")
        self.seed_manager(self.project_id, self.manager)
        self.seed_member(self.project_id, self.member, ProjectRole.CONTRIBUTOR)
        self.world.make_bare("acme", "tool", {"README.md": "hello\n"})
        self.world.make_bare("acme", "seeded", {"README.md": "hello\n"})
        self.seeded = self.seed_repository(
            self.project_id,
            name="seeded",
            remotes=("https://github.com/acme/seeded.git",),
        )
        self.alice = self.actor(self.manager)
        self.carol = self.actor(self.member)

    def project_dir(self, user_id):
        home = self.account(user_id).home
        return f"{home}/workspaces/alpha-project-{self.project_id.hex[:8]}"

    async def wait_for(self, event: asyncio.Event) -> None:
        await asyncio.wait_for(event.wait(), 20)

    def add_work(self, path: str) -> None:
        """Work the user does in the directory after the unregistration returned."""
        fs.write(path, "later-work.txt", "not pushed anywhere\n")

    def assertDirectoryKept(self, path: str) -> None:
        self.assertTrue(fs.isdir(f"{path}/.git"), "the clone was deleted")
        self.assertEqual(fs.read(path, "later-work.txt"), "not pushed anywhere\n")


@requires_postgres
@requires_git
class UnregisterDuringCreateCheckoutTest(RaceTestCase):
    async def start(self, gate: Gate):
        service = self.new_service(runner=gate.runner(**self.world.runner_options()))
        task = self.spawn(
            service.create_checkout(self.carol, self.project_id, self.seeded)
        )
        await self.wait_for(gate.started)
        (pending,) = self.checkout_rows()
        self.assertEqual(pending["state"], "pending")
        return task, pending["path"]

    async def test_remove_checkout_during_the_clone_keeps_the_directory(self):
        gate = Gate("clone")
        task, path = await self.start(gate)

        await self.new_service().remove_checkout(
            self.carol, self.project_id, self.seeded
        )
        self.add_work(path)
        gate.release.set()

        with self.assertRaises(CheckoutGoneError):
            await task
        self.assertDirectoryKept(path)
        self.assertEqual(self.checkout_rows(), [])

    async def test_remove_repository_during_the_clone_keeps_the_directory(self):
        gate = Gate("clone")
        task, path = await self.start(gate)

        await self.new_service().remove_repository(
            self.alice, self.project_id, self.seeded
        )
        self.add_work(path)
        gate.release.set()

        with self.assertRaises(CheckoutGoneError):
            await task
        self.assertDirectoryKept(path)
        self.assertEqual(self.checkout_rows(), [])
        self.assertEqual(self.repository_rows(self.project_id), [])

    async def test_a_failure_of_the_operation_after_an_unregistration_keeps_it(self):
        gate = Gate("clone", then_fail=True)
        task, path = await self.start(gate)

        await self.new_service().remove_checkout(
            self.carol, self.project_id, self.seeded
        )
        self.add_work(path)
        gate.release.set()

        with self.assertRaises(GitCommandError):
            await task
        self.assertTrue(fs.isdir(path))
        self.assertEqual(fs.read(path, "later-work.txt"), "not pushed anywhere\n")

    async def test_a_failure_of_the_operations_own_work_still_cleans_up(self):
        gate = Gate("clone", then_fail=True)
        task, path = await self.start(gate)

        gate.release.set()

        with self.assertRaises(GitCommandError):
            await task
        self.assertFalse(fs.lexists(path))
        self.assertEqual(self.checkout_rows(), [])

    async def test_a_cancellation_of_the_operations_own_work_still_cleans_up(self):
        gate = Gate("clone")
        task, path = await self.start(gate)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertFalse(fs.lexists(path))
        self.assertEqual(self.checkout_rows(), [])

    async def test_a_cancellation_after_an_unregistration_keeps_the_directory(self):
        gate = Gate("clone")
        task, path = await self.start(gate)

        await self.new_service().remove_checkout(
            self.carol, self.project_id, self.seeded
        )
        self.add_work(path)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertDirectoryKept(path)


@requires_postgres
@requires_git
class AbandonTest(RaceTestCase):
    async def test_a_finished_registration_is_never_undone_by_a_late_cleanup(self):
        # A cancellation that arrives after the second transaction committed: the
        # checkout is ``ready`` then, which is a finished registration, not a
        # reservation of the cancelled call.
        path = f"{self.project_dir(self.member)}/seeded"
        os.makedirs(path)
        self.add_work(path)
        checkout = self.seed_checkout(
            self.seeded, self.project_id, self.member, path, state="ready"
        )

        await self.new_service()._abandon(
            self.account(self.member),
            path,
            True,
            checkout_id=checkout,
            repository_id=self.seeded,
        )

        self.assertEqual([c["id"] for c in self.checkout_rows()], [checkout])
        self.assertEqual(
            [r["id"] for r in self.repository_rows(self.project_id)], [self.seeded]
        )
        self.assertEqual(fs.read(path, "later-work.txt"), "not pushed anywhere\n")


@requires_postgres
@requires_git
class UnregisterDuringCloneFromGitHubTest(RaceTestCase):
    async def start(self, gate: Gate):
        service = self.new_service(runner=gate.runner(**self.world.runner_options()))
        task = self.spawn(
            service.clone_from_github(self.alice, self.project_id, "acme/tool")
        )
        await self.wait_for(gate.started)
        (pending,) = self.checkout_rows()
        return task, pending["path"], pending["repository_id"]

    async def test_remove_checkout_keeps_the_directory_and_the_repository(self):
        gate = Gate("clone")
        task, path, repository = await self.start(gate)

        await self.new_service().remove_checkout(
            self.alice, self.project_id, repository
        )
        self.add_work(path)
        gate.release.set()

        with self.assertRaises(CheckoutGoneError):
            await task
        self.assertDirectoryKept(path)
        # The repository this call registered is what the user left registered.
        self.assertIn(
            repository, [r["id"] for r in self.repository_rows(self.project_id)]
        )
        self.assertEqual(self.checkout_rows(), [])

    async def test_remove_repository_keeps_the_directory(self):
        gate = Gate("clone")
        task, path, repository = await self.start(gate)

        await self.new_service().remove_repository(
            self.alice, self.project_id, repository
        )
        self.add_work(path)
        gate.release.set()

        with self.assertRaises(CheckoutGoneError):
            await task
        self.assertDirectoryKept(path)
        self.assertNotIn(
            repository, [r["id"] for r in self.repository_rows(self.project_id)]
        )
        self.assertEqual(self.checkout_rows(), [])

    async def test_another_users_checkout_keeps_a_failed_repository_registered(self):
        gate = Gate("clone", then_fail=True)
        task, path, repository = await self.start(gate)
        # Somebody else made a checkout of the new repository meanwhile.
        self.seed_checkout(repository, self.project_id, self.member, "/somewhere/else")
        gate.release.set()

        with self.assertRaises(GitCommandError):
            await task

        self.assertFalse(fs.lexists(path), "the operation's own directory is removed")
        self.assertIn(
            repository, [r["id"] for r in self.repository_rows(self.project_id)]
        )
        self.assertEqual([c["user_id"] for c in self.checkout_rows()], [self.member])

    async def test_a_failed_clone_without_interference_removes_everything(self):
        gate = Gate("clone", then_fail=True)
        task, path, repository = await self.start(gate)
        gate.release.set()

        with self.assertRaises(GitCommandError):
            await task

        self.assertFalse(fs.lexists(path))
        self.assertNotIn(
            repository, [r["id"] for r in self.repository_rows(self.project_id)]
        )
        self.assertEqual(self.checkout_rows(), [])


@requires_postgres
@requires_git
class UnregisterDuringCreateGitHubTest(RaceTestCase):
    async def test_a_repository_removed_while_the_gateway_answers_is_a_typed_error(
        self,
    ):
        gateway = HeldGateway()
        service = self.new_service(github=gateway)
        task = self.spawn(service.create_github(self.alice, self.project_id, "shared"))
        await self.wait_for(gateway.started)
        (pending,) = self.checkout_rows()
        path, repository = pending["path"], pending["repository_id"]

        await self.new_service().remove_repository(
            self.alice, self.project_id, repository
        )
        self.add_work(path)
        gateway.release.set()

        with self.assertLogs("paw_backend.repositories.service", "ERROR") as logs:
            with self.assertRaises(CheckoutGoneError):
                await task
        self.assertIn("was not deleted", logs.output[0])
        self.assertDirectoryKept(path)
        self.assertEqual(self.checkout_rows(), [])
        self.assertEqual(self.remote_urls(repository), [])

    async def test_a_repository_removed_while_git_adds_the_remote_is_a_typed_error(
        self,
    ):
        gate = Gate("remote")
        service = self.new_service(
            github=ImmediateGateway(),
            runner=gate.runner(**self.world.runner_options()),
        )
        task = self.spawn(service.create_github(self.alice, self.project_id, "shared"))
        await self.wait_for(gate.started)
        (pending,) = self.checkout_rows()
        path, repository = pending["path"], pending["repository_id"]

        await self.new_service().remove_repository(
            self.alice, self.project_id, repository
        )
        self.add_work(path)
        gate.release.set()

        with self.assertLogs("paw_backend.repositories.service", "ERROR"):
            with self.assertRaises(CheckoutGoneError):
                await task
        self.assertDirectoryKept(path)
        self.assertEqual(self.checkout_rows(), [])

    async def test_a_checkout_removed_while_the_gateway_answers_keeps_the_repository(
        self,
    ):
        gateway = HeldGateway()
        service = self.new_service(github=gateway)
        task = self.spawn(service.create_github(self.alice, self.project_id, "shared"))
        await self.wait_for(gateway.started)
        (pending,) = self.checkout_rows()
        path, repository = pending["path"], pending["repository_id"]

        await self.new_service().remove_checkout(
            self.alice, self.project_id, repository
        )
        self.add_work(path)
        gateway.release.set()

        with self.assertLogs("paw_backend.repositories.service", "ERROR"):
            with self.assertRaises(CheckoutGoneError):
                await task
        self.assertDirectoryKept(path)
        self.assertIn(
            repository, [r["id"] for r in self.repository_rows(self.project_id)]
        )
        # The GitHub remotes still reach the repository that stays registered.
        self.assertEqual(
            self.remote_urls(repository),
            [
                "https://github.com/alice-gh/shared",
                "https://github.com/alice-gh/shared.git",
            ],
        )


class ForeignKeyViolationTest(unittest.TestCase):
    """The database's answer for a parent that is gone is the typed error."""

    def test_a_foreign_key_violation_of_a_child_of_a_repository_is_checkout_gone(self):
        class Diag:
            def __init__(self, name):
                self.constraint_name = name

        class Orig:
            def __init__(self, name):
                self.diag = Diag(name)

        for name in (
            "fk_repository_remotes_repository_id_repositories",
            "fk_repository_checkouts_repository_id_repositories",
        ):
            with self.subTest(name=name):
                mapped = _violation(IntegrityError("insert", {}, Orig(name)))
                self.assertIsInstance(mapped, CheckoutGoneError)
        other = IntegrityError("insert", {}, Orig("fk_something_else"))
        self.assertIs(_violation(other), other)
