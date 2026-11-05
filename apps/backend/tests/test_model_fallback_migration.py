"""Revision 0325: the failure code ``model_fallback`` (issue #325, Decision
0102, 3; the human's answer 1 A).

* the values the revision writes out are the code's (``FailureCode``) and,
  without the new one, revisions 0030's and 0306's;
* it follows 0306 and is ``expand``; the upgrade rebuilds the ``failure_code``
  CHECK of ``connection_probes`` and ``connection_usage`` with
  ``model_fallback`` and changes nothing else; the downgrade puts the old
  lists back;
* on PostgreSQL, both CHECKs accept ``model_fallback`` and still refuse an
  unknown code; the downgrade fails while a row holds ``model_fallback`` and
  succeeds once none does; the revision can be applied again after it.
"""

import importlib.util
import io
import unittest
import uuid

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from paw_backend.connections.domain import FailureCode

from .memory_support import migrate, requires_postgres, sync_database_url
from .support import paw_environment
from .test_migration_grants import VERSIONS
from .test_migrations import offline_config

REVISION = "0325"
HANDLE = "cred_" + "b" * 32
CHECKS = {
    "connection_probes": "ck_connection_probes_failure_code_valid",
    "connection_usage": "ck_connection_usage_failure_code_valid",
}


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def revision_module():
    return load("revision_0325", "0325_model_fallback_failure_code.py")


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


class ValuesTest(unittest.TestCase):
    def test_the_revision_writes_the_values_of_the_code(self):
        revision = revision_module()
        self.assertEqual(
            revision.FAILURE_CODES, tuple(code.value for code in FailureCode)
        )
        self.assertEqual(revision.FAILURE_CODES[-1], FailureCode.MODEL_FALLBACK)
        self.assertEqual(
            revision.FAILURE_CODES,
            (*revision.PREVIOUS_FAILURE_CODES, "model_fallback"),
        )
        self.assertEqual(dict(revision.CHECKS), CHECKS)

    def test_the_old_list_is_the_one_of_revisions_0030_and_0306(self):
        previous = revision_module().PREVIOUS_FAILURE_CODES
        probes = load("revision_0306", "0306_connection_probes.py")
        usage = load("revision_0030", "0030_shared_connections.py")
        self.assertEqual(probes.FAILURE_CODES, previous)
        self.assertEqual(usage.FAILURE_CODES, ", ".join(f"'{v}'" for v in previous))

    def test_it_follows_the_connection_probes_and_is_an_expand(self):
        self.assertEqual(previous_revision(), "0306")
        self.assertEqual(revision_module().paw_compatibility, "expand")


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def statements(self, sql: str) -> list[str]:
        return [
            line
            for line in sql.splitlines()
            if line.startswith("ALTER TABLE") or line.startswith("CREATE")
        ]

    def expected(self, values) -> list[str]:
        listed = ", ".join(f"'{value}'" for value in values)
        lines = []
        for table, name in CHECKS.items():
            lines += [
                f"ALTER TABLE {table} DROP CONSTRAINT {name};",
                f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK (failure_code IS"
                f" NULL OR failure_code IN ({listed}));",
            ]
        return lines

    def test_upgrade(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        self.assertEqual(
            self.statements(sql), self.expected(revision_module().FAILURE_CODES)
        )
        self.assertNotIn("GRANT", sql)
        self.assertNotIn("UPDATE connection", sql)

    def test_downgrade(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertEqual(
            self.statements(sql),
            self.expected(revision_module().PREVIOUS_FAILURE_CODES),
        )
        self.assertNotIn("model_fallback", sql.split("-- Running downgrade")[1])


@requires_postgres
class PostgresMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM connection_probes WHERE secret_handle = :h"),
                {"h": HANDLE},
            )
        migrate("upgrade", "head")
        cls.engine.dispose()

    def definition(self, name: str) -> str:
        with self.engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE conname = :name"
                ),
                {"name": name},
            ).scalar_one()

    def insert_probe(self, connection, failure: str) -> None:
        connection.execute(
            text(
                "INSERT INTO connection_probes (id, connection_id, kind,"
                " secret_handle, trigger, state, outcome, failure_code,"
                " started_at, finished_at, duration_ms) VALUES (:id, :connection,"
                " 'claude', :handle, 'manual', 'finished', 'unavailable', :failure,"
                " now(), now(), 5)"
            ),
            {
                "id": uuid.uuid4(),
                "connection": uuid.uuid4(),
                "handle": HANDLE,
                "failure": failure,
            },
        )

    def test_both_checks_accept_the_new_code_and_refuse_an_unknown_one(self):
        migrate("upgrade", "head")
        for name in CHECKS.values():
            with self.subTest(name):
                self.assertIn("'model_fallback'", self.definition(name))
        with self.engine.connect() as connection, connection.begin() as transaction:
            self.insert_probe(connection, "model_fallback")
            for junk in ("oops", "MODEL_FALLBACK", "model_fallback "):
                with (
                    self.subTest(junk),
                    self.assertRaises(IntegrityError),
                    connection.begin_nested(),
                ):
                    self.insert_probe(connection, junk)
            transaction.rollback()

    def test_the_downgrade_fails_while_a_row_holds_the_new_code(self):
        migrate("upgrade", REVISION)
        with self.engine.begin() as connection:
            self.insert_probe(connection, "model_fallback")
        try:
            with self.assertRaises(IntegrityError):
                migrate("downgrade", previous_revision())
            self.assertIn(
                "'model_fallback'", self.definition(CHECKS["connection_probes"])
            )
        finally:
            with self.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM connection_probes WHERE secret_handle = :h"),
                    {"h": HANDLE},
                )
        migrate("downgrade", previous_revision())
        for name in CHECKS.values():
            with self.subTest(name):
                definition = self.definition(name)
                self.assertNotIn("model_fallback", definition)
                self.assertIn("'internal_error'", definition)
        migrate("upgrade", REVISION)
        for name in CHECKS.values():
            with self.subTest(name):
                self.assertIn("'model_fallback'", self.definition(name))


if __name__ == "__main__":
    unittest.main()
