"""Revision 0027: models, migration and database must describe the same schema.

The first classes need no server. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the migration up and down, compare it with the
models through Alembic's autogenerate and through the catalogs, and prove every CHECK
constraint, key and referential action by violating it.
"""

import importlib.util
import io
import re
import unittest
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    UniqueConstraint,
    create_engine,
    text,
)

from paw_backend.db import Base
from paw_backend.repositories import limits
from paw_backend.repositories.models import (
    BRANCH_SQL_PATTERN,
    NAME_SQL_PATTERN,
    REMOTE_SQL_PATTERN,
    TABLE_NAMES,
)
from paw_backend.repositories.records import CheckoutState, RepositorySource
from paw_backend.repositories.service import _VIOLATIONS
from paw_backend.tools.scope import MAX_PATH_LENGTH, MAX_REMOTES

from .memory_support import (
    MemoryDatabaseTestCase,
    migrate,
    requires_postgres,
    sync_database_url,
)
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0027"
SCHEMA = "paw_repositories_drift_check"
REPOSITORIES, REMOTES, CHECKOUTS = TABLE_NAMES
T0 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


def check_sql(table: str) -> dict[str, str]:
    """The text of every CHECK constraint of a model table, by its short name."""
    return {
        constraint.name.removeprefix(f"ck_{table}_"): str(constraint.sqltext)
        for constraint in Base.metadata.tables[table].constraints
        if isinstance(constraint, CheckConstraint)
    }


def literals(sql: str) -> set[str]:
    return set(re.findall(r"'(\w+)'", sql))


class ModelsMetadataTest(unittest.TestCase):
    def tables(self):
        return [Base.metadata.tables[name] for name in TABLE_NAMES]

    def test_the_schema_has_the_expected_tables(self):
        self.assertEqual(
            TABLE_NAMES, ("repositories", "repository_remotes", "repository_checkouts")
        )
        for name in TABLE_NAMES:
            self.assertIn(name, Base.metadata.tables)

    def test_every_constraint_and_index_has_a_conventional_name(self):
        prefixes = {
            "PrimaryKeyConstraint": "pk_",
            "ForeignKeyConstraint": "fk_",
            "UniqueConstraint": "uq_",
            "CheckConstraint": "ck_",
        }
        for table in self.tables():
            for constraint in table.constraints:
                with self.subTest(table=table.name, constraint=constraint.name):
                    name = constraint.name
                    self.assertIsNotNone(name)
                    prefix = prefixes[type(constraint).__name__]
                    self.assertTrue(name.startswith(prefix + table.name))
                    self.assertLessEqual(len(name), 63)
            for index in table.indexes:
                with self.subTest(table=table.name, index=index.name):
                    self.assertTrue(
                        index.name.startswith(
                            (f"ix_{table.name}_", f"uq_{table.name}_")
                        )
                    )
                    self.assertLessEqual(len(index.name), 63)

    def test_the_foreign_keys_and_their_referential_actions(self):
        found = {}
        for table in self.tables():
            for constraint in table.constraints:
                if isinstance(constraint, ForeignKeyConstraint):
                    found[(table.name, tuple(c.name for c in constraint.columns))] = (
                        constraint.referred_table.name,
                        tuple(e.column.name for e in constraint.elements),
                        constraint.ondelete,
                    )
        self.assertEqual(
            found,
            {
                (REPOSITORIES, ("project_id",)): ("projects", ("id",), "CASCADE"),
                (REPOSITORIES, ("created_by",)): ("users", ("id",), "SET NULL"),
                (REMOTES, ("repository_id", "project_id")): (
                    REPOSITORIES,
                    ("id", "project_id"),
                    "CASCADE",
                ),
                (CHECKOUTS, ("repository_id", "project_id")): (
                    REPOSITORIES,
                    ("id", "project_id"),
                    "CASCADE",
                ),
                (CHECKOUTS, ("user_id",)): ("users", ("id",), "RESTRICT"),
            },
        )

    def test_the_keys_that_carry_the_isolation_rules(self):
        def uniques(table):
            return {
                tuple(c.name for c in constraint.columns)
                for constraint in Base.metadata.tables[table].constraints
                if isinstance(constraint, UniqueConstraint)
            }

        def primary(table):
            return [c.name for c in Base.metadata.tables[table].primary_key.columns]

        self.assertEqual(uniques(REPOSITORIES), {("id", "project_id")})
        self.assertEqual(uniques(REMOTES), {("project_id", "url")})
        self.assertEqual(uniques(CHECKOUTS), {("repository_id", "user_id"), ("path",)})
        self.assertEqual(primary(REPOSITORIES), ["id"])
        self.assertEqual(primary(REMOTES), ["repository_id", "url"])
        self.assertEqual(primary(CHECKOUTS), ["id"])
        (index,) = [
            i
            for i in Base.metadata.tables[REPOSITORIES].indexes
            if i.name == "uq_repositories_project_id_lower_name"
        ]
        self.assertTrue(index.unique)

    def test_the_violation_map_of_the_service_names_real_constraints(self):
        names = set()
        for table in self.tables():
            for constraint in table.constraints:
                if isinstance(
                    constraint,
                    UniqueConstraint
                    | PrimaryKeyConstraint
                    | ForeignKeyConstraint
                    | CheckConstraint,
                ):
                    names.add(constraint.name)
            names.update(
                i.name for i in table.indexes if isinstance(i, Index) and i.unique
            )
        for name in _VIOLATIONS:
            with self.subTest(name=name):
                self.assertIn(name, names)

    def test_timestamps_have_no_default_so_the_clock_of_the_service_decides(self):
        for table, names in {
            REPOSITORIES: ("created_at", "updated_at"),
            REMOTES: ("created_at",),
            CHECKOUTS: ("created_at", "updated_at"),
        }.items():
            for name in names:
                column = Base.metadata.tables[table].columns[name]
                with self.subTest(table=table, column=name):
                    self.assertIsNone(column.server_default)
                    self.assertFalse(column.nullable)

    def test_the_limits_of_the_database_match_the_service_limits(self):
        self.assertIn(f"{{0,{limits.MAX_NAME_CHARS - 1}}}", NAME_SQL_PATTERN)
        self.assertIn(f"{{0,{limits.MAX_BRANCH_CHARS - 1}}}", BRANCH_SQL_PATTERN)
        remotes = check_sql(REMOTES)["url_valid"]
        self.assertIn(f"BETWEEN 10 AND {limits.MAX_REMOTE_URL_CHARS}", remotes)
        self.assertIn("octet_length(url)", remotes)
        path_check = check_sql(CHECKOUTS)["path_valid"]
        self.assertIn(f"BETWEEN 2 AND {limits.MAX_PATH_CHARS}", path_check)
        self.assertIn(f"octet_length(path) <= {limits.MAX_PATH_BYTES}", path_check)
        self.assertEqual(limits.MAX_PATH_BYTES, 2048)
        self.assertEqual(limits.MAX_PATH_CHARS, MAX_PATH_LENGTH)
        self.assertEqual(limits.MAX_REMOTES_PER_REPOSITORY, MAX_REMOTES)
        self.assertEqual(limits.MAX_REMOTE_URL_CHARS, 1024)

    def test_the_allowed_values_of_the_database_are_those_of_the_service(self):
        self.assertEqual(
            literals(check_sql(REPOSITORIES)["source_valid"]),
            {s.value for s in RepositorySource},
        )
        self.assertEqual(
            literals(check_sql(CHECKOUTS)["state_valid"]),
            {s.value for s in CheckoutState},
        )
        from paw_backend.authz.capabilities import RepoPermission

        self.assertEqual(
            literals(check_sql(REPOSITORIES)["acl_allowed_valid"]),
            {p.value for p in RepoPermission},
        )

    def test_the_migration_repeats_the_model_patterns(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "versions"
            / "0027_repository_registration.py"
        )
        spec = importlib.util.spec_from_file_location("migration_0027", path)
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        self.assertEqual(migration.NAME_PATTERN, NAME_SQL_PATTERN)
        self.assertEqual(migration.BRANCH_PATTERN, BRANCH_SQL_PATTERN)
        self.assertEqual(migration.REMOTE_PATTERN, REMOTE_SQL_PATTERN)
        self.assertEqual(
            (migration.revision, migration.down_revision), ("0027", "0030")
        )


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_upgrade_creates_the_repositories_before_their_children(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        for child in (REMOTES, CHECKOUTS):
            self.assertLess(
                sql.index(f"CREATE TABLE {REPOSITORIES} "),
                sql.index(f"CREATE TABLE {child} "),
            )
        self.assertIn("REFERENCES projects (id) ON DELETE CASCADE", sql)
        self.assertIn("REFERENCES users (id) ON DELETE SET NULL", sql)
        self.assertIn("REFERENCES users (id) ON DELETE RESTRICT", sql)
        self.assertIn("REFERENCES repositories (id, project_id) ON DELETE CASCADE", sql)
        self.assertIn("lower(name)", sql)

    def test_the_foreign_keys_are_spelled_so_the_task_migration_test_is_not_confused(
        self,
    ):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        self.assertNotIn("FOREIGN KEY(project_id)", sql)
        self.assertNotIn("FOREIGN KEY(created_by)", sql)

    def test_upgrade_touches_no_table_of_another_area(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        for other in ("ALTER TABLE tasks", "ALTER TABLE memories", "ALTER TABLE users"):
            self.assertNotIn(other, sql)
        self.assertNotIn("ALTER TABLE projects", sql)

    def test_downgrade_drops_the_children_before_the_repositories(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        for child in (REMOTES, CHECKOUTS):
            self.assertLess(
                sql.index(f"DROP TABLE {child};"),
                sql.index(f"DROP TABLE {REPOSITORIES};"),
            )

    def test_the_migration_revokes_public_and_grants_least_privileges(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        # Offline without PAW_APP_DATABASE_ROLE only the REVOKE is rendered; the
        # exact privilege sets are proven by ``test_repositories_grants``.
        for table in TABLE_NAMES:
            self.assertIn(f"REVOKE ALL ON {table} FROM PUBLIC", sql)


def only_repository_objects(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in TABLE_NAMES
    table = getattr(obj, "table", None)
    return table is None or table.name in TABLE_NAMES


def catalog(connection, schema: str) -> dict[str, list[tuple]]:
    """Columns, constraints and indexes of the repository tables in ``schema``."""
    params = {"schema": schema, "tables": list(TABLE_NAMES)}

    def clean(value):
        return value.replace(f"{schema}.", "") if isinstance(value, str) else value

    def rows(sql: str) -> list[tuple]:
        result = connection.execute(text(sql), params)
        return sorted(tuple(clean(v) for v in row) for row in result)

    return {
        "columns": rows(
            """
            SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
                   a.attnotnull, pg_get_expr(d.adbin, d.adrelid)
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE n.nspname = :schema AND c.relname = ANY (:tables)
              AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
            """
        ),
        "constraints": rows(
            """
            SELECT c.relname, con.conname, con.contype::text,
                   pg_get_constraintdef(con.oid)
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema AND c.relname = ANY (:tables)
            """
        ),
        "indexes": rows(
            """
            SELECT tablename, indexname, indexdef FROM pg_indexes
            WHERE schemaname = :schema AND tablename = ANY (:tables)
            """
        ),
    }


@requires_postgres
class RepositoryMigrationDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        migrate("downgrade", "base")
        self.addCleanup(migrate, "downgrade", "base")
        self.engine = create_engine(sync_database_url())
        self.addCleanup(self.engine.dispose)

    def scalars(self, sql: str, **params) -> list:
        with self.engine.connect() as connection:
            return list(connection.execute(text(sql), params).scalars())

    def tables(self) -> set[str]:
        return set(
            self.scalars("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )

    def test_upgrade_creates_the_schema_and_downgrade_removes_it(self):
        previous = previous_revision()

        migrate("upgrade", REVISION)

        self.assertTrue(set(TABLE_NAMES) <= self.tables())
        self.assertEqual(
            self.scalars("SELECT version_num FROM alembic_version"), [REVISION]
        )

        migrate("downgrade", previous)

        self.assertEqual(self.tables() & set(TABLE_NAMES), set())
        self.assertEqual(
            self.scalars("SELECT version_num FROM alembic_version"), [previous]
        )
        leftovers = self.scalars(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            " AND tablename IN ('repositories', 'repository_remotes',"
            " 'repository_checkouts')"
        )
        self.assertEqual(leftovers, [])
        # The layers below are untouched.
        self.assertIn("projects", self.tables())
        self.assertIn("users", self.tables())

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        previous = previous_revision()
        migrate("upgrade", REVISION)
        migrate("downgrade", previous)
        migrate("upgrade", REVISION)
        self.assertTrue(set(TABLE_NAMES) <= self.tables())

    def test_head_contains_the_schema(self):
        migrate("upgrade", "head")
        self.assertTrue(set(TABLE_NAMES) <= self.tables())

    def test_the_downgrade_leaves_projects_and_users_alone(self):
        migrate("upgrade", REVISION)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, login_name, system_role, status,"
                    " passkey_required, created_at, updated_at) VALUES"
                    " (gen_random_uuid(), 'someone', 'user', 'active', false,"
                    " now(), now())"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO projects (name, created_at, updated_at)"
                    " VALUES ('Alpha', now(), now())"
                )
            )

        migrate("downgrade", previous_revision())

        self.assertEqual(self.scalars("SELECT count(*) FROM users"), [1])
        self.assertEqual(self.scalars("SELECT count(*) FROM projects"), [1])

    def autogenerate_diff(self, connection) -> list:
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_object": only_repository_objects,
            },
        )
        return compare_metadata(context, Base.metadata)

    def test_alembic_autogenerate_finds_no_difference_between_models_and_migration(
        self,
    ):
        migrate("upgrade", "head")
        with self.engine.connect() as connection:
            self.assertEqual(self.autogenerate_diff(connection), [])

    def test_the_autogenerate_check_notices_a_schema_that_drifted(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection, connection.begin() as transaction:
            for statement in (
                "DROP INDEX ix_repositories_created_by",
                "ALTER TABLE repositories ALTER COLUMN name DROP NOT NULL",
                "ALTER TABLE repositories ADD COLUMN unexpected text",
                "ALTER TABLE repository_checkouts ALTER COLUMN path TYPE varchar(20)",
            ):
                connection.execute(text(statement))
            diff = self.autogenerate_diff(connection)
            transaction.rollback()
        operations = [
            step
            for entry in diff
            for step in (entry if isinstance(entry, list) else [entry])
        ]
        self.assertEqual(
            sorted({operation[0] for operation in operations}),
            ["add_index", "modify_nullable", "modify_type", "remove_column"],
        )

    def created_catalog(self) -> dict[str, list[tuple]]:
        """The catalog of a schema built from the models with ``create_all``."""
        tables = [Base.metadata.tables[n] for n in ("users", "projects", *TABLE_NAMES)]
        with self.engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
            connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        try:
            with self.engine.begin() as connection:
                scoped = connection.execution_options(
                    schema_translate_map={None: SCHEMA}
                )
                Base.metadata.create_all(scoped, tables=tables)
            with self.engine.connect() as connection:
                return catalog(connection, SCHEMA)
        finally:
            with self.engine.begin() as connection:
                connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))

    def test_the_migration_and_the_models_produce_the_same_catalog(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection:
            migrated = catalog(connection, "public")
        created = self.created_catalog()

        for kind in ("columns", "constraints", "indexes"):
            with self.subTest(kind):
                self.assertEqual(migrated[kind], created[kind])
        names = {row[1] for row in migrated["constraints"]}
        for expected in (
            "ck_repositories_name_valid",
            "ck_repositories_acl_allowed_valid",
            "ck_repository_remotes_url_valid",
            "ck_repository_checkouts_path_valid",
            "fk_repositories_project_id_projects",
            "fk_repositories_created_by_users",
            "fk_repository_remotes_repository_id_repositories",
            "fk_repository_checkouts_repository_id_repositories",
            "fk_repository_checkouts_user_id_users",
            "uq_repository_checkouts_path",
            "uq_repository_checkouts_repository_id",
            "uq_repository_remotes_project_id",
            "pk_repository_remotes",
        ):
            self.assertIn(expected, names)
        definitions = {row[1]: row[2] for row in migrated["indexes"]}
        self.assertIn(
            "(project_id, lower(name))",
            definitions["uq_repositories_project_id_lower_name"],
        )
        self.assertIn("UNIQUE", definitions["uq_repositories_project_id_lower_name"])
        self.assertEqual(
            len(migrated["columns"]),
            sum(len(Base.metadata.tables[name].columns) for name in TABLE_NAMES),
        )


@requires_postgres
class ConstraintsTest(MemoryDatabaseTestCase):
    """Every rule of the database, proven by breaking it (one rule at a time)."""

    def insert(self, table: str, **values) -> None:
        columns = ", ".join(values)
        marks = ", ".join(f":{name}" for name in values)
        self.session.execute(
            text(f"INSERT INTO {table} ({columns}) VALUES ({marks})"), values
        )

    def user(self) -> str:
        return self.session.execute(
            text(
                "INSERT INTO users (id, login_name, system_role, status,"
                " passkey_required, created_at, updated_at) VALUES"
                " (gen_random_uuid(), 'u' || substr(md5(random()::text), 1, 12),"
                " 'user', 'active', false, :now, :now) RETURNING id"
            ),
            {"now": T0},
        ).scalar_one()

    def project(self) -> str:
        return self.session.execute(
            text(
                "INSERT INTO projects (name, created_at, updated_at)"
                " VALUES ('Alpha', :now, :now) RETURNING id"
            ),
            {"now": T0},
        ).scalar_one()

    def repository_values(self, project, **overrides) -> dict:
        values = {
            "project_id": project,
            "name": "tool",
            "default_branch": "main",
            "source": "github_clone",
            "acl_allowed": None,
            "created_by": None,
            "created_at": T0,
            "updated_at": T0,
        }
        values.update(overrides)
        return values

    def repository(self, project, **overrides):
        return self.session.execute(
            text(
                "INSERT INTO repositories (project_id, name, default_branch, source,"
                " acl_allowed, created_by, created_at, updated_at) VALUES"
                " (:project_id, :name, :default_branch, :source, :acl_allowed,"
                " :created_by, :created_at, :updated_at) RETURNING id"
            ),
            self.repository_values(project, **overrides),
        ).scalar_one()

    def violates(self, project, **overrides):
        return self.violation(lambda: self.repository(project, **overrides))

    def remote(self, repository, project, url="https://github.com/acme/tool"):
        self.insert(
            REMOTES,
            repository_id=repository,
            project_id=project,
            url=url,
            created_at=T0,
        )

    def checkout(
        self,
        repository,
        project,
        user,
        path="/home/a/tool",
        state="ready",
        identity="default",
    ):
        if identity == "default":  # a ready checkout records where it is
            identity = (2049, 131) if state == "ready" else (None, None)
        return self.session.execute(
            text(
                "INSERT INTO repository_checkouts (repository_id, project_id, user_id,"
                " path, state, root_device, root_inode, created_at, updated_at)"
                " VALUES (:r, :p, :u, :path, :state, :dev, :ino, :now, :now)"
                " RETURNING id"
            ),
            {
                "r": repository,
                "p": project,
                "u": user,
                "path": path,
                "state": state,
                "dev": identity[0],
                "ino": identity[1],
                "now": T0,
            },
        ).scalar_one()

    # -- repositories ---------------------------------------------------------

    def test_a_valid_repository_of_every_source_is_accepted(self):
        project = self.project()
        for index, source in enumerate(RepositorySource):
            with self.subTest(source=source):
                self.assertIsNone(
                    self.violates(project, name=f"r{index}", source=source.value)
                )
        for index, acl in enumerate(([], ["read"], ["agent", "read", "write"])):
            with self.subTest(acl=acl):
                self.assertIsNone(
                    self.violates(project, name=f"acl{index}", acl_allowed=acl)
                )

    def test_the_name_is_a_safe_directory_name_of_at_most_100_characters(self):
        project = self.project()
        self.assertIsNone(self.violates(project, name="a" * 100))
        for name in (
            "",
            "a" * 101,
            ".hidden",
            "-flag",
            "_x",
            "a/b",
            "a b",
            "a\\b",
            "..",
            "tool.git",
            "TOOL.GIT",
            "日本語",
            "a\nb",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    self.violates(project, name=name), "ck_repositories_name_valid"
                )

    def test_the_name_is_unique_in_the_project_without_regard_to_case(self):
        project, other = self.project(), self.project()
        self.repository(project, name="Tool")
        self.assertEqual(
            self.violates(project, name="tool"), "uq_repositories_project_id_lower_name"
        )
        self.assertEqual(
            self.violates(project, name="TOOL"), "uq_repositories_project_id_lower_name"
        )
        self.assertIsNone(self.violates(other, name="tool"))

    def test_the_default_branch_cannot_be_read_as_an_option_or_climb(self):
        project = self.project()
        self.assertIsNone(
            self.violates(project, name="ok", default_branch="feature/x-1.2")
        )
        for index, branch in enumerate(
            ("", "-D", "/main", "a b", "a\\b", "日本", "x" * 201)
        ):
            with self.subTest(branch=branch):
                self.assertEqual(
                    self.violates(project, name=f"b{index}", default_branch=branch),
                    "ck_repositories_default_branch_valid",
                )

    def test_the_source_and_the_acl_must_be_known(self):
        project = self.project()
        self.assertEqual(
            self.violates(project, source="bogus"), "ck_repositories_source_valid"
        )
        for acl in (
            ["bogus"],
            ["read", "bogus"],
            ["READ"],
            ["read", "write", "agent", "read"],
        ):
            with self.subTest(acl=acl):
                self.assertEqual(
                    self.violates(
                        project, name="x" + "".join(acl)[:20], acl_allowed=acl
                    ),
                    "ck_repositories_acl_allowed_valid",
                )

    def test_the_project_must_exist_and_the_creator_is_a_user_or_null(self):
        self.assertEqual(
            self.violates("00000000-0000-0000-0000-00000000dead"),
            "fk_repositories_project_id_projects",
        )
        project = self.project()
        self.assertEqual(
            self.violates(project, created_by="00000000-0000-0000-0000-00000000dead"),
            "fk_repositories_created_by_users",
        )

    def test_deleting_the_creator_keeps_the_repository(self):
        project, user = self.project(), self.user()
        repository = self.repository(project, created_by=user)
        self.session.execute(text("DELETE FROM users WHERE id = :u"), {"u": user})
        self.assertIsNone(
            self.session.execute(
                text("SELECT created_by FROM repositories WHERE id = :r"),
                {"r": repository},
            ).scalar_one()
        )

    # -- remotes --------------------------------------------------------------

    def test_a_remote_is_a_plain_https_url_of_a_host_and_a_path(self):
        project = self.project()
        repository = self.repository(project)
        for url in (
            "https://github.com/a/b",
            "https://api.github.com/repos/a/b.git",
            "https://10.1.2.3/a",
        ):
            with self.subTest(url=url):
                self.assertIsNone(
                    self.violation(
                        lambda url=url: self.remote(repository, project, url)
                    )
                )
        for url in (
            "",
            "https://",
            "https://github.com",
            "https://github.com/",
            "http://github.com/a/b",
            "ssh://github.com/a/b",
            "git@github.com:a/b",
            "https://u@github.com/a/b",
            "https://u:p@github.com/a/b",
            "https://GitHub.com/a/b",
            "https://github.com:8443/a/b",
            "https://github.com/a b",
            "https://github.com/a?x=1",
            "https://github.com/a#x",
            "https://github.com/a\\b",
            "https://github.com/" + "a" * 1100,
        ):
            with self.subTest(url=url[:40]):
                self.assertEqual(
                    self.violation(
                        lambda url=url: self.remote(repository, project, url)
                    ),
                    "ck_repository_remotes_url_valid",
                )

    def test_a_url_belongs_to_one_repository_of_a_project(self):
        project, other = self.project(), self.project()
        first = self.repository(project, name="first")
        second = self.repository(project, name="second")
        third = self.repository(other, name="third")
        self.remote(first, project)
        self.assertEqual(
            self.violation(lambda: self.remote(first, project)), "pk_repository_remotes"
        )
        self.assertEqual(
            self.violation(lambda: self.remote(second, project)),
            "uq_repository_remotes_project_id",
        )
        self.assertIsNone(self.violation(lambda: self.remote(third, other)))

    def test_a_remote_cannot_name_another_project_than_its_repository(self):
        project, other = self.project(), self.project()
        repository = self.repository(project)
        self.assertEqual(
            self.violation(lambda: self.remote(repository, other)),
            "fk_repository_remotes_repository_id_repositories",
        )

    # -- checkouts ------------------------------------------------------------

    def test_a_checkout_path_is_absolute_canonical_and_bounded(self):
        project, user = self.project(), self.user()
        repository = self.repository(project)
        self.assertIsNone(
            self.violation(lambda: self.checkout(repository, project, user, "/a/b.c"))
        )
        for path in (
            "",
            "/",
            "relative/path",
            "/a/b/",
            "/a/../b",
            "/a/./b",
            "/..",
            "/a/..",
            "/" + "a" * 1024,
        ):
            other = self.user()
            with self.subTest(path=path[:30]):
                self.assertEqual(
                    self.violation(
                        lambda o=other, p=path: self.checkout(repository, project, o, p)
                    ),
                    "ck_repository_checkouts_path_valid",
                )

    def test_the_state_must_be_known(self):
        project, user = self.project(), self.user()
        repository = self.repository(project)
        self.assertEqual(
            self.violation(
                lambda: self.checkout(repository, project, user, state="failed")
            ),
            "ck_repository_checkouts_state_valid",
        )

    def test_the_directory_identity_exists_exactly_for_a_ready_checkout(self):
        project, user = self.project(), self.user()
        repository = self.repository(project)
        ready_without = self.violation(
            lambda: self.checkout(repository, project, user, identity=(None, None))
        )
        self.assertEqual(
            ready_without, "ck_repository_checkouts_identity_matches_state"
        )
        pending_with = self.violation(
            lambda: self.checkout(
                repository, project, user, "/h/p", state="pending", identity=(1, 2)
            )
        )
        self.assertEqual(pending_with, "ck_repository_checkouts_identity_matches_state")
        half = self.violation(
            lambda: self.checkout(repository, project, user, "/h/q", identity=(1, None))
        )
        self.assertEqual(half, "ck_repository_checkouts_identity_matches_state")
        self.assertIsNone(
            self.violation(
                lambda: self.checkout(
                    repository,
                    project,
                    user,
                    "/h/r",
                    state="pending",
                    identity=(None, None),
                )
            )
        )

    def test_the_directory_identity_is_not_negative_and_holds_64_bits(self):
        project = self.project()
        repository = self.repository(project)
        for index, identity in enumerate(((-1, 1), (1, -1))):
            user = self.user()
            with self.subTest(identity=identity):
                self.assertEqual(
                    self.violation(
                        lambda u=user, i=identity, n=index: self.checkout(
                            repository, project, u, f"/h/n{n}", identity=i
                        )
                    ),
                    "ck_repository_checkouts_identity_not_negative",
                )
        biggest = 2**64 - 1  # an unsigned 64-bit st_dev / st_ino
        self.assertIsNone(
            self.violation(
                lambda: self.checkout(
                    repository,
                    project,
                    self.user(),
                    "/h/big",
                    identity=(biggest, biggest),
                )
            )
        )

    def test_the_path_is_bounded_by_its_encoded_length_before_the_index_is(self):
        # 1024 characters of 4 bytes are 4096 bytes: more than a btree entry can
        # hold (about 2700). The CHECK refuses it first, with a name a service can map.
        project, user = self.project(), self.user()
        repository = self.repository(project)
        four = "\U00020000"
        limit = limits.MAX_PATH_BYTES
        at_limit = "/" + four * ((limit - 2) // 4) + "h" * ((limit - 2) % 4) + "x"
        self.assertEqual(len(at_limit.encode()), limit)
        self.assertIsNone(
            self.violation(lambda: self.checkout(repository, project, user, at_limit)),
            "a path of exactly the limit is stored, index entry included",
        )
        for index, path in enumerate(
            (at_limit + "y", "/" + four * (limits.MAX_PATH_CHARS - 1))
        ):
            with self.subTest(bytes=len(path.encode())):
                self.assertEqual(
                    self.violation(
                        lambda p=path, n=index: self.checkout(
                            repository, project, self.user(), p
                        )
                    ),
                    "ck_repository_checkouts_path_valid",
                )

    def test_a_remote_url_is_bounded_by_its_encoded_length_too(self):
        project = self.project()
        repository = self.repository(project)
        url = "https://git.example.org/" + "\u00e9" * 600  # 1200 bytes, 624 characters
        self.assertEqual(
            self.violation(lambda: self.remote(repository, project, url)),
            "ck_repository_remotes_url_valid",
        )

    def test_a_user_has_one_checkout_per_repository_and_a_path_belongs_to_one(self):
        project = self.project()
        first, second = self.user(), self.user()
        one = self.repository(project, name="one")
        two = self.repository(project, name="two")
        self.checkout(one, project, first, "/h/1")
        self.assertEqual(
            self.violation(lambda: self.checkout(one, project, first, "/h/other")),
            "uq_repository_checkouts_repository_id",
        )
        self.assertEqual(
            self.violation(lambda: self.checkout(two, project, second, "/h/1")),
            "uq_repository_checkouts_path",
        )
        self.assertIsNone(
            self.violation(lambda: self.checkout(one, project, second, "/h/2"))
        )
        self.assertIsNone(
            self.violation(lambda: self.checkout(two, project, first, "/h/3"))
        )

    def test_a_checkout_cannot_name_another_project_than_its_repository(self):
        project, other, user = self.project(), self.project(), self.user()
        repository = self.repository(project)
        self.assertEqual(
            self.violation(lambda: self.checkout(repository, other, user)),
            "fk_repository_checkouts_repository_id_repositories",
        )

    def test_a_user_who_has_a_checkout_cannot_be_deleted(self):
        project, user = self.project(), self.user()
        repository = self.repository(project)
        self.checkout(repository, project, user)
        with self.assertRaises(Exception) as raised:
            with self.session.begin_nested():
                self.session.execute(
                    text("DELETE FROM users WHERE id = :u"), {"u": user}
                )
        self.assertEqual(
            raised.exception.orig.diag.constraint_name,
            "fk_repository_checkouts_user_id_users",
        )

    def test_deleting_a_repository_or_its_project_removes_everything_below(self):
        project, user = self.project(), self.user()
        first = self.repository(project, name="first")
        second = self.repository(project, name="second")
        for repository, path in ((first, "/h/1"), (second, "/h/2")):
            self.remote(repository, project, f"https://github.com/a/{path[-1]}")
            self.checkout(repository, project, user, path)

        self.session.execute(
            text("DELETE FROM repositories WHERE id = :r"), {"r": first}
        )

        def count(table):
            return self.session.execute(
                text(f"SELECT count(*) FROM {table}")
            ).scalar_one()

        self.assertEqual(
            (count(REPOSITORIES), count(REMOTES), count(CHECKOUTS)), (1, 1, 1)
        )
        self.session.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project})
        self.assertEqual(
            (count(REPOSITORIES), count(REMOTES), count(CHECKOUTS)), (0, 0, 0)
        )

    def test_the_project_and_repository_of_a_checkout_cannot_change_to_a_mismatch(self):
        project, other, user = self.project(), self.project(), self.user()
        repository = self.repository(project)
        checkout = self.checkout(repository, project, user)
        with self.assertRaises(Exception) as raised:
            with self.session.begin_nested():
                self.session.execute(
                    text(
                        "UPDATE repository_checkouts SET project_id = :p WHERE id = :c"
                    ),
                    {"p": other, "c": checkout},
                )
        self.assertEqual(
            raised.exception.orig.diag.constraint_name,
            "fk_repository_checkouts_repository_id_repositories",
        )


if __name__ == "__main__":
    unittest.main()
