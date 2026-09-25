"""The approval tables under the application's own, restricted PostgreSQL role.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set (see test_postgres_integration).
``test_tools_postgres`` runs as the table owner (a superuser) and shows what the
triggers and CHECK constraints refuse; these tests connect as a **non-superuser**
role that the migration granted least privilege to (``PAW_APP_DATABASE_ROLE``),
and show what that role can and cannot do. The roles are created here with
names unique to the run and dropped again afterwards; the test user must be
allowed to create roles.
"""

import asyncio
import hashlib
import io
import unittest
import uuid
from datetime import timedelta

import psycopg.errors
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from paw_backend.authz.diagnostics import (
    read_tool_table_access,
    warn_about_loose_privileges,
    warn_if_tool_approval_tables_are_mutable,
)
from paw_backend.db import Database
from paw_backend.tasks import Actor, TaskCommand, TaskService
from paw_backend.tools import (
    ApprovalLevel,
    ApprovalStatus,
    ConsumeOutcome,
    DecideOutcome,
    OpenLimits,
    OpenOutcome,
    PostgresApprovalStore,
    PostgresTaskActivity,
    RevokeOutcome,
    TaskActivity,
    TaskRun,
)

from .support import make_settings, paw_environment
from .task_support import TEST_DATABASE_URL, requires_postgres
from .test_migrations import offline_config
from .tools_store_contract import LIMITS, binding_of, new_approval
from .tools_support import AGENT, NOW, RUN, U1, U2

GUARDS = (
    "tool_approvals_created_pending",
    "tool_approvals_state_machine",
    "tool_approvals_no_delete",
    "tool_approvals_no_truncate",
    "tool_approval_events_append_only",
    "tool_approval_events_no_truncate",
)
IDENTITY_COLUMNS = {
    "id": "gen_random_uuid()",
    "task_id": "gen_random_uuid()",
    "task_attempt": "2",
    "task_retry_count": "1",
    "project_id": "gen_random_uuid()",
    "agent_id": "gen_random_uuid()",
    "requester_user_id": "gen_random_uuid()",
    "tool": "'host.install_package'",
    "level": "'strong_approval'",
    "call_hash": f"'{'0' * 64}'",
    "targets": "'[]'::jsonb",
    "summary": "'[{}]'::jsonb",
    "created_at": "now() - interval '1 day'",
    "expires_at": "now() + interval '365 days'",
}


def migrate(action: str, revision: str, **environment: str) -> None:
    variables = {"PAW_DATABASE_URL": TEST_DATABASE_URL, **environment}
    with paw_environment(**variables):
        getattr(command, action)(offline_config(io.StringIO()), revision)


@requires_postgres
class RoleTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        suffix = uuid.uuid4().hex[:12]
        self.role = f"paw_tool_app_{suffix}"
        self.other_role = f"paw_tool_other_{suffix}"
        self.password = f"dummy-test-password-{suffix}"
        self.owner_db = Database(make_settings(database_url=TEST_DATABASE_URL))
        # Cleanups run last-in first-out: connections close, the roles go, then
        # the schema (a fresh migration follows in every test).
        self.addAsyncCleanup(asyncio.to_thread, migrate, "downgrade", "base")
        self.addAsyncCleanup(self.drop_roles)
        self.addAsyncCleanup(self.owner_db.dispose)
        await self.drop_roles()
        for role in (self.role, self.other_role):
            await self.owner_execute(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                f"PASSWORD '{self.password}'"
            )
        await asyncio.to_thread(migrate, "downgrade", "base")
        await asyncio.to_thread(
            migrate, "upgrade", "head", PAW_APP_DATABASE_ROLE=self.role
        )
        self.app_db = self.database_for(self.role)
        self.other_db = self.database_for(self.other_role)
        self.store = PostgresApprovalStore(self.app_db)

    def database_for(self, role: str) -> Database:
        url = make_url(TEST_DATABASE_URL).set(username=role, password=self.password)
        database = Database(
            make_settings(database_url=url.render_as_string(hide_password=False))
        )
        self.addAsyncCleanup(database.dispose)
        return database

    async def owner_execute(self, sql: str) -> None:
        async with self.owner_db.session() as session:
            await session.execute(text(sql))
            await session.commit()

    async def drop_roles(self) -> None:
        async with self.owner_db.session() as session:
            for role in (self.role, self.other_role):
                exists = (
                    await session.execute(
                        text("SELECT count(*) FROM pg_roles WHERE rolname = :r"),
                        {"r": role},
                    )
                ).scalar()
                if exists:
                    await session.execute(text(f"DROP OWNED BY {role}"))
                    await session.execute(text(f"DROP ROLE {role}"))
            await session.commit()

    async def attempt(self, database: Database, sql: str, **parameters):
        """Run one statement as that role; return the error it must raise."""
        async with database.session() as session:
            with self.assertRaises(DBAPIError) as caught:
                await session.execute(text(sql), parameters)
                await session.commit()
        return caught.exception.orig

    async def scalar(self, sql: str, database: Database | None = None, **params):
        async with (database or self.owner_db).session() as session:
            return (await session.execute(text(sql), params)).scalar()

    async def approved(self, **overrides):
        new = new_approval(**overrides)
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        await self.store.decide(
            new.approval_id,
            approver_id=U1,
            approve=True,
            now=NOW,
            step_up_verified=True,
        )
        return new


class ApplicationRoleTest(RoleTestCase):
    async def test_the_role_is_not_a_superuser_and_owns_nothing(self):
        privileged = await self.scalar(
            "SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls "
            "FROM pg_roles WHERE rolname = :r",
            r=self.role,
        )
        self.assertFalse(privileged)
        owners = await self.scalar(
            "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
            "WHERE r.rolname = :r",
            r=self.role,
        )
        self.assertEqual(owners, 0)

    async def test_the_role_can_run_the_whole_approval_flow(self):
        store = self.store
        new = new_approval(level=ApprovalLevel.STRONG_APPROVAL)
        opened = await store.open_request(new, now=NOW, limits=LIMITS)
        self.assertEqual(opened.outcome, OpenOutcome.CREATED)
        refused = await store.decide(
            new.approval_id, approver_id=U1, approve=True, now=NOW
        )
        self.assertEqual(refused.outcome, DecideOutcome.STEP_UP_REQUIRED)
        granted = await store.decide(
            new.approval_id,
            approver_id=U1,
            approve=True,
            now=NOW,
            step_up_verified=True,
        )
        self.assertTrue(granted.record.step_up_verified)
        self.assertEqual(
            await store.consume(new.approval_id, binding_of(new), now=NOW),
            ConsumeOutcome.CONSUMED,
        )
        other = new_approval()
        await store.open_request(other, now=NOW, limits=LIMITS)
        self.assertEqual(
            await store.revoke(other.approval_id, actor_id=U1, now=NOW),
            RevokeOutcome.REVOKED,
        )
        task_id = uuid.uuid4()
        for _ in range(2):
            await store.open_request(
                new_approval(task_id=task_id), now=NOW, limits=LIMITS
            )
        self.assertEqual(len(await store.revoke_task(task_id, now=NOW)), 2)
        history = await store.history(new.approval_id)
        self.assertEqual(
            [h.kind.value for h in history], ["requested", "approved", "consumed"]
        )
        self.assertEqual(history[0].summary, new.summary)
        record = await store.get(new.approval_id)
        self.assertEqual(record.status, ApprovalStatus.CONSUMED)

    async def test_the_role_can_read_the_state_of_a_task(self):
        # The broker asks whether a task can still act before it opens or uses
        # an approval; the role that runs it must be able to read `tasks`.
        tasks = TaskService(self.app_db)
        activity = PostgresTaskActivity(self.app_db)
        created = await tasks.create_task(project_id=U1, created_by=U1, title="t")
        self.assertEqual(
            await activity.check(created.task_id, RUN), TaskActivity.ACTIVE
        )
        await tasks.execute(created.task_id, TaskCommand.CANCEL, actor=Actor.system())
        self.assertEqual(await activity.check(created.task_id, RUN), TaskActivity.ENDED)
        self.assertEqual(await activity.check(uuid.uuid4(), RUN), TaskActivity.UNKNOWN)

    async def test_the_role_can_lock_the_task_row_when_it_uses_an_approval(self):
        # Using an approval reads the task row locked (FOR SHARE), in the same
        # transaction that consumes: the lock needs the UPDATE privilege on
        # `tasks` that the role has for the task lifecycle (PAW-032).
        tasks = TaskService(self.app_db)
        live = (
            await tasks.create_task(project_id=U1, created_by=U1, title="t")
        ).task_id
        ended = (
            await tasks.create_task(project_id=U1, created_by=U1, title="t")
        ).task_id
        await tasks.execute(ended, TaskCommand.CANCEL, actor=Actor.system())
        for task_id, expected in (
            (live, ConsumeOutcome.CONSUMED),
            (ended, ConsumeOutcome.TASK_NOT_ACTIVE),
            (uuid.uuid4(), ConsumeOutcome.TASK_UNKNOWN),
        ):
            with self.subTest(expected=expected.value):
                new = new_approval(task_id=task_id)
                await self.store.open_request(new, now=NOW, limits=LIMITS)
                await self.store.decide(
                    new.approval_id, approver_id=U1, approve=True, now=NOW
                )
                self.assertEqual(
                    await self.store.consume(
                        new.approval_id,
                        binding_of(new),
                        now=NOW,
                        require_active_task=True,
                    ),
                    expected,
                )

    async def test_the_role_can_lock_the_task_row_when_it_opens_a_request(self):
        # Opening a request reads the task row locked (FOR SHARE) in the
        # transaction that inserts: the same privileges as using an approval.
        tasks = TaskService(self.app_db)
        live = (
            await tasks.create_task(project_id=U1, created_by=U1, title="t")
        ).task_id
        ended = (
            await tasks.create_task(project_id=U1, created_by=U1, title="t")
        ).task_id
        await tasks.execute(ended, TaskCommand.CANCEL, actor=Actor.system())
        for task_id, expected in (
            (live, OpenOutcome.CREATED),
            (ended, OpenOutcome.TASK_NOT_ACTIVE),
            (uuid.uuid4(), OpenOutcome.TASK_UNKNOWN),
        ):
            with self.subTest(expected=expected.value):
                opened = await self.store.open_request(
                    new_approval(task_id=task_id),
                    now=NOW,
                    limits=LIMITS,
                    require_active_task=True,
                )
                self.assertEqual(opened.outcome, expected)

    async def test_the_role_can_use_and_open_across_a_restart(self):
        # Reading the run (SELECT on `tasks.attempt` / `retry_count`, under the
        # row lock) and revoking what an earlier run left open (UPDATE of the
        # state columns, an INSERT into the history) need no more than the role
        # has. A restart is the case: the task fails, is started again, and the
        # approval of the earlier attempt is still `approved`.
        tasks = TaskService(self.app_db)
        task_id = (
            await tasks.create_task(project_id=U1, created_by=U1, title="t")
        ).task_id
        old = new_approval(task_id=task_id, task_run=TaskRun(1, 0))
        await self.store.open_request(old, now=NOW, limits=LIMITS)
        await self.store.decide(old.approval_id, approver_id=U1, approve=True, now=NOW)
        await tasks.execute(task_id, TaskCommand.FAIL, actor=Actor.system())
        await tasks.execute(task_id, TaskCommand.RESTART, actor=Actor.system())
        flag = {"require_active_task": True}
        activity = PostgresTaskActivity(self.app_db)
        self.assertEqual(
            await activity.check(task_id, TaskRun(2, 0)), TaskActivity.ACTIVE
        )
        self.assertEqual(
            await activity.check(task_id, TaskRun(1, 0)), TaskActivity.SUPERSEDED
        )
        # the new run cannot use it; the old run is not the task's any more
        for run, outcome in (
            (TaskRun(2, 0), ConsumeOutcome.SUPERSEDED),
            (TaskRun(1, 0), ConsumeOutcome.TASK_SUPERSEDED),
        ):
            with self.subTest(run=run):
                self.assertEqual(
                    await self.store.consume(
                        old.approval_id,
                        binding_of(old, task_run=run),
                        now=NOW,
                        **flag,
                    ),
                    outcome,
                )
        # the new run asks: the earlier approval is revoked, the new one is open
        fresh = new_approval(task_id=task_id, task_run=TaskRun(2, 0))
        opened = await self.store.open_request(fresh, now=NOW, limits=LIMITS, **flag)
        self.assertEqual(opened.outcome, OpenOutcome.CREATED)
        self.assertEqual(
            (await self.store.get(old.approval_id)).status, ApprovalStatus.REVOKED
        )
        self.assertEqual(
            [h.kind.value for h in await self.store.history(old.approval_id)],
            ["requested", "approved", "revoked"],
        )

    async def test_concurrent_requests_hold_the_cap_for_the_role_too(self):
        limits = OpenLimits(max_pending=3, rejection_cooldown=timedelta(minutes=5))
        task_id = uuid.uuid4()
        opened = await asyncio.gather(
            *(
                self.store.open_request(
                    new_approval(task_id=task_id), now=NOW, limits=limits
                )
                for _ in range(12)
            )
        )
        self.assertEqual(
            sorted(o.outcome.value for o in opened),
            ["created"] * 3 + ["too_many_pending"] * 9,
        )

    async def test_the_role_cannot_rewrite_what_was_granted(self):
        new = await self.approved()
        for column, value in IDENTITY_COLUMNS.items():
            with self.subTest(column=column):
                error = await self.attempt(
                    self.app_db,
                    f"UPDATE tool_approvals SET {column} = {value} WHERE id = :id",
                    id=new.approval_id,
                )
                self.assertIsInstance(error, psycopg.errors.InsufficientPrivilege)
        record = await self.store.get(new.approval_id)
        self.assertEqual(
            (record.call_hash, record.tool, record.level, record.expires_at),
            (new.call_hash, new.tool, new.level, new.expires_at),
        )

    async def test_a_forbidden_state_change_is_refused_for_the_role(self):
        used = await self.approved()
        await self.store.consume(used.approval_id, binding_of(used), now=NOW)
        rejected = new_approval()
        await self.store.open_request(rejected, now=NOW, limits=LIMITS)
        await self.store.decide(
            rejected.approval_id, approver_id=U1, approve=False, now=NOW
        )
        pending = new_approval()
        await self.store.open_request(pending, now=NOW, limits=LIMITS)
        cases = {
            "replay a used approval": (
                "UPDATE tool_approvals SET status = 'approved' WHERE id = :id",
                used.approval_id,
            ),
            "reopen a used approval": (
                "UPDATE tool_approvals SET status = 'pending', approver_id = NULL,"
                " decided_at = NULL WHERE id = :id",
                used.approval_id,
            ),
            "approve a rejected one": (
                "UPDATE tool_approvals SET status = 'approved' WHERE id = :id",
                rejected.approval_id,
            ),
            "consume without a decision": (
                "UPDATE tool_approvals SET status = 'consumed', consumed_at = now()"
                " WHERE id = :id",
                pending.approval_id,
            ),
            "un-consume": (
                "UPDATE tool_approvals SET consumed_at = NULL WHERE id = :id",
                used.approval_id,
            ),
        }
        for label, (sql, approval_id) in cases.items():
            with self.subTest(change=label):
                error = await self.attempt(self.app_db, sql, id=approval_id)
                self.assertIsInstance(error, psycopg.errors.RestrictViolation)
        self.assertEqual(
            (await self.store.get(used.approval_id)).status, ApprovalStatus.CONSUMED
        )
        self.assertEqual(
            await self.store.consume(used.approval_id, binding_of(used), now=NOW),
            ConsumeOutcome.ALREADY_USED,
        )

    async def test_a_row_cannot_be_created_already_approved(self):
        error = await self.attempt(
            self.app_db,
            "INSERT INTO tool_approvals (id, task_id, task_attempt, task_retry_count,"
            " project_id, agent_id, requester_user_id, tool, level, call_hash, targets,"
            " summary, status, created_at, expires_at, approver_id, decided_at) VALUES"
            " (gen_random_uuid(), gen_random_uuid(), 1, 0, gen_random_uuid(), :a, :u,"
            " 'repo.delete_tree', 'approval', :h, '[]', '[{}]', 'approved', now(),"
            " now() + interval '1 hour', :u, now())",
            a=AGENT,
            u=U1,
            h=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        )
        self.assertIsInstance(error, psycopg.errors.RestrictViolation)

    async def test_only_the_delegating_user_can_be_written_as_the_approver(self):
        pending = new_approval()
        await self.store.open_request(pending, now=NOW, limits=LIMITS)
        for approver in (AGENT, U2, uuid.uuid4()):
            with self.subTest(approver=approver):
                error = await self.attempt(
                    self.app_db,
                    "UPDATE tool_approvals SET status = 'approved', approver_id = :a,"
                    " decided_at = now() WHERE id = :id",
                    a=approver,
                    id=pending.approval_id,
                )
                self.assertIsInstance(error, psycopg.errors.CheckViolation)
                self.assertIn("approver_is_delegating_user", str(error))
        strong = new_approval(level=ApprovalLevel.STRONG_APPROVAL)
        await self.store.open_request(strong, now=NOW, limits=LIMITS)
        error = await self.attempt(
            self.app_db,
            "UPDATE tool_approvals SET status = 'approved', approver_id = :u,"
            " decided_at = now() WHERE id = :id",
            u=U1,
            id=strong.approval_id,
        )
        self.assertIn("strong_needs_step_up", str(error))
        for approval in (pending, strong):
            self.assertEqual(
                (await self.store.get(approval.approval_id)).status,
                ApprovalStatus.PENDING,
            )

    async def test_nothing_can_be_deleted_truncated_or_rewritten_in_the_history(self):
        new = await self.approved()
        for sql in (
            "DELETE FROM tool_approvals WHERE id = :id",
            "TRUNCATE tool_approvals CASCADE",
            "TRUNCATE tool_approval_events",
            "DELETE FROM tool_approval_events WHERE approval_id = :id",
            "UPDATE tool_approval_events SET kind = 'expired' WHERE approval_id = :id",
            "UPDATE tool_approval_events SET summary = '[{}]'::jsonb",
        ):
            with self.subTest(sql=sql[:40]):
                error = await self.attempt(self.app_db, sql, id=new.approval_id)
                self.assertIsInstance(error, psycopg.errors.InsufficientPrivilege)
        self.assertEqual(len(await self.store.history(new.approval_id)), 2)

    async def test_the_role_cannot_switch_off_or_replace_the_guards(self):
        for sql in (
            *(
                f"ALTER TABLE {table} DISABLE TRIGGER {name}"
                for name in GUARDS
                for table in (
                    "tool_approval_events" if "events" in name else "tool_approvals",
                )
            ),
            "ALTER TABLE tool_approvals DISABLE TRIGGER ALL",
            "ALTER TABLE tool_approvals DISABLE TRIGGER USER",
            "DROP TRIGGER tool_approvals_state_machine ON tool_approvals",
            "DROP TRIGGER tool_approval_events_no_truncate ON tool_approval_events",
            "DROP FUNCTION tool_approvals_check_update() CASCADE",
            "CREATE OR REPLACE FUNCTION tool_approvals_check_update() RETURNS trigger"
            " LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
            "ALTER TABLE tool_approvals DROP COLUMN summary",
            "ALTER TABLE tool_approvals ADD COLUMN note text",
            "ALTER TABLE tool_approvals DROP CONSTRAINT"
            " ck_tool_approvals_approver_is_delegating_user",
            "DROP INDEX uq_tool_approvals_open_call",
            "CREATE RULE hide AS ON UPDATE TO tool_approvals DO INSTEAD NOTHING",
            "ALTER TABLE tool_approvals RENAME TO tool_approvals_old",
            "DROP TABLE tool_approval_events",
            f"ALTER TABLE tool_approvals OWNER TO {self.role}",
            "SET session_replication_role = replica",
            "ALTER ROLE CURRENT_USER BYPASSRLS",
        ):
            with self.subTest(sql=sql[:70]):
                error = await self.attempt(self.app_db, sql)
                self.assertIsInstance(error, psycopg.errors.InsufficientPrivilege)
        # It cannot hand itself more privileges either (PostgreSQL only warns
        # and grants nothing without the grant option) ...
        async with self.app_db.session() as session:
            await session.execute(
                text(f"GRANT DELETE, TRUNCATE, UPDATE ON tool_approvals TO {self.role}")
            )
            await session.commit()
        access = await read_tool_table_access(self.app_db)
        self.assertTrue(all(table.protected for table in access.values()))
        # ... and every guard is still there, and still ENABLE ALWAYS.
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal"
                " AND tgenabled = 'A' AND tgrelid IN ('tool_approvals'::regclass,"
                " 'tool_approval_events'::regclass)"
            ),
            len(GUARDS),
        )

    async def test_a_role_that_was_granted_nothing_has_no_access(self):
        for sql in (
            "SELECT count(*) FROM tool_approvals",
            "SELECT count(*) FROM tool_approval_events",
            "INSERT INTO tool_approval_events (approval_id, kind, agent_id,"
            " created_at, summary) VALUES (gen_random_uuid(), 'requested',"
            " gen_random_uuid(), now(), '[{}]')",
        ):
            with self.subTest(sql=sql[:40]):
                error = await self.attempt(self.other_db, sql)
                self.assertIsInstance(error, psycopg.errors.InsufficientPrivilege)
        # (``get`` runs on an abortable connection, so the error is the driver's
        # own and not SQLAlchemy's wrapper of it)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            await PostgresApprovalStore(self.other_db).get(uuid.uuid4())


class DiagnosticsTest(RoleTestCase):
    """The startup check warns exactly when a role could get around the guards."""

    async def test_what_the_restricted_role_may_do(self):
        access = await read_tool_table_access(self.app_db)
        self.assertEqual(set(access), {"tool_approvals", "tool_approval_events"})
        approvals, events = access["tool_approvals"], access["tool_approval_events"]
        self.assertEqual(
            (
                approvals.owns,
                approvals.can_insert,
                approvals.can_update,
                approvals.can_update_some,
                approvals.can_delete,
                approvals.can_truncate,
            ),
            (False, True, False, True, False, False),
        )
        self.assertEqual(
            (events.owns, events.can_insert, events.can_update_some, events.can_delete),
            (False, True, False, False),
        )
        self.assertTrue(approvals.protected and events.protected)
        self.assertFalse(approvals.cannot_work or events.cannot_work)

    async def test_the_restricted_role_gets_no_warning(self):
        with self.assertNoLogs("paw_backend.authz.diagnostics", level="WARNING"):
            await warn_about_loose_privileges(self.app_db, 3)

    async def test_the_owner_is_warned_for_both_tables_without_any_secret(self):
        with self.assertLogs("paw_backend.authz.diagnostics", level="WARNING") as logs:
            await warn_if_tool_approval_tables_are_mutable(self.owner_db, 3)
        self.assertEqual(len(logs.output), 2)
        blob = "\n".join(logs.output)
        for table in ("tool_approvals", "tool_approval_events"):
            self.assertIn(f"restricted on {table}", blob)
        self.assertIn("owner=True", blob)
        for secret in (self.role, self.password, "postgresql://"):
            self.assertNotIn(secret, blob)

    async def test_the_startup_check_covers_the_audit_trail_and_the_approvals(self):
        with self.assertLogs("paw_backend.authz.diagnostics", level="WARNING") as logs:
            await warn_about_loose_privileges(self.owner_db, 3)
        blob = "\n".join(logs.output)
        self.assertIn("audit_events", blob)
        self.assertIn("tool_approvals", blob)
        self.assertEqual(len(logs.output), 3)

    async def test_a_role_that_cannot_write_the_tables_is_warned(self):
        access = await read_tool_table_access(self.other_db)
        # nothing granted: it cannot even see the tables' privileges as usable
        self.assertTrue(all(table.cannot_work for table in access.values()))
        with self.assertLogs("paw_backend.authz.diagnostics", level="WARNING") as logs:
            await warn_if_tool_approval_tables_are_mutable(self.other_db, 3)
        blob = "\n".join(logs.output)
        self.assertIn("cannot use tool_approvals", blob)
        self.assertIn("cannot use tool_approval_events", blob)
        self.assertIn("PAW_APP_DATABASE_ROLE", blob)

    async def test_extra_privileges_are_warned(self):
        for grant, table in (
            ("UPDATE", "tool_approvals"),  # the whole table, not just the state columns
            ("DELETE", "tool_approvals"),
            ("TRUNCATE", "tool_approvals"),
            ("UPDATE", "tool_approval_events"),
            ("DELETE", "tool_approval_events"),
        ):
            with self.subTest(grant=grant, table=table):
                await self.owner_execute(f"GRANT {grant} ON {table} TO {self.role}")
                with self.assertLogs(
                    "paw_backend.authz.diagnostics", level="WARNING"
                ) as logs:
                    await warn_if_tool_approval_tables_are_mutable(self.app_db, 3)
                self.assertEqual(len(logs.output), 1)
                self.assertIn(f"restricted on {table}", logs.output[0])
                await self.owner_execute(f"REVOKE {grant} ON {table} FROM {self.role}")
                if table == "tool_approvals" and grant == "UPDATE":
                    # revoking the table-level UPDATE also revoked nothing else
                    await self.owner_execute(
                        f"GRANT UPDATE (status, approver_id, decided_at, consumed_at,"
                        f" step_up_verified, revoked_at, revoked_by) ON tool_approvals"
                        f" TO {self.role}"
                    )
        with self.assertNoLogs("paw_backend.authz.diagnostics", level="WARNING"):
            await warn_if_tool_approval_tables_are_mutable(self.app_db, 3)

    async def test_the_check_never_raises_when_the_database_is_unreachable(self):
        unreachable = Database(
            make_settings(
                database_url="postgresql://x:y@127.0.0.1:1/nothing",
                database_timeout_seconds=1,
            )
        )
        self.addAsyncCleanup(unreachable.dispose)
        with self.assertNoLogs("paw_backend.authz.diagnostics", level="WARNING"):
            await warn_about_loose_privileges(unreachable, 2)

    async def test_missing_tables_are_not_an_error(self):
        await asyncio.to_thread(migrate, "downgrade", "0033")
        with self.assertNoLogs("paw_backend.authz.diagnostics", level="WARNING"):
            await warn_if_tool_approval_tables_are_mutable(self.owner_db, 3)
        self.assertEqual(await read_tool_table_access(self.owner_db), {})


if __name__ == "__main__":
    unittest.main()
