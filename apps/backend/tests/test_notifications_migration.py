"""Revision 0188: the notification tables (issue #188, Decision 0070).

No server needed: the values the revision writes out are the code's, the upgrade
grants the application role what the store needs and nothing more, and the
downgrade removes exactly what the upgrade added. The revision's run on
PostgreSQL is covered by every PostgreSQL test (they migrate to the head) and by
``test_notifications_grants.py`` (as the application role).
"""

import importlib.util
import io
import unittest

from alembic import command
from alembic.script import ScriptDirectory

from paw_backend.notifications import domain

from .support import paw_environment
from .test_migration_grants import VERSIONS
from .test_migrations import offline_config

REVISION = "0188"


def revision_module():
    spec = importlib.util.spec_from_file_location(
        "revision_0188", VERSIONS / "0188_notifications.py"
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
        self.assertEqual(revision.KIND_PATTERN, domain.KIND_PATTERN)
        self.assertEqual(revision.KEY_MAX_CHARS, domain.KEY_MAX_CHARS)
        self.assertEqual(revision.PARAMS_MAX_TEXT_BYTES, domain.PARAMS_MAX_BYTES * 2)
        self.assertEqual(revision.SEVERITIES, tuple(s.value for s in domain.Severity))
        self.assertEqual(revision.CATEGORIES, tuple(c.value for c in domain.Category))


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
        self.assertIn("CREATE TABLE notifications", sql)
        self.assertIn("CREATE TABLE notification_receipts", sql)
        self.assertIn('GRANT DELETE, INSERT, SELECT ON notifications TO "paw_app"', sql)
        self.assertIn('GRANT UPDATE (resolved_at) ON notifications TO "paw_app"', sql)
        self.assertIn('GRANT INSERT, SELECT ON notification_receipts TO "paw_app"', sql)
        self.assertIn(
            "GRANT UPDATE (read_at, dismissed_at) ON notification_receipts"
            ' TO "paw_app"',
            sql,
        )
        # Nothing on another table.
        self.assertEqual(sql.count("GRANT "), 4)

    def test_downgrade(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn("DROP TABLE notification_receipts", sql)
        self.assertIn("DROP TABLE notifications", sql)
