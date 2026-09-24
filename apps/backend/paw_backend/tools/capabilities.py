"""Tool capability classes, approval levels and the policy context enums.

These are closed enums. Nothing in this package turns a *string* (a tool name,
an argument, a model's text) into a capability class or a level: the classes of
a tool are declared once by backend code in its :class:`~.registry.ToolSpec`,
and the level is computed from them by :class:`~.policy.ToolPolicy`.
"""

from collections.abc import Iterable
from enum import StrEnum


class ToolCapability(StrEnum):
    """What a tool can do (``docs/SECURITY_TOOL_PERMISSIONS.md``, Capabilities)."""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    NETWORK = "network"
    CREDENTIAL_USE = "credential-use"
    DESTRUCTIVE = "destructive"


class ApprovalLevel(StrEnum):
    """How a call is handled (docs/SECURITY_TOOL_PERMISSIONS.md, Approval levels)."""

    AUTO = "auto"  # read-only / low risk
    SCOPED_AUTO = "scoped_auto"  # allowed only inside the authorised task scope
    APPROVAL = "approval"  # explicit human confirmation of this exact call
    STRONG_APPROVAL = "strong_approval"  # approval plus step-up authentication
    DENY = "deny"  # never exposed to an agent, approval or not

    @property
    def severity(self) -> int:
        return _SEVERITY[self]


_SEVERITY = {
    ApprovalLevel.AUTO: 0,
    ApprovalLevel.SCOPED_AUTO: 1,
    ApprovalLevel.APPROVAL: 2,
    ApprovalLevel.STRONG_APPROVAL: 3,
    ApprovalLevel.DENY: 4,
}
# A level added without a severity would sort wrongly: refuse to import.
if set(ApprovalLevel) != set(_SEVERITY):  # pragma: no cover
    raise RuntimeError("every ApprovalLevel needs a severity")


def most_restrictive(levels: Iterable[ApprovalLevel]) -> ApprovalLevel:
    """The strictest of ``levels``; ``DENY`` for none (fail closed)."""
    result: ApprovalLevel | None = None
    for level in levels:
        if not isinstance(level, ApprovalLevel):
            return ApprovalLevel.DENY
        if result is None or level.severity > result.severity:
            result = level
    return ApprovalLevel.DENY if result is None else result


class Environment(StrEnum):
    """Where a tool acts. Declared by the tool, never chosen by a call."""

    PROJECT_LOCAL = "project_local"  # inside the project / worktree
    HOST = "host"  # host-wide: packages, services, firewall, mounts


class ScopeStatus(StrEnum):
    """Where the targets of one call lie relative to the task scope."""

    IN_SCOPE = "in_scope"
    # Only a host (network target) is outside the task's hosts.
    HOST_OUT_OF_SCOPE = "host_out_of_scope"
    # A path, project or credential handle is outside the task scope.
    OUT_OF_SCOPE = "out_of_scope"
