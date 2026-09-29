"""Revision 0133: the placement columns of ``agent_dag_node_attempts`` (#133).

The first class needs no server: the rendered SQL, and the limits the migration
repeats (a migration is a frozen snapshot) against those of the code. The
PostgreSQL class (skipped unless ``PAW_TEST_DATABASE_URL`` is set) runs the
revision down and up over attempts that exist. That the models and the migrated
schema agree (columns and every constraint) is ``test_orchestrator_migration``;
that the application role may write exactly these columns is
``test_orchestrator_grants``.
"""

import importlib.util
import io
import pathlib
import unittest

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from paw_backend.orchestrator import limits
from paw_backend.orchestrator.domain import ExecutionPlacement

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0133"
# The revision this one follows: the chain is re-linked when other revisions merge
# first, and this constant and the migration then change together.
PREVIOUS = "0085"
COLUMNS = (
    "placement",
    "placement_agent",
    "placement_model",
    "placed_at",
    "content_fingerprint",
    "content_bytes",
    "placement_audit_id",
)
_PATH = pathlib.Path(__file__).resolve().parents[1] / (
    "migrations/versions/0133_node_attempt_placement.py"
)


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


def migration_module():
    spec = importlib.util.spec_from_file_location("migration_0133", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str, **environment: str) -> str:
        output = io.StringIO()
        with paw_environment(
            PAW_DATABASE_URL="postgresql://paw:pw@db.internal/paw", **environment
        ):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_the_revision_follows_the_head_it_was_written_on(self):
        self.assertEqual(previous_revision(), PREVIOUS)

    def test_the_migration_repeats_the_limits_of_the_code(self):
        module = migration_module()
        self.assertEqual(
            module.PLACEMENTS, tuple(member.value for member in ExecutionPlacement)
        )
        self.assertEqual(module.AGENT_LABEL_PATTERN, limits.AGENT_LABEL_PATTERN)
        self.assertEqual(module.MODEL_PATTERN, limits.MODEL_PATTERN)
        self.assertEqual(module.MAX_MODEL_CHARS, limits.MAX_MODEL_CHARS)
        self.assertEqual(module.PLACEMENT_COLUMNS, COLUMNS)

    def test_the_upgrade_adds_the_columns_the_trigger_and_the_column_grant(self):
        sql = self.sql(
            "upgrade",
            f"{previous_revision()}:{REVISION}",
            PAW_APP_DATABASE_ROLE="paw_app",
        )
        for column in COLUMNS:
            self.assertIn(f"ADD COLUMN {column} ", sql)
        self.assertIn("CREATE FUNCTION paw_keep_node_attempt_placement()", sql)
        self.assertIn(
            "ENABLE ALWAYS TRIGGER tr_agent_dag_node_attempts_placement_once", sql
        )
        self.assertIn(
            "GRANT UPDATE (state, error_class, failure_signature, finished_at, "
            + ", ".join(COLUMNS)
            + ') ON agent_dag_node_attempts TO "paw_app"',
            sql,
        )
        # No DELETE, no table-level UPDATE, and audit_events is not touched.
        self.assertNotIn("GRANT DELETE", sql)
        self.assertNotIn("GRANT UPDATE ON", sql)
        self.assertNotIn("audit_events", sql)

    def test_the_downgrade_removes_what_the_upgrade_added(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn("DROP TRIGGER tr_agent_dag_node_attempts_placement_once", sql)
        self.assertIn("DROP FUNCTION paw_keep_node_attempt_placement()", sql)
        for column in COLUMNS:
            self.assertIn(f"DROP COLUMN {column}", sql)


@requires_postgres
class RoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        migrate("downgrade", "base")
        # Cleanups run last first: the rows written here are dropped with the
        # schema, then the schema comes back empty.
        self.addCleanup(migrate, "upgrade", "head")
        self.addCleanup(migrate, "downgrade", "base")
        self.engine = create_engine(sync_database_url())
        self.addCleanup(self.engine.dispose)

    def scalars(self, sql: str) -> list:
        with self.engine.connect() as connection:
            return list(connection.execute(text(sql)).scalars())

    def columns(self) -> set[str]:
        return set(
            self.scalars(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'agent_dag_node_attempts'"
            )
        )

    def test_down_and_up_over_attempts_that_exist(self):
        migrate("upgrade", previous_revision())
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tasks (id, project_id, created_by, title, input,"
                    " state, attempt, retry_count, version, created_at, updated_at)"
                    " VALUES ('00000000-0000-0000-0000-000000000001',"
                    " gen_random_uuid(), gen_random_uuid(), 'T', '{}', 'running',"
                    " 1, 0, 1, now(), now())"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO agent_dags (id, task_id, attempt, node_count,"
                    " plan_bytes) VALUES ('00000000-0000-0000-0000-000000000002',"
                    " '00000000-0000-0000-0000-000000000001', 1, 1, 10)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO agent_dag_nodes (dag_id, key, ordinal, role, title,"
                    " goal, input, required, state, attempt_count, rung_attempts)"
                    " VALUES"
                    " ('00000000-0000-0000-0000-000000000002', 'a', 0, 'worker',"
                    " 'A', 'Do it', '{}', true, 'running', 1, 1)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO agent_dag_node_attempts (dag_id, node_key, number,"
                    " agent_index, approach, epoch, state) VALUES"
                    " ('00000000-0000-0000-0000-000000000002', 'a', 1, 0, 0, 1,"
                    " 'running')"
                )
            )

        migrate("upgrade", REVISION)
        self.assertTrue(set(COLUMNS) <= self.columns())
        # An attempt from before has no placement, which every constraint takes.
        self.assertEqual(
            self.scalars("SELECT placement FROM agent_dag_node_attempts"), [None]
        )

        migrate("downgrade", previous_revision())
        self.assertEqual(set(COLUMNS) & self.columns(), set())
        self.assertEqual(
            self.scalars(
                "SELECT count(*) FROM pg_proc"
                " WHERE proname = 'paw_keep_node_attempt_placement'"
            ),
            [0],
        )
        self.assertEqual(
            self.scalars("SELECT count(*) FROM agent_dag_node_attempts"), [1]
        )

        migrate("upgrade", REVISION)
        self.assertTrue(set(COLUMNS) <= self.columns())


if __name__ == "__main__":
    unittest.main()
