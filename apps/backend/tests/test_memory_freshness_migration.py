"""Revision 0042: the index of the freshness jobs (PAW-042).

The first class needs no server. The PostgreSQL class (skipped unless
``PAW_TEST_DATABASE_URL`` is set) runs the migration up and down, and checks that
the index can serve the jobs' queries. The models and the migrations as a whole are
compared by ``tests/test_memory_migration.py`` (autogenerate and catalog at head).
"""

import importlib.util
import io
import unittest
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from paw_backend.memory.models import MemoryVersion

from .memory_support import migrate, requires_postgres, sync_database_url
from .retrieval_pg_support import FREE_INDEXES
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0042"
PREVIOUS = "0041"
INDEX = "ix_memory_versions_freshness_due"
VERSION_FILE = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0042_memory_freshness_index.py"
)


def load_migration():
    spec = importlib.util.spec_from_file_location("revision_0042", VERSION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MigrationDefinitionTest(unittest.TestCase):
    def test_the_revision_follows_the_head_of_main(self):
        script = ScriptDirectory.from_config(offline_config(io.StringIO()))
        self.assertEqual(script.get_revision(REVISION).down_revision, PREVIOUS)

    def test_the_model_declares_the_same_partial_index(self):
        (index,) = [i for i in MemoryVersion.__table__.indexes if i.name == INDEX]
        self.assertEqual([c.name for c in index.columns], ["freshness_policy"])
        self.assertEqual(
            str(index.dialect_options["postgresql"]["where"]),
            load_migration().PREDICATE,
        )

    def sql(self, action: str, revisions: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_upgrade_creates_one_index_and_nothing_else(self):
        sql = self.sql("upgrade", f"{PREVIOUS}:{REVISION}")
        self.assertEqual(sql.count("CREATE INDEX"), 1)
        self.assertIn(
            f"CREATE INDEX {INDEX} ON memory_versions (freshness_policy)", sql
        )
        self.assertIn(
            "WHERE status = 'active' AND freshness_policy <> 'permanent'", sql
        )
        for statement in ("CREATE TABLE", "ALTER TABLE", "GRANT", "TRIGGER"):
            self.assertNotIn(statement, sql)

    def test_downgrade_drops_only_the_index(self):
        sql = self.sql("downgrade", f"{REVISION}:{PREVIOUS}")
        self.assertIn(f"DROP INDEX {INDEX}", sql)
        self.assertNotIn("DROP TABLE", sql)


@requires_postgres
class MigrationDatabaseTest(unittest.TestCase):
    def setUp(self):
        migrate("downgrade", "base")
        self.addCleanup(migrate, "downgrade", "base")
        self.engine = create_engine(sync_database_url())
        self.addCleanup(self.engine.dispose)

    def definition(self):
        with self.engine.connect() as connection:
            return connection.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
                {"n": INDEX},
            ).scalar()

    def test_up_down_and_up_again(self):
        migrate("upgrade", PREVIOUS)
        self.assertIsNone(self.definition())
        migrate("upgrade", REVISION)
        self.assertIn("(freshness_policy)", self.definition())
        migrate("downgrade", PREVIOUS)
        self.assertIsNone(self.definition())
        migrate("upgrade", REVISION)
        self.assertIsNotNone(self.definition())

    def test_the_index_serves_the_query_of_a_freshness_job(self):
        migrate("upgrade", REVISION)
        with self.engine.connect() as connection:
            # Every other droppable index goes (in a transaction that is rolled
            # back), so the plan shows whether THIS index can serve the query.
            for name in connection.execute(text(FREE_INDEXES)).scalars().all():
                if name != INDEX:
                    connection.exec_driver_sql(f"DROP INDEX {name}")
            connection.exec_driver_sql("SET enable_seqscan = off")
            plan = "\n".join(
                connection.execute(
                    text(
                        "EXPLAIN SELECT id FROM memory_versions WHERE status ="
                        " 'active' AND freshness_policy = 'revalidate'"
                        " AND stale_since IS NULL"
                    )
                ).scalars()
            )
            connection.rollback()
        self.assertIn(INDEX, plan)
