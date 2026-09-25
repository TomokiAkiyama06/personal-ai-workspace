"""Audit persistence and its append-only guarantee on a real PostgreSQL.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set (see
``test_postgres_integration.py``). Each test starts from a freshly migrated
database and returns it to ``base`` afterwards. The role tests create two
NON-superuser roles (dropped again afterwards); the test user must be allowed
to create roles.
"""

import asyncio
import io
import logging
import os
import time
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import psycopg.errors
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from paw_backend.app import create_app
from paw_backend.authz import (
    ALL_PROJECTS,
    AgentGrant,
    Authorizer,
    Capability,
    Decision,
    PostgresAuditSink,
    ProjectRole,
    Reason,
    RepoPermission,
    Resource,
    SystemRole,
)
from paw_backend.authz.audit import build_event
from paw_backend.authz.diagnostics import (
    read_audit_table_access,
    warn_if_audit_table_is_mutable,
)
from paw_backend.authz.models import AuditEventRecord
from paw_backend.db import Base, Database

from .authz_support import (
    AGENT,
    P1,
    REPO,
    U1,
    U2,
    StaticDirectory,
    StaticProvider,
    add_test_routes,
    principal,
    project,
    repo_resource,
)
from .support import make_client, make_settings, paw_environment
from .test_migrations import offline_config

TEST_DATABASE_URL = os.environ.get("PAW_TEST_DATABASE_URL")
NOW = datetime(2001, 1, 1, 12, 0, tzinfo=UTC)
# Dummy credentials of the throw-away test roles. PostgreSQL roles are cluster-wide, so
# a fixed name would collide when two test runs share one server (parallel jobs, several
# working trees): every run uses its own suffix.
_RUN_ID = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_authz_test_app_{_RUN_ID}"
OTHER_ROLE = f"paw_authz_test_other_{_RUN_ID}"
ROLE_PASSWORD = "dummy-test-password-025"


def migrate(action: str, revision: str, **environment: str) -> None:
    """Run Alembic against the test database (``env.py`` reads the URL)."""
    variables = {"PAW_DATABASE_URL": TEST_DATABASE_URL, **environment}
    with paw_environment(**variables):
        config = offline_config(io.StringIO())
        getattr(command, action)(config, revision)


def url_for_role(role: str) -> str:
    url = make_url(TEST_DATABASE_URL).set(username=role, password=ROLE_PASSWORD)
    return url.render_as_string(hide_password=False)


@unittest.skipUnless(TEST_DATABASE_URL, "PAW_TEST_DATABASE_URL is not set")
class AuditPostgresTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await asyncio.to_thread(migrate, "downgrade", "base")
        await asyncio.to_thread(migrate, "upgrade", "head")
        self.addAsyncCleanup(asyncio.to_thread, migrate, "downgrade", "base")
        self.database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(self.database.dispose)

    async def scalar(self, sql: str, database: Database | None = None):
        async with (database or self.database).session() as session:
            return (await session.execute(text(sql))).scalar()

    async def rows(self, database: Database | None = None) -> list[AuditEventRecord]:
        async with (database or self.database).session() as session:
            query = select(AuditEventRecord).order_by(AuditEventRecord.occurred_at)
            return list((await session.execute(query)).scalars())

    async def execute_rejected(
        self, sql: str, database: Database | None = None
    ) -> DBAPIError:
        async with (database or self.database).session() as session:
            with self.assertRaises(DBAPIError) as caught:
                await session.execute(text(sql))
                await session.commit()
        return caught.exception


class MigrationTest(AuditPostgresTestCase):
    async def test_upgrade_creates_the_table_index_constraints_and_triggers(self):
        self.assertEqual(
            await self.scalar("SELECT to_regclass('audit_events')::text"),
            "audit_events",
        )
        constraints = await self.scalar(
            "SELECT string_agg(conname, ',' ORDER BY conname) FROM pg_constraint "
            "WHERE conrelid = 'audit_events'::regclass AND contype IN ('c', 'p')"
        )
        # The last two are revision 0087 (issue #87: ``details`` of the audit of an
        # external research send, tests/test_privacy_audit_schema.py).
        self.assertEqual(
            constraints,
            "ck_audit_events_decision_valid,ck_audit_events_details_registered,"
            "ck_audit_events_external_send_details,pk_audit_events",
        )
        self.assertEqual(
            await self.scalar(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'audit_events' "
                "AND indexname LIKE 'ix_%'"
            ),
            "ix_audit_events_occurred_at",
        )
        triggers = await self.scalar(
            "SELECT string_agg(tgname || ':' || tgenabled::text, ',' ORDER BY tgname) "
            "FROM pg_trigger "
            "WHERE tgrelid = 'audit_events'::regclass AND NOT tgisinternal"
        )
        # 'A' = ENABLE ALWAYS: they also fire under session_replication_role=replica.
        self.assertEqual(
            triggers,
            "tr_audit_events_force_recorded_at:A,tr_audit_events_reject_truncate:A,"
            "tr_audit_events_reject_update_delete:A",
        )

    async def test_ids_are_uuid_columns_and_the_server_clock_column_exists(self):
        columns = await self.scalar(
            "SELECT string_agg(column_name || ' ' || data_type || ' ' || "
            "is_nullable, ',' ORDER BY column_name) FROM information_schema.columns "
            "WHERE table_name = 'audit_events' AND column_name IN "
            "('id', 'correlation_id', 'actor_id', 'agent_id', 'resource_id', "
            "'project_id', 'repo_id', 'recorded_at', 'client_request_id')"
        )
        self.assertEqual(
            columns,
            "actor_id uuid YES,agent_id uuid YES,client_request_id text YES,"
            "correlation_id uuid NO,id uuid NO,project_id uuid YES,"
            "recorded_at timestamp with time zone NO,repo_id uuid YES,"
            "resource_id uuid YES",
        )
        self.assertEqual(
            await self.scalar(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'audit_events' AND column_name = 'recorded_at'"
            ),
            "now()",
        )

    async def test_downgrade_removes_the_table_and_the_functions(self):
        await asyncio.to_thread(migrate, "downgrade", "0025-1")
        self.assertIsNone(await self.scalar("SELECT to_regclass('audit_events')"))
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM pg_proc WHERE proname IN "
                "('paw_reject_audit_events_change', "
                "'paw_force_audit_events_recorded_at')"
            ),
            0,
        )
        # And it can be applied again.
        await asyncio.to_thread(migrate, "upgrade", "head")
        self.assertEqual(
            await self.scalar("SELECT to_regclass('audit_events')::text"),
            "audit_events",
        )

    async def test_the_model_and_the_migration_describe_the_same_table(self):
        def differences(sync_connection):
            def only_audit_events(obj, name, type_, reflected, compare_to):
                return type_ != "table" or name == "audit_events"

            context = MigrationContext.configure(
                sync_connection, opts={"include_object": only_audit_events}
            )
            return compare_metadata(context, Base.metadata)

        async with self.database.engine.connect() as connection:
            self.assertEqual(await connection.run_sync(differences), [])

    async def test_the_decision_column_only_accepts_allow_and_deny(self):
        error = await self.execute_rejected(
            "INSERT INTO audit_events (id, correlation_id, occurred_at, action, "
            "resource_kind, decision, reason) VALUES (gen_random_uuid(), "
            "gen_random_uuid(), now(), 'a', 'system', 'maybe', 'r')"
        )
        self.assertIsInstance(error.orig, psycopg.errors.CheckViolation)


class AppendOnlyTest(AuditPostgresTestCase):
    async def record_one(self) -> AuditEventRecord:
        decision = Decision.allow(
            Reason.GRANTED_BY_SYSTEM_ROLE, Capability.ADMIN_AUDIT_VIEW
        )
        event = build_event(
            decision,
            principal=principal(SystemRole.ADMIN, user_id=U1),
            resource=Resource.system(),
            client_request_id="req-1",
            occurred_at=NOW,
        )
        await PostgresAuditSink(self.database).record(event)
        (row,) = await self.rows()
        self.event = event
        return row

    async def test_the_sink_stores_every_field_of_the_event(self):
        row = await self.record_one()
        self.assertEqual(
            (row.id, row.correlation_id, row.occurred_at),
            (self.event.event_id, self.event.correlation_id, NOW),
        )
        self.assertEqual(
            (row.actor_id, row.actor_role, row.agent_id), (U1, "admin", None)
        )
        self.assertEqual(
            (row.action, row.resource_kind, row.resource_id, row.project_id),
            ("admin.audit.view", "system", None, None),
        )
        self.assertEqual(
            (row.decision, row.reason, row.client_request_id),
            ("allow", "granted_by_system_role", "req-1"),
        )

    async def test_a_repository_decision_row_keeps_the_repo_and_the_acl_kind(self):
        contributor = principal(
            SystemRole.USER, user_id=U1, projects={P1: ProjectRole.CONTRIBUTOR}
        )
        authorizer = Authorizer(PostgresAuditSink(self.database))
        write = Capability.PROJECT_REPO_WRITE
        await authorizer.authorize(contributor, write, repo_resource())
        await authorizer.authorize(
            contributor, write, repo_resource({RepoPermission.READ})
        )
        inherit, override = await self.rows()
        self.assertEqual(
            (inherit.repo_id, inherit.repo_acl, inherit.decision),
            (REPO, "inherit", "allow"),
        )
        self.assertEqual(
            (override.repo_id, override.repo_acl, override.reason),
            (REPO, "override", "repo_acl_forbids"),
        )

    async def test_recorded_at_is_the_database_clock_not_the_applications(self):
        before = datetime.now(UTC) - timedelta(seconds=5)
        row = await self.record_one()  # occurred_at was claimed to be in 2001
        self.assertEqual(row.occurred_at, NOW)
        self.assertGreater(row.recorded_at, before)
        self.assertLess(row.recorded_at, datetime.now(UTC) + timedelta(seconds=5))

    async def test_update_is_rejected_and_the_row_is_unchanged(self):
        await self.record_one()
        error = await self.execute_rejected(
            "UPDATE audit_events SET decision = 'deny', reason = 'edited'"
        )
        self.assertIsInstance(error.orig, psycopg.errors.RestrictViolation)
        self.assertIn("append-only", str(error.orig))
        (row,) = await self.rows()
        self.assertEqual(
            (row.decision, row.reason), ("allow", "granted_by_system_role")
        )

    async def test_delete_is_rejected_and_the_row_is_still_there(self):
        await self.record_one()
        error = await self.execute_rejected("DELETE FROM audit_events")
        self.assertIsInstance(error.orig, psycopg.errors.RestrictViolation)
        self.assertEqual(len(await self.rows()), 1)

    async def test_truncate_is_rejected_and_the_row_is_still_there(self):
        await self.record_one()
        error = await self.execute_rejected("TRUNCATE audit_events")
        self.assertIsInstance(error.orig, psycopg.errors.RestrictViolation)
        self.assertEqual(len(await self.rows()), 1)

    async def test_the_guard_also_holds_when_triggers_are_meant_to_be_skipped(self):
        await self.record_one()
        async with self.database.session() as session:
            try:
                await session.execute(text("SET session_replication_role = replica"))
            except DBAPIError as error:
                if isinstance(error.orig, psycopg.errors.InsufficientPrivilege):
                    self.skipTest("the test role is not a superuser")
                raise
            with self.assertRaises(DBAPIError) as caught:
                await session.execute(text("DELETE FROM audit_events"))
        self.assertIsInstance(caught.exception.orig, psycopg.errors.RestrictViolation)
        self.assertEqual(len(await self.rows()), 1)

    async def test_appending_still_works_after_a_rejected_change(self):
        await self.record_one()
        await self.execute_rejected("DELETE FROM audit_events")
        await PostgresAuditSink(self.database).record(
            build_event(
                Decision.deny(Reason.CAPABILITY_NOT_GRANTED, Capability.CHAT_USE),
                principal=principal(),
                resource=Resource.system(),
                occurred_at=NOW,
            )
        )
        self.assertEqual([row.decision for row in await self.rows()], ["allow", "deny"])


class RoleSplitTest(AuditPostgresTestCase):
    """The application role can append and read, and cannot undo the guard."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Registered after the base cleanups, so it runs before the downgrade.
        self.addAsyncCleanup(self.drop_roles)
        await self.drop_roles()
        for role in (APP_ROLE, OTHER_ROLE):
            await self.execute_committed(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                f"PASSWORD '{ROLE_PASSWORD}'"
            )
        # Re-run the audit migration so that it grants to the application role.
        await asyncio.to_thread(migrate, "downgrade", "base")
        await asyncio.to_thread(
            migrate, "upgrade", "head", PAW_APP_DATABASE_ROLE=APP_ROLE
        )
        self.app_db = Database(make_settings(database_url=url_for_role(APP_ROLE)))
        self.other_db = Database(make_settings(database_url=url_for_role(OTHER_ROLE)))
        self.addAsyncCleanup(self.app_db.dispose)
        self.addAsyncCleanup(self.other_db.dispose)

    async def execute_committed(self, sql: str) -> None:
        async with self.database.session() as session:
            await session.execute(text(sql))
            await session.commit()

    async def drop_roles(self) -> None:
        for role in (APP_ROLE, OTHER_ROLE):
            exists = await self.scalar(
                f"SELECT count(*) FROM pg_roles WHERE rolname = '{role}'"
            )
            if exists:
                await self.execute_committed(f"DROP OWNED BY {role}")
                await self.execute_committed(f"DROP ROLE {role}")

    async def test_the_application_role_can_append_and_read(self):
        event = build_event(
            Decision.allow(Reason.GRANTED_BY_SYSTEM_ROLE, Capability.ADMIN_AUDIT_VIEW),
            principal=principal(SystemRole.ADMIN, user_id=U1),
            resource=Resource.system(),
            occurred_at=NOW,
        )
        await PostgresAuditSink(self.app_db).record(event)
        (row,) = await self.rows(self.app_db)
        self.assertEqual(row.id, event.event_id)

    async def test_the_application_role_cannot_rewrite_the_trail(self):
        await PostgresAuditSink(self.app_db).record(
            build_event(
                Decision.deny(Reason.CAPABILITY_NOT_GRANTED, Capability.CHAT_USE),
                principal=principal(),
                resource=Resource.system(),
                occurred_at=NOW,
            )
        )
        for sql in (
            "UPDATE audit_events SET decision = 'allow'",
            "DELETE FROM audit_events",
            "TRUNCATE audit_events",
            "ALTER TABLE audit_events DISABLE TRIGGER "
            "tr_audit_events_reject_update_delete",
            "ALTER TABLE audit_events DISABLE TRIGGER ALL",
            "DROP TRIGGER tr_audit_events_reject_truncate ON audit_events",
            "ALTER TABLE audit_events ALTER COLUMN reason TYPE varchar(3)",
            "ALTER TABLE audit_events ADD COLUMN note text",
            "ALTER TABLE audit_events DROP COLUMN reason",
            "ALTER TABLE audit_events RENAME TO not_audit",
            "CREATE RULE hide AS ON INSERT TO audit_events DO INSTEAD NOTHING",
            "DROP TABLE audit_events",
            f"ALTER TABLE audit_events OWNER TO {APP_ROLE}",
        ):
            with self.subTest(sql=sql):
                error = await self.execute_rejected(sql, self.app_db)
                self.assertIsInstance(error.orig, psycopg.errors.InsufficientPrivilege)
        # It cannot hand itself more privileges either (PostgreSQL only warns
        # and grants nothing when the grantor has no grant option) ...
        async with self.app_db.session() as session:
            await session.execute(
                text(f"GRANT UPDATE, DELETE, TRUNCATE ON audit_events TO {APP_ROLE}")
            )
            await session.commit()
        access = await read_audit_table_access(self.app_db)
        self.assertTrue(access.protected)
        # ... and nothing changed: still one row, still 'deny', triggers intact.
        (row,) = await self.rows()
        self.assertEqual(row.decision, "deny")
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgrelid = 'audit_events'::regclass AND NOT tgisinternal "
                "AND tgenabled = 'A'"
            ),
            3,
        )

    async def test_even_an_insert_capable_role_cannot_choose_recorded_at(self):
        # The trigger overrides the value, so the database clock is the only one.
        before = datetime.now(UTC) - timedelta(seconds=5)
        async with self.app_db.session() as session:
            await session.execute(
                text(
                    "INSERT INTO audit_events (id, correlation_id, occurred_at, "
                    "recorded_at, action, resource_kind, decision, reason) VALUES "
                    "(gen_random_uuid(), gen_random_uuid(), '2000-01-01', "
                    "'2000-01-01', 'a', 'system', 'deny', 'r')"
                )
            )
            await session.commit()
        (row,) = await self.rows()
        self.assertEqual(row.occurred_at, datetime(2000, 1, 1, tzinfo=UTC))
        self.assertGreater(row.recorded_at, before)  # not 2000-01-01

    async def test_public_has_no_access_to_the_table(self):
        # OTHER_ROLE exists but was not granted anything.
        error = await self.execute_rejected(
            "SELECT count(*) FROM audit_events", self.other_db
        )
        self.assertIsInstance(error.orig, psycopg.errors.InsufficientPrivilege)
        insert = await self.execute_rejected(
            "INSERT INTO audit_events (id, correlation_id, occurred_at, action, "
            "resource_kind, decision, reason) VALUES (gen_random_uuid(), "
            "gen_random_uuid(), now(), 'a', 'system', 'deny', 'r')",
            self.other_db,
        )
        self.assertIsInstance(insert.orig, psycopg.errors.InsufficientPrivilege)

    async def test_the_diagnostic_is_silent_for_the_restricted_role(self):
        access = await read_audit_table_access(self.app_db)
        self.assertIsNotNone(access)
        self.assertTrue(access.protected)
        self.assertEqual(
            (access.owns, access.can_update, access.can_delete, access.can_truncate),
            (False, False, False, False),
        )
        with self.assertNoLogs("paw_backend.authz.diagnostics", level="WARNING"):
            await warn_if_audit_table_is_mutable(self.app_db, 3)

    async def test_the_diagnostic_warns_when_the_user_owns_the_table(self):
        access = await read_audit_table_access(self.database)
        self.assertFalse(access.protected)
        self.assertTrue(access.owns)
        with self.assertLogs("paw_backend.authz.diagnostics", level="WARNING") as logs:
            await warn_if_audit_table_is_mutable(self.database, 3)
        (line,) = logs.output
        self.assertIn("owner=True", line)
        self.assertIn("PAW_APP_DATABASE_ROLE", line)
        # No role name, password or connection detail in the message.
        for secret in (APP_ROLE, ROLE_PASSWORD, "postgresql://"):
            self.assertNotIn(secret, line)

    async def test_the_diagnostic_warns_for_a_role_that_may_update(self):
        await self.execute_committed(f"GRANT UPDATE ON audit_events TO {APP_ROLE}")
        access = await read_audit_table_access(self.app_db)
        self.assertEqual(
            (access.owns, access.can_update, access.can_delete, access.can_truncate),
            (False, True, False, False),
        )
        with self.assertLogs("paw_backend.authz.diagnostics", level="WARNING"):
            await warn_if_audit_table_is_mutable(self.app_db, 3)

    async def test_the_diagnostic_warns_when_the_role_cannot_insert(self):
        # OTHER_ROLE exists but was granted nothing: every audited action would 503.
        access = await read_audit_table_access(self.other_db)
        self.assertTrue(access.cannot_write)
        with self.assertLogs("paw_backend.authz.diagnostics", level="WARNING") as logs:
            await warn_if_audit_table_is_mutable(self.other_db, 3)
        (line,) = logs.output
        self.assertIn("cannot INSERT into audit_events", line)
        self.assertIn("PAW_APP_DATABASE_ROLE", line)
        for secret in (OTHER_ROLE, ROLE_PASSWORD):
            self.assertNotIn(secret, line)

    async def test_the_diagnostic_never_raises_when_the_database_is_unreachable(self):
        unreachable = Database(
            make_settings(
                database_url="postgresql://x:y@127.0.0.1:1/nothing",
                database_timeout_seconds=1,
            )
        )
        self.addAsyncCleanup(unreachable.dispose)
        with self.assertNoLogs("paw_backend.authz.diagnostics", level="WARNING"):
            await warn_if_audit_table_is_mutable(unreachable, 2)


class MigrationUrlTest(AuditPostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.addAsyncCleanup(self.drop_role)
        await self.drop_role()
        async with self.database.session() as session:
            await session.execute(
                text(
                    f"CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB "
                    f"NOCREATEROLE PASSWORD '{ROLE_PASSWORD}'"
                )
            )
            await session.commit()
        await asyncio.to_thread(migrate, "downgrade", "base")

    async def drop_role(self) -> None:
        exists = await self.scalar(
            f"SELECT count(*) FROM pg_roles WHERE rolname = '{APP_ROLE}'"
        )
        if exists:
            for sql in (f"DROP OWNED BY {APP_ROLE}", f"DROP ROLE {APP_ROLE}"):
                async with self.database.session() as session:
                    await session.execute(text(sql))
                    await session.commit()

    async def test_the_application_role_cannot_run_the_migrations(self):
        with self.assertRaises(Exception) as caught:
            await asyncio.to_thread(
                migrate, "upgrade", "head", PAW_DATABASE_URL=url_for_role(APP_ROLE)
            )
        self.assertIn("permission denied", str(caught.exception))

    async def test_a_missing_app_role_fails_the_migration_loudly(self):
        with self.assertRaises(RuntimeError) as caught:
            await asyncio.to_thread(
                migrate,
                "upgrade",
                "head",
                PAW_APP_DATABASE_ROLE="paw_authz_missing_025",
            )
        self.assertIn("does not exist", str(caught.exception))
        # ... and the migration did not half-apply.
        self.assertIsNone(await self.scalar("SELECT to_regclass('audit_events')"))

    async def test_public_can_not_be_named_as_the_app_role(self):
        # A quoted "public" is the pseudo-role PUBLIC: it would grant everyone.
        for name in ("public", "PUBLIC", "pg_monitor", "postgres"):
            with self.subTest(role=name):
                with self.assertRaises(ValidationError):
                    await asyncio.to_thread(
                        migrate, "upgrade", "head", PAW_APP_DATABASE_ROLE=name
                    )
                self.assertIsNone(
                    await self.scalar("SELECT to_regclass('audit_events')")
                )

    async def test_migration_role_without_app_role_leaves_the_app_unable_to_write(self):
        with (
            self.assertLogs("paw_backend.migrations.0025", level="WARNING"),
            self.assertLogs("paw_backend.db_roles", level="WARNING"),
        ):
            await asyncio.to_thread(
                migrate,
                "upgrade",
                "head",
                PAW_DATABASE_URL=url_for_role(APP_ROLE),
                PAW_MIGRATION_DATABASE_URL=TEST_DATABASE_URL,
            )
        app_db = Database(make_settings(database_url=url_for_role(APP_ROLE)))
        self.addAsyncCleanup(app_db.dispose)
        with self.assertLogs("paw_backend.authz.diagnostics", level="WARNING") as logs:
            await warn_if_audit_table_is_mutable(app_db, 3)
        self.assertIn("cannot INSERT into audit_events", "\n".join(logs.output))
        # ... and an action that must be audited is refused, not silently run.
        with self.assertLogs("paw_backend.authz.authorizer", level="ERROR"):
            decision = await Authorizer(PostgresAuditSink(app_db)).authorize(
                principal(SystemRole.OWNER),
                Capability.ADMIN_CONFIG_MANAGE,
                Resource.system(),
            )
        self.assertEqual(decision.reason, Reason.AUDIT_UNAVAILABLE)

    async def test_migrations_run_as_the_migration_role_when_one_is_configured(self):
        await asyncio.to_thread(
            migrate,
            "upgrade",
            "head",
            PAW_DATABASE_URL=url_for_role(APP_ROLE),  # cannot create tables
            PAW_MIGRATION_DATABASE_URL=TEST_DATABASE_URL,  # the schema owner
            PAW_APP_DATABASE_ROLE=APP_ROLE,
        )
        owner = await self.scalar(
            "SELECT pg_get_userbyid(relowner) FROM pg_class "
            "WHERE oid = 'audit_events'::regclass"
        )
        self.assertEqual(owner, await self.scalar("SELECT current_user"))
        self.assertNotEqual(owner, APP_ROLE)


class AuthorizerOnPostgresTest(AuditPostgresTestCase):
    async def test_every_persisted_decision_becomes_a_row(self):
        ticks = iter(NOW + timedelta(seconds=n) for n in range(100))
        contributor = principal(
            SystemRole.USER, user_id=U1, projects={P1: ProjectRole.CONTRIBUTOR}
        )
        authorizer = Authorizer(
            PostgresAuditSink(self.database),
            directory=StaticDirectory(contributor),
            clock=lambda: next(ticks),
        )
        grant = AgentGrant(
            AGENT, frozenset({Capability.PROJECT_TASK_RUN}), ALL_PROJECTS
        )
        correlation = uuid.uuid4()
        await authorizer.authorize(
            contributor,
            Capability.PROJECT_REPO_WRITE,
            project(P1),
            correlation_id=correlation,
            client_request_id="r1",
        )
        await authorizer.authorize(
            contributor, Capability.ADMIN_USERS_MANAGE, Resource.system()
        )
        await authorizer.authorize_agent_action(
            U1, grant, Capability.PROJECT_REPO_WRITE, project(P1)
        )
        # Not persisted: an allowed read, and an unauthenticated attempt.
        await authorizer.authorize(contributor, Capability.PROJECT_READ, project(P1))
        with self.assertLogs("paw_backend.authz.authorizer", level="INFO"):
            await authorizer.authorize(None, Capability.CHAT_USE, Resource.system())
        rows = await self.rows()  # ordered by occurred_at, one second apart
        self.assertEqual(
            [(r.actor_id, r.agent_id, r.action, r.decision, r.reason) for r in rows],
            [
                (U1, None, "project.repo.write", "allow", "granted_by_project_role"),
                (U1, None, "admin.users.manage", "deny", "capability_not_granted"),
                (
                    U1,
                    AGENT,
                    "project.repo.write",
                    "deny",
                    "agent_capability_not_granted",
                ),
            ],
        )
        self.assertEqual(
            (rows[0].correlation_id, rows[0].client_request_id), (correlation, "r1")
        )

    async def test_a_role_change_row_names_the_target_and_both_roles(self):
        authorizer = Authorizer(PostgresAuditSink(self.database))
        owner = principal(SystemRole.OWNER, user_id=U1)
        await authorizer.authorize_role_change(
            owner, U2, SystemRole.USER, SystemRole.ADMIN
        )
        await authorizer.authorize_ownership_transfer(owner, U2, SystemRole.ADMIN)
        promoted, transferred = await self.rows()
        self.assertEqual(
            (
                promoted.actor_id,
                promoted.action,
                promoted.resource_kind,
                promoted.resource_id,
            ),
            (U1, "owner.admins.manage", "user", U2),
        )
        self.assertEqual((promoted.old_role, promoted.new_role), ("user", "admin"))
        self.assertEqual(
            (transferred.action, transferred.resource_id, transferred.decision),
            ("owner.ownership.transfer", U2, "allow"),
        )
        self.assertEqual(
            (transferred.old_role, transferred.new_role), ("admin", "owner")
        )

    async def test_an_action_is_denied_when_the_audit_table_is_unusable(self):
        await asyncio.to_thread(migrate, "downgrade", "base")
        authorizer = Authorizer(PostgresAuditSink(self.database))
        with self.assertLogs("paw_backend.authz.authorizer", level="WARNING") as logs:
            required = await authorizer.authorize(
                principal(SystemRole.OWNER),
                Capability.ADMIN_CONFIG_MANAGE,
                Resource.system(),
            )
            side_effect = await authorizer.authorize(
                principal(SystemRole.USER, projects={P1: ProjectRole.CONTRIBUTOR}),
                Capability.PROJECT_REPO_WRITE,
                project(P1),
            )
        read = await authorizer.authorize(
            principal(SystemRole.USER, projects={P1: ProjectRole.VIEWER}),
            Capability.PROJECT_READ,
            project(P1),
        )
        self.assertEqual(required.reason, Reason.AUDIT_UNAVAILABLE)
        self.assertEqual(side_effect.reason, Reason.AUDIT_UNAVAILABLE)
        self.assertTrue(read.allowed)  # reads are not audited, so not blocked
        # The type of the driver's error (the write no longer goes through
        # SQLAlchemy), never its message.
        self.assertIn("UndefinedTable", "\n".join(logs.output))
        self.assertNotIn("audit_events", "\n".join(logs.output))

    async def test_startup_warns_when_the_application_owns_the_audit_table(self):
        settings = make_settings(database_url=TEST_DATABASE_URL)  # the schema owner

        def start_and_wait_for_the_check() -> list[str]:
            with self.assertLogs("paw_backend.authz.diagnostics", "WARNING") as logs:
                with make_client(create_app(settings)):
                    deadline = time.monotonic() + 5
                    while not logs.records and time.monotonic() < deadline:
                        time.sleep(0.05)
            return logs.output

        output = await asyncio.to_thread(start_and_wait_for_the_check)
        # The check of the tool approval tables (PAW-031) warns as well; this
        # test is about the audit trail.
        (line,) = [entry for entry in output if "audit_events" in entry]
        self.assertIn("owner=True", line)
        self.assertIn("append-only guard", line)

    async def test_http_decisions_are_persisted_but_unauthenticated_ones_are_not(self):
        settings = make_settings(database_url=TEST_DATABASE_URL)
        # The startup diagnostic (tested above) would warn about the test user.
        self.enterContext(
            patch.object(
                logging.getLogger("paw_backend.authz.diagnostics"), "disabled", True
            )
        )

        def requests():
            app = create_app(settings)
            add_test_routes(app)
            statuses = []
            with make_client(app) as client:
                headers = {"X-Request-ID": "req-a"}
                with self.assertLogs("paw_backend.authz.authorizer", level="INFO"):
                    for _ in range(20):
                        statuses.append(
                            client.get("/test/admin", headers=headers).status_code
                        )
                app.state.principal_provider = StaticProvider(
                    principal(SystemRole.USER, user_id=U1)
                )
                headers = {"X-Request-ID": "req-b"}
                statuses.append(client.get("/test/admin", headers=headers).status_code)
                app.state.principal_provider = StaticProvider(
                    principal(SystemRole.OWNER, user_id=U1)
                )
                headers = {"X-Request-ID": "req-c"}
                statuses.append(client.get("/test/admin", headers=headers).status_code)
            return statuses

        statuses = await asyncio.to_thread(requests)
        self.assertEqual(statuses, [401] * 20 + [403, 200])
        rows = await self.rows()
        # 20 anonymous requests left no rows; only the two authenticated ones did.
        self.assertEqual(
            sorted(
                (r.client_request_id, r.actor_id, r.decision, r.reason) for r in rows
            ),
            [
                ("req-b", U1, "deny", "capability_not_granted"),
                ("req-c", U1, "allow", "granted_by_system_role"),
            ],
        )
        self.assertEqual(len({r.correlation_id for r in rows}), 2)


if __name__ == "__main__":
    unittest.main()
