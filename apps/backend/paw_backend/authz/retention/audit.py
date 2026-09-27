"""Audit events of the retention / partitioning maintenance job (Issue #86).

Every maintenance action (creating a partition, archiving one, purging one)
becomes one row of the existing append-only ``audit_events`` trail (PAW-025):
this module adds no column and no CHECK constraint (unlike ``research/privacy/
audit.py``'s ``details``), because a partition's name, bound and outcome fit
the existing columns — ``reason`` (64 characters) holds the partition name,
``resource_kind`` is the fixed value ``"audit_partition"``, and ``action`` is one
of ``RetentionAction`` below (a new small namespace, the same pattern
``paw_backend.auth.audit.AuthAction`` already uses for events that are not an
authorization decision).

``actor_id`` / ``actor_role`` are ``None`` unless the caller of
``AuditRetentionService`` names one (``RetentionActor``): an unattended
scheduled run has no user, an admin who triggers maintenance from a future
Capability would pass their own identity. Either way the row is written in the
**same transaction** as the DDL that performed the action (see ``service.py``):
a partition move and its audit row commit together or not at all, unlike the
denial path of ``PostgresAuditSink`` (its own short transaction, because a
denial must be recorded even when the caller's transaction rolls back) — here
there is no caller transaction to roll back other than the maintenance step
itself, so tying the two together is strictly stronger.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz.models import AuditEventRecord
from paw_backend.authz.retention.records import PartitionWindow


class RetentionAction(StrEnum):
    """``AuditEvent.action`` values written by the retention maintenance job."""

    PARTITION_CREATED = "audit.retention.partition_created"
    PARTITION_ARCHIVED = "audit.retention.partition_archived"
    PARTITION_PURGED = "audit.retention.partition_purged"


# Fixed for every row this module writes: never a caller-supplied resource kind.
RESOURCE_KIND = "audit_partition"


@dataclass(frozen=True, slots=True)
class RetentionActor:
    """Who ran the maintenance, when it was a person and not an unattended job.

    Both ``None`` (the default ``AuditRetentionService`` uses) records the row
    with no actor: an unattended scheduled run, not any one person's action.
    """

    user_id: uuid.UUID | None = None
    system_role: str | None = None


async def record_partition_event(
    session: AsyncSession,
    action: RetentionAction,
    window: PartitionWindow,
    *,
    occurred_at: datetime,
    actor: RetentionActor | None = None,
) -> None:
    """Insert one retention audit row in ``session``'s transaction.

    Committed with the DDL the caller runs in the same transaction, or not at
    all (see the module docstring). ``window.name`` becomes ``reason``: every
    name this module ever writes is at most 22 characters (``audit_events_p_
    legacy`` or ``audit_events_pYYYY_MM``), well inside the column's 64.
    """
    actor = actor or RetentionActor()
    await session.execute(
        insert(AuditEventRecord).values(
            id=uuid.uuid4(),
            correlation_id=uuid.uuid4(),
            occurred_at=occurred_at,
            actor_id=actor.user_id,
            actor_role=actor.system_role,
            agent_id=None,
            action=action.value,
            resource_kind=RESOURCE_KIND,
            resource_id=None,
            project_id=None,
            repo_id=None,
            repo_acl=None,
            decision="allow",
            reason=window.name,
            old_role=None,
            new_role=None,
            client_request_id=None,
        )
    )
