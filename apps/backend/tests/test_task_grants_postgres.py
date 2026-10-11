"""Task-scoped approval grants (Decision 0085) on PostgreSQL: the store of
migration 0193 with the real approval store, task rows and task service.

* granting approves the approval and creates the grant in one transaction;
* the broker uses a grant only while the task can act in the grant's run (a
  task that ended, or was started again, is refused even when the listener that
  revokes the grants did not run);
* the cap of active grants holds under concurrency;
* revoking and the end of the task stop later uses;
* the guards of the tables: a grant cannot be deleted or have what it covers
  changed, and its uses are append-only.
"""

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from paw_backend.authz import SystemRole
from paw_backend.tasks import Actor, TaskCommand, TaskService
from paw_backend.tools import (
    ApprovalOutcome,
    ApprovalStatus,
    BrokerReason,
    PostgresTaskActivity,
    PostgresTaskGrantStore,
    TaskGrantStatus,
    Verdict,
)
from paw_backend.tools.approval_store import PostgresApprovalStore

from .gate_support import ALWAYS_ACTIVE
from .task_support import requires_postgres
from .test_tools_postgres import TaskFixture
from .tools_support import Clock, Harness, make_call, make_context, principal

R = BrokerReason
FETCH_DOCS = {"url": "https://docs.example.org/guide"}
INSTALL = {"package": "ripgrep"}


@requires_postgres
class PostgresTaskGrantTest(TaskFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.grants = PostgresTaskGrantStore(self.new_database())
        self.h = Harness(
            approvals=PostgresApprovalStore(self.new_database()),
            grants=self.grants,
            clock=Clock(),
            task_activity=PostgresTaskActivity(self.new_database()),
        )
        # The task service revokes the approvals and grants of a task that ends.
        self.tasks = TaskService(
            self.new_database(),
            listeners=[self.h.service.revoke_on_task_end],
            project_gate=ALWAYS_ACTIVE,
        )
        self.task_id = await self.new_task()
        await self.tasks.execute(self.task_id, TaskCommand.START, actor=Actor.system())
        self.context = make_context(task_id=self.task_id)
        self.person = principal(SystemRole.USER, self.context.delegator_id)

    async def request(self, tool="web.fetch", arguments=FETCH_DOCS, context=None):
        return await self.h.broker.request(
            make_call(tool, arguments, context=context or self.context)
        )

    async def grant(self, tool="web.fetch", arguments=FETCH_DOCS):
        asked = await self.request(tool, arguments)
        self.assertEqual(asked.verdict, Verdict.NEEDS_APPROVAL)
        result = await self.h.service.approve_for_task(asked.approval_id, self.person)
        self.assertEqual(result.outcome, ApprovalOutcome.APPROVED_FOR_TASK)
        return asked, result.grant_id

    async def scalar(self, sql: str, **parameters):
        async with self.database.engine.begin() as connection:
            return (await connection.execute(text(sql), parameters)).scalar()

    async def test_granting_approves_and_later_calls_run_and_are_recorded(self):
        asked, grant_id = await self.grant()
        record = await self.h.approvals.get(asked.approval_id)
        self.assertEqual(record.status, ApprovalStatus.APPROVED)
        stored = await self.grants.get(grant_id)
        self.assertEqual(
            (stored.status, stored.task_id, stored.tool, stored.approval_id),
            (TaskGrantStatus.ACTIVE, self.task_id, "web.fetch", asked.approval_id),
        )
        self.assertEqual(stored.pattern, record.grant_pattern)

        for url in (FETCH_DOCS["url"], FETCH_DOCS["url"] + "/install"):
            decision = await self.request(arguments={"url": url})
            self.assertEqual(decision.reason, R.TASK_GRANT_APPLIED)
            self.assertEqual(decision.grant_id, grant_id)
        wider = await self.request(arguments={"url": "https://docs.example.org/"})
        self.assertEqual(wider.verdict, Verdict.NEEDS_APPROVAL)

        self.assertEqual((await self.grants.get(grant_id)).uses, 2)
        self.assertEqual(
            [
                g.grant_id
                for g in await self.grants.list_task(self.task_id, self.person.user_id)
            ],
            [grant_id],
        )
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM tool_task_grant_uses WHERE grant_id = :g",
                g=grant_id,
            ),
            2,
        )

    async def test_a_revoked_grant_is_not_used(self):
        _asked, grant_id = await self.grant()
        revoked = await self.h.service.revoke_grant(grant_id, self.person)
        self.assertEqual(revoked.outcome, ApprovalOutcome.REVOKED)
        again = await self.h.service.revoke_grant(grant_id, self.person)
        self.assertEqual(again.outcome, ApprovalOutcome.NOT_OPEN)
        decision = await self.request()
        self.assertEqual(decision.verdict, Verdict.NEEDS_APPROVAL)
        self.assertEqual((await self.grants.get(grant_id)).uses, 0)

    async def test_the_end_of_the_task_revokes_the_grants(self):
        _asked, grant_id = await self.grant()
        await self.tasks.execute(self.task_id, TaskCommand.CANCEL, actor=Actor.system())
        stored = await self.grants.get(grant_id)
        self.assertEqual(stored.status, TaskGrantStatus.REVOKED)
        self.assertIsNone(stored.revoked_by)
        decision = await self.request()
        self.assertEqual(decision.verdict, Verdict.DENY)

    async def test_a_task_started_again_uses_no_grant_of_the_earlier_run(self):
        _asked, grant_id = await self.grant()
        # A task service WITHOUT the listener: the grant stays active, and the
        # store's own check of the task's run is what refuses it.
        bare = TaskService(self.new_database(), project_gate=ALWAYS_ACTIVE)
        await bare.execute(self.task_id, TaskCommand.CANCEL, actor=Actor.system())
        ended = await self.request()
        self.assertEqual(
            (ended.verdict, ended.reason), (Verdict.DENY, R.TASK_NOT_ACTIVE)
        )
        await bare.execute(self.task_id, TaskCommand.RESTART, actor=Actor.system())
        self.assertEqual(
            (await self.grants.get(grant_id)).status, TaskGrantStatus.ACTIVE
        )
        old_run = await self.request()
        self.assertEqual(old_run.verdict, Verdict.DENY)
        self.assertEqual((await self.grants.get(grant_id)).uses, 0)

    async def test_the_cap_of_active_grants_holds_under_concurrency(self):
        self.h.service._max_task_grants = 2
        asked = [
            await self.request(arguments={"url": f"https://docs.example.org/p{n}"})
            for n in range(5)
        ]
        results = await asyncio.gather(
            *(
                self.h.service.approve_for_task(item.approval_id, self.person)
                for item in asked
            )
        )
        outcomes = sorted(result.outcome.value for result in results)
        self.assertEqual(
            outcomes,
            ["approved_for_task"] * 2 + ["grant_limit_reached"] * 3,
        )
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM tool_task_grants WHERE task_id = :t"
                " AND status = 'active'",
                t=self.task_id,
            ),
            2,
        )
        # The refused approvals stay pending.
        statuses = [
            (await self.h.approvals.get(item.approval_id)).status for item in asked
        ]
        self.assertEqual(statuses.count(ApprovalStatus.PENDING), 3)

    async def test_the_tables_are_guarded(self):
        _asked, grant_id = await self.grant()
        await self.request()
        for sql in (
            "DELETE FROM tool_task_grants WHERE id = :g",
            "UPDATE tool_task_grants SET tool = 'host.run' WHERE id = :g",
            "UPDATE tool_task_grants SET pattern = '{}'::jsonb WHERE id = :g",
            "UPDATE tool_task_grants SET task_attempt = 2 WHERE id = :g",
            "DELETE FROM tool_task_grant_uses WHERE grant_id = :g",
            "UPDATE tool_task_grant_uses SET call_hash = repeat('0', 64)"
            " WHERE grant_id = :g",
        ):
            with self.subTest(sql=sql), self.assertRaises(DBAPIError):
                await self.execute(sql, g=grant_id)
        # The approval's pattern is part of what identifies the call.
        with self.assertRaises(DBAPIError):
            await self.execute(
                "UPDATE tool_approvals SET grant_pattern = NULL WHERE id ="
                " (SELECT approval_id FROM tool_task_grants WHERE id = :g)",
                g=grant_id,
            )
        # A revoked grant cannot become active again.
        await self.h.service.revoke_grant(grant_id, self.person)
        with self.assertRaises(DBAPIError):
            await self.execute(
                "UPDATE tool_task_grants SET status = 'active', revoked_at = NULL,"
                " revoked_by = NULL WHERE id = :g",
                g=grant_id,
            )

    async def test_another_person_cannot_grant_or_revoke(self):
        asked = await self.request()
        other = principal(SystemRole.USER, uuid.uuid4())
        result = await self.h.service.approve_for_task(asked.approval_id, other)
        self.assertEqual(result.outcome, ApprovalOutcome.NOT_FOUND)
        self.assertEqual(
            (await self.h.approvals.get(asked.approval_id)).status,
            ApprovalStatus.PENDING,
        )
        _asked, grant_id = await self.grant(
            arguments={"url": "https://docs.example.org/x"}
        )
        refused = await self.h.service.revoke_grant(grant_id, other)
        self.assertEqual(refused.outcome, ApprovalOutcome.NOT_FOUND)
        admin = principal(SystemRole.ADMIN, uuid.uuid4())
        allowed = await self.h.service.revoke_grant(grant_id, admin)
        self.assertEqual(allowed.outcome, ApprovalOutcome.REVOKED)
        self.assertEqual((await self.grants.get(grant_id)).revoked_by, admin.user_id)
