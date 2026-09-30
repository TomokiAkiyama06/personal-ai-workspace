"""The audit rows of the Recovery backup and restore (PAW-047, Decision 0054 5, 12).

As the Memory Projection (Decision 0038 6) and the audit retention job (Decision
0031 4): one row of ``audit_events`` per run, no new column, no migration.

* Backup (``resource_kind = recovery_backup_run``): ``recovery.backup.completed``
  (``reason``: ``files=N written=N removed=N commit=0|1 push=0|1 redacted=N``) or
  ``recovery.backup.failed`` (``<step>:<code>``), in a transaction of its own.
* Restore (``resource_kind = recovery_restore``): ``recovery.restore.planned`` (a
  dry run), ``recovery.restore.applied`` (written in the **same** transaction as
  the restored rows: both or neither), ``recovery.restore.refused`` (a check
  failed; nothing was written) or ``recovery.restore.failed`` (the write failed
  and was rolled back). The counts of ``planned`` / ``applied`` add the
  held-back repository memories when a repository was skipped (Decision 0061).

``reason`` holds counts or a closed code, never a path, a URL, a message or a
row's text; ``decision = allow``, no actor (a server-local command). "Last" is by
``recorded_at`` (the database clock), as in ``projection_status``.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import func, insert, literal, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz.models import AuditEventRecord
from paw_backend.db import Database

BACKUP_RESOURCE_KIND = "recovery_backup_run"
RESTORE_RESOURCE_KIND = "recovery_restore"
# Room for the restore's counts with the held-back ones (Decision 0061), each
# number up to 10 digits; the column itself is ``text``.
REASON_MAX_LENGTH = 256


class RecoveryAction(StrEnum):
    BACKUP_COMPLETED = "recovery.backup.completed"
    BACKUP_FAILED = "recovery.backup.failed"
    RESTORE_PLANNED = "recovery.restore.planned"
    RESTORE_APPLIED = "recovery.restore.applied"
    RESTORE_REFUSED = "recovery.restore.refused"
    RESTORE_FAILED = "recovery.restore.failed"


_BACKUP_ACTIONS = (RecoveryAction.BACKUP_COMPLETED, RecoveryAction.BACKUP_FAILED)


def _resource_kind(action: RecoveryAction) -> str:
    return BACKUP_RESOURCE_KIND if action in _BACKUP_ACTIONS else RESTORE_RESOURCE_KIND


async def insert_recovery_row(
    session: AsyncSession,
    action: RecoveryAction,
    reason: str,
    *,
    occurred_at: datetime,
) -> None:
    """Insert one row in the caller's transaction."""
    action = RecoveryAction(action)
    await session.execute(
        insert(AuditEventRecord).values(
            id=uuid.uuid4(),
            correlation_id=uuid.uuid4(),
            occurred_at=occurred_at,
            actor_id=None,
            actor_role=None,
            agent_id=None,
            action=action.value,
            resource_kind=_resource_kind(action),
            resource_id=None,
            project_id=None,
            repo_id=None,
            repo_acl=None,
            decision="allow",
            reason=reason[:REASON_MAX_LENGTH],
            old_role=None,
            new_role=None,
            client_request_id=None,
        )
    )


async def record_recovery_outcome(
    database: Database,
    action: RecoveryAction,
    reason: str,
    *,
    occurred_at: datetime,
) -> None:
    """Insert one row in a transaction of its own."""
    async with database.session() as session, session.begin():
        await insert_recovery_row(session, action, reason, occurred_at=occurred_at)


@dataclass(frozen=True, slots=True)
class BackupStatus:
    """The last backup run and the last completed one (``None`` when none).

    ``..._at`` are the runs' own (host) times, for display; how long ago the
    last success was is measured on the database clock only:
    ``last_completed_recorded_at`` against ``checked_at`` (the database's
    ``now()``), as ``projection_status`` does."""

    last_action: str | None
    last_run_at: datetime | None
    last_reason: str | None
    last_completed_at: datetime | None
    last_completed_recorded_at: datetime | None
    checked_at: datetime


async def backup_status(database: Database) -> BackupStatus:
    table = AuditEventRecord.__table__
    completed = (
        select(table.c.occurred_at, table.c.recorded_at)
        .where(
            table.c.resource_kind == BACKUP_RESOURCE_KIND,
            table.c.action == RecoveryAction.BACKUP_COMPLETED.value,
        )
        .order_by(table.c.recorded_at.desc(), table.c.occurred_at.desc())
        .limit(1)
        .subquery("last_completed")
    )
    runs = (
        select(table.c.action, table.c.occurred_at, table.c.reason)
        .where(
            table.c.resource_kind == BACKUP_RESOURCE_KIND,
            table.c.action.in_([action.value for action in _BACKUP_ACTIONS]),
        )
        .order_by(table.c.recorded_at.desc(), table.c.occurred_at.desc())
        .limit(1)
        .subquery("last_run")
    )
    # One statement: one snapshot, and ``now()`` is the clock ``recorded_at``
    # is stamped with.
    status = select(
        func.now().label("checked_at"),
        runs.c.action,
        runs.c.occurred_at,
        runs.c.reason,
        completed.c.occurred_at.label("last_completed_at"),
        completed.c.recorded_at.label("last_completed_recorded_at"),
    ).select_from(
        select(literal(1).label("one"))
        .subquery("one")
        .outerjoin(runs, true())
        .outerjoin(completed, true())
    )
    async with database.session() as session:
        row = (await session.execute(status)).one()
    return BackupStatus(
        last_action=row.action,
        last_run_at=row.occurred_at,
        last_reason=row.reason,
        last_completed_at=row.last_completed_at,
        last_completed_recorded_at=row.last_completed_recorded_at,
        checked_at=row.checked_at,
    )


__all__ = [
    "BACKUP_RESOURCE_KIND",
    "REASON_MAX_LENGTH",
    "RESTORE_RESOURCE_KIND",
    "BackupStatus",
    "RecoveryAction",
    "backup_status",
    "insert_recovery_row",
    "record_recovery_outcome",
]
