"""The periodic driver of ``stop_project_tasks`` (PAW-034, Decision 0008 section 8).

The loop is tested with a fake stopper and a manual clock (its schedule, its
bounds, its failure isolation, its shutdown), then against a real PostgreSQL with
the real stopper: a project that is Pending deletion has its tasks stopped by a
cycle even though no request is open, and a project that is not Pending deletion is
never touched.
"""

import asyncio
import unittest
import uuid

from paw_backend.db import Database
from paw_backend.orchestrator import project_sweep
from paw_backend.orchestrator.errors import InvalidOrchestratorArgumentError
from paw_backend.orchestrator.project_sweep import (
    PendingDeletionLister,
    ProjectTaskStopLoop,
    build_project_stop_loop,
)
from paw_backend.projects.task_stop import TaskStopResult
from paw_backend.tasks import TaskCommand, TaskState
from paw_backend.tasks.queueing import TaskQueue

from .orchestrator_support import ManualClock, PostgresOrchestratorTestCase
from .support import make_settings
from .task_support import requires_postgres


def ids(count: int) -> list[uuid.UUID]:
    return [uuid.UUID(int=1000 + i) for i in range(count)]


class FakeStopper:
    """Answers ``done`` after ``rounds[project]`` calls (default 1); a project in
    ``failing`` raises."""

    def __init__(self, open_requests=(), rounds=None, failing=()):
        self.open_requests = list(open_requests)
        self.rounds = rounds or {}
        self.failing = set(failing)
        self.calls: list[uuid.UUID] = []
        self.limits: list[int] = []

    async def pending_project_ids(self, limit: int = 100):
        self.limits.append(limit)
        return tuple(self.open_requests[:limit])

    async def stop_project_tasks(self, project_id):
        self.calls.append(project_id)
        if project_id in self.failing:
            raise RuntimeError("secret detail must not be logged")
        needed = self.rounds.get(project_id, 1)
        made = self.calls.count(project_id)
        return TaskStopResult(project_id, (uuid.UUID(int=made),), 1, made >= needed)


class FakeLister(PendingDeletionLister):
    def __init__(self, pending=()):  # no database: never used
        self.pending = sorted(pending)
        self.asked: list[tuple] = []

    async def list_after(self, after, limit):
        self.asked.append((after, limit))
        rest = [i for i in self.pending if after is None or i > after]
        return tuple(rest[:limit])


def loop_of(stopper, lister=None, clock=None, **options) -> ProjectTaskStopLoop:
    return ProjectTaskStopLoop(
        stopper, lister or FakeLister(), clock=clock or ManualClock(), **options
    )


class CycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_open_requests_come_first_then_the_pending_deletion_projects(self):
        a, b, c, d = ids(4)
        stopper = FakeStopper(open_requests=[c, a])
        lister = FakeLister(pending=[a, b, d])

        report = await loop_of(stopper, lister).run_cycle()

        # No project twice: a is listed as an open request and as Pending deletion.
        self.assertEqual(stopper.calls, [c, a, b, d])
        self.assertEqual(
            (report.projects, report.stopped_tasks, report.cancelled_entries), (4, 4, 4)
        )
        self.assertEqual((report.unfinished, report.failed), (0, 0))

    async def test_a_cycle_looks_at_a_bounded_number_of_projects(self):
        many = ids(12)
        stopper = FakeStopper(open_requests=many[:3])
        lister = FakeLister(pending=many[3:])

        report = await loop_of(stopper, lister, projects_per_cycle=5).run_cycle()

        self.assertEqual(stopper.limits, [5])
        self.assertEqual(lister.asked, [(None, 2)])  # room for two more
        self.assertEqual(report.projects, 5)
        self.assertEqual(stopper.calls, many[:5])

    async def test_the_cursor_moves_on_so_that_no_project_starves(self):
        many = ids(7)
        stopper = FakeStopper()
        lister = FakeLister(pending=many)
        loop = loop_of(stopper, lister, projects_per_cycle=3)

        for _ in range(4):
            await loop.run_cycle()

        # 3 + 3 + 1 (the end of the list: the cursor wraps) + 3 again.
        self.assertEqual(stopper.calls[:3], many[:3])
        self.assertEqual(stopper.calls[3:6], many[3:6])
        self.assertEqual(stopper.calls[6:7], many[6:7])
        self.assertEqual(stopper.calls[7:10], many[:3])
        self.assertEqual(
            lister.asked, [(None, 3), (many[2], 3), (many[5], 3), (None, 3)]
        )

    async def test_a_project_gets_a_bounded_number_of_calls_per_cycle(self):
        (slow,) = ids(1)
        stopper = FakeStopper(open_requests=[slow], rounds={slow: 10})
        loop = loop_of(stopper, rounds_per_project=3)

        report = await loop.run_cycle()

        self.assertEqual(stopper.calls, [slow] * 3)
        self.assertEqual((report.unfinished, report.stopped_tasks), (1, 3))
        # The next cycle continues it and finishes it.
        stopper.rounds[slow] = 4
        report = await loop.run_cycle()
        self.assertEqual(report.unfinished, 0)

    async def test_a_project_that_raises_does_not_stop_the_others(self):
        good_a, bad, good_b = ids(3)
        stopper = FakeStopper(open_requests=[good_a, bad, good_b], failing=[bad])

        with self.assertLogs("paw_backend.orchestrator.project_sweep", "ERROR") as logs:
            report = await loop_of(stopper).run_cycle()

        self.assertEqual(stopper.calls, [good_a, bad, good_b])
        self.assertEqual((report.projects, report.failed, report.unfinished), (3, 1, 0))
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("secret detail", text)  # the type only, never the message

    async def test_a_stop_request_ends_the_cycle_after_the_project_in_hand(self):
        a, b, c = ids(3)
        stopper = FakeStopper(open_requests=[a, b, c])
        loop = loop_of(stopper)
        original = stopper.stop_project_tasks

        async def stop_after_first(project_id):
            result = await original(project_id)
            loop.stop()
            return result

        stopper.stop_project_tasks = stop_after_first

        report = await loop.run_cycle()

        self.assertEqual(stopper.calls, [a])
        self.assertEqual(report.stopped_tasks, 1)


class ScheduleTest(unittest.IsolatedAsyncioTestCase):
    async def start(self, loop):
        task = asyncio.create_task(loop.run())
        self.addAsyncCleanup(self.finish, loop, task)
        await ManualClock.settle()  # the loop is waiting for its first cycle
        return task

    async def finish(self, loop, task):
        loop.stop()
        await asyncio.wait_for(task, 5)

    async def test_the_first_cycle_waits_then_cycles_repeat_at_the_interval(self):
        clock = ManualClock()
        stopper = FakeStopper(open_requests=ids(1))
        loop = loop_of(stopper, clock=clock, interval_seconds=60)
        await self.start(loop)

        self.assertEqual(stopper.calls, [])
        await clock.advance(4.9)
        self.assertEqual(stopper.calls, [])
        await clock.advance(0.2)  # 5 s after the start: the first cycle
        self.assertEqual(len(stopper.calls), 1)
        await clock.advance(59.0)
        self.assertEqual(len(stopper.calls), 1)
        await clock.advance(1.5)  # a whole interval after the first cycle
        self.assertEqual(len(stopper.calls), 2)
        await clock.advance(60.0)
        self.assertEqual(len(stopper.calls), 3)

    async def test_the_first_wait_is_never_longer_than_the_interval(self):
        clock = ManualClock()
        stopper = FakeStopper(open_requests=ids(1))
        await self.start(loop_of(stopper, clock=clock, interval_seconds=10))
        await clock.advance(5.1)
        self.assertEqual(len(stopper.calls), 1)

    async def test_an_unfinished_project_brings_the_next_cycle_forward(self):
        clock = ManualClock()
        (slow,) = ids(1)
        stopper = FakeStopper(open_requests=[slow], rounds={slow: 100})
        await self.start(
            loop_of(stopper, clock=clock, interval_seconds=600, rounds_per_project=1)
        )
        await clock.advance(5.1)
        self.assertEqual(len(stopper.calls), 1)
        await clock.advance(5.0)  # the catch-up delay, not the 600 s interval
        self.assertEqual(len(stopper.calls), 2)

    async def test_a_failing_cycle_backs_off_and_recovers(self):
        clock = ManualClock()
        stopper = FakeStopper()
        fails = {"on": True}
        original = stopper.pending_project_ids

        async def broken(limit=100):
            if fails["on"]:
                raise ConnectionError("the database is gone")
            return await original(limit)

        stopper.pending_project_ids = broken
        loop = loop_of(stopper, clock=clock, interval_seconds=100)
        seen = []
        real = loop.run_cycle

        async def counting():
            seen.append(clock.monotonic())
            return await real()

        loop.run_cycle = counting
        await self.start(loop)

        with self.assertLogs("paw_backend.orchestrator.project_sweep", "WARNING"):
            await clock.advance(5.1)  # cycle 1 fails; retry in 10 s
            await clock.advance(10.1)  # cycle 2 fails; retry in 20 s
            await clock.advance(20.1)  # cycle 3 fails; retry in 40 s
            await clock.advance(40.1)  # cycle 4 fails; retry in 80 s
            await clock.advance(80.1)  # cycle 5 fails; capped at the interval
        self.assertEqual(len(seen), 5)
        fails["on"] = False
        await clock.advance(100.1)  # cycle 6 succeeds
        self.assertEqual(len(seen), 6)
        await clock.advance(100.1)  # and the normal interval is back
        self.assertEqual(len(seen), 7)

    async def test_stop_ends_the_loop_at_once_even_while_it_waits(self):
        clock = ManualClock()
        loop = loop_of(FakeStopper(), clock=clock, interval_seconds=3600)
        task = asyncio.create_task(loop.run())
        await clock.settle()

        loop.stop()
        await asyncio.wait_for(task, 5)

        self.assertTrue(task.done())
        self.assertIsNone(task.exception())
        self.assertEqual(clock.sleeping, 0)  # no timer is left behind

    async def test_cancelling_the_loop_leaves_nothing_running(self):
        clock = ManualClock()
        stopper = FakeStopper(open_requests=ids(1))
        entered = asyncio.Event()

        async def slow(project_id):
            entered.set()
            await asyncio.Event().wait()

        stopper.stop_project_tasks = slow
        loop = loop_of(stopper, clock=clock)
        task = asyncio.create_task(loop.run())
        await clock.settle()
        await clock.advance(5.1)
        await asyncio.wait_for(entered.wait(), 5)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(clock.sleeping, 0)


class ArgumentTest(unittest.TestCase):
    def test_every_bad_argument_is_refused_when_the_loop_is_built(self):
        cases = [
            ("interval below the minimum", {"interval_seconds": 9.9}),
            ("interval above the maximum", {"interval_seconds": 3601}),
            ("interval zero", {"interval_seconds": 0}),
            ("interval a bool", {"interval_seconds": True}),
            ("interval text", {"interval_seconds": "60"}),
            ("interval NaN", {"interval_seconds": float("nan")}),
            ("no project per cycle", {"projects_per_cycle": 0}),
            ("too many projects per cycle", {"projects_per_cycle": 501}),
            ("projects per cycle a bool", {"projects_per_cycle": True}),
            ("no round", {"rounds_per_project": 0}),
            ("too many rounds", {"rounds_per_project": 51}),
            ("rounds a float", {"rounds_per_project": 2.0}),
        ]
        for label, options in cases:
            with (
                self.subTest(label),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                loop_of(FakeStopper(), **options)
        for label, stopper in (
            ("a stopper without the methods", object()),
            ("None", None),
        ):
            with self.subTest(label), self.assertRaises(TypeError):
                loop_of(stopper)
        with self.assertRaises(TypeError):
            ProjectTaskStopLoop(FakeStopper(), object())
        with self.assertRaises(TypeError):
            loop_of(FakeStopper(), clock=object())

    def test_the_documented_bounds_are_accepted(self):
        loop_of(
            FakeStopper(),
            interval_seconds=10,
            projects_per_cycle=1,
            rounds_per_project=1,
        )
        loop_of(
            FakeStopper(),
            interval_seconds=3600,
            projects_per_cycle=500,
            rounds_per_project=50,
        )

    def test_the_lister_checks_its_arguments_before_the_database(self):
        lister = PendingDeletionLister(Database(make_settings()))  # not configured
        for after, limit in [
            ("x", 1),
            (uuid.uuid4().hex, 1),
            (None, 0),
            (None, 501),
            (None, True),
            (None, "5"),
        ]:
            with self.subTest(after=after, limit=limit):
                with self.assertRaises(InvalidOrchestratorArgumentError):
                    asyncio.run(lister.list_after(after, limit))
        with self.assertRaises(TypeError):
            PendingDeletionLister(object())


@requires_postgres
class RealProjectSweepTest(PostgresOrchestratorTestCase):
    async def seed_project(self, status: str) -> uuid.UUID:
        project_id = uuid.uuid4()
        deletion = (
            ", now(), now() + interval '720 hours'"
            if status == "pending_deletion"
            else ", NULL, NULL"
        )
        await self.owner_sql(
            "INSERT INTO projects (id, name, status, created_at, updated_at,"
            " deletion_started_at, deletion_scheduled_at)"
            " VALUES (:id, 'p', :status, now(), now()" + deletion + ")",
            id=project_id,
            status=status,
        )
        return project_id

    async def set_project_state(self, project_id, status: str) -> None:
        """Move a project between the states with SQL (the deletion times the
        database requires go with them). Tasks are created while a project is
        Active: a project that is not is (or will be, issue #83) refused new
        tasks by the task lane itself."""
        times = (
            "now(), now() + interval '720 hours'"
            if status == "pending_deletion"
            else "NULL, NULL"
        )
        await self.owner_sql(
            "UPDATE projects SET status = :status,"
            f" (deletion_started_at, deletion_scheduled_at) = ({times})"
            " WHERE id = :id",
            id=project_id,
            status=status,
        )

    async def task_in(self, project_id, *, enqueue=True):
        task_id = await self.create_task(project_id=project_id)
        if enqueue:
            await TaskQueue(self.database).enqueue(task_id)
        return task_id

    async def state_of(self, task_id) -> str:
        return await self.scalar("SELECT state FROM tasks WHERE id = :t", t=task_id)

    async def test_a_cycle_stops_the_tasks_of_a_pending_deletion_project_only(self):
        await self.owner_sql("TRUNCATE projects CASCADE")
        pending = await self.seed_project("active")
        live = await self.seed_project("active")
        archived = await self.seed_project("active")
        doomed = [await self.task_in(pending) for _ in range(3)]
        safe = [await self.task_in(live), await self.task_in(archived)]
        await self.set_project_state(pending, "pending_deletion")
        await self.set_project_state(archived, "archived")
        loop = build_project_stop_loop(self.new_database(), interval_seconds=60)

        report = await loop.run_cycle()

        self.assertEqual(report.projects, 1)
        self.assertEqual((report.stopped_tasks, report.cancelled_entries), (3, 3))
        for task_id in doomed:
            self.assertEqual(await self.state_of(task_id), "cancelled")
        for task_id in safe:
            self.assertEqual(await self.state_of(task_id), "queued")
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM queue_entries WHERE status = 'cancelled'"
            ),
            3,
        )

    async def test_a_late_task_is_stopped_by_a_later_cycle(self):
        await self.owner_sql("TRUNCATE projects CASCADE")
        pending = await self.seed_project("pending_deletion")
        loop = build_project_stop_loop(self.new_database())
        first = await loop.run_cycle()  # nothing to stop yet
        self.assertEqual((first.projects, first.stopped_tasks), (1, 0))

        # A task that raced the deletion request: it was created while the project
        # was still Active (the project is Active again for a moment, then its
        # deletion begins again), after the first cycle had found nothing.
        await self.set_project_state(pending, "active")
        late = await self.task_in(pending)
        await self.set_project_state(pending, "pending_deletion")
        second = await loop.run_cycle()

        self.assertEqual(second.stopped_tasks, 1)
        self.assertEqual(await self.state_of(late), "cancelled")

    async def test_a_restored_project_is_left_alone(self):
        await self.owner_sql("TRUNCATE projects CASCADE")
        project = await self.seed_project("active")
        task_id = await self.task_in(project)
        await self.set_project_state(project, "pending_deletion")
        await self.set_project_state(project, "archived")  # restored
        loop = build_project_stop_loop(self.new_database())

        report = await loop.run_cycle()

        self.assertEqual(report.projects, 0)
        self.assertEqual(await self.state_of(task_id), "queued")

    async def test_the_loop_runs_and_shuts_down_cleanly(self):
        await self.owner_sql("TRUNCATE projects CASCADE")
        project = await self.seed_project("active")
        task_id = await self.task_in(project)
        await self.set_project_state(project, "pending_deletion")
        clock = ManualClock()
        loop = build_project_stop_loop(self.new_database(), clock=clock)
        run = asyncio.create_task(loop.run())
        await clock.settle()

        await clock.advance(5.1)
        for _ in range(500):
            if await self.state_of(task_id) == "cancelled":
                break
            await asyncio.sleep(0.02)
        loop.stop()
        await asyncio.wait_for(run, 10)

        self.assertEqual(await self.state_of(task_id), "cancelled")
        self.assertEqual(clock.sleeping, 0)

    async def test_the_lister_pages_through_the_pending_deletion_projects(self):
        await self.owner_sql("TRUNCATE projects CASCADE")
        wanted = sorted([await self.seed_project("pending_deletion") for _ in range(5)])
        for _ in range(3):
            await self.seed_project("active")
        lister = PendingDeletionLister(self.new_database())

        page1 = await lister.list_after(None, 2)
        page2 = await lister.list_after(page1[-1], 2)
        page3 = await lister.list_after(page2[-1], 2)
        page4 = await lister.list_after(page3[-1], 2)

        self.assertEqual([*page1, *page2, *page3], wanted)
        self.assertEqual(page4, ())

    async def test_the_lister_reads_through_the_partial_index(self):
        await self.owner_sql("TRUNCATE projects CASCADE")
        await self.owner_sql(
            "INSERT INTO projects (id, name, status, created_at, updated_at)"
            " SELECT gen_random_uuid(), 'p', 'active', now(), now()"
            " FROM generate_series(1, 20000)"
        )
        for _ in range(4):
            await self.seed_project("pending_deletion")
        await self.owner_sql("ANALYZE projects")
        sql = str(
            project_sweep.pending_deletion_statement(None, 10).compile(
                dialect=__import__(
                    "sqlalchemy.dialects.postgresql", fromlist=["x"]
                ).dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.assertIn("'pending_deletion'", sql)  # written into the statement

        plan = await self.plan(sql.replace("%", "%%"), {}, "force_generic_plan")

        def nodes(node):
            yield node
            for child in node.get("Plans", []):
                yield from nodes(child)

        found = list(nodes(plan[0]["Plan"]))
        self.assertNotIn("Seq Scan", [n["Node Type"] for n in found])
        self.assertIn(
            "ix_projects_pending_deletion", [n.get("Index Name") for n in found]
        )

    async def test_the_loops_task_service_revokes_approvals_when_a_task_ends(self):
        # Wiring check: the task service of the loop carries the revocation listener.
        loop = build_project_stop_loop(self.new_database())
        tasks = loop._stopper._tasks
        listeners = [getattr(listener, "__name__", "") for listener in tasks._listeners]
        self.assertEqual(listeners, ["revoke_on_task_end"])
        task_id = await self.create_task()
        await tasks.execute(task_id, TaskCommand.CANCEL, actor=self.system)
        self.assertEqual(await self.state_of(task_id), TaskState.CANCELLED.value)


if __name__ == "__main__":
    unittest.main()
