"""The Project state gate is composed explicitly, never by omission (Issue #83).

Decision 0020 makes ``project_gate`` a required keyword of ``TaskService`` and
``TaskQueue`` (they refuse ``None``). The one production place where the
orchestrator builds those services, the stop loop of the application's lifespan,
passes the gate it is given; ``build_project_stop_loop`` has no default for it, and
the application gives it ``ProjectStateGate()``
(``test_orchestrator_project_sweep_app``).
"""

import unittest

from paw_backend.db import Database
from paw_backend.orchestrator import project_sweep
from paw_backend.tasks import TaskService
from paw_backend.tasks.queueing import InvalidQueueingArgumentError, TaskQueue

from .gate_support import ALWAYS_ACTIVE
from .support import make_settings


class StopLoopIsComposedWithTheGateTest(unittest.TestCase):
    def build(self, **options):
        built: dict[str, object] = {}

        class Service(TaskService):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                built["service"] = self

        class Queue(TaskQueue):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                built["queue"] = self

        # Only the two services are recorded; the rest is the real composition
        # (nothing connects to a database while the loop is only built).
        original = project_sweep.TaskService, project_sweep.TaskQueue
        project_sweep.TaskService, project_sweep.TaskQueue = Service, Queue
        try:
            loop = project_sweep.build_project_stop_loop(
                Database(make_settings()), **options
            )
        finally:
            project_sweep.TaskService, project_sweep.TaskQueue = original
        return loop, built

    def test_the_gate_reaches_the_task_service_and_the_queue(self):
        _, built = self.build(project_gate=ALWAYS_ACTIVE)

        self.assertIs(built["service"]._project_gate, ALWAYS_ACTIVE)
        self.assertIs(built["queue"]._project_gate, ALWAYS_ACTIVE)

    def test_the_service_keeps_the_approval_revocation_listener(self):
        _, built = self.build(project_gate=ALWAYS_ACTIVE)

        self.assertEqual(len(built["service"]._listeners), 1)

    def test_a_missing_gate_is_refused_where_the_task_lane_requires_one(self):
        with self.assertRaises(TypeError):
            self.build(project_gate=None)

    def test_the_queue_refuses_a_gate_that_is_none_too(self):
        with self.assertRaises(InvalidQueueingArgumentError):
            TaskQueue(Database(make_settings()), project_gate=None)

    def test_the_gate_argument_cannot_be_left_out(self):
        with self.assertRaises(TypeError):
            project_sweep.build_project_stop_loop(Database(make_settings()))


if __name__ == "__main__":
    unittest.main()
