"""One projection run with a fake source and recorder (PAW-045).

Every run records its outcome (``memory.projection.completed`` / ``failed`` with a
closed code, never a path or a message); a failure never leaves a half-written or
emptied projection behind; a concurrent run does nothing; a cancellation is
recorded and the lock released. Temporary directories only.
"""

import asyncio
import os
import unittest

from paw_backend.db import Database
from paw_backend.memory.projection import (
    MARKER_NAME,
    MemoryProjectionRunner,
    ProjectionAction,
    ProjectionBusyError,
    ProjectionDatabaseError,
    ProjectionStep,
    open_target,
)

from .projection_support import T0, TemporaryRoot, memory, tree
from .support import make_settings


class FakeSource:
    def __init__(self, memories=(), error: BaseException | None = None) -> None:
        self.memories = list(memories)
        self.error = error
        self.calls = 0
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def current_versions(self):
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return list(self.memories)


class FakeRecorder:
    def __init__(self, error: BaseException | None = None) -> None:
        self.rows: list[tuple[str, str, object]] = []
        self.error = error

    async def __call__(self, action, reason, *, occurred_at):
        if self.error is not None:
            raise self.error
        self.rows.append((ProjectionAction(action).value, reason, occurred_at))


class RunnerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryRoot(self)
        self.database = Database(make_settings())
        self.addAsyncCleanup(self.database.dispose)
        self.recorder = FakeRecorder()

    def runner(self, source, *, root=None, recorder=None):
        return MemoryProjectionRunner(
            self.database,
            root or self.tmp.root,
            protected_homes=self.tmp.homes,
            clock=lambda: T0,
            source=source,
            recorder=recorder or self.recorder,
        )


class SuccessTest(RunnerTestCase):
    async def test_a_run_writes_the_projection_and_records_the_counts(self):
        token = "ghp_" + "a1B2" * 9
        values = [memory(content=token), memory(scope="shared")]
        result = await self.runner(FakeSource(values)).run()
        self.assertTrue(result.ok)
        self.assertEqual((result.memories, result.redactions), (2, 1))
        self.assertEqual(result.report.written, 4)
        self.assertEqual(
            self.recorder.rows,
            [
                (
                    "memory.projection.completed",
                    "memories=2 written=4 removed=0 redacted=1",
                    T0,
                )
            ],
        )
        files = tree(self.tmp.root)
        self.assertEqual(len(files), 5)  # + the marker
        self.assertFalse(any(token.encode() in data for data in files.values()))

    async def test_the_lock_is_released_after_the_run(self):
        await self.runner(FakeSource()).run()
        open_target(self.tmp.root, self.tmp.homes).close()


class FailureTest(RunnerTestCase):
    async def test_a_refused_directory_is_recorded_without_its_path(self):
        checkout = self.tmp.base / "checkout"
        (checkout / ".git").mkdir(parents=True)
        (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        source = FakeSource([memory()])
        result = await self.runner(source, root=checkout / "memory").run()
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_step, ProjectionStep.CHECK_TARGET)
        self.assertEqual(result.error, "inside_git_work_tree")
        self.assertEqual(source.calls, 0)  # nothing was read
        self.assertEqual(
            self.recorder.rows,
            [("memory.projection.failed", "check_target:inside_git_work_tree", T0)],
        )
        self.assertFalse((checkout / "memory").exists())

    async def test_a_home_directory_is_refused(self):
        result = await self.runner(FakeSource(), root=self.tmp.home / "m").run()
        self.assertEqual(result.error, "overlaps_home")
        self.assertEqual(os.listdir(self.tmp.home), [])

    async def test_a_failed_read_keeps_the_previous_projection(self):
        value = memory()
        await self.runner(FakeSource([value])).run()
        before = tree(self.tmp.root)
        failing = FakeSource(error=ProjectionDatabaseError("57P01"))
        result = await self.runner(failing).run()
        self.assertEqual(result.failed_step, ProjectionStep.READ_DATABASE)
        self.assertEqual(
            self.recorder.rows[-1][:2],
            ("memory.projection.failed", "read_database:ProjectionDatabaseError"),
        )
        # A failed read never empties the projection.
        self.assertEqual(tree(self.tmp.root), before)

    async def test_a_render_failure_is_recorded(self):
        broken = memory(owner_user_id=None)
        result = await self.runner(FakeSource([broken])).run()
        self.assertEqual(result.failed_step, ProjectionStep.RENDER)
        self.assertEqual(self.recorder.rows[-1][1], "render:ProjectionRenderError")
        self.assertEqual(set(tree(self.tmp.root)), {MARKER_NAME})

    async def test_a_write_failure_is_recorded(self):
        await self.runner(FakeSource()).run()
        (self.tmp.root / "users").symlink_to(self.tmp.base)
        result = await self.runner(FakeSource([memory()])).run()
        self.assertEqual(result.failed_step, ProjectionStep.WRITE_FILES)
        self.assertEqual(self.recorder.rows[-1][1], "write_files:unsafe_entry")

    async def test_an_unrecordable_outcome_is_not_ok(self):
        recorder = FakeRecorder(error=OSError("database down"))
        result = await self.runner(FakeSource([memory()]), recorder=recorder).run()
        self.assertIsNone(result.failed_step)
        self.assertFalse(result.audited)
        self.assertFalse(result.ok)

    async def test_a_second_run_at_the_same_time_does_nothing(self):
        held = open_target(self.tmp.root, self.tmp.homes)
        self.addCleanup(held.close)
        source = FakeSource([memory()])
        with self.assertRaises(ProjectionBusyError):
            await self.runner(source).run()
        self.assertEqual(source.calls, 0)
        self.assertEqual(self.recorder.rows, [])

    async def test_a_cancelled_run_is_recorded_and_releases_the_lock(self):
        source = FakeSource([memory()])
        source.gate = asyncio.Event()
        task = asyncio.ensure_future(self.runner(source).run())
        await source.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(
            self.recorder.rows,
            [("memory.projection.failed", "read_database:CancelledError", T0)],
        )
        open_target(self.tmp.root, self.tmp.homes).close()

    async def test_the_reason_fits_the_audit_column(self):
        many = [memory() for _ in range(3)]
        await self.runner(FakeSource(many)).run()
        self.assertLessEqual(len(self.recorder.rows[-1][1]), 64)


if __name__ == "__main__":
    unittest.main()
