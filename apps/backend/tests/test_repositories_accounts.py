"""The mapping from a workspace user to a Linux account (``LoginNameAccountDirectory``).

Real PostgreSQL for ``users``; the account database (``pwd``) is a fake, so no
system account is needed. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import os
import pwd
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from paw_backend.repositories import (
    LinuxAccountUnavailableError,
    LoginNameAccountDirectory,
)

from .repositories_support import PostgresRepositoryTestCase, requires_postgres


def entry(
    name="alice",
    uid=1000,
    home="/home/alice",
    shell="/bin/bash",
) -> pwd.struct_passwd:
    return pwd.struct_passwd((name, "x", uid, uid, "", home, shell))


@requires_postgres
class LoginNameAccountDirectoryTest(PostgresRepositoryTestCase):
    def directory(self, records: dict[str, pwd.struct_passwd], **options):
        calls = []

        def lookup(name: str):
            calls.append(name)
            try:
                return records[name]
            except KeyError:
                raise KeyError(name) from None

        directory = LoginNameAccountDirectory(
            self.service_database(), lookup=lookup, **options
        )
        self.addAsyncCleanup(directory._database.dispose)
        return directory, calls

    def login_of(self, user_id) -> str:
        return self.rows("SELECT login_name FROM users WHERE id = :i", i=user_id)[0][
            "login_name"
        ]

    async def test_the_login_name_is_the_linux_user_name(self):
        user = self.seed_user()
        login = self.login_of(user)
        directory, calls = self.directory({login: entry(login, 1234, "/home/" + login)})

        account = await directory.account_of(user)

        self.assertEqual(
            (account.user_id, account.username, account.uid, account.home),
            (user, login, 1234, f"/home/{login}"),
        )
        self.assertEqual(calls, [login])

    async def test_an_unknown_user_or_one_that_is_not_active_has_no_account(self):
        inactive = self.seed_user(status="pending_deletion")
        invited = self.seed_user(status="invited")
        for user in (uuid.uuid4(), inactive, invited):
            with self.subTest(user=user):
                directory, calls = self.directory({})
                with self.assertRaises(LinuxAccountUnavailableError):
                    await directory.account_of(user)
                self.assertEqual(calls, [], "no account lookup for such a user")

    async def test_a_user_without_a_linux_account_has_none(self):
        user = self.seed_user()
        directory, _ = self.directory({})
        with self.assertRaises(LinuxAccountUnavailableError):
            await directory.account_of(user)

    async def test_system_accounts_are_never_a_checkout_owner(self):
        user = self.seed_user()
        login = self.login_of(user)
        cases = {
            "root": entry(login, 0, "/root"),
            "a system uid": entry(login, 999, "/var/lib/x"),
            "the lowest human uid minus one": entry(login, 999, "/home/x"),
            "nobody": entry(login, 65534, "/nonexistent"),
            "above nobody": entry(login, 65535, "/home/x"),
            "nologin": entry(login, 1500, "/home/x", "/usr/sbin/nologin"),
            "false": entry(login, 1500, "/home/x", "/bin/false"),
            "a name that differs": entry("someone-else", 1500, "/home/x"),
            "a relative home": entry(login, 1500, "home/x"),
            "the root as home": entry(login, 1500, "/"),
            "an empty home": entry(login, 1500, ""),
            "a home with dots": entry(login, 1500, "/home/../etc"),
        }
        for label, record in cases.items():
            with self.subTest(label=label):
                directory, _ = self.directory({login: record})
                with self.assertRaises(LinuxAccountUnavailableError):
                    await directory.account_of(user)

    async def test_the_lowest_uid_is_configurable(self):
        user = self.seed_user()
        login = self.login_of(user)
        directory, _ = self.directory(
            {login: entry(login, 500, "/home/x")}, min_uid=500
        )
        self.assertEqual((await directory.account_of(user)).uid, 500)

    async def test_the_lookup_error_carries_no_detail(self):
        user = self.seed_user()
        login = self.login_of(user)

        def lookup(name):
            raise OSError("ldap://internal.example.org bind failed for cn=admin")

        directory = LoginNameAccountDirectory(self.service_database(), lookup=lookup)
        self.addAsyncCleanup(directory._database.dispose)

        with self.assertRaises(LinuxAccountUnavailableError) as raised:
            await directory.account_of(user)

        self.assertNotIn("ldap", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(login, self.login_of(user))

    def test_the_constructor_checks_its_arguments(self):
        database = self.service_database()
        for kwargs in ({"min_uid": 0}, {"min_uid": True}, {"min_uid": "1000"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(TypeError):
                    LoginNameAccountDirectory(database, **kwargs)
        with self.assertRaises(TypeError):
            LoginNameAccountDirectory(object())

    async def test_the_real_account_database_gives_the_current_user(self):
        # Not mocked: proves the default lookup and the entry fields fit.
        me = pwd.getpwuid(os.geteuid())
        if me.pw_uid < 1000 or me.pw_shell.endswith(("nologin", "false")):
            self.skipTest("the test process is not a person's account")
        user = self.seed_user()
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text("UPDATE users SET login_name = :n WHERE id = :i"),
                    {"n": me.pw_name, "i": user},
                )
        except IntegrityError:
            self.skipTest("the user name is not a valid login name")
        directory = LoginNameAccountDirectory(self.service_database())
        self.addAsyncCleanup(directory._database.dispose)

        account = await directory.account_of(user)

        self.assertEqual((account.username, account.uid), (me.pw_name, me.pw_uid))
