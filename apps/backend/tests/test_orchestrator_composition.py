"""The production composition root of task execution (issue #125).

``create_app`` composes the task execution once, with a configured database:
the ``TaskService`` whose one listener undoes what an ended task held, the Tool
Broker with its production seams, the production ``TaskAuthority`` and, with agent
runtimes, the ``Orchestrator`` over all of them, with the Parallel Worktree /
Integration Node (``GitWorktreeCoordinator``, issue #155). The last tests run a
composed execution on PostgreSQL, one of them with real git on a checkout of the
user running the tests (``SubprocessGitRunner``: no SSH, no other Linux user).
"""

import asyncio
import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from paw_backend.app import create_app
from paw_backend.authz import Authorizer, InMemoryAuditSink
from paw_backend.db import Database
from paw_backend.integration import GitWorktreeCoordinator
from paw_backend.orchestrator import Orchestrator, OrchestratorConfig
from paw_backend.orchestrator.authority import StoredTaskAuthority
from paw_backend.orchestrator.composition import (
    build_repository_scopes,
    build_task_execution,
)
from paw_backend.orchestrator.domain import RunOutcome
from paw_backend.orchestrator.gateway import TrackerBudgetProvider
from paw_backend.projects import ProjectStateGate
from paw_backend.repositories import (
    LoginNameAccountDirectory,
    RepositoryService,
    SubprocessGitRunner,
)
from paw_backend.tasks import (
    Actor,
    RepoRole,
    TaskCommand,
    TaskState,
    WorkingSetEntry,
)
from paw_backend.tasks.queueing import BudgetPreset
from paw_backend.tools import (
    WORKING_SET_TOOL_SPECS,
    PostgresTaskActivity,
    ToolBroker,
    ToolRunner,
)

from .gate_support import ALWAYS_ACTIVE
from .orchestrator_support import FakeRuntime, make_plan, node, ok
from .repositories_support import PostgresRepositoryTestCase, fs, requires_git
from .support import FakeDatabase, make_settings
from .task_support import BASELINE, single_target
from .test_scratch_janitor_lifespan import configured
from .tools_store_contract import LIMITS, new_approval
from .versioning_support import PostgresVersioningTestCase, requires_postgres
from .worktrees_support import RecordingRunner, commit_file, git


def runtimes_and_config():
    return {"local": FakeRuntime()}, OrchestratorConfig.uniform(("local",))


class ComposedAppTest(unittest.TestCase):
    def test_the_app_composes_task_execution_with_a_database(self):
        settings, database = configured()
        app = create_app(settings, database=database)
        execution = app.state.task_execution

        # One listener: the cleanup of a task's end (the approvals included).
        self.assertEqual(
            execution.tasks._listeners, (execution.task_end.on_task_event,)
        )
        self.assertIsInstance(execution.tasks._project_gate, ProjectStateGate)
        self.assertIsInstance(execution.queue._project_gate, ProjectStateGate)
        self.assertIs(execution.task_end._approvals, execution.approvals)
        self.assertIs(execution.task_end._freshness, execution.freshness)
        # The authority reads the stored Working Set and the registrations.
        authority = execution.authority
        self.assertIsInstance(authority, StoredTaskAuthority)
        self.assertIs(authority._tasks, execution.tasks)
        self.assertIsInstance(authority._repositories, RepositoryService)
        # The Broker: the application's Authorizer and the production seams.
        broker = execution.broker
        self.assertIsInstance(broker, ToolBroker)
        self.assertIs(broker._authorizer, app.state.authorizer)
        self.assertIs(broker._use_gate, execution.tasks)
        self.assertIs(broker._registrations, authority._repositories)
        self.assertIsInstance(broker._budget, TrackerBudgetProvider)
        self.assertIsInstance(broker._task_activity, PostgresTaskActivity)
        self.assertEqual(
            broker._registry.names(),
            {spec.name for spec in WORKING_SET_TOOL_SPECS},
        )
        self.assertIsInstance(execution.tools, ToolRunner)
        self.assertIs(execution.tools._broker, broker)
        # No agent runtime: no orchestrator.
        self.assertIsNone(execution.orchestrator)

    def test_no_task_execution_without_a_database(self):
        app = create_app(make_settings(), database=FakeDatabase())
        self.assertIsNone(app.state.task_execution)

    def test_with_agent_runtimes_the_orchestrator_is_composed_too(self):
        settings, database = configured()
        runtimes, config = runtimes_and_config()
        app = create_app(
            settings,
            database=database,
            agent_runtimes=runtimes,
            orchestrator_config=config,
        )
        execution = app.state.task_execution
        orchestrator = execution.orchestrator
        self.assertIsInstance(orchestrator, Orchestrator)
        self.assertIs(orchestrator._tasks, execution.tasks)
        self.assertIs(orchestrator._queue, execution.queue)
        self.assertIs(orchestrator._budget, execution.budget)
        self.assertIs(orchestrator._authority, execution.authority)
        self.assertIs(orchestrator._tools, execution.tools)
        self.assertEqual(set(orchestrator._runtimes), {"local"})
        # The Parallel Worktree / Integration Node (issue #155, Decision 0036):
        # git as the task creator's Linux account through the deployment's
        # runner (the default one here), the policy of the settings.
        coordinator = orchestrator._worktrees
        self.assertIsInstance(coordinator, GitWorktreeCoordinator)
        self.assertIs(execution.worktrees, coordinator)
        self.assertIsInstance(coordinator._git._runner, SubprocessGitRunner)
        self.assertIsInstance(coordinator._accounts, LoginNameAccountDirectory)
        self.assertEqual(
            coordinator._accounts.min_uid, settings.repository_min_linux_uid
        )
        self.assertEqual(coordinator._subdir, settings.repository_workspace_subdir)
        self.assertEqual(
            coordinator._git._timeout, settings.repository_git_timeout_seconds
        )

    def test_the_worktrees_use_the_policy_and_the_runner_of_the_deployment(self):
        settings, database = configured(
            repository_workspace_subdir="work",
            repository_min_linux_uid=2000,
            repository_git_timeout_seconds=45.0,
        )
        runtimes, config = runtimes_and_config()
        runner = RecordingRunner()
        app = create_app(
            settings,
            database=database,
            agent_runtimes=runtimes,
            orchestrator_config=config,
            git_runner=runner,
        )
        coordinator = app.state.task_execution.orchestrator._worktrees
        self.assertIs(coordinator._git._runner, runner)
        self.assertEqual(coordinator._subdir, "work")
        self.assertEqual(coordinator._accounts.min_uid, 2000)
        self.assertEqual(coordinator._git._timeout, 45.0)

    def test_without_agent_runtimes_no_worktree_coordinator_is_built(self):
        settings, database = configured()
        app = create_app(settings, database=database, git_runner=RecordingRunner())
        self.assertIsNone(app.state.task_execution.worktrees)
        self.assertIsNone(app.state.task_execution.orchestrator)


class BuildArgumentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()
        self.database = Database(self.settings)
        self.authorizer = Authorizer(InMemoryAuditSink())

    def test_runtimes_and_their_config_come_together(self):
        runtimes, config = runtimes_and_config()
        for options in ({"runtimes": runtimes}, {"orchestrator_config": config}):
            with self.subTest(options=set(options)), self.assertRaises(TypeError):
                build_task_execution(
                    self.settings, self.database, self.authorizer, **options
                )

    def test_it_refuses_what_it_cannot_use(self):
        with self.assertRaises(TypeError):
            build_task_execution(self.settings, object(), self.authorizer)
        with self.assertRaises(TypeError):
            build_task_execution(self.settings, self.database, object())
        with self.assertRaises(TypeError):
            build_task_execution(
                self.settings, self.database, self.authorizer, repositories=object()
            )

    def test_it_refuses_a_git_runner_or_accounts_it_cannot_use(self):
        runtimes, config = runtimes_and_config()
        for options in ({"git_runner": object()}, {"accounts": object()}):
            with self.subTest(options=set(options)), self.assertRaises(TypeError):
                build_task_execution(
                    self.settings,
                    self.database,
                    self.authorizer,
                    runtimes=runtimes,
                    orchestrator_config=config,
                    **options,
                )
            # Also without runtimes: a wrong seam fails at startup, not later.
            with self.subTest(options=set(options)), self.assertRaises(TypeError):
                build_task_execution(
                    self.settings, self.database, self.authorizer, **options
                )

    def test_the_default_registrations_are_the_repository_service(self):
        self.assertIsInstance(
            build_repository_scopes(self.settings, self.database, self.authorizer),
            RepositoryService,
        )


@requires_postgres
class ComposedExecutionTest(PostgresVersioningTestCase):
    async def test_a_cancelled_task_ends_clean_through_the_composed_service(self):
        database = self._database()
        execution = build_task_execution(
            make_settings(database_url=self.database_url()),
            database,
            Authorizer(InMemoryAuditSink()),
            project_gate=ALWAYS_ACTIVE,
        )
        created = await execution.tasks.create_task(
            project_id=uuid.uuid4(),
            created_by=uuid.uuid4(),
            title="Fix the parser",
            repositories=single_target(uuid.uuid4()),
        )
        task_id = created.task_id
        memory = self.seed(
            "task note", owner=self.user().user_id, freshness="session_only"
        )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO memory_sources (memory_version_id, source_type,"
                    " source_ref) VALUES (:v, 'task', :r)"
                ),
                {"v": memory.version_id, "r": str(task_id)},
            )
        now = datetime.now(UTC)
        approval = new_approval(task_id=task_id, expires_at=now + timedelta(hours=1))
        store = execution.approvals._store
        await store.open_request(approval, now=now, limits=LIMITS)

        await execution.tasks.execute(task_id, TaskCommand.CANCEL, actor=Actor.system())

        self.assertEqual(self.versions(memory.memory_id)[0].status, "deprecated")
        self.assertEqual(
            (await store.get(approval.approval_id)).status.value, "revoked"
        )


@requires_postgres
@requires_git
class ComposedWorktreeTest(PostgresRepositoryTestCase):
    """The orchestrator of the production composition gives each writing Worker
    node a worktree of the registered checkout and integrates their branches
    before evaluation (issue #155); a task without a checkout goes on as before.
    Real: PostgreSQL, the repository service, the stored authority, git."""

    @classmethod
    def clean_tables(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(text("TRUNCATE tasks CASCADE"))
        super().clean_tables()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = self.seed_project(name="Alpha")
        self.manager = self.seed_user_with_account("alice")
        self.seed_manager(self.project_id, self.manager)
        self.home = self.account(self.manager).home
        self.runner = RecordingRunner()

    def compose(self, script):
        def working(name, content):
            async def behave(assignment):
                (worktree,) = assignment.worktrees.values()
                await asyncio.to_thread(commit_file, worktree.path, name, content)
                return ok(f"{name} written")

            return behave

        self.runtime = FakeRuntime(
            "local", script={key: working(*args) for key, args in script.items()}
        )
        database = self.service_database()
        self.addAsyncCleanup(database.dispose)
        return build_task_execution(
            make_settings(),
            database,
            Authorizer(InMemoryAuditSink()),
            project_gate=ALWAYS_ACTIVE,
            repositories=self.service,
            runtimes={"local": self.runtime},
            orchestrator_config=OrchestratorConfig.uniform(
                ("local",), retry_backoff_seconds=0.0
            ),
            git_runner=self.runner,
            accounts=self.accounts,
        )

    async def register(self, name: str):
        path = f"{self.home}/workspaces/project/{name}"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.world.make_repository(path)
        result = await self.service.register_existing(
            self.actor(self.manager), self.project_id, path, name=name
        )
        return result.repository.id, path

    async def run_task(self, execution, repositories, plan):
        created = await execution.tasks.create_task(
            project_id=self.project_id,
            created_by=self.manager,
            title="Fix the parser",
            repositories=repositories,
        )
        await execution.orchestrator.submit_plan(created.task_id, plan)
        await execution.orchestrator.enqueue_task(
            created.task_id, preset=BudgetPreset.STANDARD
        )
        report = await execution.orchestrator.run_once("w1")
        return created.task_id, report

    async def test_writing_workers_get_worktrees_and_are_integrated(self):
        repo, checkout = await self.register("app")
        main_head = git("rev-parse", "HEAD", cwd=checkout)
        execution = self.compose(
            {"a": ("a.txt", "from a\n"), "b": ("b.txt", "from b\n")}
        )

        task_id, report = await self.run_task(
            execution,
            [WorkingSetEntry(repo, RepoRole.TARGET, BASELINE)],
            make_plan(node("a"), node("b")),
        )

        self.assertEqual(report.outcome, RunOutcome.DAG_SUCCEEDED)
        base = f"{self.home}/workspaces/.paw-worktrees/{task_id}/1/{repo}"
        (a,) = self.runtime.calls_of("a")
        (b,) = self.runtime.calls_of("b")
        self.assertEqual(a.worktrees[repo].path, f"{base}/a")
        self.assertEqual(b.worktrees[repo].path, f"{base}/b")
        self.assertEqual(a.worktrees[repo].branch, f"paw/{task_id}/1/a")
        snapshot = await execution.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.EVALUATING)
        integration = snapshot.attempt.repository(repo).worktree
        self.assertEqual(integration.path, f"{base}/_integration")
        self.assertEqual(integration.branch, f"paw/{task_id}/1/_integration")
        self.assertEqual(fs.read(integration.path, "a.txt"), "from a\n")
        self.assertEqual(fs.read(integration.path, "b.txt"), "from b\n")
        # The user's checkout and its default branch did not move.
        self.assertEqual(git("rev-parse", "main", cwd=checkout), main_head)
        self.assertEqual(git("status", "--porcelain", cwd=checkout), "")
        self.assertFalse(
            {"push", "fetch", "checkout", "reset"} & set(self.runner.subcommands())
        )

    async def test_a_task_without_a_checkout_goes_on_as_before(self):
        # Registered, but Alice has no checkout of it: out of the scope, so no
        # node gets a worktree, nothing is integrated and no git runs.
        elsewhere = self.seed_repository(self.project_id, name="elsewhere")
        execution = self.compose({})

        task_id, report = await self.run_task(
            execution,
            [WorkingSetEntry(elsewhere, RepoRole.TARGET, BASELINE)],
            make_plan(node("a")),
        )

        self.assertEqual(report.outcome, RunOutcome.DAG_SUCCEEDED)
        (a,) = self.runtime.calls_of("a")
        self.assertEqual(dict(a.worktrees), {})
        snapshot = await execution.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.EVALUATING)
        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":
    unittest.main()
