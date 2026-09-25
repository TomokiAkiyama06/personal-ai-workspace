"""Revision 0071 (status and stale-state history) up and down (issue #90).

The first class needs no server: the revision's place in the chain and the SQL it
emits. The second runs the revision on a real PostgreSQL that already holds
versions and history rows written by revision 0040's trigger, and checks that
the old rows survive the upgrade, what the downgrade removes (and what it
restores), and that the upgrade can be applied again. The models / migration
drift (Alembic autogenerate and the catalog comparison) is checked at head by
``tests/test_memory_migration.py``. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import io
import unittest
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from paw_backend.memory.metadata import metadata_change_actor

from .memory_support import (
    migrate,
    requires_postgres,
    sync_database_url,
    version_values,
)
from .support import paw_environment
from .test_memory_migration import catalog
from .test_migrations import offline_config

REVISION = "0071"
PREVIOUS = "0030"
FIRST = datetime(2026, 9, 1, tzinfo=UTC)

HISTORY_0040 = (
    "SELECT id, memory_version_id, old_pinned, new_pinned, old_importance,"
    " new_importance, actor_type, actor_user_id, created_at"
    " FROM memory_metadata_changes ORDER BY created_at, id"
)
HISTORY_0071 = (
    "SELECT old_pinned, new_pinned, old_importance, new_importance, old_status,"
    " new_status, old_stale_since, new_stale_since, actor_type"
    " FROM memory_metadata_changes ORDER BY created_at, id"
)


class RevisionChainTest(unittest.TestCase):
    def sql(self, action: str, revisions: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_the_revision_follows_the_head_of_main(self):
        scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))

        revision = scripts.get_revision(REVISION)

        self.assertEqual(revision.down_revision, PREVIOUS)

    def test_upgrade_adds_the_columns_the_checks_and_the_wider_trigger(self):
        sql = self.sql("upgrade", f"{PREVIOUS}:{REVISION}")

        for column in ("old_status", "new_status"):
            self.assertIn(
                f"ALTER TABLE memory_metadata_changes ADD COLUMN {column} TEXT", sql
            )
        for column in ("old_stale_since", "new_stale_since"):
            self.assertIn(
                f"ALTER TABLE memory_metadata_changes ADD COLUMN {column} TIMESTAMP",
                sql,
            )
        for name in (
            "something_changed",
            "status_pair",
            "status_valid",
            "stale_since_needs_status",
        ):
            self.assertIn(f"ck_memory_metadata_changes_{name}", sql)
        self.assertIn(
            "AFTER UPDATE OF pinned, importance, status, stale_since"
            " ON memory_versions",
            sql,
        )
        self.assertIn("OLD.status, NEW.status, OLD.stale_since, NEW.stale_since", sql)

    def test_upgrade_creates_no_table_and_grants_nothing(self):
        sql = self.sql("upgrade", f"{PREVIOUS}:{REVISION}").upper()

        self.assertNotIn("CREATE TABLE", sql)
        self.assertNotIn("GRANT", sql)
        self.assertNotIn("REVOKE", sql)

    def test_downgrade_removes_the_rows_of_the_new_shape_before_its_columns(self):
        sql = self.sql("downgrade", f"{REVISION}:{PREVIOUS}")

        delete = sql.index("DELETE FROM memory_metadata_changes")
        self.assertLess(delete, sql.index("DROP COLUMN old_status"))
        self.assertLess(delete, sql.index("DROP COLUMN new_stale_since"))
        # The 0040 trigger comes back last.
        self.assertGreater(
            sql.index("AFTER UPDATE OF pinned, importance ON memory_versions"),
            sql.index("DROP COLUMN old_status"),
        )
        self.assertNotIn("GRANT", sql.upper())


def function_definition(connection) -> str:
    return connection.execute(
        text(
            "SELECT pg_get_functiondef(p.oid) FROM pg_proc p"
            " WHERE p.pronamespace = 'public'::regnamespace"
            "   AND p.proname = 'paw_record_memory_metadata_change'"
        )
    ).scalar_one()


@requires_postgres
class MigrationWithExistingRowsTest(unittest.TestCase):
    def setUp(self) -> None:
        migrate("downgrade", "base")
        self.addCleanup(migrate, "downgrade", "base")
        self.engine = create_engine(sync_database_url())
        self.addCleanup(self.engine.dispose)
        self.alice, self.bob = uuid4(), uuid4()

    # -- helpers ------------------------------------------------------------

    def rows(self, sql: str, **params: Any) -> list[tuple]:
        with self.engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(sql), params)]

    def columns(self) -> set[str]:
        return {
            column
            for (column,) in self.rows(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'public'"
                "   AND table_name = 'memory_metadata_changes'"
            )
        }

    def catalog(self) -> tuple[dict[str, list[tuple]], str]:
        with self.engine.connect() as connection:
            return catalog(connection, "public"), function_definition(connection)

    def add_version(self, connection, status: str = "active") -> UUID:
        memory = connection.execute(
            text("INSERT INTO memories DEFAULT VALUES RETURNING id")
        ).scalar_one()
        values = version_values(memory, status=status)
        columns = ", ".join(values)
        binds = ", ".join(f":{name}" for name in values)
        return connection.execute(
            text(
                f"INSERT INTO memory_versions ({columns}) VALUES ({binds}) RETURNING id"
            ),
            values,
        ).scalar_one()

    def change(self, connection, version: UUID, actor: UUID | None, **values) -> None:
        """Update ``version`` as ``actor`` (a transaction of its own)."""
        if actor is not None:
            connection.execute(metadata_change_actor("user", actor))
        assignments = ", ".join(f"{name} = :{name}" for name in values)
        connection.execute(
            text(f"UPDATE memory_versions SET {assignments} WHERE id = :id"),
            {"id": version, **values},
        )

    def seed_as_revision_0040(self) -> tuple[UUID, UUID]:
        """Two versions, two history rows, and a status / stale change no one saw."""
        migrate("upgrade", PREVIOUS)
        with self.engine.begin() as connection:
            pinned = self.add_version(connection)
            deprecated = self.add_version(connection)
            self.change(connection, pinned, self.alice, pinned=True)
        with self.engine.begin() as connection:
            self.change(connection, pinned, self.bob, importance=80)
        with self.engine.begin() as connection:
            # Revision 0040: nothing is recorded, and no actor is needed.
            self.change(connection, deprecated, None, status="deprecated")
            self.change(connection, deprecated, None, stale_since=FIRST)
        return pinned, deprecated

    def versions(self) -> list[tuple]:
        return self.rows(
            "SELECT id, status, stale_since, pinned, importance"
            " FROM memory_versions ORDER BY id"
        )

    # -- tests --------------------------------------------------------------

    def test_the_rows_of_revision_0040_survive_the_upgrade_unchanged(self):
        pinned, deprecated = self.seed_as_revision_0040()
        history_before = self.rows(HISTORY_0040)
        versions_before = self.versions()
        self.assertEqual(len(history_before), 2)
        self.assertNotIn("old_status", self.columns())

        migrate("upgrade", REVISION)

        self.assertEqual(self.rows(HISTORY_0040), history_before)
        self.assertEqual(self.versions(), versions_before)
        self.assertEqual(
            self.rows(HISTORY_0071),
            [
                # Revision 0040 did not record the status or a stale time.
                (False, True, 50, 50, None, None, None, None, "user"),
                (True, True, 50, 80, None, None, None, None, "user"),
            ],
        )

    def test_the_upgraded_trigger_records_status_and_stale_changes(self):
        pinned, deprecated = self.seed_as_revision_0040()
        migrate("upgrade", REVISION)

        with self.engine.begin() as connection:
            self.change(connection, deprecated, self.alice, status="active")
        with self.engine.begin() as connection:
            self.change(connection, deprecated, self.bob, stale_since=None)

        self.assertEqual(
            self.rows(
                "SELECT old_status, new_status, old_stale_since, new_stale_since,"
                " actor_user_id FROM memory_metadata_changes"
                " WHERE memory_version_id = :v ORDER BY created_at",
                v=deprecated,
            ),
            [
                ("deprecated", "active", FIRST, FIRST, self.alice),
                ("active", "active", FIRST, None, self.bob),
            ],
        )
        self.assertEqual(len(self.rows(HISTORY_0040)), 4)

    def test_the_upgraded_trigger_refuses_a_status_change_without_an_actor(self):
        pinned, deprecated = self.seed_as_revision_0040()
        migrate("upgrade", REVISION)

        with self.assertRaises(IntegrityError) as caught:
            with self.engine.begin() as connection:
                self.change(connection, pinned, None, status="deprecated")

        self.assertEqual(caught.exception.orig.diag.column_name, "actor_type")
        self.assertEqual(len(self.rows(HISTORY_0040)), 2)

    def test_the_downgrade_drops_what_revision_0040_cannot_hold_and_restores_it(self):
        pinned, deprecated = self.seed_as_revision_0040()
        before_upgrade = self.catalog()
        history_0040 = self.rows(HISTORY_0040)
        migrate("upgrade", REVISION)
        with self.engine.begin() as connection:
            # A status-only change, a stale-only change, and one row that changed
            # the pin as well as the status.
            self.change(connection, deprecated, self.alice, status="active")
            self.change(connection, deprecated, self.alice, stale_since=None)
            self.change(connection, pinned, self.bob, status="superseded", pinned=False)
        self.assertEqual(len(self.rows(HISTORY_0040)), 5)
        versions_before = self.versions()

        migrate("downgrade", PREVIOUS)

        # The two status / stale rows are gone; the rows of revision 0040 and the
        # row that also changed the pin stay (as pin changes: its status is lost).
        remaining = self.rows(HISTORY_0040)
        self.assertEqual(remaining[:2], history_0040)
        self.assertEqual(
            [row[2:8] for row in remaining[2:]],
            [(True, False, 80, 80, "user", self.bob)],
        )
        self.assertEqual(len(remaining), 3)
        self.assertEqual(self.versions(), versions_before)
        # The schema is what it was before the upgrade, function and trigger too.
        self.assertNotIn("old_status", self.columns())
        self.assertEqual(self.catalog(), before_upgrade)
        # And revision 0040's behaviour is back: a status change records nothing
        # and needs no actor.
        with self.engine.begin() as connection:
            self.change(connection, deprecated, None, status="history")
        self.assertEqual(len(self.rows(HISTORY_0040)), 3)

    def test_the_upgrade_can_be_applied_again_after_a_downgrade(self):
        pinned, deprecated = self.seed_as_revision_0040()
        history_0040 = self.rows(HISTORY_0040)
        migrate("upgrade", REVISION)
        after_first_upgrade = self.catalog()
        migrate("downgrade", PREVIOUS)

        migrate("upgrade", REVISION)

        self.assertEqual(self.catalog(), after_first_upgrade)
        self.assertEqual(self.rows(HISTORY_0040), history_0040)
        with self.engine.begin() as connection:
            self.change(connection, deprecated, self.alice, status="active")
        self.assertEqual(
            self.rows(
                "SELECT old_status, new_status FROM memory_metadata_changes"
                " WHERE old_status IS NOT NULL"
            ),
            [("deprecated", "active")],
        )

    def test_up_down_up_on_an_empty_database(self):
        migrate("upgrade", PREVIOUS)
        before = self.catalog()

        migrate("upgrade", REVISION)
        after = self.catalog()
        migrate("downgrade", PREVIOUS)
        self.assertEqual(self.catalog(), before)
        migrate("upgrade", REVISION)

        self.assertNotEqual(after, before)
        self.assertEqual(self.catalog(), after)
        self.assertEqual(
            {"old_status", "new_status", "old_stale_since", "new_stale_since"}
            - self.columns(),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
