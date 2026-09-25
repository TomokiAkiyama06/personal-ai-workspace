"""Tool Broker and capability policy (PAW-031).

Agents reach tools only through :class:`ToolBroker`, which decides ``ALLOW`` /
``NEEDS_APPROVAL`` / ``DENY`` for every call and never executes one; an
injected :class:`ToolExecutor` (driven by :class:`ToolRunner`) does. The broker
narrows the PAW-025 authorization decision and never widens it. See
``apps/backend/README.md`` ("Tool Broker / Capability Policy").
"""

from paw_backend.tools.approval_memory import InMemoryApprovalStore
from paw_backend.tools.approval_store import PostgresApprovalStore
from paw_backend.tools.approval_types import (
    ApprovalBinding,
    ApprovalEvent,
    ApprovalEventKind,
    ApprovalHistoryEntry,
    ApprovalRecord,
    ApprovalStatus,
    ApprovalStore,
    ConsumeOutcome,
    DecideOutcome,
    NewApproval,
    OpenLimits,
    OpenOutcome,
    RevokeOutcome,
    SummaryItem,
)
from paw_backend.tools.approvals import (
    ApprovalOutcome,
    ApprovalResult,
    ApprovalRevocationError,
    ApprovalService,
    FailClosedStepUp,
    StepUpVerifier,
)
from paw_backend.tools.broker import ToolBroker
from paw_backend.tools.budget import (
    BudgetProvider,
    BudgetStatus,
    FailClosedBudgetProvider,
)
from paw_backend.tools.calls import TaskContext, ToolCall, ToolInvocation
from paw_backend.tools.capabilities import (
    ApprovalLevel,
    Environment,
    ScopeStatus,
    ToolCapability,
)
from paw_backend.tools.credentials import (
    contains_credential_plaintext,
    is_credential_handle,
    redact_value,
)
from paw_backend.tools.decisions import BrokerDecision, BrokerReason, Verdict
from paw_backend.tools.policy import DEFAULT_TOOL_POLICY, ToolPolicy
from paw_backend.tools.registry import (
    ArgumentKind,
    ArgumentSpec,
    ToolRegistry,
    ToolSpec,
)
from paw_backend.tools.runner import (
    ExecutionStatus,
    ToolExecutor,
    ToolOutcome,
    ToolRunner,
)
from paw_backend.tools.scope import (
    LexicalPathResolver,
    PathResolver,
    RealpathResolver,
    ScopedRepository,
    TargetError,
    TaskScope,
)
from paw_backend.tools.task_state import (
    FailClosedTaskActivity,
    PostgresTaskActivity,
    TaskActivity,
    TaskActivityProvider,
    TaskRun,
)

__all__ = [
    "DEFAULT_TOOL_POLICY",
    "ApprovalBinding",
    "ApprovalEvent",
    "ApprovalEventKind",
    "ApprovalHistoryEntry",
    "ApprovalLevel",
    "ApprovalOutcome",
    "ApprovalRecord",
    "ApprovalResult",
    "ApprovalRevocationError",
    "ApprovalService",
    "ApprovalStatus",
    "ApprovalStore",
    "ArgumentKind",
    "ArgumentSpec",
    "BrokerDecision",
    "BrokerReason",
    "BudgetProvider",
    "BudgetStatus",
    "ConsumeOutcome",
    "DecideOutcome",
    "Environment",
    "ExecutionStatus",
    "FailClosedBudgetProvider",
    "FailClosedStepUp",
    "FailClosedTaskActivity",
    "InMemoryApprovalStore",
    "LexicalPathResolver",
    "NewApproval",
    "OpenLimits",
    "OpenOutcome",
    "PathResolver",
    "PostgresApprovalStore",
    "PostgresTaskActivity",
    "RealpathResolver",
    "RevokeOutcome",
    "ScopeStatus",
    "ScopedRepository",
    "StepUpVerifier",
    "SummaryItem",
    "TargetError",
    "TaskActivity",
    "TaskActivityProvider",
    "TaskContext",
    "TaskRun",
    "TaskScope",
    "ToolBroker",
    "ToolCall",
    "ToolCapability",
    "ToolExecutor",
    "ToolInvocation",
    "ToolOutcome",
    "ToolPolicy",
    "ToolRegistry",
    "ToolRunner",
    "ToolSpec",
    "Verdict",
    "contains_credential_plaintext",
    "is_credential_handle",
    "redact_value",
]
