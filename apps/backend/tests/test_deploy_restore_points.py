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
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from paw_backend.deploy.restore_points import (
    RestorePointError,
    RestorePoints,
    _read_toc,
    pg_environment,
)

from .deploy_support import write_fake_pg_tools
from .task_support import TEST_DATABASE_URL, requires_postgres

# What pg_restore --list (PostgreSQL 18) printed for a database with vector.
REAL_TOC = (
    ";",
    "; Archive created at 2026-10-29 13:47:22 JST",
    ";     dbname: paw_dbg_src",
    ";     TOC Entries: 11",
    ";",
    "; Selected TOC Entries:",
    ";",
    "2; 3079 16386 EXTENSION - vector ",
    "3623; 0 0 COMMENT - EXTENSION vector ",
    "220; 1259 16714 TABLE public alembic_version paw_owner",
    "221; 1259 16720 TABLE public m paw_owner",
    "3615; 0 16714 TABLE DATA public alembic_version paw_owner",
    "3616; 0 16720 TABLE DATA public m paw_owner",
    "3467; 2606 16719 CONSTRAINT public alembic_version alembic_version_pkey paw_owner",
)


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
            [call["tool"] for call in calls],
            ["pg_dump", "pg_restore", "pg_restore", "pg_restore", "pg_restore"],
        )
        # Listing the TOC connects nowhere: no PG* variable at all.
        listings = [call for call in calls if "--list" in call["argv"]]
        self.assertEqual(len(listings), 2)
        for call in listings:
            self.assertEqual(call["pg"], [])
        password = self.url.password
        for call in calls:
            if call in listings:
                continue
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

    def test_a_dump_that_failed_after_writing_leaves_nothing(self):
        # Codex review #204 (80d3189): the partial file holds personal data.
        (self.tools / "pg_dump.fail_after_writing").touch()
        with self.assertRaises(RestorePointError) as caught:
            self.points.create(self.url, "p1")
        self.assertEqual(caught.exception.code, "dump_failed")
        self.assertEqual(list((self.root / "points").iterdir()), [])


class TocTest(unittest.TestCase):
    """Issue #312: the dump's extensions and the list without them."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: os.system(f"rm -rf '{self.root}'"))

    def read(self, lines) -> tuple[list[str], set[str]]:
        path = self.root / "toc.list"
        path.write_text("\n".join(lines) + "\n")
        return _read_toc(path)

    def test_extensions_and_their_comments_are_left_out(self):
        selected, extensions = self.read(REAL_TOC)
        self.assertEqual(extensions, {"vector"})
        self.assertEqual(
            [line for line in selected if not line.startswith(";")],
            list(REAL_TOC[9:]),
        )
        self.assertIn(";2; 3079 16386 EXTENSION - vector ", selected)
        self.assertIn(";3623; 0 0 COMMENT - EXTENSION vector ", selected)

    def test_a_listing_that_is_not_a_toc_is_refused(self):
        for lines in ((";", "; nothing"), (*REAL_TOC, "garbage"), ()):
            with self.subTest(lines=lines), self.assertRaises(RestorePointError) as c:
                self.read(lines)
            self.assertEqual(c.exception.code, "dump_unreadable")

    def test_the_template_name_is_checked(self):
        for name in ('x"; DROP', "", "a-b", "1abc", "x" * 64):
            with self.subTest(name=name), self.assertRaises(RestorePointError) as c:
                RestorePoints(self.root, template=name)
            self.assertEqual(c.exception.code, "invalid_restore_template")
        RestorePoints(self.root, template="paw_restore_template")


@requires_postgres
class NonSuperuserExtensionTest(unittest.TestCase):
    """Issue #312: the workspace database uses ``vector`` and the restoring
    role (the owner, ``CREATEDB``) is not a superuser, as in production.

    The fake ``pg_restore`` creates and comments on the dump's extensions as
    the real one does, so the server refuses them to this role."""

    def setUp(self):
        superuser = make_url(TEST_DATABASE_URL).set(drivername="postgresql+psycopg")
        self.superuser = create_engine(superuser, isolation_level="AUTOCOMMIT")
        self.addCleanup(self.superuser.dispose)
        with self.superuser.connect() as connection:
            if not connection.execute(
                text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
            ).first():
                self.skipTest("pgvector is not installed on the test server")
        suffix = uuid.uuid4().hex[:8]
        self.name = f"paw_rpx_{suffix}"
        self.template = f"{self.name}_template"
        self.role = f"paw_rpx_role_{suffix}"
        password = uuid.uuid4().hex
        self.addCleanup(self.cleanup)
        self.as_superuser(
            f"CREATE ROLE {self.role} LOGIN CREATEDB NOSUPERUSER PASSWORD '{password}'",
            f'CREATE DATABASE "{self.name}" OWNER {self.role}',
        )
        self.as_superuser("CREATE EXTENSION vector", database=self.name)
        role = superuser.set(username=self.role, password=password)
        # As in production: the owner, on the postgres database for the admin.
        self.url = role.set(database=self.name)
        self.admin_url = role.set(database="postgres")
        self.sql(
            "CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)",
            "INSERT INTO alembic_version VALUES ('0282')",
            "CREATE TABLE memories (id int, embedding vector(3))",
        )
        self.make_template()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: os.system(f"rm -rf '{self.root}'"))
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.pg_dump, self.pg_restore = write_fake_pg_tools(self.tools)

    def as_superuser(self, *statements: str, database: str | None = None) -> None:
        engine = self.superuser
        if database is not None:
            engine = create_engine(
                self.superuser.url.set(database=database), isolation_level="AUTOCOMMIT"
            )
        try:
            with engine.connect() as connection:
                for statement in statements:
                    connection.execute(text(statement))
        finally:
            if database is not None:
                engine.dispose()

    def make_template(self, *, extensions=("vector",), extra=(), mark=True):
        """What the human runs once as postgres (README, Deploy)."""
        self.as_superuser(
            f"CREATE DATABASE \"{self.template}\" TEMPLATE template0 ENCODING 'UTF8'"
        )
        self.as_superuser(
            *(f"CREATE EXTENSION {name} WITH SCHEMA public" for name in extensions),
            *extra,
            database=self.template,
        )
        if mark:
            self.as_superuser(
                f'ALTER DATABASE "{self.template}" WITH IS_TEMPLATE true',
                f'REVOKE CONNECT, TEMPORARY ON DATABASE "{self.template}" FROM PUBLIC',
            )

    def drop_template(self):
        self.as_superuser(
            f'ALTER DATABASE "{self.template}" WITH IS_TEMPLATE false',
            f'DROP DATABASE "{self.template}" WITH (FORCE)',
        )

    def sql(self, *statements: str, database: str | None = None) -> list:
        engine = create_engine(self.url.set(database=database or self.name))
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
        with self.superuser.connect() as connection:
            rows = connection.execute(
                text("SELECT datname FROM pg_database WHERE datname LIKE :p"),
                {"p": f"{self.name}%"},
            )
            return {row[0] for row in rows}

    def cleanup(self):
        names = self.databases()
        with self.superuser.connect() as connection:
            for name in names:
                connection.execute(
                    text(f'ALTER DATABASE "{name}" WITH IS_TEMPLATE false')
                )
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
            connection.execute(text(f"DROP ROLE IF EXISTS {self.role}"))

    def points(self, template: str | None) -> RestorePoints:
        return RestorePoints(
            self.root / "points",
            pg_dump=self.pg_dump,
            pg_restore=self.pg_restore,
            template=template,
        )

    def calls(self) -> list[dict]:
        path = self.tools / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_without_a_template_the_restore_is_refused_before_pg_restore(self):
        # The production failure (restore_failed: permission denied to create
        # extension "vector") is named, and nothing is left behind.
        points = self.points(None)
        point = points.create(self.url, "p1")
        with self.assertRaises(RestorePointError) as caught:
            points.verify(point, self.url, self.admin_url)
        self.assertEqual(caught.exception.code, "extension_needs_restore_template")
        self.assertIsNone(points.load("p1").verified)
        self.assertEqual(self.databases(), {self.name, self.template})
        self.assertEqual(
            [call["tool"] for call in self.calls()], ["pg_dump", "pg_restore"]
        )

    def test_with_the_template_a_non_superuser_verifies_and_restores(self):
        points = self.points(self.template)
        point = points.verify(points.create(self.url, "p1"), self.url, self.admin_url)
        self.assertEqual(point.verified["revision"], "0282")
        self.assertEqual(point.verified["tables"], 2)
        self.assertEqual(self.databases(), {self.name, self.template})

        # The migration ran (and failed its health check afterwards).
        self.sql(
            "UPDATE alembic_version SET version_num = '0283'",
            "CREATE TABLE added_by_the_migration (id int)",
        )
        replaced = points.restore(points.load("p1"), self.url, self.admin_url)
        self.assertEqual(self.databases(), {self.name, self.template, replaced})
        self.assertEqual(
            self.sql("SELECT version_num FROM alembic_version"), [("0282",)]
        )
        self.assertEqual(
            self.sql(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute"
                " WHERE attrelid = 'memories'::regclass AND attname = 'embedding'"
            ),
            [("vector(3)",)],
        )
        self.assertEqual(
            self.sql("SELECT extname FROM pg_extension ORDER BY 1"),
            [("plpgsql",), ("vector",)],
        )
        self.assertEqual(
            self.sql("SELECT to_regclass('added_by_the_migration') IS NULL"),
            [(True,)],
        )
        # The restored database is the role's (a later restore renames it).
        self.assertEqual(
            self.sql(
                "SELECT pg_get_userbyid(datdba) FROM pg_database"
                " WHERE datname = current_database()"
            ),
            [(self.role,)],
        )
        restores = [
            call
            for call in self.calls()
            if call["tool"] == "pg_restore" and "--list" not in call["argv"]
        ]
        self.assertEqual(len(restores), 2)
        for call in restores:
            self.assertTrue(any(a.startswith("--use-list=") for a in call["argv"]))
        # The TOC listings are gone.
        self.assertEqual(
            sorted(p.name for p in (self.root / "points").iterdir()),
            ["p1.dump", "p1.json"],
        )

    def test_the_template_must_hold_the_extensions_and_nothing_else(self):
        point = self.points(None).create(self.url, "p1")
        for kwargs, code in (
            ({"extensions": ()}, "restore_template_extension_missing"),
            (
                {"extensions": ("vector", "hstore")},
                "restore_template_unexpected_extension",
            ),
            (
                {"extra": ("CREATE TABLE leftover (id int)",)},
                "restore_template_not_empty",
            ),
            ({"mark": False}, "restore_template_not_a_template"),
        ):
            with self.subTest(code=code):
                self.drop_template()
                self.make_template(**kwargs)
                with self.assertRaises(RestorePointError) as caught:
                    self.points(self.template).verify(point, self.url, self.admin_url)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.databases(), {self.name, self.template})
        self.drop_template()
        with self.assertRaises(RestorePointError) as caught:
            self.points(self.template).verify(point, self.url, self.admin_url)
        self.assertEqual(caught.exception.code, "restore_template_missing")
        with self.assertRaises(RestorePointError) as caught:
            self.points(self.name).verify(point, self.url, self.admin_url)
        self.assertEqual(caught.exception.code, "restore_template_is_the_database")
        self.assertEqual(self.databases(), {self.name})
        self.assertIsNone(self.points(None).load("p1").verified)


def _real_pg_tools() -> tuple[str, str] | None:
    """``pg_dump`` / ``pg_restore`` of the test server's major version
    (``PAW_TEST_PG_BINDIR``, else the Debian / Ubuntu layout), or ``None``."""
    if not TEST_DATABASE_URL:
        return None
    engine = create_engine(
        make_url(TEST_DATABASE_URL).set(drivername="postgresql+psycopg")
    )
    try:
        with engine.connect() as connection:
            version = int(
                connection.execute(text("SHOW server_version_num")).scalar_one()
            )
    except Exception:
        return None
    finally:
        engine.dispose()
    directory = Path(
        os.environ.get("PAW_TEST_PG_BINDIR")
        or f"/usr/lib/postgresql/{version // 10000}/bin"
    )
    tools = (directory / "pg_dump", directory / "pg_restore")
    if all(os.access(tool, os.X_OK) for tool in tools):
        return str(tools[0]), str(tools[1])
    return None


class RealToolsExtensionTest(NonSuperuserExtensionTest):
    """The same with the real ``pg_dump`` / ``pg_restore`` (when the host has
    them for the server's major version; the CI runner does not)."""

    def setUp(self):
        tools = _real_pg_tools()
        if tools is None:
            self.skipTest("no pg_dump / pg_restore of the server's major version")
        super().setUp()
        self.pg_dump, self.pg_restore = tools
        self.sql("INSERT INTO memories VALUES (1, '[1,2,3]')")

    def test_without_a_template_the_restore_is_refused_before_pg_restore(self):
        points = self.points(None)
        point = points.create(self.url, "p1")
        with self.assertRaises(RestorePointError) as caught:
            points.verify(point, self.url, self.admin_url)
        self.assertEqual(caught.exception.code, "extension_needs_restore_template")
        self.assertEqual(self.databases(), {self.name, self.template})

    def test_the_whole_dump_is_what_failed_in_production(self):
        # Issue #312 as it happened: the dump restored whole into a copy of
        # template0 by the non-superuser role.
        point = self.points(None).create(self.url, "p1")
        scratch = f"{self.name}_whole"
        with create_engine(self.admin_url, isolation_level="AUTOCOMMIT").connect() as c:
            c.execute(text(f'CREATE DATABASE "{scratch}" TEMPLATE template0'))
        completed = subprocess.run(
            [
                self.pg_restore,
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                f"--dbname={scratch}",
                str(point.dump_path),
            ],
            env={**pg_environment(self.url, database=scratch), "PATH": "/usr/bin"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            'permission denied to create extension "vector"', completed.stderr
        )

    def test_with_the_template_a_non_superuser_verifies_and_restores(self):
        points = self.points(self.template)
        points.verify(points.create(self.url, "p1"), self.url, self.admin_url)
        self.sql(
            "UPDATE alembic_version SET version_num = '0283'",
            "CREATE TABLE added_by_the_migration (id int)",
        )
        points.restore(points.load("p1"), self.url, self.admin_url)
        self.assertEqual(
            self.sql("SELECT version_num FROM alembic_version"), [("0282",)]
        )
        self.assertEqual(
            self.sql("SELECT id, embedding::text FROM memories"), [(1, "[1,2,3]")]
        )
        self.assertEqual(
            self.sql("SELECT to_regclass('added_by_the_migration') IS NULL"),
            [(True,)],
        )
