"""The forced-command wrapper of Decision 0029 / 0036 §13 (Issue #134).

``apps/backend/deploy/ssh-git-wrapper/paw_git_wrapper.py`` runs on the server as
a workspace user; here it runs as the test process's own user, in temporary
directories only (no second Linux user, no ``sshd``, no SSH key, no other
user's home). Three layers:

* :class:`PlanTest` and friends call ``plan`` directly: every shape the backend
  sends is accepted, and everything else — other paths for ``--git-dir``,
  symbolic links that lead out of the root, ``-c`` values outside the fixed list,
  ``push`` / ``checkout`` and the rest — is refused.
* :class:`MainTest` checks what is ``exec``-ed (argv, a fixed environment, the
  directory), the exit status of a refusal, and that nothing secret reaches the
  log or stderr.
* :class:`EndToEndTest` puts the real wrapper behind ``SshGitRunner`` (a fake
  ``ssh`` that does what ``sshd`` does with a forced command: set
  ``$SSH_ORIGINAL_COMMAND`` and run the wrapper) and drives real git with
  ``GitClient`` and the worktree commands of Decision 0036.
"""

import contextlib
import http.server
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import threading
import unittest
import uuid
from pathlib import Path

from paw_backend.repositories import GitClient, LinuxAccount, RepositoryPolicy
from paw_backend.repositories.git import git_config_arguments
from paw_backend.repositories.ssh import (
    WRAPPER_REJECTED_CODE,
    SshGitRunner,
    build_remote_command,
)

from .repositories_support import World, fs, git, requires_git

WRAPPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "ssh-git-wrapper"
    / "paw_git_wrapper.py"
)


def _load_wrapper():
    spec = importlib.util.spec_from_file_location("paw_git_wrapper", WRAPPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wrapper = _load_wrapper()

COMMIT = "0123456789abcdef0123456789abcdef01234567"
BRANCH = "paw/2f1b2c3d-0000-4000-8000-000000000001/1/build"
OTHER = "paw/2f1b2c3d-0000-4000-8000-000000000001/1/_integration"
MERGE_CONFIG = [
    "-c",
    "user.name=Personal AI Workspace",
    "-c",
    "user.email=integration@paw.invalid",
    "-c",
    "commit.gpgSign=false",
    "-c",
    "merge.verifySignatures=false",
]


def run_planned(invocation):
    """Run what the wrapper would ``exec`` (with this machine's git)."""
    return subprocess.run(
        [shutil.which("git"), *invocation.argv[1:]],
        cwd=invocation.cwd,
        env={**invocation.env, "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=False,
    )


def hardened(invocation):
    """git and the wrapper's own ``-c`` of ``invocation`` (what every check
    before the call runs with too): everything up to ``credential.helper=``."""
    return invocation.argv[: invocation.argv.index("credential.helper=") + 1]


class WrapperTestCase(unittest.TestCase):
    """A home with ``workspaces/`` (a checkout, a worktree of it) and, next to
    it, ``outside/`` standing in for everything the wrapper must not reach
    (another user's home, ``/etc``, ...)."""

    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.home = self.world.make_home("alice")
        self.root = f"{self.home}/workspaces"
        self.worktrees = f"{self.root}/.paw-worktrees"
        self.checkout = f"{self.root}/project"
        self.worktree = f"{self.worktrees}/task/1/repo/build"
        self.git_dir = f"{self.checkout}/.git/worktrees/build"
        self.outside = f"{self.world.root}/outside"
        self.fresh = f"{self.root}/fresh"  # an empty directory, for `init`
        for path in (self.worktree, self.git_dir, self.outside, self.fresh):
            os.makedirs(path)
        os.makedirs(f"{self.outside}/.git/worktrees/build")
        for git_dir in (self.git_dir, f"{self.outside}/.git/worktrees/build"):
            with open(f"{git_dir}/commondir", "w", encoding="utf-8") as file:
                file.write("../..\n")  # what git itself writes
        with open(f"{self.git_dir}/gitdir", "w", encoding="utf-8") as file:
            file.write(f"{self.worktree}/.git\n")  # likewise
        self.config = self.make_config()

    def make_config(self, **options):
        values = {"root": self.root, "home": self.home, "user": "alice"}
        values.update(options)
        return wrapper.Config(**values)

    def command(self, args, *, cwd=None, ceiling=None, extra_config=()):
        """What ``SshGitRunner`` sends for ``args`` (the real encoder)."""
        return build_remote_command(
            args,
            cwd=self.checkout if cwd is None else cwd,
            ceiling=ceiling,
            allowed_protocols=("https",),
            extra_config=extra_config,
        )

    def plan(self, args, *, config=None, **options):
        return wrapper.plan(self.command(args, **options), config or self.config)

    def pinned(self, args, *, git_dir=None, work_tree=None, cwd=None):
        return self.plan(
            [
                f"--git-dir={git_dir or self.git_dir}",
                f"--work-tree={work_tree or self.worktree}",
                *args,
            ],
            cwd=cwd or self.worktree,
        )

    def assert_rejected(self, reason, call, *args, **kwargs):
        with self.assertRaises(wrapper.Rejected) as caught:
            call(*args, **kwargs)
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)


class PlanTest(WrapperTestCase):
    """Every call the backend makes is accepted, exactly as sent."""

    def accepted(self):
        url = "https://github.com/owner/repo.git"
        destination = f"{self.root}/new"
        return [
            # Decision 0029 §3 (GitClient)
            (["rev-parse", "--is-bare-repository"], {}),
            (["rev-parse", "--show-toplevel", "--absolute-git-dir"], {}),
            (["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], {}),
            (["symbolic-ref", "--quiet", "--short", "HEAD"], {}),
            (["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], {}),
            (["config", "--local", "--get", "remote.origin.url"], {}),
            (["clone", "--quiet", "--", url, destination], {"cwd": self.root}),
            (
                ["clone", "--quiet", "--branch", "dev", "--", url, destination],
                {"cwd": self.root},
            ),
            (
                [
                    "init",
                    "--quiet",
                    "--template=",
                    "--initial-branch=main",
                    "--",
                    self.fresh,
                ],
                {"cwd": self.fresh},
            ),
            (["remote", "add", "--", "origin", url], {"ceiling": self.root}),
            # Decision 0036 §13, on the checkout
            (
                ["rev-parse", "--verify", "--quiet", f"refs/heads/{BRANCH}^{{commit}}"],
                {},
            ),
            (["rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}"], {}),
            (
                [
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    "refs/remotes/origin/main^{commit}",
                ],
                {},
            ),
            (["rev-parse", "--path-format=absolute", "--git-common-dir"], {}),
            (["symbolic-ref", "--quiet", "HEAD"], {}),
            (["worktree", "list", "--porcelain", "-z"], {}),
            (["worktree", "prune"], {}),
            (
                [
                    "worktree",
                    "add",
                    "--quiet",
                    "-b",
                    BRANCH,
                    "--",
                    f"{self.worktrees}/task/1/repo/next",
                    COMMIT,
                ],
                {},
            ),
            (["worktree", "add", "--quiet", "--", self.worktree + "2", BRANCH], {}),
            (
                [
                    "merge-base",
                    "--is-ancestor",
                    f"refs/heads/{BRANCH}",
                    f"refs/heads/{OTHER}",
                ],
                {},
            ),
            (
                [
                    "merge-tree",
                    "--write-tree",
                    "--name-only",
                    "-z",
                    "--no-messages",
                    f"refs/heads/{OTHER}",
                    f"refs/heads/{BRANCH}",
                ],
                {},
            ),
            # Decision 0036 §13, unpinned inside a worktree: rev-parse only
            (["rev-parse", "--show-toplevel"], {"cwd": self.worktree}),
            (
                ["rev-parse", "--path-format=absolute", "--git-dir"],
                {"cwd": self.worktree, "ceiling": os.path.dirname(self.worktree)},
            ),
        ]

    def test_every_backend_call_is_accepted_as_sent(self):
        for args, options in self.accepted():
            with self.subTest(args=args):
                invocation = self.plan(args, **options)
                self.assertEqual(invocation.subcommand, args[0])
                self.assertEqual(invocation.argv[-len(args) :], args)
                self.assertEqual(invocation.argv[0], self.config.git)

    def test_pinned_worktree_calls_are_accepted(self):
        for args in (
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            ["symbolic-ref", "--quiet", "HEAD"],
            ["rev-parse", "--verify", "--quiet", "MERGE_HEAD^{commit}"],
            ["merge", "--abort"],
            [
                *MERGE_CONFIG,
                "merge",
                "--no-ff",
                "--no-edit",
                "--quiet",
                "-m",
                f"Integrate {BRANCH}",
                f"refs/heads/{BRANCH}",
            ],
        ):
            with self.subTest(args=args):
                invocation = self.pinned(args)
                self.assertIn(f"--git-dir={self.git_dir}", invocation.argv)
                self.assertIn(f"--work-tree={self.worktree}", invocation.argv)
                self.assertEqual(invocation.cwd, self.worktree)

    def test_the_client_hardening_is_replaced_by_the_wrappers_own(self):
        invocation = self.plan(["rev-parse", "--is-bare-repository"])
        argv = invocation.argv
        subcommand_at = argv.index("rev-parse")
        self.assertEqual(
            argv[1:subcommand_at],
            [
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "submodule.recurse=false",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                "-c",
                "diff.ignoreSubmodules=all",
                "-c",
                "maintenance.auto=false",
                # No credential helper of the repository's (only clone has one).
                "-c",
                "credential.helper=",
            ],
        )
        clone = self.plan(
            [
                "clone",
                "--quiet",
                "--",
                "https://github.com/o/r.git",
                f"{self.root}/new",
            ],
            cwd=self.root,
        )
        self.assertNotIn("credential.helper=", clone.argv)

    def test_the_merge_identity_is_the_wrappers_own_even_when_not_sent(self):
        invocation = self.pinned(
            [
                "merge",
                "--no-ff",
                "--no-edit",
                "--quiet",
                "-m",
                f"Integrate {BRANCH}",
                f"refs/heads/{BRANCH}",
            ]
        )
        for word in MERGE_CONFIG[1::2]:
            self.assertIn(word, invocation.argv)

    def test_the_environment_is_a_fixed_allowlist(self):
        invocation = self.plan(["rev-parse", "--is-bare-repository"], ceiling=self.root)
        self.assertEqual(
            invocation.env,
            {
                "PATH": wrapper.SAFE_PATH,
                "HOME": self.home,
                "LC_ALL": "C",
                "LANG": "C",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_NO_LAZY_FETCH": "1",
                # The client's ceiling, then the parent of the root, always.
                "GIT_CEILING_DIRECTORIES": f"{self.root}:{self.home}",
            },
        )
        invocation = self.plan(["rev-parse", "--is-bare-repository"])
        self.assertEqual(invocation.env["GIT_CEILING_DIRECTORIES"], self.home)

    def test_the_resolved_directory_is_used(self):
        link = f"{self.root}/link"
        os.symlink(self.checkout, link)
        invocation = self.plan(["rev-parse", "--is-bare-repository"], cwd=link)
        self.assertEqual(invocation.cwd, self.checkout)

    def test_a_credential_helper_is_accepted_only_as_configured(self):
        config = self.make_config(gh="/usr/bin/gh")
        args = [
            "clone",
            "--quiet",
            "-c",
            "credential.helper=!/usr/bin/gh auth git-credential",
            "--",
            "https://github.com/o/r.git",
            f"{self.root}/new",
        ]
        self.plan(args, cwd=self.root, config=config)
        self.assert_rejected(
            "config_not_allowed", self.plan, args, cwd=self.root
        )  # no --gh configured
        other = [*args]
        other[3] = "credential.helper=!/tmp/evil auth git-credential"
        self.assert_rejected(
            "config_not_allowed", self.plan, other, cwd=self.root, config=config
        )

    def test_extra_configuration_is_accepted_only_when_configured(self):
        pair = ("url.file:///srv/mirror/.insteadOf", "https://github.com/")
        args = ["rev-parse", "--is-bare-repository"]
        config = self.make_config(extra=(pair,))
        self.plan(args, extra_config=[pair], config=config)
        self.assert_rejected("config_not_allowed", self.plan, args, extra_config=[pair])


class RejectedPathTest(WrapperTestCase):
    """cwd, ``--git-dir=``, ``--work-tree=`` and path arguments stay in the root."""

    def test_a_cwd_outside_the_root_is_refused(self):
        args = ["rev-parse", "--is-bare-repository"]
        for cwd in (
            self.outside,
            self.home,
            "/",
            "/etc",
            f"{self.root}/../outside",
            f"{self.root}/project/",
            f"{self.root}//project",
            "project",
            ".",
            f"{self.root}/missing",
        ):
            with self.subTest(cwd=cwd):
                self.assert_rejected(None, self.plan, args, cwd=cwd)

    def test_a_symbolic_link_out_of_the_root_is_refused(self):
        os.symlink(self.outside, f"{self.root}/escape")
        args = ["rev-parse", "--is-bare-repository"]
        self.assert_rejected(
            "path_outside_root", self.plan, args, cwd=f"{self.root}/escape"
        )
        os.symlink(f"{self.root}/nowhere", f"{self.root}/dangling")
        self.assert_rejected(
            "path_unresolvable", self.plan, args, cwd=f"{self.root}/dangling"
        )
        os.symlink(f"{self.root}/loop", f"{self.root}/loop")
        self.assert_rejected(None, self.plan, args, cwd=f"{self.root}/loop")

    def test_a_linked_worktree_area_refuses_everything(self):
        shutil.rmtree(self.worktrees)
        os.symlink(self.checkout, self.worktrees)
        self.assert_rejected(
            "bad_worktrees", self.plan, ["rev-parse", "--is-bare-repository"]
        )

    def test_the_root_itself_being_unavailable_refuses_everything(self):
        config = self.make_config(root=f"{self.home}/missing")
        self.assert_rejected(
            "root_unavailable",
            self.plan,
            ["rev-parse", "--is-bare-repository"],
            config=config,
        )

    def test_a_git_dir_outside_the_root_is_refused(self):
        for git_dir in (
            f"{self.outside}/.git/worktrees/build",
            f"{self.root}/../outside/.git/worktrees/build",
            "/etc",
            "relative/.git/worktrees/build",
            f"{self.git_dir}/",
        ):
            with self.subTest(git_dir=git_dir):
                self.assert_rejected(
                    None,
                    self.pinned,
                    ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
                    git_dir=git_dir,
                )

    def test_a_git_dir_through_a_symbolic_link_out_of_the_root_is_refused(self):
        link = f"{self.checkout}/.git/worktrees/evil"
        os.symlink(f"{self.outside}/.git/worktrees/build", link)
        self.assert_rejected(
            "path_outside_root",
            self.pinned,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            git_dir=link,
        )
        # A whole parent directory swapped for a link counts the same.
        os.symlink(self.outside, f"{self.root}/project2")
        self.assert_rejected(
            "path_outside_root",
            self.pinned,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            git_dir=f"{self.root}/project2/.git/worktrees/build",
        )

    def test_a_git_dir_that_is_not_a_worktree_directory_is_refused(self):
        os.makedirs(f"{self.worktree}/.git/worktrees/x")
        for git_dir, reason in (
            (f"{self.checkout}/.git", "bad_git_dir"),
            (self.checkout, "bad_git_dir"),
            # inside .paw-worktrees: an agent writes there
            (f"{self.worktree}/.git/worktrees/x", "path_in_worktrees"),
        ):
            with self.subTest(git_dir=git_dir):
                self.assert_rejected(
                    reason,
                    self.pinned,
                    ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
                    git_dir=git_dir,
                )

    def test_a_git_dir_whose_common_dir_leads_elsewhere_is_refused(self):
        status = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
        commondir = f"{self.git_dir}/commondir"
        os.makedirs(f"{self.root}/other/.git")
        cases = (
            f"{self.outside}/.git\n",  # another repository, outside the root
            f"{self.root}/other/.git\n",  # another repository, inside the root
            "../../..\n",  # the checkout, not its .git
            "\n",
            "does-not-exist\n",
            "\xff\n",
        )
        for content in cases:
            with self.subTest(content=content):
                with open(
                    commondir, "w", encoding="utf-8", errors="surrogateescape"
                ) as f:
                    f.write(content)
                self.assert_rejected("bad_git_dir", self.pinned, status)
        # An absolute path to its own .git is what git may write too.
        with open(commondir, "w", encoding="utf-8") as f:
            f.write(f"{self.checkout}/.git\n")
        self.pinned(status)
        # A link through which it leads out is refused, and so is the file
        # itself being a link or missing.
        os.symlink(self.outside, f"{self.root}/escape")
        with open(commondir, "w", encoding="utf-8") as f:
            f.write(f"{self.root}/escape/.git\n")
        self.assert_rejected("bad_git_dir", self.pinned, status)
        os.remove(commondir)
        self.assert_rejected("bad_git_dir", self.pinned, status)
        with open(f"{self.outside}/commondir", "w", encoding="utf-8") as f:
            f.write(f"{self.checkout}/.git\n")
        os.symlink(f"{self.outside}/commondir", commondir)
        self.assert_rejected("bad_git_dir", self.pinned, status)

    def test_a_work_tree_outside_the_worktrees_is_refused(self):
        os.symlink(self.outside, f"{self.worktrees}/escape")
        for work_tree in (
            self.checkout,
            self.outside,
            self.worktrees,
            f"{self.worktrees}/escape",
        ):
            with self.subTest(work_tree=work_tree):
                self.assert_rejected(
                    None,
                    self.pinned,
                    ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
                    work_tree=work_tree,
                    cwd=work_tree if os.path.isdir(work_tree) else None,
                )

    def test_a_work_tree_other_than_the_cwd_is_refused(self):
        other = f"{self.worktrees}/task/1/repo/other"
        os.makedirs(other)
        self.assert_rejected(
            "bad_work_tree",
            self.pinned,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            work_tree=other,
        )

    def test_a_git_dir_of_another_work_tree_is_refused(self):
        status = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
        gitdir = f"{self.git_dir}/gitdir"
        other = f"{self.worktrees}/task/1/other/build"
        os.makedirs(other)
        os.symlink(self.worktree, f"{self.root}/to-worktree")
        cases = (
            f"{other}/.git\n",  # another worktree (another repository's, say)
            f"{self.outside}/.git\n",
            f"{self.worktree}/.git/x\n",
            f"{self.worktree}\n",
            f"{self.worktree}/missing/.git\n",
            "\n",
            "\xff\n",
        )
        for content in cases:
            with self.subTest(content=content):
                with open(gitdir, "w", encoding="utf-8", errors="surrogateescape") as f:
                    f.write(content)
                self.assert_rejected("bad_git_dir", self.pinned, status)
        # The same work tree through a link, or as a relative path (git's
        # worktree.useRelativePaths), is this work tree.
        for content in (
            f"{self.root}/to-worktree/.git\n",
            os.path.relpath(f"{self.worktree}/.git", self.git_dir) + "\n",
        ):
            with self.subTest(content=content):
                with open(gitdir, "w", encoding="utf-8") as f:
                    f.write(content)
                self.pinned(status)
        # The file itself being a link, or missing, is refused.
        with open(f"{self.outside}/gitdir", "w", encoding="utf-8") as f:
            f.write(f"{self.worktree}/.git\n")
        os.remove(gitdir)
        self.assert_rejected("bad_git_dir", self.pinned, status)
        os.symlink(f"{self.outside}/gitdir", gitdir)
        self.assert_rejected("bad_git_dir", self.pinned, status)

    def test_git_dir_and_work_tree_come_together_once_and_only_where_allowed(self):
        status = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
        cases = [
            [f"--git-dir={self.git_dir}", *status],
            [f"--work-tree={self.worktree}", *status],
            [
                f"--git-dir={self.git_dir}",
                f"--git-dir={self.git_dir}",
                f"--work-tree={self.worktree}",
                *status,
            ],
            [
                f"--git-dir={self.git_dir}",
                f"--work-tree={self.worktree}",
                "worktree",
                "prune",
            ],
            [
                f"--git-dir={self.git_dir}",
                f"--work-tree={self.worktree}",
                "config",
                "--local",
                "--get",
                "remote.origin.url",
            ],
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assert_rejected("bad_option", self.plan, args, cwd=self.worktree)

    def test_an_unpinned_command_inside_a_worktree_is_refused(self):
        for args in (
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            ["symbolic-ref", "--quiet", "HEAD"],
            ["merge", "--abort"],
            ["worktree", "prune"],
        ):
            with self.subTest(args=args):
                self.assert_rejected(
                    "unpinned_worktree", self.plan, args, cwd=self.worktree
                )

    @requires_git
    def test_repository_discovery_never_leaves_the_root(self):
        # The home above the root is itself a repository (a dotfiles checkout,
        # say) whose configuration the wrapper never checked. A cwd in the root
        # that is not a repository must not reach it, even when the client sends
        # no ceiling ("-") or a ceiling that is the root itself.
        git("init", "--quiet", cwd=self.home)
        plain = f"{self.root}/plain"
        os.makedirs(plain)
        for cwd, ceiling in ((plain, None), (plain, self.root), (self.root, None)):
            with self.subTest(cwd=cwd, ceiling=ceiling):
                invocation = self.plan(
                    ["rev-parse", "--show-toplevel"], cwd=cwd, ceiling=ceiling
                )
                result = run_planned(invocation)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertNotIn(self.home, result.stdout)

    @requires_git
    def test_a_repository_planted_in_a_worktree_runs_no_command(self):
        # An agent writes its worktree: it can commit a gitlink and give the
        # nested repository a configuration of its own (a clean filter is a
        # command). The pinned `status` the backend runs there must not start
        # git in that nested repository, where the filter would run.
        checkout = f"{self.root}/real"
        worktree = f"{self.worktrees}/task/2/real/build"
        git("init", "--quiet", "--initial-branch=main", checkout)
        git("commit", "--quiet", "--allow-empty", "-m", "init", cwd=checkout)
        git("worktree", "add", "--quiet", "-b", BRANCH, worktree, cwd=checkout)
        nested = f"{worktree}/sub"
        git("init", "--quiet", nested)
        fs.write(f"{nested}/f", "hi\n")
        git("add", "f", cwd=nested)
        git("commit", "--quiet", "-m", "sub", cwd=nested)
        git("add", "sub", cwd=worktree)
        git("commit", "--quiet", "-m", "gitlink", cwd=worktree)
        marker = f"{self.outside}/filter-ran"
        git("config", "filter.x.clean", f"touch {marker}; cat", cwd=nested)
        fs.write(f"{nested}/.git/info/attributes", "* filter=x\n")
        os.utime(f"{nested}/f", (2_000_000_000, 2_000_000_000))  # hash it again
        git_dir = git("rev-parse", "--path-format=absolute", "--git-dir", cwd=worktree)
        invocation = self.pinned(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            git_dir=git_dir,
            work_tree=worktree,
            cwd=worktree,
        )
        result = run_planned(invocation)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(marker))

    def test_path_arguments_stay_where_they_belong(self):
        url = "https://github.com/o/r.git"
        os.symlink(self.outside, f"{self.root}/escape")
        os.symlink(self.outside, f"{self.worktrees}/escape")
        cases = [
            ["clone", "--quiet", "--", url, f"{self.outside}/x"],
            ["clone", "--quiet", "--", url, f"{self.root}/escape/x"],
            ["clone", "--quiet", "--", url, f"{self.worktrees}/x"],
            ["clone", "--quiet", "--", url, self.root],
            ["clone", "--quiet", "--", url, "relative"],
            ["init", "--quiet", "--template=", "--initial-branch=main", "--", "/tmp"],
            [
                "init",
                "--quiet",
                "--template=",
                "--initial-branch=main",
                "--",
                f"{self.root}/escape",
            ],
            [
                "worktree",
                "add",
                "--quiet",
                "-b",
                BRANCH,
                "--",
                f"{self.root}/beside",
                COMMIT,
            ],
            [
                "worktree",
                "add",
                "--quiet",
                "-b",
                BRANCH,
                "--",
                f"{self.worktrees}/escape/x",
                COMMIT,
            ],
            ["worktree", "add", "--quiet", "--", self.outside, BRANCH],
            ["worktree", "add", "--quiet", "--", self.worktrees, BRANCH],
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assert_rejected(None, self.plan, args, cwd=self.root)


class RejectedConfigTest(WrapperTestCase):
    """``-c`` only from the fixed list (the human's condition on Decision 0036 §13)."""

    def test_a_configuration_outside_the_list_is_refused(self):
        args = ["rev-parse", "--is-bare-repository"]
        for pair in (
            "core.hooksPath=/tmp/hooks",
            "core.fsmonitor=/tmp/monitor",
            "core.sshCommand=touch /tmp/pwned",
            "protocol.allow=always",
            "protocol.file.allow=always",
            "protocol.ext.allow=always",
            "core.pager=sh -c id",
            "alias.x=!sh",
            "include.path=/tmp/evil",
            "user.name=Mallory",
            "core.HOOKSPATH=/dev/null",  # a different spelling is not in the list
            "core.hooksPath",
            "",
        ):
            with self.subTest(pair=pair):
                self.assert_rejected(
                    "config_not_allowed", self.plan, ["-c", pair, *args]
                )

    def test_the_merge_identity_is_accepted_only_for_merge(self):
        for args in (
            [*MERGE_CONFIG, "rev-parse", "--is-bare-repository"],
            ["-c", "commit.gpgSign=false", "status", "--porcelain=v1", "-z"],
        ):
            with self.subTest(args=args):
                self.assert_rejected(None, self.plan, args, cwd=self.checkout)

    def test_a_merge_identity_with_another_value_is_refused(self):
        for pair in (
            "user.name=Someone Else",
            "user.email=me@example.com",
            "commit.gpgSign=true",
            "merge.verifySignatures=true",
        ):
            with self.subTest(pair=pair):
                self.assert_rejected(
                    "config_not_allowed",
                    self.pinned,
                    [
                        "-c",
                        pair,
                        "merge",
                        "--no-ff",
                        "--no-edit",
                        "--quiet",
                        "-m",
                        f"Integrate {BRANCH}",
                        f"refs/heads/{BRANCH}",
                    ],
                )

    def test_other_global_options_are_refused(self):
        for option in (
            ["-C", "/tmp"],
            ["-ccore.hooksPath=/tmp"],
            ["--config-env=core.hooksPath=X"],
            ["--exec-path=/tmp"],
            ["--namespace=x"],
            ["--bare"],
            ["-p"],
            ["--paginate"],
            ["--git-dir", self.git_dir],
            ["-c"],
        ):
            with self.subTest(option=option):
                self.assert_rejected(
                    None, self.plan, [*option, "rev-parse", "--is-bare-repository"]
                )


class RejectedCommandTest(WrapperTestCase):
    """The sub-command allowlist and each sub-command's exact shapes."""

    def test_sub_commands_outside_the_allowlist_are_refused(self):
        for args in (
            ["push", "origin", "HEAD"],
            ["push", "--force", "origin", "main"],
            ["fetch", "origin"],
            ["pull"],
            ["checkout", "main"],
            ["switch", "main"],
            ["reset", "--hard", "HEAD"],
            ["rebase", "main"],
            ["commit", "-m", "x"],
            ["branch", "-D", "main"],
            ["gc"],
            ["submodule", "update"],
            ["archive", "HEAD"],
            ["upload-pack", "."],
            ["receive-pack", "."],
            ["daemon"],
            ["filter-branch"],
            ["update-ref", "refs/heads/main", COMMIT],
            ["!sh"],
            ["help"],
            ["--version"],
        ):
            with self.subTest(args=args):
                self.assert_rejected(None, self.plan, args)

    def test_other_shapes_of_allowed_sub_commands_are_refused(self):
        url = "https://github.com/o/r.git"
        dest = f"{self.root}/new"
        for args in (
            ["config", "--global", "user.name", "x"],
            ["config", "--local", "core.hooksPath", "/tmp"],
            ["config", "--local", "--get", "core.sshCommand"],
            ["config", "--add", "remote.origin.url", url],
            ["remote", "set-url", "origin", url],
            ["remote", "remove", "origin"],
            ["remote", "add", "origin", url],
            ["remote", "add", "--", "origin", "file:///etc"],
            ["remote", "add", "--", "origin", "ext::sh -c id"],
            ["clone", "--quiet", "--upload-pack=touch /tmp/x", "--", url, dest],
            ["clone", "--quiet", "--", "ext::sh -c id", dest],
            ["clone", "--quiet", "--", "file:///etc", dest],
            ["clone", "--quiet", "--", "http://github.com/o/r", dest],
            ["clone", "--quiet", "--", "https://github.com/o r", dest],
            ["clone", "--quiet", "--branch", "--upload-pack=x", "--", url, dest],
            ["clone", "--quiet", "--template=/tmp/t", "--", url, dest],
            ["clone", "--", url, dest],
            [
                "init",
                "--quiet",
                "--template=/tmp/t",
                "--initial-branch=main",
                "--",
                dest,
            ],
            ["init", "--quiet", "--template=", "--initial-branch=-x", "--", dest],
            ["init", "--bare", "--", dest],
            ["rev-parse", "--verify", "--quiet", "@{upstream}"],
            ["rev-parse", "--verify", "--quiet", "HEAD"],
            ["rev-parse", "--verify", "--quiet", "refs/heads/../x^{commit}"],
            ["rev-parse", "--git-path", "hooks"],
            ["rev-parse", "--local-env-vars"],
            ["symbolic-ref", "HEAD", "refs/heads/evil"],
            ["symbolic-ref", "--delete", "HEAD"],
            ["worktree", "remove", self.worktree],
            ["worktree", "move", self.worktree, f"{self.worktrees}/x"],
            [
                "worktree",
                "add",
                "--quiet",
                "-b",
                "main",
                "--",
                self.worktree + "3",
                COMMIT,
            ],
            [
                "worktree",
                "add",
                "--quiet",
                "-b",
                BRANCH,
                "--",
                self.worktree + "3",
                "HEAD",
            ],
            ["worktree", "add", "--quiet", "--", self.worktree + "3", "main"],
            ["worktree", "add", "--force", "--", self.worktree + "3", BRANCH],
            ["worktree", "list"],
            ["worktree", "prune", "--expire=now"],
            ["merge-base", "--is-ancestor", "refs/heads/main", f"refs/heads/{BRANCH}"],
            ["merge-base", "--is-ancestor", "HEAD", f"refs/heads/{BRANCH}"],
            ["merge-base", f"refs/heads/{BRANCH}", f"refs/heads/{OTHER}"],
            [
                "merge-tree",
                "--write-tree",
                "--name-only",
                "-z",
                "--no-messages",
                "refs/heads/main",
                f"refs/heads/{BRANCH}",
            ],
            [
                "merge-tree",
                "--write-tree",
                f"refs/heads/{OTHER}",
                f"refs/heads/{BRANCH}",
            ],
            ["status"],
            ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "x"],
        ):
            with self.subTest(args=args):
                self.assert_rejected(None, self.plan, args, cwd=self.root)

    def test_other_merge_shapes_are_refused(self):
        for args in (
            ["merge", "refs/heads/main"],
            [
                "merge",
                "--no-ff",
                "--no-edit",
                "--quiet",
                "-m",
                "Integrate main",
                "refs/heads/main",
            ],
            [
                "merge",
                "--no-ff",
                "--no-edit",
                "--quiet",
                "-m",
                f"Integrate {OTHER}",
                f"refs/heads/{BRANCH}",
            ],
            [
                "merge",
                "--no-ff",
                "--no-edit",
                "--quiet",
                "-s",
                "ours",
                f"refs/heads/{BRANCH}",
            ],
            ["merge", "--continue"],
        ):
            with self.subTest(args=args):
                self.assert_rejected("bad_arguments", self.pinned, args)


class RepositoryConfigurationTest(WrapperTestCase):
    """A call that reads file content first lists the configuration it would
    read, and is refused when that names a command."""

    STATUS = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]

    def test_every_call_but_clone_and_init_is_probed(self):
        # Codex review of #150 (P2): not only the calls that read file content.
        # ``rev-parse --verify`` (or ``merge-base``) resolves an object, which a
        # git that predates ``GIT_NO_LAZY_FETCH`` fetches from a partial
        # clone's promisor remote when it is missing; the configuration that
        # sets that up is refused only by the probe.
        probed = {
            "status": self.pinned(self.STATUS),
            "merge --abort": self.pinned(["merge", "--abort"]),
            "merge-tree": self.plan(
                [
                    "merge-tree",
                    "--write-tree",
                    "--name-only",
                    "-z",
                    "--no-messages",
                    f"refs/heads/{OTHER}",
                    f"refs/heads/{BRANCH}",
                ]
            ),
            "worktree add": self.plan(
                [
                    "worktree",
                    "add",
                    "--quiet",
                    "-b",
                    BRANCH,
                    "--",
                    self.worktree,
                    COMMIT,
                ]
            ),
            "rev-parse --verify": self.plan(
                ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"]
            ),
            "pinned rev-parse --verify": self.pinned(
                ["rev-parse", "--verify", "--quiet", "MERGE_HEAD^{commit}"]
            ),
            "rev-parse": self.plan(["rev-parse", "--is-bare-repository"]),
            "worktree list": self.plan(["worktree", "list", "--porcelain", "-z"]),
            "worktree prune": self.plan(["worktree", "prune"]),
            "merge-base": self.plan(
                [
                    "merge-base",
                    "--is-ancestor",
                    f"refs/heads/{OTHER}",
                    f"refs/heads/{BRANCH}",
                ]
            ),
            "symbolic-ref": self.pinned(["symbolic-ref", "--quiet", "HEAD"]),
            "config": self.plan(["config", "--local", "--get", "remote.origin.url"]),
        }
        for name, invocation in probed.items():
            with self.subTest(name=name):
                self.assertEqual(
                    invocation.probe[-5:],
                    ["config", "--no-includes", "--show-scope", "--list", "-z"],
                )
                prefix = hardened(invocation)
                self.assertEqual(invocation.probe[: len(prefix)], prefix)
        pinned = probed["status"].probe
        self.assertIn(f"--git-dir={self.git_dir}", pinned)
        self.assertIn(f"--work-tree={self.worktree}", pinned)
        url = "https://github.com/owner/repo.git"
        for invocation in (
            self.plan(
                ["clone", "--quiet", "--", url, f"{self.root}/new"], cwd=self.root
            ),
            self.plan(
                [
                    "init",
                    "--quiet",
                    "--template=",
                    "--initial-branch=main",
                    "--",
                    self.fresh,
                ],
                cwd=self.fresh,
            ),
        ):
            with self.subTest(argv=invocation.argv):
                self.assertIsNone(invocation.probe)

    def test_a_partial_clone_refuses_a_rev_parse_too(self):
        invocation = self.plan(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
        for key in ("extensions.partialclone", "remote.origin.promisor"):
            with self.subTest(key=key):
                listed = f"local\0{key}\ntrue\0".encode()

                def run(argv, *, listed=listed, **kwargs):
                    return subprocess.CompletedProcess(argv, 0, listed, b"")

                self.assert_rejected(
                    "config_unsafe", wrapper.check_configuration, invocation, run
                )

    def check(self, stdout=b"", returncode=0, error=None):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if error is not None:
                raise error
            return subprocess.CompletedProcess(argv, returncode, stdout, b"")

        invocation = self.pinned(self.STATUS)
        wrapper.check_configuration(invocation, run)
        [(argv, kwargs)] = calls
        self.assertEqual(argv, invocation.probe)
        self.assertEqual(kwargs["env"], invocation.env)
        self.assertEqual(kwargs["cwd"], invocation.cwd)

    def test_a_command_in_the_configuration_refuses_the_call(self):
        for key in (
            "filter.lfs.clean",
            "filter.x.smudge",
            "filter.x.process",
            "filter.x.required",
            "diff.x.textconv",
            "diff.x.command",
            "diff.external",
            "merge.x.driver",
            "hook.pre-merge.command",
            "include.path",
            "includeif.gitdir:/x/.path",
            "core.pager",
            "pager.status",
            "core.editor",
            "sequence.editor",
            "gpg.program",
            "gpg.ssh.program",
            "remote.origin.uploadpack",
            "core.sshcommand",
            "core.worktree",
            "Core.WorkTree",
            "gpg.format",
            "protocol.ext.allow",
            "protocol.allow",
            "extensions.partialClone",
            "remote.origin.promisor",
            "gpg.ssh.defaultkeycommand",
            "branch.paw/t/1/_integration.mergeoptions",
            "Filter.X.Clean",
        ):
            with self.subTest(key=key):
                self.assertTrue(wrapper.unsafe_setting(key))
                listed = f"local\0core.bare\nfalse\0local\0{key}\ntouch /tmp/x\0"
                listed = listed.encode()
                self.assert_rejected("config_unsafe", self.check, listed)

    def test_ordinary_configuration_is_accepted(self):
        entries = (
            b"core.repositoryformatversion\n0",
            b"core.bare\nfalse",
            b"core.hookspath\n.husky",
            b"core.fsmonitor\ntrue",
            b"remote.origin.url\nhttps://github.com/o/r.git",
            b"credential.helper\n!/usr/bin/gh auth git-credential",
            b"branch.main.merge\nrefs/heads/main",
            b"submodule.lib.path\nlib",
            b"merge.conflictstyle\nzdiff3",
            b"diff.algorithm\nhistogram",
            b"extensions.worktreeconfig",
        )
        self.check(b"".join(b"local\0" + entry + b"\0" for entry in entries))
        # The wrapper's own -c (a deployment's --config= too) is not the
        # repository's: its scope is "command".
        self.check(b"command\0core.sshcommand\nssh -F /etc/paw/ssh\0")
        self.assert_rejected(
            "config_unsafe", self.check, b"worktree\0core.sshcommand\nx\0"
        )
        self.assert_rejected("config_unreadable", self.check, b"local\0")

    def test_a_check_reads_a_bounded_output_in_a_bounded_time(self):
        def python(code, **options):
            return wrapper.bounded_run(
                [sys.executable, "-c", code],
                cwd=self.root,
                env={"PATH": os.environ.get("PATH", "")},
                **{"timeout": 30, "limit": 1024, **options},
            )

        done = python("import sys; sys.stdout.write('x' * 1024); sys.exit(3)")
        self.assertEqual((done.returncode, done.stdout), (3, b"x" * 1024))
        with self.assertRaises(wrapper.ProbeOutputTooLarge):
            python("import sys\nwhile True: sys.stdout.write('x' * 4096)")
        with self.assertRaises(subprocess.TimeoutExpired):
            python("import time; time.sleep(30)", timeout=0.5)

    def test_a_configuration_that_cannot_be_listed_refuses_the_call(self):
        self.assert_rejected("config_unreadable", self.check, returncode=128)
        self.assert_rejected(
            "config_unreadable", self.check, error=subprocess.TimeoutExpired("git", 30)
        )
        self.assert_rejected("config_unreadable", self.check, error=OSError())


class RepositoryLocationTest(WrapperTestCase):
    """A call not pinned to a git directory first asks git where the git
    directory it finds is, and is refused unless it is inside the root."""

    def locate(self, stdout=b"", returncode=0, error=None, invocation=None):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if error is not None:
                raise error
            return subprocess.CompletedProcess(argv, returncode, stdout, b"")

        invocation = invocation or self.plan(["rev-parse", "--show-toplevel"])
        wrapper.check_location(invocation, run)
        [(argv, kwargs)] = calls
        self.assertEqual(argv, invocation.locate)
        self.assertEqual(kwargs["cwd"], invocation.cwd)
        self.assertEqual(kwargs["env"], invocation.env)

    def test_which_calls_are_located(self):
        located = self.plan(["rev-parse", "--show-toplevel"])
        self.assertEqual(
            located.locate,
            [
                *hardened(located),
                "rev-parse",
                "--path-format=absolute",
                "--git-dir",
                "--git-common-dir",
            ],
        )
        self.assertIsNotNone(
            self.plan(
                [
                    "init",
                    "--quiet",
                    "--template=",
                    "--initial-branch=main",
                    "--",
                    self.fresh,
                ],
                cwd=self.fresh,
            ).locate
        )
        self.assertIsNone(self.pinned(["symbolic-ref", "--quiet", "HEAD"]).locate)
        clone = self.plan(
            [
                "clone",
                "--quiet",
                "--",
                "https://github.com/o/r.git",
                f"{self.root}/new",
            ],
            cwd=self.root,
        )
        self.assertIsNone(clone.locate)

    def test_a_git_directory_inside_the_root_is_accepted(self):
        self.locate(f"{self.checkout}/.git\n{self.checkout}/.git\n".encode())
        self.locate(f"{self.git_dir}\n{self.checkout}/.git\n".encode())
        self.locate(b"", returncode=128)  # no repository: git says so itself

    def test_a_git_directory_elsewhere_is_refused(self):
        os.symlink(self.outside, f"{self.root}/escape")
        inside = f"{self.checkout}/.git"
        for git_dir, common in (
            (f"{self.outside}/.git", f"{self.outside}/.git"),
            (inside, f"{self.outside}/.git"),
            (f"{self.root}/escape/.git", inside),
            (f"{self.worktree}/.git", inside),  # an agent's
            (inside, self.worktrees),
            (self.root, inside),
            (f"{self.root}/missing", inside),
        ):
            with self.subTest(git_dir=git_dir, common=common):
                os.makedirs(f"{self.worktree}/.git", exist_ok=True)
                os.makedirs(f"{self.outside}/.git", exist_ok=True)
                os.makedirs(inside, exist_ok=True)
                self.assert_rejected(
                    "git_dir_outside_root",
                    self.locate,
                    f"{git_dir}\n{common}\n".encode(),
                )
        for stdout in (b"", b"\n\n", f"{inside}\n".encode(), b"\xff\n\xff\n"):
            with self.subTest(stdout=stdout):
                self.assert_rejected("git_dir_outside_root", self.locate, stdout)
        self.assert_rejected("probe_failed", self.locate, error=OSError())

    def test_a_link_inside_the_git_directory_is_refused(self):
        common = f"{self.checkout}/.git"
        os.makedirs(f"{common}/refs/heads")
        os.makedirs(f"{common}/hooks")
        wrapper.check_links([common, self.git_dir])
        os.symlink(self.outside, f"{common}/hooks/linked")  # never written
        wrapper.check_links([common, self.git_dir])
        for place in (
            f"{common}/refs/heads/paw",
            f"{self.git_dir}/logs",
            f"{common}/packed-refs",
        ):
            with self.subTest(place=place):
                os.symlink(self.outside, place)
                self.assert_rejected("git_dir_link", wrapper.check_links, [common])
                self.assert_rejected(
                    "git_dir_link", wrapper.check_links, [self.git_dir, common]
                )
                os.remove(place)
        # A second hard link of a file git rewrites (outside the root, say).
        with open(f"{self.outside}/victim", "w", encoding="utf-8") as f:
            f.write("x\n")
        os.link(f"{self.outside}/victim", f"{self.git_dir}/MERGE_MSG")
        self.assert_rejected("git_dir_link", wrapper.check_links, [common])
        os.remove(f"{self.git_dir}/MERGE_MSG")
        # Object files are never rewritten: their links are left alone.
        os.makedirs(f"{common}/objects/pack")
        os.link(f"{self.outside}/victim", f"{common}/objects/pack/p.pack")
        for sub in (f"{common}/modules/lib", f"{self.git_dir}/modules/lib"):
            os.makedirs(f"{sub}/objects/ab")
            os.link(f"{self.outside}/victim", f"{sub}/objects/ab/cd")
        wrapper.check_links([common])
        for name in ("alternates", "http-alternates"):
            for info in (
                f"{common}/objects/info",
                f"{common}/modules/lib/objects/info",
            ):
                with self.subTest(name=name, info=info):
                    os.makedirs(info, exist_ok=True)
                    with open(f"{info}/{name}", "w", encoding="utf-8") as f:
                        f.write(f"{self.outside}/.git/objects\n")
                    self.assert_rejected(
                        "git_dir_alternates", wrapper.check_links, [common]
                    )
                    os.remove(f"{info}/{name}")
        # A ref named like that is no object.
        os.makedirs(f"{common}/refs/heads/modules/x/objects")
        os.link(f"{self.outside}/victim", f"{common}/refs/heads/modules/x/objects/y")
        self.assert_rejected("git_dir_link", wrapper.check_links, [common])
        os.remove(f"{common}/refs/heads/modules/x/objects/y")
        os.symlink(common, f"{self.root}/linked-git")
        self.assert_rejected(
            "git_dir_link", wrapper.check_links, [f"{self.root}/linked-git"]
        )
        os.chmod(f"{common}/refs", 0)
        self.addCleanup(os.chmod, f"{common}/refs", 0o755)
        if not os.access(f"{common}/refs", os.R_OK):  # not when run as root
            self.assert_rejected("git_dir_link", wrapper.check_links, [common])

    def test_init_never_reuses_an_existing_git(self):
        init = ["init", "--quiet", "--template=", "--initial-branch=main", "--"]
        self.plan([*init, self.fresh], cwd=self.fresh)
        cases = {
            # a directory git may not even read as a repository, whose files
            # are links out of the root
            "directory": lambda path: os.makedirs(f"{path}/refs"),
            "gitdir file": lambda path: fs.write(path, "gitdir: /elsewhere\n"),
            "link": lambda path: os.symlink(f"{self.outside}/.git", path),
            "dangling link": lambda path: os.symlink(f"{self.outside}/x", path),
        }
        for name, make in cases.items():
            with self.subTest(case=name):
                target = f"{self.root}/fresh-{name.replace(' ', '-')}"
                os.makedirs(target)
                make(f"{target}/.git")
                self.assert_rejected(
                    "init_existing", self.plan, [*init, target], cwd=target
                )
        self.assert_rejected(
            "init_existing", self.plan, [*init, self.checkout], cwd=self.checkout
        )

    def test_init_runs_only_where_it_creates_the_repository(self):
        other = f"{self.root}/other"
        os.makedirs(other)
        self.assert_rejected(
            "bad_arguments",
            self.plan,
            ["init", "--quiet", "--template=", "--initial-branch=main", "--", other],
        )


class StatusWithSubmodulesTest(WrapperTestCase):
    """The ``status`` of Decision 0051 (PR #130): pinned only, exactly this
    form, and never with a populated submodule."""

    FORM = [
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=normal",
        "--ignored=traditional",
        "--ignore-submodules=none",
    ]

    def test_the_form_is_accepted_pinned(self):
        invocation = self.pinned(self.FORM)
        self.assertEqual(invocation.argv[-len(self.FORM) :], self.FORM)
        self.assertEqual(
            invocation.gitlinks,
            [
                *hardened(invocation),
                f"--git-dir={self.git_dir}",
                f"--work-tree={self.worktree}",
                "ls-files",
                "--stage",
                "-z",
            ],
        )
        self.assertIsNotNone(invocation.probe)
        self.assertIsNone(self.pinned(RepositoryConfigurationTest.STATUS).gitlinks)

    def test_other_forms_are_refused(self):
        form = self.FORM[1:]
        variants = [
            form[:-1],
            form[1:],
            [form[0], form[1], form[2], form[4], form[3]],
            [*form[:3], "--ignored", form[4]],
            [*form[:3], "--ignored=matching", form[4]],
            [*form[:3], "--ignored=no", form[4]],
            [form[0], form[1], "--untracked-files=all", *form[3:]],
            [*form[:4], "--ignore-submodules=dirty"],
            [*form[:4], "--ignore-submodules"],
            [*form, "--"],
            [*form, "."],
            [*form, "--ignore-submodules=none"],
        ]
        for args in variants:
            with self.subTest(args=args):
                self.assert_rejected("bad_arguments", self.pinned, ["status", *args])
        # Not pinned (the checkout, whose submodules git would enter).
        self.assert_rejected("bad_arguments", self.plan, self.FORM)

    def check(self, stdout, returncode=0):
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, returncode, stdout, b"")

        wrapper.check_submodules(self.pinned(self.FORM), run)

    def test_a_populated_submodule_refuses_the_call(self):
        oid = "0" * 40
        listed = f"100644 {oid} 0\tcode.txt\x00160000 {oid} 0\tlib/sub\x00".encode()
        self.check(listed)  # not populated: no `.git` there
        os.makedirs(f"{self.worktree}/lib/sub/.git")
        self.assert_rejected("populated_submodule", self.check, listed)
        os.rmdir(f"{self.worktree}/lib/sub/.git")
        with open(f"{self.worktree}/lib/sub/.git", "w", encoding="utf-8") as f:
            f.write("gitdir: /elsewhere\n")
        self.assert_rejected("populated_submodule", self.check, listed)
        self.assert_rejected("probe_failed", self.check, b"", returncode=128)


class SubmoduleStatusTest(WrapperTestCase):
    """``submodule status --cached`` of Decision 0051 (PR #130): pinned only,
    exactly this form, never another ``submodule`` sub-command or option."""

    FORM = ["submodule", "status", "--cached"]

    def test_the_form_is_accepted_pinned(self):
        invocation = self.pinned(self.FORM)
        self.assertEqual(invocation.argv[-3:], self.FORM)
        self.assertEqual(invocation.gitlinks[-3:], ["ls-files", "--stage", "-z"])
        self.assertIsNotNone(invocation.probe)
        self.assertIn(f"--git-dir={self.git_dir}", invocation.argv)

    def test_other_submodule_calls_are_refused(self):
        for args in (
            ["status"],
            ["status", "--recursive"],
            ["status", "--cached", "--recursive"],
            ["status", "--cached", "--"],
            ["status", "--cached", "sub"],
            ["--cached", "status"],
            ["--quiet", "status", "--cached"],
            ["status", "--quiet", "--cached"],
            ["foreach", "touch x"],
            ["foreach", "--recursive", "touch x"],
            ["update", "--init"],
            ["update"],
            ["init"],
            ["sync"],
            ["add", "https://github.com/o/r.git", "sub"],
            ["deinit", "--all"],
            ["absorbgitdirs"],
            ["set-url", "sub", "https://github.com/o/r.git"],
            ["summary"],
            [],
        ):
            with self.subTest(args=args):
                self.assert_rejected("bad_arguments", self.pinned, ["submodule", *args])
        self.assert_rejected(None, self.pinned, ["submodule--helper", "status"])
        # Not pinned (the checkout, whose submodules git would enter).
        self.assert_rejected("bad_arguments", self.plan, self.FORM)


class RejectedWireTest(WrapperTestCase):
    """The encoding of ``$SSH_ORIGINAL_COMMAND`` itself."""

    def test_malformed_commands_are_refused(self):
        good = self.command(["rev-parse", "--is-bare-repository"])
        for original, reason in (
            (None, "no_command"),
            ("", "no_command"),
            ("git-upload-pack 'repo.git'", "bad_protocol"),
            (good.replace("paw-git-run/v1", "paw-git-run/v2", 1), "bad_protocol"),
            (good.replace(" -- ", " ++ ", 1), "bad_protocol"),
            (good + " 'unbalanced", "bad_encoding"),
            ("paw-git-run/v1 " + "x" * wrapper.MAX_COMMAND_BYTES, "too_long"),
            (
                f"paw-git-run/v1 {self.checkout} - -- -c core.fsmonitor=false",
                "no_subcommand",
            ),
            (
                self.command(["rev-parse", "--is-bare-repository"], ceiling="/"),
                "bad_ceiling",
            ),
        ):
            with self.subTest(original=(original or "")[:60]):
                self.assert_rejected(reason, wrapper.plan, original, self.config)

    def test_a_shell_metacharacter_is_just_a_character(self):
        # Never a shell: `;` and `$(...)` are parts of one word, which then fails
        # the shape checks like any other wrong word.
        for word in ("HEAD; touch /tmp/x", "$(touch /tmp/x)", "`id`", "a\nb"):
            with self.subTest(word=word):
                self.assert_rejected(
                    "bad_arguments",
                    self.plan,
                    ["rev-parse", "--verify", "--quiet", word],
                )


class ConfigTest(unittest.TestCase):
    def test_defaults_come_from_this_linux_user(self):
        config = wrapper.parse_config([])
        self.assertEqual(config.root, f"{config.home.rstrip('/')}/workspaces")
        self.assertEqual(config.protocols, ("https",))
        self.assertIsNone(config.gh)

    def test_options(self):
        config = wrapper.parse_config(
            [
                "--root=/srv/ws",
                "--home=/srv",
                "--git=/usr/local/bin/git",
                "--allow-protocol=https",
                "--allow-protocol=file",
                "--config=url.file:///srv/m/.insteadOf=https://github.com/",
                "--gh=/usr/bin/gh",
                "--path=/usr/bin:/bin",
            ]
        )
        self.assertEqual(config.root, "/srv/ws")
        self.assertEqual(config.protocols, ("https", "file"))
        self.assertEqual(
            config.extra, (("url.file:///srv/m/.insteadOf", "https://github.com/"),)
        )

    def test_a_bad_option_refuses_everything(self):
        for argv in (
            ["--root"],
            ["--root="],
            ["--root=relative"],
            ["--root=/"],
            ["--root=/a/../b"],
            ["--root=/a", "--root=/b"],
            ["--git=git"],
            ["--allow-protocol=HTTPS"],
            ["--config=novalue"],
            ["--config==x"],
            ["--path=/usr/bin:bin"],
            ["--unknown=1"],
            ["positional"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(wrapper.Rejected) as caught:
                    wrapper.parse_config(argv)
                self.assertEqual(caught.exception.reason, "misconfigured")


class MainTest(WrapperTestCase):
    """What ``main`` ``exec``-s, returns, logs and prints."""

    SECRET = "ghp_SECRETTOKEN0123456789"

    def run_main(self, original, *, argv=None, environ=None):
        executed = []
        logged = []
        moved = []
        environment = dict(environ or {})
        if original is not None:
            environment["SSH_ORIGINAL_COMMAND"] = original
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = wrapper.main(
                argv
                if argv is not None
                else [f"--root={self.root}", f"--home={self.home}"],
                environment,
                execve=lambda path, args, env: executed.append((path, args, env)),
                chdir=moved.append,
                log=logged.append,
            )
        return code, executed, logged, moved, stderr.getvalue()

    def test_an_accepted_call_execs_git_with_a_fixed_environment(self):
        hostile = {
            "LD_PRELOAD": "/tmp/evil.so",
            "GIT_DIR": self.outside,
            "GIT_CONFIG_PARAMETERS": "'core.hooksPath'='/tmp'",
            "PAW_DATABASE_URL": f"postgresql://u:{self.SECRET}@db/paw",
            "LC_ALL": "en_US.UTF-8",
            "PATH": "/tmp/evil",
        }
        code, executed, logged, moved, _ = self.run_main(
            self.command(["rev-parse", "--is-bare-repository"]), environ=hostile
        )
        self.assertEqual(code, 0)
        [(path, args, env)] = executed
        self.assertEqual(path, "/usr/bin/git")
        self.assertEqual(args[0], "/usr/bin/git")
        self.assertEqual(moved, [self.checkout])
        for name in hostile:
            if name in ("LC_ALL", "PATH"):
                continue
            self.assertNotIn(name, env)
        self.assertNotIn("SSH_ORIGINAL_COMMAND", env)
        self.assertEqual(env["PATH"], wrapper.SAFE_PATH)
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(
            logged, [f"accepted user={self.config_user()} subcommand=rev-parse"]
        )

    def config_user(self):
        return wrapper.parse_config([]).user

    def test_a_refused_call_returns_126_and_execs_nothing(self):
        code, executed, logged, moved, stderr = self.run_main(
            self.command(["push", "origin", "HEAD"])
        )
        self.assertEqual(code, wrapper.REJECTED)
        self.assertNotEqual(code, 255)
        # The backend tells a refusal from git's own failures by this code.
        self.assertEqual(code, WRAPPER_REJECTED_CODE)
        self.assertEqual(executed, [])
        self.assertEqual(moved, [])
        self.assertEqual(
            logged,
            [f"rejected user={self.config_user()} reason=subcommand_not_allowed"],
        )
        self.assertEqual(stderr, "paw-git-wrapper: rejected (subcommand_not_allowed)\n")

    def test_a_refused_configuration_execs_nothing(self):
        def refuse(invocation):
            raise wrapper.Rejected("config_unsafe")

        executed = []
        with contextlib.redirect_stderr(io.StringIO()):
            code = wrapper.main(
                [f"--root={self.root}", f"--home={self.home}"],
                {
                    "SSH_ORIGINAL_COMMAND": self.command(
                        ["rev-parse", "--show-toplevel"]
                    )
                },
                execve=lambda path, args, env: executed.append(args),
                chdir=lambda path: None,
                log=lambda message: None,
                check=refuse,
            )
        self.assertEqual(code, wrapper.REJECTED)
        self.assertEqual(executed, [])

    def test_an_interactive_login_is_refused(self):
        code, executed, logged, _, _ = self.run_main(None)
        self.assertEqual(code, wrapper.REJECTED)
        self.assertEqual(executed, [])
        self.assertIn("reason=no_command", logged[0])

    def test_a_misconfigured_wrapper_refuses(self):
        code, executed, logged, _, _ = self.run_main(
            self.command(["rev-parse", "--is-bare-repository"]), argv=["--root=x"]
        )
        self.assertEqual(code, wrapper.REJECTED)
        self.assertEqual(executed, [])
        self.assertEqual(logged, ["rejected user=- reason=misconfigured"])

    def test_nothing_secret_or_client_supplied_is_logged_or_printed(self):
        url = f"https://x-access-token:{self.SECRET}@github.com/o/r.git"
        cases = [
            # accepted: the URL is part of the call
            self.command(["remote", "add", "--", "origin", url], ceiling=self.root),
            # refused at the sub-command, at an argument, at a -c value
            self.command(["push", url, "HEAD"]),
            self.command(["remote", "add", "--", "origin", f"file://{self.SECRET}"]),
            self.command(
                ["-c", f"http.extraHeader=Authorization: {self.SECRET}", "status"]
            ),
            self.command([f"{self.SECRET}", "x"]),
            f"paw-git-run/v1 '{self.SECRET}",
        ]
        for original in cases:
            with self.subTest(original=original[:40]):
                _, _, logged, _, stderr = self.run_main(original)
                text = "\n".join(logged) + stderr
                self.assertNotIn(self.SECRET, text)
                self.assertNotIn("x-access-token", text)
                self.assertNotIn(self.root, text)
                self.assertNotIn(self.home, text)

    def test_an_exec_failure_is_refused_not_255(self):
        def failing(path, args, env):
            raise FileNotFoundError(path)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = wrapper.main(
                [
                    f"--root={self.root}",
                    f"--home={self.home}",
                    "--git=/nonexistent/git",
                ],
                {
                    "SSH_ORIGINAL_COMMAND": self.command(
                        ["rev-parse", "--is-bare-repository"]
                    )
                },
                execve=failing,
                chdir=lambda path: None,
                log=lambda message: None,
            )
        self.assertEqual(code, wrapper.REJECTED)


#: A fake ``ssh``: what ``sshd`` does with a forced command, minus the network.
#: It puts the remote command (the last argument) in ``$SSH_ORIGINAL_COMMAND``
#: next to a hostile environment, and runs the real wrapper with the options of
#: the ``command=`` line. Values are baked in with ``repr`` (``SshGitRunner``
#: passes its child only ``PATH``).
_FAKE_SSHD = """#!/usr/bin/env python3
import os
import sys

environment = {
    "SSH_ORIGINAL_COMMAND": sys.argv[-1],
    "PATH": "/tmp/evil-bin",
    "GIT_DIR": "/tmp/evil-git-dir",
    "LD_PRELOAD": "/tmp/evil.so",
}
os.execve(
    __PYTHON__,
    [__PYTHON__, "-I", __WRAPPER__, *__OPTIONS__],
    environment,
)
"""


def _consume(marker: str) -> bool:
    """Whether a command left ``marker`` (removed, for the next case)."""
    try:
        os.remove(marker)
    except FileNotFoundError:
        return False
    return True


class _FixedKey:
    def __init__(self, path: str) -> None:
        self._path = path

    async def key_path_of(self, account: LinuxAccount) -> str:
        return self._path


@requires_git
class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    """The real wrapper behind ``SshGitRunner``, real git, the current user only."""

    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.home = self.world.make_home("alice")
        self.root = f"{self.home}/workspaces"
        os.makedirs(self.root)
        self.account = LinuxAccount(uuid.uuid4(), "alice", os.geteuid(), self.home)
        key = f"{self.world.root}/alice.key"
        fs.write(key, "not a real key\n")
        os.chmod(key, 0o600)
        options = self.world.runner_options()
        self.runner = SshGitRunner(
            _FixedKey(key),
            ssh_executable=self.fake_sshd(options),
            **options,
        )
        self.client = GitClient(self.runner, RepositoryPolicy())

    def fake_sshd(self, runner_options) -> str:
        wrapper_options = [
            f"--root={self.root}",
            f"--home={self.home}",
            f"--git={shutil.which('git')}",
            *(f"--allow-protocol={p}" for p in runner_options["allowed_protocols"]),
            *(f"--config={k}={v}" for k, v in runner_options["extra_config"]),
        ]
        path = f"{self.world.root}/fake-sshd.py"
        fs.write(
            path,
            _FAKE_SSHD.replace("__PYTHON__", repr(sys.executable))
            .replace("__WRAPPER__", repr(str(WRAPPER_PATH)))
            .replace("__OPTIONS__", repr(wrapper_options)),
        )
        os.chmod(path, 0o755)
        return path

    async def run_git(self, args, *, cwd, ceiling=None):
        return await self.runner.run(
            args, account=self.account, cwd=cwd, timeout_s=60, ceiling=ceiling
        )

    async def test_git_client_operations_run_through_the_wrapper(self):
        self.world.make_bare("owner", "repo", {"README.md": "hi\n"})
        destination = f"{self.root}/repo"
        os.makedirs(destination)
        await self.client.clone(
            "https://github.com/owner/repo.git", destination, self.account
        )
        self.assertEqual(fs.read(destination, "README.md"), "hi\n")
        facts = await self.client.inspect(destination, self.account)
        self.assertEqual(facts.default_branch, "main")
        self.assertIsNotNone(facts.head)

        fresh = f"{self.root}/fresh"
        os.makedirs(fresh)
        await self.client.init(fresh, self.account, initial_branch="trunk")
        await self.client.add_origin(
            fresh, "https://github.com/owner/other.git", self.account
        )
        self.assertEqual(
            git("config", "--local", "--get", "remote.origin.url", cwd=fresh),
            "https://github.com/owner/other.git",
        )

    async def test_refused_calls_never_reach_git(self):
        repo = f"{self.root}/repo"
        head = self.world.make_repository(repo)
        outside = f"{self.world.root}/outside"
        self.world.make_repository(outside)
        for args, cwd in (
            (["checkout", "-b", "evil"], repo),
            (["push", "origin", "HEAD"], repo),
            (["rev-parse", "--is-bare-repository"], outside),
            (["-c", "core.hooksPath=/tmp", "rev-parse", "--is-bare-repository"], repo),
        ):
            with self.subTest(args=args):
                result = await self.run_git(args, cwd=cwd)
                self.assertEqual(result.returncode, wrapper.REJECTED)
                self.assertEqual(result.stdout, "")
        self.assertEqual(git("rev-parse", "HEAD", cwd=repo), head)
        self.assertEqual(git("branch", "--list", "evil", cwd=repo), "")

    async def test_the_worktree_commands_of_decision_0036_run_pinned(self):
        checkout = f"{self.root}/project"
        base = self.world.make_repository(checkout)
        worktree = f"{self.root}/.paw-worktrees/t/1/r/build"
        integration = f"{self.root}/.paw-worktrees/t/1/r/_integration"
        branch, target = "paw/t/1/build", "paw/t/1/_integration"
        for path, name in ((worktree, branch), (integration, target)):
            result = await self.run_git(
                ["worktree", "add", "--quiet", "-b", name, "--", path, base],
                cwd=checkout,
            )
            self.assertEqual(result.returncode, 0)
        fs.write(worktree, "work.txt", "work\n")
        git("add", "-A", cwd=worktree)
        git("commit", "--quiet", "-m", "work", cwd=worktree)

        git_dir = (
            await self.run_git(
                ["rev-parse", "--path-format=absolute", "--git-dir"], cwd=integration
            )
        ).stdout.strip()
        pin = [f"--git-dir={git_dir}", f"--work-tree={integration}"]
        status = await self.run_git(
            [*pin, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=integration,
        )
        self.assertEqual((status.returncode, status.stdout), (0, ""))
        check = await self.run_git(
            [
                "merge-tree",
                "--write-tree",
                "--name-only",
                "-z",
                "--no-messages",
                f"refs/heads/{target}",
                f"refs/heads/{branch}",
            ],
            cwd=checkout,
        )
        self.assertEqual(check.returncode, 0)
        merged = await self.run_git(
            [
                *pin,
                *MERGE_CONFIG,
                "merge",
                "--no-ff",
                "--no-edit",
                "--quiet",
                "-m",
                f"Integrate {branch}",
                f"refs/heads/{branch}",
            ],
            cwd=integration,
        )
        self.assertEqual(merged.returncode, 0)
        self.assertEqual(fs.read(integration, "work.txt"), "work\n")
        self.assertEqual(
            git("log", "-1", "--format=%an <%ae>", cwd=integration),
            "Personal AI Workspace <integration@paw.invalid>",
        )
        listed = await self.run_git(
            ["worktree", "list", "--porcelain", "-z"], cwd=checkout
        )
        self.assertIn(f"worktree {integration}\0", listed.stdout)
        # The user's checkout never moved.
        self.assertEqual(git("rev-parse", "HEAD", cwd=checkout), base)

    async def test_a_hostile_worktree_git_file_is_not_followed_when_pinned(self):
        checkout = f"{self.root}/project"
        base = self.world.make_repository(checkout)
        worktree = f"{self.root}/.paw-worktrees/t/1/r/build"
        await self.run_git(
            ["worktree", "add", "--quiet", "-b", "paw/t/1/build", "--", worktree, base],
            cwd=checkout,
        )
        git_dir = f"{checkout}/.git/worktrees/build"
        # An agent points the worktree's .git at a repository outside the root.
        outside = f"{self.world.root}/outside"
        self.world.make_repository(outside)
        fs.write(worktree, ".git", f"gitdir: {outside}/.git\n")
        unpinned = await self.run_git(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=worktree
        )
        self.assertEqual(unpinned.returncode, wrapper.REJECTED)
        pinned = await self.run_git(
            [
                f"--git-dir={git_dir}",
                f"--work-tree={worktree}",
                "symbolic-ref",
                "--quiet",
                "HEAD",
            ],
            cwd=worktree,
        )
        self.assertEqual(pinned.stdout.strip(), "refs/heads/paw/t/1/build")

    async def test_a_command_in_the_repository_configuration_is_never_run(self):
        # An agent that commits in its worktree writes the checkout's shared
        # configuration too: a filter driver there (selected by a tracked
        # .gitattributes) is a command `status`, `merge`, `merge-tree` or
        # `worktree add` would run. The wrapper refuses those calls instead.
        checkout = f"{self.root}/project"
        base = self.world.make_repository(checkout)
        worktree = f"{self.root}/.paw-worktrees/t/1/r/build"
        result = await self.run_git(
            ["worktree", "add", "--quiet", "-b", "paw/t/1/build", "--", worktree, base],
            cwd=checkout,
        )
        self.assertEqual(result.returncode, 0)
        fs.write(worktree, ".gitattributes", "* filter=x diff=x merge=x\n")
        fs.write(worktree, "f.txt", "hi\n")
        git("add", "-A", cwd=worktree)
        git("commit", "--quiet", "-m", "attributes", cwd=worktree)
        marker = f"{self.world.root}/command-ran"
        included = f"{self.world.root}/included"
        fs.write(included, f'[filter "x"]\n\tclean = touch {marker}; cat\n')
        pin = [
            f"--git-dir={checkout}/.git/worktrees/build",
            f"--work-tree={worktree}",
        ]
        ref = "refs/heads/paw/t/1/build"
        calls = (
            (
                [*pin, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                worktree,
            ),
            ([*pin, "merge", "--abort"], worktree),
            (
                [
                    "merge-tree",
                    "--write-tree",
                    "--name-only",
                    "-z",
                    "--no-messages",
                    ref,
                    ref,
                ],
                checkout,
            ),
            (
                [
                    "worktree",
                    "add",
                    "--quiet",
                    "-b",
                    "paw/t/1/other",
                    "--",
                    f"{self.root}/.paw-worktrees/t/1/r/other",
                    base,
                ],
                checkout,
            ),
        )
        settings = (
            ("filter.x.clean", f"touch {marker}; cat"),
            ("filter.x.process", f"touch {marker}"),
            ("diff.x.textconv", f"touch {marker}; cat"),
            ("merge.x.driver", f"touch {marker}; true"),
            ("hook.x.command", f"touch {marker}"),
            ("core.pager", f"touch {marker}; cat"),
            ("include.path", included),
            ("includeIf.gitdir:/.path", included),
        )
        for key, value in settings:
            git("config", key, value, cwd=worktree)  # the shared config
            os.utime(f"{worktree}/f.txt", (2_000_000_000, 2_000_000_000))
            for args, cwd in calls:
                with self.subTest(key=key, args=args):
                    result = await self.run_git(args, cwd=cwd)
                    self.assertEqual(result.returncode, wrapper.REJECTED)
                    self.assertFalse(_consume(marker))
            git("config", "--unset", key, cwd=worktree)
        # The same, in the worktree's own configuration (config.worktree).
        git("config", "extensions.worktreeConfig", "true", cwd=worktree)
        git(
            "config",
            "--worktree",
            "filter.x.clean",
            f"touch {marker}; cat",
            cwd=worktree,
        )
        result = await self.run_git(calls[0][0], cwd=worktree)
        self.assertEqual(result.returncode, wrapper.REJECTED)
        self.assertFalse(_consume(marker))
        git("config", "--worktree", "--unset", "filter.x.clean", cwd=worktree)
        # Without such a setting (and with the credential helper `clone -c`
        # leaves behind), the same calls run.
        git("config", "credential.helper", "!gh auth git-credential", cwd=worktree)
        for args, cwd in calls[:1] + calls[2:]:
            result = await self.run_git(args, cwd=cwd)
            self.assertEqual(result.returncode, 0, args)

    async def test_a_git_directory_outside_the_root_is_never_used(self):
        # A directory in the root whose `.git` (a `gitdir:` file, or a link)
        # leads to a repository outside it: git would follow it after the
        # wrapper checked only the cwd.
        outside = f"{self.world.root}/outside"
        self.world.make_repository(outside)
        before = fs.read(outside, ".git/config")
        planted = f"{self.root}/planted"
        os.makedirs(planted)
        fs.write(planted, ".git", f"gitdir: {outside}/.git\n")
        linked = f"{self.root}/linked"
        os.makedirs(linked)
        os.symlink(f"{outside}/.git", f"{linked}/.git")
        ref = "refs/heads/paw/t/1/build"
        calls = (
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            ["remote", "add", "--", "origin", "https://github.com/o/r.git"],
            ["rev-parse", "--show-toplevel"],
            ["config", "--local", "--get", "remote.origin.url"],
            [
                "merge-tree",
                "--write-tree",
                "--name-only",
                "-z",
                "--no-messages",
                ref,
                ref,
            ],
            ["worktree", "list", "--porcelain", "-z"],
        )
        for cwd in (planted, linked):
            for args in calls:
                with self.subTest(cwd=cwd, args=args):
                    result = await self.run_git(args, cwd=cwd)
                    self.assertEqual(result.returncode, wrapper.REJECTED)
                    self.assertNotIn(outside, result.stdout)
        # `init` in a directory whose `.git` leads out would re-initialise the
        # repository outside.
        for target in (planted, linked):
            with self.subTest(init=target):
                result = await self.run_git(
                    [
                        "init",
                        "--quiet",
                        "--template=",
                        "--initial-branch=main",
                        "--",
                        target,
                    ],
                    cwd=target,
                )
                self.assertEqual(result.returncode, wrapper.REJECTED)
        self.assertEqual(fs.read(outside, ".git/config"), before)
        # A directory that is no repository at all is still git's to answer.
        os.makedirs(f"{self.root}/empty")
        result = await self.run_git(
            ["rev-parse", "--is-bare-repository"], cwd=f"{self.root}/empty"
        )
        self.assertEqual(result.returncode, 128)

    async def test_a_content_command_never_fetches_or_asks_for_credentials(self):
        # A partial clone lazily fetches a missing object from its promisor
        # remote, and an HTTP 401 makes git run the repository's credential
        # helper: a command, from a setting the wrapper otherwise lets through.
        requests = []

        class Unauthorized(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (the stdlib's name)
                requests.append(self.path)
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="x"')
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = do_GET

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Unauthorized)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        options = self.world.runner_options()
        options["allowed_protocols"] = (*options["allowed_protocols"], "http")
        runner = SshGitRunner(
            _FixedKey(f"{self.world.root}/alice.key"),
            ssh_executable=self.fake_sshd(options),
            **options,
        )
        checkout = f"{self.root}/project"
        base = self.world.make_repository(checkout)
        marker = f"{self.world.root}/helper-ran"
        port = server.server_address[1]
        for key, value in (
            ("core.repositoryFormatVersion", "1"),
            ("extensions.partialClone", "origin"),
            ("remote.origin.url", f"http://127.0.0.1:{port}/r.git"),
            ("remote.origin.promisor", "true"),
            ("credential.helper", f"!f() {{ touch {marker}; }}; f"),
        ):
            git("config", key, value, cwd=checkout)
        tree = git("rev-parse", f"{base}^{{tree}}", cwd=checkout)
        os.remove(f"{checkout}/.git/objects/{tree[:2]}/{tree[2:]}")
        result = await runner.run(
            [
                "worktree",
                "add",
                "--quiet",
                "-b",
                "paw/t/1/build",
                "--",
                f"{self.root}/.paw-worktrees/t/1/r/build",
                base,
            ],
            account=self.account,
            cwd=checkout,
            timeout_s=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(_consume(marker))
        self.assertEqual(requests, [])

    async def test_the_status_of_decision_0051_runs_pinned_without_submodules(self):
        checkout = f"{self.root}/project"
        base = self.world.make_repository(checkout)
        worktree = f"{self.root}/.paw-worktrees/t/1/r/_integration"
        result = await self.run_git(
            [
                "worktree",
                "add",
                "--quiet",
                "-b",
                "paw/t/1/_integration",
                "--",
                worktree,
                base,
            ],
            cwd=checkout,
        )
        self.assertEqual(result.returncode, 0)
        status = [
            f"--git-dir={checkout}/.git/worktrees/_integration",
            f"--work-tree={worktree}",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=normal",
            "--ignored=traditional",
            "--ignore-submodules=none",
        ]
        result = await self.run_git(status, cwd=worktree)
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        fs.write(worktree, ".gitignore", ".env\n")
        git("add", ".gitignore", cwd=worktree)
        git("commit", "--quiet", "-m", "ignore", cwd=worktree)
        fs.write(worktree, ".env", "SECRET=1\n")
        result = await self.run_git(status, cwd=worktree)
        self.assertEqual((result.returncode, result.stdout), (0, "!! .env\0"))
        # A repository's `core.fsmonitor` is a command: neither the call nor
        # any check before it (they run git too) runs it.
        marker = f"{self.world.root}/fsmonitor-ran"
        git("config", "core.fsmonitor", f"touch {marker}; true", cwd=worktree)
        for args, cwd in (
            (status, worktree),
            (["rev-parse", "--show-toplevel"], checkout),
            (["worktree", "list", "--porcelain", "-z"], checkout),
        ):
            with self.subTest(args=args):
                result = await self.run_git(args, cwd=cwd)
                self.assertEqual(result.returncode, 0)
                self.assertFalse(_consume(marker))
        git("config", "--unset", "core.fsmonitor", cwd=worktree)
        # A populated submodule whose own configuration has a filter: git would
        # run it in a child git. The wrapper refuses the call instead.
        nested = f"{self.world.root}/nested"
        self.world.make_repository(nested)
        git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "--quiet",
            nested,
            "sub",
            cwd=worktree,
        )
        git("commit", "--quiet", "-m", "submodule", cwd=worktree)
        marker = f"{self.world.root}/filter-ran"
        git("config", "filter.x.clean", f"touch {marker}; cat", cwd=f"{worktree}/sub")
        fs.write(f"{worktree}/sub", ".gitattributes", "* filter=x\n")
        fs.write(f"{worktree}/sub", "code.txt", "changed\n")
        result = await self.run_git(status, cwd=worktree)
        self.assertEqual(result.returncode, wrapper.REJECTED)
        self.assertFalse(_consume(marker))
        submodules = [*status[:2], "submodule", "status", "--cached"]
        result = await self.run_git(submodules, cwd=worktree)
        self.assertEqual(result.returncode, wrapper.REJECTED)
        # Not populated: the submodule is listed (the backend reads it as
        # "not exactly its commit"), and nothing inside it ran.
        shutil.rmtree(f"{worktree}/sub")
        os.makedirs(f"{worktree}/sub")
        result = await self.run_git(submodules, cwd=worktree)
        self.assertEqual(result.returncode, 0)
        self.assertIn(" sub", result.stdout)
        self.assertFalse(_consume(marker))

    async def test_a_link_inside_the_git_directory_is_never_followed(self):
        # A symbolic link inside the repository's own metadata (planted by
        # whatever can write the shared .git) would let git create files
        # outside the root while the git directory itself is inside it.
        checkout = f"{self.root}/project"
        base = self.world.make_repository(checkout)
        outside = f"{self.world.root}/outside-refs"
        os.makedirs(outside)
        os.symlink(outside, f"{checkout}/.git/refs/heads/paw")
        add = [
            "worktree",
            "add",
            "--quiet",
            "-b",
            "paw/victim",
            "--",
            f"{self.root}/.paw-worktrees/t/1/r/victim",
            base,
        ]
        result = await self.run_git(add, cwd=checkout)
        self.assertEqual(result.returncode, wrapper.REJECTED)
        self.assertEqual(os.listdir(outside), [])
        for args in (
            ["rev-parse", "--show-toplevel"],
            ["remote", "add", "--", "origin", "https://github.com/o/r.git"],
        ):
            with self.subTest(args=args):
                result = await self.run_git(args, cwd=checkout)
                self.assertEqual(result.returncode, wrapper.REJECTED)
        # Pinned, through the worktree's own directory under .git/worktrees.
        os.remove(f"{checkout}/.git/refs/heads/paw")
        worktree = f"{self.root}/.paw-worktrees/t/1/r/build"
        result = await self.run_git(
            ["worktree", "add", "--quiet", "-b", "paw/t/1/build", "--", worktree, base],
            cwd=checkout,
        )
        self.assertEqual(result.returncode, 0)
        git_dir = f"{checkout}/.git/worktrees/build"
        shutil.rmtree(f"{git_dir}/logs", ignore_errors=True)
        os.symlink(outside, f"{git_dir}/logs")
        result = await self.run_git(
            [
                f"--git-dir={git_dir}",
                f"--work-tree={worktree}",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            cwd=worktree,
        )
        self.assertEqual(result.returncode, wrapper.REJECTED)
        # A link among the hooks (which git never writes, and never runs
        # here) is left alone.
        os.remove(f"{git_dir}/logs")
        os.symlink(outside, f"{checkout}/.git/hooks/linked")
        result = await self.run_git(["rev-parse", "--show-toplevel"], cwd=checkout)
        self.assertEqual(result.returncode, 0)

    async def test_a_signing_command_is_never_run_by_merge(self):
        # `branch.<name>.mergeOptions=-S` makes `merge` sign (the command
        # line's commit.gpgSign=false does not undo an explicit -S), and the
        # signing configuration names commands.
        checkout = f"{self.root}/project"
        base = self.world.make_repository(checkout)
        worktree = f"{self.root}/.paw-worktrees/t/1/r/build"
        integration = f"{self.root}/.paw-worktrees/t/1/r/_integration"
        branch, target = "paw/t/1/build", "paw/t/1/_integration"
        for path, name in ((worktree, branch), (integration, target)):
            result = await self.run_git(
                ["worktree", "add", "--quiet", "-b", name, "--", path, base],
                cwd=checkout,
            )
            self.assertEqual(result.returncode, 0)
        fs.write(worktree, "work.txt", "work\n")
        git("add", "-A", cwd=worktree)
        git("commit", "--quiet", "-m", "work", cwd=worktree)
        marker = f"{self.world.root}/signer-ran"
        pin = [
            f"--git-dir={checkout}/.git/worktrees/_integration",
            f"--work-tree={integration}",
        ]
        merge = [
            *pin,
            *MERGE_CONFIG,
            "merge",
            "--no-ff",
            "--no-edit",
            "--quiet",
            "-m",
            f"Integrate {branch}",
            f"refs/heads/{branch}",
        ]
        settings = (
            (f"branch.{target}.mergeOptions", "-S"),
            ("gpg.format", "ssh"),
            ("gpg.ssh.defaultKeyCommand", f"sh -c 'touch {marker}; false'"),
        )
        for key, value in settings:
            git("config", key, value, cwd=checkout)
        for key, _ in settings:
            with self.subTest(key=key):
                result = await self.run_git(merge, cwd=integration)
                self.assertEqual(result.returncode, wrapper.REJECTED)
                self.assertFalse(_consume(marker))
            git("config", "--unset", key, cwd=checkout)
        result = await self.run_git(merge, cwd=integration)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(_consume(marker))

    async def test_a_hard_link_inside_the_git_directory_is_never_written(self):
        # A metadata file hard-linked to a file outside the root: git would
        # write that file's contents (the link itself is inside the root).
        checkout = f"{self.root}/project"
        base = self.world.make_repository(checkout)
        worktree = f"{self.root}/.paw-worktrees/t/1/r/build"
        integration = f"{self.root}/.paw-worktrees/t/1/r/_integration"
        branch, target = "paw/t/1/build", "paw/t/1/_integration"
        for path, name in ((worktree, branch), (integration, target)):
            result = await self.run_git(
                ["worktree", "add", "--quiet", "-b", name, "--", path, base],
                cwd=checkout,
            )
            self.assertEqual(result.returncode, 0)
        fs.write(worktree, "work.txt", "work\n")
        git("add", "-A", cwd=worktree)
        git("commit", "--quiet", "-m", "work", cwd=worktree)
        victim = f"{self.world.root}/victim.txt"
        fs.write(victim, "untouched\n")
        git_dir = f"{checkout}/.git/worktrees/_integration"
        os.link(victim, f"{git_dir}/MERGE_MSG")
        merge = [
            f"--git-dir={git_dir}",
            f"--work-tree={integration}",
            *MERGE_CONFIG,
            "merge",
            "--no-ff",
            "--no-edit",
            "--quiet",
            "-m",
            f"Integrate {branch}",
            f"refs/heads/{branch}",
        ]
        result = await self.run_git(merge, cwd=integration)
        self.assertEqual(result.returncode, wrapper.REJECTED)
        self.assertEqual(fs.read(victim), "untouched\n")
        os.remove(f"{git_dir}/MERGE_MSG")
        result = await self.run_git(merge, cwd=integration)
        self.assertEqual(result.returncode, 0)

    async def test_objects_of_a_repository_outside_the_root_are_never_read(self):
        # `objects/info/alternates` would let git read (and check out) the
        # objects of any readable repository outside the root.
        outside = f"{self.world.root}/outside"
        self.world.make_repository(outside)
        fs.write(outside, "secret.txt", "secret\n")
        git("add", "-A", cwd=outside)
        git("commit", "--quiet", "-m", "secret", cwd=outside)
        secret = git("rev-parse", "HEAD", cwd=outside)
        checkout = f"{self.root}/project"
        self.world.make_repository(checkout)
        fs.write(checkout, ".git/objects/info/alternates", f"{outside}/.git/objects\n")
        worktree = f"{self.root}/.paw-worktrees/t/1/r/build"
        add = [
            "worktree",
            "add",
            "--quiet",
            "-b",
            "paw/t/1/build",
            "--",
            worktree,
            secret,
        ]
        result = await self.run_git(add, cwd=checkout)
        self.assertEqual(result.returncode, wrapper.REJECTED)
        with self.assertRaises(FileNotFoundError):
            fs.read(worktree, "secret.txt")
        os.remove(f"{checkout}/.git/objects/info/alternates")
        result = await self.run_git(add, cwd=checkout)
        self.assertNotEqual(result.returncode, 0)  # an object it does not have
        with self.assertRaises(FileNotFoundError):
            fs.read(worktree, "secret.txt")

    async def test_init_never_writes_through_a_planted_git(self):
        # A `.git` git cannot read as a repository (no HEAD), whose `config`
        # is a link to a file outside the root: `init` would re-initialise it
        # and write that file.
        victim = f"{self.world.root}/victim"
        fs.write(victim, "untouched\n")
        target = f"{self.root}/new"
        os.makedirs(f"{target}/.git")
        os.symlink(victim, f"{target}/.git/config")
        result = await self.run_git(
            ["init", "--quiet", "--template=", "--initial-branch=main", "--", target],
            cwd=target,
        )
        self.assertEqual(result.returncode, wrapper.REJECTED)
        self.assertEqual(fs.read(victim), "untouched\n")

    async def test_a_work_tree_named_in_the_configuration_is_never_used(self):
        # `core.worktree` in the shared configuration would move an unpinned
        # `status` / `merge` in the checkout to a directory outside the root.
        checkout = f"{self.root}/project"
        self.world.make_repository(checkout)
        elsewhere = f"{self.world.root}/elsewhere"
        os.makedirs(elsewhere)
        fs.write(elsewhere, "secret.txt", "outside the root\n")
        git("config", "core.worktree", elsewhere, cwd=checkout)
        status = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
        result = await self.run_git(status, cwd=checkout)
        self.assertEqual(result.returncode, wrapper.REJECTED)
        self.assertNotIn("secret.txt", result.stdout)
        git("config", "--unset", "core.worktree", cwd=checkout)
        result = await self.run_git(status, cwd=checkout)
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    async def test_a_git_dir_is_used_only_with_its_own_work_tree(self):
        pins = {}
        for name in ("a", "b"):
            checkout = f"{self.root}/{name}"
            base = self.world.make_repository(checkout)
            worktree = f"{self.root}/.paw-worktrees/t/1/{name}/build"
            result = await self.run_git(
                [
                    "worktree",
                    "add",
                    "--quiet",
                    "-b",
                    "paw/t/1/build",
                    "--",
                    worktree,
                    base,
                ],
                cwd=checkout,
            )
            self.assertEqual(result.returncode, 0)
            pins[name] = (f"{checkout}/.git/worktrees/build", worktree)
        status = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
        mixed = await self.run_git(
            [f"--git-dir={pins['b'][0]}", f"--work-tree={pins['a'][1]}", *status],
            cwd=pins["a"][1],
        )
        self.assertEqual(mixed.returncode, wrapper.REJECTED)
        own = await self.run_git(
            [f"--git-dir={pins['a'][0]}", f"--work-tree={pins['a'][1]}", *status],
            cwd=pins["a"][1],
        )
        self.assertEqual(own.returncode, 0)

    async def test_the_worktree_git_of_pr_130_runs_through_the_wrapper(self):
        try:
            from paw_backend.integration.git import WorktreeGit
        except ImportError:
            self.skipTest("paw_backend.integration (PR #130) is not merged yet")
        checkout = f"{self.root}/project"
        base = self.world.make_repository(checkout)
        worktrees = WorktreeGit(self.runner, timeout_s=60)
        path = f"{self.root}/.paw-worktrees/t/1/r/build"
        await worktrees.add_worktree(
            checkout, path, "paw/t/1/build", base, self.account
        )
        pinned = await worktrees.pin(checkout, path, self.account)
        self.assertIsNotNone(pinned)
        self.assertEqual(
            await worktrees.current_branch(pinned, self.account), "paw/t/1/build"
        )
        self.assertTrue(await worktrees.is_clean(pinned, self.account))
        self.assertFalse(await worktrees.merging(pinned, self.account))
        self.assertEqual(
            await worktrees.worktree_branch(checkout, path, self.account),
            "paw/t/1/build",
        )


class WireCompatibilityTest(unittest.TestCase):
    """The hardening the client sends is exactly what the wrapper accepts."""

    def test_the_client_default_hardening_is_in_the_fixed_list(self):
        config = wrapper.Config(root="/srv/ws", home="/srv", user="u")
        sent = git_config_arguments(("https",))
        allowed = {f"{k}={v}" for k, v in wrapper.hardening(config)}
        self.assertEqual(set(sent[1::2]), allowed)
        self.assertEqual(sent[0::2], ["-c"] * len(sent[1::2]))
