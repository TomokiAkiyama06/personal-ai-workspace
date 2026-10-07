"""Revision 0189: the local usage and the task of an agent incident (issue #187
item 5, Decision 0077).

* the values the revision writes out are the model's;
* the upgrade creates ``local_usage`` with its indexes and grants the application
  role SELECT / INSERT only, and adds ``agent_incidents.task_id`` with its foreign
  key and index; the downgrade removes exactly that;
* on PostgreSQL, the models and the migrated tables do not differ (autogenerate),
  a task with local usage or incidents cannot be deleted, the rows of
  ``agent_incidents`` written before keep a NULL task, and the revision can be
  applied again after a downgrade.
"""

import importlib.util
import io
import unittest
import uuid

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from paw_backend.compute.models import (
    LOCAL_PLACEMENTS,
    LOCAL_USAGE_TABLE,
    MAX_LOCAL_SECONDS,
    MAX_LOCAL_TOKENS,
    LocalUsageRow,
)
from paw_backend.db import Base
from paw_backend.orchestrator.models import INCIDENTS_TABLE, AgentIncidentRow

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migration_grants import VERSIONS
from .test_migrations import offline_config

REVISION = "0189"


def revision_module():
    spec = importlib.util.spec_from_file_location(
        "revision_0189", VERSIONS / "0189_local_usage.py"
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
        self.assertEqual(
            revision.PLACEMENTS,
            tuple(placement.value for placement in LOCAL_PLACEMENTS),
        )
        self.assertEqual(revision.MAX_TOKENS, MAX_LOCAL_TOKENS)
        self.assertEqual(revision.MAX_SECONDS, MAX_LOCAL_SECONDS)
        self.assertEqual(revision.TABLE, LOCAL_USAGE_TABLE)
        self.assertEqual(LocalUsageRow.__tablename__, LOCAL_USAGE_TABLE)
        self.assertEqual(
            set(revision.INDEXES),
            {index.name for index in LocalUsageRow.__table__.indexes},
        )
        self.assertIn(
            revision.INCIDENT_INDEX,
            {index.name for index in AgentIncidentRow.__table__.indexes},
        )
        (foreign_key,) = AgentIncidentRow.__table__.foreign_key_constraints
        self.assertEqual(revision.INCIDENT_FOREIGN_KEY, foreign_key.name)


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
        self.assertIn("CREATE TABLE local_usage", sql)
        self.assertIn(
            "CONSTRAINT ck_local_usage_placement_valid CHECK"
            " (placement IN ('local_gpu', 'local_cpu'))",
            sql,
        )
        self.assertIn(
            "CONSTRAINT fk_local_usage_task_id_tasks FOREIGN KEY(task_id)"
            " REFERENCES tasks (id) ON DELETE RESTRICT",
            sql,
        )
        self.assertIn(
            "CREATE INDEX ix_local_usage_user_id_started_at ON local_usage"
            " (user_id, started_at)",
            sql,
        )
        self.assertIn('GRANT INSERT, SELECT ON local_usage TO "paw_app"', sql)
        # Nothing else is granted, and never UPDATE or DELETE.
        self.assertEqual(sql.count("GRANT "), 1)
        self.assertIn("ALTER TABLE agent_incidents ADD COLUMN task_id UUID", sql)
        self.assertIn(
            "ALTER TABLE agent_incidents ADD CONSTRAINT"
            " fk_agent_incidents_task_id_tasks FOREIGN KEY(task_id)"
            " REFERENCES tasks (id) ON DELETE RESTRICT",
            sql,
        )
        self.assertIn(
            "CREATE INDEX ix_agent_incidents_task_id ON agent_incidents (task_id)",
            sql,
        )

    def test_downgrade(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn("DROP INDEX ix_agent_incidents_task_id", sql)
        self.assertIn(
            "ALTER TABLE agent_incidents DROP CONSTRAINT"
            " fk_agent_incidents_task_id_tasks",
            sql,
        )
        self.assertIn("ALTER TABLE agent_incidents DROP COLUMN task_id", sql)
        self.assertIn("DROP TABLE local_usage", sql)
        # The two indexes of local_usage, the incidents' index, the table.
        self.assertEqual(sql.count("DROP INDEX"), 3)
        self.assertEqual(sql.count("DROP TABLE"), 1)


def only_these(obj, name, type_, reflected, compare_to) -> bool:
    tables = (LOCAL_USAGE_TABLE, INCIDENTS_TABLE)
    if type_ == "table":
        return name in tables
    table = getattr(obj, "table", None)
    return table is None or table.name in tables


@requires_postgres
class PostgresMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls) -> None:
        migrate("upgrade", "head")
        cls.engine.dispose()

    def tables(self) -> set[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return {row[0] for row in rows}

    def columns(self, table: str) -> set[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT column_name FROM information_schema.columns"
                     " WHERE table_name = :t"),
                {"t": table},
            )  # fmt: skip
            return {row[0] for row in rows}

    def test_the_models_and_the_migration_do_not_differ(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_object": only_these,
                },
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_a_task_with_usage_or_incidents_is_kept(self):
        migrate("upgrade", "head")
        user, task = uuid.uuid4(), uuid.uuid4()
        with self.engine.connect() as connection, connection.begin() as transaction:
            connection.execute(
                text(
                    "INSERT INTO users (id, login_name, system_role, status,"
                    " passkey_required, created_at, updated_at) VALUES (:u,"
                    " :login, 'user', 'active', false, now(), now())"
                ),
                {"u": user, "login": "u" + user.hex[:12]},
            )
            connection.execute(
                text(
                    "INSERT INTO tasks (id, project_id, created_by, title, input,"
                    " state, attempt, retry_count, version, created_at, updated_at)"
                    " VALUES (:t, :p, :u, 'Task', CAST('{}' AS jsonb), 'running',"
                    " 1, 0, 1, now(), now())"
                ),
                {"t": task, "p": uuid.uuid4(), "u": user},
            )
            connection.execute(
                text(
                    "INSERT INTO local_usage (user_id, task_id, placement, calls,"
                    " tokens, seconds, started_at) VALUES (:u, :t, 'local_gpu', 1,"
                    " 0, 0, now())"
                ),
                {"u": user, "t": task},
            )
            connection.execute(
                text(
                    "INSERT INTO agent_incidents (kind, task_id)"
                    " VALUES ('escalation', :t), ('escalation', NULL)"
                ),
                {"t": task},
            )
            with self.assertRaises(IntegrityError), connection.begin_nested():
                connection.execute(text("DELETE FROM tasks WHERE id = :t"), {"t": task})
            with self.assertRaises(IntegrityError), connection.begin_nested():
                connection.execute(
                    text("INSERT INTO agent_incidents (kind, task_id)"
                         " VALUES ('escalation', :t)"),
                    {"t": uuid.uuid4()},
                )  # fmt: skip
            transaction.rollback()

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        migrate("upgrade", REVISION)
        migrate("downgrade", previous_revision())
        self.assertNotIn(LOCAL_USAGE_TABLE, self.tables())
        self.assertNotIn("task_id", self.columns(INCIDENTS_TABLE))
        self.assertIn("notifications", self.tables())  # the revision before
        migrate("upgrade", REVISION)
        self.assertIn(LOCAL_USAGE_TABLE, self.tables())
        self.assertIn("task_id", self.columns(INCIDENTS_TABLE))


if __name__ == "__main__":
    unittest.main()
