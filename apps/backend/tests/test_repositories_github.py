"""Which GitHub repository a caller means, and what an origin URL may register.

Pure functions; no database, no network.
"""

import unittest
import uuid

from paw_backend.repositories import (
    GitHubRepo,
    GitHubUnavailableError,
    InputProblem,
    InvalidRepositoryInputError,
    RemoteError,
    RemoteProblem,
    UnavailableGitHubGateway,
    parse_github_source,
)
from paw_backend.repositories.github import (
    check_created_repository,
    remote_urls_from_origin,
)

HOSTS = ("github.com",)


class ParseGitHubSourceTest(unittest.TestCase):
    def test_owner_repo_and_full_urls_name_the_same_repository(self):
        expected = GitHubRepo("github.com", "acme", "tool")
        for source in (
            "acme/tool",
            "acme/tool.git",
            "https://github.com/acme/tool",
            "https://github.com/acme/tool.git",
            "https://github.com/acme/tool/",
            "https://GitHub.COM/acme/tool",
            "HTTPS://github.com/acme/tool",
        ):
            with self.subTest(source=source):
                self.assertEqual(parse_github_source(source, HOSTS), expected)

    def test_the_two_spellings_and_the_clone_url_follow_from_the_repository(self):
        ref = parse_github_source("acme/tool", HOSTS)
        self.assertEqual(ref.clone_url, "https://github.com/acme/tool.git")
        self.assertEqual(
            ref.remote_urls,
            ("https://github.com/acme/tool", "https://github.com/acme/tool.git"),
        )

    def test_a_short_name_means_the_first_allowed_host(self):
        ref = parse_github_source("acme/tool", ("ghe.example.org", "github.com"))
        self.assertEqual(ref.host, "ghe.example.org")

    def test_only_an_allowed_host_is_accepted_exactly(self):
        for source in (
            "https://evil.example.org/acme/tool",
            "https://github.com.evil.org/acme/tool",
            "https://gist.github.com/acme/tool",
            "https://www.github.com/acme/tool",
            "https://githubxcom/acme/tool",
        ):
            with self.subTest(source=source):
                with self.assertRaises(InvalidRepositoryInputError) as raised:
                    parse_github_source(source, HOSTS)
                self.assertEqual(
                    raised.exception.problem, InputProblem.HOST_NOT_ALLOWED
                )

    def test_no_allowed_host_means_nothing_is_accepted(self):
        with self.assertRaises(InvalidRepositoryInputError) as raised:
            parse_github_source("acme/tool", ())
        self.assertEqual(raised.exception.problem, InputProblem.HOST_NOT_ALLOWED)

    def test_everything_that_is_not_a_plain_repository_is_refused(self):
        cases = [
            None,
            5,
            b"acme/tool",
            "",
            "acme",
            "acme/",
            "/tool",
            "acme/tool/extra",
            "acme//tool",
            "github.com/acme/tool",
            "http://github.com/acme/tool",
            "ssh://git@github.com/acme/tool",
            "git@github.com:acme/tool.git",
            "file:///acme/tool",
            "https://user@github.com/acme/tool",
            "https://user:pw@github.com/acme/tool",
            "https://github.com:443/acme/tool",
            "https://github.com/acme/tool?x=1",
            "https://github.com/acme/tool#x",
            "https://github.com/acme/tool/tree/main",
            "https://github.com/acme",
            "https://github.com//tool",
            "https://github.com/acme/..",
            "https://github.com/acme/.",
            "acme/..",
            "-acme/tool",
            "acme-/tool",
            "ac me/tool",
            "acme/to ol",
            "acme/tool.git.git",
            "acme/日本",
            "acme/tool\n",
            "acme\\tool",
            "a" * 40 + "/tool",
            "acme/" + "r" * 101,
            "x" * 600,
        ]
        for source in cases:
            with self.subTest(source=repr(source)[:40]):
                with self.assertRaises(InvalidRepositoryInputError) as raised:
                    parse_github_source(source, HOSTS)
                self.assertEqual(raised.exception.field, "source")
                # Nothing of the value is echoed.
                self.assertNotIn(
                    str(source)[:8] or "\0", str(raised.exception).split(":")[0]
                )


class RemoteUrlsFromOriginTest(unittest.TestCase):
    def test_nothing_or_a_transport_the_broker_cannot_map_registers_nothing(self):
        for origin in (
            None,
            "/srv/git/tool.git",
            "../tool",
            "file:///srv/git/tool.git",
            "http://github.com/acme/tool",
            "git://github.com/acme/tool.git",
            "ftp://example.org/tool",
            "git@gitlab.example.org:team/tool.git",
            "ssh://git@gitlab.example.org/team/tool.git",
            "git@github.com:acme",
            "git@github.com:acme/tool/extra",
            "ssh://git@github.com:22/acme/tool",
        ):
            with self.subTest(origin=origin):
                self.assertEqual(remote_urls_from_origin(origin, HOSTS), ())

    def test_a_github_style_origin_becomes_both_https_spellings(self):
        both = ("https://github.com/acme/tool", "https://github.com/acme/tool.git")
        for origin in (
            "git@github.com:acme/tool.git",
            "git@github.com:acme/tool",
            "ssh://git@github.com/acme/tool.git",
            "ssh://git@github.com/acme/tool/",
            "https://github.com/acme/tool.git",
            "https://github.com/acme/tool",
            "HTTPS://GitHub.com/acme/tool",
        ):
            with self.subTest(origin=origin):
                self.assertEqual(remote_urls_from_origin(origin, HOSTS), both)

    def test_another_https_host_registers_its_canonical_url(self):
        self.assertEqual(
            remote_urls_from_origin("https://git.example.org/team/sub/tool.git", HOSTS),
            ("https://git.example.org/team/sub/tool.git",),
        )

    def test_credentials_in_an_https_url_are_refused_and_not_echoed(self):
        secret = "pa55" + "word"
        for origin in (
            f"https://user:{secret}@github.com/acme/tool.git",
            f"https://{secret}@github.com/acme/tool",
            f"http://user:{secret}@github.com/acme/tool",
            f"HTTPS://user:{secret}@example.org/x/y",
            f"https://user:{secret}@evil.example.org/x",
        ):
            with self.subTest(origin=origin.split(secret)[0]):
                with self.assertRaises(RemoteError) as raised:
                    remote_urls_from_origin(origin, HOSTS)
                self.assertIs(raised.exception.problem, RemoteProblem.HAS_CREDENTIALS)
                self.assertNotIn(secret, str(raised.exception))
                self.assertNotIn(secret, repr(raised.exception.args))

    def test_a_url_the_database_would_refuse_is_not_registered(self):
        self.assertEqual(
            remote_urls_from_origin("https://git.example.org/" + "a" * 1100, HOSTS), ()
        )
        self.assertEqual(
            remote_urls_from_origin("https://git.example.org/a@b/c", HOSTS), ()
        )


class CheckCreatedRepositoryTest(unittest.TestCase):
    def check(self, host, owner, repo, name="shared", hosts=HOSTS):
        return check_created_repository(GitHubRepo(host, owner, repo), name, hosts)

    def test_a_well_formed_repository_is_returned_as_it_is(self):
        result = self.check("github.com", "alice-gh", "shared")
        self.assertEqual(result, GitHubRepo("github.com", "alice-gh", "shared"))
        self.assertEqual(
            self.check("github.com", "A1", "My_Repo.v2", "my_repo.V2").repo,
            "My_Repo.v2",
        )

    def test_every_field_is_checked_with_the_rules_of_a_callers_source(self):
        cases = [
            ("github.com", "a/../b", "shared"),
            ("github.com", "..", "shared"),
            ("github.com", "a b", "shared"),
            ("github.com", "a\nb", "shared"),
            ("github.com", "-a", "shared"),
            ("github.com", "x" * 40, "shared"),
            ("github.com", "аcme", "shared"),
            ("github.com", "alice", "shared/../x"),
            ("github.com", "alice", "shared.git"),
            ("github.com", "alice", "shared\x00"),
            ("github.com", "alice", "shаred"),
            ("GitHub.com", "alice", "shared"),
            ("github.com:443", "alice", "shared"),
            ("evil.example.org", "alice", "shared"),
            ("", "alice", "shared"),
            ("github.com", "", "shared"),
            ("github.com", "alice", ""),
        ]
        for host, owner, repo in cases:
            with self.subTest(host=host, owner=owner[:12], repo=repo[:12]):
                with self.assertRaises(InvalidRepositoryInputError) as raised:
                    self.check(host, owner, repo)
                self.assertEqual(raised.exception.field, "repository")

    def test_a_name_the_broker_could_not_read_back_is_refused_even_when_it_matches(
        self,
    ):
        # ``...`` passes the source parser (only ``.`` and ``..`` are refused there)
        # but is a URL segment that climbs out of its parent in ``normalise_remote``:
        # the derived URLs are checked with the Broker's own normaliser.
        for repo in ("...", "....", "...git"):
            with self.subTest(repo=repo):
                with self.assertRaises(InvalidRepositoryInputError):
                    self.check("github.com", "alice", repo, name=repo)

    def test_a_str_subclass_that_lies_about_its_content_registers_nothing_of_it(self):
        class Sneaky(str):
            def __format__(self, spec):
                return "alice"  # what the URL is built from ...

        for owner in (Sneaky("a/../b"), Sneaky("alice")):
            with self.subTest(owner=str(owner)):
                try:
                    result = self.check("github.com", owner, "shared")
                except InvalidRepositoryInputError:
                    continue
                # ... and when it passes, the result is clean text of that URL.
                self.assertIs(type(result.owner), str)
                self.assertEqual(result.owner, "alice")

    def test_it_must_be_the_repository_that_was_asked_for(self):
        with self.assertRaises(InvalidRepositoryInputError):
            self.check("github.com", "alice", "other")
        with self.assertRaises(InvalidRepositoryInputError):
            self.check("github.com", "alice", "n" * 101, "n" * 100)

    def test_values_that_are_not_text_or_not_a_repository_are_refused(self):
        for value in (
            None,
            "https://github.com/alice/shared",
            ("github.com", "alice", "shared"),
            GitHubRepo(5, "alice", "shared"),
            GitHubRepo("github.com", None, "shared"),
            GitHubRepo("github.com", "alice", b"shared"),
        ):
            with self.subTest(value=repr(value)[:40]):
                with self.assertRaises(InvalidRepositoryInputError):
                    check_created_repository(value, "shared", HOSTS)

    def test_no_allowed_host_means_nothing_is_accepted(self):
        with self.assertRaises(InvalidRepositoryInputError):
            self.check("github.com", "alice", "shared", hosts=())


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_default_gateway_refuses_and_says_nothing_else(self):
        with self.assertRaises(GitHubUnavailableError) as raised:
            await UnavailableGitHubGateway().create_repository(
                user_id=uuid.uuid4(), name="tool", private=True
            )
        self.assertEqual(str(raised.exception), "GitHub is not available")


if __name__ == "__main__":
    unittest.main()
