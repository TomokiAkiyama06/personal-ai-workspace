"""Backend authorization: roles, capabilities, decisions and audit events.

Permission decisions are made here, in the backend, from typed inputs. Callers
authorize through :class:`Authorizer`, which also records the decision; the
bare policy functions (``paw_backend.authz.policy``) record nothing and are
deliberately not exported. See ``apps/backend/README.md`` ("認可と Audit").
"""

from paw_backend.authz.audit import (
    AuditEvent,
    AuditSink,
    InMemoryAuditSink,
    PostgresAuditSink,
)
from paw_backend.authz.authorizer import Authorizer
from paw_backend.authz.capabilities import (
    CAPABILITIES,
    AuditMode,
    Capability,
    RepoPermission,
    Scope,
    parse_capability,
)
from paw_backend.authz.deps import (
    PrincipalProvider,
    UnauthenticatedProvider,
    install_authz,
    require_capability,
)
from paw_backend.authz.policy import DEFAULT_POLICY, Decision, Policy, Reason
from paw_backend.authz.principals import NoPrincipalDirectory, PrincipalDirectory
from paw_backend.authz.roles import ProjectRole, SystemRole
from paw_backend.authz.subjects import (
    ALL_PROJECTS,
    AgentGrant,
    Principal,
    ProjectState,
    RepoAcl,
    Resource,
)

__all__ = [
    "ALL_PROJECTS",
    "CAPABILITIES",
    "DEFAULT_POLICY",
    "AgentGrant",
    "AuditEvent",
    "AuditMode",
    "AuditSink",
    "Authorizer",
    "Capability",
    "Decision",
    "InMemoryAuditSink",
    "NoPrincipalDirectory",
    "Policy",
    "PostgresAuditSink",
    "Principal",
    "PrincipalDirectory",
    "PrincipalProvider",
    "ProjectRole",
    "ProjectState",
    "Reason",
    "RepoAcl",
    "RepoPermission",
    "Resource",
    "Scope",
    "SystemRole",
    "UnauthenticatedProvider",
    "install_authz",
    "parse_capability",
    "require_capability",
]
