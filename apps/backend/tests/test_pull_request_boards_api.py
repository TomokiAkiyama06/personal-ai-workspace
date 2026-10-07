"""The PR screen's panels and the mobile boards over HTTP (issue #185 item 6,
Decision 0078 Proposed) on PostgreSQL: changed files and diff, reviewers, audit
rows, and the tool approvals a person is asked for.

The application's own composition (``create_app`` with a database: the task
service and the approval service of ``app.state.task_execution``) and a real
Authorizer on an in-memory audit sink; the principal is a static one with the
memberships a session's provider would read.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text

from paw_backend.app import create_app
from paw_backend.authz import Authorizer, InMemoryAuditSink, Principal
from paw_backend.authz.audit import AuditEvent, PostgresAuditSink
from paw_backend.authz.roles import ProjectRole, SystemRole
from paw_backend.integration.changes import (
    ChangedFile,
    PullRequestChanges,
    PullRequestChangeStore,
)
from paw_backend.orchestrator.domain import ExecutionPlacement
from paw_backend.orchestrator.records import NodeResult
from paw_backend.orchestrator.store import DagStore
from paw_backend.tasks import (
    Actor,
    PullRequestInfo,
    PullRequestState,
    TaskCommand,
    WorktreeState,
)
from paw_backend.tools import (
    ApprovalLevel,
    ApprovalStatus,
    NewApproval,
    OpenLimits,
    SummaryItem,
)
from paw_backend.tools.approval_store import PostgresApprovalStore
from paw_backend.tools.scope import Target, TargetKind

from .authz_support import StaticProvider
from .orchestrator_support import make_plan, node
from .repositories_support import PostgresRepositoryTestCase
from .support import make_settings
from .task_support import (
    FIRST_RUN,
    PASSED_REVIEW,
    TEST_DATABASE_URL,
    requires_postgres,
    single_target,
)

PULL_REQUEST = PullRequestInfo(
    7, "https://github.com/acme/web-app/pull/7", PullRequestState.OPEN
)
HEAD = "a" * 40


@requires_postgres
class PullRequestBoardsApiTest(PostgresRepositoryTestCase):
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
        self.tasks = self.app.state.task_execution.tasks
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        )
        self.addAsyncCleanup(self.client.aclose)

        self.project = self.seed_project(name="Alpha")
        self.other_project = self.seed_project(name="Beta")
        self.repo = self.seed_repository(self.project, name="web-app")
        self.hidden_repo = self.seed_repository(
            self.project, name="secret-infra", acl_allowed=[]
        )
        self.other_repo = self.seed_repository(self.other_project, name="beta-repo")
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

    async def task_with_pull_request(
        self,
        *,
        project: uuid.UUID | None = None,
        repository: uuid.UUID | None = None,
        title: str = "Fix login",
        before_complete=None,
    ) -> tuple[uuid.UUID, int]:
        """A completed task whose repository delivered ``PULL_REQUEST``, and the
        id of its record."""
        repository = repository or self.repo
        event = await self.tasks.create_task(
            project_id=project or self.project,
            created_by=self.creator,
            title=title,
            repositories=single_target(repository),
        )
        task_id = event.task_id
        await self.tasks.execute(task_id, TaskCommand.START, actor=Actor.system())
        await self.tasks.update_attempt(
            task_id,
            run=FIRST_RUN,
            repository_id=repository,
            worktree=WorktreeState(branch="paw/x/1/_integration"),
            review=PASSED_REVIEW,
            pull_request=PULL_REQUEST,
        )
        if before_complete is not None:
            await before_complete(task_id)
        for command in (TaskCommand.BEGIN_EVALUATION, TaskCommand.COMPLETE):
            await self.tasks.execute(task_id, command, actor=Actor.system())
        record_id = self.rows(
            "SELECT id FROM task_attempt_repositories WHERE task_id = :t", t=task_id
        )[0]["id"]
        return task_id, record_id

    async def record_changes(self, task_id, repository=None, **options) -> bool:
        files = options.pop(
            "files",
            (
                ChangedFile(
                    "apps/backend/auth/session.py",
                    None,
                    "modified",
                    48,
                    12,
                    patch="@@ -1,2 +1,2 @@\n-old\n+new\n",
                ),
                ChangedFile(
                    "docs/new.md",
                    "docs/old.md",
                    "renamed",
                    3,
                    1,
                    patch="@@ -1 +1 @@\n-a\n+b\n",
                    patch_truncated=True,
                ),
                ChangedFile("assets/logo.png", None, "added", 0, 0),
            ),
        )
        changes = PullRequestChanges(
            HEAD, tuple(files), options.pop("truncated", False)
        )
        return await PullRequestChangeStore(self.database).record(
            task_id, 1, repository or self.repo, PULL_REQUEST, changes
        )

    async def get(self, path: str, **params) -> httpx.Response:
        return await self.client.get(f"/api/v1{path}", params=params)

    def denials(self) -> list[tuple[str, str]]:
        return [
            (event.action, event.reason)
            for event in self.audit.events
            if event.decision == "deny"
        ]

    # -- changed files and diff -----------------------------------------------------

    async def test_nobody_is_signed_in(self):
        for path in (
            "/pull-requests/1/files",
            "/pull-requests/1/files/0",
            "/pull-requests/1/review",
            "/pull-requests/1/audit",
            "/approvals",
        ):
            response = await self.get(path)
            self.assertEqual(response.status_code, 401, path)
        response = await self.client.post(
            f"/api/v1/approvals/{uuid.uuid4()}/decision", json={"decision": "approve"}
        )
        self.assertEqual(response.status_code, 401)

    async def test_changes_not_recorded(self):
        _, record_id = await self.task_with_pull_request()
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.VIEWER})

        response = await self.get(f"/pull-requests/{record_id}/files")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "recorded": False,
                "head_commit": None,
                "truncated": False,
                "additions": 0,
                "deletions": 0,
                "files": [],
            },
        )
        response = await self.get(f"/pull-requests/{record_id}/files/0")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "file_not_found")

    async def test_the_changed_files_and_one_diff(self):
        task_id, record_id = await self.task_with_pull_request()
        self.assertTrue(await self.record_changes(task_id, truncated=True))
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.VIEWER})

        body = (await self.get(f"/pull-requests/{record_id}/files")).json()

        self.assertEqual(
            (body["recorded"], body["head_commit"], body["truncated"]),
            (True, HEAD, True),
        )
        self.assertEqual((body["additions"], body["deletions"]), (51, 13))
        self.assertEqual(
            [
                (f["index"], f["path"], f["status"], f["has_patch"])
                for f in body["files"]
            ],
            [
                (0, "apps/backend/auth/session.py", "modified", True),
                (1, "docs/new.md", "renamed", True),
                (2, "assets/logo.png", "added", False),
            ],
        )
        self.assertEqual(body["files"][1]["previous_path"], "docs/old.md")
        # The list carries no patch.
        self.assertNotIn("patch", body["files"][0])

        diff = (await self.get(f"/pull-requests/{record_id}/files/1")).json()
        self.assertEqual(
            {key: diff[key] for key in ("index", "count", "path", "patch_truncated")},
            {"index": 1, "count": 3, "path": "docs/new.md", "patch_truncated": True},
        )
        self.assertEqual(diff["patch"], "@@ -1 +1 @@\n-a\n+b\n")
        binary = (await self.get(f"/pull-requests/{record_id}/files/2")).json()
        self.assertIsNone(binary["patch"])
        self.assertFalse(binary["has_patch"])
        for index in (3, 299):
            response = await self.get(f"/pull-requests/{record_id}/files/{index}")
            self.assertEqual(response.status_code, 404, index)
        for index in (-1, 300, "x"):
            response = await self.get(f"/pull-requests/{record_id}/files/{index}")
            self.assertEqual(response.status_code, 422, index)

    async def test_recording_again_replaces_the_changes(self):
        task_id, record_id = await self.task_with_pull_request()
        await self.record_changes(task_id)
        await self.record_changes(
            task_id, files=(ChangedFile("only.py", None, "added", 1, 0),)
        )
        self.as_creator()
        body = (await self.get(f"/pull-requests/{record_id}/files")).json()
        self.assertEqual([f["path"] for f in body["files"]], ["only.py"])

    async def test_changes_of_another_pull_request_are_not_stored(self):
        task_id, _ = await self.task_with_pull_request()
        other = PullRequestInfo(8, PULL_REQUEST.url[:-1] + "8", PullRequestState.OPEN)
        stored = await PullRequestChangeStore(self.database).record(
            task_id, 1, self.repo, other, PullRequestChanges(HEAD, (), False)
        )
        self.assertFalse(stored)
        self.assertEqual(self.rows("SELECT * FROM pull_request_changes"), [])

    async def test_an_unreadable_or_missing_pull_request_is_not_found_alike(self):
        hidden_task, hidden = await self.task_with_pull_request(
            repository=self.hidden_repo
        )
        await self.record_changes(hidden_task, self.hidden_repo)
        _, foreign = await self.task_with_pull_request(
            project=self.other_project, repository=self.other_repo
        )
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.MANAGER})
        for record_id in (hidden, foreign, max(hidden, foreign) + 1000):
            for suffix in ("files", "files/0", "review", "audit"):
                response = await self.get(f"/pull-requests/{record_id}/{suffix}")
                self.assertEqual(response.status_code, 404, (record_id, suffix))
                self.assertEqual(
                    response.json()["error"]["code"], "pull_request_not_found"
                )
                self.assertNotIn("secret-infra", response.text)

    # -- reviewers ------------------------------------------------------------------

    async def test_the_reviewers_of_the_attempt(self):
        store = DagStore(self.database)

        async def review(task_id):
            dag = await store.create(
                task_id,
                1,
                make_plan(
                    node("impl"),
                    node("codex", role="reviewer", title="Codex review"),
                    node("claude", role="reviewer", title="Claude review"),
                ),
                run=FIRST_RUN,
            )
            dag = await store.acquire(dag.id, "worker-1", FIRST_RUN)
            attempt = await store.start_node(dag.id, dag.epoch, "codex", max_attempts=3)
            await store.record_placement(
                dag.id,
                dag.epoch,
                "codex",
                attempt.number,
                placement=ExecutionPlacement.LOCAL_GPU,
                agent="codex",
                model="gpt-5-codex",
            )
            await store.complete_node(
                dag.id, dag.epoch, "codex", attempt.number, NodeResult(summary="LGTM")
            )

        _, record_id = await self.task_with_pull_request(before_complete=review)
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.VIEWER})

        response = await self.get(f"/pull-requests/{record_id}/review")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["review"], body["evaluation"]), ("approved", "passed"))
        codex, claude = body["reviewers"]
        self.assertEqual(
            (codex["key"], codex["title"], codex["state"], codex["agent"]),
            ("codex", "Codex review", "succeeded", "codex"),
        )
        self.assertEqual(codex["model"], "gpt-5-codex")
        self.assertIsNotNone(codex["finished_at"])
        self.assertEqual((claude["key"], claude["agent"]), ("claude", None))
        # What a reviewer said is not answered.
        self.assertNotIn("LGTM", response.text)

    async def test_no_dag_has_no_reviewers(self):
        _, record_id = await self.task_with_pull_request()
        self.as_creator()
        body = (await self.get(f"/pull-requests/{record_id}/review")).json()
        self.assertEqual(body["reviewers"], [])

    # -- audit rows -------------------------------------------------------------------

    async def audit_row(self, **fields) -> None:
        event = {
            "event_id": uuid.uuid4(),
            "correlation_id": uuid.uuid4(),
            "occurred_at": datetime.now(UTC),
            "action": "tool.run_tests",
            "resource_kind": "task",
            "decision": "allow",
            "reason": "auto",
        }
        event.update(fields)
        await PostgresAuditSink(self.database).record(AuditEvent(**event))

    async def test_the_audit_rows_of_the_task_and_its_approvals(self):
        task_id, record_id = await self.task_with_pull_request()
        other_task, _ = await self.task_with_pull_request(title="Other")
        approval = await self.open_approval(task_id)
        start = datetime.now(UTC) - timedelta(hours=1)
        await self.audit_row(
            occurred_at=start,
            resource_id=task_id,
            actor_id=self.creator,
            agent_id=self.agent,
            project_id=self.project,
        )
        await self.audit_row(
            occurred_at=start + timedelta(minutes=1),
            action="tool.approval.approve",
            resource_kind="tool_approval",
            resource_id=approval,
            actor_id=self.creator,
            actor_role="user",
            reason="approved",
        )
        await self.audit_row(
            occurred_at=start + timedelta(minutes=2),
            action="tool.approval.revoke",
            resource_kind="tool_approval",
            resource_id=approval,
            reason="task_ended",
        )
        await self.audit_row(
            occurred_at=start + timedelta(minutes=3),
            resource_id=task_id,
            decision="deny",
            reason="path_out_of_scope",
            actor_id=self.creator,
            agent_id=self.agent,
        )
        # Another task's row and a row of another kind with the task's id.
        await self.audit_row(resource_id=other_task)
        await self.audit_row(resource_kind="project", resource_id=task_id)
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.VIEWER})

        response = await self.get(f"/pull-requests/{record_id}/audit")

        self.assertEqual(response.status_code, 200)
        rows = response.json()["rows"]
        self.assertEqual(
            [(r["action"], r["decision"], r["reason"], r["actor"]) for r in rows],
            [
                ("tool.run_tests", "deny", "path_out_of_scope", "agent"),
                ("tool.approval.revoke", "allow", "task_ended", "system"),
                ("tool.approval.approve", "allow", "approved", "person"),
                ("tool.run_tests", "allow", "auto", "agent"),
            ],
        )
        # A closed projection: no actor, request or resource id.
        self.assertNotIn(str(self.creator), response.text)
        self.assertNotIn(str(approval), response.text)

        limited = await self.get(f"/pull-requests/{record_id}/audit", limit=1)
        self.assertEqual(len(limited.json()["rows"]), 1)
        for limit in (0, 101):
            response = await self.get(f"/pull-requests/{record_id}/audit", limit=limit)
            self.assertEqual(response.status_code, 422)

    # -- tool approvals -------------------------------------------------------------

    async def open_approval(
        self,
        task_id: uuid.UUID,
        *,
        level: ApprovalLevel = ApprovalLevel.APPROVAL,
        expires_in: timedelta = timedelta(hours=1),
        repositories: tuple[uuid.UUID, ...] = (),
        requester: uuid.UUID | None = None,
    ) -> uuid.UUID:
        project = self.rows("SELECT project_id FROM tasks WHERE id = :t", t=task_id)[0][
            "project_id"
        ]
        now = datetime.now(UTC)
        result = await PostgresApprovalStore(self.database).open_request(
            NewApproval(
                approval_id=uuid.uuid4(),
                task_id=task_id,
                task_run=FIRST_RUN,
                project_id=project,
                agent_id=self.agent,
                requester_user_id=requester or self.creator,
                tool="package.add",
                level=level,
                call_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                targets=tuple(
                    Target(TargetKind.REPOSITORY, str(repository))
                    for repository in repositories
                ),
                summary=(SummaryItem("command", "text", 'uv add "pyjwt>=2.9"'),),
                expires_at=now + expires_in,
            ),
            now=now - timedelta(seconds=1)
            if expires_in > timedelta(0)
            else now + expires_in - timedelta(minutes=1),
            limits=OpenLimits(),
        )
        assert result.record is not None, result.outcome
        return result.record.approval_id

    async def running_task(self, **options) -> uuid.UUID:
        event = await self.tasks.create_task(
            project_id=options.get("project", self.project),
            created_by=self.creator,
            title=options.get("title", "Add a dependency"),
            repositories=single_target(options.get("repository", self.repo)),
        )
        await self.tasks.execute(event.task_id, TaskCommand.START, actor=Actor.system())
        return event.task_id

    def status_of(self, approval_id: uuid.UUID) -> str:
        return self.rows(
            "SELECT status FROM tool_approvals WHERE id = :a", a=approval_id
        )[0]["status"]

    async def decide(self, approval_id, decision="approve") -> httpx.Response:
        return await self.client.post(
            f"/api/v1/approvals/{approval_id}/decision", json={"decision": decision}
        )

    async def test_the_approvals_the_person_is_asked_for(self):
        task_id = await self.running_task()
        other_task = await self.running_task(title="Another")
        mine = await self.open_approval(
            task_id, repositories=(self.repo, self.hidden_repo)
        )
        strong = await self.open_approval(
            other_task, level=ApprovalLevel.STRONG_APPROVAL
        )
        await self.open_approval(task_id, expires_in=timedelta(seconds=-30))
        # Somebody else's approval in the same project.
        someone = uuid.uuid4()
        await self.open_approval(task_id, requester=someone)
        foreign_task = await self.running_task(
            project=self.other_project, repository=self.other_repo
        )
        await self.open_approval(foreign_task)
        self.as_creator()

        response = await self.get("/approvals")

        self.assertEqual(response.status_code, 200)
        approvals = response.json()["approvals"]
        self.assertEqual([a["id"] for a in approvals], [str(strong), str(mine)])
        first = approvals[1]
        self.assertEqual(
            {
                key: first[key]
                for key in ("task_id", "task_title", "tool", "level", "repositories")
            },
            {
                "task_id": str(task_id),
                "task_title": "Add a dependency",
                "tool": "package.add",
                "level": "approval",
                "repositories": ["web-app"],
            },
        )
        self.assertEqual(
            first["summary"],
            [{"name": "command", "kind": "text", "value": 'uv add "pyjwt>=2.9"'}],
        )
        self.assertEqual(approvals[0]["level"], "strong_approval")

        only = await self.get("/approvals", task_id=str(task_id))
        self.assertEqual([a["id"] for a in only.json()["approvals"]], [str(mine)])

        # The other person sees only their own.
        self.sign_in(someone, {self.project: ProjectRole.CONTRIBUTOR})
        listed = (await self.get("/approvals")).json()["approvals"]
        self.assertEqual([a["task_id"] for a in listed], [str(task_id)])
        self.assertNotIn(str(mine), str(listed))

    async def test_approve_and_reject_once(self):
        task_id = await self.running_task()
        approval = await self.open_approval(task_id)
        rejected = await self.open_approval(task_id)
        self.as_creator()

        response = await self.decide(approval)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": str(approval), "outcome": "approved"})
        self.assertEqual(self.status_of(approval), ApprovalStatus.APPROVED.value)
        again = await self.decide(approval, "reject")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["error"]["code"], "approval_not_pending")
        self.assertEqual(self.status_of(approval), ApprovalStatus.APPROVED.value)

        response = await self.decide(rejected, "reject")
        self.assertEqual(response.json()["outcome"], "rejected")
        self.assertEqual(self.status_of(rejected), ApprovalStatus.REJECTED.value)
        # The decision is audited: the capability (REQUIRED) by the Authorizer,
        # the approval by the service.
        self.assertIn(
            ("project.task.run", "allow"),
            [(event.action, event.decision) for event in self.audit.events],
        )
        self.assertEqual(
            [
                row["action"]
                for row in self.rows(
                    "SELECT action FROM audit_events WHERE resource_id = :a",
                    a=approval,
                )
            ],
            ["tool.approval.approve", "tool.approval.reject"],
        )
        # Gone from the list once decided.
        self.assertEqual((await self.get("/approvals")).json()["approvals"], [])

    async def test_a_strong_approval_is_never_granted_here(self):
        task_id = await self.running_task()
        approval = await self.open_approval(
            task_id, level=ApprovalLevel.STRONG_APPROVAL
        )
        self.as_creator()

        response = await self.decide(approval)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["error"]["code"], "strong_approval_unavailable"
        )
        self.assertEqual(self.status_of(approval), ApprovalStatus.PENDING.value)
        # Rejecting one needs no step-up.
        response = await self.decide(approval, "reject")
        self.assertEqual(response.json()["outcome"], "rejected")

    async def test_only_the_person_asked_may_decide(self):
        task_id = await self.running_task()
        approval = await self.open_approval(task_id)
        # Another contributor of the project: told the approval does not exist.
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.CONTRIBUTOR})
        response = await self.decide(approval)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "approval_not_found")
        # A viewer (even the person asked) may not run the project's tasks.
        self.as_creator(ProjectRole.VIEWER)
        response = await self.decide(approval)
        self.assertEqual(response.status_code, 403)
        self.assertIn(("project.task.run", "capability_not_granted"), self.denials())
        # Not a member of the project, or no such approval: forbidden alike.
        self.sign_in(self.creator, {self.other_project: ProjectRole.MANAGER})
        for approval_id in (approval, uuid.uuid4()):
            response = await self.decide(approval_id)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["error"]["code"], "forbidden")
        self.assertEqual(self.status_of(approval), ApprovalStatus.PENDING.value)

    async def test_an_expired_approval_cannot_be_decided(self):
        task_id = await self.running_task()
        approval = await self.open_approval(task_id, expires_in=timedelta(seconds=-30))
        self.as_creator()
        response = await self.decide(approval)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "approval_expired")

    async def test_the_request_is_checked(self):
        task_id = await self.running_task()
        approval = await self.open_approval(task_id)
        self.as_creator()
        for body in ({}, {"decision": "allow"}, {"decision": "approve", "x": 1}):
            response = await self.client.post(
                f"/api/v1/approvals/{approval}/decision", json=body
            )
            self.assertEqual(response.status_code, 422, body)
        self.assertEqual(self.status_of(approval), ApprovalStatus.PENDING.value)

    async def test_there_is_no_merge_route(self):
        _, record_id = await self.task_with_pull_request()
        self.as_creator()
        response = await self.client.post(
            f"/api/v1/pull-requests/{record_id}/merge", json={}
        )
        self.assertIn(response.status_code, (404, 405))
