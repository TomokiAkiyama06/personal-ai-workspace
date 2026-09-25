"""The seam to the agent runtimes (PAW-034): a Protocol, what a node is given and
what it returns.

The real runtimes (Codex, Claude, the Local model) are other issues; the
orchestrator drives any object with ``async run_node(assignment) -> NodeOutcome``
(``AgentRuntime``), which it checks when it is built (``validate_runtime``): a
missing method, a plain function where a coroutine is required or a wrong number
of parameters fails loudly at construction, not later as "every node failed".

What a runtime is given (``NodeAssignment``) and what it is **not** given:

* it gets the goal, the bounded input, the structured results of the nodes it
  depends on (its direct dependencies only), the label of the agent it plays and
  the attempt and approach it is on;
* it gets ``tools`` (:class:`NodeTools`) to call tools, and ``budget``
  (:class:`NodeBudget`) to report what it consumed. Both are the orchestrator's:
  every tool call passes the orchestrator's task check and the Tool Broker, and
  every charge goes to the parent task's budget;
* it never gets the Tool Broker, the runner, a ``TaskContext``, a grant, a scope,
  a database connection or another node's conversation.

``NodeStopped`` (``errors.py``) raised by ``tools`` or ``budget`` means: stop now.
A runtime lets it pass.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.errors import InvalidOrchestratorArgumentError
from paw_backend.orchestrator.plan import Plan
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.validation import check_label
from paw_backend.tasks.queueing import BudgetKind
from paw_backend.tools import ToolOutcome
from paw_backend.tools.interfaces import require_async_method


class NodeTools(Protocol):
    async def call(
        self,
        tool: object,
        arguments: object,
        *,
        approval_id: uuid.UUID | None = None,
    ) -> ToolOutcome:
        """Ask the Tool Broker to run a tool. ``tool`` and ``arguments`` are the
        model's; everything else is the backend's. Raises ``NodeStopped`` when the
        task can no longer act (it ended, another run replaced it, the worker lost
        its lease, the budget is used up)."""
        ...


class NodeBudget(Protocol):
    async def charge(self, kind: BudgetKind, amount: int) -> None:
        """Report consumption (tokens, tool calls, GPU seconds, ...) to the parent
        task's budget. Raises ``NodeStopped`` when the budget is now used up."""
        ...

    async def remaining(self) -> Mapping[BudgetKind, int | None]:
        """What is left of the parent task's budget (``None``: unlimited)."""
        ...


@dataclass(frozen=True, slots=True)
class NodeAssignment:
    """What an agent runtime is asked to do for one attempt of one node."""

    task_id: uuid.UUID
    node_key: str
    role: NodeRole
    title: str
    goal: str
    input: Mapping[str, Any]
    # The results of the direct dependencies, by node key.
    upstream: Mapping[str, NodeResult]
    # The label of the agent (a rung of the ladder) that plays the node.
    agent: str
    # The number of this attempt of the node (1 for the first) and the approach:
    # 0 is the original method, 1 the first alternative, and so on.
    attempt: int
    approach: int
    tools: NodeTools = field(repr=False)
    budget: NodeBudget = field(repr=False)


@dataclass(frozen=True, slots=True)
class NodeOutcome:
    """How an attempt ended. Build it with :meth:`succeeded` or :meth:`failed`.

    ``plan`` may accompany the result of the decomposition (the planner role's
    first call) only: no other node may propose nodes. ``message`` is the failure
    text; it is hashed for the loop detector and **never stored, logged or echoed**.
    ``retryable=False`` says that trying again is pointless (the node fails at
    once, without a retry or an escalation).
    """

    result: NodeResult | None = None
    plan: Mapping[str, Any] | Plan | None = None
    error_class: str | None = None
    message: str = field(default="", repr=False)
    retryable: bool = True

    def __post_init__(self) -> None:
        if (self.result is None) == (self.error_class is None):
            raise InvalidOrchestratorArgumentError("outcome")
        if self.result is not None and not isinstance(self.result, NodeResult):
            raise InvalidOrchestratorArgumentError("result")
        if self.plan is not None and (
            self.result is None or not isinstance(self.plan, Mapping | Plan)
        ):
            raise InvalidOrchestratorArgumentError("plan")
        if self.error_class is not None:
            check_label("error_class", self.error_class, maximum=200)
        if not isinstance(self.message, str) or not isinstance(self.retryable, bool):
            raise InvalidOrchestratorArgumentError("message")

    @classmethod
    def succeeded(
        cls, result: NodeResult, *, plan: Mapping[str, Any] | Plan | None = None
    ) -> "NodeOutcome":
        return cls(result=result, plan=plan)

    @classmethod
    def failed(
        cls, error_class: str, message: str = "", *, retryable: bool = True
    ) -> "NodeOutcome":
        return cls(error_class=error_class, message=message, retryable=retryable)

    @property
    def ok(self) -> bool:
        return self.result is not None


class AgentRuntime(Protocol):
    """Runs one attempt of one node."""

    async def run_node(self, assignment: NodeAssignment) -> NodeOutcome: ...


def validate_runtime(runtime: object, label: str) -> None:
    """``runtime`` must have ``async run_node(assignment)``; ``TypeError`` otherwise."""
    if runtime is None:
        raise TypeError(f"the runtime of {label} must not be None")
    require_async_method(runtime, "run_node", 1)
