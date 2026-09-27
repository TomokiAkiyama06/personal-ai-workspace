"""Grants of child agents, derived from the grant of their parent (PAW-034).

``docs/decisions/0004-*.md`` made ``agent.use`` and ``project.agent.use`` non
delegable "until a way exists to derive the grant of a child agent as a subset of
its parent's". This module is that way: the only function that makes the grant
of a sub-agent is :func:`derive_child_grant`, and it can only **narrow**.

* The child's capabilities are a subset of the parent's, and only ones that can
  be delegated at all (``CAPABILITIES[...].delegable``). Asking for one the
  parent does not hold is an error (``GrantEscalationError``), never a silent
  clip: a request for more than the parent has is a bug or an attack that must
  be seen.
* The child's projects are a subset of the parent's. ``ALL_PROJECTS`` can only
  come from a parent that has ``ALL_PROJECTS``.
* The child is a different agent (its id differs from the parent's), so that an
  audit row tells the two apart.

A grant still never widens what the delegating user may do: at decision time the
child's rights are the intersection of its grant and the user's rights
(``policy.decide_agent``), so a chain of derived grants is bounded by the user at
every link. The derivation adds the second bound: by the parent, at derivation
time (a parent whose own grant is later narrowed does not narrow a child that was
already made; the orchestrator derives a fresh grant for every node attempt).

Pure functions: no database, no clock, no randomness.
"""

import uuid
from collections.abc import Iterable
from enum import StrEnum

from paw_backend.authz.capabilities import CAPABILITIES, Capability
from paw_backend.authz.subjects import ALL_PROJECTS, AgentGrant, ProjectScope, to_uuid


class EscalationReason(StrEnum):
    SAME_AGENT = "same_agent"  # the child would be the parent itself
    CAPABILITY = "capability"  # a capability the parent does not hold
    NOT_DELEGABLE = "not_delegable"  # a capability that no agent may exercise
    PROJECT = "project"  # a project the parent may not touch


_MESSAGES = {
    EscalationReason.SAME_AGENT: "A child agent must differ from its parent",
    EscalationReason.CAPABILITY: "A child grant cannot exceed its parent's rights",
    EscalationReason.NOT_DELEGABLE: "A child grant cannot hold a non-delegable right",
    EscalationReason.PROJECT: "A child grant cannot reach its parent's other projects",
}


class GrantEscalationError(ValueError):
    """A child grant would be wider than its parent's (or not a child at all).

    ``reason`` is a closed enum member; the message is fixed and holds no id,
    capability name or project.
    """

    def __init__(self, reason: EscalationReason) -> None:
        self.reason = reason
        super().__init__(_MESSAGES[reason])


def _capabilities(value: object) -> frozenset[Capability]:
    if isinstance(value, str | bytes) or not isinstance(value, Iterable):
        raise TypeError("capabilities must be a collection of Capability")
    members = list(value)
    if not all(isinstance(member, Capability) for member in members):
        raise ValueError("capabilities can only contain Capability members")
    return frozenset(members)


def _projects(value: object) -> frozenset[uuid.UUID] | ProjectScope:
    if value is ALL_PROJECTS:
        return ALL_PROJECTS
    if isinstance(value, str | bytes) or not isinstance(value, Iterable):
        raise TypeError("project_ids must be ALL_PROJECTS or a collection")
    return frozenset(to_uuid(project, "project_id") for project in value)


def delegable_capabilities(grant: AgentGrant) -> frozenset[Capability]:
    """The capabilities of ``grant`` that an agent can exercise at all."""
    return frozenset(c for c in grant.capabilities if CAPABILITIES[c].delegable)


def projects_within(
    child: frozenset[uuid.UUID] | ProjectScope,
    parent: frozenset[uuid.UUID] | ProjectScope,
) -> bool:
    """Whether the project set ``child`` reaches nothing that ``parent`` does not."""
    if parent is ALL_PROJECTS:
        return True
    if child is ALL_PROJECTS:
        return False
    return child <= parent


def is_subgrant(child: AgentGrant, parent: AgentGrant) -> bool:
    """Whether ``child`` holds nothing (capability or project) beyond ``parent``."""
    if not isinstance(child, AgentGrant) or not isinstance(parent, AgentGrant):
        raise TypeError("both arguments must be AgentGrant objects")
    return child.capabilities <= parent.capabilities and projects_within(
        child.project_ids, parent.project_ids
    )


def derive_child_grant(
    parent: AgentGrant,
    *,
    child_agent_id: uuid.UUID | str,
    capabilities: Iterable[Capability] | None = None,
    project_ids: Iterable[uuid.UUID | str] | ProjectScope | None = None,
) -> AgentGrant:
    """The grant of a sub-agent of the agent that holds ``parent``.

    ``capabilities`` (``None``: every delegable capability of the parent) and
    ``project_ids`` (``None``: the parent's) can only ask for less. A request for
    something the parent lacks raises :class:`GrantEscalationError`; a wrong type
    raises ``TypeError`` / ``ValueError`` like ``AgentGrant`` does. The result is
    always a subgrant of ``parent`` (:func:`is_subgrant`).
    """
    if not isinstance(parent, AgentGrant):
        raise TypeError("parent must be an AgentGrant")
    child_id = to_uuid(child_agent_id, "child_agent_id")
    if child_id == parent.agent_id:
        raise GrantEscalationError(EscalationReason.SAME_AGENT)

    if capabilities is None:
        granted = delegable_capabilities(parent)
    else:
        granted = _capabilities(capabilities)
        if not granted <= parent.capabilities:
            raise GrantEscalationError(EscalationReason.CAPABILITY)
        if not all(CAPABILITIES[c].delegable for c in granted):
            raise GrantEscalationError(EscalationReason.NOT_DELEGABLE)

    if project_ids is None:
        reach = parent.project_ids
    else:
        reach = _projects(project_ids)
        if not projects_within(reach, parent.project_ids):
            raise GrantEscalationError(EscalationReason.PROJECT)

    return AgentGrant(child_id, granted, reach)
