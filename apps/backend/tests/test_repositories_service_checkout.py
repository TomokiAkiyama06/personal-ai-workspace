"""Per-user checkouts: each user's own working copy, in their own Linux account.

Real PostgreSQL, real git on local temporary repositories, the real Authorizer.
Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import os
import shutil
import uuid
from datetime import timedelta

from paw_backend.authz import Principal, SystemRole
from paw_backend.authz.roles import ProjectRole
from paw_backend.projects import ProjectStatus
from paw_backend.repositories import (
    CheckoutExistsError,
    CheckoutGoneError,
    CheckoutInProgressError,
    CheckoutNotFoundError,
    CheckoutState,
    GitCommandError,
    LinuxAccountUnavailableError,
    NoCloneSourceError,
    PathProblem,
    PathRejectedError,
    ProjectNotActiveError,
    ProjectUnavailableError,
    RepositoryNotFoundError,
    RepositoryPermissionDeniedError,
    SubprocessGitRunner,
)

from .repositories_support import (
    PostgresRepositoryTestCase,
    fs,
    git,
    requires_git,
    requires_postgres,
)


class CheckoutTestCase(PostgresRepositoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha Project")
        self.manager = self.seed_user_with_account("alice")
        self.member = self.seed_user_with_account("carol")
        self.viewer = self.seed_user_with_account("vera")
        self.outsider = self.seed_user_with_account("olga")
        self.seed_manager(self.project_id, self.manager)
        self.seed_member(self.project_id, self.member, ProjectRole.CONTRIBUTOR)
        self.seed_member(self.project_id, self.viewer, ProjectRole.VIEWER)
        self.bare = self.world.make_bare("acme", "tool", {"README.md": "hello\n"})
        self.repository = self.seed_repository(
            self.project_id,
            name="tool",
            created_by=self.manager,
            remotes=(
                "https://github.com/acme/tool",
                "https://github.com/acme/tool.git",
            ),
        )

    def path_of(self, user_id, name="tool"):
        home = self.account(user_id).home
        return f"{home}/workspaces/alpha-project-{self.project_id.hex[:8]}/{name}"

    async def checkout(self, user_id, service=None):
        return await (service or self.service).create_checkout(
            self.actor(user_id), self.project_id, self.repository
        )


@requires_postgres
@requires_git
class CreateCheckoutTest(CheckoutTestCase):
    async def test_a_member_gets_a_clone_in_their_own_home(self):
        result = await self.checkout(self.member)

        path = self.path_of(self.member)
        self.assertEqual(
            (result.path, result.state, result.user_id, result.repository_id),
            (path, CheckoutState.READY, self.member, self.repository),
        )
        self.assertEqual(fs.read(path, "README.md"), "hello\n")
        self.assertEqual(
            git("rev-parse", "HEAD", cwd=path), git("rev-parse", "HEAD", cwd=self.bare)
        )
        (row,) = self.checkout_rows(self.repository)
        self.assertEqual(
            (row["user_id"], row["path"], row["state"]), (self.member, path, "ready")
        )

    async def test_every_user_has_a_separate_directory_in_their_own_home(self):
        one = await self.checkout(self.manager)
        two = await self.checkout(self.member)
        three = await self.checkout(self.viewer)

        paths = [one.path, two.path, three.path]
        self.assertEqual(len(set(paths)), 3)
        for user, checkout in (
            (self.manager, one),
            (self.member, two),
            (self.viewer, three),
        ):
            self.assertTrue(
                checkout.path.startswith(self.account(user).home + "/workspaces/"),
                "a checkout lives below its user's own home",
            )
            self.assertEqual(os.stat(checkout.path).st_uid, self.account(user).uid)
        # Editing one working tree does not touch another.
        fs.write(one.path, "README.md", "changed by alice\n")
        self.assertEqual(fs.read(two.path, "README.md"), "hello\n")
        self.assertEqual(fs.read(three.path, "README.md"), "hello\n")
        self.assertEqual(len(self.checkout_rows(self.repository)), 3)

    async def test_a_viewer_can_read_so_the_viewer_can_check_out(self):
        result = await self.checkout(self.viewer)
        self.assertEqual(result.user_id, self.viewer)

    async def test_the_audit_records_the_workspace_decision_naming_the_checkout(self):
        result = await self.checkout(self.member)

        # ``project.read`` is a read: only its denials are audited. The
        # ``workspace.use`` decision (audit REQUIRED) is the record of the change.
        (event,) = self.sink.events
        self.assertEqual(
            (
                event.action,
                event.decision,
                event.reason,
                event.resource_kind,
                event.resource_id,
                event.project_id,
                event.actor_id,
            ),
            (
                "workspace.use",
                "allow",
                "granted_to_resource_owner",
                "checkout",
                result.id,
                self.project_id,
                self.member,
            ),
        )

    async def test_no_file_is_added_to_the_clone(self):
        result = await self.checkout(self.member)
        self.assertEqual(fs.listdir(result.path), [".git", "README.md"])
        self.assertEqual(git("status", "--porcelain", cwd=result.path), "")

    async def test_an_outsider_learns_nothing_and_gets_nothing(self):
        with self.assertRaises(ProjectUnavailableError):
            await self.checkout(self.outsider)

        self.assertEqual(self.checkout_rows(), [])
        self.assertFalse(fs.lexists(self.account(self.outsider).home + "/workspaces"))
        self.assertEqual(self.audit_actions(), [("project.read", "deny", "repository")])

    async def test_an_owner_or_admin_who_is_no_member_cannot_read_the_project(self):
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            user = self.seed_user_with_account(f"boss-{role.value}")
            with self.subTest(role=role):
                with self.assertRaises(ProjectUnavailableError):
                    await self.service.create_checkout(
                        Principal(user, role), self.project_id, self.repository
                    )

    async def test_the_internal_system_identity_cannot_make_a_checkout(self):
        user = self.seed_user_with_account("robot")
        with self.assertRaises(RepositoryPermissionDeniedError):
            await self.service.create_checkout(
                Principal(user, SystemRole.SYSTEM), self.project_id, self.repository
            )
        self.assertEqual(self.checkout_rows(), [])

    async def test_a_repository_whose_acl_denies_read_does_not_exist_for_the_member(
        self,
    ):
        self.set_acl(["write", "agent"])  # an override without ``read``

        with self.assertRaises(RepositoryNotFoundError):
            await self.checkout(self.member)

        self.assertEqual(self.checkout_rows(), [])
        self.assertEqual(self.audit_actions(), [("project.read", "deny", "repository")])

    async def test_an_acl_override_that_keeps_read_still_allows_it(self):
        self.set_acl(["read"])
        result = await self.checkout(self.viewer)
        self.assertEqual(result.user_id, self.viewer)

    def set_acl(self, allowed):
        with self.engine.begin() as connection:
            from sqlalchemy import text

            connection.execute(
                text("UPDATE repositories SET acl_allowed = :a WHERE id = :i"),
                {"a": allowed, "i": self.repository},
            )

    async def test_only_an_active_project_takes_new_checkouts(self):
        for status in (ProjectStatus.ARCHIVED, ProjectStatus.PENDING_DELETION):
            project = self.seed_project(status, name="Other")
            self.seed_member(project, self.member, ProjectRole.CONTRIBUTOR)
            repository = self.seed_repository(
                project,
                remotes=("https://github.com/acme/tool",),
            )
            with self.subTest(status=status):
                with self.assertRaises(ProjectNotActiveError) as raised:
                    await self.service.create_checkout(
                        self.actor(self.member), project, repository
                    )
                self.assertIs(raised.exception.status, status)
        deleted = self.seed_project(ProjectStatus.DELETED)
        with self.assertRaises(ProjectUnavailableError):
            await self.service.create_checkout(
                self.actor(self.member), deleted, self.repository
            )
        self.assertEqual(self.checkout_rows(), [])

    async def test_a_repository_of_another_project_or_a_missing_one_is_not_found(self):
        other = self.seed_project(name="Other")
        self.seed_member(other, self.member, ProjectRole.CONTRIBUTOR)
        foreign = self.seed_repository(other, name="foreign")
        for project, repository in (
            (self.project_id, uuid.uuid4()),
            (self.project_id, foreign),
            (other, self.repository),
        ):
            with self.subTest(
                project=project == other, repository=repository == foreign
            ):
                with self.assertRaises(RepositoryNotFoundError):
                    await self.service.create_checkout(
                        self.actor(self.member), project, repository
                    )

    async def test_one_checkout_per_user_and_repository(self):
        first = await self.checkout(self.member)
        fs.write(first.path, "local.txt", "my work\n")

        with self.assertRaises(CheckoutExistsError):
            await self.checkout(self.member)

        self.assertEqual(fs.read(first.path, "local.txt"), "my work\n")
        self.assertEqual(len(self.checkout_rows(self.repository)), 1)

    async def test_a_fresh_pending_checkout_blocks_a_second_attempt(self):
        path = self.path_of(self.member)
        self.seed_checkout(
            self.repository,
            self.project_id,
            self.member,
            path,
            state="pending",
            created_at=self.clock.now,
        )
        with self.assertRaises(CheckoutInProgressError):
            await self.checkout(self.member)
        self.assertEqual(len(self.checkout_rows(self.repository)), 1)

    async def test_a_stale_pending_checkout_is_replaced_but_its_directory_is_kept(self):
        path = self.path_of(self.member)
        os.makedirs(path)
        fs.write(path, "half-cloned.tmp", "junk\n")
        self.seed_checkout(
            self.repository,
            self.project_id,
            self.member,
            path,
            state="pending",
            created_at=self.clock.now,
        )
        self.clock.advance(seconds=self.policy.pending_timeout_s + 1)

        with self.assertRaises(PathRejectedError) as raised:
            await self.checkout(self.member)

        # Nothing is deleted on a guess: the directory stays, the dead
        # reservation is gone, and the owner can clear the way and try again.
        self.assertIs(raised.exception.problem, PathProblem.EXISTS)
        self.assertEqual(fs.listdir(path), ["half-cloned.tmp"])
        self.assertEqual(self.checkout_rows(), [])
        shutil.rmtree(path)
        result = await self.checkout(self.member)
        self.assertEqual(result.state, CheckoutState.READY)
        self.assertEqual(fs.listdir(path), [".git", "README.md"])

    async def test_a_stale_pending_checkout_without_a_directory_is_replaced(self):
        path = self.path_of(self.member)
        self.seed_checkout(
            self.repository,
            self.project_id,
            self.member,
            path,
            state="pending",
            created_at=self.clock.now,
        )
        self.clock.advance(seconds=self.policy.pending_timeout_s + 1)

        result = await self.checkout(self.member)

        self.assertEqual(result.state, CheckoutState.READY)
        (row,) = self.checkout_rows(self.repository)
        self.assertEqual((row["state"], row["id"]), ("ready", result.id))

    async def test_pending_is_stale_only_after_twice_the_clone_timeout(self):
        path = self.path_of(self.member)
        self.seed_checkout(
            self.repository,
            self.project_id,
            self.member,
            path,
            state="pending",
            created_at=self.clock.now,
        )
        self.clock.advance(seconds=self.policy.pending_timeout_s - 1)
        with self.assertRaises(CheckoutInProgressError):
            await self.checkout(self.member)
        self.clock.advance(seconds=2)
        await self.checkout(self.member)

    async def test_a_local_only_repository_cannot_be_cloned_by_others(self):
        local = self.seed_repository(self.project_id, name="local", source="new_local")
        with self.assertRaises(NoCloneSourceError):
            await self.service.create_checkout(
                self.actor(self.member), self.project_id, local
            )
        self.assertEqual(self.checkout_rows(), [])
        self.assertFalse(fs.lexists(self.account(self.member).home + "/workspaces"))

    async def test_a_remote_on_a_host_that_may_not_be_cloned_from_is_no_source(self):
        elsewhere = self.seed_repository(
            self.project_id,
            name="elsewhere",
            remotes=("https://evil.example.org/x/y", "https://gitlab.example.org/x/y"),
        )
        with self.assertRaises(NoCloneSourceError):
            await self.service.create_checkout(
                self.actor(self.member), self.project_id, elsewhere
            )

    async def test_the_source_is_a_registered_remote_never_a_path_of_another_user(self):
        # Alice's own directory is registered as her checkout, but nobody else
        # clones from it: only a registered remote is a source.
        alice = await self.checkout(self.manager)
        carol = await self.checkout(self.member)
        source = "https://github.com/acme/tool"
        self.assertEqual(git("remote", "get-url", "origin", cwd=alice.path), source)
        self.assertEqual(git("remote", "get-url", "origin", cwd=carol.path), source)

    async def test_a_user_without_a_linux_account_gets_none(self):
        stranger = self.seed_user()
        self.seed_member(self.project_id, stranger, ProjectRole.CONTRIBUTOR)
        with self.assertRaises(LinuxAccountUnavailableError):
            await self.checkout(stranger)

    async def test_a_failed_clone_leaves_no_row_and_no_directory_and_can_be_retried(
        self,
    ):
        os.rename(self.bare, self.bare + ".away")

        with self.assertRaises(GitCommandError) as raised:
            await self.checkout(self.member)

        self.assertEqual(str(raised.exception), "git clone failed: nonzero_exit")
        self.assertEqual(self.checkout_rows(), [])
        self.assertFalse(fs.lexists(self.path_of(self.member)))
        os.rename(self.bare + ".away", self.bare)
        self.assertEqual((await self.checkout(self.member)).state, CheckoutState.READY)

    async def test_one_users_failure_does_not_touch_another_users_checkout(self):
        alice = await self.checkout(self.manager)
        os.rename(self.bare, self.bare + ".away")
        with self.assertRaises(GitCommandError):
            await self.checkout(self.member)
        self.assertEqual(fs.read(alice.path, "README.md"), "hello\n")
        self.assertEqual(len(self.checkout_rows(self.repository)), 1)

    async def test_a_directory_in_the_way_is_never_reused(self):
        path = self.path_of(self.member)
        os.makedirs(path)
        fs.write(path, "precious.txt", "mine\n")

        with self.assertRaises(PathRejectedError) as raised:
            await self.checkout(self.member)

        self.assertIs(raised.exception.problem, PathProblem.EXISTS)
        self.assertEqual(fs.read(path, "precious.txt"), "mine\n")
        self.assertEqual(self.checkout_rows(), [])

    async def test_a_symbolic_link_for_the_workspace_directory_is_refused(self):
        target = f"{self.world.root}/elsewhere"
        os.makedirs(target)
        os.symlink(target, f"{self.account(self.member).home}/workspaces")

        with self.assertRaises(PathRejectedError) as raised:
            await self.checkout(self.member)

        self.assertIs(raised.exception.problem, PathProblem.SYMLINK)
        self.assertEqual(fs.listdir(target), [])
        self.assertEqual(self.checkout_rows(), [])

    async def test_a_cancelled_clone_removes_its_directory_and_reservation(self):
        started = asyncio.Event()

        class Hanging(SubprocessGitRunner):
            async def run(self, args, **options):
                if args[0] == "clone":
                    started.set()
                    await asyncio.sleep(3600)
                return await super().run(args, **options)

        service = self.new_service(runner=Hanging(**self.world.runner_options()))
        task = self.spawn(
            service.create_checkout(
                self.actor(self.member), self.project_id, self.repository
            )
        )
        await asyncio.wait_for(started.wait(), 10)
        (pending,) = self.checkout_rows()
        self.assertEqual(pending["state"], "pending")

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(self.checkout_rows(), [])
        self.assertFalse(fs.lexists(pending["path"]))

    async def test_a_repository_removed_during_the_clone_leaves_no_rows(self):
        started, release = asyncio.Event(), asyncio.Event()

        class Held(SubprocessGitRunner):
            async def run(self, args, **options):
                result = await super().run(args, **options)
                if args[0] == "clone":
                    started.set()
                    await release.wait()
                return result

        service = self.new_service(runner=Held(**self.world.runner_options()))
        task = self.spawn(
            service.create_checkout(
                self.actor(self.member), self.project_id, self.repository
            )
        )
        await asyncio.wait_for(started.wait(), 10)
        (pending,) = self.checkout_rows()
        with self.engine.begin() as connection:
            from sqlalchemy import text

            connection.execute(text("DELETE FROM repositories"))
        release.set()

        with self.assertRaises(CheckoutGoneError):
            await task

        self.assertTrue(fs.isdir(pending["path"]), "unregistering never deletes files")
        self.assertEqual(self.checkout_rows(), [])

    async def test_an_audit_failure_blocks_the_checkout(self):
        from paw_backend.authz import Authorizer
        from paw_backend.repositories import GitClient, RepositoryService

        class BrokenSink:
            async def record(self, event):
                raise RuntimeError("down")

        service = RepositoryService(
            self.service_database(),
            Authorizer(BrokenSink(), clock=self.clock),
            self.accounts,
            GitClient(self.world.runner(), self.policy),
            clock=self.clock,
        )
        self.addAsyncCleanup(service._database.dispose)

        with self.assertRaises(RepositoryPermissionDeniedError):
            await service.create_checkout(
                self.actor(self.member), self.project_id, self.repository
            )

        self.assertEqual(self.checkout_rows(), [])
        self.assertFalse(fs.lexists(self.account(self.member).home + "/workspaces"))


@requires_postgres
@requires_git
class RemoveCheckoutTest(CheckoutTestCase):
    async def remove(self, user_id, *, project=None, repository=None, service=None):
        await (service or self.service).remove_checkout(
            self.actor(user_id),
            project or self.project_id,
            repository or self.repository,
        )

    async def test_it_unregisters_the_checkout_and_leaves_the_files(self):
        result = await self.checkout(self.member)
        fs.write(result.path, "work.txt", "unpushed work\n")
        self.sink.events.clear()

        await self.remove(self.member)

        self.assertEqual(self.checkout_rows(), [])
        self.assertEqual(fs.read(result.path, "work.txt"), "unpushed work\n")
        (event,) = self.sink.events
        self.assertEqual(
            (event.action, event.decision, event.resource_kind, event.resource_id),
            ("workspace.use", "allow", "checkout", result.id),
        )

    async def test_it_can_be_made_again_afterwards_in_a_new_directory_only_if_free(
        self,
    ):
        result = await self.checkout(self.member)
        await self.remove(self.member)

        with self.assertRaises(PathRejectedError) as raised:
            await self.checkout(self.member)

        self.assertIs(raised.exception.problem, PathProblem.EXISTS)
        self.assertEqual(fs.listdir(result.path), [".git", "README.md"])

    async def test_nobody_removes_another_users_checkout(self):
        await self.checkout(self.manager)
        # Carol has none: the lookup is by (repository, the actor), so Alice's
        # row cannot even be addressed.
        with self.assertRaises(CheckoutNotFoundError):
            await self.remove(self.member)
        self.assertEqual(len(self.checkout_rows(self.repository)), 1)

    async def test_the_project_in_the_call_must_be_the_checkouts_project(self):
        await self.checkout(self.member)
        with self.assertRaises(CheckoutNotFoundError):
            await self.remove(self.member, project=uuid.uuid4())
        self.assertEqual(len(self.checkout_rows(self.repository)), 1)

    async def test_no_checkout_is_not_found(self):
        with self.assertRaises(CheckoutNotFoundError):
            await self.remove(self.member)
        with self.assertRaises(CheckoutNotFoundError):
            await self.remove(self.member, repository=uuid.uuid4())

    async def test_it_works_in_every_project_state_and_after_leaving_the_project(self):
        result = await self.checkout(self.member)
        with self.engine.begin() as connection:
            from sqlalchemy import text

            connection.execute(
                text("UPDATE projects SET status = 'archived' WHERE id = :p"),
                {"p": self.project_id},
            )
            connection.execute(
                text("DELETE FROM project_members WHERE user_id = :u"),
                {"u": self.member},
            )

        await self.remove(self.member)

        self.assertEqual(self.checkout_rows(), [])
        self.assertTrue(fs.isdir(result.path))

    async def test_a_pending_checkout_can_be_removed(self):
        self.seed_checkout(
            self.repository,
            self.project_id,
            self.member,
            self.path_of(self.member),
            state="pending",
        )
        await self.remove(self.member)
        self.assertEqual(self.checkout_rows(), [])

    async def test_the_system_identity_cannot_and_a_bad_actor_is_refused(self):
        await self.checkout(self.member)
        with self.assertRaises(RepositoryPermissionDeniedError):
            await self.service.remove_checkout(
                Principal(self.member, SystemRole.SYSTEM),
                self.project_id,
                self.repository,
            )
        with self.assertRaises(RepositoryPermissionDeniedError):
            await self.service.remove_checkout(None, self.project_id, self.repository)
        self.assertEqual(len(self.checkout_rows(self.repository)), 1)

    async def test_an_audit_failure_keeps_the_registration(self):
        from paw_backend.authz import Authorizer
        from paw_backend.repositories import GitClient, RepositoryService

        await self.checkout(self.member)

        class BrokenSink:
            async def record(self, event):
                raise RuntimeError("down")

        service = RepositoryService(
            self.service_database(),
            Authorizer(BrokenSink(), clock=self.clock),
            self.accounts,
            GitClient(self.world.runner(), self.policy),
            clock=self.clock,
        )
        self.addAsyncCleanup(service._database.dispose)

        with self.assertRaises(RepositoryPermissionDeniedError):
            await self.remove(self.member, service=service)

        self.assertEqual(len(self.checkout_rows(self.repository)), 1)


@requires_postgres
@requires_git
class ListMyCheckoutsTest(CheckoutTestCase):
    async def test_only_the_actors_own_checkouts_newest_first(self):
        second_repo = self.seed_repository(
            self.project_id, name="second", remotes=("https://github.com/acme/second",)
        )
        first = self.seed_checkout(
            self.repository,
            self.project_id,
            self.member,
            "/a/one",
            created_at=self.clock.now,
        )
        second = self.seed_checkout(
            second_repo,
            self.project_id,
            self.member,
            "/a/two",
            created_at=self.clock.now + timedelta(minutes=1),
        )
        self.seed_checkout(
            self.repository, self.project_id, self.manager, "/a/other-user"
        )

        found = await self.service.list_my_checkouts(self.actor(self.member))

        self.assertEqual([c.id for c in found], [second, first])
        self.assertTrue(all(c.user_id == self.member for c in found))
        page = await self.service.list_my_checkouts(
            self.actor(self.member), limit=1, offset=1
        )
        self.assertEqual([c.id for c in page], [first])

    async def test_a_user_without_checkouts_has_an_empty_list(self):
        self.assertEqual(
            await self.service.list_my_checkouts(self.actor(self.viewer)), ()
        )
