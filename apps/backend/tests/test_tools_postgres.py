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

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from paw_backend.authz import SystemRole
from paw_backend.tools import (
    ApprovalOutcome,
    ApprovalService,
    BrokerReason,
    PostgresApprovalStore,
    Verdict,
)

from .task_support import migrate, new_database, requires_postgres
from .tools_store_contract import HOUR, StoreContract, binding_of, new_approval
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
    principal,
)

DELETE = {"path": f"{ROOT}/build"}


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
                    # TRUNCATE fires no row triggers, so the append-only history
                    # of the throwaway test database can still be emptied.
                    await connection.execute(text("TRUNCATE tool_approvals CASCADE"))
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
    "status": "pending",
    "created_at": NOW,
    "expires_at": NOW + HOUR,
    "approver_id": None,
    "decided_at": None,
    "consumed_at": None,
}


@requires_postgres
class ConstraintTest(PostgresTestCase):
    """Rows the application must never write are refused by the database too."""

    async def insert(self, **overrides):
        row = dict(ROW)
        row["id"] = uuid.uuid4()
        row["call_hash"] = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        row.update(overrides)
        columns = ", ".join(row)
        values = ", ".join(f":{name}" for name in row)
        sql = f"INSERT INTO tool_approvals ({columns}) VALUES ({values})"
        sql = sql.replace(":targets", "CAST(:targets AS jsonb)")
        await self.execute(sql, **row)
        return row["id"]

    async def refused(self, constraint: str, **overrides):
        with self.assertRaises(IntegrityError) as caught:
            await self.insert(**overrides)
        self.assertIn(constraint, str(caught.exception))

    async def test_a_valid_row_is_accepted(self):
        await self.insert()
        await self.insert(
            status="approved", approver_id=U1, decided_at=NOW, level="strong_approval"
        )
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
            }
            row.update(overrides)
            await self.execute(
                "INSERT INTO tool_approval_events "
                "(approval_id, kind, actor_user_id, agent_id, created_at) "
                "VALUES (:approval_id, :kind, :actor_user_id, :agent_id, :created_at)",
                **row,
            )

        await event()
        await event(kind="approved", actor_user_id=U1, agent_id=None)
        for overrides, constraint in (
            (
                {"kind": "granted", "agent_id": None},
                "ck_tool_approval_events_kind_valid",
            ),
            ({"agent_id": None}, "ck_tool_approval_events_agent_matches_kind"),
            (
                {"kind": "approved", "agent_id": None},
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
            "(approval_id, kind, agent_id, created_at) "
            "VALUES (:a, 'requested', :agent, :at)",
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
class CrossProcessTest(PostgresTestCase):
    """Several engines (as several backend processes) race for one approval."""

    async def stores(self, count=4):
        return [PostgresApprovalStore(self.new_database()) for _ in range(count)]

    async def test_concurrent_approvals_from_separate_pools_have_one_winner(self):
        stores = await self.stores()
        new = new_approval()
        await stores[0].open_request(new, now=NOW)
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
        await stores[0].open_request(new, now=NOW)
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
                    new_approval(call_hash=first.call_hash), now=NOW
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
        await store.open_request(first, now=NOW)
        later = NOW + HOUR + timedelta(seconds=1)
        opened = await store.open_request(
            new_approval(call_hash=first.call_hash, expires_at=later + HOUR), now=later
        )
        self.assertTrue(opened.created)


if __name__ == "__main__":
    unittest.main()
