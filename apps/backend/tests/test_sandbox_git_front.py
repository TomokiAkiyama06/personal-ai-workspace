"""The sandbox's ``git`` front (Decision 0090, 1; issue #208).

``/usr/local/bin/git`` in the tool sandbox image is ``deploy/sandbox/
paw_git_front.py``: a call aimed at the node's repository (``/workspace``) goes
to the shim as before (Decision 0083, 2), anything else to the image's real git
(a repository a test makes under ``/tmp``). ``init`` and ``clone`` are decided
by where they create the repository, every other call by where it runs (``cwd``
and ``-C``) and by what points git at another place (``--git-dir``,
``--work-tree``, ``GIT_DIR`` and the like).

The front is not a boundary (the model can run the real git itself): the mounts
and masks are, and they do not change (``test_ssh_git_wrapper_sandbox.py``).
Here the routing is a pure function (``route``) and the real program runs with
two stand-ins for the real git and the shim that print what they were given.
The image itself is checked as text (``Containerfile``).
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SANDBOX = BACKEND / "deploy" / "sandbox"
FRONT = SANDBOX / "paw_git_front.py"
CONTAINERFILE = SANDBOX / "Containerfile"


def _load_front():
    spec = importlib.util.spec_from_file_location("paw_git_front", FRONT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


front = _load_front()
REAL, SHIM = front.REAL, front.SHIM


def route(*argv, cwd="/workspace", env=None):
    return front.route(list(argv), cwd, env or {})


class RouteByPlaceTest(unittest.TestCase):
    def test_the_node_repository_goes_to_the_shim(self):
        for cwd in ("/workspace", "/workspace/src", "/workspace/a/../b"):
            self.assertEqual(route("status", cwd=cwd), SHIM, cwd)
        self.assertEqual(route("diff", "--stat", cwd="/workspace/x"), SHIM)
        self.assertEqual(route("-C", "/workspace", "log", cwd="/tmp"), SHIM)
        self.assertEqual(route("-C", "../workspace/src", "status", cwd="/tmp"), SHIM)
        self.assertEqual(
            route("-C", "/", "-C", "workspace", "status", cwd="/tmp"), SHIM
        )

    def test_a_repository_outside_goes_to_real_git(self):
        self.assertEqual(route("status", cwd="/tmp/repo"), REAL)
        self.assertEqual(route("-C", "/tmp/repo", "commit", "-m", "x"), REAL)
        self.assertEqual(route("-C", "/tmp", "-C", "repo", "add", "."), REAL)
        self.assertEqual(route("-C", "..", "status", cwd="/workspace"), REAL)
        self.assertEqual(route("-c", "user.name=a", "-C", "/tmp/r", "commit"), REAL)
        self.assertEqual(route("--no-pager", "-C", "/tmp/r", "log"), REAL)
        self.assertEqual(route("--version", cwd="/tmp"), REAL)
        # A name that only starts like the mount is outside it.
        self.assertEqual(route("status", cwd="/workspace2/r"), REAL)
        self.assertEqual(route("status", cwd="/tmp/workspace"), REAL)

    def test_the_last_C_wins_like_in_git(self):
        self.assertEqual(route("-C", "/tmp/r", "-C", "/workspace", "status"), SHIM)
        self.assertEqual(route("-C", "/workspace", "-C", "/tmp/r", "status"), REAL)

    def test_symbolic_links_are_resolved(self):
        base = tempfile.mkdtemp(prefix="paw-front-")
        self.addCleanup(shutil.rmtree, base, True)
        os.symlink("/workspace/src", os.path.join(base, "into"))
        os.mkdir(os.path.join(base, "plain"))
        os.symlink(os.path.join(base, "plain"), os.path.join(base, "out"))
        self.assertEqual(route("status", cwd=os.path.join(base, "into")), SHIM)
        self.assertEqual(route("-C", "into", "status", cwd=base), SHIM)
        self.assertEqual(route("-C", "out", "status", cwd=base), REAL)
        self.assertEqual(route("init", "into/new", cwd=base), SHIM)

    def test_options_and_variables_that_point_into_the_mount(self):
        cases = [
            ("--git-dir=/workspace/.git", "status"),
            ("--git-dir", "/workspace/.git", "status"),
            ("--work-tree=/workspace", "status"),
            ("--work-tree", "../../workspace", "add", "."),
            ("-c", "core.worktree=/workspace", "status"),
        ]
        for argv in cases:
            self.assertEqual(route(*argv, cwd="/tmp/r"), SHIM, argv)
        self.assertEqual(route("--git-dir=/tmp/r/.git", "status", cwd="/tmp"), REAL)
        for name in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
        ):
            env = {name: "/workspace/.git"}
            self.assertEqual(route("status", cwd="/tmp/r", env=env), SHIM, name)
            self.assertEqual(route("init", "/tmp/n", cwd="/tmp", env=env), SHIM, name)
            env = {name: "/tmp/r/.git"}
            self.assertEqual(route("status", cwd="/tmp/r", env=env), REAL, name)
        alternates = {"GIT_ALTERNATE_OBJECT_DIRECTORIES": "/tmp/o:/workspace/.git/o"}
        self.assertEqual(route("status", cwd="/tmp/r", env=alternates), SHIM)
        # Relative to where git runs.
        self.assertEqual(
            route("status", cwd="/tmp", env={"GIT_DIR": "../workspace/.git"}), SHIM
        )
        self.assertEqual(
            route(
                "--config-env=core.worktree=WT",
                "status",
                cwd="/tmp/r",
                env={"WT": "/workspace"},
            ),
            SHIM,
        )

    def test_quoted_alternates_are_decoded_like_git(self):
        # Codex review of #248 (P2), issue #250: an entry that starts with a
        # double quote is a C-style quoted path (git's parse_alt_odb_entry);
        # ``:`` inside it does not separate, ``#`` starts a comment.
        def alternates(value):
            env = {"GIT_ALTERNATE_OBJECT_DIRECTORIES": value}
            return route("status", cwd="/tmp/r", env=env)

        for value in (
            '"/workspace/a:b/.git/objects"',
            '/tmp/o:"/workspace/a:b/.git/objects"',
            '"/tmp/o":"/workspace/.git/objects"',
            '"/work\\163pace/.git/objects"',  # octal escape: s
            '"/tmp/o\\"x":/workspace/.git/objects',
        ):
            self.assertEqual(alternates(value), SHIM, value)
        for value in (
            '"/tmp/a:/workspace/.git/objects"',  # one entry under /tmp
            "#/workspace/.git/objects",  # a comment
            '"/workspace/x',  # broken quoting: a relative path, as is
            '"/workspace\\q"',  # unknown escape: a relative path, as is
        ):
            self.assertEqual(alternates(value), REAL, value)

    def test_what_cannot_be_read_goes_to_the_shim(self):
        for argv in (
            ("-C",),
            ("--git-dir",),
            ("--unknown-global-option", "status"),
        ):
            self.assertEqual(route(*argv, cwd="/tmp/r"), SHIM, argv)


class RouteByDestinationTest(unittest.TestCase):
    def test_init_is_decided_by_its_directory(self):
        # From the node's repository, as a test calls it without changing cwd
        # (``tests/repositories_support.py``).
        self.assertEqual(route("init", "/tmp/repo", cwd="/workspace"), REAL)
        self.assertEqual(route("init", "-q", "-b", "main", "/tmp/repo"), REAL)
        self.assertEqual(route("init", "--initial-branch=main", "/tmp/r"), REAL)
        self.assertEqual(route("init", "--bare", "--", "/tmp/r.git"), REAL)
        self.assertEqual(route("-C", "/tmp", "init", "repo"), REAL)
        self.assertEqual(route("init", cwd="/tmp/repo"), REAL)
        # Into the node's repository, from wherever.
        self.assertEqual(route("init", cwd="/workspace"), SHIM)
        self.assertEqual(route("init", "sub", cwd="/workspace"), SHIM)
        self.assertEqual(route("init", "/workspace/generated", cwd="/tmp"), SHIM)
        self.assertEqual(route("init", "../workspace/x", cwd="/tmp"), SHIM)
        self.assertEqual(route("-C", "/workspace", "init"), SHIM)
        self.assertEqual(route("-C", "/tmp", "init", "/workspace/x"), SHIM)
        # The separate git directory is a destination too.
        for argv in (
            ("init", "--separate-git-dir=/workspace/meta", "/tmp/repo"),
            ("init", "--separate-git-dir", "/workspace/meta", "/tmp/repo"),
        ):
            self.assertEqual(route(*argv, cwd="/tmp"), SHIM, argv)
        self.assertEqual(
            route("init", "--separate-git-dir=/tmp/meta", "/tmp/repo"), REAL
        )

    def test_clone_is_decided_by_its_directory(self):
        self.assertEqual(route("clone", "/tmp/a", "/tmp/b", cwd="/workspace"), REAL)
        self.assertEqual(
            route("clone", "-q", "--branch", "x", "/tmp/a", "/tmp/b"), REAL
        )
        self.assertEqual(route("clone", "/tmp/a", cwd="/tmp/work"), REAL)
        self.assertEqual(route("clone", "--", "/tmp/a", "/tmp/b"), REAL)
        self.assertEqual(route("clone", "/tmp/a", "/workspace/x", cwd="/tmp"), SHIM)
        self.assertEqual(route("clone", "/tmp/a", "x", cwd="/workspace"), SHIM)
        # Without a directory: the repository's name under where git runs.
        self.assertEqual(route("clone", "/tmp/a", cwd="/workspace"), SHIM)
        self.assertEqual(route("clone", "/tmp/workspace/.git", cwd="/"), SHIM)
        self.assertEqual(route("clone", "/tmp/workspace.git", cwd="/"), SHIM)
        self.assertEqual(route("clone", "/tmp/a/", cwd="/tmp/w"), REAL)
        for argv in (
            ("clone", "--separate-git-dir=/workspace/m", "/tmp/a", "/tmp/b"),
            ("clone", "--separate-git-dir", "/workspace/m", "/tmp/a", "/tmp/b"),
        ):
            self.assertEqual(route(*argv, cwd="/tmp"), SHIM, argv)

    def test_a_bare_clone_is_named_with_git(self):
        # Codex review of #248 (P2): ``--bare`` / ``--mirror`` without a
        # directory clone into ``<name>.git``.
        base = tempfile.mkdtemp(prefix="paw-front-")
        self.addCleanup(shutil.rmtree, base, True)
        os.symlink("/workspace/sub", os.path.join(base, "source.git"))
        for flag in ("--bare", "--mirror"):
            for source in ("/tmp/x/source", "/tmp/x/source.git", "/tmp/x/source/.git"):
                self.assertEqual(route("clone", flag, source, cwd=base), SHIM, flag)
            self.assertEqual(route("clone", flag, "/tmp/a", "b", cwd=base), REAL)
        self.assertEqual(route("clone", "/tmp/x/source", cwd=base), REAL)

    def test_a_clone_keeps_bundle_unless_the_source_is_a_bundle(self):
        # Codex review of #248 (P2), issue #250: git strips ``.bundle`` only
        # when the source is a bundle file, ``.git`` otherwise (one of them;
        # git_url_basename), as git 2.53 does.
        base = tempfile.mkdtemp(prefix="paw-front-")
        self.addCleanup(shutil.rmtree, base, True)
        source = os.path.join(base, "source")
        work = os.path.join(base, "work")
        os.mkdir(source)
        os.mkdir(work)

        def repository(path):
            for name in ("objects", "refs"):
                os.makedirs(os.path.join(path, name))
            with open(os.path.join(path, "HEAD"), "w") as handle:
                handle.write("ref: refs/heads/main\n")

        def bundle(path):
            with open(path, "w") as handle:
                handle.write("# v2 git bundle\n0000\n\n")

        repository(os.path.join(source, "repo.bundle"))
        repository(os.path.join(source, "with-git.bundle", ".git"))
        bundle(os.path.join(source, "file.bundle"))
        bundle(os.path.join(source, "named.git.bundle"))
        # A directory that is no repository, next to a bundle of its name.
        os.mkdir(os.path.join(source, "plain.git"))
        bundle(os.path.join(source, "plain.git.bundle"))
        with open(os.path.join(source, "link.bundle"), "w") as handle:
            handle.write(f"gitdir: {os.path.join(source, 'repo.bundle')}\n")
        expected = {
            "repo.bundle": "repo.bundle",
            "repo.bundle/": "repo.bundle",
            "with-git.bundle": "with-git.bundle",
            "with-git.bundle/.git": "with-git.bundle",
            "file.bundle": "file",
            "file": "file",
            "named.git.bundle": "named.git",
            "plain.git": "plain.git",
            "link.bundle": "link.bundle",
        }
        for given, name in expected.items():
            os.symlink("/workspace/sub", os.path.join(work, name))
            self.assertEqual(
                route("clone", os.path.join(source, given), cwd=work), SHIM, given
            )
            # Relative to where git runs.
            self.assertEqual(
                route("clone", os.path.join("..", "source", given), cwd=work),
                SHIM,
                given,
            )
            os.remove(os.path.join(work, name))
        # A source that is not a local path is never a bundle.
        os.symlink("/workspace/sub", os.path.join(work, "remote.bundle"))
        for given in (
            "https://example.com/remote.bundle",
            "https://user@example.com/x/remote.bundle/",
            "example.com:x/remote.bundle",
        ):
            self.assertEqual(route("clone", given, cwd=work), SHIM, given)
        # Like git: one suffix is stripped, trailing spaces and a port go.
        os.symlink("/workspace/sub", os.path.join(work, "remote"))
        for given in (
            "https://example.com/remote.git",
            "https://example.com/remote.git  ",
            "ssh://example.com:2222/remote/.git",
        ):
            self.assertEqual(route("clone", given, cwd=work), SHIM, given)
        os.symlink("/workspace/sub", os.path.join(work, "example.com"))
        self.assertEqual(route("clone", "example.com:22", cwd=work), SHIM)
        self.assertEqual(route("clone", "https://example.com:22", cwd=work), SHIM)

    def test_abbreviated_and_clustered_options_are_read_like_git(self):
        # Codex review of #248 (P2): git accepts a unique prefix of a long
        # option (``--sep`` is ``--separate-git-dir``) and clustered short
        # options (``-qb main``).
        for argv in (
            ("clone", "--sep=/workspace/m", "/tmp/a", "/tmp/b"),
            ("clone", "--separate", "/workspace/m", "/tmp/a", "/tmp/b"),
            ("init", "--sep=/workspace/m", "/tmp/r"),
            ("init", "--separate-git", "/workspace/m", "/tmp/r"),
        ):
            self.assertEqual(route(*argv, cwd="/tmp"), SHIM, argv)
        base = tempfile.mkdtemp(prefix="paw-front-")
        self.addCleanup(shutil.rmtree, base, True)
        os.symlink("/workspace/sub", os.path.join(base, "source.git"))
        for flag in ("--bar", "--mir"):
            self.assertEqual(route("clone", flag, "/tmp/source", cwd=base), SHIM)
        # --mirror implies bare; --no-mirror does not undo --bare (Codex review
        # of #248, P2).
        for flags in (("--mirror", "--no-bare"), ("--bare", "--no-mirror")):
            self.assertEqual(
                route("clone", *flags, "/tmp/source", cwd=base), SHIM, flags
            )
        self.assertEqual(
            route("clone", "--bare", "--no-bare", "/tmp/source", cwd=base), REAL
        )
        # --recursive is an alias of --recurse-submodules: not ambiguous.
        for prefix in ("--rec", "--no-rec"):
            self.assertEqual(route("clone", prefix, "/tmp/a", "/tmp/b"), REAL)
        for argv in (
            ("clone", "--sing", "/tmp/a", "/tmp/b"),  # --single-branch
            ("clone", "--bran", "x", "/tmp/a", "/tmp/b"),
            ("clone", "-qb", "x", "/tmp/a", "/tmp/b"),
            ("clone", "-qbx", "/tmp/a", "/tmp/b"),
            ("init", "-qb", "main", "/tmp/r"),
            ("init", "--init=main", "/tmp/r"),
        ):
            self.assertEqual(route(*argv, cwd="/tmp"), REAL, argv)
        # An option git would not take (unknown, or a prefix of two).
        for argv in (
            ("clone", "--no-such-option", "/tmp/a", "/tmp/b"),
            ("clone", "--s", "/tmp/a", "/tmp/b"),
            ("clone", "-Z", "/tmp/a", "/tmp/b"),
            ("init", "--unknown", "/tmp/r"),
        ):
            self.assertEqual(route(*argv, cwd="/tmp"), SHIM, argv)

    def test_a_creation_that_cannot_be_read_goes_to_the_shim(self):
        for argv in (
            ("init", "/tmp/a", "/tmp/b"),
            ("clone",),
            ("clone", "/tmp/a", "/tmp/b", "/tmp/c"),
            ("init", "--separate-git-dir"),
        ):
            self.assertEqual(route(*argv, cwd="/tmp"), SHIM, argv)


class ProgramTest(unittest.TestCase):
    """The real program, with stand-ins for the real git and the shim."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="paw-front-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.workspace = os.path.join(self.base, "workspace")
        self.outside = os.path.join(self.base, "tmp")
        os.mkdir(self.workspace)
        os.mkdir(self.outside)
        stand_ins = {}
        for name in ("real", "shim"):
            path = os.path.join(self.base, name)
            with open(path, "w") as handle:
                handle.write(
                    f"#!{sys.executable}\nimport json, sys\n"
                    f"print(json.dumps([{name!r}, *sys.argv[1:]]))\n"
                )
            os.chmod(path, 0o755)
            stand_ins[name] = path
        source = FRONT.read_text()
        for constant, value in (
            ("WORKSPACE", self.workspace),
            ("REAL_GIT", stand_ins["real"]),
            ("SHIM_PROGRAM", stand_ins["shim"]),
        ):
            line = next(
                x for x in source.splitlines() if x.startswith(f"{constant} = ")
            )
            source = source.replace(line, f"{constant} = {value!r}")
        self.program = os.path.join(self.base, "git")
        with open(self.program, "w") as handle:
            handle.write(source)

    def run_git(self, *argv, cwd, env=None):
        result = subprocess.run(
            [sys.executable, "-I", self.program, *argv],
            cwd=cwd,
            env=env or {},
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr

    def test_the_call_is_passed_on_unchanged(self):
        code, out, _ = self.run_git("status", "--short", "a b", cwd=self.workspace)
        self.assertEqual((code, out), (0, '["shim", "status", "--short", "a b"]'))
        code, out, _ = self.run_git("init", "-q", "r", cwd=self.outside)
        self.assertEqual((code, out), (0, '["real", "init", "-q", "r"]'))
        target = os.path.join(self.outside, "r")
        code, out, _ = self.run_git("init", target, cwd=self.workspace)
        self.assertEqual(out, f'["real", "init", "{target}"]')
        code, out, _ = self.run_git(
            "status", cwd=self.outside, env={"GIT_DIR": self.workspace}
        )
        self.assertEqual(out, '["shim", "status"]')

    def test_a_missing_git_is_gits_fatal(self):
        os.remove(os.path.join(self.base, "real"))
        code, out, err = self.run_git("status", cwd=self.outside)
        self.assertEqual((code, out), (128, ""))
        self.assertIn("git: ", err)


class ContainerfileTest(unittest.TestCase):
    def setUp(self):
        self.text = CONTAINERFILE.read_text()

    def test_the_front_the_shim_and_the_real_git_are_installed(self):
        self.assertIn(
            "COPY --chmod=0755 paw_git_front.py /usr/local/bin/git", self.text
        )
        self.assertIn(
            f"COPY --chmod=0755 paw_git_shim.py {front.SHIM_PROGRAM}", self.text
        )
        self.assertEqual(front.REAL_GIT, "/usr/lib/paw/git")
        self.assertIn(front.REAL_GIT, self.text)
        self.assertEqual(front.WORKSPACE, "/workspace")

    def test_the_build_refuses_a_real_git_on_the_path(self):
        # The image's own git is moved off PATH; the build fails if a copy of
        # it is still on PATH under any name (only the front is ``git``).
        self.assertIn("rm -f /usr/bin/git ", self.text)
        self.assertIn("cmp -s", self.text)
        self.assertIn(f"{front.REAL_GIT} --version", self.text)


if __name__ == "__main__":
    unittest.main()
