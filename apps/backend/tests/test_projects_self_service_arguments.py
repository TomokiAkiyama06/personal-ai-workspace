"""Every argument of the four self service methods, before anything is decided.

``create_project``, ``accept_invite``, ``decline_invite`` and ``leave_project``
ask the Authorizer (Decision 0022), and the Authorizer writes an audit event. A
malformed call must therefore be refused **before** that: no event, no database
access, and only the module's typed error (never ``AttributeError`` /
``TypeError`` / a driver error). The service sits on a ``Database`` without a
URL, so any query would raise ``DatabaseNotConfiguredError``: an
``InvalidProjectInputError`` that arrives instead proves that nothing was
attempted. No PostgreSQL is needed.

Each table is method x argument x bad value; the last test of each group shows
that a valid value gets past validation (the event is written, then the database
is reached), so the bad values are refused for their value and not for the shape
of the call.
"""

import unittest
import uuid

from paw_backend.authz import Authorizer, InMemoryAuditSink, Principal, SystemRole
from paw_backend.authz.policy import Reason
from paw_backend.authz.roles import ProjectRole
from paw_backend.db import Database, DatabaseNotConfiguredError
from paw_backend.projects import (
    InputProblem,
    InvalidProjectInputError,
    ProjectPermissionDeniedError,
    ProjectService,
)

from .projects_support import T0
from .support import make_settings

PID = uuid.UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")  # hex letters: upper() differs

NOT_A_UUID = (
    None,
    5,
    True,
    b"0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    "",
    "not-a-uuid",
    str(PID).upper(),
    " " + str(PID),
    str(PID) + "\n",
    str(PID).replace("-", ""),
    "urn:uuid:" + str(PID),
    [PID],
    object(),
)
NAMES = (
    (None, InputProblem.NOT_A_STRING),
    (5, InputProblem.NOT_A_STRING),
    (b"Alpha", InputProblem.NOT_A_STRING),
    ("", InputProblem.EMPTY),
    ("   ", InputProblem.EMPTY),
    ("a" * 101, InputProblem.TOO_LONG),
    ("a\nb", InputProblem.INVALID_CHARACTERS),
    ("a\tb", InputProblem.INVALID_CHARACTERS),
    ("a\x00b", InputProblem.INVALID_CHARACTERS),
    ("a\ud800b", InputProblem.INVALID_CHARACTERS),
    ("a\u2028b", InputProblem.INVALID_CHARACTERS),
    ("a\u202eb", InputProblem.INVALID_CHARACTERS),
)
DESCRIPTIONS = (
    (5, InputProblem.NOT_A_STRING),
    (b"d", InputProblem.NOT_A_STRING),
    (["d"], InputProblem.NOT_A_STRING),
    ("d" * 2001, InputProblem.TOO_LONG),
    ("a\x00b", InputProblem.INVALID_CHARACTERS),
    ("a\ud800b", InputProblem.INVALID_CHARACTERS),
    ("a\u202eb", InputProblem.INVALID_CHARACTERS),
)


class ArgumentsBeforeTheDecisionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sink = InMemoryAuditSink()
        self.service = ProjectService(
            Database(make_settings()), Authorizer(self.sink), clock=lambda: T0
        )
        self.actor = Principal(uuid.uuid4(), SystemRole.USER)

    async def assertRefusedBeforeTheDecision(self, field, problem, awaitable):
        with self.assertRaises(InvalidProjectInputError) as raised:
            await awaitable
        self.assertEqual(
            (raised.exception.field, raised.exception.problem), (field, problem)
        )
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(self.sink.events, [], "the Authorizer must not be asked")

    async def assertDecidedThenReachesTheDatabase(self, awaitable, capability):
        with self.assertRaises(DatabaseNotConfiguredError):
            await awaitable
        (event,) = self.sink.events
        self.assertEqual(
            (event.action, event.decision, event.actor_id),
            (capability, "allow", self.actor.user_id),
        )
        self.sink.events.clear()

    # -- the project id of accept / decline / leave ---------------------------------

    def id_methods(self):
        return {
            "accept_invite": lambda bad: self.service.accept_invite(self.actor, bad),
            "decline_invite": lambda bad: self.service.decline_invite(self.actor, bad),
            "leave_project": lambda bad: self.service.leave_project(self.actor, bad),
        }

    async def test_project_id_must_be_a_canonical_uuid(self):
        for method, call in self.id_methods().items():
            for bad in NOT_A_UUID:
                with self.subTest(method=method, bad=type(bad).__name__ + repr(bad)):
                    await self.assertRefusedBeforeTheDecision(
                        "project_id", InputProblem.NOT_A_UUID, call(bad)
                    )

    async def test_a_valid_project_id_is_decided_then_reaches_the_database(self):
        capability = {
            "accept_invite": "project.invitation.respond",
            "decline_invite": "project.invitation.respond",
            "leave_project": "project.leave",
        }
        for method, call in self.id_methods().items():
            for value in (PID, str(PID)):
                with self.subTest(method=method, value=type(value).__name__):
                    await self.assertDecidedThenReachesTheDatabase(
                        call(value), capability[method]
                    )

    # -- create_project ---------------------------------------------------------------

    async def test_create_project_name_must_be_a_clean_bounded_string(self):
        for bad, problem in NAMES:
            with self.subTest(bad=repr(bad)):
                await self.assertRefusedBeforeTheDecision(
                    "name", problem, self.service.create_project(self.actor, bad)
                )

    async def test_create_project_description_must_be_a_clean_bounded_string(self):
        for bad, problem in DESCRIPTIONS:
            with self.subTest(bad=repr(bad)):
                await self.assertRefusedBeforeTheDecision(
                    "description",
                    problem,
                    self.service.create_project(self.actor, "Alpha", bad),
                )

    async def test_the_name_is_checked_before_the_description(self):
        await self.assertRefusedBeforeTheDecision(
            "name",
            InputProblem.EMPTY,
            self.service.create_project(self.actor, "", "d" * 2001),
        )

    async def test_valid_names_and_descriptions_are_decided_then_reach_the_database(
        self,
    ):
        for name, description in (
            ("Alpha", None),
            ("a" * 100, "d" * 2000),
            ("  padded  ", "   "),
            ("日本語の名前", "説明\n二行目"),
        ):
            with self.subTest(name=name[:10], description=repr(description)[:10]):
                await self.assertDecidedThenReachesTheDatabase(
                    self.service.create_project(self.actor, name, description),
                    "project.create",
                )

    async def test_unknown_or_misspelled_fields_are_rejected(self):
        for call in (
            lambda: self.service.create_project(self.actor, "Alpha", descripton="x"),
            lambda: self.service.create_project(self.actor, "Alpha", role="manager"),
            lambda: self.service.accept_invite(self.actor, PID, user_id=PID),
            lambda: self.service.decline_invite(self.actor, PID, force=True),
            lambda: self.service.leave_project(self.actor, PID, project=PID),
            lambda: self.service.leave_project(self.actor, PID, PID),
        ):
            with self.subTest(call=call.__code__.co_firstlineno):
                with self.assertRaises(TypeError):
                    call()
        self.assertEqual(self.sink.events, [])

    # -- the actor ---------------------------------------------------------------------

    def actor_calls(self):
        return {
            "create_project": lambda a: self.service.create_project(a, "Alpha"),
            "accept_invite": lambda a: self.service.accept_invite(a, PID),
            "decline_invite": lambda a: self.service.decline_invite(a, PID),
            "leave_project": lambda a: self.service.leave_project(a, PID),
        }

    async def test_something_that_is_not_a_principal_is_unauthenticated(self):
        bad_actors = (
            None,
            "user",
            {"user_id": str(uuid.uuid4()), "system_role": "owner"},
            uuid.uuid4(),
            object(),
        )
        for method, call in self.actor_calls().items():
            for bad in bad_actors:
                with self.subTest(method=method, actor=type(bad).__name__):
                    with self.assertRaises(ProjectPermissionDeniedError) as raised:
                        await call(bad)
                    self.assertIs(raised.exception.reason, Reason.UNAUTHENTICATED)
        # An unauthenticated denial is never an audit row.
        self.assertEqual(self.sink.events, [])

    async def test_the_actor_is_checked_before_the_arguments(self):
        with self.assertRaises(ProjectPermissionDeniedError):
            await self.service.create_project(None, "")
        with self.assertRaises(ProjectPermissionDeniedError):
            await self.service.leave_project("nobody", "not-a-uuid")
        self.assertEqual(self.sink.events, [])

    async def test_the_internal_identity_is_denied_and_recorded_without_the_database(
        self,
    ):
        system = Principal(uuid.uuid4(), SystemRole.SYSTEM)
        for method, call in self.actor_calls().items():
            with self.subTest(method=method):
                self.sink.events.clear()
                with self.assertRaises(ProjectPermissionDeniedError) as raised:
                    await call(system)
                self.assertIs(raised.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
                (event,) = self.sink.events
                self.assertEqual(
                    (event.decision, event.reason, event.actor_id, event.actor_role),
                    ("deny", "capability_not_granted", system.user_id, "system"),
                )

    async def test_the_callers_project_roles_are_not_passed_to_the_authorizer(self):
        seen = []

        class Spy(Authorizer):
            async def authorize(self, principal, capability, resource, **kwargs):
                seen.append((principal, capability, resource))
                return await super().authorize(
                    principal, capability, resource, **kwargs
                )

        service = ProjectService(
            Database(make_settings()), Spy(self.sink), clock=lambda: T0
        )
        claimed = Principal(
            self.actor.user_id, SystemRole.USER, {PID: ProjectRole.MANAGER}
        )
        for call in (
            lambda: service.create_project(claimed, "Alpha"),
            lambda: service.accept_invite(claimed, PID),
            lambda: service.decline_invite(claimed, PID),
            lambda: service.leave_project(claimed, PID),
        ):
            with self.assertRaises(DatabaseNotConfiguredError):
                await call()
        self.assertEqual(len(seen), 4)
        for principal, _, _ in seen:
            self.assertEqual(dict(principal.project_roles), {})
            self.assertEqual(
                (principal.user_id, principal.system_role),
                (self.actor.user_id, SystemRole.USER),
            )

    async def test_errors_never_echo_the_rejected_value(self):
        secret = "sk_" + "live_SECRET-0123456789"
        for awaitable in (
            self.service.create_project(self.actor, secret + "\x00"),
            self.service.create_project(self.actor, "a" * 101 + secret),
            self.service.create_project(self.actor, "Alpha", secret * 500),
            self.service.accept_invite(self.actor, secret),
            self.service.decline_invite(self.actor, secret),
            self.service.leave_project(self.actor, secret),
        ):
            with self.assertRaises(InvalidProjectInputError) as raised:
                await awaitable
            text = str(raised.exception) + repr(raised.exception.args)
            self.assertNotIn(secret, text)
        self.assertEqual(self.sink.events, [])


if __name__ == "__main__":
    unittest.main()
