"""The production ``CloudPolicy`` (Decision 0099, 2) and the single-user
recount in the record's transaction.

``ConfiguredCloudPolicy`` answers ``True`` only when the switch is on, the
agent is a configured CLI runtime on the role's ladder, the deployment has
exactly one active user and the task's project is not being deleted; anything
it cannot tell is ``False``. The first classes use a fake reader of the
database. The PostgreSQL classes (skipped unless ``PAW_TEST_DATABASE_URL`` is
set) check the reader, the recount under the advisory lock in the placement
record's transaction (``DagStore`` ``cloud_send_check``), and that a user's
activation and the recount are serialised (Decision 0099, "Test の方針").
``test_orchestrator_grants`` repeats the PostgreSQL classes as the
unprivileged application role.
"""

import asyncio
import unittest
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from paw_backend.agents.cloud import CloudGate
from paw_backend.agents.cloud_policy import (
    CloudFacts,
    CloudSettings,
    ConfiguredCloudPolicy,
    PostgresCloudFacts,
)
from paw_backend.agents.failures import Failure, RuntimeFailure
from paw_backend.connections import ConnectionKind, SessionRequest, UsagePurpose
from paw_backend.identity.single_user import (
    lock_user_activation,
    recheck_single_user_in,
)
from paw_backend.orchestrator.audit import CLOUD_SEND_ACTION
from paw_backend.orchestrator.domain import ExecutionPlacement, NodeRole
from paw_backend.orchestrator.errors import CloudSendRefusedError
from paw_backend.orchestrator.gateway import RunGuard
from paw_backend.orchestrator.placement import NodePlacementHandle
from paw_backend.orchestrator.runtime import NodeAssignment
from paw_backend.orchestrator.store import DagStore
from paw_backend.tasks import TaskRun

from .gate_support import ALWAYS_ACTIVE
from .task_support import new_database, requires_postgres
from .test_orchestrator_placement import PlacementStoreCase, a_send

L = ExecutionPlacement
LADDERS = {
    NodeRole.PLANNER: ("claude", "local"),
    NodeRole.WORKER: ("local", "codex"),
    NodeRole.RESEARCHER: ("local", "claude"),
    NodeRole.REVIEWER: ("claude", "codex"),
}
ONE_USER_ACTIVE_PROJECT = CloudFacts(active_users=1, project_status="active")


class Facts:
    def __init__(self, answer=ONE_USER_ACTIVE_PROJECT, error=None):
        self.answer = answer
        self.error = error
        self.asked = []

    async def read(self, task_id):
        self.asked.append(task_id)
        if self.error is not None:
            raise self.error
        return self.answer


def policy(
    *, enabled=True, facts=None, agents=("claude", "codex"), ladders=LADDERS
) -> ConfiguredCloudPolicy:
    return ConfiguredCloudPolicy(
        CloudSettings(enabled=enabled),
        ladders=ladders,
        agents=agents,
        facts=facts if facts is not None else Facts(),
    )


def assignment(role=NodeRole.REVIEWER, agent="claude", **overrides) -> NodeAssignment:
    values = dict(
        task_id=uuid.uuid4(),
        node_key="paw-review-claude",
        role=role,
        title="t",
        goal="g",
        input={},
        upstream={},
        agent=agent,
        attempt=1,
        approach=0,
        tools=None,
        budget=None,
    )
    values.update(overrides)
    return NodeAssignment(**values)


class CloudSettingsTest(unittest.TestCase):
    def test_off_by_default_and_single_user_only(self):
        self.assertEqual(CloudSettings(), CloudSettings(False, True))
        with self.assertRaises(ValueError):
            CloudSettings(enabled=True, single_user_only=False)
        with self.assertRaises(TypeError):
            CloudSettings(enabled=1)

    def test_the_policy_checks_its_arguments(self):
        with self.assertRaises(TypeError):
            ConfiguredCloudPolicy(
                {"enabled": True}, ladders=LADDERS, agents=(), facts=Facts()
            )
        with self.assertRaises(TypeError):
            ConfiguredCloudPolicy(
                CloudSettings(),
                ladders={"worker": ("local",)},
                agents=(),
                facts=Facts(),
            )
        with self.assertRaises(TypeError):
            ConfiguredCloudPolicy(
                CloudSettings(), ladders=LADDERS, agents=(), facts=object()
            )


class AllowsTest(unittest.IsolatedAsyncioTestCase):
    """Decision 0099, 2, conditions 1 to 4; fail closed."""

    async def test_everything_holds(self):
        facts = Facts()
        work = assignment()
        self.assertIs(await policy(facts=facts).allows(work), True)
        self.assertEqual(facts.asked, [work.task_id])
        for role, agent in (
            (NodeRole.REVIEWER, "codex"),
            (NodeRole.PLANNER, "claude"),
            (NodeRole.RESEARCHER, "claude"),
            # A worker reaches codex only by an escalation (point 4 A): the
            # policy allows the rung; the plan's check keeps it from starting
            # there (Decision 0099, 4).
            (NodeRole.WORKER, "codex"),
        ):
            with self.subTest(role=role, agent=agent):
                self.assertIs(await policy().allows(assignment(role, agent)), True)
        archived = Facts(CloudFacts(1, "archived"))
        self.assertIs(await policy(facts=archived).allows(assignment()), True)

    async def test_the_switch_is_off(self):
        facts = Facts()
        self.assertIs(
            await policy(enabled=False, facts=facts).allows(assignment()), False
        )
        self.assertEqual(facts.asked, [])

    async def test_the_agent_is_not_a_configured_cli_rung_of_the_role(self):
        for role, agent, agents in (
            # The worker's ladder has no claude.
            (NodeRole.WORKER, "claude", ("claude", "codex")),
            # The local runtime never goes out.
            (NodeRole.WORKER, "local", ("claude", "codex")),
            # Codex is not a planner's rung.
            (NodeRole.PLANNER, "codex", ("claude", "codex")),
            # An agent the file has no [agents.<name>] for.
            (NodeRole.REVIEWER, "codex", ("claude",)),
            # An unknown label.
            (NodeRole.REVIEWER, "gemini", ("claude", "codex", "gemini")),
        ):
            with self.subTest(role=role, agent=agent):
                facts = Facts()
                self.assertIs(
                    await policy(facts=facts, agents=agents).allows(
                        assignment(role, agent)
                    ),
                    False,
                )
                self.assertEqual(facts.asked, [])
        # A role without a ladder.
        partial = {NodeRole.REVIEWER: ("claude",)}
        self.assertIs(
            await policy(ladders=partial).allows(assignment(NodeRole.RESEARCHER)),
            False,
        )

    async def test_not_exactly_one_active_user(self):
        for users in (0, 2, 5):
            with self.subTest(users=users):
                facts = Facts(CloudFacts(users, "active"))
                self.assertIs(await policy(facts=facts).allows(assignment()), False)

    async def test_the_project(self):
        for status in ("pending_deletion", "deleted", None, "unknown", 1):
            with self.subTest(status=status):
                facts = Facts(CloudFacts(1, status))
                self.assertIs(await policy(facts=facts).allows(assignment()), False)

    async def test_what_it_cannot_tell_is_false(self):
        for answer in (
            None,  # no such task
            object(),
            CloudFacts("1", "active"),
            CloudFacts(True, "active"),
        ):
            with self.subTest(answer=answer):
                facts = Facts(answer)
                self.assertIs(await policy(facts=facts).allows(assignment()), False)
        for error in (RuntimeError("the database is down zq-mark"), TimeoutError()):
            with (
                self.subTest(error=error),
                self.assertLogs("paw_backend.agents.cloud_policy", "WARNING") as logs,
            ):
                facts = Facts(error=error)
                self.assertIs(await policy(facts=facts).allows(assignment()), False)
            self.assertNotIn("zq-mark", "".join(logs.output))
        for work in (object(), assignment(role="reviewer"), assignment(agent=None)):
            with self.subTest(work=work):
                self.assertIs(await policy().allows(work), False)

    async def test_the_gate_sends_with_it(self):
        timeline = []

        class Placement:
            async def record(self, placement, *, agent, model):
                timeline.append("placement")

        class Connections:
            async def execute_session(self, kind, request, *, gate=None):
                timeline.append("admission")
                await gate()
                timeline.append("adapter")
                return "done"

        work = assignment(placement=Placement(), connections=Connections())
        request = SessionRequest("claude-opus-4-1", UsagePurpose.CODING, "t", 600)
        gate = CloudGate(policy())
        result = await gate.session(
            work,
            kind=ConnectionKind.CLAUDE,
            request=request,
            agent="claude",
            model="claude-opus-4-1",
        )
        self.assertEqual(result, "done")
        self.assertEqual(timeline, ["admission", "placement", "adapter"])
        with self.assertRaises(RuntimeFailure) as caught:
            await CloudGate(policy(enabled=False)).session(
                work,
                kind=ConnectionKind.CLAUDE,
                request=request,
                agent="claude",
                model="claude-opus-4-1",
            )
        self.assertIs(caught.exception.failure, Failure.CLOUD_POLICY_DENIED)

    async def test_a_refused_recount_is_a_refused_gate(self):
        """The record's transaction refused the send (Decision 0099, 2): the
        gate sends nothing and answers ``CLOUD_POLICY_DENIED``."""
        timeline = []

        class Placement:
            async def record(self, placement, *, agent, model):
                timeline.append("placement")
                raise CloudSendRefusedError()

        class Connections:
            async def execute_session(self, kind, request, *, gate=None):
                timeline.append("admission")
                await gate()
                timeline.append("adapter")

        work = assignment(placement=Placement(), connections=Connections())
        with self.assertRaises(RuntimeFailure) as caught:
            await CloudGate(policy()).session(
                work,
                kind=ConnectionKind.CLAUDE,
                request=SessionRequest("m", UsagePurpose.CODING, "t", 600),
                agent="claude",
                model="m",
            )
        self.assertIs(caught.exception.failure, Failure.CLOUD_POLICY_DENIED)
        self.assertEqual(timeline, ["admission", "placement"])

    async def test_recheck_in_when_the_switch_is_off(self):
        class NoSession:
            async def execute(self, *args, **kwargs):
                raise AssertionError("the database is not asked")

        self.assertIs(await policy(enabled=False).recheck_in(NoSession()), False)


class SingleUserCase(PlacementStoreCase):
    """A running attempt, the users table emptied, and helpers to add users and
    a project."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.owner_sql("TRUNCATE users CASCADE")

    async def add_user(self, status: str = "active") -> uuid.UUID:
        user_id = uuid.uuid4()
        await self.owner_sql(
            "INSERT INTO users (id, login_name, system_role, status,"
            " passkey_required, created_at, updated_at) VALUES (:id, :login,"
            " 'user', :status, false, :now, :now)",
            id=user_id,
            login="u" + user_id.hex[:12],
            status=status,
            now=datetime.now(UTC),
        )
        return user_id

    async def add_project(self, status: str = "active") -> None:
        started = (
            datetime.now(UTC) if status in ("pending_deletion", "deleted") else None
        )
        scheduled = None
        if started is not None:
            scheduled = started + timedelta(days=30)
        await self.owner_sql(
            "INSERT INTO projects (id, name, description, status, created_by,"
            " created_at, updated_at, deletion_started_at, deletion_scheduled_at,"
            " deleted_at) VALUES (:id, :name, NULL, :status, NULL, :now, :now,"
            " :started, :scheduled, :deleted)",
            id=self.project_id,
            name="Deleted Project" if status == "deleted" else "Alpha",
            status=status,
            now=datetime.now(UTC),
            started=started,
            scheduled=scheduled,
            deleted=scheduled if status == "deleted" else None,
        )

    def owner_database(self):
        """An engine as the schema's owner: the application role cannot change
        a user's status by an UPDATE (an activation's own function does)."""
        database = new_database()
        self.addAsyncCleanup(database.dispose)
        return database

    def the_policy(self) -> ConfiguredCloudPolicy:
        return ConfiguredCloudPolicy(
            CloudSettings(enabled=True),
            ladders=LADDERS,
            agents=("claude", "codex"),
            facts=PostgresCloudFacts(self.database),
        )

    def checked_store(self) -> DagStore:
        return DagStore(self.database, cloud_send_check=self.the_policy().recheck_in)

    async def record_cloud(self, store: DagStore, dag_id):
        return await store.record_placement(
            dag_id,
            1,
            "a",
            1,
            placement=L.CLOUD,
            agent="codex",
            model="gpt-5-codex",
            cloud=a_send(),
        )

    async def sends(self, task_id) -> int:
        (row,) = await self.rows(
            "SELECT count(*) AS n FROM audit_events WHERE action = :action"
            " AND resource_id = :task",
            action=CLOUD_SEND_ACTION,
            task=task_id,
        )
        return row["n"]


@requires_postgres
class PostgresCloudFactsTest(SingleUserCase):
    async def test_the_facts_and_the_policy(self):
        dag, _ = await self.running()
        await self.add_user()
        await self.add_user("invited")
        await self.add_user("pending_deletion")
        await self.add_user("deleted")
        facts = PostgresCloudFacts(self.database)
        # No project row: the task's project is unknown.
        self.assertEqual(await facts.read(dag.task_id), CloudFacts(1, None))
        self.assertIsNone(await facts.read(uuid.uuid4()))
        work = assignment(task_id=dag.task_id)
        self.assertIs(await self.the_policy().allows(work), False)
        await self.add_project()
        self.assertEqual(await facts.read(dag.task_id), CloudFacts(1, "active"))
        self.assertIs(await self.the_policy().allows(work), True)
        await self.add_user()
        self.assertIs(await self.the_policy().allows(work), False)

    async def test_a_project_being_deleted(self):
        dag, _ = await self.running()
        await self.add_user()
        await self.add_project("pending_deletion")
        self.assertIs(
            await self.the_policy().allows(assignment(task_id=dag.task_id)), False
        )


@requires_postgres
class SingleUserRecheckTest(SingleUserCase):
    """The recount in the placement record's transaction (Decision 0099, 2)."""

    async def test_one_active_user_records_the_send(self):
        dag, _ = await self.running()
        await self.add_user()
        await self.add_user("invited")
        record = await self.record_cloud(self.checked_store(), dag.id)
        self.assertEqual(record.placement, L.CLOUD)
        self.assertEqual(await self.sends(dag.task_id), 1)

    async def test_two_or_no_active_users_record_and_send_nothing(self):
        for users in (2, 0):
            with self.subTest(users=users):
                await self.owner_sql("TRUNCATE users CASCADE")
                dag, _ = await self.running()
                for _ in range(users):
                    await self.add_user()
                with self.assertRaises(CloudSendRefusedError):
                    await self.record_cloud(self.checked_store(), dag.id)
                row = await self.attempt_row(dag.id)
                self.assertIsNone(row["placement"])
                self.assertIsNone(row["placement_audit_id"])
                self.assertEqual(await self.sends(dag.task_id), 0)
                # A local placement is not the policy's: it is recorded.
                await self.checked_store().record_placement(
                    dag.id, 1, "a", 1, placement=L.LOCAL_GPU, agent="local", model="m"
                )

    async def test_a_store_without_the_check_records_as_before(self):
        dag, _ = await self.running()
        await self.add_user()
        await self.add_user()
        record = await self.record_cloud(self.store, dag.id)
        self.assertEqual(record.placement, L.CLOUD)

    async def test_the_handle_lets_the_refusal_through(self):
        """``NodePlacementHandle.record`` passes the refusal to the gate."""
        dag, _ = await self.running()
        await self.add_user()
        await self.add_user()
        guard = RunGuard(dag.task_id, TaskRun(1, 0), ALWAYS_ACTIVE)
        handle = NodePlacementHandle(
            guard,
            self.checked_store(),
            dag_id=dag.id,
            epoch=1,
            node_key="a",
            attempt=1,
            agent_id=uuid.uuid4(),
            content=("sha256:" + "cd" * 32, 10),
        )
        with self.assertRaises(CloudSendRefusedError):
            await handle.record(L.CLOUD, agent="codex", model="gpt-5-codex")
        self.assertIsNone(guard.stop_reason)
        self.assertEqual(await self.sends(dag.task_id), 0)

    async def test_an_activation_in_progress_holds_the_record_back(self):
        """A user being made active (the exclusive lock taken) after the
        policy's ``allows`` and before the record: the record waits for it,
        counts two users, and writes and sends nothing."""
        dag, _ = await self.running()
        await self.add_user()
        second = await self.add_user("invited")
        activation = self.owner_database()
        async with activation.engine.connect() as connection:
            transaction = await connection.begin()
            await lock_user_activation(connection)
            await connection.execute(
                text("UPDATE users SET status = 'active' WHERE id = :id"),
                {"id": second},
            )
            record = asyncio.ensure_future(
                self.record_cloud(self.checked_store(), dag.id)
            )
            await asyncio.sleep(0.5)
            self.assertFalse(record.done())
            await transaction.commit()
        with self.assertRaises(CloudSendRefusedError):
            await record
        self.assertIsNone((await self.attempt_row(dag.id))["placement"])
        self.assertEqual(await self.sends(dag.task_id), 0)

    async def test_a_recount_holds_an_activation_back(self):
        """The recount holds the lock shared until its transaction ends: an
        activation waits, so a recorded send was the send of one user."""
        await self.add_user()
        second = await self.add_user("invited")
        recount = self.new_database()
        activated = asyncio.Event()

        async def activate():
            database = self.owner_database()
            async with database.engine.begin() as connection:
                await lock_user_activation(connection)
                await connection.execute(
                    text("UPDATE users SET status = 'active' WHERE id = :id"),
                    {"id": second},
                )
            activated.set()

        async with recount.engine.connect() as connection:
            transaction = await connection.begin()
            self.assertIs(await recheck_single_user_in(connection), True)
            waiting = asyncio.ensure_future(activate())
            await asyncio.sleep(0.5)
            self.assertFalse(activated.is_set())
            # Still one, counted again in the same transaction.
            self.assertIs(await recheck_single_user_in(connection), True)
            await transaction.commit()
        await waiting
        self.assertTrue(activated.is_set())
        async with recount.engine.begin() as connection:
            self.assertIs(await recheck_single_user_in(connection), False)

    async def test_recounts_do_not_wait_for_each_other(self):
        await self.add_user()
        first, second = self.new_database(), self.new_database()
        async with first.engine.begin() as a, second.engine.begin() as b:
            self.assertIs(await recheck_single_user_in(a), True)
            self.assertIs(
                await asyncio.wait_for(recheck_single_user_in(b), timeout=5), True
            )


if __name__ == "__main__":
    unittest.main()
