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
    # Reading one's own Long-term Memory (Hybrid Retrieval's user scope):
    # Decision 0024 (Approved) supersedes Decision 0004 for it alone, adding it to
    # the read-only allowlist and to the delegation list. ``memory.use`` keeps
    # covering writing and proposing (``REQUIRED``). Held by the same roles.
    MEMORY_READ = "memory.read"
    PR_CREATE = "pr.create"
    # A person's own project membership (Decision 0022, Approved: it extends
    # Decision 0004's delegation list and Scope.SELF, and supersedes Decision 0008
    # section 5): answering an invitation addressed to oneself, and leaving a
    # project one is a member of. The resource is owned by the actor; the
    # project's state and the actor's role in it are not consulted (see
    # ``ProjectService``).
    PROJECT_INVITATION_RESPOND = "project.invitation.respond"
    PROJECT_LEAVE = "project.leave"

    # --- Scope.SYSTEM: workspace-wide ---
    # Any human user may create a project (REQUIREMENTS.md does not restrict who
    # may; Decision 0022). The creator becomes its first Manager.
    PROJECT_CREATE = "project.create"
    SHARED_MEMORY_READ = "shared_memory.read"
    # Reads what only managers may see (deleted memories, Candidates). The
    # operations that change Shared Memory each have a capability of their own
    # below, so that the audit action (the capability value) names the operation.
    SHARED_MEMORY_MANAGE = "shared_memory.manage"
    SHARED_MEMORY_CREATE = "shared_memory.create"
    SHARED_MEMORY_EDIT = "shared_memory.edit"
    SHARED_MEMORY_DELETE = "shared_memory.delete"
    SHARED_MEMORY_RESTORE = "shared_memory.restore"
    SHARED_MEMORY_CANDIDATE_APPROVE = "shared_memory.candidate.approve"
    SHARED_MEMORY_CANDIDATE_REJECT = "shared_memory.candidate.reject"
    # A person's own account (PAW-022): who is signed in and from where, changing
    # the password, signing devices out. Any human role holds them; an Agent
    # never does. ``account.read`` also covers the read-only view of the sessions.
    ACCOUNT_READ = "account.read"
    ACCOUNT_MANAGE = "account.manage"
    # The compact System Health state for the Global Header: the overall severity
    # and whether Codex / Claude are available (PAW-066, Decision 0059
    # Proposed). Any human role; read-only; never an Agent's.
    SYSTEM_HEALTH_SUMMARY_READ = "system_health.summary.read"
    # A person's own Notification Center (issue #188, Decision 0070 Approved):
    # reading their notifications and the event stream (``notification.read``,
    # read-only), marking them read and dismissing them (``notification.manage``).
    # Which notifications a person receives is decided by the store (their own,
    # and the audiences their role holds). Any human role; never an Agent's.
    NOTIFICATION_READ = "notification.read"
    NOTIFICATION_MANAGE = "notification.manage"
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
    # The workspace authentication policy (Passkey requirement per role, Step-up
    # window; Decision 0015): an Admin may look at it, only the Owner changes it.
    ADMIN_AUTH_POLICY_VIEW = "admin.auth_policy.view"
    # Start and end Kaggle / Full GPU Mode (issue #33, Decision 0055):
    # every local GPU task of every user is held while an exclusive job has the
    # GPU. Owner / Admin, never an Agent.
    ADMIN_COMPUTE_FULL_GPU = "admin.compute.full_gpu"
    # The System Health detail: every component, its metrics, the time series and
    # the health events (PAW-066, Decision 0059 Proposed). Owner / Admin;
    # read-only; never an Agent's.
    ADMIN_SYSTEM_HEALTH_VIEW = "admin.system_health.view"
    # Owner only.
    OWNER_ADMINS_MANAGE = "owner.admins.manage"
    OWNER_OWNERSHIP_TRANSFER = "owner.ownership.transfer"
    OWNER_RECOVERY_MANAGE = "owner.recovery.manage"
    OWNER_USER_RESTORE = "owner.user_restore"
    OWNER_BACKUP_MANAGE = "owner.backup.manage"
    OWNER_AUTH_POLICY_MANAGE = "owner.auth_policy.manage"

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
    # Change the Working Set of a task of the project (issue #85, Decision 0030):
    # add a repository, change its role, remove it. Decided on the project; the
    # repository itself is decided separately, with the permission of the change
    # (``tasks.working_set.required_permission``), and both must allow.
    PROJECT_TASK_WORKING_SET_MANAGE = "project.task.working_set.manage"
    # Release, by hand, the repository write reservation a crashed process left
    # on a task of the project (issue #129, Decision 0049): project
    # Manager, and Owner / Admin. Not delegable; a Passkey Step-up besides
    # (``projects.task_write_release``).
    PROJECT_TASK_WRITE_RESERVATION_RELEASE = "project.task.write_reservation.release"


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
        # Not delegable: an agent must not start agents that hold more than it
        # does. The derived (subset) grant of a child agent now exists
        # (``delegation.derive_child_grant``, PAW-034) and the orchestrator is the
        # only thing that starts sub-agents, so no agent needs this capability;
        # making it delegable is a policy change for a new Decision
        # (docs/decisions/0021-*.md, section 9).
        C.AGENT_USE: _info(Scope.SELF, delegable=False),
        C.WORKSPACE_USE: _info(Scope.SELF, delegable=True),
        C.GITHUB_USE: _info(Scope.SELF, delegable=True),
        C.MEMORY_USE: _info(Scope.SELF, delegable=True),
        # An agent's decision is REQUIRED whatever the mode (Decision 0004).
        C.MEMORY_READ: _info(Scope.SELF, delegable=True, read_only=True),
        C.PR_CREATE: _info(Scope.SELF, delegable=True),
        # Answering one's own invitation and leaving a project change who belongs
        # to a project: a person's own act, never an agent's (Decision 0022).
        C.PROJECT_INVITATION_RESPOND: _info(Scope.SELF, delegable=False),
        C.PROJECT_LEAVE: _info(Scope.SELF, delegable=False),
        C.PROJECT_CREATE: _info(Scope.SYSTEM, delegable=False),
        # The person's own account: never delegable (an Agent must not read or
        # change credentials and sessions). Reading is on the read-only allowlist.
        C.ACCOUNT_READ: _info(Scope.SYSTEM, delegable=False, read_only=True),
        C.ACCOUNT_MANAGE: _info(Scope.SYSTEM, delegable=False),
        # Operational data, no user content: on the read-only allowlist
        # (Decision 0059). Not delegable: no agent needs to watch the system.
        C.SYSTEM_HEALTH_SUMMARY_READ: _info(
            Scope.SYSTEM, delegable=False, read_only=True
        ),
        # A person's own notifications (Decision 0070): no agent needs them.
        # Reading is on the read-only allowlist (the list is the person's own and
        # holds codes only); read / dismissed changes only their own view.
        C.NOTIFICATION_READ: _info(Scope.SYSTEM, delegable=False, read_only=True),
        C.NOTIFICATION_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.SHARED_MEMORY_READ: _info(Scope.SYSTEM, delegable=True, read_only=True),
        C.SHARED_MEMORY_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.SHARED_MEMORY_CREATE: _info(Scope.SYSTEM, delegable=False),
        C.SHARED_MEMORY_EDIT: _info(Scope.SYSTEM, delegable=False),
        C.SHARED_MEMORY_DELETE: _info(Scope.SYSTEM, delegable=False),
        C.SHARED_MEMORY_RESTORE: _info(Scope.SYSTEM, delegable=False),
        C.SHARED_MEMORY_CANDIDATE_APPROVE: _info(Scope.SYSTEM, delegable=False),
        C.SHARED_MEMORY_CANDIDATE_REJECT: _info(Scope.SYSTEM, delegable=False),
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
        C.ADMIN_AUTH_POLICY_VIEW: _info(Scope.SYSTEM, delegable=False),
        C.ADMIN_COMPUTE_FULL_GPU: _info(Scope.SYSTEM, delegable=False),
        C.ADMIN_SYSTEM_HEALTH_VIEW: _info(
            Scope.SYSTEM, delegable=False, read_only=True
        ),
        C.OWNER_ADMINS_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.OWNER_OWNERSHIP_TRANSFER: _info(Scope.SYSTEM, delegable=False),
        C.OWNER_RECOVERY_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.OWNER_USER_RESTORE: _info(Scope.SYSTEM, delegable=False),
        C.OWNER_BACKUP_MANAGE: _info(Scope.SYSTEM, delegable=False),
        C.OWNER_AUTH_POLICY_MANAGE: _info(Scope.SYSTEM, delegable=False),
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
        # Delegable (#85 constraint 3; Decision 0035, Approved, point 2): the
        # Working Set is changed through tools an agent calls (Decision 0030,
        # section 3); a non-delegable capability
        # would refuse every such call before its approval. It never widens what
        # the agent may do on a repository: every change also needs the
        # repository permission of the change (``project.read`` / ``project.repo
        # .write``, AND), and every change but adding a ``referenced`` repository
        # needs STRONG_APPROVAL (a human, with Step-up).
        C.PROJECT_TASK_WORKING_SET_MANAGE: _info(Scope.PROJECT, delegable=True),
        # Not delegable (Decision 0049): it lifts a guard that exists
        # because an agent's executor may still be writing; a person decides,
        # after checking that the executor is gone.
        C.PROJECT_TASK_WRITE_RESERVATION_RELEASE: _info(Scope.PROJECT, delegable=False),
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
