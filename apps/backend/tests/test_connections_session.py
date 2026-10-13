"""A CLI session through a shared connection (Decision 0083, 1 and 5): real
PostgreSQL, a fake session adapter (no CLI, no network).

* the floor: a session starts only with ``SESSION_MIN_TOKENS`` of the task's
  budget left (nothing written otherwise);
* turn by turn the tokens go into the session's ``in_flight`` row and the task's
  budget in one transaction with the lease check; a lost lease or a replaced run
  writes nothing and stops the session; a used-up budget stops it;
* at the end only the remainder is settled (never less than counted, never
  pulled back); unknown totals count the 200,000 estimate less what was counted;
* the caller's gate runs between the admission and the adapter: when it fails
  nothing is sent and the row is ``cancelled``;
* a cancellation is settled before it passes;
* the schema: only a session's ``in_flight`` row may carry tokens, and the
  reaper keeps them.
"""

import asyncio
import dataclasses
import unittest

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from paw_backend.connections import (
    AgentSessionResult,
    ConnectionKind,
    FailureCode,
    RefusalReason,
    SessionRequest,
    SessionStop,
    TaskBudgetError,
    TaskNotUsableError,
    UsagePurpose,
)
from paw_backend.connections.store import SESSION_LEASE_SQL
from paw_backend.db import Database
from paw_backend.orchestrator.errors import NodeStopped, StopReason
from paw_backend.tasks.queueing import BudgetKind, BudgetPreset, BudgetTracker
from paw_backend.tasks.queueing.domain import QueueLease

from .connections_fakes import CANARY
from .connections_support import PostgresConnectionTestCase, requires_postgres
from .gate_support import ALWAYS_ACTIVE
from .support import make_settings
from .task_support import TEST_DATABASE_URL

CODEX = ConnectionKind.CODEX


class FakeSession:
    """A session adapter that reports ``turns`` and returns ``result``."""

    kind = CODEX

    def __init__(self, turns=(), result=None, *, error=None, gate=None, before=None):
        self.turns = list(turns)
        self.before = before  # awaited once the session was launched
        self.result = result or AgentSessionResult(submission={"summary": "done"})
        self.error = error
        self.gate = gate
        self.answers: list[bool] = []
        self.calls = 0

    async def run_session(self, secret, request):
        self.calls += 1
        if self.before is not None:
            await self.before()
        for input_tokens, output_tokens in self.turns:
            go_on = await request.on_usage(input_tokens, output_tokens)
            self.answers.append(go_on)
            if not go_on:
                break
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.result


@requires_postgres
class SessionCase(PostgresConnectionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.seed_connection(CODEX)
        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        self.budget = BudgetTracker(database)
        self.task = self.seed_task(self.user)
        await self.budget.set_preset(self.task, BudgetPreset.STANDARD)
        self.entry = self.seed_entry(self.task)

    def expire_entry(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE queue_entries SET lease_expires_at = now()"
                    " - interval '1 minute' WHERE id = :id"
                ),
                {"id": self.entry},
            )

    def seed_entry(self, task_id, *, expired=False):
        with self.engine.begin() as connection:
            return connection.execute(
                text(
                    "INSERT INTO queue_entries (task_id, priority, priority_rank,"
                    " status, enqueued_at, claimed_by, claimed_at, claim_count,"
                    " lease_expires_at) VALUES (:task, 'normal', 1, 'claimed',"
                    " now() - interval '5 minutes', 'w1',"
                    " now() - interval '5 minutes', 1,"
                    " now() + make_interval(secs => :secs))"
                    " RETURNING id"
                ),
                {"task": task_id, "secs": -60 if expired else 600},
            ).scalar_one()

    def session_service(self, adapter):
        return self.new_service(budget=self.budget, session_adapters={CODEX: adapter})

    def session_context(self, task=None, entry=None):
        task = task or self.task
        context = self.context(task, self.user, self.project_of(task))
        return dataclasses.replace(
            context, lease=QueueLease(entry or self.entry, "w1", 1)
        )

    def start(self, adapter, *, gate=None, task=None, entry=None, service=None):
        return (service or self.session_service(adapter)).execute_session(
            self.principal(self.user),
            self.session_context(task, entry),
            CODEX,
            SessionRequest("gpt-5-codex", UsagePurpose.CODING, "the task", 600),
            gate=gate,
        )

    async def consumed(self):
        usage = await self.budget.usage(self.task)
        return next(u.consumed for u in usage if u.kind is BudgetKind.TOKENS)

    def row(self):
        (row,) = self.rows(
            "SELECT status, failure_code, input_tokens, output_tokens, session"
            " FROM connection_usage"
        )
        return row


class TurnTest(SessionCase):
    async def test_turns_are_counted_and_only_the_rest_is_settled(self):
        adapter = FakeSession(
            [(100, 10), (200, 20)],
            AgentSessionResult(
                submission={"summary": "s"}, input_tokens=350, output_tokens=40
            ),
        )
        result = await self.start(adapter)
        self.assertEqual(adapter.answers, [True, True])
        self.assertEqual((result.input_tokens, result.output_tokens), (350, 40))
        self.assertEqual(result.submission, {"summary": "s"})
        self.assertIsNone(result.stopped)
        row = self.row()
        self.assertEqual(
            (row["status"], row["input_tokens"], row["output_tokens"], row["session"]),
            ("succeeded", 350, 40, True),
        )
        self.assertEqual(await self.consumed(), 390)  # never twice

    async def test_totals_below_what_was_counted_pull_nothing_back(self):
        adapter = FakeSession(
            [(100, 10)], AgentSessionResult(input_tokens=5, output_tokens=1)
        )
        result = await self.start(adapter)
        self.assertEqual((result.input_tokens, result.output_tokens), (100, 10))
        self.assertEqual(await self.consumed(), 110)

    async def test_unknown_totals_count_the_estimate_less_what_was_counted(self):
        adapter = FakeSession([(1000, 500)], AgentSessionResult())
        result = await self.start(adapter)
        self.assertEqual(result.input_tokens + result.output_tokens, 200_000)
        self.assertEqual(await self.consumed(), 200_000)

    async def test_a_known_part_of_the_totals_is_kept(self):
        # A known input total with an unknown output: the input is not lost
        # to the estimate; only the unknown part is estimated.
        await self.budget.set_preset(self.task, BudgetPreset.UNLIMITED)
        adapter = FakeSession([(1000, 500)], AgentSessionResult(input_tokens=1_000_000))
        result = await self.start(adapter)
        self.assertEqual(result.input_tokens, 1_000_000)
        self.assertEqual(result.output_tokens, 500)
        self.assertEqual(await self.consumed(), 1_000_500)

    async def test_a_small_known_part_is_topped_up_to_the_estimate(self):
        adapter = FakeSession([(1000, 500)], AgentSessionResult(output_tokens=700))
        result = await self.start(adapter)
        self.assertEqual(result.output_tokens, 700)
        self.assertEqual(result.input_tokens + result.output_tokens, 200_000)
        self.assertEqual(await self.consumed(), 200_000)

    async def test_a_row_in_flight_carries_what_was_counted(self):
        gate = asyncio.Event()
        adapter = FakeSession([(70, 7)], gate=gate)
        task = asyncio.create_task(self.start(adapter))
        for _ in range(200):
            rows = self.rows("SELECT status, input_tokens FROM connection_usage")
            if rows and rows[0]["input_tokens"] == 70:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(rows[0]["status"], "in_flight")
        self.assertEqual(await self.consumed(), 77)
        gate.set()
        await task


class StopTest(SessionCase):
    async def test_a_used_up_budget_stops_the_session(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE budget_usages SET limit_value = 60000"
                    " WHERE task_id = :task AND kind = 'tokens'"
                ),
                {"task": self.task},
            )
        adapter = FakeSession([(30_000, 0), (30_001, 0), (5, 0)])
        result = await self.start(adapter)
        self.assertEqual(adapter.answers, [True, False])
        self.assertIs(result.stopped, SessionStop.BUDGET_EXCEEDED)
        self.assertEqual(self.row()["status"], "cancelled")

    async def test_a_turn_that_uses_the_budget_up_exactly_stops_the_session(self):
        # "May the task do one more?": nothing is left, so the next turn
        # must not run (consumed + 1 > limit, as at the start).
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE budget_usages SET limit_value = 60000"
                    " WHERE task_id = :task AND kind = 'tokens'"
                ),
                {"task": self.task},
            )
        adapter = FakeSession([(30_000, 0), (30_000, 0), (5, 0)])
        result = await self.start(adapter)
        self.assertEqual(adapter.answers, [True, False])
        self.assertIs(result.stopped, SessionStop.BUDGET_EXCEEDED)
        self.assertEqual(self.row()["status"], "cancelled")

    async def test_a_lost_lease_writes_nothing_and_stops(self):
        async def expire():  # the lease runs out during the session
            self.expire_entry()

        adapter = FakeSession([(100, 10)], before=expire)
        result = await self.start(adapter)
        self.assertEqual(adapter.answers, [False])
        self.assertIs(result.stopped, SessionStop.LEASE_LOST)

    async def test_a_replaced_run_writes_nothing_and_stops(self):
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE tasks SET retry_count = 1 WHERE id = :task"),
                {"task": self.task},
            )
        adapter = FakeSession([(100, 10)])
        with self.assertRaises(TaskNotUsableError):  # the old run is refused
            await self.start(adapter)


class RunFenceTest(SessionCase):
    async def test_a_run_replaced_during_the_session_writes_nothing(self):
        # The turn is charged through ``BudgetTracker.record_in`` itself (its
        # run fence included), in the transaction of the row's increment.
        async def replace_run():  # a Retry during the session
            with self.engine.begin() as connection:
                connection.execute(
                    text("UPDATE tasks SET retry_count = 1 WHERE id = :task"),
                    {"task": self.task},
                )

        adapter = FakeSession([(100, 10)], before=replace_run)
        result = await self.start(adapter)
        self.assertEqual(adapter.answers, [False])
        self.assertIs(result.stopped, SessionStop.RUN_REPLACED)


class LockOrderTest(SessionCase):
    async def test_a_turn_locks_the_budget_before_the_queue_entry(self):
        # The order of every other writer (the task, its budget, then the
        # queue entry: ``start_runtime_in``, a deletion's ``cancel_in``): a
        # turn that waits for the queue entry already holds the budget row,
        # so the two orders can never wait on each other.
        holder = self.engine.connect()
        transaction = holder.begin()

        class Locking(FakeSession):
            async def run_session(inner, secret, request):
                holder.execute(
                    text("SELECT 1 FROM queue_entries WHERE id = :id FOR UPDATE"),
                    {"id": self.entry},
                )
                return await FakeSession.run_session(inner, secret, request)

        adapter = Locking([(100, 10)])
        running = asyncio.create_task(self.start(adapter))
        try:
            for _ in range(200):  # until the turn waits for the entry
                await asyncio.sleep(0.025)
                with self.engine.connect() as probe:
                    waiting = probe.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity"
                            " WHERE wait_event_type = 'Lock'"
                            " AND datname = current_database()"
                        )
                    ).scalar_one()
                if waiting:
                    break
            self.assertTrue(waiting)
            with self.engine.connect() as probe, self.assertRaises(OperationalError):
                probe.execute(
                    text(
                        "SELECT 1 FROM budget_usages WHERE task_id = :task"
                        " AND kind = 'tokens' FOR UPDATE NOWAIT"
                    ),
                    {"task": self.task},
                )
        finally:
            transaction.rollback()
            holder.close()
        result = await running
        self.assertEqual(adapter.answers, [True])
        self.assertIsNone(result.stopped)

    async def test_a_zero_token_turn_is_fenced_on_the_run_too(self):
        # A (0, 0) turn charges nothing but still passes the run fence.
        async def replace_run():  # a Retry during the session
            with self.engine.begin() as connection:
                connection.execute(
                    text("UPDATE tasks SET retry_count = 1 WHERE id = :task"),
                    {"task": self.task},
                )

        adapter = FakeSession([(0, 0)], before=replace_run)
        result = await self.start(adapter)
        self.assertEqual(adapter.answers, [False])
        self.assertIs(result.stopped, SessionStop.RUN_REPLACED)

    async def test_a_turn_refused_by_the_lease_charges_nothing(self):
        # The charge comes first now: a lease that does not hold rolls it back.
        before = await self.consumed()

        async def expire():  # during the session
            self.expire_entry()

        adapter = FakeSession(
            [(100, 10)],
            AgentSessionResult(input_tokens=100, output_tokens=10),
            before=expire,
        )
        result = await self.start(adapter)
        self.assertIs(result.stopped, SessionStop.LEASE_LOST)
        # The settlement charges the whole 110 once: the refused turn did not.
        self.assertEqual(await self.consumed() - before, 110)


class ReaperTest(SessionCase):
    async def test_the_reaper_keeps_what_a_session_counted(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO connection_usage (id, user_id, task_id, project_id,"
                    " kind, model, purpose, status, started_at, input_tokens,"
                    " output_tokens, session) VALUES (gen_random_uuid(), :user,"
                    " :task, :project, 'codex', 'm', 'coding', 'in_flight',"
                    " now() - interval '3 days', 1234, 56, true)"
                ),
                {
                    "user": self.user,
                    "task": self.task,
                    "project": self.project_of(self.task),
                },
            )
        service = self.session_service(FakeSession())
        (reaped,) = await service._store.reap_abandoned()
        self.assertEqual((reaped.input_tokens, reaped.output_tokens), (1234, 56))
        self.assertEqual(self.row()["status"], "failed")


class FencedAuthorizationTest(SessionCase):
    async def test_the_next_turn_is_decided_inside_the_fenced_transaction(self):
        # Whether the budget allows another turn is read in the turn's own
        # transaction (with its run and lease fences), never by a separate
        # read afterwards that a lost lease or a new run could overtake.
        service = self.session_service(FakeSession([(100, 10), (5, 0)]))
        unfenced = []
        original = self.budget.check

        async def check(*args, **kwargs):
            if service_running[0]:
                unfenced.append(args)
            return await original(*args, **kwargs)

        service_running = [False]
        self.budget.check = check
        adapter = service._session_adapters[CODEX]
        run_session = adapter.run_session

        async def running(secret, request):
            service_running[0] = True
            try:
                return await run_session(secret, request)
            finally:
                service_running[0] = False

        adapter.run_session = running
        result = await self.start(None, service=service)
        self.assertIsNone(result.stopped)
        self.assertEqual(unfenced, [])


class AdmissionTest(SessionCase):
    async def test_the_floor_refuses_before_anything_is_written(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE budget_usages SET limit_value = 40000"
                    " WHERE task_id = :task AND kind = 'tokens'"
                ),
                {"task": self.task},
            )
        adapter = FakeSession()
        with self.assertRaises(TaskBudgetError) as caught:
            await self.start(adapter)
        self.assertIs(caught.exception.reason, RefusalReason.TASK_TOKEN_FLOOR)
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(self.rows("SELECT id FROM connection_usage"), [])

    async def test_a_failed_gate_sends_nothing(self):
        adapter = FakeSession([(1, 1)])

        async def gate():
            raise RuntimeError("the placement could not be recorded")

        with self.assertRaises(RuntimeError):
            await self.start(adapter, gate=gate)
        self.assertEqual(adapter.calls, 0)
        row = self.row()
        self.assertEqual((row["status"], row["input_tokens"]), ("cancelled", 0))
        self.assertEqual(await self.consumed(), 0)

    async def test_the_gate_runs_after_the_admission(self):
        seen = []

        async def gate():
            seen.append(self.rows("SELECT status FROM connection_usage"))

        await self.start(FakeSession(), gate=gate)
        self.assertEqual(seen, [[{"status": "in_flight"}]])

    async def test_a_failing_adapter_is_a_failure_code(self):
        from paw_backend.connections import AdapterFailure

        adapter = FakeSession([(10, 1)], error=AdapterFailure(FailureCode.EXPIRED))
        result = await self.start(adapter)
        self.assertIs(result.failure, FailureCode.EXPIRED)
        row = self.row()
        self.assertEqual((row["status"], row["failure_code"]), ("failed", "expired"))
        # Unknown totals: the estimate.
        self.assertEqual(await self.consumed(), 200_000)


class CancelTest(SessionCase):
    async def test_a_cancellation_is_settled_before_it_passes(self):
        gate = asyncio.Event()
        adapter = FakeSession([(10, 1)], gate=gate)
        task = asyncio.create_task(self.start(adapter))
        for _ in range(200):
            if self.rows(
                "SELECT input_tokens FROM connection_usage WHERE input_tokens = 10"
            ):
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        row = self.row()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["input_tokens"] + row["output_tokens"], 200_000)

    async def test_a_cancellation_right_after_a_turns_commit_charges_it_once(self):
        # The turn is committed, then the cancellation lands before the
        # service has noted it: the settlement reads what was counted from
        # the row, so the turn is neither charged twice nor counted twice.
        before = await self.consumed()
        service = self.session_service(FakeSession([(10, 1)]))
        original = service._store.add_session_turn

        async def committed_then_cancelled(*args, **kwargs):
            await original(*args, **kwargs)
            raise asyncio.CancelledError

        service._store.add_session_turn = committed_then_cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.start(None, service=service)
        row = self.row()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["input_tokens"] + row["output_tokens"], 200_000)
        self.assertEqual(await self.consumed() - before, 200_000)


class LaunchAndSettlementTest(SessionCase):
    async def test_a_run_replaced_before_the_launch_sends_nothing(self):
        adapter = FakeSession([(100, 10)])

        async def replace_run():  # after the admission, before the launch
            with self.engine.begin() as connection:
                connection.execute(
                    text("UPDATE tasks SET retry_count = 1 WHERE id = :task"),
                    {"task": self.task},
                )

        result = await self.start(adapter, gate=replace_run)
        self.assertEqual(adapter.calls, 0)
        self.assertIs(result.stopped, SessionStop.RUN_REPLACED)
        self.assertEqual(self.row()["status"], "cancelled")

    async def test_an_attempt_abandoned_before_the_launch_sends_nothing(self):
        adapter = FakeSession([(100, 10)])
        result = await self.session_service(adapter).execute_session(
            self.principal(self.user),
            self.session_context(),
            CODEX,
            SessionRequest("gpt-5-codex", UsagePurpose.CODING, "the task", 600),
            alive=lambda: False,
        )
        self.assertEqual(adapter.calls, 0)
        self.assertIs(result.stopped, SessionStop.ABANDONED)

    async def test_known_totals_count_even_when_the_submission_is_refused(self):
        await self.budget.set_preset(self.task, BudgetPreset.UNLIMITED)
        adapter = FakeSession(
            result=AgentSessionResult(
                submission={"bad": object()}, input_tokens=1_000_000
            )
        )
        result = await self.start(adapter)
        self.assertIs(result.failure, FailureCode.INVALID_RESPONSE)
        self.assertEqual(await self.consumed(), 1_000_000)

    async def test_the_row_is_not_closed_without_its_charge(self):
        # The remainder's charge fails: the settlement rolls back and the
        # row stays in flight (the reaper's), never closed uncharged.
        adapter = FakeSession()  # unknown totals: the estimate is the remainder
        record, record_in = self.budget.record, self.budget.record_in

        async def failing_record(task_id, kind, amount, *args, **kwargs):
            if amount == 200_000:
                raise RuntimeError("the charge failed")
            return await record(task_id, kind, amount, *args, **kwargs)

        async def failing_record_in(session, task_id, kind, amount, **kwargs):
            if amount == 200_000:
                raise RuntimeError("the charge failed")
            return await record_in(session, task_id, kind, amount, **kwargs)

        self.budget.record = failing_record
        self.budget.record_in = failing_record_in
        await self.start(adapter)
        self.assertEqual(self.row()["status"], "in_flight")


class FloorAtomicityTest(SessionCase):
    async def test_the_floor_is_judged_again_inside_the_admission(self):
        # Another node charges the task between the early check and the
        # admission: the admission itself refuses below the floor.
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE budget_usages SET limit_value = 60000"
                    " WHERE task_id = :task AND kind = 'tokens'"
                ),
                {"task": self.task},
            )
        adapter = FakeSession([(10, 1)])
        service = self.session_service(adapter)
        original = service._store.admit

        async def charged_meanwhile(*args, **kwargs):
            await self.budget.record(self.task, BudgetKind.TOKENS, 20_000)
            return await original(*args, **kwargs)

        service._store.admit = charged_meanwhile
        with self.assertRaises(TaskBudgetError) as caught:
            await self.start(None, service=service)
        self.assertIs(caught.exception.reason, RefusalReason.TASK_TOKEN_FLOOR)
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(self.rows("SELECT id FROM connection_usage"), [])


class SubmissionTest(SessionCase):
    async def test_the_credential_is_scrubbed_from_the_submission(self):
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # split: a shape only
        submitted = {
            "summary": f"used {CANARY} here",
            "notes": [{"line": "token " + token}, 3, None, True],
            CANARY: "as a key",
        }
        adapter = FakeSession(result=AgentSessionResult(submission=submitted))
        result = await self.start(adapter)
        self.assertIsNone(result.failure)
        text = repr(result.submission)
        self.assertNotIn(CANARY, text)
        self.assertNotIn(token, text)
        self.assertEqual(result.submission["notes"][1:], [3, None, True])

    async def test_a_value_under_a_sensitive_key_is_redacted(self):
        opaque = "opaque" + "-value-without-a-known-shape"  # split: a shape only
        adapter = FakeSession(
            result=AgentSessionResult(
                submission={"config": {"api_key": opaque}, "summary": "s"}
            )
        )
        result = await self.start(adapter)
        self.assertIsNone(result.failure)
        self.assertNotIn(opaque, repr(result.submission))
        self.assertEqual(result.submission["summary"], "s")

    async def test_a_submission_beyond_the_read_limits_is_refused(self):
        from paw_backend.tools.credentials import MAX_RESULT_NODES

        adapter = FakeSession(
            result=AgentSessionResult(
                submission={"items": [0] * (MAX_RESULT_NODES + 1)}
            )
        )
        result = await self.start(adapter)
        self.assertIs(result.failure, FailureCode.INVALID_RESPONSE)
        self.assertIsNone(result.submission)

    async def test_a_malformed_submission_is_refused(self):
        adapter = FakeSession(
            result=AgentSessionResult(submission={"summary": object()})
        )
        result = await self.start(adapter)
        self.assertIs(result.failure, FailureCode.INVALID_RESPONSE)
        self.assertIsNone(result.submission)

    async def test_memory_running_out_passes_unchanged(self):
        adapter = FakeSession(error=MemoryError())
        with self.assertRaises(MemoryError):
            await self.start(adapter)
        self.assertEqual(self.row()["status"], "cancelled")

    async def test_an_abandoned_attempt_gets_no_further_turn(self):
        # The turn that was spent is counted; the next one is refused.
        open_ = [True]

        async def abandon():  # the orchestrator closes the attempt's fence
            open_[0] = False

        adapter = FakeSession([(100, 10), (5, 0)], before=abandon)

        async def run():
            return await self.session_service(adapter).execute_session(
                self.principal(self.user),
                self.session_context(),
                CODEX,
                SessionRequest("gpt-5-codex", UsagePurpose.CODING, "the task", 600),
                alive=lambda: open_[0],
            )

        result = await run()
        self.assertEqual(adapter.answers, [False])
        self.assertIs(result.stopped, SessionStop.ABANDONED)
        self.assertGreaterEqual(result.input_tokens, 100)

    async def test_a_node_stop_from_the_bridge_passes_unchanged(self):
        adapter = FakeSession(error=NodeStopped(StopReason.LEASE_LOST))
        with self.assertRaises(NodeStopped) as caught:
            await self.start(adapter)
        self.assertIs(caught.exception.reason, StopReason.LEASE_LOST)
        self.assertEqual(self.row()["status"], "cancelled")


class SchemaTest(SessionCase):
    async def test_only_a_sessions_row_may_carry_tokens_in_flight(self):
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO connection_usage (id, user_id, task_id,"
                        " project_id, kind, model, purpose, status, started_at,"
                        " input_tokens) VALUES (gen_random_uuid(), :user, :task,"
                        " :project, 'codex', 'm', 'coding', 'in_flight', now(), 1)"
                    ),
                    {
                        "user": self.user,
                        "task": self.task,
                        "project": self.project_of(self.task),
                    },
                )

    async def test_the_lease_sql_is_the_queues_rule(self):
        from paw_backend.tasks.queueing import TaskQueue

        database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        queue = TaskQueue(database, project_gate=ALWAYS_ACTIVE)
        cases = [(self.entry, "w1"), (self.entry, "w2"), (self.entry + 999, "w1")]
        for entry, worker in cases:
            lease = QueueLease(entry, worker, 1)
            with self.engine.begin() as connection:
                ours = (
                    connection.execute(
                        text(SESSION_LEASE_SQL),
                        {
                            "entry": entry,
                            "task": self.task,
                            "worker": worker,
                            "claims": 1,
                        },
                    ).first()
                    is not None
                )
            self.assertEqual(ours, await queue.holds_lease(self.task, lease))

    def test_the_lease_is_judged_by_the_clock_not_the_transactions_start(self):
        # A turn's transaction can wait on the task or budget lock past the
        # lease's end: ``now()`` (its start) would still accept the lease.
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE queue_entries SET lease_expires_at ="
                    " clock_timestamp() + interval '300 milliseconds' WHERE id = :id"
                ),
                {"id": self.entry},
            )
        with self.engine.begin() as connection:
            connection.execute(text("SELECT now()"))
            connection.execute(text("SELECT pg_sleep(0.6)"))
            held = connection.execute(
                text(SESSION_LEASE_SQL),
                {"entry": self.entry, "task": self.task, "worker": "w1", "claims": 1},
            ).first()
        self.assertIsNone(held)


class SessionFinalsTest(unittest.TestCase):
    """The fallback of the settlement uses the same rule as the store."""

    def test_the_rule(self):
        from paw_backend.connections.service import _session_finals

        cases = [
            ((10, 5, (100, 50), 200), (100, 50)),
            ((10, 5, (None, None), 200), (195, 5)),
            ((10, 5, (1_000, None), 200), (1_000, 5)),
            ((10, 5, (100, None), 200), (100, 100)),
            ((10, 5, (None, 70), 200), (130, 70)),
            ((300, 5, (100, 50), 200), (300, 50)),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(_session_finals(*args), expected)


if __name__ == "__main__":
    unittest.main()
