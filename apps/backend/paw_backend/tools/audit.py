"""Audit records of tool decisions, through the existing ``AuditSink``.

The event carries ids and enums only: the acting user and agent, the action
(``tool.<registered tool name>``, ``tool.unknown`` for a name that is not
registered, ``tool.approval.*`` for the approval lifecycle), the task or the
approval as the resource, the allow / deny outcome and a fixed reason code. It
never carries arguments, targets, results or any text that came from a model.

``AuditEvent.decision`` has two values only (a column CHECK of PAW-025): a call
that needs an approval is recorded as ``deny`` with reason ``approval_required``
(it may not run *now*), and the approval that later lets it run as ``allow``
with reason ``approval_consumed``.
"""

import asyncio
import logging
import uuid
from datetime import datetime

from paw_backend.authz import AuditEvent, AuditSink

logger = logging.getLogger(__name__)

UNKNOWN_TOOL_ACTION = "tool.unknown"


def tool_action(tool: str | None) -> str:
    return UNKNOWN_TOOL_ACTION if tool is None else f"tool.{tool}"


def build_tool_event(
    *,
    action: str,
    allowed: bool,
    reason: str,
    correlation_id: uuid.UUID,
    occurred_at: datetime,
    resource_kind: str,
    resource_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    actor_id: uuid.UUID | None,
    actor_role: str | None = None,
    agent_id: uuid.UUID | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_id=uuid.uuid4(),
        correlation_id=correlation_id,
        occurred_at=occurred_at,
        actor_id=actor_id,
        actor_role=actor_role,
        agent_id=agent_id,
        action=action,
        resource_kind=resource_kind,
        resource_id=resource_id,
        project_id=project_id,
        decision="allow" if allowed else "deny",
        reason=reason,
    )


async def record_event(
    sink: AuditSink, event: AuditEvent, timeout_seconds: float
) -> bool:
    """Write ``event``; ``False`` (never an exception) when it was not stored.

    Only the exception's type name is logged: driver messages can name the host
    or the user.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            await sink.record(event)
    except Exception as error:
        logger.error(
            "Tool audit write failed (%s) for action %s",
            type(error).__name__,
            event.action,
        )
        return False
    return True
