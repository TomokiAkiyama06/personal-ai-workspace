"""Composition helpers of the orchestrator (PAW-034): the Project state gate.

Issue #83 (Decision 0020) makes the Project state gate MANDATORY for the task
lane: ``TaskService(database, project_gate=...)`` and
``TaskQueue(database, project_gate=...)`` take it as a required keyword and refuse
``None``, so that no composition can skip the check by leaving it out. The
orchestrator builds those two services in exactly one production place (the stop
loop of ``project_sweep.build_project_stop_loop``, wired by the application's
lifespan), and it must never rely on the argument being optional.

Until #83 is merged the two constructors do not have the argument yet, so the code
that composes them goes through :func:`gate_arguments`, which passes
``project_gate=`` **when the constructor has it** and nothing otherwise. That is
the whole compatibility shim, in one place: once #83 has merged, ``gate_arguments``
always finds the parameter, a missing gate raises the constructors' own
``TypeError`` (loudly, at start-up), and the shim can be replaced by passing
``project_gate=`` directly.

The gate itself (``ProjectStateGate``, ``projects/task_gate.py`` of #83) is created
by :func:`production_project_gate`, the one place the application asks for it.
"""

import inspect
from typing import Any


def gate_arguments(constructor: Any, gate: object | None) -> dict[str, object]:
    """``{"project_gate": gate}`` when ``constructor`` takes a ``project_gate``.

    Otherwise ``{}`` (the constructor predates #83). The gate is passed as it is,
    ``None`` included: a constructor that requires one refuses it itself, so a
    forgotten gate fails at construction and is never read as "no gate needed".
    """
    if "project_gate" in inspect.signature(constructor).parameters:
        return {"project_gate": gate}
    return {}


def production_project_gate() -> object | None:
    """The Project state gate the application composes the task lane with.

    ``ProjectStateGate()`` when ``paw_backend.projects`` has it (Issue #83); ``None``
    before that, when the task lane has no gate to be given.
    """
    from paw_backend import projects

    factory = getattr(projects, "ProjectStateGate", None)
    return None if factory is None else factory()
