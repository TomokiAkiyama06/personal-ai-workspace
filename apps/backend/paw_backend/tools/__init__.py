"""Tool Broker and capability policy (PAW-031).

Agents reach tools only through :class:`ToolBroker`, which decides ``ALLOW`` /
``NEEDS_APPROVAL`` / ``DENY`` for every call and never executes one; an
injected :class:`ToolExecutor` (driven by :class:`ToolRunner`) does. The broker
narrows the PAW-025 authorization decision and never widens it. See
``apps/backend/README.md`` ("Tool Broker / Capability Policy").
"""

# The run of a task is the lifecycle's own class (``TaskEvent.run``,
# ``TaskSnapshot.run``); the broker has none of its own. Re-exported so that
# ``from paw_backend.tools import TaskRun`` keeps working: it is the same object.
from paw_backend.tasks import TaskRun
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
    ROLE_WRITE_CEILING,
    LexicalPathResolver,
    PathResolver,
    RealpathResolver,
    ScopedRepository,
    TargetError,
    TaskScope,
    with_working_set_roles,
)
from paw_backend.tools.task_state import (
    FailClosedTaskActivity,
    PostgresTaskActivity,
    TaskActivity,
    TaskActivityProvider,
)
from paw_backend.tools.working_set import (
    WORKING_SET_TOOL_SPECS,
    BaselineProvider,
    FailClosedRegistrations,
    FailClosedWriteRecorder,
    RepositoryWriteRecorder,
    WorkingSetExecutor,
    WorkingSetRegistrations,
)

__all__ = [
    "DEFAULT_TOOL_POLICY",
    "ROLE_WRITE_CEILING",
    "WORKING_SET_TOOL_SPECS",
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
    "BaselineProvider",
    "BrokerDecision",
    "BrokerReason",
    "BudgetProvider",
    "BudgetStatus",
    "ConsumeOutcome",
    "DecideOutcome",
    "Environment",
    "ExecutionStatus",
    "FailClosedBudgetProvider",
    "FailClosedRegistrations",
    "FailClosedStepUp",
    "FailClosedTaskActivity",
    "FailClosedWriteRecorder",
    "InMemoryApprovalStore",
    "LexicalPathResolver",
    "NewApproval",
    "OpenLimits",
    "OpenOutcome",
    "PathResolver",
    "PostgresApprovalStore",
    "PostgresTaskActivity",
    "RealpathResolver",
    "RepositoryWriteRecorder",
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
    "WorkingSetExecutor",
    "WorkingSetRegistrations",
    "contains_credential_plaintext",
    "is_credential_handle",
    "redact_value",
    "with_working_set_roles",
]
