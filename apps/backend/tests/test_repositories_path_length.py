"""A generated checkout path that would not fit the database is refused up front.

``LinuxAccount`` allows a home directory of up to 1024 characters, and the checkout
path appends ``workspaces/<project>/<repository>`` to it, so a valid account can
generate a path longer than ``ck_repository_checkouts_path_valid`` accepts. The service
must check the whole generated path before it inserts anything and refuse it with the
typed path error, never with a raw ``IntegrityError``. Real PostgreSQL and real git on
local temporary repositories; skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import os

from paw_backend.authz.roles import ProjectRole
from paw_backend.repositories import (
    InputProblem,
    InvalidRepositoryInputError,
    LinuxAccount,
    PathProblem,
    PathRejectedError,
    limits,
)
from paw_backend.repositories.paths import project_directory_name

from .repositories_support import (
    PostgresRepositoryTestCase,
    fs,
    requires_git,
    requires_postgres,
)


def make_directory_of_length(base: str, length: int) -> str:
    """Create nested directories below ``base`` so that the last one is ``length`` long.

    Components are at most 200 characters (a file name holds 255 bytes), never empty.
    """
    path = base
    os.makedirs(path)
    while len(path) < length:
        remaining = length - len(path)  # "/" plus the next component
        component = min(200, remaining - 1)
        if remaining - (component + 1) == 1:  # the rest could not hold a component
            component -= 1
        path = f"{path}/{'h' * component}"
        os.mkdir(path)
    assert len(path) == length, (len(path), length)
    return path


@requires_postgres
@requires_git
class GeneratedPathLengthTest(PostgresRepositoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha Project")
        self.manager = self.seed_user_with_account("alice")
        self.seed_manager(self.project_id, self.manager)
        self.alice = self.actor(self.manager)
        self.projdir = project_directory_name("Alpha Project", self.project_id)

    def home_for(self, username: str, repository_name: str, total: int) -> str:
        """A home for which ``repository_name``'s checkout path is ``total`` long."""
        suffix = f"/workspaces/{self.projdir}/{repository_name}"
        return make_directory_of_length(
            f"{self.world.root}/long-{username}", total - len(suffix)
        )

    def live_in(self, user_id, username: str, home: str) -> None:
        self.accounts.accounts[user_id] = LinuxAccount(
            user_id, username, os.geteuid(), home
        )

    async def test_a_path_of_exactly_the_limit_is_accepted(self):
        self.live_in(
            self.manager, "alice", self.home_for("alice", "r", limits.MAX_PATH_CHARS)
        )

        result = await self.service.create_local(self.alice, self.project_id, "r")

        self.assertEqual(len(result.checkout.path), limits.MAX_PATH_CHARS)
        (row,) = self.checkout_rows(result.repository.id)
        self.assertEqual(len(row["path"]), limits.MAX_PATH_CHARS)
        self.assertTrue(fs.isdir(f"{row['path']}/.git"))

    async def test_a_path_one_character_over_the_limit_is_refused_before_any_insert(
        self,
    ):
        home = self.home_for("alice", "r", limits.MAX_PATH_CHARS)
        self.live_in(self.manager, "alice", home)

        with self.assertRaises(PathRejectedError) as raised:
            await self.service.create_local(self.alice, self.project_id, "rr")

        self.assertIs(raised.exception.problem, PathProblem.TOO_LONG)
        self.assertEqual(str(raised.exception), "Path rejected: too_long")
        self.assertEqual(self.repository_rows(self.project_id), [])
        self.assertEqual(self.checkout_rows(), [])
        self.assertFalse(fs.lexists(f"{home}/workspaces"), "nothing was created")

    async def test_the_clone_route_applies_the_same_limit(self):
        self.world.make_bare("acme", "tool")
        self.live_in(
            self.manager, "alice", self.home_for("alice", "tool", limits.MAX_PATH_CHARS)
        )

        result = await self.service.clone_from_github(
            self.alice, self.project_id, "acme/tool"
        )
        self.assertEqual(len(result.checkout.path), limits.MAX_PATH_CHARS)
        with self.assertRaises(PathRejectedError) as raised:
            await self.service.clone_from_github(
                self.alice, self.project_id, "acme/tool", name="tool2"
            )

        self.assertIs(raised.exception.problem, PathProblem.TOO_LONG)
        self.assertEqual(len(self.repository_rows(self.project_id)), 1)

    async def test_a_members_checkout_applies_the_same_limit(self):
        member = self.seed_user()
        self.seed_member(self.project_id, member, ProjectRole.CONTRIBUTOR)
        self.live_in(
            member, "bob", self.home_for("bob", "toolong", limits.MAX_PATH_CHARS)
        )
        repository = self.seed_repository(
            self.project_id,
            name="toolong1",  # one character more than that home leaves room for
            remotes=("https://github.com/acme/tool.git",),
        )

        with self.assertRaises(PathRejectedError) as raised:
            await self.service.create_checkout(
                self.actor(member), self.project_id, repository
            )

        self.assertIs(raised.exception.problem, PathProblem.TOO_LONG)
        self.assertEqual(self.checkout_rows(repository), [])

    async def test_an_existing_path_is_bounded_by_the_same_limit(self):
        with self.assertRaises(InvalidRepositoryInputError) as raised:
            await self.service.register_existing(
                self.alice, self.project_id, "/" + "a" * limits.MAX_PATH_CHARS
            )
        self.assertEqual(raised.exception.problem, InputProblem.TOO_LONG)
