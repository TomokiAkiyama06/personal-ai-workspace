"""Revision 0282: ``agent_dag_nodes.assigned_agent``, the agent the accepted
plan assigned each node (issue #297, Decision 0099, 4).

* the values the revision writes out are the code's (the agent label's form);
* it follows 0281 and is ``expand`` (an older release never names the column);
* the upgrade adds a NULL column and its CHECK and grants nothing (the
  application role's INSERT on the table covers it; it has no UPDATE of it);
  the downgrade drops them;
* on PostgreSQL, the model and the migrated table do not differ
  (autogenerate), a row written before the revision stays NULL and valid, the
  CHECK refuses a value that is not a label, and the revision can be applied
  again after a downgrade.
"""

import importlib.util
import io
import unittest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from paw_backend.db import Base
from paw_backend.orchestrator import limits
from paw_backend.orchestrator.models import DagNodeRow

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_deploy_release import load_tool
from .test_migration_grants import VERSIONS
from .test_migrations import offline_config

REVISION = "0282"
# The revision this one follows: the chain is re-linked when other revisions
# merge first, and this constant and the migration then change together.
PREVIOUS = "0281"
TABLE = "agent_dag_nodes"
TASK = "00000000-0000-0000-0000-0000000002a2"
DAG = "00000000-0000-0000-0000-0000000002a3"


def revision_module():
    spec = importlib.util.spec_from_file_location(
        "revision_0282", VERSIONS / "0282_dag_node_assigned_agent.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


class ValuesTest(unittest.TestCase):
    def test_the_revision_writes_the_values_of_the_code(self):
        revision = revision_module()
        self.assertEqual(revision.TABLE, DagNodeRow.__tablename__)
        self.assertEqual(revision.AGENT_LABEL_PATTERN, limits.AGENT_LABEL_PATTERN)
        self.assertIn(revision.COLUMN, DagNodeRow.__table__.columns.keys())
        self.assertTrue(DagNodeRow.__table__.columns[revision.COLUMN].nullable)

    def test_it_follows_the_head_it_was_written_on(self):
        self.assertEqual(previous_revision(), PREVIOUS)

    def test_an_older_release_runs_on_this_schema(self):
        # Decision 0079, 4: a NULL column that an older release never names.
        self.assertEqual(revision_module().paw_compatibility, "expand")
        migrations = load_tool().read_migrations(VERSIONS)
        self.assertEqual(migrations[REVISION].compatibility, "expand")


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str, **environment: str) -> str:
        output = io.StringIO()
        with paw_environment(
            PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw", **environment
        ):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_upgrade(self):
        sql = self.sql(
            "upgrade",
            f"{previous_revision()}:{REVISION}",
            PAW_APP_DATABASE_ROLE="paw_app",
        )
        self.assertIn(
            "ALTER TABLE agent_dag_nodes ADD COLUMN assigned_agent VARCHAR(64)", sql
        )
        self.assertNotIn("NOT NULL", sql)
        self.assertNotIn("DEFAULT", sql)
        self.assertIn("ck_agent_dag_nodes_assigned_agent_format", sql)
        self.assertIn("assigned_agent IS NULL OR assigned_agent ~", sql)
        # Existing rows are not rewritten, and nothing is granted (no UPDATE).
        self.assertNotIn("UPDATE agent_dag_nodes", sql)
        self.assertNotIn("GRANT", sql)

    def test_downgrade(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn("DROP CONSTRAINT ck_agent_dag_nodes_assigned_agent_format", sql)
        self.assertIn("ALTER TABLE agent_dag_nodes DROP COLUMN assigned_agent", sql)


def only_this(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name == TABLE
    table = getattr(obj, "table", None)
    return table is None or table.name == TABLE


INSERT_TASK = text(
    "INSERT INTO tasks (id, project_id, created_by, title, input, state, attempt,"
    " retry_count, version, created_at, updated_at) VALUES (:task,"
    " gen_random_uuid(), gen_random_uuid(), 'T', '{}', 'running', 1, 0, 1, now(),"
    " now())"
)
INSERT_DAG = text(
    "INSERT INTO agent_dags (id, task_id, attempt, node_count, plan_bytes)"
    " VALUES (:dag, :task, 1, 2, 10)"
)
INSERT_NODE = text(
    "INSERT INTO agent_dag_nodes (dag_id, key, ordinal, role, title, goal, input,"
    " required, state) VALUES (:dag, :key, :ordinal, 'worker', 'T', 'G', '{}',"
    " true, 'ready')"
)


@requires_postgres
class PostgresMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls) -> None:
        migrate("upgrade", "head")
        cls.engine.dispose()

    def columns(self) -> set[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = current_schema() AND table_name = :t"
                ),
                {"t": TABLE},
            )
            return {row[0] for row in rows}

    def test_the_model_and_the_migration_do_not_differ(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_object": only_this,
                },
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_a_row_written_before_stays_null_and_the_check_holds(self):
        migrate("upgrade", "head")
        migrate("downgrade", previous_revision())
        try:
            with self.engine.begin() as connection:
                connection.execute(INSERT_TASK, {"task": TASK})
                connection.execute(INSERT_DAG, {"dag": DAG, "task": TASK})
                connection.execute(
                    INSERT_NODE, {"dag": DAG, "key": "old", "ordinal": 0}
                )
            migrate("upgrade", REVISION)
            with self.engine.connect() as connection, connection.begin() as outer:
                value = connection.scalar(
                    text(
                        "SELECT assigned_agent FROM agent_dag_nodes"
                        " WHERE dag_id = :dag AND key = 'old'"
                    ),
                    {"dag": DAG},
                )
                self.assertIsNone(value)
                for bad in ("Claude", "claude opus", "", "1local", "local\n"):
                    with (
                        self.subTest(bad),
                        self.assertRaises(IntegrityError),
                        connection.begin_nested(),
                    ):
                        connection.execute(
                            text(
                                "UPDATE agent_dag_nodes SET assigned_agent = :a"
                                " WHERE dag_id = :dag"
                            ),
                            {"a": bad, "dag": DAG},
                        )
                connection.execute(
                    text(
                        "UPDATE agent_dag_nodes SET assigned_agent = 'claude'"
                        " WHERE dag_id = :dag"
                    ),
                    {"dag": DAG},
                )
                outer.rollback()
        finally:
            with self.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM agent_dag_nodes WHERE dag_id = :dag"),
                    {"dag": DAG},
                )
                connection.execute(
                    text("DELETE FROM agent_dags WHERE id = :dag"), {"dag": DAG}
                )
                connection.execute(
                    text("DELETE FROM tasks WHERE id = :task"), {"task": TASK}
                )

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        migrate("upgrade", REVISION)
        migrate("downgrade", previous_revision())
        self.assertNotIn("assigned_agent", self.columns())
        migrate("upgrade", REVISION)
        self.assertIn("assigned_agent", self.columns())


if __name__ == "__main__":
    unittest.main()
