"""Audit events: schema, sink protocol and the PostgreSQL / in-memory sinks.

An event records *who* asked for *which action* on *what*, whether it was
allowed and why. It never carries secrets, prompts, message or file content:
every field is a UUID, an enum value or a timestamp (plus the validated,
length-bounded ``client_request_id``). The ids are opaque: which person a user
id belongs to is only known to the user store, so deleting that mapping
anonymises the trail without rewriting it.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import insert

from paw_backend.authz.models import AuditEventRecord
from paw_backend.authz.policy import Decision
from paw_backend.authz.roles import SystemRole
from paw_backend.authz.subjects import (
    CLIENT_REQUEST_ID_PATTERN,
    KIND_PATTERN,
    UNKNOWN_RESOURCE,
    AgentGrant,
    Principal,
    Resource,
    is_valid_client_request_id,
)
from paw_backend.db import Database

UNKNOWN_ACTION = "unknown"


class AuditEvent(BaseModel):
    """One authorization decision. Immutable once built."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: uuid.UUID
    # Server-generated; the decisions of one request share it.
    correlation_id: uuid.UUID
    occurred_at: AwareDatetime
    actor_id: uuid.UUID | None = None
    actor_role: str | None = Field(default=None, max_length=32)
    agent_id: uuid.UUID | None = None
    # The capability value, or "unknown" when the requested one does not exist:
    # the text that was asked for is never stored.
    action: str = Field(max_length=64)
    resource_kind: str = Field(pattern=f"^{KIND_PATTERN}$")
    resource_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    repo_id: uuid.UUID | None = None
    decision: Literal["allow", "deny"]
    reason: str = Field(max_length=64)
    # For a change of a user's system role: the role before and after (enum
    # values; ``None`` = removed). Empty for every other action.
    old_role: str | None = Field(default=None, max_length=32)
    new_role: str | None = Field(default=None, max_length=32)
    # What the client sent as X-Request-ID: validated and length-bounded, but
    # still client-controlled. Never use it to identify a request.
    client_request_id: str | None = Field(
        default=None, pattern=f"^{CLIENT_REQUEST_ID_PATTERN}$"
    )


def build_event(
    decision: Decision,
    *,
    principal: Principal | None,
    resource: Resource | None,
    actor_id: uuid.UUID | None = None,
    agent: AgentGrant | None = None,
    correlation_id: uuid.UUID | None = None,
    client_request_id: str | None = None,
    occurred_at: datetime | None = None,
    old_role: SystemRole | None = None,
    new_role: SystemRole | None = None,
) -> AuditEvent:
    """Turn a decision into an event; never raises for odd input.

    ``actor_id`` names the acting user when there is no principal (an agent
    whose delegating user could no longer be resolved).
    """
    target = resource if isinstance(resource, Resource) else UNKNOWN_RESOURCE
    who = principal if isinstance(principal, Principal) else None
    return AuditEvent(
        event_id=uuid.uuid4(),
        correlation_id=correlation_id or uuid.uuid4(),
        occurred_at=occurred_at or datetime.now(UTC),
        actor_id=who.user_id if who else actor_id,
        actor_role=who.system_role.value if who else None,
        agent_id=agent.agent_id if isinstance(agent, AgentGrant) else None,
        action=decision.capability.value if decision.capability else UNKNOWN_ACTION,
        resource_kind=target.kind,
        resource_id=target.id,
        project_id=target.project_id,
        repo_id=target.repo_id,
        decision="allow" if decision.allowed else "deny",
        reason=decision.reason.value,
        old_role=old_role.value if isinstance(old_role, SystemRole) else None,
        new_role=new_role.value if isinstance(new_role, SystemRole) else None,
        # A value that does not look like a request ID is dropped, not stored.
        client_request_id=(
            client_request_id if is_valid_client_request_id(client_request_id) else None
        ),
    )


class AuditSink(Protocol):
    """Where audit events go. ``record`` raises when the event was not stored."""

    async def record(self, event: AuditEvent) -> None: ...


class InMemoryAuditSink:
    """Keeps events in a list. For unit tests; nothing is persisted."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class PostgresAuditSink:
    """Appends events to ``audit_events`` in their own short transaction.

    The write is independent of the request's session, so a denied request
    that rolls its own transaction back still leaves its audit row.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    async def record(self, event: AuditEvent) -> None:
        values = {
            "id": event.event_id,
            "correlation_id": event.correlation_id,
            "occurred_at": event.occurred_at,
            "actor_id": event.actor_id,
            "actor_role": event.actor_role,
            "agent_id": event.agent_id,
            "action": event.action,
            "resource_kind": event.resource_kind,
            "resource_id": event.resource_id,
            "project_id": event.project_id,
            "repo_id": event.repo_id,
            "decision": event.decision,
            "reason": event.reason,
            "old_role": event.old_role,
            "new_role": event.new_role,
            "client_request_id": event.client_request_id,
        }
        async with self._database.session() as session:
            await session.execute(insert(AuditEventRecord).values(**values))
            await session.commit()
