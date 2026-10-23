"""The result of a read-only task over HTTP (issue #290, Decision 0098 Approved)
on PostgreSQL.

``GET /api/v1/tasks/{task_id}/result`` answers the ``complete`` event's
``detail.result`` of a read-only task that completed, to a person who may read
the task's project AND every repository that was ever in its Working Set (rows
removed from it, and rows of an earlier attempt, included). The strings are
returned as stored (no ``redact_text``, Decision 0098, 2 C) with the control
characters but tab and line feed made visible. ``GET /api/v1/tasks/{task_id}``
says only whether there is one (``result``: ``none`` / ``available`` /
``hidden``).
"""

import json
import uuid
from datetime import datetime

import httpx
from sqlalchemy import text

from paw_backend.api.v1.tasks import TaskResultOut
from paw_backend.app import create_app
from paw_backend.authz import Authorizer, InMemoryAuditSink, Principal
from paw_backend.authz.roles import ProjectRole, SystemRole
from paw_backend.orchestrator.limits import (
    MAX_KEY_LENGTH,
    MAX_NODES,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
)
from paw_backend.tasks import Actor, RepoRole, TaskCommand, WorkingSetEntry
from paw_backend.tasks.domain import WorkingSetOperation

from .authz_support import StaticProvider
from .repositories_support import PostgresRepositoryTestCase
from .support import make_settings
from .task_support import BASELINE, TEST_DATABASE_URL, requires_postgres, single_target

# Built at run time so that no secret-looking literal sits in the source.
GITHUB = "ghp_" + "a1B2" * 9

RESULT = {
    "nodes": [
        {
            "key": "read",
            "role": "researcher",
            "title": "Read the parser",
            "summary": "The parser is LL(1).\nIt has no left recursion.",
        },
        {
            "key": "check",
            "role": "reviewer",
            "title": "Check what was found",
            "summary": "Nothing contradicts it.",
        },
    ]
}


@requires_postgres
class TaskResultApiTest(PostgresRepositoryTestCase):
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
        self.tasks = self.app.state.task_execution.tasks
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        )
        self.addAsyncCleanup(self.client.aclose)

        self.project = self.seed_project(name="Alpha")
        self.other_project = self.seed_project(name="Beta")
        self.repo = self.seed_repository(self.project, name="web-app")
        self.vendor = self.seed_repository(self.project, name="vendor-secrets")
        self.creator = uuid.uuid4()

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

    def deny_reading(self, repository_id: uuid.UUID) -> None:
        """Narrow the repository's ACL to "access denied" (after the task ran)."""
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE repositories SET acl_allowed = ARRAY[]::text[] "
                    "WHERE id = :id"
                ),
                {"id": repository_id},
            )

    async def read_only_task(self, *repository_ids: uuid.UUID) -> uuid.UUID:
        event = await self.tasks.create_task(
            project_id=self.project,
            created_by=self.creator,
            title="Explain the parser",
            repositories=[
                WorkingSetEntry(repository_id, RepoRole.REFERENCED, BASELINE)
                for repository_id in (repository_ids or (self.repo,))
            ],
            read_only=True,
        )
        return event.task_id

    async def apply(self, task_id: uuid.UUID, *commands: TaskCommand) -> None:
        for command in commands:
            await self.tasks.execute(task_id, command, actor=Actor.system())

    async def complete(self, task_id: uuid.UUID, result: dict = RESULT) -> None:
        await self.tasks.execute(
            task_id, TaskCommand.COMPLETE, actor=Actor.system(), result=result
        )

    async def completed_task(
        self, *repository_ids: uuid.UUID, result: dict = RESULT
    ) -> uuid.UUID:
        task_id = await self.read_only_task(*repository_ids)
        await self.apply(task_id, TaskCommand.START)
        await self.complete(task_id, result)
        return task_id

    async def remove(self, task_id: uuid.UUID, repository_id: uuid.UUID) -> None:
        await self.tasks.change_working_set(
            task_id,
            WorkingSetOperation.REMOVE,
            repository_id,
            actor=Actor.user(self.creator),
            expected_role=RepoRole.REFERENCED,
        )

    async def result(self, task_id: uuid.UUID) -> httpx.Response:
        return await self.client.get(f"/api/v1/tasks/{task_id}/result")

    async def detail(self, task_id: uuid.UUID) -> dict:
        response = await self.client.get(f"/api/v1/tasks/{task_id}")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def complete_events(self, task_id: uuid.UUID) -> list[dict]:
        return self.rows(
            "SELECT detail, created_at FROM task_events "
            "WHERE task_id = :t AND command = 'complete'",
            t=task_id,
        )

    def denials(self) -> list[tuple[str, str, str | None]]:
        return [
            (event.action, event.reason, event.resource_kind)
            for event in self.audit.events
            if event.decision == "deny"
        ]

    def assertForbidden(self, response: httpx.Response) -> None:
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "task_result_forbidden")
        # Neither the result nor the repository is named.
        self.assertNotIn("vendor", response.text)
        self.assertNotIn("parser", response.text)

    def assertNotFound(self, response: httpx.Response) -> None:
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "task_result_not_found")

    # -- the result -----------------------------------------------------------------

    async def test_a_completed_read_only_task_answers_its_result(self):
        task_id = await self.completed_task(self.repo, self.vendor)
        self.as_creator()

        response = await self.result(task_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        self.assertEqual(set(body), {"nodes", "completed_at"})
        # The plan's order, the four fields, the line breaks kept.
        self.assertEqual(body["nodes"], RESULT["nodes"])
        (event,) = self.complete_events(task_id)
        # The API's own form of a time (pydantic's JSON: ``Z`` for UTC, else the
        # offset of the database session's time zone), of the ``complete`` event.
        self.assertEqual(
            body["completed_at"],
            TaskResultOut(nodes=[], completed_at=event["created_at"]).model_dump(
                mode="json"
            )["completed_at"],
        )
        self.assertEqual(
            datetime.fromisoformat(body["completed_at"]), event["created_at"]
        )
        self.assertEqual((await self.detail(task_id))["result"], "available")
        self.assertEqual(self.denials(), [])

    async def test_a_viewer_reads_it_as_well(self):
        task_id = await self.completed_task()
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.VIEWER})
        response = await self.result(task_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["nodes"], RESULT["nodes"])
        self.assertEqual((await self.detail(task_id))["result"], "available")

    async def test_no_result_without_a_completed_read_only_task(self):
        self.as_creator()
        ordinary = (
            await self.tasks.create_task(
                project_id=self.project,
                created_by=self.creator,
                title="Fix login",
                repositories=single_target(self.repo),
            )
        ).task_id
        await self.apply(ordinary, TaskCommand.START)
        queued = await self.read_only_task()
        running = await self.read_only_task()
        await self.apply(running, TaskCommand.START)
        failed = await self.read_only_task()
        await self.apply(failed, TaskCommand.START, TaskCommand.FAIL)
        cancelled = await self.read_only_task()
        await self.apply(cancelled, TaskCommand.START, TaskCommand.CANCEL)
        for label, task_id in {
            "an ordinary task": ordinary,
            "queued": queued,
            "running": running,
            "failed": failed,
            "cancelled": cancelled,
        }.items():
            with self.subTest(label):
                self.assertNotFound(await self.result(task_id))
                self.assertEqual((await self.detail(task_id))["result"], "none")

    async def test_a_repository_the_reader_may_not_read_hides_it(self):
        # The creator too: an ACL narrowed after the task ran.
        task_id = await self.completed_task(self.repo, self.vendor)
        self.deny_reading(self.vendor)
        for label, user, role in (
            ("the creator", self.creator, ProjectRole.CONTRIBUTOR),
            ("a manager", uuid.uuid4(), ProjectRole.MANAGER),
            ("a viewer", uuid.uuid4(), ProjectRole.VIEWER),
        ):
            with self.subTest(label):
                self.audit.events.clear()
                self.sign_in(user, {self.project: role})

                self.assertForbidden(await self.result(task_id))

                # The refusal is audited as a decision on the repository.
                self.assertEqual(
                    self.denials(),
                    [("project.read", "repo_acl_forbids", "repository")],
                )
                detail = await self.detail(task_id)
                self.assertEqual(detail["result"], "hidden")
                self.assertNotIn("vendor", json.dumps(detail))

    async def test_a_repository_removed_before_completion_still_counts(self):
        # Two referenced repositories; one is taken out of the Working Set while
        # the task runs, and then the DAG succeeds. Reading the current Working
        # Set only would show the result to a person who may not read it.
        task_id = await self.read_only_task(self.repo, self.vendor)
        await self.apply(task_id, TaskCommand.START)
        await self.remove(task_id, self.vendor)
        await self.complete(task_id)
        self.as_creator()
        self.assertEqual((await self.result(task_id)).status_code, 200)

        self.deny_reading(self.vendor)

        detail = await self.detail(task_id)
        self.assertEqual(
            [repository["name"] for repository in detail["repositories"]],
            ["web-app"],
        )
        self.assertEqual(detail["result"], "hidden")
        self.assertNotIn("vendor", json.dumps(detail))
        self.assertForbidden(await self.result(task_id))
        self.assertEqual(
            self.denials(), [("project.read", "repo_acl_forbids", "repository")]
        )

    async def test_a_repository_removed_and_added_again_counts_once(self):
        task_id = await self.read_only_task(self.repo, self.vendor)
        await self.apply(task_id, TaskCommand.START)
        await self.remove(task_id, self.vendor)
        await self.tasks.change_working_set(
            task_id,
            WorkingSetOperation.ADD_REFERENCED,
            self.vendor,
            actor=Actor.user(self.creator),
            expected_role=None,
            starting_commit=BASELINE,
        )
        await self.complete(task_id)
        self.deny_reading(self.vendor)
        self.as_creator()

        self.assertEqual((await self.detail(task_id))["result"], "hidden")
        self.assertForbidden(await self.result(task_id))

    async def test_a_repository_of_an_attempt_before_a_retry_counts(self):
        task_id = await self.read_only_task(self.repo, self.vendor)
        await self.apply(task_id, TaskCommand.START)
        await self.remove(task_id, self.vendor)
        await self.apply(
            task_id, TaskCommand.FAIL, TaskCommand.RETRY, TaskCommand.START
        )
        await self.complete(task_id)
        self.deny_reading(self.vendor)
        self.as_creator()

        self.assertEqual((await self.detail(task_id))["result"], "hidden")
        self.assertForbidden(await self.result(task_id))

    async def test_a_repository_that_is_not_registered_any_more_hides_it(self):
        # A purged repository has no ACL to read: nobody may read the result.
        task_id = await self.completed_task(self.repo, uuid.uuid4())
        self.as_creator()

        self.assertEqual((await self.detail(task_id))["result"], "hidden")
        self.assertForbidden(await self.result(task_id))
        self.assertEqual(
            self.denials(), [("project.read", "repo_acl_unresolved", "repository")]
        )

    async def test_the_task_itself_unreadable_is_forbidden_as_before(self):
        task_id = await self.completed_task()
        for label, principal in (
            ("another project's member", ({self.other_project: ProjectRole.MANAGER},)),
            ("an owner who is no member", ({}, SystemRole.OWNER)),
            ("an admin who is no member", ({}, SystemRole.ADMIN)),
        ):
            with self.subTest(label):
                self.sign_in(uuid.uuid4(), *principal)
                response = await self.result(task_id)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "forbidden")
        self.as_creator()
        response = await self.result(uuid.uuid4())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden")

    async def test_nobody_signed_in_is_401(self):
        task_id = await self.completed_task()
        self.provider.who = None
        self.assertEqual((await self.result(task_id)).status_code, 401)

    async def test_the_result_is_returned_as_stored_without_redaction(self):
        # Decision 0098 (2 C): what the model wrote is shown as it is; the
        # stored event is not changed either.
        result = {
            "nodes": [
                {
                    "key": "read",
                    "role": "researcher",
                    "title": "Read the configuration",
                    "summary": f"The token is {GITHUB}.\nDB_PASSWORD=hunter2",
                }
            ]
        }
        task_id = await self.completed_task(result=result)
        self.as_creator()

        body = (await self.result(task_id)).json()

        self.assertEqual(body["nodes"], result["nodes"])
        self.assertNotIn("[REDACTED]", json.dumps(body))
        self.assertNotIn("redacted", body)
        (event,) = self.complete_events(task_id)
        self.assertEqual(event["detail"], {"result": result})

    async def test_control_characters_are_made_visible_in_every_field(self):
        result = {
            "nodes": [
                {
                    "key": "a\x1bb",
                    "role": "researcher\x01",
                    "title": "Bell\x07 and DEL\x7f",
                    "summary": "red \x1b[31mtext\x1b[0m\ttab\nline\r\nC1 \x9b end",
                }
            ]
        }
        task_id = await self.completed_task(result=result)
        self.as_creator()

        (shown,) = (await self.result(task_id)).json()["nodes"]

        self.assertEqual(
            shown,
            {
                "key": "a\\u001bb",
                "role": "researcher\\u0001",
                "title": "Bell\\u0007 and DEL\\u007f",
                "summary": (
                    "red \\u001b[31mtext\\u001b[0m\ttab\nline\\u000d\nC1 \\u009b end"
                ),
            },
        )
        (event,) = self.complete_events(task_id)
        self.assertEqual(event["detail"], {"result": result})

    async def test_the_largest_result_is_returned_whole(self):
        # 32 nodes with the longest title and summary, in the characters that
        # weigh most when stored (four-byte) and when shown (control characters,
        # six characters each).
        for label, character in (("emoji", "\U0001f600"), ("control", "\x01")):
            with self.subTest(label):
                result = {
                    "nodes": [
                        {
                            "key": f"n{index:02d}" + "x" * (MAX_KEY_LENGTH - 3),
                            "role": "researcher",
                            "title": character * MAX_TITLE_CHARS,
                            "summary": character * MAX_SUMMARY_CHARS,
                        }
                        for index in range(MAX_NODES)
                    ]
                }
                task_id = await self.completed_task(result=result)
                self.as_creator()

                response = await self.result(task_id)

                self.assertEqual(response.status_code, 200)
                nodes = response.json()["nodes"]
                self.assertEqual(len(nodes), MAX_NODES)
                shown = character if label == "emoji" else "\\u0001"
                for node in nodes:
                    self.assertEqual(node["summary"], shown * MAX_SUMMARY_CHARS)
                    self.assertEqual(node["title"], shown * MAX_TITLE_CHARS)

    async def test_the_detail_does_not_carry_the_result_itself(self):
        task_id = await self.completed_task()
        self.as_creator()
        detail = await self.detail(task_id)
        self.assertEqual(detail["result"], "available")
        self.assertNotIn("LL(1)", json.dumps(detail))
