"""Audit events of the retention / partitioning maintenance job (Issue #86).

Every maintenance action (creating a partition, archiving one, purging one)
becomes one row of the existing append-only ``audit_events`` trail (PAW-025):
this module adds no column and no CHECK constraint (unlike ``research/privacy/
audit.py``'s ``details``), because a partition's name, bound and outcome fit
the existing columns — ``reason`` (64 characters) holds the partition name,
``resource_kind`` is the fixed value ``"audit_partition"`` (``"audit_retention_run"``
for the one run-level row of Issue #117), and ``action`` is one
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
    # One row per scheduled / manual run (Issue #117, Decision 0031): the run
    # finished every step and the partitions cover the checked span
    # (``reason`` = ``created=N archived=N purged=N``), or it did not
    # (``reason`` = ``<step>:<error type>``, never the error's message).
    MAINTENANCE_COMPLETED = "audit.retention.maintenance_completed"
    MAINTENANCE_FAILED = "audit.retention.maintenance_failed"


# Fixed for every row this module writes: never a caller-supplied resource kind.
RESOURCE_KIND = "audit_partition"
# The resource kind of the run-level rows (``MAINTENANCE_*``): a run is not one
# partition, so it does not reuse ``RESOURCE_KIND``.
RUN_RESOURCE_KIND = "audit_retention_run"
# ``audit_events.reason`` is 64 characters; a run-level reason is cut to fit.
REASON_MAX_LENGTH = 64


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
    await _insert(
        session,
        action,
        RESOURCE_KIND,
        window.name,
        occurred_at=occurred_at,
        actor=actor,
    )


async def record_maintenance_event(
    session: AsyncSession,
    action: RetentionAction,
    reason: str,
    *,
    occurred_at: datetime,
    actor: RetentionActor | None = None,
) -> None:
    """Insert one run-level row (``MAINTENANCE_COMPLETED`` / ``_FAILED``).

    ``reason`` is cut to the column's 64 characters. ``decision`` stays
    ``allow`` (the run was not an authorization decision that was refused; the
    outcome is in ``action``), the same fixed value as the partition rows.
    """
    if action not in (
        RetentionAction.MAINTENANCE_COMPLETED,
        RetentionAction.MAINTENANCE_FAILED,
    ):
        raise ValueError(f"not a run-level retention action: {action}")
    await _insert(
        session,
        action,
        RUN_RESOURCE_KIND,
        reason[:REASON_MAX_LENGTH],
        occurred_at=occurred_at,
        actor=actor,
    )


async def _insert(
    session: AsyncSession,
    action: RetentionAction,
    resource_kind: str,
    reason: str,
    *,
    occurred_at: datetime,
    actor: RetentionActor | None,
) -> None:
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
            resource_kind=resource_kind,
            resource_id=None,
            project_id=None,
            repo_id=None,
            repo_acl=None,
            decision="allow",
            reason=reason,
            old_role=None,
            new_role=None,
            client_request_id=None,
        )
    )
