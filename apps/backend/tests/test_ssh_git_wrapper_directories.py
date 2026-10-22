"""``paw-dir/v1``: the wrapper makes a managed checkout's directory (Decision 0097).

Issue #285: under ``git: ssh`` the backend (``paw``) cannot create a directory in
another account's home, so the account itself makes it, through the forced
command, with the guarantees Decision 0017 §1 asks of the backend: every
directory from the root down is opened with ``O_NOFOLLOW`` and must be the
user's, and the checkout directory itself is new. Here the wrapper runs as the
test process's own user, in temporary directories only (no second Linux user, no
``sshd``): a directory of another uid cannot be made without root, so
``not_owner`` is only reached through the backend's own copy of the rule
(``tests/test_repositories_paths.py``).

* :class:`DirectoryPlanTest` calls ``directory_plan``: the one shape the backend
  sends is accepted, every other word, path and depth is refused.
* :class:`DirectoryMainTest` runs ``main``: what is created (and with which
  mode), the answers on stdout and the exit status, and that the log names no
  path.
* :class:`DirectoryEndToEndTest` puts the real wrapper behind
  ``SshCheckoutDirectories`` (the fake ``sshd`` of ``test_ssh_git_wrapper``) and
  clones into the directory it made, through the same wrapper.
"""

import contextlib
import io
import os
import pwd
import shlex
import shutil
import stat
import struct
import sys
import unittest
import uuid

from paw_backend.repositories import (
    GitClient,
    GitCommandError,
    GitFailure,
    LinuxAccount,
    PathProblem,
    PathRejectedError,
    RepositoryPolicy,
)
from paw_backend.repositories.ssh import (
    DIRECTORY_TAG,
    SshCheckoutDirectories,
    SshGitRunner,
    build_directory_command,
)

from .repositories_support import World, fs, requires_git
from .test_ssh_git_wrapper import _FAKE_SSHD, WRAPPER_PATH, _FixedKey, wrapper

PROJECT = "alpha-project-0123abcd"
#: The backend's Linux user in the ACLs (``setfacl -m u:paw:x``): any uid will
#: do, the owner may name a user that does not exist.
BACKEND_UID = 4242
_UNDEFINED = 0xFFFFFFFF
USER_OBJ, USER, GROUP_OBJ, MASK, OTHER = 0x01, 0x02, 0x04, 0x10, 0x20


def mode_of(path: str) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def acl_value(entries) -> bytes:
    """``system.posix_acl_*`` of ``(tag, perm, id)`` entries (sorted by tag)."""
    return struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, perm, _UNDEFINED if ident is None else ident)
        for tag, perm, ident in sorted(entries, key=lambda e: (e[0], e[2] or 0))
    )


def acl_entries(path: str, name: str = wrapper.ACL_ACCESS):
    """The entries of ``path``'s ACL, ``None`` when it has none."""
    try:
        value = os.getxattr(path, name, follow_symlinks=False)
    except OSError:
        return None
    return {
        (tag, perm, None if ident == _UNDEFINED else ident)
        for tag, perm, ident in struct.iter_unpack("<HHI", value[4:])
    }


#: ``setfacl -m u:paw:x`` on a ``0700`` directory: what the deployment does to
#: ``<home>/workspaces`` (PR #288).
BACKEND_TRAVERSES = {
    (USER_OBJ, 7, None),
    (USER, 1, BACKEND_UID),
    (GROUP_OBJ, 0, None),
    (MASK, 1, None),
    (OTHER, 0, None),
}


def acls_supported(path: str) -> bool:
    try:
        os.setxattr(path, wrapper.ACL_ACCESS, acl_value(BACKEND_TRAVERSES))
    except OSError:
        return False
    os.removexattr(path, wrapper.ACL_ACCESS)
    return True


class DirectoryTestCase(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.home = self.world.make_home("alice")
        self.root = f"{self.home}/workspaces"
        os.makedirs(self.root, mode=0o700)
        os.chmod(self.root, 0o700)
        if not acls_supported(self.root):
            self.skipTest("the file system has no POSIX ACLs")
        # The documented deployment (PR #288): ``0700`` and the backend's
        # user may pass through (``setfacl -m u:paw:x``), nobody else.
        os.setxattr(self.root, wrapper.ACL_ACCESS, acl_value(BACKEND_TRAVERSES))
        self.project = f"{self.root}/{PROJECT}"
        self.checkout = f"{self.project}/tool"
        self.outside = f"{self.world.root}/outside"
        os.makedirs(self.outside)
        self.config = wrapper.Config(root=self.root, home=self.home, user="alice")
        # ``main`` names the Linux user it runs as (the test process's).
        self.user = pwd.getpwuid(os.geteuid()).pw_name

    def plan(self, op, path, *, config=None):
        return wrapper.directory_plan(
            build_directory_command(op, path), config or self.config
        )

    def assert_rejected(self, reason, call, *args, **kwargs):
        with self.assertRaises(wrapper.Rejected) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.reason, reason)

    def run_main(self, op, path, *, original=None):
        logged = []
        stdout, stderr = io.StringIO(), io.StringIO()
        environment = {
            "SSH_ORIGINAL_COMMAND": (
                build_directory_command(op, path) if original is None else original
            )
        }
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = wrapper.main(
                [f"--root={self.root}", f"--home={self.home}"],
                environment,
                execve=lambda *args: self.fail("nothing is exec-ed"),
                chdir=lambda path: self.fail("no chdir"),
                log=logged.append,
            )
        return code, stdout.getvalue(), stderr.getvalue(), logged


class DirectoryPlanTest(DirectoryTestCase):
    def test_the_backends_shape_is_accepted(self):
        for op in ("mkdir", "rmdir"):
            with self.subTest(op=op):
                call = self.plan(op, self.checkout)
                self.assertEqual(
                    (call.op, call.root, call.project, call.name),
                    (op, self.root, PROJECT, "tool"),
                )

    def test_the_resolved_spelling_of_the_root_is_accepted_too(self):
        # The account database may spell the home through a link; the backend
        # sends the resolved path (``paths.real_home``).
        linked = f"{self.world.root}/linked-home"
        os.symlink(self.home, linked)
        config = wrapper.Config(root=f"{linked}/workspaces", home=linked, user="alice")
        call = self.plan("mkdir", self.checkout, config=config)
        self.assertEqual(call.root, self.root)
        call = self.plan("mkdir", f"{linked}/workspaces/{PROJECT}/tool", config=config)
        self.assertEqual(call.root, self.root)

    def test_the_command_is_three_fixed_words(self):
        quoted = shlex.quote(self.checkout)
        for original, reason in (
            (f"{DIRECTORY_TAG} mkdir", "bad_protocol"),
            (f"{DIRECTORY_TAG} mkdir {quoted} {quoted}", "bad_protocol"),
            (f"{DIRECTORY_TAG} rm {quoted}", "bad_arguments"),
            (f"{DIRECTORY_TAG} mkdir -p", "bad_path"),
            (f"paw-dir/v2 mkdir {quoted}", "bad_protocol"),
            (f"{DIRECTORY_TAG} mkdir '{self.checkout}", "bad_encoding"),
        ):
            with self.subTest(original=original):
                self.assert_rejected(
                    reason, wrapper.directory_plan, original, self.config
                )

    def test_only_two_levels_below_the_root_are_accepted(self):
        for path, reason in (
            (self.root, "path_outside_root"),
            (self.project, "bad_path"),
            (f"{self.checkout}/deeper", "bad_path"),
            (f"{self.outside}/{PROJECT}/tool", "path_outside_root"),
            (f"{self.home}/{PROJECT}/tool", "path_outside_root"),
            (f"{self.root}x/{PROJECT}/tool", "path_outside_root"),
            ("relative/path", "bad_path"),
            (f"{self.root}/{PROJECT}/../tool", "bad_path"),
            (f"{self.root}//{PROJECT}/tool", "bad_path"),
            (f"{self.checkout}/", "bad_path"),
        ):
            with self.subTest(path=path):
                self.assert_rejected(reason, self.plan, "mkdir", path)

    def test_a_name_must_be_a_plain_directory_name(self):
        for project, name in (
            (".paw-worktrees", "tool"),
            (PROJECT, ".git"),
            (".hidden", "tool"),
            (PROJECT, "-rf"),
            (PROJECT, "a b"),
            (PROJECT, "tool\n"),
            (PROJECT, "x" * 256),
        ):
            with self.subTest(project=project, name=name):
                self.assert_rejected(
                    "bad_path", self.plan, "mkdir", f"{self.root}/{project}/{name}"
                )

    def test_a_missing_root_refuses_everything(self):
        os.rmdir(self.root)
        self.assert_rejected("root_unavailable", self.plan, "mkdir", self.checkout)


class DirectoryMainTest(DirectoryTestCase):
    def test_mkdir_makes_the_project_and_checkout_directories(self):
        previous = os.umask(0o077)  # sshd's umask decides nothing
        self.addCleanup(os.umask, previous)
        code, out, err, logged = self.run_main("mkdir", self.checkout)
        self.assertEqual((code, out, err), (0, "created\n", ""))
        # The root's traversal, nothing more: the backend's user may pass
        # through (``lstat`` a checkout), nobody may list it, other users get
        # nothing at all.
        self.assertEqual(acl_entries(self.project), BACKEND_TRAVERSES)
        self.assertEqual(mode_of(self.project), 0o710)
        self.assertIsNone(acl_entries(self.project, wrapper.ACL_DEFAULT))
        self.assertEqual(mode_of(self.checkout), 0o700)
        self.assertIsNone(acl_entries(self.checkout))
        self.assertEqual(os.listdir(self.checkout), [])
        self.assertEqual(logged, [f"accepted user={self.user} directory=mkdir"])

    def test_the_project_directory_gets_execute_only_from_the_roots_acl(self):
        os.setxattr(
            self.root,
            wrapper.ACL_ACCESS,
            acl_value(
                {
                    (USER_OBJ, 7, None),
                    (USER, 7, BACKEND_UID),
                    (GROUP_OBJ, 5, None),
                    (MASK, 7, None),
                    (OTHER, 0, None),
                }
            ),
        )
        code, _, _, _ = self.run_main("mkdir", self.checkout)
        self.assertEqual(code, 0)
        self.assertEqual(
            acl_entries(self.project),
            {
                (USER_OBJ, 7, None),
                (USER, 1, BACKEND_UID),
                (GROUP_OBJ, 1, None),
                (MASK, 1, None),
                (OTHER, 0, None),
            },
        )

    def test_without_an_acl_the_project_directory_gets_the_roots_execute_bits(self):
        os.removexattr(self.root, wrapper.ACL_ACCESS)
        for root_mode, project_mode in ((0o700, 0o700), (0o711, 0o711), (0o750, 0o710)):
            with self.subTest(root=oct(root_mode)):
                os.chmod(self.root, root_mode)
                code, _, _, _ = self.run_main("mkdir", self.checkout)
                self.assertEqual(code, 0)
                self.assertEqual(mode_of(self.project), project_mode)
                self.assertIsNone(acl_entries(self.project))
                os.rmdir(self.checkout)
                os.rmdir(self.project)

    def test_an_inherited_default_acl_reaches_nothing_below(self):
        # ``setfacl -d -m u:paw:x`` on the root: new directories would inherit
        # it, and git's own directories in the checkout after them.
        os.setxattr(self.root, wrapper.ACL_DEFAULT, acl_value(BACKEND_TRAVERSES))
        code, _, _, _ = self.run_main("mkdir", self.checkout)
        self.assertEqual(code, 0)
        self.assertIsNone(acl_entries(self.project, wrapper.ACL_DEFAULT))
        self.assertEqual(acl_entries(self.project), BACKEND_TRAVERSES)
        self.assertIsNone(acl_entries(self.checkout))
        self.assertIsNone(acl_entries(self.checkout, wrapper.ACL_DEFAULT))
        self.assertEqual(mode_of(self.checkout), 0o700)

    def test_an_existing_project_directorys_default_acl_is_not_inherited(self):
        os.makedirs(self.project, mode=0o700)
        os.setxattr(self.project, wrapper.ACL_DEFAULT, acl_value(BACKEND_TRAVERSES))
        code, _, _, _ = self.run_main("mkdir", self.checkout)
        self.assertEqual(code, 0)
        self.assertIsNone(acl_entries(self.checkout))
        self.assertIsNone(acl_entries(self.checkout, wrapper.ACL_DEFAULT))
        self.assertEqual(mode_of(self.checkout), 0o700)
        # The project directory is not the wrapper's to change.
        self.assertEqual(
            acl_entries(self.project, wrapper.ACL_DEFAULT), BACKEND_TRAVERSES
        )

    def test_an_existing_project_directory_is_used_as_it_is(self):
        os.makedirs(self.project, mode=0o750)
        os.chmod(self.project, 0o750)
        code, out, _, _ = self.run_main("mkdir", self.checkout)
        self.assertEqual((code, out), (0, "created\n"))
        self.assertEqual(mode_of(self.project), 0o750)  # not the wrapper's to change

    def test_an_existing_entry_is_never_reused(self):
        for kind, make in (
            ("directory", lambda: os.makedirs(self.checkout)),
            ("file", lambda: fs.write(self.checkout, "file\n")),
            ("link", lambda: os.symlink(self.outside, self.checkout)),
        ):
            with self.subTest(kind=kind):
                shutil.rmtree(self.project, ignore_errors=True)
                os.makedirs(self.project)
                make()
                code, out, _, logged = self.run_main("mkdir", self.checkout)
                self.assertEqual((code, out), (1, "exists\n"))
                self.assertEqual(
                    logged, [f"rejected user={self.user} directory=mkdir reason=exists"]
                )
                self.assertEqual(os.listdir(self.outside), [])

    def test_a_linked_project_directory_is_never_followed(self):
        os.symlink(self.outside, self.project)
        code, out, _, _ = self.run_main("mkdir", self.checkout)
        self.assertEqual((code, out), (1, "symlink\n"))
        self.assertEqual(os.listdir(self.outside), [])

    def test_a_project_entry_that_is_not_a_directory_is_refused(self):
        fs.write(self.project, "file\n")
        code, out, _, _ = self.run_main("mkdir", self.checkout)
        self.assertEqual((code, out), (1, "not_a_directory\n"))

    def test_a_world_writable_project_directory_is_refused(self):
        os.makedirs(self.project)
        os.chmod(self.project, 0o777)
        code, out, _, _ = self.run_main("mkdir", self.checkout)
        self.assertEqual((code, out), (1, "world_writable\n"))
        self.assertFalse(os.path.lexists(self.checkout))

    def test_a_world_writable_root_refuses_the_call(self):
        os.chmod(self.root, 0o777)
        code, out, err, logged = self.run_main("mkdir", self.checkout)
        self.assertEqual((code, out), (wrapper.REJECTED, ""))
        self.assertIn("root_unsafe", err)
        self.assertFalse(os.path.lexists(self.project))

    def test_rmdir_removes_an_empty_directory_only(self):
        os.makedirs(self.checkout)
        code, out, _, logged = self.run_main("rmdir", self.checkout)
        self.assertEqual((code, out), (0, "removed\n"))
        self.assertFalse(os.path.lexists(self.checkout))
        self.assertTrue(os.path.isdir(self.project))  # the project's stays
        self.assertEqual(logged, [f"accepted user={self.user} directory=rmdir"])

        os.makedirs(f"{self.checkout}/.git")
        code, out, _, _ = self.run_main("rmdir", self.checkout)
        self.assertEqual((code, out), (1, "not_empty\n"))
        self.assertTrue(os.path.isdir(f"{self.checkout}/.git"))

    def test_rmdir_of_something_gone_is_done(self):
        code, out, _, _ = self.run_main("rmdir", self.checkout)
        self.assertEqual((code, out), (0, "removed\n"))  # no project directory
        os.makedirs(self.project)
        code, out, _, _ = self.run_main("rmdir", self.checkout)
        self.assertEqual((code, out), (0, "removed\n"))

    def test_rmdir_never_follows_a_link(self):
        os.symlink(self.outside, self.project)
        os.makedirs(f"{self.outside}/tool")
        code, out, _, _ = self.run_main("rmdir", self.checkout)
        self.assertEqual((code, out), (1, "symlink\n"))
        self.assertTrue(os.path.isdir(f"{self.outside}/tool"))
        os.remove(self.project)
        os.makedirs(self.project)
        os.symlink(f"{self.outside}/tool", self.checkout)
        code, out, _, _ = self.run_main("rmdir", self.checkout)
        self.assertEqual((code, out), (1, "not_a_directory\n"))
        self.assertTrue(os.path.isdir(f"{self.outside}/tool"))

    def test_a_refused_call_prints_and_logs_no_path(self):
        code, out, err, logged = self.run_main("mkdir", f"{self.outside}/x/y")
        self.assertEqual((code, out), (wrapper.REJECTED, ""))
        self.assertEqual(err, "paw-git-wrapper: rejected (path_outside_root)\n")
        self.assertEqual(
            logged, [f"rejected user={self.user} reason=path_outside_root"]
        )
        os.makedirs(self.checkout)
        _, _, err, logged = self.run_main("mkdir", self.checkout)
        for text in (err, *logged):
            self.assertNotIn(self.root, text)
            self.assertNotIn(PROJECT, text)

    def test_an_unexpected_os_error_is_a_refusal_not_a_path_problem(self):
        def broken_fchmod(fd, mode):
            raise PermissionError(1, "denied", self.checkout)

        original = wrapper.os.fchmod
        wrapper.os.fchmod = broken_fchmod
        self.addCleanup(setattr, wrapper.os, "fchmod", original)
        code, out, err, logged = self.run_main("mkdir", self.checkout)
        self.assertEqual((code, out), (wrapper.REJECTED, ""))
        self.assertEqual(
            logged, [f"failed user={self.user} directory=mkdir reason=os_error"]
        )
        self.assertNotIn(self.root, err)


@requires_git
class DirectoryEndToEndTest(unittest.IsolatedAsyncioTestCase):
    """``SshCheckoutDirectories`` → fake ``sshd`` → the real wrapper."""

    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.home = self.world.make_home("alice")
        self.root = f"{self.home}/workspaces"
        os.makedirs(self.root)
        os.chmod(self.root, 0o755)
        self.checkout = f"{self.root}/{PROJECT}/tool"
        self.account = LinuxAccount(uuid.uuid4(), "alice", os.geteuid(), self.home)
        self.key = f"{self.world.root}/alice.key"
        fs.write(self.key, "not a real key\n")
        os.chmod(self.key, 0o600)
        self.options = self.world.runner_options()

    def runner(self, wrapper_path=WRAPPER_PATH) -> SshGitRunner:
        options = [
            f"--root={self.root}",
            f"--home={self.home}",
            f"--git={git_executable()}",
            *(f"--allow-protocol={p}" for p in self.options["allowed_protocols"]),
            *(f"--config={k}={v}" for k, v in self.options["extra_config"]),
        ]
        path = f"{self.world.root}/fake-sshd.py"
        fs.write(
            path,
            _FAKE_SSHD.replace("__PYTHON__", repr(sys.executable))
            .replace("__WRAPPER__", repr(str(wrapper_path)))
            .replace("__OPTIONS__", repr(options)),
        )
        os.chmod(path, 0o755)
        return SshGitRunner(_FixedKey(self.key), ssh_executable=path, **self.options)

    async def test_the_directory_is_made_by_the_account_and_cloned_into(self):
        self.world.make_bare("acme", "tool", {"README.md": "hello\n"})
        runner = self.runner()
        directories = SshCheckoutDirectories(runner, timeout_s=60)
        await directories.create(self.checkout, self.account)
        # A root without an ACL (``0755`` here): its execute bits.
        self.assertEqual(mode_of(f"{self.root}/{PROJECT}"), 0o711)
        self.assertEqual(mode_of(self.checkout), 0o700)
        client = GitClient(runner, RepositoryPolicy())
        await client.clone(
            "https://github.com/acme/tool.git", self.checkout, self.account
        )
        self.assertEqual(fs.read(self.checkout, "README.md"), "hello\n")
        self.assertEqual(mode_of(self.checkout), 0o700)  # git keeps it

        with self.assertRaises(PathRejectedError) as caught:
            await directories.create(self.checkout, self.account)
        self.assertEqual(caught.exception.problem, PathProblem.EXISTS)
        # Not empty: the clean-up leaves it (and says so).
        self.assertFalse(await directories.remove(self.checkout, self.account))
        self.assertTrue(fs.isdir(f"{self.checkout}/.git"))

    async def test_an_empty_directory_is_cleaned_up(self):
        directories = SshCheckoutDirectories(self.runner(), timeout_s=60)
        await directories.create(self.checkout, self.account)
        self.assertTrue(await directories.remove(self.checkout, self.account))
        self.assertFalse(fs.lexists(self.checkout))
        self.assertTrue(await directories.remove(self.checkout, self.account))

    async def test_a_link_answers_with_its_problem(self):
        outside = f"{self.world.root}/outside"
        os.makedirs(outside)
        os.symlink(outside, f"{self.root}/{PROJECT}")
        directories = SshCheckoutDirectories(self.runner(), timeout_s=60)
        with self.assertRaises(PathRejectedError) as caught:
            await directories.create(self.checkout, self.account)
        self.assertEqual(caught.exception.problem, PathProblem.SYMLINK)
        self.assertEqual(os.listdir(outside), [])

    async def test_a_wrapper_that_does_not_know_the_tag_is_a_git_failure(self):
        # A wrapper from before Decision 0097 answers ``bad_protocol`` (126): not
        # a problem of the path, so not reported as one.
        old = f"{self.world.root}/old-wrapper.py"
        fs.write(
            old,
            "import sys\nsys.stderr.write('paw-git-wrapper: rejected "
            "(bad_protocol)\\n')\nsys.exit(126)\n",
        )
        directories = SshCheckoutDirectories(self.runner(old), timeout_s=60)
        with self.assertRaises(GitCommandError) as caught:
            await directories.create(self.checkout, self.account)
        self.assertEqual(caught.exception.failure, GitFailure.NONZERO_EXIT)
        self.assertFalse(await directories.remove(self.checkout, self.account))
        self.assertFalse(fs.lexists(f"{self.root}/{PROJECT}"))


def git_executable() -> str:
    found = shutil.which("git")
    assert found is not None
    return found
