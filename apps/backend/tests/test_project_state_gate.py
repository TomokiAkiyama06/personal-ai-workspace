"""The Project state gate of the task lane (Issue #83, Decisions 0008 and 0020).

``TaskService.create_task`` / Retry / Restart / Start and ``TaskQueue.enqueue`` lock
the project row ``FOR SHARE`` in the transaction of their own write and refuse unless
the project is Active; the gate is mandatory. The tasks and queue entries are seeded
through the owner's services, which are built with the tests' ``AlwaysActiveGate``,
and every assertion reads the database with SQL. The race
tests interleave the gated write with a Delete / Archive through explicit hooks
(a gate that holds its transaction open until told to go on) and through row locks
that another connection holds, never through sleeps; a wait is proven by asking
PostgreSQL which backends are blocked on a lock. Every test runs once as the owner
and once as the unprivileged application role (``test_projects_grants``).
"""

import ast
import asyncio
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from paw_backend.projects import (
    InvalidProjectInputError,
    ProjectBusyError,
    ProjectStateGate,
    ProjectStatus,
)
from paw_backend.tasks import (
    Actor,
    IllegalTransitionError,
    InvalidCommandArgumentError,
    ProjectNotActiveError,
    TaskCommand,
    TaskNotFoundError,
    TaskService,
    TaskState,
    WaitReason,
)
from paw_backend.tasks.queueing import (
    InvalidQueueingArgumentError,
    LeaseLostError,
    TaskAlreadyQueuedError,
    TaskQueue,
)

from . import test_projects_task_stop as stop_tests
from .gate_support import ALWAYS_ACTIVE
from .projects_support import T0, requires_postgres

NOT_ACTIVE = (
    ProjectStatus.ARCHIVED,
    ProjectStatus.PENDING_DELETION,
    ProjectStatus.DELETED,
)
DEADLINE = 30


BACKEND = Path(__file__).resolve().parents[1]


class LayeringTest(unittest.TestCase):
    """``tasks`` must not import ``projects``: the gate is injected."""

    def test_importing_the_task_lane_loads_nothing_of_the_project_module(self):
        code = (
            "import sys, paw_backend.tasks, paw_backend.tasks.queueing;"
            "print(sorted(m for m in sys.modules"
            " if m.startswith('paw_backend.projects')))"
        )

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=BACKEND,
            check=True,
        )

        self.assertEqual(result.stdout.strip(), "[]")

    def test_no_source_file_of_the_task_lane_imports_the_project_module(self):
        files = sorted((BACKEND / "paw_backend" / "tasks").rglob("*.py"))
        self.assertGreaterEqual(len(files), 15)  # the whole lane is scanned
        imported: dict[str, list[str]] = {}
        for path in files:
            for node in ast.walk(ast.parse(path.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                    if node.level:  # a relative import of a sibling package
                        names = [("." * node.level) + names[0]]
                for name in names:
                    if name.startswith(
                        "paw_backend.projects"
                    ) or "projects" in name.split("."):
                        imported.setdefault(str(path.relative_to(BACKEND)), []).append(
                            name
                        )

        self.assertEqual(imported, {})


class RecordingGate:
    """The real gate, and a record of the projects it was asked about."""

    def __init__(self, inner: ProjectStateGate) -> None:
        self.inner = inner
        self.calls: list[UUID] = []
        self.conditions = 0  # how often the claim asked for its SQL condition

    async def require_active(self, session, project_id) -> None:
        self.calls.append(project_id)
        await self.inner.require_active(session, project_id)

    def active_condition(self, project_id):
        self.conditions += 1
        return self.inner.active_condition(project_id)


class PausingGate:
    """Takes the real lock, then keeps the write's transaction open until released."""

    def __init__(self, inner: ProjectStateGate) -> None:
        self.inner = inner
        self.locked = asyncio.Event()
        self.release = asyncio.Event()

    async def require_active(self, session, project_id) -> None:
        await self.inner.require_active(session, project_id)
        self.locked.set()
        await self.release.wait()

    def active_condition(self, project_id):
        return self.inner.active_condition(project_id)


class GateTestCase(stop_tests.TaskStopTestCase):
    """A project with a team; gated task and queue services on ``self.database``."""

    gate = ProjectStateGate()

    # The gated services are built from ``self.database`` when they are used, not in
    # ``asyncSetUp``: ``test_projects_grants`` swaps ``self.database`` for one that
    # connects as the unprivileged application role AFTER ``asyncSetUp`` ran, and the
    # services under test must follow it (the seeding ones, in the base class, stay
    # on the owner's connection).
    @property
    def tasks(self) -> TaskService:
        return TaskService(self.database, project_gate=self.gate)

    @property
    def queue(self) -> TaskQueue:
        return TaskQueue(self.database, project_gate=self.gate)

    def project_in(self, status: ProjectStatus) -> UUID:
        return self.seed_project(status, name=f"Project {status.value}")

    async def create(self, service: TaskService | None = None, **overrides: Any):
        arguments: dict[str, Any] = {
            "project_id": self.project_id,
            "created_by": self.team.manager,
            "title": "Fix the parser",
        }
        arguments.update(overrides)
        return await (service or self.tasks).create_task(**arguments)

    def count(self, table: str, where: str = "true", **parameters: Any) -> int:
        (found,) = self.scalars(
            f"SELECT count(*) FROM {table} WHERE {where}", **parameters
        )
        return found

    def rows_of_tasks(self) -> tuple[int, int, int]:
        """(tasks, attempts, events): all that a created task would have written."""
        return (
            self.count("tasks"),
            self.count("task_attempts"),
            self.count("task_events"),
        )

    def task_columns(self, task_id: UUID) -> tuple[Any, ...]:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT state, attempt, retry_count, version FROM tasks"
                    " WHERE id = :id"
                ),
                {"id": task_id},
            ).one()
        return tuple(row)

    def blocked_backends(self) -> int:
        return self.scalars(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE datname = current_database() AND wait_event_type = 'Lock'"
        )[0]

    async def wait_until_blocked(self, count: int = 1) -> None:
        """Wait until ``count`` backends wait for a lock held by another one."""
        async with asyncio.timeout(DEADLINE):
            while True:
                if await asyncio.to_thread(self.blocked_backends) >= count:
                    return
                await asyncio.sleep(0.02)

    def project_is_share_locked(self, project_id: UUID) -> bool:
        """Whether another connection is refused ``FOR UPDATE`` on the project now."""
        connection = self.engine.connect()
        try:
            with connection.begin():
                try:
                    connection.execute(
                        text(
                            "SELECT id FROM projects WHERE id = :id FOR UPDATE NOWAIT"
                        ),
                        {"id": project_id},
                    )
                except DBAPIError as error:
                    return error.orig.sqlstate == "55P03"  # lock_not_available
                return False
        finally:
            connection.close()

    def commit_lifecycle_change(self, status: str, holder) -> None:
        """Change the project on the connection that holds its row, then commit."""
        connection, transaction = holder
        started = T0
        connection.execute(
            text(
                "UPDATE projects SET status = :status,"
                " deletion_started_at = :started, deletion_scheduled_at = :due"
                " WHERE id = :id"
            ),
            {
                "status": status,
                "started": started if status == "pending_deletion" else None,
                "due": (started + stop_tests.DAYS_30)
                if status == "pending_deletion"
                else None,
                "id": self.project_id,
            },
        )
        transaction.commit()


@requires_postgres
class CreateTaskGateTest(GateTestCase):
    async def test_an_active_project_admits_the_task(self):
        event = await self.create()

        self.assertEqual(self.task_columns(event.task_id), ("queued", 1, 0, 1))
        self.assertEqual(self.commands(event.task_id), ["create"])
        self.assertEqual(
            self.scalars(
                "SELECT project_id FROM tasks WHERE id = :id", id=event.task_id
            ),
            [self.project_id],
        )

    async def test_a_project_that_is_not_active_refuses_and_writes_nothing(self):
        for status in NOT_ACTIVE:
            with self.subTest(status=status.value):
                project_id = self.project_in(status)
                before = self.rows_of_tasks()

                with self.assertRaises(ProjectNotActiveError) as caught:
                    await self.create(project_id=project_id)

                self.assertEqual(str(caught.exception), "The project is not active")
                self.assertEqual(caught.exception.code, "project_not_active")
                self.assertEqual(self.rows_of_tasks(), before)
                self.assertEqual(
                    self.count("tasks", "project_id = :p", p=project_id), 0
                )

    async def test_an_unknown_project_is_refused_like_one_that_is_not_active(self):
        before = self.rows_of_tasks()

        with self.assertRaises(ProjectNotActiveError):
            await self.create(project_id=uuid4())

        self.assertEqual(self.rows_of_tasks(), before)

    async def test_the_test_gate_that_says_so_admits_a_task_in_any_project(self):
        # ``AlwaysActiveGate``: the explicit, named gate of the tests and tools of the
        # task lane that have no projects. There is no other way to build a service
        # without asking a real gate (see ``GateIsMandatoryTest``).
        service = TaskService(self.database, project_gate=ALWAYS_ACTIVE)
        for project_id in (self.project_in(ProjectStatus.ARCHIVED), uuid4()):
            event = await self.create(service, project_id=project_id)
            self.assertEqual(self.task_state(event.task_id), "queued")

    async def test_a_bad_argument_is_refused_before_the_gate_is_asked(self):
        gate = RecordingGate(self.gate)

        with self.assertRaises(InvalidCommandArgumentError):
            await self.create(TaskService(self.database, project_gate=gate), title="")

        self.assertEqual(gate.calls, [])


@requires_postgres
class RetryAndRestartGateTest(GateTestCase):
    async def seed_in(self, project_id: UUID, state: TaskState) -> UUID:
        return await self.seed_task(state, project_id=project_id)

    async def test_retry_and_restart_are_refused_unless_the_project_is_active(self):
        for status in NOT_ACTIVE:
            for command, state in (
                (TaskCommand.RETRY, TaskState.FAILED),
                (TaskCommand.RESTART, TaskState.FAILED),
                (TaskCommand.RESTART, TaskState.CANCELLED),
            ):
                with self.subTest(
                    status=status.value, command=command.value, state=state
                ):
                    project_id = self.project_in(status)
                    task_id = await self.seed_in(project_id, state)
                    before = (self.task_columns(task_id), self.commands(task_id))

                    with self.assertRaises(ProjectNotActiveError):
                        await self.tasks.execute(
                            task_id, command, actor=Actor.user(self.team.manager)
                        )

                    self.assertEqual(
                        (self.task_columns(task_id), self.commands(task_id)), before
                    )
                    self.assertEqual(self.task_state(task_id), state.value)

    async def test_they_work_in_an_active_project(self):
        failed = await self.seed_in(self.project_id, TaskState.FAILED)
        cancelled = await self.seed_in(self.project_id, TaskState.CANCELLED)
        actor = Actor.user(self.team.manager)

        await self.tasks.execute(failed, TaskCommand.RETRY, actor=actor)
        await self.tasks.execute(cancelled, TaskCommand.RESTART, actor=actor)

        self.assertEqual(self.task_columns(failed)[:3], ("queued", 1, 1))
        self.assertEqual(self.task_columns(cancelled)[:3], ("queued", 2, 0))

    async def test_an_illegal_command_is_reported_before_the_project(self):
        project_id = self.project_in(ProjectStatus.ARCHIVED)
        running = await self.seed_in(project_id, TaskState.RUNNING)

        for command in (TaskCommand.RETRY, TaskCommand.RESTART):
            with self.subTest(command=command.value):
                with self.assertRaises(IllegalTransitionError):
                    await self.tasks.execute(
                        running, command, actor=Actor.user(self.team.manager)
                    )

    async def test_the_other_commands_never_ask_the_gate(self):
        # Cancel (which the stop processor issues in a Pending deletion project),
        # Fail, Pause, Resume, ...: only the ways to admit or BEGIN work are gated
        # (create, Retry, Restart, Start), so a stop always works and running work
        # goes on (Decision 0020, C).
        gate = RecordingGate(self.gate)
        tasks = TaskService(self.database, project_gate=gate)
        project_id = self.project_in(ProjectStatus.PENDING_DELETION)
        running = await self.seed_in(project_id, TaskState.RUNNING)
        cancelled = await self.seed_in(project_id, TaskState.RUNNING)
        system = Actor.system()

        await tasks.execute(running, TaskCommand.PAUSE, actor=system)
        await tasks.execute(running, TaskCommand.RESUME, actor=system)
        await tasks.execute(
            running, TaskCommand.WAIT, actor=system, wait_reason=WaitReason.USER
        )
        await tasks.execute(running, TaskCommand.UNBLOCK, actor=system)
        await tasks.execute(running, TaskCommand.FAIL, actor=system)
        await tasks.execute(cancelled, TaskCommand.CANCEL, actor=Actor.policy())

        self.assertEqual(gate.calls, [])
        self.assertEqual(self.task_state(running), "failed")
        self.assertEqual(self.task_state(cancelled), "cancelled")

    async def test_start_asks_the_gate_for_the_project_of_the_task(self):
        gate = RecordingGate(self.gate)
        tasks = TaskService(self.database, project_gate=gate)
        queued = await self.seed_in(self.project_id, TaskState.QUEUED)

        await tasks.execute(queued, TaskCommand.START, actor=Actor.system())

        self.assertEqual(gate.calls, [self.project_id])
        self.assertEqual(self.task_state(queued), "running")

    async def test_a_stopped_task_restarts_only_after_the_project_is_active_again(self):
        # Decision 0008, section 8: a Restore leaves the project Archived, which
        # admits no new work; the Manager unarchives it, then Restart works.
        task_id = await self.seed_task(TaskState.RUNNING)
        await self.begin_deletion()
        await self.new_stopper().stop_project_tasks(self.project_id)
        self.assertEqual(self.task_state(task_id), "cancelled")
        actor = Actor.user(self.team.manager)

        await self.service.restore(self.manager, self.project_id)
        with self.assertRaises(ProjectNotActiveError):
            await self.tasks.execute(task_id, TaskCommand.RESTART, actor=actor)
        self.assertEqual(self.task_state(task_id), "cancelled")

        await self.service.unarchive(self.manager, self.project_id)
        await self.tasks.execute(task_id, TaskCommand.RESTART, actor=actor)
        entry = await self.queue.enqueue(task_id)

        self.assertEqual(self.task_columns(task_id)[:3], ("queued", 2, 0))
        self.assertEqual(entry.task_id, task_id)


@requires_postgres
class EnqueueGateTest(GateTestCase):
    async def test_an_active_project_admits_the_entry(self):
        task_id = await self.seed_task(TaskState.QUEUED, queue=False)

        entry = await self.queue.enqueue(task_id)

        self.assertEqual((entry.task_id, entry.status.value), (task_id, "queued"))
        self.assertEqual(self.entry_statuses(task_id), ["queued"])

    async def test_a_project_that_is_not_active_refuses_and_writes_nothing(self):
        for status in NOT_ACTIVE:
            with self.subTest(status=status.value):
                project_id = self.project_in(status)
                task_id = await self.seed_task(
                    TaskState.QUEUED, project_id=project_id, queue=False
                )

                with self.assertRaises(ProjectNotActiveError):
                    await self.queue.enqueue(task_id)

                self.assertEqual(self.entry_statuses(task_id), [])

    async def test_the_gate_is_asked_about_the_project_of_the_task(self):
        gate = RecordingGate(self.gate)
        other = self.seed_project(name="Beta")
        task_id = await self.seed_task(TaskState.QUEUED, project_id=other, queue=False)

        await TaskQueue(self.database, project_gate=gate).enqueue(task_id)

        self.assertEqual(gate.calls, [other])

    async def test_an_unknown_task_is_not_found(self):
        with self.assertRaises(TaskNotFoundError):
            await self.queue.enqueue(uuid4())

    async def test_a_second_active_entry_is_still_refused_as_before(self):
        task_id = await self.seed_task(TaskState.QUEUED)

        with self.assertRaises(TaskAlreadyQueuedError):
            await self.queue.enqueue(task_id)

        self.assertEqual(self.entry_statuses(task_id), ["queued"])

    async def test_the_test_gate_that_says_so_enqueues_for_any_project(self):
        project_id = self.project_in(ProjectStatus.ARCHIVED)
        task_id = await self.seed_task(
            TaskState.QUEUED, project_id=project_id, queue=False
        )

        await TaskQueue(self.database, project_gate=ALWAYS_ACTIVE).enqueue(task_id)

        self.assertEqual(self.entry_statuses(task_id), ["queued"])

    async def test_the_other_operations_never_ask_the_gate(self):
        gate = RecordingGate(self.gate)
        queue = TaskQueue(self.database, project_gate=gate)
        project_id = self.project_in(ProjectStatus.PENDING_DELETION)
        first = await self.seed_task(TaskState.QUEUED, project_id=project_id)
        second = await self.seed_task(TaskState.QUEUED, project_id=project_id)
        # Both entries were claimed before the project began to be deleted: the
        # holder of a lease can still heartbeat, give it back, complete and be
        # cancelled (the project's state decides only who may be handed NEW work).
        held = await self.seed_queue.claim_next("worker-1")
        other = await self.seed_queue.claim_next("worker-2")
        assert held is not None and other is not None

        heartbeat = await queue.heartbeat(held.id, "worker-1", held.claim_count)
        released = await queue.release(held.id, "worker-1", held.claim_count)
        completed = await queue.complete(other.id, "worker-2", other.claim_count)
        cancelled = await queue.cancel(held.task_id)

        self.assertEqual(gate.calls, [])
        self.assertEqual(
            (heartbeat.status.value, released.status.value, completed.status.value),
            ("claimed", "queued", "completed"),
        )
        self.assertTrue(cancelled)  # the released entry, queued again
        self.assertEqual({held.task_id, other.task_id}, {first, second})
        with self.assertRaises(LeaseLostError):
            await queue.heartbeat(held.id, "worker-1", held.claim_count)

    async def test_claiming_asks_for_a_condition_and_never_locks_the_project(self):
        gate = RecordingGate(self.gate)
        queue = TaskQueue(self.database, project_gate=gate)
        task_id = await self.seed_task(TaskState.QUEUED)

        claimed = await queue.claim_next("worker-1")

        assert claimed is not None
        self.assertEqual(claimed.task_id, task_id)
        self.assertEqual((gate.calls, gate.conditions), ([], 1))


@requires_postgres
class GateLockTest(GateTestCase):
    """The lock is taken in the transaction of the write and lasts as long as it."""

    class Probe:
        """Asks the gate, then reports whether the project row is share-locked."""

        def __init__(self, test: "GateLockTest", inner: ProjectStateGate) -> None:
            self.test, self.inner = test, inner
            self.locked_during: list[bool] = []

        async def require_active(self, session, project_id) -> None:
            await self.inner.require_active(session, project_id)
            self.locked_during.append(
                await asyncio.to_thread(self.test.project_is_share_locked, project_id)
            )

        def active_condition(self, project_id):
            return self.inner.active_condition(project_id)

    async def test_every_gated_write_holds_the_share_lock_until_it_commits(self):
        probe = self.Probe(self, self.gate)
        tasks = TaskService(self.database, project_gate=probe)
        queue = TaskQueue(self.database, project_gate=probe)
        actor = Actor.user(self.team.manager)
        failed = await self.seed_task(TaskState.FAILED)
        cancelled = await self.seed_task(TaskState.CANCELLED)
        queued = await self.seed_task(TaskState.QUEUED, queue=False)
        starting = await self.seed_task(TaskState.QUEUED, queue=False)
        self.assertFalse(self.project_is_share_locked(self.project_id))

        await self.create(tasks)
        await tasks.execute(failed, TaskCommand.RETRY, actor=actor)
        await tasks.execute(cancelled, TaskCommand.RESTART, actor=actor)
        await tasks.execute(starting, TaskCommand.START, actor=actor)
        await queue.enqueue(queued)

        # Locked while each write was open, free again once it had committed.
        self.assertEqual(probe.locked_during, [True] * 5)
        self.assertFalse(self.project_is_share_locked(self.project_id))

    async def test_a_refused_write_releases_the_lock(self):
        project_id = self.project_in(ProjectStatus.ARCHIVED)

        with self.assertRaises(ProjectNotActiveError):
            await self.create(project_id=project_id)

        self.assertFalse(self.project_is_share_locked(project_id))

    async def test_a_lock_that_is_not_granted_in_time_is_busy_and_writes_nothing(self):
        tasks = TaskService(
            self.database, project_gate=ProjectStateGate(lock_timeout_ms=50)
        )
        before = self.rows_of_tasks()
        _, transaction = self.lock_project(self.project_id)

        with self.assertRaises(ProjectBusyError):
            await self.create(tasks)
        transaction.rollback()

        self.assertEqual(self.rows_of_tasks(), before)
        event = await self.create(tasks)  # the lock is free: it works
        self.assertEqual(self.task_state(event.task_id), "queued")

    async def test_the_lock_timeout_of_the_transaction_is_put_back(self):
        async def timeout_after_the_gate(setting: str | None) -> str:
            async with self.database.session() as session, session.begin():
                if setting is not None:
                    await session.execute(
                        select(func.set_config("lock_timeout", setting, True))
                    )
                await self.gate.require_active(session, self.project_id)
                return (await session.execute(text("SHOW lock_timeout"))).scalar_one()

        self.assertEqual(await timeout_after_the_gate(None), "0")
        self.assertEqual(await timeout_after_the_gate("7s"), "7s")

    async def test_the_arguments_are_checked(self):
        for bad in (True, "3000", 3000.0, None):
            with self.subTest(lock_timeout_ms=repr(bad)):
                with self.assertRaises(TypeError):
                    ProjectStateGate(lock_timeout_ms=bad)
        for bad in (0, -1, 60_001):
            with self.subTest(lock_timeout_ms=bad):
                with self.assertRaises(ValueError):
                    ProjectStateGate(lock_timeout_ms=bad)
        async with self.database.session() as session, session.begin():
            for bad in (None, "not-a-uuid", 5, self.project_id.hex):
                with self.subTest(project_id=repr(bad)):
                    with self.assertRaises(InvalidProjectInputError):
                        await self.gate.require_active(session, bad)


class GateIsMandatoryTest(unittest.TestCase):
    """No service or queue can be built without a gate (Decision 0020, A).

    Omitting the gate used to skip the check without a word; now it is an error at
    construction. No database is needed: the constructors refuse before any use.
    """

    DATABASE = object()

    def test_a_service_needs_a_gate_argument(self):
        with self.assertRaises(TypeError):
            TaskService(self.DATABASE)
        with self.assertRaises(TypeError):
            TaskService(self.DATABASE, listeners=[])

    def test_a_queue_needs_a_gate_argument(self):
        with self.assertRaises(TypeError):
            TaskQueue(self.DATABASE)
        with self.assertRaises(TypeError):
            TaskQueue(self.DATABASE, lease_seconds=5, allow_explicit_now=True)

    def test_none_is_not_a_gate(self):
        with self.assertRaises(TypeError):
            TaskService(self.DATABASE, project_gate=None)
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            TaskQueue(self.DATABASE, project_gate=None)
        self.assertEqual(caught.exception.parameter, "project_gate")

    def test_something_that_is_not_a_gate_is_refused(self):
        for wrong in (object(), "gate", 5, False, type("Half", (), {})()):
            with self.subTest(project_gate=type(wrong).__name__):
                with self.assertRaises(TypeError):
                    TaskService(self.DATABASE, project_gate=wrong)
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    TaskQueue(self.DATABASE, project_gate=wrong)
                self.assertEqual(caught.exception.parameter, "project_gate")

    def test_a_gate_is_accepted_by_both(self):
        for gate in (ALWAYS_ACTIVE, ProjectStateGate()):
            with self.subTest(gate=type(gate).__name__):
                self.assertIsInstance(
                    TaskService(self.DATABASE, project_gate=gate), TaskService
                )
                self.assertEqual(
                    TaskQueue(self.DATABASE, project_gate=gate).lease_seconds, 60
                )

    def test_the_test_gate_is_not_part_of_the_production_code(self):
        # ``AlwaysActiveGate`` admits every project, Pending deletion ones included:
        # it lives in the tests' support module. Nothing under ``paw_backend`` may
        # define, import or mention it, so production composition code cannot reach it.
        mentions = {
            str(path.relative_to(BACKEND)): text
            for path in sorted((BACKEND / "paw_backend").rglob("*.py"))
            if (text := path.read_text()).count("AlwaysActive")
            or text.count("gate_support")
        }
        production_files = list((BACKEND / "paw_backend").rglob("*.py"))

        self.assertGreater(len(production_files), 100)  # the whole package is scanned
        self.assertEqual({name: "" for name in mentions}, {})
        # ... and the support module is where the tests say it is.
        self.assertTrue((BACKEND / "tests" / "gate_support.py").is_file())
        self.assertFalse([p for p in (BACKEND / "paw_backend").rglob("gate_support*")])


@requires_postgres
class GateRaceTest(GateTestCase):
    """A write and a lifecycle change that overlap are serialised by the row lock."""

    def deleting(self):
        service = self.new_service(lock_timeout_ms=30_000)
        return self.spawn(
            service.begin_deletion(self.manager, self.project_id, "Alpha")
        )

    async def test_a_create_that_holds_the_lock_makes_the_deletion_wait(self):
        gate = PausingGate(self.gate)
        tasks = TaskService(self.database, project_gate=gate)
        creating = self.spawn(self.create(tasks))
        async with asyncio.timeout(DEADLINE):
            await gate.locked.wait()

        deleting = self.deleting()
        await self.wait_until_blocked()
        self.assertEqual(self.status(), "active")
        self.assertFalse(deleting.done())

        gate.release.set()
        async with asyncio.timeout(DEADLINE):
            event = await creating
            await deleting

        # The task was admitted BEFORE the deletion, so the processor sees it.
        self.assertEqual(self.status(), "pending_deletion")
        self.assertEqual(self.task_state(event.task_id), "queued")
        result = await self.new_stopper().stop_project_tasks(self.project_id)
        self.assertEqual((result.stopped, result.done), ((event.task_id,), True))
        self.assertEqual(self.task_state(event.task_id), "cancelled")

    async def test_an_enqueue_that_holds_the_lock_makes_the_deletion_wait(self):
        task_id = await self.seed_task(TaskState.QUEUED, queue=False)
        gate = PausingGate(self.gate)
        enqueuing = self.spawn(
            TaskQueue(self.database, project_gate=gate).enqueue(task_id)
        )
        async with asyncio.timeout(DEADLINE):
            await gate.locked.wait()

        deleting = self.deleting()
        await self.wait_until_blocked()
        self.assertEqual(self.status(), "active")

        gate.release.set()
        async with asyncio.timeout(DEADLINE):
            await enqueuing
            await deleting

        self.assertEqual(self.entry_statuses(task_id), ["queued"])
        result = await self.new_stopper().stop_project_tasks(self.project_id)
        self.assertEqual((result.stopped, result.cancelled_entries), ((task_id,), 1))
        self.assertEqual(self.entry_statuses(task_id), ["cancelled"])
        self.assertTrue(result.done)

    async def test_a_deletion_that_commits_first_refuses_the_create(self):
        for status in ("pending_deletion", "archived"):
            with self.subTest(status=status):
                self.set_project(
                    self.project_id,
                    status="active",
                    deletion_started_at=None,
                    deletion_scheduled_at=None,
                )
                before = self.rows_of_tasks()
                holder = self.lock_project(self.project_id)
                creating = self.spawn(self.create())
                await self.wait_until_blocked()

                self.commit_lifecycle_change(status, holder)

                with self.assertRaises(ProjectNotActiveError):
                    async with asyncio.timeout(DEADLINE):
                        await creating
                self.assertEqual(self.rows_of_tasks(), before)

    async def test_a_deletion_that_commits_first_refuses_retry_and_restart(self):
        actor = Actor.user(self.team.manager)
        for command, state in (
            (TaskCommand.RETRY, TaskState.FAILED),
            (TaskCommand.RESTART, TaskState.CANCELLED),
        ):
            with self.subTest(command=command.value):
                self.set_project(
                    self.project_id,
                    status="active",
                    deletion_started_at=None,
                    deletion_scheduled_at=None,
                )
                task_id = await self.seed_task(state)
                before = (self.task_columns(task_id), self.commands(task_id))
                holder = self.lock_project(self.project_id)
                executing = self.spawn(
                    self.tasks.execute(task_id, command, actor=actor)
                )
                await self.wait_until_blocked()

                self.commit_lifecycle_change("pending_deletion", holder)

                with self.assertRaises(ProjectNotActiveError):
                    async with asyncio.timeout(DEADLINE):
                        await executing
                self.assertEqual(
                    (self.task_columns(task_id), self.commands(task_id)), before
                )

    async def test_a_deletion_that_commits_first_refuses_the_enqueue(self):
        task_id = await self.seed_task(TaskState.QUEUED, queue=False)
        holder = self.lock_project(self.project_id)
        enqueuing = self.spawn(self.queue.enqueue(task_id))
        await self.wait_until_blocked()

        self.commit_lifecycle_change("pending_deletion", holder)

        with self.assertRaises(ProjectNotActiveError):
            async with asyncio.timeout(DEADLINE):
                await enqueuing
        self.assertEqual(self.entry_statuses(task_id), [])

    async def test_a_restart_that_holds_the_lock_makes_the_deletion_wait(self):
        task_id = await self.seed_task(TaskState.CANCELLED)
        gate = PausingGate(self.gate)
        tasks = TaskService(self.database, project_gate=gate)
        restarting = self.spawn(
            tasks.execute(
                task_id, TaskCommand.RESTART, actor=Actor.user(self.team.manager)
            )
        )
        async with asyncio.timeout(DEADLINE):
            await gate.locked.wait()

        deleting = self.deleting()
        await self.wait_until_blocked()
        gate.release.set()
        async with asyncio.timeout(DEADLINE):
            await restarting
            await deleting

        # The restarted task is queued and active; the deletion stops it.
        self.assertEqual(self.task_state(task_id), "queued")
        result = await self.new_stopper().stop_project_tasks(self.project_id)
        self.assertEqual((result.stopped, result.done), ((task_id,), True))
        self.assertEqual(self.task_state(task_id), "cancelled")


if __name__ == "__main__":
    unittest.main()
