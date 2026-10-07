"""Revision 0183: the agent incidents of System Health (issue #183, Decision 0071).

* the values the revision writes out are the model's and the enum's;
* the upgrade creates the table and its index and grants the application role
  SELECT / INSERT / DELETE only; the downgrade removes exactly that;
* on PostgreSQL, the model and the migrated table do not differ (autogenerate),
  the kind is checked, and the revision can be applied again after a downgrade.

The writes and the counts are ``test_orchestrator_store.py``,
``test_orchestrator_failures.py``, ``test_orchestrator_planning.py`` and
``test_health_postgres.py`` (also as the application role, through
``test_orchestrator_grants.py`` and ``test_health_grants.py``).
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
from paw_backend.orchestrator.domain import IncidentKind
from paw_backend.orchestrator.models import INCIDENTS_TABLE, AgentIncidentRow

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migration_grants import VERSIONS
from .test_migrations import offline_config

REVISION = "0183"


def revision_module():
    spec = importlib.util.spec_from_file_location(
        "revision_0183", VERSIONS / "0183_agent_incidents.py"
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
        self.assertEqual(revision.KINDS, tuple(kind.value for kind in IncidentKind))
        self.assertEqual(AgentIncidentRow.__tablename__, INCIDENTS_TABLE)
        # The index of this revision (revision 0189 adds the one of ``task_id``).
        (index,) = (
            index
            for index in AgentIncidentRow.__table__.indexes
            if [column.name for column in index.columns] == ["occurred_at"]
        )
        self.assertEqual(revision.INDEX, index.name)


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
        self.assertIn("CREATE TABLE agent_incidents", sql)
        self.assertIn(
            "CONSTRAINT ck_agent_incidents_kind_valid CHECK"
            " (kind IN ('out_of_memory', 'escalation'))",
            sql,
        )
        self.assertIn(
            "CREATE INDEX ix_agent_incidents_occurred_at ON agent_incidents"
            " (occurred_at)",
            sql,
        )
        self.assertIn(
            'GRANT DELETE, INSERT, SELECT ON agent_incidents TO "paw_app"', sql
        )
        # Nothing on another table, and never UPDATE.
        self.assertEqual(sql.count("GRANT "), 1)
        self.assertNotIn("UPDATE", sql.split("GRANT", 1)[1].split(";", 1)[0])

    def test_downgrade(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn("DROP INDEX ix_agent_incidents_occurred_at", sql)
        self.assertIn("DROP TABLE agent_incidents", sql)
        self.assertEqual(sql.count("DROP "), 2)


def only_incidents(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name == INCIDENTS_TABLE
    table = getattr(obj, "table", None)
    return table is None or table.name == INCIDENTS_TABLE


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
                    "include_object": only_incidents,
                },
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_only_a_known_kind_is_stored(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection, connection.begin() as transaction:
            connection.execute(
                text("INSERT INTO agent_incidents (kind) VALUES ('escalation')")
            )
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text("INSERT INTO agent_incidents (kind) VALUES ('oom')")
                )
            transaction.rollback()

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        migrate("upgrade", REVISION)
        migrate("downgrade", previous_revision())
        self.assertNotIn(INCIDENTS_TABLE, self.tables())
        self.assertIn("health_events", self.tables())  # the revision before
        migrate("upgrade", REVISION)
        self.assertIn(INCIDENTS_TABLE, self.tables())


if __name__ == "__main__":
    unittest.main()
