"""Tasks, DAG, controls and pull request records over HTTP (issue #185, Decision
0067 Approved) on PostgreSQL.

The application's own composition (``create_app`` with a database: the task
service, the queue and the budget tracker of ``app.state.task_execution``, with
the real Project state gate) and a real Authorizer on an in-memory audit sink. The
principal is a static one with the memberships a session's provider would read.
"""

import uuid

import httpx
from sqlalchemy import text

from paw_backend.app import create_app
from paw_backend.authz import Authorizer, InMemoryAuditSink, Principal
from paw_backend.authz.roles import ProjectRole, SystemRole
from paw_backend.compute import ComputeConfig, ComputeRequest, ResourceClass
from paw_backend.compute.wiring import ComputeSetup
from paw_backend.orchestrator.domain import ExecutionPlacement
from paw_backend.orchestrator.store import DagStore
from paw_backend.projects.records import ProjectStatus
from paw_backend.tasks import (
    Actor,
    PullRequestInfo,
    PullRequestState,
    TaskCommand,
    WorktreeState,
)
from paw_backend.tasks.queueing import BudgetPreset, Priority

from .authz_support import StaticProvider
from .compute_support import GIB, FakeControl, FakeProbe, ManualClock, default_specs
from .orchestrator_support import make_plan, node
from .projects_support import DELETION_RETENTION, T0
from .repositories_support import PostgresRepositoryTestCase
from .support import make_settings
from .task_support import (
    FIRST_RUN,
    PASSED_REVIEW,
    TEST_DATABASE_URL,
    requires_postgres,
    single_target,
)

QUIET = {"event_heartbeat_seconds": 3600}


@requires_postgres
class TasksApiTest(PostgresRepositoryTestCase):
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
        execution = self.app.state.task_execution
        self.tasks = execution.tasks
        self.queue = execution.queue
        self.budget = execution.budget
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
        self.contributor = uuid.uuid4()

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

    async def new_task(
        self,
        title: str = "Fix login",
        *,
        project: uuid.UUID | None = None,
        repository: uuid.UUID | None = None,
        creator: uuid.UUID | None = None,
    ) -> uuid.UUID:
        event = await self.tasks.create_task(
            project_id=project or self.project,
            created_by=creator or self.creator,
            title=title,
            repositories=single_target(repository or self.repo),
        )
        return event.task_id

    async def apply(self, task_id: uuid.UUID, *commands: TaskCommand) -> None:
        for command in commands:
            await self.tasks.execute(task_id, command, actor=Actor.system())

    async def running_task(self, **options) -> uuid.UUID:
        task_id = await self.new_task(**options)
        await self.apply(task_id, TaskCommand.START)
        return task_id

    async def control(self, task_id: uuid.UUID, **body) -> httpx.Response:
        return await self.client.post(f"/api/v1/tasks/{task_id}/controls", json=body)

    async def version(self, task_id: uuid.UUID) -> int:
        return (await self.tasks.restore(task_id, log_limit=0)).version

    def queue_rows(self, task_id: uuid.UUID) -> list[dict]:
        return self.rows(
            "SELECT status, priority FROM queue_entries WHERE task_id = :t ORDER BY id",
            t=task_id,
        )

    def denials(self) -> list[tuple[str, str]]:
        return [
            (event.action, event.reason)
            for event in self.audit.events
            if event.decision == "deny"
        ]

    # -- the list ---------------------------------------------------------------------

    async def test_nobody_is_signed_in(self):
        for path in ("/api/v1/tasks", "/api/v1/pull-requests"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"]["code"], "unauthorized")

    async def test_the_list_holds_the_tasks_of_readable_projects_only(self):
        mine = await self.running_task()
        await self.queue.enqueue(mine, priority=Priority.HIGH)
        queued = await self.new_task("Write docs")
        foreign = await self.new_task(
            "Other team", project=self.other_project, repository=self.other_repo
        )
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.VIEWER})

        response = await self.client.get("/api/v1/tasks")

        self.assertEqual(response.status_code, 200)
        tasks = {item["id"]: item for item in response.json()["tasks"]}
        self.assertEqual(set(tasks), {str(mine), str(queued)})
        self.assertNotIn(str(foreign), tasks)
        self.assertEqual(tasks[str(mine)]["state"], "running")
        self.assertEqual(tasks[str(mine)]["priority"], "high")
        self.assertEqual(tasks[str(mine)]["repository"], "web-app")
        self.assertEqual(tasks[str(mine)]["project_name"], "Alpha")
        self.assertIsNotNone(tasks[str(mine)]["started_at"])
        self.assertIsNone(tasks[str(queued)]["started_at"])
        self.assertIsNone(tasks[str(queued)]["priority"])
        # Newest first.
        self.assertEqual(response.json()["tasks"][0]["id"], str(queued))

    async def test_a_limit_bounds_the_list(self):
        for number in range(3):
            await self.new_task(f"Task {number}")
        self.as_creator()
        response = await self.client.get("/api/v1/tasks", params={"limit": 2})
        self.assertEqual(len(response.json()["tasks"]), 2)
        for limit in (0, 201, "x"):
            response = await self.client.get("/api/v1/tasks", params={"limit": limit})
            self.assertEqual(response.status_code, 422)

    async def test_no_capacity_without_a_compute_scheduler(self):
        self.as_creator()
        response = await self.client.get("/api/v1/tasks")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["capacity"])

    async def with_compute(self) -> FakeProbe:
        """The application again, with a compute scheduler over the fake GPU (the
        main model, the Memory Worker and the embedding model on it)."""
        specs = default_specs()
        probe = FakeProbe(total=96 * GIB)
        control = FakeControl(probe, specs)
        for spec in specs:
            control.start_on_gpu(spec.name)
        self.app = create_app(
            make_settings(database_url=TEST_DATABASE_URL),
            compute=ComputeSetup(
                ComputeConfig(deployments=specs),
                probe,
                control=control,
                clock=ManualClock(),
            ),
        )
        self.app.state.authorizer = Authorizer(self.audit)
        self.app.state.principal_provider = self.provider
        self.addAsyncCleanup(self.app.state.database.dispose)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        )
        self.addAsyncCleanup(self.client.aclose)
        await self.app.state.compute.scheduler.refresh()
        return probe

    async def test_the_list_has_the_parallel_limit_of_the_scheduler(self):
        await self.with_compute()
        scheduler = self.app.state.compute.scheduler
        lease = (
            await scheduler.try_acquire(
                ComputeRequest(
                    ResourceClass.CODING, deployment="main", context_tokens=8_000
                )
            )
        ).lease
        self.addAsyncCleanup(lease.release)
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.VIEWER})

        response = await self.client.get("/api/v1/tasks")

        self.assertEqual(response.status_code, 200)
        # The running agent; (112,066 - 8,000) // 65,536 = 1 more of a whole
        # context. A person without System Health's detail sees no VRAM.
        self.assertEqual(
            response.json()["capacity"],
            {
                "parallel_limit": 2,
                "running": 1,
                "vram_used_bytes": None,
                "vram_total_bytes": None,
            },
        )
        self.assertEqual(self.denials(), [])  # a filter, not a denial

    async def test_system_healths_viewers_also_see_the_vram(self):
        probe = await self.with_compute()
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            with self.subTest(role=role):
                self.sign_in(uuid.uuid4(), system_role=role)
                capacity = (await self.client.get("/api/v1/tasks")).json()["capacity"]
                self.assertEqual(capacity["parallel_limit"], 1)
                self.assertEqual(capacity["vram_total_bytes"], 96 * GIB)
                self.assertEqual(capacity["vram_used_bytes"], probe.used)
        # Without a fresh reading: no VRAM, and no local agent would start.
        probe.fail = True
        await self.app.state.compute.scheduler.refresh()
        capacity = (await self.client.get("/api/v1/tasks")).json()["capacity"]
        self.assertEqual(capacity["parallel_limit"], 0)
        self.assertIsNone(capacity["vram_used_bytes"])
        self.assertIsNone(capacity["vram_total_bytes"])

    async def test_an_invitation_or_a_deleted_project_shows_nothing(self):
        await self.new_task()
        # Not a member at all (an Admin is no member by being an Admin).
        self.sign_in(uuid.uuid4(), {}, SystemRole.ADMIN)
        self.assertEqual((await self.client.get("/api/v1/tasks")).json()["tasks"], [])
        self.set_project(
            self.project,
            status=ProjectStatus.DELETED.value,
            name="Deleted Project",
            deletion_started_at=T0,
            deletion_scheduled_at=T0 + DELETION_RETENTION,
            deleted_at=T0 + DELETION_RETENTION,
        )
        self.as_creator()
        self.assertEqual((await self.client.get("/api/v1/tasks")).json()["tasks"], [])

    async def test_a_repository_the_person_may_not_read_is_left_out(self):
        task_id = await self.new_task(repository=self.hidden_repo)
        self.as_creator()

        listed = (await self.client.get("/api/v1/tasks")).json()["tasks"]
        self.assertEqual(listed[0]["repository"], None)
        detail = (await self.client.get(f"/api/v1/tasks/{task_id}")).json()
        self.assertEqual(detail["repositories"], [])
        self.assertNotIn("secret-infra", str(detail))

    # -- the detail ---------------------------------------------------------------

    async def test_the_detail_has_the_working_set_dag_step_and_budget(self):
        task_id = await self.running_task()
        await self.budget.set_preset(task_id, BudgetPreset.LONG)
        await self.tasks.update_attempt(
            task_id,
            run=FIRST_RUN,
            repository_id=self.repo,
            worktree=WorktreeState(branch="paw/fix-login", path="/srv/wt/1"),
        )
        step = await self.tasks.begin_step(task_id, "implement", run=FIRST_RUN)
        call = await self.tasks.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="run_tests"
        )
        store = DagStore(self.app.state.database)
        dag = await store.create(
            task_id,
            1,
            make_plan(node("plan", role="planner"), node("impl", "plan")),
            run=FIRST_RUN,
        )
        dag = await store.acquire(dag.id, "worker-1", FIRST_RUN)
        attempt = await store.start_node(dag.id, dag.epoch, "plan", max_attempts=3)
        await store.record_placement(
            dag.id,
            dag.epoch,
            "plan",
            attempt.number,
            placement=ExecutionPlacement.LOCAL_GPU,
            agent="local",
            model="qwen-coder",
        )
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.VIEWER})

        response = await self.client.get(f"/api/v1/tasks/{task_id}")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["state"], "running")
        self.assertEqual(body["version"], await self.version(task_id))
        self.assertEqual(body["created_by"], str(self.creator))
        self.assertEqual(body["attempt"], 1)
        self.assertEqual(body["retry_count"], 0)
        self.assertEqual(body["repository"], "web-app")
        self.assertEqual(
            body["repositories"],
            [
                {
                    "repository_id": str(self.repo),
                    "name": "web-app",
                    "role": "target",
                    "branch": "paw/fix-login",
                    "worktree": "/srv/wt/1",
                    "review": "not_started",
                    "evaluation": "not_run",
                    "pull_request": None,
                }
            ],
        )
        self.assertEqual(body["current_step"]["name"], "implement")
        self.assertEqual(body["current_step"]["status"], "running")
        self.assertEqual(
            [
                (c["id"], c["name"], c["status"])
                for c in body["current_step"]["tool_calls"]
            ],
            [(str(call.id), "run_tests", "started")],
        )
        self.assertEqual(body["budget"]["preset"], "long")
        self.assertEqual(
            {item["kind"] for item in body["budget"]["usage"]},
            {
                "runtime_seconds",
                "steps",
                "retries",
                "tool_calls",
                "tokens",
                "gpu_seconds",
            },
        )
        plan, impl = body["dag"]
        self.assertEqual(
            (plan["key"], plan["role"], plan["state"], plan["depends_on"]),
            ("plan", "planner", "running", []),
        )
        self.assertEqual((plan["agent"], plan["model"]), ("local", "qwen-coder"))
        self.assertEqual(
            [
                (a["number"], a["state"], a["placement"], a["agent"])
                for a in plan["attempts"]
            ],
            [(1, "running", "local_gpu", "local")],
        )
        self.assertEqual((impl["state"], impl["depends_on"]), ("pending", ["plan"]))
        self.assertEqual(impl["attempts"], [])
        # Nothing the screens do not show: no input, logs, goals or results.
        self.assertNotIn("input", body)
        self.assertNotIn("goal", plan)

    async def test_a_task_without_dag_or_budget(self):
        task_id = await self.new_task()
        self.as_creator()
        body = (await self.client.get(f"/api/v1/tasks/{task_id}")).json()
        self.assertIsNone(body["dag"])
        self.assertIsNone(body["budget"])
        self.assertIsNone(body["current_step"])

    async def test_another_project_or_no_task_is_forbidden_alike(self):
        foreign = await self.new_task(
            project=self.other_project, repository=self.other_repo
        )
        self.as_creator(ProjectRole.MANAGER)
        for task_id in (foreign, uuid.uuid4()):
            for response in (
                await self.client.get(f"/api/v1/tasks/{task_id}"),
                await self.control(task_id, command="cancel", expected_version=1),
            ):
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "forbidden")
        self.assertEqual(
            sorted(set(self.denials())),
            [
                ("project.read", "invalid_resource"),
                ("project.read", "not_project_member"),
                ("project.task.run", "invalid_resource"),
                ("project.task.run", "not_project_member"),
            ],
        )
        self.assertEqual(
            (await self.tasks.restore(foreign, log_limit=0)).state.value, "queued"
        )

    async def test_a_malformed_id_is_refused(self):
        self.as_creator()
        response = await self.client.get("/api/v1/tasks/not-a-uuid")
        self.assertIn(response.status_code, (403, 422))

    # -- controls ---------------------------------------------------------------------

    async def test_pause_with_the_version_the_operator_saw(self):
        task_id = await self.running_task()
        seen = await self.version(task_id)
        self.as_creator()

        response = await self.control(task_id, command="pause", expected_version=seen)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "paused")
        self.assertEqual(response.json()["version"], seen + 1)
        last = (await self.tasks.history(task_id))[-1]
        self.assertEqual(last.command, TaskCommand.PAUSE)
        self.assertEqual(last.actor, Actor.user(self.creator))
        # The control is audited (project.task.run is REQUIRED), allowed.
        self.assertIn(
            ("project.task.run", "allow"),
            [(event.action, event.decision) for event in self.audit.events],
        )

    async def test_a_stale_version_is_a_conflict(self):
        task_id = await self.running_task()
        seen = await self.version(task_id)
        await self.apply(task_id, TaskCommand.PAUSE)
        self.as_creator()

        response = await self.control(task_id, command="cancel", expected_version=seen)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "task_conflict")
        self.assertEqual((await self.tasks.restore(task_id)).state.value, "paused")

    async def test_the_transition_table_decides(self):
        task_id = await self.running_task()
        self.as_creator()
        response = await self.control(
            task_id, command="resume", expected_version=await self.version(task_id)
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "illegal_transition")
        self.assertEqual((await self.tasks.restore(task_id)).state.value, "running")

    async def test_stop_now_needs_a_reason(self):
        task_id = await self.running_task()
        self.as_creator()
        version = await self.version(task_id)
        for reason in (None, "", "   "):
            response = await self.control(
                task_id, command="stop_now", expected_version=version, reason=reason
            )
            self.assertEqual(response.status_code, 422, reason)
            self.assertEqual(
                response.json()["error"]["code"], "invalid_command_argument"
            )
        response = await self.control(
            task_id,
            command="stop_now",
            expected_version=version,
            reason="looping on the same test",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "cancelled")
        last = (await self.tasks.history(task_id))[-1]
        self.assertEqual(
            (last.command, last.reason),
            (TaskCommand.STOP_NOW, "looping on the same test"),
        )

    async def test_the_request_is_checked(self):
        task_id = await self.running_task()
        self.as_creator()
        version = await self.version(task_id)
        for body in (
            {"command": "pause"},  # no version
            {"command": "pause", "expected_version": 0},
            {"command": "pause", "expected_version": "1"},
            {"command": "pause", "expected_version": True},
            {"command": "start", "expected_version": version},  # not a control
            {"command": "complete", "expected_version": version},
            {"command": "pause", "expected_version": version, "extra": 1},
            {"command": "pause", "expected_version": version, "reason": "x" * 501},
        ):
            response = await self.control(task_id, **body)
            self.assertEqual(response.status_code, 422, body)
        response = await self.control(
            task_id, command="pause", expected_version=version, agent="codex"
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_command_argument")
        self.assertEqual((await self.tasks.restore(task_id)).state.value, "running")

    async def test_a_viewer_may_read_but_not_control(self):
        task_id = await self.running_task()
        self.sign_in(self.creator, {self.project: ProjectRole.VIEWER})
        self.assertEqual(
            (await self.client.get(f"/api/v1/tasks/{task_id}")).status_code, 200
        )
        response = await self.control(
            task_id, command="pause", expected_version=await self.version(task_id)
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn(("project.task.run", "capability_not_granted"), self.denials())
        self.assertEqual((await self.tasks.restore(task_id)).state.value, "running")

    async def test_an_archived_project_is_read_only(self):
        task_id = await self.running_task()
        self.set_project(self.project, status=ProjectStatus.ARCHIVED.value)
        self.as_creator()
        self.assertEqual(
            (await self.client.get(f"/api/v1/tasks/{task_id}")).status_code, 200
        )
        response = await self.control(
            task_id, command="cancel", expected_version=await self.version(task_id)
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn(("project.task.run", "project_state_forbids"), self.denials())

    async def test_another_member_may_stop_but_not_restart_the_task(self):
        task_id = await self.running_task()
        self.sign_in(self.contributor, {self.project: ProjectRole.CONTRIBUTOR})
        response = await self.control(
            task_id, command="pause", expected_version=await self.version(task_id)
        )
        self.assertEqual(response.status_code, 200)

        response = await self.control(
            task_id, command="resume", expected_version=await self.version(task_id)
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "task_creator_only")
        self.assertEqual((await self.tasks.restore(task_id)).state.value, "paused")

        response = await self.control(
            task_id, command="cancel", expected_version=await self.version(task_id)
        )
        self.assertEqual(response.status_code, 200)
        for command in ("restart", "retry"):
            response = await self.control(
                task_id, command=command, expected_version=await self.version(task_id)
            )
            self.assertEqual(response.status_code, 403, command)
        self.assertEqual(self.queue_rows(task_id), [])

    async def test_resume_puts_the_task_in_the_queue_again(self):
        task_id = await self.new_task()
        await self.queue.enqueue(task_id, priority=Priority.LOW)
        entry = await self.queue.claim_next("worker-1")
        await self.apply(task_id, TaskCommand.START, TaskCommand.PAUSE)
        # The worker quiesced: its entry is completed.
        await self.queue.complete(entry.id, "worker-1", entry.claim_count)
        self.as_creator()

        response = await self.control(
            task_id, command="resume", expected_version=await self.version(task_id)
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "running")
        self.assertEqual(
            self.queue_rows(task_id),
            [
                {"status": "completed", "priority": "low"},
                {"status": "queued", "priority": "low"},
            ],
        )

    async def test_a_worker_that_still_holds_the_entry_goes_on(self):
        task_id = await self.new_task()
        await self.queue.enqueue(task_id)
        await self.queue.claim_next("worker-1")
        await self.apply(task_id, TaskCommand.START, TaskCommand.PAUSE)
        self.as_creator()

        response = await self.control(
            task_id, command="resume", expected_version=await self.version(task_id)
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.queue_rows(task_id), [{"status": "claimed", "priority": "normal"}]
        )

    async def test_retry_with_another_agent_and_restart(self):
        task_id = await self.running_task()
        await self.apply(task_id, TaskCommand.FAIL)
        self.as_creator()

        response = await self.control(
            task_id,
            command="retry",
            expected_version=await self.version(task_id),
            agent="codex",
            model="gpt-5-codex",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            (body["state"], body["retry_count"], body["agent"], body["model"]),
            ("queued", 1, "codex", "gpt-5-codex"),
        )
        self.assertEqual(
            self.queue_rows(task_id), [{"status": "queued", "priority": "normal"}]
        )

        await self.queue.cancel(task_id)
        await self.apply(task_id, TaskCommand.CANCEL)
        response = await self.control(
            task_id, command="restart", expected_version=await self.version(task_id)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (response.json()["state"], response.json()["attempt"]), ("queued", 2)
        )
        self.assertEqual(self.queue_rows(task_id)[-1]["status"], "queued")

    async def test_a_refused_restart_writes_no_queue_entry(self):
        task_id = await self.running_task()
        await self.apply(task_id, TaskCommand.FAIL)
        self.as_creator()
        response = await self.control(task_id, command="restart", expected_version=1)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.queue_rows(task_id), [])

    # -- pull requests ------------------------------------------------------------

    async def completed_task_with_pull_request(self, **options) -> uuid.UUID:
        task_id = await self.running_task(**options)
        repository = options.get("repository", self.repo)
        await self.tasks.update_attempt(
            task_id,
            run=FIRST_RUN,
            repository_id=repository,
            worktree=WorktreeState(branch="paw/x/1/_integration"),
            review=PASSED_REVIEW,
            pull_request=PullRequestInfo(
                7, "https://github.com/acme/web-app/pull/7", PullRequestState.OPEN
            ),
        )
        await self.apply(task_id, TaskCommand.BEGIN_EVALUATION, TaskCommand.COMPLETE)
        return task_id

    async def test_pull_requests_with_merge_ready(self):
        done = await self.completed_task_with_pull_request()
        evaluating = await self.running_task(title="Half way")
        await self.tasks.update_attempt(
            evaluating,
            run=FIRST_RUN,
            repository_id=self.repo,
            review=PASSED_REVIEW,
            pull_request=PullRequestInfo(
                8, "https://github.com/acme/web-app/pull/8", PullRequestState.OPEN
            ),
        )
        await self.completed_task_with_pull_request(
            project=self.other_project, repository=self.other_repo
        )
        hidden = await self.completed_task_with_pull_request(
            repository=self.hidden_repo
        )
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.VIEWER})

        response = await self.client.get("/api/v1/pull-requests")

        self.assertEqual(response.status_code, 200)
        items = {item["number"]: item for item in response.json()["pull_requests"]}
        self.assertEqual(set(items), {7, 8})
        ready = items[7]
        self.assertEqual(ready["task_id"], str(done))
        self.assertEqual(
            {
                key: ready[key]
                for key in (
                    "url",
                    "state",
                    "title",
                    "repository",
                    "branch",
                    "base",
                    "review",
                    "evaluation",
                    "merge_ready",
                )
            },
            {
                "url": "https://github.com/acme/web-app/pull/7",
                "state": "open",
                "title": "Fix login",
                "repository": "web-app",
                "branch": "paw/x/1/_integration",
                "base": "main",
                "review": "approved",
                "evaluation": "passed",
                "merge_ready": True,
            },
        )
        self.assertFalse(items[8]["merge_ready"])
        self.assertNotIn(str(hidden), str(response.json()))

        # The task's detail links the same record.
        detail = (await self.client.get(f"/api/v1/tasks/{done}")).json()
        self.assertEqual(detail["repositories"][0]["pull_request"]["id"], ready["id"])

    async def test_the_limit_counts_readable_pull_requests_only(self):
        # A newer record of a repository the person may not read must not take
        # the place of an older readable one (Codex P2 on #196).
        readable = await self.completed_task_with_pull_request()
        await self.completed_task_with_pull_request(repository=self.hidden_repo)
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.VIEWER})

        response = await self.client.get("/api/v1/pull-requests?limit=1")

        self.assertEqual(response.status_code, 200)
        items = response.json()["pull_requests"]
        self.assertEqual([item["task_id"] for item in items], [str(readable)])

    async def test_one_pull_request_by_its_id_beyond_the_list(self):
        # The PR screen opens a record the bounded list does not hold (Codex P2
        # on #196).
        older = await self.completed_task_with_pull_request(title="Older")
        await self.completed_task_with_pull_request(title="Newer")
        self.as_creator(ProjectRole.VIEWER)
        listed = (await self.client.get("/api/v1/pull-requests?limit=1")).json()
        self.assertEqual(
            [item["task_title"] for item in listed["pull_requests"]], ["Newer"]
        )
        record_id = (await self.client.get(f"/api/v1/tasks/{older}")).json()[
            "repositories"
        ][0]["pull_request"]["id"]

        response = await self.client.get(f"/api/v1/pull-requests/{record_id}")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], record_id)
        self.assertEqual(body["task_id"], str(older))
        self.assertEqual(body["task_title"], "Older")
        self.assertTrue(body["merge_ready"])

    async def test_an_unreadable_or_missing_pull_request_is_not_found_alike(self):
        hidden = await self.completed_task_with_pull_request(
            repository=self.hidden_repo
        )
        foreign = await self.completed_task_with_pull_request(
            project=self.other_project, repository=self.other_repo
        )
        ids = [
            self.rows(
                "SELECT id FROM task_attempt_repositories WHERE task_id = :t", t=task
            )[0]["id"]
            for task in (hidden, foreign)
        ]
        self.sign_in(uuid.uuid4(), {self.project: ProjectRole.VIEWER})
        for record_id in (*ids, max(ids) + 1000):
            response = await self.client.get(f"/api/v1/pull-requests/{record_id}")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "pull_request_not_found")
        response = await self.client.get("/api/v1/pull-requests/not-a-number")
        self.assertEqual(response.status_code, 422)

    async def test_a_merged_or_superseded_pull_request_is_not_merge_ready(self):
        task_id = await self.completed_task_with_pull_request()
        await self.tasks.update_attempt(
            task_id,
            run=FIRST_RUN,
            repository_id=self.repo,
            pull_request=PullRequestInfo(
                7, "https://github.com/acme/web-app/pull/7", PullRequestState.MERGED
            ),
        )
        self.as_creator()
        items = (await self.client.get("/api/v1/pull-requests")).json()["pull_requests"]
        self.assertEqual([item["merge_ready"] for item in items], [False])

    async def test_there_is_no_merge_route(self):
        self.as_creator()
        for path in ("/api/v1/pull-requests/1/merge", "/api/v1/tasks/x/merge"):
            response = await self.client.post(path, json={})
            self.assertIn(response.status_code, (404, 405))
