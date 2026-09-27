"""Immutable values the DAG store returns (PAW-034).

Plain dataclasses, independent of SQLAlchemy, so that a DAG can be handed to the
API layer (or a test) without exposing ORM rows. ``NodeRecord.result`` is the
typed ``NodeResult``; the failure text of a node is never here (only its class).
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from paw_backend.authz import Capability
from paw_backend.orchestrator.domain import (
    AttemptState,
    DagState,
    NodeRole,
    NodeState,
)
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.scheduling import NodeView


@dataclass(frozen=True, slots=True)
class NodeRecord:
    key: str
    ordinal: int
    role: NodeRole
    title: str
    goal: str
    input: Mapping[str, Any]
    required: bool
    capabilities: tuple[Capability, ...] | None
    repositories: tuple[uuid.UUID, ...] | None
    depends_on: tuple[str, ...]
    state: NodeState
    agent_index: int
    approach: int
    attempt_count: int
    rung_attempts: int
    result: NodeResult | None
    error_class: str | None

    def view(self) -> NodeView:
        return NodeView(
            self.key, self.ordinal, self.state, self.depends_on, self.required
        )


@dataclass(frozen=True, slots=True)
class DagRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    attempt: int
    task_retry_count: int
    state: DagState
    epoch: int
    owner: str | None
    nodes: tuple[NodeRecord, ...]  # in ordinal order
    created_at: datetime
    updated_at: datetime

    def node(self, key: str) -> NodeRecord:
        for node in self.nodes:
            if node.key == key:
                return node
        raise KeyError(key)

    def views(self) -> tuple[NodeView, ...]:
        return tuple(node.view() for node in self.nodes)

    def states(self) -> dict[str, NodeState]:
        return {node.key: node.state for node in self.nodes}


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    id: int
    dag_id: uuid.UUID
    node_key: str
    number: int
    agent_index: int
    approach: int
    epoch: int
    state: AttemptState
    error_class: str | None
    failure_signature: str | None
    started_at: datetime
    finished_at: datetime | None
