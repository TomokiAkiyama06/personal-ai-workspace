"""``PAW_REPOSITORY_MIN_LINUX_UID`` is applied on the real service path (PAW-027).

The service is built the way production builds it (``RepositoryService.from_policy``
from ``RepositoryPolicy.from_settings``), the account database is a fake that returns
the account of the test process, and the same account is accepted under the default
minimum and refused once the setting is raised above its uid. Real PostgreSQL, real
git on local temporary repositories; skipped unless ``PAW_TEST_DATABASE_URL`` is set,
and skipped when the test process has a uid below the default minimum (there is then
no account that the default would accept).
"""

import os
import pwd
import unittest

from paw_backend.authz import Authorizer
from paw_backend.config import Settings
from paw_backend.repositories import (
    GitClient,
    LinuxAccountUnavailableError,
    LoginNameAccountDirectory,
    RepositoryPolicy,
    RepositoryService,
    limits,
)

from .repositories_support import (
    PostgresRepositoryTestCase,
    fs,
    requires_git,
    requires_postgres,
)
from .support import make_settings, paw_environment


@requires_postgres
@requires_git
@unittest.skipIf(
    os.geteuid() < limits.DEFAULT_MIN_LINUX_UID,
    "the test process has a uid below the default minimum",
)
class MinimumUidOnTheServicePathTest(PostgresRepositoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha")
        self.user = self.seed_user()
        self.seed_manager(self.project_id, self.user)
        self.login = self.rows(
            "SELECT login_name FROM users WHERE id = :i", i=self.user
        )[0]["login_name"]
        self.home = self.world.make_home(self.login)
        self.actor_ = self.actor(self.user)
        self.lookups = []

    def lookup(self, name):
        self.lookups.append(name)
        return pwd.struct_passwd(
            (name, "x", os.geteuid(), os.geteuid(), "", self.home, "/bin/bash")
        )

    def build(self, policy: RepositoryPolicy) -> RepositoryService:
        service = RepositoryService.from_policy(
            self.service_database(),
            Authorizer(self.sink, clock=self.clock),
            self.world.runner(),
            policy,
            account_lookup=self.lookup,
            clock=self.clock,
        )
        self.addAsyncCleanup(service._database.dispose)
        return service

    def raised_policy(self) -> RepositoryPolicy:
        """The policy of an operator who set the minimum just above this account."""
        with paw_environment(PAW_REPOSITORY_MIN_LINUX_UID=str(os.geteuid() + 1)):
            return RepositoryPolicy.from_settings(Settings())

    async def test_the_default_minimum_accepts_the_account(self):
        result = await self.build(RepositoryPolicy()).create_local(
            self.actor_, self.project_id, "one"
        )

        self.assertEqual(self.lookups, [self.login])
        self.assertTrue(result.checkout.path.startswith(self.home + "/workspaces/"))

    async def test_a_raised_minimum_refuses_the_same_account_on_every_path(self):
        service = self.build(self.raised_policy())
        self.assertEqual(service._policy.min_uid, os.geteuid() + 1)
        repository = self.seed_repository(
            self.project_id, name="seeded", remotes=("https://github.com/acme/x.git",)
        )
        existing = f"{self.home}/src/tool"
        os.makedirs(os.path.dirname(existing))
        self.world.make_repository(existing)
        calls = {
            "create_local": lambda: service.create_local(
                self.actor_, self.project_id, "two"
            ),
            "clone_from_github": lambda: service.clone_from_github(
                self.actor_, self.project_id, "acme/tool"
            ),
            "register_existing": lambda: service.register_existing(
                self.actor_, self.project_id, existing
            ),
            "create_checkout": lambda: service.create_checkout(
                self.actor_, self.project_id, repository
            ),
        }

        for name, call in calls.items():
            with self.subTest(method=name):
                with self.assertRaises(LinuxAccountUnavailableError):
                    await call()

        self.assertEqual(self.checkout_rows(), [])
        self.assertEqual(
            [r["name"] for r in self.repository_rows(self.project_id)], ["seeded"]
        )
        self.assertEqual(self.sink.events, [], "refused before any decision")
        self.assertFalse(fs.lexists(f"{self.home}/workspaces"))

    async def test_the_minimum_itself_is_accepted(self):
        policy = RepositoryPolicy(min_uid=os.geteuid())
        result = await self.build(policy).create_local(
            self.actor_, self.project_id, "one"
        )
        self.assertEqual(result.repository.name, "one")

    async def test_the_setting_reaches_the_service_through_the_environment(self):
        with paw_environment(PAW_REPOSITORY_MIN_LINUX_UID=str(os.geteuid() + 1)):
            policy = RepositoryPolicy.from_settings(Settings())
        self.assertEqual(policy.min_uid, os.geteuid() + 1)
        self.assertEqual(
            RepositoryPolicy.from_settings(make_settings()).min_uid,
            limits.DEFAULT_MIN_LINUX_UID,
        )


@requires_postgres
class OneValueTest(PostgresRepositoryTestCase):
    """A directory and a service that disagree about the minimum are refused."""

    def parts(self, directory_policy, **options):
        database = self.service_database()
        self.addAsyncCleanup(database.dispose)
        directory = LoginNameAccountDirectory(database, policy=directory_policy)
        return dict(
            database=database,
            authorizer=Authorizer(self.sink),
            accounts=directory,
            git=GitClient(self.world.runner(), RepositoryPolicy()),
            **options,
        )

    def test_the_same_value_is_accepted(self):
        RepositoryService(**self.parts(RepositoryPolicy()))
        RepositoryService(
            **self.parts(
                RepositoryPolicy(min_uid=2000), policy=RepositoryPolicy(min_uid=2000)
            )
        )

    def test_different_values_are_refused(self):
        for directory_policy, service_policy in (
            (RepositoryPolicy(min_uid=2000), None),
            (RepositoryPolicy(min_uid=2000), RepositoryPolicy()),
            (RepositoryPolicy(), RepositoryPolicy(min_uid=2000)),
        ):
            with self.subTest(directory=directory_policy.min_uid):
                with self.assertRaises(ValueError):
                    RepositoryService(
                        **self.parts(directory_policy, policy=service_policy)
                    )

    def test_the_factory_checks_its_arguments(self):
        database = self.service_database()
        self.addAsyncCleanup(database.dispose)
        authorizer, runner = Authorizer(self.sink), self.world.runner()
        with self.assertRaises(TypeError):
            RepositoryService.from_policy(database, authorizer, runner, None)
        with self.assertRaises(TypeError):
            RepositoryService.from_policy(database, authorizer, runner, {})
        with self.assertRaises(TypeError):  # a lookup is only for the default directory
            RepositoryService.from_policy(
                database,
                authorizer,
                runner,
                RepositoryPolicy(),
                accounts=self.accounts,
                account_lookup=lambda name: None,
            )
        service = RepositoryService.from_policy(
            database, authorizer, runner, RepositoryPolicy(), accounts=self.accounts
        )
        self.assertIs(service._accounts, self.accounts)


if __name__ == "__main__":
    unittest.main()
