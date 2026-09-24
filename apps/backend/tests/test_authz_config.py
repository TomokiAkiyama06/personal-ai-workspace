"""Settings and offline-migration checks for the database role split (no PostgreSQL)."""

import io
import unittest

from alembic import command
from pydantic import ValidationError

from paw_backend.authz.diagnostics import AuditTableAccess

from .support import make_settings, paw_environment
from .test_migrations import offline_config

URL = "postgresql://paw:s3cr3t-pw@db.internal/paw"
MIGRATION_URL = "postgresql://owner:0wner-pw@db.internal/paw"


def offline_upgrade_sql(**environment: str) -> str:
    output = io.StringIO()
    with paw_environment(PAW_DATABASE_URL=URL, **environment):
        command.upgrade(offline_config(output), "head", sql=True)
    return output.getvalue()


class RoleSettingsTest(unittest.TestCase):
    def test_valid_role_names_are_accepted(self):
        for role in ("paw_app", "PawApp", "_svc", "a", "x" * 63):
            with self.subTest(role=role):
                self.assertEqual(
                    make_settings(app_database_role=role).app_database_role, role
                )

    def test_names_postgresql_treats_specially_are_refused(self):
        # A quoted "public" is still the pseudo-role PUBLIC: granting to it
        # would let every role insert into the audit table.
        for role in (
            "public",
            "PUBLIC",
            "Public",
            "postgres",
            "Postgres",
            "pg_write_all_data",
            "PG_monitor",
            "pg_",
            "none",
            "user",
            "current_user",
            "CURRENT_ROLE",
            "session_user",
        ):
            with self.subTest(role=role):
                with self.assertRaises(ValidationError) as caught:
                    make_settings(app_database_role=role)
                self.assertNotIn(role, str(caught.exception))
        # Ordinary names that merely contain those words are fine.
        for role in ("public_app", "my_postgres", "app_pg_"):
            self.assertEqual(
                make_settings(app_database_role=role).app_database_role, role
            )

    def test_anything_that_is_not_a_plain_identifier_is_refused(self):
        for role in (
            'a"b',
            "a b",
            "x; DROP TABLE audit_events",
            "a-b",
            "1abc",
            "ünï",
            "x" * 64,
            "a\nb",
            'paw_app" TO PUBLIC; --',
        ):
            with self.subTest(role=role):
                with self.assertRaises(ValidationError) as caught:
                    make_settings(app_database_role=role)
                self.assertNotIn(role, str(caught.exception))

    def test_unset_or_empty_means_no_role(self):
        self.assertIsNone(make_settings().app_database_role)
        self.assertIsNone(make_settings(app_database_role="").app_database_role)

    def test_the_migration_url_is_optional_secret_and_normalised(self):
        self.assertIsNone(make_settings().migration_database_url)
        self.assertIsNone(
            make_settings(migration_database_url="").migration_database_url
        )
        settings = make_settings(migration_database_url=MIGRATION_URL)
        self.assertEqual(
            settings.migration_database_url.get_secret_value(),
            "postgresql+psycopg://owner:0wner-pw@db.internal/paw",
        )
        self.assertNotIn("0wner-pw", repr(settings))

    def test_an_invalid_migration_url_is_refused_without_echoing_it(self):
        with self.assertRaises(ValidationError) as caught:
            make_settings(migration_database_url="mysql://u:hunter2@h/db")
        self.assertNotIn("hunter2", str(caught.exception))


class AuditTableAccessTest(unittest.TestCase):
    def test_only_an_unprivileged_non_owner_is_protected(self):
        # (owns, can_insert, can_update, can_delete, can_truncate)
        self.assertTrue(AuditTableAccess(False, True, False, False, False).protected)
        for flags in (
            (True, True, False, False, False),  # owner: can drop the triggers
            (False, True, True, False, False),
            (False, True, False, True, False),
            (False, True, False, False, True),
        ):
            with self.subTest(flags=flags):
                self.assertFalse(AuditTableAccess(*flags).protected)

    def test_a_user_that_cannot_insert_cannot_write_the_trail(self):
        self.assertTrue(
            AuditTableAccess(False, False, False, False, False).cannot_write
        )
        self.assertFalse(
            AuditTableAccess(False, True, False, False, False).cannot_write
        )


class OfflineMigrationSqlTest(unittest.TestCase):
    def test_public_is_revoked_and_nothing_is_granted_without_an_app_role(self):
        sql = offline_upgrade_sql()
        self.assertIn("REVOKE ALL ON audit_events FROM PUBLIC", sql)
        self.assertNotIn("GRANT", sql)

    def test_the_app_role_gets_insert_and_select_only_and_is_quoted(self):
        sql = offline_upgrade_sql(PAW_APP_DATABASE_ROLE="paw_app")
        self.assertIn('GRANT INSERT, SELECT ON audit_events TO "paw_app"', sql)
        # Exactly one grant on the audit table (later migrations grant on their
        # own tables, so the whole history is not counted).
        self.assertEqual(sql.count("ON audit_events TO"), 1)
        self.assertLess(sql.index("REVOKE ALL"), sql.index("GRANT INSERT"))

    def test_the_role_keeps_its_case_because_it_is_quoted(self):
        sql = offline_upgrade_sql(PAW_APP_DATABASE_ROLE="PawApp")
        self.assertIn('TO "PawApp"', sql)

    def test_a_hostile_role_name_never_reaches_the_sql(self):
        output = io.StringIO()
        with paw_environment(
            PAW_DATABASE_URL=URL,
            PAW_APP_DATABASE_ROLE='x"; DROP TABLE audit_events; --',
        ):
            with self.assertRaises(ValidationError):
                command.upgrade(offline_config(output), "head", sql=True)
        self.assertNotIn("DROP TABLE audit_events", output.getvalue())
        self.assertNotIn("GRANT", output.getvalue())

    def test_recorded_at_is_forced_to_the_database_clock_by_a_trigger(self):
        sql = offline_upgrade_sql()
        self.assertIn("NEW.recorded_at := now()", sql)
        self.assertIn("BEFORE INSERT ON audit_events", sql)
        self.assertIn(
            "ALTER TABLE audit_events ENABLE ALWAYS TRIGGER "
            "tr_audit_events_force_recorded_at",
            sql,
        )

    def test_a_migration_role_without_an_app_role_is_warned_about(self):
        with self.assertLogs("paw_backend.migrations.0025", level="WARNING") as logs:
            offline_upgrade_sql(PAW_MIGRATION_DATABASE_URL=MIGRATION_URL)
        (line,) = logs.output
        self.assertIn("PAW_APP_DATABASE_ROLE", line)
        self.assertIn("refused (503)", line)
        self.assertNotIn("0wner-pw", line)

    def test_no_warning_when_the_role_is_set_or_no_split_is_configured(self):
        with self.assertNoLogs("paw_backend.migrations.0025", level="WARNING"):
            offline_upgrade_sql(
                PAW_MIGRATION_DATABASE_URL=MIGRATION_URL,
                PAW_APP_DATABASE_ROLE="paw_app",
            )
            offline_upgrade_sql()

    def test_the_offline_sql_states_the_append_only_guard(self):
        sql = offline_upgrade_sql()
        for fragment in (
            "BEFORE UPDATE OR DELETE ON audit_events",
            "BEFORE TRUNCATE ON audit_events",
            "ENABLE ALWAYS TRIGGER",
            "recorded_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL",
        ):
            self.assertIn(fragment, sql)


if __name__ == "__main__":
    unittest.main()
