"""The approval store on a real PostgreSQL.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set (see test_postgres_integration).

The store contract is the one the in-memory double passes; on top of it these
tests check what only a database can: the constraints that make an approval
trustworthy even against a bug in the application, the append-only history and
the single winner among concurrent callers on separate connection pools.
"""

import asyncio
import hashlib
import unittest
import uuid
from datetime import timedelta

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from paw_backend.authz import SystemRole
from paw_backend.tools import (
    ApprovalLevel,
    ApprovalOutcome,
    ApprovalService,
    BrokerReason,
    ConsumeOutcome,
    OpenLimits,
    OpenOutcome,
    PostgresApprovalStore,
    Verdict,
)

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
        decisions = await asyncio.gather(
            *(
                harnesses[i % 3].broker.request(
                    make_call(
                        "repo.delete_tree",
                        {"path": f"{ROOT}/cap-{i}"},
                        context=make_context(task_id=uuid.uuid4()),
                    )
                )
                for i in range(1)
            )
        )
        self.assertEqual(decisions[0].verdict, Verdict.NEEDS_APPROVAL)
        task_context = make_context(task_id=uuid.uuid4())
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

    async def test_the_state_survives_a_restart(self):
        # "Restart" = every object is rebuilt from the database alone.
        clock = Clock()
        first = Harness(
            approvals=PostgresApprovalStore(self.new_database()), clock=clock
        )
        pending = await first.broker.request(make_call("repo.delete_tree", DELETE))
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
            make_call("repo.delete_tree", DELETE), approval_id=pending.approval_id
        )
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.ALLOW, BrokerReason.APPROVAL_CONSUMED)
        )
        replay = await third.broker.request(
            make_call("repo.delete_tree", DELETE), approval_id=pending.approval_id
        )
        self.assertEqual(replay.reason, BrokerReason.APPROVAL_ALREADY_USED)

    async def test_the_broker_flow_races_to_a_single_execution(self):
        stores = [PostgresApprovalStore(self.new_database()) for _ in range(3)]
        clock = Clock()
        harnesses = [Harness(approvals=store, clock=clock) for store in stores]
        call = make_call("repo.delete_tree", {"path": f"{ROOT}/race-{uuid.uuid4()}"})
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
