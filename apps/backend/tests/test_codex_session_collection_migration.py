"""Revision 0280: what a Codex session leaves for its late collection (issue
#280, Decision 0096, 2 and 5).

* the values the revision writes out are the model's;
* the upgrade adds the session, the credential's handle and version and
  ``kept`` to ``connection_session_leases`` and the collection's mark to
  ``shared_connections``, and grants the application role UPDATE of exactly
  those columns; the downgrade drops them;
* on PostgreSQL, the model and the migrated tables do not differ
  (autogenerate), the CHECKs hold (the three session columns all or none,
  ``kept`` only with a session, a positive version, a handle's shape, the
  mark's two columns together and its reason a known code), and the revision
  can be applied again after a downgrade.
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

from paw_backend.connections.domain import CredentialSuspicion
from paw_backend.connections.models import (
    HANDLE_SQL_PATTERN,
    SESSION_LEASE_TABLE,
    ConnectionSessionLeaseRow,
    SharedConnectionRow,
)
from paw_backend.db import Base

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_deploy_release import load_tool
from .test_migration_grants import VERSIONS
from .test_migrations import offline_config

REVISION = "0280"
HANDLE = "cred_" + "a" * 32


def revision_module():
    spec = importlib.util.spec_from_file_location(
        "revision_0280", VERSIONS / "0280_codex_session_collection.py"
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
        self.assertEqual(revision.LEASES, SESSION_LEASE_TABLE)
        self.assertEqual(revision.CONNECTIONS, SharedConnectionRow.__tablename__)
        self.assertEqual(revision.HANDLE_PATTERN, HANDLE_SQL_PATTERN)
        self.assertEqual(
            revision.SUSPICIONS, tuple(reason.value for reason in CredentialSuspicion)
        )
        leases = set(ConnectionSessionLeaseRow.__table__.columns.keys())
        self.assertLessEqual(set(revision.LEASE_COLUMNS), leases)
        connections = set(SharedConnectionRow.__table__.columns.keys())
        self.assertLessEqual(set(revision.CONNECTION_COLUMNS), connections)

    def test_it_follows_the_session_slot(self):
        # Written on 0239; re-chained onto 0279 (#279), which merged first.
        self.assertEqual(previous_revision(), "0279")

    def test_a_rollback_does_not_keep_this_schema(self):
        # Codex review of #291 (P1): the release before 0280 would take over
        # an expired row bound to a session (its acquisition checks only the
        # expiry), so the revision is not ``expand`` (Decision 0079, 4): a
        # rollback over it needs a restore point.
        self.assertNotEqual(
            getattr(revision_module(), "paw_compatibility", None), "expand"
        )
        migrations = load_tool().read_migrations(VERSIONS)
        self.assertNotEqual(migrations[REVISION].compatibility, "expand")


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
        for column in ("session UUID", "secret_handle TEXT", "kept BOOLEAN"):
            self.assertIn(
                f"ALTER TABLE connection_session_leases ADD COLUMN {column}", sql
            )
        self.assertIn(
            "ALTER TABLE shared_connections ADD COLUMN suspect_reason TEXT",
            sql,
        )
        self.assertIn("ck_connection_session_leases_session_complete", sql)
        self.assertIn("ck_shared_connections_suspect_reason_valid", sql)
        self.assertIn(
            "GRANT UPDATE (session, secret_handle, expected_version, kept)"
            ' ON connection_session_leases TO "paw_app"',
            sql,
        )
        self.assertIn(
            "GRANT UPDATE (suspect_at, suspect_reason)"
            ' ON shared_connections TO "paw_app"',
            sql,
        )
        self.assertEqual(sql.count("GRANT "), 2)

    def test_downgrade(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertEqual(sql.count("DROP COLUMN"), 6)
        self.assertNotIn("DROP TABLE", sql)


def only_these(obj, name, type_, reflected, compare_to) -> bool:
    tables = {SESSION_LEASE_TABLE, SharedConnectionRow.__tablename__}
    if type_ == "table":
        return name in tables
    table = getattr(obj, "table", None)
    return table is None or table.name in tables


@requires_postgres
class PostgresMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls) -> None:
        migrate("upgrade", "head")
        cls.engine.dispose()

    def columns(self, table: str) -> set[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = :t"
                ),
                {"t": table},
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
                    "include_object": only_these,
                },
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_the_checks_hold(self):
        migrate("upgrade", "head")
        lease = text(
            "INSERT INTO connection_session_leases (kind, holder, acquired_at,"
            " heartbeat_at, expires_at, session, secret_handle,"
            " expected_version, kept) VALUES (:kind, :holder, now(), now(),"
            " now() + interval '1 minute', :session, :handle, :version, :kept)"
        )
        good = {
            "kind": "codex",
            "session": uuid.uuid4(),
            "handle": HANDLE,
            "version": 1,
            "kept": True,
        }
        with self.engine.connect() as connection, connection.begin() as transaction:
            connection.execute(lease, {**good, "holder": uuid.uuid4()})
            connection.execute(
                lease,
                {
                    "kind": "claude",
                    "holder": uuid.uuid4(),
                    "session": None,
                    "handle": None,
                    "version": None,
                    "kept": False,
                },
            )
            for values in (
                {"session": None},  # a handle and a version without a session
                {"handle": None},
                {"version": None},
                {"session": None, "handle": None, "version": None},  # kept
                {"version": 0},
                {"handle": "not-a-handle"},
            ):
                with (
                    self.subTest(values),
                    self.assertRaises(IntegrityError),
                    connection.begin_nested(),
                ):
                    connection.execute(text("DELETE FROM connection_session_leases"))
                    connection.execute(
                        lease, {**good, **values, "holder": uuid.uuid4()}
                    )
            mark = text(
                "INSERT INTO shared_connections (kind, secret_handle, status,"
                " enabled, created_at, updated_at, suspect_at,"
                " suspect_reason) VALUES ('codex', :h, 'unavailable',"
                " true, now(), now(), :at, :reason)"
            )
            connection.execute(text("DELETE FROM shared_connections"))
            connection.execute(mark, {"h": HANDLE, "at": None, "reason": None})
            connection.execute(text("DELETE FROM shared_connections"))
            for values in (
                {"reason": "missing"},  # a reason without an instant
                {"reason": "other", "at": "2026-10-22T00:00:00Z"},
                {"reason": None, "at": "2026-10-22T00:00:00Z"},
            ):
                with (
                    self.subTest(values),
                    self.assertRaises(IntegrityError),
                    connection.begin_nested(),
                ):
                    connection.execute(mark, {"h": HANDLE, "at": None, **values})
            transaction.rollback()

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        migrate("upgrade", REVISION)
        migrate("downgrade", previous_revision())
        self.assertNotIn("session", self.columns(SESSION_LEASE_TABLE))
        self.assertNotIn("suspect_at", self.columns(SharedConnectionRow.__tablename__))
        migrate("upgrade", REVISION)
        self.assertIn("kept", self.columns(SESSION_LEASE_TABLE))
        self.assertIn(
            "suspect_reason",
            self.columns(SharedConnectionRow.__tablename__),
        )


if __name__ == "__main__":
    unittest.main()
