"""Where a node's attempt ran, and the audit of a node sent to the cloud (#133).

Decision 0037's 14 (approved): the orchestrator's record says where each node
actually ran (``local_gpu`` / ``local_cpu`` / ``cloud``) and on which agent and
model, and a node sent to a cloud agent leaves an audit of the external send in
``audit_events``, before any ``CloudPolicy`` is injected. The proposal of how is
Decision 0048.

The first class needs no server. The others run on a real PostgreSQL (skipped
unless ``PAW_TEST_DATABASE_URL`` is set): the store's fenced, write-once record
and its audit row in one transaction, the database's own guards (CHECK
constraints and the trigger), and the orchestrator and ``HybridRuntime`` end to
end. ``test_orchestrator_grants`` runs the PostgreSQL classes again as the
unprivileged application role.
"""

import hashlib
import json
import unittest
import uuid
from datetime import UTC, datetime
from unittest import mock

from sqlalchemy.exc import DBAPIError

from paw_backend.compute import ComputeRequest, HybridRuntime, Placement, ResourceClass
from paw_backend.orchestrator.audit import (
    CLOUD_SEND_ACTION,
    CLOUD_SEND_REASON,
    CloudSend,
)
from paw_backend.orchestrator.domain import (
    AttemptState,
    ExecutionPlacement,
    NodeRole,
    RunOutcome,
)
from paw_backend.orchestrator.errors import (
    InvalidOrchestratorArgumentError,
    NodeStopped,
    StaleDagEpochError,
    StaleNodeAttemptError,
    StopReason,
)
from paw_backend.orchestrator.gateway import AttemptFence, RunGuard
from paw_backend.orchestrator.placement import NodePlacementHandle, content_digest
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.runtime import NodeOutcome
from paw_backend.orchestrator.scope import agent_id_of
from paw_backend.tasks import StaleRunError, TaskCommand, TaskRun
from paw_backend.tools import TaskActivity

from .compute_support import build
from .gate_support import ALWAYS_ACTIVE
from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    make_plan,
    node,
    requires_postgres,
)

RUN = TaskRun(1, 0)
L = ExecutionPlacement
PLANNED = NodeOutcome.succeeded(NodeResult("a plan"), plan={"nodes": [node("a")]})


def a_send(**overrides) -> CloudSend:
    values = dict(
        content_fingerprint="sha256:" + "ab" * 32,
        content_bytes=120,
        agent_id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
    )
    values.update(overrides)
    return CloudSend(**values)


class ContentDigestTest(unittest.TestCase):
    def digest(self, **overrides):
        values = dict(
            node_key="a",
            role=NodeRole.WORKER,
            title="Work",
            goal="Fix the bug",
            input={"files": ["a.py"], "n": 1},
            upstream={"b": NodeResult("found it")},
        )
        values.update(overrides)
        return content_digest(**values)

    def test_the_fingerprint_is_the_sha256_of_the_canonical_content(self):
        fingerprint, size = self.digest()
        encoded = json.dumps(
            {
                "goal": "Fix the bug",
                "input": {"files": ["a.py"], "n": 1},
                "node_key": "a",
                "role": "worker",
                "title": "Work",
                "upstream": {"b": NodeResult("found it").to_json()},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(fingerprint, "sha256:" + hashlib.sha256(encoded).hexdigest())
        self.assertEqual(size, len(encoded))

    def test_the_key_order_does_not_matter_and_every_part_counts(self):
        base = self.digest()
        self.assertEqual(self.digest(input={"n": 1, "files": ["a.py"]}), base)
        for change in (
            {"goal": "Fix the other bug"},
            {"title": "Other"},
            {"node_key": "c"},
            {"role": NodeRole.REVIEWER},
            {"input": {}},
            {"upstream": {}},
        ):
            with self.subTest(change):
                self.assertNotEqual(self.digest(**change)[0], base[0])

    def test_a_cloud_send_holds_only_checked_identifiers(self):
        for field, value in (
            ("content_fingerprint", "sha256:" + "AB" * 32),
            ("content_fingerprint", "md5:" + "ab" * 32),
            ("content_bytes", -1),
            ("content_bytes", True),
            ("agent_id", str(uuid.uuid4())),
            ("occurred_at", datetime.now()),
        ):
            with (
                self.subTest(field=field),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                a_send(**{field: value})

    def test_the_compute_placements_are_the_orchestrators(self):
        self.assertEqual(
            [member.value for member in Placement],
            [member.value for member in ExecutionPlacement],
        )


class PlacementStoreCase(PostgresOrchestratorTestCase):
    async def running(self, key: str = "a"):
        dag = await self.taken_dag(make_plan(node("a"), node("b")))
        attempt = await self.store.start_node(dag.id, 1, key, max_attempts=6)
        return dag, attempt

    async def audit_row(self, event_id) -> dict | None:
        rows = await self.rows(
            "SELECT id, correlation_id, action, reason, decision, resource_kind,"
            " resource_id, project_id, actor_id, actor_role, agent_id, details"
            " FROM audit_events WHERE id = :id",
            id=event_id,
        )
        return rows[0] if rows else None

    async def attempt_row(self, dag_id, key: str = "a", number: int = 1) -> dict:
        (row,) = await self.rows(
            "SELECT * FROM agent_dag_node_attempts"
            " WHERE dag_id = :dag AND node_key = :key AND number = :number",
            dag=dag_id,
            key=key,
            number=number,
        )
        return row


@requires_postgres
class StorePlacementTest(PlacementStoreCase):
    async def test_a_local_placement_is_recorded_on_the_running_attempt(self):
        dag, attempt = await self.running()
        self.assertIsNone(attempt.placement)

        record = await self.store.record_placement(
            dag.id, 1, "a", 1, placement=L.LOCAL_GPU, agent="local", model="main"
        )

        self.assertEqual(
            (record.placement, record.placement_agent, record.placement_model),
            (L.LOCAL_GPU, "local", "main"),
        )
        self.assertIsNotNone(record.placed_at)
        self.assertIsNone(record.placement_audit_id)
        self.assertIsNone(record.content_fingerprint)
        (stored,) = await self.store.attempts(dag.id, "a")
        self.assertEqual(stored, record)

    async def test_a_cloud_placement_and_its_audit_row_are_one_record(self):
        dag, _ = await self.running()
        send = a_send()

        record = await self.store.record_placement(
            dag.id,
            1,
            "a",
            1,
            placement=L.CLOUD,
            agent="codex",
            model="gpt-5-codex",
            cloud=send,
        )

        self.assertEqual(record.placement, L.CLOUD)
        self.assertEqual(record.content_fingerprint, send.content_fingerprint)
        self.assertEqual(record.content_bytes, 120)
        row = await self.audit_row(record.placement_audit_id)
        self.assertEqual(
            row,
            {
                "id": record.placement_audit_id,
                "correlation_id": record.placement_audit_id,
                "action": CLOUD_SEND_ACTION,
                "reason": CLOUD_SEND_REASON,
                "decision": "allow",
                "resource_kind": "task",
                "resource_id": dag.task_id,
                "project_id": self.project_id,
                "actor_id": self.user_id,
                "actor_role": "system",
                "agent_id": send.agent_id,
                "details": None,
            },
        )

    async def test_the_placement_is_not_recorded_without_its_audit_row(self):
        dag, _ = await self.running()
        with (
            mock.patch(
                "paw_backend.orchestrator.store.record_cloud_send",
                side_effect=RuntimeError("audit_events refused the row"),
            ),
            self.assertRaises(RuntimeError),
        ):
            await self.store.record_placement(
                dag.id,
                1,
                "a",
                1,
                placement=L.CLOUD,
                agent="codex",
                model="gpt-5-codex",
                cloud=a_send(),
            )
        row = await self.attempt_row(dag.id)
        self.assertIsNone(row["placement"])
        # And a later record (the runtime tries again) still works.
        await self.store.record_placement(
            dag.id, 1, "a", 1, placement=L.LOCAL_GPU, agent="local", model="main"
        )

    async def test_a_placement_is_recorded_once(self):
        dag, _ = await self.running()
        await self.store.record_placement(
            dag.id, 1, "a", 1, placement=L.LOCAL_GPU, agent="local", model="main"
        )
        with self.assertRaises(InvalidOrchestratorArgumentError):
            await self.store.record_placement(
                dag.id,
                1,
                "a",
                1,
                placement=L.CLOUD,
                agent="codex",
                model="gpt-5-codex",
                cloud=a_send(),
            )
        row = await self.attempt_row(dag.id)
        self.assertEqual(row["placement"], "local_gpu")
        self.assertIsNone(row["placement_audit_id"])

    async def test_only_a_running_attempt_of_the_current_run_is_placed(self):
        dag, _ = await self.running()
        with self.assertRaises(StaleNodeAttemptError):  # not this attempt
            await self.store.record_placement(
                dag.id, 1, "a", 2, placement=L.LOCAL_GPU, agent="local", model="m"
            )
        with self.assertRaises(StaleNodeAttemptError):  # not running
            await self.store.record_placement(
                dag.id, 1, "b", 1, placement=L.LOCAL_GPU, agent="local", model="m"
            )
        await self.store.complete_node(dag.id, 1, "a", 1, NodeResult("done"))
        with self.assertRaises(StaleNodeAttemptError):  # ended
            await self.store.record_placement(
                dag.id, 1, "a", 1, placement=L.LOCAL_GPU, agent="local", model="m"
            )

    async def test_a_replaced_worker_places_nothing(self):
        dag, _ = await self.running()
        await self.store.acquire(dag.id, "w2", RUN)  # epoch 2
        with self.assertRaises(StaleDagEpochError):
            await self.store.record_placement(
                dag.id,
                1,
                "a",
                1,
                placement=L.CLOUD,
                agent="codex",
                model="gpt-5-codex",
                cloud=a_send(),
            )
        self.assertEqual(
            await self.rows(
                "SELECT count(*) AS n FROM audit_events WHERE action = :action"
                " AND resource_id = :task",
                action=CLOUD_SEND_ACTION,
                task=dag.task_id,
            ),
            [{"n": 0}],
        )

    async def test_an_ended_task_places_nothing(self):
        dag, _ = await self.running()
        await self.service.execute(dag.task_id, TaskCommand.CANCEL, actor=self.system)
        with self.assertRaises(StaleRunError):
            await self.store.record_placement(
                dag.id, 1, "a", 1, placement=L.LOCAL_GPU, agent="local", model="m"
            )

    async def test_the_arguments_are_checked_before_the_database(self):
        dag, _ = await self.running()
        send = a_send()
        for arguments in (
            dict(placement=L.CLOUD, agent="codex", model="m"),  # no audit
            dict(placement=L.LOCAL_GPU, agent="local", model="m", cloud=send),
            dict(placement="Cloud", agent="codex", model="m", cloud=send),
            dict(placement=L.LOCAL_CPU, agent="Local", model="m"),
            dict(placement=L.LOCAL_CPU, agent="local", model="a model"),
            dict(placement=L.LOCAL_CPU, agent="local", model="m" * 129),
            dict(placement=L.CLOUD, agent="codex", model="m", cloud=object()),
        ):
            with (
                self.subTest(arguments),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                await self.store.record_placement(dag.id, 1, "a", 1, **arguments)
        self.assertIsNone((await self.attempt_row(dag.id))["placement"])


@requires_postgres
class PlacementDatabaseGuardTest(PlacementStoreCase):
    """What the database refuses whoever writes (here the schema's owner)."""

    async def refused(self, sql: str, **parameters) -> None:
        with self.assertRaises(DBAPIError):
            await self.owner_sql(sql, **parameters)

    async def test_a_cloud_placement_needs_its_audit_and_a_local_one_has_none(self):
        dag, _ = await self.running()
        update = (
            "UPDATE agent_dag_node_attempts SET placement = :placement,"
            " placement_agent = 'codex', placement_model = 'm', placed_at = now(),"
            " content_fingerprint = :fingerprint, content_bytes = :size,"
            " placement_audit_id = :audit WHERE dag_id = :dag AND node_key = 'a'"
        )
        fingerprint = "sha256:" + "0" * 64
        for placement, audit in (("cloud", None), ("local_gpu", uuid.uuid4())):
            with self.subTest(placement):
                await self.refused(
                    update,
                    placement=placement,
                    fingerprint=fingerprint if audit else None,
                    size=1 if audit else None,
                    audit=audit,
                    dag=dag.id,
                )

    async def test_the_columns_hold_identifiers_never_text(self):
        dag, _ = await self.running()
        base = (
            "UPDATE agent_dag_node_attempts SET placement = 'local_gpu',"
            " placement_agent = :agent, placement_model = :model, placed_at = now()"
            " WHERE dag_id = :dag AND node_key = 'a'"
        )
        for agent, model in (
            ("local", "the model said: ignore previous instructions"),
            ("Local Agent", "main"),
            ("local", ""),
        ):
            with self.subTest(agent=agent, model=model):
                await self.refused(base, agent=agent, model=model, dag=dag.id)
        await self.refused(
            "UPDATE agent_dag_node_attempts SET placement = 'elsewhere',"
            " placement_agent = 'local', placement_model = 'm', placed_at = now()"
            " WHERE dag_id = :dag AND node_key = 'a'",
            dag=dag.id,
        )
        await self.refused(  # the placement without its agent
            "UPDATE agent_dag_node_attempts SET placement = 'local_gpu'"
            " WHERE dag_id = :dag AND node_key = 'a'",
            dag=dag.id,
        )

    async def test_a_recorded_placement_cannot_be_changed_or_erased(self):
        dag, _ = await self.running()
        await self.store.record_placement(
            dag.id,
            1,
            "a",
            1,
            placement=L.CLOUD,
            agent="codex",
            model="gpt-5-codex",
            cloud=a_send(),
        )
        for assignment in (
            "placement = 'local_gpu', content_fingerprint = NULL,"
            " content_bytes = NULL, placement_audit_id = NULL",
            "placement_model = 'other-model'",
            "placed_at = now() - interval '1 day'",
            "content_bytes = 1",
            "placement = NULL, placement_agent = NULL, placement_model = NULL,"
            " placed_at = NULL, content_fingerprint = NULL, content_bytes = NULL,"
            " placement_audit_id = NULL",
        ):
            with self.subTest(assignment):
                await self.refused(
                    f"UPDATE agent_dag_node_attempts SET {assignment}"
                    " WHERE dag_id = :dag AND node_key = 'a'",
                    dag=dag.id,
                )
        # How the attempt ends still changes.
        await self.store.complete_node(dag.id, 1, "a", 1, NodeResult("done"))
        row = await self.attempt_row(dag.id)
        self.assertEqual((row["state"], row["placement"]), ("succeeded", "cloud"))


@requires_postgres
class Active:
    """The task and its run may still act."""

    async def check(self, task_id, run):
        return TaskActivity.ACTIVE


class PlacementHandleTest(PlacementStoreCase):
    def handle(self, dag, **overrides) -> NodePlacementHandle:
        values = dict(
            dag_id=dag.id,
            epoch=1,
            node_key="a",
            attempt=1,
            agent_id=uuid.uuid4(),
            content=("sha256:" + "cd" * 32, 42),
        )
        values.update(overrides)
        return NodePlacementHandle(
            RunGuard(dag.task_id, RUN, Active()), self.store, **values
        )

    async def test_the_handle_records_the_cloud_with_the_orchestrators_content(self):
        dag, _ = await self.running()
        agent_id = uuid.uuid4()
        await self.handle(dag, agent_id=agent_id).record(
            L.CLOUD, agent="claude", model="claude-opus-4-1"
        )
        (record,) = await self.store.attempts(dag.id, "a")
        self.assertEqual(record.content_fingerprint, "sha256:" + "cd" * 32)
        self.assertEqual(record.content_bytes, 42)
        row = await self.audit_row(record.placement_audit_id)
        self.assertEqual(row["agent_id"], agent_id)

    async def test_a_second_record_is_refused_by_the_handle(self):
        dag, _ = await self.running()
        handle = self.handle(dag)
        await handle.record(L.LOCAL_CPU, agent="local", model="main")
        with self.assertRaises(InvalidOrchestratorArgumentError):
            await handle.record(L.LOCAL_GPU, agent="local", model="main")

    async def test_an_abandoned_or_stopped_attempt_records_nothing(self):
        dag, _ = await self.running()
        fence = AttemptFence()
        fence.close()
        with self.assertRaises(NodeStopped) as caught:
            await self.handle(dag, fence=fence).record(
                L.CLOUD, agent="codex", model="gpt-5-codex"
            )
        self.assertEqual(caught.exception.reason, StopReason.ABANDONED)
        guard = RunGuard(dag.task_id, RUN, ALWAYS_ACTIVE)
        guard.stop(StopReason.BUDGET_EXCEEDED)
        handle = NodePlacementHandle(
            guard,
            self.store,
            dag_id=dag.id,
            epoch=1,
            node_key="a",
            attempt=1,
            agent_id=uuid.uuid4(),
            content=("sha256:" + "cd" * 32, 42),
        )
        with self.assertRaises(NodeStopped):
            await handle.record(L.CLOUD, agent="codex", model="gpt-5-codex")
        self.assertIsNone((await self.attempt_row(dag.id))["placement"])

    async def test_ensure_active_says_whether_the_attempt_may_still_act(self):
        # What ``HybridRuntime`` asks when the runtime it chose records the
        # placement already on record: it writes nothing.
        dag, _ = await self.running()
        fence = AttemptFence()
        handle = self.handle(dag, fence=fence)
        await handle.record(L.CLOUD, agent="codex", model="gpt-5-codex")
        await handle.ensure_active()
        fence.close()
        with self.assertRaises(NodeStopped) as caught:
            await handle.ensure_active()
        self.assertEqual(caught.exception.reason, StopReason.ABANDONED)
        guard = RunGuard(dag.task_id, RUN, ALWAYS_ACTIVE)
        guard.stop(StopReason.TASK_ENDED)
        stopped = NodePlacementHandle(
            guard,
            self.store,
            dag_id=dag.id,
            epoch=1,
            node_key="a",
            attempt=1,
            agent_id=uuid.uuid4(),
            content=("sha256:" + "cd" * 32, 42),
        )
        with self.assertRaises(NodeStopped) as caught:
            await stopped.ensure_active()
        self.assertEqual(caught.exception.reason, StopReason.TASK_ENDED)
        (record,) = await self.store.attempts(dag.id, "a")
        self.assertEqual(record.placement, L.CLOUD)

    async def test_a_replaced_run_stops_the_node(self):
        dag, _ = await self.running()
        await self.service.execute(dag.task_id, TaskCommand.CANCEL, actor=self.system)

        class Ended:
            async def check(self, task_id, run):
                return TaskActivity.ENDED

        handle = NodePlacementHandle(
            RunGuard(dag.task_id, RUN, Ended()),
            self.store,
            dag_id=dag.id,
            epoch=1,
            node_key="a",
            attempt=1,
            agent_id=uuid.uuid4(),
            content=("sha256:" + "cd" * 32, 42),
        )
        with self.assertRaises(NodeStopped) as caught:
            await handle.record(L.LOCAL_GPU, agent="local", model="main")
        self.assertEqual(caught.exception.reason, StopReason.TASK_ENDED)


class CloudRecording(FakeRuntime):
    """A runtime that says it runs every node on a cloud agent."""

    async def run_node(self, assignment):
        await assignment.placement.record(
            ExecutionPlacement.CLOUD, agent="codex", model="gpt-5-codex"
        )
        return await super().run_node(assignment)


def fill_main(scheduler):
    async def fill():
        leases = []
        for _ in range(3):
            admission = await scheduler.try_acquire(
                ComputeRequest(
                    ResourceClass.INTERACTIVE, deployment="main", context_tokens=35_000
                )
            )
            leases.append(admission.lease)
        return leases

    return fill()


class Allow:
    async def allows(self, assignment):
        return True


@requires_postgres
class OrchestratorPlacementTest(PlacementStoreCase):
    async def test_every_node_attempt_gets_a_placement_and_the_planner_none(self):
        planner = FakeRuntime("local", script={"plan": PLANNED})
        h = self.harness(runtimes={"local": planner})
        task_id = await self.create_task()
        await h.orchestrator.enqueue_task(task_id, preset="standard")

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, RunOutcome.DAG_SUCCEEDED)
        (plan_call,) = planner.calls_of("plan")
        self.assertIsNone(plan_call.placement)
        (node_call,) = planner.calls_of("a")
        self.assertIsInstance(node_call.placement, NodePlacementHandle)
        # A runtime that reports nothing leaves the placement NULL.
        dag = await self.store.get(task_id, 1)
        (record,) = await self.store.attempts(dag.id, "a")
        self.assertIsNone(record.placement)

    async def test_a_node_sent_to_the_cloud_is_on_record_and_audited(self):
        runtime = CloudRecording("local")
        h = self.harness(runtimes={"local": runtime})
        plan = make_plan(node("a", input={"issue": 7}), node("b", "a"))
        task_id = await self.prepare(h, plan)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, RunOutcome.DAG_SUCCEEDED)
        dag = await self.store.get(task_id, 1)
        for key in ("a", "b"):
            (assignment,) = runtime.calls_of(key)
            (record,) = await self.store.attempts(dag.id, key)
            with self.subTest(key):
                self.assertEqual(record.state, AttemptState.SUCCEEDED)
                self.assertEqual(
                    (record.placement, record.placement_agent, record.placement_model),
                    (L.CLOUD, "codex", "gpt-5-codex"),
                )
                # The fingerprint of what the orchestrator handed the node.
                self.assertEqual(
                    (record.content_fingerprint, record.content_bytes),
                    content_digest(
                        node_key=key,
                        role=assignment.role,
                        title=assignment.title,
                        goal=assignment.goal,
                        input=assignment.input,
                        upstream=assignment.upstream,
                    ),
                )
                row = await self.audit_row(record.placement_audit_id)
                self.assertEqual(row["resource_id"], task_id)
                self.assertEqual(
                    row["agent_id"], agent_id_of(task_id, RUN, key, record.number)
                )
        # b's content includes a's result: the two fingerprints differ.
        a, b = [(await self.store.attempts(dag.id, k))[0] for k in ("a", "b")]
        self.assertNotEqual(a.content_fingerprint, b.content_fingerprint)

    async def test_the_hybrid_runtime_records_local_and_cloud_attempts(self):
        scheduler, _probe, _control, clock = build()
        await scheduler.refresh()
        local = FakeRuntime("local")
        cloud = FakeRuntime("cloud")
        hybrid = HybridRuntime(
            scheduler,
            local,
            deployment="main",
            cloud=cloud,
            cloud_policy=Allow(),
            cloud_agent="codex",
            cloud_model="gpt-5-codex",
            clock=clock,
            wait_seconds=0,
            # About what a large node needs: it fits an idle GPU, not a busy one.
            estimate=lambda assignment: 30_000,
        )
        h = self.harness(runtimes={"local-coder": hybrid}, ladder=("local-coder",))
        first = await self.prepare(h, make_plan(node("a")))
        self.assertEqual(
            (await h.orchestrator.run_once("w1")).outcome, RunOutcome.DAG_SUCCEEDED
        )
        await fill_main(scheduler)  # the local GPU is now busy
        second = await self.prepare(h, make_plan(node("a")))
        self.assertEqual(
            (await h.orchestrator.run_once("w1")).outcome, RunOutcome.DAG_SUCCEEDED
        )

        local_dag = await self.store.get(first, 1)
        (on_gpu,) = await self.store.attempts(local_dag.id, "a")
        self.assertEqual(
            (on_gpu.placement, on_gpu.placement_agent, on_gpu.placement_model),
            (L.LOCAL_GPU, "local-coder", "main"),
        )
        self.assertIsNone(on_gpu.placement_audit_id)
        cloud_dag = await self.store.get(second, 1)
        (in_cloud,) = await self.store.attempts(cloud_dag.id, "a")
        self.assertEqual(
            (in_cloud.placement, in_cloud.placement_agent, in_cloud.placement_model),
            (L.CLOUD, "codex", "gpt-5-codex"),
        )
        self.assertEqual(len(cloud.calls_of("a")), 1)
        row = await self.audit_row(in_cloud.placement_audit_id)
        self.assertEqual(row["action"], CLOUD_SEND_ACTION)
        self.assertEqual(row["project_id"], self.project_id)


if __name__ == "__main__":
    unittest.main()
