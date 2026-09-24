"""Audit persistence and its append-only guarantee on a real PostgreSQL.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set (see
``test_postgres_integration.py``). Each test starts from a freshly migrated
database and returns it to ``base`` afterwards.
"""

import asyncio
import io
import os
import unittest
from datetime import UTC, datetime, timedelta

import psycopg.errors
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from paw_backend.app import create_app
from paw_backend.authz import (
    AgentGrant,
    Authorizer,
    Capability,
    Decision,
    PostgresAuditSink,
    ProjectRole,
    Reason,
    Resource,
    SystemRole,
)
from paw_backend.authz.audit import build_event
from paw_backend.authz.models import AuditEventRecord
from paw_backend.db import Base, Database

from .authz_support import StaticProvider, add_test_routes, principal
from .support import make_client, make_settings, paw_environment
from .test_migrations import offline_config

TEST_DATABASE_URL = os.environ.get("PAW_TEST_DATABASE_URL")
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def migrate(action: str, revision: str) -> None:
    """Run Alembic against the test database (``env.py`` reads the URL)."""
    with paw_environment(PAW_DATABASE_URL=TEST_DATABASE_URL):
        config = offline_config(io.StringIO())
        getattr(command, action)(config, revision)


@unittest.skipUnless(TEST_DATABASE_URL, "PAW_TEST_DATABASE_URL is not set")
class AuditPostgresTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await asyncio.to_thread(migrate, "upgrade", "head")
        self.addAsyncCleanup(asyncio.to_thread, migrate, "downgrade", "base")
        self.database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(self.database.dispose)

    async def scalar(self, sql: str):
        async with self.database.session() as session:
            return (await session.execute(text(sql))).scalar()

    async def rows(self) -> list[AuditEventRecord]:
        async with self.database.session() as session:
            query = select(AuditEventRecord).order_by(AuditEventRecord.occurred_at)
            return list((await session.execute(query)).scalars())

    async def execute_rejected(self, sql: str) -> DBAPIError:
        async with self.database.session() as session:
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
        self.assertEqual(constraints, "ck_audit_events_decision_valid,pk_audit_events")
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
            "tr_audit_events_reject_truncate:A,tr_audit_events_reject_update_delete:A",
        )

    async def test_downgrade_removes_the_table_and_the_function(self):
        await asyncio.to_thread(migrate, "downgrade", "0025-1")
        self.assertIsNone(await self.scalar("SELECT to_regclass('audit_events')"))
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM pg_proc "
                "WHERE proname = 'paw_reject_audit_events_change'"
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
            "INSERT INTO audit_events (id, occurred_at, action, resource_kind, "
            "decision, reason) VALUES (gen_random_uuid(), now(), 'a', 'system', "
            "'maybe', 'r')"
        )
        self.assertIsInstance(error.orig, psycopg.errors.CheckViolation)


class AppendOnlyTest(AuditPostgresTestCase):
    async def record_one(self) -> AuditEventRecord:
        decision = Decision.allow(
            Reason.GRANTED_BY_SYSTEM_ROLE, Capability.ADMIN_AUDIT_VIEW
        )
        event = build_event(
            decision,
            principal=principal(SystemRole.ADMIN, user_id="admin-1"),
            resource=Resource.system(),
            request_id="req-1",
            occurred_at=NOW,
        )
        await PostgresAuditSink(self.database).record(event)
        (row,) = await self.rows()
        self.event = event
        return row

    async def test_the_sink_stores_every_field_of_the_event(self):
        row = await self.record_one()
        self.assertEqual(
            (row.id, row.occurred_at, row.actor_id, row.actor_role, row.agent_id),
            (self.event.event_id, NOW, "admin-1", "admin", None),
        )
        self.assertEqual(
            (row.action, row.resource_kind, row.resource_id, row.project_id),
            ("admin.audit.view", "system", None, None),
        )
        self.assertEqual(
            (row.decision, row.reason, row.request_id),
            ("allow", "granted_by_system_role", "req-1"),
        )

    async def test_update_is_rejected_and_the_row_is_unchanged(self):
        await self.record_one()
        error = await self.execute_rejected(
            "UPDATE audit_events SET decision = 'deny', actor_id = 'someone-else'"
        )
        self.assertIsInstance(error.orig, psycopg.errors.RestrictViolation)
        self.assertIn("append-only", str(error.orig))
        (row,) = await self.rows()
        self.assertEqual((row.decision, row.actor_id), ("allow", "admin-1"))

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
                Decision.deny(Reason.UNAUTHENTICATED, Capability.CHAT_USE),
                principal=None,
                resource=Resource.system(),
                occurred_at=NOW,
            )
        )
        self.assertEqual([row.decision for row in await self.rows()], ["allow", "deny"])


class AuthorizerOnPostgresTest(AuditPostgresTestCase):
    async def test_every_decision_becomes_a_row(self):
        ticks = iter(NOW + timedelta(seconds=n) for n in range(100))
        authorizer = Authorizer(
            PostgresAuditSink(self.database), clock=lambda: next(ticks)
        )
        user = principal(SystemRole.USER, user_id="u1", p1=ProjectRole.CONTRIBUTOR)
        grant = AgentGrant("agent-7", frozenset({Capability.PROJECT_TASK_RUN}))
        await authorizer.authorize(
            user, Capability.PROJECT_READ, Resource.project("p1"), request_id="r1"
        )
        await authorizer.authorize(
            user, Capability.ADMIN_USERS_MANAGE, Resource.system()
        )
        await authorizer.authorize(None, Capability.CHAT_USE, Resource.system())
        await authorizer.authorize_agent_action(
            user, grant, Capability.PROJECT_REPO_WRITE, Resource.project("p1")
        )
        rows = await self.rows()  # ordered by occurred_at, one second apart
        self.assertEqual(
            [(r.actor_id, r.agent_id, r.action, r.decision, r.reason) for r in rows],
            [
                ("u1", None, "project.read", "allow", "granted_by_project_role"),
                ("u1", None, "admin.users.manage", "deny", "capability_not_granted"),
                (None, None, "chat.use", "deny", "unauthenticated"),
                (
                    "u1",
                    "agent-7",
                    "project.repo.write",
                    "deny",
                    "agent_capability_not_granted",
                ),
            ],
        )
        self.assertEqual(rows[0].request_id, "r1")

    async def test_a_privileged_action_is_denied_when_the_audit_table_is_unusable(self):
        await asyncio.to_thread(migrate, "downgrade", "base")
        authorizer = Authorizer(PostgresAuditSink(self.database))
        with self.assertLogs("paw_backend.authz.authorizer", level="WARNING") as logs:
            privileged = await authorizer.authorize(
                principal(SystemRole.OWNER),
                Capability.ADMIN_CONFIG_MANAGE,
                Resource.system(),
            )
            ordinary = await authorizer.authorize(
                principal(SystemRole.USER, user_id="u1"),
                Capability.CHAT_USE,
                Resource.owned_by("u1", "chat"),
            )
        self.assertEqual(privileged.reason, Reason.AUDIT_UNAVAILABLE)
        self.assertTrue(ordinary.allowed)
        self.assertIn("ProgrammingError", "\n".join(logs.output))

    async def test_http_decisions_are_persisted_with_the_request_id(self):
        settings = make_settings(database_url=TEST_DATABASE_URL)

        def requests():
            app = create_app(settings)
            add_test_routes(app)
            statuses = []
            with make_client(app) as client:
                statuses.append(
                    client.get(
                        "/test/admin", headers={"X-Request-ID": "req-a"}
                    ).status_code
                )
                app.state.principal_provider = StaticProvider(
                    principal(SystemRole.USER, user_id="u1")
                )
                statuses.append(
                    client.get(
                        "/test/admin", headers={"X-Request-ID": "req-b"}
                    ).status_code
                )
                statuses.append(
                    client.get(
                        "/test/chat", headers={"X-Request-ID": "req-c"}
                    ).status_code
                )
            return statuses

        self.assertEqual(await asyncio.to_thread(requests), [401, 403, 200])
        rows = await self.rows()
        self.assertEqual(
            sorted((r.request_id, r.actor_id, r.decision, r.reason) for r in rows),
            [
                ("req-a", None, "deny", "unauthenticated"),
                ("req-b", "u1", "deny", "capability_not_granted"),
                ("req-c", "u1", "allow", "granted_by_system_role"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
