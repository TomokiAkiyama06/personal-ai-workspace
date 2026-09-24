"""Plain value objects the policy decides on: who, on what, and agent grants.

Nothing here talks to a database or to the request. Authentication (PAW-022)
and the user / project stores (PAW-021, PAW-026) build a :class:`Principal`
and hand it to the policy through the provider seam in ``deps.py``.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from paw_backend.authz.capabilities import Capability, coerce_capability
from paw_backend.authz.roles import ProjectRole, SystemRole

# Identifiers end up in audit rows and logs, so they are restricted to a safe
# alphabet and length (UUIDs, slugs). Anything else is refused at construction.
ID_PATTERN = r"[A-Za-z0-9._:-]{1,128}"
_ID = re.compile(ID_PATTERN)
KIND_PATTERN = r"[a-z][a-z0-9_]{0,63}"
_KIND = re.compile(KIND_PATTERN)


def is_valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _checked_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not is_valid_id(value):
        # The offending value is deliberately not echoed.
        raise ValueError(f"{name} is not a valid identifier")
    return value


def _optional_id(value: object, name: str) -> str | None:
    return None if value is None else _checked_id(value, name)


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated human user, as far as authorization is concerned.

    ``project_roles`` maps a project id to the role of the user in that
    project; a project that is not in the map is a project the user does not
    belong to. The provider must only yield principals of *active* users.
    """

    user_id: str
    system_role: SystemRole
    project_roles: Mapping[str, ProjectRole] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _checked_id(self.user_id, "user_id")
        # Unknown roles are refused here rather than silently treated as "none".
        object.__setattr__(self, "system_role", SystemRole(self.system_role))
        object.__setattr__(
            self,
            "project_roles",
            MappingProxyType(
                {
                    _checked_id(project_id, "project_id"): ProjectRole(role)
                    for project_id, role in self.project_roles.items()
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class Resource:
    """What an action is about. Only ids the backend itself resolved belong here."""

    kind: str
    id: str | None = None
    project_id: str | None = None
    owner_id: str | None = None
    # Informational (audit only): the Repo ACL override is a later issue.
    repo_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or _KIND.fullmatch(self.kind) is None:
            raise ValueError("kind is not a valid resource kind")
        _optional_id(self.id, "id")
        _optional_id(self.project_id, "project_id")
        _optional_id(self.owner_id, "owner_id")
        _optional_id(self.repo_id, "repo_id")

    @classmethod
    def system(cls) -> "Resource":
        """The workspace itself (for workspace-wide capabilities)."""
        return cls(kind="system")

    @classmethod
    def project(cls, project_id: str, *, repo_id: str | None = None) -> "Resource":
        return cls(
            kind="project", id=project_id, project_id=project_id, repo_id=repo_id
        )

    @classmethod
    def owned_by(cls, owner_id: str, kind: str, id: str | None = None) -> "Resource":
        """A resource that belongs to one user (chat, memory, workspace, ...)."""
        return cls(kind=kind, id=id, owner_id=owner_id)


UNKNOWN_RESOURCE = Resource(kind="unknown")


@dataclass(frozen=True, slots=True)
class AgentGrant:
    """What the delegating user allowed one agent to do.

    A grant can only narrow: the agent's effective capabilities are the
    intersection of this set and what the user themselves may do (see
    ``policy.decide_agent``). ``project_ids`` further restricts the agent to
    those projects; ``None`` means no project restriction. The backend builds a
    grant from a Task's scope; text produced by a model is never parsed into one.
    """

    agent_id: str
    capabilities: frozenset[Capability]
    project_ids: frozenset[str] | None = None

    def __post_init__(self) -> None:
        _checked_id(self.agent_id, "agent_id")
        capabilities = _capabilities(self.capabilities)
        object.__setattr__(self, "capabilities", capabilities)
        if self.project_ids is not None:
            object.__setattr__(
                self,
                "project_ids",
                frozenset(_checked_id(p, "project_id") for p in self.project_ids),
            )


def _capabilities(values: Iterable[object]) -> frozenset[Capability]:
    result = set()
    for value in values:
        capability = coerce_capability(value)
        if capability is None:
            raise ValueError("a grant can only contain known capabilities")
        result.add(capability)
    return frozenset(result)
