"""Plain value objects the policy decides on: who, on what, and agent grants.

Nothing here talks to a database or to the request. Authentication (PAW-022)
and the user / project stores (PAW-021, PAW-026) build a :class:`Principal`
and hand it to the policy through the provider seam in ``deps.py``.

Every identifier (user, project, repository, resource, agent) is a UUID. A
``uuid.UUID`` or its canonical string (lower-case, hyphenated) is accepted and
normalised to ``uuid.UUID``; nothing else is, so free-text names can never end
up in a decision, a log line or an audit row, and an audit trail holds opaque
ids only.
"""

import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from types import MappingProxyType

from paw_backend.authz.capabilities import Capability, parse_capability
from paw_backend.authz.roles import ProjectRole, SystemRole

KIND_PATTERN = r"[a-z][a-z0-9_]{0,63}"
_KIND = re.compile(KIND_PATTERN)
# A client-chosen request ID (X-Request-ID) that is safe to store and log.
CLIENT_REQUEST_ID_PATTERN = r"[A-Za-z0-9._-]{1,64}"
_CLIENT_REQUEST_ID = re.compile(CLIENT_REQUEST_ID_PATTERN)


def to_uuid(value: object, name: str) -> uuid.UUID:
    """A ``uuid.UUID`` or its canonical string as ``uuid.UUID``; else ``ValueError``."""
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            parsed = None
        if parsed is not None and str(parsed) == value:
            return parsed
    # The offending value is deliberately not echoed.
    raise ValueError(f"{name} is not a UUID")


def _optional_uuid(value: object, name: str) -> uuid.UUID | None:
    return None if value is None else to_uuid(value, name)


def is_valid_client_request_id(value: object) -> bool:
    return isinstance(value, str) and _CLIENT_REQUEST_ID.fullmatch(value) is not None


class ProjectState(StrEnum):
    """Lifecycle state of a project (``REQUIREMENTS.md`` "Project lifecycle")."""

    ACTIVE = "active"
    ARCHIVED = "archived"  # read-only
    PENDING_DELETION = "pending_deletion"  # access stopped, restorable


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated human user, as far as authorization is concerned.

    ``project_roles`` maps a project id to the role of the user in that
    project; a project that is not in the map is a project the user does not
    belong to. The provider must only yield principals of *active* users.
    """

    user_id: uuid.UUID
    system_role: SystemRole
    project_roles: Mapping[uuid.UUID, ProjectRole] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", to_uuid(self.user_id, "user_id"))
        # Unknown roles are refused here rather than silently treated as "none".
        object.__setattr__(self, "system_role", SystemRole(self.system_role))
        object.__setattr__(
            self,
            "project_roles",
            MappingProxyType(
                {
                    to_uuid(project_id, "project_id"): ProjectRole(role)
                    for project_id, role in self.project_roles.items()
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class Resource:
    """What an action is about. Only ids the backend itself resolved belong here.

    ``project_state`` is the stored state of ``project_id``. It has no default:
    a decision on a project resource without it is refused, so a caller cannot
    forget to pass "archived" and get write access by accident.

    ``repo_id`` is refused by the policy (``repo_acl_not_supported``) until
    per-repository ACLs exist, because a repository can be "access denied"
    inside a project the user belongs to (``REQUIREMENTS.md``, "Project roles").
    """

    kind: str
    id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    repo_id: uuid.UUID | None = None
    project_state: ProjectState | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or _KIND.fullmatch(self.kind) is None:
            raise ValueError("kind is not a valid resource kind")
        for name in ("id", "project_id", "owner_id", "repo_id"):
            object.__setattr__(self, name, _optional_uuid(getattr(self, name), name))
        if self.project_state is not None:
            object.__setattr__(self, "project_state", ProjectState(self.project_state))
        if self.project_state is not None and self.project_id is None:
            raise ValueError("project_state needs a project_id")

    @classmethod
    def system(cls) -> "Resource":
        """The workspace itself (for workspace-wide capabilities)."""
        return cls(kind="system")

    @classmethod
    def project(
        cls,
        project_id: uuid.UUID | str,
        project_state: ProjectState,
        *,
        repo_id: uuid.UUID | str | None = None,
    ) -> "Resource":
        return cls(
            kind="project",
            id=project_id,
            project_id=project_id,
            repo_id=repo_id,
            project_state=project_state,
        )

    @classmethod
    def owned_by(
        cls, owner_id: uuid.UUID | str, kind: str, id: uuid.UUID | str | None = None
    ) -> "Resource":
        """A resource that belongs to one user (chat, memory, workspace, ...)."""
        return cls(kind=kind, id=id, owner_id=owner_id)


UNKNOWN_RESOURCE = Resource(kind="unknown")


class ProjectScope(Enum):
    ALL = "all"


# "Every project the delegating user is in". It has to be written out: an
# agent working for one project must not silently reach the user's others.
ALL_PROJECTS = ProjectScope.ALL


@dataclass(frozen=True, slots=True)
class AgentGrant:
    """What the delegating user allowed one agent to do.

    A grant can only narrow: the agent's effective capabilities are the
    intersection of ``capabilities`` and what the user themselves may do (see
    ``policy.decide_agent``). ``project_ids`` is **required**: the set of
    projects the agent may touch, or ``ALL_PROJECTS``. A project-restricted
    grant covers no resource outside those projects, personal ones included.

    The backend builds a grant from a Task's scope (:meth:`from_names` for
    names read from storage); text produced by a model is never parsed into one.
    """

    agent_id: uuid.UUID
    capabilities: frozenset[Capability]
    project_ids: frozenset[uuid.UUID] | ProjectScope

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", to_uuid(self.agent_id, "agent_id"))
        capabilities = self.capabilities
        if isinstance(capabilities, str | bytes) or not isinstance(
            capabilities, Iterable
        ):
            raise TypeError("capabilities must be a collection of Capability")
        if not all(isinstance(c, Capability) for c in capabilities):
            raise ValueError("a grant can only contain Capability members")
        object.__setattr__(self, "capabilities", frozenset(capabilities))

        projects = self.project_ids
        if projects is not ALL_PROJECTS:
            if isinstance(projects, str | bytes) or not isinstance(projects, Iterable):
                # A bare "p1" would otherwise become the projects {"p", "1"}.
                raise TypeError("project_ids must be ALL_PROJECTS or a collection")
            object.__setattr__(
                self,
                "project_ids",
                frozenset(to_uuid(p, "project_id") for p in projects),
            )

    @classmethod
    def from_names(
        cls,
        agent_id: uuid.UUID | str,
        capability_names: Iterable[str],
        project_ids: Iterable[uuid.UUID | str] | ProjectScope,
    ) -> "AgentGrant":
        """Boundary constructor for capability names read from storage."""
        if isinstance(capability_names, str | bytes):
            raise TypeError("capability_names must be a collection of names")
        capabilities = set()
        for name in capability_names:
            capability = parse_capability(name)
            if capability is None:
                raise ValueError("a grant can only contain known capabilities")
            capabilities.add(capability)
        return cls(agent_id, frozenset(capabilities), project_ids)
