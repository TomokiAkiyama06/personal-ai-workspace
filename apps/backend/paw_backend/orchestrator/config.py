"""Configuration and the clock seam of the orchestrator (PAW-034).

The clock is injected so that a test moves time by hand: the heartbeat, the poll
of the task's state, the retry back-off and the node timeouts all wait through
``Clock.sleep`` and read ``Clock.monotonic``; nothing in the orchestrator reads a
wall clock (lease expiry and runtime are the database's, ``TaskQueue`` /
``BudgetTracker``).
"""

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.errors import InvalidOrchestratorArgumentError
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
class OrchestratorConfig:
    """Everything the orchestrator is told, checked when it is built.

    ``ladders`` maps every role to the agents that can take a node of that role,
    weakest first (Decision 0021, section 3): a node starts on the first, and an
    escalation moves it to the next. The labels name the runtimes the
    orchestrator is given. The three roles that judge (the Reviewer) or plan
    should not share an agent with the Worker they check when the ladder allows
    (``REQUIREMENTS.md``, "Review independence"): that is a matter of how the
    ladders are written, not something the orchestrator enforces.

    ``max_parallel_nodes``: the most nodes that run at once (an upper bound; the
    Resource Scheduler of PAW-036 will decide the real number).
    ``node_timeout_seconds``: an attempt that runs longer is failed.
    ``poll_seconds``: how often a running task's state is read for a Pause, a
    Cancel or a Stop Now. ``heartbeat_seconds``: how often the lease is extended
    (``None``: a third of the lease).
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
    _labels: frozenset[str] = field(init=False, repr=False, default=frozenset())

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

    @classmethod
    def uniform(cls, ladder: Sequence[str], **options) -> "OrchestratorConfig":
        """The same ladder for every role."""
        return cls({role: tuple(ladder) for role in NodeRole}, **options)

    @property
    def labels(self) -> frozenset[str]:
        """Every agent label some ladder names."""
        return self._labels

    def backoff(self, rung_attempts: int) -> float:
        """The wait before the next attempt, after ``rung_attempts`` on the rung."""
        if self.retry_backoff_seconds == 0 or rung_attempts < 1:
            return 0.0
        return min(
            self.retry_backoff_seconds * 2 ** (rung_attempts - 1),
            MAX_RETRY_BACKOFF_SECONDS,
        )
