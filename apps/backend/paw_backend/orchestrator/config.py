"""Configuration and the clock seam of the orchestrator (PAW-034).

The clock is injected so that a test moves time by hand: the heartbeat, the poll
of the task's state, the retry back-off and the node timeouts all wait through
``Clock.sleep`` and read ``Clock.monotonic``; nothing in the orchestrator reads a
wall clock (lease expiry and runtime are the database's, ``TaskQueue`` /
``BudgetTracker``).
"""

import asyncio
import dataclasses
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.errors import (
    InvalidOrchestratorArgumentError,
    InvalidPlanError,
    PlanReason,
)
from paw_backend.orchestrator.limits import (
    DEFAULT_MAX_ATTEMPTS_PER_RUNG,
    DEFAULT_MAX_PARALLEL_NODES,
    DEFAULT_MAX_PLAN_ATTEMPTS,
    DEFAULT_NODE_TIMEOUT_SECONDS,
    DEFAULT_POLL_SECONDS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    MAX_ATTEMPTS_PER_RUNG,
    MAX_LADDER_LENGTH,
    MAX_NODE_TIMEOUT_SECONDS,
    MAX_PARALLEL_NODES,
    MAX_PLAN_ATTEMPTS,
    MAX_RETRY_BACKOFF_SECONDS,
)
from paw_backend.orchestrator.plan import Plan
from paw_backend.orchestrator.validation import (
    check_agent_label,
    check_int,
    check_seconds,
)


class Clock(Protocol):
    """Time as the orchestrator uses it: a monotonic reading and a sleep."""

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock."""

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    """One role of ``[orchestrator.assignment]`` (Decision 0099, 4): the agents
    a planner may assign a node of the role to (``allowed``, each on the role's
    ladder), and the one a node without an ``agent`` gets (``default``; ``None``
    for the reviewer, whose nodes without one start on the first rung of its
    ladder that ``allowed`` names)."""

    allowed: tuple[str, ...]
    default: str | None = None

    def __post_init__(self) -> None:
        allowed = self.allowed
        if isinstance(allowed, str | bytes) or not isinstance(allowed, Sequence):
            raise InvalidOrchestratorArgumentError("assignment")
        labels = tuple(check_agent_label("assignment", label) for label in allowed)
        if not labels or len(set(labels)) != len(labels):
            raise InvalidOrchestratorArgumentError("assignment")
        if self.default is not None and self.default not in labels:
            raise InvalidOrchestratorArgumentError("assignment")
        object.__setattr__(self, "allowed", labels)


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    """Everything the orchestrator is told, checked when it is built.

    ``ladders`` maps every role to the agents that can take a node of that role,
    weakest first (Decision 0021, section 3): a node starts on the rung of the
    agent it was assigned (Decision 0099, 4; the first rung for a node stored
    without one), and an escalation moves it to the next. The labels name the
    runtimes the orchestrator is given. The three roles that judge (the
    Reviewer) or plan should not share an agent with the Worker they check when
    the ladder allows (``REQUIREMENTS.md``, "Review independence"): that is a
    matter of how the ladders are written, not something the orchestrator
    enforces.

    ``assignment`` (``[orchestrator.assignment]``, Decision 0099, 4) maps every
    role to its :class:`RoleAssignment`: the agents a plan may assign a node of
    the role to (each on the role's ladder, or the configuration is refused) and
    the default. ``None`` (no assignment configured): every role allows only the
    first rung of its ladder, which is also its default, so a plan cannot move a
    node off the first rung (what the orchestrator did before Decision 0099).

    ``max_parallel_nodes``: the most nodes that run at once (an upper bound; the
    Resource Scheduler of PAW-036 will decide the real number).
    ``node_timeout_seconds``: an attempt that runs longer is failed.
    ``poll_seconds``: how often a running task's state is read for a Pause, a
    Cancel or a Stop Now. ``heartbeat_seconds``: how often the lease is extended
    (``None``: a quarter of the lease; ``Orchestrator`` refuses an interval
    whose three failed heartbeats in a row would not end before the lease does).
    ``retry_backoff_seconds``: the wait after the first failed attempt of a node;
    it doubles with each attempt on the rung (0 turns it off).
    ``max_attempts_per_rung``: attempts of one node on one agent in one run.
    ``max_plan_attempts``: tries to get an acceptable plan from the planner.
    """

    ladders: Mapping[NodeRole, tuple[str, ...]]
    max_parallel_nodes: int = DEFAULT_MAX_PARALLEL_NODES
    node_timeout_seconds: float = DEFAULT_NODE_TIMEOUT_SECONDS
    poll_seconds: float = DEFAULT_POLL_SECONDS
    heartbeat_seconds: float | None = None
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS
    max_attempts_per_rung: int = DEFAULT_MAX_ATTEMPTS_PER_RUNG
    max_plan_attempts: int = DEFAULT_MAX_PLAN_ATTEMPTS
    assignment: Mapping[NodeRole, RoleAssignment] | None = None
    _labels: frozenset[str] = field(init=False, repr=False, default=frozenset())
    _assigned: Mapping[NodeRole, RoleAssignment] = field(
        init=False, repr=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        if not isinstance(self.ladders, Mapping) or set(self.ladders) != set(NodeRole):
            raise InvalidOrchestratorArgumentError("ladders")
        ladders: dict[NodeRole, tuple[str, ...]] = {}
        for role in NodeRole:
            ladder = self.ladders[role]
            if isinstance(ladder, str | bytes) or not isinstance(ladder, Sequence):
                raise InvalidOrchestratorArgumentError("ladders")
            labels = tuple(check_agent_label("ladders", label) for label in ladder)
            if not 1 <= len(labels) <= MAX_LADDER_LENGTH or len(set(labels)) != len(
                labels
            ):
                raise InvalidOrchestratorArgumentError("ladders")
            ladders[role] = labels
        check_int(
            "max_parallel_nodes",
            self.max_parallel_nodes,
            minimum=1,
            maximum=MAX_PARALLEL_NODES,
        )
        check_seconds(
            "node_timeout_seconds",
            self.node_timeout_seconds,
            minimum=0.001,
            maximum=MAX_NODE_TIMEOUT_SECONDS,
        )
        check_seconds("poll_seconds", self.poll_seconds, minimum=0.001, maximum=3600)
        if self.heartbeat_seconds is not None:
            check_seconds(
                "heartbeat_seconds", self.heartbeat_seconds, minimum=0.001, maximum=3600
            )
        check_seconds(
            "retry_backoff_seconds",
            self.retry_backoff_seconds,
            minimum=0.001,
            maximum=MAX_RETRY_BACKOFF_SECONDS,
            allow_zero=True,
        )
        check_int(
            "max_attempts_per_rung",
            self.max_attempts_per_rung,
            minimum=1,
            maximum=MAX_ATTEMPTS_PER_RUNG,
        )
        check_int(
            "max_plan_attempts",
            self.max_plan_attempts,
            minimum=1,
            maximum=MAX_PLAN_ATTEMPTS,
        )
        object.__setattr__(self, "ladders", ladders)
        object.__setattr__(
            self,
            "_labels",
            frozenset(label for ladder in ladders.values() for label in ladder),
        )
        object.__setattr__(self, "_assigned", self._check_assignment(ladders))

    def _check_assignment(
        self, ladders: Mapping[NodeRole, tuple[str, ...]]
    ) -> Mapping[NodeRole, RoleAssignment]:
        if self.assignment is None:
            return MappingProxyType(
                {
                    role: RoleAssignment((ladder[0],), ladder[0])
                    for role, ladder in ladders.items()
                }
            )
        if not isinstance(self.assignment, Mapping) or set(self.assignment) != set(
            NodeRole
        ):
            raise InvalidOrchestratorArgumentError("assignment")
        assigned: dict[NodeRole, RoleAssignment] = {}
        for role in NodeRole:
            entry = self.assignment[role]
            if not isinstance(entry, RoleAssignment):
                raise InvalidOrchestratorArgumentError("assignment")
            # Decision 0099, 4 (1): an assignment is a rung of the role's ladder.
            if not set(entry.allowed) <= set(ladders[role]):
                raise InvalidOrchestratorArgumentError("assignment")
            assigned[role] = entry
        assigned_view = MappingProxyType(assigned)
        object.__setattr__(self, "assignment", assigned_view)
        return assigned_view

    @classmethod
    def uniform(cls, ladder: Sequence[str], **options) -> "OrchestratorConfig":
        """The same ladder for every role."""
        return cls({role: tuple(ladder) for role in NodeRole}, **options)

    @property
    def labels(self) -> frozenset[str]:
        """Every agent label some ladder names."""
        return self._labels

    def allowed_agents(self, role: NodeRole) -> RoleAssignment:
        """What a plan may assign a node of ``role`` to (see the class); the
        default filled in (the first allowed rung of the ladder for a role
        configured without one)."""
        entry = self._assigned[role]
        if entry.default is not None:
            return entry
        first = next(label for label in self.ladders[role] if label in entry.allowed)
        return RoleAssignment(entry.allowed, first)

    def assign(self, plan: Plan) -> Plan:
        """``plan`` with every node's agent assigned (Decision 0099, 4): a node's
        own ``agent`` when its role allows it, its role's default when it has
        none. ``InvalidPlanError`` (``AGENT_NOT_ALLOWED``) when a node names an
        agent its role does not allow: the whole plan is refused, never quietly
        given the default."""
        if not isinstance(plan, Plan):
            raise InvalidOrchestratorArgumentError("plan")
        nodes = []
        for node in plan.nodes:
            entry = self.allowed_agents(node.role)
            if node.agent is None:
                node = dataclasses.replace(node, agent=entry.default)
            elif node.agent not in entry.allowed:
                raise InvalidPlanError(PlanReason.AGENT_NOT_ALLOWED)
            nodes.append(node)
        # Built again: the assignment counts toward the plan's size.
        return Plan(tuple(nodes))

    def start_rung(self, role: NodeRole, agent: str) -> int:
        """The rung of ``role``'s ladder a node assigned ``agent`` starts on
        (Decision 0099, 4 (4): the absolute position on the ladder)."""
        return self.ladders[role].index(agent)

    def backoff(self, rung_attempts: int) -> float:
        """The wait before the next attempt, after ``rung_attempts`` on the rung."""
        if self.retry_backoff_seconds == 0 or rung_attempts < 1:
            return 0.0
        return min(
            self.retry_backoff_seconds * 2 ** (rung_attempts - 1),
            MAX_RETRY_BACKOFF_SECONDS,
        )
