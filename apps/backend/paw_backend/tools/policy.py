"""The capability x context table: which approval level a tool needs.

``ToolPolicy`` maps a tool's capability classes, the environment the tool acts
in and where the call's targets lie relative to the task scope to an
:class:`~.capabilities.ApprovalLevel`. It is **default deny**: a combination
that has no entry is ``DENY``, and a tool's level is the most restrictive level
of its classes. The table is data, written out cell by cell in
``DEFAULT_TOOL_POLICY`` and pinned cell by cell in the tests; it is immutable,
and its inputs are enums the broker computes (never a string from a call), so
no prompt or model output can select a different row or edit the table.

The resource ACL is not a column: the intersection of the delegating user's
rights and the agent grant (PAW-025) is decided *before* a level is looked up,
and a denial there ends the call. The policy can only narrow it.

The default table follows ``docs/SECURITY_TOOL_PERMISSIONS.md``. Where the
document leaves a choice, the table takes the stricter reading (see the README
section "Tool Broker" for the list).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from paw_backend.tools.capabilities import (
    ApprovalLevel,
    Environment,
    ScopeStatus,
    ToolCapability,
    most_restrictive,
)

PolicyKey = tuple[ToolCapability, Environment, ScopeStatus]


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    table: Mapping[PolicyKey, ApprovalLevel]

    def __post_init__(self) -> None:
        if not isinstance(self.table, Mapping):
            raise TypeError("the policy table must be a mapping")
        checked: dict[PolicyKey, ApprovalLevel] = {}
        for key, level in self.table.items():
            if (
                not isinstance(key, tuple)
                or len(key) != 3
                or not isinstance(key[0], ToolCapability)
                or not isinstance(key[1], Environment)
                or not isinstance(key[2], ScopeStatus)
                or not isinstance(level, ApprovalLevel)
            ):
                raise TypeError("a policy entry is (capability, environment, scope)")
            if key[2] is ScopeStatus.OUT_OF_SCOPE and level is not ApprovalLevel.DENY:
                # A policy cannot allow what the task scope excludes: paths,
                # projects and credentials outside it stay denied.
                raise ValueError("out-of-scope entries must be DENY")
            checked[key] = level
        object.__setattr__(self, "table", MappingProxyType(checked))

    def level_for(
        self,
        capabilities: Iterable[ToolCapability],
        environment: Environment,
        scope: ScopeStatus,
    ) -> ApprovalLevel:
        """The level for a tool with ``capabilities``; ``DENY`` unless every
        class has an entry."""
        try:
            classes = list(capabilities)
        except TypeError:
            return ApprovalLevel.DENY
        if (
            not classes
            or not isinstance(environment, Environment)
            or not isinstance(scope, ScopeStatus)
            or not all(isinstance(c, ToolCapability) for c in classes)
        ):
            return ApprovalLevel.DENY
        return most_restrictive(
            self.table.get((c, environment, scope), ApprovalLevel.DENY) for c in classes
        )


_A, _S, _P = ApprovalLevel.AUTO, ApprovalLevel.SCOPED_AUTO, ApprovalLevel.APPROVAL
_X, _D = ApprovalLevel.STRONG_APPROVAL, ApprovalLevel.DENY
_C = ToolCapability
_LOCAL, _HOST = Environment.PROJECT_LOCAL, Environment.HOST
_IN, _HOST_OUT, _OUT = (
    ScopeStatus.IN_SCOPE,
    ScopeStatus.HOST_OUT_OF_SCOPE,
    ScopeStatus.OUT_OF_SCOPE,
)

# (capability, environment): the levels for (in scope, host out of scope, out
# of scope). Anything outside the task scope is denied, except a *host* beyond
# the task's hosts: reaching it needs a human, who sees the exact URL (a read
# is ``read`` + ``network``, an external write ``write`` + ``network``: both
# ``approval``; "external write beyond the task scope: APPROVAL"). A credential
# never goes to a host it is not valid for (``TaskScope.credential_handles``),
# whatever the level says.
_ROWS: dict[tuple[ToolCapability, Environment], tuple[ApprovalLevel, ...]] = {
    (_C.READ, _LOCAL): (_A, _P, _D),
    (_C.WRITE, _LOCAL): (_S, _P, _D),
    (_C.EXECUTE, _LOCAL): (_S, _D, _D),
    (_C.NETWORK, _LOCAL): (_S, _P, _D),
    (_C.CREDENTIAL_USE, _LOCAL): (_S, _D, _D),
    (_C.DESTRUCTIVE, _LOCAL): (_P, _D, _D),
    # Host-wide changes (packages, services, firewall, proxies, mounts) need
    # approval or more, network included; only a read is automatic.
    (_C.READ, _HOST): (_A, _P, _D),
    (_C.WRITE, _HOST): (_P, _P, _D),
    (_C.EXECUTE, _HOST): (_P, _D, _D),
    (_C.NETWORK, _HOST): (_P, _P, _D),
    (_C.CREDENTIAL_USE, _HOST): (_X, _D, _D),
    (_C.DESTRUCTIVE, _HOST): (_X, _D, _D),
}

DEFAULT_TOOL_POLICY = ToolPolicy(
    {
        (capability, environment, scope): level
        for (capability, environment), levels in _ROWS.items()
        for scope, level in zip((_IN, _HOST_OUT, _OUT), levels, strict=True)
    }
)
del _A, _S, _P, _X, _D, _C, _LOCAL, _HOST, _IN, _HOST_OUT, _OUT
