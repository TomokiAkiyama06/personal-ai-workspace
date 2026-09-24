"""Audit events: schema, sink protocol and the PostgreSQL / in-memory sinks.

An event records *who* asked for *which action* on *what*, whether it was
allowed and why. It never carries secrets, prompts, message or file content:
every field is an identifier, an enum value or a timestamp, and identifiers
are restricted to a safe alphabet (``subjects.ID_PATTERN``).
"""

import uuid
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import insert

from paw_backend.authz.models import AuditEventRecord
from paw_backend.authz.policy import Decision
from paw_backend.authz.subjects import (
    ID_PATTERN,
    KIND_PATTERN,
    UNKNOWN_RESOURCE,
    AgentGrant,
    Principal,
    Resource,
    is_valid_id,
)
from paw_backend.db import Database

UNKNOWN_ACTION = "unknown"
_ID = {"pattern": f"^{ID_PATTERN}$"}


class AuditEvent(BaseModel):
    """One authorization decision. Immutable once built."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: uuid.UUID
    occurred_at: AwareDatetime
    actor_id: str | None = Field(default=None, **_ID)
    actor_role: str | None = Field(default=None, max_length=32)
    agent_id: str | None = Field(default=None, **_ID)
    # The capability value, or "unknown" when the requested one does not exist:
    # the text that was asked for is never stored.
    action: str = Field(max_length=64)
    resource_kind: str = Field(pattern=f"^{KIND_PATTERN}$")
    resource_id: str | None = Field(default=None, **_ID)
    project_id: str | None = Field(default=None, **_ID)
    repo_id: str | None = Field(default=None, **_ID)
    decision: Literal["allow", "deny"]
    reason: str = Field(max_length=64)
    request_id: str | None = Field(default=None, **_ID)


def build_event(
    decision: Decision,
    *,
    principal: Principal | None,
    resource: Resource | None,
    agent: AgentGrant | None = None,
    request_id: str | None = None,
    occurred_at: datetime | None = None,
) -> AuditEvent:
    """Turn a decision into an event; never raises for odd input."""
    target = resource if isinstance(resource, Resource) else UNKNOWN_RESOURCE
    who = principal if isinstance(principal, Principal) else None
    return AuditEvent(
        event_id=uuid.uuid4(),
        occurred_at=occurred_at or datetime.now(UTC),
        actor_id=who.user_id if who else None,
        actor_role=who.system_role.value if who else None,
        agent_id=agent.agent_id if isinstance(agent, AgentGrant) else None,
        action=decision.capability.value if decision.capability else UNKNOWN_ACTION,
        resource_kind=target.kind,
        resource_id=target.id,
        project_id=target.project_id,
        repo_id=target.repo_id,
        decision="allow" if decision.allowed else "deny",
        reason=decision.reason.value,
        # A request ID that does not look like one is dropped, not stored.
        request_id=request_id if is_valid_id(request_id) else None,
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
            "request_id": event.request_id,
        }
        async with self._database.session() as session:
            await session.execute(insert(AuditEventRecord).values(**values))
            await session.commit()
