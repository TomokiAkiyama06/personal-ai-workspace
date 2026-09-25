"""Approvals kept in memory: a test double with the same rules as PostgreSQL.

Not for production (nothing survives a restart and it cannot be shared between
processes); it holds at most ``max_records`` approvals. ``tests/
tools_store_contract.py`` runs the same tests against this class and against
:class:`~.approval_store.PostgresApprovalStore`.
"""

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime

from paw_backend.tools.approval_types import (
    CONSUME_TASK_REFUSAL,
    OPEN_TASK_REFUSAL,
    ApprovalBinding,
    ApprovalEventKind,
    ApprovalHistoryEntry,
    ApprovalRecord,
    ApprovalStatus,
    ConsumeOutcome,
    DecideOutcome,
    DecideResult,
    NewApproval,
    OpenLimits,
    OpenOutcome,
    OpenResult,
    RevokeOutcome,
    SummaryItem,
    diagnose_consume,
    diagnose_decide,
    diagnose_revoke,
    is_expired,
)
from paw_backend.tools.task_state import TaskActivity, TaskActivityProvider

_OPEN = (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)


class InMemoryApprovalStore:
    """``task_activity`` is what ``open_request`` / ``consume`` with
    ``require_active_task=True`` ask about the task and the **run** of the
    request or binding (``check(task_id, run)``); like PostgreSQL, ``open_request``
    then revokes the task's open approvals of any other run. Without a provider
    the flag cannot be honoured (this double has no tasks): the broker's own
    check before the use is then the only one."""

    def __init__(
        self,
        *,
        max_records: int = 10_000,
        task_activity: TaskActivityProvider | None = None,
    ) -> None:
        self._task_activity = task_activity
        self._records: dict[uuid.UUID, ApprovalRecord] = {}
        self._history: list[ApprovalHistoryEntry] = []
        self._lock = asyncio.Lock()
        self._max_records = max_records

    def _log(
        self,
        approval_id: uuid.UUID,
        kind: ApprovalEventKind,
        now: datetime,
        *,
        actor_user_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        summary: tuple[SummaryItem, ...] | None = None,
    ) -> None:
        self._history.append(
            ApprovalHistoryEntry(
                seq=len(self._history) + 1,
                approval_id=approval_id,
                kind=kind,
                actor_user_id=actor_user_id,
                agent_id=agent_id,
                occurred_at=now,
                summary=summary,
            )
        )

    def _expire(self, record: ApprovalRecord, now: datetime) -> ApprovalRecord:
        """Mark an open, timed-out approval ``expired`` (with history)."""
        if record.status in _OPEN and is_expired(record, now):
            record = replace(record, status=ApprovalStatus.EXPIRED)
            self._records[record.approval_id] = record
            self._log(record.approval_id, ApprovalEventKind.EXPIRED, now)
        return record

    async def open_request(
        self,
        new: NewApproval,
        *,
        now: datetime,
        limits: OpenLimits,
        require_active_task: bool = False,
    ) -> OpenResult:
        async with self._lock:
            if require_active_task and self._task_activity is not None:
                # Under the store's lock, like the check of ``consume``.
                activity = await self._task_activity.check(new.task_id, new.task_run)
                if activity is not TaskActivity.ACTIVE:
                    return OpenResult(OPEN_TASK_REFUSAL[activity])
                # ``new.task_run`` is the task's current run: any other is an
                # earlier one, which nothing can use any more (as PostgreSQL).
                for record in list(self._records.values()):
                    if (
                        record.task_id == new.task_id
                        and record.task_run != new.task_run
                        and record.status in _OPEN
                        and not is_expired(record, now)
                    ):
                        self._records[record.approval_id] = replace(
                            record,
                            status=ApprovalStatus.REVOKED,
                            revoked_at=now,
                            revoked_by=None,
                        )
                        self._log(record.approval_id, ApprovalEventKind.REVOKED, now)
            for record in list(self._records.values()):
                if record.call_hash != new.call_hash:
                    continue
                record = self._expire(record, now)
                if record.status in _OPEN:
                    return OpenResult(OpenOutcome.EXISTING, record)
            cooldown_start = now - limits.rejection_cooldown
            if any(
                r.call_hash == new.call_hash
                and r.status is ApprovalStatus.REJECTED
                and r.decided_at is not None
                and r.decided_at > cooldown_start
                for r in self._records.values()
            ):
                return OpenResult(OpenOutcome.COOLING_DOWN)
            open_now = sum(
                1
                for r in self._records.values()
                if r.task_id == new.task_id
                and r.requester_user_id == new.requester_user_id
                and r.status in _OPEN
                and not is_expired(r, now)
            )
            if open_now >= limits.max_pending:
                return OpenResult(OpenOutcome.TOO_MANY_PENDING)
            if len(self._records) >= self._max_records:
                raise RuntimeError("the in-memory approval store is full")
            record = ApprovalRecord(
                approval_id=new.approval_id,
                task_id=new.task_id,
                task_run=new.task_run,
                project_id=new.project_id,
                agent_id=new.agent_id,
                requester_user_id=new.requester_user_id,
                tool=new.tool,
                level=new.level,
                call_hash=new.call_hash,
                targets=new.targets,
                summary=new.summary,
                status=ApprovalStatus.PENDING,
                created_at=now,
                expires_at=new.expires_at,
            )
            if record.expires_at <= record.created_at:
                raise ValueError("an approval must expire after it is created")
            self._records[record.approval_id] = record
            self._log(
                record.approval_id,
                ApprovalEventKind.REQUESTED,
                now,
                agent_id=new.agent_id,
                summary=new.summary,
            )
            return OpenResult(OpenOutcome.CREATED, record)

    async def get(self, approval_id: uuid.UUID) -> ApprovalRecord | None:
        return self._records.get(approval_id)

    async def decide(
        self,
        approval_id: uuid.UUID,
        *,
        approver_id: uuid.UUID,
        approve: bool,
        now: datetime,
        step_up_verified: bool = False,
    ) -> DecideResult:
        async with self._lock:
            record = self._records.get(approval_id)
            outcome = diagnose_decide(
                record,
                approver_id,
                now,
                approve=approve,
                step_up_verified=step_up_verified,
            )
            if record is None:
                return DecideResult(outcome)
            if outcome is DecideOutcome.EXPIRED:
                self._expire(record, now)
            if outcome is not DecideOutcome.DECIDED:
                return DecideResult(outcome)
            status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
            record = replace(
                record,
                status=status,
                approver_id=approver_id,
                decided_at=now,
                step_up_verified=approve and step_up_verified,
            )
            self._records[approval_id] = record
            kind = ApprovalEventKind.APPROVED if approve else ApprovalEventKind.REJECTED
            self._log(approval_id, kind, now, actor_user_id=approver_id)
            return DecideResult(DecideOutcome.DECIDED, record)

    async def consume(
        self,
        approval_id: uuid.UUID,
        binding: ApprovalBinding,
        *,
        now: datetime,
        require_active_task: bool = False,
    ) -> ConsumeOutcome:
        async with self._lock:
            record = self._records.get(approval_id)
            outcome = diagnose_consume(record, binding, now)
            if record is None:
                return outcome
            if outcome is ConsumeOutcome.EXPIRED:
                self._expire(record, now)
            if outcome not in (ConsumeOutcome.CONSUMED, ConsumeOutcome.SUPERSEDED):
                return outcome
            if require_active_task and self._task_activity is not None:
                # Under the store's lock, so no other use interleaves. (A double
                # has no task store to lock, which PostgreSQL does.) The task
                # is why an approval of another run is not used, when it is.
                activity = await self._task_activity.check(
                    binding.task_id, binding.task_run
                )
                if activity is not TaskActivity.ACTIVE:
                    return CONSUME_TASK_REFUSAL[activity]
            if outcome is not ConsumeOutcome.CONSUMED:
                return outcome
            self._records[approval_id] = replace(
                record, status=ApprovalStatus.CONSUMED, consumed_at=now
            )
            self._log(
                approval_id,
                ApprovalEventKind.CONSUMED,
                now,
                agent_id=binding.agent_id,
            )
            return ConsumeOutcome.CONSUMED

    async def revoke(
        self, approval_id: uuid.UUID, *, actor_id: uuid.UUID | None, now: datetime
    ) -> RevokeOutcome:
        async with self._lock:
            record = self._records.get(approval_id)
            outcome = diagnose_revoke(record, now)
            if record is None or outcome is not RevokeOutcome.REVOKED:
                return outcome
            self._records[approval_id] = replace(
                record,
                status=ApprovalStatus.REVOKED,
                revoked_at=now,
                revoked_by=actor_id,
            )
            self._log(
                approval_id, ApprovalEventKind.REVOKED, now, actor_user_id=actor_id
            )
            return RevokeOutcome.REVOKED

    async def revoke_task(
        self, task_id: uuid.UUID, *, now: datetime
    ) -> list[uuid.UUID]:
        async with self._lock:
            revoked: list[uuid.UUID] = []
            for record in list(self._records.values()):
                if record.task_id == task_id and diagnose_revoke(record, now) is (
                    RevokeOutcome.REVOKED
                ):
                    self._records[record.approval_id] = replace(
                        record,
                        status=ApprovalStatus.REVOKED,
                        revoked_at=now,
                        revoked_by=None,
                    )
                    self._log(record.approval_id, ApprovalEventKind.REVOKED, now)
                    revoked.append(record.approval_id)
            return revoked

    async def history(self, approval_id: uuid.UUID) -> list[ApprovalHistoryEntry]:
        return [h for h in self._history if h.approval_id == approval_id]
