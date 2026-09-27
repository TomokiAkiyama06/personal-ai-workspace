"""Every argument of every public method is checked before anything else happens.

The service here sits on a ``Database`` without a URL (any query raises
``DatabaseNotConfiguredError``), on an account directory that fails the test when it
is asked, and on a git runner that fails the test when it is used: an error that is
reported instead proves that nothing was attempted. The tables below are the whole
method x argument x bad value matrix. No PostgreSQL is needed.
"""

import unittest
import uuid
from datetime import datetime

from paw_backend.authz import Authorizer, InMemoryAuditSink, Principal, SystemRole
from paw_backend.authz.capabilities import RepoPermission
from paw_backend.authz.policy import Reason
from paw_backend.db import Database, DatabaseNotConfiguredError
from paw_backend.repositories import (
    GitClient,
    InputProblem,
    InvalidRepositoryInputError,
    RepositoryPermissionDeniedError,
    RepositoryPolicy,
    RepositoryService,
    UnavailableGitHubGateway,
    limits,
)

from .support import make_settings

PID, RID, UID = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
T0 = datetime.fromisoformat("2026-09-25T12:00:00+00:00")
SECRET = "sk_live_" + "SECRET-0123456789"


class AccountReached(Exception):
    """Raised by the account directory: validation let the call through."""


class Accounts:
    async def account_of(self, user_id):
        raise AccountReached


class Runner:
    async def run(self, *args, **options):
        raise AssertionError("git must not be used")


UUID_BAD = [
    None,
    "not-a-uuid",
    5,
    str(uuid.uuid4()).upper(),
    uuid.uuid4().hex,
    b"x",
    "",
]
NAME_BAD = [
    None,
    5,
    "",
    " a",
    "a b",
    ".a",
    "a/b",
    "a.git",
    "日本",
    "x" * 101,
    "a\x00b",
]
BRANCH_BAD = [None, 5, "", "-a", "a..b", "a b", "a/", "x" * 201, "a\x00"]
PATH_BAD = [None, 5, "", "relative", "/a/../b", "/a//b", "/a/", "/", "~/a", "/a\x00"]
URL_BAD = [
    None,
    5,
    "",
    "http://github.com/a/b",
    "git@github.com:a/b",
    "https://u@x.com/a",
]
SOURCE_BAD = [None, 5, "", "acme", "http://github.com/a/b", "https://evil.org/a/b"]
BOOL_BAD = [None, 0, 1, "true", "yes"]
LIMIT_BAD = [None, True, "5", 1.5, 0, -1, limits.MAX_LIST_LIMIT + 1]
OFFSET_BAD = [None, True, "5", 1.5, -1, limits.MAX_LIST_OFFSET + 1]
ACL_BAD = ["read", b"read", 5, ["read"], [RepoPermission.READ, "write"], [None]]
IDS_BAD = ["x", None, 5, ["x"], [1], [uuid.uuid4()] * (limits.MAX_PURGE_PROJECTS + 1)]

# method -> (positional arguments with valid values, keyword arguments with valid
# values, the actor kind: "user" (the actor argument comes first) or "internal").
GOOD_PATH = "/home/alice/src/tool"
METHODS = {
    "register_existing": (
        {"project_id": PID, "path": GOOD_PATH},
        {"name": "tool"},
    ),
    "clone_from_github": (
        {"project_id": PID, "source": "acme/tool"},
        {"name": "tool", "branch": "main"},
    ),
    "create_local": (
        {"project_id": PID, "name": "fresh"},
        {"default_branch": "main"},
    ),
    "create_github": (
        {"project_id": PID, "name": "fresh"},
        {"private": True, "default_branch": "main"},
    ),
    "create_checkout": ({"project_id": PID, "repository_id": RID}, {}),
    "remove_checkout": ({"project_id": PID, "repository_id": RID}, {}),
    "remove_repository": ({"project_id": PID, "repository_id": RID}, {}),
    "add_remote": (
        {"project_id": PID, "repository_id": RID, "url": "https://github.com/a/b"},
        {},
    ),
    "remove_remote": (
        {"project_id": PID, "repository_id": RID, "url": "https://github.com/a/b"},
        {},
    ),
    "set_acl": (
        {"project_id": PID, "repository_id": RID, "allowed": [RepoPermission.READ]},
        {},
    ),
    "get_repository": ({"project_id": PID, "repository_id": RID}, {}),
    "list_repositories": ({"project_id": PID}, {"limit": 10, "offset": 0}),
    "list_my_checkouts": ({}, {"limit": 10, "offset": 0}),
}
INTERNAL = {
    "scope_entries": (
        {"user_id": UID, "project_id": PID, "repository_id": RID},
        {},
    ),
    "purge_projects": ({"project_ids": [PID]}, {}),
}
# Methods whose valid call goes to the account directory before the database.
ACCOUNT_FIRST = {
    "register_existing",
    "clone_from_github",
    "create_local",
    "create_github",
    "create_checkout",
    "scope_entries",  # the Linux account (its uid) is needed to verify the roots
}
# The bad values of each argument name.
BAD = {
    "project_id": UUID_BAD,
    "repository_id": UUID_BAD,
    "user_id": UUID_BAD,
    "path": PATH_BAD,
    "source": SOURCE_BAD,
    "url": URL_BAD,
    "allowed": ACL_BAD,
    "project_ids": IDS_BAD,
    "limit": LIMIT_BAD,
    "offset": OFFSET_BAD,
    "private": BOOL_BAD,
    "default_branch": BRANCH_BAD,
    "branch": BRANCH_BAD[:1] + BRANCH_BAD[1:],
}
# ``name`` may be None for the two methods that derive it; elsewhere it is required.
NAME_OPTIONAL = {"register_existing", "clone_from_github"}


def bad_values(method: str, argument: str):
    if argument == "name":
        return NAME_BAD if method not in NAME_OPTIONAL else NAME_BAD[1:]
    if argument == "branch":
        return BRANCH_BAD[1:]  # None means "the remote's default"
    return BAD[argument]


class ServiceValidationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sink = InMemoryAuditSink()
        self.service = RepositoryService(
            Database(make_settings()),
            Authorizer(self.sink),
            Accounts(),
            GitClient(Runner(), RepositoryPolicy()),
            clock=lambda: T0,
        )
        self.actor = Principal(uuid.uuid4(), SystemRole.USER)

    def invoke(self, method, arguments, keywords, actor=None):
        function = getattr(self.service, method)
        first = [self.actor if actor is None else actor] if method in METHODS else []
        return function(*first, *arguments.values(), **keywords)

    async def test_the_valid_calls_of_the_table_get_past_validation(self):
        for method, (arguments, keywords) in {**METHODS, **INTERNAL}.items():
            expected = (
                AccountReached
                if method in ACCOUNT_FIRST
                else DatabaseNotConfiguredError
            )
            with self.subTest(method=method):
                with self.assertRaises(expected):
                    await self.invoke(method, arguments, keywords)

    async def test_every_bad_argument_is_refused_by_name_before_anything_happens(self):
        checked = 0
        for method, (arguments, keywords) in {**METHODS, **INTERNAL}.items():
            for argument in [*arguments, *keywords]:
                if argument == "actor":
                    continue
                for bad in bad_values(method, argument):
                    with self.subTest(
                        method=method, argument=argument, bad=repr(bad)[:30]
                    ):
                        new_arguments = dict(arguments)
                        new_keywords = dict(keywords)
                        (new_arguments if argument in arguments else new_keywords)[
                            argument
                        ] = bad
                        with self.assertRaises(InvalidRepositoryInputError) as raised:
                            await self.invoke(method, new_arguments, new_keywords)
                        self.assertEqual(raised.exception.field, argument)
                        self.assertIsInstance(raised.exception.problem, InputProblem)
                        self.assertNotIn(SECRET, str(raised.exception))
                        checked += 1
        self.assertGreater(checked, 250, "the table must cover the whole matrix")
        self.assertEqual(self.sink.events, [])

    async def test_the_message_never_contains_the_value(self):
        hostile = {
            "add_remote": ("url", f"http://x.example.org/{SECRET}"),
            "register_existing": ("path", f"/{SECRET}/../x"),
            "create_local": ("name", f"{SECRET}/x"),
        }
        for method, (field, value) in hostile.items():
            arguments, keywords = METHODS[method]
            with self.subTest(method=method):
                with self.assertRaises(InvalidRepositoryInputError) as raised:
                    await self.invoke(method, {**arguments, field: value}, keywords)
                self.assertEqual(raised.exception.field, field)
                self.assertNotIn(SECRET, str(raised.exception))
                self.assertNotIn(SECRET, repr(raised.exception))
                self.assertNotIn(SECRET, repr(raised.exception.args))

    async def test_the_first_bad_argument_in_signature_order_is_reported(self):
        with self.assertRaises(InvalidRepositoryInputError) as raised:
            await self.service.add_remote(self.actor, "bad", "bad", "bad")
        self.assertEqual(raised.exception.field, "project_id")
        with self.assertRaises(InvalidRepositoryInputError) as raised:
            await self.service.add_remote(self.actor, PID, "bad", "bad")
        self.assertEqual(raised.exception.field, "repository_id")
        with self.assertRaises(InvalidRepositoryInputError) as raised:
            await self.service.create_github(self.actor, PID, "bad name", private="x")
        self.assertEqual(raised.exception.field, "name")

    async def test_ids_can_be_given_as_canonical_strings(self):
        with self.assertRaises(DatabaseNotConfiguredError):
            await self.service.remove_repository(self.actor, str(PID), str(RID))

    async def test_a_missing_or_foreign_actor_is_denied_before_anything_else(self):
        for method, (arguments, _) in METHODS.items():
            for bad in (None, "user", {"user_id": str(uuid.uuid4())}, uuid.uuid4()):
                with self.subTest(method=method, actor=type(bad).__name__):
                    with self.assertRaises(RepositoryPermissionDeniedError) as raised:
                        # A bad actor comes first, even when arguments are bad too.
                        await getattr(self.service, method)(
                            bad, *[None] * len(arguments)
                        )
                    self.assertIs(raised.exception.reason, Reason.UNAUTHENTICATED)
        self.assertEqual(self.sink.events, [])

    async def test_the_system_identity_cannot_use_the_methods_that_write(self):
        system = Principal(uuid.uuid4(), SystemRole.SYSTEM)
        for method, (arguments, keywords) in METHODS.items():
            if method in ("get_repository", "list_repositories"):
                continue  # reads: the Authorizer decides
            with self.subTest(method=method):
                with self.assertRaises(RepositoryPermissionDeniedError) as raised:
                    await self.invoke(method, arguments, keywords, actor=system)
                self.assertIs(raised.exception.reason, Reason.CAPABILITY_NOT_GRANTED)

    async def test_the_reads_reach_the_authorizer_for_the_system_identity(self):
        system = Principal(uuid.uuid4(), SystemRole.SYSTEM)
        with self.assertRaises(DatabaseNotConfiguredError):
            await self.service.get_repository(system, PID, RID)

    async def test_a_clock_that_is_not_aware_is_a_programming_error(self):
        service = RepositoryService(
            Database(make_settings()),
            Authorizer(self.sink),
            Accounts(),
            GitClient(Runner(), RepositoryPolicy()),
            clock=lambda: datetime(2026, 9, 25),
        )
        with self.assertRaises(ValueError):
            await service.add_remote(self.actor, PID, RID, "https://github.com/a/b")
        service = RepositoryService(
            Database(make_settings()),
            Authorizer(self.sink),
            Accounts(),
            GitClient(Runner(), RepositoryPolicy()),
            clock=lambda: "now",
        )
        with self.assertRaises(ValueError):
            await service.set_acl(self.actor, PID, RID, None)


class ConstructorTest(unittest.TestCase):
    def parts(self):
        return dict(
            database=Database(make_settings()),
            authorizer=Authorizer(InMemoryAuditSink()),
            accounts=Accounts(),
            git=GitClient(Runner(), RepositoryPolicy()),
        )

    def test_a_valid_service(self):
        RepositoryService(**self.parts())
        RepositoryService(
            **self.parts(),
            policy=RepositoryPolicy(),
            github=UnavailableGitHubGateway(),
            clock=lambda: T0,
            lock_timeout_ms=1,
        )

    def test_every_dependency_is_checked(self):
        cases = [
            ("database", object(), TypeError),
            ("database", None, TypeError),
            ("authorizer", object(), TypeError),
            ("accounts", object(), TypeError),
            ("git", object(), TypeError),
            ("git", Runner(), TypeError),
            ("policy", {}, TypeError),
            ("github", object(), TypeError),
            ("clock", "now", TypeError),
            ("lock_timeout_ms", True, TypeError),
            ("lock_timeout_ms", "3000", TypeError),
            ("lock_timeout_ms", 1.5, TypeError),
            ("lock_timeout_ms", 0, ValueError),
            ("lock_timeout_ms", limits.MAX_LOCK_TIMEOUT_MS + 1, ValueError),
        ]
        for name, value, error in cases:
            with self.subTest(name=name, value=repr(value)[:20]):
                arguments = self.parts()
                arguments[name] = value
                with self.assertRaises(error):
                    RepositoryService(**arguments)


if __name__ == "__main__":
    unittest.main()
