"""The database restore point of an update (Issue #54, Decision 0079 5).

* credentials go to the tools in ``PG*`` variables, never on the command line;
* the directory is private (0700), not in the Recovery Repository; labels are
  checked and never overwritten; a failed dump leaves nothing behind;
* verify restores into a scratch database that is dropped afterwards and
  records the result; a dump that changed is refused;
* restore needs a verified point, puts it back under the workspace database's
  name (what a failed migration added is gone) and keeps the replaced one.

``pg_dump`` / ``pg_restore`` are fakes on a real database (``deploy_support``).
"""

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from paw_backend.deploy.restore_points import (
    RestorePointError,
    RestorePoints,
    pg_environment,
)

from .deploy_support import write_fake_pg_tools
from .task_support import TEST_DATABASE_URL, requires_postgres


class EnvironmentTest(unittest.TestCase):
    def test_the_url_becomes_libpq_variables(self):
        url = make_url(
            "postgresql+psycopg://owner:s3cret@db.example:6543/paw"
            "?sslmode=verify-full&application_name=x"
        )
        self.assertEqual(
            pg_environment(url),
            {
                "PGHOST": "db.example",
                "PGPORT": "6543",
                "PGUSER": "owner",
                "PGPASSWORD": "s3cret",
                "PGDATABASE": "paw",
                "PGSSLMODE": "verify-full",
            },
        )
        self.assertEqual(pg_environment(url, database="other")["PGDATABASE"], "other")
        socket = make_url("postgresql:///paw?host=/run/postgresql")
        self.assertEqual(pg_environment(socket)["PGHOST"], "/run/postgresql")


class DirectoryTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: os.system(f"rm -rf '{self.root}'"))

    def points(self, directory: Path, **kwargs) -> RestorePoints:
        return RestorePoints(directory, **kwargs)

    def test_a_relative_directory_is_refused(self):
        with self.assertRaises(RestorePointError) as caught:
            RestorePoints(Path("points"))
        self.assertEqual(caught.exception.code, "directory_not_absolute")

    def test_the_directory_must_be_private_and_outside_the_recovery_repository(self):
        url = make_url("postgresql://u:p@db.invalid/paw")
        shared = self.root / "shared"
        shared.mkdir(mode=0o755)
        os.chmod(shared, 0o755)
        recovery = self.root / "recovery"
        for directory, kwargs, code in (
            (shared, {}, "directory_not_private"),
            (recovery / "points", {"recovery_directory": recovery}, None),
            (self.root, {"recovery_directory": recovery}, None),
        ):
            with self.subTest(directory=directory.name):
                with self.assertRaises(RestorePointError) as caught:
                    self.points(directory, **kwargs).create(url, "before-r2")
                self.assertEqual(
                    caught.exception.code, code or "directory_in_recovery_repository"
                )

    def test_labels_are_checked_and_missing_points_reported(self):
        points = self.points(self.root / "points")
        url = make_url("postgresql://u:p@db.invalid/paw")
        for label in ("", "../x", "a/b", ".hidden", "x" * 65):
            with self.subTest(label=label), self.assertRaises(RestorePointError) as c:
                points.create(url, label)
            self.assertEqual(c.exception.code, "invalid_label")
        (self.root / "points").mkdir(mode=0o700)
        with self.assertRaises(RestorePointError) as caught:
            points.load("absent")
        self.assertEqual(caught.exception.code, "not_found")


@requires_postgres
class PostgresRestorePointTest(unittest.TestCase):
    """A workspace database of its own per test (the shared test database is not
    renamed), and the fakes of the tools."""

    def setUp(self):
        self.admin_url = make_url(TEST_DATABASE_URL).set(
            drivername="postgresql+psycopg"
        )
        self.name = f"paw_rp_{uuid.uuid4().hex[:8]}"
        self.admin = create_engine(self.admin_url, isolation_level="AUTOCOMMIT")
        self.addCleanup(self.admin.dispose)
        with self.admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{self.name}"'))
        self.addCleanup(self.drop_databases)
        self.url = self.admin_url.set(database=self.name)
        self.sql(
            "CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)",
            "INSERT INTO alembic_version VALUES ('0188')",
            "CREATE TABLE tasks (id int)",
        )
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: os.system(f"rm -rf '{self.root}'"))
        self.tools = self.root / "tools"
        self.tools.mkdir()
        pg_dump, pg_restore = write_fake_pg_tools(self.tools)
        self.points = RestorePoints(
            self.root / "points", pg_dump=pg_dump, pg_restore=pg_restore
        )

    def sql(self, *statements: str, database: str | None = None) -> list:
        engine = create_engine(self.admin_url.set(database=database or self.name))
        try:
            with engine.begin() as connection:
                result = None
                for statement in statements:
                    result = connection.execute(text(statement))
                return (
                    list(result) if result is not None and result.returns_rows else []
                )
        finally:
            engine.dispose()

    def databases(self) -> set[str]:
        with self.admin.connect() as connection:
            rows = connection.execute(
                text("SELECT datname FROM pg_database WHERE datname LIKE :p"),
                {"p": f"{self.name}%"},
            )
            return {row[0] for row in rows}

    def drop_databases(self):
        for name in self.databases():
            with self.admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))

    def calls(self) -> list[dict]:
        path = self.tools / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_create_verify_and_restore(self):
        point = self.points.create(self.url, "before-r2")
        self.assertEqual((point.revision, point.database), ("0188", self.name))
        directory = self.root / "points"
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for name in ("before-r2.dump", "before-r2.json"):
            self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            sorted(p.name for p in directory.iterdir()),
            [
                "before-r2.dump",
                "before-r2.json",
            ],
        )
        with self.assertRaises(RestorePointError) as caught:
            self.points.create(self.url, "before-r2")
        self.assertEqual(caught.exception.code, "label_exists")

        # Not verified yet: no restore.
        with self.assertRaises(RestorePointError) as caught:
            self.points.restore(self.points.load("before-r2"), self.url, self.admin_url)
        self.assertEqual(caught.exception.code, "not_verified")

        verified = self.points.verify(
            self.points.load("before-r2"), self.url, self.admin_url
        )
        self.assertEqual(verified.verified["revision"], "0188")
        self.assertEqual(verified.verified["tables"], 2)
        self.assertEqual(self.points.load("before-r2").verified["tables"], 2)
        self.assertEqual(self.databases(), {self.name})  # the scratch one is gone

        # The migration ran (and failed its health check afterwards).
        self.sql(
            "UPDATE alembic_version SET version_num = '0191'",
            "CREATE TABLE deploy_maintenance (id int)",
        )
        replaced = self.points.restore(
            self.points.load("before-r2"), self.url, self.admin_url
        )
        self.assertEqual(self.databases(), {self.name, replaced})
        self.assertEqual(
            self.sql("SELECT version_num FROM alembic_version"), [("0188",)]
        )
        tables = self.sql(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
        )
        self.assertEqual(tables, [("alembic_version",), ("tasks",)])
        self.assertEqual(
            self.sql("SELECT version_num FROM alembic_version", database=replaced),
            [("0191",)],
        )

        # No credential on any command line, always in the environment.
        calls = self.calls()
        self.assertEqual(
            [call["tool"] for call in calls], ["pg_dump", "pg_restore", "pg_restore"]
        )
        password = self.url.password
        for call in calls:
            self.assertIn("PGPASSWORD", call["pg"])
            self.assertFalse(any(password and password in a for a in call["argv"]))
            self.assertIn("--no-password", call["argv"])

    def test_a_changed_dump_is_refused(self):
        self.points.create(self.url, "p1")
        dump = self.root / "points" / "p1.dump"
        dump.write_text(dump.read_text().replace("0188", "0187"))
        with self.assertRaises(RestorePointError) as caught:
            self.points.verify(self.points.load("p1"), self.url, self.admin_url)
        self.assertEqual(caught.exception.code, "checksum_mismatch")
        self.assertEqual(self.databases(), {self.name})

    def test_a_failed_dump_leaves_nothing(self):
        (self.tools / "pg_dump.fail").touch()
        with self.assertRaises(RestorePointError) as caught:
            self.points.create(self.url, "p1")
        self.assertEqual(caught.exception.code, "dump_failed")
        self.assertEqual(
            [
                p.name
                for p in (self.root / "points").iterdir()
                if not p.name.startswith(".")
            ],
            [],
        )

    def test_a_failed_verification_drops_the_scratch_database(self):
        self.points.create(self.url, "p1")
        (self.tools / "pg_restore.fail").touch()
        with self.assertRaises(RestorePointError) as caught:
            self.points.verify(self.points.load("p1"), self.url, self.admin_url)
        self.assertEqual(caught.exception.code, "restore_failed")
        self.assertIsNone(self.points.load("p1").verified)
        self.assertEqual(self.databases(), {self.name})

    def test_a_restore_while_the_database_is_in_use_changes_nothing(self):
        self.points.verify(self.points.create(self.url, "p1"), self.url, self.admin_url)
        writer = create_engine(self.url)
        self.addCleanup(writer.dispose)
        with writer.connect() as connection:
            connection.execute(text("SELECT 1"))
            with self.assertRaises(RestorePointError) as caught:
                self.points.restore(self.points.load("p1"), self.url, self.admin_url)
        self.assertEqual(caught.exception.code, "database_in_use_or_not_allowed")
        self.assertEqual(self.databases(), {self.name})

    def test_a_restore_is_refused_when_a_user_was_deleted_since_the_point(self):
        self.sql(
            "CREATE TABLE user_status_changes (new_status text,"
            " recorded_at timestamptz DEFAULT clock_timestamp())",
            "INSERT INTO user_status_changes (new_status) VALUES ('deleted')",
        )
        point = self.points.verify(
            self.points.create(self.url, "p1"), self.url, self.admin_url
        )
        # Deleted before the point: in the restored data already.
        self.sql("INSERT INTO user_status_changes (new_status) VALUES ('active')")
        self.points.restore(point, self.url, self.admin_url)
        self.sql(
            "INSERT INTO user_status_changes VALUES"
            " ('pending_deletion', clock_timestamp())"
        )
        with self.assertRaises(RestorePointError) as caught:
            self.points.restore(self.points.load("p1"), self.url, self.admin_url)
        self.assertEqual(caught.exception.code, "users_deleted_since_the_point")

    def test_a_failed_second_rename_puts_the_workspace_database_back(self):
        # Codex review #204 (ad3c59c): after the first rename the workspace
        # database must not be left without its name.
        self.points.verify(self.points.create(self.url, "p1"), self.url, self.admin_url)
        name = self.name

        class Failing:
            """The engine of the admin connection, failing the second rename."""

            def __init__(self, engine):
                self._engine = engine

            def connect(self):
                connection = self._engine.connect()
                execute = connection.execute

                def guarded(statement, *args, **kwargs):
                    sql = str(statement)
                    if f'RENAME TO "{name}"' in sql and "_restore_" in sql:
                        raise RuntimeError("rename refused")
                    return execute(statement, *args, **kwargs)

                connection.execute = guarded
                return connection

            def dispose(self):
                self._engine.dispose()

        def engine(url, **kwargs):
            return Failing(create_engine(url, **kwargs))

        points = RestorePoints(
            self.root / "points",
            pg_dump=str(self.tools / "pg_dump"),
            pg_restore=str(self.tools / "pg_restore"),
            engine=engine,
        )
        self.sql("CREATE TABLE added_by_the_migration (id int)")
        with self.assertRaises(RestorePointError) as caught:
            points.restore(points.load("p1"), self.url, self.admin_url)
        self.assertEqual(caught.exception.code, "rename_failed")
        self.assertEqual(self.databases(), {self.name})
        self.assertIn(
            ("added_by_the_migration",),
            self.sql("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"),
        )
