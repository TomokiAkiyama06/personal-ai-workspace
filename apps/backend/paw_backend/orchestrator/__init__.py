"""DAG Agent Orchestrator (PAW-034).

Decomposes a task into a dependency DAG (a planner proposes, ``plan.py`` accepts),
runs the independent nodes in parallel, retries / changes approach / escalates a
failing node on its own, passes structured results between nodes and never lets a
sub-agent exceed its parent's rights or budget. There is no HTTP surface: the
orchestrator is driven by ``Orchestrator.run_once`` / ``serve`` (a worker process)
(and, in a later step, the periodic project task-stop loop). See
``apps/backend/README.md`` and ``docs/decisions/0021-dag-orchestrator-policy.md``.
"""

from paw_backend.orchestrator.config import Clock, OrchestratorConfig, SystemClock
from paw_backend.orchestrator.domain import (
    ROLE_CEILING,
    AttemptState,
    DagState,
    NextStep,
    NodeRole,
    NodeState,
    RunOutcome,
)
from paw_backend.orchestrator.errors import (
    DagAlreadyExistsError,
    DagNotFoundError,
    DagStateError,
    InvalidNodeResultError,
    InvalidOrchestratorArgumentError,
    InvalidPlanError,
    NodeStateError,
    NodeStopped,
    OrchestratorError,
    PlanReason,
    ResultReason,
    ScopeEscalationError,
    StaleDagEpochError,
    StaleNodeAttemptError,
    StopReason,
)
from paw_backend.orchestrator.gateway import (
    NodeBudgetHandle,
    NodeToolGateway,
    RunGuard,
    ToolCaller,
    TrackerBudgetProvider,
)
from paw_backend.orchestrator.orchestrator import (
    Orchestrator,
    RunReport,
    TaskAuthority,
    format_error_class,
    format_failure_text,
)
from paw_backend.orchestrator.plan import Plan, PlanNode
from paw_backend.orchestrator.records import AttemptRecord, DagRecord, NodeRecord
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.runtime import (
    AgentRuntime,
    NodeAssignment,
    NodeBudget,
    NodeOutcome,
    NodeTools,
    validate_runtime,
)
from paw_backend.orchestrator.scope import (
    agent_id_of,
    derive_child_scope,
    node_grant,
    scope_within,
)
from paw_backend.orchestrator.store import DagStore

__all__ = [
    "ROLE_CEILING",
    "AgentRuntime",
    "AttemptRecord",
    "AttemptState",
    "Clock",
    "DagAlreadyExistsError",
    "DagNotFoundError",
    "DagRecord",
    "DagState",
    "DagStateError",
    "DagStore",
    "InvalidNodeResultError",
    "InvalidOrchestratorArgumentError",
    "InvalidPlanError",
    "NextStep",
    "NodeAssignment",
    "NodeBudget",
    "NodeBudgetHandle",
    "NodeOutcome",
    "NodeRecord",
    "NodeResult",
    "NodeRole",
    "NodeState",
    "NodeStateError",
    "NodeStopped",
    "NodeToolGateway",
    "NodeTools",
    "Orchestrator",
    "OrchestratorConfig",
    "OrchestratorError",
    "Plan",
    "PlanNode",
    "PlanReason",
    "ResultReason",
    "RunGuard",
    "RunOutcome",
    "RunReport",
    "ScopeEscalationError",
    "StaleDagEpochError",
    "StaleNodeAttemptError",
    "StopReason",
    "SystemClock",
    "TaskAuthority",
    "ToolCaller",
    "TrackerBudgetProvider",
    "agent_id_of",
    "derive_child_scope",
    "format_error_class",
    "format_failure_text",
    "node_grant",
    "scope_within",
    "validate_runtime",
]
