"""Revision 0341: the Admin's policy of the CLI agents' models (issue #341,
Decision 0103, 5 A, 7 A and 9 A).

* the values the revision writes out are the code's (the kinds, the model's
  shape, the description's length, the probe's triggers) and, without
  ``policy``, revision 0306's triggers;
* it follows 0325 and is ``expand``; the upgrade creates both tables with
  their indexes, grants the application role SELECT / INSERT / DELETE on the
  policy and SELECT / INSERT only on its history, and rebuilds the probe's
  ``trigger`` CHECK with ``policy``; the downgrade undoes all of it;
* on PostgreSQL, the models and the migrated tables do not differ
  (autogenerate), the CHECKs hold, one default per kind, the trigger CHECK
  accepts ``policy``; the downgrade fails while a probe holds ``policy`` and
  the revision can be applied again after it.
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

from paw_backend.agent_models.models import (
    CHANGE_TABLE,
    POLICY_TABLE,
    AgentModelPolicyChangeRow,
    AgentModelPolicyRow,
)
from paw_backend.agent_models.policy import MAX_DESCRIPTION_CHARS
from paw_backend.connections.domain import ConnectionKind
from paw_backend.connections.models import MODEL_SQL_PATTERN
from paw_backend.connections.probe import ProbeTrigger
from paw_backend.db import Base

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migration_grants import VERSIONS
from .test_migrations import offline_config

REVISION = "0341"
HANDLE = "cred_" + "c" * 32
TABLES = (POLICY_TABLE, CHANGE_TABLE)


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def revision_module():
    return load("revision_0341", "0341_agent_model_policies.py")


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


class ValuesTest(unittest.TestCase):
    def test_the_revision_writes_the_values_of_the_code(self):
        revision = revision_module()
        self.assertEqual(revision.POLICY_TABLE, AgentModelPolicyRow.__tablename__)
        self.assertEqual(revision.CHANGE_TABLE, AgentModelPolicyChangeRow.__tablename__)
        self.assertEqual(revision.KINDS, tuple(kind.value for kind in ConnectionKind))
        self.assertEqual(revision.MODEL_PATTERN, MODEL_SQL_PATTERN)
        self.assertEqual(revision.MAX_DESCRIPTION_CHARS, MAX_DESCRIPTION_CHARS)
        self.assertEqual(revision.TRIGGERS, tuple(t.value for t in ProbeTrigger))
        self.assertEqual(revision.TRIGGERS[-1], ProbeTrigger.POLICY)

    def test_the_old_triggers_are_the_ones_of_revision_0306(self):
        probes = load("revision_0306", "0306_connection_probes.py")
        self.assertEqual(revision_module().PREVIOUS_TRIGGERS, probes.TRIGGERS)

    def test_it_follows_the_model_fallback_code_and_is_an_expand(self):
        self.assertEqual(previous_revision(), "0325")
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
        self.assertIn("CREATE TABLE agent_model_policies", sql)
        self.assertIn(
            "CONSTRAINT pk_agent_model_policies PRIMARY KEY (kind, model)", sql
        )
        self.assertIn(
            "CREATE UNIQUE INDEX uq_agent_model_policies_default ON"
            " agent_model_policies (kind) WHERE is_default",
            sql,
        )
        self.assertIn("CREATE TABLE agent_model_policy_changes", sql)
        self.assertIn("CREATE INDEX ix_agent_model_policy_changes_changed_at", sql)
        self.assertIn(
            'GRANT DELETE, INSERT, SELECT ON agent_model_policies TO "paw_app"', sql
        )
        self.assertIn(
            'GRANT INSERT, SELECT ON agent_model_policy_changes TO "paw_app"', sql
        )
        self.assertEqual(sql.count("GRANT "), 2)
        self.assertNotIn("UPDATE", sql.replace("UPDATE alembic_version", ""))
        self.assertIn(
            "ALTER TABLE connection_probes DROP CONSTRAINT"
            " ck_connection_probes_trigger_valid;",
            sql,
        )
        self.assertIn(
            "ALTER TABLE connection_probes ADD CONSTRAINT"
            " ck_connection_probes_trigger_valid CHECK (trigger IN ('attach',"
            " 'replace', 'manual', 'policy'));",
            sql,
        )

    def test_downgrade(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn("DROP TABLE agent_model_policy_changes", sql)
        self.assertIn("DROP TABLE agent_model_policies", sql)
        self.assertEqual(sql.count("DROP TABLE"), 2)
        self.assertIn(
            "ck_connection_probes_trigger_valid CHECK (trigger IN ('attach',"
            " 'replace', 'manual'));",
            sql,
        )


def only_these(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in TABLES
    table = getattr(obj, "table", None)
    return table is None or table.name in TABLES


@requires_postgres
class PostgresMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls) -> None:
        migrate("upgrade", "head")
        with cls.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM connection_probes WHERE secret_handle = :h"),
                {"h": HANDLE},
            )
        cls.engine.dispose()

    def tables(self) -> set[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return {row[0] for row in rows}

    def insert_probe(self, connection, trigger: str) -> None:
        connection.execute(
            text(
                "INSERT INTO connection_probes (id, connection_id, kind,"
                " secret_handle, trigger, state, started_at) VALUES (:id,"
                " :connection, 'codex', :handle, :trigger, 'running', now())"
            ),
            {
                "id": uuid.uuid4(),
                "connection": uuid.uuid4(),
                "handle": HANDLE,
                "trigger": trigger,
            },
        )

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

    def test_the_checks_hold(self):
        migrate("upgrade", "head")
        insert = text(
            "INSERT INTO agent_model_policies (kind, model, is_default,"
            " description, updated_by, updated_at) VALUES (:kind, :model,"
            " :default, :description, :by, now())"
        )
        good = {
            "kind": "codex",
            "model": "gpt-6-sol",
            "default": True,
            "description": "Coding",
            "by": uuid.uuid4(),
        }
        with self.engine.connect() as connection, connection.begin() as transaction:
            connection.execute(text("DELETE FROM agent_model_policies"))
            connection.execute(insert, good)
            connection.execute(
                insert, {**good, "model": "gpt-6-luna", "default": False}
            )
            connection.execute(
                insert, {**good, "kind": "claude", "model": "claude-opus-5-5"}
            )
            for name, values in {
                "the same model twice": {**good, "default": False},
                "a second default of the kind": {**good, "model": "gpt-6-astra"},
                "unknown kind": {**good, "kind": "gemini", "default": False},
                "a bad model": {**good, "model": "a model", "default": False},
                "an empty description": {**good, "model": "x", "default": False,
                                         "description": ""},
                "a long description": {**good, "model": "y", "default": False,
                                       "description": "d" * 121},
            }.items():  # fmt: skip
                with (
                    self.subTest(name),
                    self.assertRaises(IntegrityError),
                    connection.begin_nested(),
                ):
                    connection.execute(insert, values)
            self.insert_probe(connection, "policy")
            with (
                self.subTest("unknown trigger"),
                self.assertRaises(IntegrityError),
                connection.begin_nested(),
            ):
                self.insert_probe(connection, "policies")
            transaction.rollback()

    def test_the_downgrade_fails_while_a_probe_holds_policy(self):
        migrate("upgrade", REVISION)
        with self.engine.begin() as connection:
            self.insert_probe(connection, "policy")
        try:
            with self.assertRaises(IntegrityError):
                migrate("downgrade", previous_revision())
            self.assertIn(POLICY_TABLE, self.tables())
        finally:
            with self.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM connection_probes WHERE secret_handle = :h"),
                    {"h": HANDLE},
                )
        migrate("downgrade", previous_revision())
        self.assertFalse(set(TABLES) & self.tables())
        self.assertIn("connection_probes", self.tables())
        migrate("upgrade", REVISION)
        self.assertTrue(set(TABLES) <= self.tables())


if __name__ == "__main__":
    unittest.main()
