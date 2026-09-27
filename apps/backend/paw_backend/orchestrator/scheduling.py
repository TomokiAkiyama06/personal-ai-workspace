"""Deterministic scheduling rules of the DAG (PAW-034): pure functions.

No database, no clock, no randomness, no I/O. Everything the orchestrator decides
about *which node runs next* and *what a failure does to the other nodes* is
here, so that a random-DAG property test can check it without a database.

``nodes`` are always given in ordinal order (a dependency has a smaller ordinal
than its dependent, see ``plan.py``).

Failure isolation (``REQUIREMENTS.md``, "Dependency / failure isolation"):

* a node that failed does not stop the nodes that do not depend on it;
* only the nodes that depend on it, directly or through other nodes, wait: they
  become ``blocked`` and never start;
* the task does not fail at the first failed node: when nothing can run any more
  the DAG is ``failed`` if a **required** node did not succeed, and ``succeeded``
  if every required node did (an optional node may have failed).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from paw_backend.orchestrator.domain import UNSATISFIABLE_NODE_STATES, NodeState


@dataclass(frozen=True, slots=True)
class NodeView:
    """What scheduling needs to know of a node."""

    key: str
    ordinal: int
    state: NodeState
    depends_on: tuple[str, ...]
    required: bool


class DagVerdict(StrEnum):
    ACTIVE = "active"  # a node is ready or running
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# States that dependency effects may change; every other state is fixed.
_OPEN = frozenset({NodeState.PENDING, NodeState.READY, NodeState.BLOCKED})


def settle_states(nodes: Sequence[NodeView]) -> dict[str, NodeState]:
    """The state of every node once the effects of its dependencies are applied.

    A node that is pending, ready or blocked becomes

    * ``blocked`` when a dependency failed, is blocked or was cancelled (so a
      failure reaches every transitive dependent in one pass: nodes are visited
      in ordinal order and read the *new* state of their dependencies);
    * ``ready`` when every dependency succeeded;
    * ``pending`` otherwise.

    Running, succeeded, failed and cancelled nodes keep their state. A blocked
    node whose failed dependency was re-opened becomes pending or ready again.
    """
    settled: dict[str, NodeState] = {}
    for node in nodes:
        if node.state not in _OPEN:
            settled[node.key] = node.state
            continue
        states = [settled[key] for key in node.depends_on]
        if any(state in UNSATISFIABLE_NODE_STATES for state in states):
            settled[node.key] = NodeState.BLOCKED
        elif all(state is NodeState.SUCCEEDED for state in states):
            settled[node.key] = NodeState.READY
        else:
            settled[node.key] = NodeState.PENDING
    return settled


def ready_batch(
    nodes: Sequence[NodeView],
    capacity: int,
    *,
    exclude: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Keys of the ready nodes to start now: the lowest ordinals first, at most
    ``capacity``, none of ``exclude`` (nodes that wait for a retry back-off).

    Independent nodes are started together (parallel-first): the only limits are
    ``capacity`` and the dependencies. The order is a function of the ordinals
    alone, so the same DAG always starts the same nodes in the same order.
    """
    if capacity <= 0:
        return ()
    ready = [
        node.key
        for node in nodes
        if node.state is NodeState.READY and node.key not in exclude
    ]
    return tuple(ready[:capacity])


def dag_verdict(nodes: Sequence[NodeView]) -> DagVerdict:
    """Whether the DAG is still active, succeeded or failed (given settled states).

    ``ACTIVE`` while a node is ready or running. Otherwise nothing can run any
    more: ``SUCCEEDED`` when every required node succeeded, else ``FAILED``. A
    node that is still ``pending`` although nothing is ready or running cannot
    happen in a DAG whose states were settled; it counts as not succeeded
    (fail closed).
    """
    if any(node.state in (NodeState.READY, NodeState.RUNNING) for node in nodes):
        return DagVerdict.ACTIVE
    if all(node.state is NodeState.SUCCEEDED for node in nodes if node.required):
        return DagVerdict.SUCCEEDED
    return DagVerdict.FAILED


def transitive_dependents(nodes: Sequence[NodeView], key: str) -> frozenset[str]:
    """Every node that depends on ``key``, directly or through other nodes."""
    found: set[str] = set()
    for node in nodes:
        if key in node.depends_on or found.intersection(node.depends_on):
            found.add(node.key)
    return frozenset(found)
