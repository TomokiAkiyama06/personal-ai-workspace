"""Revision 0192: the answers to held preference candidates (issue #38, Decision 0081).

The first classes need no server: the values the revision writes out are the code's,
the upgrade grants the application role SELECT and INSERT only, and the downgrade
removes exactly what the upgrade added. The PostgreSQL class (skipped unless
``PAW_TEST_DATABASE_URL`` is set) runs the revision up and down, compares it with the
models through Alembic's autogenerate (the new table and the journal's indexes), and
tries the constraints and the cascades.
"""

import importlib.util
import io
import unittest
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from paw_backend.db import Base
from paw_backend.memory.preferences.domain import Resolution
from paw_backend.memory.preferences.models import TABLE_NAMES

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migration_grants import VERSIONS
from .test_migrations import offline_config

REVISION = "0192"
TABLE = "memory_preference_resolutions"
JOURNAL_INDEX = "ix_memory_journal_entries_owner_user_id_recorded_at"


def revision_module():
    spec = importlib.util.spec_from_file_location(
        "revision_0192", VERSIONS / "0192_memory_preference_resolutions.py"
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
        self.assertEqual(
            revision_module().RESOLUTIONS, tuple(r.value for r in Resolution)
        )
        self.assertEqual(TABLE_NAMES, (TABLE,))


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
        self.assertIn(f"CREATE INDEX {JOURNAL_INDEX}", sql)
        self.assertIn(f'GRANT INSERT, SELECT ON {TABLE} TO "paw_app"', sql)
        # Nothing on another table.
        self.assertEqual(sql.count("GRANT "), 1)

    def test_downgrade(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn(f"DROP TABLE {TABLE}", sql)
        self.assertIn(f"DROP INDEX {JOURNAL_INDEX}", sql)


def only_ours(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in (TABLE, "memory_journal_entries")
    table = getattr(obj, "table", None)
    return table is None or table.name in (TABLE, "memory_journal_entries")


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
            return set(
                connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                ).scalars()
            )

    def indexes(self) -> dict[str, str]:
        with self.engine.connect() as connection:
            return dict(
                connection.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes"
                        " WHERE tablename IN (:a, 'memory_journal_entries')"
                    ),
                    {"a": TABLE},
                ).all()
            )

    def test_up_and_down(self):
        migrate("upgrade", REVISION)
        self.assertIn(TABLE, self.tables())
        self.assertIn(JOURNAL_INDEX, self.indexes())
        self.assertIn(
            "WHERE (memory_id IS NOT NULL)",
            self.indexes()[f"ix_{TABLE}_memory_id"],
        )
        migrate("downgrade", previous_revision())
        self.assertNotIn(TABLE, self.tables())
        self.assertNotIn(JOURNAL_INDEX, self.indexes())
        migrate("upgrade", REVISION)
        self.assertIn(TABLE, self.tables())

    def test_alembic_autogenerate_finds_no_difference(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_object": only_ours,
                },
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_constraints_and_cascades(self):
        migrate("upgrade", "head")
        owner = uuid4()
        with self.engine.begin() as connection:
            conversation = connection.execute(
                text(
                    "INSERT INTO conversations (owner_user_id) VALUES (:o) RETURNING id"
                ),
                {"o": owner},
            ).scalar_one()
            message = connection.execute(
                text(
                    "INSERT INTO messages (conversation_id, turn_id, event_sequence,"
                    " role, content) VALUES (:c, gen_random_uuid(), 0, 'user', 'x')"
                    " RETURNING id"
                ),
                {"c": conversation},
            ).scalar_one()
            entry = connection.execute(
                text(
                    "INSERT INTO memory_journal_entries (conversation_id, message_id,"
                    " turn_id, event_sequence, owner_user_id) SELECT :c, :m, turn_id,"
                    " 0, :o FROM messages WHERE id = :m RETURNING id"
                ),
                {"c": conversation, "m": message, "o": owner},
            ).scalar_one()
            memory = connection.execute(
                text("INSERT INTO memories DEFAULT VALUES RETURNING id")
            ).scalar_one()
        insert = (
            f"INSERT INTO {TABLE} (entry_id, item_index, owner_user_id, resolution,"
            " memory_id) VALUES (:e, :i, :o, :r, :m)"
        )
        refused = (
            ({"i": 0, "r": "maybe", "m": None}, "resolution_valid"),
            ({"i": -1, "r": "rejected", "m": None}, "item_index_not_negative"),
            ({"i": 0, "r": "rejected", "m": memory}, "only_confirmed_has_memory"),
        )
        for values, name in refused:
            with self.subTest(name), self.assertRaises(IntegrityError) as caught:
                with self.engine.begin() as connection:
                    connection.execute(text(insert), {"e": entry, "o": owner, **values})
            self.assertEqual(
                caught.exception.orig.diag.constraint_name, f"ck_{TABLE}_{name}"
            )
        with self.engine.begin() as connection:
            connection.execute(
                text(insert),
                {"e": entry, "o": owner, "i": 0, "r": "confirmed", "m": memory},
            )
            connection.execute(
                text(insert),
                {"e": entry, "o": owner, "i": 1, "r": "rejected", "m": None},
            )
            connection.execute(
                text("DELETE FROM memories WHERE id = :m"), {"m": memory}
            )
            self.assertEqual(
                connection.execute(
                    text(f"SELECT count(*) FROM {TABLE} WHERE memory_id IS NULL")
                ).scalar_one(),
                2,
            )
            # Deleting the conversation deletes the entry and its answers.
            connection.execute(
                text("DELETE FROM conversations WHERE id = :c"), {"c": conversation}
            )
            self.assertEqual(
                connection.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar_one(),
                0,
            )
