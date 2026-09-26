"""``SshGitRunner``: git as another Linux user over SSH (Issue #105, Decision 0029).

No real ``sshd``, no real second Linux user, no network: ``ssh_executable`` points
at a small fake program this file writes (never the real ``ssh``). For the
"success" and "round trip" tests, the fake stands in for *both* ``ssh`` and the
(not-yet-deployed) forced-command wrapper: it receives exactly the argv
:class:`SshGitRunner` builds, decodes the last argument the same way
``build_remote_command``'s docstring says a wrapper must, and then runs the real
``git`` locally — proving the wire format round-trips, without depending on one
existing anywhere.
"""

import os
import shlex
import unittest
import uuid
from unittest import mock

from paw_backend.config import Settings
from paw_backend.repositories import (
    GitClient,
    GitCommandError,
    GitFailure,
    LinuxAccount,
    RepositoryPolicy,
)
from paw_backend.repositories.ssh import (
    PROTOCOL_TAG,
    SshGitRunner,
    SshGitRunnerPolicy,
    SshKeyDirectory,
    TemplateSshKeyDirectory,
    build_remote_command,
    validate_identity_template,
)

from .repositories_support import World, fs, requires_git
from .support import make_settings, paw_environment

#: A fake ``ssh`` executable, generated fresh per test by ``fake_ssh`` below.
#: ``__MARKER__``, ``__MODE__`` and ``__TAG__`` are baked in as Python literals
#: (``repr``) at write time — never read from the environment, matching
#: production reality: ``SshGitRunner`` gives its child only ``PATH`` (module
#: docstring), so a test double that expected its own env vars to arrive would
#: be testing a plumbing path production does not have.
_FAKE_SSH = """#!/usr/bin/env python3
import os
import shlex
import subprocess
import sys

marker = __MARKER__
mode = __MODE__
with open(marker, "w") as handle:
    handle.write("\\n".join(sys.argv[1:]))

command = sys.argv[-1]

if mode == "fail_transport":
    sys.exit(255)
if mode == "fail_other":
    sys.exit(17)
if mode == "echo_env":
    for key in sorted(os.environ):
        print(f"{key}={os.environ[key]}")
    sys.exit(0)

# mode == "wrap": act as the (not yet deployed) forced-command wrapper would:
# decode the words, honour the protocol tag, cwd and ceiling, then run real git.
words = shlex.split(command)
tag, cwd, ceiling, sep, *rest = words
if tag != __TAG__:
    sys.exit(90)
if sep != "--":
    sys.exit(91)
env = {"PATH": os.environ.get("PATH", "")}
if ceiling != "-":
    env["GIT_CEILING_DIRECTORIES"] = ceiling
result = subprocess.run(
    ["git", *rest], cwd=cwd, env=env, capture_output=True, text=True
)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
sys.exit(result.returncode)
"""


class _FixedKey:
    """An ``SshKeyDirectory`` that always answers the same (already checked) path."""

    def __init__(self, path: str, *, error: OSError | None = None) -> None:
        self._path = path
        self._error = error

    async def key_path_of(self, account: LinuxAccount) -> str:
        if self._error is not None:
            raise self._error
        return self._path


class SshTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.account = LinuxAccount(
            uuid.uuid4(), "alice", os.geteuid(), self.world.make_home("alice")
        )
        self.marker = f"{self.world.root}/ssh.marker"

    def fake_ssh(self, mode: str = "wrap") -> str:
        path = f"{self.world.root}/fake-ssh-{mode}.py"
        script = (
            _FAKE_SSH.replace("__MARKER__", repr(self.marker))
            .replace("__MODE__", repr(mode))
            .replace("__TAG__", repr(PROTOCOL_TAG))
        )
        fs.write(path, script)
        os.chmod(path, 0o755)
        return path

    def key_at(self, path: str, *, mode: int = 0o600) -> str:
        fs.write(path, "not a real key\n")
        os.chmod(path, mode)
        return path

    def key(self, *, mode: int = 0o600) -> str:
        """A private key file only this process can read, owned by it."""
        return self.key_at(f"{self.world.root}/alice.key", mode=mode)

    def runner(self, *, mode: str = "wrap", **options) -> SshGitRunner:
        keys = _FixedKey(self.key())
        return SshGitRunner(keys, ssh_executable=self.fake_ssh(mode), **options)

    async def run_git(self, args, *, cwd=None, timeout_s=30, runner=None, mode="wrap"):
        runner = runner or self.runner(mode=mode)
        return await runner.run(
            args,
            account=self.account,
            cwd=cwd or self.account.home,
            timeout_s=timeout_s,
        )

    def sent_argv(self) -> list[str]:
        return fs.read(self.marker).split("\n")


class IdentityTemplateTest(unittest.TestCase):
    def test_a_template_names_the_user_once(self):
        for value in (
            "/etc/paw/ssh-keys/{user}.key",
            "/etc/paw/ssh-keys/{user}",
            "/srv/keys/{user}/id_ed25519",
        ):
            self.assertEqual(validate_identity_template(value), value)

    def test_a_bad_template_is_refused(self):
        for value in (
            "",
            None,
            5,
            "/etc/paw/ssh-keys/{home}/{user}.key",  # {home} is user-writable
            "/etc/paw/ssh-keys/id.key",  # no {user} at all
            "/etc/paw/ssh-keys/{user}/{user}.key",  # {user} twice
            "relative/{user}.key",
            "/etc/paw/ssh-keys/{user}/../escape.key",
            "/etc/paw/ssh-keys/{user}/./x.key",
            "/etc/paw/ssh-keys/{user}}.key",
            "{user",
            "/etc/{unknown}/{user}.key",
        ):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    validate_identity_template(value)


class TemplateSshKeyDirectoryTest(SshTestCase):
    """The real class, through its public constructor: ``{user}`` is ``alice``."""

    def directory(self) -> TemplateSshKeyDirectory:
        return TemplateSshKeyDirectory(f"{self.world.root}/{{user}}")

    async def test_a_normal_key_file_is_accepted(self):
        path = self.key_at(f"{self.world.root}/alice", mode=0o600)
        self.assertEqual(await self.directory().key_path_of(self.account), path)

    async def test_a_missing_key_is_refused(self):
        with self.assertRaises(FileNotFoundError):
            await self.directory().key_path_of(self.account)

    async def test_a_key_readable_or_writable_by_others_is_refused(self):
        for mode in (0o604, 0o640, 0o660, 0o666, 0o602):
            with self.subTest(mode=oct(mode)):
                self.key_at(f"{self.world.root}/alice", mode=mode)
                with self.assertRaises(PermissionError):
                    await self.directory().key_path_of(self.account)

    async def test_a_directory_is_not_a_key(self):
        os.makedirs(f"{self.world.root}/alice", mode=0o700)
        with self.assertRaises(PermissionError):
            await self.directory().key_path_of(self.account)

    async def test_a_symlink_is_refused_even_when_the_target_is_fine(self):
        real = self.key_at(f"{self.world.root}/real.key", mode=0o600)
        os.symlink(real, f"{self.world.root}/alice")
        with self.assertRaises(PermissionError):
            await self.directory().key_path_of(self.account)

    async def test_a_key_owned_by_someone_else_is_refused(self):
        self.key_at(f"{self.world.root}/alice", mode=0o600)
        with mock.patch("os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(PermissionError):
                await self.directory().key_path_of(self.account)

    def test_key_path_of_is_the_protocol(self):
        self.assertTrue(hasattr(self.directory(), "key_path_of"))
        directory: SshKeyDirectory = self.directory()
        self.assertTrue(callable(directory.key_path_of))


class SshGitRunnerPolicyTest(unittest.TestCase):
    def test_the_defaults(self):
        policy = SshGitRunnerPolicy()
        self.assertEqual(
            (
                policy.host,
                policy.port,
                policy.connect_timeout_s,
                policy.known_hosts_path,
            ),
            ("127.0.0.1", 22, 10, "/etc/paw/ssh_known_hosts"),
        )

    def test_the_host_may_be_an_address_unlike_clone_hosts(self):
        self.assertEqual(SshGitRunnerPolicy(host="127.0.0.1").host, "127.0.0.1")
        self.assertEqual(SshGitRunnerPolicy(host="10.0.0.5").host, "10.0.0.5")
        self.assertEqual(SshGitRunnerPolicy(host="[::1]").host, "[::1]")

    def test_a_bad_host_is_refused(self):
        for host in ("", None, 5, "GITHUB.com", "host name", "a\n"):
            with self.subTest(host=repr(host)):
                with self.assertRaises(ValueError):
                    SshGitRunnerPolicy(host=host)

    def test_port_and_timeout_are_bounded(self):
        for kwargs in (
            {"port": 0},
            {"port": 70_000},
            {"port": True},
            {"port": "22"},
            {"connect_timeout_s": 0},
            {"connect_timeout_s": -1},
            {"connect_timeout_s": True},
            {"connect_timeout_s": 301},
            {"connect_timeout_s": 1.5},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    SshGitRunnerPolicy(**kwargs)

    def test_known_hosts_path_must_be_canonical(self):
        for value in ("relative/known_hosts", "/etc/../known_hosts", "", None, 5):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    SshGitRunnerPolicy(known_hosts_path=value)

    def test_from_settings(self):
        with paw_environment(
            PAW_REPOSITORY_SSH_HOST="10.0.0.9",
            PAW_REPOSITORY_SSH_PORT="2222",
            PAW_REPOSITORY_SSH_CONNECT_TIMEOUT_SECONDS="5",
            PAW_REPOSITORY_SSH_KNOWN_HOSTS_PATH="/etc/paw/hosts",
        ):
            policy = SshGitRunnerPolicy.from_settings(Settings())
        self.assertEqual(
            (
                policy.host,
                policy.port,
                policy.connect_timeout_s,
                policy.known_hosts_path,
            ),
            ("10.0.0.9", 2222, 5, "/etc/paw/hosts"),
        )

    def test_without_settings_the_defaults_apply(self):
        self.assertEqual(
            SshGitRunnerPolicy.from_settings(make_settings()), SshGitRunnerPolicy()
        )


class BuildRemoteCommandTest(unittest.TestCase):
    def test_the_words_round_trip_through_shlex(self):
        command = build_remote_command(
            ["commit", "-m", "a message with spaces and 'quotes'"],
            cwd="/home/alice/workspaces/x",
            ceiling="/home/alice",
            allowed_protocols=("https",),
        )
        words = shlex.split(command)
        self.assertEqual(words[0], PROTOCOL_TAG)
        self.assertEqual(words[1], "/home/alice/workspaces/x")
        self.assertEqual(words[2], "/home/alice")
        self.assertEqual(words[3], "--")
        self.assertEqual(
            words[-3:], ["commit", "-m", "a message with spaces and 'quotes'"]
        )

    def test_hostile_characters_never_break_the_word_boundary(self):
        for hostile in (
            "; rm -rf /",
            "$(touch /tmp/x)",
            "`touch /tmp/x`",
            "a\nb",
            "a'b\"c",
            "--upload-pack=touch /tmp/never",
        ):
            with self.subTest(hostile=hostile[:20]):
                command = build_remote_command(
                    ["rev-parse", hostile],
                    cwd="/x",
                    ceiling=None,
                    allowed_protocols=("https",),
                )
                words = shlex.split(command)
                self.assertEqual(words[-1], hostile)
                self.assertEqual(words[-2], "rev-parse")

    def test_no_cwd_or_ceiling_encode_as_fixed_placeholders(self):
        command = build_remote_command(
            ["status"], cwd=None, ceiling=None, allowed_protocols=("https",)
        )
        words = shlex.split(command)
        self.assertEqual((words[1], words[2]), (".", "-"))

    def test_extra_config_travels_after_the_built_in_pairs(self):
        command = build_remote_command(
            ["clone", "--", "u", "d"],
            cwd="/x",
            ceiling=None,
            allowed_protocols=("https",),
            extra_config=[("credential.helper", "!true")],
        )
        words = shlex.split(command)
        self.assertIn("-c", words)
        self.assertIn("credential.helper=!true", words)
        self.assertEqual(words[-4:], ["clone", "--", "u", "d"])


class ConstructionTest(SshTestCase):
    def test_keys_must_look_like_an_ssh_key_directory(self):
        with self.assertRaises(TypeError):
            SshGitRunner(object())

    def test_policy_must_be_an_ssh_git_runner_policy(self):
        with self.assertRaises(TypeError):
            SshGitRunner(_FixedKey(self.key()), policy=object())

    def test_allowed_protocols_and_output_limit_are_validated(self):
        key = _FixedKey(self.key())
        for kwargs in (
            {"allowed_protocols": ()},
            {"allowed_protocols": ("HTTPS",)},
            {"allowed_protocols": (5,)},
            {"max_output_bytes": 0},
            {"max_output_bytes": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    SshGitRunner(key, **kwargs)

    def test_ssh_config_path_must_be_canonical(self):
        with self.assertRaises(ValueError):
            SshGitRunner(_FixedKey(self.key()), ssh_config_path="relative")


@requires_git
class RunTest(SshTestCase):
    async def test_the_fixed_options_are_always_sent(self):
        identity = self.key()
        runner = SshGitRunner(
            _FixedKey(identity),
            policy=SshGitRunnerPolicy(port=2222, connect_timeout_s=7),
            ssh_executable=self.fake_ssh("wrap"),
        )
        await self.run_git(
            ["rev-parse", "--is-bare-repository"], runner=runner, mode="wrap"
        )
        argv = self.sent_argv()
        self.assertEqual(argv[0:2], ["-i", identity])
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertIn("RequestTTY=no", argv)
        self.assertIn("ForwardAgent=no", argv)
        self.assertIn("ConnectTimeout=7", argv)
        self.assertEqual(argv[-2], f"{self.account.username}@127.0.0.1")
        # the encoded command is the very last argument, on its own
        words = shlex.split(argv[-1])
        self.assertEqual(words[0], PROTOCOL_TAG)

    async def test_a_successful_round_trip_through_a_compliant_wrapper(self):
        self.world.make_repository(f"{self.account.home}/repo")
        result = await self.run_git(
            ["rev-parse", "--is-bare-repository"], cwd=f"{self.account.home}/repo"
        )
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "false"))

    async def test_the_ceiling_reaches_the_wrapper(self):
        outer = f"{self.account.home}/outer"
        self.world.make_repository(outer)
        inner = f"{outer}/inner"
        os.makedirs(inner)

        # ``GIT_CEILING_DIRECTORIES`` excludes the directories it names, not the
        # directory git started in: naming ``outer`` is what stops discovery from
        # adopting it as the repository of ``inner`` (this is exactly how
        # ``GitClient.inspect`` uses it: the ceiling is the checkout's *parent*).
        unbounded = await self.runner().run(
            ["rev-parse", "--show-toplevel"],
            account=self.account,
            cwd=inner,
            timeout_s=10,
        )
        bounded = await self.runner().run(
            ["rev-parse", "--show-toplevel"],
            account=self.account,
            cwd=inner,
            timeout_s=10,
            ceiling=outer,
        )

        self.assertEqual((unbounded.returncode, unbounded.stdout.strip()), (0, outer))
        self.assertNotEqual(bounded.returncode, 0)

    async def test_a_transport_failure_is_reported_distinctly(self):
        with self.assertRaises(GitCommandError) as raised:
            await self.run_git(["status"], mode="fail_transport")
        self.assertIs(raised.exception.failure, GitFailure.SSH_UNAVAILABLE)

    async def test_the_remote_commands_own_exit_code_is_not_a_transport_failure(self):
        result = await self.run_git(["status"], mode="fail_other")
        self.assertEqual(result.returncode, 17)

    async def test_only_path_reaches_the_local_ssh_process(self):
        result = await self.run_git(["status"], mode="echo_env")
        names = set()
        for line in result.stdout.splitlines():
            if "=" in line:
                names.add(line.split("=", 1)[0])
        # ``LC_CTYPE`` is not something ``SshGitRunner`` adds: a bare CPython
        # child coerces its own locale at startup (PEP 538) when none of
        # LC_ALL/LC_CTYPE/LANG were set, which is exactly this fake double's own
        # situation (the real ``ssh`` binary is not Python and does no such
        # thing). What matters is that nothing *else* — no backend secret, no
        # inherited ``SSH_*`` or ``GIT_*`` variable — reaches the child.
        self.assertEqual(names - {"LC_CTYPE"}, {"PATH"})

    async def test_a_key_the_directory_refuses_never_starts_ssh(self):
        keys = _FixedKey(self.key(), error=PermissionError("nope"))
        runner = SshGitRunner(keys, ssh_executable=self.fake_ssh("wrap"))
        with self.assertRaises(GitCommandError) as raised:
            await self.run_git(["status"], runner=runner)
        self.assertIs(raised.exception.failure, GitFailure.SSH_KEY_UNAVAILABLE)
        self.assertFalse(fs.exists(self.marker), "ssh was started despite no key")

    async def test_a_missing_ssh_executable_is_reported_as_such(self):
        runner = SshGitRunner(
            _FixedKey(self.key()), ssh_executable=f"{self.world.root}/no-such-ssh"
        )
        with self.assertRaises(GitCommandError) as raised:
            await self.run_git(["status"], runner=runner)
        self.assertIs(raised.exception.failure, GitFailure.NOT_INSTALLED)

    async def test_a_command_that_runs_too_long_is_killed(self):
        script = f"{self.world.root}/slow-ssh.py"
        fs.write(script, "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n")
        os.chmod(script, 0o755)
        runner = SshGitRunner(_FixedKey(self.key()), ssh_executable=script)
        with self.assertRaises(GitCommandError) as raised:
            await self.run_git(["status"], runner=runner, timeout_s=1)
        self.assertIs(raised.exception.failure, GitFailure.TIMEOUT)


@requires_git
class ClientIntegrationTest(SshTestCase):
    """``GitClient`` does not know or care which ``GitRunner`` it was given."""

    async def test_inspect_works_the_same_over_the_ssh_seam(self):
        path = f"{self.account.home}/repo"
        head = self.world.make_repository(path, origin="git@github.com:acme/tool.git")
        runner = SshGitRunner(
            _FixedKey(self.key()), ssh_executable=self.fake_ssh("wrap")
        )
        client = GitClient(runner, RepositoryPolicy())
        facts = await client.inspect(path, self.account)
        self.assertEqual(
            (facts.default_branch, facts.head, facts.origin_url),
            ("main", head, "git@github.com:acme/tool.git"),
        )


if __name__ == "__main__":
    unittest.main()
