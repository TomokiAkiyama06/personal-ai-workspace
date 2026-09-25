"""Typed errors of the DAG orchestrator (PAW-034).

They extend ``paw_backend.tasks.errors.TaskError`` so that the API layer can
handle every task-related failure in one place. Messages are fixed strings: they
never contain a caller-supplied value (a title, a goal, a node key, a failure
text), a driver message or a connection string. ``code`` is the stable
machine-readable identifier.

Two things here are not errors of a caller:

* :class:`NodeStopped` is raised **inside a running node** (by the tool gateway or
  the budget handle the orchestrator gives it) to tell the agent runtime that it
  must stop now: the task ended, the worker lost its lease, the budget is used up.
  It is not a ``TaskError`` and a runtime must let it pass.
* :func:`error_class_of` names an exception for the failure records (the loop
  detector and the node attempts). It never reads a message and never trusts a
  class name that a foreign adapter chose.
"""

import asyncio
import json
from enum import StrEnum
from typing import ClassVar

from paw_backend.tasks.errors import TaskError


class OrchestratorError(TaskError):
    """Base class of every error raised by ``paw_backend.orchestrator``."""

    code: ClassVar[str] = "orchestrator_error"


class InvalidOrchestratorArgumentError(OrchestratorError):
    """A caller passed an argument of the wrong type or outside its bounds.

    ``parameter`` is the name of the offending parameter (a constant of this
    package, never user input). The value is never part of the message.
    """

    code = "invalid_orchestrator_argument"

    def __init__(self, parameter: str) -> None:
        self.parameter = parameter
        super().__init__(f"Invalid value for {parameter}")


class PlanReason(StrEnum):
    """Why a plan was refused. A closed set: the message never quotes the plan."""

    NOT_A_MAPPING = "not_a_mapping"
    UNKNOWN_FIELD = "unknown_field"
    MISSING_FIELD = "missing_field"
    BAD_TYPE = "bad_type"
    BAD_TEXT = "bad_text"
    BAD_KEY = "bad_key"
    DUPLICATE_KEY = "duplicate_key"
    UNKNOWN_ROLE = "unknown_role"
    EMPTY = "empty"
    TOO_MANY_NODES = "too_many_nodes"
    TOO_MANY_DEPENDENCIES = "too_many_dependencies"
    TOO_MANY_EDGES = "too_many_edges"
    UNKNOWN_DEPENDENCY = "unknown_dependency"
    SELF_DEPENDENCY = "self_dependency"
    DUPLICATE_DEPENDENCY = "duplicate_dependency"
    CYCLE = "cycle"
    TOO_DEEP = "too_deep"
    BAD_INPUT = "bad_input"
    TOO_LARGE = "too_large"
    UNKNOWN_CAPABILITY = "unknown_capability"
    CAPABILITY_ABOVE_ROLE = "capability_above_role"
    BAD_REPOSITORY = "bad_repository"
    NO_REQUIRED_NODE = "no_required_node"


class InvalidPlanError(OrchestratorError):
    """A plan (a planner's output) is not acceptable; nothing was stored."""

    code = "invalid_plan"

    def __init__(self, reason: PlanReason) -> None:
        self.reason = reason
        super().__init__(f"The plan is not acceptable ({reason.value})")


class ResultReason(StrEnum):
    NOT_A_RESULT = "not_a_result"
    UNKNOWN_FIELD = "unknown_field"
    MISSING_FIELD = "missing_field"
    BAD_TYPE = "bad_type"
    BAD_TEXT = "bad_text"
    BAD_NUMBER = "bad_number"
    TOO_MANY_ITEMS = "too_many_items"
    TOO_LARGE = "too_large"


class InvalidNodeResultError(OrchestratorError):
    """A node's result does not fit the result schema (or its size limit)."""

    code = "invalid_node_result"

    def __init__(self, reason: ResultReason) -> None:
        self.reason = reason
        super().__init__(f"The node result is not acceptable ({reason.value})")


class ScopeEscalationError(OrchestratorError):
    """A node asked for a scope wider than its parent task's (or for something the
    task's scope does not contain, such as a repository outside the working set)."""

    code = "scope_escalation"

    def __init__(self) -> None:
        super().__init__("A node scope cannot exceed the scope of its task")


class DagNotFoundError(OrchestratorError):
    code = "dag_not_found"

    def __init__(self) -> None:
        super().__init__("DAG not found")


class DagAlreadyExistsError(OrchestratorError):
    """The task attempt already has a DAG: a plan is accepted once per attempt."""

    code = "dag_already_exists"

    def __init__(self) -> None:
        super().__init__("This task attempt already has a DAG")


class StaleDagEpochError(OrchestratorError):
    """The writer is not the DAG's current owner: another worker took it over.

    Nothing was written. The worker that gets this must stop working on the DAG.
    """

    code = "stale_dag_epoch"

    def __init__(self) -> None:
        super().__init__("The DAG belongs to a newer worker")


class StaleNodeAttemptError(OrchestratorError):
    """The node is no longer running the attempt the caller reports on."""

    code = "stale_node_attempt"

    def __init__(self) -> None:
        super().__init__("The node has moved on to another attempt")


class NodeStateError(OrchestratorError):
    """The node is not in a state that allows the operation."""

    code = "node_state"

    def __init__(self) -> None:
        super().__init__("The node is not in a state that allows this")


class DagStateError(OrchestratorError):
    """The DAG is not in a state that allows the operation."""

    code = "dag_state"

    def __init__(self) -> None:
        super().__init__("The DAG is not in a state that allows this")


class StopReason(StrEnum):
    TASK_ENDED = "task_ended"  # completed, failed or cancelled under the node
    SUPERSEDED = "superseded"  # a Retry / Restart replaced this run
    LEASE_LOST = "lease_lost"  # the worker no longer holds the queue lease
    BUDGET_EXCEEDED = "budget_exceeded"  # the parent task's budget is used up
    SHUTDOWN = "shutdown"  # the orchestrator is stopping


class NodeStopped(Exception):
    """Raised inside a running node: stop now (see the module docstring).

    ``reason`` is a closed enum member. A runtime must not swallow it; the
    orchestrator has already decided what happens to the node.
    """

    def __init__(self, reason: StopReason) -> None:
        self.reason = reason
        super().__init__(f"The node must stop ({reason.value})")


# Exception classes whose names may appear in failure records. Anything else
# (an adapter's own class, a class with a hostile ``__name__``) is recorded under
# ``ADAPTER_ERROR``: the name of a foreign class is data an adapter chose.
ADAPTER_ERROR = "AdapterError"
_NAMED: dict[int, str] = {
    id(cls): cls.__name__
    for cls in (
        ArithmeticError,
        AssertionError,
        AttributeError,
        ConnectionError,
        EOFError,
        FileNotFoundError,
        ImportError,
        IndexError,
        KeyError,
        LookupError,
        MemoryError,
        NotImplementedError,
        OSError,
        OverflowError,
        PermissionError,
        RecursionError,
        RuntimeError,
        TimeoutError,
        TypeError,
        UnicodeError,
        ValueError,
        ZeroDivisionError,
        asyncio.TimeoutError,
        json.JSONDecodeError,
        InvalidNodeResultError,
        InvalidPlanError,
        ScopeEscalationError,
        NodeStopped,
        OrchestratorError,
        TaskError,
    )
}
# The names of the failures the orchestrator itself records for a node.
NODE_TIMEOUT = "NodeTimeout"
INVALID_OUTCOME = "InvalidNodeOutcome"
GRANT_ESCALATION = "GrantEscalation"


def error_class_of(error: BaseException) -> str:
    """A fixed name for ``error``; it never raises and never reads an attribute.

    The class is looked up by ``id(type(error))`` (as ``type()`` gives the class
    without asking the object), so a ``__class__``, ``__name__`` or ``__eq__`` set
    by an adapter is never consulted.
    """
    return _NAMED.get(id(type(error)), ADAPTER_ERROR)
