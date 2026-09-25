"""Revision 0043: the full-text index of Hybrid Retrieval (PAW-043).

The first classes need no server. The PostgreSQL class (skipped unless
``PAW_TEST_DATABASE_URL`` is set) runs the migration up and down on a database
with data, and checks that the expression the migration, the model and the queries
use is one and the same.
"""

import importlib.util
import io
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from paw_backend.memory import fulltext
from paw_backend.memory.models import MemoryScope, MemoryVersion
from paw_backend.memory.retrieval import queries
from paw_backend.memory.retrieval.scopes import ResolvedScopes

from .memory_support import migrate, requires_postgres, sync_database_url
from .retrieval_pg_support import FREE_INDEXES
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0043"
PREVIOUS = "0026"
INDEX = "ix_memory_versions_search"
VERSION_FILE = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0043_memory_search_index.py"
)


def load_migration():
    spec = importlib.util.spec_from_file_location("revision_0043", VERSION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MigrationDefinitionTest(unittest.TestCase):
    def test_the_revision_chain(self):
        script = ScriptDirectory.from_config(offline_config(io.StringIO()))
        revision = script.get_revision(REVISION)
        self.assertEqual(revision.revision, REVISION)
        self.assertEqual(revision.down_revision, PREVIOUS)

    def test_the_frozen_expression_is_the_one_of_the_model_and_the_queries(self):
        migration = load_migration()
        self.assertEqual(migration.SEARCH_DOCUMENT, fulltext.search_document_sql())
        (index,) = [i for i in MemoryVersion.__table__.indexes if i.name == INDEX]
        (expression,) = index.expressions
        self.assertEqual(str(expression), fulltext.search_document_sql())
        # A query is the same expression over the aliased columns.
        self.assertEqual(
            fulltext.search_document_sql("mv.title", "mv.content").replace("mv.", ""),
            fulltext.search_document_sql(),
        )

    def test_the_index_is_a_partial_gin_index_of_the_active_versions(self):
        (index,) = [i for i in MemoryVersion.__table__.indexes if i.name == INDEX]
        self.assertEqual(index.dialect_options["postgresql"]["using"], "gin")
        self.assertEqual(
            str(index.dialect_options["postgresql"]["where"]), "status = 'active'"
        )

    def sql(self, action: str, revisions: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_upgrade_creates_one_index_and_no_table(self):
        sql = self.sql("upgrade", f"{PREVIOUS}:{REVISION}")
        self.assertEqual(sql.count("CREATE INDEX"), 1)
        self.assertIn(f"CREATE INDEX {INDEX} ON memory_versions USING gin", sql)
        self.assertIn("WHERE status = 'active'", sql)
        self.assertNotIn("CREATE TABLE", sql)
        self.assertNotIn("GRANT", sql)

    def test_upgrade_adds_no_approximate_nearest_neighbour_index(self):
        sql = self.sql("upgrade", f"{PREVIOUS}:{REVISION}").lower()
        self.assertNotIn("hnsw", sql)
        self.assertNotIn("ivfflat", sql)

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
        definition = self.definition()
        self.assertIn("USING gin", definition)
        self.assertIn("(status = 'active'::text)", definition)
        migrate("downgrade", PREVIOUS)
        self.assertIsNone(self.definition())
        migrate("upgrade", REVISION)
        self.assertIsNotNone(self.definition())

    def test_the_index_is_built_over_rows_that_exist_and_finds_them(self):
        migrate("upgrade", PREVIOUS)
        with self.engine.begin() as connection:
            for n, status in enumerate(("active", "superseded")):
                memory_id = connection.execute(
                    text("INSERT INTO memories DEFAULT VALUES RETURNING id")
                ).scalar_one()
                connection.execute(
                    text(
                        "INSERT INTO memory_versions (memory_id, version_number, scope,"
                        " memory_type, title, content, status, confirmation_state,"
                        " freshness_policy, actor_type) VALUES (:m, 1, 'shared', 'n',"
                        " :t, :c, :s, 'confirmed', 'permanent', 'system')"
                    ),
                    {
                        "m": memory_id,
                        "t": f"t{n}",
                        "c": "デプロイは金曜日",
                        "s": status,
                    },
                )
        migrate("upgrade", REVISION)
        document = fulltext.search_document_sql()
        with self.engine.connect() as connection:
            # Every other index that can be dropped is dropped (in a transaction
            # that is rolled back), so the plan shows whether THIS index can serve
            # the query, not what the planner prefers on a table of two rows.
            for name in connection.execute(text(FREE_INDEXES)).scalars().all():
                if name != INDEX:
                    connection.exec_driver_sql(f"DROP INDEX {name}")
            connection.exec_driver_sql("SET enable_seqscan = off")
            plan = "\n".join(
                connection.execute(
                    text(
                        f"EXPLAIN SELECT title FROM memory_versions WHERE status ="
                        f" 'active' AND {document} @@ to_tsquery('simple', :q)"
                    ),
                    {"q": "'金' <-> '曜'"},
                ).scalars()
            )
            connection.rollback()
        with self.engine.connect() as connection:
            found = (
                connection.execute(
                    text(
                        f"SELECT title FROM memory_versions WHERE status = 'active'"
                        f" AND {document} @@ to_tsquery('simple', :q)"
                    ),
                    {"q": "'金' <-> '曜'"},
                )
                .scalars()
                .all()
            )
        self.assertIn(INDEX, plan)
        self.assertEqual(found, ["t0"])

    def test_the_document_folds_width_case_and_separates_japanese_characters(self):
        migrate("upgrade", REVISION)
        document = fulltext.search_document_sql("'ＡＢＣ　ﾃｽﾄ 検索'", "''")
        with self.engine.connect() as connection:
            tokens = connection.execute(text(f"SELECT {document}::text")).scalar()
        self.assertEqual(tokens, "'abc':1 'ス':3 'テ':2 'ト':4 '検':5 '索':6")

    def test_the_statement_the_queries_run_uses_the_same_expression(self):
        scopes = ResolvedScopes(uuid4(), frozenset(MemoryScope))
        statement = queries.keyword_statement(scopes, datetime.now(UTC), "'a'", 1)
        compiled = str(statement.compile(dialect=self.engine.dialect))
        self.assertIn(fulltext.search_document_sql("mv.title", "mv.content"), compiled)


if __name__ == "__main__":
    unittest.main()
