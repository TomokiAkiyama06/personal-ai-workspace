"""The wrapper's sandbox commands and git read forms (Decision 0083, 2 and 6).

``deploy/ssh-git-wrapper/paw_git_wrapper.py`` runs here as the test process's
own user in temporary directories (no other Linux user, no ``sshd``, no
Podman). Three layers, as in ``test_ssh_git_wrapper.py``:

* ``sandbox_plan`` / ``plan`` directly: what the backend sends is accepted;
  a writable checkout, a link, a missing or misplaced mask, a path a mount
  cannot take, an image by tag, an unknown operation are refused;
* the shim's git reads, as ``gitread.py`` sends them, pinned and not; the
  option lists are the backend's (``gitargs.py``);
* end to end: the real wrapper behind ``SshSandboxRunner`` /
  ``SshGitRunner`` (a fake ``ssh`` that does what ``sshd`` does with a forced
  command) and a fake ``podman`` that records its arguments and runs the
  command on the host.
"""

import asyncio
import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from paw_backend.repositories import SubprocessGitRunner
from paw_backend.repositories.ssh import SshGitRunner
from paw_backend.sandbox import gitargs
from paw_backend.sandbox.gitargs import parse_git_read
from paw_backend.sandbox.gitread import GitReader, pathspecs
from paw_backend.sandbox.plan import plan_sandbox, repository_of
from paw_backend.sandbox.runner import SshSandboxRunner
from paw_backend.sandbox.spec import (
    CommandKind,
    SandboxCommand,
    SandboxGoneError,
    SandboxMount,
    SandboxSpec,
    SandboxUnavailableError,
)
from paw_backend.sandbox.wire import exec_command, start_command, stop_command
from paw_backend.tools.scope import RealpathResolver

from .repositories_support import requires_git
from .sandbox_support import FILE_HELPER, World
from .test_ssh_git_wrapper import _FAKE_SSHD, WRAPPER_PATH, _FixedKey, wrapper

IMAGE = "registry.example/paw-sandbox@sha256:" + "a" * 64
SANDBOX = uuid.UUID("12345678-1234-5678-1234-567812345678")
NAME = "paw-sbx-12345678123456781234567812345678"
NONCE = "0123456789abcdef0123456789abcdef"

_FAKE_PODMAN = """#!__PYTHON__
import json, os, subprocess, sys
STATE = __STATE__
HELPER = __HELPER__
args = sys.argv[1:]
with open(os.path.join(STATE, "calls.jsonl"), "a") as log:
    log.write(json.dumps(args) + "\\n")
def state(name):
    return os.path.join(STATE, name + ".json")
if args[0] == "run":
    name = args[args.index("--name") + 1]
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    with open(state(name), "w") as handle:
        json.dump(mounts, handle)
    print("container-id")
elif args[:2] == ["container", "inspect"]:
    name = args[-1]
    if os.path.exists(state(name)):
        print("true")
    else:
        sys.exit(125)
elif args[0] == "rm":
    name = args[-1]
    if os.path.exists(state(name)):
        os.remove(state(name))
elif args[0] == "exec":
    rest = args[1:]
    stdin = None
    if rest[0] == "--interactive":
        rest = rest[1:]
        stdin = sys.stdin.buffer.read()
    assert rest[0] == "--user"
    rest = rest[2:]
    workdir, name, entry = rest[1], rest[2], rest[3:]
    with open(state(name)) as handle:
        mounts = json.load(handle)
    source = mounts[0].split(",")[1].removeprefix("source=")
    def host(path):
        return source + path[len("/workspace"):]
    if entry[4:6] == ["python3", "-I"]:
        entry = [sys.executable, "-I", HELPER, entry[7], host(entry[8])]
    result = subprocess.run(entry, cwd=host(workdir), input=stdin)
    sys.exit(result.returncode)
"""


class _Root:
    """A test home with a root, a checkout and a worktree inside it."""

    def __init__(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="paw-wsbx-"))
        self.home = f"{self.tmp}/home"
        self.root = f"{self.home}/workspaces"
        self.checkout = f"{self.root}/project/repo"
        self.worktree = f"{self.root}/.paw-worktrees/t/1/r/a"
        for directory in (self.checkout, self.worktree):
            os.makedirs(f"{directory}/secret")
        os.makedirs(f"{self.checkout}/.git")
        with open(f"{self.worktree}/.git", "w") as handle:
            handle.write("gitdir: elsewhere\n")
        with open(f"{self.worktree}/secret.txt", "w") as handle:
            handle.write("x")
        self.shims = f"{self.tmp}/shims"
        os.makedirs(f"{self.shims}/{SANDBOX.hex}")

    def config(self, *extra: str):
        return wrapper.parse_config(
            [
                f"--root={self.root}",
                f"--home={self.home}",
                f"--sandbox-image={IMAGE}",
                f"--sandbox-shim-base={self.shims}",
                *extra,
            ]
        )

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class SandboxPlanTest(unittest.TestCase):
    def setUp(self):
        self.r = _Root()
        self.addCleanup(self.r.close)

    def plan(self, spec, *extra):
        return wrapper.sandbox_plan(start_command(SANDBOX, spec), self.r.config(*extra))

    def refused(self, reason, spec=None, original=None, *extra):
        with self.assertRaises(wrapper.Rejected) as caught:
            if original is not None:
                wrapper.sandbox_plan(original, self.r.config(*extra))
            else:
                self.plan(spec, *extra)
        self.assertEqual(caught.exception.reason, reason)

    def test_a_writable_worktree_with_its_masks(self):
        worktree = self.r.worktree
        spec = SandboxSpec(
            (SandboxMount(worktree, "/workspace", writable=True),),
            masks=(f"{worktree}/secret", f"{worktree}/secret.txt"),
            readonly=(f"{worktree}/.git",),
            shim=True,
        )
        call = self.plan(spec)
        self.assertEqual(call.op, "start")
        argv = call.argv
        self.assertEqual(
            argv[:6], ["/usr/bin/podman", "run", "--detach", "--rm", "--name", NAME]
        )
        for pair in (
            ("--network", "none"),
            ("--pull", "never"),
            ("--userns", "keep-id"),
            ("--cap-drop", "all"),
            ("--security-opt", "no-new-privileges"),
            ("--cpus", "4"),
            ("--memory", "8g"),
            ("--pids-limit", "1024"),
            ("--timeout", "7200"),
            ("--user", f"{os.geteuid()}:{os.getegid()}"),
        ):
            index = argv.index(pair[0])
            self.assertEqual(argv[index + 1], pair[1], pair)
        self.assertEqual(argv[-3:], [IMAGE, "sleep", "infinity"])
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
        self.assertEqual(
            mounts,
            [
                f"type=bind,source={worktree},destination=/workspace",
                "type=tmpfs,destination=/workspace/secret,tmpfs-size=4k,tmpfs-mode=0555",
                "type=bind,source=/dev/null,destination=/workspace/secret.txt,ro=true",
                f"type=bind,source={worktree}/.git,destination=/workspace/.git,ro=true",
                f"type=bind,source={self.r.shims}/{SANDBOX.hex},destination=/run/paw-shim,ro=true",
            ],
        )
        self.assertNotIn("--group-add", argv)
        self.assertEqual(
            set(call.env), {"PATH", "HOME", "LC_ALL", "LANG", "XDG_RUNTIME_DIR"}
        )
        grouped = self.plan(spec, "--sandbox-keep-groups=yes")
        self.assertIn("keep-groups", grouped.argv)

    def test_a_checkout_is_read_only_or_refused(self):
        checkout = self.r.checkout
        spec = SandboxSpec(
            (SandboxMount(checkout, "/workspace", writable=False),),
            masks=(f"{checkout}/.git",),
        )
        mounts = [a for a in self.plan(spec).argv if a.startswith("type=")]
        self.assertEqual(
            mounts[0], f"type=bind,source={checkout},destination=/workspace,ro=true"
        )
        writable = SandboxSpec((SandboxMount(checkout, "/workspace", writable=True),))
        self.refused("path_outside_worktrees", writable)
        # A directory of .paw-worktrees without its .git file is no worktree.
        os.remove(f"{self.r.worktree}/.git")
        self.refused(
            "not_a_worktree",
            SandboxSpec((SandboxMount(self.r.worktree, "/workspace", writable=True),)),
        )

    def test_paths_are_checked_again(self):
        worktree = self.r.worktree
        mount = SandboxMount(worktree, "/workspace", writable=True)
        os.symlink(f"{self.r.checkout}/secret", f"{worktree}/link")
        self.refused("path_is_link", SandboxSpec((mount,), masks=(f"{worktree}/link",)))
        self.refused("bad_path", SandboxSpec((mount,), masks=(f"{worktree}/none",)))
        self.refused(
            "bad_mask",
            SandboxSpec(
                (mount,),
                masks=(f"{worktree}/secret",),
                readonly=(f"{worktree}/secret",),
            ),
        )
        outside = SandboxMount(self.r.tmp, "/workspace")
        self.refused("path_outside_root", SandboxSpec((outside,)))
        os.symlink(self.r.checkout, f"{self.r.root}/alias")
        self.refused(
            "path_is_link",
            SandboxSpec((SandboxMount(f"{self.r.root}/alias", "/workspace"),)),
        )
        os.makedirs(f"{worktree}/a,b")
        document = json.loads(
            wrapper.shlex.split(start_command(SANDBOX, SandboxSpec((mount,))))[3]
        )
        document["masks"] = [f"{worktree}/a,b"]
        original = shlex.join(
            ["paw-sandbox/v1", "start", str(SANDBOX), json.dumps(document)]
        )
        self.refused("bad_path", original=original)

    def test_the_spec_shape(self):
        mount = {"source": self.r.worktree, "target": "/workspace", "writable": True}
        cases = [
            ({"mounts": [mount], "masks": [], "readonly": []}, "bad_spec"),
            ({"mounts": [], "masks": [], "readonly": [], "shim": False}, "bad_spec"),
            (
                {"mounts": [mount, mount], "masks": [], "readonly": [], "shim": False},
                "bad_spec",
            ),
            (
                {
                    "mounts": [{**mount, "target": "/etc"}],
                    "masks": [],
                    "readonly": [],
                    "shim": False,
                },
                "bad_path",
            ),
            (
                {
                    "mounts": [{**mount, "target": "/workspace/x"}],
                    "masks": [],
                    "readonly": [],
                    "shim": False,
                },
                "bad_spec",
            ),
            (
                {
                    "mounts": [{**mount, "writable": "yes"}],
                    "masks": [],
                    "readonly": [],
                    "shim": False,
                },
                "bad_spec",
            ),
        ]
        for document, reason in cases:
            original = shlex.join(
                ["paw-sandbox/v1", "start", str(SANDBOX), json.dumps(document)]
            )
            self.refused(reason, original=original)
        self.refused(
            "bad_spec",
            original=shlex.join(["paw-sandbox/v1", "start", str(SANDBOX), "{"]),
        )

    def test_ids_operations_and_configuration(self):
        self.refused("bad_sandbox_id", original="paw-sandbox/v1 stop 1234")
        self.refused(
            "bad_sandbox_id",
            original="paw-sandbox/v1 stop ABCDEF78-1234-5678-1234-567812345678",
        )
        self.refused("bad_arguments", original=f"paw-sandbox/v1 kill {SANDBOX}")
        self.refused("bad_arguments", original=f"paw-sandbox/v1 stop {SANDBOX} extra")
        stop = wrapper.sandbox_plan(stop_command(SANDBOX), self.r.config())
        self.assertEqual(
            stop.argv,
            ["/usr/bin/podman", "rm", "--force", "--time", "5", "--ignore", NAME],
        )
        unconfigured = wrapper.parse_config(
            [f"--root={self.r.root}", f"--home={self.r.home}"]
        )
        with self.assertRaises(wrapper.Rejected) as caught:
            wrapper.sandbox_plan(stop_command(SANDBOX), unconfigured)
        self.assertEqual(caught.exception.reason, "sandbox_not_configured")
        for option in (
            "--sandbox-image=registry.example/paw-sandbox:latest",
            "--sandbox-cpus=0",
            "--sandbox-memory=lots",
            "--sandbox-pids=1",
            "--sandbox-lifetime=999999",
            "--sandbox-keep-groups=maybe",
            "--podman=relative",
        ):
            with self.assertRaises(wrapper.Rejected, msg=option):
                wrapper.parse_config([f"--root={self.r.root}", option])

    def test_exec_forms(self):
        config = self.r.config()
        shell = SandboxCommand(CommandKind.SHELL, "/workspace/src", "make")
        call = wrapper.sandbox_plan(exec_command(SANDBOX, NONCE, shell), config)
        self.assertEqual(
            call.argv,
            [
                "/usr/bin/podman",
                "exec",
                "--user",
                f"{os.geteuid()}:{os.getegid()}",
                "--workdir",
                "/workspace/src",
                NAME,
                "timeout",
                "-k",
                "5",
                "300",
                "bash",
                "-lc",
            ],
        )
        self.assertEqual(call.inspect[-1], NAME)
        write = SandboxCommand(
            CommandKind.FILE_WRITE, "/workspace", "/workspace/a.py", stdin=b"{}"
        )
        call = wrapper.sandbox_plan(exec_command(SANDBOX, NONCE, write), config)
        self.assertEqual(
            call.argv[2:],
            [
                "--interactive",
                "--user",
                f"{os.geteuid()}:{os.getegid()}",
                "--workdir",
                "/workspace",
                NAME,
                "timeout",
                "-k",
                "5",
                "300",
                "python3",
                "-I",
                "/usr/local/lib/paw/paw-file-tool",
                "write",
                "/workspace/a.py",
            ],
        )
        for words in (
            ["exec", str(SANDBOX), NONCE, "/etc", "shell"],
            ["exec", str(SANDBOX), NONCE, "/workspace", "python"],
            ["exec", str(SANDBOX), "x", "/workspace", "shell"],
            ["exec", str(SANDBOX), NONCE, "/workspace", "shell", "extra"],
            ["exec", str(SANDBOX), NONCE, "/workspace", "file-write"],
            ["exec", str(SANDBOX), NONCE, "/workspace", "file-write", "/etc/passwd"],
            ["exec", str(SANDBOX), NONCE, "/workspace/../x", "shell"],
        ):
            with self.assertRaises(wrapper.Rejected, msg=words):
                wrapper.sandbox_plan(shlex.join(["paw-sandbox/v1", *words]), config)


class ExecDeadlineTest(unittest.TestCase):
    def test_a_command_past_the_wrappers_deadline_has_no_status(self):
        r = _Root()
        self.addCleanup(r.close)
        config = r.config()
        command = SandboxCommand(CommandKind.SHELL, "/workspace", "sleep 9")
        call = wrapper.sandbox_plan(exec_command(SANDBOX, NONCE, command), config)

        def capture(argv, env, stdin, timeout):
            if argv == call.inspect:
                return 0, b"true\n", b"", False
            return None, b"", b"", False

        with self.assertRaises(wrapper.SandboxTimeout):
            wrapper.run_sandbox_exec(call, config, b"sleep 9", capture=capture)


class ReadFormTest(unittest.TestCase):
    """The shim's git reads as ``gitread.py`` sends them (``check_read``)."""

    def test_the_option_lists_are_the_backends(self):
        self.assertEqual(
            {name: set(options) for name, options in wrapper.READ_OPTIONS.items()},
            {name: set(options) for name, options in gitargs.CANONICAL_OPTIONS.items()},
        )
        self.assertEqual(wrapper.READ_FORMATS, gitargs.FORMATS)
        self.assertEqual(wrapper.READ_NAMES_ONLY_LOG, gitargs.NAMES_ONLY_LOG)
        from paw_backend.sandbox.gitread import _FIXED

        self.assertEqual(dict(wrapper.READ_FIXED), _FIXED)

    def test_accepted_and_refused_forms(self):
        sha = "0" * 40
        fixed = list(wrapper.READ_FIXED["log"])
        accepted = [
            ("status", ["--short", "--", "."], False),
            (
                "diff",
                [
                    *wrapper.READ_FIXED["diff"],
                    "--stat",
                    "--",
                    ":(literal)src/a.py",
                    ":(exclude,literal)secret",
                ],
                False,
            ),
            ("log", [*fixed, "--oneline", "--max-count=5", "--", "."], False),
            ("log", [*fixed, "--patch", sha, f"^{sha}", "--", "."], True),
            (
                "show",
                [*wrapper.READ_FIXED["show"], "--format=fuller", sha, "--", "."],
                True,
            ),
            (
                "diff",
                [*wrapper.READ_FIXED["diff"], "--unified=5", sha, sha, "--", "."],
                True,
            ),
        ]
        for subcommand, args, pinned in accepted:
            wrapper.check_read(subcommand, args, pinned=pinned)
        refused = [
            ("status", ["--short"], True),  # no pathspec
            ("status", ["--", ":(exclude,literal)x"], True),  # no positive one
            ("diff", ["--stat", "--", "."], True),  # the fixed options missing
            ("diff", [*wrapper.READ_FIXED["diff"], "--no-index", "--", "."], True),
            ("diff", [*wrapper.READ_FIXED["diff"], "--", ":(glob)*"], True),
            ("diff", [*wrapper.READ_FIXED["diff"], "--", ":(literal)../x"], True),
            (
                "diff",
                [*wrapper.READ_FIXED["diff"], "--", ":(literal)/etc/passwd"],
                True,
            ),
            ("diff", [*wrapper.READ_FIXED["diff"], "HEAD", "--", "."], True),
            ("diff", [*wrapper.READ_FIXED["diff"], f"{sha}:a", "--", "."], True),
            ("diff", [*wrapper.READ_FIXED["diff"], f"^{sha}", "--", "."], True),
            ("log", [*fixed, "--format=%H", "--", "."], True),
            ("log", [*fixed, "--patch", "--", "."], False),  # unpinned: names only
            ("log", [*fixed, sha, "--", "."], False),  # unpinned: no revision
            ("show", [*wrapper.READ_FIXED["show"], sha, "--", "."], False),
            ("status", ["--short", sha, "--", "."], True),
        ]
        for subcommand, args, pinned in refused:
            with self.assertRaises(wrapper.Rejected, msg=(subcommand, args, pinned)):
                wrapper.check_read(subcommand, args, pinned=pinned)

    def test_other_reads_of_the_shim(self):
        sha = "1" * 40
        for args in (
            ["rev-parse", "--verify", "--quiet", "HEAD~3^{commit}"],
            ["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
            ["rev-parse", "--verify", "--quiet", "refs/paw/base/paw/t/1/a^{commit}"],
            ["cat-file", "-t", sha],
            ["merge-base", "--is-ancestor", sha, sha],
        ):
            checker, _ = wrapper.SUBCOMMANDS[args[0]]
            checker(args[1:], None, None)
        for args in (
            ["rev-parse", "--verify", "--quiet", "refs/paw/base/main^{commit}"],
            ["rev-parse", "--verify", "--quiet", "HEAD~0^{commit}"],
            ["cat-file", "-p", sha],
            ["cat-file", "-t", "HEAD"],
            ["merge-base", "--is-ancestor", "HEAD", sha],
        ):
            checker, _ = wrapper.SUBCOMMANDS[args[0]]
            with self.assertRaises(wrapper.Rejected, msg=args):
                checker(args[1:], None, None)


@requires_git
class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    """The real wrapper behind the SSH runners, a fake ``podman``, real git."""

    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        ws = self.world.ws
        self.state = tempfile.mkdtemp(prefix="paw-podman-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.podman = f"{self.state}/podman"
        with open(self.podman, "w") as handle:
            handle.write(
                _FAKE_PODMAN.replace("__PYTHON__", sys.executable)
                .replace("__STATE__", repr(self.state))
                .replace("__HELPER__", repr(str(FILE_HELPER)))
            )
        os.chmod(self.podman, 0o755)
        key = f"{self.state}/alice.key"
        with open(key, "w") as handle:
            handle.write("not a real key\n")
        os.chmod(key, 0o600)
        options = [
            f"--root={ws.home}/workspaces",
            f"--home={ws.home}",
            f"--git={shutil.which('git')}",
            f"--podman={self.podman}",
            f"--sandbox-image={IMAGE}",
        ]
        sshd = f"{self.state}/fake-sshd.py"
        with open(sshd, "w") as handle:
            handle.write(
                # ``sshd``'s environment (here a hostile one, as in
                # ``test_ssh_git_wrapper``) never reaches podman. LD_PRELOAD is
                # left out: the loader would complain on the wrapper's stderr.
                _FAKE_SSHD.replace('    "LD_PRELOAD": "/tmp/evil.so",\n', "")
                .replace("__PYTHON__", repr(sys.executable))
                .replace("__WRAPPER__", repr(str(WRAPPER_PATH)))
                .replace("__OPTIONS__", repr(options))
            )
        os.chmod(sshd, 0o755)
        self.sandboxes = SshSandboxRunner(_FixedKey(key), ssh_executable=sshd)
        self.git = SshGitRunner(_FixedKey(key), ssh_executable=sshd)

    def calls(self):
        with open(f"{self.state}/calls.jsonl") as handle:
            return [json.loads(line) for line in handle]

    async def test_start_exec_stop(self):
        world = self.world
        repository = repository_of(world.scope, world.root)
        located = await GitReader(SubprocessGitRunner()).locate(
            world.root, account=world.account, checkouts=world.scope.excluded_paths
        )
        spec = plan_sandbox(
            world.scope, repository, located, writable=True, shim=False
        ).spec
        account = world.account
        await self.sandboxes.start(account, SANDBOX, spec)
        result = await self.sandboxes.exec(
            account,
            SANDBOX,
            SandboxCommand(
                CommandKind.SHELL, "/workspace", "cat code.txt; echo e >&2; exit 7"
            ),
            timeout_s=60,
        )
        self.assertEqual(
            (result.exit_code, result.stdout, result.stderr), (7, "code\n", "e\n")
        )
        write = SandboxCommand(
            CommandKind.FILE_WRITE,
            "/workspace",
            "/workspace/new.txt",
            stdin=json.dumps({"content": "hi"}).encode(),
        )
        result = await self.sandboxes.exec(account, SANDBOX, write, timeout_s=60)
        self.assertEqual(json.loads(result.stdout), {"ok": True, "chars": 2})
        written = await asyncio.to_thread(Path(world.worktree, "new.txt").read_text)
        self.assertEqual(written, "hi")
        await self.sandboxes.stop(account, SANDBOX)
        with self.assertRaises(SandboxGoneError):
            await self.sandboxes.exec(
                account,
                SANDBOX,
                SandboxCommand(CommandKind.SHELL, "/workspace", "true"),
                timeout_s=60,
            )
        operations = [
            call[0] if call[0] != "container" else "inspect" for call in self.calls()
        ]
        self.assertEqual(
            operations, ["run", "inspect", "exec", "inspect", "exec", "rm", "inspect"]
        )
        # The checkout is never mounted writable: refused before podman runs.
        checkout = SandboxSpec(
            (SandboxMount(world.checkout, "/workspace", writable=True),)
        )
        with self.assertRaises(SandboxUnavailableError) as caught:
            await self.sandboxes.start(account, uuid.uuid4(), checkout)
        self.assertEqual(caught.exception.reason, "refused")
        self.assertEqual(len(self.calls()), 7)

    async def test_the_shims_git_runs_through_the_wrapper(self):
        world = self.world
        from .worktrees_support import commit_file

        commit = commit_file(world.worktree, "src/a.py", "one\n", "node work")
        reader = GitReader(self.git)
        located = await reader.locate(
            world.root, account=world.account, checkouts=world.scope.excluded_paths
        )
        self.assertEqual(located.base, world.base_commit)
        repository = repository_of(world.scope, world.root)
        hidden = plan_sandbox(
            world.scope, repository, located, writable=True, shim=False
        ).hidden
        outputs = []
        for argv in (
            ["status", "--short"],
            ["diff", "--stat", "HEAD~1"],
            ["log", "-p"],
            ["show", "--format=oneline", commit],
        ):
            args = parse_git_read(argv)
            specs = await pathspecs(
                args.paths,
                workdir=world.root,
                root=world.root,
                scope=world.scope,
                resolver=RealpathResolver(),
                hidden=hidden,
            )
            answer = await reader.read(
                located, args, account=world.account, pathspecs=specs
            )
            self.assertEqual(answer.exit_code, 0, argv)
            outputs.append(answer.output)
        self.assertIn("src/a.py", outputs[1])
        self.assertIn("+one", outputs[2])
        self.assertIn(commit, outputs[3])
        self.assertNotIn("s3cret", "".join(outputs))


if __name__ == "__main__":
    unittest.main()
