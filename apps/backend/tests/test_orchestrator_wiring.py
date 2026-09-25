"""The Project state gate is composed explicitly, never by omission (Issue #83).

Decision 0020 makes ``project_gate`` a required keyword of ``TaskService`` and
``TaskQueue``. The orchestrator's composition passes it explicitly and works both
before that change is merged (the constructors take none) and after it (they
require one and refuse ``None``).
"""

import inspect
import unittest
from unittest.mock import patch

from paw_backend import projects
from paw_backend.db import Database
from paw_backend.orchestrator import project_sweep, wiring
from paw_backend.orchestrator.wiring import gate_arguments, production_project_gate
from paw_backend.tasks import TaskService
from paw_backend.tasks.queueing import TaskQueue

from .support import make_settings


class WithGate:
    """A constructor of the world after #83: the gate is a required keyword."""

    def __init__(self, database, *, project_gate, listeners=()):
        if project_gate is None:
            raise TypeError("project_gate is required")
        self.database = database
        self.project_gate = project_gate
        self.listeners = listeners


class WithoutGate:
    """A constructor of the world before #83."""

    def __init__(self, database, *, listeners=()):
        self.database = database
        self.listeners = listeners


class GateArgumentsTest(unittest.TestCase):
    def test_a_constructor_that_takes_a_gate_is_given_it(self):
        gate = object()
        self.assertEqual(gate_arguments(WithGate, gate), {"project_gate": gate})

    def test_a_constructor_that_takes_none_is_given_nothing(self):
        self.assertEqual(gate_arguments(WithoutGate, object()), {})
        self.assertEqual(gate_arguments(WithoutGate, None), {})

    def test_a_missing_gate_is_passed_on_as_none_so_that_the_constructor_refuses_it(
        self,
    ):
        arguments = gate_arguments(WithGate, None)
        self.assertEqual(arguments, {"project_gate": None})
        with self.assertRaises(TypeError):
            WithGate(object(), **arguments)

    def test_the_real_task_lane_constructors_are_handled_in_either_world(self):
        for constructor in (TaskService, TaskQueue):
            takes_gate = "project_gate" in inspect.signature(constructor).parameters
            self.assertEqual(
                gate_arguments(constructor, "G"),
                {"project_gate": "G"} if takes_gate else {},
            )


class ProductionGateTest(unittest.TestCase):
    def test_the_gate_is_the_project_state_gate_when_there_is_one(self):
        class Gate:
            pass

        with patch.object(projects, "ProjectStateGate", Gate, create=True):
            self.assertIsInstance(production_project_gate(), Gate)

    def test_there_is_none_before_the_gate_exists(self):
        with patch.object(projects, "ProjectStateGate", None, create=True):
            self.assertIsNone(production_project_gate())
        self.assertTrue(callable(wiring.production_project_gate))


class StopLoopIsComposedWithTheGateTest(unittest.TestCase):
    def build(self, service, queue, **options):
        with (
            patch.object(project_sweep, "TaskService", service),
            patch.object(project_sweep, "TaskQueue", queue),
            patch.object(project_sweep, "ProjectTaskStopper", lambda *a, **k: object()),
            patch.object(project_sweep, "ProjectTaskStopLoop", lambda *a, **k: (a, k)),
            patch.object(project_sweep, "PendingDeletionLister", lambda db: db),
            patch.object(
                project_sweep, "ApprovalService", lambda *a, **k: _Approvals()
            ),
            patch.object(project_sweep, "PostgresApprovalStore", lambda db: db),
            patch.object(project_sweep, "PostgresAuditSink", lambda db: db),
        ):
            return project_sweep.build_project_stop_loop(
                Database(make_settings()), **options
            )

    def test_the_gate_reaches_the_task_service_and_the_queue(self):
        class Service(WithGate):
            built: list = []

            def __init__(self, database, *, project_gate, listeners=()):
                super().__init__(
                    database, project_gate=project_gate, listeners=listeners
                )
                self.built.append(self)

        class Queue(Service):
            built: list = []

        gate = object()
        self.build(Service, Queue, project_gate=gate)

        (service,) = Service.built
        (queue,) = Queue.built
        self.assertIs(service.project_gate, gate)
        self.assertIs(queue.project_gate, gate)
        # The service keeps the approval revocation listener.
        self.assertEqual(len(service.listeners), 1)

    def test_a_missing_gate_fails_where_the_task_lane_requires_one(self):
        with self.assertRaises(TypeError):
            self.build(WithGate, WithGate, project_gate=None)

    def test_the_gate_argument_cannot_be_left_out(self):
        with self.assertRaises(TypeError):
            project_sweep.build_project_stop_loop(Database(make_settings()))

    def test_before_the_gate_exists_the_constructors_are_built_without_one(self):
        self.build(WithoutGate, WithoutGate, project_gate=None)


class _Approvals:
    async def revoke_on_task_end(self, event):
        return None


if __name__ == "__main__":
    unittest.main()
