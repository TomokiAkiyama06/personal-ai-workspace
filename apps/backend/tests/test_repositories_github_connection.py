"""GitHub user connection: ``gh auth status``, and the ``GitHubGateway`` seam
this issue (PAW-028) fills.

No PostgreSQL: authorization is exercised with a real ``Authorizer`` on an
``InMemoryAuditSink`` (as ``test_repositories_service_validation.py`` does), and the
Linux account directory is the test world's ``FakeAccounts``. ``SubprocessGhRunnerTest``
runs the real ``gh`` executable name against small planted scripts (exactly the
``test_repositories_git.py`` pattern: no real network call, no real GitHub account).
"""

import asyncio
import json
import os
import unittest
import uuid
from unittest import mock

from paw_backend.authz import Authorizer, InMemoryAuditSink, Principal, SystemRole
from paw_backend.authz.policy import Reason
from paw_backend.db import Database
from paw_backend.repositories import (
    GhCliGitHubGateway,
    GhCommandError,
    GhFailure,
    GhResult,
    GitHubConnectionService,
    GitHubConnectionState,
    GitHubConnectionStatus,
    GitHubRepo,
    InputProblem,
    InvalidRepositoryInputError,
    LinuxAccount,
    LinuxAccountUnavailableError,
    RepositoryPermissionDeniedError,
    RepositoryPolicy,
    RepositoryService,
    SubprocessGhRunner,
    UnavailableGitHubGateway,
)
from paw_backend.repositories import github_connection as gh_module

from .repositories_support import FakeAccounts, World, fs
from .support import make_settings

HOSTS = ("github.com",)


class GitHubConnectionStatusTest(unittest.TestCase):
    """The value object never holds a token; ``login`` and ``state`` agree."""

    def test_connected_needs_a_login_and_not_connected_must_not_have_one(self):
        GitHubConnectionStatus("github.com", GitHubConnectionState.CONNECTED, "octo")
        GitHubConnectionStatus("github.com", GitHubConnectionState.NOT_CONNECTED)
        for bad in (
            (GitHubConnectionState.CONNECTED, None),
            (GitHubConnectionState.NOT_CONNECTED, "octo"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    GitHubConnectionStatus("github.com", *bad)

    def test_a_login_must_look_like_a_github_user_name(self):
        for login in ("-octo", "octo/evil", "a b", "", "x" * 40, "octo\n"):
            with self.subTest(login=login):
                with self.assertRaises(ValueError):
                    GitHubConnectionStatus(
                        "github.com", GitHubConnectionState.CONNECTED, login
                    )

    def test_hostname_and_state_are_required(self):
        with self.assertRaises(ValueError):
            GitHubConnectionStatus("", GitHubConnectionState.NOT_CONNECTED)
        with self.assertRaises(ValueError):
            GitHubConnectionStatus("github.com", "connected")  # a plain string


class GhEnvironmentTest(unittest.TestCase):
    def test_the_environment_is_a_fixed_allowlist_naming_the_host(self):
        account = LinuxAccount(uuid.uuid4(), "alice", os.geteuid(), "/home/alice")
        environment = gh_module.gh_environment(account, hostname="github.com")
        self.assertEqual(
            sorted(environment),
            [
                "GH_CONFIG_DIR",
                "GH_HOST",
                "GH_NO_UPDATE_NOTIFIER",
                "GH_PROMPT_DISABLED",
                "HOME",
                "LANG",
                "LC_ALL",
                "NO_COLOR",
                "PATH",
            ],
        )
        self.assertEqual(environment["HOME"], "/home/alice")
        self.assertEqual(environment["GH_CONFIG_DIR"], "/home/alice/.config/gh")
        self.assertEqual(environment["GH_HOST"], "github.com")


class GhRunnerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.account = LinuxAccount(
            uuid.uuid4(), "alice", os.geteuid(), self.world.make_home("alice")
        )

    def script(self, name: str, body: str) -> str:
        path = f"{self.world.root}/{name}.sh"
        fs.write(path, f"#!/bin/sh\n{body}\n")
        os.chmod(path, 0o755)
        return path

    async def run_gh(
        self, args, *, account=None, hostname="github.com", timeout_s=5, runner=None
    ):
        runner = runner or SubprocessGhRunner()
        return await runner.run(
            args,
            account=account or self.account,
            hostname=hostname,
            timeout_s=timeout_s,
        )


class SubprocessGhRunnerTest(GhRunnerTestCase):
    async def test_a_successful_command_returns_its_output(self):
        fake = self.script("gh", 'echo "hello $1"')
        result = await self.run_gh(
            ["world"], runner=SubprocessGhRunner(gh_executable=fake)
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "hello world\n")

    async def test_nothing_is_inherited_from_the_backends_own_environment(self):
        marker_path = f"{self.world.root}/env.txt"
        fake = self.script("gh", f"env | sort > {marker_path}")
        with mock.patch.dict(
            os.environ, {"PAW_TEST_HOSTILE_SECRET": "leak-me"}, clear=False
        ):
            await self.run_gh(["x"], runner=SubprocessGhRunner(gh_executable=fake))
        recorded = fs.read(marker_path)
        self.assertNotIn("PAW_TEST_HOSTILE_SECRET", recorded)
        self.assertNotIn("leak-me", recorded)
        expected = gh_module.gh_environment(self.account, hostname="github.com")
        seen = dict(line.split("=", 1) for line in recorded.splitlines() if "=" in line)
        seen.pop("PWD", None)  # ``sh``'s own doing when a child starts with a cwd
        self.assertEqual(seen, expected)

    async def test_a_process_that_is_not_the_accounts_user_is_never_started(self):
        marker = f"{self.world.root}/started.marker"
        fake = self.script("gh", f"echo started > {marker}")
        other = LinuxAccount(uuid.uuid4(), "bob", os.geteuid() + 1, self.account.home)
        with self.assertRaises(GhCommandError) as raised:
            await self.run_gh(
                ["auth", "status"],
                account=other,
                runner=SubprocessGhRunner(gh_executable=fake),
            )
        self.assertIs(raised.exception.failure, GhFailure.IDENTITY_MISMATCH)
        self.assertFalse(fs.exists(marker))

    async def test_a_missing_gh_is_reported_as_such(self):
        with self.assertRaises(GhCommandError) as raised:
            await self.run_gh(
                ["auth", "status"], runner=SubprocessGhRunner(path="/nonexistent")
            )
        self.assertIs(raised.exception.failure, GhFailure.NOT_INSTALLED)
        with self.assertRaises(GhCommandError) as raised:
            await self.run_gh(
                ["auth", "status"],
                runner=SubprocessGhRunner(gh_executable="/nonexistent/gh"),
            )
        self.assertIs(raised.exception.failure, GhFailure.NOT_INSTALLED)

    async def test_a_command_that_runs_too_long_is_killed(self):
        pidfile = f"{self.world.root}/pid"
        fake = self.script("gh", f"echo $$ > {pidfile}; sleep 60")
        with self.assertRaises(GhCommandError) as raised:
            await self.run_gh(
                ["x"], timeout_s=2, runner=SubprocessGhRunner(gh_executable=fake)
            )
        self.assertIs(raised.exception.failure, GhFailure.TIMEOUT)
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
        fake = self.script("gh", "yes abcdefghij | head -c 100000")
        with self.assertRaises(GhCommandError) as raised:
            await self.run_gh(
                ["x"],
                runner=SubprocessGhRunner(gh_executable=fake, max_output_bytes=100),
            )
        self.assertIs(raised.exception.failure, GhFailure.OUTPUT_TOO_LARGE)

    async def test_output_that_is_not_utf8_is_refused(self):
        fake = self.script("gh", r"printf '\377\376'")
        with self.assertRaises(GhCommandError) as raised:
            await self.run_gh(["x"], runner=SubprocessGhRunner(gh_executable=fake))
        self.assertIs(raised.exception.failure, GhFailure.UNSAFE_OUTPUT)


class RecordingRunner:
    """An in-Python ``GhRunner`` fake: records every call, returns a canned result."""

    def __init__(self, result: GhResult | Exception | None = None) -> None:
        self.result = GhResult(0, "") if result is None else result
        self.calls: list[dict] = []

    async def run(self, args, *, account, hostname, timeout_s):
        self.calls.append(
            {
                "args": list(args),
                "account": account,
                "hostname": hostname,
                "timeout_s": timeout_s,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _status_json(*, logged_in: bool, login: str = "octocat", state: str = "success"):
    if not logged_in:
        return json.dumps({"hosts": {}})
    entry = {"state": state, "active": True, "host": "github.com", "login": login}
    return json.dumps({"hosts": {"github.com": [entry]}})


class GitHubConnectionServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.accounts = FakeAccounts(self.world)
        self.user_id = uuid.uuid4()
        self.accounts.add(self.user_id, "alice")
        self.user = Principal(self.user_id, SystemRole.USER)
        self.admin = Principal(uuid.uuid4(), SystemRole.ADMIN)
        self.other_user = Principal(uuid.uuid4(), SystemRole.USER)

    def service(self, runner) -> GitHubConnectionService:
        return GitHubConnectionService(
            Authorizer(InMemoryAuditSink()), self.accounts, runner, hosts=HOSTS
        )

    async def test_a_connected_account_is_reported_with_its_login(self):
        runner = RecordingRunner(
            GhResult(0, _status_json(logged_in=True, login="octo"))
        )
        status = await self.service(runner).status(self.user, self.user_id)
        self.assertEqual(
            status,
            GitHubConnectionStatus(
                "github.com", GitHubConnectionState.CONNECTED, "octo"
            ),
        )

    async def test_an_account_that_never_logged_in_is_not_connected(self):
        runner = RecordingRunner(GhResult(0, _status_json(logged_in=False)))
        status = await self.service(runner).status(self.user, self.user_id)
        self.assertEqual(
            status,
            GitHubConnectionStatus("github.com", GitHubConnectionState.NOT_CONNECTED),
        )

    async def test_an_unhealthy_login_is_reported_as_not_connected_not_as_an_error(
        self,
    ):
        runner = RecordingRunner(
            GhResult(0, _status_json(logged_in=True, state="the token is invalid"))
        )
        status = await self.service(runner).status(self.user, self.user_id)
        self.assertEqual(status.state, GitHubConnectionState.NOT_CONNECTED)

    async def test_the_token_flag_is_never_sent(self):
        runner = RecordingRunner(GhResult(0, _status_json(logged_in=True)))
        await self.service(runner).status(self.user, self.user_id)
        (call,) = runner.calls
        self.assertNotIn("--show-token", call["args"])
        self.assertNotIn("-t", call["args"])
        self.assertEqual(call["hostname"], "github.com")

    async def test_malformed_json_is_a_typed_error_not_a_guess(self):
        for bad_output in (
            "not json",
            "[]",
            json.dumps({"hosts": "nope"}),
            json.dumps({}),
        ):
            with self.subTest(bad_output=bad_output):
                runner = RecordingRunner(GhResult(0, bad_output))
                with self.assertRaises(GhCommandError) as raised:
                    await self.service(runner).status(self.user, self.user_id)
                self.assertIs(raised.exception.failure, GhFailure.INVALID_RESPONSE)

    async def test_a_fatal_gh_error_is_never_reported_as_not_connected(self):
        runner = RecordingRunner(GhResult(1, "unexpected error\n"))
        with self.assertRaises(GhCommandError) as raised:
            await self.service(runner).status(self.user, self.user_id)
        self.assertIs(raised.exception.failure, GhFailure.NONZERO_EXIT)

    async def test_an_identity_mismatch_propagates_as_is(self):
        runner = RecordingRunner(
            GhCommandError("auth status", GhFailure.IDENTITY_MISMATCH)
        )
        with self.assertRaises(GhCommandError) as raised:
            await self.service(runner).status(self.user, self.user_id)
        self.assertIs(raised.exception.failure, GhFailure.IDENTITY_MISMATCH)

    async def test_viewing_ones_own_status_needs_only_github_use(self):
        runner = RecordingRunner(GhResult(0, _status_json(logged_in=True)))
        await self.service(runner).status(self.user, self.user_id)  # does not raise

    async def test_viewing_anothers_status_needs_admin_usage_view(self):
        runner = RecordingRunner(GhResult(0, _status_json(logged_in=True)))
        with self.assertRaises(RepositoryPermissionDeniedError) as raised:
            await self.service(runner).status(self.other_user, self.user_id)
        self.assertIs(raised.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
        self.assertEqual(runner.calls, [])  # denied before gh is ever run

        self.accounts.add(self.user_id, "alice")  # re-add: same account, admin path
        status = await self.service(runner).status(self.admin, self.user_id)
        self.assertEqual(status.state, GitHubConnectionState.CONNECTED)

    async def test_an_unauthenticated_actor_is_refused(self):
        runner = RecordingRunner(GhResult(0, _status_json(logged_in=True)))
        with self.assertRaises(RepositoryPermissionDeniedError) as raised:
            await self.service(runner).status(object(), self.user_id)
        self.assertIs(raised.exception.reason, Reason.UNAUTHENTICATED)
        self.assertEqual(runner.calls, [])

    async def test_an_unconfigured_hostname_is_rejected_before_gh_runs(self):
        runner = RecordingRunner(GhResult(0, _status_json(logged_in=True)))
        with self.assertRaises(InvalidRepositoryInputError) as raised:
            await self.service(runner).status(
                self.user, self.user_id, hostname="evil.example.org"
            )
        self.assertEqual(raised.exception.field, "hostname")
        self.assertIs(raised.exception.problem, InputProblem.HOST_NOT_ALLOWED)
        self.assertEqual(runner.calls, [])

    async def test_a_bad_user_id_is_rejected_before_anything_else(self):
        runner = RecordingRunner(GhResult(0, ""))
        with self.assertRaises(InvalidRepositoryInputError):
            await self.service(runner).status(self.user, "not-a-uuid")
        self.assertEqual(runner.calls, [])

    async def test_a_user_with_no_linux_account_is_reported_as_such(self):
        runner = RecordingRunner(GhResult(0, ""))
        with self.assertRaises(LinuxAccountUnavailableError):
            # An Admin, not ``self.user``: viewing someone else must pass
            # authorization first and reach the account lookup.
            await self.service(runner).status(self.admin, uuid.uuid4())

    def test_construction_rejects_the_wrong_shapes(self):
        authorizer = Authorizer(InMemoryAuditSink())
        with self.assertRaises(TypeError):
            GitHubConnectionService(object(), self.accounts, RecordingRunner())
        with self.assertRaises(TypeError):
            GitHubConnectionService(authorizer, object(), RecordingRunner())
        with self.assertRaises(TypeError):
            GitHubConnectionService(authorizer, self.accounts, object())
        with self.assertRaises(ValueError):
            GitHubConnectionService(
                authorizer, self.accounts, RecordingRunner(), hosts=()
            )


class GhCliGitHubGatewayTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.accounts = FakeAccounts(self.world)
        self.user_id = uuid.uuid4()
        self.accounts.add(self.user_id, "alice")

    def gateway(self, runner) -> GhCliGitHubGateway:
        return GhCliGitHubGateway(self.accounts, runner, HOSTS)

    async def test_a_created_repository_is_parsed_from_ghs_own_output(self):
        runner = RecordingRunner(GhResult(0, "https://github.com/alice/tool\n"))
        repo = await self.gateway(runner).create_repository(
            user_id=self.user_id, name="tool", private=True
        )
        self.assertEqual(repo, GitHubRepo("github.com", "alice", "tool"))
        (call,) = runner.calls
        self.assertEqual(call["args"], ["repo", "create", "tool", "--private"])
        self.assertEqual(call["hostname"], "github.com")

    async def test_public_is_asked_for_when_private_is_false(self):
        runner = RecordingRunner(GhResult(0, "https://github.com/alice/tool\n"))
        await self.gateway(runner).create_repository(
            user_id=self.user_id, name="tool", private=False
        )
        (call,) = runner.calls
        self.assertIn("--public", call["args"])
        self.assertNotIn("--private", call["args"])

    async def test_a_nonzero_exit_is_a_typed_error_not_a_parse_attempt(self):
        runner = RecordingRunner(GhResult(1, "not logged in\n"))
        with self.assertRaises(GhCommandError) as raised:
            await self.gateway(runner).create_repository(
                user_id=self.user_id, name="tool", private=True
            )
        self.assertIs(raised.exception.failure, GhFailure.NONZERO_EXIT)

    async def test_output_gh_could_not_have_produced_is_refused(self):
        runner = RecordingRunner(GhResult(0, "https://evil.example.org/alice/tool\n"))
        with self.assertRaises(InvalidRepositoryInputError):
            await self.gateway(runner).create_repository(
                user_id=self.user_id, name="tool", private=True
            )

    async def test_a_runner_failure_propagates_unwrapped(self):
        runner = RecordingRunner(GhCommandError("repo create", GhFailure.TIMEOUT))
        with self.assertRaises(GhCommandError) as raised:
            await self.gateway(runner).create_repository(
                user_id=self.user_id, name="tool", private=True
            )
        self.assertIs(raised.exception.failure, GhFailure.TIMEOUT)

    async def test_an_unknown_user_is_reported_as_such(self):
        runner = RecordingRunner(GhResult(0, ""))
        with self.assertRaises(LinuxAccountUnavailableError):
            await self.gateway(runner).create_repository(
                user_id=uuid.uuid4(), name="tool", private=True
            )

    def test_construction_rejects_the_wrong_shapes(self):
        with self.assertRaises(TypeError):
            GhCliGitHubGateway(object(), RecordingRunner(), HOSTS)
        with self.assertRaises(TypeError):
            GhCliGitHubGateway(self.accounts, object(), HOSTS)
        with self.assertRaises(ValueError):
            GhCliGitHubGateway(self.accounts, RecordingRunner(), ())


class _NullGitRunner:
    """A ``GitRunner`` that must never actually run (these tests never clone)."""

    async def run(self, *args, **kwargs):
        raise AssertionError("git must not be used")


class FromPolicyWiringTest(unittest.TestCase):
    """``RepositoryService.from_policy(..., gh_runner=...)`` closes the seam."""

    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.accounts = FakeAccounts(self.world)

    def build(self, **kwargs) -> RepositoryService:
        return RepositoryService.from_policy(
            Database(make_settings()),
            Authorizer(InMemoryAuditSink()),
            _NullGitRunner(),
            RepositoryPolicy(),
            accounts=self.accounts,
            **kwargs,
        )

    def test_a_gh_runner_wires_ghcligithubgateway(self):
        service = self.build(gh_runner=RecordingRunner())
        self.assertIsInstance(service._github, GhCliGitHubGateway)

    def test_a_gh_runner_also_wires_the_clone_credential_helper(self):
        # PAW-028's other half of the seam (Codex P1): a private clone must be
        # able to use the same actor's gh auth login, not just status checks.
        service = self.build(gh_runner=RecordingRunner())
        self.assertTrue(service._git._credential_helper)

    def test_neither_given_keeps_the_gateway_unavailable(self):
        service = self.build()
        self.assertIsInstance(service._github, UnavailableGitHubGateway)
        self.assertFalse(service._git._credential_helper)

    def test_github_and_gh_runner_together_is_rejected(self):
        with self.assertRaises(TypeError):
            self.build(gh_runner=RecordingRunner(), github=UnavailableGitHubGateway())


class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    """A real ``gh``-shaped script through the real subprocess runner."""

    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.accounts = FakeAccounts(self.world)
        self.user_id = uuid.uuid4()
        self.accounts.add(self.user_id, "alice")

    async def test_status_through_the_real_runner_and_a_gh_shaped_script(self):
        script = f"{self.world.root}/gh.sh"
        fs.write(
            script,
            "#!/bin/sh\n"
            'if [ "$1" = "auth" ]; then\n'
            f"  echo '{_status_json(logged_in=True, login='octocat')}'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
        )
        os.chmod(script, 0o755)
        service = GitHubConnectionService(
            Authorizer(InMemoryAuditSink()),
            self.accounts,
            SubprocessGhRunner(gh_executable=script),
            hosts=HOSTS,
        )
        status = await service.status(
            Principal(self.user_id, SystemRole.USER), self.user_id
        )
        self.assertEqual(
            status,
            GitHubConnectionStatus(
                "github.com", GitHubConnectionState.CONNECTED, "octocat"
            ),
        )


if __name__ == "__main__":
    unittest.main()
