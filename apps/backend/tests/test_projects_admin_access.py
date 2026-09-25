"""``list_all_projects``: the actor, the arguments and the audit come first (Issue #84).

The service here sits on a ``Database`` without a URL: any attempt to read
raises ``DatabaseNotConfiguredError``. So an error that is reported instead
proves that **no read was attempted**, which is what these tests need to show:

* a caller who is not a ``Principal`` (an agent, a token, ``None``) and a caller
  with a bad argument make no Authorizer decision, so they write no Audit event;
* every argument of the method is validated, table-driven, before the Authorizer
  and the database;
* a denial (User, the ``system`` identity) writes exactly one Audit event and
  reads nothing; an Audit store that is down denies an Owner and reads nothing;
* an Owner or Admin writes exactly one event, whose resource names the filter
  and holds no project id, name or cursor.

No PostgreSQL is needed.
"""

import json
import unittest
from decimal import Decimal
from uuid import uuid4

from paw_backend.authz import (
    ALL_PROJECTS,
    AgentGrant,
    Authorizer,
    Capability,
    InMemoryAuditSink,
    Principal,
    ProjectRole,
    Reason,
    Resource,
    SystemRole,
)
from paw_backend.db import Database, DatabaseNotConfiguredError
from paw_backend.projects import (
    InputProblem,
    InvalidProjectInputError,
    ProjectPermissionDeniedError,
    ProjectService,
    ProjectStatus,
)
from paw_backend.projects.cursor import encode_cursor

from .authz_support import FailingSink, StaticDirectory
from .projects_support import T0
from .support import make_settings

CANARY = "CANARY-4f2a9c"
CURSOR = encode_cursor(None, T0, uuid4())
S = ProjectStatus


class Sub(str):
    """A ``str`` subclass: not an exact ``str``."""


class AdminListAccessTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sink = InMemoryAuditSink()
        self.service = ProjectService(
            Database(make_settings()), Authorizer(self.sink), clock=lambda: T0
        )
        self.owner = Principal(uuid4(), SystemRole.OWNER)
        self.admin = Principal(uuid4(), SystemRole.ADMIN)

    async def assertRejected(self, field, problem, awaitable):
        with self.assertRaises(InvalidProjectInputError) as raised:
            await awaitable
        self.assertEqual(
            (raised.exception.field, raised.exception.problem), (field, problem)
        )
        self.assertNotIn(CANARY, str(raised.exception))
        # No decision was taken: nothing was audited and nothing was read.
        self.assertEqual(self.sink.events, [])

    async def assertReadsTheDatabase(self, awaitable):
        with self.assertRaises(DatabaseNotConfiguredError):
            await awaitable

    # -- the actor ------------------------------------------------------------------

    async def test_only_a_principal_can_call_it_and_nothing_is_audited(self):
        grant = AgentGrant(
            uuid4(), frozenset({Capability.ADMIN_PROJECTS_MANAGE}), ALL_PROJECTS
        )
        for bad in (
            None,
            "owner",
            str(uuid4()),
            uuid4(),
            5,
            {"user_id": str(uuid4()), "system_role": "owner"},
            object(),
            grant,  # an agent: it is not a Principal and never becomes one
            (self.owner.user_id, SystemRole.OWNER),
        ):
            with self.subTest(actor=type(bad).__name__):
                with self.assertRaises(ProjectPermissionDeniedError) as raised:
                    await self.service.list_all_projects(bad)
                self.assertIs(raised.exception.reason, Reason.UNAUTHENTICATED)
        self.assertEqual(self.sink.events, [])

    async def test_the_actor_is_checked_before_the_arguments(self):
        with self.assertRaises(ProjectPermissionDeniedError):
            await self.service.list_all_projects(None, status="x", limit=0, cursor=5)
        self.assertEqual(self.sink.events, [])

    async def test_an_agent_acting_for_an_owner_is_denied_and_audited(self):
        # ``admin.projects.manage`` is not delegable: even a grant that names it,
        # for an agent whose Owner may do it, is refused by the Authorizer.
        agent_id = uuid4()
        grant = AgentGrant(
            agent_id, frozenset({Capability.ADMIN_PROJECTS_MANAGE}), ALL_PROJECTS
        )
        authorizer = Authorizer(self.sink, directory=StaticDirectory(self.owner))
        decision = await authorizer.authorize_agent_action(
            self.owner.user_id,
            grant,
            Capability.ADMIN_PROJECTS_MANAGE,
            Resource(kind="project_list_all"),
        )
        self.assertFalse(decision.allowed)
        self.assertIs(decision.reason, Reason.AGENT_CAPABILITY_FORBIDDEN)
        (event,) = self.sink.events
        self.assertEqual(
            (event.decision, event.agent_id, event.actor_id, event.action),
            ("deny", agent_id, self.owner.user_id, "admin.projects.manage"),
        )

    # -- the arguments --------------------------------------------------------------

    async def test_every_bad_argument_is_refused_before_anything_else(self):
        cases = {
            "status": (
                InputProblem.NOT_A_STATUS,
                [
                    "ACTIVE",
                    "Archived",
                    " active",
                    "active ",
                    "active\n",
                    "active\x00",
                    "",
                    "pending-deletion",
                    "pending deletion",
                    "all",
                    "unknown",
                    0,
                    1,
                    True,
                    False,
                    1.5,
                    b"active",
                    ["active"],
                    ("active",),
                    {"active"},
                    object(),
                    Sub("active"),
                    CANARY,
                ],
            ),
            "limit": (
                InputProblem.NOT_AN_INTEGER,
                [
                    None,
                    True,
                    False,
                    1.0,
                    1.5,
                    float("nan"),
                    float("inf"),
                    Decimal(10),
                    "10",
                    b"10",
                    [10],
                    object(),
                ],
            ),
            "cursor": (
                InputProblem.NOT_A_STRING,
                [b"", CURSOR.encode(), 0, 1, True, False, 1.5, [CURSOR], {}, object()],
            ),
        }
        for field, (problem, values) in cases.items():
            for bad in values:
                with self.subTest(field=field, bad=repr(bad)):
                    await self.assertRejected(
                        field,
                        problem,
                        self.service.list_all_projects(self.owner, **{field: bad}),
                    )

    async def test_out_of_range_values_are_refused(self):
        await self.assertRejected(
            "status",
            InputProblem.OUT_OF_RANGE,
            self.service.list_all_projects(self.owner, status=S.DELETED),
        )
        await self.assertRejected(
            "status",
            InputProblem.OUT_OF_RANGE,
            self.service.list_all_projects(self.owner, status="deleted"),
        )
        for bad in (0, -1, 201, 10**30, -(10**30)):
            with self.subTest(limit=bad):
                await self.assertRejected(
                    "limit",
                    InputProblem.OUT_OF_RANGE,
                    self.service.list_all_projects(self.owner, limit=bad),
                )

    async def test_a_bad_cursor_is_refused(self):
        for bad, problem in (
            ("", InputProblem.INVALID_CURSOR),
            (" ", InputProblem.INVALID_CURSOR),
            ("not a cursor", InputProblem.INVALID_CURSOR),
            ("A" * 100, InputProblem.TOO_LONG),
            (CURSOR + "\n", InputProblem.INVALID_CURSOR),
            ("\x00", InputProblem.INVALID_CURSOR),
            ("'; DROP TABLE projects; --", InputProblem.INVALID_CURSOR),
        ):
            with self.subTest(cursor=bad):
                await self.assertRejected(
                    "cursor",
                    problem,
                    self.service.list_all_projects(self.owner, cursor=bad),
                )

    async def test_a_cursor_of_another_filter_is_refused(self):
        cursor = encode_cursor(S.ARCHIVED, T0, uuid4())
        for status in (None, S.ACTIVE, S.PENDING_DELETION, "active"):
            with self.subTest(status=status):
                await self.assertRejected(
                    "cursor",
                    InputProblem.INVALID_CURSOR,
                    self.service.list_all_projects(
                        self.owner, status=status, cursor=cursor
                    ),
                )
        await self.assertReadsTheDatabase(
            self.service.list_all_projects(self.owner, status=S.ARCHIVED, cursor=cursor)
        )

    async def test_arguments_are_checked_in_signature_order(self):
        await self.assertRejected(
            "status",
            InputProblem.NOT_A_STATUS,
            self.service.list_all_projects(self.owner, status="x", limit=0, cursor=5),
        )
        await self.assertRejected(
            "limit",
            InputProblem.OUT_OF_RANGE,
            self.service.list_all_projects(self.owner, limit=0, cursor=5),
        )
        await self.assertRejected(
            "cursor",
            InputProblem.NOT_A_STRING,
            self.service.list_all_projects(self.owner, cursor=5),
        )

    async def test_the_arguments_are_checked_before_the_authorizer(self):
        # A User (who would be denied) with a bad argument gets the input error and
        # no decision is made: the order of the checks of every method.
        user = Principal(uuid4(), SystemRole.USER)
        await self.assertRejected(
            "limit",
            InputProblem.OUT_OF_RANGE,
            self.service.list_all_projects(user, limit=0),
        )

    async def test_unknown_keywords_are_not_accepted(self):
        for name in ("offset", "Limit", "statuses", "project_id", "page"):
            with self.subTest(name=name):
                with self.assertRaises(TypeError):
                    await self.service.list_all_projects(self.owner, **{name: 1})
        with self.assertRaises(TypeError):
            await self.service.list_all_projects(self.owner, S.ACTIVE)  # keyword only
        self.assertEqual(self.sink.events, [])

    async def test_the_extreme_valid_arguments_pass_to_the_database(self):
        for kwargs in (
            {},
            {"limit": 1},
            {"limit": 200},
            {"status": S.ACTIVE},
            {"status": "archived"},
            {"status": S.PENDING_DELETION, "limit": 200, "cursor": None},
            {"cursor": CURSOR},
        ):
            with self.subTest(kwargs=kwargs):
                await self.assertReadsTheDatabase(
                    self.service.list_all_projects(self.owner, **kwargs)
                )

    # -- the decision ---------------------------------------------------------------

    async def test_an_owner_and_an_admin_are_allowed_and_audited_once(self):
        for actor in (self.owner, self.admin):
            self.sink.events.clear()
            with self.subTest(role=actor.system_role):
                await self.assertReadsTheDatabase(self.service.list_all_projects(actor))
                (event,) = self.sink.events
                self.assertEqual(
                    (
                        event.decision,
                        event.reason,
                        event.action,
                        event.actor_id,
                        event.actor_role,
                        event.agent_id,
                    ),
                    (
                        "allow",
                        "granted_by_system_role",
                        "admin.projects.manage",
                        actor.user_id,
                        actor.system_role.value,
                        None,
                    ),
                )

    async def test_the_event_names_the_filter_and_nothing_of_the_result(self):
        for status, kind in (
            (None, "project_list_all"),
            (S.ACTIVE, "project_list_active"),
            ("archived", "project_list_archived"),
            (S.PENDING_DELETION, "project_list_pending_deletion"),
        ):
            self.sink.events.clear()
            with self.subTest(status=status):
                cursor = encode_cursor(
                    None if status is None else S(status), T0, uuid4()
                )
                await self.assertReadsTheDatabase(
                    self.service.list_all_projects(
                        self.owner, status=status, limit=7, cursor=cursor
                    )
                )
                (event,) = self.sink.events
                self.assertEqual(event.resource_kind, kind)
                # No project, repository, cursor or count is in the event.
                self.assertEqual(
                    (event.resource_id, event.project_id, event.repo_id), (None,) * 3
                )
                text = json.dumps(event.model_dump(mode="json"))
                self.assertNotIn(cursor, text)

    async def test_a_user_is_denied_with_one_audited_event_and_reads_nothing(self):
        for role in (SystemRole.USER, SystemRole.SYSTEM):
            self.sink.events.clear()
            with self.subTest(role=role):
                actor = Principal(uuid4(), role)
                with self.assertRaises(ProjectPermissionDeniedError) as raised:
                    await self.service.list_all_projects(actor)
                # Not DatabaseNotConfiguredError: the database was never touched.
                self.assertIs(raised.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
                (event,) = self.sink.events
                self.assertEqual(
                    (
                        event.decision,
                        event.reason,
                        event.action,
                        event.actor_id,
                        event.actor_role,
                        event.resource_kind,
                    ),
                    (
                        "deny",
                        "capability_not_granted",
                        "admin.projects.manage",
                        actor.user_id,
                        role.value,
                        "project_list_all",
                    ),
                )

    async def test_being_a_manager_of_projects_does_not_grant_it(self):
        # ``project_roles`` on the Principal are ignored; the capability is a
        # system-role one.
        actor = Principal(uuid4(), SystemRole.USER, {uuid4(): ProjectRole.MANAGER})
        with self.assertRaises(ProjectPermissionDeniedError) as raised:
            await self.service.list_all_projects(actor)
        self.assertIs(raised.exception.reason, Reason.CAPABILITY_NOT_GRANTED)

    async def test_an_audit_store_that_is_down_denies_and_reads_nothing(self):
        sink = FailingSink()
        service = ProjectService(
            Database(make_settings()), Authorizer(sink), clock=lambda: T0
        )
        for actor in (self.owner, self.admin):
            with self.subTest(role=actor.system_role):
                with self.assertRaises(ProjectPermissionDeniedError) as raised:
                    await service.list_all_projects(actor)
                self.assertIs(raised.exception.reason, Reason.AUDIT_UNAVAILABLE)
        self.assertEqual(sink.attempts, 2)

    async def test_each_call_is_one_event(self):
        for _ in range(3):
            await self.assertReadsTheDatabase(
                self.service.list_all_projects(self.owner)
            )
        self.assertEqual(len(self.sink.events), 3)
        self.assertEqual(len({e.correlation_id for e in self.sink.events}), 3)


if __name__ == "__main__":
    unittest.main()
