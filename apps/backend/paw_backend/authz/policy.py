"""The role to capability policy and the decision functions.

Everything here is a pure function of its arguments: no I/O, no clock, no
randomness, and no global mutable state. The same input always yields the same
:class:`Decision`. The answer is *default deny*: an action is allowed only when
a rule below says so, and every other path (unknown capability, missing or
malformed input, unmapped role, non-member of a project) ends in a denial with
a stable reason code.

Prompts, model output and tool arguments are never inputs to a decision. The
callers pass a typed :class:`Capability`, a :class:`Resource` the backend
resolved itself, and a :class:`Principal` (or, for an agent, the delegating
user's principal plus an :class:`AgentGrant` the backend built).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from paw_backend.authz.capabilities import (
    CAPABILITIES,
    Capability,
    Scope,
    coerce_capability,
)
from paw_backend.authz.roles import ProjectRole, SystemRole
from paw_backend.authz.subjects import AgentGrant, Principal, Resource


class Reason(StrEnum):
    """Stable, non-sensitive explanation of a decision (also stored in audit)."""

    # Allowed
    GRANTED_BY_SYSTEM_ROLE = "granted_by_system_role"
    GRANTED_BY_PROJECT_ROLE = "granted_by_project_role"
    GRANTED_TO_RESOURCE_OWNER = "granted_to_resource_owner"
    # Denied
    UNAUTHENTICATED = "unauthenticated"
    UNKNOWN_CAPABILITY = "unknown_capability"
    INVALID_RESOURCE = "invalid_resource"
    CAPABILITY_NOT_GRANTED = "capability_not_granted"
    NOT_PROJECT_MEMBER = "not_project_member"
    NOT_RESOURCE_OWNER = "not_resource_owner"
    AGENT_CAPABILITY_FORBIDDEN = "agent_capability_forbidden"
    AGENT_CAPABILITY_NOT_GRANTED = "agent_capability_not_granted"
    AGENT_PROJECT_NOT_GRANTED = "agent_project_not_granted"
    AUDIT_UNAVAILABLE = "audit_unavailable"


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: Reason
    # ``None`` when the requested capability does not exist.
    capability: Capability | None = None

    @classmethod
    def allow(cls, reason: Reason, capability: Capability) -> "Decision":
        return cls(True, reason, capability)

    @classmethod
    def deny(cls, reason: Reason, capability: Capability | None) -> "Decision":
        return cls(False, reason, capability)


def _grants(*sets: frozenset[Capability]) -> frozenset[Capability]:
    return frozenset().union(*sets)


C = Capability

_USER = frozenset(
    {
        C.CHAT_USE,
        C.AGENT_USE,
        C.WORKSPACE_USE,
        C.GITHUB_USE,
        C.MEMORY_USE,
        C.PR_CREATE,
        C.SHARED_MEMORY_READ,
    }
)
_ADMIN_ONLY = frozenset(
    {
        C.SHARED_MEMORY_MANAGE,
        C.ADMIN_USERS_MANAGE,
        C.ADMIN_USAGE_VIEW,
        C.ADMIN_QUOTA_MANAGE,
        C.ADMIN_AUDIT_VIEW,
        C.ADMIN_SYSTEM_PROMPT_MANAGE,
        C.ADMIN_MODELS_MANAGE,
        C.ADMIN_ROUTING_MANAGE,
        C.ADMIN_PERMISSIONS_MANAGE,
        C.ADMIN_CONFIG_MANAGE,
        C.ADMIN_PROJECTS_MANAGE,
        # "System Owner / Admin: administrative operations" on a project.
        C.PROJECT_LIFECYCLE_MANAGE,
    }
)
_OWNER_ONLY = frozenset(
    {
        C.OWNER_ADMINS_MANAGE,
        C.OWNER_OWNERSHIP_TRANSFER,
        C.OWNER_RECOVERY_MANAGE,
        C.OWNER_USER_RESTORE,
        C.OWNER_BACKUP_MANAGE,
    }
)

_VIEWER = frozenset({C.PROJECT_READ})
_CONTRIBUTOR_ONLY = frozenset(
    {
        C.PROJECT_CHAT,
        C.PROJECT_TASK_RUN,
        C.PROJECT_REPO_WRITE,
        C.PROJECT_AGENT_USE,
        C.PROJECT_PR_CREATE,
        C.PROJECT_MEMORY_USE,
    }
)
_MANAGER_ONLY = frozenset(
    {
        C.PROJECT_MEMBERS_MANAGE,
        C.PROJECT_REPO_ADD,
        C.PROJECT_SETTINGS_MANAGE,
        C.PROJECT_AGENT_POLICY_MANAGE,
        C.PROJECT_MEMORY_MANAGE,
        C.PROJECT_LIFECYCLE_MANAGE,
    }
)
del C


@dataclass(frozen=True, slots=True)
class Policy:
    """Which capabilities each role holds. Roles missing from a table hold none."""

    system_grants: Mapping[SystemRole, frozenset[Capability]]
    project_grants: Mapping[ProjectRole, frozenset[Capability]]

    def __post_init__(self) -> None:
        for role, granted in self.project_grants.items():
            not_project = {
                c for c in granted if CAPABILITIES[c].scope is not Scope.PROJECT
            }
            if not_project:
                # A membership must never confer workspace-wide or personal rights.
                raise ValueError(f"project role {role} grants a non-project capability")
        object.__setattr__(
            self, "system_grants", MappingProxyType(dict(self.system_grants))
        )
        object.__setattr__(
            self, "project_grants", MappingProxyType(dict(self.project_grants))
        )

    def system_role_allows(self, role: SystemRole, capability: Capability) -> bool:
        return capability in self.system_grants.get(role, frozenset())

    def project_role_allows(self, role: ProjectRole, capability: Capability) -> bool:
        return capability in self.project_grants.get(role, frozenset())


_ADMIN = _grants(_USER, _ADMIN_ONLY)
_CONTRIBUTOR = _grants(_VIEWER, _CONTRIBUTOR_ONLY)

DEFAULT_POLICY = Policy(
    system_grants={
        SystemRole.OWNER: _grants(_ADMIN, _OWNER_ONLY),
        SystemRole.ADMIN: _ADMIN,
        SystemRole.USER: _USER,
        SystemRole.SYSTEM: frozenset(),
    },
    project_grants={
        ProjectRole.MANAGER: _grants(_CONTRIBUTOR, _MANAGER_ONLY),
        ProjectRole.CONTRIBUTOR: _CONTRIBUTOR,
        ProjectRole.VIEWER: _VIEWER,
    },
)


def decide(
    principal: Principal | None,
    capability: Capability | str,
    resource: Resource | None,
    *,
    policy: Policy = DEFAULT_POLICY,
) -> Decision:
    """Decide whether ``principal`` may exercise ``capability`` on ``resource``."""
    cap = coerce_capability(capability)
    if cap is None:
        return Decision.deny(Reason.UNKNOWN_CAPABILITY, None)
    if not isinstance(principal, Principal):
        return Decision.deny(Reason.UNAUTHENTICATED, cap)
    if not isinstance(resource, Resource):
        return Decision.deny(Reason.INVALID_RESOURCE, cap)

    role = principal.system_role
    match CAPABILITIES[cap].scope:
        case Scope.SYSTEM:
            if policy.system_role_allows(role, cap):
                return Decision.allow(Reason.GRANTED_BY_SYSTEM_ROLE, cap)
            return Decision.deny(Reason.CAPABILITY_NOT_GRANTED, cap)

        case Scope.SELF:
            # Even the Owner has no access to another user's private data.
            if resource.owner_id is None:
                return Decision.deny(Reason.INVALID_RESOURCE, cap)
            if not policy.system_role_allows(role, cap):
                return Decision.deny(Reason.CAPABILITY_NOT_GRANTED, cap)
            if resource.owner_id != principal.user_id:
                return Decision.deny(Reason.NOT_RESOURCE_OWNER, cap)
            return Decision.allow(Reason.GRANTED_TO_RESOURCE_OWNER, cap)

        case Scope.PROJECT:
            if resource.project_id is None:
                return Decision.deny(Reason.INVALID_RESOURCE, cap)
            if policy.system_role_allows(role, cap):
                return Decision.allow(Reason.GRANTED_BY_SYSTEM_ROLE, cap)
            # A system role alone never opens a project (membership is by
            # invitation), so everything else needs a role in *this* project.
            project_role = principal.project_roles.get(resource.project_id)
            if project_role is None:
                return Decision.deny(Reason.NOT_PROJECT_MEMBER, cap)
            if policy.project_role_allows(project_role, cap):
                return Decision.allow(Reason.GRANTED_BY_PROJECT_ROLE, cap)
            return Decision.deny(Reason.CAPABILITY_NOT_GRANTED, cap)

    # Unreachable while every Scope is handled above; deny rather than fall through.
    return Decision.deny(Reason.CAPABILITY_NOT_GRANTED, cap)  # pragma: no cover


def decide_agent(
    delegator: Principal | None,
    grant: AgentGrant,
    capability: Capability | str,
    resource: Resource | None,
    *,
    policy: Policy = DEFAULT_POLICY,
) -> Decision:
    """Decide an action an agent performs on behalf of ``delegator``.

    The agent's effective capabilities are the *intersection* of what the
    delegating user may do and what the grant lists. So an agent can never do
    what its user cannot, whatever the grant says, and a grant never widens a
    user. Privileged capabilities (role / permission changes, workspace
    configuration, Owner operations) are refused for every agent, in line with
    "self privilege escalation" being DENY in ``docs/SECURITY_TOOL_PERMISSIONS.md``.
    """
    user_decision = decide(delegator, capability, resource, policy=policy)
    if not user_decision.allowed:
        return user_decision
    cap = user_decision.capability
    if cap is None or not isinstance(grant, AgentGrant):
        return Decision.deny(Reason.AGENT_CAPABILITY_NOT_GRANTED, cap)
    if CAPABILITIES[cap].privileged:
        return Decision.deny(Reason.AGENT_CAPABILITY_FORBIDDEN, cap)
    if cap not in grant.capabilities:
        return Decision.deny(Reason.AGENT_CAPABILITY_NOT_GRANTED, cap)
    if grant.project_ids is not None and (
        not isinstance(resource, Resource)
        or resource.project_id not in grant.project_ids
    ):
        return Decision.deny(Reason.AGENT_PROJECT_NOT_GRANTED, cap)
    return user_decision
