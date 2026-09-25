"""The repository policy and its ``PAW_REPOSITORY_*`` settings (PAW-027).

No database and no file system.
"""

import unittest

from paw_backend.config import Settings
from paw_backend.repositories import RepositoryPolicy, limits

from .support import make_settings, paw_environment


class RootTemplateTest(unittest.TestCase):
    def test_a_root_belongs_to_one_user(self):
        for template in (
            "{home}",
            "{home}/src",
            "{home}/a/b",
            "/srv/repos/{user}",
            "/srv/{user}/repos",
            "/data/{user}",
        ):
            with self.subTest(template=template):
                self.assertEqual(
                    RepositoryPolicy(existing_roots=(template,)).existing_roots,
                    (template,),
                )

    def test_a_root_that_is_shared_or_unsafe_is_refused(self):
        for template in (
            "",
            "/srv/repos",  # one directory for everybody
            "/home",
            "/",
            "{home}/../other",
            "{home}/./src",
            "{home}//src",
            "{home}/src/",
            "src/{user}",
            "relative/{home}",
            "/x/{home}",  # {home} is an absolute path itself
            "{home}{home}",
            "{home}/{user}/{home}",
            "{HOME}/src",
            "{user",
            "user}",
            "{}/x",
            "{0}/x",
            "{home!r}",
            "{home.__class__}",
            "/srv/{user}/{unknown}",
            "/srv/\\{user}",
            "/srv/{user}\n",
            5,
            None,
        ):
            with self.subTest(template=repr(template)):
                with self.assertRaises(ValueError):
                    RepositoryPolicy(existing_roots=(template,))

    def test_there_must_be_a_bounded_number_of_roots(self):
        for roots in (
            (),
            tuple(f"/r{i}/{{user}}" for i in range(limits.MAX_ROOT_TEMPLATES + 1)),
            "{home}",
            None,
            5,
        ):
            with self.subTest(roots=repr(roots)[:30]):
                with self.assertRaises(ValueError):
                    RepositoryPolicy(existing_roots=roots)

    def test_duplicates_collapse_and_order_is_kept(self):
        policy = RepositoryPolicy(existing_roots=("{home}/b", "{home}/a", "{home}/b"))
        self.assertEqual(policy.existing_roots, ("{home}/b", "{home}/a"))


class OtherFieldsTest(unittest.TestCase):
    def test_the_defaults_are_the_proposed_ones(self):
        policy = RepositoryPolicy()
        self.assertEqual(
            (
                policy.workspace_subdir,
                policy.existing_roots,
                policy.clone_hosts,
                policy.min_uid,
                policy.git_timeout_s,
                policy.clone_timeout_s,
            ),
            ("workspaces", ("{home}",), ("github.com",), 1000, 30.0, 900.0),
        )
        self.assertEqual(policy.pending_timeout_s, 1800.0)

    def test_the_workspace_subdir_is_one_safe_name(self):
        for value in ("workspaces", "ws", "a.b_c-d", "0"):
            self.assertEqual(
                RepositoryPolicy(workspace_subdir=value).workspace_subdir, value
            )
        for value in (
            "",
            ".",
            "..",
            ".hidden",
            "a/b",
            "/a",
            "a b",
            "-x",
            "a\n",
            None,
            5,
            "x" * 65,
        ):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    RepositoryPolicy(workspace_subdir=value)

    def test_clone_hosts_are_lower_case_dns_names(self):
        self.assertEqual(
            RepositoryPolicy(clone_hosts=("github.com", "ghe.example.org")).clone_hosts,
            ("github.com", "ghe.example.org"),
        )
        for host in (
            "GitHub.com",
            "github.com:443",
            "https://github.com",
            "github.com/",
            "127.0.0.1",
            "10.0.0.1",
            "[::1]",
            "localhost.",
            "",
            "a b",
            None,
            5,
        ):
            with self.subTest(host=repr(host)):
                with self.assertRaises(ValueError):
                    RepositoryPolicy(clone_hosts=(host,))

    def test_min_uid_and_timeouts_are_bounded_numbers(self):
        for kwargs in (
            {"min_uid": 0},
            {"min_uid": -1},
            {"min_uid": True},
            {"min_uid": 1.5},
            {"min_uid": "1000"},
            {"min_uid": 2**32},
            {"git_timeout_s": 0},
            {"git_timeout_s": -1},
            {"git_timeout_s": True},
            {"git_timeout_s": "30"},
            {"git_timeout_s": limits.MAX_GIT_TIMEOUT_S + 1},
            {"clone_timeout_s": 0},
            {"clone_timeout_s": None},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    RepositoryPolicy(**kwargs)
        self.assertEqual(RepositoryPolicy(git_timeout_s=5).git_timeout_s, 5.0)
        self.assertEqual(RepositoryPolicy(min_uid=1).min_uid, 1)


class SettingsTest(unittest.TestCase):
    def test_the_environment_becomes_a_policy(self):
        with paw_environment(
            PAW_REPOSITORY_WORKSPACE_SUBDIR="work",
            PAW_REPOSITORY_EXISTING_ROOTS="{home}/src, /srv/repos/{user}",
            PAW_REPOSITORY_CLONE_HOSTS="github.com,ghe.example.org",
            PAW_REPOSITORY_MIN_LINUX_UID="2000",
            PAW_REPOSITORY_GIT_TIMEOUT_SECONDS="12.5",
            PAW_REPOSITORY_CLONE_TIMEOUT_SECONDS="60",
        ):
            policy = RepositoryPolicy.from_settings(Settings())

        self.assertEqual(
            (
                policy.workspace_subdir,
                policy.existing_roots,
                policy.clone_hosts,
                policy.min_uid,
                policy.git_timeout_s,
                policy.clone_timeout_s,
            ),
            (
                "work",
                ("{home}/src", "/srv/repos/{user}"),
                ("github.com", "ghe.example.org"),
                2000,
                12.5,
                60.0,
            ),
        )

    def test_without_settings_the_defaults_apply(self):
        self.assertEqual(
            RepositoryPolicy.from_settings(make_settings()), RepositoryPolicy()
        )

    def test_a_bad_value_fails_when_the_policy_is_built_not_later(self):
        for name, value in (
            ("repository_existing_roots", ["/srv/shared"]),
            ("repository_clone_hosts", ["10.0.0.1"]),
            ("repository_clone_hosts", ["GitHub.com"]),
        ):
            with self.subTest(name=name, value=value):
                settings = make_settings(**{name: value})
                with self.assertRaises(ValueError):
                    RepositoryPolicy.from_settings(settings)

    def test_the_settings_themselves_bound_the_values(self):
        for name, value in (
            ("repository_workspace_subdir", "../x"),
            ("repository_workspace_subdir", ""),
            ("repository_existing_roots", []),
            ("repository_clone_hosts", []),
            ("repository_min_linux_uid", 0),
            ("repository_git_timeout_seconds", 0),
            ("repository_clone_timeout_seconds", 7201),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValueError):
                    make_settings(**{name: value})


if __name__ == "__main__":
    unittest.main()
