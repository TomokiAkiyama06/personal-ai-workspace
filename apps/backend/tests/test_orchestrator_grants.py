"""The DAG tables in the split-role deployment (real PostgreSQL).

The migrations run as the owner of the schema and the backend connects as a
NON-superuser application role (``PAW_APP_DATABASE_ROLE``). These tests migrate
the test database that way, then

* run the DAG store, orchestrator, failure, budget, control, lease, planning and
  tool test classes as that role, so every statement the orchestrator executes is
  proven to work with exactly the privileges revision 0034 grants (and, through
  the real stopper, those of the tables it reads), and
* check that nothing else is allowed (rewriting the plan or a node's identity,
  deleting history, changing keys, changing the schema).

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
from paw_backend.tasks import TaskRun

from . import (
    test_orchestrator_budget,
    test_orchestrator_control,
    test_orchestrator_failures,
    test_orchestrator_lease,
    test_orchestrator_planning,
    test_orchestrator_project_sweep,
    test_orchestrator_run,
    test_orchestrator_shutdown,
    test_orchestrator_store,
    test_orchestrator_tools,
)
from .orchestrator_support import PostgresOrchestratorTestCase, diamond
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate, new_database, requires_postgres

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_orch_app_{_RUN}"
OTHER_ROLE = f"paw_orch_other_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-orchestrator"

# table -> (table-level privileges, columns the application may UPDATE). The exact
# copy of the choices (and their reasons) in migration 0034.
EXPECTED = {
    "agent_dags": (
        {"SELECT", "INSERT"},
        {"state", "epoch", "owner", "task_retry_count", "updated_at"},
    ),
    "agent_dag_nodes": (
        {"SELECT", "INSERT"},
        {
            "state",
            "agent_index",
            "approach",
            "attempt_count",
            "rung_attempts",
            "result",
            "error_class",
            "finished_at",
            "updated_at",
        },
    ),
    "agent_dag_edges": ({"SELECT", "INSERT"}, set()),
    "agent_dag_node_attempts": (
        {"SELECT", "INSERT"},
        {"state", "error_class", "failure_signature", "finished_at"},
    ),
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
    # Recreate every table with the grants of the split-role deployment.
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
class CreateAsAppRole(AsAppRole, test_orchestrator_store.CreateTest):
    pass


class AcquireAsAppRole(AsAppRole, test_orchestrator_store.AcquireTest):
    pass


class NodeLifecycleAsAppRole(AsAppRole, test_orchestrator_store.NodeLifecycleTest):
    pass


class FencingAsAppRole(AsAppRole, test_orchestrator_store.FencingTest):
    pass


class HappyPathAsAppRole(AsAppRole, test_orchestrator_run.HappyPathTest):
    pass


class RetryAsAppRole(AsAppRole, test_orchestrator_failures.RetryTest):
    pass


class EscalationAsAppRole(AsAppRole, test_orchestrator_failures.EscalationTest):
    pass


class IsolationAsAppRole(AsAppRole, test_orchestrator_failures.IsolationTest):
    pass


class PlannerAsAppRole(AsAppRole, test_orchestrator_planning.PlannerTest):
    pass


class SubmitPlanAsAppRole(AsAppRole, test_orchestrator_planning.SubmitPlanTest):
    pass


class BudgetAsAppRole(AsAppRole, test_orchestrator_budget.BudgetTest):
    pass


class ControlAsAppRole(AsAppRole, test_orchestrator_control.ControlTest):
    pass


class LeaseAsAppRole(AsAppRole, test_orchestrator_lease.LeaseTest):
    pass


class ShutdownAsAppRole(AsAppRole, test_orchestrator_shutdown.ShutdownTest):
    pass


class ToolsAsAppRole(
    AsAppRole, test_orchestrator_tools.ToolsThroughTheOrchestratorTest
):
    pass


class ProjectSweepAsAppRole(
    AsAppRole, test_orchestrator_project_sweep.RealProjectSweepTest
):
    pass


@requires_postgres
class AppRolePrivilegesTest(AsAppRole, PostgresOrchestratorTestCase):
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

    async def snapshot(self) -> dict[str, list[dict]]:
        return {
            table: await self.rows(f"SELECT * FROM {table} ORDER BY 1, 2")
            for table in EXPECTED
        }

    async def test_the_services_really_run_as_a_non_superuser_role(self):
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

    async def test_the_expectations_cover_every_orchestrator_table(self):
        tables = {
            row["tablename"]
            for row in await self.rows(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                " AND tablename LIKE 'agent\\_dag%'"
            )
        }
        self.assertEqual(tables, set(EXPECTED))

    async def test_the_app_role_holds_exactly_the_least_privileges(self):
        for table, (privileges, update_columns) in EXPECTED.items():
            with self.subTest(table=table):
                for privilege in ALL_PRIVILEGES:
                    granted = await self.scalar(
                        "SELECT has_table_privilege(:r, :t, :p)",
                        r=APP_ROLE,
                        t=table,
                        p=privilege,
                    )
                    self.assertEqual(granted, privilege in privileges, privilege)
                columns = [
                    row["column_name"]
                    for row in await self.rows(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :t",
                        t=table,
                    )
                ]
                updatable = set()
                for column in columns:
                    if await self.scalar(
                        "SELECT has_column_privilege(:r, :t, :c, 'UPDATE')",
                        r=APP_ROLE,
                        t=table,
                        c=column,
                    ):
                        updatable.add(column)
                self.assertEqual(updatable, update_columns)

    async def test_a_role_without_grants_reaches_no_orchestrator_table(self):
        for table in EXPECTED:
            with self.subTest(table=table):
                await self.refused(self.other, f"SELECT count(*) FROM {table}")
                await self.refused(self.other, f"DELETE FROM {table}")

    async def test_the_app_role_cannot_rewrite_the_plan_history_or_the_schema(self):
        dag = await self.make_dag(diamond())
        await self.store.acquire(dag.id, "w1", TaskRun(1, 0))
        await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        before = await self.snapshot()

        forbidden = [
            # The DAG's identity never changes, and a DAG is never deleted.
            "UPDATE agent_dags SET task_id = gen_random_uuid()",
            "UPDATE agent_dags SET attempt = 9",
            "UPDATE agent_dags SET id = gen_random_uuid()",
            "UPDATE agent_dags SET node_count = 1",
            "UPDATE agent_dags SET plan_bytes = 1",
            "UPDATE agent_dags SET created_at = now()",
            "DELETE FROM agent_dags",
            "TRUNCATE agent_dags CASCADE",
            # What the plan said is fixed for good.
            "UPDATE agent_dag_nodes SET goal = 'something else'",
            "UPDATE agent_dag_nodes SET input = '{}'",
            "UPDATE agent_dag_nodes SET role = 'planner'",
            "UPDATE agent_dag_nodes SET required = false",
            "UPDATE agent_dag_nodes SET capabilities = '[]'",
            "UPDATE agent_dag_nodes SET repositories = '[]'",
            "UPDATE agent_dag_nodes SET key = 'zz'",
            "UPDATE agent_dag_nodes SET ordinal = 99",
            "UPDATE agent_dag_nodes SET title = 'x'",
            "DELETE FROM agent_dag_nodes",
            "TRUNCATE agent_dag_nodes CASCADE",
            # The dependencies are never edited or removed.
            "UPDATE agent_dag_edges SET depends_on_key = 'a'",
            "DELETE FROM agent_dag_edges",
            "TRUNCATE agent_dag_edges",
            # An attempt is a record: only how it ended is written, once.
            "UPDATE agent_dag_node_attempts SET epoch = 99",
            "UPDATE agent_dag_node_attempts SET number = 9",
            "UPDATE agent_dag_node_attempts SET agent_index = 1",
            "UPDATE agent_dag_node_attempts SET approach = 3",
            "UPDATE agent_dag_node_attempts SET started_at = now()",
            "DELETE FROM agent_dag_node_attempts",
            "TRUNCATE agent_dag_node_attempts",
            # The schema belongs to the migration role.
            "ALTER TABLE agent_dag_nodes ADD COLUMN extra text",
            "ALTER TABLE agent_dag_nodes DROP CONSTRAINT ck_agent_dag_nodes_key_format",
            "DROP TABLE agent_dag_edges",
            "ALTER TABLE agent_dags DISABLE TRIGGER ALL",
        ]
        for sql in forbidden:
            with self.subTest(sql=sql):
                await self.refused(self.database, sql)

        self.assertEqual(await self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
