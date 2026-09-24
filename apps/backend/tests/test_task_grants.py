"""The task tables in the split-role deployment, on a real PostgreSQL.

The migrations run as the owner of the schema and the backend connects as a
NON-superuser application role (``PAW_APP_DATABASE_ROLE``). These tests migrate
the test database that way, then

* run the ``TaskService`` test classes as that role, so every operation the
  service performs is proven to work with exactly the privileges the migration
  grants, and
* check that nothing else is allowed (rewriting the history, deleting rows,
  changing what identifies a task, changing the schema).

Role names are unique per run and dropped afterwards; the test user must be
allowed to create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import unittest
import uuid

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from paw_backend.db import Database
from paw_backend.tasks import (
    EvaluationResult,
    PullRequestInfo,
    PullRequestState,
    ReviewState,
    ReviewStatus,
    StepStatus,
    TaskCommand,
    TaskService,
    TaskState,
    ToolInvocationStatus,
    WaitReason,
    WorktreeState,
)

from . import test_task_races, test_task_service, test_task_tools
from .support import make_settings
from .task_support import (
    TEST_DATABASE_URL,
    PostgresTaskTestCase,
    migrate,
    new_database,
    requires_postgres,
)

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_tasks_app_{_RUN}"
OTHER_ROLE = f"paw_tasks_other_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-tasks"
C = TaskCommand

# table -> (table-level privileges, columns the application may UPDATE)
EXPECTED = {
    "tasks": (
        {"SELECT", "INSERT"},
        {
            "state",
            "wait_reason",
            "agent",
            "model",
            "attempt",
            "retry_count",
            "version",
            "updated_at",
        },
    ),
    "task_attempts": (
        {"SELECT", "INSERT"},
        {
            "branch",
            "worktree_path",
            "head_commit",
            "review_status",
            "evaluation_result",
            "pr_number",
            "pr_url",
            "pr_state",
            "updated_at",
        },
    ),
    "task_steps": ({"SELECT", "INSERT"}, {"status", "finished_at"}),
    "task_tool_invocations": ({"SELECT", "INSERT"}, {"status", "finished_at"}),
    "task_logs": ({"SELECT", "INSERT"}, set()),
    "task_events": ({"SELECT", "INSERT"}, set()),
}
ALL_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


def role_database(role: str) -> Database:
    url = make_url(TEST_DATABASE_URL).set(username=role, password=ROLE_PASSWORD)
    return Database(
        make_settings(database_url=url.render_as_string(hide_password=False))
    )


async def owner_sql(sql: str) -> None:
    database = new_database()
    try:
        async with database.engine.begin() as connection:
            await connection.execute(text(sql))
    finally:
        await database.dispose()


async def drop_roles() -> None:
    database = new_database()
    try:
        async with database.engine.begin() as connection:
            for role in (APP_ROLE, OTHER_ROLE):
                exists = await connection.execute(
                    text("SELECT count(*) FROM pg_roles WHERE rolname = :r"),
                    {"r": role},
                )
                if exists.scalar():
                    await connection.execute(text(f"DROP OWNED BY {role}"))
                    await connection.execute(text(f"DROP ROLE {role}"))
    finally:
        await database.dispose()


def setUpModule():
    if not TEST_DATABASE_URL:
        raise unittest.SkipTest("PAW_TEST_DATABASE_URL is not set")
    asyncio.run(drop_roles())
    for role in (APP_ROLE, OTHER_ROLE):
        asyncio.run(
            owner_sql(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                f"PASSWORD '{ROLE_PASSWORD}'"
            )
        )
    # Recreate the tables with the grants of the split-role deployment.
    migrate("base", downgrade=True)
    migrate(PAW_APP_DATABASE_ROLE=APP_ROLE)


def tearDownModule():
    if TEST_DATABASE_URL:
        asyncio.run(drop_roles())


class AsAppRole:
    """Mixed into a ``PostgresTaskTestCase``: its databases connect as the app role."""

    def new_database(self) -> Database:
        database = role_database(APP_ROLE)
        self.addAsyncCleanup(database.dispose)
        return database


# The service test classes, unchanged, but every statement runs as the
# unprivileged role. (Classes that need raw owner SQL are not repeated here.)
class CreateTaskAsAppRole(AsAppRole, test_task_service.CreateTaskTest):
    pass


class TransitionsAsAppRole(AsAppRole, test_task_service.TransitionTest):
    pass


class StepsAndLogsAsAppRole(AsAppRole, test_task_service.StepAndLogTest):
    pass


class ToolCallsAsAppRole(AsAppRole, test_task_tools.ToolInvocationTest):
    pass


class SupersededWorkersAsAppRole(AsAppRole, test_task_races.SupersededWorkerTest):
    pass


@requires_postgres
class AppRolePrivilegesTest(AsAppRole, PostgresTaskTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.other = role_database(OTHER_ROLE)
        self.addAsyncCleanup(self.other.dispose)

    async def refused(self, database: Database, sql: str, **parameters):
        async with database.session() as session:
            with self.assertRaises(DBAPIError) as caught:
                await session.execute(text(sql), parameters)
                await session.commit()
        self.assertIsInstance(
            caught.exception.orig, psycopg.errors.InsufficientPrivilege
        )

    async def rows(self, sql: str, **parameters):
        async with self.database.engine.connect() as connection:
            return (await connection.execute(text(sql), parameters)).all()

    async def test_the_service_really_runs_as_a_non_superuser_role(self):
        for database in (self.database, self.other):
            async with database.engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT current_user, rolsuper, rolcreaterole, "
                            "rolbypassrls FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).one()
            self.assertIn(row[0], (APP_ROLE, OTHER_ROLE))
            self.assertEqual(tuple(row[1:]), (False, False, False))

    async def test_the_expectations_cover_every_task_table(self):
        tables = {
            row[0]
            for row in await self.rows(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
                "AND tablename LIKE 'task%'"
            )
        }
        self.assertEqual(tables, set(EXPECTED))

    async def test_the_app_role_holds_exactly_the_least_privileges(self):
        for table, (privileges, update_columns) in EXPECTED.items():
            with self.subTest(table=table):
                for privilege in ALL_PRIVILEGES:
                    (row,) = await self.rows(
                        "SELECT has_table_privilege(:r, :t, :p)",
                        r=APP_ROLE,
                        t=table,
                        p=privilege,
                    )
                    self.assertEqual(row[0], privilege in privileges, privilege)
                columns = [
                    name
                    for (name,) in await self.rows(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :t",
                        t=table,
                    )
                ]
                updatable = set()
                for column in columns:
                    (row,) = await self.rows(
                        "SELECT has_column_privilege(:r, :t, :c, 'UPDATE')",
                        r=APP_ROLE,
                        t=table,
                        c=column,
                    )
                    if row[0]:
                        updatable.add(column)
                self.assertEqual(updatable, update_columns)

    async def test_a_role_without_grants_reaches_no_task_table(self):
        for table in EXPECTED:
            with self.subTest(table=table):
                await self.refused(self.other, f"SELECT count(*) FROM {table}")
                await self.refused(self.other, f"DELETE FROM {table}")

    async def test_the_app_role_cannot_rewrite_the_history_or_remove_anything(self):
        task_id = await self.create_task(title="Original title", input={"a": 1})
        await self.service.execute(task_id, C.START, actor=self.system)
        step = await self.service.begin_step(task_id, "work", attempt=1)
        await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="shell"
        )
        await self.service.add_log(task_id, "kept", attempt=1)
        before = await self.service.restore(task_id)
        history = await self.service.history(task_id)

        forbidden = [
            # The history and the logs are only ever added to.
            "UPDATE task_events SET reason = 'edited'",
            "DELETE FROM task_events",
            "TRUNCATE task_events",
            "UPDATE task_logs SET message = 'edited'",
            "DELETE FROM task_logs",
            "TRUNCATE task_logs",
            # Nothing is deleted anywhere.
            "DELETE FROM tasks",
            "DELETE FROM task_attempts",
            "DELETE FROM task_steps",
            "DELETE FROM task_tool_invocations",
            "TRUNCATE tasks CASCADE",
            # What identifies a task, and what it was asked to do, never changes.
            "UPDATE tasks SET title = 'x'",
            "UPDATE tasks SET input = '{}'",
            "UPDATE tasks SET project_id = gen_random_uuid()",
            "UPDATE tasks SET created_by = gen_random_uuid()",
            "UPDATE tasks SET starting_commit = 'x'",
            "UPDATE tasks SET id = gen_random_uuid()",
            "UPDATE task_steps SET name = 'x'",
            "UPDATE task_steps SET task_id = gen_random_uuid()",
            "UPDATE task_attempts SET number = 99",
            "UPDATE task_tool_invocations SET tool_name = 'x'",
            # The schema and the append-only trigger belong to the migration role.
            "ALTER TABLE task_events DISABLE TRIGGER ALL",
            "DROP TRIGGER task_events_append_only ON task_events",
            "ALTER TABLE tasks ADD COLUMN extra text",
            "DROP TABLE task_events",
        ]
        for sql in forbidden:
            with self.subTest(sql=sql):
                await self.refused(self.database, sql)

        after = await self.service.restore(task_id)
        self.assertEqual(after, before)
        self.assertEqual(after.title, "Original title")
        self.assertEqual(await self.service.history(task_id), history)

    async def test_the_app_role_can_run_a_whole_task_through_a_second_process(self):
        service = TaskService(self.new_database())
        task_id = await self.create_task(service)
        commands = [
            (C.START, None),
            (C.WAIT, WaitReason.APPROVAL),
            (C.UNBLOCK, None),
            (C.PAUSE, None),
            (C.RESUME, None),
            (C.BEGIN_EVALUATION, None),
            (C.COMPLETE, None),
        ]
        for command, wait_reason in commands[:3]:
            await service.execute(
                task_id, command, actor=self.system, wait_reason=wait_reason
            )
        step = await service.begin_step(task_id, "implement", attempt=1)
        call = await service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="shell"
        )
        await service.add_log(task_id, "working", attempt=1)
        await service.update_attempt(
            task_id,
            attempt=1,
            worktree=WorktreeState("agent/task", "/srv/worktrees/task", "a" * 40),
            review=ReviewState(ReviewStatus.APPROVED, EvaluationResult.PASSED),
            pull_request=PullRequestInfo(
                5, "https://example.test/pr/5", PullRequestState.OPEN
            ),
        )
        for command, _ in commands[3:5]:
            await service.execute(task_id, command, actor=self.user)
        await service.finish_tool_invocation(
            task_id, call.id, ToolInvocationStatus.SUCCEEDED
        )
        await service.finish_step(task_id, step.id, StepStatus.SUCCEEDED)
        for command, _ in commands[5:]:
            await service.execute(task_id, command, actor=self.system)

        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.COMPLETED)
        self.assertEqual(snapshot.attempt.pull_request.number, 5)
        self.assertEqual(
            snapshot.tool_invocations[0].status, ToolInvocationStatus.SUCCEEDED
        )
        self.assertEqual(
            [event.command for event in await self.service.history(task_id)],
            [C.CREATE] + [command for command, _ in commands],
        )

    async def test_failure_retry_restart_and_stop_now_work_as_the_app_role(self):
        task_id = await self.task_in_state(TaskState.FAILED)
        await self.service.execute(task_id, C.RETRY, actor=self.user, agent="codex")
        await self.service.execute(task_id, C.START, actor=self.system)
        await self.service.execute(task_id, C.STOP_NOW, actor=self.user)
        await self.service.execute(task_id, C.RESTART, actor=self.user, model="big")
        snapshot = await self.service.restore(task_id)
        self.assertEqual(
            (snapshot.state, snapshot.attempt.number, snapshot.retry_count),
            (TaskState.QUEUED, 2, 1),
        )


if __name__ == "__main__":
    unittest.main()
