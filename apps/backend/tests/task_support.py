"""Shared fixtures for the task tests that need a real PostgreSQL."""

import asyncio
import contextlib
import io
import json
import os
import re
import unittest
import uuid

from alembic import command as alembic_command
from sqlalchemy import event, text

from paw_backend.db import Database
from paw_backend.tasks import (
    Actor,
    TaskCommand,
    TaskEvent,
    TaskRun,
    TaskService,
    TaskState,
    WaitReason,
)

from .support import make_settings, paw_environment
from .test_migrations import offline_config

TEST_DATABASE_URL = os.environ.get("PAW_TEST_DATABASE_URL")

requires_postgres = unittest.skipUnless(
    TEST_DATABASE_URL, "PAW_TEST_DATABASE_URL is not set"
)


def migrate(
    revision: str = "head", *, downgrade: bool = False, **environment: str
) -> None:
    """Run Alembic against the test database (synchronously; no event loop).

    ``environment`` adds variables such as ``PAW_APP_DATABASE_ROLE``.
    """
    with paw_environment(PAW_DATABASE_URL=TEST_DATABASE_URL, **environment):
        config = offline_config(io.StringIO())
        if downgrade:
            alembic_command.downgrade(config, revision)
        else:
            alembic_command.upgrade(config, revision)


def new_database() -> Database:
    return Database(make_settings(database_url=TEST_DATABASE_URL))


# The run of a task that has not been retried or restarted: what a worker started
# by the first Start event passes to ``begin_step`` / ``add_log`` / ``update_attempt``.
FIRST_RUN = TaskRun(1, 0)


def command_reason(command: TaskCommand) -> str | None:
    """The ``reason`` a test passes with ``command``.

    Stop Now is refused without a reason (REQUIREMENTS: the reason of an
    emergency stop is kept in the Audit / Task log); every other command is
    called without one unless the test is about reasons.
    """
    return "emergency stop" if command is TaskCommand.STOP_NOW else None


# How to bring a fresh (queued) task into each state through legal commands.
PATH_TO_STATE: dict[TaskState, list[tuple[TaskCommand, WaitReason | None]]] = {
    TaskState.QUEUED: [],
    TaskState.RUNNING: [(TaskCommand.START, None)],
    TaskState.WAITING: [(TaskCommand.START, None), (TaskCommand.WAIT, WaitReason.USER)],
    TaskState.PAUSED: [(TaskCommand.START, None), (TaskCommand.PAUSE, None)],
    TaskState.EVALUATING: [
        (TaskCommand.START, None),
        (TaskCommand.BEGIN_EVALUATION, None),
    ],
    TaskState.COMPLETED: [
        (TaskCommand.START, None),
        (TaskCommand.BEGIN_EVALUATION, None),
        (TaskCommand.COMPLETE, None),
    ],
    TaskState.FAILED: [(TaskCommand.START, None), (TaskCommand.FAIL, None)],
    TaskState.CANCELLED: [(TaskCommand.CANCEL, None)],
}


class PostgresTaskTestCase(unittest.IsolatedAsyncioTestCase):
    """A service on its own connection pool, against a migrated database."""

    @classmethod
    def setUpClass(cls):
        migrate()

    @classmethod
    def tearDownClass(cls):
        async def clean():
            database = new_database()
            try:
                async with database.engine.begin() as connection:
                    # TRUNCATE fires no row triggers, so the append-only history
                    # of the throwaway test database can still be emptied.
                    await connection.execute(text("TRUNCATE tasks CASCADE"))
            finally:
                await database.dispose()

        asyncio.run(clean())

    async def asyncSetUp(self):
        self.database = self.new_database()
        self.service = TaskService(self.database)
        self.project_id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.user = Actor.user(self.user_id)
        self.system = Actor.system()

    def new_database(self) -> Database:
        """Another independent engine, as a second backend process would have."""
        database = new_database()
        self.addAsyncCleanup(database.dispose)
        return database

    async def create_task(
        self, service: TaskService | None = None, **overrides
    ) -> uuid.UUID:
        arguments = {
            "project_id": self.project_id,
            "created_by": self.user_id,
            "title": "Fix the parser",
        }
        arguments.update(overrides)
        event = await (service or self.service).create_task(**arguments)
        return event.task_id

    async def task_in_state(
        self, state: TaskState, service: TaskService | None = None
    ) -> uuid.UUID:
        service = service or self.service
        task_id = await self.create_task(service)
        for command, wait_reason in PATH_TO_STATE[state]:
            await service.execute(
                task_id, command, actor=self.system, wait_reason=wait_reason
            )
        return task_id

    async def scalar(self, sql: str, **parameters):
        async with self.database.engine.connect() as connection:
            return (await connection.execute(text(sql), parameters)).scalar()

    async def generic_plan(self, sql: str, parameters: dict) -> str:
        """The plan PostgreSQL caches for a prepared statement (parameters unknown).

        A driver prepares a statement it runs often, and PostgreSQL may then
        stop planning it for each set of parameter values; a partial index can
        only be used by such a plan when the index condition is written into
        the statement, not passed as a parameter.
        """
        return json.dumps(await self.plan(sql, parameters, "force_generic_plan"))

    async def plan(
        self, sql: str, parameters: dict, mode: str, *, analyze: bool = False
    ) -> list:
        """``EXPLAIN`` of the statement as a prepared statement planned in ``mode``.

        ``mode`` is a value of ``plan_cache_mode`` (``force_generic_plan`` plans
        without the parameter values, ``force_custom_plan`` with them).
        ``analyze`` also runs the statement and reports the rows each node read.
        """
        names = list(dict.fromkeys(re.findall(r"%\((\w+)\)s", sql)))
        numbered = re.sub(
            r"%\((\w+)\)s", lambda m: f"${names.index(m.group(1)) + 1}", sql
        )

        def literal(value) -> str:
            if value is None:
                return "NULL"
            if isinstance(value, int):
                return str(value)
            return "'" + str(value).replace("'", "''") + "'"

        arguments = ", ".join(literal(parameters[name]) for name in names)
        options = "ANALYZE, FORMAT JSON" if analyze else "FORMAT JSON"
        async with self.database.engine.connect() as connection:
            await connection.exec_driver_sql(f"SET plan_cache_mode = {mode}")
            # With a handful of matching rows the planner may choose a bitmap scan
            # and a Sort over it, depending on the ANALYZE sample: the same plan
            # the index serves in order, so the tests rule it out to stay stable.
            await connection.exec_driver_sql("SET enable_bitmapscan = off")
            await connection.exec_driver_sql(f"PREPARE checked_statement AS {numbered}")
            try:
                result = await connection.exec_driver_sql(
                    f"EXPLAIN ({options}) EXECUTE checked_statement"
                    + (f"({arguments})" if names else "")
                )
                return result.scalar()
            finally:
                # The connection goes back to the pool: leave no session state.
                await connection.exec_driver_sql("DEALLOCATE checked_statement")
                await connection.exec_driver_sql("RESET plan_cache_mode")
                await connection.exec_driver_sql("RESET enable_bitmapscan")

    @staticmethod
    def plan_nodes(plan: dict):
        """Every node of an ``EXPLAIN (FORMAT JSON)`` plan tree, parents first."""
        yield plan
        for child in plan.get("Plans", ()):
            yield from PostgresTaskTestCase.plan_nodes(child)

    @contextlib.contextmanager
    def captured_statements(self):
        """The SQL statements (and their parameters) the service sends meanwhile."""
        captured: list[tuple[str, dict]] = []

        def capture(connection, cursor, statement, parameters, context, many):
            captured.append((statement, dict(parameters)))

        engine = self.database.engine.sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            yield captured
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    async def wait_for_lock_waiters(self, count: int, limit: float = 10.0) -> None:
        """Wait until ``count`` backends are blocked on a lock held by another one.

        This is how the race tests control the interleaving: a step in the
        sequence is only started once the previous one is provably waiting.
        """
        async with asyncio.timeout(limit):
            while True:
                waiting = await self.scalar(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
                if waiting >= count:
                    return
                await asyncio.sleep(0.02)

    async def events(self, task_id: uuid.UUID) -> list[TaskEvent]:
        return await self.service.history(task_id)
