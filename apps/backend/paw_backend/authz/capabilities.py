"""The capabilities the backend can authorize.

A capability is one named action. Its :class:`Scope` says what part of the
resource decides the outcome, and ``privileged`` marks the actions that change
who may do what or the configuration of the whole workspace: they are never
delegated to an agent and are denied when the audit trail cannot be written
(see ``authorizer.py``).

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


@dataclass(frozen=True, slots=True)
class CapabilityInfo:
    scope: Scope
    privileged: bool = False


_SELF = CapabilityInfo(Scope.SELF)
_SYSTEM = CapabilityInfo(Scope.SYSTEM)
_PRIVILEGED_SYSTEM = CapabilityInfo(Scope.SYSTEM, privileged=True)
_PROJECT = CapabilityInfo(Scope.PROJECT)
_PRIVILEGED_PROJECT = CapabilityInfo(Scope.PROJECT, privileged=True)

C = Capability
CAPABILITIES: MappingProxyType[Capability, CapabilityInfo] = MappingProxyType(
    {
        C.CHAT_USE: _SELF,
        C.AGENT_USE: _SELF,
        C.WORKSPACE_USE: _SELF,
        C.GITHUB_USE: _SELF,
        C.MEMORY_USE: _SELF,
        C.PR_CREATE: _SELF,
        C.SHARED_MEMORY_READ: _SYSTEM,
        C.SHARED_MEMORY_MANAGE: _PRIVILEGED_SYSTEM,
        C.ADMIN_USERS_MANAGE: _PRIVILEGED_SYSTEM,
        C.ADMIN_USAGE_VIEW: _PRIVILEGED_SYSTEM,
        C.ADMIN_QUOTA_MANAGE: _PRIVILEGED_SYSTEM,
        C.ADMIN_AUDIT_VIEW: _PRIVILEGED_SYSTEM,
        C.ADMIN_SYSTEM_PROMPT_MANAGE: _PRIVILEGED_SYSTEM,
        C.ADMIN_MODELS_MANAGE: _PRIVILEGED_SYSTEM,
        C.ADMIN_ROUTING_MANAGE: _PRIVILEGED_SYSTEM,
        C.ADMIN_PERMISSIONS_MANAGE: _PRIVILEGED_SYSTEM,
        C.ADMIN_CONFIG_MANAGE: _PRIVILEGED_SYSTEM,
        C.ADMIN_PROJECTS_MANAGE: _PRIVILEGED_SYSTEM,
        C.OWNER_ADMINS_MANAGE: _PRIVILEGED_SYSTEM,
        C.OWNER_OWNERSHIP_TRANSFER: _PRIVILEGED_SYSTEM,
        C.OWNER_RECOVERY_MANAGE: _PRIVILEGED_SYSTEM,
        C.OWNER_USER_RESTORE: _PRIVILEGED_SYSTEM,
        C.OWNER_BACKUP_MANAGE: _PRIVILEGED_SYSTEM,
        C.PROJECT_READ: _PROJECT,
        C.PROJECT_CHAT: _PROJECT,
        C.PROJECT_TASK_RUN: _PROJECT,
        C.PROJECT_REPO_WRITE: _PROJECT,
        C.PROJECT_AGENT_USE: _PROJECT,
        C.PROJECT_PR_CREATE: _PROJECT,
        C.PROJECT_MEMORY_USE: _PROJECT,
        C.PROJECT_MEMORY_MANAGE: _PROJECT,
        C.PROJECT_SETTINGS_MANAGE: _PROJECT,
        C.PROJECT_REPO_ADD: _PROJECT,
        C.PROJECT_MEMBERS_MANAGE: _PRIVILEGED_PROJECT,
        C.PROJECT_AGENT_POLICY_MANAGE: _PRIVILEGED_PROJECT,
        C.PROJECT_LIFECYCLE_MANAGE: _PRIVILEGED_PROJECT,
    }
)
del C

# A capability without an entry would be an authorization hole (KeyError at
# decision time is fail-closed, but it must not be able to ship).
_undeclared = set(Capability) - set(CAPABILITIES)
if _undeclared:
    raise RuntimeError(f"capabilities without a CapabilityInfo: {sorted(_undeclared)}")
del _undeclared


def coerce_capability(value: object) -> Capability | None:
    """Return the capability named by ``value``, or ``None`` if there is none.

    Exact match only (no case folding or trimming), so a string produced by a
    model can never be interpreted into a capability it did not name exactly.
    """
    if isinstance(value, Capability):
        return value
    if isinstance(value, str):
        try:
            return Capability(value)
        except ValueError:
            return None
    return None
