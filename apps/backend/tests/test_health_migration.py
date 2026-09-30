"""Revision 0066: the System Health tables and the in-flight index (PAW-066).

No server needed: the values the revision writes out (metric name pattern,
resolutions, components, severities, statuses) are the model's and the
domain's, the upgrade grants the application role what the store needs and
nothing more, the index on ``connection_usage`` is partial, and the downgrade
removes exactly what the upgrade added. The revision's run on PostgreSQL is
covered by every PostgreSQL test (they migrate to the head) and by
``test_health_grants.py`` (as the application role).
"""

import importlib.util
import io
import unittest

from alembic import command
from alembic.script import ScriptDirectory

from paw_backend.health import limits, models
from paw_backend.health.domain import Component, Severity, Status

from .support import paw_environment
from .test_migration_grants import VERSIONS
from .test_migrations import offline_config

REVISION = "0066"


def revision_module():
    spec = importlib.util.spec_from_file_location(
        "revision_0066", VERSIONS / "0066_system_health.py"
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
        self.assertEqual(revision.METRIC_NAME_PATTERN, limits.METRIC_NAME_PATTERN)
        self.assertEqual(revision.RESOLUTIONS, models.RESOLUTIONS)
        self.assertEqual(revision.COMPONENTS, tuple(c.value for c in Component))
        self.assertEqual(revision.SEVERITIES, tuple(s.value for s in Severity))
        self.assertEqual(revision.STATUSES, tuple(s.value for s in Status))
        self.assertEqual(revision.MAX_REASONS_CHARS, models.MAX_REASONS_CHARS)


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
        self.assertIn("CREATE TABLE health_metric_samples", sql)
        self.assertIn("CREATE TABLE health_events", sql)
        self.assertIn(
            "CREATE INDEX ix_connection_usage_in_flight_started_at ON"
            " connection_usage (started_at) WHERE status = 'in_flight'",
            sql,
        )
        samples = "GRANT DELETE, INSERT, SELECT, UPDATE ON health_metric_samples"
        self.assertIn(f'{samples} TO "paw_app"', sql)
        self.assertIn('GRANT DELETE, INSERT, SELECT ON health_events TO "paw_app"', sql)
        self.assertIn(
            "CREATE INDEX ix_task_events_retry_fail_created_at ON task_events"
            " (created_at) WHERE command IN ('retry', 'fail')",
            sql,
        )
        self.assertIn(
            "CREATE INDEX ix_tasks_active_state ON tasks (state) WHERE state IN"
            " ('queued', 'running', 'waiting', 'paused', 'evaluating')",
            sql,
        )
        self.assertIn(
            "CREATE INDEX ix_tasks_ended_updated_at ON tasks (updated_at)"
            " WHERE state IN ('completed', 'cancelled')",
            sql,
        )
        self.assertIn(
            "CREATE INDEX ix_loop_failure_signatures_created_at ON"
            " loop_failure_signatures (created_at)",
            sql,
        )
        # Nothing on another table.
        self.assertEqual(sql.count("GRANT "), 2)

    def test_downgrade(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn("DROP INDEX ix_connection_usage_in_flight_started_at", sql)
        self.assertIn("DROP INDEX ix_task_events_retry_fail_created_at", sql)
        self.assertIn("DROP INDEX ix_tasks_active_state", sql)
        self.assertIn("DROP INDEX ix_tasks_ended_updated_at", sql)
        self.assertIn("DROP INDEX ix_loop_failure_signatures_created_at", sql)
        self.assertIn("DROP TABLE health_events", sql)
        self.assertIn("DROP TABLE health_metric_samples", sql)
        self.assertNotIn("connection_usage DROP", sql)
