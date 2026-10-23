"""The record of the planner's calls (issue #297, Decision 0099, 3).

Every call of the planner has a row in ``agent_plan_calls``, made with the
call's start and numbered by the database within the task's run (``seq``); the
call's runtime records where it runs through ``PlanCallPlacementHandle``, once,
before it runs there, and for the cloud the ``audit_events`` row of the send in
the same transaction. The record is fenced in that transaction by the task's
run, the worker's queue lease and the row's owner and ``seq``: a worker that
lost its lease (or whose call was superseded) writes nothing, and a gate that
cannot record sends nothing.

The first class needs no server. The others run on a real PostgreSQL (skipped
unless ``PAW_TEST_DATABASE_URL`` is set); ``test_orchestrator_grants`` runs them
again as the unprivileged application role.
"""

import asyncio
import unittest
import uuid
from datetime import UTC, datetime
from types import MappingProxyType
from unittest import mock

from paw_backend.agents.cloud import CloudGate
from paw_backend.connections import ConnectionKind
from paw_backend.orchestrator.audit import (
    CLOUD_PLANNING_REASON,
    CLOUD_SEND_ACTION,
    CLOUD_SEND_REASON,
    CloudSend,
    cloud_send_event,
)
from paw_backend.orchestrator.domain import ExecutionPlacement, NodeRole, RunOutcome
from paw_backend.orchestrator.errors import (
    InvalidOrchestratorArgumentError,
    NodeStopped,
    StopReason,
)
from paw_backend.orchestrator.gateway import AttemptFence, RunGuard
from paw_backend.orchestrator.orchestrator import PLANNER_IDENTITY
from paw_backend.orchestrator.placement import content_digest
from paw_backend.orchestrator.plan_calls import (
    PlanCallPlacementHandle,
    PlanCallStore,
)
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.runtime import NodeAssignment, NodeOutcome
from paw_backend.orchestrator.scope import agent_id_of
from paw_backend.tasks import TaskCommand, TaskRun
from paw_backend.tools import PostgresTaskActivity, TaskActivity

from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    fail,
    node,
    requires_postgres,
    until,
)

L = ExecutionPlacement
RUN = TaskRun(1, 0)
CONTENT = ("sha256:" + "cd" * 32, 42)


def planned(*nodes: dict) -> NodeOutcome:
    return NodeOutcome.succeeded(NodeResult("a plan"), plan={"nodes": list(nodes)})


GOOD = planned(node("a"))
CYCLE = planned(node("a", "b"), node("b", "a"))


class AuditEventTest(unittest.TestCase):
    def send(self) -> CloudSend:
        return CloudSend(
            content_fingerprint=CONTENT[0],
            content_bytes=CONTENT[1],
            agent_id=uuid.uuid4(),
            occurred_at=datetime.now(UTC),
        )

    def test_a_planner_send_has_its_own_fixed_reason(self):
        event = cloud_send_event(
            self.send(),
            event_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            project_id=None,
            delegator_id=None,
            reason=CLOUD_PLANNING_REASON,
        )
        self.assertEqual(
            (event.action, event.reason), (CLOUD_SEND_ACTION, "cloud_planning")
        )
        node_event = cloud_send_event(
            self.send(),
            event_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            project_id=None,
            delegator_id=None,
        )
        self.assertEqual(node_event.reason, CLOUD_SEND_REASON)

    def test_no_other_reason_is_written(self):
        with self.assertRaises(InvalidOrchestratorArgumentError):
            cloud_send_event(
                self.send(),
                event_id=uuid.uuid4(),
                task_id=uuid.uuid4(),
                project_id=None,
                delegator_id=None,
                reason="anything the caller says",
            )


class Active:
    """The task and its run may still act."""

    async def check(self, task_id, run):
        return TaskActivity.ACTIVE


class PlanCallCase(PostgresOrchestratorTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.calls = PlanCallStore(self.database)

    async def running(self, h, worker: str = "w1"):
        """A queued task claimed by ``worker`` and started: ``(task_id, entry)``."""
        task_id = await self.prepare(h)
        entry = await h.queue.claim_next(worker)
        await h.tasks.execute(task_id, TaskCommand.START, actor=self.system)
        return task_id, entry

    async def start_call(
        self, h, entry, worker: str, run: TaskRun = RUN, claim_call: int = 1
    ) -> int:
        """What ``_start_planner_call`` writes besides the budget: the proof of
        the lease and the call's row, in one transaction."""
        async with h.queue.database.session() as session, session.begin():
            await h.queue.heartbeat_in(session, entry.id, worker, entry.claim_count)
            return await self.calls.start_in(
                session, entry.task_id, run, claim_call=claim_call, owner=worker
            )

    def handle(self, h, entry, worker: str, seq: int, **overrides):
        values = dict(
            entry=entry,
            worker_id=worker,
            seq=seq,
            agent_id=uuid.uuid4(),
            content=CONTENT,
        )
        guard = overrides.pop("guard", None) or RunGuard(entry.task_id, RUN, Active())
        values.update(overrides)
        return PlanCallPlacementHandle(
            guard, PlanCallStore(h.queue.database), h.queue, **values
        )

    async def expire_the_lease(self) -> None:
        await self.owner_sql(
            "UPDATE queue_entries SET claimed_at = now() - interval '10 seconds',"
            " lease_expires_at = now() - interval '5 seconds' WHERE status = 'claimed'"
        )

    async def sends_of(self, task_id) -> list[dict]:
        return await self.rows(
            "SELECT id, action, reason, decision, resource_kind, resource_id,"
            " project_id, actor_id, actor_role, agent_id, details"
            " FROM audit_events WHERE resource_id = :task AND action = :action",
            task=task_id,
            action=CLOUD_SEND_ACTION,
        )

    async def call_rows(self, task_id):
        return await self.calls.calls(task_id)


@requires_postgres
class PlanCallRecordTest(PlanCallCase):
    async def test_a_local_placement_is_recorded_on_the_call(self):
        h = self.harness()
        task_id, entry = await self.running(h)
        seq = await self.start_call(h, entry, "w1")
        self.assertEqual(seq, 1)

        await self.handle(h, entry, "w1", seq).record(
            L.LOCAL_GPU, agent="local", model="main"
        )

        (call,) = await self.call_rows(task_id)
        self.assertEqual(
            (call.placement, call.placement_agent, call.placement_model),
            (L.LOCAL_GPU, "local", "main"),
        )
        self.assertIsNotNone(call.placed_at)
        self.assertIsNone(call.placement_audit_id)
        self.assertIsNone(call.content_fingerprint)
        self.assertEqual(await self.sends_of(task_id), [])

    async def test_a_cloud_placement_and_its_audit_row_are_one_record(self):
        h = self.harness()
        task_id, entry = await self.running(h)
        seq = await self.start_call(h, entry, "w1")
        agent_id = uuid.uuid4()

        await self.handle(h, entry, "w1", seq, agent_id=agent_id).record(
            L.CLOUD, agent="claude", model="claude-opus"
        )

        (call,) = await self.call_rows(task_id)
        self.assertEqual(call.placement, L.CLOUD)
        self.assertEqual((call.content_fingerprint, call.content_bytes), CONTENT)
        (send,) = await self.sends_of(task_id)
        self.assertEqual(
            send,
            {
                "id": call.placement_audit_id,
                "action": CLOUD_SEND_ACTION,
                "reason": CLOUD_PLANNING_REASON,
                "decision": "allow",
                "resource_kind": "task",
                "resource_id": task_id,
                "project_id": self.project_id,
                "actor_id": self.user_id,
                "actor_role": "system",
                "agent_id": agent_id,
                "details": None,
            },
        )

    async def test_the_placement_is_not_recorded_without_its_audit_row(self):
        h = self.harness()
        task_id, entry = await self.running(h)
        seq = await self.start_call(h, entry, "w1")
        with (
            mock.patch(
                "paw_backend.orchestrator.plan_calls.record_cloud_send",
                side_effect=RuntimeError("audit_events refused the row"),
            ),
            self.assertRaises(RuntimeError),
        ):
            await self.handle(h, entry, "w1", seq).record(
                L.CLOUD, agent="claude", model="claude-opus"
            )
        (call,) = await self.call_rows(task_id)
        self.assertIsNone(call.placement)
        self.assertEqual(await self.sends_of(task_id), [])
        # And a later record (the runtime tries again) still works.
        await self.handle(h, entry, "w1", seq).record(
            L.LOCAL_CPU, agent="local", model="main"
        )
        (call,) = await self.call_rows(task_id)
        self.assertEqual(call.placement, L.LOCAL_CPU)

    async def test_a_placement_is_recorded_once(self):
        h = self.harness()
        task_id, entry = await self.running(h)
        seq = await self.start_call(h, entry, "w1")
        handle = self.handle(h, entry, "w1", seq)
        await handle.record(L.LOCAL_GPU, agent="local", model="main")
        with self.assertRaises(InvalidOrchestratorArgumentError):
            await handle.record(L.CLOUD, agent="claude", model="claude-opus")
        # Another handle of the same call: the store refuses it too.
        with self.assertRaises(InvalidOrchestratorArgumentError):
            await self.handle(h, entry, "w1", seq).record(
                L.CLOUD, agent="claude", model="claude-opus"
            )
        (call,) = await self.call_rows(task_id)
        self.assertEqual(call.placement, L.LOCAL_GPU)
        self.assertEqual(await self.sends_of(task_id), [])

    async def test_a_worker_that_lost_its_lease_records_nothing(self):
        h = self.harness()
        task_id, entry = await self.running(h)
        seq = await self.start_call(h, entry, "w1")
        await self.expire_the_lease()
        guard = RunGuard(task_id, RUN, Active())

        with self.assertRaises(NodeStopped) as caught:
            await self.handle(h, entry, "w1", seq, guard=guard).record(
                L.CLOUD, agent="claude", model="claude-opus"
            )

        self.assertEqual(caught.exception.reason, StopReason.LEASE_LOST)
        self.assertEqual(guard.stop_reason, StopReason.LEASE_LOST)
        (call,) = await self.call_rows(task_id)
        self.assertIsNone(call.placement)  # started, never placed: kept
        self.assertEqual(await self.sends_of(task_id), [])

    async def test_a_take_over_continues_the_seq_and_fences_the_old_worker(self):
        h = self.harness()
        task_id, old = await self.running(h, "wA")
        first = await self.start_call(h, old, "wA")
        await self.expire_the_lease()
        new = await h.queue.claim_next("wB")
        # The new worker counts its claim's calls from 1 (the ladder's rung),
        # the database numbers the run's calls on.
        second = await self.start_call(h, new, "wB", claim_call=1)
        self.assertEqual((first, second), (1, 2))

        with self.assertRaises(NodeStopped) as caught:
            await self.handle(h, old, "wA", first).record(
                L.CLOUD, agent="claude", model="claude-opus"
            )
        self.assertEqual(caught.exception.reason, StopReason.LEASE_LOST)
        await self.handle(h, new, "wB", second).record(
            L.LOCAL_GPU, agent="local", model="main"
        )

        calls = await self.call_rows(task_id)
        self.assertEqual(
            [(c.seq, c.claim_call, c.owner, c.placement) for c in calls],
            [(1, 1, "wA", None), (2, 1, "wB", L.LOCAL_GPU)],
        )
        self.assertEqual(await self.sends_of(task_id), [])

    async def test_a_call_that_is_not_the_runs_latest_records_nothing(self):
        h = self.harness()
        task_id, entry = await self.running(h)
        first = await self.start_call(h, entry, "w1")
        await self.start_call(h, entry, "w1", claim_call=2)
        with self.assertRaises(NodeStopped) as caught:
            await self.handle(h, entry, "w1", first).record(
                L.CLOUD, agent="claude", model="claude-opus"
            )
        self.assertEqual(caught.exception.reason, StopReason.ABANDONED)
        self.assertEqual(
            [c.placement for c in await self.call_rows(task_id)], [None] * 2
        )
        self.assertEqual(await self.sends_of(task_id), [])

    async def test_the_lease_holder_cannot_place_another_workers_call(self):
        h = self.harness()
        task_id, old = await self.running(h, "wA")
        first = await self.start_call(h, old, "wA")
        await self.expire_the_lease()
        new = await h.queue.claim_next("wB")
        with self.assertRaises(NodeStopped) as caught:
            await self.handle(h, new, "wB", first).record(
                L.LOCAL_GPU, agent="local", model="main"
            )
        self.assertEqual(caught.exception.reason, StopReason.ABANDONED)
        (call,) = await self.call_rows(task_id)
        self.assertIsNone(call.placement)

    async def test_a_replaced_run_records_nothing(self):
        h = self.harness()
        task_id, entry = await self.running(h)
        seq = await self.start_call(h, entry, "w1")
        await h.tasks.execute(task_id, TaskCommand.FAIL, actor=self.system)
        await h.tasks.execute(task_id, TaskCommand.RETRY, actor=self.user)
        guard = RunGuard(task_id, RUN, PostgresTaskActivity(self.database))

        with self.assertRaises(NodeStopped) as caught:
            await self.handle(h, entry, "w1", seq, guard=guard).record(
                L.CLOUD, agent="claude", model="claude-opus"
            )

        self.assertEqual(caught.exception.reason, StopReason.SUPERSEDED)
        (call,) = await self.call_rows(task_id)
        self.assertIsNone(call.placement)
        self.assertEqual(await self.sends_of(task_id), [])

    async def test_a_call_is_placed_only_while_the_task_runs(self):
        h = self.harness()
        task_id, entry = await self.running(h)
        seq = await self.start_call(h, entry, "w1")
        await h.tasks.execute(task_id, TaskCommand.PAUSE, actor=self.user)
        guard = RunGuard(task_id, RUN, Active())

        with self.assertRaises(NodeStopped):
            await self.handle(h, entry, "w1", seq, guard=guard).record(
                L.LOCAL_GPU, agent="local", model="main"
            )

        self.assertIsNone(guard.stop_reason)  # the run is not over
        (call,) = await self.call_rows(task_id)
        self.assertIsNone(call.placement)

    async def test_an_abandoned_or_stopped_call_records_nothing(self):
        h = self.harness()
        task_id, entry = await self.running(h)
        seq = await self.start_call(h, entry, "w1")
        fence = AttemptFence()
        fence.close()
        with self.assertRaises(NodeStopped) as caught:
            await self.handle(h, entry, "w1", seq, fence=fence).record(
                L.CLOUD, agent="claude", model="claude-opus"
            )
        self.assertEqual(caught.exception.reason, StopReason.ABANDONED)
        guard = RunGuard(task_id, RUN, Active())
        guard.stop(StopReason.BUDGET_EXCEEDED)
        with self.assertRaises(NodeStopped):
            await self.handle(h, entry, "w1", seq, guard=guard).record(
                L.CLOUD, agent="claude", model="claude-opus"
            )
        (call,) = await self.call_rows(task_id)
        self.assertIsNone(call.placement)
        self.assertEqual(await self.sends_of(task_id), [])

    async def test_the_recheck_runs_in_the_record_transaction_for_the_cloud(self):
        # Decision 0099, 2 (3): the single-user recheck's call point.
        h = self.harness()
        task_id, entry = await self.running(h)
        seen = []

        async def recheck(session):
            seen.append(session.in_transaction())

        local = await self.start_call(h, entry, "w1")
        await self.handle(h, entry, "w1", local, recheck=recheck).record(
            L.LOCAL_GPU, agent="local", model="main"
        )
        self.assertEqual(seen, [])  # a local call sends nothing
        cloud = await self.start_call(h, entry, "w1", claim_call=2)
        await self.handle(h, entry, "w1", cloud, recheck=recheck).record(
            L.CLOUD, agent="claude", model="claude-opus"
        )
        self.assertEqual(seen, [True])
        self.assertEqual(len(await self.sends_of(task_id)), 1)

    async def test_a_refusing_recheck_writes_nothing(self):
        h = self.harness()
        task_id, entry = await self.running(h)
        seq = await self.start_call(h, entry, "w1")

        async def refuse(session):
            raise NodeStopped(StopReason.TASK_ENDED)

        with self.assertRaises(NodeStopped):
            await self.handle(h, entry, "w1", seq, recheck=refuse).record(
                L.CLOUD, agent="claude", model="claude-opus"
            )
        (call,) = await self.call_rows(task_id)
        self.assertIsNone(call.placement)
        self.assertEqual(await self.sends_of(task_id), [])

    async def test_the_gate_sends_nothing_after_a_refused_record(self):
        # ``CloudGate``: the admission runs, then the record (the gate), then
        # the send. A worker whose lease was lost after the admission is
        # refused at the record, and the session is never started.
        h = self.harness()
        task_id, entry = await self.running(h)
        seq = await self.start_call(h, entry, "w1")
        sent = []

        class Connections:
            async def execute_session(self, kind, request, *, gate=None):
                await gate()
                sent.append(request)
                return "result"

        class Allow:
            async def allows(self, assignment):
                return True

        def assignment(placement):
            return NodeAssignment(
                task_id=task_id,
                node_key="plan",
                role=NodeRole.PLANNER,
                title="Fix the parser",
                goal="Fix the parser",
                input={},
                upstream={},
                agent="claude",
                attempt=1,
                approach=0,
                tools=object(),
                budget=object(),
                roots=MappingProxyType({}),
                placement=placement,
                connections=Connections(),
            )

        await self.expire_the_lease()
        with self.assertRaises(NodeStopped):
            await CloudGate(Allow()).session(
                assignment(self.handle(h, entry, "w1", seq)),
                kind=ConnectionKind.CLAUDE,
                request="the request",
                agent="claude",
                model="claude-opus",
            )
        self.assertEqual(sent, [])
        (call,) = await self.call_rows(task_id)
        self.assertIsNone(call.placement)
        self.assertEqual(await self.sends_of(task_id), [])


class CloudPlanner(FakeRuntime):
    """A planner runtime that records a cloud placement before it "sends", and
    notes what the record held at that moment."""

    def __init__(self, test, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.test = test
        self.seen: list = []

    async def run_node(self, assignment):
        if assignment.role is not NodeRole.PLANNER:
            return await super().run_node(assignment)
        await assignment.placement.record(
            ExecutionPlacement.CLOUD, agent="claude", model="claude-opus"
        )
        # The send is on record before anything leaves.
        self.seen.append(
            (
                [c.placement for c in await self.test.call_rows(assignment.task_id)],
                len(await self.test.sends_of(assignment.task_id)),
            )
        )
        return await super().run_node(assignment)


@requires_postgres
class PlanCallOrchestratorTest(PlanCallCase):
    async def test_every_planner_call_has_its_row_and_its_placement(self):
        planner = FakeRuntime("local", script={"plan": [CYCLE, GOOD]})
        h = self.harness(runtimes={"local": planner})
        task_id = await self.prepare(h)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, RunOutcome.DAG_SUCCEEDED)
        calls = planner.calls_of("plan")
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIsInstance(call.placement, PlanCallPlacementHandle)
            self.assertIsNone(call.connections)  # no connection service given
        rows = await self.call_rows(task_id)
        self.assertEqual(
            [(r.run, r.seq, r.claim_call, r.owner) for r in rows],
            [(RUN, 1, 1, "w1"), (RUN, 2, 2, "w1")],
        )
        # A runtime that reports nothing leaves the placement NULL.
        self.assertEqual([r.placement for r in rows], [None, None])

    async def test_a_planner_sent_to_the_cloud_is_on_record_before_it_runs(self):
        planner = CloudPlanner(self, "local", script={"plan": GOOD})
        h = self.harness(runtimes={"local": planner})
        task_id = await self.create_task(title="Fix the parser", input={"issue": 7})
        await h.orchestrator.enqueue_task(task_id, preset="standard")

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, RunOutcome.DAG_SUCCEEDED)
        self.assertEqual(planner.seen, [([L.CLOUD], 1)])
        (call,) = await self.call_rows(task_id)
        # The fingerprint is the orchestrator's: of what it handed the planner.
        self.assertEqual(
            (call.content_fingerprint, call.content_bytes),
            content_digest(
                node_key="plan",
                role=NodeRole.PLANNER,
                title="Fix the parser",
                goal="Fix the parser",
                input={"issue": 7},
                upstream={},
            ),
        )
        (send,) = await self.sends_of(task_id)
        self.assertEqual(send["id"], call.placement_audit_id)
        self.assertEqual(send["reason"], CLOUD_PLANNING_REASON)
        (entry,) = await self.rows("SELECT id FROM queue_entries")
        self.assertEqual(
            send["agent_id"],
            agent_id_of(task_id, RUN, PLANNER_IDENTITY, 1, claim=(entry["id"], 1)),
        )

    async def test_a_retried_run_numbers_its_calls_without_a_collision(self):
        planner = FakeRuntime(
            "local",
            script={"plan": [fail("Boom", "no", retryable=False), GOOD]},
        )
        h = self.harness(runtimes={"local": planner})
        task_id = await self.prepare(h)
        report = await h.orchestrator.run_once("w1")
        self.assertEqual(report.outcome, RunOutcome.PLAN_FAILED)

        await h.tasks.execute(task_id, TaskCommand.RETRY, actor=self.user)
        await h.orchestrator.enqueue_task(task_id, preset="standard")
        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, RunOutcome.DAG_SUCCEEDED)
        rows = await self.call_rows(task_id)
        self.assertEqual(
            [(r.run, r.seq, r.claim_call) for r in rows],
            [(TaskRun(1, 0), 1, 1), (TaskRun(1, 1), 1, 1)],
        )

    async def test_a_stale_planner_sends_nothing_after_a_take_over(self):
        # Worker A plans for longer than its lease; B claims the entry and
        # plans the same run (its call is the run's seq 2, the first of its
        # claim). When A's planner finally wants to send, its record is refused
        # by the lease check: it sends nothing, and its row stays unplaced.
        release = asyncio.Event()
        sent: list[str] = []

        def sends(worker):
            async def call(assignment):
                if worker == "wA":
                    await release.wait()
                await assignment.placement.record(
                    ExecutionPlacement.CLOUD, agent="claude", model="claude-opus"
                )
                sent.append(worker)
                return planned(node("only"))

            return call

        runtime_a = FakeRuntime("local", script={"plan": sends("wA")})
        a = self.harness(runtimes={"local": runtime_a})
        task_id = await self.prepare(a)
        run_a = asyncio.create_task(a.orchestrator.run_once("wA"))
        await until(lambda: len(runtime_a.calls_of("plan")) == 1, message="A plans")

        await self.expire_the_lease()
        runtime_b = FakeRuntime("local", script={"plan": sends("wB")})
        runtime_b.gate("only")  # the task keeps running while A records
        b = self.harness(runtimes={"local": runtime_b})
        run_b = asyncio.create_task(b.orchestrator.run_once("wB"))
        await until(lambda: len(runtime_b.calls_of("only")) == 1, message="B's node")

        release.set()
        report_a = await asyncio.wait_for(run_a, 120)

        self.assertEqual(report_a.outcome, RunOutcome.LEASE_LOST)
        self.assertEqual(sent, ["wB"])
        runtime_b.gates["only"].set()
        report_b = await asyncio.wait_for(run_b, 120)
        self.assertEqual(report_b.outcome, RunOutcome.DAG_SUCCEEDED)
        rows = await self.call_rows(task_id)
        self.assertEqual(
            [(r.seq, r.claim_call, r.owner, r.placement) for r in rows],
            [(1, 1, "wA", None), (2, 1, "wB", L.CLOUD)],
        )
        (send,) = await self.sends_of(task_id)
        self.assertEqual(send["id"], rows[1].placement_audit_id)

    async def test_a_planner_call_stopped_by_a_pause_ends_the_run_as_paused(self):
        async def paused_first(assignment):
            await self.service.execute(
                assignment.task_id, TaskCommand.PAUSE, actor=self.user
            )
            await assignment.placement.record(
                ExecutionPlacement.LOCAL_GPU, agent="local", model="main"
            )
            raise AssertionError("unreachable")

        planner = FakeRuntime("local", script={"plan": paused_first})
        h = self.harness(runtimes={"local": planner})
        task_id = await self.prepare(h)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, RunOutcome.PAUSED)
        (call,) = await self.call_rows(task_id)
        self.assertIsNone(call.placement)
        self.assertIsNone(await self.store.get(task_id, 1))


if __name__ == "__main__":
    unittest.main()
