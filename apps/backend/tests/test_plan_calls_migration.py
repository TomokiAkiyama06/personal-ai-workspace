"""Revision 0281: ``agent_plan_calls``, the record of the planner's calls
(issue #297, Decision 0099, 3).

* the values the revision writes out are the code's (the placements, the
  patterns and limits of revision 0133, the planner's attempts);
* it follows 0280 and is ``expand`` (an older release never names the table);
* the upgrade creates the table, its CHECKs, its UNIQUE, its write-once trigger
  and grants the application role SELECT, INSERT and UPDATE of the seven
  placement columns only (no DELETE); the downgrade drops them;
* on PostgreSQL, the model and the migrated table do not differ
  (autogenerate), a call without a placement is a valid row, a partial or
  unaudited placement is refused, two calls of one run cannot share a ``seq``,
  a recorded placement is never changed or erased, and the revision can be
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
from sqlalchemy.exc import DBAPIError, IntegrityError

from paw_backend.db import Base
from paw_backend.orchestrator import limits
from paw_backend.orchestrator.domain import ExecutionPlacement
from paw_backend.orchestrator.models import PLAN_CALLS_TABLE, PlanCallRow

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_deploy_release import load_tool
from .test_migration_grants import VERSIONS
from .test_migrations import offline_config

REVISION = "0281"
# The revision this one follows: the chain is re-linked when other revisions
# merge first, and this constant and the migration then change together.
PREVIOUS = "0280"
COLUMNS = (
    "placement",
    "placement_agent",
    "placement_model",
    "placed_at",
    "content_fingerprint",
    "content_bytes",
    "placement_audit_id",
)
TASK = "00000000-0000-0000-0000-0000000002a1"


def revision_module():
    spec = importlib.util.spec_from_file_location(
        "revision_0281", VERSIONS / "0281_agent_plan_calls.py"
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
        self.assertEqual(revision.TABLE, PLAN_CALLS_TABLE)
        self.assertEqual(
            revision.PLACEMENTS, tuple(member.value for member in ExecutionPlacement)
        )
        self.assertEqual(revision.AGENT_LABEL_PATTERN, limits.AGENT_LABEL_PATTERN)
        self.assertEqual(revision.MODEL_PATTERN, limits.MODEL_PATTERN)
        self.assertEqual(revision.MAX_MODEL_CHARS, limits.MAX_MODEL_CHARS)
        self.assertEqual(revision.MAX_PLAN_ATTEMPTS, limits.MAX_PLAN_ATTEMPTS)
        self.assertEqual(revision.PLACEMENT_COLUMNS, COLUMNS)
        self.assertLessEqual(set(COLUMNS), set(PlanCallRow.__table__.columns.keys()))

    def test_it_follows_the_head_it_was_written_on(self):
        self.assertEqual(previous_revision(), PREVIOUS)

    def test_an_older_release_runs_on_this_schema(self):
        # Decision 0079, 4: a new table that an older release never names.
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
        self.assertIn("CREATE TABLE agent_plan_calls (", sql)
        for column in ("seq INTEGER NOT NULL", "owner VARCHAR(100) NOT NULL"):
            self.assertIn(column, sql)
        self.assertIn(
            "CONSTRAINT uq_agent_plan_calls_task_id"
            " UNIQUE (task_id, task_attempt, retry_count, seq)",
            sql,
        )
        for check in (
            "counters_valid",
            "claim_call_in_range",
            "placement_complete",
            "cloud_is_audited",
        ):
            self.assertIn(f"ck_agent_plan_calls_{check}", sql)
        self.assertIn("CREATE FUNCTION paw_keep_plan_call_placement()", sql)
        self.assertIn("ENABLE ALWAYS TRIGGER tr_agent_plan_calls_placement_once", sql)
        self.assertIn('GRANT INSERT, SELECT ON agent_plan_calls TO "paw_app"', sql)
        self.assertIn(
            "GRANT UPDATE ("
            + ", ".join(COLUMNS)
            + ') ON agent_plan_calls TO "paw_app"',
            sql,
        )
        # No DELETE (the record is kept) and no table-level UPDATE.
        self.assertNotIn("GRANT DELETE", sql)
        self.assertNotIn(", DELETE", sql)
        self.assertNotIn("GRANT UPDATE ON", sql)

    def test_downgrade(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn("DROP TRIGGER tr_agent_plan_calls_placement_once", sql)
        self.assertIn("DROP FUNCTION paw_keep_plan_call_placement()", sql)
        self.assertIn("DROP TABLE agent_plan_calls", sql)


def only_this(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name == PLAN_CALLS_TABLE
    table = getattr(obj, "table", None)
    return table is None or table.name == PLAN_CALLS_TABLE


INSERT = text(
    "INSERT INTO agent_plan_calls (task_id, task_attempt, retry_count, seq,"
    " claim_call, owner, placement, placement_agent, placement_model, placed_at,"
    " content_fingerprint, content_bytes, placement_audit_id) VALUES (:task,"
    " :attempt, :retry, :seq, :call, :owner, :placement, :agent, :model, :at,"
    " :fingerprint, :bytes, :audit)"
)
UNPLACED = {
    "task": TASK,
    "attempt": 1,
    "retry": 0,
    "seq": 1,
    "call": 1,
    "owner": "w1",
    "placement": None,
    "agent": None,
    "model": None,
    "at": None,
    "fingerprint": None,
    "bytes": None,
    "audit": None,
}
CLOUD = {
    **UNPLACED,
    "placement": "cloud",
    "agent": "claude",
    "model": "claude-opus",
    "at": "2026-10-24T00:00:00Z",
    "fingerprint": "sha256:" + "ab" * 32,
    "bytes": 10,
    "audit": str(uuid.uuid4()),
}


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
                text(
                    "SELECT tablename FROM pg_tables"
                    " WHERE schemaname = current_schema()"
                )
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

    def test_the_constraints_and_the_trigger_hold(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection, connection.begin() as transaction:
            connection.execute(
                text(
                    "INSERT INTO tasks (id, project_id, created_by, title, input,"
                    " state, attempt, retry_count, version, created_at, updated_at)"
                    " VALUES (:task, gen_random_uuid(), gen_random_uuid(), 'T',"
                    " '{}', 'running', 1, 0, 1, now(), now())"
                ),
                {"task": TASK},
            )
            # A call that was never placed is a valid row.
            connection.execute(INSERT, UNPLACED)
            for values in (
                {"seq": 1},  # the same seq in the same run
                {"seq": 0},
                {"seq": 2, "call": 0},
                {"seq": 2, "call": limits.MAX_PLAN_ATTEMPTS + 1},
                {"seq": 2, "retry": -1},
                {"seq": 2, "placement": "local_gpu"},  # without its agent etc.
                {**CLOUD, "seq": 2, "audit": None},  # a cloud send unaudited
                {**CLOUD, "seq": 2, "fingerprint": "not a fingerprint"},
                {**CLOUD, "seq": 2, "agent": "Claude Opus"},  # text
                {**CLOUD, "seq": 2, "placement": "local_cpu"},  # audited local
            ):
                with (
                    self.subTest(values),
                    self.assertRaises(IntegrityError),
                    connection.begin_nested(),
                ):
                    connection.execute(INSERT, {**UNPLACED, **values})
            # Another run of the task numbers its calls from 1 again.
            connection.execute(INSERT, {**UNPLACED, "retry": 1})
            connection.execute(INSERT, {**CLOUD, "seq": 2})
            # The placement of an unplaced call is written once ...
            connection.execute(
                text(
                    "UPDATE agent_plan_calls SET placement = 'local_gpu',"
                    " placement_agent = 'local', placement_model = 'main',"
                    " placed_at = now() WHERE seq = 1 AND retry_count = 0"
                )
            )
            # ... and never changed or erased afterwards.
            for sql in (
                "UPDATE agent_plan_calls SET placement_model = 'other'"
                " WHERE seq = 1 AND retry_count = 0",
                "UPDATE agent_plan_calls SET placement = NULL, placement_agent ="
                " NULL, placement_model = NULL, placed_at = NULL,"
                " content_fingerprint = NULL, content_bytes = NULL,"
                " placement_audit_id = NULL WHERE seq = 2",
            ):
                with (
                    self.subTest(sql),
                    self.assertRaises(DBAPIError) as caught,
                    connection.begin_nested(),
                ):
                    connection.execute(text(sql))
                self.assertIn("recorded once", str(caught.exception))
            transaction.rollback()

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        migrate("upgrade", REVISION)
        migrate("downgrade", previous_revision())
        self.assertNotIn(PLAN_CALLS_TABLE, self.tables())
        migrate("upgrade", REVISION)
        self.assertIn(PLAN_CALLS_TABLE, self.tables())


if __name__ == "__main__":
    unittest.main()
