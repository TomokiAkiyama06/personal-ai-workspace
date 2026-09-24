"""The capabilities the backend can authorize.

A capability is one named action. Every capability has an explicit entry in
``CAPABILITIES`` with three properties:

* ``scope``: what part of the resource decides the outcome (:class:`Scope`);
* ``delegable``: whether an agent may ever exercise it on a user's behalf.
  There is **no default**: a new capability cannot be added without deciding
  this, and only the ones marked ``True`` can be delegated (an allowlist);
* ``audit``: how decisions are recorded (:class:`AuditMode`). The default is
  ``REQUIRED``: every decision is persisted and an allowed action is denied if
  the audit write fails. Only an explicit allowlist of read-only capabilities
  uses ``DENIED_ONLY``.

The tool-level capabilities of the Tool Broker (read / write / execute /
network / credential-use / destructive) are a separate, later layer (PAW-031);
they can only ever narrow what is decided here.
"""

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Scope(StrEnum):
    SYSTEM = "system"  # the system role alone decides; the resource is not consulted
    PROJECT = "project"  # a system role grant, or membership of resource.project_id
    SELF = "self"  # the resource must be owned by the principal itself


class Capability(StrEnum):
    # --- Scope.SELF: a person's own data (User and above) ---
    CHAT_USE = "chat.use"
    AGENT_USE = "agent.use"
    WORKSPACE_USE = "workspace.use"
    GITHUB_USE = "github.use"
    MEMORY_USE = "memory.use"
    PR_CREATE = "pr.create"

    # --- Scope.SYSTEM: workspace-wide ---
    SHARED_MEMORY_READ = "shared_memory.read"
    SHARED_MEMORY_MANAGE = "shared_memory.manage"
    # Admin (and Owner): "Admin-only" in docs/SECURITY_RBAC_AUDIT.md.
    ADMIN_USERS_MANAGE = "admin.users.manage"
    ADMIN_USAGE_VIEW = "admin.usage.view"
    ADMIN_QUOTA_MANAGE = "admin.quota.manage"
    ADMIN_AUDIT_VIEW = "admin.audit.view"
    ADMIN_SYSTEM_PROMPT_MANAGE = "admin.system_prompt.manage"
    ADMIN_MODELS_MANAGE = "admin.models.manage"
    ADMIN_ROUTING_MANAGE = "admin.routing.manage"
    ADMIN_PERMISSIONS_MANAGE = "admin.permissions.manage"
    ADMIN_CONFIG_MANAGE = "admin.config.manage"
    ADMIN_PROJECTS_MANAGE = "admin.projects.manage"
    # Owner only.
    OWNER_ADMINS_MANAGE = "owner.admins.manage"
    OWNER_OWNERSHIP_TRANSFER = "owner.ownership.transfer"
    OWNER_RECOVERY_MANAGE = "owner.recovery.manage"
    OWNER_USER_RESTORE = "owner.user_restore"
    OWNER_BACKUP_MANAGE = "owner.backup.manage"

    # --- Scope.PROJECT: inside one project ---
    PROJECT_READ = "project.read"
    PROJECT_CHAT = "project.chat"
    PROJECT_TASK_RUN = "project.task.run"
    PROJECT_REPO_WRITE = "project.repo.write"
    PROJECT_AGENT_USE = "project.agent.use"
    PROJECT_PR_CREATE = "project.pr.create"
    PROJECT_MEMORY_USE = "project.memory.use"
    PROJECT_MEMORY_MANAGE = "project.memory.manage"
    PROJECT_SETTINGS_MANAGE = "project.settings.manage"
    PROJECT_REPO_ADD = "project.repo.add"
    PROJECT_MEMBERS_MANAGE = "project.members.manage"
    PROJECT_AGENT_POLICY_MANAGE = "project.agent_policy.manage"
    PROJECT_LIFECYCLE_MANAGE = "project.lifecycle.manage"


class RepoPermission(StrEnum):
    """The permissions a repository's ACL override can grant (``REQUIREMENTS.md``)."""

    READ = "read"  # view the repository and its memory
    WRITE = "write"  # change the repository, commit, open PRs
    AGENT = "agent"  # let an agent operate on the repository


# Which repository permission a project capability needs when the resource is a
# repository. A capability that is absent here is not about a single repository:
# a resource that names a repository for it is refused.
REPO_PERMISSION_OF: MappingProxyType[Capability, RepoPermission] = MappingProxyType(
    {
        Capability.PROJECT_READ: RepoPermission.READ,
        Capability.PROJECT_MEMORY_USE: RepoPermission.READ,
        Capability.PROJECT_REPO_WRITE: RepoPermission.WRITE,
        Capability.PROJECT_PR_CREATE: RepoPermission.WRITE,
        Capability.PROJECT_TASK_RUN: RepoPermission.AGENT,
        Capability.PROJECT_AGENT_USE: RepoPermission.AGENT,
    }
)


class AuditMode(StrEnum):
    # Every decision is persisted. If the write fails, an allow becomes a denial.
    REQUIRED = "required"
    # Read-only capabilities: only denials are persisted (best effort), allowed
    # reads are not, and an audit failure never blocks the read.
    DENIED_ONLY = "denied_only"


@dataclass(frozen=True, slots=True)
class CapabilityInfo:
    scope: Scope
    # No default on purpose: see the module docstring.
    delegable: bool
    audit: AuditMode = AuditMode.REQUIRED


def _info(scope: Scope, *, delegable: bool, read_only: bool = False) -> CapabilityInfo:
    audit = AuditMode.DENIED_ONLY if read_only else AuditMode.REQUIRED
    return CapabilityInfo(scope, delegable=delegable, audit=audit)


C = Capability
CAPABILITIES: MappingProxyType[Capability, CapabilityInfo] = MappingProxyType(
    {
        C.CHAT_USE: _info(Scope.SELF, delegable=True),
        # Not delegable until PAW-032 defines derived (subset) grants for child
        # agents: an agent must not start agents that hold more than it does.
        C.AGENT_USE: _info(Scope.SELF, delegable=False),
        C.WORKSPACE_USE: _info(Scope.SELF, delegable=True),
        C.GITHUB_USE: _info(Scope.SELF, delegable=True),
        C.MEMORY_USE: _info(Scope.SELF, delegable=True),
        C.PR_CREATE: _info(Scope.SELF, delegable=True),
        C.SHARED_MEMORY_READ: _info(Scope.SYSTEM, delegable=True, read_only=True),
        C.SHARED_MEMORY_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.ADMIN_USERS_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.ADMIN_USAGE_VIEW: _info(Scope.SYSTEM, delegable=False),
        C.ADMIN_QUOTA_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.ADMIN_AUDIT_VIEW: _info(Scope.SYSTEM, delegable=False),
        C.ADMIN_SYSTEM_PROMPT_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.ADMIN_MODELS_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.ADMIN_ROUTING_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.ADMIN_PERMISSIONS_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.ADMIN_CONFIG_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.ADMIN_PROJECTS_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.OWNER_ADMINS_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.OWNER_OWNERSHIP_TRANSFER: _info(Scope.SYSTEM, delegable=False),
        C.OWNER_RECOVERY_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.OWNER_USER_RESTORE: _info(Scope.SYSTEM, delegable=False),
        C.OWNER_BACKUP_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.PROJECT_READ: _info(Scope.PROJECT, delegable=True, read_only=True),
        C.PROJECT_CHAT: _info(Scope.PROJECT, delegable=True),
        C.PROJECT_TASK_RUN: _info(Scope.PROJECT, delegable=True),
        C.PROJECT_REPO_WRITE: _info(Scope.PROJECT, delegable=True),
        C.PROJECT_AGENT_USE: _info(Scope.PROJECT, delegable=False),  # see AGENT_USE
        C.PROJECT_PR_CREATE: _info(Scope.PROJECT, delegable=True),
        C.PROJECT_MEMORY_USE: _info(Scope.PROJECT, delegable=True),
        C.PROJECT_MEMORY_MANAGE: _info(Scope.PROJECT, delegable=False),
        C.PROJECT_SETTINGS_MANAGE: _info(Scope.PROJECT, delegable=False),
        C.PROJECT_REPO_ADD: _info(Scope.PROJECT, delegable=False),
        C.PROJECT_MEMBERS_MANAGE: _info(Scope.PROJECT, delegable=False),
        C.PROJECT_AGENT_POLICY_MANAGE: _info(Scope.PROJECT, delegable=False),
        C.PROJECT_LIFECYCLE_MANAGE: _info(Scope.PROJECT, delegable=False),
    }
)
del C

# A capability without an entry would be an authorization hole (KeyError at
# decision time is fail-closed, but it must not be able to ship).
_undeclared = set(Capability) - set(CAPABILITIES)
if _undeclared:
    raise RuntimeError(f"capabilities without a CapabilityInfo: {sorted(_undeclared)}")
del _undeclared


def parse_capability(name: object) -> Capability | None:
    """Boundary helper: the capability with exactly this name, or ``None``.

    The authorization functions take :class:`Capability` members only. Code
    that has a *name* (stored in a Task record, say) converts it here, once,
    at the edge. Exact match only, no case folding or trimming, so a name a
    model produced can never be read as a capability it did not spell out.
    """
    if isinstance(name, str):
        try:
            return Capability(name)
        except ValueError:
            return None
    return None
