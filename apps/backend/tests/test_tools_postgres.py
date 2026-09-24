"""The approval store on a real PostgreSQL.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set (see test_postgres_integration).

The store contract is the one the in-memory double passes; on top of it these
tests check what only a database can: the constraints that make an approval
trustworthy even against a bug in the application, the append-only history and
the single winner among concurrent callers on separate connection pools.
"""

import asyncio
import contextlib
import hashlib
import time
import unittest
import uuid
from datetime import timedelta

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from paw_backend.authz import InMemoryAuditSink, SystemRole
from paw_backend.db import Database
from paw_backend.tasks import (
    TERMINAL_STATES,
    Actor,
    TaskCommand,
    TaskService,
    TaskState,
    WaitReason,
)
from paw_backend.tools import (
    ApprovalLevel,
    ApprovalOutcome,
    ApprovalService,
    ApprovalStatus,
    BrokerReason,
    ConsumeOutcome,
    DecideOutcome,
    OpenLimits,
    OpenOutcome,
    PostgresApprovalStore,
    PostgresTaskActivity,
    TaskActivity,
    Verdict,
)

from .fake_postgres import HangingPostgres
from .support import make_settings
from .task_support import migrate, new_database, requires_postgres
from .tools_store_contract import (
    HOUR,
    LIMITS,
    StoreContract,
    binding_of,
    new_approval,
)
from .tools_support import (
    AGENT,
    NOW,
    P1,
    ROOT,
    TASK,
    U1,
    U2,
    Clock,
    Harness,
    StepUp,
    make_call,
    make_context,
    principal,
)

DELETE = {"path": f"{ROOT}/build"}


# Every trigger of the two tables, with the table it guards.
TRIGGERS = {
    "tool_approvals_created_pending": "tool_approvals",
    "tool_approvals_state_machine": "tool_approvals",
    "tool_approvals_no_delete": "tool_approvals",
    "tool_approvals_no_truncate": "tool_approvals",
    "tool_approval_events_append_only": "tool_approval_events",
    "tool_approval_events_no_truncate": "tool_approval_events",
}


async def without_trigger(connection, name: str, statement: str, **parameters):
    """Run ``statement`` with one guard switched off, inside the caller's
    transaction: the trigger comes back (``ENABLE ALWAYS``) before it commits,
    and if the statement fails the whole transaction, switch-off included, is
    rolled back. Only the table owner can do this (the test user is one)."""
    table = TRIGGERS[name]
    await connection.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {name}"))
    result = await connection.execute(text(statement), parameters)
    await connection.execute(text(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}"))
    return result


async def empty_tool_tables(connection) -> None:
    """Empty the two tables of a throw-away test database (the guards forbid
    TRUNCATE for everybody else, so they are switched off around it)."""
    for name, table in TRIGGERS.items():
        if name.endswith("no_truncate"):
            await connection.execute(
                text(f"ALTER TABLE {table} DISABLE TRIGGER {name}")
            )
    await connection.execute(text("TRUNCATE tool_approvals, tool_approval_events"))
    for name, table in TRIGGERS.items():
        if name.endswith("no_truncate"):
            await connection.execute(
                text(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}")
            )


class PostgresTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        migrate()

    @classmethod
    def tearDownClass(cls):
        async def clean():
            database = new_database()
            try:
                async with database.engine.begin() as connection:
                    await empty_tool_tables(connection)
            finally:
                await database.dispose()

        asyncio.run(clean())

    async def asyncSetUp(self):
        self.database = self.new_database()
        self.store = PostgresApprovalStore(self.database)

    def new_database(self):
        """Another independent engine, as a second backend process would have."""
        database = new_database()
        self.addAsyncCleanup(database.dispose)
        return database

    async def execute(self, sql: str, **parameters):
        async with self.database.engine.begin() as connection:
            return (await connection.execute(text(sql), parameters)).rowcount


@requires_postgres
class PostgresStoreContractTest(StoreContract, PostgresTestCase):
    pass


ROW = {
    "id": None,
    "task_id": TASK,
    "project_id": P1,
    "agent_id": AGENT,
    "requester_user_id": U1,
    "tool": "repo.delete_tree",
    "level": "approval",
    "call_hash": None,
    "targets": "[]",
    "summary": '[{"name": "path", "kind": "path", "value": "/x"}]',
    "status": "pending",
    "created_at": NOW,
    "expires_at": NOW + HOUR,
    "approver_id": None,
    "decided_at": None,
    "consumed_at": None,
}


@requires_postgres
class ConstraintTest(PostgresTestCase):
    """Rows the application must never write are refused by the database too.

    These are the CHECK constraints, tested with the insert trigger off (the
    trigger alone refuses every row that is not pending: see StateMachineTest).
    """

    async def insert(self, **overrides):
        row = dict(ROW)
        row["id"] = uuid.uuid4()
        row["call_hash"] = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        row.update(overrides)
        columns = ", ".join(row)
        values = ", ".join(f":{name}" for name in row)
        sql = f"INSERT INTO tool_approvals ({columns}) VALUES ({values})"
        sql = sql.replace(":targets", "CAST(:targets AS jsonb)")
        sql = sql.replace(":summary", "CAST(:summary AS jsonb)")
        # The insert trigger allows only pending rows: to test the CHECK
        # constraints behind it, it is switched off for this one statement.
        async with self.database.engine.begin() as connection:
            await without_trigger(
                connection, "tool_approvals_created_pending", sql, **row
            )
        return row["id"]

    async def refused(self, constraint: str, **overrides):
        with self.assertRaises(IntegrityError) as caught:
            await self.insert(**overrides)
        self.assertIn(constraint, str(caught.exception))

    async def test_a_valid_row_is_accepted(self):
        await self.insert()
        await self.insert(
            status="approved",
            approver_id=U1,
            decided_at=NOW,
            level="strong_approval",
            step_up_verified=True,
        )
        await self.insert(status="revoked", revoked_at=NOW, revoked_by=U1)
        await self.insert(status="revoked", revoked_at=NOW)
        await self.insert(
            status="consumed", approver_id=U1, decided_at=NOW, consumed_at=NOW
        )
        await self.insert(status="expired")

    async def test_only_the_delegating_user_can_be_the_approver(self):
        await self.refused(
            "ck_tool_approvals_approver_is_delegating_user",
            status="approved",
            approver_id=U2,
            decided_at=NOW,
        )
        await self.refused(
            "ck_tool_approvals_approver_is_delegating_user",
            status="approved",
            approver_id=AGENT,
            decided_at=NOW,
        )

    async def test_an_agent_cannot_be_the_user_it_acts_for(self):
        await self.refused("ck_tool_approvals_agent_is_not_user", agent_id=U1)

    async def test_the_shape_of_a_row_is_checked(self):
        await self.refused("ck_tool_approvals_call_hash_sha256", call_hash="A" * 64)
        await self.refused("ck_tool_approvals_call_hash_sha256", call_hash="a" * 63)
        await self.refused("ck_tool_approvals_status_valid", status="granted")
        await self.refused("ck_tool_approvals_level_valid", level="auto")
        await self.refused("ck_tool_approvals_level_valid", level="deny")
        await self.refused(
            "ck_tool_approvals_expires_after_creation", expires_at=NOW - HOUR
        )

    async def test_state_and_decision_must_agree(self):
        await self.refused(
            "ck_tool_approvals_pending_is_undecided", approver_id=U1, decided_at=NOW
        )
        await self.refused("ck_tool_approvals_decision_has_approver", status="approved")
        await self.refused(
            "ck_tool_approvals_decision_has_approver",
            status="rejected",
            approver_id=U1,
        )
        await self.refused(
            "ck_tool_approvals_consumed_matches_status",
            status="consumed",
            approver_id=U1,
            decided_at=NOW,
        )
        await self.refused(
            "ck_tool_approvals_consumed_matches_status",
            status="approved",
            approver_id=U1,
            decided_at=NOW,
            consumed_at=NOW,
        )

    async def test_a_strong_approval_needs_a_step_up(self):
        for status in ("approved", "consumed"):
            with self.subTest(status=status):
                await self.refused(
                    "ck_tool_approvals_strong_needs_step_up",
                    level="strong_approval",
                    status=status,
                    approver_id=U1,
                    decided_at=NOW,
                    consumed_at=NOW if status == "consumed" else None,
                )
        await self.refused(
            "ck_tool_approvals_pending_is_undecided", step_up_verified=True
        )

    async def test_a_revocation_is_recorded_with_its_time(self):
        await self.refused("ck_tool_approvals_revoked_matches_status", status="revoked")
        await self.refused(
            "ck_tool_approvals_revoked_matches_status", revoked_at=NOW, status="expired"
        )
        await self.refused("ck_tool_approvals_revoked_matches_status", revoked_by=U1)

    async def test_the_summary_must_be_a_non_empty_bounded_array(self):
        for summary in (
            "[]",
            "{}",
            '"text"',
            "null",
            "[" + ",".join(["{}"] * 17) + "]",
        ):
            with self.subTest(summary=summary[:20]):
                with self.assertRaises(IntegrityError):
                    await self.insert(summary=summary)
        await self.insert(summary="[" + ",".join(["{}"] * 16) + "]")

    async def test_at_most_one_open_approval_exists_per_exact_call(self):
        call_hash = hashlib.sha256(b"one call").hexdigest()
        await self.insert(call_hash=call_hash)
        with self.assertRaises(IntegrityError) as caught:
            await self.insert(call_hash=call_hash)
        self.assertIn("uq_tool_approvals_open_call", str(caught.exception))
        with self.assertRaises(IntegrityError):
            await self.insert(
                call_hash=call_hash, status="approved", approver_id=U1, decided_at=NOW
            )
        # finished approvals do not block a new one
        await self.insert(call_hash=call_hash, status="expired")
        await self.insert(
            call_hash=call_hash,
            status="consumed",
            approver_id=U1,
            decided_at=NOW,
            consumed_at=NOW,
        )

    async def test_the_history_rows_are_checked_and_tied_to_an_approval(self):
        approval_id = await self.insert()

        async def event(**overrides):
            row = {
                "approval_id": approval_id,
                "kind": "requested",
                "actor_user_id": None,
                "agent_id": AGENT,
                "created_at": NOW,
                "summary": '[{"name": "path", "kind": "path", "value": "/x"}]',
            }
            row.update(overrides)
            await self.execute(
                "INSERT INTO tool_approval_events (approval_id, kind, actor_user_id,"
                " agent_id, created_at, summary) VALUES (:approval_id, :kind,"
                " :actor_user_id, :agent_id, :created_at, CAST(:summary AS jsonb))",
                **row,
            )

        await event()
        await event(kind="approved", actor_user_id=U1, agent_id=None, summary=None)
        await event(kind="revoked", actor_user_id=U1, agent_id=None, summary=None)
        await event(kind="revoked", actor_user_id=None, agent_id=None, summary=None)
        for overrides, constraint in (
            ({"summary": None}, "ck_tool_approval_events_summary_matches_kind"),
            (
                {"kind": "approved", "actor_user_id": U1, "agent_id": None},
                "ck_tool_approval_events_summary_matches_kind",
            ),
            (
                {
                    "kind": "expired",
                    "actor_user_id": U1,
                    "agent_id": None,
                    "summary": None,
                },
                "ck_tool_approval_events_user_matches_kind",
            ),
            (
                {"kind": "granted", "agent_id": None},
                "ck_tool_approval_events_kind_valid",
            ),
            ({"agent_id": None}, "ck_tool_approval_events_agent_matches_kind"),
            (
                {"kind": "approved", "agent_id": None, "summary": None},
                "ck_tool_approval_events_user_matches_kind",
            ),
            ({"approval_id": uuid.uuid4()}, "fk_tool_approval_events_approval_id"),
        ):
            with self.subTest(overrides=str(overrides)[:40]):
                with self.assertRaises(IntegrityError) as caught:
                    await event(**overrides)
                self.assertIn(constraint, str(caught.exception))

    async def test_the_history_cannot_be_rewritten(self):
        approval_id = await self.insert()
        await self.execute(
            "INSERT INTO tool_approval_events "
            "(approval_id, kind, agent_id, created_at, summary) "
            "VALUES (:a, 'requested', :agent, :at, CAST('[{}]' AS jsonb))",
            a=approval_id,
            agent=AGENT,
            at=NOW,
        )
        for sql in (
            "UPDATE tool_approval_events SET kind = 'expired' WHERE approval_id = :a",
            "DELETE FROM tool_approval_events WHERE approval_id = :a",
        ):
            with self.subTest(sql=sql.split()[0]):
                with self.assertRaises(DBAPIError) as caught:
                    await self.execute(sql, a=approval_id)
                self.assertIn("append-only", str(caught.exception))
        rows = await self.execute(
            "SELECT 1 FROM tool_approval_events WHERE approval_id = :a", a=approval_id
        )
        self.assertEqual(rows, 1)


@requires_postgres
class StateMachineTest(PostgresTestCase):
    """The triggers: only legal changes, immutable identity, no removal.

    Run as the table owner (a superuser), so what stops these statements is the
    database's own rules, not privileges: see test_tools_postgres_roles for the
    application role.
    """

    async def approval(self, status="pending", **overrides):
        """A stored approval in the given state (reached through legal steps)."""
        new = new_approval(**overrides)
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        if status in ("approved", "consumed", "expired-after-approval"):
            await self.store.decide(
                new.approval_id,
                approver_id=U1,
                approve=True,
                now=NOW,
                step_up_verified=True,
            )
        if status == "consumed":
            await self.store.consume(new.approval_id, binding_of(new), now=NOW)
        if status == "rejected":
            await self.store.decide(
                new.approval_id, approver_id=U1, approve=False, now=NOW
            )
        if status == "revoked":
            await self.store.revoke(new.approval_id, actor_id=U1, now=NOW)
        if status == "expired":
            await self.store.consume(
                new.approval_id, binding_of(new), now=NOW + 2 * HOUR
            )
        return new

    async def refused(self, sql: str, **parameters):
        with self.assertRaises(DBAPIError) as caught:
            await self.execute(sql, **parameters)
        if sql == "TRUNCATE tool_approvals":
            # PostgreSQL refuses it on its own (a foreign key points at the
            # table) before any trigger runs.
            self.assertIsInstance(
                caught.exception.orig, psycopg.errors.FeatureNotSupported
            )
        else:
            self.assertIsInstance(
                caught.exception.orig, psycopg.errors.RestrictViolation
            )
        return str(caught.exception.orig)

    async def status_of(self, approval_id) -> str:
        record = await self.store.get(approval_id)
        return record.status.value

    async def test_all_six_guards_are_enabled_always(self):
        async with self.database.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT tgname, tgenabled::text FROM pg_trigger "
                        "WHERE NOT tgisinternal AND tgrelid IN "
                        "('tool_approvals'::regclass, 'tool_approval_events'::regclass)"
                    )
                )
            ).all()
        # 'A' = ENABLE ALWAYS: they also fire under session_replication_role=replica
        self.assertEqual(dict(rows), dict.fromkeys(TRIGGERS, "A"))

    async def test_the_identity_of_an_approval_cannot_be_rewritten(self):
        new = await self.approval()
        for column, value in (
            ("task_id", uuid.uuid4()),
            ("project_id", uuid.uuid4()),
            ("agent_id", uuid.uuid4()),
            ("requester_user_id", U2),
            ("tool", "host.install_package"),
            ("level", "strong_approval"),
            ("call_hash", hashlib.sha256(b"another call").hexdigest()),
            ("targets", '[{"kind": "path", "value": "/etc"}]'),
            ("summary", '[{"name": "path", "kind": "path", "value": "/etc"}]'),
            ("created_at", NOW - HOUR),
            ("expires_at", NOW + timedelta(days=365)),
            ("id", uuid.uuid4()),
        ):
            with self.subTest(column=column):
                cast = "CAST(:v AS jsonb)" if column in ("targets", "summary") else ":v"
                message = await self.refused(
                    f"UPDATE tool_approvals SET {column} = {cast} WHERE id = :id",
                    v=value,
                    id=new.approval_id,
                )
                self.assertIn("cannot change", message)
        # ... also while the state changes legally at the same time
        await self.refused(
            "UPDATE tool_approvals SET status = 'approved', approver_id = :u,"
            " decided_at = :at, expires_at = :far WHERE id = :id",
            u=U1,
            at=NOW,
            far=NOW + timedelta(days=365),
            id=new.approval_id,
        )
        self.assertEqual(await self.status_of(new.approval_id), "pending")

    async def test_only_the_legal_state_changes_are_possible(self):
        legal = {
            ("pending", "approved"),
            ("pending", "rejected"),
            ("pending", "revoked"),
            ("pending", "expired"),
            ("approved", "consumed"),
            ("approved", "revoked"),
            ("approved", "expired"),
        }
        statuses = ("pending", "approved", "rejected", "consumed", "revoked", "expired")
        columns = {
            "approved": "approver_id = :u, decided_at = :at",
            "rejected": "approver_id = :u, decided_at = :at",
            "consumed": "consumed_at = :at",
            "revoked": "revoked_at = :at",
            "expired": "",
            "pending": "",
        }
        for old in statuses:
            for new_status in statuses:
                if (old, new_status) in legal:
                    continue
                with self.subTest(change=f"{old} -> {new_status}"):
                    new = await self.approval(old)
                    assignments = f"status = '{new_status}'"
                    if columns[new_status] and old == "pending":
                        assignments += ", " + columns[new_status]
                    elif columns[new_status] and new_status != "consumed":
                        assignments += ", " + columns[new_status]
                    elif new_status == "consumed":
                        assignments += ", consumed_at = :at"
                    message = await self.refused(
                        f"UPDATE tool_approvals SET {assignments} WHERE id = :id",
                        u=U1,
                        at=NOW,
                        id=new.approval_id,
                    )
                    self.assertTrue(
                        "not allowed" in message or "sets other columns" in message
                    )
                    self.assertEqual(await self.status_of(new.approval_id), old)

    async def test_a_used_approval_cannot_be_replayed(self):
        new = await self.approval("consumed")
        for sql in (
            "UPDATE tool_approvals SET status = 'approved', consumed_at = NULL"
            " WHERE id = :id",
            "UPDATE tool_approvals SET status = 'pending', approver_id = NULL,"
            " decided_at = NULL, consumed_at = NULL WHERE id = :id",
            "UPDATE tool_approvals SET consumed_at = NULL WHERE id = :id",
        ):
            with self.subTest(sql=sql[:40]):
                await self.refused(sql, id=new.approval_id)
        self.assertEqual(await self.status_of(new.approval_id), "consumed")
        self.assertEqual(
            await self.store.consume(new.approval_id, binding_of(new), now=NOW),
            ConsumeOutcome.ALREADY_USED,
        )

    async def test_an_update_that_changes_no_state_is_refused(self):
        new = await self.approval("approved")
        await self.refused(
            "UPDATE tool_approvals SET decided_at = :at WHERE id = :id",
            at=NOW + HOUR,
            id=new.approval_id,
        )
        await self.refused(
            "UPDATE tool_approvals SET step_up_verified = false WHERE id = :id",
            id=new.approval_id,
        )

    async def test_each_change_sets_only_its_own_columns(self):
        for old, sql in (
            (
                "pending",
                "UPDATE tool_approvals SET status = 'approved', approver_id = :u,"
                " decided_at = :at, consumed_at = :at WHERE id = :id",
            ),
            (
                "pending",
                "UPDATE tool_approvals SET status = 'rejected', approver_id = :u,"
                " decided_at = :at, step_up_verified = true WHERE id = :id",
            ),
            (
                "approved",
                "UPDATE tool_approvals SET status = 'consumed', consumed_at = :at,"
                " approver_id = :u2 WHERE id = :id",
            ),
            (
                "approved",
                "UPDATE tool_approvals SET status = 'revoked', revoked_at = :at,"
                " decided_at = :far WHERE id = :id",
            ),
            (
                "approved",
                "UPDATE tool_approvals SET status = 'expired', consumed_at = :at"
                " WHERE id = :id",
            ),
        ):
            with self.subTest(old=old, sql=sql[30:80]):
                new = await self.approval(old)
                await self.refused(
                    sql, u=U1, u2=U2, at=NOW, far=NOW + HOUR, id=new.approval_id
                )
                self.assertEqual(await self.status_of(new.approval_id), old)

    async def test_a_strong_approval_cannot_be_approved_without_a_step_up(self):
        new = await self.approval(level=ApprovalLevel.STRONG_APPROVAL)
        with self.assertRaises(IntegrityError) as caught:
            await self.execute(
                "UPDATE tool_approvals SET status = 'approved', approver_id = :u,"
                " decided_at = :at WHERE id = :id",
                u=U1,
                at=NOW,
                id=new.approval_id,
            )
        self.assertIn("ck_tool_approvals_strong_needs_step_up", str(caught.exception))
        self.assertEqual(await self.status_of(new.approval_id), "pending")
        await self.execute(
            "UPDATE tool_approvals SET status = 'approved', approver_id = :u,"
            " decided_at = :at, step_up_verified = true WHERE id = :id",
            u=U1,
            at=NOW,
            id=new.approval_id,
        )
        self.assertEqual(await self.status_of(new.approval_id), "approved")

    async def test_the_legal_changes_still_work(self):
        # what the store does, as plain SQL (the trigger is not in its way)
        new = await self.approval()
        await self.execute(
            "UPDATE tool_approvals SET status = 'approved', approver_id = :u,"
            " decided_at = :at WHERE id = :id",
            u=U1,
            at=NOW,
            id=new.approval_id,
        )
        await self.execute(
            "UPDATE tool_approvals SET status = 'consumed', consumed_at = :at"
            " WHERE id = :id",
            at=NOW,
            id=new.approval_id,
        )
        self.assertEqual(await self.status_of(new.approval_id), "consumed")

    async def test_a_row_is_born_pending_and_undecided(self):
        head = (
            "INSERT INTO tool_approvals (id, task_id, project_id, agent_id,"
            " requester_user_id, tool, level, call_hash, targets, summary, status,"
            " created_at, expires_at"
        )
        body = (
            ") VALUES (:id, :t, :p, :a, :u, 'repo.delete_tree', 'approval', :h,"
            " CAST('[]' AS jsonb), CAST('[{}]' AS jsonb), :status, :at, :far"
        )
        cases = {
            "approved": ("approver_id, decided_at", ":u, :at", "approved"),
            "consumed": (
                "approver_id, decided_at, consumed_at",
                ":u, :at, :at",
                "consumed",
            ),
            "expired": ("", "", "expired"),
            "revoked": ("revoked_at", ":at", "revoked"),
            "pending with a step-up": ("step_up_verified", "true", "pending"),
            "pending with an approver": (
                "approver_id, decided_at",
                ":u, :at",
                "pending",
            ),
        }
        for label, (extra_columns, extra_values, status) in cases.items():
            with self.subTest(row=label):
                sql = head + (", " + extra_columns if extra_columns else "")
                sql += body + (", " + extra_values if extra_values else "") + ")"
                message = await self.refused(
                    sql,
                    id=uuid.uuid4(),
                    t=uuid.uuid4(),
                    p=P1,
                    a=AGENT,
                    u=U1,
                    h=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                    status=status,
                    at=NOW,
                    far=NOW + HOUR,
                )
                self.assertIn("created pending", message)
        # ... and an ordinary pending row is fine
        sql = head + body + ")"
        await self.execute(
            sql,
            id=uuid.uuid4(),
            t=uuid.uuid4(),
            p=P1,
            a=AGENT,
            u=U1,
            h=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            status="pending",
            at=NOW,
            far=NOW + HOUR,
        )

    async def test_approvals_and_their_history_cannot_be_removed(self):
        new = await self.approval("consumed")
        for sql in (
            "DELETE FROM tool_approvals WHERE id = :id",
            "DELETE FROM tool_approval_events WHERE approval_id = :id",
            "UPDATE tool_approval_events SET kind = 'expired' WHERE approval_id = :id",
            "TRUNCATE tool_approvals",
            "TRUNCATE tool_approvals CASCADE",
            "TRUNCATE tool_approvals, tool_approval_events",
            "TRUNCATE tool_approval_events",
        ):
            with self.subTest(sql=sql[:40]):
                await self.refused(sql, id=new.approval_id)
        self.assertEqual(await self.status_of(new.approval_id), "consumed")
        self.assertEqual(len(await self.store.history(new.approval_id)), 3)

    async def test_the_guards_also_hold_under_the_replica_role(self):
        # session_replication_role = replica switches off ordinary triggers (and
        # foreign keys); the ENABLE ALWAYS ones still fire.
        new = await self.approval("consumed")
        for sql in (
            "DELETE FROM tool_approval_events WHERE approval_id = :id",
            "UPDATE tool_approval_events SET kind = 'expired' WHERE approval_id = :id",
            "TRUNCATE tool_approval_events",
            "TRUNCATE tool_approvals CASCADE",
            "DELETE FROM tool_approvals WHERE id = :id",
            "UPDATE tool_approvals SET status = 'approved', consumed_at = NULL"
            " WHERE id = :id",
            "UPDATE tool_approvals SET expires_at = expires_at + interval '365 days'"
            " WHERE id = :id",
        ):
            with self.subTest(sql=sql[:50]):
                with self.assertRaises(DBAPIError) as caught:
                    async with self.database.engine.begin() as connection:
                        await connection.execute(
                            text("SET LOCAL session_replication_role = replica")
                        )
                        await connection.execute(text(sql), {"id": new.approval_id})
                self.assertIsInstance(
                    caught.exception.orig, psycopg.errors.RestrictViolation
                )
        self.assertEqual(await self.status_of(new.approval_id), "consumed")


@requires_postgres
class CrossProcessTest(PostgresTestCase):
    """Several engines (as several backend processes) race for one approval."""

    async def stores(self, count=4):
        return [PostgresApprovalStore(self.new_database()) for _ in range(count)]

    async def live_context(self):
        """The context of a task that exists (the store reads its state when an
        approval is used, so a task the database does not know cannot use one)."""
        created = await TaskService(self.new_database()).create_task(
            project_id=P1, created_by=U1, title="A task that uses an approval"
        )
        return make_context(task_id=created.task_id)

    async def test_concurrent_approvals_from_separate_pools_have_one_winner(self):
        stores = await self.stores()
        new = new_approval()
        await stores[0].open_request(new, now=NOW, limits=LIMITS)
        results = await asyncio.gather(
            *(
                stores[i % len(stores)].decide(
                    new.approval_id, approver_id=U1, approve=True, now=NOW
                )
                for i in range(24)
            )
        )
        self.assertEqual(
            sorted(r.outcome.value for r in results), ["decided"] + ["not_pending"] * 23
        )
        history = await stores[0].history(new.approval_id)
        self.assertEqual([h.kind.value for h in history], ["requested", "approved"])

    async def test_concurrent_uses_from_separate_pools_consume_once(self):
        stores = await self.stores()
        new = new_approval()
        await stores[0].open_request(new, now=NOW, limits=LIMITS)
        await stores[0].decide(new.approval_id, approver_id=U1, approve=True, now=NOW)
        outcomes = await asyncio.gather(
            *(
                stores[i % len(stores)].consume(
                    new.approval_id, binding_of(new), now=NOW
                )
                for i in range(24)
            )
        )
        self.assertEqual(
            sorted(o.value for o in outcomes), ["already_used"] * 23 + ["consumed"]
        )
        history = await stores[0].history(new.approval_id)
        self.assertEqual(
            [h.kind.value for h in history], ["requested", "approved", "consumed"]
        )

    async def test_concurrent_requests_for_one_call_open_one_approval(self):
        stores = await self.stores()
        first = new_approval()
        opened = await asyncio.gather(
            *(
                stores[i % len(stores)].open_request(
                    new_approval(task_id=first.task_id, call_hash=first.call_hash),
                    now=NOW,
                    limits=LIMITS,
                )
                for i in range(24)
            )
        )
        self.assertEqual(sum(o.created for o in opened), 1)
        self.assertEqual(len({o.record.approval_id for o in opened}), 1)
        count = await self.execute(
            "SELECT 1 FROM tool_approvals WHERE call_hash = :h", h=first.call_hash
        )
        self.assertEqual(count, 1)

    async def test_concurrent_requests_from_separate_pools_cannot_exceed_the_cap(self):
        stores = await self.stores()
        limits = OpenLimits(max_pending=5, rejection_cooldown=timedelta(minutes=5))
        task_id = uuid.uuid4()
        opened = await asyncio.gather(
            *(
                stores[i % len(stores)].open_request(
                    new_approval(task_id=task_id), now=NOW, limits=limits
                )
                for i in range(40)
            )
        )
        outcomes = sorted(o.outcome.value for o in opened)
        self.assertEqual(outcomes, ["created"] * 5 + ["too_many_pending"] * 35)
        rows = await self.execute(
            "SELECT 1 FROM tool_approvals WHERE task_id = :t", t=task_id
        )
        self.assertEqual(rows, 5)

    async def test_the_broker_flow_cannot_exceed_the_cap_either(self):
        stores = [PostgresApprovalStore(self.new_database()) for _ in range(3)]
        clock = Clock()
        harnesses = [
            Harness(approvals=store, clock=clock, max_pending_approvals=4)
            for store in stores
        ]
        # tasks the database has: the store checks the task when it opens a request
        single = await harnesses[0].broker.request(
            make_call(
                "repo.delete_tree",
                {"path": f"{ROOT}/cap-0"},
                context=await self.live_context(),
            )
        )
        self.assertEqual(single.verdict, Verdict.NEEDS_APPROVAL)
        task_context = await self.live_context()
        decisions = await asyncio.gather(
            *(
                harnesses[i % 3].broker.request(
                    make_call(
                        "repo.delete_tree",
                        {"path": f"{ROOT}/race-cap-{i}"},
                        context=task_context,
                    )
                )
                for i in range(20)
            )
        )
        self.assertEqual(sum(d.verdict is Verdict.NEEDS_APPROVAL for d in decisions), 4)
        self.assertEqual(
            {d.reason for d in decisions if d.verdict is Verdict.DENY},
            {BrokerReason.APPROVAL_LIMIT_REACHED},
        )

    async def test_a_rejection_cools_the_call_down_across_processes(self):
        stores = await self.stores(2)
        new = new_approval()
        await stores[0].open_request(new, now=NOW, limits=LIMITS)
        await stores[0].decide(new.approval_id, approver_id=U1, approve=False, now=NOW)
        again = new_approval(task_id=new.task_id, call_hash=new.call_hash)
        refused = await stores[1].open_request(again, now=NOW, limits=LIMITS)
        self.assertEqual(refused.outcome, OpenOutcome.COOLING_DOWN)

    async def test_a_cancelled_task_revokes_its_approvals_through_the_task_service(
        self,
    ):
        from paw_backend.tasks import Actor, TaskCommand, TaskService

        clock = Clock()
        h = Harness(approvals=PostgresApprovalStore(self.new_database()), clock=clock)
        tasks = TaskService(
            self.new_database(), listeners=[h.service.revoke_on_task_end]
        )
        created = await tasks.create_task(
            project_id=P1, created_by=U1, title="Delete the build"
        )
        context = make_context(task_id=created.task_id)
        call = make_call("repo.delete_tree", DELETE, context=context)
        pending = await h.broker.request(call)
        self.assertEqual(pending.verdict, Verdict.NEEDS_APPROVAL)
        approved = await h.broker.request(
            make_call("repo.delete_tree", {"path": f"{ROOT}/other"}, context=context)
        )
        await h.service.approve(approved.approval_id, principal(SystemRole.USER, U1))

        await tasks.execute(created.task_id, TaskCommand.CANCEL, actor=Actor.user(U1))

        for decision, arguments in (
            (pending, DELETE),
            (approved, {"path": f"{ROOT}/other"}),
        ):
            record = await h.approvals.get(decision.approval_id)
            self.assertEqual(record.status.value, "revoked")
            used = await h.broker.request(
                make_call("repo.delete_tree", arguments, context=context),
                approval_id=decision.approval_id,
            )
            self.assertEqual(used.reason, BrokerReason.APPROVAL_REVOKED)

    async def test_the_state_survives_a_restart(self):
        # "Restart" = every object is rebuilt from the database alone.
        clock = Clock()
        context = await self.live_context()
        first = Harness(
            approvals=PostgresApprovalStore(self.new_database()), clock=clock
        )
        pending = await first.broker.request(
            make_call("repo.delete_tree", DELETE, context=context)
        )
        self.assertEqual(pending.verdict, Verdict.NEEDS_APPROVAL)
        second = Harness(
            approvals=PostgresApprovalStore(self.new_database()), clock=clock
        )
        service = ApprovalService(
            second.approvals, second.sink, step_up=StepUp(True), clock=clock
        )
        result = await service.approve(
            pending.approval_id, principal(SystemRole.USER, U1)
        )
        self.assertEqual(result.outcome, ApprovalOutcome.APPROVED)
        third = Harness(
            approvals=PostgresApprovalStore(self.new_database()), clock=clock
        )
        used = await third.broker.request(
            make_call("repo.delete_tree", DELETE, context=context),
            approval_id=pending.approval_id,
        )
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.ALLOW, BrokerReason.APPROVAL_CONSUMED)
        )
        replay = await third.broker.request(
            make_call("repo.delete_tree", DELETE, context=context),
            approval_id=pending.approval_id,
        )
        self.assertEqual(replay.reason, BrokerReason.APPROVAL_ALREADY_USED)

    async def test_the_broker_flow_races_to_a_single_execution(self):
        stores = [PostgresApprovalStore(self.new_database()) for _ in range(3)]
        clock = Clock()
        harnesses = [Harness(approvals=store, clock=clock) for store in stores]
        call = make_call(
            "repo.delete_tree",
            {"path": f"{ROOT}/race-{uuid.uuid4()}"},
            context=await self.live_context(),
        )
        pending = await harnesses[0].broker.request(call)
        await harnesses[0].service.approve(
            pending.approval_id, principal(SystemRole.USER, U1)
        )
        decisions = await asyncio.gather(
            *(
                harnesses[i % 3].broker.request(call, approval_id=pending.approval_id)
                for i in range(18)
            )
        )
        self.assertEqual(sum(d.allowed for d in decisions), 1)
        self.assertEqual(
            {d.reason for d in decisions if not d.allowed},
            {BrokerReason.APPROVAL_ALREADY_USED},
        )

    async def test_an_expired_open_request_does_not_block_a_new_one(self):
        store = PostgresApprovalStore(self.database)
        first = new_approval()
        await store.open_request(first, now=NOW, limits=LIMITS)
        later = NOW + HOUR + timedelta(seconds=1)
        opened = await store.open_request(
            new_approval(
                task_id=first.task_id,
                call_hash=first.call_hash,
                expires_at=later + HOUR,
            ),
            now=later,
            limits=LIMITS,
        )
        self.assertTrue(opened.created)


if __name__ == "__main__":
    unittest.main()


C = TaskCommand
# Every state of the task lifecycle, and the commands that lead there.
PATHS_TO_STATES = {
    TaskState.QUEUED: [],
    TaskState.RUNNING: [(C.START, {})],
    TaskState.WAITING: [
        (C.START, {}),
        (C.WAIT, {"wait_reason": WaitReason.APPROVAL}),
    ],
    TaskState.PAUSED: [(C.START, {}), (C.PAUSE, {})],
    TaskState.EVALUATING: [(C.START, {}), (C.BEGIN_EVALUATION, {})],
    TaskState.COMPLETED: [
        (C.START, {}),
        (C.BEGIN_EVALUATION, {}),
        (C.COMPLETE, {}),
    ],
    TaskState.FAILED: [(C.FAIL, {})],
    TaskState.CANCELLED: [(C.CANCEL, {})],
}


class LockWaits:
    """Drive transactions in a fixed order: a step starts only once the previous
    one is provably blocked on a lock (``pg_stat_activity``)."""

    async def lock_waiters(self) -> int:
        async with self.database.engine.begin() as connection:
            return (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                        "current_database() AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()

    async def wait_for_lock_waiters(self, count: int, *unless_done) -> None:
        """Until ``count`` backends wait on a lock, or one of ``unless_done`` (a
        task that should have been blocked) has finished: then the caller's
        assertion says what went wrong, instead of a time-out."""
        async with asyncio.timeout(10):
            while await self.lock_waiters() < count:
                if any(task.done() for task in unless_done):
                    return
                await asyncio.sleep(0.02)


class TaskFixture(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tasks = TaskService(self.new_database())

    async def new_task(self, tasks: TaskService | None = None) -> uuid.UUID:
        created = await (tasks or self.tasks).create_task(
            project_id=P1, created_by=U1, title="A task with approvals"
        )
        return created.task_id

    async def drive(self, task_id, steps, tasks: TaskService | None = None):
        for command, arguments in steps:
            await (tasks or self.tasks).execute(
                task_id, command, actor=Actor.system(), **arguments
            )


@requires_postgres
class PostgresTaskActivityTest(TaskFixture):
    """The broker's view of a task is the real ``tasks`` row."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.activity = PostgresTaskActivity(self.new_database())

    async def test_every_state_of_the_lifecycle_is_active_or_ended(self):
        self.assertEqual(set(PATHS_TO_STATES), set(TaskState))  # none forgotten
        for state, steps in PATHS_TO_STATES.items():
            with self.subTest(state=state.value):
                task_id = await self.new_task()
                await self.drive(task_id, steps)
                expected = (
                    TaskActivity.ENDED
                    if state in TERMINAL_STATES
                    else TaskActivity.ACTIVE
                )
                self.assertEqual(await self.activity.check(task_id), expected)

    async def test_a_task_that_is_started_again_is_active_again(self):
        for state, command in (
            (TaskState.FAILED, C.RETRY),
            (TaskState.FAILED, C.RESTART),
            (TaskState.CANCELLED, C.RESTART),
        ):
            with self.subTest(state=state.value, command=command.value):
                task_id = await self.new_task()
                await self.drive(task_id, PATHS_TO_STATES[state])
                self.assertEqual(await self.activity.check(task_id), TaskActivity.ENDED)
                await self.drive(task_id, [(command, {})])
                self.assertEqual(
                    await self.activity.check(task_id), TaskActivity.ACTIVE
                )

    async def test_a_task_that_does_not_exist_is_unknown(self):
        self.assertEqual(await self.activity.check(uuid.uuid4()), TaskActivity.UNKNOWN)


class FakeDatabase:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows, error, []

    async def fetch_abortable(self, sql, params=None, *, timeout_seconds=None):
        self.calls.append((sql, params, timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.rows


class TaskActivityMappingTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_a_state_the_lifecycle_knows_is_an_answer(self):
        task_id = uuid.uuid4()
        for rows, expected in (
            ([], TaskActivity.UNKNOWN),
            ([("weird",)], TaskActivity.UNKNOWN),
            ([(None,)], TaskActivity.UNKNOWN),
            ([("Completed",)], TaskActivity.UNKNOWN),  # exact values only
            ([("completed",)], TaskActivity.ENDED),
            ([("running",)], TaskActivity.ACTIVE),
        ):
            with self.subTest(rows=rows):
                database = FakeDatabase(rows)
                activity = PostgresTaskActivity(database, timeout_seconds=1.5)
                self.assertEqual(await activity.check(task_id), expected)
                # the id is bound by the driver, and the read is bounded
                ((sql, params, timeout),) = database.calls
                self.assertEqual(params, {"id": task_id})
                self.assertNotIn(str(task_id), sql)
                self.assertEqual(timeout, 1.5)

    async def test_a_database_failure_is_not_turned_into_an_answer(self):
        activity = PostgresTaskActivity(FakeDatabase(error=ConnectionError("down")))
        with self.assertRaises(ConnectionError):
            await activity.check(uuid.uuid4())


class RevokeDownStore(PostgresApprovalStore):
    """The real store, except that revoking a task's approvals can fail."""

    down = False

    async def revoke_task(self, task_id, *, now):
        if self.down:
            raise ConnectionError("the database went away")
        return await super().revoke_task(task_id, now=now)


@requires_postgres
class TaskEndPathsTest(TaskFixture):
    """Every way a task ends, with the real TaskService, tables and provider.

    There is no "expired" task state (the lifecycle ends in completed, failed
    or cancelled); an approval runs out by its own ``expires_at``.
    """

    ENDS = {
        TaskState.COMPLETED: PATHS_TO_STATES[TaskState.COMPLETED],
        TaskState.FAILED: PATHS_TO_STATES[TaskState.FAILED],
        TaskState.CANCELLED: PATHS_TO_STATES[TaskState.CANCELLED],
    }

    async def wire(self):
        """A broker, approval service and TaskService that revokes on task end."""
        self.store = RevokeDownStore(self.new_database())
        self.h = Harness(
            approvals=self.store,
            clock=Clock(),
            task_activity=PostgresTaskActivity(self.new_database()),
        )
        self.tasks = TaskService(
            self.new_database(), listeners=[self.h.service.revoke_on_task_end]
        )
        self.task_id = await self.new_task()
        self.context = make_context(task_id=self.task_id)
        self.user = principal(SystemRole.USER, U1)
        self.pending = await self.request("pending")
        self.approved = await self.request("approved")
        await self.h.service.approve(self.approved.approval_id, self.user)

    async def request(self, name, approval_id=None):
        return await self.h.broker.request(
            make_call(
                "repo.delete_tree", {"path": f"{ROOT}/{name}"}, context=self.context
            ),
            approval_id=approval_id,
        )

    async def statuses(self):
        return [
            (await self.store.get(d.approval_id)).status
            for d in (self.pending, self.approved)
        ]

    async def rows_of_task(self) -> int:
        async with self.database.engine.begin() as connection:
            return (
                await connection.execute(
                    text("SELECT count(*) FROM tool_approvals WHERE task_id = :t"),
                    {"t": self.task_id},
                )
            ).scalar_one()

    async def test_each_end_revokes_the_approvals_and_the_broker_refuses_the_task(self):
        for state, steps in self.ENDS.items():
            with self.subTest(end=state.value):
                await self.wire()
                await self.drive(self.task_id, steps)
                self.assertEqual(
                    await self.statuses(),
                    [ApprovalStatus.REVOKED, ApprovalStatus.REVOKED],
                )
                for decision, name in (
                    (self.pending, "pending"),
                    (self.approved, "approved"),
                ):
                    used = await self.request(name, decision.approval_id)
                    self.assertEqual(
                        (used.verdict, used.reason),
                        (Verdict.DENY, BrokerReason.TASK_NOT_ACTIVE),
                    )
                rows = [e for e in self.h.sink.events if e.reason == "task_ended"]
                self.assertEqual(len(rows), 2)

    async def test_a_failed_revocation_is_reported_and_the_approval_still_cannot_run(
        self,
    ):
        for state, steps in self.ENDS.items():
            with self.subTest(end=state.value):
                await self.wire()
                self.store.down = True
                with self.assertLogs("paw_backend.tasks.service", "WARNING") as logs:
                    with self.assertLogs("paw_backend.tools.approvals", "ERROR"):
                        await self.drive(self.task_id, steps)
                # reported by type; the message of the driver is not
                text_logged = "\n".join(logs.output)
                self.assertIn("ApprovalRevocationError", text_logged)
                self.assertNotIn("went away", text_logged)
                # the task did end, though, and the approvals are still open
                activity = await self.h.task_activity.check(self.task_id)
                self.assertEqual(activity, TaskActivity.ENDED)
                self.assertEqual(
                    await self.statuses(),
                    [ApprovalStatus.PENDING, ApprovalStatus.APPROVED],
                )
                # ... yet the approved call is refused, and not used up
                before = await self.rows_of_task()
                used = await self.request("approved", self.approved.approval_id)
                self.assertEqual(
                    (used.verdict, used.reason, used.invocation),
                    (Verdict.DENY, BrokerReason.TASK_NOT_ACTIVE, None),
                )
                asked = await self.request("something-else")
                self.assertEqual(asked.reason, BrokerReason.TASK_NOT_ACTIVE)
                self.assertEqual(await self.rows_of_task(), before)  # nothing opened
                self.assertEqual(
                    await self.statuses(),
                    [ApprovalStatus.PENDING, ApprovalStatus.APPROVED],
                )
                # revoking again once the database is back finishes the job
                self.store.down = False
                self.assertEqual(await self.h.service.revoke_task(self.task_id), 2)
                self.assertEqual(
                    await self.statuses(),
                    [ApprovalStatus.REVOKED, ApprovalStatus.REVOKED],
                )

    async def test_a_task_that_is_started_again_needs_new_approvals(self):
        for failing in (False, True):
            with self.subTest(revocation_fails_at_the_end=failing):
                await self.wire()
                self.store.down = failing
                with (
                    self.assertLogs("paw_backend", "WARNING")
                    if failing
                    else contextlib.nullcontext()
                ):
                    await self.drive(self.task_id, self.ENDS[TaskState.FAILED])
                self.store.down = False
                await self.drive(self.task_id, [(C.RETRY, {})])  # failed -> queued
                self.assertEqual(
                    await self.statuses(),
                    [ApprovalStatus.REVOKED, ApprovalStatus.REVOKED],
                )
                used = await self.request("approved", self.approved.approval_id)
                self.assertEqual(
                    (used.verdict, used.reason),
                    (Verdict.DENY, BrokerReason.APPROVAL_REVOKED),
                )
                fresh = await self.request("approved")  # the run asks again
                self.assertEqual(fresh.verdict, Verdict.NEEDS_APPROVAL)
                self.assertNotEqual(fresh.approval_id, self.approved.approval_id)


class EndsTheTaskAfterAnswering:
    """A provider that says the task is alive, and *then* lets it end.

    That is the interleaving of the finding: the broker has read ``ACTIVE``, the
    terminal transition commits, and only then is the approval used. No
    listener has revoked anything yet (the task service of these tests has none).
    """

    def __init__(self, inner, end):
        self.inner, self.end, self.armed = inner, end, False

    async def check(self, task_id):
        activity = await self.inner.check(task_id)
        if self.armed and activity is TaskActivity.ACTIVE:
            self.armed = False
            await self.end()
        return activity


@requires_postgres
class ConsumeRacesWithTaskEndTest(LockWaits, TaskFixture):
    """Using an approval and the end of its task are ordered, never crossed.

    Finding of the review of PR #74: the task-state check and the consumption
    were two operations, so a terminal transition committing between them let the
    consumption win against the revocation that follows the transition, and the
    approved call ran for a task that had already ended. The consumption now
    reads the task row **locked** in its own transaction. Each test drives real
    transactions in a fixed order (a step starts only once the previous one is
    provably blocked on a lock).
    """

    async def wire(self):
        self.store = PostgresApprovalStore(self.new_database())
        self.provider = EndsTheTaskAfterAnswering(
            PostgresTaskActivity(self.new_database()), self.end_the_task
        )
        self.h = Harness(
            approvals=self.store, clock=Clock(), task_activity=self.provider
        )
        # no listener: the revocation after the end has not run (yet)
        self.tasks = TaskService(self.new_database())
        self.task_id = await self.new_task()
        self.context = make_context(task_id=self.task_id)
        opened = await self.request()
        await self.h.service.approve(opened.approval_id, principal(SystemRole.USER, U1))
        self.approval_id = opened.approval_id

    async def end_the_task(self):
        await self.tasks.execute(self.task_id, C.CANCEL, actor=Actor.system())

    async def request(self, approval_id=None):
        return await self.h.broker.request(
            make_call("repo.delete_tree", DELETE, context=self.context),
            approval_id=approval_id,
        )

    async def status(self):
        return (await self.store.get(self.approval_id)).status

    async def test_a_task_that_ends_after_the_check_cannot_have_its_approval_used(
        self,
    ):
        await self.wire()
        self.provider.armed = True
        used = await self.request(self.approval_id)
        # the check said ACTIVE and the task ended before the use: refused, and
        # the approval was not used up
        self.assertEqual(
            (used.verdict, used.reason, used.invocation),
            (Verdict.DENY, BrokerReason.TASK_NOT_ACTIVE, None),
        )
        self.assertEqual(await self.status(), ApprovalStatus.APPROVED)
        self.assertEqual(
            await self.h.task_activity.inner.check(self.task_id), TaskActivity.ENDED
        )
        # the revocation that follows the end still finds it, and revokes it
        self.assertEqual(await self.h.service.revoke_task(self.task_id), 1)
        self.assertEqual(await self.status(), ApprovalStatus.REVOKED)

    async def test_a_use_waits_for_an_end_that_is_in_flight_and_then_refuses(self):
        await self.wire()
        async with self.database.engine.connect() as ending:
            # the terminal transition holds the task row and has not committed
            await ending.execute(
                text("UPDATE tasks SET state = 'cancelled' WHERE id = :id"),
                {"id": self.task_id},
            )
            use = asyncio.create_task(self.request(self.approval_id))
            await self.wait_for_lock_waiters(1, use)
            self.assertFalse(use.done(), "the use did not wait for the end")
            await ending.commit()
        used = await use
        self.assertEqual(
            (used.verdict, used.reason, used.invocation),
            (Verdict.DENY, BrokerReason.TASK_NOT_ACTIVE, None),
        )
        self.assertEqual(await self.status(), ApprovalStatus.APPROVED)
        self.assertEqual(await self.h.service.revoke_task(self.task_id), 1)

    async def test_an_end_waits_for_a_use_that_is_in_flight(self):
        await self.wire()
        async with self.database.engine.connect() as holder:
            # something else holds the approval row, so the use has read the task
            # (and holds its lock) but cannot finish
            await holder.execute(
                text("SELECT id FROM tool_approvals WHERE id = :id FOR UPDATE"),
                {"id": self.approval_id},
            )
            use = asyncio.create_task(self.request(self.approval_id))
            await self.wait_for_lock_waiters(1, use)
            end = asyncio.create_task(self.end_the_task())
            await self.wait_for_lock_waiters(2, use, end)
            self.assertFalse(end.done(), "the end did not wait for the use")
            await holder.rollback()
        used = await use
        await end
        # the use came first, so it stands; the end came second and the
        # revocation after it has nothing left to take away
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.ALLOW, BrokerReason.APPROVAL_CONSUMED)
        )
        self.assertEqual(await self.status(), ApprovalStatus.CONSUMED)
        self.assertEqual(await self.h.service.revoke_task(self.task_id), 0)

    async def approved(self, task_id):
        new = new_approval(task_id=task_id)
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        await self.store.decide(new.approval_id, approver_id=U1, approve=True, now=NOW)
        return new

    async def use(self, new, **arguments):
        return await self.store.consume(
            new.approval_id, binding_of(new), now=NOW, **arguments
        )

    async def test_the_store_checks_the_task_only_when_asked_to_and_reads_it_live(
        self,
    ):
        await self.wire()
        alive = self.task_id
        ended = await self.new_task()
        await self.tasks.execute(ended, C.CANCEL, actor=Actor.system())
        with_flag = {"require_active_task": True}
        # a live task: used
        new = await self.approved(alive)
        self.assertEqual(await self.use(new, **with_flag), ConsumeOutcome.CONSUMED)
        # an ended task, and a task the database does not have: nothing is used
        for label, task_id, outcome in (
            ("ended", ended, ConsumeOutcome.TASK_NOT_ACTIVE),
            ("unknown", uuid.uuid4(), ConsumeOutcome.TASK_UNKNOWN),
        ):
            with self.subTest(label):
                new = await self.approved(task_id)
                self.assertEqual(await self.use(new, **with_flag), outcome)
                self.assertEqual(
                    (await self.store.get(new.approval_id)).status,
                    ApprovalStatus.APPROVED,
                )
                self.assertEqual(
                    [h.kind.value for h in await self.store.history(new.approval_id)],
                    ["requested", "approved"],
                )
        # without the flag the store looks at no task (the contract tests above
        # and every other caller of ``consume``)
        new = await self.approved(ended)
        self.assertEqual(await self.use(new), ConsumeOutcome.CONSUMED)


@requires_postgres
class OpenRacesWithTaskEndTest(LockWaits, TaskFixture):
    """Opening an approval request and the end of its task are ordered, never crossed.

    Finding of the review of PR #74: the broker read the task as ``ACTIVE`` and
    the request was inserted afterwards, in another transaction. A terminal
    transition that committed in between had its revocation (which runs after
    the commit) find nothing, and the request created next was open for an
    ended task: it could be approved, and once the task was started again a
    worker could use it before the listener of that transition revoked it. The
    insert now reads the task row **locked** in its own transaction. Each test
    drives real transactions in a fixed order.
    """

    async def wire(self):
        self.store = PostgresApprovalStore(self.new_database())
        self.provider = EndsTheTaskAfterAnswering(
            PostgresTaskActivity(self.new_database()), self.end_the_task
        )
        self.h = Harness(
            approvals=self.store, clock=Clock(), task_activity=self.provider
        )
        # no listener: the revocation after the end is run by the test
        self.tasks = TaskService(self.new_database())
        self.task_id = await self.new_task()
        self.context = make_context(task_id=self.task_id)

    async def end_the_task(self):
        await self.tasks.execute(self.task_id, C.CANCEL, actor=Actor.system())

    async def request(self, path="build"):
        return await self.h.broker.request(
            make_call(
                "repo.delete_tree", {"path": f"{ROOT}/{path}"}, context=self.context
            )
        )

    async def statuses(self, task_id=None):
        async with self.database.engine.begin() as connection:
            rows = await connection.execute(
                text(
                    "SELECT status FROM tool_approvals WHERE task_id = :id "
                    "ORDER BY created_at, id"
                ),
                {"id": self.task_id if task_id is None else task_id},
            )
            return [ApprovalStatus(row[0]) for row in rows]

    async def test_a_task_that_ends_after_the_check_gets_no_request(self):
        await self.wire()
        self.provider.armed = True
        asked = await self.request()
        # the check said ACTIVE and the task ended before the insert: refused,
        # and nothing was created for the ended task
        self.assertEqual(
            (asked.verdict, asked.reason, asked.approval_id, asked.invocation),
            (Verdict.DENY, BrokerReason.TASK_NOT_ACTIVE, None, None),
        )
        self.assertEqual(
            await self.provider.inner.check(self.task_id), TaskActivity.ENDED
        )
        self.assertEqual(await self.statuses(), [])
        # the revocation that follows the end has nothing to take away, and
        # there is nothing for a task that is started again to pick up
        self.assertEqual(await self.h.service.revoke_task(self.task_id), 0)
        await self.drive(self.task_id, [(C.RESTART, {})])
        self.assertEqual(await self.statuses(), [])

    async def test_a_request_waits_for_an_end_that_is_in_flight_and_then_refuses(self):
        await self.wire()
        async with self.database.engine.connect() as ending:
            # the terminal transition holds the task row and has not committed
            await ending.execute(
                text("UPDATE tasks SET state = 'cancelled' WHERE id = :id"),
                {"id": self.task_id},
            )
            asked = asyncio.create_task(self.request())
            # (the broker read ``ACTIVE``: the end is not committed yet)
            await self.wait_for_lock_waiters(1, asked)
            self.assertFalse(asked.done(), "the request did not wait for the end")
            await ending.commit()
        decision = await asked
        self.assertEqual(
            (decision.verdict, decision.reason, decision.approval_id),
            (Verdict.DENY, BrokerReason.TASK_NOT_ACTIVE, None),
        )
        self.assertEqual(await self.statuses(), [])

    async def test_an_end_waits_for_a_request_in_flight_and_its_revocation_finds_it(
        self,
    ):
        await self.wire()
        async with self.database.engine.connect() as holder:
            # a lock on the table blocks the insert of the request, which has
            # by then read the task (and holds its lock)
            await holder.execute(
                text("LOCK TABLE tool_approvals IN SHARE ROW EXCLUSIVE MODE")
            )
            asked = asyncio.create_task(self.request())
            await self.wait_for_lock_waiters(1, asked)
            end = asyncio.create_task(self.end_the_task())
            await self.wait_for_lock_waiters(2, asked, end)
            self.assertFalse(end.done(), "the end did not wait for the request")
            await holder.rollback()
        decision = await asked
        await end
        # the request came first, so it stands; the end came second and the
        # revocation after it finds the request and takes it away
        self.assertEqual(
            (decision.verdict, decision.reason),
            (Verdict.NEEDS_APPROVAL, BrokerReason.APPROVAL_REQUIRED),
        )
        self.assertEqual(await self.statuses(), [ApprovalStatus.PENDING])
        self.assertEqual(await self.h.service.revoke_task(self.task_id), 1)
        self.assertEqual(await self.statuses(), [ApprovalStatus.REVOKED])

    async def test_the_store_checks_the_task_only_when_asked_to_and_reads_it_live(
        self,
    ):
        await self.wire()
        alive = self.task_id
        ended = await self.new_task()
        await self.tasks.execute(ended, C.CANCEL, actor=Actor.system())
        with_flag = {"require_active_task": True}

        async def open_for(task_id, **arguments):
            new = new_approval(task_id=task_id)
            opened = await self.store.open_request(
                new, now=NOW, limits=LIMITS, **arguments
            )
            return new, opened

        # a live task: created
        first, opened = await open_for(alive, **with_flag)
        self.assertEqual(opened.outcome, OpenOutcome.CREATED)
        # an ended task, and a task the database does not have: nothing is
        # created, and nothing is written for it
        for label, task_id, outcome in (
            ("ended", ended, OpenOutcome.TASK_NOT_ACTIVE),
            ("unknown", uuid.uuid4(), OpenOutcome.TASK_UNKNOWN),
        ):
            with self.subTest(label):
                new, opened = await open_for(task_id, **with_flag)
                self.assertEqual((opened.outcome, opened.record), (outcome, None))
                self.assertIsNone(await self.store.get(new.approval_id))
                self.assertEqual(await self.store.history(new.approval_id), [])
                self.assertEqual(await self.statuses(task_id), [])
        # the request that was open when its task ended (the revocation failed)
        # is not handed out again either ...
        await self.tasks.execute(alive, C.CANCEL, actor=Actor.system())
        again = new_approval(task_id=alive, call_hash=first.call_hash)
        handed = await self.store.open_request(
            again, now=NOW, limits=LIMITS, **with_flag
        )
        self.assertEqual(
            (handed.outcome, handed.record), (OpenOutcome.TASK_NOT_ACTIVE, None)
        )
        # ... and without the flag the store looks at no task (the contract
        # tests and every other caller of ``open_request``)
        handed = await self.store.open_request(again, now=NOW, limits=LIMITS)
        self.assertEqual(handed.outcome, OpenOutcome.EXISTING)
        new, opened = await open_for(ended)
        self.assertEqual(opened.outcome, OpenOutcome.CREATED)


@requires_postgres
class RevocationDeadlineTest(TaskFixture):
    """A revocation that meets a stalled statement returns, and the task still ends.

    Finding of the review of PR #74: ``revoke_task`` ran on a pooled session with
    no deadline, and ``TaskService`` awaits its listeners, so a database that
    accepted the connection but stalled the statement held up every cancel,
    complete and retry request after the transition had committed. The statement
    now runs on an abortable connection (``Database.execute_abortable``: the
    socket is shut down at the deadline). Here the stall is a row lock that
    another transaction holds, which makes the UPDATE wait exactly like a stalled
    statement would.
    """

    LIMIT = 0.3  # the deadline of the revocation
    GUARD = 10  # far above it: only reached by a call that ignores the deadline

    async def wire(self):
        self.store = PostgresApprovalStore(
            self.new_database(), revoke_timeout_seconds=self.LIMIT
        )
        self.h = Harness(approvals=self.store, clock=Clock())
        self.h.service._timeout_seconds = self.LIMIT
        self.tasks = TaskService(
            self.new_database(), listeners=[self.h.service.revoke_on_task_end]
        )
        self.task_id = await self.new_task()
        self.context = make_context(task_id=self.task_id)
        opened = await self.h.broker.request(
            make_call("repo.delete_tree", DELETE, context=self.context)
        )
        self.approval_id = opened.approval_id

    async def status(self):
        return (await self.store.get(self.approval_id)).status

    @contextlib.asynccontextmanager
    async def stalled(self):
        """Hold the approval row, as a statement that is stalled would."""
        async with self.database.engine.connect() as holder:
            await holder.execute(
                text("SELECT id FROM tool_approvals WHERE id = :id FOR UPDATE"),
                {"id": self.approval_id},
            )
            try:
                yield
            finally:
                await holder.rollback()

    async def test_the_store_gives_up_at_its_deadline(self):
        await self.wire()
        started = asyncio.get_running_loop().time()
        async with self.stalled():
            with self.assertRaises(TimeoutError):
                async with asyncio.timeout(self.GUARD):
                    await self.store.revoke_task(self.task_id, now=NOW)
            elapsed = asyncio.get_running_loop().time() - started
        self.assertLess(elapsed, self.GUARD / 2)
        # revoking again, once the database answers, finishes the job (idempotent);
        # the statement that was abandoned may also have finished it by then
        await self.store.revoke_task(self.task_id, now=NOW)
        self.assertEqual(await self.status(), ApprovalStatus.REVOKED)

    async def test_a_task_end_returns_although_its_revocation_is_stalled(self):
        await self.wire()
        started = asyncio.get_running_loop().time()
        async with self.stalled():
            with self.assertLogs("paw_backend", "WARNING") as logs:
                async with asyncio.timeout(self.GUARD):
                    await self.tasks.execute(
                        self.task_id, C.CANCEL, actor=Actor.user(U1)
                    )
            elapsed = asyncio.get_running_loop().time() - started
        self.assertLess(elapsed, self.GUARD / 2)
        # the transition is committed, the failure is reported by type ...
        text_logged = "\n".join(logs.output)
        self.assertIn("ApprovalRevocationError", text_logged)
        # ... and the broker refuses the approval of the ended task meanwhile
        h = Harness(
            approvals=self.store,
            clock=self.h.clock,
            task_activity=PostgresTaskActivity(self.new_database()),
        )
        used = await h.broker.request(
            make_call("repo.delete_tree", DELETE, context=self.context),
            approval_id=self.approval_id,
        )
        self.assertEqual(used.reason, BrokerReason.TASK_NOT_ACTIVE)
        # revoking again, once the database answers, finishes the job
        await self.h.service.revoke_task(self.task_id)
        self.assertEqual(await self.status(), ApprovalStatus.REVOKED)

    async def test_the_revocation_is_one_atomic_statement_with_its_history(self):
        await self.wire()
        second = await self.h.broker.request(
            make_call(
                "repo.delete_tree", {"path": f"{ROOT}/other"}, context=self.context
            )
        )
        revoked = await self.store.revoke_task(self.task_id, now=NOW)
        self.assertEqual(
            sorted(revoked), sorted([self.approval_id, second.approval_id])
        )
        for approval_id in revoked:
            history = await self.store.history(approval_id)
            self.assertEqual(
                [(h.kind.value, h.actor_user_id) for h in history],
                [("requested", None), ("revoked", None)],
            )
        self.assertEqual(await self.store.revoke_task(self.task_id, now=NOW), [])


class SlowDatabase:
    """Answers each ``fetch_abortable`` from a script after ``step`` seconds, and
    records the time limit each statement was given (it does not enforce it)."""

    def __init__(self, script, step=0.0):
        self.script, self.step, self.timeouts = list(script), step, []

    async def fetch_abortable(self, sql, params=None, *, timeout_seconds=None):
        self.timeouts.append(timeout_seconds)
        await asyncio.sleep(self.step)
        return self.script.pop(0)

    async def execute_abortable(self, sql, params=None, *, timeout_seconds=None):
        await self.fetch_abortable(sql, params, timeout_seconds=timeout_seconds)


def approval_row(approval_id, **overrides):
    """A row of ``tool_approvals`` as the abortable connection returns it."""
    values = {
        "id": approval_id,
        "task_id": TASK,
        "project_id": P1,
        "agent_id": AGENT,
        "requester_user_id": U1,
        "tool": "repo.delete_tree",
        "level": "approval",
        "call_hash": "0" * 64,
        "targets": [{"kind": "path", "value": f"{ROOT}/x"}],
        "summary": [{"name": "path", "kind": "path", "value": f"{ROOT}/x"}],
        "status": "pending",
        "created_at": NOW - HOUR,
        "expires_at": NOW + HOUR,
        "approver_id": None,
        "decided_at": None,
        "consumed_at": None,
        "step_up_verified": False,
        "revoked_at": None,
        "revoked_by": None,
    }
    values.update(overrides)
    return tuple(values.values())


class DecisionStatementsShareOneDeadlineTest(unittest.IsolatedAsyncioTestCase):
    """The statements of one ``get`` / ``decide`` / ``revoke`` share one limit.

    ``decide`` and ``revoke`` are one statement, plus (only when it matched
    nothing) the read that explains why, plus for an expired approval the marking
    as expired: each was given the full limit, so a call could take three times
    it. They now get what is left of ONE limit, started with the call.
    """

    LIMIT = 1.0

    def store(self, script, step, limit=LIMIT):
        database = SlowDatabase(script, step)
        return database, PostgresApprovalStore(database, decision_timeout_seconds=limit)

    async def test_each_statement_gets_what_is_left_of_one_limit(self):
        approval_id = uuid.uuid4()
        expired = approval_row(approval_id, expires_at=NOW - timedelta(minutes=1))
        cases = {
            # the update matches nothing, the read finds the approval expired,
            # and it is marked as expired: three statements
            "decide": lambda store: store.decide(
                approval_id, approver_id=U1, approve=True, now=NOW
            ),
            # the update matches nothing and the read explains why: two
            "revoke": lambda store: store.revoke(approval_id, actor_id=U1, now=NOW),
        }
        for name, call in cases.items():
            script = [[], [expired], []] if name == "decide" else [[], [expired]]
            with self.subTest(call=name):
                database, store = self.store(script, step=0.2)
                result = await call(store)
                self.assertEqual(
                    getattr(result, "outcome", result).value,
                    "expired" if name == "decide" else "not_open",
                )
                self.assertEqual(len(database.timeouts), len(script))
                first, *rest = database.timeouts
                self.assertLessEqual(first, self.LIMIT)
                self.assertGreater(first, self.LIMIT - 0.1)
                # every statement gets less than the one before by what that one
                # took (0.2 s), not the limit again
                for before, after in zip(database.timeouts, rest, strict=False):
                    self.assertLess(after, before - 0.15)

    async def test_a_limit_that_the_first_statement_used_up_starts_no_second(self):
        approval_id = uuid.uuid4()
        # the first statement takes longer than the limit (a stand-in that does
        # not enforce it, as the real connection would)
        for name, call in {
            "decide": lambda store: store.decide(
                approval_id, approver_id=U1, approve=True, now=NOW
            ),
            "revoke": lambda store: store.revoke(approval_id, actor_id=U1, now=NOW),
        }.items():
            with self.subTest(call=name):
                database, store = self.store([[], []], step=0.4, limit=0.3)
                with self.assertRaises(TimeoutError):
                    await call(store)
                self.assertEqual(len(database.timeouts), 1)  # the read never started

    async def test_the_limit_must_be_positive(self):
        for value in (0, -1, float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    PostgresApprovalStore(
                        SlowDatabase([]), decision_timeout_seconds=value
                    )


def stalled_settings(server):
    return make_settings(
        database_url=f"postgresql://paw:pw@127.0.0.1:{server.port}/paw"
    )


class StalledServerTest(unittest.IsolatedAsyncioTestCase):
    """A PostgreSQL that accepts the connection and then answers nothing.

    Finding of the third review of PR #74: the store calls of a human's decision
    ran on pooled sessions with no deadline, and cancelling a query on a pooled
    session waits for the server to confirm the cancel (about ten seconds with
    this server, which never does), so not even ``asyncio.timeout`` bounded them.
    They now run on abortable connections whose socket is shut down at the
    deadline. No PostgreSQL is needed for these tests.
    """

    LIMIT = 0.3
    FAR = 5  # far above the limit, far below the ten seconds of a pooled cancel

    async def wire(self, server):
        database = Database(stalled_settings(server))
        self.addAsyncCleanup(database.dispose)
        return PostgresApprovalStore(database, decision_timeout_seconds=self.LIMIT)

    async def test_the_store_calls_of_a_decision_give_up_at_their_deadline(self):
        approval_id = uuid.uuid4()
        async with HangingPostgres() as server:
            store = await self.wire(server)
            for name, call in {
                "get": lambda: store.get(approval_id),
                "decide": lambda: store.decide(
                    approval_id, approver_id=U1, approve=True, now=NOW
                ),
                "revoke": lambda: store.revoke(approval_id, actor_id=U1, now=NOW),
            }.items():
                with self.subTest(call=name):
                    started = time.monotonic()
                    with self.assertRaises(TimeoutError):
                        async with asyncio.timeout(20):
                            await call()
                    self.assertLess(time.monotonic() - started, self.FAR)

    async def test_a_decision_returns_unavailable_when_the_server_stalls(self):
        approval_id = uuid.uuid4()
        async with HangingPostgres() as server:
            store = await self.wire(server)
            sink = InMemoryAuditSink()
            service = ApprovalService(
                store,
                sink,
                step_up=StepUp(True),
                clock=Clock(),
                timeout_seconds=self.LIMIT,
            )
            user = principal(SystemRole.USER, U1)
            for operation in ("approve", "reject", "revoke"):
                with self.subTest(operation=operation):
                    started = time.monotonic()
                    with self.assertLogs("paw_backend", "ERROR") as logs:
                        result = await asyncio.wait_for(
                            getattr(service, operation)(approval_id, user), 20
                        )
                    self.assertLess(time.monotonic() - started, self.FAR)
                    self.assertEqual(
                        (result.outcome, result.approval_id),
                        (ApprovalOutcome.UNAVAILABLE, approval_id),
                    )
                    self.assertIn("TimeoutError", "\n".join(logs.output))
            self.assertEqual(sink.events, [])  # nothing to attribute a row to


@requires_postgres
class DecisionDeadlineTest(LockWaits, PostgresTestCase):
    """A decision that meets a stalled statement returns, and stays atomic.

    Here the stall is a row lock that another transaction holds, which makes the
    UPDATE wait exactly like a stalled statement would. The statement that was
    abandoned may still finish once the lock is released (the server does not
    notice the closed socket before it has something to send), so what is
    asserted is not "nothing changed" but that the change is all or nothing:
    the approval and its history row agree, and repeating the call says which.
    """

    LIMIT = 0.5
    GUARD = 10  # far above the limit: only reached by a call that ignores it

    async def wire(self):
        self.store = PostgresApprovalStore(
            self.new_database(), decision_timeout_seconds=self.LIMIT
        )
        self.sink = InMemoryAuditSink()
        self.service = ApprovalService(
            self.store,
            self.sink,
            step_up=StepUp(True),
            clock=Clock(),
            timeout_seconds=self.LIMIT,
        )
        self.new = new_approval()
        await self.store.open_request(self.new, now=NOW, limits=LIMITS)
        self.user = principal(SystemRole.USER, U1)

    @contextlib.asynccontextmanager
    async def stalled(self):
        """Hold the approval row, as a statement that is stalled would."""
        async with self.database.engine.connect() as holder:
            await holder.execute(
                text("SELECT id FROM tool_approvals WHERE id = :id FOR UPDATE"),
                {"id": self.new.approval_id},
            )
            try:
                yield
            finally:
                await holder.rollback()

    async def settled(self):
        """Wait until the abandoned statement has left the database."""
        async with asyncio.timeout(self.GUARD):
            while True:
                if await self.lock_waiters() == 0:
                    return
                await asyncio.sleep(0.02)

    async def state(self):
        record = await self.store.get(self.new.approval_id)
        kinds = [h.kind.value for h in await self.store.history(self.new.approval_id)]
        return record.status, kinds

    async def test_a_decision_meets_a_stalled_statement_in_time(self):
        expected = {
            # what the abandoned statement leaves, whichever way it ends: the
            # approval and its history row agree
            "approve": {
                (ApprovalStatus.PENDING, ("requested",)),
                (ApprovalStatus.APPROVED, ("requested", "approved")),
            },
            "reject": {
                (ApprovalStatus.PENDING, ("requested",)),
                (ApprovalStatus.REJECTED, ("requested", "rejected")),
            },
            "revoke": {
                (ApprovalStatus.PENDING, ("requested",)),
                (ApprovalStatus.REVOKED, ("requested", "revoked")),
            },
        }
        for operation, allowed in expected.items():
            with self.subTest(operation=operation):
                await self.wire()
                started = time.monotonic()
                async with self.stalled():
                    with self.assertLogs("paw_backend", "ERROR") as logs:
                        async with asyncio.timeout(self.GUARD):
                            result = await getattr(self.service, operation)(
                                self.new.approval_id, self.user
                            )
                    elapsed = time.monotonic() - started
                self.assertLess(elapsed, self.GUARD / 2)
                self.assertEqual(result.outcome, ApprovalOutcome.UNAVAILABLE)
                self.assertIn("TimeoutError", "\n".join(logs.output))
                self.assertEqual(
                    [(e.action, e.decision, e.reason) for e in self.sink.events],
                    [(f"tool.approval.{operation}", "deny", "unavailable")],
                )
                await self.settled()
                status, kinds = await self.state()
                self.assertIn((status, tuple(kinds)), allowed)
                # repeating the call says which it was, and the pool still works
                if status is ApprovalStatus.PENDING:
                    outcome = {
                        "approve": ApprovalOutcome.APPROVED,
                        "reject": ApprovalOutcome.REJECTED,
                        "revoke": ApprovalOutcome.REVOKED,
                    }[operation]
                else:
                    outcome = (
                        ApprovalOutcome.NOT_OPEN
                        if operation == "revoke"
                        else ApprovalOutcome.NOT_PENDING
                    )
                again = await getattr(self.service, operation)(
                    self.new.approval_id, self.user
                )
                self.assertEqual(again.outcome, outcome)

    async def test_a_decision_returns_the_stored_record_and_writes_its_history(self):
        # (the contract tests check every outcome; this one reads back what the
        # single statement wrote and returned)
        await self.wire()
        for approve in (True, False):
            new = new_approval()
            await self.store.open_request(new, now=NOW, limits=LIMITS)
            decided = await self.store.decide(
                new.approval_id, approver_id=U1, approve=approve, now=NOW
            )
            self.assertEqual(decided.outcome, DecideOutcome.DECIDED)
            self.assertEqual(
                (
                    decided.record.status,
                    decided.record.approver_id,
                    decided.record.decided_at,
                ),
                (
                    ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED,
                    U1,
                    NOW,
                ),
            )
            history = await self.store.history(new.approval_id)
            self.assertEqual(
                [(h.kind.value, h.actor_user_id) for h in history],
                [("requested", None), ("approved" if approve else "rejected", U1)],
            )
