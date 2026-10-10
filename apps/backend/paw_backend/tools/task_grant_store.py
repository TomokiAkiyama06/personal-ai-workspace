"""PostgreSQL implementation of the task-scoped grants (Decision 0085).

Tables of migration ``0193`` (``tool_task_grants``, ``tool_task_grant_uses``)
and the approval they are made from (``tool_approvals``, migration ``0031``).

* ``grant_from_approval`` and ``use`` need several statements in one transaction
  (``Database.transact_abortable``: an abortable connection, ONE deadline for the
  whole call, the server told the same limit), like ``open_request`` and
  ``consume`` of ``PostgresApprovalStore``. Both read the task's row **locked**
  (``FOR SHARE``, ``task_state.lock_task_activity``) before the approval's or the
  grant's own row, in the order every approval path takes (the task first), so a
  grant is never made, and never used, across the task's end, a Retry or a
  Restart (Decision 0006, section 9).
* ``grant_from_approval`` approves the approval with the very statement
  ``decide`` uses (the change and its history row, never a strong approval) and
  inserts the grant in the same transaction: both or neither. The active grants
  of a (task, user) are counted under a transaction-scoped advisory lock, so the
  cap holds under concurrency.
* ``use`` reads the grant's row ``FOR SHARE`` and records the use; ``revoke``
  updates the row, so the two are ordered: no use is recorded after a
  revocation committed.
* ``get``, ``active_grants``, ``list_task``, ``revoke`` and ``revoke_task`` are
  single statements on ``Database.fetch_abortable`` (bounded; a revocation and
  its update are one atomic statement).

The expiry of the approval is judged on the application's clock after the
locks are held (``approval_store._Moment``), as ``consume`` does.
"""

import time
import uuid
from collections.abc import Callable
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from paw_backend.db import Database
from paw_backend.tasks import TaskRun
from paw_backend.tools.approval_store import (
    _COLUMNS as _APPROVAL_COLUMNS,
)
from paw_backend.tools.approval_store import (
    _DECIDE,
    _MARK_EXPIRED,
    _Moment,
    _record_of_values,
    _summary,
    _summary_json,
)
from paw_backend.tools.approval_types import RevokeOutcome
from paw_backend.tools.grant_pattern import GrantPattern
from paw_backend.tools.task_grants import (
    GRANT_TASK_REFUSAL,
    USE_TASK_REFUSAL,
    GrantOutcome,
    GrantResult,
    GrantUse,
    GrantUseOutcome,
    TaskGrantRecord,
    TaskGrantStatus,
    diagnose_grant,
    diagnose_use,
)
from paw_backend.tools.task_state import TaskActivity, lock_task_activity

_ACTIVE = TaskGrantStatus.ACTIVE.value
_REVOKED = TaskGrantStatus.REVOKED.value
# The columns of a grant, in the order of ``TaskGrantRecord``'s fields; the last
# one is the number of its uses.
_COLUMNS = (
    "g.id, g.approval_id, g.task_id, g.task_attempt, g.task_retry_count,"
    " g.project_id, g.agent_id, g.requester_user_id, g.tool, g.pattern, g.summary,"
    " g.status, g.created_at, g.revoked_at, g.revoked_by,"
    " (SELECT count(*) FROM tool_task_grant_uses u WHERE u.grant_id = g.id)"
)
_GET = f"SELECT {_COLUMNS} FROM tool_task_grants g WHERE g.id = %(id)s"
_ACTIVE_GRANTS = f"""
SELECT {_COLUMNS} FROM tool_task_grants g
 WHERE g.task_id = %(task_id)s
   AND g.task_attempt = %(attempt)s
   AND g.task_retry_count = %(retry_count)s
   AND g.agent_id = %(agent_id)s
   AND g.requester_user_id = %(user_id)s
   AND g.tool = %(tool)s
   AND g.status = '{_ACTIVE}'
 ORDER BY g.created_at, g.id
"""
_LIST_TASK = f"""
SELECT {_COLUMNS} FROM tool_task_grants g
 WHERE g.task_id = %(task_id)s
   AND g.requester_user_id = %(user_id)s
   AND g.status = '{_ACTIVE}'
 ORDER BY g.created_at, g.id
"""
_APPROVAL_TASK = (
    "SELECT task_id, task_attempt, task_retry_count FROM tool_approvals"
    " WHERE id = %(id)s"
)
_LOCK_APPROVAL = (
    f"SELECT {_APPROVAL_COLUMNS} FROM tool_approvals WHERE id = %(id)s"
    " FOR NO KEY UPDATE"
)
_GRANT_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%(key)s::text, 0))"
_COUNT_ACTIVE = f"""
SELECT count(*) FROM tool_task_grants
 WHERE task_id = %(task_id)s
   AND task_attempt = %(attempt)s
   AND task_retry_count = %(retry_count)s
   AND requester_user_id = %(user_id)s
   AND status = '{_ACTIVE}'
"""
_INSERT_GRANT = f"""
INSERT INTO tool_task_grants (
    id, approval_id, task_id, task_attempt, task_retry_count, project_id,
    agent_id, requester_user_id, tool, pattern, summary, status, created_at
) VALUES (
    %(id)s, %(approval_id)s, %(task_id)s, %(attempt)s, %(retry_count)s,
    %(project_id)s, %(agent_id)s, %(user_id)s, %(tool)s, %(pattern)s,
    %(summary)s, '{_ACTIVE}', %(now)s
)
"""
_LOCK_GRANT = f"SELECT {_COLUMNS} FROM tool_task_grants g WHERE g.id = %(id)s FOR SHARE"
_INSERT_USE = """
INSERT INTO tool_task_grant_uses (
    grant_id, call_hash, correlation_id, task_attempt, task_retry_count, used_at
) VALUES (
    %(grant_id)s, %(call_hash)s, %(correlation_id)s, %(attempt)s, %(retry_count)s,
    %(now)s
)
"""
_REVOKE = f"""
UPDATE tool_task_grants
   SET status = '{_REVOKED}', revoked_at = %(now)s, revoked_by = %(actor)s
 WHERE id = %(id)s AND status = '{_ACTIVE}'
RETURNING id
"""
_REVOKE_TASK = f"""
UPDATE tool_task_grants
   SET status = '{_REVOKED}', revoked_at = %(now)s, revoked_by = NULL
 WHERE task_id = %(task_id)s AND status = '{_ACTIVE}'
RETURNING id
"""


def _grant_of_values(values: tuple) -> TaskGrantRecord:
    (
        grant_id,
        approval_id,
        task_id,
        task_attempt,
        task_retry_count,
        project_id,
        agent_id,
        requester_user_id,
        tool,
        pattern,
        summary,
        status,
        created_at,
        revoked_at,
        revoked_by,
        uses,
    ) = values
    return TaskGrantRecord(
        grant_id=grant_id,
        approval_id=approval_id,
        task_id=task_id,
        task_run=TaskRun(task_attempt, task_retry_count),
        project_id=project_id,
        agent_id=agent_id,
        requester_user_id=requester_user_id,
        tool=tool,
        pattern=GrantPattern.from_json(pattern),
        summary=_summary(summary),
        status=TaskGrantStatus(status),
        created_at=created_at,
        revoked_at=revoked_at,
        revoked_by=revoked_by,
        uses=int(uses),
    )


class PostgresTaskGrantStore:
    """Grants in ``tool_task_grants`` / ``tool_task_grant_uses`` (migration 0193)."""

    def __init__(
        self,
        database: Database,
        *,
        timeout_seconds: float = 3.0,
        transaction_timeout_seconds: float = 3.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """``timeout_seconds`` bounds each single-statement call and
        ``transaction_timeout_seconds`` ``grant_from_approval`` and ``use`` (each
        call as a whole). ``monotonic`` measures the wait for locks."""
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        for name, value in (
            ("timeout_seconds", timeout_seconds),
            ("transaction_timeout_seconds", transaction_timeout_seconds),
        ):
            if not value > 0:
                raise ValueError(f"{name} must be positive")
        self._database = database
        self._timeout_seconds = timeout_seconds
        self._transaction_timeout_seconds = transaction_timeout_seconds
        self._monotonic = monotonic

    async def grant_from_approval(
        self,
        approval_id: uuid.UUID,
        *,
        approver_id: uuid.UUID,
        now: datetime,
        max_active: int,
    ) -> GrantResult:
        moment = _Moment(now, self._monotonic)
        return await self._database.transact_abortable(
            lambda connection: self._grant_once(
                connection, approval_id, approver_id, moment, max_active
            ),
            timeout_seconds=self._transaction_timeout_seconds,
        )

    async def _grant_once(
        self,
        connection: psycopg.AsyncConnection,
        approval_id: uuid.UUID,
        approver_id: uuid.UUID,
        moment: _Moment,
        max_active: int,
    ) -> GrantResult:
        # Which task and run the approval belongs to (never changes: the
        # trigger of migration 0031), to lock the task's row first.
        found = await (
            await connection.execute(_APPROVAL_TASK, {"id": approval_id})
        ).fetchone()
        if found is None:
            return GrantResult(GrantOutcome.NOT_FOUND)
        task_id, attempt, retry_count = found
        activity = await lock_task_activity(
            connection, task_id, TaskRun(attempt, retry_count)
        )
        locked = await (
            await connection.execute(_LOCK_APPROVAL, {"id": approval_id})
        ).fetchone()
        now = moment.read()
        record = None if locked is None else _record_of_values(locked)
        outcome = diagnose_grant(record, approver_id, now)
        if outcome is GrantOutcome.EXPIRED:
            await connection.execute(_MARK_EXPIRED, {"id": approval_id, "now": now})
        if outcome is not GrantOutcome.GRANTED or record is None:
            return GrantResult(outcome)
        if activity is not TaskActivity.ACTIVE:
            return GrantResult(GRANT_TASK_REFUSAL[activity])
        await connection.execute(
            _GRANT_LOCK,
            {"key": f"tool_task_grants:{record.task_id}:{record.requester_user_id}"},
        )
        counted = await (
            await connection.execute(
                _COUNT_ACTIVE,
                # Only the grants of this run: those of an earlier one (a Retry
                # or Restart) can no longer be used.
                {
                    "task_id": record.task_id,
                    "attempt": record.task_run.attempt,
                    "retry_count": record.task_run.retry_count,
                    "user_id": record.requester_user_id,
                },
            )
        ).fetchone()
        if counted[0] >= max_active:
            return GrantResult(GrantOutcome.LIMIT_REACHED)
        decided = await (
            await connection.execute(
                _DECIDE[(True, True)],
                {
                    "id": approval_id,
                    "approver": approver_id,
                    "now": now,
                    "verified": False,
                },
            )
        ).fetchone()
        if decided is None:  # pragma: no cover - the row is locked and was pending
            raise RuntimeError("the locked approval could not be approved")
        grant_id = uuid.uuid4()
        pattern = record.grant_pattern
        assert pattern is not None  # diagnose_grant checked it
        await connection.execute(
            _INSERT_GRANT,
            {
                "id": grant_id,
                "approval_id": approval_id,
                "task_id": record.task_id,
                "attempt": record.task_run.attempt,
                "retry_count": record.task_run.retry_count,
                "project_id": record.project_id,
                "agent_id": record.agent_id,
                "user_id": record.requester_user_id,
                "tool": record.tool,
                "pattern": Jsonb(pattern.to_json()),
                "summary": Jsonb(_summary_json(record.summary)),
                "now": now,
            },
        )
        return GrantResult(
            GrantOutcome.GRANTED,
            TaskGrantRecord(
                grant_id=grant_id,
                approval_id=approval_id,
                task_id=record.task_id,
                task_run=record.task_run,
                project_id=record.project_id,
                agent_id=record.agent_id,
                requester_user_id=record.requester_user_id,
                tool=record.tool,
                pattern=pattern,
                summary=record.summary,
                status=TaskGrantStatus.ACTIVE,
                created_at=now,
            ),
        )

    async def active_grants(
        self,
        task_id: uuid.UUID,
        task_run: TaskRun,
        agent_id: uuid.UUID,
        requester_user_id: uuid.UUID,
        tool: str,
    ) -> list[TaskGrantRecord]:
        rows = await self._database.fetch_abortable(
            _ACTIVE_GRANTS,
            {
                "task_id": task_id,
                "attempt": task_run.attempt,
                "retry_count": task_run.retry_count,
                "agent_id": agent_id,
                "user_id": requester_user_id,
                "tool": tool,
            },
            timeout_seconds=self._timeout_seconds,
        )
        return [_grant_of_values(row) for row in rows]

    async def use(
        self, grant_id: uuid.UUID, use: GrantUse, *, now: datetime
    ) -> GrantUseOutcome:
        return await self._database.transact_abortable(
            lambda connection: self._use_once(connection, grant_id, use, now),
            timeout_seconds=self._transaction_timeout_seconds,
        )

    async def _use_once(
        self,
        connection: psycopg.AsyncConnection,
        grant_id: uuid.UUID,
        use: GrantUse,
        now: datetime,
    ) -> GrantUseOutcome:
        # The task's row first (locked, as for every approval path), then the
        # grant's: a revocation in flight is waited for and then seen.
        activity = await lock_task_activity(connection, use.task_id, use.task_run)
        locked = await (
            await connection.execute(_LOCK_GRANT, {"id": grant_id})
        ).fetchone()
        record = None if locked is None else _grant_of_values(locked)
        outcome = diagnose_use(record, use)
        if outcome in (GrantUseOutcome.USED, GrantUseOutcome.SUPERSEDED) and (
            activity is not TaskActivity.ACTIVE
        ):
            # The task is why it is not used (as for an approval's consumption).
            return USE_TASK_REFUSAL[activity]
        if outcome is not GrantUseOutcome.USED:
            return outcome
        await connection.execute(
            _INSERT_USE,
            {
                "grant_id": grant_id,
                "call_hash": use.call_hash,
                "correlation_id": use.correlation_id,
                "attempt": use.task_run.attempt,
                "retry_count": use.task_run.retry_count,
                "now": now,
            },
        )
        return GrantUseOutcome.USED

    async def get(self, grant_id: uuid.UUID) -> TaskGrantRecord | None:
        rows = await self._database.fetch_abortable(
            _GET, {"id": grant_id}, timeout_seconds=self._timeout_seconds
        )
        return None if not rows else _grant_of_values(rows[0])

    async def list_task(
        self, task_id: uuid.UUID, requester_user_id: uuid.UUID
    ) -> list[TaskGrantRecord]:
        rows = await self._database.fetch_abortable(
            _LIST_TASK,
            {"task_id": task_id, "user_id": requester_user_id},
            timeout_seconds=self._timeout_seconds,
        )
        return [_grant_of_values(row) for row in rows]

    async def revoke(
        self, grant_id: uuid.UUID, *, actor_id: uuid.UUID | None, now: datetime
    ) -> RevokeOutcome:
        rows = await self._database.fetch_abortable(
            _REVOKE,
            {"id": grant_id, "actor": actor_id, "now": now},
            timeout_seconds=self._timeout_seconds,
        )
        if rows:
            return RevokeOutcome.REVOKED
        record = await self.get(grant_id)
        return RevokeOutcome.NOT_FOUND if record is None else RevokeOutcome.NOT_OPEN

    async def revoke_task(
        self, task_id: uuid.UUID, *, now: datetime
    ) -> list[uuid.UUID]:
        rows = await self._database.fetch_abortable(
            _REVOKE_TASK,
            {"task_id": task_id, "now": now},
            timeout_seconds=self._timeout_seconds,
        )
        return [row[0] for row in rows]
