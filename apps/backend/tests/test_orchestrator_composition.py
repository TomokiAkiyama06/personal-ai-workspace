"""The production composition root of task execution (issue #125).

``create_app`` composes the task execution once, with a configured database:
the ``TaskService`` whose one listener undoes what an ended task held, the Tool
Broker with its production seams, the production ``TaskAuthority`` and, with agent
runtimes, the ``Orchestrator`` over all of them. The last test runs a composed
execution on PostgreSQL.
"""

import unittest
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from paw_backend.app import create_app
from paw_backend.authz import Authorizer, InMemoryAuditSink
from paw_backend.db import Database
from paw_backend.orchestrator import Orchestrator, OrchestratorConfig
from paw_backend.orchestrator.authority import StoredTaskAuthority
from paw_backend.orchestrator.composition import (
    build_repository_scopes,
    build_task_execution,
)
from paw_backend.orchestrator.gateway import TrackerBudgetProvider
from paw_backend.projects import ProjectStateGate
from paw_backend.repositories import RepositoryService
from paw_backend.tasks import Actor, TaskCommand
from paw_backend.tools import (
    WORKING_SET_TOOL_SPECS,
    PostgresTaskActivity,
    ToolBroker,
    ToolRunner,
)

from .gate_support import ALWAYS_ACTIVE
from .orchestrator_support import FakeRuntime
from .support import FakeDatabase, make_settings
from .task_support import single_target
from .test_scratch_janitor_lifespan import configured
from .tools_store_contract import LIMITS, new_approval
from .versioning_support import PostgresVersioningTestCase, requires_postgres


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


if __name__ == "__main__":
    unittest.main()
