"""The audit rows of an update (Issue #54, Decision 0079 7).

As the other server-local jobs (Decision 0031 4, Decision 0054 12): rows of
``audit_events`` with no new column and no migration, ``resource_kind =
deploy_update``, ``decision = allow``, no actor. ``reason`` holds release names,
revisions and counts only (never a path, a URL or a message).
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import insert

from paw_backend.authz.models import AuditEventRecord
from paw_backend.db import Database

RESOURCE_KIND = "deploy_update"
REASON_MAX_LENGTH = 256


class DeployAction(StrEnum):
    MAINTENANCE_STARTED = "deploy.maintenance.started"
    MAINTENANCE_ENDED = "deploy.maintenance.ended"
    RESTORE_POINT_CREATED = "deploy.restore_point.created"
    RESTORE_POINT_VERIFIED = "deploy.restore_point.verified"
    RESTORE_POINT_RESTORED = "deploy.restore_point.restored"


async def record_deploy_event(
    database: Database,
    action: DeployAction,
    reason: str,
    *,
    occurred_at: datetime | None = None,
) -> None:
    """Insert one row in a transaction of its own."""
    action = DeployAction(action)
    async with database.session() as session, session.begin():
        await session.execute(
            insert(AuditEventRecord).values(
                id=uuid.uuid4(),
                correlation_id=uuid.uuid4(),
                occurred_at=occurred_at or datetime.now(UTC),
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
