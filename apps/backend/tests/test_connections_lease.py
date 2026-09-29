"""The queue lease of the worker that asks for a shared connection call.

Issue #153, Decision 0057: ``ConnectionService.execute`` asks the
service's ``LeaseVerifier`` whether ``context.lease`` is still valid, as the Tool
Broker does for a tool call (Decision 0046). A worker whose lease was lost,
expired or taken over is refused before the call starts (``lease_lost``), and so
is every call whose answer cannot be had (``lease_unavailable``): fail closed. A
refused call resolves no credential, reaches no provider and writes no usage row
(it spends no quota); the refusal is audited like the others (``connection.use``,
denied). The check is a read at one instant: a call that passed it and is running
when the lease is lost is settled and charged as usual (the tokens were spent).
"""

import asyncio
import logging
from dataclasses import replace

from sqlalchemy import text

from paw_backend.authz import Principal, SystemRole
from paw_backend.connections import (
    AdapterRegistry,
    ConnectionKind,
    ConnectionPermissionDeniedError,
    ConnectionUnavailableError,
    QuotaExceededError,
    RefusalReason,
    TaskBudgetError,
    TaskNotUsableError,
    UsageStatus,
)
from paw_backend.db import Database
from paw_backend.orchestrator import QueueLeaseVerifier
from paw_backend.tasks.queueing import (
    BudgetKind,
    BudgetPreset,
    BudgetTracker,
    QueueLease,
    TaskQueue,
)
from paw_backend.tools import LeaseStatus

from .connections_fakes import CANARY
from .connections_support import (
    FakeLeaseVerifier,
    PostgresConnectionTestCase,
    requires_postgres,
)
from .gate_support import ALWAYS_ACTIVE
from .support import make_settings
from .task_support import TEST_DATABASE_URL

CODEX = ConnectionKind.CODEX


@requires_postgres
class LeaseCase(PostgresConnectionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.seed_connection(CODEX)
        self.lease = FakeLeaseVerifier()
        self.service = self.new_service(lease=self.lease)
        self.task = self.seed_task(self.user)
        self.project = self.project_of(self.task)

    def task_context(self, task=None, lease=None):
        """The context of a call for ``task`` (default: the test's task) and, if
        given, another claim of a worker (``TaskContext`` is frozen)."""
        task = task or self.task
        context = self.context(task, self.user, self.project_of(task))
        return context if lease is None else replace(context, lease=lease)

    def call(self, service=None, *, task=None, lease=None):
        return (service or self.service).execute(
            self.principal(self.user),
            self.task_context(task, lease),
            CODEX,
            self.request(),
        )

    def uses(self) -> list[tuple[str, str]]:
        return [
            (e.decision, e.reason)
            for e in self.sink.events
            if e.action == "connection.use"
        ]

    def assert_nothing_started(self):
        self.assertEqual(self.codex.requests, [])
        self.assertEqual(self.resolver.resolved, [])
        self.assertEqual(self.usage_rows(), [])


@requires_postgres
class LostLeaseTest(LeaseCase):
    async def test_a_worker_that_lost_its_lease_is_refused_before_the_call(self):
        self.lease.status = LeaseStatus.LOST
        with self.assertRaises(TaskNotUsableError) as caught:
            await self.call()
        self.assertIs(caught.exception.reason, RefusalReason.LEASE_LOST)
        self.assert_nothing_started()
        self.assertEqual(self.uses(), [("deny", "lease_lost")])
        # The worker's own claim was asked about, for the task of the call.
        context = self.task_context()
        self.assertEqual(self.lease.asked, [(self.task, context.lease)])

    async def test_the_lease_is_asked_for_every_call(self):
        await self.call()
        await self.call()
        self.lease.status = LeaseStatus.LOST
        with self.assertRaises(TaskNotUsableError):
            await self.call()
        self.assertEqual(len(self.lease.asked), 3)
        self.assertEqual(len(self.usage_rows()), 2)

    async def test_a_lost_lease_is_refused_before_the_quota_is_looked_at(self):
        # The admission (quota, usage row) comes after the lease: a stale worker
        # neither spends a request of the quota nor learns about it.
        self.seed_quota(self.user, 0)
        self.lease.status = LeaseStatus.LOST
        with self.assertRaises(TaskNotUsableError) as caught:
            await self.call()
        self.assertIs(caught.exception.reason, RefusalReason.LEASE_LOST)
        self.assertEqual(self.uses(), [("deny", "lease_lost")])
        self.lease.status = LeaseStatus.HELD
        with self.assertRaises(QuotaExceededError):
            await self.call()


@requires_postgres
class UnavailableLeaseTest(LeaseCase):
    async def assert_unavailable(self, service=None):
        with self.assertRaises(TaskNotUsableError) as caught:
            await self.call(service)
        self.assertIs(caught.exception.reason, RefusalReason.LEASE_UNAVAILABLE)
        self.assert_nothing_started()
        self.assertEqual(self.uses(), [("deny", "lease_unavailable")])

    async def test_an_unknown_answer_is_refused(self):
        self.lease.status = LeaseStatus.UNKNOWN
        await self.assert_unavailable()

    async def test_an_answer_that_is_not_a_lease_status_is_refused(self):
        for answer in ("held", True, None, object()):
            with self.subTest(answer=repr(answer)):
                self.sink.events.clear()
                self.lease.status = answer
                await self.assert_unavailable()

    async def test_a_failing_check_is_refused_and_its_text_is_not_logged(self):
        self.lease.error = RuntimeError("queue down " + CANARY)
        with self.assertLogs("paw_backend.connections", logging.ERROR) as logs:
            await self.assert_unavailable()
        self.assertNotIn(CANARY, "\n".join(logs.output))
        self.assertIn("RuntimeError", "\n".join(logs.output))

    async def test_a_check_that_does_not_answer_in_time_is_refused(self):
        self.lease.gate = asyncio.Event()  # never set
        service = self.new_service(lease=self.lease, database_timeout_seconds=0.2)
        with self.assertLogs("paw_backend.connections", logging.ERROR):
            await self.assert_unavailable(service)

    async def test_a_service_without_a_verifier_refuses_every_call(self):
        # The default knows no lease (``FailClosedLeaseVerifier``).
        await self.assert_unavailable(self.new_service(lease=None))


@requires_postgres
class CheckOrderTest(LeaseCase):
    async def test_a_denied_principal_is_refused_without_asking_the_lease(self):
        stranger = Principal(self.seed_user(), SystemRole.USER)
        with self.assertRaises(ConnectionPermissionDeniedError):
            await self.service.execute(
                stranger, self.task_context(), CODEX, self.request()
            )
        self.assertEqual(self.lease.asked, [])

    async def test_a_kind_without_an_adapter_is_refused_without_asking(self):
        self.adapters = AdapterRegistry()  # no adapter at all
        service = self.new_service(lease=self.lease)
        with self.assertRaises(ConnectionUnavailableError):
            await self.call(service)
        self.assertEqual(self.lease.asked, [])

    async def test_the_task_budget_is_checked_before_the_lease(self):
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        service = self.new_service(lease=self.lease, budget=BudgetTracker(database))
        with self.assertRaises(TaskBudgetError):  # no budget configured
            await self.call(service)
        self.assertEqual(self.lease.asked, [])


@requires_postgres
class InFlightCallTest(LeaseCase):
    async def test_a_call_running_when_the_lease_is_lost_is_settled_and_charged(self):
        # The check is a read at one instant (Decision 0046, 3): a call that
        # passed it runs to its end, and what it spent is recorded, although the
        # lease is lost meanwhile (Decision 0057, 2).
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        budget = BudgetTracker(database)
        await budget.set_preset(self.task, BudgetPreset.STANDARD)
        service = self.new_service(lease=self.lease, budget=budget)
        self.codex.gate = asyncio.Event()
        running = self.spawn(self.call(service))
        await asyncio.wait_for(self.codex.started.wait(), 30)
        self.lease.status = LeaseStatus.LOST
        self.codex.gate.set()
        result = await running
        self.assertEqual(result.text, "an answer")
        (row,) = self.usage_rows()
        self.assertEqual(row["status"], UsageStatus.SUCCEEDED.value)
        usage = {u.kind: u.consumed for u in await budget.usage(self.task)}
        self.assertEqual(usage[BudgetKind.TOKENS], 15)
        self.assertEqual(len(self.lease.asked), 1)  # the settlement asks nothing
        # The next call of the stale worker is refused.
        with self.assertRaises(TaskNotUsableError):
            await self.call(service)


@requires_postgres
class QueueLeaseTest(LeaseCase):
    """The production verifier (``QueueLeaseVerifier``) on the real queue."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        self.queue = TaskQueue(database, project_gate=ALWAYS_ACTIVE)
        self.service = self.new_service(lease=QueueLeaseVerifier(self.queue))
        entry = await self.queue.enqueue(self.task)
        self.entry_id = entry.id
        self.first = QueueLease.of(await self.queue.claim_next("w1"), "w1")

    def expire(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE queue_entries SET claimed_at = now() - interval"
                    " '10 seconds', lease_expires_at = now() - interval '5 seconds'"
                    " WHERE id = :i"
                ),
                {"i": self.entry_id},
            )

    async def refused(self, lease, task=None):
        with self.assertRaises(TaskNotUsableError) as caught:
            await self.call(task=task, lease=lease)
        self.assertIs(caught.exception.reason, RefusalReason.LEASE_LOST)

    async def test_the_worker_that_holds_the_lease_may_call(self):
        result = await self.call(lease=self.first)
        self.assertEqual(result.text, "an answer")

    async def test_a_call_after_another_worker_took_the_entry_over_is_refused(self):
        self.expire()
        second = QueueLease.of(await self.queue.claim_next("w2"), "w2")
        await self.refused(self.first)
        # The worker that took over calls through the same service.
        result = await self.call(lease=second)
        self.assertEqual(result.text, "an answer")
        self.assertEqual(len(self.usage_rows()), 1)
        self.assertEqual([reason for _, reason in self.uses()], ["lease_lost"])

    async def test_a_stale_claim_of_the_same_worker_id_is_refused(self):
        self.expire()
        again = QueueLease.of(await self.queue.claim_next("w1"), "w1")
        self.assertEqual(again.claim_count, 2)
        await self.refused(self.first)
        await self.call(lease=again)

    async def test_a_call_after_the_lease_ran_out_is_refused_before_any_take_over(
        self,
    ):
        self.expire()
        await self.refused(self.first)
        self.assertEqual(self.usage_rows(), [])

    async def test_the_lease_of_another_task_is_refused(self):
        other = self.seed_task(self.user)
        await self.refused(self.first, task=other)
