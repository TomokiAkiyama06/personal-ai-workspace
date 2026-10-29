"""Revision 0306: the health probes of the shared connections (issue #306,
Decision 0100).

* the values the revision writes out are the code's (the enums, the shapes,
  the limits, the columns a probe's end updates);
* the upgrade creates ``connection_probes`` with its two indexes and grants the
  application role SELECT, INSERT and UPDATE of the end's columns only (never
  DELETE); the downgrade drops it;
* on PostgreSQL, the model and the migrated table do not differ
  (autogenerate), the CHECKs hold, one ``running`` probe per credential, and
  the revision can be applied again after a downgrade.
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

from paw_backend.connections.domain import ConnectionKind, FailureCode
from paw_backend.connections.limits import MAX_DURATION_MS, MAX_TOKENS_PER_CALL
from paw_backend.connections.models import (
    HANDLE_SQL_PATTERN,
    MODEL_SQL_PATTERN,
    PROBE_TABLE,
    ConnectionProbeRow,
)
from paw_backend.connections.probe import ProbeOutcome, ProbeState, ProbeTrigger
from paw_backend.db import Base

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migration_grants import VERSIONS
from .test_migrations import offline_config

REVISION = "0306"
HANDLE = "cred_" + "a" * 32


def revision_module():
    spec = importlib.util.spec_from_file_location(
        "revision_0306", VERSIONS / "0306_connection_probes.py"
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
        self.assertEqual(revision.TABLE, PROBE_TABLE)
        self.assertEqual(ConnectionProbeRow.__tablename__, PROBE_TABLE)
        self.assertEqual(revision.KINDS, tuple(kind.value for kind in ConnectionKind))
        self.assertEqual(revision.TRIGGERS, tuple(t.value for t in ProbeTrigger))
        self.assertEqual(revision.STATES, tuple(s.value for s in ProbeState))
        self.assertEqual(revision.OUTCOMES, tuple(o.value for o in ProbeOutcome))
        self.assertEqual(
            revision.FAILURE_CODES, tuple(code.value for code in FailureCode)
        )
        self.assertEqual(revision.HANDLE_PATTERN, HANDLE_SQL_PATTERN)
        self.assertEqual(revision.MODEL_PATTERN, MODEL_SQL_PATTERN)
        self.assertEqual(revision.MAX_TOKENS, MAX_TOKENS_PER_CALL)
        self.assertEqual(revision.MAX_DURATION_MS, MAX_DURATION_MS)
        columns = set(ConnectionProbeRow.__table__.columns.keys())
        started = {
            "id",
            "connection_id",
            "kind",
            "secret_handle",
            "credential_version",
            "trigger",
            "actor_id",
            "started_at",
        }
        self.assertEqual(set(revision.UPDATE_COLUMNS), columns - started)

    def test_it_follows_the_dag_node_assignment(self):
        self.assertEqual(previous_revision(), "0282")
        self.assertEqual(revision_module().paw_compatibility, "expand")


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
        self.assertIn("CREATE TABLE connection_probes", sql)
        self.assertIn("CONSTRAINT pk_connection_probes PRIMARY KEY (id)", sql)
        self.assertIn(
            "CREATE UNIQUE INDEX uq_connection_probes_running ON connection_probes"
            " (connection_id, secret_handle) WHERE state = 'running'",
            sql,
        )
        self.assertIn("CREATE INDEX ix_connection_probes_connection_id_started_at", sql)
        self.assertIn('GRANT INSERT, SELECT ON connection_probes TO "paw_app"', sql)
        self.assertIn(
            "GRANT UPDATE (state, outcome, failure_code, model, input_tokens,"
            " output_tokens, finished_at, duration_ms)"
            ' ON connection_probes TO "paw_app"',
            sql,
        )
        self.assertEqual(sql.count("GRANT "), 2)
        self.assertNotIn("DELETE", sql)

    def test_downgrade(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn("DROP TABLE connection_probes", sql)
        self.assertEqual(sql.count("DROP TABLE"), 1)


def only_this(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name == PROBE_TABLE
    table = getattr(obj, "table", None)
    return table is None or table.name == PROBE_TABLE


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

    def test_the_checks_hold(self):
        migrate("upgrade", "head")
        insert = text(
            "INSERT INTO connection_probes (id, connection_id, kind, secret_handle,"
            " credential_version, trigger, model, state, outcome, failure_code,"
            " input_tokens, started_at, finished_at, duration_ms) VALUES (:id,"
            " :connection, :kind, :handle, :version, :trigger, :model, :state,"
            " :outcome, :failure, :tokens, now(), CASE WHEN :ended THEN now()"
            " + make_interval(secs => :end) END, CASE WHEN :ended THEN 5 END)"
        )
        good = {
            "connection": uuid.uuid4(),
            "kind": "claude",
            "handle": HANDLE,
            "version": 1,
            "trigger": "manual",
            "model": None,
            "state": "running",
            "outcome": None,
            "failure": None,
            "tokens": None,
            "ended": False,
            "end": 0,
        }
        finished = {**good, "state": "finished", "outcome": "connected", "ended": True}
        with self.engine.connect() as connection, connection.begin() as transaction:
            connection.execute(insert, {**good, "id": uuid.uuid4()})
            connection.execute(insert, {**finished, "id": uuid.uuid4()})
            connection.execute(
                insert,
                {
                    **finished,
                    "id": uuid.uuid4(),
                    "outcome": "unavailable",
                    "failure": "timeout",
                    "model": "claude-x",
                    "tokens": 12,
                },
            )
            for name, values in {
                "a second running probe of the credential": good,
                "unknown kind": {**good, "connection": uuid.uuid4(), "kind": "x"},
                "unknown trigger": {**finished, "trigger": "loop"},
                "unknown state": {**finished, "state": "done"},
                "unknown outcome": {**finished, "outcome": "ok"},
                "unknown failure": {**finished, "outcome": "unavailable",
                                    "failure": "oops"},
                "not a handle": {**finished, "handle": "sk-secret"},
                "a bad model": {**finished, "model": "a model"},
                "running with an outcome": {**good, "connection": uuid.uuid4(),
                                            "outcome": "connected"},
                "finished without an outcome": {**finished, "outcome": None},
                "running with an end": {**good, "connection": uuid.uuid4(),
                                        "ended": True},
                "finished without an end": {**finished, "ended": False},
                "connected with a failure": {**finished, "failure": "timeout"},
                "an end before the start": {**finished, "end": -60},
                "negative tokens": {**finished, "tokens": -1},
                "version zero": {**finished, "version": 0},
            }.items():  # fmt: skip
                with (
                    self.subTest(name),
                    self.assertRaises(IntegrityError),
                    connection.begin_nested(),
                ):
                    connection.execute(insert, {**values, "id": uuid.uuid4()})
            transaction.rollback()

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        migrate("upgrade", REVISION)
        migrate("downgrade", previous_revision())
        self.assertNotIn(PROBE_TABLE, self.tables())
        self.assertIn("shared_connections", self.tables())
        migrate("upgrade", REVISION)
        self.assertIn(PROBE_TABLE, self.tables())


if __name__ == "__main__":
    unittest.main()
