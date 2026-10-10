"""「このタスクの間は許可」over HTTP (Decision 0085) on PostgreSQL.

``GET /approvals`` says which approval may be granted for its task
(``task_grant_allowed``); ``POST /approvals/{id}/decision`` with
``approve_for_task`` approves it and creates the grant; ``GET
/tasks/{id}/approval-grants`` lists the person's active grants of the task's
current run; ``POST /approval-grants/{id}/revoke`` withdraws one.

The application's own composition (``create_app`` with a database: the approval
service of ``app.state.task_execution`` with the PostgreSQL grant store), a real
Authorizer on an in-memory audit sink, and a static principal.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text

from paw_backend.app import create_app
from paw_backend.authz import Authorizer, InMemoryAuditSink, Principal
from paw_backend.authz.roles import ProjectRole, SystemRole
from paw_backend.tasks import Actor, TaskCommand
from paw_backend.tools import (
    ApprovalLevel,
    ApprovalStatus,
    ArgumentKind,
    GrantArgument,
    GrantMatch,
    GrantPattern,
    NewApproval,
    OpenLimits,
    ScopeStatus,
    SummaryItem,
)
from paw_backend.tools.approval_store import PostgresApprovalStore

from .authz_support import StaticProvider
from .repositories_support import PostgresRepositoryTestCase
from .support import make_settings
from .task_support import FIRST_RUN, TEST_DATABASE_URL, requires_postgres, single_target
from .test_tools_postgres import empty_tool_tables

PATTERN = GrantPattern(
    ScopeStatus.IN_SCOPE,
    (
        GrantArgument(
            "package",
            ArgumentKind.TEXT,
            GrantMatch.DIGEST,
            hashlib.sha256(b"x").hexdigest(),
        ),
    ),
)


@requires_postgres
class TaskGrantsApiTest(PostgresRepositoryTestCase):
    @classmethod
    def clean_tables(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(text("TRUNCATE tasks CASCADE"))
        super().clean_tables()

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.app = create_app(make_settings(database_url=TEST_DATABASE_URL))
        self.audit = InMemoryAuditSink()
        self.app.state.authorizer = Authorizer(self.audit)
        self.provider = StaticProvider(None)
        self.app.state.principal_provider = self.provider
        self.addAsyncCleanup(self.app.state.database.dispose)
        self.database = self.app.state.database
        async with self.database.engine.begin() as connection:
            await empty_tool_tables(connection)
        self.tasks = self.app.state.task_execution.tasks
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.project = self.seed_project(name="Alpha")
        self.other_project = self.seed_project(name="Beta")
        self.repo = self.seed_repository(self.project, name="web-app")
        self.creator = uuid.uuid4()
        self.agent = uuid.uuid4()

    # -- helpers --------------------------------------------------------------------

    def sign_in(
        self,
        user_id: uuid.UUID,
        roles: dict[uuid.UUID, ProjectRole] | None = None,
        system_role: SystemRole = SystemRole.USER,
    ) -> None:
        self.provider.who = Principal(user_id, system_role, roles or {})

    def as_creator(self, role: ProjectRole = ProjectRole.CONTRIBUTOR) -> None:
        self.sign_in(self.creator, {self.project: role})

    async def running_task(self) -> uuid.UUID:
        event = await self.tasks.create_task(
            project_id=self.project,
            created_by=self.creator,
            title="Install the tools",
            repositories=single_target(self.repo),
        )
        await self.tasks.execute(event.task_id, TaskCommand.START, actor=Actor.system())
        return event.task_id

    async def open_approval(
        self,
        task_id: uuid.UUID,
        *,
        level: ApprovalLevel = ApprovalLevel.APPROVAL,
        pattern: GrantPattern | None = PATTERN,
        requester: uuid.UUID | None = None,
    ) -> uuid.UUID:
        now = datetime.now(UTC)
        result = await PostgresApprovalStore(self.database).open_request(
            NewApproval(
                approval_id=uuid.uuid4(),
                task_id=task_id,
                task_run=FIRST_RUN,
                project_id=self.project,
                agent_id=self.agent,
                requester_user_id=requester or self.creator,
                tool="host.install_package",
                level=level,
                call_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                targets=(),
                summary=(SummaryItem("package", "text", "ripgrep"),),
                expires_at=now + timedelta(hours=1),
                grant_pattern=pattern,
            ),
            now=now - timedelta(seconds=1),
            limits=OpenLimits(),
        )
        assert result.record is not None, result.outcome
        return result.record.approval_id

    async def decide(self, approval_id, decision="approve_for_task") -> httpx.Response:
        return await self.client.post(
            f"/api/v1/approvals/{approval_id}/decision", json={"decision": decision}
        )

    async def grants(self, task_id) -> httpx.Response:
        return await self.client.get(f"/api/v1/tasks/{task_id}/approval-grants")

    async def revoke(self, grant_id) -> httpx.Response:
        return await self.client.post(f"/api/v1/approval-grants/{grant_id}/revoke")

    def status_of(self, approval_id: uuid.UUID) -> str:
        return self.rows(
            "SELECT status FROM tool_approvals WHERE id = :a", a=approval_id
        )[0]["status"]

    def code(self, response: httpx.Response) -> tuple[int, str]:
        return response.status_code, response.json()["error"]["code"]

    # -- tests ----------------------------------------------------------------------

    async def test_the_list_says_which_approvals_may_be_granted_for_the_task(self):
        task_id = await self.running_task()
        grantable = await self.open_approval(task_id)
        never = await self.open_approval(task_id, pattern=None)
        strong = await self.open_approval(
            task_id, level=ApprovalLevel.STRONG_APPROVAL, pattern=None
        )
        self.as_creator()

        listed = (await self.client.get("/api/v1/approvals")).json()["approvals"]

        self.assertEqual(
            {item["id"]: item["task_grant_allowed"] for item in listed},
            {str(grantable): True, str(never): False, str(strong): False},
        )
        one = (await self.client.get(f"/api/v1/approvals/{grantable}")).json()
        self.assertTrue(one["task_grant_allowed"])

    async def test_approve_for_task_approves_and_lists_the_grant(self):
        task_id = await self.running_task()
        approval = await self.open_approval(task_id)
        self.as_creator()

        response = await self.decide(approval)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            (body["id"], body["outcome"]), (str(approval), "approved_for_task")
        )
        grant_id = uuid.UUID(body["grant_id"])
        self.assertEqual(self.status_of(approval), ApprovalStatus.APPROVED.value)
        listed = await self.grants(task_id)
        self.assertEqual(listed.status_code, 200)
        grants = listed.json()["grants"]
        self.assertEqual(len(grants), 1)
        self.assertEqual(
            {key: grants[0][key] for key in ("id", "approval_id", "tool", "uses")},
            {
                "id": str(grant_id),
                "approval_id": str(approval),
                "tool": "host.install_package",
                "uses": 0,
            },
        )
        self.assertEqual(
            grants[0]["summary"],
            [{"name": "package", "kind": "text", "value": "ripgrep"}],
        )
        # The approval is audited, and so is the grant.
        actions = [
            row["action"]
            for row in self.rows(
                "SELECT action FROM audit_events WHERE resource_id IN (:a, :g)"
                " ORDER BY occurred_at",
                a=approval,
                g=grant_id,
            )
        ]
        self.assertEqual(
            sorted(actions), ["tool.approval.approve", "tool.approval.grant"]
        )
        # Granted once: the approval is no longer pending.
        again = await self.decide(approval)
        self.assertEqual(self.code(again), (409, "approval_not_pending"))

    async def test_calls_that_cannot_be_granted_stay_pending(self):
        task_id = await self.running_task()
        never = await self.open_approval(task_id, pattern=None)
        strong = await self.open_approval(
            task_id, level=ApprovalLevel.STRONG_APPROVAL, pattern=None
        )
        self.as_creator()
        for approval in (never, strong):
            with self.subTest(approval=approval):
                response = await self.decide(approval)
                self.assertEqual(self.code(response), (409, "task_grant_not_allowed"))
                self.assertEqual(self.status_of(approval), ApprovalStatus.PENDING.value)
        self.assertEqual((await self.grants(task_id)).json()["grants"], [])

    async def test_only_the_person_asked_may_grant(self):
        task_id = await self.running_task()
        approval = await self.open_approval(task_id)
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.MANAGER})
        self.assertEqual(
            self.code(await self.decide(approval)), (404, "approval_not_found")
        )
        self.as_creator(ProjectRole.VIEWER)
        self.assertEqual((await self.decide(approval)).status_code, 403)
        self.assertEqual(self.status_of(approval), ApprovalStatus.PENDING.value)

    async def test_an_ended_task_gets_no_grant(self):
        task_id = await self.running_task()
        approval = await self.open_approval(task_id)
        await self.tasks.execute(task_id, TaskCommand.CANCEL, actor=Actor.system())
        self.as_creator()
        response = await self.decide(approval)
        # The end of the task revoked its open approvals already.
        self.assertIn(
            self.code(response),
            ((409, "approval_not_pending"), (409, "task_not_active")),
        )
        self.assertEqual(
            self.rows("SELECT count(*) AS n FROM tool_task_grants")[0]["n"], 0
        )

    async def test_the_grant_ends_with_the_task(self):
        task_id = await self.running_task()
        approval = await self.open_approval(task_id)
        self.as_creator()
        grant_id = (await self.decide(approval)).json()["grant_id"]

        await self.tasks.execute(task_id, TaskCommand.CANCEL, actor=Actor.system())

        self.assertEqual((await self.grants(task_id)).json()["grants"], [])
        self.assertEqual(
            self.rows("SELECT status FROM tool_task_grants WHERE id = :g", g=grant_id)[
                0
            ]["status"],
            "revoked",
        )

    async def test_the_list_is_the_persons_own_and_readable_only(self):
        task_id = await self.running_task()
        mine = await self.open_approval(task_id)
        someone = uuid.uuid4()
        theirs = await self.open_approval(task_id, requester=someone)
        self.sign_in(someone, {self.project: ProjectRole.CONTRIBUTOR})
        their_grant = (await self.decide(theirs)).json()["grant_id"]
        self.as_creator()
        await self.decide(mine)

        listed = (await self.grants(task_id)).json()["grants"]
        self.assertEqual([item["approval_id"] for item in listed], [str(mine)])
        self.assertNotIn(their_grant, str(listed))
        # Not a member of the project any more: nothing.
        self.sign_in(self.creator, {self.other_project: ProjectRole.MANAGER})
        self.assertEqual((await self.grants(task_id)).json()["grants"], [])

    async def test_the_person_or_an_administrator_may_revoke_nobody_else(self):
        task_id = await self.running_task()
        self.as_creator()
        first = (await self.decide(await self.open_approval(task_id))).json()[
            "grant_id"
        ]
        second = (await self.decide(await self.open_approval(task_id))).json()[
            "grant_id"
        ]

        # Another member of the project: not found.
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.MANAGER})
        self.assertEqual(self.code(await self.revoke(first)), (404, "grant_not_found"))
        self.assertEqual(
            self.code(await self.revoke(uuid.uuid4())), (404, "grant_not_found")
        )

        self.as_creator()
        response = await self.revoke(first)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": first, "outcome": "revoked"})
        self.assertEqual(self.code(await self.revoke(first)), (409, "grant_not_active"))

        self.sign_in(uuid.uuid4(), {}, SystemRole.ADMIN)
        self.assertEqual((await self.revoke(second)).status_code, 200)

        self.as_creator()
        self.assertEqual((await self.grants(task_id)).json()["grants"], [])
        self.assertEqual(
            [
                row["action"]
                for row in self.rows(
                    "SELECT action FROM audit_events WHERE resource_id = :g"
                    " AND action = 'tool.approval.grant.revoke' AND decision = 'allow'",
                    g=first,
                )
            ],
            ["tool.approval.grant.revoke"],
        )

    async def test_the_request_is_checked(self):
        task_id = await self.running_task()
        approval = await self.open_approval(task_id)
        self.as_creator()
        response = await self.client.post(
            f"/api/v1/approvals/{approval}/decision",
            json={"decision": "approve_for_task", "x": 1},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            (await self.client.post("/api/v1/approval-grants/nope/revoke")).status_code,
            422,
        )
        self.assertEqual(self.status_of(approval), ApprovalStatus.PENDING.value)
