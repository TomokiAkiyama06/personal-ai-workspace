"""Audit of creating, answering an invitation and leaving (real PostgreSQL).

Decision 0022 (Proposed): ``project.create``, ``project.invitation.respond`` and
``project.leave`` go through the Authorizer with the audit mode ``REQUIRED``. The
events are written to the real ``audit_events`` table by ``PostgresAuditSink`` (its
own short transaction, not the request's), and the tests read them back with SQL.

What must hold, for each of the four methods (``create_project``,
``accept_invite``, ``decline_invite``, ``leave_project``):

* **allowed**: the state changes and exactly one ``allow`` row exists;
* **denied**: nothing changes and exactly one ``deny`` row exists per call;
* **audit down**: the operation is refused (``audit_unavailable``) and nothing
  changes, and there is no row (it could not be written);
* **decision first**: when the row is written, the state is still the old one and
  the project row is not locked (the decision precedes the transaction);
* **decision, not outcome**: an allowed attempt that a rule then refuses (no
  invitation, expired, last Manager, lock timeout) keeps its ``allow`` row and
  changes nothing. A state change without an ``allow`` row never happens.

``audit_events`` is append-only (it cannot be truncated), so every test reads only
the rows of the users it created (random ids).
"""

import asyncio
import unittest
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from paw_backend.authz import (
    ALL_PROJECTS,
    AgentGrant,
    Authorizer,
    Capability,
    InMemoryAuditSink,
    PostgresAuditSink,
    Principal,
    Resource,
    SystemRole,
)
from paw_backend.authz.policy import Reason
from paw_backend.authz.roles import ProjectRole
from paw_backend.db import Database
from paw_backend.projects import (
    InvalidProjectInputError,
    InviteExpiredError,
    InviteNotFoundError,
    LastManagerError,
    MemberStatus,
    ProjectBusyError,
    ProjectNotFoundError,
    ProjectPermissionDeniedError,
    ProjectService,
)
from paw_backend.projects.limits import INVITE_TTL

from .authz_support import StaticDirectory
from .memory_support import TEST_DATABASE_URL
from .projects_support import T0, PostgresProjectTestCase, requires_postgres
from .support import make_settings

INVITED = MemberStatus.INVITED
MANAGER, CONTRIBUTOR, VIEWER = (
    ProjectRole.MANAGER,
    ProjectRole.CONTRIBUTOR,
    ProjectRole.VIEWER,
)
RESPOND, LEAVE, CREATE = (
    "project.invitation.respond",
    "project.leave",
    "project.create",
)
AUTHORIZER_LOGGER = "paw_backend.authz.authorizer"


class ProbeSink:
    """Runs ``probe`` (in a thread) each time an event is about to be written.

    It is how a test observes *when* the decision is recorded relative to the
    change it guards: ``seen`` holds what the database looked like at that moment.
    """

    def __init__(self, inner, probe):
        self.inner, self.probe, self.seen = inner, probe, []

    async def record(self, event) -> None:
        self.seen.append(await asyncio.to_thread(self.probe))
        await self.inner.record(event)


class SelfServiceAuditTestCase(PostgresProjectTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.audit_database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(self.audit_database.dispose)

    # -- services ------------------------------------------------------------------

    def real_sink(self) -> PostgresAuditSink:
        return PostgresAuditSink(self.audit_database)

    def unreachable_sink(self) -> PostgresAuditSink:
        """A real sink whose database refuses the connection (an audit store down)."""
        url = make_url(TEST_DATABASE_URL).set(port=1)
        database = Database(
            make_settings(database_url=url.render_as_string(hide_password=False))
        )
        self.addAsyncCleanup(database.dispose)
        return PostgresAuditSink(database)

    def service_with(self, sink, *, clock=None, **options) -> ProjectService:
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        return ProjectService(
            database,
            Authorizer(sink, clock=self.clock),
            clock=clock or self.clock,
            **options,
        )

    # -- reading the audit table -------------------------------------------------

    def audit_rows(self, actor_id: UUID) -> list[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT action, decision, reason, resource_kind, resource_id,"
                    " project_id, actor_id, actor_role, agent_id, occurred_at"
                    " FROM audit_events WHERE actor_id = :actor"
                    " ORDER BY recorded_at, id"
                ),
                {"actor": actor_id},
            )
            return [dict(row) for row in rows.mappings()]

    def summary(self, actor_id: UUID) -> list[tuple[str, str, str]]:
        return [
            (row["action"], row["decision"], row["reason"])
            for row in self.audit_rows(actor_id)
        ]

    # -- probes (sync, run from a thread) -----------------------------------------

    def project_lock_is_free(self, project_id: UUID) -> bool:
        """True if the project row can be locked at once (nobody holds it)."""
        with self.engine.connect() as connection, connection.begin():
            try:
                connection.execute(
                    text("SELECT id FROM projects WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": project_id},
                )
            except DBAPIError as error:
                if isinstance(error.orig, psycopg.errors.LockNotAvailable):
                    return False
                raise
            return True

    def member_status(self, project_id: UUID, user_id: UUID) -> str | None:
        row = self.member_row(project_id, user_id)
        return None if row is None else row["status"]

    def projects_created_by(self, user_id: UUID) -> int:
        with self.engine.connect() as connection:
            return connection.execute(
                text("SELECT count(*) FROM projects WHERE created_by = :u"),
                {"u": user_id},
            ).scalar_one()

    # -- seeding -------------------------------------------------------------------

    def seed_invitation(self, project_id: UUID, **values) -> UUID:
        return self.seed_member(project_id, status=INVITED, role=VIEWER, **values)

    def make_pending_deletion(self, project_id: UUID) -> None:
        self.set_project(
            project_id,
            status="pending_deletion",
            deletion_started_at=T0,
            deletion_scheduled_at=T0 + timedelta(days=30),
        )


@requires_postgres
class ProbeControlTest(SelfServiceAuditTestCase):
    """The probes below can fail: they see a held lock and a changed row."""

    async def test_the_lock_probe_sees_a_lock_that_is_held(self):
        project_id = self.seed_project()
        self.assertTrue(self.project_lock_is_free(project_id))
        self.lock_project(project_id)
        self.assertFalse(self.project_lock_is_free(project_id))

    async def test_the_audit_reader_returns_only_the_rows_of_one_actor(self):
        one, other = self.seed_user(), self.seed_user()
        service = self.service_with(self.real_sink())
        await service.create_project(self.actor(one), "One")
        self.assertEqual(
            self.summary(one), [(CREATE, "allow", "granted_by_system_role")]
        )
        self.assertEqual(self.summary(other), [])


@requires_postgres
class CreateProjectAuditTest(SelfServiceAuditTestCase):
    async def test_every_human_role_gets_one_allow_row_and_a_project(self):
        service = self.service_with(self.real_sink())
        for role in (SystemRole.OWNER, SystemRole.ADMIN, SystemRole.USER):
            with self.subTest(role=role.value):
                user = self.seed_user()
                project = await service.create_project(
                    self.actor(user, role), f"By {role.value}"
                )
                (row,) = self.audit_rows(user)
                self.assertEqual(
                    (
                        row["action"],
                        row["decision"],
                        row["reason"],
                        row["actor_role"],
                        row["resource_kind"],
                    ),
                    (CREATE, "allow", "granted_by_system_role", role.value, "system"),
                )
                self.assertEqual((row["resource_id"], row["project_id"]), (None, None))
                self.assertEqual(row["occurred_at"], T0)
                self.assertEqual(self.project_row(project.id)["created_by"], user)

    async def test_the_decision_is_recorded_before_the_project_exists(self):
        user = self.seed_user()
        probe = ProbeSink(self.real_sink(), lambda: self.projects_created_by(user))
        service = self.service_with(probe)
        await service.create_project(self.actor(user), "Alpha")
        self.assertEqual(probe.seen, [0])
        self.assertEqual(self.projects_created_by(user), 1)

    async def test_the_internal_identity_is_denied_once_per_call_and_creates_nothing(
        self,
    ):
        user = self.seed_user()
        service = self.service_with(self.real_sink())
        for _ in range(3):
            with self.assertRaises(ProjectPermissionDeniedError) as caught:
                await service.create_project(
                    self.actor(user, SystemRole.SYSTEM), "Alpha"
                )
            self.assertIs(caught.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
        self.assertEqual(
            self.summary(user), [(CREATE, "deny", "capability_not_granted")] * 3
        )
        self.assertEqual(self.audit_rows(user)[0]["actor_role"], "system")
        self.assertEqual(self.table_count("projects"), 0)
        self.assertEqual(self.table_count("project_members"), 0)

    async def test_a_create_that_cannot_be_audited_is_refused_and_creates_nothing(self):
        user = self.seed_user()
        service = self.service_with(self.unreachable_sink())
        with self.assertLogs(AUTHORIZER_LOGGER, level="ERROR") as logs:
            with self.assertRaises(ProjectPermissionDeniedError) as caught:
                await service.create_project(self.actor(user), "Alpha")
        self.assertIs(caught.exception.reason, Reason.AUDIT_UNAVAILABLE)
        # The type of the driver's error only, never its text or the host.
        self.assertIn("OperationalError", "\n".join(logs.output))
        self.assertEqual(self.table_count("projects"), 0)
        self.assertEqual(self.table_count("project_members"), 0)
        self.assertEqual(self.audit_rows(user), [])

    async def test_a_rejected_argument_writes_no_row(self):
        user = self.seed_user()
        service = self.service_with(self.real_sink())
        for bad in ("", "a" * 101, None):
            with self.assertRaises(InvalidProjectInputError):
                await service.create_project(self.actor(user), bad)
        self.assertEqual(self.audit_rows(user), [])
        self.assertEqual(self.table_count("projects"), 0)

    async def test_the_clock_is_read_after_the_decision(self):
        user = self.seed_user()
        order = []

        class OrderSink(InMemoryAuditSink):
            async def record(self, event):
                order.append("record")
                await super().record(event)

        service = self.service_with(
            OrderSink(), clock=lambda: order.append("clock") or T0
        )
        await service.create_project(self.actor(user), "Alpha")
        self.assertEqual(order, ["record", "clock"])


@requires_postgres
class InvitationAuditTestCase(SelfServiceAuditTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha")
        self.team = self.seed_team(self.project_id)
        self.invitee = self.seed_invitation(self.project_id)
        self.me = self.actor(self.invitee)


@requires_postgres
class AcceptInviteAuditTest(InvitationAuditTestCase):
    async def test_an_accepted_invitation_leaves_one_allow_row_naming_the_project(self):
        service = self.service_with(self.real_sink())
        member = await service.accept_invite(self.me, self.project_id)
        self.assertIs(member.status, MemberStatus.ACTIVE)
        (row,) = self.audit_rows(self.invitee)
        self.assertEqual(
            (
                row["action"],
                row["decision"],
                row["reason"],
                row["resource_kind"],
                row["project_id"],
                row["resource_id"],
                row["actor_role"],
                row["agent_id"],
            ),
            (
                RESPOND,
                "allow",
                "granted_to_resource_owner",
                "project_invitation",
                self.project_id,
                None,
                "user",
                None,
            ),
        )

    async def test_the_decision_is_recorded_before_the_change_and_outside_the_lock(
        self,
    ):
        probe = ProbeSink(
            self.real_sink(),
            lambda: (
                self.member_status(self.project_id, self.invitee),
                self.project_lock_is_free(self.project_id),
            ),
        )
        service = self.service_with(probe)
        await service.accept_invite(self.me, self.project_id)
        # Still invited, and nobody held the project row, when the event was written.
        self.assertEqual(probe.seen, [("invited", True)])
        self.assertEqual(self.member_status(self.project_id, self.invitee), "active")

    async def test_a_denied_accept_changes_nothing_and_is_recorded_each_time(self):
        service = self.service_with(self.real_sink())
        before = self.snapshot()
        system = self.actor(self.invitee, SystemRole.SYSTEM)
        for _ in range(2):
            with self.assertRaises(ProjectPermissionDeniedError) as caught:
                await service.accept_invite(system, self.project_id)
            self.assertIs(caught.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            self.summary(self.invitee),
            [(RESPOND, "deny", "capability_not_granted")] * 2,
        )

    async def test_a_denial_does_not_reveal_whether_the_invitation_exists(self):
        service = self.service_with(self.real_sink())
        stranger = self.seed_user()
        for user, project_id in (
            (self.invitee, self.project_id),  # an invitation exists
            (stranger, self.project_id),  # none for this user
            (stranger, uuid4()),  # no such project
        ):
            with self.subTest(project=project_id == self.project_id):
                system = self.actor(user, SystemRole.SYSTEM)
                with self.assertRaises(ProjectPermissionDeniedError) as caught:
                    await service.accept_invite(system, project_id)
                self.assertIs(caught.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
                self.assertEqual(
                    self.summary(user)[-1], (RESPOND, "deny", "capability_not_granted")
                )

    async def test_an_accept_that_cannot_be_audited_is_refused_and_changes_nothing(
        self,
    ):
        service = self.service_with(self.unreachable_sink())
        before = self.snapshot()
        with self.assertLogs(AUTHORIZER_LOGGER, level="ERROR"):
            with self.assertRaises(ProjectPermissionDeniedError) as caught:
                await service.accept_invite(self.me, self.project_id)
        self.assertIs(caught.exception.reason, Reason.AUDIT_UNAVAILABLE)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.member_status(self.project_id, self.invitee), "invited")
        self.assertEqual(self.audit_rows(self.invitee), [])

    async def test_an_allowed_attempt_without_an_invitation_keeps_its_allow_row(self):
        service = self.service_with(self.real_sink())
        stranger = self.seed_user()
        before = self.snapshot()
        for project_id in (self.project_id, uuid4()):
            with self.assertRaises(InviteNotFoundError):
                await service.accept_invite(self.actor(stranger), project_id)
        self.assertEqual(self.snapshot(), before)
        # The event is the decision for the attempt, not its outcome.
        self.assertEqual(
            self.summary(stranger),
            [(RESPOND, "allow", "granted_to_resource_owner")] * 2,
        )

    async def test_an_expired_invitation_stays_and_the_attempt_stays_recorded(self):
        service = self.service_with(self.real_sink())
        self.clock.now = T0 + INVITE_TTL
        with self.assertRaises(InviteExpiredError):
            await service.accept_invite(self.me, self.project_id)
        self.assertEqual(self.member_status(self.project_id, self.invitee), "invited")
        self.assertEqual(
            self.summary(self.invitee),
            [(RESPOND, "allow", "granted_to_resource_owner")],
        )

    async def test_the_clock_is_read_after_the_decision(self):
        order = []

        class OrderSink(InMemoryAuditSink):
            async def record(self, event):
                order.append("record")
                await super().record(event)

        service = self.service_with(
            OrderSink(), clock=lambda: order.append("clock") or T0
        )
        await service.accept_invite(self.me, self.project_id)
        self.assertEqual(order, ["record", "clock"])


@requires_postgres
class DeclineInviteAuditTest(InvitationAuditTestCase):
    async def test_a_declined_invitation_leaves_one_allow_row_with_the_same_action(
        self,
    ):
        service = self.service_with(self.real_sink())
        self.assertIsNone(await service.decline_invite(self.me, self.project_id))
        self.assertIsNone(self.member_row(self.project_id, self.invitee))
        (row,) = self.audit_rows(self.invitee)
        self.assertEqual(
            (row["action"], row["decision"], row["resource_kind"], row["project_id"]),
            (RESPOND, "allow", "project_invitation", self.project_id),
        )

    async def test_the_decision_is_recorded_before_the_row_is_deleted_outside_the_lock(
        self,
    ):
        probe = ProbeSink(
            self.real_sink(),
            lambda: (
                self.member_status(self.project_id, self.invitee),
                self.project_lock_is_free(self.project_id),
            ),
        )
        service = self.service_with(probe)
        await service.decline_invite(self.me, self.project_id)
        self.assertEqual(probe.seen, [("invited", True)])

    async def test_a_denied_decline_keeps_the_invitation_and_is_recorded_each_time(
        self,
    ):
        service = self.service_with(self.real_sink())
        system = self.actor(self.invitee, SystemRole.SYSTEM)
        for _ in range(2):
            with self.assertRaises(ProjectPermissionDeniedError):
                await service.decline_invite(system, self.project_id)
        self.assertEqual(self.member_status(self.project_id, self.invitee), "invited")
        self.assertEqual(
            self.summary(self.invitee),
            [(RESPOND, "deny", "capability_not_granted")] * 2,
        )

    async def test_a_decline_that_cannot_be_audited_is_refused_and_keeps_the_row(self):
        service = self.service_with(self.unreachable_sink())
        with self.assertLogs(AUTHORIZER_LOGGER, level="ERROR"):
            with self.assertRaises(ProjectPermissionDeniedError) as caught:
                await service.decline_invite(self.me, self.project_id)
        self.assertIs(caught.exception.reason, Reason.AUDIT_UNAVAILABLE)
        self.assertEqual(self.member_status(self.project_id, self.invitee), "invited")
        self.assertEqual(self.audit_rows(self.invitee), [])

    async def test_declining_twice_records_both_attempts_but_deletes_once(self):
        service = self.service_with(self.real_sink())
        await service.decline_invite(self.me, self.project_id)
        with self.assertRaises(InviteNotFoundError):
            await service.decline_invite(self.me, self.project_id)
        self.assertEqual(
            self.summary(self.invitee),
            [(RESPOND, "allow", "granted_to_resource_owner")] * 2,
        )


@requires_postgres
class LeaveProjectAuditTest(SelfServiceAuditTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha")
        self.team = self.seed_team(self.project_id)

    async def test_a_leave_writes_one_allow_row_naming_the_project(self):
        service = self.service_with(self.real_sink())
        await service.leave_project(self.actor(self.team.viewer), self.project_id)
        self.assertIsNone(self.member_row(self.project_id, self.team.viewer))
        (row,) = self.audit_rows(self.team.viewer)
        self.assertEqual(
            (
                row["action"],
                row["decision"],
                row["reason"],
                row["resource_kind"],
                row["project_id"],
                row["resource_id"],
            ),
            (
                LEAVE,
                "allow",
                "granted_to_resource_owner",
                "project_membership",
                self.project_id,
                None,
            ),
        )

    async def test_the_decision_is_recorded_before_the_row_is_deleted_outside_the_lock(
        self,
    ):
        probe = ProbeSink(
            self.real_sink(),
            lambda: (
                self.member_status(self.project_id, self.team.contributor),
                self.project_lock_is_free(self.project_id),
            ),
        )
        service = self.service_with(probe)
        await service.leave_project(self.actor(self.team.contributor), self.project_id)
        self.assertEqual(probe.seen, [("active", True)])
        self.assertIsNone(self.member_row(self.project_id, self.team.contributor))

    async def test_every_role_can_leave_in_every_state_and_each_is_recorded(self):
        states = {
            "active": lambda pid: None,
            "archived": lambda pid: self.set_project(pid, status="archived"),
            "pending_deletion": self.make_pending_deletion,
        }
        service = self.service_with(self.real_sink())
        for state, prepare in states.items():
            for role in (VIEWER, CONTRIBUTOR, MANAGER):
                with self.subTest(state=state, role=role.value):
                    project_id = self.seed_project(name=state)
                    prepare(project_id)
                    self.seed_manager(project_id)  # someone else keeps managing
                    user = self.seed_member(project_id, role=role)
                    await service.leave_project(self.actor(user), project_id)
                    self.assertIsNone(self.member_row(project_id, user))
                    self.assertEqual(
                        self.summary(user),
                        [(LEAVE, "allow", "granted_to_resource_owner")],
                    )

    async def test_a_denied_leave_keeps_the_membership_and_is_recorded_each_time(self):
        service = self.service_with(self.real_sink())
        before = self.snapshot()
        system = self.actor(self.team.viewer, SystemRole.SYSTEM)
        for _ in range(2):
            with self.assertRaises(ProjectPermissionDeniedError) as caught:
                await service.leave_project(system, self.project_id)
            self.assertIs(caught.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            self.summary(self.team.viewer),
            [(LEAVE, "deny", "capability_not_granted")] * 2,
        )

    async def test_a_denial_does_not_reveal_whether_the_project_exists(self):
        service = self.service_with(self.real_sink())
        outsider = self.seed_user()
        for user, project_id in (
            (self.team.viewer, self.project_id),  # a member
            (outsider, self.project_id),  # not a member
            (outsider, uuid4()),  # no such project
        ):
            system = self.actor(user, SystemRole.SYSTEM)
            with self.assertRaises(ProjectPermissionDeniedError) as caught:
                await service.leave_project(system, project_id)
            self.assertIs(caught.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
        self.assertEqual(
            self.member_status(self.project_id, self.team.viewer), "active"
        )

    async def test_a_leave_that_cannot_be_audited_is_refused_and_keeps_the_member(self):
        service = self.service_with(self.unreachable_sink())
        before = self.snapshot()
        with self.assertLogs(AUTHORIZER_LOGGER, level="ERROR"):
            with self.assertRaises(ProjectPermissionDeniedError) as caught:
                await service.leave_project(
                    self.actor(self.team.contributor), self.project_id
                )
        self.assertIs(caught.exception.reason, Reason.AUDIT_UNAVAILABLE)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.audit_rows(self.team.contributor), [])

    async def test_the_last_manager_is_refused_but_the_attempt_stays_recorded(self):
        service = self.service_with(self.real_sink())
        before = self.snapshot()
        with self.assertRaises(LastManagerError):
            await service.leave_project(self.actor(self.team.manager), self.project_id)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            self.summary(self.team.manager),
            [(LEAVE, "allow", "granted_to_resource_owner")],
        )

    async def test_a_non_member_is_allowed_by_the_policy_and_not_found_by_the_rule(
        self,
    ):
        service = self.service_with(self.real_sink())
        outsider = self.seed_user()
        before = self.snapshot()
        for project_id in (self.project_id, uuid4()):
            with self.assertRaises(ProjectNotFoundError):
                await service.leave_project(self.actor(outsider), project_id)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            self.summary(outsider), [(LEAVE, "allow", "granted_to_resource_owner")] * 2
        )

    async def test_an_invitee_cannot_leave_and_keeps_the_invitation(self):
        service = self.service_with(self.real_sink())
        invitee = self.seed_invitation(self.project_id)
        with self.assertRaises(ProjectNotFoundError):
            await service.leave_project(self.actor(invitee), self.project_id)
        self.assertEqual(self.member_status(self.project_id, invitee), "invited")

    async def test_a_lock_timeout_after_the_decision_changes_nothing(self):
        service = self.service_with(self.real_sink(), lock_timeout_ms=200)
        self.lock_project(self.project_id)
        before = self.snapshot()
        with self.assertRaises(ProjectBusyError):
            await service.leave_project(self.actor(self.team.viewer), self.project_id)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            self.summary(self.team.viewer),
            [(LEAVE, "allow", "granted_to_resource_owner")],
        )

    async def test_two_managers_leaving_at_once_leave_one_and_both_are_recorded(self):
        second = self.seed_manager(self.project_id)
        first = self.team.manager
        services = [self.service_with(self.real_sink()) for _ in range(2)]
        # Both are decided (and recorded) first, then serialised on the project row.
        async with asyncio.timeout(30):
            results = await asyncio.gather(
                services[0].leave_project(self.actor(first), self.project_id),
                services[1].leave_project(self.actor(second), self.project_id),
                return_exceptions=True,
            )
        self.assertEqual(sum(r is None for r in results), 1, results)
        self.assertEqual(sum(isinstance(r, LastManagerError) for r in results), 1)
        managers = [
            uid
            for uid, row in self.member_rows(self.project_id).items()
            if row["role"] == "manager"
        ]
        self.assertEqual(len(managers), 1)
        for user in (first, second):
            self.assertEqual(
                self.summary(user), [(LEAVE, "allow", "granted_to_resource_owner")]
            )


@requires_postgres
class AgentsAreDeniedAndRecordedTest(SelfServiceAuditTestCase):
    """The service takes a human Principal only; an agent's attempt is denied."""

    async def test_no_grant_lets_an_agent_create_answer_or_leave(self):
        owner = self.seed_user(system_role="owner")
        directory = StaticDirectory(Principal(owner, SystemRole.OWNER))
        authorizer = Authorizer(self.real_sink(), directory=directory, clock=self.clock)
        agent = uuid4()
        grant = AgentGrant(
            agent,
            frozenset(
                {
                    Capability.PROJECT_CREATE,
                    Capability.PROJECT_INVITATION_RESPOND,
                    Capability.PROJECT_LEAVE,
                }
            ),
            ALL_PROJECTS,
        )
        project_id = uuid4()
        attempts = {
            Capability.PROJECT_CREATE: Resource.system(),
            Capability.PROJECT_INVITATION_RESPOND: Resource(
                kind="project_invitation", project_id=project_id, owner_id=owner
            ),
            Capability.PROJECT_LEAVE: Resource(
                kind="project_membership", project_id=project_id, owner_id=owner
            ),
        }
        for capability, resource in attempts.items():
            decision = await authorizer.authorize_agent_action(
                owner, grant, capability, resource
            )
            self.assertFalse(decision.allowed)
            self.assertIs(decision.reason, Reason.AGENT_CAPABILITY_FORBIDDEN)
        rows = self.audit_rows(owner)
        self.assertEqual(
            [(r["action"], r["decision"], r["reason"], r["agent_id"]) for r in rows],
            [(c.value, "deny", "agent_capability_forbidden", agent) for c in attempts],
        )
        self.assertEqual(self.table_count("projects"), 0)
        self.assertEqual(self.table_count("project_members"), 0)


@requires_postgres
class OutcomeConsistencyTest(SelfServiceAuditTestCase):
    """The same four scenarios for every method: state and audit row agree.

    Table: allowed (changes state, one ``allow`` row), denied (no change, one
    ``deny`` row), audit down (no change, no row, ``audit_unavailable``), and an
    allowed attempt that a rule refuses (no change, one ``allow`` row). A change of
    state without an ``allow`` row never happens.
    """

    def scenario(self, name: str):
        """``(user, call(service, principal), changed())`` for one method."""
        project_id = self.seed_project(name="Alpha")
        self.seed_manager(project_id)
        user = self.seed_user()
        if name == "create_project":
            return (
                user,
                lambda service, who: service.create_project(who, "Created"),
                lambda: self.projects_created_by(user) == 1,
            )
        if name == "accept_invite":
            self.seed_invitation(project_id, user_id=user)
            return (
                user,
                lambda service, who: service.accept_invite(who, project_id),
                lambda: self.member_status(project_id, user) == "active",
            )
        if name == "decline_invite":
            self.seed_invitation(project_id, user_id=user)
            return (
                user,
                lambda service, who: service.decline_invite(who, project_id),
                lambda: self.member_status(project_id, user) is None,
            )
        self.seed_member(project_id, user_id=user, role=CONTRIBUTOR)
        return (
            user,
            lambda service, who: service.leave_project(who, project_id),
            lambda: self.member_status(project_id, user) is None,
        )

    METHODS = ("create_project", "accept_invite", "decline_invite", "leave_project")
    CAPABILITY = {
        "create_project": CREATE,
        "accept_invite": RESPOND,
        "decline_invite": RESPOND,
        "leave_project": LEAVE,
    }

    async def test_allowed_changes_the_state_and_writes_exactly_one_allow_row(self):
        for method in self.METHODS:
            with self.subTest(method=method):
                user, call, changed = self.scenario(method)
                self.assertFalse(changed())
                await call(self.service_with(self.real_sink()), self.actor(user))
                self.assertTrue(changed())
                self.assertEqual(
                    self.summary(user),
                    [(self.CAPABILITY[method], "allow", self.allow_reason(method))],
                )

    async def test_denied_changes_nothing_and_writes_exactly_one_deny_row(self):
        for method in self.METHODS:
            with self.subTest(method=method):
                user, call, changed = self.scenario(method)
                before = self.snapshot()
                with self.assertRaises(ProjectPermissionDeniedError) as caught:
                    await call(
                        self.service_with(self.real_sink()),
                        self.actor(user, SystemRole.SYSTEM),
                    )
                self.assertIs(caught.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
                self.assertFalse(changed())
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(
                    self.summary(user),
                    [(self.CAPABILITY[method], "deny", "capability_not_granted")],
                )

    async def test_an_audit_store_that_is_down_refuses_and_changes_nothing(self):
        for method in self.METHODS:
            with self.subTest(method=method):
                user, call, changed = self.scenario(method)
                before = self.snapshot()
                with self.assertLogs(AUTHORIZER_LOGGER, level="ERROR"):
                    with self.assertRaises(ProjectPermissionDeniedError) as caught:
                        await call(
                            self.service_with(self.unreachable_sink()), self.actor(user)
                        )
                self.assertIs(caught.exception.reason, Reason.AUDIT_UNAVAILABLE)
                self.assertFalse(changed())
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.audit_rows(user), [])

    async def test_an_allowed_attempt_that_a_rule_refuses_keeps_its_allow_row(self):
        # create_project has no such rule (its arguments are refused before the
        # decision, see the argument tests).
        project_id = self.seed_project(name="Alpha")
        manager = self.seed_manager(project_id)  # the only Manager
        stranger = self.seed_user()
        service = self.service_with(self.real_sink())
        refusals = {
            "accept_invite": (stranger, InviteNotFoundError, RESPOND),
            "decline_invite": (stranger, InviteNotFoundError, RESPOND),
            "leave_project": (manager, LastManagerError, LEAVE),
        }
        for method, (user, error, capability) in refusals.items():
            with self.subTest(method=method):
                before = self.snapshot()
                with self.assertRaises(error):
                    await getattr(service, method)(self.actor(user), project_id)
                self.assertEqual(self.snapshot(), before)
                self.assertIn(
                    (capability, "allow", "granted_to_resource_owner"),
                    self.summary(user),
                )

    def allow_reason(self, method: str) -> str:
        return (
            "granted_by_system_role"
            if method == "create_project"
            else "granted_to_resource_owner"
        )


if __name__ == "__main__":
    unittest.main()
