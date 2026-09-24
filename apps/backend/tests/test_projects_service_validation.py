"""The actor and the arguments are checked before the database is touched.

The service here sits on a ``Database`` without a URL: any attempt to run a
query raises ``DatabaseNotConfiguredError``, so an error that is reported
instead proves that nothing was attempted. No PostgreSQL is needed.
"""

import unittest
from datetime import datetime
from uuid import uuid4

from paw_backend.authz import Authorizer, InMemoryAuditSink, Principal, SystemRole
from paw_backend.authz.policy import Reason
from paw_backend.authz.roles import ProjectRole
from paw_backend.db import Database, DatabaseNotConfiguredError
from paw_backend.projects import (
    InputProblem,
    InvalidProjectInputError,
    ProjectPermissionDeniedError,
    ProjectService,
    ProjectStatus,
)

from .projects_support import T0
from .support import make_settings

SECRET = "sk-live-SECRET-0123456789"


class ServiceValidationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sink = InMemoryAuditSink()
        self.service = ProjectService(
            Database(make_settings()), Authorizer(self.sink), clock=lambda: T0
        )
        self.actor = Principal(uuid4(), SystemRole.USER)
        self.pid, self.uid = uuid4(), uuid4()

    async def assertRejected(self, field, problem, awaitable):
        with self.assertRaises(InvalidProjectInputError) as raised:
            await awaitable
        self.assertEqual(
            (raised.exception.field, raised.exception.problem), (field, problem)
        )

    async def assertReachesTheDatabase(self, awaitable):
        with self.assertRaises(DatabaseNotConfiguredError):
            await awaitable

    # -- the actor ------------------------------------------------------------------

    async def test_a_missing_or_foreign_actor_is_denied_before_anything_else(self):
        calls = {
            "create_project": lambda a: self.service.create_project(a, "Alpha"),
            "get_project": lambda a: self.service.get_project(a, self.pid),
            "list_projects": lambda a: self.service.list_projects(a),
            "rename_project": lambda a: self.service.rename_project(a, self.pid, "X"),
            "set_description": lambda a: self.service.set_description(a, self.pid, "d"),
            "invite_member": lambda a: self.service.invite_member(
                a, self.pid, self.uid, ProjectRole.VIEWER
            ),
            "accept_invite": lambda a: self.service.accept_invite(a, self.pid),
            "decline_invite": lambda a: self.service.decline_invite(a, self.pid),
            "remove_member": lambda a: self.service.remove_member(
                a, self.pid, self.uid
            ),
            "leave_project": lambda a: self.service.leave_project(a, self.pid),
            "change_role": lambda a: self.service.change_role(
                a, self.pid, self.uid, ProjectRole.VIEWER
            ),
            "list_members": lambda a: self.service.list_members(a, self.pid),
            "list_invites": lambda a: self.service.list_invites(a, self.pid),
            "list_my_invites": lambda a: self.service.list_my_invites(a),
            "archive": lambda a: self.service.archive(a, self.pid),
            "unarchive": lambda a: self.service.unarchive(a, self.pid),
            "begin_deletion": lambda a: self.service.begin_deletion(a, self.pid, "x"),
            "restore": lambda a: self.service.restore(a, self.pid),
        }
        for name, call in calls.items():
            for bad in (None, "user", {"user_id": str(uuid4())}, uuid4()):
                with self.subTest(method=name, actor=type(bad).__name__):
                    with self.assertRaises(ProjectPermissionDeniedError) as raised:
                        await call(bad)
                    self.assertIs(raised.exception.reason, Reason.UNAUTHENTICATED)
        self.assertEqual(self.sink.events, [])

    async def test_the_system_identity_cannot_use_the_self_service_methods(self):
        system = Principal(uuid4(), SystemRole.SYSTEM)
        calls = {
            "create_project": self.service.create_project(system, "Alpha"),
            "list_projects": self.service.list_projects(system),
            "accept_invite": self.service.accept_invite(system, self.pid),
            "decline_invite": self.service.decline_invite(system, self.pid),
            "leave_project": self.service.leave_project(system, self.pid),
            "list_my_invites": self.service.list_my_invites(system),
        }
        for name, awaitable in calls.items():
            with self.subTest(method=name):
                with self.assertRaises(ProjectPermissionDeniedError) as raised:
                    await awaitable
                self.assertIs(raised.exception.reason, Reason.CAPABILITY_NOT_GRANTED)

    async def test_the_actor_is_checked_before_the_arguments(self):
        with self.assertRaises(ProjectPermissionDeniedError):
            await self.service.create_project(None, "")
        with self.assertRaises(ProjectPermissionDeniedError):
            await self.service.rename_project(None, "not-a-uuid", None)

    # -- arguments -----------------------------------------------------------------

    async def test_create_project_validates_name_and_description(self):
        await self.assertRejected(
            "name", InputProblem.EMPTY, self.service.create_project(self.actor, "  ")
        )
        await self.assertRejected(
            "name",
            InputProblem.TOO_LONG,
            self.service.create_project(self.actor, "a" * 101),
        )
        await self.assertRejected(
            "description",
            InputProblem.NOT_A_STRING,
            self.service.create_project(self.actor, "Alpha", 5),
        )
        await self.assertRejected(
            "description",
            InputProblem.TOO_LONG,
            self.service.create_project(self.actor, "Alpha", "d" * 2001),
        )
        await self.assertReachesTheDatabase(
            self.service.create_project(self.actor, "Alpha", "d" * 2000)
        )
        await self.assertReachesTheDatabase(
            self.service.create_project(self.actor, "Alpha")
        )

    async def test_ids_must_be_uuids(self):
        for bad in ("not-a-uuid", str(uuid4()).upper(), None, 5):
            with self.subTest(bad=repr(bad)):
                await self.assertRejected(
                    "project_id",
                    InputProblem.NOT_A_UUID,
                    self.service.get_project(self.actor, bad),
                )
        await self.assertRejected(
            "user_id",
            InputProblem.NOT_A_UUID,
            self.service.remove_member(self.actor, self.pid, "nope"),
        )
        await self.assertReachesTheDatabase(
            self.service.get_project(self.actor, str(self.pid))
        )
        await self.assertReachesTheDatabase(
            self.service.get_project(self.actor, self.pid)
        )

    async def test_a_role_must_be_a_project_role_not_a_string(self):
        await self.assertRejected(
            "role",
            InputProblem.NOT_A_ROLE,
            self.service.invite_member(self.actor, self.pid, self.uid, "viewer"),
        )
        await self.assertRejected(
            "role",
            InputProblem.NOT_A_ROLE,
            self.service.change_role(self.actor, self.pid, self.uid, "manager"),
        )
        await self.assertReachesTheDatabase(
            self.service.invite_member(
                self.actor, self.pid, self.uid, ProjectRole.VIEWER
            )
        )

    async def test_arguments_are_checked_in_signature_order(self):
        await self.assertRejected(
            "project_id",
            InputProblem.NOT_A_UUID,
            self.service.invite_member(self.actor, "bad", "bad", "bad"),
        )
        await self.assertRejected(
            "user_id",
            InputProblem.NOT_A_UUID,
            self.service.invite_member(self.actor, self.pid, "bad", "bad"),
        )

    async def test_rename_and_description_are_validated(self):
        await self.assertRejected(
            "name",
            InputProblem.INVALID_CHARACTERS,
            self.service.rename_project(self.actor, self.pid, "a\nb"),
        )
        await self.assertRejected(
            "description",
            InputProblem.INVALID_CHARACTERS,
            self.service.set_description(self.actor, self.pid, "a\x00b"),
        )
        await self.assertReachesTheDatabase(
            self.service.set_description(self.actor, self.pid, None)
        )

    async def test_begin_deletion_validates_the_confirmation(self):
        await self.assertRejected(
            "confirm_name",
            InputProblem.NOT_A_STRING,
            self.service.begin_deletion(self.actor, self.pid, None),
        )
        await self.assertRejected(
            "confirm_name",
            InputProblem.TOO_LONG,
            self.service.begin_deletion(self.actor, self.pid, "a" * 101),
        )
        await self.assertReachesTheDatabase(
            self.service.begin_deletion(self.actor, self.pid, "")
        )

    async def test_list_projects_validates_status_limit_and_offset(self):
        await self.assertRejected(
            "status",
            InputProblem.NOT_A_STATUS,
            self.service.list_projects(self.actor, status="active"),
        )
        await self.assertRejected(
            "status",
            InputProblem.OUT_OF_RANGE,
            self.service.list_projects(self.actor, status=ProjectStatus.DELETED),
        )
        await self.assertRejected(
            "limit",
            InputProblem.OUT_OF_RANGE,
            self.service.list_projects(self.actor, limit=0),
        )
        await self.assertRejected(
            "limit",
            InputProblem.NOT_AN_INTEGER,
            self.service.list_projects(self.actor, limit=True),
        )
        await self.assertRejected(
            "offset",
            InputProblem.OUT_OF_RANGE,
            self.service.list_projects(self.actor, offset=-1),
        )
        await self.assertReachesTheDatabase(
            self.service.list_projects(self.actor, limit=200, offset=100_000)
        )

    async def test_roles_of_and_purge_expired_validate_their_arguments(self):
        await self.assertRejected(
            "user_id", InputProblem.NOT_A_UUID, self.service.roles_of("nope")
        )
        await self.assertRejected(
            "now",
            InputProblem.NAIVE_DATETIME,
            self.service.purge_expired(datetime(2026, 9, 24)),
        )
        await self.assertRejected(
            "now", InputProblem.NOT_A_DATETIME, self.service.purge_expired("2026-09-24")
        )
        await self.assertRejected(
            "batch_size",
            InputProblem.OUT_OF_RANGE,
            self.service.purge_expired(T0, batch_size=0),
        )
        await self.assertRejected(
            "batch_size",
            InputProblem.NOT_AN_INTEGER,
            self.service.purge_expired(T0, batch_size=True),
        )
        await self.assertReachesTheDatabase(self.service.purge_expired())
        await self.assertReachesTheDatabase(
            self.service.purge_expired(T0, batch_size=500)
        )

    async def test_the_clock_must_return_an_aware_datetime(self):
        service = ProjectService(
            Database(make_settings()),
            Authorizer(self.sink),
            clock=lambda: datetime(2026, 9, 24),
        )
        await self.assertRejected(
            "clock", InputProblem.NAIVE_DATETIME, service.archive(self.actor, self.pid)
        )
        await self.assertRejected(
            "clock", InputProblem.NAIVE_DATETIME, service.list_my_invites(self.actor)
        )
        service = ProjectService(
            Database(make_settings()), Authorizer(self.sink), clock=lambda: "now"
        )
        await self.assertRejected(
            "clock", InputProblem.NOT_A_DATETIME, service.archive(self.actor, self.pid)
        )

    async def test_the_clock_is_read_after_the_arguments(self):
        calls = []
        service = ProjectService(
            Database(make_settings()),
            Authorizer(self.sink),
            clock=lambda: calls.append(1) or T0,
        )
        await self.assertRejected(
            "project_id", InputProblem.NOT_A_UUID, service.archive(self.actor, "bad")
        )
        self.assertEqual(calls, [])

    async def test_errors_never_echo_the_rejected_value(self):
        for awaitable in (
            self.service.create_project(self.actor, SECRET + "\x00"),
            self.service.create_project(self.actor, "a" * 101 + SECRET),
            self.service.get_project(self.actor, SECRET),
            self.service.begin_deletion(self.actor, self.pid, SECRET * 10),
        ):
            with self.assertRaises(InvalidProjectInputError) as raised:
                await awaitable
            text = str(raised.exception) + repr(raised.exception.args)
            self.assertNotIn(SECRET, text)
            self.assertIsNone(raised.exception.__cause__)


class ConstructorTest(unittest.TestCase):
    def build(self, **overrides):
        values = dict(
            database=Database(make_settings()),
            authorizer=Authorizer(InMemoryAuditSink()),
        )
        values.update(overrides)
        return ProjectService(
            values.pop("database"), values.pop("authorizer"), **values
        )

    def test_a_valid_service_can_be_built_with_defaults(self):
        self.assertIsInstance(self.build(), ProjectService)
        self.assertIsInstance(self.build(lock_timeout_ms=1), ProjectService)
        self.assertIsInstance(self.build(lock_timeout_ms=60_000), ProjectService)

    def test_wrong_types_are_refused(self):
        with self.assertRaises(TypeError):
            self.build(database=object())
        with self.assertRaises(TypeError):
            self.build(authorizer=object())
        with self.assertRaises(TypeError):
            self.build(clock="now")
        for bad in (True, 3.0, "3000", None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(TypeError):
                    self.build(lock_timeout_ms=bad)

    def test_the_lock_timeout_must_be_within_its_range(self):
        for bad in (0, -1, 60_001):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.build(lock_timeout_ms=bad)


if __name__ == "__main__":
    unittest.main()
