"""Path safety of the repository module: hostile paths against a real file system.

Every case builds the situation with real directories, links and files below a
temporary directory owned by the test process; nothing here needs a database. The
one thing an unprivileged test cannot create, a directory owned by *another* user,
is simulated by reporting another owner for one path (``foreign_owner``), and the
account of another user is an account with another uid.
"""

import os
import stat
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from paw_backend.repositories import LinuxAccount, PathProblem, PathRejectedError, paths

from .repositories_support import World, git, requires_git

SUBDIR = "workspaces"


def account_in(world: World, name: str = "alice", *, uid: int | None = None):
    return LinuxAccount(
        uuid.uuid4(),
        name,
        os.geteuid() if uid is None else uid,
        world.make_home(name),
    )


@contextmanager
def foreign_owner(path: str, uid: int | None = None):
    """``os.lstat`` reports another owner for exactly ``path`` (nothing else)."""
    real = os.lstat
    uid = os.geteuid() + 1 if uid is None else uid

    def lstat(target, *args, **kwargs):
        info = real(target, *args, **kwargs)
        if os.fspath(target) == path:
            fields = list(info)
            fields[4] = uid
            return os.stat_result(fields)
        return info

    with mock.patch("paw_backend.repositories.paths.os.lstat", lstat):
        yield


class PathTestCase(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.account = account_in(self.world)
        self.home = self.account.home

    def assertRejected(self, problem, function, *args):
        with self.assertRaises(PathRejectedError) as raised:
            function(*args)
        self.assertIs(raised.exception.problem, problem)
        # The message names the reason only, never a path.
        self.assertNotIn(self.world.root, str(raised.exception))

    def check(self, path, roots=("{home}",), account=None):
        return paths.check_existing_repository(
            path, account or self.account, roots, SUBDIR
        )

    def repo(self, relative: str) -> str:
        path = f"{self.home}/{relative}"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.world.make_repository(path)
        return path


@requires_git
class ExistingRepositoryAcceptedTest(PathTestCase):
    def test_a_normal_repository_in_the_home_is_accepted(self):
        path = self.repo("src/tool")
        self.assertEqual(self.check(path), path)

    def test_a_repository_inside_another_one_is_accepted(self):
        outer = self.repo("outer")
        inner = self.repo("outer/vendor/inner")
        self.assertEqual(self.check(inner), inner)
        self.assertEqual(self.check(outer), outer)

    def test_a_group_writable_directory_is_accepted(self):
        path = self.repo("src/tool")
        os.chmod(path, 0o775)
        self.assertEqual(self.check(path), path)

    def test_a_directory_inside_the_checkout_root_is_accepted(self):
        path = self.repo("workspaces/proj/tool")
        self.assertEqual(self.check(path), path)

    def test_roots_with_the_user_placeholder_and_a_subdirectory(self):
        shared = f"{self.world.root}/srv/alice/tool"
        os.makedirs(os.path.dirname(shared))
        self.world.make_repository(shared)
        roots = (f"{self.world.root}/srv/{{user}}", "{home}/src")
        self.assertEqual(self.check(shared, roots), shared)
        inside = self.repo("src/tool")
        self.assertEqual(self.check(inside, roots), inside)
        self.assertRejected(
            PathProblem.OUTSIDE_ROOTS, self.check, self.repo("other/tool"), roots
        )

    def test_a_home_that_is_a_symbolic_link_works_with_the_resolved_spelling(self):
        real = f"{self.world.root}/real-home"
        os.makedirs(f"{real}/src")
        link = f"{self.world.homes}/carol"
        os.symlink(real, link)
        carol = LinuxAccount(uuid.uuid4(), "carol", os.geteuid(), link)
        path = f"{real}/src/tool"
        self.world.make_repository(path)

        self.assertEqual(self.check(path, account=carol), path)
        # The spelling that goes through the link is a symbolic link in the path.
        self.assertRejected(
            PathProblem.SYMLINK, self.check, f"{link}/src/tool", ("{home}",), carol
        )


@requires_git
class ExistingRepositoryRefusedTest(PathTestCase):
    def test_a_missing_path(self):
        self.assertRejected(PathProblem.NOT_FOUND, self.check, f"{self.home}/nothing")

    def test_a_symbolic_link_as_the_repository(self):
        real = self.repo("src/real")
        link = f"{self.home}/src/link"
        os.symlink(real, link)
        self.assertRejected(PathProblem.SYMLINK, self.check, link)

    def test_a_symbolic_link_in_the_middle_of_the_path(self):
        self.repo("src/real/tool")
        os.symlink(f"{self.home}/src/real", f"{self.home}/src/link")
        self.assertRejected(
            PathProblem.SYMLINK, self.check, f"{self.home}/src/link/tool"
        )

    def test_a_link_out_of_the_home_to_a_repository_of_someone_else(self):
        bob = self.world.make_home("bob")
        foreign = f"{bob}/secret"
        self.world.make_repository(foreign)
        os.symlink(foreign, f"{self.home}/innocent")
        # Spelled through the link: refused because it is a link. Spelled as the
        # target: refused because it is not below the user's root.
        self.assertRejected(PathProblem.SYMLINK, self.check, f"{self.home}/innocent")
        self.assertRejected(PathProblem.OUTSIDE_ROOTS, self.check, foreign)

    def test_another_users_home_and_lookalike_prefixes(self):
        bob = self.world.make_home("bob")
        self.world.make_repository(f"{bob}/tool")
        lookalike = self.world.make_home("alice2")
        self.world.make_repository(f"{lookalike}/tool")
        self.assertRejected(PathProblem.OUTSIDE_ROOTS, self.check, f"{bob}/tool")
        self.assertRejected(PathProblem.OUTSIDE_ROOTS, self.check, f"{lookalike}/tool")

    def test_a_path_outside_every_root(self):
        elsewhere = f"{self.world.root}/elsewhere/tool"
        os.makedirs(os.path.dirname(elsewhere))
        self.world.make_repository(elsewhere)
        self.assertRejected(PathProblem.OUTSIDE_ROOTS, self.check, elsewhere)
        self.assertRejected(PathProblem.OUTSIDE_ROOTS, self.check, "/etc")
        self.assertRejected(PathProblem.OUTSIDE_ROOTS, self.check, "/tmp")

    def test_a_root_that_does_not_exist_holds_nothing(self):
        path = self.repo("src/tool")
        self.assertRejected(
            PathProblem.OUTSIDE_ROOTS, self.check, path, (f"{self.home}/missing",)
        )

    def test_the_root_itself_is_not_a_repository_even_when_it_is_one(self):
        git("init", "--quiet", self.home)
        self.assertRejected(PathProblem.IS_A_ROOT, self.check, self.home)
        root = f"{self.home}/src"
        self.world.make_repository(root)
        self.assertRejected(PathProblem.IS_A_ROOT, self.check, root, (root,))

    def test_the_checkout_root_is_not_a_repository(self):
        managed = f"{self.home}/{SUBDIR}"
        self.world.make_repository(managed)
        self.assertRejected(PathProblem.IS_A_ROOT, self.check, managed)

    def test_a_directory_that_contains_the_checkout_root_is_not_a_repository(self):
        # A user named "home" whose root is ``<world>/home``: the directory of
        # alice's home lies below it and contains alice's checkout root.
        git("init", "--quiet", self.home)
        account = LinuxAccount(uuid.uuid4(), "home", os.geteuid(), self.home)
        roots = (f"{self.world.root}/{{user}}",)
        self.assertRejected(
            PathProblem.IS_A_ROOT,
            paths.check_existing_repository,
            self.home,
            account,
            roots,
            SUBDIR,
        )

    def test_hidden_components_below_the_root(self):
        self.repo(".dotfiles/tool")
        self.repo("src/.hidden")
        self.assertRejected(
            PathProblem.HIDDEN, self.check, f"{self.home}/.dotfiles/tool"
        )
        self.assertRejected(PathProblem.HIDDEN, self.check, f"{self.home}/src/.hidden")

    def test_a_dot_directory_inside_the_root_itself_is_no_problem_for_the_root(self):
        # Only components *below* the root count: a root that is itself hidden works.
        hidden_root = f"{self.home}/.repos"
        path = f"{hidden_root}/tool"
        os.makedirs(hidden_root)
        self.world.make_repository(path)
        self.assertEqual(self.check(path, (hidden_root,)), path)

    def test_a_file_is_not_a_repository(self):
        path = f"{self.home}/file"
        Path(path).write_text("x")
        self.assertRejected(PathProblem.NOT_A_DIRECTORY, self.check, path)

    def test_a_directory_that_is_not_a_repository(self):
        path = f"{self.home}/plain"
        os.makedirs(path)
        self.assertRejected(PathProblem.NOT_A_REPOSITORY, self.check, path)

    def test_a_bare_repository_is_not_a_repository_with_a_work_tree(self):
        bare = f"{self.home}/bare.git"
        git("init", "--bare", "--quiet", bare)
        self.assertRejected(PathProblem.NOT_A_REPOSITORY, self.check, bare)

    def test_everybody_can_write_to_the_directory_or_to_git(self):
        path = self.repo("src/tool")
        os.chmod(path, 0o777)
        self.assertRejected(PathProblem.WORLD_WRITABLE, self.check, path)
        os.chmod(path, 0o755)
        os.chmod(f"{path}/.git", 0o777)
        self.assertRejected(PathProblem.WORLD_WRITABLE, self.check, path)

    def test_a_directory_of_another_owner(self):
        path = self.repo("src/tool")
        with foreign_owner(path):
            self.assertRejected(PathProblem.NOT_OWNER, self.check, path)
        with foreign_owner(f"{path}/.git", uid=self.account.uid + 1):
            self.assertRejected(PathProblem.NOT_OWNER, self.check, path)

    def test_an_account_of_another_user_cannot_use_this_users_home(self):
        other = LinuxAccount(uuid.uuid4(), "mallory", os.geteuid() + 1, self.home)
        self.assertRejected(
            PathProblem.HOME_UNSAFE,
            self.check,
            self.repo("src/tool"),
            ("{home}",),
            other,
        )


@requires_git
class GitTricksTest(PathTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.repo("src/tool")
        self.git_dir = f"{self.path}/.git"

    def test_a_git_file_that_points_elsewhere(self):
        other = self.repo("src/other")
        import shutil

        shutil.rmtree(self.git_dir)
        Path(self.git_dir).write_text(f"gitdir: {other}/.git\n")
        self.assertRejected(PathProblem.GIT_TRICK, self.check, self.path)

    def test_a_git_directory_that_is_a_link_to_another_repository(self):
        other = self.repo("src/other")
        import shutil

        shutil.rmtree(self.git_dir)
        os.symlink(f"{other}/.git", self.git_dir)
        self.assertRejected(PathProblem.GIT_TRICK, self.check, self.path)

    def test_a_linked_work_tree_and_a_submodule_have_a_git_file(self):
        worktree = f"{self.home}/src/wt"
        git("worktree", "add", "--quiet", "-b", "side", worktree, cwd=self.path)
        self.assertTrue(os.path.isfile(f"{worktree}/.git"))
        self.assertRejected(PathProblem.GIT_TRICK, self.check, worktree)

    def test_alternates_would_read_the_objects_of_another_repository(self):
        Path(self.git_dir, "objects/info").mkdir(parents=True, exist_ok=True)
        Path(self.git_dir, "objects/info/alternates").write_text("/somewhere/objects\n")
        self.assertRejected(PathProblem.GIT_TRICK, self.check, self.path)

    def test_a_commondir_points_at_another_git_directory(self):
        Path(self.git_dir, "commondir").write_text("../../other/.git\n")
        self.assertRejected(PathProblem.GIT_TRICK, self.check, self.path)

    def test_head_config_objects_and_refs_must_be_what_they_claim(self):
        for name in ("HEAD", "config"):
            with self.subTest(name=name):
                target = f"{self.world.root}/target-{name}"
                Path(target).write_text("x")
                original = Path(self.git_dir, name).read_bytes()
                os.unlink(f"{self.git_dir}/{name}")
                os.symlink(target, f"{self.git_dir}/{name}")
                self.assertRejected(PathProblem.GIT_TRICK, self.check, self.path)
                os.unlink(f"{self.git_dir}/{name}")
                Path(self.git_dir, name).write_bytes(original)
        import shutil

        for name in ("objects", "refs"):
            with self.subTest(name=name):
                moved = f"{self.git_dir}/{name}"
                backup = f"{self.world.root}/backup-{name}"
                shutil.move(moved, backup)
                os.symlink(backup, moved)
                self.assertRejected(PathProblem.GIT_TRICK, self.check, self.path)
                os.unlink(moved)
                shutil.move(backup, moved)
        self.assertEqual(self.check(self.path), self.path)

    def test_a_missing_head_or_refs_is_not_a_repository(self):
        os.unlink(f"{self.git_dir}/HEAD")
        self.assertRejected(PathProblem.NOT_A_REPOSITORY, self.check, self.path)


class CheckoutDirectoryTest(PathTestCase):
    def plan(self, project="alpha-1234abcd", name="tool"):
        return paths.plan_checkout_path(self.account, SUBDIR, project, name)

    def test_the_path_is_below_the_resolved_home(self):
        self.assertEqual(self.plan(), f"{self.home}/{SUBDIR}/alpha-1234abcd/tool")

    def test_creation_makes_private_directories_and_an_empty_checkout(self):
        path = self.plan()
        paths.create_checkout_directory(path, self.account)
        for directory in (
            f"{self.home}/{SUBDIR}",
            f"{self.home}/{SUBDIR}/alpha-1234abcd",
            path,
        ):
            info = os.stat(directory)
            self.assertTrue(stat.S_ISDIR(info.st_mode))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o700, directory)
        self.assertEqual(os.listdir(path), [])

    def test_existing_directories_on_the_way_are_kept_and_shared_by_repositories(self):
        paths.create_checkout_directory(self.plan(name="one"), self.account)
        os.chmod(f"{self.home}/{SUBDIR}", 0o750)
        paths.create_checkout_directory(self.plan(name="two"), self.account)
        self.assertEqual(
            sorted(os.listdir(f"{self.home}/{SUBDIR}/alpha-1234abcd")), ["one", "two"]
        )
        self.assertEqual(stat.S_IMODE(os.stat(f"{self.home}/{SUBDIR}").st_mode), 0o750)

    def test_whatever_is_at_the_path_already_is_never_reused(self):
        path = self.plan()
        paths.create_checkout_directory(path, self.account)
        self.assertRejected(
            PathProblem.EXISTS, paths.create_checkout_directory, path, self.account
        )
        os.rmdir(path)
        for make in (
            lambda: Path(path).write_text("file"),
            lambda: os.symlink(self.home, path),
        ):
            make()
            self.assertRejected(
                PathProblem.EXISTS, paths.create_checkout_directory, path, self.account
            )
            os.unlink(path)

    def test_a_symbolic_link_for_the_workspace_directory_is_refused(self):
        target = f"{self.world.root}/elsewhere"
        os.makedirs(target)
        os.symlink(target, f"{self.home}/{SUBDIR}")
        self.assertRejected(PathProblem.SYMLINK, self.plan)
        self.assertRejected(
            PathProblem.SYMLINK,
            paths.create_checkout_directory,
            f"{self.home}/{SUBDIR}/alpha-1234abcd/tool",
            self.account,
        )
        self.assertEqual(os.listdir(target), [])

    def test_a_symbolic_link_for_the_project_directory_is_refused(self):
        target = f"{self.world.root}/elsewhere"
        os.makedirs(target)
        os.makedirs(f"{self.home}/{SUBDIR}")
        os.symlink(target, f"{self.home}/{SUBDIR}/alpha-1234abcd")
        self.assertRejected(PathProblem.SYMLINK, self.plan)
        self.assertEqual(os.listdir(target), [])

    def test_a_link_swapped_in_after_the_plan_is_still_refused(self):
        path = self.plan()
        target = f"{self.world.root}/elsewhere"
        os.makedirs(target)
        os.symlink(target, f"{self.home}/{SUBDIR}")  # the race: after the plan
        self.assertRejected(
            PathProblem.SYMLINK, paths.create_checkout_directory, path, self.account
        )
        self.assertEqual(os.listdir(target), [])

    def test_a_directory_everybody_can_write_to_is_refused_on_the_way(self):
        os.makedirs(f"{self.home}/{SUBDIR}")
        os.chmod(f"{self.home}/{SUBDIR}", 0o777)
        self.assertRejected(PathProblem.WORLD_WRITABLE, self.plan)

    def test_a_directory_of_another_owner_on_the_way_is_refused(self):
        os.makedirs(f"{self.home}/{SUBDIR}")
        with foreign_owner(f"{self.home}/{SUBDIR}"):
            self.assertRejected(PathProblem.NOT_OWNER, self.plan)

    def test_only_a_path_below_the_home_can_be_created(self):
        for path in (f"{self.world.root}/other/tool", self.home, "/tmp/tool", "/"):
            with self.subTest(path=path):
                self.assertRejected(
                    PathProblem.OUTSIDE_ROOTS,
                    paths.create_checkout_directory,
                    path,
                    self.account,
                )

    def test_dot_segments_are_refused(self):
        for suffix in ("a/../b", "./a", "a//b", "a/."):
            with self.subTest(suffix=suffix):
                self.assertRejected(
                    PathProblem.NOT_CANONICAL,
                    paths.create_checkout_directory,
                    f"{self.home}/{suffix}",
                    self.account,
                )

    def test_an_account_whose_home_is_not_its_own_gets_no_directory(self):
        other = LinuxAccount(uuid.uuid4(), "mallory", os.geteuid() + 1, self.home)
        self.assertRejected(
            PathProblem.HOME_UNSAFE, paths.plan_checkout_path, other, SUBDIR, "p", "r"
        )
        self.assertEqual(os.listdir(self.home), [])

    def test_a_home_that_is_missing_or_the_root_is_unusable(self):
        for home in (f"{self.world.root}/no-such-home", "/"):
            with self.subTest(home=home):
                account = (
                    LinuxAccount(uuid.uuid4(), "eve", os.geteuid(), home)
                    if home != "/"
                    else None
                )
                if account is None:
                    with self.assertRaises(ValueError):
                        LinuxAccount(uuid.uuid4(), "eve", os.geteuid(), home)
                else:
                    self.assertRejected(
                        PathProblem.HOME_UNSAFE, paths.real_home, account
                    )


class RemoveDirectoryTest(PathTestCase):
    def test_a_directory_made_here_is_removed_with_its_contents(self):
        path = self.plan_and_create()
        Path(path, "file").write_text("x")
        os.makedirs(f"{path}/sub/deeper")
        self.assertTrue(paths.remove_directory(path, self.account))
        self.assertFalse(os.path.lexists(path))

    def plan_and_create(self):
        path = paths.plan_checkout_path(self.account, SUBDIR, "p-12345678", "r")
        paths.create_checkout_directory(path, self.account)
        return path

    def test_a_missing_directory_counts_as_removed(self):
        self.assertTrue(paths.remove_directory(f"{self.home}/nothing", self.account))

    def test_a_symbolic_link_is_never_followed_or_removed(self):
        target = f"{self.world.root}/precious"
        os.makedirs(target)
        Path(target, "file").write_text("keep")
        link = f"{self.home}/link"
        os.symlink(target, link)
        self.assertFalse(paths.remove_directory(link, self.account))
        self.assertEqual(Path(target, "file").read_text(), "keep")
        self.assertTrue(os.path.islink(link))

    def test_nothing_outside_the_home_and_not_the_home_itself(self):
        outside = f"{self.world.root}/outside"
        os.makedirs(outside)
        for path in (outside, self.home, "/", "/tmp"):
            with self.subTest(path=path):
                self.assertFalse(paths.remove_directory(path, self.account))
        self.assertTrue(os.path.isdir(outside))
        self.assertTrue(os.path.isdir(self.home))

    def test_a_directory_of_another_owner_is_not_removed(self):
        path = self.plan_and_create()
        with foreign_owner(path):
            self.assertFalse(paths.remove_directory(path, self.account))
        self.assertTrue(os.path.isdir(path))

    def test_a_file_is_not_removed(self):
        path = f"{self.home}/file"
        Path(path).write_text("x")
        self.assertFalse(paths.remove_directory(path, self.account))
        self.assertTrue(os.path.isfile(path))


class LinuxAccountTest(unittest.TestCase):
    def test_a_valid_account(self):
        account = LinuxAccount(str(uuid.uuid4()), "alice", 1000, "/home/alice")
        self.assertIsInstance(account.user_id, uuid.UUID)

    def test_invalid_accounts_are_refused(self):
        user = uuid.uuid4()
        cases = [
            ("not-a-uuid", "alice", 1000, "/home/alice"),
            (user, "", 1000, "/home/alice"),
            (user, "Alice", 1000, "/home/alice"),
            (user, "a b", 1000, "/home/alice"),
            (user, "../x", 1000, "/home/alice"),
            (user, ".hidden", 1000, "/home/alice"),
            (user, "a/b", 1000, "/home/alice"),
            (user, None, 1000, "/home/alice"),
            (user, "alice", -1, "/home/alice"),
            (user, "alice", True, "/home/alice"),
            (user, "alice", "1000", "/home/alice"),
            (user, "alice", 2**32, "/home/alice"),
            (user, "alice", 1000, "home/alice"),
            (user, "alice", 1000, "/"),
            (user, "alice", 1000, ""),
            (user, "alice", 1000, "/home/../etc"),
            (user, "alice", 1000, "/home/alice/"),
            (user, "alice", 1000, "/home//alice"),
            (user, "alice", 1000, "~alice"),
            (user, "alice", 1000, None),
        ]
        for case in cases:
            with self.subTest(case=repr(case)[:60]):
                with self.assertRaises(ValueError):
                    LinuxAccount(*case)


class PureHelpersTest(unittest.TestCase):
    def test_the_project_directory_is_a_readable_slug_and_a_short_id(self):
        project = uuid.UUID("12345678-1234-5678-1234-567812345678")
        cases = {
            "Alpha Project": "alpha-project-12345678",
            "  --Alpha__Beta!!  ": "alpha-beta-12345678",
            "日本語のプロジェクト": "project-12345678",
            "Ünïcödé Nàme": "unicode-name-12345678",
            "a" * 100: "a" * 40 + "-12345678",
            "../../etc": "etc-12345678",
            "x/y\\z": "x-y-z-12345678",
            "": "project-12345678",
            "..": "project-12345678",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(paths.project_directory_name(name, project), expected)

    def test_two_projects_with_one_name_get_different_directories(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        self.assertNotEqual(
            paths.project_directory_name("Same", a),
            paths.project_directory_name("Same", b),
        )

    def test_a_truncated_slug_never_ends_with_a_dash(self):
        name = "a" * 39 + "-" + "b" * 10
        directory = paths.project_directory_name(name, uuid.UUID(int=1))
        self.assertEqual(directory, "a" * 39 + "-00000000")

    def test_ancestor_paths_are_the_proper_ancestors_top_first(self):
        self.assertEqual(paths.ancestor_paths("/a/b/c"), ["/a", "/a/b"])
        self.assertEqual(paths.ancestor_paths("/a/b"), ["/a"])
        self.assertEqual(paths.ancestor_paths("/a"), [])

    def test_a_root_template_is_expanded_by_plain_replacement(self):
        account = LinuxAccount(uuid.uuid4(), "alice", 1000, "/home/alice")
        self.assertEqual(paths.expand_root("{home}/src", account), "/home/alice/src")
        self.assertEqual(paths.expand_root("/srv/{user}", account), "/srv/alice")
        self.assertEqual(paths.expand_root("{home}", account), "/home/alice")


if __name__ == "__main__":
    unittest.main()
