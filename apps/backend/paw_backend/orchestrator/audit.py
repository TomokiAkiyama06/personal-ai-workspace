"""The audit row of a node sent to a cloud agent (issue #133, Decision 0037's 14).

When ``HybridRuntime`` runs a node on a cloud agent (Codex / Claude), the node's
content (its goal, input and upstream results) leaves the backend. Before it
does, ``DagStore.record_placement`` writes the attempt's placement and appends
one row to the append-only ``audit_events`` table (PAW-025) **in the same
transaction** (the way the Shared Memory service records a completion,
``memory/shared/audit.py``): the send is on record if and only if the placement
is, and a runtime that cannot record it does not send (fail closed, as
Decision 0010 / 0023 do for research queries).

The row holds ids, enum values and fixed words only (``AuditEvent`` has no field
for text, and ``details`` stays NULL: Decision 0023's registry is not touched):

* ``action`` ``orchestrator.cloud_send``, ``reason`` ``cloud_placement``,
  ``decision`` ``allow`` (the only other value of the column is ``deny``);
* ``resource_kind`` ``task`` and ``resource_id`` the task, ``project_id`` its
  project;
* ``actor_id`` the task's delegator (the user the agents act for),
  ``actor_role`` ``system`` (the backend placed the node, no user decided it),
  ``agent_id`` the node's agent id (``scope.agent_id_of``: the same id its tool
  calls carry);
* ``id`` and ``correlation_id`` one fresh UUID, which the attempt row keeps as
  ``placement_audit_id``: the row and the attempt (with the cloud agent, the
  model and the fingerprint and size of the content) are found from each other.

What is not recorded: what the cloud agent sends out by itself while it runs
(its own tool calls go through the Tool Broker and its audit, like a local
agent's), and the content itself (only its SHA-256 and size, on the attempt).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz import AuditEvent
from paw_backend.authz.models import AuditEventRecord
from paw_backend.authz.roles import SystemRole
from paw_backend.orchestrator.errors import InvalidOrchestratorArgumentError

CLOUD_SEND_ACTION = "orchestrator.cloud_send"
CLOUD_SEND_REASON = "cloud_placement"
CLOUD_SEND_RESOURCE_KIND = "task"

_TABLE = AuditEventRecord.__table__
_FINGERPRINT_PREFIX = "sha256:"
_HEX = frozenset("0123456789abcdef")


def check_fingerprint(value: object) -> str:
    """``sha256:`` and 64 lower case hexadecimal digits."""
    if (
        type(value) is not str
        or len(value) != len(_FINGERPRINT_PREFIX) + 64
        or not value.startswith(_FINGERPRINT_PREFIX)
        or not set(value[len(_FINGERPRINT_PREFIX) :]) <= _HEX
    ):
        raise InvalidOrchestratorArgumentError("content_fingerprint")
    return value


@dataclass(frozen=True, slots=True)
class CloudSend:
    """What the orchestrator knows of a send to the cloud, for its audit row."""

    # The SHA-256 (``sha256:`` + 64 hex) and the UTF-8 size of the content.
    content_fingerprint: str
    content_bytes: int
    # The node's agent id (``scope.agent_id_of``).
    agent_id: uuid.UUID
    # The orchestrator's clock when it decided (``recorded_at`` is the database's).
    occurred_at: datetime

    def __post_init__(self) -> None:
        check_fingerprint(self.content_fingerprint)
        if (
            type(self.content_bytes) is not int
            or not 0 <= self.content_bytes <= 2**31 - 1
        ):
            raise InvalidOrchestratorArgumentError("content_bytes")
        if type(self.agent_id) is not uuid.UUID:
            raise InvalidOrchestratorArgumentError("agent_id")
        if type(self.occurred_at) is not datetime or self.occurred_at.tzinfo is None:
            raise InvalidOrchestratorArgumentError("occurred_at")


def cloud_send_event(
    send: CloudSend,
    *,
    event_id: uuid.UUID,
    task_id: uuid.UUID,
    project_id: uuid.UUID | None,
    delegator_id: uuid.UUID | None,
) -> AuditEvent:
    """The ``audit_events`` row of ``send``; no I/O."""
    return AuditEvent(
        event_id=event_id,
        correlation_id=event_id,
        occurred_at=send.occurred_at,
        actor_id=delegator_id,
        actor_role=SystemRole.SYSTEM.value,
        agent_id=send.agent_id,
        action=CLOUD_SEND_ACTION,
        resource_kind=CLOUD_SEND_RESOURCE_KIND,
        resource_id=task_id,
        project_id=project_id,
        decision="allow",
        reason=CLOUD_SEND_REASON,
    )


async def record_cloud_send(session: AsyncSession, event: AuditEvent) -> None:
    """Append ``event`` to ``audit_events`` in the transaction of ``session``."""
    values = event.model_dump()
    values["id"] = values.pop("event_id")
    await session.execute(insert(_TABLE).values(**values))
