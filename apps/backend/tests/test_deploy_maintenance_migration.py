"""Revision 0191: the maintenance of an update (Issue #54, Decision 0079).

* the values the revision writes out are the model's, and it declares itself
  ``expand`` (Decision 0079 4);
* the upgrade creates the table and grants the application role SELECT only;
  the downgrade removes exactly that;
* on PostgreSQL, the model and the migrated table do not differ, there is at
  most one row, release names are checked, and the revision can be applied again
  after a downgrade.

The maintenance itself is ``test_deploy_maintenance.py``.
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
from paw_backend.tasks.queueing.models import (
    RELEASE_NAME_PATTERN,
    DeployMaintenanceRow,
)

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migration_grants import VERSIONS
from .test_migrations import offline_config

REVISION = "0191"
TABLE = DeployMaintenanceRow.__tablename__


def revision_module():
    spec = importlib.util.spec_from_file_location(
        "revision_0191", VERSIONS / "0191_deploy_maintenance.py"
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
        self.assertEqual(revision.RELEASE_NAME_PATTERN, RELEASE_NAME_PATTERN)
        self.assertEqual(revision.paw_compatibility, "expand")


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
        self.assertIn(f"CREATE TABLE {TABLE}", sql)
        self.assertIn(f"CONSTRAINT ck_{TABLE}_single_row CHECK (id = 1)", sql)
        self.assertIn(f'GRANT SELECT ON {TABLE} TO "paw_app"', sql)
        self.assertEqual(sql.count("GRANT "), 1)

    def test_downgrade(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn(f"DROP TABLE {TABLE}", sql)
        self.assertEqual(sql.count("DROP "), 1)


def only_maintenance(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name == TABLE
    table = getattr(obj, "table", None)
    return table is None or table.name == TABLE


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
                    "include_object": only_maintenance,
                },
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_one_row_with_checked_release_names(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection, connection.begin() as transaction:
            connection.execute(text(f"DELETE FROM {TABLE}"))
            connection.execute(
                text(
                    f"INSERT INTO {TABLE} (from_release, to_release)"
                    " VALUES ('20261001-abc', '20261007-def')"
                )
            )
            for statement in (
                f"INSERT INTO {TABLE} (from_release) VALUES (NULL)",
                f"INSERT INTO {TABLE} (id) VALUES (2)",
                f"UPDATE {TABLE} SET to_release = '../x'",
            ):
                with self.subTest(statement=statement):
                    with self.assertRaises(IntegrityError), connection.begin_nested():
                        connection.execute(text(statement))
            transaction.rollback()

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        migrate("upgrade", REVISION)
        migrate("downgrade", previous_revision())
        self.assertNotIn(TABLE, self.tables())
        migrate("upgrade", REVISION)
        self.assertIn(TABLE, self.tables())
