"""The repository a GitHub gateway returns is validated in full (PAW-027).

``GitHubRepo`` is a plain value: a gateway (PAW-028) can build one from anything. A
host on the allowed list is not enough, because the owner and the repository name
become URLs that are stored as remotes and later parsed by the Tool Broker
(``normalise_remote``). A value such as ``a/../b`` matches the database's URL pattern
but not the Broker's, so the repository would be registered and then be unusable. The
service therefore applies the rules of ``parse_github_source`` to all three fields
(and to the URLs derived from them) **before** it writes ``origin`` or stores a
remote, and requires the repository to be the one that was asked for. Real PostgreSQL
and real git on local temporary repositories; skipped unless
``PAW_TEST_DATABASE_URL`` is set.
"""

from paw_backend.repositories import (
    GitHubRepo,
    GitHubUnavailableError,
    SubprocessGitRunner,
)
from paw_backend.repositories.validation import validate_remote_url

from .repositories_support import fs, requires_git, requires_postgres
from .test_repositories_service_register import RegistrationTestCase

SECRET = "hostile-" + "value-0123"


class FixedGateway:
    """A gateway that answers with exactly the given (possibly hostile) value."""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def create_repository(self, *, user_id, name, private):
        self.calls += 1
        return self.result


class RecordingRunner(SubprocessGitRunner):
    """Remembers which git sub-commands ran."""

    def __init__(self, **options):
        super().__init__(**options)
        self.commands: list[str] = []

    async def run(self, args, **run_options):
        self.commands.append(args[0])
        return await super().run(args, **run_options)


OWNERS = [
    "a/../b",
    "..",
    ".",
    "a/b",
    "/a",
    "a/",
    "a\nb",
    "a\x00b",
    "a\tb",
    "a b",
    "-a",
    "a-",
    "x" * 40,
    "",
    "аcme",  # a Cyrillic "а"
    "ａcme",  # a full-width "a"
    "acmé",
    "a@b",
    "a:b",
    "a\\b",
    "a%2e%2e",
    "a?x=1",
    "a#f",
    "acme.evil.org/x",
    SECRET + "/../x",
]
REPOS = [
    "shared/../x",
    "shared/x",
    "/shared",
    "shared/",
    "..",
    ".",
    "shared.git",
    "shared\n",
    "shared\x00",
    "shared ",
    "shаred",  # a Cyrillic "а"
    "ｓhared",  # a full-width "s"
    "shared%2e",
    "shared?x",
    "shared#f",
    "shared@x",
    "other",  # a well-formed name, but not the one that was asked for
    "",
    SECRET + "/../x",
]
HOSTS = [
    "GitHub.com",
    "github.com:443",
    "github.com/",
    "github.com@evil.example.org",
    " github.com",
    "github.com\n",
    "github.com.evil.example.org",
    "evil.example.org",
    "127.0.0.1",
    "",
]


@requires_postgres
@requires_git
class GatewayResultTest(RegistrationTestCase):
    def expected_path(self, name="shared"):
        return f"{self.home}/workspaces/alpha-project-{self.project_id.hex[:8]}/{name}"

    async def refused(self, result, *, name="shared"):
        runner = RecordingRunner(**self.world.runner_options())
        gateway = FixedGateway(result)
        service = self.new_service(github=gateway, runner=runner)
        with self.assertLogs("paw_backend.repositories.service", "ERROR") as logs:
            with self.assertRaises(GitHubUnavailableError) as raised:
                await service.create_github(self.alice, self.project_id, name)
        self.assertEqual(gateway.calls, 1)
        text = str(raised.exception) + "\n".join(logs.output)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("remote", runner.commands, "origin was never written")
        self.assertNothingRegistered()
        self.assertFalse(fs.lexists(self.expected_path(name)))

    async def test_hostile_owners_are_refused_before_anything_is_written(self):
        for owner in OWNERS:
            with self.subTest(owner=owner[:30]):
                await self.refused(GitHubRepo("github.com", owner, "shared"))

    async def test_hostile_repository_names_are_refused_before_anything_is_written(
        self,
    ):
        for repo in REPOS:
            with self.subTest(repo=repo[:30]):
                await self.refused(GitHubRepo("github.com", "alice-gh", repo))

    async def test_hostile_hosts_are_refused_before_anything_is_written(self):
        for host in HOSTS:
            with self.subTest(host=host):
                await self.refused(GitHubRepo(host, "alice-gh", "shared"))

    async def test_an_over_long_repository_name_is_refused(self):
        name = "n" * 100  # the longest name the module itself accepts
        await self.refused(GitHubRepo("github.com", "alice-gh", "n" * 101), name=name)

    async def test_values_that_are_not_text_are_refused(self):
        for result in (
            GitHubRepo(5, "alice-gh", "shared"),
            GitHubRepo("github.com", 5, "shared"),
            GitHubRepo("github.com", "alice-gh", None),
            GitHubRepo("github.com", b"alice-gh", "shared"),
            None,
            "https://github.com/alice-gh/shared",
            {"host": "github.com", "owner": "alice-gh", "repo": "shared"},
        ):
            with self.subTest(result=repr(result)[:40]):
                await self.refused(result)

    async def test_a_well_formed_repository_is_accepted_and_its_urls_are_valid(self):
        runner = RecordingRunner(**self.world.runner_options())
        result = GitHubRepo("github.com", "Alice-gh-2", "my_repo.v2")
        service = self.new_service(github=FixedGateway(result), runner=runner)

        registered = await service.create_github(
            self.alice, self.project_id, "my_repo.v2"
        )

        self.assertEqual(
            registered.remotes,
            (
                "https://github.com/Alice-gh-2/my_repo.v2",
                "https://github.com/Alice-gh-2/my_repo.v2.git",
            ),
        )
        for url in registered.remotes:
            self.assertEqual(validate_remote_url(url), url)
        self.assertIn("remote", runner.commands)

    async def test_the_repository_may_differ_in_case_from_the_name_asked_for(self):
        result = GitHubRepo("github.com", "alice-gh", "Shared")
        service = self.new_service(github=FixedGateway(result))

        registered = await service.create_github(self.alice, self.project_id, "shared")

        self.assertEqual(registered.repository.name, "shared")
        self.assertEqual(registered.remotes[0], "https://github.com/alice-gh/Shared")
