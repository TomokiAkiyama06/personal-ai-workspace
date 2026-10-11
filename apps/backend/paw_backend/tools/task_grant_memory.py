"""Task-scoped grants kept in memory: a test double with the rules of PostgreSQL.

Not for production (nothing survives a restart). It approves the approval in
the :class:`~.approval_memory.InMemoryApprovalStore` it is given, under its own
lock (the double has no transaction spanning both stores: a test that races a
grant with a decision of the same approval needs PostgreSQL).
``tests/test_task_grants_postgres.py`` runs the PostgreSQL store.
"""

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime

from paw_backend.tasks import TaskRun
from paw_backend.tools.approval_memory import InMemoryApprovalStore
from paw_backend.tools.approval_types import DecideOutcome, RevokeOutcome
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
from paw_backend.tools.task_state import TaskActivity, TaskActivityProvider


class InMemoryTaskGrantStore:
    """``task_activity`` answers what ``grant_from_approval`` and ``use`` ask
    about the task and run (without one, every task is taken to be active: the
    broker's own check before is then the only one)."""

    def __init__(
        self,
        approvals: InMemoryApprovalStore,
        *,
        task_activity: TaskActivityProvider | None = None,
    ) -> None:
        if not isinstance(approvals, InMemoryApprovalStore):
            raise TypeError("approvals must be an InMemoryApprovalStore")
        self._approvals = approvals
        self._task_activity = task_activity
        self._grants: dict[uuid.UUID, TaskGrantRecord] = {}
        # (grant id, call hash, correlation id, run, time) of every use.
        self.uses: list[tuple[uuid.UUID, str, uuid.UUID, TaskRun, datetime]] = []
        self._lock = asyncio.Lock()

    async def _activity(self, task_id: uuid.UUID, run: TaskRun) -> TaskActivity:
        if self._task_activity is None:
            return TaskActivity.ACTIVE
        return await self._task_activity.check(task_id, run)

    def _with_uses(self, record: TaskGrantRecord) -> TaskGrantRecord:
        return replace(
            record, uses=sum(1 for use in self.uses if use[0] == record.grant_id)
        )

    async def grant_from_approval(
        self,
        approval_id: uuid.UUID,
        *,
        approver_id: uuid.UUID,
        now: datetime,
        max_active: int,
    ) -> GrantResult:
        async with self._lock:
            record = await self._approvals.get(approval_id)
            outcome = diagnose_grant(record, approver_id, now)
            if outcome is not GrantOutcome.GRANTED or record is None:
                if outcome is GrantOutcome.EXPIRED:
                    # The approval store marks it expired, as PostgreSQL does.
                    await self._approvals.decide(
                        approval_id, approver_id=approver_id, approve=True, now=now
                    )
                return GrantResult(outcome)
            activity = await self._activity(record.task_id, record.task_run)
            if activity is not TaskActivity.ACTIVE:
                return GrantResult(GRANT_TASK_REFUSAL[activity])
            active = sum(
                1
                for grant in self._grants.values()
                if grant.task_id == record.task_id
                and grant.task_run == record.task_run
                and grant.requester_user_id == record.requester_user_id
                and grant.status is TaskGrantStatus.ACTIVE
            )
            if active >= max_active:
                return GrantResult(GrantOutcome.LIMIT_REACHED)
            decided = await self._approvals.decide(
                approval_id, approver_id=approver_id, approve=True, now=now
            )
            if decided.outcome is not DecideOutcome.DECIDED:
                return GrantResult(GrantOutcome.NOT_PENDING)
            assert record.grant_pattern is not None  # diagnose_grant checked it
            grant = TaskGrantRecord(
                grant_id=uuid.uuid4(),
                approval_id=approval_id,
                task_id=record.task_id,
                task_run=record.task_run,
                project_id=record.project_id,
                agent_id=record.agent_id,
                requester_user_id=record.requester_user_id,
                tool=record.tool,
                pattern=record.grant_pattern,
                summary=record.summary,
                status=TaskGrantStatus.ACTIVE,
                created_at=now,
            )
            self._grants[grant.grant_id] = grant
            return GrantResult(GrantOutcome.GRANTED, grant)

    async def active_grants(
        self,
        task_id: uuid.UUID,
        task_run: TaskRun,
        agent_id: uuid.UUID,
        requester_user_id: uuid.UUID,
        tool: str,
    ) -> list[TaskGrantRecord]:
        return [
            self._with_uses(grant)
            for grant in self._grants.values()
            if (
                grant.task_id,
                grant.task_run,
                grant.agent_id,
                grant.requester_user_id,
                grant.tool,
                grant.status,
            )
            == (
                task_id,
                task_run,
                agent_id,
                requester_user_id,
                tool,
                TaskGrantStatus.ACTIVE,
            )
        ]

    async def use(
        self, grant_id: uuid.UUID, use: GrantUse, *, now: datetime
    ) -> GrantUseOutcome:
        async with self._lock:
            record = self._grants.get(grant_id)
            outcome = diagnose_use(record, use)
            if outcome in (GrantUseOutcome.USED, GrantUseOutcome.SUPERSEDED):
                activity = await self._activity(use.task_id, use.task_run)
                if activity is not TaskActivity.ACTIVE:
                    return USE_TASK_REFUSAL[activity]
            if outcome is not GrantUseOutcome.USED:
                return outcome
            self.uses.append(
                (grant_id, use.call_hash, use.correlation_id, use.task_run, now)
            )
            return GrantUseOutcome.USED

    async def get(self, grant_id: uuid.UUID) -> TaskGrantRecord | None:
        record = self._grants.get(grant_id)
        return None if record is None else self._with_uses(record)

    async def list_task(
        self, task_id: uuid.UUID, requester_user_id: uuid.UUID
    ) -> list[TaskGrantRecord]:
        return [
            self._with_uses(grant)
            for grant in self._grants.values()
            if grant.task_id == task_id
            and grant.requester_user_id == requester_user_id
            and grant.status is TaskGrantStatus.ACTIVE
        ]

    async def revoke(
        self, grant_id: uuid.UUID, *, actor_id: uuid.UUID | None, now: datetime
    ) -> RevokeOutcome:
        async with self._lock:
            record = self._grants.get(grant_id)
            if record is None:
                return RevokeOutcome.NOT_FOUND
            if record.status is not TaskGrantStatus.ACTIVE:
                return RevokeOutcome.NOT_OPEN
            self._grants[grant_id] = replace(
                record,
                status=TaskGrantStatus.REVOKED,
                revoked_at=now,
                revoked_by=actor_id,
            )
            return RevokeOutcome.REVOKED

    async def revoke_task(
        self, task_id: uuid.UUID, *, now: datetime
    ) -> list[uuid.UUID]:
        async with self._lock:
            revoked = []
            for grant in list(self._grants.values()):
                if grant.task_id == task_id and grant.status is TaskGrantStatus.ACTIVE:
                    self._grants[grant.grant_id] = replace(
                        grant,
                        status=TaskGrantStatus.REVOKED,
                        revoked_at=now,
                        revoked_by=None,
                    )
                    revoked.append(grant.grant_id)
            return revoked
