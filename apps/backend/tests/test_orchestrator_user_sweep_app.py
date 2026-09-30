"""The application starts the user task-stop loop and stops it (Issue #127).

``PAW_USER_TASK_STOP_INTERVAL_SECONDS`` (0 off, otherwise 10 to 3600, default 60)
and the lifespan: the loop starts with a configured database, is asked to stop and
cancelled before the database is disposed, and does not start without a database
or when it is switched off.
"""

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from paw_backend.app import create_app
from paw_backend.config import Settings
from paw_backend.projects import ProjectStateGate

from .support import FakeDatabase, make_settings, paw_environment, wait_until
from .test_orchestrator_project_sweep_app import LifespanTestCase, RecordingLoop
from .test_scratch_janitor_lifespan import configured


class UserRecordingLoop(RecordingLoop):
    instances: list["UserRecordingLoop"] = []


class IntervalSettingTest(unittest.TestCase):
    def test_the_default_is_a_minute(self):
        self.assertEqual(make_settings().user_task_stop_interval_seconds, 60)

    def test_it_is_read_from_the_environment(self):
        with paw_environment(PAW_USER_TASK_STOP_INTERVAL_SECONDS="120"):
            self.assertEqual(Settings().user_task_stop_interval_seconds, 120)

    def test_zero_turns_it_off_and_a_few_seconds_are_refused(self):
        self.assertEqual(
            make_settings(
                user_task_stop_interval_seconds=0
            ).user_task_stop_interval_seconds,
            0,
        )
        for value in (1, 9, 3601, -1):
            with self.subTest(value), self.assertRaises(ValidationError):
                make_settings(user_task_stop_interval_seconds=value)


class UserLoopLifespanTest(LifespanTestCase):
    def setUp(self) -> None:
        super().setUp()
        UserRecordingLoop.instances = []
        patcher = patch("paw_backend.app.build_user_stop_loop", UserRecordingLoop)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_it_starts_and_is_stopped_before_the_database_is_disposed(self):
        settings, database = configured(user_task_stop_interval_seconds=90)
        seen = []
        database.on_dispose = lambda: seen.append(
            [(loop.stopped, loop.cancelled) for loop in UserRecordingLoop.instances]
        )
        app = create_app(settings, database=database)

        async with app.router.lifespan_context(app):
            (loop,) = UserRecordingLoop.instances
            self.assertTrue(await wait_until(loop.started.is_set))
            self.assertIs(loop.database, database)
            # The application's own task service (issue #125): a task the loop
            # cancels ends through the listener that undoes what it held.
            self.assertEqual(
                set(loop.options), {"project_gate", "interval_seconds", "tasks"}
            )
            self.assertIsInstance(loop.options["project_gate"], ProjectStateGate)
            self.assertEqual(loop.options["interval_seconds"], 90)
            self.assertIs(loop.options["tasks"], app.state.task_execution.tasks)

        self.assertEqual(seen, [[(True, True)]])

    async def test_not_without_a_database_or_when_switched_off(self):
        app = create_app(make_settings(), database=FakeDatabase())
        await self.run_lifespan(app)
        settings, database = configured(user_task_stop_interval_seconds=0)
        await self.run_lifespan(create_app(settings, database=database))

        self.assertEqual(UserRecordingLoop.instances, [])


if __name__ == "__main__":
    unittest.main()
