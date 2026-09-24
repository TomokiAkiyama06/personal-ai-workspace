"""Revision 0026: models, migration and database must describe the same schema.

The first classes need no server. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the migration up and down, compare it with
the models through Alembic's autogenerate and through the two catalogs, and
prove every CHECK constraint, key and referential action by violating it.
"""

import io
import re
import unittest
from datetime import UTC, datetime, timedelta

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, create_engine, text

from paw_backend.authz.roles import ProjectRole
from paw_backend.db import Base
from paw_backend.projects import limits
from paw_backend.projects.models import TABLE_NAMES
from paw_backend.projects.records import MemberStatus, ProjectStatus

from .memory_support import (
    MemoryDatabaseTestCase,
    migrate,
    requires_postgres,
    sync_database_url,
)
from .support import paw_environment
from .test_migrations import offline_config

REVISION = "0026"
SCHEMA = "paw_projects_drift_check"
PROJECTS, MEMBERS, STOPS = TABLE_NAMES
T0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


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
    def project_tables(self):
        return [Base.metadata.tables[name] for name in TABLE_NAMES]

    def test_the_schema_has_the_expected_tables(self):
        self.assertEqual(
            TABLE_NAMES, ("projects", "project_members", "project_task_stops")
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
        for table in self.project_tables():
            for constraint in table.constraints:
                with self.subTest(table=table.name, constraint=constraint.name):
                    name = constraint.name
                    self.assertIsNotNone(name)
                    prefix = prefixes[type(constraint).__name__]
                    self.assertTrue(name.startswith(prefix + table.name))
                    self.assertLessEqual(len(name), 63)
            for index in table.indexes:
                with self.subTest(table=table.name, index=index.name):
                    self.assertTrue(index.name.startswith(f"ix_{table.name}_"))
                    self.assertLessEqual(len(index.name), 63)

    def test_the_foreign_keys_and_their_referential_actions(self):
        found = {}
        for table in self.project_tables():
            for constraint in table.constraints:
                if isinstance(constraint, ForeignKeyConstraint):
                    found[(table.name, constraint.elements[0].parent.name)] = (
                        constraint.referred_table.name,
                        constraint.ondelete,
                    )
        self.assertEqual(
            found,
            {
                (PROJECTS, "created_by"): ("users", "SET NULL"),
                (MEMBERS, "project_id"): (PROJECTS, "CASCADE"),
                (MEMBERS, "user_id"): ("users", "RESTRICT"),
                (STOPS, "project_id"): (PROJECTS, "CASCADE"),
            },
        )

    def test_the_project_ids_of_other_areas_stay_plain_uuid_columns(self):
        for table_name in ("tasks", "research_scratch_items", "task_queue_entries"):
            table = Base.metadata.tables.get(table_name)
            if table is None or "project_id" not in table.columns:
                continue
            with self.subTest(table=table_name):
                column = table.columns["project_id"]
                self.assertEqual(type(column.type).__name__, "Uuid")
                self.assertEqual(list(column.foreign_keys), [])
        self.assertIn("project_id", Base.metadata.tables["tasks"].columns)
        self.assertIn(
            "project_id", Base.metadata.tables["research_scratch_items"].columns
        )

    def test_the_primary_key_of_a_membership_is_the_pair(self):
        members = Base.metadata.tables[MEMBERS]
        self.assertEqual(
            [column.name for column in members.primary_key.columns],
            ["project_id", "user_id"],
        )

    def test_the_outbox_has_one_row_per_project_and_an_index_of_the_open_ones(self):
        stops = Base.metadata.tables[STOPS]
        self.assertEqual([c.name for c in stops.primary_key.columns], ["project_id"])
        self.assertEqual(
            {c.name: c.nullable for c in stops.columns},
            {"project_id": False, "requested_at": False, "processed_at": True},
        )
        (index,) = stops.indexes
        self.assertEqual(index.name, "ix_project_task_stops_open")
        self.assertEqual(
            [c.name for c in index.columns], ["requested_at", "project_id"]
        )
        self.assertEqual(
            str(index.dialect_options["postgresql"]["where"]), "processed_at IS NULL"
        )

    def test_timestamps_have_no_default_so_the_clock_of_the_service_decides(self):
        for table, names in {
            PROJECTS: ("created_at", "updated_at"),
            MEMBERS: ("invited_at",),
            STOPS: ("requested_at",),
        }.items():
            for name in names:
                column = Base.metadata.tables[table].columns[name]
                with self.subTest(table=table, column=name):
                    self.assertIsNone(column.server_default)
                    self.assertIsNone(column.default)
                    self.assertFalse(column.nullable)

    def test_the_limits_of_the_database_match_the_service_limits(self):
        projects = check_sql(PROJECTS)
        self.assertIn(f"BETWEEN 1 AND {limits.MAX_NAME_CHARS}", projects["name_length"])
        self.assertIn(
            f"BETWEEN 1 AND {limits.MAX_DESCRIPTION_CHARS}",
            projects["description_length"],
        )
        self.assertEqual(limits.DELETION_RETENTION, timedelta(days=30))
        self.assertIn("interval '720 hours'", projects["deletion_retention"])
        self.assertIn(
            f"name = '{limits.DELETED_PROJECT_NAME}'",
            projects["deleted_is_a_tombstone"],
        )

    def test_the_allowed_values_of_the_database_are_those_of_the_service(self):
        projects, members = check_sql(PROJECTS), check_sql(MEMBERS)
        self.assertEqual(
            literals(projects["status_valid"]), {s.value for s in ProjectStatus}
        )
        self.assertEqual(
            literals(members["status_valid"]), {s.value for s in MemberStatus}
        )
        self.assertEqual(
            literals(members["role_valid"]), {r.value for r in ProjectRole}
        )

    def test_only_the_purge_can_find_due_projects_through_a_partial_index(self):
        index = {i.name: i for i in Base.metadata.tables[PROJECTS].indexes}[
            "ix_projects_pending_deletion"
        ]
        self.assertEqual(
            [c.name for c in index.columns], ["deletion_scheduled_at", "id"]
        )
        self.assertIn(
            "pending_deletion", str(index.dialect_options["postgresql"]["where"])
        )


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_upgrade_creates_the_projects_before_their_members(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        self.assertLess(
            sql.index(f"CREATE TABLE {PROJECTS} "),
            sql.index(f"CREATE TABLE {MEMBERS} "),
        )
        self.assertLess(
            sql.index(f"CREATE TABLE {PROJECTS} "),
            sql.index(f"CREATE TABLE {STOPS} "),
        )
        self.assertIn("interval '720 hours'", sql)
        self.assertIn("REFERENCES users (id) ON DELETE RESTRICT", sql)
        self.assertIn("REFERENCES projects (id) ON DELETE CASCADE", sql)
        self.assertIn("REFERENCES users (id) ON DELETE SET NULL", sql)

    def test_upgrade_touches_no_table_of_another_area(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        for other in (
            "ALTER TABLE tasks",
            "ALTER TABLE research_scratch",
            "ALTER TABLE memories",
        ):
            self.assertNotIn(other, sql)
        self.assertNotIn("ALTER TABLE users", sql)

    def test_downgrade_drops_the_members_before_the_projects(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertLess(
            sql.index(f"DROP TABLE {MEMBERS};"), sql.index(f"DROP TABLE {PROJECTS};")
        )
        self.assertLess(
            sql.index(f"DROP TABLE {STOPS};"), sql.index(f"DROP TABLE {PROJECTS};")
        )

    def test_the_migration_grants_the_application_role_least_privileges(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        # Offline without PAW_APP_DATABASE_ROLE only the REVOKE is rendered; the
        # exact privilege sets are proven by ``test_projects_grants``.
        self.assertIn("REVOKE ALL ON projects FROM PUBLIC", sql)
        self.assertIn("REVOKE ALL ON project_members FROM PUBLIC", sql)
        self.assertIn("REVOKE ALL ON project_task_stops FROM PUBLIC", sql)


def only_project_objects(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in TABLE_NAMES
    table = getattr(obj, "table", None)
    return table is None or table.name in TABLE_NAMES


def catalog(connection, schema: str) -> dict[str, list[tuple]]:
    """Columns, constraints and indexes of the project tables in ``schema``."""
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
class ProjectMigrationDatabaseTest(unittest.TestCase):
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

    def version(self) -> list[str]:
        return self.scalars("SELECT version_num FROM alembic_version")

    def test_upgrade_creates_the_schema_and_downgrade_removes_it(self):
        previous = previous_revision()

        migrate("upgrade", REVISION)

        self.assertTrue(set(TABLE_NAMES) <= self.tables())
        self.assertEqual(self.version(), [REVISION])

        migrate("downgrade", previous)

        self.assertEqual(self.tables() & set(TABLE_NAMES), set())
        self.assertEqual(self.version(), [previous])
        leftovers = self.scalars(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            " AND tablename IN ('projects', 'project_members', 'project_task_stops')"
        )
        self.assertEqual(leftovers, [])
        # The migration touches nothing of the layers below it.
        self.assertIn("users", self.tables())
        self.assertIn("tasks", self.tables())

    def test_the_migration_can_be_applied_again_after_a_downgrade(self):
        previous = previous_revision()
        migrate("upgrade", REVISION)
        migrate("downgrade", previous)
        migrate("upgrade", REVISION)
        self.assertTrue(set(TABLE_NAMES) <= self.tables())

    def test_head_contains_the_schema(self):
        migrate("upgrade", "head")
        self.assertTrue(set(TABLE_NAMES) <= self.tables())

    def test_the_downgrade_leaves_the_users_table_and_its_rows_alone(self):
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

        migrate("downgrade", previous_revision())

        self.assertEqual(self.scalars("SELECT count(*) FROM users"), [1])

    def autogenerate_diff(self, connection) -> list:
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_object": only_project_objects,
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
                "DROP INDEX ix_projects_pending_deletion",
                "ALTER TABLE projects ALTER COLUMN status DROP DEFAULT",
                "ALTER TABLE projects ADD COLUMN unexpected text",
                "ALTER TABLE project_members ALTER COLUMN joined_at SET NOT NULL",
                "ALTER TABLE project_members ALTER COLUMN invited_at TYPE timestamp",
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
            [
                "add_index",
                "modify_default",
                "modify_nullable",
                "modify_type",
                "remove_column",
            ],
        )

    def created_catalog(self) -> dict[str, list[tuple]]:
        """The catalog of a schema built from the models with ``create_all``."""
        tables = [Base.metadata.tables["users"]] + [
            Base.metadata.tables[name] for name in TABLE_NAMES
        ]
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
            "ck_projects_deletion_retention",
            "ck_project_members_invite_expiry_matches_status",
            "fk_projects_created_by_users",
            "fk_project_members_project_id_projects",
            "fk_project_members_user_id_users",
            "pk_project_members",
            "fk_project_task_stops_project_id_projects",
            "pk_project_task_stops",
        ):
            self.assertIn(expected, names)
        index_definitions = {row[1]: row[2] for row in migrated["indexes"]}
        self.assertIn(
            "WHERE (status = 'pending_deletion'::text)",
            index_definitions["ix_projects_pending_deletion"],
        )
        self.assertIn(
            "(requested_at, project_id) WHERE (processed_at IS NULL)",
            index_definitions["ix_project_task_stops_open"],
        )
        self.assertEqual(
            len(migrated["columns"]),
            sum(len(Base.metadata.tables[name].columns) for name in TABLE_NAMES),
        )

    def test_the_catalog_check_notices_a_changed_check_constraint(self):
        migrate("upgrade", "head")
        with self.engine.connect() as connection, connection.begin() as transaction:
            before = catalog(connection, "public")
            connection.execute(
                text(
                    "ALTER TABLE projects"
                    " DROP CONSTRAINT ck_projects_deletion_retention"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE projects ADD CONSTRAINT ck_projects_deletion_retention"
                    " CHECK (deletion_scheduled_at IS NULL OR deletion_scheduled_at"
                    " = deletion_started_at + interval '48 hours')"
                )
            )
            after = catalog(connection, "public")
            transaction.rollback()
        self.assertEqual(before["columns"], after["columns"])
        self.assertNotEqual(before["constraints"], after["constraints"])


@requires_postgres
class ConstraintsTest(MemoryDatabaseTestCase):
    """Every rule of the database, proven by breaking it (one rule at a time)."""

    def insert(self, table: str, **values) -> None:
        columns = ", ".join(values)
        marks = ", ".join(f":{name}" for name in values)
        self.session.execute(
            text(f"INSERT INTO {table} ({columns}) VALUES ({marks})"), values
        )

    def user(self, status: str = "active") -> str:
        row = self.session.execute(
            text(
                "INSERT INTO users (id, login_name, system_role, status,"
                " passkey_required, created_at, updated_at) VALUES"
                " (gen_random_uuid(), 'u' || substr(md5(random()::text), 1, 12),"
                " 'user', :status, false, :now, :now) RETURNING id"
            ),
            {"status": status, "now": T0},
        )
        return row.scalar_one()

    def project_values(self, **overrides) -> dict:
        values = {
            "name": "Alpha",
            "description": None,
            "status": "active",
            "created_by": None,
            "created_at": T0,
            "updated_at": T0,
            "deletion_started_at": None,
            "deletion_scheduled_at": None,
            "deleted_at": None,
        }
        values.update(overrides)
        return values

    def project(self, **overrides):
        row = self.session.execute(
            text(
                "INSERT INTO projects (name, description, status, created_by,"
                " created_at, updated_at, deletion_started_at,"
                " deletion_scheduled_at, deleted_at) VALUES (:name, :description,"
                " :status, :created_by, :created_at, :updated_at,"
                " :deletion_started_at, :deletion_scheduled_at, :deleted_at)"
                " RETURNING id"
            ),
            self.project_values(**overrides),
        )
        return row.scalar_one()

    def violates_project(self, **overrides) -> str | None:
        return self.violation(lambda: self.project(**overrides))

    def pending(self, **overrides) -> dict:
        values = {
            "status": "pending_deletion",
            "deletion_started_at": T0,
            "deletion_scheduled_at": T0 + timedelta(days=30),
        }
        values.update(overrides)
        return values

    def tombstone(self, **overrides) -> dict:
        values = self.pending(
            status="deleted",
            name="Deleted Project",
            deleted_at=T0 + timedelta(days=30),
        )
        values.update(overrides)
        return values

    # -- projects -------------------------------------------------------------------

    def test_a_valid_project_of_every_status_is_accepted(self):
        for values in (
            {},
            {"status": "archived"},
            {"description": "d" * 2000, "name": "n" * 100},
            self.pending(),
            self.tombstone(),
        ):
            with self.subTest(values=values):
                self.assertIsNone(self.violates_project(**values))

    def test_the_status_must_be_known(self):
        self.assertEqual(
            self.violates_project(status="bogus"), "ck_projects_status_valid"
        )
        self.assertEqual(self.violates_project(status=""), "ck_projects_status_valid")

    def test_the_name_has_1_to_100_characters_and_no_outer_spaces(self):
        self.assertEqual(self.violates_project(name=""), "ck_projects_name_length")
        self.assertEqual(
            self.violates_project(name="n" * 101), "ck_projects_name_length"
        )
        self.assertEqual(self.violates_project(name=" x"), "ck_projects_name_trimmed")
        self.assertEqual(self.violates_project(name="x "), "ck_projects_name_trimmed")
        self.assertIsNone(self.violates_project(name="x y"))

    def test_the_description_is_none_or_1_to_2000_characters(self):
        self.assertEqual(
            self.violates_project(description=""), "ck_projects_description_length"
        )
        self.assertEqual(
            self.violates_project(description="d" * 2001),
            "ck_projects_description_length",
        )

    def test_the_two_deletion_timestamps_come_together(self):
        self.assertEqual(
            self.violates_project(deletion_started_at=T0),
            "ck_projects_deletion_times_paired",
        )
        self.assertEqual(
            self.violates_project(
                status="pending_deletion",
                deletion_scheduled_at=T0 + timedelta(days=30),
            ),
            "ck_projects_deletion_times_paired",
        )

    def test_the_deletion_timestamps_exist_exactly_for_pending_and_deleted(self):
        self.assertEqual(
            self.violates_project(status="pending_deletion"),
            "ck_projects_deletion_times_match_status",
        )
        self.assertEqual(
            self.violates_project(
                deletion_started_at=T0, deletion_scheduled_at=T0 + timedelta(days=30)
            ),
            "ck_projects_deletion_times_match_status",
        )
        self.assertEqual(
            self.violates_project(
                status="archived",
                deletion_started_at=T0,
                deletion_scheduled_at=T0 + timedelta(days=30),
            ),
            "ck_projects_deletion_times_match_status",
        )

    def test_pending_deletion_lasts_exactly_30_days(self):
        one_off = timedelta(microseconds=1)
        for scheduled in (
            T0 + timedelta(days=29),
            T0 + timedelta(days=30) + one_off,
            T0 + timedelta(days=30) - one_off,
            T0 + timedelta(days=31),
            T0,
            T0 - timedelta(days=30),
        ):
            with self.subTest(scheduled=scheduled.isoformat()):
                self.assertEqual(
                    self.violates_project(
                        **self.pending(deletion_scheduled_at=scheduled)
                    ),
                    "ck_projects_deletion_retention",
                )
        self.assertIsNone(self.violates_project(**self.pending()))

    def test_30_days_are_720_hours_in_every_session_time_zone(self):
        # Berlin changes to summer time on 2026-03-29: a calendar-day interval
        # would be one hour short of 720 hours here.
        started = datetime(2026, 3, 1, tzinfo=UTC)
        for zone in ("UTC", "Europe/Berlin", "America/New_York", "Asia/Tokyo"):
            with self.subTest(zone=zone):
                self.session.execute(text(f"SET LOCAL TIME ZONE '{zone}'"))
                self.assertIsNone(
                    self.violates_project(
                        **self.pending(
                            deletion_started_at=started,
                            deletion_scheduled_at=started + timedelta(hours=720),
                        )
                    )
                )

    def test_a_deletion_time_exists_only_for_a_deleted_project(self):
        self.assertEqual(
            self.violates_project(**self.tombstone(deleted_at=None)),
            "ck_projects_deleted_at_matches_status",
        )
        self.assertEqual(
            self.violates_project(**self.pending(deleted_at=T0)),
            "ck_projects_deleted_at_matches_status",
        )
        self.assertEqual(
            self.violates_project(deleted_at=T0),
            "ck_projects_deleted_at_matches_status",
        )

    def test_a_deleted_project_keeps_neither_its_name_nor_its_description(self):
        self.assertEqual(
            self.violates_project(**self.tombstone(name="Alpha")),
            "ck_projects_deleted_is_a_tombstone",
        )
        self.assertEqual(
            self.violates_project(**self.tombstone(description="Secret")),
            "ck_projects_deleted_is_a_tombstone",
        )

    def test_the_creator_must_be_a_user_and_becomes_null_with_the_user(self):
        self.assertEqual(
            self.violation(
                lambda: self.project(created_by="00000000-0000-0000-0000-00000000dead")
            ),
            "fk_projects_created_by_users",
        )
        creator = self.user()
        project = self.project(created_by=creator)
        self.session.execute(text("DELETE FROM users WHERE id = :id"), {"id": creator})
        kept = self.session.execute(
            text("SELECT created_by FROM projects WHERE id = :id"), {"id": project}
        ).scalar_one()
        self.assertIsNone(kept)

    # -- members ---------------------------------------------------------------------

    def member_values(self, project, user, **overrides) -> dict:
        values = {
            "project_id": project,
            "user_id": user,
            "role": "viewer",
            "status": "active",
            "invited_at": T0,
            "invite_expires_at": None,
            "joined_at": T0,
        }
        values.update(overrides)
        return values

    def violates_member(self, project, user, **overrides) -> str | None:
        values = self.member_values(project, user, **overrides)
        return self.violation(lambda: self.insert(MEMBERS, **values))

    def invited(self, **overrides) -> dict:
        values = {
            "status": "invited",
            "invite_expires_at": T0 + timedelta(days=14),
            "joined_at": None,
        }
        values.update(overrides)
        return values

    def test_a_valid_member_and_invitation_of_every_role_are_accepted(self):
        project, user = self.project(), self.user()
        self.assertIsNone(self.violates_member(project, user))
        for role in ("manager", "contributor", "viewer"):
            other = self.user()
            with self.subTest(role=role):
                self.assertIsNone(self.violates_member(project, other, role=role))
        third = self.user()
        self.assertIsNone(self.violates_member(project, third, **self.invited()))

    def test_the_role_and_the_status_must_be_known(self):
        project, user = self.project(), self.user()
        self.assertEqual(
            self.violates_member(project, user, role="owner"),
            "ck_project_members_role_valid",
        )
        self.assertEqual(
            self.violates_member(project, user, role="Manager"),
            "ck_project_members_role_valid",
        )
        self.assertEqual(
            self.violates_member(project, user, status="declined", joined_at=None),
            "ck_project_members_status_valid",
        )

    def test_an_expiry_exists_exactly_for_an_invitation(self):
        project, user = self.project(), self.user()
        self.assertEqual(
            self.violates_member(project, user, **self.invited(invite_expires_at=None)),
            "ck_project_members_invite_expiry_matches_status",
        )
        self.assertEqual(
            self.violates_member(
                project, user, invite_expires_at=T0 + timedelta(days=1)
            ),
            "ck_project_members_invite_expiry_matches_status",
        )

    def test_a_join_time_exists_exactly_for_an_accepted_member(self):
        project, user = self.project(), self.user()
        self.assertEqual(
            self.violates_member(project, user, joined_at=None),
            "ck_project_members_joined_at_matches_status",
        )
        self.assertEqual(
            self.violates_member(project, user, **self.invited(joined_at=T0)),
            "ck_project_members_joined_at_matches_status",
        )

    def test_an_invitation_expires_after_it_was_made(self):
        project, user = self.project(), self.user()
        for expires in (T0, T0 - timedelta(seconds=1)):
            with self.subTest(expires=expires.isoformat()):
                self.assertEqual(
                    self.violates_member(
                        project, user, **self.invited(invite_expires_at=expires)
                    ),
                    "ck_project_members_invite_expires_after_invited",
                )
        self.assertIsNone(
            self.violates_member(
                project,
                user,
                **self.invited(invite_expires_at=T0 + timedelta(microseconds=1)),
            )
        )

    def test_a_user_has_one_row_per_project(self):
        project, other_project, user = self.project(), self.project(), self.user()
        self.assertIsNone(self.violates_member(project, user))
        self.assertEqual(self.violates_member(project, user), "pk_project_members")
        self.assertEqual(
            self.violates_member(project, user, **self.invited()), "pk_project_members"
        )
        self.assertIsNone(self.violates_member(other_project, user))

    def test_the_project_and_the_user_must_exist(self):
        project, user = self.project(), self.user()
        ghost = "00000000-0000-0000-0000-00000000dead"
        self.assertEqual(
            self.violates_member(ghost, user), "fk_project_members_project_id_projects"
        )
        self.assertEqual(
            self.violates_member(project, ghost), "fk_project_members_user_id_users"
        )

    def test_a_user_who_is_still_a_member_cannot_be_deleted(self):
        project, user = self.project(), self.user()
        self.insert(MEMBERS, **self.member_values(project, user))
        self.assertEqual(
            self.violation(
                lambda: self.session.execute(
                    text("DELETE FROM users WHERE id = :id"), {"id": user}
                )
            ),
            "fk_project_members_user_id_users",
        )
        invited_user = self.user()
        self.insert(
            MEMBERS, **self.member_values(project, invited_user, **self.invited())
        )
        self.assertEqual(
            self.violation(
                lambda: self.session.execute(
                    text("DELETE FROM users WHERE id = :id"), {"id": invited_user}
                )
            ),
            "fk_project_members_user_id_users",
        )

    def test_deleting_a_project_row_removes_its_members_only(self):
        project, other, user = self.project(), self.project(), self.user()
        self.insert(MEMBERS, **self.member_values(project, user))
        self.insert(MEMBERS, **self.member_values(other, user))
        self.session.execute(
            text("DELETE FROM projects WHERE id = :id"), {"id": project}
        )
        remaining = (
            self.session.execute(text("SELECT project_id FROM project_members"))
            .scalars()
            .all()
        )
        self.assertEqual(remaining, [other])

    # -- the task-stop outbox ---------------------------------------------------------

    def violates_stop(self, project, **overrides) -> str | None:
        values = {"project_id": project, "requested_at": T0, "processed_at": None}
        values.update(overrides)
        return self.violation(lambda: self.insert(STOPS, **values))

    def test_a_project_has_one_stop_request_and_it_must_exist(self):
        project, other = self.project(), self.project()
        ghost = "00000000-0000-0000-0000-00000000dead"
        self.assertIsNone(self.violates_stop(project))
        self.assertEqual(self.violates_stop(project), "pk_project_task_stops")
        self.assertIsNone(self.violates_stop(other, processed_at=T0))
        self.assertEqual(
            self.violates_stop(ghost), "fk_project_task_stops_project_id_projects"
        )

    def test_deleting_a_project_row_removes_its_stop_request_only(self):
        project, other = self.project(), self.project()
        self.insert(STOPS, project_id=project, requested_at=T0, processed_at=None)
        self.insert(STOPS, project_id=other, requested_at=T0, processed_at=None)
        self.session.execute(
            text("DELETE FROM projects WHERE id = :id"), {"id": project}
        )
        remaining = (
            self.session.execute(text("SELECT project_id FROM project_task_stops"))
            .scalars()
            .all()
        )
        self.assertEqual(remaining, [other])

    # -- indexes ----------------------------------------------------------------------

    def test_every_foreign_key_has_an_index_that_leads_with_its_column(self):
        rows = (
            self.session.execute(
                text(
                    """
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class child ON child.oid = con.conrelid
                WHERE con.contype = 'f'
                  AND child.relname = ANY (:tables)
                  AND NOT EXISTS (
                    SELECT 1 FROM pg_index i
                    WHERE i.indrelid = con.conrelid AND i.indisvalid
                      AND i.indkey[0] = ANY (con.conkey) AND i.indpred IS NULL)
                ORDER BY con.conname
                """
                ),
                {"tables": list(TABLE_NAMES)},
            )
            .scalars()
            .all()
        )
        self.assertEqual(rows, [])

    def test_the_due_projects_are_served_by_a_partial_index(self):
        definition = self.session.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE indexname ="
                " 'ix_projects_pending_deletion'"
            )
        ).scalar_one()
        self.assertIn("(deletion_scheduled_at, id)", definition)
        self.assertIn("pending_deletion", definition)


if __name__ == "__main__":
    unittest.main()
