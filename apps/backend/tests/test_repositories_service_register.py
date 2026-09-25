"""Adding repositories: register an existing one, clone from GitHub, create a new one.

Real PostgreSQL, real git on local temporary repositories (``https://github.com/`` is
rewritten to a local directory by the test runner, see ``repositories_support``), the
real Authorizer. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import os
import shutil
import uuid
from pathlib import Path

from paw_backend.authz import Principal, SystemRole
from paw_backend.authz.audit import AuditEvent
from paw_backend.authz.policy import Reason
from paw_backend.authz.roles import ProjectRole
from paw_backend.projects import ProjectStatus
from paw_backend.repositories import (
    CheckoutExistsError,
    CheckoutState,
    GitCommandError,
    GitFailure,
    GitHubRepo,
    GitHubUnavailableError,
    InputProblem,
    InvalidRepositoryInputError,
    PathProblem,
    PathRejectedError,
    ProjectNotActiveError,
    ProjectUnavailableError,
    RemoteAlreadyRegisteredError,
    RemoteError,
    RemoteProblem,
    RepositoryNameTakenError,
    RepositoryPermissionDeniedError,
    RepositorySource,
    SubprocessGitRunner,
)

from .repositories_support import (
    PostgresRepositoryTestCase,
    fs,
    git,
    requires_git,
    requires_postgres,
)

WATCHED = ("AGENTS.md", "MEMORY.md", ".personal-ai")


def tree(path: str) -> dict[str, bytes | None]:
    """Every file below ``path`` (contents), ``.git`` included: a full snapshot."""
    found: dict[str, bytes | None] = {}
    for directory, names, files in os.walk(path):
        for name in names:
            found[os.path.relpath(f"{directory}/{name}", path)] = None
        for name in files:
            full = f"{directory}/{name}"
            found[os.path.relpath(full, path)] = Path(full).read_bytes()
    return found


class RegistrationTestCase(PostgresRepositoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha Project")
        self.manager = self.seed_user_with_account("alice")
        self.seed_manager(self.project_id, self.manager)
        self.alice = self.actor(self.manager)
        self.home = self.account(self.manager).home

    def actors(self):
        """One user per role: manager, contributor, viewer, and an outsider."""
        contributor = self.seed_user_with_account("carol")
        viewer = self.seed_user_with_account("vera")
        self.seed_member(self.project_id, contributor, ProjectRole.CONTRIBUTOR)
        self.seed_member(self.project_id, viewer, ProjectRole.VIEWER)
        outsider = self.seed_user_with_account("olga")
        return {
            "contributor": self.actor(contributor),
            "viewer": self.actor(viewer),
            "outsider": self.actor(outsider),
        }

    def assertNothingRegistered(self):
        self.assertEqual(self.repository_rows(self.project_id), [])
        self.assertEqual(self.checkout_rows(), [])
        self.assertEqual(self.rows("SELECT * FROM repository_remotes"), [])


@requires_postgres
@requires_git
class RegisterExistingTest(RegistrationTestCase):
    def make(self, name="tool", *, origin=None, branch="main", home=None):
        path = f"{home or self.home}/src/{name}"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path, self.world.make_repository(path, origin=origin, branch=branch)

    def make_for(self, principal: Principal, name="tool"):
        """A repository in the home of ``principal`` (a path only that user may use)."""
        return self.make(name, home=self.account(principal.user_id).home)[0]

    async def test_it_registers_the_repository_and_the_actors_checkout(self):
        path, head = self.make(origin="https://github.com/acme/tool.git")

        result = await self.service.register_existing(self.alice, self.project_id, path)

        repository = result.repository
        self.assertEqual(
            (
                repository.name,
                repository.default_branch,
                repository.source,
                repository.project_id,
                repository.created_by,
            ),
            (
                "tool",
                "main",
                RepositorySource.EXISTING_PATH,
                self.project_id,
                self.manager,
            ),
        )
        self.assertTrue(repository.acl.inherits)
        self.assertEqual(result.head, head)
        self.assertEqual(
            result.remotes,
            ("https://github.com/acme/tool", "https://github.com/acme/tool.git"),
        )
        self.assertEqual(self.remote_urls(repository.id), list(result.remotes))
        checkout = result.checkout
        self.assertEqual(
            (checkout.path, checkout.state, checkout.user_id, checkout.repository_id),
            (path, CheckoutState.READY, self.manager, repository.id),
        )
        (row,) = self.checkout_rows(repository.id)
        self.assertEqual((row["path"], row["state"]), (path, "ready"))

    async def test_the_audit_names_the_new_repository(self):
        path, _ = self.make()

        result = await self.service.register_existing(self.alice, self.project_id, path)

        (event,) = self.sink.events
        self.assertIsInstance(event, AuditEvent)
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
                "project.repo.add",
                "allow",
                "granted_by_project_role",
                "repository",
                result.repository.id,
                self.project_id,
                self.manager,
            ),
        )

    async def test_the_name_defaults_to_the_directory_and_can_be_chosen(self):
        first, _ = self.make("first")
        second, _ = self.make("second")

        one = await self.service.register_existing(self.alice, self.project_id, first)
        two = await self.service.register_existing(
            self.alice, self.project_id, second, name="Renamed.v2"
        )

        self.assertEqual(
            (one.repository.name, two.repository.name), ("first", "Renamed.v2")
        )

    async def test_a_directory_name_that_is_no_valid_name_needs_an_explicit_one(self):
        path, _ = self.make("my repo")

        with self.assertRaises(InvalidRepositoryInputError) as raised:
            await self.service.register_existing(self.alice, self.project_id, path)
        self.assertEqual(
            (raised.exception.field, raised.exception.problem),
            ("name", InputProblem.INVALID_FORMAT),
        )
        result = await self.service.register_existing(
            self.alice, self.project_id, path, name="my-repo"
        )
        self.assertEqual(result.repository.name, "my-repo")

    async def test_the_default_branch_is_the_one_origin_head_points_at(self):
        path, _ = self.make(origin="https://github.com/acme/tool.git")
        git("branch", "develop", cwd=path)
        git(
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/develop",
            cwd=path,
        )

        result = await self.service.register_existing(self.alice, self.project_id, path)

        self.assertEqual(result.repository.default_branch, "develop")

    async def test_without_origin_head_it_is_the_current_branch(self):
        path, _ = self.make(branch="trunk")

        result = await self.service.register_existing(self.alice, self.project_id, path)

        self.assertEqual(result.repository.default_branch, "trunk")

    async def test_a_detached_head_without_origin_head_is_refused(self):
        path, head = self.make()
        git("checkout", "--quiet", "--detach", head, cwd=path)

        with self.assertRaises(PathRejectedError) as raised:
            await self.service.register_existing(self.alice, self.project_id, path)

        self.assertIs(raised.exception.problem, PathProblem.DEFAULT_BRANCH_UNKNOWN)
        self.assertNothingRegistered()

    async def test_a_repository_without_a_commit_registers_with_no_head(self):
        path = f"{self.home}/src/empty"
        os.makedirs(path)
        git("init", "--quiet", "--initial-branch=main", path)

        result = await self.service.register_existing(self.alice, self.project_id, path)

        self.assertIsNone(result.head)
        self.assertEqual(result.repository.default_branch, "main")

    async def test_a_branch_name_the_module_refuses_is_not_registered(self):
        path, _ = self.make(branch="feature/日本語")

        with self.assertRaises(PathRejectedError) as raised:
            await self.service.register_existing(self.alice, self.project_id, path)

        self.assertIs(raised.exception.problem, PathProblem.UNSUPPORTED_BRANCH)

    async def test_the_origin_url_is_registered_only_in_a_safe_form(self):
        cases = {
            "git@github.com:acme/one.git": (
                "https://github.com/acme/one",
                "https://github.com/acme/one.git",
            ),
            "ssh://git@github.com/acme/two": (
                "https://github.com/acme/two",
                "https://github.com/acme/two.git",
            ),
            "https://git.example.org/team/three.git": (
                "https://git.example.org/team/three.git",
            ),
            "git@gitlab.example.org:team/tool.git": (),
            "http://github.com/acme/tool.git": (),
            "/srv/somewhere/tool.git": (),
            "file:///srv/somewhere/tool.git": (),
        }
        for index, (origin, expected) in enumerate(cases.items()):
            with self.subTest(origin=origin):
                path, _ = self.make(f"tool{index}", origin=origin)
                result = await self.service.register_existing(
                    self.alice, self.project_id, path
                )
                self.assertEqual(result.remotes, expected)
                self.assertEqual(self.remote_urls(result.repository.id), list(expected))

    async def test_an_origin_url_with_credentials_is_refused_and_never_stored(self):
        secret = "s3cr3t" + "-token"
        path, _ = self.make(origin=f"https://user:{secret}@github.com/acme/tool.git")

        with self.assertRaises(RemoteError) as raised:
            await self.service.register_existing(self.alice, self.project_id, path)

        self.assertIs(raised.exception.problem, RemoteProblem.HAS_CREDENTIALS)
        self.assertNotIn(secret, str(raised.exception))
        self.assertNothingRegistered()
        self.assertEqual(self.sink.events, [])

    async def test_nothing_is_written_into_the_repository(self):
        path, head = self.make(origin="https://github.com/acme/tool.git")
        before = tree(path)

        await self.service.register_existing(self.alice, self.project_id, path)

        self.assertEqual(tree(path), before)
        self.assertEqual(git("status", "--porcelain", cwd=path), "")
        self.assertEqual(git("rev-parse", "HEAD", cwd=path), head)
        self.assertEqual(git("rev-list", "--count", "HEAD", cwd=path), "1")
        for name in WATCHED:
            self.assertFalse(fs.lexists(f"{path}/{name}"), name)

    async def test_hostile_paths_are_refused_and_nothing_is_registered(self):
        bob = self.seed_user_with_account("bob")
        foreign, _ = self.make("secret", home=self.account(bob).home)
        real, _ = self.make("real")
        os.symlink(real, f"{self.home}/innocent")
        os.symlink(foreign, f"{self.home}/to-bob")
        trick, _ = self.make("trick")
        shutil.rmtree(f"{trick}/.git")
        fs.write(f"{trick}/.git", f"gitdir: {foreign}/.git\n")
        hidden, _ = self.make(".dotfiles/tool")
        cases = {
            "another user's home": (foreign, PathProblem.OUTSIDE_ROOTS),
            "the system": ("/etc", PathProblem.OUTSIDE_ROOTS),
            "a link to a repository": (f"{self.home}/innocent", PathProblem.SYMLINK),
            "a link into another home": (f"{self.home}/to-bob", PathProblem.SYMLINK),
            "a .git file": (trick, PathProblem.GIT_TRICK),
            "a hidden directory": (hidden, PathProblem.HIDDEN),
            "the home itself": (self.home, PathProblem.IS_A_ROOT),
            "a missing path": (f"{self.home}/nothing", PathProblem.NOT_FOUND),
        }
        for label, (path, problem) in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(PathRejectedError) as raised:
                    await self.service.register_existing(
                        self.alice, self.project_id, path, name="x"
                    )
                self.assertIs(raised.exception.problem, problem)
        self.assertNothingRegistered()
        self.assertEqual(self.sink.events, [], "refused before any decision")

    async def test_a_dot_dot_path_is_refused_before_the_file_system_is_read(self):
        for path in (f"{self.home}/../bob/secret", f"{self.home}/src/../../bob"):
            with self.subTest(path=path):
                with self.assertRaises(InvalidRepositoryInputError) as raised:
                    await self.service.register_existing(
                        self.alice, self.project_id, path, name="x"
                    )
                self.assertEqual(raised.exception.field, "path")

    async def test_a_contributor_a_viewer_and_an_outsider_cannot_register(self):
        others = self.actors()

        for role in ("contributor", "viewer"):
            with self.subTest(role=role):
                with self.assertRaises(RepositoryPermissionDeniedError) as raised:
                    await self.service.register_existing(
                        others[role], self.project_id, self.make_for(others[role])
                    )
                self.assertIs(raised.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
        with self.assertRaises(ProjectUnavailableError):
            await self.service.register_existing(
                others["outsider"],
                self.project_id,
                self.make_for(others["outsider"]),
            )
        self.assertNothingRegistered()
        # Every refusal was audited as a denial of the capability.
        self.assertEqual(
            self.audit_actions(),
            [("project.repo.add", "deny", "repository")] * 3,
        )

    async def test_an_owner_or_admin_who_is_no_member_cannot_register(self):
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            user = self.seed_user_with_account(f"boss-{role.value}")
            principal = Principal(user, role)
            with self.subTest(role=role):
                with self.assertRaises(ProjectUnavailableError):
                    await self.service.register_existing(
                        principal, self.project_id, self.make_for(principal)
                    )

    async def test_the_caller_supplied_project_role_is_ignored(self):
        viewer = self.seed_user_with_account("vic")
        self.seed_member(self.project_id, viewer, ProjectRole.VIEWER)
        forged = Principal(
            viewer, SystemRole.USER, {self.project_id: ProjectRole.MANAGER}
        )
        path = self.make_for(forged)

        with self.assertRaises(RepositoryPermissionDeniedError):
            await self.service.register_existing(forged, self.project_id, path)

    async def test_only_an_active_project_takes_a_repository(self):
        path, _ = self.make()
        for status in (ProjectStatus.ARCHIVED, ProjectStatus.PENDING_DELETION):
            project = self.seed_project(status)
            self.seed_manager(project, self.manager)
            with self.subTest(status=status):
                with self.assertRaises(ProjectNotActiveError) as raised:
                    await self.service.register_existing(self.alice, project, path)
                self.assertIs(raised.exception.status, status)
        self.assertNothingRegistered()

    async def test_a_missing_or_deleted_project_is_not_found(self):
        path, _ = self.make()
        deleted = self.seed_project(ProjectStatus.DELETED)
        for project_id in (uuid.uuid4(), deleted):
            with self.subTest(project=project_id):
                with self.assertRaises(ProjectUnavailableError):
                    await self.service.register_existing(self.alice, project_id, path)

    async def test_a_name_is_unique_in_a_project_without_regard_to_case(self):
        first, _ = self.make("first")
        second, _ = self.make("second")
        await self.service.register_existing(
            self.alice, self.project_id, first, name="Tool"
        )

        with self.assertRaises(RepositoryNameTakenError):
            await self.service.register_existing(
                self.alice, self.project_id, second, name="tOOL"
            )

        self.assertEqual(len(self.repository_rows(self.project_id)), 1)
        self.assertEqual(len(self.checkout_rows()), 1)

    async def test_the_same_name_may_exist_in_another_project(self):
        path, _ = self.make()
        other = self.seed_project(name="Beta")
        self.seed_manager(other, self.manager)
        await self.service.register_existing(self.alice, self.project_id, path)
        second, _ = self.make("second")

        result = await self.service.register_existing(
            self.alice, other, second, name="tool"
        )

        self.assertEqual(result.repository.project_id, other)

    async def test_a_directory_belongs_to_one_checkout(self):
        path, _ = self.make()
        other = self.seed_project(name="Beta")
        self.seed_manager(other, self.manager)
        await self.service.register_existing(self.alice, self.project_id, path)

        with self.assertRaises(CheckoutExistsError):
            await self.service.register_existing(self.alice, other, path, name="again")

        self.assertEqual(self.repository_rows(other), [])

    async def test_a_url_belongs_to_one_repository_of_a_project(self):
        first, _ = self.make("first", origin="https://github.com/acme/tool.git")
        second, _ = self.make("second", origin="https://github.com/acme/tool")
        await self.service.register_existing(self.alice, self.project_id, first)

        with self.assertRaises(RemoteAlreadyRegisteredError):
            await self.service.register_existing(self.alice, self.project_id, second)

        # The failed registration left nothing behind, not even the repository row.
        self.assertEqual(len(self.repository_rows(self.project_id)), 1)
        self.assertEqual(len(self.checkout_rows()), 1)

    async def test_the_project_holds_a_bounded_number_of_repositories(self):
        from paw_backend.repositories import RepositoryLimitError, limits

        self.seed_many_repositories(
            self.project_id, limits.MAX_REPOSITORIES_PER_PROJECT
        )
        path, _ = self.make()

        with self.assertRaises(RepositoryLimitError):
            await self.service.register_existing(self.alice, self.project_id, path)

    async def test_an_audit_failure_blocks_the_registration(self):
        path, _ = self.make()

        class BrokenSink:
            async def record(self, event):
                raise RuntimeError("audit store is down")

        from paw_backend.authz import Authorizer
        from paw_backend.repositories import GitClient, RepositoryService

        service = RepositoryService(
            self.service_database(),
            Authorizer(BrokenSink(), clock=self.clock),
            self.accounts,
            GitClient(self.world.runner(), self.policy),
            clock=self.clock,
        )
        self.addAsyncCleanup(service._database.dispose)

        with self.assertRaises(RepositoryPermissionDeniedError) as raised:
            await service.register_existing(self.alice, self.project_id, path)

        self.assertIs(raised.exception.reason, Reason.AUDIT_UNAVAILABLE)
        self.assertNothingRegistered()

    async def test_a_user_without_a_linux_account_cannot_register(self):
        from paw_backend.repositories import LinuxAccountUnavailableError

        path, _ = self.make()
        stranger = self.seed_user()
        self.seed_manager(self.project_id, stranger)

        with self.assertRaises(LinuxAccountUnavailableError):
            await self.service.register_existing(
                self.actor(stranger), self.project_id, path
            )


@requires_postgres
@requires_git
class CloneFromGitHubTest(RegistrationTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bare = self.world.make_bare(
            "acme", "tool", {"README.md": "hello\n", "src/app.py": "print(1)\n"}
        )
        self.project_dir_prefix = "alpha-project-"

    def expected_path(self, name="tool"):
        return (
            f"{self.home}/workspaces/"
            f"{self.project_dir_prefix}{self.project_id.hex[:8]}/{name}"
        )

    async def test_it_clones_into_the_actors_workspace_and_registers(self):
        result = await self.service.clone_from_github(
            self.alice, self.project_id, "acme/tool"
        )

        path = self.expected_path()
        self.assertEqual(result.checkout.path, path)
        self.assertEqual(result.checkout.state, CheckoutState.READY)
        self.assertEqual(fs.read(path, "src/app.py"), "print(1)\n")
        self.assertEqual(
            git("rev-parse", "HEAD", cwd=path), git("rev-parse", "HEAD", cwd=self.bare)
        )
        self.assertEqual(result.head, git("rev-parse", "HEAD", cwd=path))
        repository = result.repository
        self.assertEqual(
            (repository.name, repository.default_branch, repository.source),
            ("tool", "main", RepositorySource.GITHUB_CLONE),
        )
        self.assertEqual(
            self.remote_urls(repository.id),
            ["https://github.com/acme/tool", "https://github.com/acme/tool.git"],
        )
        (row,) = self.checkout_rows(repository.id)
        self.assertEqual((row["path"], row["state"]), (path, "ready"))
        self.assertEqual(
            self.audit_actions(), [("project.repo.add", "allow", "repository")]
        )

    async def test_the_directories_the_backend_made_are_private_to_the_user(self):
        await self.service.clone_from_github(self.alice, self.project_id, "acme/tool")

        workspaces = f"{self.home}/workspaces"
        project_dir = os.path.dirname(self.expected_path())
        for directory in (workspaces, project_dir):
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700, directory)
            self.assertEqual(os.stat(directory).st_uid, os.geteuid())

    async def test_a_full_url_and_the_dot_git_spelling_name_the_same_repository(self):
        for source in (
            "https://github.com/acme/tool",
            "https://GITHUB.com/acme/tool.git",
            "https://github.com/acme/tool/",
        ):
            with self.subTest(source=source):
                project = self.seed_project(name="Beta")
                self.seed_manager(project, self.manager)
                result = await self.service.clone_from_github(
                    self.alice, project, source
                )
                self.assertEqual(result.repository.name, "tool")

    async def test_nothing_is_added_to_the_clone(self):
        result = await self.service.clone_from_github(
            self.alice, self.project_id, "acme/tool"
        )

        path = result.checkout.path
        self.assertEqual(git("status", "--porcelain", cwd=path), "")
        self.assertEqual(git("rev-list", "--count", "HEAD", cwd=path), "1")
        self.assertEqual(sorted(os.listdir(path)), sorted([".git", "README.md", "src"]))
        for name in WATCHED:
            self.assertFalse(fs.lexists(f"{path}/{name}"), name)

    async def test_a_branch_can_be_chosen_and_the_default_branch_stays_the_remotes(
        self,
    ):
        work = f"{self.world.root}/edit"
        git("clone", "--quiet", self.bare, work)
        git("checkout", "--quiet", "-b", "develop", cwd=work)
        fs.write(work, "dev.txt", "dev\n")
        git("add", "-A", cwd=work)
        git("commit", "--quiet", "-m", "dev", cwd=work)
        git("push", "--quiet", "origin", "develop", cwd=work)

        result = await self.service.clone_from_github(
            self.alice, self.project_id, "acme/tool", branch="develop"
        )

        self.assertEqual(
            git("branch", "--show-current", cwd=result.checkout.path), "develop"
        )
        self.assertTrue(fs.exists(f"{result.checkout.path}/dev.txt"))
        self.assertEqual(result.repository.default_branch, "main")

    async def test_a_failed_clone_leaves_nothing_behind(self):
        with self.assertRaises(GitCommandError):
            await self.service.clone_from_github(
                self.alice, self.project_id, "acme/does-not-exist"
            )

        self.assertNothingRegistered()
        self.assertFalse(
            fs.lexists(self.expected_path("does-not-exist")),
            "the directory the call made was removed",
        )

    async def test_the_error_of_a_failed_clone_holds_nothing_git_printed(self):
        with self.assertRaises(GitCommandError) as raised:
            await self.service.clone_from_github(
                self.alice, self.project_id, "acme/does-not-exist"
            )

        text = str(raised.exception)
        self.assertEqual(text, "git clone failed: nonzero_exit")
        self.assertNotIn(self.world.root, text)
        self.assertNotIn("does-not-exist", text)

    async def test_a_host_that_may_not_be_cloned_from_is_refused_before_anything(self):
        with self.assertRaises(InvalidRepositoryInputError) as raised:
            await self.service.clone_from_github(
                self.alice, self.project_id, "https://evil.example.org/acme/tool"
            )

        self.assertEqual(raised.exception.problem, InputProblem.HOST_NOT_ALLOWED)
        self.assertEqual(self.sink.events, [])

    async def test_a_name_that_is_taken_is_refused_before_a_directory_is_made(self):
        self.seed_repository(self.project_id, name="Tool")

        with self.assertRaises(RepositoryNameTakenError):
            await self.service.clone_from_github(
                self.alice, self.project_id, "acme/tool"
            )

        self.assertFalse(fs.lexists(f"{self.home}/workspaces"))

    async def test_only_a_manager_of_an_active_project_can_clone(self):
        others = self.actors()
        for role in ("contributor", "viewer"):
            with self.subTest(role=role):
                with self.assertRaises(RepositoryPermissionDeniedError):
                    await self.service.clone_from_github(
                        others[role], self.project_id, "acme/tool"
                    )
        with self.assertRaises(ProjectUnavailableError):
            await self.service.clone_from_github(
                others["outsider"], self.project_id, "acme/tool"
            )
        self.set_project(self.project_id, status="archived")
        with self.assertRaises(ProjectNotActiveError):
            await self.service.clone_from_github(
                self.alice, self.project_id, "acme/tool"
            )
        self.assertNothingRegistered()
        self.assertFalse(fs.lexists(f"{self.home}/workspaces"))

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
            service.clone_from_github(self.alice, self.project_id, "acme/tool")
        )
        await asyncio.wait_for(started.wait(), 10)
        # While it runs the name and the path are reserved: pending, not usable.
        (pending,) = self.checkout_rows()
        self.assertEqual(pending["state"], "pending")
        self.assertTrue(fs.isdir(pending["path"]))

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertNothingRegistered()
        self.assertFalse(fs.lexists(pending["path"]))

    async def test_a_removed_repository_during_the_clone_leaves_nothing(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class Held(SubprocessGitRunner):
            async def run(self, args, **options):
                result = await super().run(args, **options)
                if args[0] == "clone":
                    started.set()
                    await release.wait()
                return result

        service = self.new_service(runner=Held(**self.world.runner_options()))
        task = self.spawn(
            service.clone_from_github(self.alice, self.project_id, "acme/tool")
        )
        await asyncio.wait_for(started.wait(), 10)
        (pending,) = self.checkout_rows()
        with self.engine.begin() as connection:
            from sqlalchemy import text

            connection.execute(text("DELETE FROM repositories"))
        release.set()

        from paw_backend.repositories import CheckoutGoneError

        with self.assertRaises(CheckoutGoneError):
            await task

        self.assertFalse(fs.lexists(pending["path"]))
        self.assertNothingRegistered()


class RecordingGateway:
    """A stand-in for the GitHub API (PAW-028) that records what it was asked."""

    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result
        self.error = error

    async def create_repository(self, *, user_id, name, private):
        self.calls.append((user_id, name, private))
        if self.error is not None:
            raise self.error
        return self.result or GitHubRepo("github.com", "alice-gh", name)


@requires_postgres
@requires_git
class CreateRepositoryTest(RegistrationTestCase):
    def expected_path(self, name):
        return f"{self.home}/workspaces/alpha-project-{self.project_id.hex[:8]}/{name}"

    async def test_a_new_local_repository_is_empty_and_has_no_remote(self):
        result = await self.service.create_local(self.alice, self.project_id, "fresh")

        path = self.expected_path("fresh")
        self.assertEqual(result.checkout.path, path)
        self.assertEqual(os.listdir(path), [".git"])
        self.assertEqual(git("symbolic-ref", "HEAD", cwd=path), "refs/heads/main")
        self.assertEqual(git("remote", cwd=path), "")
        self.assertEqual(git("status", "--porcelain", cwd=path), "")
        self.assertEqual(git("rev-list", "--all", "--count", cwd=path), "0")
        self.assertIsNone(result.head)
        self.assertEqual(result.remotes, ())
        repository = result.repository
        self.assertEqual(
            (repository.name, repository.default_branch, repository.source),
            ("fresh", "main", RepositorySource.NEW_LOCAL),
        )
        self.assertEqual(self.remote_urls(repository.id), [])
        self.assertEqual(
            self.audit_actions(), [("project.repo.add", "allow", "repository")]
        )

    async def test_no_git_template_files_are_copied_into_a_new_repository(self):
        result = await self.service.create_local(self.alice, self.project_id, "fresh")

        git_dir = f"{result.checkout.path}/.git"
        self.assertFalse(fs.exists(f"{git_dir}/hooks"))
        self.assertFalse(fs.exists(f"{git_dir}/description"))

    async def test_the_initial_branch_can_be_chosen(self):
        result = await self.service.create_local(
            self.alice, self.project_id, "fresh", default_branch="trunk"
        )

        self.assertEqual(result.repository.default_branch, "trunk")
        self.assertEqual(
            git("symbolic-ref", "HEAD", cwd=result.checkout.path), "refs/heads/trunk"
        )

    async def test_a_new_github_repository_gets_origin_and_both_remote_spellings(self):
        gateway = RecordingGateway()
        service = self.new_service(github=gateway)

        result = await service.create_github(
            self.alice, self.project_id, "shared", private=False
        )

        self.assertEqual(gateway.calls, [(self.manager, "shared", False)])
        path = self.expected_path("shared")
        self.assertEqual(
            git("remote", "get-url", "origin", cwd=path),
            "https://github.com/alice-gh/shared.git",
        )
        self.assertEqual(
            self.remote_urls(result.repository.id),
            [
                "https://github.com/alice-gh/shared",
                "https://github.com/alice-gh/shared.git",
            ],
        )
        self.assertEqual(result.repository.source, RepositorySource.NEW_GITHUB)
        self.assertEqual(os.listdir(path), [".git"])

    async def test_a_new_github_repository_is_private_unless_said_otherwise(self):
        gateway = RecordingGateway()
        service = self.new_service(github=gateway)

        await service.create_github(self.alice, self.project_id, "shared")

        self.assertEqual(gateway.calls, [(self.manager, "shared", True)])

    async def test_without_a_github_connection_nothing_is_created(self):
        with self.assertRaises(GitHubUnavailableError):
            await self.service.create_github(self.alice, self.project_id, "shared")

        self.assertNothingRegistered()
        self.assertFalse(fs.lexists(self.expected_path("shared")))

    async def test_a_gateway_failure_says_nothing_about_the_cause(self):
        secret = "ghp_" + "0123456789abcdef"
        gateway = RecordingGateway(error=RuntimeError(f"bad token {secret}"))
        service = self.new_service(github=gateway)

        with self.assertLogs("paw_backend.repositories.service", "WARNING") as logs:
            with self.assertRaises(GitHubUnavailableError) as raised:
                await service.create_github(self.alice, self.project_id, "shared")

        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, "\n".join(logs.output))
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNothingRegistered()
        self.assertFalse(fs.lexists(self.expected_path("shared")))

    async def test_a_github_repository_that_cannot_be_registered_is_logged_and_undone(
        self,
    ):
        taken = self.seed_repository(
            self.project_id,
            name="taken",
            remotes=("https://github.com/alice-gh/shared",),
        )
        gateway = RecordingGateway()
        service = self.new_service(github=gateway)

        with self.assertLogs("paw_backend.repositories.service", "ERROR") as logs:
            with self.assertRaises(RemoteAlreadyRegisteredError):
                await service.create_github(self.alice, self.project_id, "shared")

        self.assertEqual(len(gateway.calls), 1)  # the repository exists on GitHub
        self.assertEqual(
            logs.output,
            [
                "ERROR:paw_backend.repositories.service:A GitHub repository was "
                "created but could not be registered; it was not deleted"
            ],
        )
        self.assertEqual(
            [r["id"] for r in self.repository_rows(self.project_id)], [taken]
        )
        self.assertEqual(self.checkout_rows(), [])
        self.assertFalse(fs.lexists(self.expected_path("shared")))

    async def test_a_failure_of_git_after_the_github_repository_is_logged_too(self):
        class NoRemote(SubprocessGitRunner):
            async def run(self, args, **options):
                if args[0] == "remote":
                    raise GitCommandError("remote", GitFailure.NONZERO_EXIT)
                return await super().run(args, **options)

        service = self.new_service(
            github=RecordingGateway(), runner=NoRemote(**self.world.runner_options())
        )

        with self.assertLogs("paw_backend.repositories.service", "ERROR") as logs:
            with self.assertRaises(GitCommandError):
                await service.create_github(self.alice, self.project_id, "shared")

        self.assertEqual(len(logs.output), 1)
        self.assertIn("was not deleted", logs.output[0])
        self.assertNothingRegistered()
        self.assertFalse(fs.lexists(self.expected_path("shared")))

    async def test_a_repository_on_a_host_that_may_not_be_used_is_refused(self):
        gateway = RecordingGateway(result=GitHubRepo("evil.example.org", "x", "shared"))
        service = self.new_service(github=gateway)

        with self.assertRaises(GitHubUnavailableError):
            await service.create_github(self.alice, self.project_id, "shared")

        self.assertNothingRegistered()

    async def test_new_repositories_need_a_manager_and_a_free_name(self):
        others = self.actors()
        with self.assertRaises(RepositoryPermissionDeniedError):
            await self.service.create_local(
                others["contributor"], self.project_id, "fresh"
            )
        with self.assertRaises(ProjectUnavailableError):
            await self.service.create_local(
                others["outsider"], self.project_id, "fresh"
            )
        self.assertFalse(fs.lexists(f"{self.home}/workspaces"))
        await self.service.create_local(self.alice, self.project_id, "fresh")
        with self.assertRaises(RepositoryNameTakenError):
            await self.service.create_local(self.alice, self.project_id, "FRESH")

    async def test_a_directory_that_is_in_the_way_is_never_reused(self):
        path = self.expected_path("fresh")
        os.makedirs(path)
        fs.write(path, "precious.txt", "mine\n")

        from paw_backend.repositories import PathRejectedError

        with self.assertRaises(PathRejectedError) as raised:
            await self.service.create_local(self.alice, self.project_id, "fresh")

        self.assertIs(raised.exception.problem, PathProblem.EXISTS)
        self.assertEqual(fs.read(path, "precious.txt"), "mine\n")
        self.assertNothingRegistered()
