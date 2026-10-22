"""Revision 0239: the per-connection session slot (issue #239, Decision 0083, 6).

* the values the revision writes out are the model's;
* the upgrade creates ``connection_session_leases`` and grants the application
  role SELECT, INSERT, DELETE and UPDATE of the holder and the three instants
  only; the downgrade drops the table;
* on PostgreSQL, the model and the migrated table do not differ
  (autogenerate), the CHECKs hold (a known kind, a heartbeat not before the
  acquisition, an expiry after the heartbeat, one row per kind), and the
  revision can be applied again after a downgrade.
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

from paw_backend.connections.domain import ConnectionKind
from paw_backend.connections.models import (
    SESSION_LEASE_TABLE,
    ConnectionSessionLeaseRow,
)
from paw_backend.db import Base

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migration_grants import VERSIONS
from .test_migrations import offline_config

REVISION = "0239"


def revision_module():
    spec = importlib.util.spec_from_file_location(
        "revision_0239", VERSIONS / "0239_connection_session_leases.py"
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
        self.assertEqual(revision.TABLE, SESSION_LEASE_TABLE)
        self.assertEqual(ConnectionSessionLeaseRow.__tablename__, SESSION_LEASE_TABLE)
        self.assertEqual(revision.KINDS, tuple(kind.value for kind in ConnectionKind))
        columns = set(ConnectionSessionLeaseRow.__table__.columns.keys())
        # The columns of a later revision (0280, issue #280) have their own
        # grants (``tests/test_codex_session_collection_migration.py``).
        later = {"session", "secret_handle", "expected_version", "kept"}
        self.assertEqual(set(revision.UPDATE_COLUMNS), columns - {"kind"} - later)

    def test_it_follows_the_connection_sessions(self):
        self.assertEqual(previous_revision(), "0194")
        self.assertEqual(revision_module().paw_compatibility, "expand")


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
        self.assertIn("CREATE TABLE connection_session_leases", sql)
        self.assertIn(
            "CONSTRAINT ck_connection_session_leases_kind_valid CHECK"
            " (kind IN ('codex', 'claude'))",
            sql,
        )
        self.assertIn("CONSTRAINT pk_connection_session_leases PRIMARY KEY (kind)", sql)
        self.assertIn(
            'GRANT DELETE, INSERT, SELECT ON connection_session_leases TO "paw_app"',
            sql,
        )
        self.assertIn(
            "GRANT UPDATE (holder, acquired_at, heartbeat_at, expires_at)"
            ' ON connection_session_leases TO "paw_app"',
            sql,
        )
        self.assertEqual(sql.count("GRANT "), 2)

    def test_downgrade(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn("DROP TABLE connection_session_leases", sql)
        self.assertEqual(sql.count("DROP "), 1)


def only_this(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name == SESSION_LEASE_TABLE
    table = getattr(obj, "table", None)
    return table is None or table.name == SESSION_LEASE_TABLE


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
                    "include_object": only_this,
                },
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_the_checks_hold(self):
        migrate("upgrade", "head")
        insert = text(
            "INSERT INTO connection_session_leases"
            " (kind, holder, acquired_at, heartbeat_at, expires_at)"
            " VALUES (:kind, :holder, now(), now() + make_interval(secs => :beat),"
            " now() + make_interval(secs => :expires))"
        )
        with self.engine.connect() as connection, connection.begin() as transaction:
            connection.execute(
                insert,
                {"kind": "codex", "holder": uuid.uuid4(), "beat": 0, "expires": 60},
            )
            for values in (
                {"kind": "codex", "beat": 0, "expires": 60},  # one row per kind
                {"kind": "other", "beat": 0, "expires": 60},
                {"kind": "claude", "beat": -1, "expires": 60},
                {"kind": "claude", "beat": 10, "expires": 10},
            ):
                with (
                    self.subTest(values),
                    self.assertRaises(IntegrityError),
                    connection.begin_nested(),
                ):
                    connection.execute(insert, {**values, "holder": uuid.uuid4()})
            transaction.rollback()

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        migrate("upgrade", REVISION)
        migrate("downgrade", previous_revision())
        self.assertNotIn(SESSION_LEASE_TABLE, self.tables())
        self.assertIn("connection_usage", self.tables())
        migrate("upgrade", REVISION)
        self.assertIn(SESSION_LEASE_TABLE, self.tables())


if __name__ == "__main__":
    unittest.main()
