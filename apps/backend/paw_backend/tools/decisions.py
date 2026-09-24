"""The broker's answer: a verdict, a stable reason code, and nothing sensitive.

A :class:`BrokerDecision` never contains the arguments or their content. The
tool name in it is the *registered* name (a name the model invented is never
echoed, not even in a denial), and every reason is a member of a closed enum,
safe to store, log and show.
"""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from paw_backend.authz import Reason
from paw_backend.tools.capabilities import ApprovalLevel

if TYPE_CHECKING:
    from paw_backend.tools.calls import ToolInvocation


class Verdict(StrEnum):
    ALLOW = "allow"  # the call may be executed now
    NEEDS_APPROVAL = "needs_approval"  # a human must approve this exact call first
    DENY = "deny"


class BrokerReason(StrEnum):
    """Why. Stable codes; also stored in the audit trail."""

    # --- ALLOW ---
    AUTO = "auto"
    SCOPED_AUTO = "scoped_auto"
    APPROVAL_CONSUMED = "approval_consumed"
    # --- NEEDS_APPROVAL ---
    APPROVAL_REQUIRED = "approval_required"
    STRONG_APPROVAL_REQUIRED = "strong_approval_required"
    APPROVAL_PENDING = "approval_pending"
    # --- DENY: the call itself ---
    INVALID_CALL = "invalid_call"
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    INVALID_TARGET = "invalid_target"
    # --- DENY: credentials ---
    CREDENTIAL_PLAINTEXT_DENIED = "credential_plaintext_denied"
    CREDENTIAL_PLAINTEXT_IN_ARGUMENTS = "credential_plaintext_in_arguments"
    CREDENTIAL_HANDLE_INVALID = "credential_handle_invalid"
    # --- DENY: task scope ---
    PATH_OUT_OF_SCOPE = "path_out_of_scope"
    HOST_OUT_OF_SCOPE = "host_out_of_scope"
    PROJECT_OUT_OF_SCOPE = "project_out_of_scope"
    CREDENTIAL_OUT_OF_SCOPE = "credential_out_of_scope"
    PATH_RESOLUTION_UNAVAILABLE = "path_resolution_unavailable"
    # --- DENY: authorization, policy, budget ---
    AUTHZ_DENIED = "authz_denied"
    AUTHZ_UNAVAILABLE = "authz_unavailable"
    POLICY_DENIED = "policy_denied"
    BUDGET_EXCEEDED = "budget_exceeded"
    BUDGET_UNKNOWN = "budget_unknown"
    BUDGET_UNAVAILABLE = "budget_unavailable"
    # --- DENY: approvals ---
    APPROVAL_NOT_FOUND = "approval_not_found"
    APPROVAL_MISMATCH = "approval_mismatch"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_ALREADY_USED = "approval_already_used"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_UNAVAILABLE = "approval_unavailable"
    # --- DENY: the record of the decision could not be written ---
    AUDIT_UNAVAILABLE = "audit_unavailable"
    # --- audit rows written after an allowed call ran (never a decision) ---
    EXECUTED = "executed"
    EXECUTION_FAILED = "execution_failed"


@dataclass(frozen=True, slots=True)
class BrokerDecision:
    verdict: Verdict
    reason: BrokerReason
    correlation_id: uuid.UUID
    # The registered tool name; ``None`` for a call that named no known tool.
    tool: str | None = None
    # The level the call needed; ``None`` when it was refused before that.
    level: ApprovalLevel | None = None
    # A lower-case hex SHA-256 binding tool, normalised arguments, task and requester.
    call_hash: str | None = None
    approval_id: uuid.UUID | None = None
    # The PAW-025 reason, when the authorization layer was the one to refuse.
    authz_reason: Reason | None = None
    # Present exactly when ``verdict`` is ALLOW: what the executor may run.
    invocation: "ToolInvocation | None" = field(default=None, repr=False)

    def __bool__(self) -> bool:
        # ``if await broker.request(call)`` must mean "may run now". Without
        # this every decision, a denial included, would be truthy.
        return self.verdict is Verdict.ALLOW

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW
