"""Revision 0147: the length limit of ``memory_versions.content`` (issue #147).

The first class needs no server: the rendered SQL carries the limit of the
services (a test fails when the migration, the model and the two service limits
drift apart). The PostgreSQL class (skipped unless ``PAW_TEST_DATABASE_URL`` is
set) runs the revision over a database that already holds a text over the limit:
the upgrade stops with a message that names the rows and changes nothing
(Decision 0053, 1). Nothing here assumes 0147 is the head: the previous revision
is read from the script directory.
"""

import io
import unittest
from uuid import UUID

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, insert, text
from sqlalchemy.exc import IntegrityError

from paw_backend.memory import models
from paw_backend.memory.models import Memory, MemoryVersion
from paw_backend.memory.shared import limits as shared_limits
from paw_backend.memory.versioning import limits as versioning_limits

from .memory_support import (
    migrate,
    requires_postgres,
    sync_database_url,
    version_values,
)
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0147"
CONSTRAINT = "ck_memory_versions_content_length"
LIMIT = 20_000


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


class LimitTest(unittest.TestCase):
    def test_the_services_and_the_model_share_one_limit(self):
        self.assertEqual(models.MAX_VERSION_CONTENT_CHARS, LIMIT)
        self.assertEqual(versioning_limits.MAX_CONTENT_CHARS, LIMIT)
        self.assertEqual(shared_limits.MAX_CONTENT_CHARS, LIMIT)

    def test_the_model_declares_the_constraint(self):
        checks = {
            constraint.name: str(constraint.sqltext)
            for constraint in MemoryVersion.__table__.constraints
            if constraint.name and constraint.name.startswith("ck_")
        }
        self.assertEqual(checks[CONSTRAINT], f"char_length(content) <= {LIMIT}")


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str, **environment: str) -> str:
        output = io.StringIO()
        with paw_environment(
            PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw", **environment
        ):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_upgrade_checks_the_rows_then_adds_the_constraint(self):
        sql = self.sql(
            "upgrade",
            f"{previous_revision()}:{REVISION}",
            PAW_APP_DATABASE_ROLE="paw_app",
        )
        self.assertIn(
            f"ALTER TABLE memory_versions ADD CONSTRAINT {CONSTRAINT}"
            f" CHECK (char_length(content) <= {LIMIT})",
            sql,
        )
        self.assertLess(sql.index("RAISE EXCEPTION"), sql.index("ADD CONSTRAINT"))
        # Nothing is rewritten and no privilege changes.
        for forbidden in ("UPDATE memory_versions", "DELETE", "GRANT", "REVOKE"):
            with self.subTest(forbidden):
                self.assertNotIn(forbidden, sql)

    def test_downgrade_drops_only_the_constraint(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn(f"ALTER TABLE memory_versions DROP CONSTRAINT {CONSTRAINT}", sql)
        self.assertNotIn("DROP TABLE", sql)

    def test_the_revision_follows_the_head_it_was_written_on(self):
        self.assertEqual(previous_revision(), "0129")


@requires_postgres
class ExistingRowsTest(unittest.TestCase):
    """The upgrade over a database that already holds a text over the limit."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()
        migrate("downgrade", "base")

    def setUp(self) -> None:
        migrate("downgrade", "base")
        migrate("upgrade", previous_revision())

    def add_version(self, content: str) -> UUID:
        with self.engine.begin() as connection:
            memory = connection.execute(
                insert(Memory).returning(Memory.id)
            ).scalar_one()
            return connection.execute(
                insert(MemoryVersion)
                .values(**version_values(memory, content=content))
                .returning(MemoryVersion.id)
            ).scalar_one()

    def constraint_exists(self) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = :n"),
                    {"n": CONSTRAINT},
                ).scalar_one()
            )

    def contents(self) -> dict[UUID, int]:
        with self.engine.connect() as connection:
            return dict(
                connection.execute(
                    text("SELECT id, char_length(content) FROM memory_versions")
                ).all()
            )

    def test_rows_within_the_limit_are_kept_and_validated(self):
        kept = self.add_version("c" * LIMIT)
        migrate("upgrade", REVISION)
        self.assertTrue(self.constraint_exists())
        self.assertEqual(self.contents(), {kept: LIMIT})

    def test_an_overlong_row_stops_the_upgrade_and_nothing_changes(self):
        within = self.add_version("c" * LIMIT)
        overlong = [self.add_version("c" * (LIMIT + n)) for n in (1, 5)]
        before = self.contents()

        with self.assertRaises(IntegrityError) as caught:
            migrate("upgrade", REVISION)

        self.assertEqual(caught.exception.orig.sqlstate, "23514")  # check_violation
        message = str(caught.exception)
        self.assertIn("2 memory_versions row(s)", message)
        self.assertIn(f"longer than {LIMIT} characters", message)
        for version_id in overlong:
            self.assertIn(str(version_id), message)
        self.assertNotIn(str(within), message)
        self.assertIn("0053", message)
        # Non-destructive: no constraint, every text as it was.
        self.assertFalse(self.constraint_exists())
        self.assertEqual(self.contents(), before)

    def test_the_upgrade_passes_once_the_rows_are_dealt_with(self):
        overlong = self.add_version("c" * (LIMIT + 1))
        with self.assertRaises(IntegrityError):
            migrate("upgrade", REVISION)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM memories WHERE id ="
                    " (SELECT memory_id FROM memory_versions WHERE id = :v)"
                ),
                {"v": overlong},
            )
        migrate("upgrade", REVISION)
        self.assertTrue(self.constraint_exists())

    def test_the_downgrade_drops_the_constraint(self):
        migrate("upgrade", REVISION)
        migrate("downgrade", previous_revision())
        self.assertFalse(self.constraint_exists())
        self.add_version("c" * (LIMIT + 1))  # no limit before the revision
