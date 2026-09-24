"""Backend authorization: roles, capabilities, decisions and audit events.

Permission decisions are made here, in the backend, from typed inputs. See
``apps/backend/README.md`` ("認可と Audit") for the design.
"""

from paw_backend.authz.audit import (
    AuditEvent,
    AuditSink,
    InMemoryAuditSink,
    PostgresAuditSink,
)
from paw_backend.authz.authorizer import Authorizer
from paw_backend.authz.capabilities import CAPABILITIES, Capability, Scope
from paw_backend.authz.deps import (
    PrincipalProvider,
    UnauthenticatedProvider,
    install_authz,
    require_capability,
)
from paw_backend.authz.policy import (
    DEFAULT_POLICY,
    Decision,
    Policy,
    Reason,
    decide,
    decide_agent,
)
from paw_backend.authz.roles import ProjectRole, SystemRole
from paw_backend.authz.subjects import AgentGrant, Principal, Resource

__all__ = [
    "CAPABILITIES",
    "DEFAULT_POLICY",
    "AgentGrant",
    "AuditEvent",
    "AuditSink",
    "Authorizer",
    "Capability",
    "Decision",
    "InMemoryAuditSink",
    "Policy",
    "PostgresAuditSink",
    "Principal",
    "PrincipalProvider",
    "ProjectRole",
    "Reason",
    "Resource",
    "Scope",
    "SystemRole",
    "UnauthenticatedProvider",
    "decide",
    "decide_agent",
    "install_authz",
    "require_capability",
]
