"""The run-level audit rows of the Memory Projection (PAW-045, Decision 0038 6).

Every run records its outcome as one row of the append-only ``audit_events``
trail, the same way the audit retention job does (Decision 0031 4): no new
column, no CHECK constraint, no migration.

* ``memory.projection.completed``: every step succeeded. ``reason`` is
  ``memories=N written=N removed=N redacted=N``, then `` truncated=N`` when a
  text too long to scan was cut (Decision 0038 5).
* ``memory.projection.failed``: a step failed. ``reason`` is
  ``<step>:<code>`` (``check_target:inside_git_work_tree``,
  ``read_database:ProjectionDatabaseError``, ``write_files:PermissionError``): a
  closed code or an exception's **type**, never its message or a path.
* ``resource_kind = memory_projection_run``, ``decision = allow`` (the outcome is
  in ``action``), no actor (an unattended run), ``reason`` cut to the column's 64
  characters.

The row is written in a transaction of its own, after the run. The application
role already holds INSERT and SELECT on ``audit_events`` (revisions 0025 / 0086),
which is all this needs. ``projection_status`` reads the last rows back for a
monitor (``memory-projection-check``) and, later, the Backup / Recovery screen
("Last successful projection generation", UI_DESIGN.md). "Last" is by
``recorded_at``, the database clock the ``0025`` trigger sets, not by the
caller's ``occurred_at``: a host clock corrected backwards must not hide a newer
failure behind an older success.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import func, insert, literal, select, true

from paw_backend.authz.models import AuditEventRecord
from paw_backend.db import Database
from paw_backend.memory.projection.records import ProjectionStatus

RESOURCE_KIND = "memory_projection_run"
REASON_MAX_LENGTH = 64


class ProjectionAction(StrEnum):
    COMPLETED = "memory.projection.completed"
    FAILED = "memory.projection.failed"


async def record_projection_outcome(
    database: Database,
    action: ProjectionAction,
    reason: str,
    *,
    occurred_at: datetime,
) -> None:
    """Insert one run-level row in a transaction of its own."""
    action = ProjectionAction(action)
    async with database.session() as session, session.begin():
        await session.execute(
            insert(AuditEventRecord).values(
                id=uuid.uuid4(),
                correlation_id=uuid.uuid4(),
                occurred_at=occurred_at,
                actor_id=None,
                actor_role=None,
                agent_id=None,
                action=action.value,
                resource_kind=RESOURCE_KIND,
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


async def projection_status(database: Database) -> ProjectionStatus:
    """The last recorded run and the last completed one (``None`` when none)."""
    table = AuditEventRecord.__table__
    completed = (
        select(table.c.occurred_at, table.c.recorded_at)
        .where(
            table.c.resource_kind == RESOURCE_KIND,
            table.c.action == ProjectionAction.COMPLETED.value,
        )
        .order_by(table.c.recorded_at.desc(), table.c.occurred_at.desc())
        .limit(1)
        .subquery("last_completed")
    )
    runs = (
        select(table.c.action, table.c.occurred_at, table.c.reason)
        .where(
            table.c.resource_kind == RESOURCE_KIND,
            table.c.action.in_([action.value for action in ProjectionAction]),
        )
        .order_by(table.c.recorded_at.desc(), table.c.occurred_at.desc())
        .limit(1)
        .subquery("last_run")
    )
    # One statement, so every answer comes from the same snapshot: a run that
    # commits meanwhile cannot make the last run and the last success disagree.
    # ``now()`` is the database clock the ``0025`` trigger stamps ``recorded_at``
    # with, so the age of the last success never depends on a host's clock.
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
    return ProjectionStatus(
        last_action=row.action,
        last_run_at=row.occurred_at,
        last_reason=row.reason,
        last_completed_at=row.last_completed_at,
        last_completed_recorded_at=row.last_completed_recorded_at,
        checked_at=row.checked_at,
    )


__all__ = [
    "REASON_MAX_LENGTH",
    "RESOURCE_KIND",
    "ProjectionAction",
    "projection_status",
    "record_projection_outcome",
]
