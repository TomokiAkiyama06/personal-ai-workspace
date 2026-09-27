"""Running git safely: environment, configuration, limits and hostile repositories.

The runner runs the real ``git`` on temporary repositories; the tests that prove
"git did not run this" plant a script that writes a marker file and assert that the
marker is absent while the *same setup* runs the script under plain git (so the test
cannot pass because the trap was never armed). No database and no network.
"""

import asyncio
import os
import shutil
import subprocess
import unittest
import uuid
from unittest import mock

from paw_backend.repositories import (
    GitClient,
    GitCommandError,
    GitFailure,
    LinuxAccount,
    PathProblem,
    PathRejectedError,
    RepositoryPolicy,
    SubprocessGitRunner,
)
from paw_backend.repositories import git as git_module

from .repositories_support import World, fs, git, requires_git


class GitTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.account = LinuxAccount(
            uuid.uuid4(), "alice", os.geteuid(), self.world.make_home("alice")
        )
        self.home = self.account.home

    def runner(self, **options):
        return SubprocessGitRunner(**options)

    async def run_git(self, args, *, cwd=None, timeout_s=30, runner=None, **options):
        return await (runner or self.runner()).run(
            args,
            account=self.account,
            cwd=cwd or self.home,
            timeout_s=timeout_s,
            **options,
        )

    def plain_git_alias(self):
        """``git pwn`` with only ``HOME`` set: the global configuration is honoured."""
        return subprocess.run(
            ["git", "pwn"],
            cwd=self.home,
            env={"HOME": self.home, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )

    def script(self, name: str) -> tuple[str, str]:
        """An executable that records that it ran: ``(path, marker path)``."""
        marker = f"{self.world.root}/{name}.marker"
        path = f"{self.world.root}/{name}.sh"
        fs.write(path, f"#!/bin/sh\necho ran > {marker}\nexit 0\n")
        os.chmod(path, 0o755)
        return path, marker


@requires_git
class EnvironmentTest(GitTestCase):
    def test_the_environment_is_a_fixed_allowlist(self):
        environment = git_module.git_environment(self.account)
        self.assertEqual(
            sorted(environment),
            [
                "GIT_ATTR_NOSYSTEM",
                "GIT_CONFIG_GLOBAL",
                "GIT_CONFIG_NOSYSTEM",
                "GIT_OPTIONAL_LOCKS",
                "GIT_TERMINAL_PROMPT",
                "HOME",
                "LANG",
                "LC_ALL",
                "PATH",
            ],
        )
        self.assertEqual(environment["HOME"], self.home)
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["PATH"], git_module.SAFE_PATH)
        self.assertEqual(
            git_module.git_environment(self.account, ceiling="/a")[
                "GIT_CEILING_DIRECTORIES"
            ],
            "/a",
        )

    async def test_nothing_of_the_backends_own_environment_reaches_git(self):
        planted = {
            "PAW_DATABASE_URL": "postgresql://user:pw@db.invalid/paw",
            "GH_TOKEN": "gh-" + "token-value",
            "GIT_DIR": "/nonexistent",
            "GIT_SSH_COMMAND": "touch /tmp/paw-should-not-exist",
            "GIT_ASKPASS": "/bin/false",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "GIT_EXEC_PATH": "/nonexistent",
            "XDG_CONFIG_HOME": "/nonexistent",
        }
        with mock.patch.dict(os.environ, planted):
            result = await self.run_git(
                ["dumpenv"],
                runner=self.runner(extra_config=[("alias.dumpenv", "!env")]),
            )

        names = {line.split("=", 1)[0] for line in result.stdout.splitlines()}
        for name in planted:
            if name != "GIT_EXEC_PATH":  # git sets its own
                self.assertNotIn(name, names)
        self.assertNotIn("pw@db.invalid", result.stdout)
        self.assertNotIn("token-value", result.stdout)
        self.assertIn("GIT_CONFIG_GLOBAL", names)
        self.assertIn(f"HOME={self.home}", result.stdout.splitlines())
        (path,) = [
            line for line in result.stdout.splitlines() if line.startswith("PATH=")
        ]
        # git puts its own directory in front; the rest is the fixed value.
        self.assertTrue(path.endswith(f":{git_module.SAFE_PATH}"), path)

    async def test_a_hostile_global_configuration_is_not_read(self):
        fs.write(self.home, ".gitconfig", "[alias]\n\tpwn = !echo pwned\n")

        result = await self.run_git(["pwn"])

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("pwned", result.stdout)
        # The trap is armed: git honours that file when the environment lets it.
        plain = self.plain_git_alias()
        self.assertEqual(plain.stdout.strip(), "pwned")


@requires_git
class ConfigurationTest(GitTestCase):
    def test_every_command_carries_the_overrides_in_front_of_the_subcommand(self):
        arguments = git_module.git_config_arguments(("https",), [("a.b", "c")])
        pairs = [arguments[i + 1] for i in range(0, len(arguments), 2)]
        self.assertEqual(arguments[0::2], ["-c"] * len(pairs))
        self.assertEqual(
            pairs,
            [
                "core.hooksPath=/dev/null",
                "core.fsmonitor=false",
                "submodule.recurse=false",
                "protocol.allow=never",
                "protocol.https.allow=always",
                "a.b=c",
            ],
        )

    def test_a_runner_needs_protocol_names(self):
        for protocols in ((), ("",), ("https;x",), ("HTTPS",), (5,)):
            with self.subTest(protocols=protocols):
                with self.assertRaises(ValueError):
                    SubprocessGitRunner(allowed_protocols=protocols)
        with self.assertRaises(ValueError):
            SubprocessGitRunner(max_output_bytes=0)

    async def test_a_hook_of_the_repository_is_not_run_by_our_git(self):
        repo = f"{self.home}/repo"
        self.world.make_repository(repo)
        hook, marker = self.script("hook")
        shutil.copy(hook, f"{repo}/.git/hooks/pre-commit")
        commit = [
            "-c",
            "user.name=A",
            "-c",
            "user.email=a@example.invalid",
            "commit",
            "--allow-empty",
            "--quiet",
            "-m",
            "x",
        ]

        result = await self.run_git(commit, cwd=repo)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(fs.exists(marker), "the hook ran")
        # The trap is armed: plain git runs the same hook.
        git(*commit, cwd=repo)
        self.assertTrue(fs.exists(marker), "the trap was never armed")

    async def test_a_file_system_monitor_of_the_repository_is_not_run(self):
        repo = f"{self.home}/repo"
        self.world.make_repository(repo)
        monitor, marker = self.script("monitor")
        git("config", "core.fsmonitor", monitor, cwd=repo)

        result = await self.run_git(["status", "--porcelain"], cwd=repo)

        self.assertEqual(result.returncode, 0)
        self.assertFalse(fs.exists(marker), "the monitor ran")
        git("status", "--porcelain", cwd=repo, check=False)
        self.assertTrue(fs.exists(marker), "the trap was never armed")

    async def test_only_the_allowed_transports_can_be_used(self):
        marker = f"{self.world.root}/ext.marker"
        bare = self.world.make_bare("acme", "tool")
        for url in (f"ext::sh -c 'touch {marker}'", f"file://{bare}", bare):
            with self.subTest(url=url[:25]):
                destination = f"{self.home}/clone-{abs(hash(url))}"
                result = await self.run_git(
                    ["clone", "--quiet", "--", url, destination]
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(fs.exists(destination))
        self.assertFalse(fs.exists(marker))
        # With file allowed the same command works: the block is the configuration.
        allowed = self.runner(allowed_protocols=("https", "file"))
        destination = f"{self.home}/allowed"
        result = await self.run_git(
            ["clone", "--quiet", "--", f"file://{bare}", destination], runner=allowed
        )
        self.assertEqual(result.returncode, 0)

    async def test_submodules_are_not_fetched_by_a_clone(self):
        bare = self.world.make_bare("acme", "lib")
        outer = self.world.make_bare("acme", "app")
        work = f"{self.world.root}/edit"
        git("clone", "--quiet", outer, work)
        git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "--quiet",
            bare,
            "lib",
            cwd=work,
        )
        git("commit", "--quiet", "-m", "sub", cwd=work)
        git("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=work)
        allowed = self.runner(allowed_protocols=("https", "file"))
        destination = f"{self.home}/app"

        result = await self.run_git(
            ["clone", "--quiet", "--", f"file://{outer}", destination], runner=allowed
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(os.listdir(f"{destination}/lib"), [])  # empty: not fetched


@requires_git
class LimitsTest(GitTestCase):
    async def test_no_shell_interprets_an_argument(self):
        marker = f"{self.world.root}/shell.marker"
        for argument in (
            f"x; touch {marker}",
            f"$(touch {marker})",
            f"`touch {marker}`",
            f"x && touch {marker}",
            f"x | touch {marker}",
        ):
            with self.subTest(argument=argument[:15]):
                result = await self.run_git(["rev-parse", "--verify", argument])
                self.assertNotEqual(result.returncode, 0)
        self.assertFalse(fs.exists(marker))

    async def test_a_command_that_runs_too_long_is_killed_with_its_children(self):
        pidfile = f"{self.world.root}/pid"
        runner = self.runner(
            extra_config=[("alias.slow", f"!echo $$ > {pidfile}; sleep 60")]
        )

        with self.assertLogs("paw_backend.repositories.git", "WARNING") as logs:
            with self.assertRaises(GitCommandError) as raised:
                await self.run_git(["slow"], timeout_s=4, runner=runner)

        self.assertIs(raised.exception.failure, GitFailure.TIMEOUT)
        self.assertEqual(str(raised.exception), "git slow failed: timeout")
        # Only the sub-command name and the reason were logged.
        self.assertEqual(
            logs.output,
            ["WARNING:paw_backend.repositories.git:git slow stopped (timeout)"],
        )
        pid = int(fs.read(pidfile))
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.1)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_a_command_that_writes_too_much_is_stopped(self):
        runner = self.runner(
            max_output_bytes=100,
            extra_config=[("alias.loud", "!yes abcdefghij | head -c 100000")],
        )
        with self.assertRaises(GitCommandError) as raised:
            await self.run_git(["loud"], runner=runner)
        self.assertIs(raised.exception.failure, GitFailure.OUTPUT_TOO_LARGE)

    async def test_stderr_is_bounded_too_and_never_returned(self):
        runner = self.runner(
            max_output_bytes=100,
            extra_config=[("alias.loud", "!yes abcdefghij | head -c 100000 1>&2")],
        )
        with self.assertRaises(GitCommandError) as raised:
            await self.run_git(["loud"], runner=runner)
        self.assertIs(raised.exception.failure, GitFailure.OUTPUT_TOO_LARGE)
        quiet = self.runner(extra_config=[("alias.err", "!echo secret-on-stderr 1>&2")])
        result = await self.run_git(["err"], runner=quiet)
        self.assertEqual(result.stdout, "")
        self.assertFalse(hasattr(result, "stderr"))

    async def test_output_that_is_not_utf8_is_refused(self):
        runner = self.runner(extra_config=[("alias.bad", r"!printf '\377\376'")])
        with self.assertRaises(GitCommandError) as raised:
            await self.run_git(["bad"], runner=runner)
        self.assertIs(raised.exception.failure, GitFailure.UNSAFE_OUTPUT)

    async def test_a_cancelled_command_kills_the_process(self):
        pidfile = f"{self.world.root}/pid"
        runner = self.runner(
            extra_config=[("alias.slow", f"!echo $$ > {pidfile}; sleep 60")]
        )
        task = asyncio.ensure_future(self.run_git(["slow"], runner=runner))
        for _ in range(400):
            if fs.exists(pidfile) and fs.read(pidfile).strip():
                break
            await asyncio.sleep(0.05)
        pid = int(fs.read(pidfile))

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.1)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_a_process_that_is_not_the_accounts_user_is_never_started(self):
        marker = f"{self.world.root}/started.marker"
        fake = f"{self.world.root}/fake-git"
        fs.write(fake, f"#!/bin/sh\necho started > {marker}\n")
        os.chmod(fake, 0o755)
        other = LinuxAccount(uuid.uuid4(), "bob", os.geteuid() + 1, self.home)

        with self.assertRaises(GitCommandError) as raised:
            await SubprocessGitRunner(git_executable=fake).run(
                ["status"], account=other, cwd=self.home, timeout_s=5
            )

        self.assertIs(raised.exception.failure, GitFailure.IDENTITY_MISMATCH)
        self.assertFalse(fs.exists(marker))

    async def test_a_missing_git_is_reported_as_such(self):
        with self.assertRaises(GitCommandError) as raised:
            await self.run_git(["status"], runner=self.runner(path="/nonexistent"))
        self.assertIs(raised.exception.failure, GitFailure.NOT_INSTALLED)
        with self.assertRaises(GitCommandError) as raised:
            await self.run_git(
                ["status"], runner=self.runner(git_executable="/nonexistent/git")
            )
        self.assertIs(raised.exception.failure, GitFailure.NOT_INSTALLED)


@requires_git
class InspectTest(GitTestCase):
    def client(self):
        return GitClient(SubprocessGitRunner(), RepositoryPolicy())

    def make(self, name="repo", **options):
        path = f"{self.home}/{name}"
        head = self.world.make_repository(path, **options)
        return path, head

    async def test_the_facts_of_an_ordinary_repository(self):
        path, head = self.make(origin="git@github.com:acme/tool.git")
        facts = await self.client().inspect(path, self.account)
        self.assertEqual(
            (facts.default_branch, facts.current_branch, facts.head, facts.origin_url),
            ("main", "main", head, "git@github.com:acme/tool.git"),
        )

    async def test_the_raw_origin_url_is_returned_without_rewriting(self):
        path, _ = self.make()
        git(
            "config",
            "url.https://mirror.example.org/.insteadOf",
            "https://github.com/",
            cwd=path,
        )
        git("remote", "add", "origin", "https://github.com/acme/tool.git", cwd=path)
        facts = await self.client().inspect(path, self.account)
        self.assertEqual(facts.origin_url, "https://github.com/acme/tool.git")

    async def test_a_worktree_setting_that_points_elsewhere_is_a_trick(self):
        path, _ = self.make()
        other = f"{self.home}/other"
        os.makedirs(other)
        git("config", "core.worktree", other, cwd=path)
        with self.assertRaises(PathRejectedError) as raised:
            await self.client().inspect(path, self.account)
        self.assertIs(raised.exception.problem, PathProblem.GIT_TRICK)

    async def test_core_bare_makes_it_a_bare_repository(self):
        path, _ = self.make()
        git("config", "core.bare", "true", cwd=path)
        with self.assertRaises(PathRejectedError) as raised:
            await self.client().inspect(path, self.account)
        self.assertIs(raised.exception.problem, PathProblem.BARE)

    async def test_a_directory_inside_a_repository_is_not_that_repository(self):
        path, _ = self.make()
        inner = f"{path}/sub"
        os.makedirs(inner)
        with self.assertRaises(PathRejectedError) as raised:
            await self.client().inspect(inner, self.account)
        self.assertIs(raised.exception.problem, PathProblem.NOT_A_REPOSITORY)

    async def test_a_repository_command_in_the_repository_does_not_run_its_config(self):
        path, _ = self.make()
        hook, marker = self.script("pager")
        for key in (
            "core.pager",
            "core.editor",
            "core.sshCommand",
            "diff.external",
            "credential.helper",
            "core.fsmonitor",
        ):
            git("config", key, hook, cwd=path)

        await self.client().inspect(path, self.account)

        self.assertFalse(fs.exists(marker))

    async def test_a_branch_that_is_not_a_plain_name_is_refused(self):
        path, _ = self.make(branch="feature/日本語")
        with self.assertRaises(PathRejectedError) as raised:
            await self.client().inspect(path, self.account)
        self.assertIs(raised.exception.problem, PathProblem.UNSUPPORTED_BRANCH)

    async def test_unusual_git_output_is_refused(self):
        path, _ = self.make()
        git("remote", "add", "origin", "https://example.org/x\ty", cwd=path)
        # A control character in the URL never becomes a stored value.
        with self.assertRaises(GitCommandError) as raised:
            await self.client().inspect(path, self.account)
        self.assertIs(raised.exception.failure, GitFailure.UNSAFE_OUTPUT)


@requires_git
class OperationsTest(GitTestCase):
    def client(self, **options):
        return GitClient(SubprocessGitRunner(**options), RepositoryPolicy())

    async def test_init_makes_an_empty_repository_without_templates(self):
        destination = f"{self.home}/new"
        os.makedirs(destination)

        await self.client().init(destination, self.account, initial_branch="trunk")

        self.assertEqual(os.listdir(destination), [".git"])
        self.assertEqual(
            git("symbolic-ref", "HEAD", cwd=destination), "refs/heads/trunk"
        )
        self.assertFalse(fs.exists(f"{destination}/.git/hooks"))

    async def test_a_branch_that_looks_like_an_option_is_only_its_value(self):
        # The service validates branches (no leading dash); this is the second line
        # of defence: ``--initial-branch=<value>`` is one argument, so ``--bare``
        # can never switch the repository to bare.
        destination = f"{self.home}/new"
        os.makedirs(destination)

        try:
            await self.client().init(destination, self.account, initial_branch="--bare")
        except GitCommandError:
            pass

        self.assertFalse(fs.exists(f"{destination}/HEAD"), "it became bare")
        self.assertFalse(fs.exists(f"{destination}/objects"), "it became bare")

    async def test_clone_takes_the_url_after_the_option_terminator(self):
        destination = f"{self.home}/c"
        os.makedirs(destination)
        with self.assertRaises(GitCommandError):
            await self.client().clone(
                "--upload-pack=touch /tmp/paw-never", destination, self.account
            )
        self.assertFalse(fs.exists("/tmp/paw-never"))

    async def test_add_origin_registers_the_url_as_given(self):
        repo = f"{self.home}/r"
        self.world.make_repository(repo)
        await self.client().add_origin(
            repo, "https://github.com/acme/tool.git", self.account
        )
        self.assertEqual(
            git("remote", "get-url", "origin", cwd=repo),
            "https://github.com/acme/tool.git",
        )


if __name__ == "__main__":
    unittest.main()
